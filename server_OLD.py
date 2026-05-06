"""Light Blue - Anguillara Sabazia · backend entry point.

Thin app shell: configures CORS, mounts routers, runs startup tasks (admin seed,
settings migration, iCal scheduler).
"""
import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI
from starlette.middleware.cors import CORSMiddleware

from auth import hash_pwd
from db import db, get_settings, ICAL_SYNC_INTERVAL_HOURS
from ical_service import ical_sync_run
from models import VillaSettings
from routers.admin import router as admin_router
from routers.payments import router as payments_router
from routers.public import router as public_router

load_dotenv()

app = FastAPI(title="Light Blue - Anguillara Sabazia")

api_router = APIRouter(prefix="/api")
api_router.include_router(public_router)
api_router.include_router(payments_router)
api_router.include_router(admin_router)
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    # Seed admin
    seed_email = os.environ.get('ADMIN_SEED_EMAIL')
    seed_pwd = os.environ.get('ADMIN_SEED_PASSWORD')
    if seed_email and seed_pwd:
        existing = await db.admins.find_one({'email': seed_email}, {'_id': 0})
        if not existing:
            await db.admins.insert_one({
                'id': str(uuid.uuid4()),
                'email': seed_email,
                'password_hash': hash_pwd(seed_pwd),
                'created_at': datetime.now(timezone.utc).isoformat(),
            })
    # Ensure settings exist + migrate placeholder demo data
    s = await get_settings()
    if s.get('villa_name') in ('Lake Relax Villa', None):
        defaults = VillaSettings().model_dump()
        migrate = {k: defaults[k] for k in [
            'villa_name', 'villa_address', 'villa_email',
            'villa_description', 'villa_cir', 'villa_lake',
        ]}
        await db.settings.update_one({'id': 'global'}, {'$set': migrate}, upsert=True)
    # Background iCal scheduler
    asyncio.create_task(_ical_scheduler_loop())


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
        await asyncio.sleep(ICAL_SYNC_INTERVAL_HOURS * 3600)


@app.on_event("shutdown")
async def shutdown_db_client():
    from db import client
    client.close()


logging.basicConfig(
    level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
