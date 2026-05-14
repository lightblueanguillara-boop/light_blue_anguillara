import asyncio
import logging
from datetime import datetime
import resend

from db import RESEND_API_KEY, SENDER_EMAIL

resend.api_key = RESEND_API_KEY


def _it_date(iso: str) -> str:
    """Convert YYYY-MM-DD -> GG-MM-AAAA. Returns input on failure."""
    if not iso:
        return ''
    try:
        return datetime.strptime(iso, '%Y-%m-%d').strftime('%d-%m-%Y')
    except (ValueError, TypeError):
        return iso


async def send_email_async(to_email: str, subject: str, html: str) -> bool:
    try:
        params = {'from': SENDER_EMAIL, 'to': [to_email], 'subject': subject, 'html': html}
        await asyncio.to_thread(resend.Emails.send, params)
        return True
    except Exception as e:
        logging.warning(f"Email send failed to {to_email}: {e}")
        return False


def email_booking_confirmation_html(booking: dict, settings: dict) -> str:
    villa = settings.get('villa_name', 'Light Blue')
    choice = booking.get('payment_choice')
    paid = booking.get('total_price') if choice == 'full' else booking.get('deposit_amount')
    balance = 0 if choice == 'full' else round(booking.get('total_price', 0) - booking.get('deposit_amount', 0), 2)
    balance_row = ''
    if balance > 0:
        balance_row = f"<tr><td style='padding:8px 0;color:#5C6A79'>Saldo da versare</td><td style='padding:8px 0;text-align:right'>€{balance}</td></tr>"
    return f"""
    <div style="font-family:Manrope,Arial,sans-serif;max-width:560px;margin:0 auto;padding:32px;background:#FAF9F6;color:#2A333C">
      <h1 style="font-family:'Outfit',sans-serif;font-weight:300;font-size:28px;letter-spacing:-0.5px">Prenotazione confermata</h1>
      <p>Ciao {booking.get('guest_name')},</p>
      <p>la tua prenotazione presso <strong>{villa}</strong> è stata confermata.</p>
      <table style="width:100%;border-collapse:collapse;margin:24px 0">
        <tr><td style="padding:8px 0;color:#5C6A79">Check-in</td><td style="padding:8px 0;text-align:right"><strong>{_it_date(booking.get('check_in'))}</strong></td></tr>
        <tr><td style="padding:8px 0;color:#5C6A79">Check-out</td><td style="padding:8px 0;text-align:right"><strong>{_it_date(booking.get('check_out'))}</strong></td></tr>
        <tr><td style="padding:8px 0;color:#5C6A79">Ospiti</td><td style="padding:8px 0;text-align:right">{booking.get('adults')} adulti, {booking.get('children')} bambini</td></tr>
        <tr><td style="padding:8px 0;color:#5C6A79">Totale soggiorno</td><td style="padding:8px 0;text-align:right">€{booking.get('total_price')}</td></tr>
        <tr style="border-top:1px solid #E5E0D8"><td style="padding:12px 0;color:#5C6A79">Pagato ora</td><td style="padding:12px 0;text-align:right;color:#7A93AC"><strong>€{paid}</strong></td></tr>
        {balance_row}
      </table>
      <p>Politica di cancellazione: <strong>{booking.get('cancellation_policy')}</strong></p>
      <p>A presto,<br/>{villa}</p>
      <p style="color:#5C6A79;font-size:12px;margin-top:32px">{settings.get('villa_address','')}<br/>CIR {settings.get('villa_cir','')}</p>
    </div>
    """


def email_balance_reminder_html(booking: dict, settings: dict) -> str:
    villa = settings.get('villa_name', 'Light Blue')
    balance = round(booking.get('total_price', 0) - booking.get('deposit_amount', 0), 2)
    return f"""
    <div style="font-family:Manrope,Arial,sans-serif;max-width:560px;margin:0 auto;padding:32px;background:#FAF9F6;color:#2A333C">
      <h1 style="font-family:'Outfit',sans-serif;font-weight:300;font-size:28px">Promemoria saldo</h1>
      <p>Ciao {booking.get('guest_name')},</p>
      <p>ti ricordiamo che per il tuo soggiorno a <strong>{villa}</strong> dal {_it_date(booking.get('check_in'))} al {_it_date(booking.get('check_out'))} è previsto un saldo residuo di <strong style="color:#7A93AC">€{balance}</strong>.</p>
      <p>Ti chiediamo gentilmente di completare il pagamento. Per qualsiasi domanda rispondi a questa email.</p>
      <p>Grazie,<br/>{villa}</p>
    </div>
    """


def email_admin_contact_notification_html(msg: dict) -> str:
    return f"""
    <div style="font-family:Manrope,Arial,sans-serif;max-width:560px;margin:0 auto;padding:32px;color:#2A333C">
      <h2 style="font-family:'Outfit',sans-serif;font-weight:300">Nuova richiesta dal sito</h2>
      <p><strong>{msg.get('name')}</strong> ({msg.get('email')}) — {msg.get('phone') or 'tel non fornito'}</p>
      <p><em>{msg.get('subject') or 'Senza oggetto'}</em></p>
      <blockquote style="border-left:3px solid #7A93AC;padding-left:12px;color:#5C6A79">{msg.get('message')}</blockquote>
    </div>
    """
