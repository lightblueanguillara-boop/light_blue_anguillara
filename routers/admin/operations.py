"""Operations: analytics, settings, iCal sync."""
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_admin
from db import db, get_settings
from ical_service import ical_sync_run
from models import SettingsUpdate
from pricing import daterange

router = APIRouter()


@router.get("/admin/analytics")
async def analytics(admin=Depends(get_current_admin)):
    bookings = await db.bookings.find(
        {'status': {'$in': ['confirmed', 'pending']}}, {'_id': 0}
    ).to_list(10000)
    now = datetime.now(timezone.utc)
    months = []
    for i in range(11, -1, -1):
        m = (now.replace(day=1) - timedelta(days=30 * i))
        months.append({
            'year': m.year, 'month': m.month, 'label': m.strftime('%b %Y'),
            'nights': 0, 'revenue': 0.0,
        })

    for b in bookings:
        nights_total = max(1, (datetime.strptime(b['check_out'], '%Y-%m-%d').date()
                               - datetime.strptime(b['check_in'], '%Y-%m-%d').date()).days)
        nightly = b['total_price'] / nights_total
        for d in daterange(b['check_in'], b['check_out']):
            for mm in months:
                if d.year == mm['year'] and d.month == mm['month']:
                    mm['nights'] += 1
                    mm['revenue'] += round(nightly, 2)

    total_revenue = sum(m['revenue'] for m in months)
    total_nights = sum(m['nights'] for m in months)
    confirmed_count = sum(1 for b in bookings if b['status'] == 'confirmed')
    pending_count = sum(1 for b in bookings if b['status'] == 'pending')
    messages_new = await db.contact_messages.count_documents({'status': 'new'})
    subs_count = await db.subscribers.count_documents({'consent': True})

    return {
        'monthly': months,
        'totals': {
            'revenue': round(total_revenue, 2),
            'nights': total_nights,
            'confirmed_bookings': confirmed_count,
            'pending_bookings': pending_count,
            'new_messages': messages_new,
            'subscribers': subs_count,
        },
    }


@router.get("/admin/settings")
async def get_admin_settings(admin=Depends(get_current_admin)):
    return await get_settings()


@router.put("/admin/settings")
async def update_admin_settings(
    updates: SettingsUpdate, admin=Depends(get_current_admin)
):
    patch = updates.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(400, 'No fields to update')
    await db.settings.update_one({'id': 'global'}, {'$set': patch}, upsert=True)
    return await get_settings()


@router.post("/admin/ical/sync")
async def ical_sync(admin=Depends(get_current_admin)):
    return await ical_sync_run()
