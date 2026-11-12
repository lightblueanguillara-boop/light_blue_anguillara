"""Notifiche sul telefono: iscrizione dei dispositivi, prova, elenco e rimozione."""
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import notifications
from auth import get_current_admin
from db import db, VAPID_PUBLIC_KEY
from models import PushSubscribe
from ratelimit import rate_limit

router = APIRouter()


def device_label(ua: str) -> str:
    """Nome leggibile del dispositivo dallo User-Agent (es. "iPhone, Safari")."""
    ua = ua or ''
    os_ = ('iPhone' if 'iPhone' in ua else 'iPad' if 'iPad' in ua else 'Android' if 'Android' in ua
           else 'Mac' if 'Macintosh' in ua else 'Windows' if 'Windows' in ua else 'Linux' if 'Linux' in ua else 'dispositivo')
    br = ('Edge' if 'Edg/' in ua else 'Samsung Internet' if 'SamsungBrowser' in ua else 'Chrome' if ('Chrome/' in ua or 'CriOS' in ua)
          else 'Firefox' if ('Firefox/' in ua or 'FxiOS' in ua) else 'Safari' if 'Safari/' in ua else 'browser')
    return f'{os_}, {br}'


@router.get("/admin/push/config")
async def push_config(admin=Depends(get_current_admin)):
    return {'enabled': notifications.push_enabled(), 'public_key': VAPID_PUBLIC_KEY}


@router.get("/admin/push/subscriptions")
async def list_subscriptions(admin=Depends(get_current_admin)):
    subs = await db.push_subscriptions.find({}, {'_id': 0, 'keys': 0}).sort('created_at', 1).to_list(50)
    return [{k: s.get(k) for k in ('id', 'label', 'created_at', 'last_success_at', 'endpoint')} for s in subs]


@router.post("/admin/push/subscribe")
async def subscribe(payload: PushSubscribe, request: Request, admin=Depends(get_current_admin)):
    if not notifications.push_enabled():
        raise HTTPException(503, 'Notifiche non configurate sul server')
    if not notifications.valid_push_endpoint(payload.endpoint):
        raise HTTPException(400, 'Servizio di notifica non riconosciuto')
    label = (payload.label or '').strip() or device_label(request.headers.get('user-agent', ''))
    existing = await db.push_subscriptions.find_one({'endpoint': payload.endpoint}, {'_id': 0, 'id': 1})
    sub_id = existing['id'] if existing else uuid.uuid4().hex[:12]
    await db.push_subscriptions.update_one(
        {'endpoint': payload.endpoint},
        {'$set': {'id': sub_id, 'endpoint': payload.endpoint, 'keys': payload.keys, 'label': label[:120],
                  'admin_email': admin['email'], 'failures': 0},
         '$setOnInsert': {'created_at': datetime.now(timezone.utc).isoformat()}},
        upsert=True,
    )
    return {'ok': True, 'id': sub_id, 'label': label}


@router.delete("/admin/push/subscriptions/{sub_id}")
async def unsubscribe(sub_id: str, admin=Depends(get_current_admin)):
    res = await db.push_subscriptions.delete_one({'id': sub_id})
    if not res.deleted_count:
        raise HTTPException(404, 'Dispositivo non trovato')
    return {'ok': True}


class TestRequest(BaseModel):
    id: Optional[str] = None


@router.post("/admin/push/test")
async def push_test(payload: TestRequest, request: Request, admin=Depends(get_current_admin)):
    rate_limit(request, 'push-test', 10, 600)
    sent = await notifications.send_test(payload.id)
    if not sent:
        raise HTTPException(400, 'Nessun dispositivo ha ricevuto la notifica: riattiva le notifiche su questo telefono')
    return {'ok': True, 'sent': sent}


@router.get("/admin/push/badge")
async def badge(admin=Depends(get_current_admin)):
    return {'count': await notifications.badge_count()}
