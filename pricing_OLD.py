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
    """Pick highest-priority seasonal rate matching the date; fallback to default."""
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
        except Exception:
            continue
    if best:
        return float(best['price_per_night'])
    return float(settings.get('default_price_per_night', 250.0))


async def compute_stay_pricing(check_in: str, check_out: str):
    settings = await get_settings()
    total = 0.0
    nights = 0
    breakdown = []
    for d in daterange(check_in, check_out):
        p = price_for_date(d, settings)
        total += p
        nights += 1
        breakdown.append({'date': d.isoformat(), 'price': p})
    deposit_pct = settings.get('deposit_percent', 30.0)
    deposit = round(total * deposit_pct / 100.0, 2)
    return {
        'nights': nights,
        'total': round(total, 2),
        'deposit_amount': deposit,
        'deposit_percent': deposit_pct,
        'breakdown': breakdown,
    }


async def dates_available(check_in: str, check_out: str, exclude_id: Optional[str] = None) -> bool:
    bookings = await db.bookings.find(
        {'status': {'$in': ['pending', 'confirmed', 'external']}}, {'_id': 0}
    ).to_list(10000)
    ci = datetime.strptime(check_in, '%Y-%m-%d').date()
    co = datetime.strptime(check_out, '%Y-%m-%d').date()
    for b in bookings:
        if exclude_id and b.get('id') == exclude_id:
            continue
        bci = datetime.strptime(b['check_in'], '%Y-%m-%d').date()
        bco = datetime.strptime(b['check_out'], '%Y-%m-%d').date()
        if ci < bco and bci < co:
            return False
    return True


def compute_refund_amount(booking: dict) -> dict:
    """Airbnb-style policy:
    - flexible: 100% fino a 24h prima del check-in; 0% oltre
    - moderate: 100% fino a 5 giorni prima del check-in; 50% fino a 24h; 0% oltre
    - strict:   100% entro 48h dalla prenotazione E >=14gg prima del check-in;
                50% fino a 7gg prima; 0% oltre
    """
    policy = booking.get('cancellation_policy', 'moderate')
    total_paid = 0.0
    if booking.get('payment_status') == 'deposit_paid':
        total_paid = booking.get('deposit_amount', 0)
    elif booking.get('payment_status') == 'fully_paid':
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
        if hours_to_checkin >= 24 * 5:
            pct, reason = 100, 'Moderata: >5 giorni dal check-in'
        elif hours_to_checkin >= 24:
            pct, reason = 50, 'Moderata: 1-5 giorni dal check-in'
        else:
            pct, reason = 0, 'Moderata: <24h dal check-in'
    else:  # strict
        if hours_since_booking <= 48 and hours_to_checkin >= 24 * 14:
            pct, reason = 100, 'Rigorosa: entro 48h dalla prenotazione e >14 giorni dal check-in'
        elif hours_to_checkin >= 24 * 7:
            pct, reason = 50, 'Rigorosa: >7 giorni dal check-in'
        else:
            pct, reason = 0, 'Rigorosa: condizioni non soddisfatte'
    refund = round(total_paid * pct / 100.0, 2)
    return {'refund': refund, 'percent': pct, 'reason': reason, 'paid': total_paid}
