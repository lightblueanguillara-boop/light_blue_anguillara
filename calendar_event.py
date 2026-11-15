"""Evento del calendario allegato alle email di conferma, modifica e cancellazione (RFC 5545).

- UID stabile (<id>@<dominio>): una modifica aggiorna l'evento invece di duplicarlo; SEQUENCE sale a ogni modifica.
- Cancellazione: METHOD:CANCEL + STATUS:CANCELLED, così l'evento sparisce dal calendario dell'ospite.
- Dal check-in al check-out con gli orari, se impostati; altrimenti a giornata intera.
- Escape, righe piegate a 75 ottetti e fine riga CRLF li gestisce la libreria `icalendar`.
"""
import re
from datetime import date, datetime, time, timezone
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event

from db import FRONTEND_URL

ROME = ZoneInfo('Europe/Rome')
_TIME = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')


def _domain() -> str:
    return urlparse(FRONTEND_URL).hostname or 'lightblueanguillara.com'


def _bounds(booking: dict, settings: dict):
    """(inizio, fine, a_giornata_intera). Con gli orari: datetime in UTC; senza: date."""
    ci = date.fromisoformat(booking['check_in'])
    co = date.fromisoformat(booking['check_out'])
    t_in, t_out = settings.get('checkin_time') or '', settings.get('checkout_time') or ''
    if _TIME.match(t_in) and _TIME.match(t_out):
        start = datetime.combine(ci, time.fromisoformat(t_in), tzinfo=ROME).astimezone(timezone.utc)
        end = datetime.combine(co, time.fromisoformat(t_out), tzinfo=ROME).astimezone(timezone.utc)
        return start, end, False
    return ci, co, True


def _summary(settings: dict) -> str:
    return f"Soggiorno a {settings.get('message_signature') or 'Light Blue Anguillara'}"


def build_ics(booking: dict, settings: dict, method: str = 'publish') -> bytes:
    start, end, all_day = _bounds(booking, settings)
    cal = Calendar()
    cal.add('prodid', '-//Light Blue Anguillara//Prenotazioni//IT')
    cal.add('version', '2.0')
    cal.add('method', 'CANCEL' if method == 'cancel' else 'PUBLISH')
    ev = Event()
    ev.add('uid', f"{booking['id']}@{_domain()}")
    ev.add('sequence', int(booking.get('ics_sequence') or 0) + (1 if method == 'cancel' else 0))
    ev.add('dtstamp', datetime.now(timezone.utc))
    ev.add('summary', _summary(settings))
    ev.add('dtstart', start)
    ev.add('dtend', end)
    address = settings.get('villa_address') or ''
    if address:
        ev.add('location', address)
    ev.add('description', f"Prenotazione {booking['id']}. Per informazioni: {FRONTEND_URL}")
    ev.add('status', 'CANCELLED' if method == 'cancel' else 'CONFIRMED')
    ev.add('transp', 'TRANSPARENT')
    cal.add_component(ev)
    return cal.to_ical()


def google_calendar_link(booking: dict, settings: dict) -> str:
    start, end, all_day = _bounds(booking, settings)
    fmt = (lambda d: d.strftime('%Y%m%d')) if all_day else (lambda d: d.strftime('%Y%m%dT%H%M%SZ'))
    return ('https://calendar.google.com/calendar/render?action=TEMPLATE'
            f"&text={quote(_summary(settings))}&dates={fmt(start)}/{fmt(end)}"
            f"&location={quote(settings.get('villa_address') or '')}"
            f"&details={quote('Prenotazione ' + booking['id'])}")
