import os
from pathlib import Path
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Environment
JWT_SECRET = os.environ.get('JWT_SECRET', 'changeme')
JWT_ALGO = 'HS256'
STRIPE_API_KEY = os.environ.get('STRIPE_API_KEY', 'sk_test_emergent')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', 're_placeholder_key')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', 'onboarding@resend.dev')
ADMIN_NOTIFY_EMAIL = os.environ.get('ADMIN_NOTIFY_EMAIL', 'admin@lakerelaxvilla.it')
ICAL_SYNC_INTERVAL_HOURS = int(os.environ.get('ICAL_SYNC_INTERVAL_HOURS', '6'))

# Cloudinary (per la gestione della Galleria immagini)
CLOUDINARY_CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME', '')
CLOUDINARY_API_KEY = os.environ.get('CLOUDINARY_API_KEY', '')
CLOUDINARY_API_SECRET = os.environ.get('CLOUDINARY_API_SECRET', '')

# Mongo
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Collezioni (shortcuts per facilitare l'uso nei router)
gallery_collection = db.gallery
settings_collection = db.settings
bookings_collection = db.bookings


async def get_settings() -> dict:
    """Fetch global VillaSettings, seeding defaults if missing.
    Also fills any missing default fields from the model into the persisted doc
    so newly-introduced fields work after schema additions."""
    from models import VillaSettings  # local import avoids cycle
    s = await settings_collection.find_one({'id': 'global'}, {'_id': 0})
    if not s:
        default = VillaSettings().model_dump()
        await settings_collection.insert_one(default.copy())
        return default
    defaults = VillaSettings().model_dump()
    missing = {k: v for k, v in defaults.items() if k not in s}
    if missing:
        await settings_collection.update_one({'id': 'global'}, {'$set': missing})
        s.update(missing)
    return s
