"""WhatsApp «al tocco»: il sito prepara il messaggio, il proprietario lo apre e lo invia dal suo numero.

Niente API di Meta (costi, account Business verificato, modelli approvati). I messaggi vanno in una coda
nel pannello: «Apri WhatsApp» apre l'app con il testo pronto, poi il proprietario segna «Inviato».
L'interfaccia `Sender` permette un giorno di passare all'invio automatico senza toccare il resto.
"""
import logging
import re
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from db import db

PENDING, SENT, REMOVED = 'da_inviare', 'inviato', 'annullato'
OPEN_WITH = ('auto', 'app', 'web')


def normalize_phone(raw) -> str:
    """Numero internazionale di sole cifre (es. 393331234567), '' se non valido."""
    s = str(raw or '').strip()
    if not s:
        return ''
    digits = re.sub(r'\D', '', s)
    if s.startswith('+'):
        pass
    elif digits.startswith('00'):
        digits = digits[2:]
    elif digits.startswith('3') and len(digits) in (9, 10):      # cellulare italiano senza prefisso
        digits = '39' + digits
    elif digits.startswith('0') and 6 <= len(digits) <= 11:      # fisso italiano senza prefisso
        digits = '39' + digits
    return digits if 8 <= len(digits) <= 15 else ''


def whatsapp_link(phone: str, text: str, open_with: str = 'auto') -> str:
    """Link che apre WhatsApp con il testo pronto. Mai wa.me: il suo rimando rovina le emoji."""
    t = quote(text or '')
    if open_with == 'web':
        return f'https://web.whatsapp.com/send?phone={phone}&text={t}'
    if open_with == 'app':
        return f'whatsapp://send?phone={phone}&text={t}'
    return f'https://api.whatsapp.com/send?phone={phone}&text={t}'


def sms_link(phone: str, text: str) -> str:
    return f'sms:+{phone}?&body={quote(text or "")}'


class Sender:
    """Modo di consegna dei messaggi WhatsApp. Oggi solo «al tocco» (coda nel pannello)."""
    name = 'base'

    async def enqueue(self, msg: dict):
        raise NotImplementedError


class TapToSend(Sender):
    name = 'al_tocco'

    async def enqueue(self, msg: dict):
        # niente doppioni: se per la stessa prenotazione c'è già lo stesso messaggio da inviare, si aggiorna
        if msg.get('booking_id'):
            existing = await db.whatsapp_queue.find_one(
                {'booking_id': msg['booking_id'], 'key': msg['key'], 'status': PENDING}, {'_id': 0, 'id': 1})
            if existing:
                await db.whatsapp_queue.update_one(
                    {'id': existing['id']},
                    {'$set': {k: msg[k] for k in ('text', 'phone', 'name', 'for_date')} | {
                        'updated_at': datetime.now(timezone.utc).isoformat()}})
                return existing['id']
        await db.whatsapp_queue.insert_one(dict(msg))
        return msg['id']


class _Disabled(Sender):
    """Segnaposto per l'invio automatico futuro (Meta Business, SMS): per ora ripiega sulla coda."""
    async def enqueue(self, msg: dict):
        return await TapToSend().enqueue(msg)


SENDERS = {'al_tocco': TapToSend(), 'meta_business': _Disabled(), 'sms_gateway': _Disabled()}
sender: Sender = SENDERS['al_tocco']


async def ensure_indexes() -> None:
    await db.whatsapp_queue.create_index([('status', 1), ('for_date', 1)])
    await db.whatsapp_queue.create_index('booking_id')


async def enqueue(key: str, phone: str, text: str, *, name: str = None, booking_id: str = None,
                  for_date: str = None):
    """Mette un messaggio in coda. Non solleva mai errori (un WhatsApp non deve bloccare nulla)."""
    try:
        number = normalize_phone(phone)
        if not number or not (text or '').strip():
            return None
        now = datetime.now(timezone.utc).isoformat()
        return await sender.enqueue({
            'id': uuid.uuid4().hex, 'key': key, 'phone': number, 'text': text, 'name': name,
            'booking_id': booking_id, 'for_date': for_date, 'status': PENDING,
            'created_at': now, 'updated_at': now, 'sent_at': None,
        })
    except Exception:
        logging.exception(f'WhatsApp {key} non messo in coda')
        return None


async def cancel_for_booking(booking_id: str, keys=None, keep=()) -> int:
    """Toglie i messaggi ancora da inviare che non hanno più senso (prenotazione spostata o annullata)."""
    query = {'booking_id': booking_id, 'status': PENDING}
    if keys is not None:
        query['key'] = {'$in': list(keys)}
    elif keep:
        query['key'] = {'$nin': list(keep)}
    res = await db.whatsapp_queue.update_many(query, {'$set': {
        'status': REMOVED, 'removed_reason': 'superato', 'updated_at': datetime.now(timezone.utc).isoformat()}})
    return res.modified_count


async def pending_count() -> int:
    return await db.whatsapp_queue.count_documents({'status': PENDING})
