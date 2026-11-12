from datetime import datetime, timezone, timedelta
import bcrypt
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from db import db, JWT_SECRET, JWT_ALGO

security = HTTPBearer()
TOKEN_DAYS = 7


def hash_pwd(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()


def verify_pwd(p: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(p.encode(), h.encode())
    except Exception:
        return False


def create_token(email: str, token_version: int = 0) -> str:
    payload = {
        'email': email,
        'tv': token_version,  # cambia a ogni cambio password -> invalida i token precedenti
        'exp': datetime.now(timezone.utc) + timedelta(days=TOKEN_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGO)


async def get_current_admin(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.PyJWTError:
        raise HTTPException(401, 'Invalid token')
    admin = await db.admins.find_one({'email': payload.get('email')}, {'_id': 0})
    if not admin or payload.get('tv', 0) != admin.get('token_version', 0):
        raise HTTPException(401, 'Invalid token')
    return admin


# --- Token di disiscrizione newsletter ---------------------------------------
import hashlib  # noqa: E402
import hmac  # noqa: E402


def newsletter_token(email: str) -> str:
    """Token firmato per il link di disiscrizione: email + HMAC (non scade, non indovinabile)."""
    import base64
    e = email.strip().lower()
    sig = hmac.new(f'unsub:{JWT_SECRET}'.encode(), e.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f'{e}|{sig}'.encode()).decode().rstrip('=')


def email_from_newsletter_token(token: str):
    import base64
    try:
        raw = base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)).decode()
        e, sig = raw.rsplit('|', 1)
    except Exception:
        return None
    expected = hmac.new(f'unsub:{JWT_SECRET}'.encode(), e.encode(), hashlib.sha256).hexdigest()[:32]
    return e if hmac.compare_digest(sig, expected) else None
