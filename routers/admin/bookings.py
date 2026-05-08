"""Bookings CRUD + refund + balance-reminder."""
import asyncio
import logging
from datetime import datetime, timezone

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
    # Questo serve per caricare la lista nella dashboard
    return await db.bookings.find({}, {'_id': 0}).sort('created_at', -1).to_list(10000)


@router.post("/admin/bookings")
async def create_booking_generic(b: Booking, admin=Depends(get_current_admin)):
    # Rotta generica di backup
    await db.bookings.insert_one(b.model_dump())
    return b.model_dump()


@router.post("/admin/bookings/manual")
async def create_manual_booking(b: Booking, admin=Depends(get_current_admin)):
    """Questa risolve l'errore 405 del tasto 'Salva' manuale."""
    b.source = 'manual'
    if not b.created_at:
        b.created_at = datetime.now(timezone.utc).isoformat()
    await db.bookings.insert_one(b.model_dump())
    return b.model_dump()


@router.patch("/admin/bookings/{booking_id}")
async def update_booking(
    booking_id: str, updates: BookingUpdate, admin=Depends(get_current_admin)
):
    patch = updates.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(400, 'No fields to update')
    await db.bookings.update_one({'id': booking_id}, {'$set': patch})
    return await db.bookings.find_one({'id': booking_id}, {'_id': 0})


@router.delete("/admin/bookings/{booking_id}")
async def delete_booking(booking_id: str, admin=Depends(get_current_admin)):
    await db.bookings.delete_one({'id': booking_id})
    return {'ok': True}


@router.get("/admin/bookings/{booking_id}/refund-preview")
async def refund_preview(booking_id: str, admin=Depends(get_current_admin)):
    b = await db.bookings.find_one({'id': booking_id}, {'_id': 0})
    if not b:
        raise HTTPException(404, 'Booking not found')
    return compute_refund_amount(b)


@router.post("/admin/bookings/{booking_id}/cancel-refund")
async def cancel_and_refund(
    booking_id: str, payload: RefundRequest, admin=Depends(get_current_admin)
):
    b = await db.bookings.find_one({'id': booking_id}, {'_id': 0})
    if not b:
        raise HTTPException(404, 'Booking not found')
    policy_calc = compute_refund_amount(b)
    refund_amount = float(payload.amount) if payload.amount is not None else policy_calc['refund']
    refund_obj = None
    if refund_amount > 0:
        tx = await db.payment_transactions.find_one(
            {'booking_id': booking_id, 'payment_status': 'paid'}, {'_id': 0}
        )
        pi = (tx or {}).get('payment_intent_id') or b.get('payment_intent_id')
        if not pi:
            raise HTTPException(400, 'Payment intent non disponibile per il rimborso automatico')
        try:
            refund_obj = await asyncio.to_thread(
                stripe.Refund.create,
                payment_intent=pi,
                amount=int(round(refund_amount * 100)),
                reason='requested_by_customer',
            )
        except Exception as e:
            logging.exception('Stripe refund failed')
            raise HTTPException(500, f'Rimborso Stripe fallito: {e}')
    await db.bookings.update_one(
        {'id': booking_id},
        {'$set': {
            'status': 'cancelled',
            'payment_status': 'refunded' if refund_amount > 0 else b.get('payment_status', 'unpaid'),
            'refund_amount': refund_amount,
            'refund_reason': payload.reason or policy_calc['reason'],
            'refund_at': datetime.now(timezone.utc).isoformat(),
        }},
    )
    return {
        'ok': True,
        'refund_amount': refund_amount,
        'stripe_refund_id': refund_obj.id if refund_obj else None,
    }


@router.post("/admin/bookings/{booking_id}/balance-reminder")
async def balance_reminder(booking_id: str, admin=Depends(get_current_admin)):
    b = await db.bookings.find_one({'id': booking_id}, {'_id': 0})
    if not b:
        raise HTTPException(404, 'Booking not found')
    settings = await get_settings()
    ok = await send_email_async(
        b['guest_email'],
        f"Promemoria saldo — {settings.get('villa_name','Light Blue')}",
        email_balance_reminder_html(b, settings),
    )
    await db.bookings.update_one(
        {'id': booking_id},
        {'$set': {'last_reminder_at': datetime.now(timezone.utc).isoformat()}},
    )
    return {'ok': ok}
