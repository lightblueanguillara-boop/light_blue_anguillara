"""Admin authentication: login + identity check."""
from fastapi import APIRouter, Depends, HTTPException

from auth import create_token, get_current_admin, verify_pwd
from db import db
from models import AdminLogin

router = APIRouter()


@router.post("/admin/login")
async def admin_login(payload: AdminLogin):
    admin = await db.admins.find_one({'email': payload.email}, {'_id': 0})
    if not admin or not verify_pwd(payload.password, admin['password_hash']):
        raise HTTPException(401, 'Credenziali non valide')
    token = create_token(payload.email)
    return {'token': token, 'email': payload.email}


@router.get("/admin/me")
async def admin_me(admin=Depends(get_current_admin)):
    return {'email': admin['email']}
