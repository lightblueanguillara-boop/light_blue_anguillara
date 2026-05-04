"""iCal helper used by both admin sync endpoint and background scheduler."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone

import requests
from icalendar import Calendar

from db import db, get_settings
from models import Booking


async def ical_sync_run():
    s = await get_settings()
    urls = [
        ('airbnb', s.get('ical_airbnb_url', '')),
        ('booking', s.get('ical_booking_url', '')),
    ]
    imported = 0
    await db.bookings.delete_many({'source': {'$in': ['airbnb', 'booking']}, 'status': 'external'})
    for src, url in urls:
        if not url:
            continue
        try:
            r = await asyncio.to_thread(lambda u=url: requests.get(u, timeout=15))
            if r.status_code != 200:
                continue
            cal = Calendar.from_ical(r.content)
            for comp in cal.walk():
                if comp.name == 'VEVENT':
                    start = comp.get('dtstart').dt
                    end = comp.get('dtend').dt
                    if isinstance(start, datetime):
                        start = start.date()
                    if isinstance(end, datetime):
                        end = end.date()
                    uid = str(comp.get('uid', uuid.uuid4()))
                    booking = Booking(
                        id=f"ext-{src}-{uid}",
                        guest_name=f"External ({src})",
                        guest_email=f"{src}@external.invalid",
                        check_in=start.isoformat(),
                        check_out=end.isoformat(),
                        total_price=0,
                        deposit_amount=0,
                        status='external',
                        payment_status='unpaid',
                        source=src,
                    )
                    await db.bookings.update_one(
                        {'id': booking.id}, {'$set': booking.model_dump()}, upsert=True
                    )
                    imported += 1
        except Exception as e:
            logging.exception(f'iCal sync error for {src}: {e}')
    await db.settings.update_one(
        {'id': 'global'},
        {'$set': {
            'last_ical_sync_at': datetime.now(timezone.utc).isoformat(),
            'last_ical_sync_count': imported,
        }},
    )
    return {'imported': imported, 'at': datetime.now(timezone.utc).isoformat()}
