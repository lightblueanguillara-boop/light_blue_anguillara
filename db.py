import os
from pathlib import Path
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# --- Sicurezza -------------------------------------------------------------
# JWT_SECRET firma i token di accesso admin: deve essere lungo e casuale.
# Genera un valore con:  python -c "import secrets; print(secrets.token_urlsafe(48))"
JWT_SECRET = os.environ.get('JWT_SECRET', '')
if len(JWT_SECRET) < 32 or JWT_SECRET in ('changeme', 'secret'):
    raise RuntimeError('JWT_SECRET mancante o troppo debole (minimo 32 caratteri casuali).')
JWT_ALGO = 'HS256'

# --- Fornitori esterni -----------------------------------------------------
STRIPE_API_KEY = os.environ.get('STRIPE_API_KEY', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '')
SENDER_EMAIL = os.environ.get('SENDER_EMAIL', 'onboarding@resend.dev')
ADMIN_NOTIFY_EMAIL = os.environ.get('ADMIN_NOTIFY_EMAIL', '')
# Ogni quanto rileggere i calendari Airbnb/Booking (più è breve, minore il rischio di doppie prenotazioni)
ICAL_SYNC_INTERVAL_MINUTES = int(os.environ.get('ICAL_SYNC_INTERVAL_MINUTES', '30'))

# URL pubblico del sito (per i link nelle email, es. disiscrizione newsletter)
FRONTEND_URL = os.environ.get('FRONTEND_URL', 'https://www.lightblueanguillara.com').rstrip('/')
# URL pubblico del backend (per il logo nelle email)
PUBLIC_API_URL = os.environ.get('PUBLIC_API_URL', 'https://api.lightblueanguillara.com').rstrip('/')

# Notifiche push (Web Push / VAPID). Genera le chiavi con:  python scripts/gen_vapid_keys.py
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY', '')
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY', '')
VAPID_SUBJECT = os.environ.get('VAPID_SUBJECT', 'mailto:info@lightblueanguillara.com')

# Cloudinary (gestione della Galleria immagini)
CLOUDINARY_CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME', '')
CLOUDINARY_API_KEY = os.environ.get('CLOUDINARY_API_KEY', '')
CLOUDINARY_API_SECRET = os.environ.get('CLOUDINARY_API_SECRET', '')

# --- Mongo -----------------------------------------------------------------
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# Collezioni (shortcuts per facilitare l'uso nei router)
gallery_collection = db.gallery
settings_collection = db.settings
bookings_collection = db.bookings


async def ensure_settings_unique() -> None:
    """Un solo documento di impostazioni: indice unico su `id`.

    Se per una vecchia condizione di gara esistessero già copie doppie, si tiene quella che il sito
    sta già usando (la prima in ordine di inserimento, la stessa che restituisce find_one) e si tolgono le altre."""
    docs = await settings_collection.find({'id': 'global'}, {'_id': 1}).sort('$natural', 1).to_list(100)
    if len(docs) > 1:
        await settings_collection.delete_many({'_id': {'$in': [d['_id'] for d in docs[1:]]}})
    await settings_collection.create_index('id', unique=True)


async def get_settings() -> dict:
    """Fetch global VillaSettings, seeding defaults if missing.
    Also fills any missing default fields from the model into the persisted doc
    so newly-introduced fields work after schema additions."""
    from models import VillaSettings  # local import avoids cycle
    s = await settings_collection.find_one({'id': 'global'}, {'_id': 0})
    if not s:
        # upsert atomico: due richieste contemporanee non possono creare due documenti
        try:
            await settings_collection.update_one(
                {'id': 'global'}, {'$setOnInsert': VillaSettings().model_dump()}, upsert=True)
        except DuplicateKeyError:
            pass   # creato nello stesso istante da un'altra richiesta: va bene così
        s = await settings_collection.find_one({'id': 'global'}, {'_id': 0})
    defaults = VillaSettings().model_dump()
    missing = {k: v for k, v in defaults.items() if k not in s}
    if missing:
        await settings_collection.update_one({'id': 'global'}, {'$set': missing})
        s.update(missing)
    return s
