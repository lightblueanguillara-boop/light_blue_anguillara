"""Inbox: contact messages."""
from fastapi import APIRouter, Depends, HTTPException

from auth import get_current_admin
from db import db
from models import MessageUpdate

router = APIRouter()


@router.get("/admin/messages")
async def list_messages(admin=Depends(get_current_admin)):
    return await db.contact_messages.find({}, {'_id': 0}).sort('created_at', -1).to_list(10000)


@router.patch("/admin/messages/{msg_id}")
async def update_message(
    msg_id: str, updates: MessageUpdate, admin=Depends(get_current_admin)
):
    patch = updates.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(400, 'No fields to update')
    await db.contact_messages.update_one({'id': msg_id}, {'$set': patch})
    return await db.contact_messages.find_one({'id': msg_id}, {'_id': 0})


@router.delete("/admin/messages/{msg_id}")
async def delete_message(msg_id: str, admin=Depends(get_current_admin)):
    result = await db.contact_messages.delete_one({'id': msg_id})
    if result.deleted_count == 0:
        raise HTTPException(404, 'Messaggio non trovato')
    return {'ok': True}
