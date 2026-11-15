"""Prenotazione su richiesta: l'ospite invia una richiesta, il proprietario accetta o rifiuta.

Flusso:
  request ──(accetta)──> awaiting_payment ──(paga dal link)──> confirmed
     │                        └──(scade il link)──> expired
     ├──(rifiuta)──> rejected
     └──(nessuna risposta entro request_hold_hours)──> expired
Le date restano bloccate negli stati `request` e `awaiting_payment`.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import get_current_admin
from db import db, get_settings, FRONTEND_URL
import messaging
import whatsapp_queue
from models import Booking, BookingRequestCreate, RejectRequest, Subscriber
from pricing import BLOCKING_STATUSES, compute_dual_pricing, dates_available
from notifications import notify
from ratelimit import rate_limit

router = APIRouter()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _in_background(coro):
    """Email e WhatsApp in sottofondo: la risposta all'ospite non aspetta il servizio email."""
    asyncio.create_task(coro)


# --- PUBBLICO -----------------------------------------------------------------

@router.post("/bookings/request")
async def create_booking_request(payload: BookingRequestCreate, request: Request):
    rate_limit(request, 'booking-request', 5, 3600)
    settings = await get_settings()
    if settings.get('booking_mode', 'request') != 'request':
        raise HTTPException(403, 'Le prenotazioni avvengono con pagamento immediato.')
    if not await dates_available(payload.check_in, payload.check_out):
        raise HTTPException(409, 'Date non disponibili')

    pricing = await compute_dual_pricing(payload.check_in, payload.check_out)
    total = pricing['refundable_total'] if payload.is_refundable else pricing['non_refundable_total']
    hold_hours = int(settings.get('request_hold_hours', 24))

    booking = Booking(
        guest_name=payload.guest_name.strip(),
        guest_email=payload.guest_email,
        guest_phone=payload.guest_phone.strip(),
        check_in=payload.check_in,
        check_out=payload.check_out,
        adults=payload.adults,
        children=payload.children,
        total_price=total,
        deposit_amount=round(total * settings.get('deposit_percent', 30.0) / 100.0, 2),
        payment_choice='full',
        is_refundable=payload.is_refundable,
        status='request',
        payment_status='unpaid',
        source='website',
        notes=payload.notes,
        consent_newsletter=payload.consent_newsletter,
        contact_whatsapp=payload.contact_whatsapp,
        terms_accepted_at=_now().isoformat(),
        request_expires_at=(_now() + timedelta(hours=hold_hours)).isoformat(),
    )
    await db.bookings.insert_one(booking.model_dump())

    # due richieste simultanee sulle stesse date: vince la prima
    rivals = await db.bookings.find({
        'id': {'$ne': booking.id},
        'status': {'$in': BLOCKING_STATUSES},
        'check_in': {'$lt': booking.check_out},
        'check_out': {'$gt': booking.check_in},
    }, {'_id': 0, 'created_at': 1}).to_list(10)
    if any(r.get('created_at', '') <= booking.created_at for r in rivals):
        await db.bookings.delete_one({'id': booking.id})
        raise HTTPException(409, 'Date non disponibili')

    if payload.consent_newsletter:
        if not await db.subscribers.find_one({'email': payload.guest_email}):
            await db.subscribers.insert_one(
                Subscriber(email=payload.guest_email, name=payload.guest_name, source='booking_request').model_dump())
        await db.unsubscribed.delete_one({'email': payload.guest_email.lower()})

    b = booking.model_dump()
    _in_background(messaging.invia('richiesta_ricevuta', b, settings=settings, whatsapp=False))
    _in_background(messaging.notify_owner('gestore_nuova_richiesta', messaging.booking_vars(b, settings),
                                          booking=b, settings=settings, reply_to=b['guest_email']))
    asyncio.create_task(notify('new_request', booking=b))
    return {'ok': True, 'booking_id': booking.id, 'request_expires_at': booking.request_expires_at,
            'total_price': total, 'hold_hours': hold_hours}


# --- ADMIN --------------------------------------------------------------------

async def _get_booking(booking_id: str) -> dict:
    b = await db.bookings.find_one({'id': booking_id}, {'_id': 0})
    if not b:
        raise HTTPException(404, 'Prenotazione non trovata')
    return b


@router.post("/admin/bookings/{booking_id}/approve")
async def approve_request(booking_id: str, admin=Depends(get_current_admin)):
    from routers.payments import create_checkout_session  # import locale: evita import circolare

    b = await _get_booking(booking_id)
    if b.get('status') != 'request':
        raise HTTPException(409, 'Questa richiesta non è più in attesa di risposta')
    if not await dates_available(b['check_in'], b['check_out'], exclude_id=b['id']):
        raise HTTPException(409, 'Le date non sono più disponibili (sovrapposizione con un\'altra prenotazione)')

    settings = await get_settings()
    hours = max(1, min(24, int(settings.get('payment_link_hours', 24))))
    due = _now() + timedelta(hours=hours) - timedelta(minutes=1)  # Stripe: massimo 24 ore
    try:
        session = await create_checkout_session(
            b, float(b['total_price']), 'full', FRONTEND_URL, int(due.timestamp()), settings)
    except Exception:
        logging.exception('Stripe session create failed (approve)')
        raise HTTPException(502, 'Impossibile creare il link di pagamento Stripe. Riprova tra poco.')

    update = {
        'status': 'awaiting_payment',
        'payment_due_at': due.isoformat(),
        'payment_url': session.url,
        'decided_at': _now().isoformat(),
    }
    await db.bookings.update_one({'id': b['id'], 'status': 'request'}, {'$set': update})
    b.update(update)
    sent = await messaging.invia('richiesta_accettata', b, settings=settings)
    return {'ok': True, 'booking': b, 'email_sent': sent['email'], 'whatsapp_id': sent['whatsapp_id']}


@router.post("/admin/bookings/{booking_id}/reject")
async def reject_request(booking_id: str, payload: RejectRequest, admin=Depends(get_current_admin)):
    from routers.payments import _expire_session

    b = await _get_booking(booking_id)
    if b.get('status') not in ('request', 'awaiting_payment'):
        raise HTTPException(409, 'Questa richiesta non può più essere rifiutata')
    if b.get('status') == 'awaiting_payment':
        tx = await db.payment_transactions.find_one({'booking_id': b['id']}, {'_id': 0})
        if tx and await _expire_session(tx['session_id']) in ('complete', 'unknown'):
            raise HTTPException(409, 'Il pagamento risulta completato o in verifica: ricarica la pagina; se è stato pagato, usa "Cancella e rimborsa" dalla scheda della prenotazione')

    settings = await get_settings()
    update = {
        'status': 'rejected',
        'rejection_reason': (payload.reason or '').strip() or None,
        'decided_at': _now().isoformat(),
    }
    await db.bookings.update_one({'id': b['id']}, {'$set': update})
    b.update(update)
    # il messaggio "richiesta accettata" ancora in coda non ha più senso
    await whatsapp_queue.cancel_for_booking(b['id'], keys=['richiesta_accettata'])
    sent = await messaging.invia('richiesta_rifiutata', b, settings=settings)
    return {'ok': True, 'booking': b, 'email_sent': sent['email'], 'whatsapp_id': sent['whatsapp_id']}


# --- SCADENZE -----------------------------------------------------------------

async def expire_awaiting_payment(booking_id: str):
    """Richiesta accettata ma non pagata in tempo: chiude il link e libera le date."""
    b = await db.bookings.find_one({'id': booking_id, 'status': 'awaiting_payment'}, {'_id': 0})
    if not b:
        return
    res = await db.bookings.update_one(
        {'id': booking_id, 'status': 'awaiting_payment', 'payment_status': 'unpaid'},
        {'$set': {'status': 'expired', 'decided_at': _now().isoformat()}},
    )
    if res.modified_count:
        await whatsapp_queue.cancel_for_booking(booking_id, keys=['richiesta_accettata'])
        _in_background(messaging.invia('pagamento_scaduto', b, whatsapp=False))
        await notify('payment_expired', booking=b)


async def expire_requests():
    """Chiamata periodicamente: chiude richieste senza risposta e link di pagamento scaduti."""
    from routers.payments import _expire_session

    now_iso = _now().isoformat()
    settings = None

    # 1) richieste a cui il proprietario non ha risposto in tempo
    for b in await db.bookings.find(
            {'status': 'request', 'request_expires_at': {'$lt': now_iso}}, {'_id': 0}).to_list(200):
        res = await db.bookings.update_one(
            {'id': b['id'], 'status': 'request'},
            {'$set': {'status': 'expired', 'decided_at': now_iso}},
        )
        if res.modified_count:
            settings = settings or await get_settings()
            _in_background(messaging.invia('richiesta_scaduta', b, settings=settings, whatsapp=False))
            logging.info(f"Richiesta {b['id']} scaduta senza risposta")
            await notify('request_expired', booking=b)

    # 2) richieste accettate ma non pagate entro la scadenza
    for b in await db.bookings.find(
            {'status': 'awaiting_payment', 'payment_due_at': {'$lt': now_iso}}, {'_id': 0}).to_list(200):
        tx = await db.payment_transactions.find_one({'booking_id': b['id']}, {'_id': 0})
        if tx and await _expire_session(tx['session_id']) in ('complete', 'unknown'):
            continue  # pagata all'ultimo momento (già confermata) o Stripe non raggiungibile: riprova dopo
        await expire_awaiting_payment(b['id'])
        logging.info(f"Richiesta {b['id']} scaduta: pagamento non completato")
