"""Bookings CRUD + refund + balance-reminder."""
import asyncio
import logging
import uuid
import urllib.parse
from datetime import datetime, timezone

import stripe
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_admin
from db import db, get_settings, STRIPE_API_KEY
from email_helpers import (
    send_email_async, 
    email_balance_reminder_html, 
    email_booking_confirmation_html, 
    email_cancellation_html,
    email_modification_confirmation_html,
    is_placeholder_email,
    _it_date
)
from models import BookingUpdate, RefundRequest, _validate_iso_date
from pricing import BLOCKING_STATUSES, compute_refund_amount

router = APIRouter()
stripe.api_key = STRIPE_API_KEY

@router.get("/admin/bookings")
async def list_bookings(admin=Depends(get_current_admin)):
    return await db.bookings.find({}, {'_id': 0}).sort('created_at', -1).to_list(10000)

@router.post("/admin/bookings/manual")
async def create_manual_booking(payload: dict, admin=Depends(get_current_admin)):
    """Crea una prenotazione manuale e invia email di conferma all'ospite."""
    try:
        total_price = float(payload.get('total_price', 0))
        deposit_amount = float(payload.get('deposit_amount', 0))

        # Estrazione corretta degli ospiti dal payload inviato dal front-end
        adults = int(payload.get('adults', 2))
        children = int(payload.get('children', 0))

        # Allineamento con la gestione politiche rimborsabili delle email
        is_refundable = bool(payload.get('is_refundable', True))

        guest_email = payload.get('guest_email', '').strip()
        placeholder_email = not guest_email or guest_email == ""

        if placeholder_email:
            guest_email = "manual@booking.com"

        check_in = payload.get('check_in')
        check_out = payload.get('check_out')

        if not check_in or not check_out:
            raise HTTPException(400, "Date check-in e check-out mancanti")
        try:
            _validate_iso_date(check_in)
            _validate_iso_date(check_out)
        except ValueError:
            raise HTTPException(400, "Date non valide (formato AAAA-MM-GG)")
        if check_out <= check_in:
            raise HTTPException(400, "La data di partenza deve essere successiva a quella di arrivo")

        # Controllo sovrapposizione date per nuove prenotazioni manuali
        existing_overlap = await db.bookings.find_one({
            "status": {"$in": BLOCKING_STATUSES},
            "check_in": {"$lt": check_out},
            "check_out": {"$gt": check_in}
        })
        if existing_overlap:
            raise HTTPException(
                400,
                f"Le date richieste si sovrappongono con la prenotazione di {existing_overlap.get('guest_name')} ({_it_date(existing_overlap.get('check_in'))} → {_it_date(existing_overlap.get('check_out'))})"
            )

        settings = await get_settings()

        booking_data = {
            "id": payload.get('id') or f"bk_{uuid.uuid4().hex[:12]}",
            "guest_name": payload.get('guest_name', 'Ospite Manuale'),
            "guest_email": guest_email,
            "guest_phone": payload.get('guest_phone', ''),
            "check_in": check_in,
            "check_out": check_out,
            "adults": adults,
            "children": children,
            "total_price": total_price,
            "deposit_amount": deposit_amount,
            "payment_choice": payload.get('payment_choice', 'full'),
            "is_refundable": is_refundable,
            "status": 'confirmed',
            "payment_status": payload.get('payment_status', 'unpaid'),
            "source": 'manual',
            "notes": payload.get('notes', ''),
            "consent_newsletter": False,
            "created_at": datetime.now(timezone.utc).isoformat()
        }

        await db.bookings.insert_one(booking_data)

        if "_id" in booking_data:
            del booking_data["_id"]

        if not placeholder_email:
            asyncio.create_task(send_email_async(
                guest_email,
                f"Prenotazione confermata — {settings.get('villa_name', 'Light Blue')}",
                email_booking_confirmation_html(booking_data, settings),
            ))
            logging.info(f"Email di conferma inviata per prenotazione manuale {booking_data['id']} a {guest_email}")
        else:
            logging.info(f"Prenotazione manuale {booking_data['id']} creata con email placeholder — nessuna conferma inviata.")

        return {
            "ok": True,
            "message": "Prenotazione creata con successo",
            "booking": booking_data,
            "email_sent": not placeholder_email,
        }

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Errore creazione manuale: {e}")
        raise HTTPException(500, f"Errore durante il salvataggio: {str(e)}")

@router.patch("/admin/bookings/{booking_id}")
async def update_booking(booking_id: str, updates: BookingUpdate, admin=Depends(get_current_admin)):
    decoded_id = urllib.parse.unquote(booking_id)
    patch = updates.model_dump(exclude_unset=True)

    if not patch:
        raise HTTPException(400, 'No fields to update')

    current_b = await db.bookings.find_one({'id': decoded_id})
    if not current_b and decoded_id != booking_id:
        current_b = await db.bookings.find_one({'id': booking_id})

    if current_b:
        new_check_in = patch.get('check_in', current_b.get('check_in'))
        new_check_out = patch.get('check_out', current_b.get('check_out'))
        new_status = patch.get('status', current_b.get('status'))
        
        if new_status in BLOCKING_STATUSES and new_check_in and new_check_out:
            existing_overlap = await db.bookings.find_one({
                "id": {"$ne": current_b['id']},
                "status": {"$in": BLOCKING_STATUSES},
                "check_in": {"$lt": new_check_out},
                "check_out": {"$gt": new_check_in}
            })
            if existing_overlap:
                raise HTTPException(
                    400,
                    f"Le date modificate si sovrappongono con la prenotazione di {existing_overlap.get('guest_name')} ({_it_date(existing_overlap.get('check_in'))} → {_it_date(existing_overlap.get('check_out'))})"
                )

    result = await db.bookings.update_one({'id': decoded_id}, {'$set': patch})
    if result.matched_count == 0 and decoded_id != booking_id:
        await db.bookings.update_one({'id': booking_id}, {'$set': patch})

    updated_booking = await db.bookings.find_one({'id': decoded_id}, {'_id': 0})
    if not updated_booking and decoded_id != booking_id:
        updated_booking = await db.bookings.find_one({'id': booking_id}, {'_id': 0})

    # L'email all'ospite parte solo se cambiano dati che lo riguardano (non per stato/pagamento)
    guest_relevant = {'check_in', 'check_out', 'guest_name', 'guest_email', 'is_refundable'}
    changed = current_b and any(k in patch and patch[k] != current_b.get(k) for k in guest_relevant)
    if updated_booking and changed and updated_booking.get('status') != 'cancelled':
        guest_email = updated_booking.get('guest_email', '').strip()
        if not is_placeholder_email(guest_email):
            settings = await get_settings()
            asyncio.create_task(send_email_async(
                guest_email,
                f"Modifica prenotazione — {settings.get('villa_name', 'Light Blue')}",
                email_modification_confirmation_html(updated_booking, settings),
            ))

    return updated_booking

@router.delete("/admin/bookings/{booking_id}")
async def delete_booking(booking_id: str, admin=Depends(get_current_admin)):
    """Archivia/Cancella una prenotazione dal pannello admin."""
    decoded_id = urllib.parse.unquote(booking_id)

    b = await db.bookings.find_one({'id': decoded_id}, {'_id': 0})
    if not b:
        raise HTTPException(404, 'Booking not found')

    if b.get('payment_status') in PAID_STATES and await _stripe_payment_intent(b):
        # pagata online: cancellarla senza rimborso lascerebbe l'ospite senza soldi né soggiorno
        raise HTTPException(409, 'Questa prenotazione è stata pagata online: per cancellarla usa '
                                 '"Cancella e rimborsa" dalla scheda della prenotazione.')
    if b.get('status') in ('pending', 'awaiting_payment'):
        # chiude la pagina di pagamento Stripe ancora aperta, così l'ospite non può più pagare
        from routers.payments import _expire_session
        tx = await db.payment_transactions.find_one({'booking_id': b['id']}, {'_id': 0})
        if tx and await _expire_session(tx['session_id']) in ('complete', 'unknown'):
            raise HTTPException(409, 'Il pagamento risulta completato o in verifica: ricarica la pagina e riprova tra poco.')

    settings = await get_settings()

    await db.bookings.update_one(
        {'id': decoded_id},
        {'$set': {
            'status': 'cancelled',
            'cancelled_at': datetime.now(timezone.utc).isoformat(),
        }},
    )

    # email di cortesia solo per prenotazioni confermate con un indirizzo vero
    send = b.get('status') == 'confirmed' and not is_placeholder_email(b.get('guest_email'))
    if send:
        asyncio.create_task(send_email_async(
            b['guest_email'].strip(),
            f"Prenotazione cancellata — {settings.get('villa_name', 'Light Blue')}",
            email_cancellation_html(b, settings),
        ))
    logging.info(f"Prenotazione {decoded_id} archiviata (email inviata: {send})")
    return {'ok': True, 'archived': True, 'email_sent': send}


PAID_STATES = ('deposit_paid', 'fully_paid')


async def _stripe_payment_intent(b: dict):
    """Pagamento Stripe da cui rimborsare (None se la prenotazione non è stata pagata online)."""
    tx = await db.payment_transactions.find_one({'booking_id': b['id'], 'payment_status': 'paid'}, {'_id': 0})
    return (tx or {}).get('payment_intent_id') or b.get('payment_intent_id')


def _paid_amount(b: dict) -> float:
    if b.get('payment_status') == 'deposit_paid':
        return float(b.get('deposit_amount') or 0)
    if b.get('payment_status') == 'fully_paid':
        return float(b.get('total_price') or 0)
    return 0.0


@router.get("/admin/bookings/{booking_id}/refund-preview")
async def refund_preview(booking_id: str, admin=Depends(get_current_admin)):
    """Quanto ha pagato l'ospite e quanto prevedono le condizioni, prima di cancellare e rimborsare."""
    b = await db.bookings.find_one({'id': urllib.parse.unquote(booking_id)}, {'_id': 0})
    if not b:
        raise HTTPException(404, 'Prenotazione non trovata')
    policy = compute_refund_amount(b)
    return {
        'paid': round(_paid_amount(b), 2),
        'suggested': policy['refund'],
        'reason': policy['reason'],
        'online': bool(await _stripe_payment_intent(b)),
        'already_refunded': b.get('payment_status') == 'refunded',
        'email': not is_placeholder_email(b.get('guest_email')),
    }


@router.post("/admin/bookings/{booking_id}/cancel-refund")
async def cancel_and_refund(booking_id: str, payload: RefundRequest, admin=Depends(get_current_admin)):
    decoded_id = urllib.parse.unquote(booking_id)
    b = await db.bookings.find_one({'id': decoded_id}, {'_id': 0})
    if not b:
        raise HTTPException(404, 'Prenotazione non trovata')
    if b.get('payment_status') == 'refunded' or b.get('refund_at'):
        raise HTTPException(409, 'Questa prenotazione è già stata rimborsata')
    paid = round(_paid_amount(b), 2)   # 0 se non pagata: si può solo cancellare, senza rimborso
    refund_amount = round(float(payload.amount), 2) if payload.amount is not None else compute_refund_amount(b)['refund']
    if refund_amount < 0 or refund_amount > paid:
        raise HTTPException(400, f'Importo non valido: puoi rimborsare da 0 a {paid:.2f} €')

    refund_id = None
    if refund_amount > 0:
        pi = await _stripe_payment_intent(b)
        if not pi:
            raise HTTPException(400, 'Pagamento non effettuato online: il rimborso va fatto a mano '
                                     '(poi segna la prenotazione come cancellata)')
        # "prenota" il rimborso: due clic ravvicinati non possono generarne due
        lock = await db.bookings.update_one({'id': decoded_id, 'refund_lock': {'$exists': False}},
                                            {'$set': {'refund_lock': datetime.now(timezone.utc).isoformat()}})
        if lock.modified_count == 0:
            raise HTTPException(409, 'Rimborso già in corso')
        try:
            refund = await asyncio.to_thread(
                stripe.Refund.create,
                payment_intent=pi,
                amount=int(round(refund_amount * 100)),
                reason='requested_by_customer',
                idempotency_key=f'refund-{decoded_id}',
            )
            refund_id = getattr(refund, 'id', None)
        except Exception as e:
            await db.bookings.update_one({'id': decoded_id}, {'$unset': {'refund_lock': ''}})
            logging.exception('Stripe refund failed')
            msg = getattr(e, 'user_message', None) or 'Stripe non ha accettato il rimborso'
            raise HTTPException(502, f'Rimborso non riuscito: {msg}')

    now = datetime.now(timezone.utc).isoformat()
    await db.bookings.update_one(
        {'id': decoded_id},
        {'$set': {
            'status': 'cancelled',
            'cancelled_at': b.get('cancelled_at') or now,
            'payment_status': 'refunded' if refund_amount > 0 else b.get('payment_status'),
            'refund_amount': refund_amount,
            'refund_id': refund_id,
            'refund_at': now,
            'refund_reason': (payload.reason or '').strip()[:500] or None,
        }},
    )

    email_sent = not is_placeholder_email(b.get('guest_email'))
    if email_sent:
        settings = await get_settings()
        asyncio.create_task(send_email_async(
            b['guest_email'].strip(),
            f"Prenotazione cancellata — {settings.get('villa_name', 'Light Blue')}",
            email_cancellation_html(b, settings, refund_amount=refund_amount if paid > 0 else None),
            ref_url=f"/admin/bookings?open={decoded_id}",
        ))
    return {'ok': True, 'refund_amount': refund_amount, 'email_sent': email_sent}

@router.post("/admin/bookings/{booking_id}/balance-reminder")
async def balance_reminder(booking_id: str, admin=Depends(get_current_admin)):
    decoded_id = urllib.parse.unquote(booking_id)

    b = await db.bookings.find_one({'id': decoded_id}, {'_id': 0})
    if not b:
        raise HTTPException(404, 'Booking not found')

    settings = await get_settings()

    ok = await send_email_async(
        b['guest_email'],
        f"Promemoria saldo — {settings.get('villa_name','Light Blue')}",
        email_balance_reminder_html(b, settings),
    )

    await db.bookings.update_one(
        {'id': decoded_id},
        {'$set': {'last_reminder_at': datetime.now(timezone.utc).isoformat()}},
    )

    return {'ok': ok}
