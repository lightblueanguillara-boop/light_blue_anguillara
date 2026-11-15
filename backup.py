"""Backup completo del database.

- Il file è un JSON compresso (.json.gz) con tutte le raccolte, nel formato esteso di MongoDB
  (conserva date, numeri e immagini del logo esattamente come sono).
- Si scarica dal pannello (Impostazioni → Backup dei dati) e parte in automatico per email
  all'indirizzo del proprietario (ADMIN_NOTIFY_EMAIL), ogni settimana o ogni giorno, alle 3 di notte:
  così una copia dei dati sta sempre fuori da Railway.
- Si ripristina con:  python scripts/restore_backup.py <file> --mongo-url ... --db ...
"""
import asyncio
import base64
import gzip
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from bson import json_util

from db import db, get_settings, ADMIN_NOTIFY_EMAIL

FORMAT = 'light-blue-backup'
VERSION = 1
# coda temporanea delle notifiche notturne: non serve ripristinarla
EXCLUDED = {'push_outbox'}
# oltre questa dimensione l'email parte senza allegato (limite Resend: 40 MB)
MAX_EMAIL_BYTES = 30 * 1024 * 1024
SEND_AFTER = time(3, 0)                       # ora italiana
ROME = ZoneInfo('Europe/Rome')
FREQUENCIES = {'daily': timedelta(hours=23), 'weekly': timedelta(days=7) - timedelta(hours=1)}


def backup_filename(now: datetime | None = None) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone(ROME)
    return f"light-blue-backup-{now:%Y-%m-%d-%H%M}.json.gz"


async def build_backup() -> tuple[bytes, dict]:
    """Ritorna (file compresso, riepilogo con il numero di documenti per raccolta)."""
    names = sorted(n for n in await db.list_collection_names()
                   if n not in EXCLUDED and not n.startswith('system.'))
    collections, counts = {}, {}
    for name in names:
        docs = await db[name].find({}).to_list(None)
        collections[name] = docs
        counts[name] = len(docs)
    meta = {
        'format': FORMAT,
        'version': VERSION,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'database': db.name,
        'counts': counts,
    }
    raw = json_util.dumps({'meta': meta, 'collections': collections},
                          json_options=json_util.CANONICAL_JSON_OPTIONS).encode()
    data = await asyncio.to_thread(gzip.compress, raw, 9)
    return data, meta


def read_backup(data: bytes) -> dict:
    """Legge e controlla un file di backup (usato dal ripristino)."""
    doc = json_util.loads(gzip.decompress(data).decode())
    meta = doc.get('meta') or {}
    if meta.get('format') != FORMAT or not isinstance(doc.get('collections'), dict):
        raise ValueError('Questo non è un file di backup di Light Blue')
    if int(meta.get('version', 0)) > VERSION:
        raise ValueError('Backup creato da una versione più recente del programma')
    return doc


def _summary_html(meta: dict, size: int, attached: bool) -> str:
    rows = ''.join(
        f'<tr><td style="padding:4px 12px 4px 0;color:#5C6A79">{name}</td><td style="text-align:right">{n}</td></tr>'
        for name, n in meta['counts'].items())
    when = datetime.fromisoformat(meta['created_at']).astimezone(ROME).strftime('%d/%m/%Y %H:%M')
    note = ("<p>Il backup è <b>allegato</b> a questa email. Conservala: contiene i dati personali degli ospiti.</p>"
            if attached else
            "<p><b>Il file è troppo grande per l'email</b>: scaricalo dal pannello, in Impostazioni → Backup dei dati.</p>")
    return (f'<div style="font-family:Arial,sans-serif;color:#2A333C;max-width:560px">'
            f'<h2 style="font-weight:300">Backup dei dati — {when}</h2>{note}'
            f'<table style="border-collapse:collapse;font-size:14px">{rows}</table>'
            f'<p style="color:#5C6A79;font-size:13px">Dimensione: {size / 1024:.0f} KB. '
            f'Per ripristinarlo serve il programma scripts/restore_backup.py del progetto.</p></div>')


async def email_backup(at: datetime | None = None) -> dict:
    """Crea il backup e lo invia al proprietario. Registra l'esito nelle impostazioni."""
    from email_helpers import send_email_async   # import locale: evita import circolari
    now = (at or datetime.now(timezone.utc)).isoformat()
    if not ADMIN_NOTIFY_EMAIL:
        result = {'ok': False, 'error': "Manca l'email del proprietario (ADMIN_NOTIFY_EMAIL)"}
    else:
        try:
            data, meta = await build_backup()
            attached = len(data) <= MAX_EMAIL_BYTES
            attachments = [{'filename': backup_filename(), 'content': base64.b64encode(data).decode(),
                            'content_type': 'application/gzip'}] if attached else None
            ok = await send_email_async(ADMIN_NOTIFY_EMAIL, '[Light Blue] Backup dei dati',
                                        _summary_html(meta, len(data), attached), attachments=attachments)
            result = {'ok': ok, 'size': len(data), 'error': None if ok else "Invio dell'email non riuscito"}
        except Exception as e:
            logging.exception('Backup non riuscito')
            result = {'ok': False, 'error': f'Backup non riuscito ({type(e).__name__})'}
    update = {'last_backup_attempt_at': now, 'last_backup_error': result.get('error')}
    if result['ok']:
        update.update(last_backup_email_at=now, last_backup_size=result['size'])
    await db.settings.update_one({'id': 'global'}, {'$set': update})
    return result


async def run_scheduled_backup(now: datetime | None = None) -> bool:
    """Chiamata ogni 5 minuti: invia il backup quando è il momento. Ritorna True se l'ha inviato."""
    settings = await get_settings()
    every = FREQUENCIES.get(settings.get('backup_email_frequency', 'weekly'))
    if not every or not ADMIN_NOTIFY_EMAIL:
        return False
    now = now or datetime.now(timezone.utc)
    if now.astimezone(ROME).time() < SEND_AFTER:
        return False
    last = settings.get('last_backup_attempt_at')
    if settings.get('last_backup_error'):
        every = min(every, timedelta(hours=6))   # l'ultimo tentativo è fallito: si riprova prima
    if last and now - datetime.fromisoformat(last) < every:
        return False
    # "prenota" l'invio: se il server gira due volte in parallelo, parte un solo backup
    claimed = await db.settings.update_one(
        {'id': 'global', 'last_backup_attempt_at': last},
        {'$set': {'last_backup_attempt_at': now.isoformat()}})
    if claimed.modified_count == 0:
        return False
    result = await email_backup(now)
    logging.info(f"Backup automatico: {'inviato' if result['ok'] else result.get('error')}")
    return True
