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

# --- UTILS & CLEANUP ---

async def cleanup_expired_bookings():
    """Rimuove prenotazioni pending abbandonate per liberare le date."""
    try:
        # Calcoliamo il limite di 30 minuti fa in formato ISO string (come nel tuo model)
        cutoff_dt = datetime.now(timezone.utc) - timedelta(minutes=30)
        cutoff_iso = cutoff_dt.isoformat()
        
        # Cerchiamo i booking pending creati prima del cutoff
        expired_bookings = await db.bookings.find({
            'status': 'pending',
            'payment_status': 'unpaid',
            'created_at': {'$lt': cutoff_iso}
        }).to_list(length=100)

        for b in expired_bookings:
            booking_id = b.get('id')
            await db.bookings.delete_one({'id': booking_id})
            await db.payment_transactions.delete_one({'booking_id': booking_id})
            logging.info(f"Cleanup: rimosso booking abbandonato {booking_id}")
            
    except Exception as e:
        logging.error(f"Cleanup Error: {e}")

async def _retrieve_payment_intent_id(session_id: str) -> str | None:
    """Retrieves PaymentIntent id from a checkout session safely."""
    try:
        s = await asyncio.to_thread(stripe.checkout.Session.retrieve, session_id)
        pi = getattr(s, 'payment_intent', None)
        if pi and hasattr(pi, 'id'):
            return pi.id
        return pi
    except Exception as e:
        logging.error(f"PaymentIntent retrieval error: {e}")
        return None

async def _on_payment_paid(session_id: str, choice: str, booking_id: str):
    """Mark booking confirmed, store payment_intent_id, send confirmation email."""
    pi = await _retrieve_payment_intent_id(session_id)
    new_payment_status = 'fully_paid' if choice == 'full' else 'deposit_paid'
    update = {'status': 'confirmed', 'payment_status': new_payment_status}
    
    if pi:
        update['payment_intent_id'] = pi
        await db.payment_transactions.update_one(
            {'session_id': session_id}, {'$set': {'payment_intent_id': pi}}
        )
        
    await db.bookings.update_one({'id': booking_id}, {'$set': update})
    booking = await db.bookings.find_one({'id': booking_id}, {'_id': 0})
    settings = await get_settings()
    
    if booking:
        asyncio.create_task(send_email_async(
            booking['guest_email'],
            f"Prenotazione confermata — {settings.get('villa_name','Light Blue')}",
            email_booking_confirmation_html(booking, settings),
        ))

# --- ROUTES ---

@router.post("/bookings/checkout")
async def create_booking_checkout(payload: BookingCreate, request: Request, background_tasks: BackgroundTasks):
    # Avvia la pulizia delle vecchie sessioni abbandonate in background
    background_tasks.add_task(cleanup_expired_bookings)

    if payload.check_in >= payload.check_out:
        raise HTTPException(400, 'check_out must be after check_in')
    if not await dates_available(payload.check_in, payload.check_out):
        raise HTTPException(409, 'Date non disponibili')

    pricing = await compute_stay_pricing(payload.check_in, payload.check_out)
    settings = await get_settings()

    amount = pricing['total'] if payload.payment_choice == 'full' else pricing['deposit_amount']
    amount_cents = int(round(amount * 100))

    booking = Booking(
        guest_name=payload.guest_name,
        guest_email=payload.guest_email,
        guest_phone=payload.guest_phone,
        check_in=payload.check_in,
        check_out=payload.check_out,
        adults=payload.adults,
        children=payload.children,
        total_price=pricing['total'],
        deposit_amount=pricing['deposit_amount'],
        payment_choice=payload.payment_choice,
        status='pending',
        payment_status='unpaid',
        notes=payload.notes,
        consent_newsletter=payload.consent_newsletter,
    )
    await db.bookings.insert_one(booking.model_dump())

    # Gestione Newsletter
    if payload.consent_newsletter:
        existing = await db.subscribers.find_one({'email': payload.guest_email})
        if not existing:
            from models import Subscriber
            sub = Subscriber(email=payload.guest_email, name=payload.guest_name, source='booking')
            await db.subscribers.insert_one(sub.model_dump())

    host_url = payload.origin_url.rstrip('/')
    
    try:
        session = await asyncio.to_thread(
            stripe.checkout.Session.create,
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'eur',
                    'product_data': {'name': f"Soggiorno presso {settings.get('villa_name','Light Blue')}"},
                    'unit_amount': amount_cents,
                },
                'quantity': 1,
            }],
            mode='payment',
            # Forza la scadenza Stripe a 31 minuti (minimo consentito circa 30m)
            expires_at=int((datetime.now(timezone.utc) + timedelta(minutes=31)).timestamp()),
            success_url=f"{host_url}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{host_url}/payment/cancel",
            metadata={
                'booking_id': booking.id,
                'payment_choice': payload.payment_choice,
                'guest_email': payload.guest_email,
            },
        )
    except Exception as e:
        logging.exception('Stripe session create failed')
        await db.bookings.delete_one({'id': booking.id})
        raise HTTPException(500, f'Errore Stripe: {e}')

    tx = PaymentTransaction(
        session_id=session.id,
        booking_id=booking.id,
        amount=amount,
        payment_status='initiated',
        status='open',
        metadata={'payment_choice': payload.payment_choice, 'booking_id': booking.id},
    )
    await db.payment_transactions.insert_one(tx.model_dump())

    return {
        'url': session.url,
        'session_id': session.id,
        'booking_id': booking.id,
        'amount': amount,
    }


@router.get("/payments/status/{session_id}")
async def payment_status(session_id: str):
    tx = await db.payment_transactions.find_one({'session_id': session_id}, {'_id': 0})
    if not tx:
        raise HTTPException(404, 'Transaction not found')

    if tx.get('payment_status') == 'paid':
        return tx

    try:
        session = await asyncio.to_thread(stripe.checkout.Session.retrieve, session_id)
        p_status = 'paid' if session.payment_status == 'paid' else 'unpaid'
        status = session.status
    except Exception as e:
        logging.warning(f"Stripe status retrieval failed: {e}")
        return tx

    await db.payment_transactions.update_one(
        {'session_id': session_id},
        {'$set': {'status': status, 'payment_status': p_status}},
    )

    if p_status == 'paid' and tx.get('payment_status') != 'paid':
        choice = tx.get('metadata', {}).get('payment_choice', 'deposit')
        await _on_payment_paid(session_id, choice, tx.get('booking_id'))

    return {
        'payment_status': p_status,
        'status': status,
        'booking_id': tx.get('booking_id'),
    }


@router.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    body = await request.body()
    sig = request.headers.get('Stripe-Signature', '')

    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(body, sig, STRIPE_WEBHOOK_SECRET)
            etype = event.type
            data = event.data.object
        else:
            event = _json.loads(body)
            etype = event['type']
            data = event['data']['object']
        
        if etype in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
            session_id = getattr(data, 'id', None) or data.get('id')
            tx = await db.payment_transactions.find_one({'session_id': session_id})
            
            if tx and tx.get('payment_status') != 'paid':
                pi = getattr(data, 'payment_intent', None) or data.get('payment_intent')
                
                await db.payment_transactions.update_one(
                    {'session_id': session_id},
                    {'$set': {'payment_status': 'paid', 'status': 'complete', 'payment_intent_id': pi}}
                )
                
                choice = tx.get('metadata', {}).get('payment_choice', 'deposit')
                new_payment_status = 'fully_paid' if choice == 'full' else 'deposit_paid'
                
                await db.bookings.update_one(
                    {'id': tx['booking_id']},
                    {'$set': {
                        'status': 'confirmed',
                        'payment_status': new_payment_status,
                        'payment_intent_id': pi,
                    }},
                )
                
                booking = await db.bookings.find_one({'id': tx['booking_id']})
                settings = await get_settings()
                if booking:
                    asyncio.create_task(send_email_async(
                        booking['guest_email'],
                        f"Prenotazione confermata — {settings.get('villa_name','Light Blue')}",
                        email_booking_confirmation_html(booking, settings),
                    ))

        elif etype == 'checkout.session.expired':
            session_id = getattr(data, 'id', None) or data.get('id')
            tx = await db.payment_transactions.find_one({'session_id': session_id})
            if tx:
                # Libera le date eliminando il booking pending
                await db.bookings.delete_one({'id': tx['booking_id']})
                await db.payment_transactions.update_one(
                    {'session_id': session_id},
                    {'$set': {'status': 'expired'}}
                )

        return {'received': True}

    except Exception as e:
        logging.exception('Stripe webhook error')
        raise HTTPException(400, f"Webhook Error: {str(e)}")
