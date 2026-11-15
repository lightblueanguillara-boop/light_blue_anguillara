"""Esito della consegna delle email (webhook di Resend, firmato con Svix).

Configurazione nel pannello di Resend: Webhooks → https://<api>/api/webhook/resend, eventi email.delivered,
email.delivery_delayed, email.bounced, email.complained; la «Signing secret» va in RESEND_WEBHOOK_SECRET.
Mai tracciamento di aperture e clic.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from db import db

router = APIRouter()
TOLERANCE = 5 * 60
EVENTS = {'email.delivered': 'consegnata', 'email.delivery_delayed': 'in_ritardo',
          'email.bounced': 'tornata_indietro', 'email.complained': 'segnalata_spam'}
# un esito non torna indietro (gli eventi possono arrivare in disordine)
OVERWRITABLE = {'consegnata': [None, 'in_ritardo'], 'in_ritardo': [None, 'in_ritardo'],
                'tornata_indietro': [None, 'in_ritardo', 'consegnata'],
                'segnalata_spam': [None, 'in_ritardo', 'consegnata']}


def verify(secret: str, msg_id: str, timestamp: str, body: bytes, signatures: str, now: float = None) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((now or time.time()) - ts) > TOLERANCE:
        return False
    try:
        key = base64.b64decode(secret.removeprefix('whsec_'))
    except Exception:
        return False
    expected = base64.b64encode(hmac.new(key, f'{msg_id}.{ts}.'.encode() + body, hashlib.sha256).digest()).decode()
    for part in (signatures or '').split():
        version, _, sig = part.partition(',')
        if version == 'v1' and hmac.compare_digest(sig, expected):
            return True
    return False


@router.post("/webhook/resend")
async def resend_webhook(request: Request):
    secret = os.environ.get('RESEND_WEBHOOK_SECRET', '')
    if not secret:
        raise HTTPException(503, 'Webhook non configurato')
    body = await request.body()
    h = request.headers
    if not verify(secret, h.get('svix-id', ''), h.get('svix-timestamp', ''), body, h.get('svix-signature', '')):
        raise HTTPException(400, 'Firma non valida')
    try:
        event = json.loads(body)
    except ValueError:
        raise HTTPException(400, 'Contenuto non valido')
    state = EVENTS.get(event.get('type'))
    provider_id = (event.get('data') or {}).get('email_id')
    if not state or not provider_id:
        return {'ignored': True}
    detail = None
    if state == 'tornata_indietro':
        detail = ((event.get('data') or {}).get('bounce') or {}).get('type')   # solo il tipo di rimbalzo
    res = await db.email_outbox.update_one(
        {'provider_id': provider_id, 'delivery': {'$in': OVERWRITABLE[state]}},
        {'$set': {'delivery': state, 'delivery_at': datetime.now(timezone.utc).isoformat(),
                  'delivery_detail': detail}})
    if res.modified_count and state in ('tornata_indietro', 'segnalata_spam'):
        rec = await db.email_outbox.find_one({'provider_id': provider_id}, {'_id': 0})
        if rec and not (rec.get('key') or '').startswith(('gestore_', 'prova:')):
            from notifications import notify
            await notify('email_failed', to=rec['to'], subject=rec['subject'],
                         url=f"/admin/bookings?open={rec['booking_id']}" if rec.get('booking_id') else None,
                         tag=f"bounce:{provider_id}")
        logging.info(f"Email {rec.get('key') if rec else ''} {state}")
    return {'ok': True}
