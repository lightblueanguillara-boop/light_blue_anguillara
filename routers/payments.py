"""Stripe payments: booking checkout, status polling, webhook, and cleanup."""
import asyncio
import html
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import stripe
from fastapi import APIRouter, HTTPException, Request, BackgroundTasks

from db import db, get_settings, STRIPE_API_KEY, STRIPE_WEBHOOK_SECRET, ADMIN_NOTIFY_EMAIL, FRONTEND_URL
from email_helpers import send_email_async, email_booking_confirmation_html
from models import Booking, BookingCreate, PaymentTransaction, Subscriber
from pricing import BLOCKING_STATUSES, compute_dual_pricing, dates_available
from notifications import notify
from ratelimit import rate_limit

router = APIRouter()
stripe.api_key = STRIPE_API_KEY

# Durata della pagina di pagamento Stripe (minimo consentito da Stripe: 30 minuti).
CHECKOUT_MINUTES = 30
# Le prenotazioni "pending" vengono ripulite solo DOPO la scadenza della sessione Stripe,
# così un ospite che paga all'ultimo minuto non perde mai la prenotazione.
CLEANUP_AFTER_MINUTES = CHECKOUT_MINUTES + 5


def _allowed_origins() -> list:
    raw = os.environ.get('CORS_ORIGINS', '*')
    return [o.strip().rstrip('/') for o in raw.split(',') if o.strip()]


def _return_base_url(origin_url: str) -> str:
    """URL a cui Stripe rimanda l'ospite: solo domini nostri, mai un URL arbitrario."""
    origin = (origin_url or '').rstrip('/')
    allowed = _allowed_origins()
    if origin and ('*' in allowed or origin in allowed):
        return origin
    return FRONTEND_URL


def _obj_get(obj, key):
    """Legge un campo sia da StripeObject sia da dict."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


# --- PAGAMENTO RIUSCITO -----------------------------------------------------

async def _mark_paid(session_id: str, payment_intent: Optional[str]):
    """Segna come pagata la transazione e conferma la prenotazione (idempotente)."""
    tx = await db.payment_transactions.find_one({'session_id': session_id}, {'_id': 0})
    if not tx:
        logging.error(f'Pagamento ricevuto per sessione sconosciuta {session_id}')
        return
    # aggiornamento atomico: solo il primo chiamante (webhook o polling) procede
    res = await db.payment_transactions.update_one(
        {'session_id': session_id, 'payment_status': {'$ne': 'paid'}},
        {'$set': {'payment_status': 'paid', 'status': 'complete', 'payment_intent_id': payment_intent}},
    )
    if res.modified_count == 0:
        return  # già gestito

    choice = (tx.get('metadata') or {}).get('payment_choice', 'deposit')
    payment = {'payment_status': 'fully_paid' if choice == 'full' else 'deposit_paid'}
    if payment_intent:
        payment['payment_intent_id'] = payment_intent
    settings = await get_settings()
    current = await db.bookings.find_one({'id': tx['booking_id']}, {'_id': 0})

    if current and not await _can_confirm(current):
        # Prenotazione già chiusa (cancellata, rifiutata, o scaduta con date ormai occupate):
        # NON va riattivata, altrimenti si rischia una doppia prenotazione. Registriamo il
        # pagamento e avvisiamo subito il proprietario, che potrà rimborsare dal pannello.
        await _record_payment_on_closed(current, payment, tx)
        return

    # conferma solo se nel frattempo lo stato non è cambiato (es. cancellata in quel momento)
    query = {'id': tx['booking_id']}
    if current:
        query['status'] = current.get('status')
    r = await db.bookings.update_one(query, {'$set': {'status': 'confirmed', **payment}})
    if current and r.matched_count == 0:
        latest = await db.bookings.find_one({'id': tx['booking_id']}, {'_id': 0})
        if latest:
            await _record_payment_on_closed(latest, payment, tx)
            return

    if r.matched_count == 0:
        # Non dovrebbe più succedere, ma se succede il gestore DEVE saperlo subito.
        logging.critical(f"Pagamento incassato ma prenotazione {tx['booking_id']} assente (sessione {session_id})")
        await notify('payment_orphan', amount=tx.get('amount'), tag=session_id)
        if ADMIN_NOTIFY_EMAIL:
            asyncio.create_task(send_email_async(
                ADMIN_NOTIFY_EMAIL,
                '[Light Blue] ATTENZIONE: pagamento senza prenotazione',
                f"<p>Stripe ha incassato la sessione <b>{session_id}</b> ma la prenotazione "
                f"<b>{tx['booking_id']}</b> non esiste più. Verifica su Stripe e contatta l'ospite.</p>",
            ))
        return

    booking = await db.bookings.find_one({'id': tx['booking_id']}, {'_id': 0})
    if booking:
        asyncio.create_task(notify('booking_confirmed', booking=booking))
        asyncio.create_task(send_email_async(
            booking['guest_email'],
            f"Prenotazione confermata — {settings.get('villa_name', 'Light Blue')}",
            email_booking_confirmation_html(booking, settings),
            ref_url=f"/admin/bookings?open={booking['id']}",
        ))


async def _can_confirm(booking: dict) -> bool:
    """Un pagamento può confermare la prenotazione solo se è ancora in attesa di pagamento,
    oppure se è scaduta da poco e nel frattempo le date sono rimaste libere."""
    status = booking.get('status')
    if status in ('pending', 'awaiting_payment', 'confirmed'):
        return True
    if status == 'expired':
        return await dates_available(booking['check_in'], booking['check_out'], exclude_id=booking['id'])
    return False


async def _record_payment_on_closed(booking: dict, payment: dict, tx: dict) -> None:
    """Pagamento arrivato su una prenotazione chiusa: lo registra senza riattivarla e avvisa subito."""
    await db.bookings.update_one({'id': booking['id']}, {'$set': {**payment, 'paid_while_closed': True}})
    logging.critical(f"Pagamento ricevuto su prenotazione chiusa {booking['id']} (stato {booking.get('status')})")
    await notify('payment_on_closed', booking=booking, amount=tx.get('amount'))
    if ADMIN_NOTIFY_EMAIL:
        asyncio.create_task(send_email_async(
            ADMIN_NOTIFY_EMAIL,
            '[Light Blue] ATTENZIONE: pagamento su prenotazione chiusa',
            f"<p>{html.escape(booking.get('guest_name') or 'Un ospite')} ha pagato <b>{tx.get('amount')} €</b> "
            f"per il soggiorno dal {booking.get('check_in')} al {booking.get('check_out')}, ma la prenotazione "
            f"era già chiusa (stato: {html.escape(str(booking.get('status')))}).</p>"
            "<p>La prenotazione <b>non</b> è stata riattivata. Aprila nel pannello e, se le date non sono più "
            "disponibili, usa <i>Cancella e rimborsa</i>.</p>",
        ))


async def _expire_session(session_id: str) -> str:
    """Chiude la pagina di pagamento Stripe. Ritorna lo stato finale ('expired', 'complete', ...)."""
    try:
        s = await asyncio.to_thread(stripe.checkout.Session.expire, session_id)
        return _obj_get(s, 'status') or 'expired'
    except Exception:
        # già scaduta o già pagata: leggiamo lo stato reale
        try:
            s = await asyncio.to_thread(stripe.checkout.Session.retrieve, session_id)
            if _obj_get(s, 'payment_status') == 'paid':
                await _mark_paid(session_id, _obj_get(s, 'payment_intent'))
                return 'complete'
            return _obj_get(s, 'status') or 'unknown'
        except Exception as e:
            logging.error(f'Impossibile verificare la sessione Stripe {session_id}: {e}')
            return 'unknown'


async def _release_pending_booking(booking_id: str) -> bool:
    """Elimina una prenotazione dal sito non pagata, dopo aver chiuso la sessione Stripe."""
    tx = await db.payment_transactions.find_one({'booking_id': booking_id}, {'_id': 0})
    if tx:
        status = await _expire_session(tx['session_id'])
        if status in ('complete', 'unknown'):
            # pagata, oppure Stripe non raggiungibile: non cancelliamo nulla
            return False
    res = await db.bookings.delete_one({
        'id': booking_id, 'status': 'pending', 'payment_status': 'unpaid', 'source': 'website',
    })
    if tx:
        await db.payment_transactions.update_one(
            {'session_id': tx['session_id']}, {'$set': {'status': 'expired'}}
        )
    return res.deleted_count > 0


# --- CLEANUP ------------------------------------------------------------------

async def cleanup_expired_bookings():
    """
    Rimuove le prenotazioni PENDING provenienti dal SITO abbandonate (sessione Stripe scaduta).
    NON tocca le prenotazioni manuali o esterne.
    """
    try:
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(minutes=CLEANUP_AFTER_MINUTES)).isoformat()
        expired = await db.bookings.find({
            'status': 'pending',
            'payment_status': 'unpaid',
            'source': 'website',
            'created_at': {'$lt': cutoff_iso},
        }, {'_id': 0, 'id': 1}).to_list(length=100)
        for b in expired:
            if await _release_pending_booking(b['id']):
                logging.info(f"Cleanup: rimossa prenotazione abbandonata {b['id']}")
    except Exception as e:
        logging.error(f"Cleanup Error: {e}")


async def create_checkout_session(booking: dict, amount: float, payment_choice: str,
                                  return_base: str, expires_ts: int, settings: dict):
    """Crea la pagina di pagamento Stripe per una prenotazione e registra la transazione."""
    session = await asyncio.to_thread(
        stripe.checkout.Session.create,
        payment_method_types=['card'],
        line_items=[{
            'price_data': {
                'currency': 'eur',
                'product_data': {'name': f"Soggiorno presso {settings.get('villa_name', 'Light Blue')} "
                                         f"({booking['check_in']} → {booking['check_out']})"},
                'unit_amount': int(round(amount * 100)),
            },
            'quantity': 1,
        }],
        mode='payment',
        customer_email=booking['guest_email'],
        expires_at=expires_ts,
        success_url=f"{return_base}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{return_base}/payment/cancel?session_id={{CHECKOUT_SESSION_ID}}",
        metadata={'booking_id': booking['id'], 'payment_choice': payment_choice},
    )
    tx = PaymentTransaction(
        session_id=session.id,
        booking_id=booking['id'],
        amount=amount,
        payment_status='initiated',
        status='open',
        metadata={'payment_choice': payment_choice, 'booking_id': booking['id']},
    )
    await db.payment_transactions.insert_one(tx.model_dump())
    return session


# --- ROUTES -------------------------------------------------------------------

@router.post("/bookings/checkout")
async def create_booking_checkout(payload: BookingCreate, request: Request, background_tasks: BackgroundTasks):
    rate_limit(request, 'checkout', 10, 10 * 60)
    background_tasks.add_task(cleanup_expired_bookings)
    if (await get_settings()).get('booking_mode', 'request') != 'instant':
        raise HTTPException(403, 'Le prenotazioni online avvengono su richiesta: invia una richiesta di prenotazione.')

    if not await dates_available(payload.check_in, payload.check_out):
        raise HTTPException(409, 'Date non disponibili')

    pricing = await compute_dual_pricing(payload.check_in, payload.check_out)
    settings = await get_settings()
    deposit_pct = settings.get('deposit_percent', 30.0)

    base_price = pricing['refundable_total'] if payload.is_refundable else pricing['non_refundable_total']
    deposit_amount = round(base_price * deposit_pct / 100.0, 2)
    amount = base_price if payload.payment_choice == 'full' else deposit_amount
    amount_cents = int(round(amount * 100))
    if amount_cents < 50:
        raise HTTPException(400, 'Importo non valido')

    booking = Booking(
        guest_name=payload.guest_name.strip(),
        guest_email=payload.guest_email,
        guest_phone=payload.guest_phone,
        check_in=payload.check_in,
        check_out=payload.check_out,
        adults=payload.adults,
        children=payload.children,
        total_price=base_price,
        deposit_amount=deposit_amount,
        payment_choice=payload.payment_choice,
        is_refundable=payload.is_refundable,
        status='pending',
        payment_status='unpaid',
        source='website',
        notes=payload.notes,
        consent_newsletter=payload.consent_newsletter,
        terms_accepted_at=datetime.now(timezone.utc).isoformat(),
    )
    await db.bookings.insert_one(booking.model_dump())

    # Protezione da doppia prenotazione simultanea: se nel frattempo un'altra richiesta
    # ha occupato le stesse date, vince la prima e questa viene annullata.
    rivals = await db.bookings.find({
        'id': {'$ne': booking.id},
        'status': {'$in': BLOCKING_STATUSES},
        'check_in': {'$lt': booking.check_out},
        'check_out': {'$gt': booking.check_in},
    }, {'_id': 0, 'created_at': 1}).to_list(10)
    if any(r.get('created_at', '') <= booking.created_at for r in rivals):
        await db.bookings.delete_one({'id': booking.id})
        raise HTTPException(409, 'Date non disponibili')

    host_url = _return_base_url(payload.origin_url)
    expires_ts = int((datetime.now(timezone.utc) + timedelta(minutes=CHECKOUT_MINUTES, seconds=30)).timestamp())
    try:
        session = await create_checkout_session(booking.model_dump(), amount, payload.payment_choice, host_url, expires_ts, settings)
    except Exception:
        logging.exception('Stripe session create failed')
        await db.bookings.delete_one({'id': booking.id})
        # il dettaglio tecnico resta nei log: all'ospite un messaggio generico
        raise HTTPException(502, 'Il pagamento non è al momento disponibile. Riprova tra qualche minuto o contattaci.')

    # newsletter solo dopo che la sessione di pagamento è stata creata
    if payload.consent_newsletter:
        existing = await db.subscribers.find_one({'email': payload.guest_email})
        if not existing:
            sub = Subscriber(email=payload.guest_email, name=payload.guest_name, source='booking')
            await db.subscribers.insert_one(sub.model_dump())
        # nuovo consenso esplicito: annulla un'eventuale disiscrizione precedente
        await db.unsubscribed.delete_one({'email': payload.guest_email.lower()})

    return {'url': session.url, 'session_id': session.id, 'booking_id': booking.id, 'amount': amount}


@router.delete("/bookings/checkout/{session_id}")
async def cancel_booking_checkout(session_id: str):
    """
    Chiamato dal frontend quando l'utente annulla il pagamento su Stripe.
    Chiude la sessione Stripe (così non è più pagabile) e libera subito le date.
    """
    tx = await db.payment_transactions.find_one({'session_id': session_id}, {'_id': 0})
    if not tx:
        return {'cancelled': False, 'reason': 'transaction not found'}
    booking = await db.bookings.find_one({'id': tx['booking_id']}, {'_id': 0})
    if not booking:
        return {'cancelled': False, 'reason': 'booking not found'}
    if booking.get('status') != 'pending' or booking.get('payment_status') != 'unpaid':
        return {'cancelled': False, 'reason': 'booking already processed'}
    if booking.get('source') != 'website':
        return {'cancelled': False, 'reason': 'not a website booking'}

    if await _release_pending_booking(tx['booking_id']):
        logging.info(f"Cancel checkout: rimossa prenotazione {tx['booking_id']} (session {session_id})")
        return {'cancelled': True}
    return {'cancelled': False, 'reason': 'payment in progress or completed'}


@router.get("/payments/status/{session_id}")
async def payment_status(session_id: str):
    tx = await db.payment_transactions.find_one({'session_id': session_id}, {'_id': 0})
    if not tx:
        raise HTTPException(404, 'Transaction not found')
    if tx.get('payment_status') == 'paid':
        return {'payment_status': 'paid', 'status': 'complete', 'booking_id': tx.get('booking_id')}

    try:
        session = await asyncio.to_thread(stripe.checkout.Session.retrieve, session_id)
    except Exception as e:
        logging.warning(f'Payment status check failed for {session_id}: {e}')
        return {'payment_status': tx.get('payment_status'), 'status': tx.get('status'), 'booking_id': tx.get('booking_id')}

    paid = _obj_get(session, 'payment_status') == 'paid'
    status = _obj_get(session, 'status')
    if paid:
        await _mark_paid(session_id, _obj_get(session, 'payment_intent'))
    else:
        await db.payment_transactions.update_one({'session_id': session_id}, {'$set': {'status': status}})
    return {'payment_status': 'paid' if paid else 'unpaid', 'status': status, 'booking_id': tx.get('booking_id')}


@router.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    if not STRIPE_WEBHOOK_SECRET:
        # Senza segreto non possiamo verificare che la richiesta arrivi davvero da Stripe.
        logging.error('Webhook Stripe ricevuto ma STRIPE_WEBHOOK_SECRET non è configurato: ignorato')
        raise HTTPException(503, 'Webhook non configurato')

    body = await request.body()
    sig = request.headers.get('Stripe-Signature', '')
    try:
        event = stripe.Webhook.construct_event(body, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(400, 'Firma webhook non valida')

    etype, data = event.type, event.data.object
    try:
        if etype in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
            if _obj_get(data, 'payment_status') == 'paid':
                await _mark_paid(_obj_get(data, 'id'), _obj_get(data, 'payment_intent'))
        elif etype == 'checkout.session.expired':
            tx = await db.payment_transactions.find_one({'session_id': _obj_get(data, 'id')}, {'_id': 0})
            if tx:
                from routers.booking_requests import expire_awaiting_payment
                await expire_awaiting_payment(tx['booking_id'])
                await db.bookings.delete_one({
                    'id': tx['booking_id'], 'status': 'pending', 'payment_status': 'unpaid', 'source': 'website',
                })
                await db.payment_transactions.update_one(
                    {'session_id': tx['session_id']}, {'$set': {'status': 'expired'}}
                )
    except Exception:
        logging.exception('Stripe webhook processing error')
        raise HTTPException(500, 'Errore elaborazione webhook')  # Stripe ritenterà
    return {'received': True}
