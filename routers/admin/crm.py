"""CRM: subscribers + marketing email blasts."""
import asyncio
import uuid
from datetime import datetime, timezone

import resend
from fastapi import APIRouter, Depends

from auth import get_current_admin
from db import db, SENDER_EMAIL
from models import MarketingEmail

router = APIRouter()


@router.get("/admin/subscribers")
async def list_subscribers(admin=Depends(get_current_admin)):
    return await db.subscribers.find({}, {'_id': 0}).sort('created_at', -1).to_list(10000)


@router.delete("/admin/subscribers/{sub_id}")
async def delete_subscriber(sub_id: str, admin=Depends(get_current_admin)):
    await db.subscribers.delete_one({'id': sub_id})
    return {'ok': True}


@router.post("/admin/marketing/send")
async def send_marketing(payload: MarketingEmail, admin=Depends(get_current_admin)):
    subs = await db.subscribers.find({'consent': True}, {'_id': 0}).to_list(10000)
    if payload.recipient_ids:
        subs = [s for s in subs if s['id'] in payload.recipient_ids]
    sent, failed, errors = 0, 0, []
    for s in subs:
        try:
            params = {
                'from': SENDER_EMAIL,
                'to': [s['email']],
                'subject': payload.subject,
                'html': payload.html_content,
            }
            await asyncio.to_thread(resend.Emails.send, params)
            sent += 1
        except Exception as e:
            failed += 1
            errors.append(str(e))
    log_doc = {
        'id': str(uuid.uuid4()),
        'subject': payload.subject,
        'sent_count': sent,
        'failed_count': failed,
        'total': len(subs),
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    await db.marketing_logs.insert_one(log_doc.copy())
    return {'sent': sent, 'failed': failed, 'total': len(subs), 'errors': errors[:3]}


@router.get("/admin/marketing/logs")
async def marketing_logs(admin=Depends(get_current_admin)):
    return await db.marketing_logs.find({}, {'_id': 0}).sort('created_at', -1).to_list(500)


@router.delete("/admin/marketing/logs/{log_id}")
async def delete_marketing_log(log_id: str, admin=Depends(get_current_admin)):
    await db.marketing_logs.delete_one({'id': log_id})
    return {'ok': True}
