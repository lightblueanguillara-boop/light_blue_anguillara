"""Admin authentication: login, identity check, password change."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from auth import create_token, get_current_admin, hash_pwd, verify_pwd
from db import db
from models import AdminLogin, PasswordChange
from ratelimit import check_rate, rate_limit

router = APIRouter()


@router.post("/admin/login")
async def admin_login(payload: AdminLogin, request: Request):
    # max 10 tentativi ogni 15 minuti per IP e per account
    rate_limit(request, 'login', 10, 15 * 60)
    check_rate(f'login-email:{payload.email.lower()}', 10, 15 * 60)
    admin = await db.admins.find_one({'email': payload.email}, {'_id': 0})
    if not admin or not verify_pwd(payload.password, admin['password_hash']):
        raise HTTPException(401, 'Credenziali non valide')
    token = create_token(payload.email, admin.get('token_version', 0))
    return {'token': token, 'email': payload.email}


@router.get("/admin/me")
async def admin_me(admin=Depends(get_current_admin)):
    return {'email': admin['email']}


@router.post("/admin/password")
async def change_password(payload: PasswordChange, request: Request, admin=Depends(get_current_admin)):
    rate_limit(request, 'password', 5, 15 * 60)
    if not verify_pwd(payload.current_password, admin['password_hash']):
        raise HTTPException(400, 'La password attuale non è corretta')
    if payload.current_password == payload.new_password:
        raise HTTPException(400, 'La nuova password deve essere diversa da quella attuale')
    new_version = admin.get('token_version', 0) + 1
    await db.admins.update_one(
        {'email': admin['email']},
        {'$set': {
            'password_hash': hash_pwd(payload.new_password),
            'token_version': new_version,
            'password_changed_at': datetime.now(timezone.utc).isoformat(),
        }},
    )
    # nuovo token per la sessione corrente; tutte le altre vengono invalidate
    return {'ok': True, 'token': create_token(admin['email'], new_version)}
