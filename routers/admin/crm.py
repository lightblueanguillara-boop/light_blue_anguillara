"""CRM: subscribers + marketing email blasts."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
import resend
from fastapi import APIRouter, Depends, HTTPException
from auth import get_current_admin
from db import db, SENDER_EMAIL
from models import MarketingEmail

resend.api_key = None  # viene inizializzato da email_helpers tramite db, qui usiamo resend direttamente

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
    Invia una campagna email ESCLUSIVAMENTE ai destinatari presenti
    nell'array 'recipients' ricevuto dal frontend.

    FIX: non viene mai interrogato il DB degli iscritti per allargare
    la lista — si usano SOLO le email passate esplicitamente.
    """
    from db import RESEND_API_KEY
    import resend as _resend
    _resend.api_key = RESEND_API_KEY

    # ---------------------------------------------------------------
    # FIX: usiamo esclusivamente la lista passata dal frontend.
    # La convertiamo in lista di stringhe pure per sicurezza
    # (EmailStr di pydantic è già validato, ma lo normalizziamo).
    # ---------------------------------------------------------------
    target_emails = [str(e).lower().strip() for e in payload.recipients]

    if not target_emails:
        raise HTTPException(
            status_code=400,
            detail="Nessun destinatario selezionato nella lista di invio."
        )

    logging.info(
        f"Marketing send avviato: {len(target_emails)} destinatari selezionati — "
        f"{target_emails[:5]}{'...' if len(target_emails) > 5 else ''}"
    )

    sent, failed, errors = 0, 0, []

    # Invio uno per uno per gestire gli errori singolarmente
    for email_addr in target_emails:
        try:
            params = {
                'from': SENDER_EMAIL,
                'to': [email_addr],           # sempre una sola email per invio
                'subject': payload.subject,
                'html': payload.html_content,
            }
            await asyncio.to_thread(_resend.Emails.send, params)
            sent += 1
            logging.info(f"Marketing: email inviata a {email_addr}")
        except Exception as e:
            failed += 1
            err_msg = f"Fallito invio a {email_addr}: {str(e)}"
            errors.append(err_msg)
            logging.warning(err_msg)

    # Log nello storico
    log_doc = {
        'id': str(uuid.uuid4()),
        'subject': payload.subject,
        'html_content': payload.html_content,
        'sent_count': sent,
        'failed_count': failed,
        'total': len(target_emails),
        'recipients_list': target_emails,
        'created_at': datetime.now(timezone.utc).isoformat(),
    }

    await db.marketing_logs.insert_one(log_doc)

    logging.info(f"Marketing send completato: {sent} inviati, {failed} falliti su {len(target_emails)} totali.")

    return {
        'sent': sent,
        'failed': failed,
        'total': len(target_emails),
        'errors': errors[:5]
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
