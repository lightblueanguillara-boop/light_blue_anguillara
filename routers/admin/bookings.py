"""Bookings CRUD + refund + balance-reminder."""
import asyncio
import logging
import uuid
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

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
    _it_date
)
from models import Booking, BookingUpdate, RefundRequest
from pricing import compute_refund_amount

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

        # Controllo sovrapposizione date per nuove prenotazioni manuali
        existing_overlap = await db.bookings.find_one({
            "status": {"$ne": "cancelled"},
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
        
        if new_status != 'cancelled' and new_check_in and new_check_out:
            existing_overlap = await db.bookings.find_one({
                "id": {"$ne": current_b['id']},
                "status": {"$ne": "cancelled"},
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

    if updated_booking and updated_booking.get('status') != 'cancelled':
        guest_email = updated_booking.get('guest_email', '').strip()
        is_placeholder = not guest_email or guest_email in ('', 'manual@booking.com')
        if not is_placeholder:
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

    settings = await get_settings()

    await db.bookings.update_one(
        {'id': decoded_id},
        {'$set': {
            'status': 'cancelled',
            'cancelled_at': datetime.now(timezone.utc).isoformat(),
        }},
    )

    guest_email = b.get('guest_email', '').strip()
    is_placeholder = not guest_email or guest_email in ('', 'manual@booking.com')

    # Se la prenotazione manuale non è pagata (unpaid) ma l'email è reale, 
    # permettiamo l'invio della notifica di cancellazione cortesia all'ospite.
    should_send_email = b.get('status') == 'confirmed'

    if not is_placeholder and should_send_email:
        asyncio.create_task(send_email_async(
            guest_email,
            f"Prenotazione cancellata — {settings.get('villa_name', 'Light Blue')}",
            email_cancellation_html(b, settings),
        ))
        logging.info(f"Email di cancellazione inviata per prenotazione {decoded_id} a {guest_email}")
    else:
        logging.info(f"Prenotazione {decoded_id} archiviata. Nessuna email inviata (Email valida: {not is_placeholder}, Stato valido: {should_send_email})")

    return {
        'ok': True,
        'archived': True,
        'email_sent': (not is_placeholder and should_send_email)
    }

@router.post("/admin/bookings/{booking_id}/cancel-refund")
async def cancel_and_refund(booking_id: str, payload: RefundRequest, admin=Depends(get_current_admin)):
    decoded_id = urllib.parse.unquote(booking_id)

    b = await db.bookings.find_one({'id': decoded_id}, {'_id': 0})
    if not b:
        raise HTTPException(404, 'Booking not found')

    policy_calc = compute_refund_amount(b)
    refund_amount = float(payload.amount) if payload.amount is not None else policy_calc['refund']

    if refund_amount > 0:
        tx = await db.payment_transactions.find_one({'booking_id': decoded_id, 'payment_status': 'paid'}, {'_id': 0})
        pi = (tx or {}).get('payment_intent_id') or b.get('payment_intent_id')

        if not pi:
            raise HTTPException(400, 'Payment intent non disponibile per il rimborso Stripe')

        try:
            await asyncio.to_thread(
                stripe.Refund.create,
                payment_intent=pi,
                amount=int(round(refund_amount * 100)),
                reason='requested_by_customer',
            )
        except Exception as e:
            logging.exception('Stripe refund failed')
            raise HTTPException(500, f'Rimborso fallito: {e}')

    await db.bookings.update_one(
        {'id': decoded_id},
        {'$set': {
            'status': 'cancelled',
            'payment_status': 'refunded' if refund_amount > 0 else b.get('payment_status', 'unpaid'),
            'refund_amount': refund_amount,
            'refund_at': datetime.now(timezone.utc).isoformat(),
        }},
    )

    return {'ok': True, 'refund_amount': refund_amount}

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
