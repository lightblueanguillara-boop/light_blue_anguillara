import asyncio
import logging
from datetime import datetime, timezone, timedelta
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


def _cancellation_policy_info(booking: dict, settings: dict) -> dict:
    """
    Restituisce il testo descrittivo della politica di cancellazione
    e la data ultima entro cui è possibile disdire con rimborso completo.

    La politica viene letta in questo ordine:
      1. booking['cancellation_policy'] (salvato al momento della prenotazione)
      2. settings['default_cancellation_policy'] (fallback dalle impostazioni)
      3. 'moderate' (default assoluto)

    Regole (allineate a compute_refund_amount in pricing.py):
      - flexible : rimborso 100% fino a 24h prima del check-in
      - moderate : rimborso 100% fino a 7 giorni prima del check-in;
                   50% da 1 a 7 giorni; 0% nelle ultime 24h
      - strict   : rimborso 100% entro 48h dalla prenotazione E
                   almeno 14 giorni prima del check-in;
                   50% fino a 7 giorni prima; 0% oltre
    """
    policy = (
        booking.get('cancellation_policy')
        or settings.get('default_cancellation_policy')
        or 'moderate'
    )

    try:
        check_in_dt = datetime.strptime(booking['check_in'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
    except (KeyError, ValueError, TypeError):
        check_in_dt = None

    labels = {
        'flexible': 'Flessibile',
        'moderate': 'Moderata',
        'strict':   'Rigorosa',
    }
    label = labels.get(policy, policy.capitalize())

    if policy == 'flexible':
        description = (
            'Rimborso completo (100%) se disdici almeno 24 ore prima del check-in. '
            'Nessun rimborso nelle ultime 24 ore.'
        )
        deadline_dt = check_in_dt - timedelta(hours=24) if check_in_dt else None
        deadline_label = 'Disdetta gratuita entro le ore 00:00 del'

    elif policy == 'moderate':
        description = (
            'Rimborso completo (100%) se disdici almeno 7 giorni prima del check-in. '
            'Rimborso del 50% da 1 a 7 giorni prima. '
            'Nessun rimborso nelle ultime 24 ore.'
        )
        deadline_dt = check_in_dt - timedelta(days=7) if check_in_dt else None
        deadline_label = 'Disdetta gratuita entro'

    else:  # strict
        description = (
            'Rimborso completo (100%) solo se disdici entro 48 ore dalla prenotazione '
            'E almeno 14 giorni prima del check-in. '
            'Rimborso del 50% fino a 7 giorni prima del check-in. '
            'Nessun rimborso oltre.'
        )
        # Per la "strict" il termine più favorevole al cliente è 14gg prima del check-in
        deadline_dt = check_in_dt - timedelta(days=14) if check_in_dt else None
        deadline_label = 'Disdetta gratuita entro'

    if deadline_dt:
        deadline_str = deadline_dt.strftime('%d/%m/%Y')
        deadline_text = f'{deadline_label} il <strong>{deadline_str}</strong>'
    else:
        deadline_text = ''

    return {
        'policy':      policy,
        'label':       label,
        'description': description,
        'deadline_text': deadline_text,
    }


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

    # Recupera testo politica e data limite calcolata dinamicamente
    pol = _cancellation_policy_info(booking, settings)

    cancellation_block = f"""
      <tr style="border-top:1px solid #E5E0D8">
        <td colspan="2" style="padding:16px 0 4px 0">
          <strong style="color:#2A333C">Politica di cancellazione: {pol['label']}</strong><br/>
          <span style="color:#5C6A79;font-size:13px">{pol['description']}</span>
          {"<br/><span style='color:#7A93AC;font-size:13px;margin-top:4px;display:inline-block'>" + pol['deadline_text'] + "</span>" if pol['deadline_text'] else ""}
        </td>
      </tr>
    """

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
        {cancellation_block}
      </table>
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


def email_cancellation_html(booking: dict, settings: dict) -> str:
    """Email di cancellazione prenotazione — stessa grafica della conferma."""
    villa = settings.get('villa_name', 'Light Blue')
    total_paid = 0.0
    if booking.get('payment_status') == 'deposit_paid':
        total_paid = booking.get('deposit_amount', 0)
    elif booking.get('payment_status') == 'fully_paid':
        total_paid = booking.get('total_price', 0)

    refund_row = ''
    if total_paid and total_paid > 0:
        refund_row = f"""
        <tr style="border-top:1px solid #E5E0D8">
          <td colspan="2" style="padding:16px 0 4px 0">
            <strong style="color:#2A333C">Rimborso</strong><br/>
            <span style="color:#5C6A79;font-size:13px">
              Il rimborso verrà accreditato entro
              <strong>5 giorni lavorativi</strong> sul metodo di pagamento originale.
            </span>
          </td>
        </tr>"""

    return f"""
    <div style="font-family:Manrope,Arial,sans-serif;max-width:560px;margin:0 auto;padding:32px;background:#FAF9F6;color:#2A333C">
      <h1 style="font-family:'Outfit',sans-serif;font-weight:300;font-size:28px;letter-spacing:-0.5px">Prenotazione cancellata</h1>
      <p>Ciao {booking.get('guest_name')},</p>
      <p>la tua prenotazione presso <strong>{villa}</strong> è stata cancellata.</p>
      <table style="width:100%;border-collapse:collapse;margin:24px 0">
        <tr><td style="padding:8px 0;color:#5C6A79">Check-in</td><td style="padding:8px 0;text-align:right"><strong>{_it_date(booking.get('check_in'))}</strong></td></tr>
        <tr><td style="padding:8px 0;color:#5C6A79">Check-out</td><td style="padding:8px 0;text-align:right"><strong>{_it_date(booking.get('check_out'))}</strong></td></tr>
        <tr><td style="padding:8px 0;color:#5C6A79">Ospiti</td><td style="padding:8px 0;text-align:right">{booking.get('adults', 1)} adulti, {booking.get('children', 0)} bambini</td></tr>
        <tr><td style="padding:8px 0;color:#5C6A79">Totale soggiorno</td><td style="padding:8px 0;text-align:right">€{booking.get('total_price')}</td></tr>
        {refund_row}
      </table>
      <p style="color:#5C6A79;font-size:14px">
        Per qualsiasi domanda o chiarimento non esitare a contattarci.
      </p>
      <p>A presto,<br/>{villa}</p>
      <p style="color:#5C6A79;font-size:12px;margin-top:32px">{settings.get('villa_address','')}<br/>CIR {settings.get('villa_cir','')}</p>
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
