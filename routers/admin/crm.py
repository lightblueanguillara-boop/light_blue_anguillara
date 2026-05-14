"""CRM: subscribers + marketing email blasts."""
import asyncio
import uuid
from datetime import datetime, timezone
import resend
from fastapi import APIRouter, Depends, HTTPException
from auth import get_current_admin
from db import db, SENDER_EMAIL
from models import MarketingEmail

router = APIRouter()

@router.get("/admin/subscribers")
async def list_subscribers(admin=Depends(get_current_admin)):
    return await db.subscribers.find({}, {'_id': 0}).sort('created_at', -1).to_list(10000)

@router.delete("/admin/subscribers/{sub_id}")
async def delete_subscriber(sub_id: str, admin=Depends(get_current_admin)):
    await db.subscribers.delete_one({'id': sub_id})
    return {'ok': True}

@router.post("/admin/marketing/send")
async def send_marketing(payload: MarketingEmail, admin=Depends(get_current_admin)):
    # 1. Recupero tutti gli iscritti che hanno dato il consenso
    all_subs = await db.subscribers.find({'consent': True}, {'_id': 0}).to_list(10000)
    
    # 2. Logica di filtraggio basata sulla selezione frontend
    if payload.recipients and len(payload.recipients) > 0:
        # Se abbiamo ricevuto una lista di email, filtriamo per indirizzo email
        subs_to_send = [s for s in all_subs if s['email'] in payload.recipients]
    elif payload.recipient_ids and len(payload.recipient_ids) > 0:
        # Se abbiamo ricevuto una lista di ID, filtriamo per ID
        subs_to_send = [s for s in all_subs if s['id'] in payload.recipient_ids]
    else:
        # Se non c'è selezione, inviamo a tutti quelli che hanno dato il consenso
        subs_to_send = all_subs

    if not subs_to_send:
        return {'sent': 0, 'failed': 0, 'total': 0, 'message': 'Nessun destinatario selezionato'}

    sent, failed, errors = 0, 0, []
    
    # 3. Ciclo di invio effettivo
    for s in subs_to_send:
        try:
            params = {
                'from': SENDER_EMAIL,
                'to': [s['email']],
                'subject': payload.subject,
                'html': payload.html_content,
            }
            # Libreria Resend è sincrona, usiamo asyncio.to_thread
            await asyncio.to_thread(resend.Emails.send, params)
            sent += 1
        except Exception as e:
            failed += 1
            errors.append(str(e))
    
    # 4. Creazione Log Storico
    log_doc = {
        'id': str(uuid.uuid4()),
        'subject': payload.subject,
        'html_content': payload.html_content,
        'sent_count': sent,
        'failed_count': failed,
        'total': len(subs_to_send),
        'recipients_list': [s['email'] for s in subs_to_send],
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    await db.marketing_logs.insert_one(log_doc)
    
    return {
        'sent': sent, 
        'failed': failed, 
        'total': len(subs_to_send), 
        'errors': errors[:3]
    }

@router.get("/admin/marketing/logs")
async def marketing_logs(admin=Depends(get_current_admin)):
    return await db.marketing_logs.find({}, {'_id': 0}).sort('created_at', -1).to_list(500)

@router.delete("/admin/marketing/logs/{log_id}")
async def delete_marketing_log(log_id: str, admin=Depends(get_current_admin)):
    await db.marketing_logs.delete_one({'id': log_id})
    return {'ok': True}
