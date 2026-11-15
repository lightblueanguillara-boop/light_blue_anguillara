"""Link corti: ogni link lungo nei messaggi diventa <sito>/l/<codice> (regola del committente).

- Codice di 10 caratteri [A-Za-z0-9] da `secrets` (~59 bit: non si indovina). Stesso link = stesso codice.
- Si accorciano solo i link sopra i 60 caratteri, mai le immagini.
- I link con un token monouso (?token=, ?accesso=) scadono dopo 2 giorni.
- La risoluzione è GET /api/l/{codice}; la pagina /l/<codice> del sito porta alla destinazione.
"""
import re
import secrets
import string
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError

from db import db, FRONTEND_URL

ALPHABET = string.ascii_letters + string.digits
MIN_LENGTH = 60
TOKEN_TTL = timedelta(days=2)
_URL = re.compile(r'https?://[^\s<>"\']+')
_TRAILING = '.,;:!?)]}*_~'
_IMAGE = re.compile(r'\.(png|jpe?g|gif|webp|avif|svg)(\?|$)', re.I)


def short_base() -> str:
    return f'{FRONTEND_URL}/l/'


def _skip(url: str) -> bool:
    return (len(url) <= MIN_LENGTH or url.startswith(short_base()) or _IMAGE.search(url) is not None
            or 'res.cloudinary.com' in url)


async def ensure_indexes() -> None:
    await db.short_links.create_index('code', unique=True)
    await db.short_links.create_index('destination', unique=True)


async def shorten(url: str) -> str:
    """Ritorna il link corto (lo crea se serve). I link brevi o le immagini restano come sono."""
    if not url or _skip(url):
        return url
    found = await db.short_links.find_one({'destination': url}, {'_id': 0, 'code': 1})
    if found:
        return short_base() + found['code']
    doc = {'destination': url, 'created_at': datetime.now(timezone.utc).isoformat()}
    if re.search(r'[?&](token|accesso)=', url):
        doc['expires_at'] = (datetime.now(timezone.utc) + TOKEN_TTL).isoformat()
    for _ in range(5):
        doc['code'] = ''.join(secrets.choice(ALPHABET) for _ in range(10))
        try:
            await db.short_links.insert_one(dict(doc))
            return short_base() + doc['code']
        except DuplicateKeyError:
            # stesso link creato nello stesso istante da un'altra richiesta: usiamo quello
            found = await db.short_links.find_one({'destination': url}, {'_id': 0, 'code': 1})
            if found:
                return short_base() + found['code']
    return url   # non dovrebbe mai succedere: meglio il link lungo che nessun link


async def shorten_text(text: str) -> str:
    """Accorcia tutti i link lunghi di un testo (email in testo semplice, WhatsApp)."""
    if not text:
        return text
    out, last = [], 0
    for m in _URL.finditer(text):
        url = m.group(0)
        tail = ''
        while url and url[-1] in _TRAILING:
            tail = url[-1] + tail
            url = url[:-1]
        out.append(text[last:m.start()])
        out.append(await shorten(url) + tail)
        last = m.end()
    out.append(text[last:])
    return ''.join(out)


async def resolve(code: str):
    """Destinazione di un codice, o None se sconosciuto o scaduto."""
    if not re.fullmatch(r'[A-Za-z0-9]{10}', code or ''):
        return None
    doc = await db.short_links.find_one({'code': code}, {'_id': 0})
    if not doc:
        return None
    if doc.get('expires_at') and doc['expires_at'] < datetime.now(timezone.utc).isoformat():
        return None
    return doc['destination']


async def expand(text: str) -> str:
    """Rimette i link veri al posto di quelli corti (usato nei test e nelle anteprime)."""
    base = re.escape(short_base())
    out, last = [], 0
    for m in re.finditer(base + r'([A-Za-z0-9]{10})', text or ''):
        dest = await resolve(m.group(1))
        out.append(text[last:m.start()])
        out.append(dest or m.group(0))
        last = m.end()
    out.append((text or '')[last:])
    return ''.join(out)
