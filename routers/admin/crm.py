"""CRM: subscribers + marketing email blasts."""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from auth import get_current_admin, newsletter_token
from db import db, SENDER_EMAIL, FRONTEND_URL, RESEND_API_KEY, get_settings
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
    Invia una campagna email ESCLUSIVAMENTE ai destinatari presenti
    nell'array 'recipients' ricevuto dal frontend.

    FIX: non viene mai interrogato il DB degli iscritti per allargare
    la lista — si usano SOLO le email passate esplicitamente.
    """
    import resend as _resend
    _resend.api_key = RESEND_API_KEY

    requested = list(dict.fromkeys(str(e).lower().strip() for e in payload.recipients))
    if not requested:
        raise HTTPException(status_code=400, detail="Nessun destinatario selezionato nella lista di invio.")

    # GDPR / ePrivacy: le email promozionali possono andare SOLO a chi ha dato il consenso
    # (iscritti presenti nel CRM) e mai a chi si è disiscritto.
    subs = await db.subscribers.find({}, {'_id': 0, 'email': 1}).to_list(100000)
    subscribed = {s['email'].lower() for s in subs}
    unsub = {u['email'].lower() for u in await db.unsubscribed.find({}, {'_id': 0, 'email': 1}).to_list(100000)}
    target_emails = [e for e in requested if e in subscribed and e not in unsub]
    skipped = [e for e in requested if e not in target_emails]

    if not target_emails:
        raise HTTPException(400, "Nessuno dei destinatari selezionati risulta iscritto alla newsletter.")

    logging.info(f"Marketing send avviato: {len(target_emails)} destinatari (esclusi {len(skipped)} non iscritti)")

    settings = await get_settings()
    villa = settings.get('villa_name', 'Light Blue')
    sent, failed, errors = 0, 0, []

    # Invio uno per uno: ogni email ha il proprio link di disiscrizione
    for email_addr in target_emails:
        unsub_url = f"{FRONTEND_URL}/disiscrizione?token={newsletter_token(email_addr)}"
        footer = (
            '<div style="font-family:Arial,sans-serif;font-size:12px;color:#888;margin-top:32px;'
            'padding-top:16px;border-top:1px solid #eee;text-align:center">'
            f'Ricevi questa email perché ti sei iscritto alle comunicazioni di {villa}.<br/>'
            f'<a href="{unsub_url}" style="color:#888">Disiscriviti</a> in qualsiasi momento.</div>'
        )
        try:
            params = {
                'from': SENDER_EMAIL,
                'to': [email_addr],
                'subject': payload.subject,
                'html': payload.html_content + footer,
                'headers': {'List-Unsubscribe': f'<{unsub_url}>'},
            }
            await asyncio.to_thread(_resend.Emails.send, params)
            sent += 1
        except Exception as e:
            failed += 1
            errors.append(f"Fallito invio a {email_addr}: {str(e)[:200]}")
            logging.warning(errors[-1])

    # Log nello storico
    log_doc = {
        'id': str(uuid.uuid4()),
        'subject': payload.subject,
        'html_content': payload.html_content,
        'sent_count': sent,
        'failed_count': failed,
        'total': len(target_emails),
        'recipients_list': target_emails,
        'skipped_not_subscribed': skipped,
        'created_at': datetime.now(timezone.utc).isoformat(),
    }

    await db.marketing_logs.insert_one(log_doc)

    logging.info(f"Marketing send completato: {sent} inviati, {failed} falliti su {len(target_emails)} totali.")

    return {
        'sent': sent,
        'failed': failed,
        'total': len(target_emails),
        'skipped': len(skipped),
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
