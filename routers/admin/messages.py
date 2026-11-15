"""Pannello: modelli dei messaggi, registro email, coda WhatsApp, pulsante WhatsApp nelle schede."""
import base64
from datetime import date, datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import message_templates as T
import messaging
import short_links
import whatsapp_queue as W
from auth import get_current_admin
from calendar_event import build_ics
from db import db, get_settings, ADMIN_NOTIFY_EMAIL
from email_helpers import email_service_status, send_email_async
from ratelimit import rate_limit

router = APIRouter()
Channel = Literal['email', 'whatsapp']


# ------------------------------------------------------------------ modelli
def _public(t: dict) -> dict:
    return {
        'key': t['key'], 'channel': t['channel'], 'title': T.TITLES.get(t['key'], t['key']),
        'when': T.INFO.get(t['key'], ''), 'recipient': t.get('recipient', 'ospite'),
        'subject': t.get('subject'), 'body': t['body'], 'active': t.get('active', True),
        'modified': t.get('body') != t.get('original_body') or t.get('subject') != t.get('original_subject'),
        'variables': T.allowed_vars(t['key']), 'order': t.get('order', 0),
    }


async def _template(key: str, channel: str) -> dict:
    t = await db.message_templates.find_one({'key': key, 'channel': channel}, {'_id': 0})
    if not t:
        raise HTTPException(404, 'Modello non trovato')
    return t


@router.get("/admin/messages/templates")
async def list_templates(admin=Depends(get_current_admin)):
    docs = await db.message_templates.find({}, {'_id': 0}).sort('order', 1).to_list(200)
    return [_public(t) for t in docs]


class TemplateUpdate(BaseModel):
    subject: Optional[str] = Field(default=None, max_length=200)
    body: Optional[str] = Field(default=None, min_length=1, max_length=6000)
    active: Optional[bool] = None


def _check(key: str, channel: str, subject, body):
    if channel == 'email' and subject is not None and not subject.strip():
        raise HTTPException(400, "L'oggetto dell'email non può essere vuoto")
    bad = messaging.unknown_vars(key, subject or '', body or '')
    if bad:
        raise HTTPException(400, 'Dati non disponibili in questo messaggio: '
                                 + ', '.join('{' + b + '}' for b in bad) + '. Usa solo quelli elencati.')


@router.put("/admin/messages/templates/{key}/{channel}")
async def update_template(key: str, channel: Channel, payload: TemplateUpdate, admin=Depends(get_current_admin)):
    t = await _template(key, channel)
    patch = payload.model_dump(exclude_unset=True)
    if channel == 'whatsapp':
        patch.pop('subject', None)
    _check(key, channel, patch.get('subject', t.get('subject')), patch.get('body', t['body']))
    if patch:
        patch['updated_at'] = datetime.now(timezone.utc).isoformat()
        await db.message_templates.update_one({'key': key, 'channel': channel}, {'$set': patch})
    return _public(await _template(key, channel))


@router.post("/admin/messages/templates/{key}/{channel}/reset")
async def reset_template(key: str, channel: Channel, admin=Depends(get_current_admin)):
    t = await _template(key, channel)
    await db.message_templates.update_one({'key': key, 'channel': channel}, {'$set': {
        'subject': t.get('original_subject'), 'body': t['original_body'],
        'updated_at': datetime.now(timezone.utc).isoformat()}})
    return _public(await _template(key, channel))


class PreviewRequest(BaseModel):
    key: str
    channel: Channel
    subject: Optional[str] = Field(default=None, max_length=200)
    body: str = Field(min_length=1, max_length=6000)


async def _render_sample(p: PreviewRequest) -> dict:
    if p.key not in T.KEYS:
        raise HTTPException(404, 'Modello non trovato')
    _check(p.key, p.channel, p.subject, p.body)
    settings = await get_settings()
    variables = messaging.sample_vars(p.key, settings)
    if p.channel == 'whatsapp':
        return {'text': await short_links.shorten_text(messaging.render(p.body, variables))}
    booking = None if p.key in T.CONTACT_KEYS else messaging.SAMPLE_BOOKING
    subject, html, text, attachments = await messaging.build_template_email(
        p.key, variables, settings, booking, subject=p.subject or '', body=p.body)
    return {'subject': subject, 'html': html, 'text': text, 'attachments': [a['filename'] for a in attachments],
            '_attachments': attachments}


@router.post("/admin/messages/preview")
async def preview(p: PreviewRequest, admin=Depends(get_current_admin)):
    out = await _render_sample(p)
    out.pop('_attachments', None)
    return out


@router.post("/admin/messages/test")
async def send_test(p: PreviewRequest, request: Request, admin=Depends(get_current_admin)):
    """«Invia una prova a me»: l'email arriva al proprietario; per WhatsApp si apre la chat con il proprio numero."""
    rate_limit(request, 'message-test', 20, 3600)
    out = await _render_sample(p)
    if p.channel == 'whatsapp':
        settings = await get_settings()
        phone = W.normalize_phone(settings.get('owner_whatsapp') or messaging._clean_phone(settings))
        if not phone:
            raise HTTPException(400, 'Imposta il tuo numero WhatsApp nelle impostazioni dei messaggi')
        return {'link': W.whatsapp_link(phone, out['text'], settings.get('whatsapp_open_with', 'auto')),
                'text': out['text']}
    if not ADMIN_NOTIFY_EMAIL:
        raise HTTPException(400, "Manca l'email del proprietario (ADMIN_NOTIFY_EMAIL)")
    ok = await send_email_async(ADMIN_NOTIFY_EMAIL, f"[Prova] {out['subject']}", out['html'],
                                attachments=out['_attachments'] or None, text=out['text'], key=f'prova:{p.key}')
    status = email_service_status()
    return {'ok': ok, 'to': ADMIN_NOTIFY_EMAIL,
            'reason': None if ok else ('non_attivo' if not status['active'] else 'errore')}


@router.get("/admin/messages/status")
async def messages_status(admin=Depends(get_current_admin)):
    return {**email_service_status(), 'owner_email': bool(ADMIN_NOTIFY_EMAIL),
            'whatsapp_pending': await W.pending_count()}


# ------------------------------------------------------------------ registro email
LIST_FIELDS = {'_id': 0, 'html': 0, 'text': 0}
# email che parlano di una situazione precisa: si possono rimandare solo se è ancora quella
STATUS_FOR_KEY = {'richiesta_accettata': 'awaiting_payment', 'prenotazione_confermata': 'confirmed',
                  'prenotazione_modificata': 'confirmed', 'istruzioni_arrivo': 'confirmed',
                  'promemoria_saldo': 'confirmed'}


@router.get("/admin/email/outbox")
async def outbox(limit: int = 100, booking_id: Optional[str] = None, admin=Depends(get_current_admin)):
    q = {'booking_id': booking_id} if booking_id else {}
    return await db.email_outbox.find(q, LIST_FIELDS).sort('created_at', -1).to_list(max(1, min(limit, 500)))


@router.get("/admin/email/view/{email_id}")
async def view_email(email_id: str, admin=Depends(get_current_admin)):
    rec = await db.email_outbox.find_one({'id': email_id}, {'_id': 0})
    if not rec:
        raise HTTPException(404, 'Email non trovata')
    return rec


async def _resend_problem(rec: dict):
    """Motivo per cui un'email NON si può rimandare (None = si può)."""
    newer = await db.email_outbox.find_one({'key': rec.get('key'), 'booking_id': rec.get('booking_id'),
                                            'message_id': rec.get('message_id'),
                                            'created_at': {'$gt': rec['created_at']}}, {'_id': 0, 'id': 1})
    if newer and (rec.get('booking_id') or rec.get('message_id')):
        return "C'è già un'email più recente dello stesso tipo"
    need = STATUS_FOR_KEY.get(rec.get('key'))
    if need and rec.get('booking_id'):
        b = await db.bookings.find_one({'id': rec['booking_id']}, {'_id': 0})
        if not b or b.get('status') != need:
            return 'La prenotazione nel frattempo è cambiata: questa email non è più valida'
        if (b.get('check_out') or '') < date.today().isoformat():
            return 'Il soggiorno è già concluso'
        if (b.get('updated_at') or '') > rec['created_at']:
            return 'La prenotazione è stata modificata dopo questa email: i dati non sono più aggiornati'
        if need == 'awaiting_payment' and (b.get('payment_due_at') or '') < datetime.now(timezone.utc).isoformat():
            return 'Il link di pagamento è scaduto'
    return None


async def _with_status(rec: dict) -> dict:
    problem = await _resend_problem(rec)
    return {**rec, 'can_resend': problem is None, 'resend_problem': problem}


@router.get("/admin/email/booking/{booking_id}")
async def booking_emails(booking_id: str, admin=Depends(get_current_admin)):
    """Ultima email di ogni tipo per una prenotazione, con lo stato e la possibilità di rimandarla."""
    latest = {}
    for rec in await db.email_outbox.find({'booking_id': booking_id}, LIST_FIELDS).sort('created_at', -1).to_list(200):
        latest.setdefault(rec.get('key'), rec)
    return [await _with_status(r) for r in sorted(latest.values(), key=lambda r: r['created_at'], reverse=True)]


@router.post("/admin/email/{email_id}/resend")
async def resend_email(email_id: str, request: Request, admin=Depends(get_current_admin)):
    rate_limit(request, 'email-resend', 30, 3600)
    rec = await db.email_outbox.find_one({'id': email_id}, {'_id': 0})
    if not rec:
        raise HTTPException(404, 'Email non trovata')
    problem = await _resend_problem(rec)
    if problem:
        raise HTTPException(409, problem)
    attachments = None
    method = T.CALENDAR_KEYS.get(rec.get('key'))
    if method and rec.get('booking_id'):
        b = await db.bookings.find_one({'id': rec['booking_id']}, {'_id': 0})
        if b:
            ics = build_ics(b, await get_settings(), method)
            attachments = [{'filename': 'prenotazione.ics', 'content': base64.b64encode(ics).decode(),
                            'content_type': 'text/calendar'}]
    ok = await send_email_async(rec['to'], rec['subject'], rec['html'], attachments=attachments, text=rec.get('text'),
                                key=rec.get('key'), booking_id=rec.get('booking_id'),
                                message_id=rec.get('message_id'), reply_to=rec.get('reply_to'), resent_from=rec['id'])
    latest = await db.email_outbox.find_one({'resent_from': rec['id']}, LIST_FIELDS, sort=[('created_at', -1)])
    return {'ok': ok, 'email': latest}


# ------------------------------------------------------------------ WhatsApp
async def _twin_email(item: dict):
    if not item.get('booking_id'):
        return None
    return await db.email_outbox.find_one({'booking_id': item['booking_id'], 'key': item['key']},
                                          {'_id': 0, 'status': 1, 'reason': 1, 'delivery': 1, 'created_at': 1,
                                           'sent_at': 1, 'delivery_at': 1}, sort=[('created_at', -1)])


async def _queue_item(item: dict, open_with: str) -> dict:
    b = await db.bookings.find_one({'id': item['booking_id']}, {'_id': 0, 'check_in': 1, 'check_out': 1,
                                                                 'status': 1}) if item.get('booking_id') else None
    return {**item, 'title': T.TITLES.get(item['key'], item['key']),
            'link': W.whatsapp_link(item['phone'], item['text'], open_with),
            'web_link': W.whatsapp_link(item['phone'], item['text'], 'web'),
            'sms_link': W.sms_link(item['phone'], item['text']),
            'booking': b, 'email': await _twin_email(item)}


@router.get("/admin/whatsapp/queue")
async def whatsapp_list(status: Literal['pending', 'done'] = 'pending', admin=Depends(get_current_admin)):
    open_with = (await get_settings()).get('whatsapp_open_with', 'auto')
    if status == 'pending':
        items = await db.whatsapp_queue.find({'status': W.PENDING}, {'_id': 0}).sort(
            [('for_date', 1), ('created_at', 1)]).to_list(500)
    else:
        items = await db.whatsapp_queue.find({'status': {'$in': [W.SENT, W.REMOVED]}}, {'_id': 0}).sort(
            'updated_at', -1).to_list(50)
    return [await _queue_item(i, open_with) for i in items]


@router.get("/admin/whatsapp/count")
async def whatsapp_count(admin=Depends(get_current_admin)):
    return {'pending': await W.pending_count()}


class QueueText(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


async def _queue_get(item_id: str) -> dict:
    item = await db.whatsapp_queue.find_one({'id': item_id}, {'_id': 0})
    if not item:
        raise HTTPException(404, 'Messaggio non trovato')
    return item


async def _queue_set(item_id: str, **fields) -> dict:
    fields['updated_at'] = datetime.now(timezone.utc).isoformat()
    await db.whatsapp_queue.update_one({'id': item_id}, {'$set': fields})
    open_with = (await get_settings()).get('whatsapp_open_with', 'auto')
    return await _queue_item(await _queue_get(item_id), open_with)


@router.patch("/admin/whatsapp/{item_id}")
async def whatsapp_edit(item_id: str, payload: QueueText, admin=Depends(get_current_admin)):
    item = await _queue_get(item_id)
    if item['status'] != W.PENDING:
        raise HTTPException(409, 'Questo messaggio è già stato gestito')
    return await _queue_set(item_id, text=await short_links.shorten_text(payload.text.strip()))


@router.post("/admin/whatsapp/{item_id}/sent")
async def whatsapp_sent(item_id: str, admin=Depends(get_current_admin)):
    await _queue_get(item_id)
    return await _queue_set(item_id, status=W.SENT, sent_at=datetime.now(timezone.utc).isoformat())


@router.post("/admin/whatsapp/{item_id}/remove")
async def whatsapp_remove(item_id: str, admin=Depends(get_current_admin)):
    await _queue_get(item_id)
    return await _queue_set(item_id, status=W.REMOVED, removed_reason='tolto')


@router.post("/admin/whatsapp/{item_id}/restore")
async def whatsapp_restore(item_id: str, admin=Depends(get_current_admin)):
    """Annulla «Segnato come inviato» o «Togli»: il messaggio torna da inviare."""
    await _queue_get(item_id)
    return await _queue_set(item_id, status=W.PENDING, sent_at=None, removed_reason=None)


class LinkRequest(BaseModel):
    booking_id: str
    key: Optional[str] = None
    text: Optional[str] = Field(default=None, max_length=4000)


@router.post("/admin/whatsapp/link")
async def whatsapp_link(payload: LinkRequest, admin=Depends(get_current_admin)):
    """Pulsante WhatsApp nella scheda: testo pronto (da un modello o libero) e link. Non mette nulla in coda."""
    b = await db.bookings.find_one({'id': payload.booking_id}, {'_id': 0})
    if not b:
        raise HTTPException(404, 'Prenotazione non trovata')
    settings = await get_settings()
    if payload.text is not None:
        text = payload.text
    else:
        key = payload.key or _suggested_key(b)
        tpl = await db.message_templates.find_one({'key': key, 'channel': 'whatsapp'}, {'_id': 0}) if key else None
        variables = messaging.booking_vars(b, settings)
        text = messaging.render(tpl['body'], variables) if tpl else f"Ciao {variables['nome']} 🌊\n\n\n\n_{variables['firma']}_"
    text = await short_links.shorten_text(text)
    phone = W.normalize_phone(b.get('guest_phone'))
    open_with = settings.get('whatsapp_open_with', 'auto')
    return {'text': text, 'phone': phone, 'key': payload.key or _suggested_key(b),
            'link': W.whatsapp_link(phone, text, open_with) if phone else '',
            'web_link': W.whatsapp_link(phone, text, 'web') if phone else '',
            'sms_link': W.sms_link(phone, text) if phone else '',
            'consent': messaging.whatsapp_allowed(b),
            'templates': [{'key': k, 'title': T.TITLES[k]} for k in T.KEYS
                          if any(t['key'] == k and t['channel'] == 'whatsapp' for t in T.SEED)]}


def _suggested_key(b: dict):
    status = b.get('status')
    if status == 'awaiting_payment':
        return 'richiesta_accettata'
    if status == 'rejected':
        return 'richiesta_rifiutata'
    if status == 'cancelled':
        return 'prenotazione_cancellata'
    if status == 'confirmed':
        return 'istruzioni_arrivo' if (b.get('check_in') or '') >= date.today().isoformat() else 'grazie_recensione'
    return None
