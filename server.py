"""Light Blue - Anguillara Sabazia · backend entry point.

Thin app shell: configures CORS, mounts routers, runs startup tasks (admin seed,
iCal scheduler, cleanup of abandoned checkouts).
"""
import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

load_dotenv()

from auth import hash_pwd  # noqa: E402
from db import db, get_settings, ICAL_SYNC_INTERVAL_MINUTES  # noqa: E402
from ical_service import ical_sync_run  # noqa: E402
from routers.admin import router as admin_router  # noqa: E402
from routers.payments import router as payments_router, cleanup_expired_bookings  # noqa: E402
from routers.public import router as public_router  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# La documentazione interattiva (/docs) è disattivata in produzione: ENABLE_DOCS=1 per attivarla.
_docs = os.environ.get('ENABLE_DOCS') == '1'
app = FastAPI(
    title="Light Blue - Anguillara Sabazia",
    docs_url='/docs' if _docs else None,
    redoc_url=None,
    openapi_url='/openapi.json' if _docs else None,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(public_router)
api_router.include_router(payments_router)
api_router.include_router(admin_router)
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=[o.strip() for o in os.environ.get('CORS_ORIGINS', '*').split(',') if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    if request.url.path.startswith('/api/admin'):
        response.headers.setdefault('Cache-Control', 'no-store')
    return response


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """Errori di validazione come messaggio leggibile (il frontend lo mostra all'utente)."""
    errors = exc.errors()
    msg = 'Dati non validi'
    if errors:
        e = errors[0]
        text = str(e.get('msg', '')).removeprefix('Value error, ')
        field = '.'.join(str(x) for x in e.get('loc', []) if x not in ('body', 'query'))
        msg = text if e.get('type') == 'value_error' or not field else f'{field}: {text}'
    return JSONResponse(status_code=422, content={'detail': msg})


@app.on_event("startup")
async def on_startup():
    # Seed admin (solo se non esiste ancora: per cambiare password usare il pannello)
    seed_email = os.environ.get('ADMIN_SEED_EMAIL')
    seed_pwd = os.environ.get('ADMIN_SEED_PASSWORD')
    if seed_email and seed_pwd:
        existing = await db.admins.find_one({'email': seed_email}, {'_id': 0})
        if not existing:
            await db.admins.insert_one({
                'id': str(uuid.uuid4()),
                'email': seed_email,
                'password_hash': hash_pwd(seed_pwd),
                'token_version': 0,
                'created_at': datetime.now(timezone.utc).isoformat(),
            })
    await get_settings()  # crea le impostazioni di default se mancano
    asyncio.create_task(_ical_scheduler_loop())
    asyncio.create_task(_cleanup_scheduler_loop())


async def _ical_scheduler_loop():
    await asyncio.sleep(30)  # initial delay so app fully starts
    while True:
        try:
            s = await get_settings()
            if s.get('ical_airbnb_url') or s.get('ical_booking_url'):
                result = await ical_sync_run()
                logging.info(f"Scheduled iCal sync: {result}")
        except Exception as e:
            logging.exception(f'Scheduled iCal sync failed: {e}')
        await asyncio.sleep(max(5, ICAL_SYNC_INTERVAL_MINUTES) * 60)


async def _cleanup_scheduler_loop():
    """Gira ogni 5 minuti e rimuove le prenotazioni pending abbandonate."""
    await asyncio.sleep(60)  # attesa iniziale
    while True:
        try:
            await cleanup_expired_bookings()
        except Exception as e:
            logging.exception(f'Cleanup scheduler failed: {e}')
        await asyncio.sleep(5 * 60)  # ogni 5 minuti


@app.on_event("shutdown")
async def shutdown_db_client():
    from db import client
    client.close()
