"""Messaggi all'ospite e al proprietario: modelli modificabili, email e WhatsApp «al tocco».

Punto d'ingresso unico: `invia(chiave, prenotazione)` → manda l'email (se il modello email è attivo) e mette in
coda il WhatsApp (se c'è un modello WhatsApp attivo con la stessa chiave e l'ospite può essere contattato).
Un errore di email o WhatsApp non fa MAI fallire l'operazione che l'ha chiamato.
Logica descritta in docs/GUIDA_EMAIL_WHATSAPP.md; testi di partenza in message_templates.py.
"""
import base64
import html
import logging
import re
from datetime import date, datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import message_templates as T
import short_links
import whatsapp_queue
from calendar_event import build_ics, google_calendar_link
from db import db, get_settings, ADMIN_NOTIFY_EMAIL, FRONTEND_URL
from email_helpers import _eur, _logo_html, is_placeholder_email, send_email_async

ROME = ZoneInfo('Europe/Rome')
GIORNI = ['lunedì', 'martedì', 'mercoledì', 'giovedì', 'venerdì', 'sabato', 'domenica']
MESI = ['gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno', 'luglio', 'agosto',
        'settembre', 'ottobre', 'novembre', 'dicembre']
PLACEHOLDER_PHONE = '+39 000 000 0000'
DEFAULT_SIGNATURE = 'Light Blue Anguillara'

# ------------------------------------------------------------------ render
_VAR = re.compile(r'\{([a-z_]+)\}')


def render(template: str, variables: dict) -> str:
    """{variabile} → valore. Una riga in cui TUTTI i dati mancano sparisce intera
    (es. «Check-in: dalle {ora_checkin}» senza orario, «Wi-Fi: {wifi}» senza Wi-Fi), così lo stesso
    modello va bene in casi diversi e non restano mai frasi a metà."""
    out = []
    for line in (template or '').split('\n'):
        names = _VAR.findall(line)
        if names and all(not variables.get(n) for n in names):
            continue
        out.append(_VAR.sub(lambda m: str(variables.get(m.group(1)) or ''), line))
    text = re.sub(r'[ \t]{2,}', ' ', '\n'.join(out))
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def unknown_vars(key: str, *texts: str) -> list:
    allowed = set(T.allowed_vars(key))
    return sorted({n for t in texts for n in _VAR.findall(t or '') if n not in allowed})


# ------------------------------------------------------------------ modelli nel database
async def ensure_indexes() -> None:
    await db.message_templates.create_index([('key', 1), ('channel', 1)], unique=True)
    await db.email_outbox.create_index('provider_id')
    await db.email_outbox.create_index([('booking_id', 1), ('key', 1), ('created_at', -1)])


async def seed() -> int:
    """Crea i modelli mancanti. Non sovrascrive mai quelli modificati dal proprietario:
    aggiorna solo il «testo originale» (quello di «Ripristina») e i modelli mai toccati."""
    created = 0
    for i, t in enumerate(T.SEED):
        subject = t.get('subject')
        doc = await db.message_templates.find_one({'key': t['key'], 'channel': t['channel']}, {'_id': 0})
        base = {'original_subject': subject, 'original_body': t['body'], 'order': i,
                'recipient': 'gestore' if t['key'] in T.GESTORE_KEYS else 'ospite'}
        if not doc:
            await db.message_templates.insert_one({
                'key': t['key'], 'channel': t['channel'], 'subject': subject, 'body': t['body'], 'active': True,
                'created_at': datetime.now(timezone.utc).isoformat(), **base})
            created += 1
            continue
        untouched = doc.get('body') == doc.get('original_body') and doc.get('subject') == doc.get('original_subject')
        update = dict(base)
        if untouched:
            update.update(subject=subject, body=t['body'])
        await db.message_templates.update_one({'key': t['key'], 'channel': t['channel']}, {'$set': update})
    return created


async def get_template(key: str, channel: str):
    """Modello attivo, o None se manca o è spento."""
    tpl = await db.message_templates.find_one({'key': key, 'channel': channel}, {'_id': 0})
    return tpl if tpl and tpl.get('active', True) else None


# ------------------------------------------------------------------ dati per i testi
def fmt_date(iso: str) -> str:
    """'2026-12-12' → 'sabato 12 dicembre' (con l'anno se non è quello in corso)."""
    if not iso:
        return ''
    d = date.fromisoformat(str(iso)[:10])
    s = f'{GIORNI[d.weekday()]} {d.day} {MESI[d.month - 1]}'
    return s if d.year == datetime.now(ROME).year else f'{s} {d.year}'


def fmt_deadline(iso: str) -> str:
    """Scadenza in ora italiana: '13 dicembre alle 18:30'."""
    if not iso:
        return ''
    dt = datetime.fromisoformat(iso).astimezone(ROME)
    s = f'{dt.day} {MESI[dt.month - 1]}'
    if dt.year != datetime.now(ROME).year:
        s += f' {dt.year}'
    return f'{s} alle {dt:%H:%M}'


def _nights(b: dict) -> str:
    try:
        n = (date.fromisoformat(b['check_out']) - date.fromisoformat(b['check_in'])).days
    except (KeyError, ValueError, TypeError):
        return ''
    return '1 notte' if n == 1 else f'{n} notti'


def _guests(b: dict) -> str:
    a, c = int(b.get('adults') or 0), int(b.get('children') or 0)
    if not a and not c:
        return ''
    s = '1 adulto' if a == 1 else f'{a} adulti'
    if c:
        s += ', 1 bambino' if c == 1 else f', {c} bambini'
    return s


def _policy(b: dict) -> str:
    if b.get('is_refundable', True) is False:
        return 'Tariffa non rimborsabile: in caso di cancellazione non è previsto alcun rimborso.'
    text = ('Tariffa rimborsabile: rimborso completo se disdici almeno 10 giorni prima del check-in, '
            'nessun rimborso nei 10 giorni precedenti.')
    try:
        limit = date.fromisoformat(b['check_in']).toordinal() - 10
        d = date.fromordinal(limit)
        if d >= date.today():
            text += f' Disdetta gratuita entro il {d.day} {MESI[d.month - 1]}.'
    except (KeyError, ValueError, TypeError):
        pass
    return text


def paid_amount(b: dict) -> float:
    if b.get('payment_status') == 'deposit_paid':
        return float(b.get('deposit_amount') or 0)
    if b.get('payment_status') == 'fully_paid':
        return float(b.get('total_price') or 0)
    return 0.0


def refund_note(paid: float, refund) -> str:
    """Frase sul rimborso per la cancellazione ('' = nessuna frase)."""
    if refund is None or paid <= 0:
        return ''
    if refund > 0:
        return (f'Ti abbiamo rimborsato {_eur(refund)} sul metodo di pagamento che hai usato: di solito compare '
                f"sull'estratto conto entro 5-10 giorni lavorativi.")
    return 'In base alle condizioni della tariffa scelta non è previsto alcun rimborso.'


def _clean_phone(settings: dict) -> str:
    p = (settings.get('villa_phone') or '').strip()
    return '' if p == PLACEHOLDER_PHONE else p


def _maps_link(address: str) -> str:
    return f'https://www.google.com/maps/search/?api=1&query={quote(address)}' if address else ''


def common_vars(settings: dict) -> dict:
    address = settings.get('villa_address') or ''
    return {
        'indirizzo': address, 'link_mappa': _maps_link(address), 'telefono': _clean_phone(settings),
        'link_sito': FRONTEND_URL, 'firma': settings.get('message_signature') or DEFAULT_SIGNATURE,
        'tassa_soggiorno': settings.get('tourist_tax_note') or '',
        'ora_checkin': settings.get('checkin_time') or '', 'ora_checkout': settings.get('checkout_time') or '',
        'come_arrivare': settings.get('arrival_directions') or '', 'parcheggio': settings.get('parking_info') or '',
        'istruzioni_ingresso': settings.get('entry_instructions') or '', 'wifi': settings.get('wifi_info') or '',
        'istruzioni_checkout': settings.get('checkout_instructions') or '',
        'link_recensione': settings.get('review_url') or '',
        'ore_risposta': str(settings.get('request_hold_hours', 24)),
    }


def booking_vars(b: dict, settings: dict, extra: dict = None) -> dict:
    """Tutti i dati di una prenotazione, con gli stessi nomi in ogni modello e in ogni invio automatico."""
    total = float(b.get('total_price') or 0)
    paid = paid_amount(b)
    due = round(total - paid, 2) if b.get('payment_status') == 'deposit_paid' else 0
    full_name = (b.get('guest_name') or '').strip()
    v = common_vars(settings) | {
        'nome': full_name.split(' ')[0] if full_name else '',
        'nome_completo': full_name,
        'data_arrivo': fmt_date(b.get('check_in')), 'data_partenza': fmt_date(b.get('check_out')),
        'notti': _nights(b), 'ospiti': _guests(b),
        'totale': _eur(total) if total else '', 'pagato': _eur(paid) if paid else '',
        'da_saldare': _eur(due) if due > 0 else '',
        'modalita_saldo': (settings.get('balance_payment_method') or '') if due > 0 else '',
        'politica_cancellazione': _policy(b),
        'scadenza_pagamento': fmt_deadline(b.get('payment_due_at')),
        'link_pagamento': b.get('payment_url') or '',
        'motivo': (b.get('rejection_reason') or '').strip(),
        'link_pannello': f"{FRONTEND_URL}/admin/bookings?open={quote(str(b.get('id', '')))}",
    }
    return v | (extra or {})


def contact_vars(msg: dict, settings: dict) -> dict:
    full_name = (msg.get('name') or '').strip()
    return common_vars(settings) | {
        'nome': full_name.split(' ')[0] if full_name else '', 'nome_completo': full_name,
        'messaggio': (msg.get('message') or '').strip(),
        'link_pannello': f"{FRONTEND_URL}/admin/inbox?open={quote(str(msg.get('id', '')))}",
    }


SAMPLE_BOOKING = {
    'id': 'esempio', 'guest_name': 'Giulia Russo', 'guest_email': 'giulia@example.com', 'guest_phone': '3331234567',
    'check_in': '2026-12-12', 'check_out': '2026-12-15', 'adults': 2, 'children': 1, 'total_price': 360,
    'deposit_amount': 108, 'payment_status': 'deposit_paid', 'is_refundable': True,
    'payment_url': 'https://checkout.stripe.com/c/pay/cs_test_esempio_di_link_di_pagamento_molto_lungo',
    'payment_due_at': '2026-12-01T17:30:00+00:00', 'rejection_reason': 'la casa non è disponibile in quelle date',
}


def sample_vars(key: str, settings: dict) -> dict:
    """Dati d'esempio per anteprime e prove (mai dati veri di ospiti)."""
    if key in T.CONTACT_KEYS:
        return contact_vars({'id': 'esempio', 'name': 'Elena Conti',
                             'message': 'Buongiorno, la casa è libera il primo weekend di dicembre?'}, settings)
    return booking_vars(SAMPLE_BOOKING, settings, {'nota_rimborso': refund_note(360, 200)})


# ------------------------------------------------------------------ email
_URL_HTML = re.compile(r'https?://[^\s<]+')
INK, MUTED, BG, BORDER = '#2A333C', '#5C6A79', '#FAF9F6', '#E5E0D8'


def _linkify(escaped_line: str) -> str:
    def repl(m):
        url, tail = m.group(0), ''
        while url and url[-1] in '.,;:!?)]}':
            tail, url = url[-1] + tail, url[:-1]
        return f'<a href="{url}" style="color:{INK};text-decoration:underline">{url}</a>{tail}'
    return _URL_HTML.sub(repl, escaped_line)


def _footer_lines(settings: dict) -> list:
    email = settings.get('villa_email') or ''
    lines = [settings.get('message_signature') or DEFAULT_SIGNATURE, settings.get('villa_address') or '',
             f"CIN {settings['villa_cir']}" if settings.get('villa_cir') else '', _clean_phone(settings),
             '' if is_placeholder_email(email) else email]
    return [x for x in lines if x]


def compose_email(settings: dict, subject: str, body: str, buttons=()) -> tuple:
    """(html, testo) con la grafica del sito: logo, testo, pulsanti, piè di pagina con i recapiti."""
    button_urls = {u for _, u, _ in buttons}
    lines = [ln for ln in body.split('\n') if ln.strip() not in button_urls]   # il link diventa il pulsante
    body = re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()
    paragraphs = [p for p in body.split('\n\n') if p.strip()]
    pre = next((p for p in paragraphs if not p.lower().startswith('ciao')), paragraphs[0] if paragraphs else '')
    pre = html.escape(' '.join(pre.split())[:110])
    paras = ''.join(
        f'<p style="margin:0 0 16px 0;line-height:1.6">'
        f"{'<br>'.join(_linkify(html.escape(x)) for x in p.split(chr(10)))}</p>" for p in paragraphs)
    btns = ''.join(
        f'<a href="{html.escape(url)}" style="display:inline-block;margin:4px 8px 4px 0;padding:12px 22px;'
        f'border-radius:4px;text-decoration:none;font-weight:600;'
        + (f'background:{INK};color:#ffffff;border:1px solid {INK}' if primary else
           f'background:#ffffff;color:{INK};border:1px solid {INK}')
        + f'">{html.escape(label)}</a>' for label, url, primary in buttons)
    footer = '<br>'.join(html.escape(x) for x in _footer_lines(settings))
    html_body = (
        f'<!DOCTYPE html><html lang="it"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width"><title>{html.escape(subject)}</title></head>'
        f'<body style="margin:0;padding:0;background:{BG}">'
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">{pre}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{BG}"><tr>'
        f'<td align="center" style="padding:24px 12px">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px">'
        f'<tr><td>{_logo_html(settings)}</td></tr>'
        f'<tr><td style="background:#ffffff;border:1px solid {BORDER};border-radius:6px;padding:28px 24px;'
        f'font-family:Manrope,Arial,sans-serif;font-size:15px;color:{INK}">{paras}'
        + (f'<div style="margin-top:8px">{btns}</div>' if btns else '') +
        f'</td></tr><tr><td style="padding:20px 8px;font-family:Arial,sans-serif;font-size:12px;'
        f'line-height:1.6;color:{MUTED};text-align:center">{footer}</td></tr>'
        f'</table></td></tr></table></body></html>')
    text = body
    if buttons:
        text += '\n\n' + '\n'.join(f'{label}: {url}' for label, url, _ in buttons)
    text += '\n\n--\n' + '\n'.join(_footer_lines(settings))
    return html_body, text


def guest_reply_to(settings: dict) -> str:
    for candidate in (settings.get('email_reply_to'), settings.get('villa_email'), ADMIN_NOTIFY_EMAIL):
        if candidate and not is_placeholder_email(candidate):
            return candidate.strip()
    return ''


async def build_template_email(key: str, variables: dict, settings: dict, booking: dict = None,
                               subject: str = None, body: str = None):
    """Prepara (oggetto, html, testo, allegati) di un modello email; None se il modello è spento."""
    if body is None:
        tpl = await get_template(key, 'email')
        if not tpl:
            return None
        subject, body = tpl.get('subject') or '', tpl['body']
    subject = ' '.join(render(subject or '', variables).split())
    text = await short_links.shorten_text(render(body, variables))
    buttons = []
    for label, var, primary in T.BUTTONS.get(key, []):
        if variables.get(var):
            buttons.append((label, await short_links.shorten(variables[var]), primary))
    attachments = []
    method = T.CALENDAR_KEYS.get(key)
    if method and booking and booking.get('check_in') and booking.get('check_out'):
        ics = build_ics(booking, settings, method)
        attachments.append({'filename': 'prenotazione.ics', 'content': base64.b64encode(ics).decode(),
                            'content_type': 'text/calendar'})
        if method == 'publish':
            buttons.append(('Aggiungi a Google Calendar',
                            await short_links.shorten(google_calendar_link(booking, settings)), False))
    html_body, plain = compose_email(settings, subject, text, buttons)
    return subject, html_body, plain, attachments


async def send_template_email(key: str, to: str, variables: dict, *, settings: dict = None, booking: dict = None,
                              message_id: str = None, reply_to: str = None):
    """Manda l'email del modello. None se il modello è spento o l'indirizzo non è valido; altrimenti True/False."""
    if is_placeholder_email(to):
        return None
    settings = settings or await get_settings()
    built = await build_template_email(key, variables, settings, booking)
    if not built:
        logging.info(f'Modello email {key} spento o mancante: nessun invio')
        return None
    subject, html_body, plain, attachments = built
    if reply_to is None:
        reply_to = guest_reply_to(settings) if key not in T.GESTORE_KEYS else ''
    return await send_email_async(
        to.strip(), subject, html_body, attachments=attachments or None, text=plain, key=key,
        booking_id=(booking or {}).get('id'), message_id=message_id, reply_to=reply_to or None,
        ref_url=f"/admin/bookings?open={booking['id']}" if booking else None)


# ------------------------------------------------------------------ WhatsApp
def whatsapp_allowed(b: dict) -> bool:
    """WhatsApp in coda solo se l'ospite ha accettato di essere contattato (o l'hai inserito tu a mano)."""
    return bool(b.get('contact_whatsapp')) or b.get('source') == 'manual'


async def queue_whatsapp(key: str, b: dict, variables: dict, *, for_date: str = None):
    tpl = await get_template(key, 'whatsapp')
    if not tpl or not b.get('guest_phone'):
        return None
    text = await short_links.shorten_text(render(tpl['body'], variables))
    return await whatsapp_queue.enqueue(key, b['guest_phone'], text, name=b.get('guest_name'),
                                        booking_id=b.get('id'), for_date=for_date or b.get('check_in'))


# ------------------------------------------------------------------ punto d'ingresso unico
async def invia(key: str, booking: dict, extra: dict = None, *, settings: dict = None, whatsapp: bool = True,
                for_date: str = None) -> dict:
    """Manda la comunicazione `key` all'ospite: email + WhatsApp in coda. Mai bloccante."""
    result = {'email': None, 'whatsapp_id': None}
    try:
        settings = settings or await get_settings()
        variables = booking_vars(booking, settings, extra)
    except Exception:
        logging.exception(f'Messaggio {key}: dati non preparati')
        return result
    try:
        result['email'] = await send_template_email(key, booking.get('guest_email'), variables,
                                                    settings=settings, booking=booking)
    except Exception:
        logging.exception(f'Email {key} non registrata')
    if whatsapp and whatsapp_allowed(booking):
        try:
            result['whatsapp_id'] = await queue_whatsapp(key, booking, variables, for_date=for_date)
        except Exception:
            logging.exception(f'WhatsApp {key} non messo in coda')
    return result


async def notify_owner(key: str, variables: dict, *, booking: dict = None, settings: dict = None,
                       reply_to: str = None):
    """Email al proprietario (se il modello è attivo e l'indirizzo è configurato). Mai bloccante."""
    if not ADMIN_NOTIFY_EMAIL:
        return None
    try:
        return await send_template_email(key, ADMIN_NOTIFY_EMAIL, variables, settings=settings, booking=booking,
                                         reply_to=reply_to or '')
    except Exception:
        logging.exception(f'Email al proprietario {key} non registrata')
        return None


async def booking_dates_changed(booking_id: str) -> None:
    """Date spostate: i promemoria ripartiranno per le date nuove e l'evento del calendario si aggiorna."""
    await db.bookings.update_one({'id': booking_id}, {'$unset': {'reminders': ''}, '$inc': {'ics_sequence': 1}})
    await whatsapp_queue.cancel_for_booking(booking_id, keys=REMINDER_KEYS + ['prenotazione_confermata'])


REMINDER_KEYS = ['promemoria_saldo', 'istruzioni_arrivo', 'promemoria_checkout', 'grazie_recensione']
