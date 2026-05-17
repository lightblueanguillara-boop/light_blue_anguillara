"""Inbox: contact messages with chat history."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from datetime import datetime, timezone
import uuid

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
    """Invia l'email e salva la risposta nella cronologia 'chat'."""
    msg = await db.contact_messages.find_one({'id': msg_id}, {'_id': 0})
    if not msg:
        raise HTTPException(404, 'Messaggio non trovato')

    # Invia l'email reale
    ok = await send_email_async(msg['email'], body.subject, body.html)
    if not ok:
        raise HTTPException(502, 'Invio email fallito — controlla le credenziali Resend')

    # Crea l'oggetto risposta da salvare nel DB
    new_reply = {
        "id": str(uuid.uuid4()),
        "content": body.html,
        "subject": body.subject,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sender": "admin"
    }

    # Aggiorna il messaggio aggiungendo la risposta nella chat e modificando lo stato in risposto
    await db.contact_messages.update_one(
        {'id': msg_id},
        {
            '$push': {'chat': new_reply},
            '$set': {'status': 'replied', 'updated_at': datetime.now(timezone.utc).isoformat()}
        }
    )

    return await db.contact_messages.find_one({'id': msg_id}, {'_id': 0})

@router.delete("/admin/messages/{msg_id}")
async def delete_message(msg_id: str, admin=Depends(get_current_admin)):
    """Elimina definitivamente una chat/messaggio dal database."""
    msg = await db.contact_messages.find_one({'id': msg_id})
    if not msg:
        raise HTTPException(404, 'Messaggio non trovato')
    
    await db.contact_messages.delete_one({'id': msg_id})
    return {'ok': True, 'detail': 'Messaggio eliminato con successo'}
