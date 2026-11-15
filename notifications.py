"""Notifiche push sul telefono del proprietario (Web Push standard, firmate con chiavi VAPID).

Tipi di notifica (decisi con il proprietario):
  A  prenotazioni          1 nuova richiesta, 2 richiesta in scadenza, 3 richiesta scaduta,
                           4 prenotazione confermata, 5 pagamento non completato
  B  Airbnb / Booking.com  6 nuova prenotazione, 7 prenotazione rimossa, 8 calendario non aggiornato
  C  messaggi              9 nuovo messaggio dal sito
  D  promemoria            10 arrivo domani (18:00), 11 partenza oggi (8:00)
  E  sicurezza e problemi  12 pagamento da verificare, 13 email non consegnata, 14 nuovo accesso,
                           15 pagamento su prenotazione chiusa (cancellata/rifiutata/scaduta)
Regole: niente emoji o simboli nei testi; ogni notifica apre la pagina/scheda giusta;
gruppo E sempre attivo; silenzio notturno (le urgenti 12 e 14 arrivano comunque).
"""
import asyncio
import json
import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from db import db, get_settings, PUBLIC_API_URL, VAPID_PRIVATE_KEY, VAPID_PUBLIC_KEY, VAPID_SUBJECT

ROME = ZoneInfo('Europe/Rome')
MESI = ['gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno', 'luglio', 'agosto',
        'settembre', 'ottobre', 'novembre', 'dicembre']

GROUP_OF = {
    'new_request': 'A', 'request_expiring': 'A', 'request_expired': 'A', 'booking_confirmed': 'A', 'payment_expired': 'A',
    'external_new': 'B', 'external_removed': 'B', 'ical_failing': 'B',
    'new_message': 'C',
    'arrival_tomorrow': 'D', 'departure_today': 'D',
    'payment_orphan': 'E', 'email_failed': 'E', 'new_login': 'E', 'payment_on_closed': 'E',
}
URGENT = {'payment_orphan', 'new_login', 'payment_on_closed'}   # arrivano anche durante il silenzio notturno

# Servizi di notifica ammessi (evita che il server venga usato per chiamare indirizzi arbitrari)
ALLOWED_PUSH_HOSTS = ('fcm.googleapis.com', 'updates.push.services.mozilla.com', 'push.apple.com',
                      'notify.windows.com', 'push.services.mozilla.com')


def push_enabled() -> bool:
    return bool(VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY)


def valid_push_endpoint(endpoint: str) -> bool:
    try:
        u = urlparse(endpoint)
    except Exception:
        return False
    host = (u.hostname or '').lower()
    return u.scheme == 'https' and any(host == h or host.endswith('.' + h) for h in ALLOWED_PUSH_HOSTS)


# --------------------------------------------------------------------------- testi
def _d(iso: str) -> date:
    return datetime.strptime(iso[:10], '%Y-%m-%d').date()


def periodo(check_in: str, check_out: str) -> str:
    """'dal 24 al 27 novembre' / 'dal 30 novembre al 6 dicembre'."""
    a, b = _d(check_in), _d(check_out)
    if a.month == b.month and a.year == b.year:
        return f'dal {a.day} al {b.day} {MESI[b.month - 1]}'
    return f'dal {a.day} {MESI[a.month - 1]} al {b.day} {MESI[b.month - 1]}'


def notti(b: dict) -> str:
    n = (_d(b['check_out']) - _d(b['check_in'])).days
    return '1 notte' if n == 1 else f'{n} notti'


def ospiti(b: dict) -> str:
    a, c = int(b.get('adults') or 0), int(b.get('children') or 0)
    s = '1 adulto' if a == 1 else f'{a} adulti'
    if c:
        s += ', 1 bambino' if c == 1 else f', {c} bambini'
    return s


def euro(v) -> str:
    v = float(v or 0)
    s = f'{v:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    return f'€ {s[:-3] if s.endswith(",00") else s}'


def ora_locale(iso: str) -> str:
    """'oggi alle 21:40' / 'domani alle 9:05' / 'il 3 dicembre alle 10:00' (ora italiana)."""
    dt = datetime.fromisoformat(iso).astimezone(ROME)
    oggi = datetime.now(ROME).date()
    hh = f'{dt.hour}:{dt.minute:02d}'
    if dt.date() == oggi:
        return f'oggi alle {hh}'
    if dt.date() == oggi + timedelta(days=1):
        return f'domani alle {hh}'
    return f'il {dt.day} {MESI[dt.month - 1]} alle {hh}'


def nome(b: dict) -> str:
    return (b.get('guest_name') or 'Ospite').strip()


def piattaforma(src: str) -> str:
    return {'airbnb': 'Airbnb', 'booking': 'Booking.com'}.get(src, src)


def booking_url(booking_id: str) -> str:
    return f'/admin/bookings?open={quote(str(booking_id))}'


def build(kind: str, ctx: dict) -> dict:
    """Testo completo, testo discreto e pagina da aprire per ogni tipo di notifica."""
    b = ctx.get('booking') or {}
    if kind == 'new_request':
        return dict(title='Nuova richiesta di prenotazione',
                    body=f"{nome(b)}, {periodo(b['check_in'], b['check_out'])} ({notti(b)}), {ospiti(b)}, {euro(b.get('total_price'))}. "
                         f"Rispondi entro {ora_locale(b['request_expires_at'])}.",
                    discreet='Hai una nuova richiesta di prenotazione da valutare.', url=booking_url(b['id']))
    if kind == 'request_expiring':
        return dict(title='Richiesta in scadenza',
                    body=f"La richiesta di {nome(b)} ({periodo(b['check_in'], b['check_out'])}) scade tra 3 ore, poi le date si liberano.",
                    discreet='Una richiesta di prenotazione scade tra 3 ore.', url=booking_url(b['id']))
    if kind == 'request_expired':
        return dict(title='Richiesta scaduta',
                    body=f"Non hai risposto a {nome(b)} ({periodo(b['check_in'], b['check_out'])}): le date sono di nuovo libere. "
                         f"L'ospite è stato avvisato.",
                    discreet='Una richiesta di prenotazione è scaduta senza risposta.', url=booking_url(b['id']))
    if kind == 'booking_confirmed':
        paid = b.get('total_price') if b.get('payment_status') == 'fully_paid' else b.get('deposit_amount')
        return dict(title='Prenotazione confermata',
                    body=f"{nome(b)} ha pagato {euro(paid)} per il soggiorno {periodo(b['check_in'], b['check_out'])}.",
                    discreet='Pagamento ricevuto: una prenotazione è confermata.', url=booking_url(b['id']))
    if kind == 'payment_expired':
        return dict(title='Pagamento non completato',
                    body=f"{nome(b)} non ha pagato entro la scadenza: le date {periodo(b['check_in'], b['check_out'])} sono di nuovo libere.",
                    discreet='Un ospite non ha completato il pagamento: le date sono di nuovo libere.', url=booking_url(b['id']))
    if kind == 'external_new':
        return dict(title=f"Nuova prenotazione {piattaforma(b.get('source'))}",
                    body=f"Date occupate {periodo(b['check_in'], b['check_out'])} ({notti(b)}).",
                    discreet=f"Nuova prenotazione da {piattaforma(b.get('source'))}.",
                    url=f"/admin/calendar?date={b['check_in']}&open={quote(str(b['id']))}")
    if kind == 'external_removed':
        return dict(title=f"Prenotazione {piattaforma(b.get('source'))} rimossa",
                    body=f"Le date {periodo(b['check_in'], b['check_out'])} sono tornate libere sul sito.",
                    discreet=f"Una prenotazione {piattaforma(b.get('source'))} è stata rimossa.",
                    url=f"/admin/calendar?date={b['check_in']}")
    if kind == 'ical_failing':
        p = piattaforma(ctx['source'])
        return dict(title=f'Calendario {p} non aggiornato',
                    body=f"Da un'ora non riesco a leggere il calendario {p}. Le date già note restano bloccate: "
                         f"controlla il link nelle Impostazioni.",
                    discreet=f'Problema di sincronizzazione con {p}.', url='/admin/settings#ical')
    if kind == 'new_message':
        m = ctx['message']
        text = ' '.join((m.get('message') or '').split())
        return dict(title=f"Nuovo messaggio da {(m.get('name') or 'un visitatore').strip()}",
                    body=text[:140] + ('...' if len(text) > 140 else ''),
                    discreet='Hai un nuovo messaggio dal sito.', url=f"/admin/inbox?open={quote(str(m['id']))}")
    if kind == 'arrival_tomorrow':
        extra = ''
        note = ' '.join((b.get('notes') or '').split())
        if note and b.get('source') not in ('airbnb', 'booking'):
            extra = f' Note: {note[:100]}'
        who = ospiti(b) if int(b.get('adults') or 0) else None
        parts = [f"{_d(b['check_in']).day} {MESI[_d(b['check_in']).month - 1]}", notti(b)] + ([who] if who else [])
        return dict(title=f'Domani arriva {nome(b)}', body=', '.join(parts) + '.' + extra,
                    discreet='Promemoria: domani è previsto un arrivo.', url=booking_url(b['id']))
    if kind == 'departure_today':
        nxt = ctx.get('next')
        if nxt:
            quando = 'oggi' if nxt['check_in'] == date.today().isoformat() else f"il {_d(nxt['check_in']).day} {MESI[_d(nxt['check_in']).month - 1]}"
            body = f"Prossimo arrivo: {nome(nxt)}, {quando}."
        else:
            body = 'Nessun altro arrivo in programma.'
        return dict(title=f'Oggi parte {nome(b)}', body=body,
                    discreet='Promemoria: oggi è prevista una partenza.', url=booking_url(b['id']))
    if kind == 'payment_orphan':
        return dict(title='Pagamento da verificare',
                    body=f"Stripe ha incassato {euro(ctx.get('amount'))} ma la prenotazione non risulta. Controlla subito su Stripe.",
                    discreet='Un pagamento richiede una verifica urgente.', url='/admin/bookings')
    if kind == 'payment_on_closed':
        b = ctx['booking']
        return dict(title='Pagamento su prenotazione chiusa',
                    body=f"{nome(b)} ha pagato {euro(ctx.get('amount'))} per {periodo(b['check_in'], b['check_out'])}, "
                         "ma la prenotazione era chiusa. Non è stata riattivata: verifica e rimborsa.",
                    discreet='Un pagamento richiede una verifica urgente.', url=booking_url(b['id']))
    if kind == 'email_failed':
        return dict(title='Email non consegnata',
                    body=f"Non sono riuscito a inviare l'email \"{ctx.get('subject', '')[:60]}\" a {ctx.get('to')}: contatta l'ospite.",
                    discreet="Un'email a un ospite non è stata consegnata.", url=ctx.get('url') or '/admin/bookings')
    if kind == 'new_login':
        return dict(title='Nuovo accesso al pannello',
                    body=f"{ora_locale(ctx['at']).capitalize()} da {ctx.get('device') or 'un nuovo dispositivo'}. "
                         f"Se non sei stato tu, cambia subito la password.",
                    discreet='Nuovo accesso al pannello da un dispositivo sconosciuto.', url='/admin/settings#password')
    if kind == 'test':
        return dict(title='Notifiche attive',
                    body='Questa è una notifica di prova: da ora riceverai qui gli avvisi di Light Blue.',
                    discreet='Notifica di prova di Light Blue.', url='/admin/settings#notifiche')
    raise ValueError(kind)


# --------------------------------------------------------------------------- invio
def _in_quiet_hours(settings: dict, now: datetime) -> bool:
    if not settings.get('notify_quiet_enabled', True):
        return False
    try:
        start = time.fromisoformat(settings.get('notify_quiet_start', '23:00'))
        end = time.fromisoformat(settings.get('notify_quiet_end', '08:00'))
    except ValueError:
        return False
    t = now.astimezone(ROME).time()
    return (start <= t or t < end) if start > end else (start <= t < end)


def _quiet_end(settings: dict, now: datetime) -> datetime:
    end = time.fromisoformat(settings.get('notify_quiet_end', '08:00'))
    local = now.astimezone(ROME)
    candidate = datetime.combine(local.date(), end, tzinfo=ROME)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


async def badge_count() -> int:
    """Numero sull'icona dell'app: richieste da valutare + messaggi non letti."""
    return (await db.bookings.count_documents({'status': 'request'})
            + await db.contact_messages.count_documents({'status': 'new'}))


def _send_one(sub: dict, payload: str):
    from pywebpush import webpush   # import locale: la libreria serve solo se le notifiche sono attive
    return webpush(
        subscription_info={'endpoint': sub['endpoint'], 'keys': sub['keys']},
        data=payload,
        vapid_private_key=VAPID_PRIVATE_KEY,
        vapid_claims={'sub': VAPID_SUBJECT},
        ttl=24 * 3600,
        timeout=10,
    )


async def _deliver(message: dict, only_sub_id: str = None) -> int:
    """Invia a tutti i dispositivi attivi; rimuove quelli non più validi. Ritorna quanti invii riusciti."""
    if not push_enabled():
        logging.info(f"Notifica non inviata (chiavi VAPID mancanti): {message.get('title')}")
        return 0
    message = {**message, 'badge': await badge_count()}
    payload = json.dumps(message, ensure_ascii=False)
    query = {'id': only_sub_id} if only_sub_id else {}
    sent = 0
    for sub in await db.push_subscriptions.find(query, {'_id': 0}).to_list(50):
        try:
            await asyncio.to_thread(_send_one, sub, payload)
            sent += 1
            await db.push_subscriptions.update_one(
                {'id': sub['id']}, {'$set': {'last_success_at': datetime.now(timezone.utc).isoformat(), 'failures': 0}})
        except Exception as e:
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            if status in (404, 410):     # dispositivo disinstallato o permesso revocato
                await db.push_subscriptions.delete_one({'id': sub['id']})
                logging.info(f"Dispositivo notifiche rimosso (non più valido): {sub.get('label')}")
            else:
                await db.push_subscriptions.update_one({'id': sub['id']}, {'$inc': {'failures': 1}})
                logging.warning(f"Invio notifica fallito a {sub.get('label')}: {e}")
    return sent


async def notify(kind: str, **ctx) -> None:
    """Prepara e invia (o mette in attesa per il silenzio notturno) una notifica. Non solleva mai errori."""
    try:
        settings = await get_settings()
        group = GROUP_OF[kind]
        if group != 'E' and not (settings.get('notify_groups') or {}).get(group, True):
            return
        n = build(kind, ctx)
        detailed = settings.get('notify_detail', 'detailed') != 'discreet'
        v = settings.get('logo_version', 'default')
        message = {
            'title': n['title'],
            'body': n['body'] if detailed else n['discreet'],
            'url': n['url'],
            'tag': f"{kind}:{ctx.get('tag') or (ctx.get('booking') or {}).get('id') or (ctx.get('message') or {}).get('id') or uuid.uuid4().hex[:8]}",
            'icon': f'{PUBLIC_API_URL}/api/brand/logo-192.png?v={v}',
            'kind': kind,
        }
        now = datetime.now(timezone.utc)
        if kind not in URGENT and _in_quiet_hours(settings, now):
            await db.push_outbox.insert_one({
                'id': uuid.uuid4().hex, 'message': message,
                'deliver_after': _quiet_end(settings, now).isoformat(), 'created_at': now.isoformat(),
            })
            return
        await _deliver(message)
    except Exception:
        logging.exception(f'Errore nella notifica {kind}')


async def send_test(sub_id: str = None) -> int:
    n = build('test', {})
    settings = await get_settings()
    v = settings.get('logo_version', 'default')
    return await _deliver({'title': n['title'], 'body': n['body'], 'url': n['url'], 'tag': 'test',
                           'icon': f'{PUBLIC_API_URL}/api/brand/logo-192.png?v={v}', 'kind': 'test'}, sub_id)


# --------------------------------------------------------------------------- notifiche programmate
async def flush_outbox() -> None:
    """Consegna le notifiche trattenute durante il silenzio notturno."""
    now_iso = datetime.now(timezone.utc).isoformat()
    for item in await db.push_outbox.find({'deliver_after': {'$lte': now_iso}}, {'_id': 0}).sort('created_at', 1).to_list(100):
        await db.push_outbox.delete_one({'id': item['id']})
        await _deliver(item['message'])


async def _mark_once(booking_id: str, key: str) -> bool:
    """Segna un promemoria come inviato; False se era già stato inviato (niente doppioni)."""
    res = await db.bookings.update_one({'id': booking_id, f'notified.{key}': {'$exists': False}},
                                       {'$set': {f'notified.{key}': datetime.now(timezone.utc).isoformat()}})
    return res.modified_count == 1


async def run_scheduled() -> None:
    """Chiamata ogni 5 minuti: avvisi di scadenza, promemoria di arrivo/partenza, coda notturna."""
    now = datetime.now(timezone.utc)
    local = now.astimezone(ROME)

    # 2) richieste che scadono entro 3 ore
    soon = (now + timedelta(hours=3)).isoformat()
    for b in await db.bookings.find({'status': 'request', 'request_expires_at': {'$lte': soon, '$gt': now.isoformat()}},
                                    {'_id': 0}).to_list(50):
        if await _mark_once(b['id'], 'expiring'):
            await notify('request_expiring', booking=b)

    active = ['confirmed', 'external', 'awaiting_payment']
    # 10) arrivi di domani, dalle 18:00
    if local.hour >= 18:
        tomorrow = (local.date() + timedelta(days=1)).isoformat()
        for b in await db.bookings.find({'status': {'$in': active}, 'check_in': tomorrow}, {'_id': 0}).to_list(20):
            if await _mark_once(b['id'], f'arrival_{tomorrow}'):
                await notify('arrival_tomorrow', booking=b)
    # 11) partenze di oggi, dalle 8:00
    if local.hour >= 8:
        today = local.date().isoformat()
        for b in await db.bookings.find({'status': {'$in': active}, 'check_out': today}, {'_id': 0}).to_list(20):
            if await _mark_once(b['id'], f'departure_{today}'):
                nxt = await db.bookings.find_one({'status': {'$in': active}, 'check_in': {'$gte': today}, 'id': {'$ne': b['id']}},
                                                 {'_id': 0}, sort=[('check_in', 1)])
                await notify('departure_today', booking=b, next=nxt)

    await flush_outbox()
