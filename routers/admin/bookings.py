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

        # ESTRAZIONE DEL NUOVO CAMPO IS_REFUNDABLE DALLE IMPOSTAZIONI MANUALI
        is_refundable = bool(payload.get('is_refundable', True))

        settings = await get_settings()

        # Se l'acconto non è esplicitamente calcolato, usa la percentuale dei settings
        if deposit_amount <= 0:
            dep_pct = float(settings.get('deposit_percent', 30.0))
            deposit_amount = round((total_price * dep_pct) / 100.0, 2)

        new_booking = {
            "id": f"bk_{uuid.uuid4().hex[:12]}",
            "guest_name": payload.get('guest_name', 'Blocco Manuale'),
            "guest_email": payload.get('guest_email', 'admin@lightblue.it'),
            "guest_phone": payload.get('guest_phone'),
            "check_in": payload.get('check_in'),
            "check_out": payload.get('check_out'),
            "adults": adults,
            "children": children,
            "total_price": total_price,
            "deposit_amount": deposit_amount,
            "payment_choice": "deposit",
            "is_refundable": is_refundable,  # Salva la scelta impostata manualmente dall'admin
            "status": "confirmed",
            "payment_status": "unpaid",
            "source": payload.get('source', 'manual'),
            "notes": payload.get('notes', ''),
            "consent_newsletter": False,
            "created_at": datetime.now(timezone.utc).isoformat()
        }

        await db.bookings.insert_one(new_booking)

        # Rimuove l'identificatore MongoDB interno per la risposta ed evitare conflitti
        if '_id' in new_booking:
            del new_booking['_id']

        # Invio asincrono dell'email di conferma allineato alla scelta rimborsabile/non rimborsabile
        asyncio.create_task(
            send_email_async(
                new_booking['guest_email'],
                f"Conferma Prenotazione — {settings.get('villa_name','Light Blue')}",
                email_booking_confirmation_html(new_booking, settings)
            )
        )

        return new_booking

    except Exception as e:
        logging.exception("Error creating manual booking")
        raise HTTPException(status_code=400, detail=f"Errore creazione prenotazione: {str(e)}")

@router.post("/admin/bookings/{booking_id}/cancel")
async def admin_cancel_booking(booking_id: str, admin=Depends(get_current_admin)):
    """Cancella una prenotazione lato amministratore calcolando il rimborso esatto."""
    decoded_id = urllib.parse.unquote(booking_id)

    b = await db.bookings.find_one({'id': decoded_id})
    if not b:
        raise HTTPException(404, 'Booking not found')

    if b.get('status') == 'cancelled':
        return {'ok': True, 'refund_amount': b.get('refund_amount', 0.0), 'msg': 'Already cancelled'}

    # Calcolo dinamico del rimborso basato sulla regola dei 10 giorni o Tariffa Non Rimborsabile
    ref_res = compute_refund_amount(b)
    refund_amount = ref_res.get('refund', 0.0)

    # Se c'era un pagamento Stripe registrato ed è previsto un rimborso monetario, procedi via API Stripe
    pi = b.get('payment_intent_id')
    if pi and refund_amount > 0:
        try:
            stripe.Refund.create(
                payment_intent=pi,
                amount=int(refund_amount * 100),
                reason='requested_by_customer',
            )
        except Exception as e:
            logging.exception('Stripe refund failed')
            raise HTTPException(500, f'Rimborso Stripe fallito ma registrato localmente: {e}')

    await db.bookings.update_one(
        {'id': decoded_id},
        {'$set': {
            'status': 'cancelled',
            'payment_status': 'refunded' if refund_amount > 0 else b.get('payment_status', 'unpaid'),
            'refund_amount': refund_amount,
            'refund_at': datetime.now(timezone.utc).isoformat(),
        }},
    )

    # Invia email di notifica cancellazione all'ospite
    settings = await get_settings()
    b['status'] = 'cancelled'
    b['refund_amount'] = refund_amount
    asyncio.create_task(
        send_email_async(
            b['guest_email'],
            f"Annullamento Prenotazione — {settings.get('villa_name','Light Blue')}",
            email_cancellation_html(b, settings)
        )
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
        {'$set': {'last_reminder_at': datetime.now(timezone.utc).isoformat()}}
    )
    return {'ok': ok}
