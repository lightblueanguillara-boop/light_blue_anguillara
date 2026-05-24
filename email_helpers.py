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
    basato sul campo is_refundable della prenotazione.
    
    Regole nuove:
    - is_refundable = True:  "Rimborso completo (100%) fino a 10 giorni prima del check-in"
    - is_refundable = False: "Nessun rimborso in caso di cancellazione o mancata presentazione"
    """
    is_refundable = booking.get('is_refundable', True)
    
    try:
        check_in_dt = datetime.strptime(booking['check_in'], '%Y-%m-%d').replace(tzinfo=timezone.utc)
    except (KeyError, ValueError, TypeError):
        check_in_dt = None
    
    if is_refundable:
        label = 'Rimborsabile'
        description = (
            'Rimborso completo (100%) se disdici almeno 10 giorni prima del check-in. '
            'Nessun rimborso entro 10 giorni dal check-in.'
        )
        deadline_dt = check_in_dt - timedelta(days=10) if check_in_dt else None
        deadline_label = 'Disdetta gratuita entro'
    else:
        label = 'Non Rimborsabile'
        description = (
            'Nessun rimborso in caso di cancellazione o mancata presentazione. '
            'La tariffa non rimborsabile non è modificabile dopo la conferma.'
        )
        deadline_dt = None
        deadline_label = ''
    
    if deadline_dt:
        deadline_str = deadline_dt.strftime('%d/%m/%Y')
        deadline_text = f'{deadline_label} il <strong>{deadline_str}</strong>'
    else:
        deadline_text = ''
    
    return {
        'policy': 'refundable' if is_refundable else 'non_refundable',
        'label': label,
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

    # Recupera testo politica basato su is_refundable
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


def email_modification_confirmation_html(booking: dict, settings: dict) -> str:
    """Email di modifica prenotazione — simile alla conferma."""
    villa = settings.get('villa_name', 'Light Blue')
    choice = booking.get('payment_choice')
    paid = booking.get('total_price') if choice == 'full' else booking.get('deposit_amount')
    balance = 0 if choice == 'full' else round(booking.get('total_price', 0) - booking.get('deposit_amount', 0), 2)
    balance_row = ''
    if balance > 0:
        balance_row = f"<tr><td style='padding:8px 0;color:#5C6A79'>Saldo da versare</td><td style='padding:8px 0;text-align:right'>€{balance}</td></tr>"

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
      <h1 style="font-family:'Outfit',sans-serif;font-weight:300;font-size:28px;letter-spacing:-0.5px">Modifica prenotazione</h1>
      <p>Ciao {booking.get('guest_name')},</p>
      <p>la tua prenotazione presso <strong>{villa}</strong> è stata modificata.</p>
      <table style="width:100%;border-collapse:collapse;margin:24px 0">
        <tr><td style="padding:8px 0;color:#5C6A79">Check-in</td><td style="padding:8px 0;text-align:right"><strong>{_it_date(booking.get('check_in'))}</strong></td></tr>
        <tr><td style="padding:8px 0;color:#5C6A79">Check-out</td><td style="padding:8px 0;text-align:right"><strong>{_it_date(booking.get('check_out'))}</strong></td></tr>
        <tr><td style="padding:8px 0;color:#5C6A79">Ospiti</td><td style="padding:8px 0;text-align:right">{booking.get('adults')} adulti, {booking.get('children')} bambini</td></tr>
        <tr><td style="padding:8px 0;color:#5C6A79">Totale soggiorno</td><td style="padding:8px 0;text-align:right">€{booking.get('total_price')}</td></tr>
        <tr style="border-top:1px solid #E5E0D8"><td style="padding:12px 0;color:#5C6A79">Pagato</td><td style="padding:12px 0;text-align:right;color:#7A93AC"><strong>€{paid}</strong></td></tr>
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


def email_guest_contact_confirmation_html(msg: dict) -> str:
    """Email di conferma ricezione contatto inviata automaticamente all'ospite."""
    name = msg.get('name', '').split()[0] if msg.get('name') else 'Ospite'
    return f"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html dir="ltr" lang="it">
  <head>
    <meta content="width=device-width" name="viewport" />
    <meta content="text/html; charset=UTF-8" http-equiv="Content-Type" />
  </head>
  <body style="background-color:#ffffff">
    <table border="0" width="100%" cellpadding="0" cellspacing="0" role="presentation" align="center">
      <tbody>
        <tr>
          <td style="background-color:#ffffff" align="center">
            <table align="center" width="100%" border="0" cellpadding="0" cellspacing="0" role="presentation" style="max-width:600px;margin:0 auto;width:100%;color:#000000;background-color:#ffffff;padding:0px;">
              <tbody>
                <tr>
                  <td align="center" style="padding:60px 20px">
                    <table align="center" width="100%" border="0" cellpadding="0" cellspacing="0" role="presentation" style="margin:0 auto;max-width:500px;text-align:center">
                      <tbody>
                        <tr>
                          <td align="center" style="padding:0;padding-bottom:10px">
                            <img alt="Logo Light Blue" src="https://www.lightblueanguillara.com/favicon.ico" width="80" style="display:block;border:0;" />
                          </td>
                        </tr>
                        <tr>
                          <td align="center" style="padding:0;padding-bottom:40px">
                            <h1 style="margin:0;font-size:26px;font-weight:normal;color:#1a4a5e;letter-spacing:4px;text-transform:uppercase">LIGHT BLUE</h1>
                            <p style="margin:8px 0 0 0;font-size:14px;font-style:italic;color:#19a7d7;letter-spacing:1px"><em>Anguillara Sabazia</em></p>
                          </td>
                        </tr>
                        <tr>
                          <td align="left" style="padding:0;padding-bottom:50px;font-size:17px;line-height:1.8;color:#333333;text-align:left">
                            <p style="margin:0;padding:0">
                              Gentile {name},<br /><br />
                              abbiamo ricevuto la tua richiesta e ti ringraziamo per averci contattato.<br /><br />
                              Il nostro team la esaminerà e ti risponderemo al più presto.<br /><br />
                              <em style="color:#5C6A79;font-size:15px">Per favore non rispondere a questa email — la casella non è monitorata. Per urgenze puoi contattarci direttamente tramite il sito.</em>
                            </p>
                          </td>
                        </tr>
                        <tr>
                          <td align="center" style="padding:0">
                            <table border="0" cellpadding="0" cellspacing="0" role="presentation">
                              <tr>
                                <td align="center" style="background-color:#07445a;border-radius:2px">
                                  <a href="https://www.lightblueanguillara.com" style="color:#ffffff;text-decoration:none;display:inline-block;padding:20px 45px;font-family:Helvetica, Arial, sans-serif;font-size:13px;font-weight:bold;letter-spacing:2px;text-transform:uppercase" target="_blank">Vai al sito</a>
                                </td>
                              </tr>
                            </table>
                          </td>
                        </tr>
                        <tr>
                          <td align="center" style="padding:0;padding-top:80px">
                            <p style="margin:0;font-size:11px;color:#999999;letter-spacing:1px;text-transform:uppercase">Light Blue Anguillara Sabazia</p>
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </td>
                </tr>
              </tbody>
            </table>
          </td>
        </tr>
      </tbody>
    </table>
  </body>
</html>"""


def email_admin_contact_notification_html(msg: dict) -> str:
    return f"""
    <div style="font-family:Manrope,Arial,sans-serif;max-width:560px;margin:0 auto;padding:32px;color:#2A333C">
      <h2 style="font-family:'Outfit',sans-serif;font-weight:300">Nuova richiesta dal sito</h2>
      <p><strong>{msg.get('name')}</strong> ({msg.get('email')}) — {msg.get('phone') or 'tel non fornito'}</p>
      <p><em>{msg.get('subject') or 'Senza oggetto'}</em></p>
      <blockquote style="border-left:3px solid #7A93AC;padding-left:12px;color:#5C6A79">{msg.get('message')}</blockquote>
    </div>
    """
