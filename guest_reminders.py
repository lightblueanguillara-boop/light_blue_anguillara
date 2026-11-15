"""Invii automatici all'ospite, chiamati ogni 5 minuti dall'orologio interno del server.

Ogni messaggio parte UNA VOLTA SOLA per prenotazione: la prenotazione viene «prenotata» con un
aggiornamento atomico (campo reminders.<chiave>) prima dell'invio, quindi anche due giri in parallelo
non mandano doppioni. Se le date cambiano i campi si azzerano e il promemoria ripartirà per le date nuove.
Orari e anticipo si regolano dal pannello (impostazione `reminders`), ognuno con il suo interruttore.
"""
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import messaging
import whatsapp_queue
from db import db, get_settings
from notifications import notify

ROME = ZoneInfo('Europe/Rome')
MAX_PER_RUN = 50
DEFAULTS = {
    'promemoria_saldo': {'enabled': True, 'days': 7, 'time': '10:00'},
    'istruzioni_arrivo': {'enabled': True, 'days': 2, 'time': '10:00'},
    'promemoria_checkout': {'enabled': True, 'time': '18:00'},
    'grazie_recensione': {'enabled': True, 'time': '11:00'},
    'whatsapp_da_inviare': {'enabled': True, 'time': '19:00'},
}
# solo prenotazioni vere fatte dal sito o inserite dal proprietario (mai Airbnb/Booking o blocchi)
BASE = {'status': 'confirmed', 'source': {'$in': ['website', 'manual']}}


def _conf(settings: dict, key: str) -> dict:
    return {**DEFAULTS[key], **((settings.get('reminders') or {}).get(key) or {})}


def _reached(conf: dict, local: datetime) -> bool:
    return bool(conf.get('enabled', True)) and local.strftime('%H:%M') >= conf.get('time', '10:00')


async def _each(query: dict, key: str, settings: dict, now: datetime, for_date_field: str) -> int:
    sent = 0
    for _ in range(MAX_PER_RUN):
        b = await db.bookings.find_one_and_update(
            {**query, f'reminders.{key}': {'$exists': False}},
            {'$set': {f'reminders.{key}': now.isoformat()}},
            projection={'_id': 0})
        if not b:
            break
        await messaging.invia(key, b, settings=settings, for_date=b.get(for_date_field))
        sent += 1
    return sent


async def run(now: datetime = None) -> dict:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(ROME)
    today = local.date()
    day = lambda n: (today + timedelta(days=n)).isoformat()   # noqa: E731
    settings = await get_settings()
    done = {}

    c = _conf(settings, 'promemoria_saldo')
    if _reached(c, local):
        done['promemoria_saldo'] = await _each(
            {**BASE, 'payment_status': 'deposit_paid', 'check_in': {'$gt': day(0), '$lte': day(int(c['days']))}},
            'promemoria_saldo', settings, now, 'check_in')

    c = _conf(settings, 'istruzioni_arrivo')
    if _reached(c, local):
        done['istruzioni_arrivo'] = await _each(
            {**BASE, 'check_in': {'$gte': day(0), '$lte': day(int(c['days']))}},
            'istruzioni_arrivo', settings, now, 'check_in')

    c = _conf(settings, 'promemoria_checkout')
    if _reached(c, local):
        done['promemoria_checkout'] = await _each(
            {**BASE, 'check_out': day(1)}, 'promemoria_checkout', settings, now, 'check_out')

    c = _conf(settings, 'grazie_recensione')
    if _reached(c, local):
        done['grazie_recensione'] = await _each(
            {**BASE, 'check_out': {'$gte': day(-2), '$lte': day(-1)}}, 'grazie_recensione', settings, now, 'check_out')

    c = _conf(settings, 'whatsapp_da_inviare')
    if _reached(c, local) and settings.get('whatsapp_notice_day') != today.isoformat():
        claimed = await db.settings.update_one(
            {'id': 'global', 'whatsapp_notice_day': settings.get('whatsapp_notice_day')},
            {'$set': {'whatsapp_notice_day': today.isoformat()}})
        n = await whatsapp_queue.pending_count()
        if claimed.modified_count and n:
            await notify('whatsapp_pending', count=n, tag=today.isoformat())
            done['whatsapp_da_inviare'] = n

    await _cleanup(now)
    if any(done.values()):
        logging.info(f'Invii automatici: {done}')
    return done


async def _cleanup(now: datetime) -> None:
    """Privacy: il registro email e i WhatsApp già gestiti non si tengono per sempre."""
    await db.email_outbox.delete_many({'created_at': {'$lt': (now - timedelta(days=400)).isoformat()}})
    await db.whatsapp_queue.delete_many({'status': {'$ne': whatsapp_queue.PENDING},
                                         'updated_at': {'$lt': (now - timedelta(days=180)).isoformat()}})
