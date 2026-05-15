from datetime import datetime, timezone, timedelta, date
from typing import Optional
from db import db, get_settings

def daterange(start: str, end: str):
    s = datetime.strptime(start, '%Y-%m-%d').date()
    e = datetime.strptime(end, '%Y-%m-%d').date()
    cur = s
    while cur < e:
        yield cur
        cur += timedelta(days=1)

def price_for_date(d: date, settings: dict) -> float:
    rates = settings.get('seasonal_rates', [])
    best = None
    for r in rates:
        sd = r['start_date']
        ed = r['end_date']
        try:
            if len(sd) == 10:
                s = datetime.strptime(sd, '%Y-%m-%d').date()
                e = datetime.strptime(ed, '%Y-%m-%d').date()
                in_range = s <= d <= e
            else:
                sm, sday = map(int, sd.split('-'))
                em, eday = map(int, ed.split('-'))
                s = date(d.year, sm, sday)
                e = date(d.year, em, eday)
                if s <= e:
                    in_range = s <= d <= e
                else:
                    in_range = d >= s or d <= e
            if in_range:
                if best is None or r.get('priority', 1) > best.get('priority', 1):
                    best = r
        except: continue
    return best['price_per_night'] if best else settings.get('default_price_per_night', 200.0)

async def dates_available(start: str, end: str, exclude_booking_id: Optional[str] = None) -> bool:
    query = {
        'status': {'$ne': 'cancelled'},
        '$or': [
            {'check_in': {'$lt': end}, 'check_out': {'$gt': start}}
        ]
    }
    if exclude_booking_id:
        query['id'] = {'$ne': exclude_booking_id}
    existing = await db.bookings.find_one(query)
    return existing is None

async def compute_stay_pricing(start: str, end: str):
    settings = await get_settings()
    total = 0.0
    breakdown = []
    for d in daterange(start, end):
        p = price_for_date(d, settings)
        total += p
        breakdown.append({'date': d.isoformat(), 'price': p})
    
    available = await dates_available(start, end)
    
    return {
        'total': round(total, 2),
        'breakdown': breakdown,
        'available': available
    }

def compute_refund_amount(booking: dict) -> dict:
    policy = booking.get('cancellation_policy', 'moderate')
    total_paid = booking.get('total_price', 0)
    now = datetime.now(timezone.utc)
    check_in = datetime.strptime(booking['check_in'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
    created = datetime.fromisoformat(booking.get('created_at')) if booking.get('created_at') else now
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    
    hours_to_checkin = (check_in - now).total_seconds() / 3600
    hours_since_booking = (now - created).total_seconds() / 3600
    
    pct, reason = 0, ''
    if policy == 'flexible':
        if hours_to_checkin >= 24:
            pct, reason = 100, 'Flessibile: >24h dal check-in'
        else:
            pct, reason = 0, 'Flessibile: <24h dal check-in'
    elif policy == 'moderate':
        if hours_to_checkin >= 24 * 7:
            pct, reason = 100, 'Moderata: >7 giorni dal check-in'
        elif hours_to_checkin >= 24:
            pct, reason = 50, 'Moderata: 1-7 giorni dal check-in'
        else:
            pct, reason = 0, 'Moderata: <24h dal check-in'
    else:  # strict
        if hours_since_booking <= 48 and hours_to_checkin >= 24 * 14:
            pct, reason = 100, 'Rigorosa: entro 48h e >14gg'
        elif hours_to_checkin >= 24 * 7:
            pct, reason = 50, 'Rigorosa: >7 giorni dal check-in'
        else:
            pct, reason = 0, 'Rigorosa: <7 giorni o oltre 48h'
            
    refund = round((total_paid * pct) / 100, 2)
    return {'refund': refund, 'percent': pct, 'reason': reason}
