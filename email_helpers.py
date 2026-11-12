import asyncio
import html
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
import resend

from db import RESEND_API_KEY, SENDER_EMAIL

resend.api_key = RESEND_API_KEY


def _esc(data: dict) -> dict:
    """Copia del dizionario con tutte le stringhe "escapate" per l'HTML.

    I campi delle prenotazioni e dei messaggi arrivano da form pubblici: senza escape
    chiunque potrebbe inserire link o markup nelle email inviate dal nostro dominio.
    """
    return {k: html.escape(v, quote=True) if isinstance(v, str) else v for k, v in (data or {}).items()}


def _eur(value) -> str:
    """Importo in formato italiano: 1234.5 -> '€ 1.234,50'."""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return f"€ {value}"
    return "€ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


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
            'Nessun rimborso in caso di cancellazione. '
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


async def send_email_async(to_email: str, subject: str, html_body: str, headers: Optional[dict] = None) -> bool:
    try:
        params = {'from': SENDER_EMAIL, 'to': [to_email], 'subject': subject, 'html': html_body}
        if headers:
            params['headers'] = headers
        await asyncio.to_thread(resend.Emails.send, params)
        return True
    except Exception as e:
        logging.warning(f"Email send failed to {to_email}: {e}")
        return False


def email_booking_confirmation_html(booking: dict, settings: dict) -> str:
    booking, settings = _esc(booking), _esc(settings)
    villa = settings.get('villa_name', 'Light Blue')

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
        <tr style="border-top:1px solid #E5E0D8"><td style="padding:12px 0;color:#2A333C;font-weight:bold">Totale complessivo</td><td style="padding:12px 0;text-align:right;color:#2A333C;font-size:16px"><strong>{_eur(booking.get('total_price'))}</strong></td></tr>
        {cancellation_block}
      </table>
      <p>A presto,<br/>{villa}</p>
      <p style="color:#5C6A79;font-size:12px;margin-top:32px">{settings.get('villa_address','')}<br/>CIN {settings.get('villa_cir','')}</p>
    </div>
    """


def email_modification_confirmation_html(booking: dict, settings: dict) -> str:
    """Email di modifica prenotazione — simile alla conferma."""
    booking, settings = _esc(booking), _esc(settings)
    villa = settings.get('villa_name', 'Light Blue')

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
        <tr style="border-top:1px solid #E5E0D8"><td style="padding:12px 0;color:#2A333C;font-weight:bold">Totale complessivo</td><td style="padding:12px 0;text-align:right;color:#2A333C;font-size:16px"><strong>{_eur(booking.get('total_price'))}</strong></td></tr>
        {cancellation_block}
      </table>
      <p>A presto,<br/>{villa}</p>
      <p style="color:#5C6A79;font-size:12px;margin-top:32px">{settings.get('villa_address','')}<br/>CIN {settings.get('villa_cir','')}</p>
    </div>
    """


def email_balance_reminder_html(booking: dict, settings: dict) -> str:
    booking, settings = _esc(booking), _esc(settings)
    villa = settings.get('villa_name', 'Light Blue')
    balance = round(booking.get('total_price', 0) - booking.get('deposit_amount', 0), 2)
    return f"""
    <div style="font-family:Manrope,Arial,sans-serif;max-width:560px;margin:0 auto;padding:32px;background:#FAF9F6;color:#2A333C">
      <h1 style="font-family:'Outfit',sans-serif;font-weight:300;font-size:28px">Promemoria saldo</h1>
      <p>Ciao {booking.get('guest_name')},</p>
      <p>ti ricordiamo che per il tuo soggiorno a <strong>{villa}</strong> dal {_it_date(booking.get('check_in'))} al {_it_date(booking.get('check_out'))} è previsto un saldo residuo di <strong style="color:#7A93AC">{_eur(balance)}</strong>.</p>
      <p>Ti chiediamo gentilmente di completare il pagamento. Per qualsiasi domanda rispondi a questa email.</p>
      <p>Grazie,<br/>{villa}</p>
    </div>
    """


def email_cancellation_html(booking: dict, settings: dict) -> str:
    """Email di cancellazione prenotazione — stessa grafica della conferma."""
    booking, settings = _esc(booking), _esc(settings)
    villa = settings.get('villa_name', 'Light Blue')
    total_paid = 0.0
    if booking.get('payment_status') == 'deposit_paid':
        total_paid = booking.get('deposit_amount', 0)
    elif booking.get('payment_status') == 'fully_paid':
        total_paid = booking.get('total_price', 0)

    refund_row = ''
    if total_paid and total_paid > 0:
        refund_row = """
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
        <tr><td style="padding:8px 0;color:#5C6A79">Totale soggiorno</td><td style="padding:8px 0;text-align:right">{_eur(booking.get('total_price'))}</td></tr>
        {refund_row}
      </table>
      <p style="color:#5C6A79;font-size:14px">
        Per qualsiasi domanda o chiarimento non esitare a contattarci.
      </p>
      <p>A presto,<br/>{villa}</p>
      <p style="color:#5C6A79;font-size:12px;margin-top:32px">{settings.get('villa_address','')}<br/>CIN {settings.get('villa_cir','')}</p>
    </div>
    """


def email_guest_contact_confirmation_html(msg: dict) -> str:
    """Email di conferma ricezione contatto inviata automaticamente all'ospite."""
    raw_name = (msg.get('name') or '').split()
    name = html.escape(raw_name[0][:40]) if raw_name else 'Ospite'
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
    msg = _esc(msg)
    return f"""
    <div style="font-family:Manrope,Arial,sans-serif;max-width:560px;margin:0 auto;padding:32px;color:#2A333C">
      <h2 style="font-family:'Outfit',sans-serif;font-weight:300">Nuova richiesta dal sito</h2>
      <p><strong>{msg.get('name')}</strong> ({msg.get('email')}) — {msg.get('phone') or 'tel non fornito'}</p>
      <p><em>{msg.get('subject') or 'Senza oggetto'}</em></p>
      <blockquote style="border-left:3px solid #7A93AC;padding-left:12px;color:#5C6A79">{msg.get('message')}</blockquote>
    </div>
    """


# =============================================================================
# Prenotazione su richiesta
# =============================================================================

def _it_datetime(iso: str) -> str:
    """ISO datetime (UTC) -> 'GG-MM-AAAA alle HH:MM' ora italiana."""
    try:
        from zoneinfo import ZoneInfo
        dt = datetime.fromisoformat(iso).astimezone(ZoneInfo('Europe/Rome'))
        return dt.strftime('%d-%m-%Y alle %H:%M')
    except Exception:
        return iso or ''


def _layout(title: str, body: str, settings: dict) -> str:
    """Grafica comune delle email (stessa della conferma di prenotazione)."""
    villa = settings.get('villa_name', 'Light Blue')
    return f"""
    <div style="font-family:Manrope,Arial,sans-serif;max-width:560px;margin:0 auto;padding:32px;background:#FAF9F6;color:#2A333C">
      <h1 style="font-family:'Outfit',sans-serif;font-weight:300;font-size:28px;letter-spacing:-0.5px">{title}</h1>
      {body}
      <p>A presto,<br/>{villa}</p>
      <p style="color:#5C6A79;font-size:12px;margin-top:32px">{settings.get('villa_address','')}<br/>CIN {settings.get('villa_cir','')}</p>
    </div>
    """


def _stay_table(b: dict) -> str:
    return f"""
      <table style="width:100%;border-collapse:collapse;margin:24px 0">
        <tr><td style="padding:8px 0;color:#5C6A79">Check-in</td><td style="padding:8px 0;text-align:right"><strong>{_it_date(b.get('check_in'))}</strong></td></tr>
        <tr><td style="padding:8px 0;color:#5C6A79">Check-out</td><td style="padding:8px 0;text-align:right"><strong>{_it_date(b.get('check_out'))}</strong></td></tr>
        <tr><td style="padding:8px 0;color:#5C6A79">Ospiti</td><td style="padding:8px 0;text-align:right">{b.get('adults')} adulti, {b.get('children')} bambini</td></tr>
        <tr><td style="padding:8px 0;color:#5C6A79">Tariffa</td><td style="padding:8px 0;text-align:right">{'Rimborsabile' if b.get('is_refundable', True) else 'Non rimborsabile'}</td></tr>
        <tr style="border-top:1px solid #E5E0D8"><td style="padding:12px 0;font-weight:bold">Totale</td><td style="padding:12px 0;text-align:right;font-size:16px"><strong>{_eur(b.get('total_price'))}</strong></td></tr>
      </table>"""


def email_request_received_html(booking: dict, settings: dict) -> str:
    b, s = _esc(booking), _esc(settings)
    body = f"""
      <p>Ciao {b.get('guest_name')},</p>
      <p>abbiamo ricevuto la tua <strong>richiesta di prenotazione</strong>. Non è ancora una prenotazione confermata
      e <strong>non ti è stato addebitato nulla</strong>.</p>
      {_stay_table(b)}
      <p>Ti risponderemo entro il <strong>{_it_datetime(booking.get('request_expires_at'))}</strong>: nel frattempo le date restano riservate per te.
      Se la richiesta viene accettata riceverai un link per completare il pagamento e confermare il soggiorno.</p>"""
    return _layout('Richiesta ricevuta', body, s)


def email_request_admin_html(booking: dict, settings: dict, dashboard_url: str) -> str:
    b, s = _esc(booking), _esc(settings)
    body = f"""
      <p>È arrivata una nuova richiesta di prenotazione dal sito.</p>
      <p><strong>{b.get('guest_name')}</strong><br/>{b.get('guest_email')} · {b.get('guest_phone') or ''}
      {' · contattabile su WhatsApp' if booking.get('contact_whatsapp') else ''}</p>
      {_stay_table(b)}
      {f'<p style="background:#fff8e6;padding:12px;border-left:3px solid #e0b040"><em>{b.get("notes")}</em></p>' if b.get('notes') else ''}
      <p>Rispondi entro il <strong>{_it_datetime(booking.get('request_expires_at'))}</strong>, poi la richiesta scade e le date si liberano.</p>
      <p><a href="{dashboard_url}" style="display:inline-block;background:#7A93AC;color:#fff;padding:12px 24px;text-decoration:none;border-radius:2px">Apri il pannello per accettare o rifiutare</a></p>"""
    return _layout('Nuova richiesta di prenotazione', body, s)


def email_request_approved_html(booking: dict, settings: dict) -> str:
    b, s = _esc(booking), _esc(settings)
    url = html.escape(booking.get('payment_url') or '', quote=True)
    body = f"""
      <p>Ciao {b.get('guest_name')},</p>
      <p>buone notizie: la tua richiesta è stata <strong>accettata</strong>!</p>
      {_stay_table(b)}
      <p>Per confermare la prenotazione completa il pagamento entro il <strong>{_it_datetime(booking.get('payment_due_at'))}</strong>.
      Dopo questa scadenza le date torneranno disponibili.</p>
      <p style="text-align:center;margin:28px 0"><a href="{url}" style="display:inline-block;background:#2A333C;color:#fff;padding:14px 32px;text-decoration:none;border-radius:2px;font-weight:bold;letter-spacing:1px">PAGA E CONFERMA</a></p>
      <p style="font-size:13px;color:#5C6A79">Pagamento sicuro tramite Stripe. Riceverai la conferma definitiva via email a pagamento completato.</p>"""
    return _layout('Richiesta accettata', body, s)


def email_request_rejected_html(booking: dict, settings: dict) -> str:
    b, s = _esc(booking), _esc(settings)
    reason = b.get('rejection_reason')
    body = f"""
      <p>Ciao {b.get('guest_name')},</p>
      <p>ci dispiace, ma non possiamo accettare la tua richiesta di prenotazione dal {_it_date(b.get('check_in'))} al {_it_date(b.get('check_out'))}.</p>
      {f'<p style="background:#f4f1ec;padding:12px"><em>{reason}</em></p>' if reason else ''}
      <p>Non ti è stato addebitato nulla. Se vuoi, puoi provare altre date sul nostro sito.</p>"""
    return _layout('Richiesta non accettata', body, s)


def email_request_expired_html(booking: dict, settings: dict, unpaid: bool) -> str:
    b, s = _esc(booking), _esc(settings)
    if unpaid:
        text = ('il tempo per completare il pagamento è scaduto e la prenotazione non è stata confermata. '
                'Non ti è stato addebitato nulla. Se sei ancora interessato puoi inviare una nuova richiesta dal sito.')
    else:
        text = ('purtroppo non siamo riusciti a rispondere in tempo alla tua richiesta, che è scaduta. '
                'Non ti è stato addebitato nulla: ci scusiamo e ti invitiamo a contattarci o a inviare una nuova richiesta.')
    body = f"""
      <p>Ciao {b.get('guest_name')},</p>
      <p>riguardo al soggiorno dal {_it_date(b.get('check_in'))} al {_it_date(b.get('check_out'))}: {text}</p>"""
    return _layout('Richiesta scaduta', body, s)


def whatsapp_text(kind: str, booking: dict, settings: dict) -> str:
    """Testo pronto da inviare su WhatsApp (testo semplice, niente HTML)."""
    villa = settings.get('villa_name', 'Light Blue')
    first = (booking.get('guest_name') or '').split(' ')[0] or 'Ciao'
    dates = f"dal {_it_date(booking.get('check_in'))} al {_it_date(booking.get('check_out'))}"
    if kind == 'approved':
        return (f"Ciao {first}! La tua richiesta di soggiorno a {villa} {dates} è stata accettata 🎉\n"
                f"Per confermare completa il pagamento entro il {_it_datetime(booking.get('payment_due_at'))}:\n"
                f"{booking.get('payment_url')}\n"
                f"Trovi lo stesso link anche nella tua email. A presto!")
    if kind == 'rejected':
        reason = booking.get('rejection_reason')
        return (f"Ciao {first}, grazie per la richiesta di soggiorno a {villa} {dates}. "
                f"Purtroppo non possiamo accettarla" + (f": {reason}" if reason else ".") +
                " Non ti è stato addebitato nulla. Speriamo di ospitarti in un'altra occasione!")
    return (f"Ciao {first}, ti scrivo da {villa} riguardo alla tua richiesta di soggiorno {dates}.")
