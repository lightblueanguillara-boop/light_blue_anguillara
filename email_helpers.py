import asyncio
import os
import html
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
import resend

from db import RESEND_API_KEY, SENDER_EMAIL, PUBLIC_API_URL

resend.api_key = RESEND_API_KEY


def _eur(value) -> str:
    """Importo in formato italiano: 1234.5 -> '€ 1.234,50'."""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return f"€ {value}"
    return "€ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _logo_url(settings: dict = None) -> str:
    v = html.escape(str((settings or {}).get('logo_version', 'default')))
    return f"{PUBLIC_API_URL}/api/brand/logo-256.png?v={v}"


def _logo_html(settings: dict = None) -> str:
    """Logo in testa alle email (preso dal server: cambia insieme al logo del sito)."""
    villa = html.escape(str((settings or {}).get('villa_name') or 'Light Blue'))
    return (f'<div style="text-align:center;margin:0 0 24px 0"><img src="{_logo_url(settings)}" alt="{villa}" '
            f'width="72" height="72" style="display:inline-block;border:0;border-radius:50%" /></div>')


def _it_date(iso: str) -> str:
    """Convert YYYY-MM-DD -> GG-MM-AAAA. Returns input on failure."""
    if not iso:
        return ''
    try:
        return datetime.strptime(iso, '%Y-%m-%d').strftime('%d-%m-%Y')
    except (ValueError, TypeError):
        return iso


def is_placeholder_email(email: Optional[str]) -> bool:
    """Indirizzi segnaposto (prenotazioni manuali senza email, ospiti Airbnb/Booking): mai scrivere lì."""
    e = (email or '').strip().lower()
    return not e or e == 'manual@booking.com' or e.endswith('@lightblue-anguillara.it')


def _base_address(email: str) -> str:
    """nome+etichetta@dominio -> nome@dominio (per l'elenco degli indirizzi ammessi in prova)."""
    e = (email or '').strip().lower()
    local, _, domain = e.partition('@')
    return f"{local.split('+')[0]}@{domain}"


def email_service_status() -> dict:
    """Stato del servizio email, per l'avviso nel pannello."""
    only = [a for a in (os.environ.get('EMAIL_SOLO_A') or '').split(',') if a.strip()]
    return {
        'active': bool(RESEND_API_KEY and SENDER_EMAIL),
        'test_only': [a.strip() for a in only],
        'daily_limit': int(os.environ.get('EMAIL_LIMITE_GIORNO') or 0) or None,
        'webhook': bool(os.environ.get('RESEND_WEBHOOK_SECRET')),
    }


async def _block_reason(to_email: str):
    """Motivo per cui un'email NON deve partire (None = può partire)."""
    only = {_base_address(a) for a in (os.environ.get('EMAIL_SOLO_A') or '').split(',') if a.strip()}
    if only and _base_address(to_email) not in only:
        return 'solo_prove'
    limit = int(os.environ.get('EMAIL_LIMITE_GIORNO') or 0)
    if limit:
        from zoneinfo import ZoneInfo
        midnight = datetime.now(ZoneInfo('Europe/Rome')).replace(hour=0, minute=0, second=0, microsecond=0)
        from db import db
        sent = await db.email_outbox.count_documents(
            {'status': 'inviata', 'created_at': {'$gte': midnight.astimezone(timezone.utc).isoformat()}})
        if sent >= limit:
            return 'limite'
    if not (RESEND_API_KEY and SENDER_EMAIL):
        return 'non_attivo'
    return None


async def send_email_async(to_email: str, subject: str, html_body: str, headers: Optional[dict] = None,
                           ref_url: Optional[str] = None, attachments: Optional[list] = None, *,
                           text: Optional[str] = None, key: Optional[str] = None, booking_id: Optional[str] = None,
                           message_id: Optional[str] = None, reply_to: Optional[str] = None,
                           resent_from: Optional[str] = None) -> bool:
    """Invia un'email con Resend e la registra SEMPRE in email_outbox, qualunque sia l'esito.

    Mai bloccante: ritorna False in caso di problemi, senza sollevare errori.
    """
    import uuid
    from db import db, ADMIN_NOTIFY_EMAIL
    record = {
        'id': uuid.uuid4().hex, 'to': to_email, 'subject': subject, 'key': key, 'booking_id': booking_id,
        'message_id': message_id, 'reply_to': reply_to, 'text': text, 'html': html_body,
        'attachments': [a.get('filename') for a in (attachments or [])],
        'status': 'non_inviata', 'reason': None, 'error': None, 'provider_id': None,
        'delivery': None, 'delivery_at': None, 'resent_from': resent_from,
        'created_at': datetime.now(timezone.utc).isoformat(), 'sent_at': None,
    }
    ok = False
    try:
        record['reason'] = await _block_reason(to_email)
        if not record['reason']:
            params = {'from': SENDER_EMAIL, 'to': [to_email], 'subject': subject, 'html': html_body}
            if text:
                params['text'] = text
            if reply_to:
                params['reply_to'] = reply_to
            if headers:
                params['headers'] = headers
            if attachments:
                params['attachments'] = attachments
            result = await asyncio.to_thread(resend.Emails.send, params)
            record.update(status='inviata', sent_at=datetime.now(timezone.utc).isoformat(),
                          provider_id=(result or {}).get('id') if isinstance(result, dict) else getattr(result, 'id', None))
            ok = True
    except Exception as e:
        # nei log solo il tipo di errore e il modello: mai indirizzi o contenuti
        logging.warning(f"Email {key or subject[:30]!r} non inviata: {type(e).__name__}")
        record.update(status='errore', reason='errore', error=type(e).__name__)
        if to_email and to_email.lower() != (ADMIN_NOTIFY_EMAIL or '').lower():
            from notifications import notify   # import locale: evita import circolari
            await notify('email_failed', to=to_email, subject=subject, url=ref_url, tag=f'{to_email}:{subject}')
    try:
        await db.email_outbox.insert_one(record)
    except Exception:
        logging.exception('Registro email non aggiornato')
    return ok


# =============================================================================
# Prenotazione su richiesta
# =============================================================================


