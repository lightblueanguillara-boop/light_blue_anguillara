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
from email_helpers import send_email_async, email_balance_reminder_html
from models import Booking, BookingUpdate, RefundRequest
from pricing import compute_refund_amount

router = APIRouter()
stripe.api_key = STRIPE_API_KEY

@router.get("/admin/bookings")
async def list_bookings(admin=Depends(get_current_admin)):
    return await db.bookings.find({}, {'_id': 0}).sort('created_at', -1).to_list(10000)

@router.post("/admin/bookings/manual")
async def create_manual_booking(b: Booking, admin=Depends(get_current_admin)):
    """Crea una prenotazione manuale garantendo la stabilità del frontend."""
    try:
        # Forziamo i valori necessari che potrebbero mancare nel form manuale
        b.source = 'manual'
        if not b.id:
            b.id = str(uuid.uuid4())
        
        # Gestione date creazione
        if not b.created_at:
            b.created_at = datetime.now(timezone.utc).isoformat()
        
        # Valori di default per evitare errori 422 se il frontend non li invia
        if not b.status: b.status = 'confirmed'
        if not b.payment_status: b.payment_status = 'unpaid'
        if not b.guest_name: b.guest_name = "Ospite Manuale"
        
        # Inserimento nel database
        data = b.model_dump()
        await db.bookings.insert_one(data)
        
        return {
            "ok": True,
            "message": "Prenotazione creata con successo",
            "booking": data
        }
    except Exception as e:
        logging.error(f"Errore creazione manuale: {e}")
        # Se l'errore è di validazione (Pydantic), FastAPI restituisce 422 prima di entrare qui.
        # Ma catturiamo altri errori di database o logica.
        raise HTTPException(500, f"Errore interno: {str(e)}")

@router.patch("/admin/bookings/{booking_id}")
async def update_booking(
    booking_id: str, updates: BookingUpdate, admin=Depends(get_current_admin)
):
    decoded_id = urllib.parse.unquote(booking_id)
    patch = updates.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(400, 'No fields to update')
    
    result = await db.bookings.update_one({'id': decoded_id}, {'$set': patch})
    if result.matched_count == 0 and decoded_id != booking_id:
        await db.bookings.update_one({'id': booking_id}, {'$set': patch})

    return await db.bookings.find_one({'id': decoded_id}, {'_id': 0})

@router.delete("/admin/bookings/{booking_id}")
async def delete_booking(booking_id: str, admin=Depends(get_current_admin)):
    decoded_id = urllib.parse.unquote(booking_id)
    await db.bookings.delete_one({'id': decoded_id})
    return {'ok': True}

@router.post("/admin/bookings/{booking_id}/cancel-refund")
async def cancel_and_refund(
    booking_id: str, payload: RefundRequest, admin=Depends(get_current_admin)
):
    decoded_id = urllib.parse.unquote(booking_id)
    b = await db.bookings.find_one({'id': decoded_id}, {'_id': 0})
    if not b:
        raise HTTPException(404, 'Booking not found')
    
    policy_calc = compute_refund_amount(b)
    refund_amount = float(payload.amount) if payload.amount is not None else policy_calc['refund']
    
    if refund_amount > 0:
        tx = await db.payment_transactions.find_one(
            {'booking_id': decoded_id, 'payment_status': 'paid'}, {'_id': 0}
        )
        pi = (tx or {}).get('payment_intent_id') or b.get('payment_intent_id')
        if not pi:
            raise HTTPException(400, 'Payment intent non disponibile')
            
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
