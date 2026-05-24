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
    """Calcola il prezzo base (Rimborsabile) per un soggiorno."""
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


async def compute_dual_pricing(check_in: str, check_out: str):
    """
    Calcola entrambe le tariffe: Rimborsabile e Non Rimborsabile.
    
    Ritorna un dizionario con:
    - refundable_total: prezzo pieno (Rimborsabile)
    - non_refundable_total: prezzo scontato (Non Rimborsabile)
    - nights: numero di notti
    - deposit_amount: acconto calcolato sul totale rimborsabile
    - discount_percent: percentuale di sconto applicata
    - breakdown: elenco prezzi giornalieri
    """
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
    
    # Calcolo sconto per tariffa non rimborsabile
    discount_pct = settings.get('non_refundable_discount_percent', 5.0)
    non_refundable_total = round(total * (1 - discount_pct / 100.0), 2)
    
    return {
        'nights': nights,
        'refundable_total': round(total, 2),
        'non_refundable_total': non_refundable_total,
        'deposit_amount': deposit,
        'deposit_percent': deposit_pct,
        'discount_percent': discount_pct,
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
    """
    Logica semplificata di rimborso basata su is_refundable:
    - is_refundable = True:  100% rimborso se cancellato almeno 10 giorni prima del check-in; 0% altrimenti
    - is_refundable = False: 0% sempre
    """
    is_refundable = booking.get('is_refundable', True)
    total_paid = 0.0
    if booking.get('payment_status') == 'deposit_paid':
        total_paid = booking.get('deposit_amount', 0)
    elif booking.get('payment_status') == 'fully_paid':
        total_paid = booking.get('total_price', 0)
    
    # Se non è rimborsabile, rimborso sempre 0
    if not is_refundable:
        return {
            'refund': 0.0,
            'percent': 0,
            'reason': 'Tariffa Non Rimborsabile: nessun rimborso',
            'paid': total_paid
        }
    
    # Se è rimborsabile, applica la regola dei 10 giorni
    now = datetime.now(timezone.utc)
    try:
        check_in = datetime.strptime(booking['check_in'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
    except (KeyError, ValueError, TypeError):
        return {'refund': 0.0, 'percent': 0, 'reason': 'Data check-in non valida', 'paid': total_paid}
    
    days_to_checkin = (check_in - now).total_seconds() / (3600 * 24)
    
    pct, reason = 0, ''
    if days_to_checkin >= 10:
        pct, reason = 100, 'Rimborsabile: >10 giorni dal check-in'
    else:
        pct, reason = 0, 'Rimborsabile: <10 giorni dal check-in'
    
    refund = round(total_paid * pct / 100.0, 2)
    return {'refund': refund, 'percent': pct, 'reason': reason, 'paid': total_paid}
