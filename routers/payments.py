"""Stripe payments: booking checkout, status polling, webhook, and cleanup."""
import asyncio
import json as _json
import logging
from datetime import datetime, timezone, timedelta

import stripe
from fastapi import APIRouter, HTTPException, Request, BackgroundTasks

from db import db, get_settings, STRIPE_API_KEY, STRIPE_WEBHOOK_SECRET
from email_helpers import send_email_async, email_booking_confirmation_html
from models import Booking, BookingCreate, PaymentTransaction, Subscriber
from pricing import compute_stay_pricing, dates_available

router = APIRouter()
stripe.api_key = STRIPE_API_KEY

async def cleanup_expired_bookings():
    try:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(minutes=10)
        cutoff_iso = cutoff_dt.isoformat()
        expired_bookings = await db.bookings.find({
            'status': 'pending',
            'payment_status': 'unpaid',
            'source': 'website',
            'created_at': {'$lt': cutoff_iso}
        }).to_list(100)
        for b in expired_bookings:
            await db.bookings.delete_one({'id': b['id']})
            await db.payment_transactions.delete_many({'booking_id': b['id']})
            logging.info(f"Cleanup: rimosso booking scaduto {b['id']}")
    except Exception as e:
        logging.error(f"Cleanup error: {e}")

@router.post("/bookings/checkout")
async def create_checkout(payload: BookingCreate, bg: BackgroundTasks):
    bg.add_task(cleanup_expired_bookings)
    if not await dates_available(payload.check_in, payload.check_out):
        raise HTTPException(400, "Date non più disponibili")
    
    pricing = await compute_stay_pricing(payload.check_in, payload.check_out)
    settings = await get_settings()
    
    # Rimosso calcolo acconto, si usa sempre il totale
    amount_to_pay = pricing['total']
    
    booking_id = f"web-{int(datetime.now(timezone.utc).timestamp())}"
    booking_doc = {
        **payload.model_dump(),
        "id": booking_id,
        "total_price": pricing['total'],
        "payment_choice": "full",
        "cancellation_policy": settings.get('default_cancellation_policy', 'moderate'),
        "status": "pending",
        "payment_status": "unpaid",
        "source": "website",
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    
    await db.bookings.insert_one(booking_doc)
    if payload.consent_newsletter:
        await db.subscribers.update_one({'email': payload.guest_email}, {'$set': {'email': payload.guest_email, 'created_at': datetime.now(timezone.utc).isoformat()}}, upsert=True)

    try:
        session = await asyncio.to_thread(
            stripe.checkout.Session.create,
            payment_method_types=['card', 'paypal', 'klarna', 'sepa_debit'],
            line_items=[{
                'price_data': {
                    'currency': 'eur',
                    'product_data': {'name': f"Soggiorno {settings.get('villa_name','Villa')}", 'description': f"{payload.check_in} al {payload.check_out}"},
                    'unit_amount': int(amount_to_pay * 100),
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=f"https://lightbluelakecomo.com/booking-success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url="https://lightbluelakecomo.com/booking",
            metadata={'booking_id': booking_id, 'payment_choice': 'full'}
        )
        
        tx = PaymentTransaction(
            session_id=session.id,
            booking_id=booking_id,
            amount=amount_to_pay,
            metadata={'payment_choice': 'full'}
        )
        await db.payment_transactions.insert_one(tx.model_dump())
        return {"url": session.url}
    except Exception as e:
        await db.bookings.delete_one({'id': booking_id})
        logging.error(f"Stripe error: {e}")
        raise HTTPException(500, "Errore nella creazione del pagamento")

@router.get("/bookings/status/{session_id}")
async def get_payment_status(session_id: str):
    tx = await db.payment_transactions.find_one({'session_id': session_id})
    if not tx: raise HTTPException(404, "Sessione non trovata")
    
    if tx['payment_status'] == 'paid':
        return {'status': 'paid'}
        
    try:
        session = await asyncio.to_thread(stripe.checkout.Session.retrieve, session_id)
        if session.payment_status == 'paid':
            pi = session.payment_intent
            await db.payment_transactions.update_one({'session_id': session_id}, {'$set': {'payment_status': 'paid', 'status': 'complete'}})
            await db.bookings.update_one({'id': tx['booking_id']}, {'$set': {'status': 'confirmed', 'payment_status': 'fully_paid', 'payment_intent_id': pi}})
            
            booking = await db.bookings.find_one({'id': tx['booking_id']})
            settings = await get_settings()
            if booking:
                asyncio.create_task(send_email_async(booking['guest_email'], f"Prenotazione confermata — {settings.get('villa_name','Light Blue')}", email_booking_confirmation_html(booking, settings)))
            return {'status': 'paid'}
        return {'status': session.payment_status}
    except Exception as e:
        logging.error(f"Status check error: {e}")
        return {'status': tx['payment_status']}

@router.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get('Stripe-Signature')
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        data = event['data']['object']
        etype = event['type']
        
        if etype in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
            session_id = data['id']
            pi = data.get('payment_intent')
            tx = await db.payment_transactions.find_one({'session_id': session_id})
            if tx and tx['payment_status'] != 'paid':
                await db.payment_transactions.update_one({'session_id': session_id}, {'$set': {'payment_status': 'paid', 'status': 'complete', 'payment_intent_id': pi}})
                await db.bookings.update_one({'id': tx['booking_id']}, {'$set': {'status': 'confirmed', 'payment_status': 'fully_paid', 'payment_intent_id': pi}})
                
                booking = await db.bookings.find_one({'id': tx['booking_id']})
                settings = await get_settings()
                if booking:
                    asyncio.create_task(send_email_async(booking['guest_email'], f"Prenotazione confermata — {settings.get('villa_name','Light Blue')}", email_booking_confirmation_html(booking, settings)))

        elif etype == 'checkout.session.expired':
            session_id = data['id']
            tx = await db.payment_transactions.find_one({'session_id': session_id})
            if tx:
                booking = await db.bookings.find_one({'id': tx['booking_id']})
                if booking and booking.get('source') == 'website':
                    await db.bookings.delete_one({'id': tx['booking_id']})
                await db.payment_transactions.update_one({'session_id': session_id}, {'$set': {'status': 'expired'}})

        return {'received': True}
    except Exception as e:
        logging.exception('Stripe webhook error')
        raise HTTPException(400, "Webhook Error")
