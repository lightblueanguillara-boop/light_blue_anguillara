"""Calendario admin: prezzi per notte, prezzi personalizzati e blocco date."""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_admin
from db import db, get_settings
from email_helpers import _it_date
from models import BlockCreate, PriceUpdate, _validate_iso_date
from pricing import BLOCKING_STATUSES, daterange, load_price_overrides, rate_for_date

router = APIRouter()
MAX_RANGE_DAYS = 400


def _parse_range(start: str, end: str):
    try:
        _validate_iso_date(start)
        _validate_iso_date(end)
    except ValueError:
        raise HTTPException(400, 'Date non valide (formato AAAA-MM-GG)')
    s = datetime.strptime(start, '%Y-%m-%d').date()
    e = datetime.strptime(end, '%Y-%m-%d').date()
    if e <= s or (e - s).days > MAX_RANGE_DAYS:
        raise HTTPException(400, 'Intervallo non valido')
    return s, e


@router.get("/admin/calendar")
async def calendar_days(start: str, end: str, admin=Depends(get_current_admin)):
    """Prezzo di ogni notte in [start, end) con la sua origine (personalizzato, stagionale, base)."""
    _parse_range(start, end)
    settings = await get_settings()
    overrides = await load_price_overrides(start, end)
    days = []
    for d in daterange(start, end):
        key = d.isoformat()
        base, rate_name = rate_for_date(d, settings)
        if key in overrides:
            days.append({'date': key, 'price': overrides[key], 'source': 'custom', 'base_price': base, 'rate_name': rate_name})
        else:
            days.append({'date': key, 'price': base, 'source': 'seasonal' if rate_name else 'default',
                         'base_price': base, 'rate_name': rate_name})
    return {'days': days}


@router.put("/admin/calendar/prices")
async def set_prices(payload: PriceUpdate, admin=Depends(get_current_admin)):
    """Imposta (o ripristina, con price=null) il prezzo delle notti selezionate."""
    keys = [d.isoformat() for d in daterange(payload.start, payload.end)]
    if payload.price is None:
        await db.price_overrides.delete_many({'date': {'$in': keys}})
    else:
        now = datetime.now(timezone.utc).isoformat()
        for k in keys:
            await db.price_overrides.update_one(
                {'date': k}, {'$set': {'date': k, 'price': round(payload.price, 2), 'updated_at': now}}, upsert=True)
    return {'ok': True, 'nights': len(keys), 'price': payload.price}


@router.post("/admin/blocks")
async def create_block(payload: BlockCreate, admin=Depends(get_current_admin)):
    """Chiude delle date: non prenotabili dal sito ed esportate come occupate verso Airbnb/Booking."""
    conflict = await db.bookings.find_one({
        'status': {'$in': BLOCKING_STATUSES},
        'check_in': {'$lt': payload.check_out},
        'check_out': {'$gt': payload.check_in},
    }, {'_id': 0})
    if conflict:
        raise HTTPException(
            409,
            f"Le date si sovrappongono con {conflict.get('guest_name', 'un’altra prenotazione')} "
            f"({_it_date(conflict.get('check_in'))} → {_it_date(conflict.get('check_out'))})")
    block = {
        'id': f"blk_{uuid.uuid4().hex[:12]}",
        'guest_name': 'Date bloccate',
        'guest_email': '',
        'check_in': payload.check_in,
        'check_out': payload.check_out,
        'adults': 0,
        'children': 0,
        'total_price': 0,
        'deposit_amount': 0,
        'status': 'blocked',
        'payment_status': 'unpaid',
        'source': 'block',
        'notes': (payload.note or '').strip() or None,
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    await db.bookings.insert_one(block)
    block.pop('_id', None)
    return {'ok': True, 'block': block}


@router.delete("/admin/blocks/{block_id}")
async def delete_block(block_id: str, admin=Depends(get_current_admin)):
    res = await db.bookings.delete_one({'id': block_id, 'status': 'blocked'})
    if not res.deleted_count:
        raise HTTPException(404, 'Blocco non trovato')
    return {'ok': True}


