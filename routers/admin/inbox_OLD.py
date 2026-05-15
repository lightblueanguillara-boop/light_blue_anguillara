"""Inbox: contact messages."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import get_current_admin
from db import db
from email_helpers import send_email_async
from models import MessageUpdate

router = APIRouter()


class MessageReply(BaseModel):
    subject: str
    html: str


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


@router.post("/admin/messages/{msg_id}/reply")
async def reply_message(
    msg_id: str, body: MessageReply, admin=Depends(get_current_admin)
):
    """Send an HTML reply email to the contact, then mark as replied."""
    msg = await db.contact_messages.find_one({'id': msg_id}, {'_id': 0})
    if not msg:
        raise HTTPException(404, 'Messaggio non trovato')

    ok = await send_email_async(msg['email'], body.subject, body.html)
    if not ok:
        raise HTTPException(502, 'Invio email fallito — controlla le credenziali Resend')

    await db.contact_messages.update_one({'id': msg_id}, {'$set': {'status': 'replied'}})
    return await db.contact_messages.find_one({'id': msg_id}, {'_id': 0})


@router.delete("/admin/messages/{msg_id}")
async def delete_message(msg_id: str, admin=Depends(get_current_admin)):
    result = await db.contact_messages.delete_one({'id': msg_id})
    if result.deleted_count == 0:
        raise HTTPException(404, 'Messaggio non trovato')
    return {'ok': True}
