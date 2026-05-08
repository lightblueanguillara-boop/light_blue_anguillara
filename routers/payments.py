"""Stripe payments: booking checkout, status polling, webhook.

Replaced emergentintegrations with direct stripe library calls for 
standard compatibility.
"""
import asyncio
import json as _json
import logging

import stripe
from fastapi import APIRouter, HTTPException, Request

from db import db, get_settings, STRIPE_API_KEY, STRIPE_WEBHOOK_SECRET
from email_helpers import send_email_async, email_booking_confirmation_html
from models import Booking, BookingCreate, PaymentTransaction, Subscriber
from pricing import compute_stay_pricing, dates_available

router = APIRouter()
stripe.api_key = STRIPE_API_KEY


async def _retrieve_payment_intent_id(session_id: str) -> str | None:
    """Retrieves PaymentIntent id from a checkout session safely."""
    try:
        # Recupero la sessione tramite thread per non bloccare l'event loop
        s = await asyncio.to_thread(stripe.checkout.Session.retrieve, session_id)
        
        # Gli oggetti Stripe usano attributi, non .get()
        pi = getattr(s, 'payment_intent', None)
        
        # Se il payment_intent è un oggetto espanso, restituiamo solo l'ID
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


@router.post("/bookings/checkout")
async def create_booking_checkout(payload: BookingCreate, request: Request):
    if payload.check_in >= payload.check_out:
        raise HTTPException(400, 'check_out must be after check_in')
    if not await dates_available(payload.check_in, payload.check_out):
        raise HTTPException(409, 'Date non disponibili')

    pricing = await compute_stay_pricing(payload.check_in, payload.check_out)
    settings = await get_settings()

    amount = pricing['total'] if payload.payment_choice == 'full' else pricing['deposit_amount']
    amount_cents = int(round(amount * 100)) # Stripe uses cents

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
        cancellation_policy=settings.get('default_cancellation_policy', 'moderate'),
        status='pending',
        payment_status='unpaid',
        source='website',
        notes=payload.notes,
        consent_newsletter=payload.consent_newsletter,
    )
    await db.bookings.insert_one(booking.model_dump())

    if payload.consent_newsletter:
        existing = await db.subscribers.find_one({'email': payload.guest_email}, {'_id': 0})
        if not existing:
            sub = Subscriber(email=payload.guest_email, name=payload.guest_name, source='booking')
            await db.subscribers.insert_one(sub.model_dump())

    host_url = payload.origin_url.rstrip('/')
    success_url = f"{host_url}/payment/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{host_url}/payment/cancel"
    
    try:
        session = await asyncio.to_thread(
            stripe.checkout.Session.create,
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'eur',
                    'product_data': {
                        'name': f"Soggiorno presso {settings.get('villa_name','Light Blue')}",
                    },
                    'unit_amount': amount_cents,
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                'booking_id': booking.id,
                'payment_choice': payload.payment_choice,
                'guest_email': payload.guest_email,
            },
        )
    except Exception as e:
        logging.exception('Stripe session create failed')
        raise HTTPException(500, f'Errore creazione pagamento: {e}')

    tx = PaymentTransaction(
        session_id=session.id,
        booking_id=booking.id,
        amount=amount,
        currency='eur',
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
async def payment_status(session_id: str, request: Request):
    tx = await db.payment_transactions.find_one({'session_id': session_id}, {'_id': 0})
    if not tx:
        raise HTTPException(404, 'Transaction not found')

    if tx.get('payment_status') == 'paid':
        return {'payment_status': 'paid', 'status': tx.get('status', 'complete'), 'booking_id': tx.get('booking_id')}

    try:
        session = await asyncio.to_thread(stripe.checkout.Session.retrieve, session_id)
        p_status = 'paid' if session.payment_status == 'paid' else 'unpaid'
        status = session.status # 'open', 'complete', or 'expired'
    except Exception as e:
        logging.warning(f"Stripe status retrieval failed: {e}")
        return {'payment_status': tx.get('payment_status', 'initiated'), 'status': tx.get('status', 'open'), 'booking_id': tx.get('booking_id')}

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
        
        # Gestiamo il completamento del checkout
        if etype in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
            # Accesso sicuro agli attributi dell'oggetto Stripe
            session_id = getattr(data, 'id', None) or data.get('id')
            
            tx = await db.payment_transactions.find_one({'session_id': session_id}, {'_id': 0})
            
            if tx and tx.get('payment_status') != 'paid':
                pi = getattr(data, 'payment_intent', None) or data.get('payment_intent')
                
                await db.payment_transactions.update_one(
                    {'session_id': session_id},
                    {'$set': {'payment_status': 'paid', 'status': 'complete', 'payment_intent_id': pi}},
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
                
                booking = await db.bookings.find_one({'id': tx['booking_id']}, {'_id': 0})
                settings = await get_settings()
                if booking:
                    asyncio.create_task(send_email_async(
                        booking['guest_email'],
                        f"Prenotazione confermata — {settings.get('villa_name','Light Blue')}",
                        email_booking_confirmation_html(booking, settings),
                    ))

        # Opzionale: Gestione sessione scaduta (rimette le date libere se necessario)
        elif etype == 'checkout.session.expired':
            session_id = getattr(data, 'id', None) or data.get('id')
            await db.payment_transactions.update_one(
                {'session_id': session_id},
                {'$set': {'status': 'expired'}}
            )

        return {'received': True}

    except Exception as e:
        logging.exception('Stripe webhook error')
        # Restituiamo 400 così Stripe sa che deve riprovare
        raise HTTPException(400, f"Webhook Error: {str(e)}")
