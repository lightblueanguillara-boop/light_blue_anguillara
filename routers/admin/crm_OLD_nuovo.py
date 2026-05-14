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
    """Recupera la lista completa dei sottoscrittori ordinata per data."""
    return await db.subscribers.find({}, {'_id': 0}).sort('created_at', -1).to_list(10000)

@router.delete("/admin/subscribers/{sub_id}")
async def delete_subscriber(sub_id: str, admin=Depends(get_current_admin)):
    """Elimina un sottoscrittore dal database."""
    await db.subscribers.delete_one({'id': sub_id})
    return {'ok': True}

@router.post("/admin/marketing/send")
async def send_marketing(payload: MarketingEmail, admin=Depends(get_current_admin)):
    """
    Invia una campagna email ai destinatari selezionati nel frontend.
    La logica è ora filtrata esclusivamente sull'array 'recipients'.
    """
    target_emails = payload.recipients
    
    if not target_emails:
        raise HTTPException(
            status_code=400, 
            detail="Nessun destinatario selezionato nella lista di invio."
        )

    sent, failed, errors = 0, 0, []
    
    # Esecuzione dell'invio massivo tramite thread pool per non bloccare l'event loop
    for email_addr in target_emails:
        try:
            params = {
                'from': SENDER_EMAIL,
                'to': [email_addr],
                'subject': payload.subject,
                'html': payload.html_content,
            }
            # Utilizziamo to_thread perché la libreria 'resend' è sincrona
            await asyncio.to_thread(resend.Emails.send, params)
            sent += 1
        except Exception as e:
            failed += 1
            errors.append(f"Fallito invio a {email_addr}: {str(e)}")
    
    # Registrazione del Log nello storico per la visualizzazione nel frontend
    log_doc = {
        'id': str(uuid.uuid4()),
        'subject': payload.subject,
        'html_content': payload.html_content,
        'sent_count': sent,
        'failed_count': failed,
        'total': len(target_emails),
        'recipients_list': target_emails, # Salviamo la lista completa per riferimento futuro
        'created_at': datetime.now(timezone.utc).isoformat(),
    }
    
    await db.marketing_logs.insert_one(log_doc)
    
    return {
        'sent': sent, 
        'failed': failed, 
        'total': len(target_emails), 
        'errors': errors[:5] # Ritorna solo i primi 5 errori per brevità
    }

@router.get("/admin/marketing/logs")
async def marketing_logs(admin=Depends(get_current_admin)):
    """Recupera lo storico delle campagne inviate."""
    return await db.marketing_logs.find({}, {'_id': 0}).sort('created_at', -1).to_list(500)

@router.delete("/admin/marketing/logs/{log_id}")
async def delete_marketing_log(log_id: str, admin=Depends(get_current_admin)):
    """Elimina un record dallo storico dei log."""
    await db.marketing_logs.delete_one({'id': log_id})
    return {'ok': True}
