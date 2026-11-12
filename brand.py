"""Logo e immagine di anteprima del sito: un'unica immagine caricata, tutte le versioni generate.

Dal logo caricato si ricavano favicon, icone dell'app (iPhone/Android), logo per sito ed email.
Dall'immagine di anteprima si ricava la foto 1200x630 usata da WhatsApp/Facebook/Telegram.
Le immagini restano sul nostro server (MongoDB), senza servizi esterni.
"""
import asyncio
import hashlib
import io
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from db import db

ASSETS = Path(__file__).parent / 'assets'
DEFAULT_FILES = {'logo': ASSETS / 'default-logo.jpg', 'share': ASSETS / 'default-share.jpg'}

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
ALLOWED_FORMATS = {'PNG', 'JPEG', 'WEBP'}       # niente SVG: può contenere codice
Image.MAX_IMAGE_PIXELS = 40_000_000              # protezione da immagini "bomba"
MIN_SIZE = {'logo': (256, 256), 'share': (600, 315)}

# Colore di sfondo della schermata d'ingresso dell'app ("blu acqua"): uguale nel frontend
APP_BG_COLOR = (47, 164, 199)   # #2FA4C7

# Schermate di avvio per iPhone/iPad (pixel, verticale): l'iPhone non le genera da solo
SPLASH_SIZES = {
    (640, 1136), (750, 1334), (828, 1792), (1125, 2436), (1170, 2532), (1179, 2556), (1206, 2622),
    (1242, 2208), (1242, 2688), (1284, 2778), (1290, 2796), (1320, 2868),
    (1488, 2266), (1536, 2048), (1640, 2360), (1668, 2224), (1668, 2388), (2048, 2732),
}
_splash_cache: dict = {}

# nome pubblico del file -> (tipo, funzione che lo genera)
LOGO_PNG_SIZES = {'logo-32.png': 32, 'logo-180.png': 180, 'logo-192.png': 192, 'logo-256.png': 256, 'logo-512.png': 512}
PUBLIC_FILES = set(LOGO_PNG_SIZES) | {'favicon.ico', 'logo-maskable-512.png', 'share.jpg'}

_default_cache: dict = {}


class BrandImageError(ValueError):
    """Immagine non valida (messaggio leggibile per l'utente)."""


def _open_checked(data: bytes, kind: str) -> Image.Image:
    if len(data) > MAX_UPLOAD_BYTES:
        raise BrandImageError('Immagine troppo grande (massimo 8 MB)')
    try:
        img = Image.open(io.BytesIO(data))
        img.verify()                       # controlla che sia davvero un'immagine integra
        img = Image.open(io.BytesIO(data))  # verify() invalida l'oggetto: si riapre
        fmt = img.format
        img = ImageOps.exif_transpose(img)  # rispetta la rotazione delle foto da telefono
    except Image.DecompressionBombError:
        raise BrandImageError('Immagine troppo grande in pixel')
    except Exception:
        raise BrandImageError('File non riconosciuto come immagine')
    if fmt not in ALLOWED_FORMATS:
        raise BrandImageError('Formati ammessi: PNG, JPG, WEBP')
    w, h = img.size
    mw, mh = MIN_SIZE[kind]
    if w < mw or h < mh:
        raise BrandImageError(f'Immagine troppo piccola: almeno {mw}×{mh} pixel')
    return img.convert('RGBA')


def _square(img: Image.Image) -> Image.Image:
    """Ritaglio quadrato centrato (il logo dovrebbe essere già quadrato)."""
    w, h = img.size
    side = min(w, h)
    return img.crop(((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side))


def _png(img: Image.Image, size: int) -> bytes:
    out = io.BytesIO()
    img.resize((size, size), Image.LANCZOS).save(out, 'PNG', optimize=True)
    return out.getvalue()


def _on_background(img: Image.Image, size: int, padding: float) -> Image.Image:
    """Logo centrato su sfondo bianco con margine (icone "maskable" di Android)."""
    bg = Image.new('RGBA', (size, size), (255, 255, 255, 255))
    inner = int(size * (1 - 2 * padding))
    logo = img.resize((inner, inner), Image.LANCZOS)
    bg.alpha_composite(logo, ((size - inner) // 2, (size - inner) // 2))
    return bg


def render_logo(data: bytes) -> dict:
    img = _square(_open_checked(data, 'logo'))
    files = {name: _png(img, size) for name, size in LOGO_PNG_SIZES.items()}
    files['logo-maskable-512.png'] = _png(_on_background(img, 512, 0.1), 512)
    ico = io.BytesIO()
    img.resize((48, 48), Image.LANCZOS).save(ico, 'ICO', sizes=[(16, 16), (32, 32), (48, 48)])
    files['favicon.ico'] = ico.getvalue()
    return files


def render_share(data: bytes) -> dict:
    img = _open_checked(data, 'share').convert('RGB')
    img = ImageOps.fit(img, (1200, 630), Image.LANCZOS, centering=(0.5, 0.5))
    out = io.BytesIO()
    img.save(out, 'JPEG', quality=85, optimize=True, progressive=True)  # niente dati EXIF
    return {'share.jpg': out.getvalue()}


def render_splash(logo_png: bytes, w: int, h: int) -> bytes:
    """Logo rotondo centrato su fondo blu acqua (stessa posizione della schermata animata)."""
    bg = Image.new('RGB', (w, h), APP_BG_COLOR)
    side = int(min(w, h) * 0.30)
    logo = Image.open(io.BytesIO(logo_png)).convert('RGBA').resize((side, side), Image.LANCZOS)
    mask = Image.new('L', (side, side), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, side - 1, side - 1), fill=255)
    bg.paste(logo, ((w - side) // 2, (h - side) // 2), mask)
    out = io.BytesIO()
    bg.save(out, 'PNG', optimize=True)
    return out.getvalue()


async def get_splash(w: int, h: int):
    logo512, version = await get_file('logo-512.png')
    key = (version, w, h)
    if key not in _splash_cache:
        if len(_splash_cache) > 60:
            _splash_cache.clear()
        _splash_cache[key] = await asyncio.to_thread(render_splash, logo512, w, h)
    return _splash_cache[key], version


RENDERERS = {'logo': render_logo, 'share': render_share}
KIND_OF = {name: ('share' if name == 'share.jpg' else 'logo') for name in PUBLIC_FILES}


def _defaults(kind: str) -> dict:
    if kind not in _default_cache:
        _default_cache[kind] = RENDERERS[kind](DEFAULT_FILES[kind].read_bytes())
    return _default_cache[kind]


async def get_file(name: str):
    """Ritorna (bytes, versione) del file pubblico richiesto."""
    kind = KIND_OF[name]
    doc = await db.brand_assets.find_one({'kind': kind}, {'_id': 0})
    if doc:
        return doc['files'][name.replace('.', '_')], doc['version']
    return _defaults(kind)[name], 'default'


async def save_upload(kind: str, data: bytes) -> str:
    files = await asyncio.to_thread(RENDERERS[kind], data)  # elaborazione immagini fuori dal ciclo async
    version = hashlib.sha256(data).hexdigest()[:12]
    await db.brand_assets.update_one(
        {'kind': kind},
        {'$set': {
            'kind': kind,
            'version': version,
            # le chiavi Mongo non possono contenere punti
            'files': {k.replace('.', '_'): v for k, v in files.items()},
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )
    await db.settings.update_one({'id': 'global'}, {'$set': {f'{kind}_version': version}}, upsert=True)
    return version


async def reset(kind: str) -> None:
    await db.brand_assets.delete_one({'kind': kind})
    await db.settings.update_one({'id': 'global'}, {'$set': {f'{kind}_version': 'default'}}, upsert=True)
