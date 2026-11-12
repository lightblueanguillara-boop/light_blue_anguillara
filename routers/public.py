"""Public routes: villa info, availability, contact, newsletter, iCal export, and Gallery."""
import asyncio
import re
from datetime import datetime, timezone, timedelta
from typing import List

from fastapi import APIRouter, HTTPException, Request, Response
from icalendar import Calendar, Event
from pydantic import BaseModel

from auth import email_from_newsletter_token
from db import db, get_settings, ADMIN_NOTIFY_EMAIL
from email_helpers import send_email_async, email_admin_contact_notification_html, email_guest_contact_confirmation_html
from models import ContactMessage, ContactCreate, Subscriber, GalleryImage, _validate_iso_date, validate_stay
from pricing import BLOCKING_STATUSES, compute_dual_pricing, dates_available, daterange
from ratelimit import rate_limit, check_rate

router = APIRouter()


@router.get("/")
async def root():
    return {"app": "Light Blue - Anguillara Sabazia", "status": "ok"}


@router.get("/villa/info")
async def villa_info():
    s = await get_settings()
    return {
        'name': s.get('villa_name'),
        'address': s.get('villa_address'),
        'phone': s.get('villa_phone'),
        'email': s.get('villa_email'),
        'description': s.get('villa_description'),
        'cir': s.get('villa_cir', ''),
        'lake': s.get('villa_lake', 'Lago di Bracciano'),
        'default_price_per_night': s.get('default_price_per_night'),
        'deposit_percent': s.get('deposit_percent'),
        'non_refundable_discount_percent': s.get('non_refundable_discount_percent', 5.0),
        'logo_version': s.get('logo_version', 'default'),
        'share_version': s.get('share_version', 'default'),
        'booking_mode': s.get('booking_mode', 'request'),
        'request_hold_hours': s.get('request_hold_hours', 24),
        'payment_link_hours': s.get('payment_link_hours', 24),
        'legal_owner_name': s.get('legal_owner_name', ''),
        'legal_owner_tax_id': s.get('legal_owner_tax_id', ''),
        'legal_owner_address': s.get('legal_owner_address', ''),
        'tourist_tax_note': s.get('tourist_tax_note', ''),
    }

@router.get("/gallery", response_model=List[GalleryImage])
async def public_gallery():
    """Recupera le immagini della galleria per il sito pubblico (senza autenticazione)."""
    # Leggiamo dal DB ordinando per il campo 'order'
    images = await db.gallery.find({}, {'_id': 0}).sort("order", 1).to_list(1000)
    return images


@router.get("/availability")
async def availability(start: str, end: str):
    try:
        _validate_iso_date(start)
        _validate_iso_date(end)
    except ValueError:
        raise HTTPException(400, 'Date non valide (formato AAAA-MM-GG)')
    bookings = await db.bookings.find(
        {'status': {'$in': BLOCKING_STATUSES}}, {'_id': 0}
    ).to_list(10000)
    blocked = []
    for b in bookings:
        for d in daterange(b['check_in'], b['check_out']):
            if start <= d.isoformat() <= end:
                blocked.append(d.isoformat())
    return {'blocked_dates': sorted(set(blocked))}


@router.post("/quote")
async def quote(check_in: str, check_out: str):
    """
    Endpoint di preventivo che ritorna entrambe le tariffe:
    - refundable_total: prezzo pieno (Rimborsabile)
    - non_refundable_total: prezzo scontato (Non Rimborsabile)
    """
    try:
        _validate_iso_date(check_in)
        _validate_iso_date(check_out)
        validate_stay(check_in, check_out)
    except ValueError as e:
        raise HTTPException(400, str(e))

    pricing = await compute_dual_pricing(check_in, check_out)
    available = await dates_available(check_in, check_out)
    
    return {
        **pricing,
        'available': available
    }


@router.post("/contact")
async def contact(payload: ContactCreate, request: Request):
    # anti-spam: max 5 messaggi l'ora per IP e 3 conferme al giorno verso lo stesso indirizzo
    rate_limit(request, 'contact', 5, 3600)
    if payload.website:
        # campo trappola compilato: è un bot. Rispondiamo "ok" senza salvare né inviare nulla.
        return {'ok': True}
    check_rate(f'contact-to:{payload.email.lower()}', 3, 24 * 3600)

    msg = ContactMessage(**payload.model_dump(exclude={'website'}))
    await db.contact_messages.insert_one(msg.model_dump())
    if payload.consent_newsletter:
        existing = await db.subscribers.find_one({'email': payload.email}, {'_id': 0})
        if not existing:
            sub = Subscriber(email=payload.email, name=payload.name, source='contact_form')
            await db.subscribers.insert_one(sub.model_dump())
        # nuovo consenso esplicito: annulla un'eventuale disiscrizione precedente
        await db.unsubscribed.delete_one({'email': payload.email.lower()})
    if ADMIN_NOTIFY_EMAIL:
        asyncio.create_task(send_email_async(
            ADMIN_NOTIFY_EMAIL,
            f"[Light Blue] Nuova richiesta da {payload.name[:60]}",
            email_admin_contact_notification_html(msg.model_dump()),
        ))
    # Conferma automatica all'ospite
    asyncio.create_task(send_email_async(
        payload.email,
        "Abbiamo ricevuto la tua richiesta — Light Blue Anguillara",
        email_guest_contact_confirmation_html(msg.model_dump()),
    ))
    return {'ok': True, 'id': msg.id}


class UnsubscribeRequest(BaseModel):
    token: str


@router.post("/newsletter/unsubscribe")
async def unsubscribe(payload: UnsubscribeRequest, request: Request):
    """Disiscrizione tramite il link firmato presente in ogni email marketing."""
    rate_limit(request, 'unsubscribe', 20, 3600)
    email = email_from_newsletter_token(payload.token)
    if not email:
        raise HTTPException(400, 'Link non valido')
    await db.subscribers.delete_many({'email': {'$regex': f'^{re.escape(email)}$', '$options': 'i'}})
    await db.unsubscribed.update_one(
        {'email': email},
        {'$set': {'email': email, 'at': datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )
    return {'ok': True}


@router.get("/villa/last-minute")
async def last_minute():
    s = await get_settings()
    if not s.get('last_minute_enabled'):
        return {'enabled': False}
    window = int(s.get('last_minute_window_days', 14))
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=window)
    bookings = await db.bookings.find(
        {'status': {'$in': BLOCKING_STATUSES}}, {'_id': 0}
    ).to_list(10000)
    blocked: set = set()
    for b in bookings:
        for d in daterange(b['check_in'], b['check_out']):
            if today <= d <= end:
                blocked.add(d)
    ranges = []
    cur_start = None
    cur_prev = None
    d = today
    while d <= end:
        if d not in blocked:
            if cur_start is None:
                cur_start = d
            cur_prev = d
        else:
            if cur_start and cur_prev and (cur_prev - cur_start).days >= 1:
                ranges.append({
                    'check_in': cur_start.isoformat(),
                    'check_out': (cur_prev + timedelta(days=1)).isoformat(),
                    'nights': (cur_prev - cur_start).days + 1,
                })
            cur_start = None
            cur_prev = None
        d += timedelta(days=1)
    if cur_start and cur_prev and (cur_prev - cur_start).days >= 1:
        ranges.append({
            'check_in': cur_start.isoformat(),
            'check_out': (cur_prev + timedelta(days=1)).isoformat(),
            'nights': (cur_prev - cur_start).days + 1,
        })
    return {
        'enabled': True,
        'discount_percent': s.get('last_minute_discount_percent', 15.0),
        'window_days': window,
        'title': s.get('last_minute_title', ''),
        'subtitle': s.get('last_minute_subtitle', ''),
        'ranges': ranges[:6],
    }


@router.get("/ical/export.ics")
async def export_ical():
    cal = Calendar()
    cal.add('prodid', '-//Light Blue Anguillara Sabazia//IT')
    cal.add('version', '2.0')
    bookings = await db.bookings.find(
        {'status': {'$in': BLOCKING_STATUSES}}, {'_id': 0}
    ).to_list(10000)
    for b in bookings:
        ev = Event()
        ev.add('uid', f"{b['id']}@lightblue-anguillara")
        ev.add('summary', f"BLOCKED - {b.get('source', 'website')}")
        ev.add('dtstart', datetime.strptime(b['check_in'], '%Y-%m-%d').date())
        ev.add('dtend', datetime.strptime(b['check_out'], '%Y-%m-%d').date())
        ev.add('dtstamp', datetime.now(timezone.utc))
        cal.add_component(ev)
    return Response(content=cal.to_ical(), media_type='text/calendar')
