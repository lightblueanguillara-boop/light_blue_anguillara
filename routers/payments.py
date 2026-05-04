"""Stripe payments: booking checkout, status polling, webhook.

Uses emergentintegrations for checkout creation/status (works with sk_test_emergent
proxied key in test mode; in live mode replace with real sk_live_*).
Uses direct stripe library to retrieve PaymentIntent ID after a successful payment
so refunds work in live mode.
"""
import asyncio
import json as _json
import logging

import stripe
from emergentintegrations.payments.stripe.checkout import (
    StripeCheckout,
    CheckoutSessionRequest,
    CheckoutSessionResponse,
    CheckoutStatusResponse,
)
from fastapi import APIRouter, HTTPException, Request

from db import db, get_settings, STRIPE_API_KEY, STRIPE_WEBHOOK_SECRET
from email_helpers import send_email_async, email_booking_confirmation_html
from models import Booking, BookingCreate, PaymentTransaction, Subscriber
from pricing import compute_stay_pricing, dates_available

router = APIRouter()
stripe.api_key = STRIPE_API_KEY


async def _retrieve_payment_intent_id(session_id: str) -> str | None:
    """Best-effort retrieval of PaymentIntent id (works in live mode with real key).
    Returns None if Stripe call fails (e.g., test proxy doesn't expose this)."""
    try:
        s = await asyncio.to_thread(stripe.checkout.Session.retrieve, session_id)
        return s.get('payment_intent') if isinstance(s, dict) else getattr(s, 'payment_intent', None)
    except Exception as e:
        logging.info(f"PaymentIntent retrieval skipped (test mode or error): {e}")
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
    amount = float(round(amount, 2))

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
    webhook_url = f"{str(request.base_url).rstrip('/')}/api/webhook/stripe"
    stripe_checkout = StripeCheckout(api_key=STRIPE_API_KEY, webhook_url=webhook_url)
    success_url = f"{host_url}/payment/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{host_url}/payment/cancel"
    req = CheckoutSessionRequest(
        amount=amount,
        currency='eur',
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={
            'booking_id': booking.id,
            'payment_choice': payload.payment_choice,
            'guest_email': payload.guest_email,
        },
    )
    try:
        session: CheckoutSessionResponse = await stripe_checkout.create_checkout_session(req)
    except Exception as e:
        logging.exception('Stripe session create failed')
        raise HTTPException(500, f'Errore creazione pagamento: {e}')

    tx = PaymentTransaction(
        session_id=session.session_id,
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
        'session_id': session.session_id,
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

    webhook_url = f"{str(request.base_url).rstrip('/')}/api/webhook/stripe"
    stripe_checkout = StripeCheckout(api_key=STRIPE_API_KEY, webhook_url=webhook_url)
    try:
        status: CheckoutStatusResponse = await stripe_checkout.get_checkout_status(session_id)
    except Exception as e:
        logging.warning(f"Stripe status retrieval failed (cached): {e}")
        return {'payment_status': tx.get('payment_status', 'initiated'), 'status': tx.get('status', 'open'), 'booking_id': tx.get('booking_id')}

    await db.payment_transactions.update_one(
        {'session_id': session_id},
        {'$set': {'status': status.status, 'payment_status': status.payment_status}},
    )

    if status.payment_status == 'paid' and tx.get('payment_status') != 'paid':
        choice = tx.get('metadata', {}).get('payment_choice', 'deposit')
        await _on_payment_paid(session_id, choice, tx.get('booking_id'))

    return {
        'payment_status': status.payment_status,
        'status': status.status,
        'booking_id': tx.get('booking_id'),
    }


@router.post("/webhook/stripe")
async def stripe_webhook(request: Request):
    body = await request.body()
    sig = request.headers.get('Stripe-Signature', '')
    webhook_url = f"{str(request.base_url).rstrip('/')}/api/webhook/stripe"

    # Try emergentintegrations webhook first (matches its checkout flow)
    stripe_checkout = StripeCheckout(api_key=STRIPE_API_KEY, webhook_url=webhook_url)
    try:
        resp = await stripe_checkout.handle_webhook(body, sig)
        tx = await db.payment_transactions.find_one({'session_id': resp.session_id}, {'_id': 0})
        if tx:
            await db.payment_transactions.update_one(
                {'session_id': resp.session_id},
                {'$set': {'payment_status': resp.payment_status}},
            )
            if resp.payment_status == 'paid' and tx.get('payment_status') != 'paid':
                choice = tx.get('metadata', {}).get('payment_choice', 'deposit')
                await _on_payment_paid(resp.session_id, choice, tx['booking_id'])
        return {'received': True}
    except Exception as e:
        logging.warning(f'Emergent webhook parse failed, trying direct stripe: {e}')

    # Fallback: parse with direct stripe library (for live mode with secret)
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(body, sig, STRIPE_WEBHOOK_SECRET)
        else:
            event = _json.loads(body)
        etype = event.get('type') if isinstance(event, dict) else event['type']
        data = (event.get('data', {}).get('object', {}) if isinstance(event, dict)
                else event['data']['object'])
        if etype in ('checkout.session.completed', 'checkout.session.async_payment_succeeded'):
            session_id = data.get('id')
            tx = await db.payment_transactions.find_one({'session_id': session_id}, {'_id': 0})
            if tx and tx.get('payment_status') != 'paid':
                pi = data.get('payment_intent')
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
        return {'received': True}
    except Exception as e:
        logging.exception('Stripe webhook error')
        raise HTTPException(400, str(e))
