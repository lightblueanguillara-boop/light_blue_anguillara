"""Email e WhatsApp: modelli, registro, link corti, coda WhatsApp, promemoria automatici, webhook di Resend."""
import base64
import hashlib
import hmac
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import messaging
import short_links
from test_api import checkout_payload, d, wait_for
from test_booking_requests import request_payload

ROME = ZoneInfo('Europe/Rome')
LONG_URL = 'https://checkout.stripe.com/c/pay/cs_live_a1B2c3D4e5F6g7H8i9J0kLmNoPqRsTuVwXyZ#fidkdWxOYHwnPyd1blpxYHZxWjA0'


def _run(client, fn, *a, **kw):
    return client.portal.call(lambda: fn(*a, **kw))


def _expand(client, text):
    return client.portal.call(short_links.expand, text)


def _manual(client, auth, ci=20, co=23, **kw):
    body = {'guest_name': 'Anna Bianchi', 'guest_email': 'anna@example.com', 'guest_phone': '347 1112233',
            'check_in': d(ci), 'check_out': d(co), 'total_price': 360, 'adults': 2, 'children': 1, **kw}
    r = client.post('/api/admin/bookings/manual', headers=auth, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _guest_mails(sent_emails, to='anna@example.com'):
    return [m for m in sent_emails if m['to'] == [to]]


# ------------------------------------------------------------------ 2. render
@pytest.mark.parametrize('template,vars,expected', [
    ('Wi-Fi: {wifi}\nCiao', {}, 'Ciao'),                                         # riga «Etichetta: {var}» vuota
    ('Check-in: dalle {ora_checkin}\nOk', {}, 'Ok'),                            # niente frasi a metà
    ('🕘 Check-in dalle *{ora_checkin}*\nOk', {}, 'Ok'),                         # con emoji e grassetto
    ('_{firma}_', {}, ''),                                                       # dentro il corsivo
    ('{link_mappa}\nFine', {}, 'Fine'),                                          # riga fatta solo di variabile
    ('Dal {data_arrivo} al {data_partenza}', {'data_arrivo': 'ieri'}, 'Dal ieri al'),   # una sola vuota: resta
    ('Ciao {nome},\n\n\n\nA presto', {'nome': 'Giulia'}, 'Ciao Giulia,\n\nA presto'),
])
def test_render(template, vars, expected):
    assert messaging.render(template, vars) == expected


def test_all_seed_templates_use_only_documented_variables():
    import message_templates as T
    for t in T.SEED:
        assert messaging.unknown_vars(t['key'], t.get('subject') or '', t['body']) == [], t['key']


# ------------------------------------------------------------------ modelli: seed e pannello
def test_templates_seeded_and_listed(client, auth):
    items = client.get('/api/admin/messages/templates', headers=auth).json()
    assert len(items) == 24 and len({i['key'] for i in items}) == 16   # 15 email + 9 WhatsApp
    wa = next(i for i in items if i['key'] == 'istruzioni_arrivo' and i['channel'] == 'whatsapp')
    assert wa['active'] and not wa['modified'] and 'wifi' in wa['variables'] and wa['when']


def test_seed_never_overwrites_owner_changes(client, auth, mongo):
    key = '/api/admin/messages/templates/richiesta_ricevuta/email'
    r = client.put(key, headers=auth, json={'body': 'Ciao {nome}, testo mio'})
    assert r.status_code == 200 and r.json()['modified'] is True
    _run(client, messaging.seed)
    assert mongo.message_templates.find_one({'key': 'richiesta_ricevuta', 'channel': 'email'})['body'] == 'Ciao {nome}, testo mio'
    r = client.post(key + '/reset', headers=auth).json()
    assert r['modified'] is False and r['body'].startswith('Ciao {nome},\n\ngrazie per aver scelto')


def test_template_validation(client, auth):
    base = '/api/admin/messages/templates/richiesta_ricevuta/email'
    r = client.put(base, headers=auth, json={'body': 'Ciao {nome}, il codice è {codice_segreto}'})
    assert r.status_code == 400 and '{codice_segreto}' in r.json()['detail']
    assert client.put(base, headers=auth, json={'subject': '   '}).status_code == 400
    assert client.put('/api/admin/messages/templates/inesistente/email', headers=auth, json={'body': 'x'}).status_code == 404


def test_template_switched_off_sends_nothing(client, auth, sent_emails, mongo):
    client.put('/api/admin/messages/templates/prenotazione_confermata/email', headers=auth, json={'active': False})
    j = _manual(client, auth)
    time.sleep(0.3)
    assert j['email_sent'] is False and _guest_mails(sent_emails) == []
    assert mongo.whatsapp_queue.count_documents({'key': 'prenotazione_confermata'}) == 1   # il WhatsApp resta acceso


def test_preview_and_test_send(client, auth, sent_emails, mongo):
    body = 'Ciao {nome},\n\nWi-Fi: {wifi}\nTotale: {totale}\n\n{firma}'
    r = client.post('/api/admin/messages/preview', headers=auth,
                    json={'key': 'istruzioni_arrivo', 'channel': 'email', 'subject': 'Per {nome}', 'body': body}).json()
    assert r['subject'] == 'Per Giulia' and 'Giulia' in r['html'] and '€ 360,00' in r['text'] and 'Wi-Fi' not in r['text']
    wa = client.post('/api/admin/messages/preview', headers=auth,
                     json={'key': 'richiesta_accettata', 'channel': 'whatsapp', 'body': '👉 {link_pagamento}'}).json()
    assert re.fullmatch(r'👉 https://www\.example-site\.test/l/[A-Za-z0-9]{10}', wa['text'])
    t = client.post('/api/admin/messages/test', headers=auth,
                    json={'key': 'prenotazione_confermata', 'channel': 'email', 'subject': 'Ok {nome}', 'body': body}).json()
    assert t['ok'] and t['to'] == 'notify@example.com'
    mail = sent_emails[-1]
    assert mail['subject'] == '[Prova] Ok Giulia' and mail['attachments'][0]['filename'] == 'prenotazione.ics'
    # prova WhatsApp: serve il numero del proprietario
    r = client.post('/api/admin/messages/test', headers=auth, json={'key': 'richiesta_accettata', 'channel': 'whatsapp', 'body': 'x'})
    assert r.status_code == 400
    client.put('/api/admin/settings', headers=auth, json={'owner_whatsapp': '+39 333 0000000'})
    r = client.post('/api/admin/messages/test', headers=auth, json={'key': 'richiesta_accettata', 'channel': 'whatsapp', 'body': 'Prova'}).json()
    assert r['link'] == 'https://api.whatsapp.com/send?phone=393330000000&text=Prova'


# ------------------------------------------------------------------ 1. email e registro
def test_email_without_resend_key_is_recorded_and_booking_works(client, auth, mongo, monkeypatch, sent_emails):
    import email_helpers
    monkeypatch.setattr(email_helpers, 'RESEND_API_KEY', '')
    j = _manual(client, auth)
    assert j['ok'] and mongo.bookings.find_one({'id': j['booking']['id']})['status'] == 'confirmed'
    rec = mongo.email_outbox.find_one({'key': 'prenotazione_confermata'})
    assert rec['status'] == 'non_inviata' and rec['reason'] == 'non_attivo' and rec['attachments'] == ['prenotazione.ics']
    assert sent_emails == []
    s = client.get('/api/admin/messages/status', headers=auth).json()
    assert s['active'] is False


def test_confirmation_email_content(client, auth, mongo, sent_emails):
    client.put('/api/admin/settings', headers=auth, json={'checkin_time': '15:00', 'checkout_time': '10:00',
                                                         'villa_email': 'info@casa-vera.it'})
    j = _manual(client, auth, guest_name='<b>Anna</b> Bianchi')
    mail = _guest_mails(sent_emails)[0]
    assert mail['subject'] == 'Prenotazione confermata, ti aspettiamo!'
    assert mail['reply_to'] == 'info@casa-vera.it'                  # le risposte arrivano al proprietario
    assert '<b>Anna</b>' not in mail['html'] and '&lt;b&gt;Anna&lt;/b&gt;' in mail['html']   # niente HTML dall'ospite
    assert 'Check-in: dalle 15:00' in mail['text'] and 'Wi-Fi' not in mail['text']          # righe vuote tolte
    assert 'Aggiungi a Google Calendar' in mail['html'] and 'Light Blue Anguillara' in mail['text']
    assert all(len(u) <= 60 for u in re.findall(r'https?://\S+', mail['text']))          # solo link corti
    ics = base64.b64decode(mail['attachments'][0]['content']).decode()
    assert f"UID:{j['booking']['id']}@www.example-site.test" in ics and 'METHOD:PUBLISH' in ics
    assert re.search(r'DTSTART:\d{8}T1[34]0000Z', ics)                                 # 15:00 ora italiana in UTC
    rec = mongo.email_outbox.find_one({'key': 'prenotazione_confermata'})
    assert rec['status'] == 'inviata' and rec['booking_id'] == j['booking']['id'] and rec['provider_id'] == 'x'


def test_calendar_event_update_and_cancel(client, auth, mongo, sent_emails):
    j = _manual(client, auth)
    bid = j['booking']['id']
    client.patch(f'/api/admin/bookings/{bid}', headers=auth, json={'check_out': d(24)})
    assert wait_for(lambda: len(_guest_mails(sent_emails)) == 2)
    upd = base64.b64decode(_guest_mails(sent_emails)[1]['attachments'][0]['content']).decode()
    assert 'SEQUENCE:1' in upd and f"DTEND;VALUE=DATE:{d(24).replace('-', '')}" in upd           # senza orari: giornata intera
    assert client.delete(f'/api/admin/bookings/{bid}', headers=auth).status_code == 200
    cancel = _guest_mails(sent_emails)[-1]
    ics = base64.b64decode(cancel['attachments'][0]['content']).decode()
    assert 'METHOD:CANCEL' in ics and 'STATUS:CANCELLED' in ics and 'rimborso' not in cancel['text'].lower()


@pytest.mark.parametrize('env,reason', [({'EMAIL_SOLO_A': 'mio@example.com'}, 'solo_prove'),
                                        ({'EMAIL_LIMITE_GIORNO': '1'}, 'limite')])
def test_test_mode_limits(client, auth, mongo, monkeypatch, env, reason):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _manual(client, auth)
    _manual(client, auth, ci=40, co=42, guest_email='anna+due@example.com')
    recs = list(mongo.email_outbox.find({'key': 'prenotazione_confermata'}).sort('created_at', 1))
    assert recs[-1]['reason'] == reason


def test_only_address_ignores_plus_tag(client, auth, mongo, monkeypatch):
    monkeypatch.setenv('EMAIL_SOLO_A', 'anna@example.com')
    _manual(client, auth, guest_email='anna+prova@example.com')
    assert mongo.email_outbox.find_one({'key': 'prenotazione_confermata'})['status'] == 'inviata'


# ------------------------------------------------------------------ 3. WhatsApp
def test_whatsapp_queue_no_duplicates_and_superseded(client, auth, mongo):
    j = _manual(client, auth)
    bid = j['booking']['id']
    item = mongo.whatsapp_queue.find_one({'id': j['whatsapp_id']})
    assert item['phone'] == '393471112233' and item['status'] == 'da_inviare' and item['for_date'] == d(20)
    assert 'Ciao Anna 🌊' in item['text'] and 'Light Blue Anguillara' in item['text']
    # stesso messaggio di nuovo: aggiorna, non duplica
    b = mongo.bookings.find_one({'id': bid}, {'_id': 0})
    _run(client, messaging.invia, 'prenotazione_confermata', b)
    assert mongo.whatsapp_queue.count_documents({'booking_id': bid, 'key': 'prenotazione_confermata'}) == 1
    # spostamento: la conferma vecchia non ha più senso, arriva la modifica
    client.patch(f'/api/admin/bookings/{bid}', headers=auth, json={'check_in': d(21)})
    assert mongo.whatsapp_queue.find_one({'id': j['whatsapp_id']})['status'] == 'annullato'
    assert mongo.whatsapp_queue.find_one({'booking_id': bid, 'key': 'prenotazione_modificata', 'status': 'da_inviare'})
    # cancellazione: tutto il resto tolto, resta solo l'avviso di cancellazione
    client.delete(f'/api/admin/bookings/{bid}', headers=auth)
    pending = list(mongo.whatsapp_queue.find({'booking_id': bid, 'status': 'da_inviare'}))
    assert [p['key'] for p in pending] == ['prenotazione_cancellata']


def test_whatsapp_needs_valid_phone_and_consent(client, auth, mongo, fake_stripe, sent_emails):
    _manual(client, auth, guest_phone='12')                                  # numero non valido
    assert mongo.whatsapp_queue.count_documents({}) == 0
    # richiesta dal sito SENZA consenso WhatsApp: accettandola parte l'email ma niente coda
    bid = client.post('/api/bookings/request', json=request_payload(60, 62, contact_whatsapp=False)).json()['booking_id']
    r = client.post(f'/api/admin/bookings/{bid}/approve', headers=auth).json()
    assert r['email_sent'] is True and r['whatsapp_id'] is None
    assert mongo.whatsapp_queue.count_documents({}) == 0
    # il pulsante WhatsApp nella scheda resta disponibile, con l'avviso del consenso mancante
    link = client.post('/api/admin/whatsapp/link', headers=auth, json={'booking_id': bid}).json()
    assert link['consent'] is False and link['key'] == 'richiesta_accettata'


@pytest.mark.parametrize('raw,expected', [('333 123 4567', '393331234567'), ('+39 333 1234567', '393331234567'),
                                          ('0039 06 1234567', '39061234567'), ('06 1234567', '39061234567'),
                                          ('+44 7700 900123', '447700900123'), ('12', ''), ('', '')])
def test_normalize_phone(raw, expected):
    from whatsapp_queue import normalize_phone
    assert normalize_phone(raw) == expected


def test_whatsapp_links_never_use_wa_me():
    from whatsapp_queue import whatsapp_link, sms_link
    assert whatsapp_link('393331234567', 'Ciao 🌊') == 'https://api.whatsapp.com/send?phone=393331234567&text=Ciao%20%F0%9F%8C%8A'
    assert whatsapp_link('39', 'x', 'app').startswith('whatsapp://send?') and whatsapp_link('39', 'x', 'web').startswith('https://web.whatsapp.com/')
    assert sms_link('393331234567', 'a b') == 'sms:+393331234567?&body=a%20b'


def test_queue_page_actions(client, auth, mongo, sent_emails):
    j = _manual(client, auth)
    wid = j['whatsapp_id']
    items = client.get('/api/admin/whatsapp/queue', headers=auth).json()
    assert len(items) == 1 and items[0]['email']['status'] == 'inviata' and items[0]['booking']['check_in'] == d(20)
    assert items[0]['link'].startswith('https://api.whatsapp.com/send?phone=393471112233')
    assert client.get('/api/admin/whatsapp/count', headers=auth).json() == {'pending': 1}
    edited = client.patch(f'/api/admin/whatsapp/{wid}', headers=auth, json={'text': f'Testo mio {LONG_URL}'}).json()
    assert edited['text'].startswith('Testo mio https://www.example-site.test/l/')          # anche qui link corto
    client.put('/api/admin/settings', headers=auth, json={'whatsapp_open_with': 'app'})
    assert client.get('/api/admin/whatsapp/queue', headers=auth).json()[0]['link'].startswith('whatsapp://')
    assert client.post(f'/api/admin/whatsapp/{wid}/sent', headers=auth).json()['status'] == 'inviato'
    assert client.get('/api/admin/whatsapp/count', headers=auth).json() == {'pending': 0}
    assert client.patch(f'/api/admin/whatsapp/{wid}', headers=auth, json={'text': 'x'}).status_code == 409
    assert client.post(f'/api/admin/whatsapp/{wid}/restore', headers=auth).json()['status'] == 'da_inviare'
    assert client.post(f'/api/admin/whatsapp/{wid}/remove', headers=auth).json()['status'] == 'annullato'
    done = client.get('/api/admin/whatsapp/queue', headers=auth, params={'status': 'done'}).json()
    assert [x['id'] for x in done] == [wid]


def test_whatsapp_button_in_booking(client, auth, mongo):
    j = _manual(client, auth)
    bid = j['booking']['id']
    r = client.post('/api/admin/whatsapp/link', headers=auth, json={'booking_id': bid}).json()
    assert r['key'] == 'istruzioni_arrivo' and r['consent'] is True and 'manca poco' in r['text']
    r = client.post('/api/admin/whatsapp/link', headers=auth, json={'booking_id': bid, 'text': 'Ciao!'}).json()
    assert r['text'] == 'Ciao!' and r['link'].endswith('text=Ciao%21')
    assert mongo.whatsapp_queue.count_documents({'booking_id': bid}) == 1                # non mette nulla in coda


# ------------------------------------------------------------------ 4. link corti
def test_short_links(client, mongo):
    short = client.portal.call(short_links.shorten, LONG_URL)
    assert re.fullmatch(r'https://www\.example-site\.test/l/[A-Za-z0-9]{10}', short)
    assert client.portal.call(short_links.shorten, LONG_URL) == short                  # stesso link = stesso codice
    code = short.rsplit('/', 1)[1]
    r = client.get(f'/api/l/{code}')
    assert r.json() == {'destination': LONG_URL} and r.headers['cache-control'] == 'no-store'
    assert client.get('/api/l/AAAAAAAAAA').status_code == 404
    assert client.get('/api/l/../../x').status_code == 404
    assert client.portal.call(short_links.shorten, 'https://www.example-site.test/book') == 'https://www.example-site.test/book'
    img = 'https://res.cloudinary.com/demo/image/upload/v1/una-foto-molto-lunga-della-casa-vacanze.jpg'
    assert client.portal.call(short_links.shorten, img) == img                       # mai le immagini
    text = client.portal.call(short_links.shorten_text, f'Paga qui: {LONG_URL}. Grazie')
    assert LONG_URL not in text and text.endswith('. Grazie')


def test_token_links_expire(client, mongo):
    url = 'https://www.example-site.test/disiscrizione?token=' + 'x' * 60
    code = client.portal.call(short_links.shorten, url).rsplit('/', 1)[1]
    assert client.get(f'/api/l/{code}').status_code == 200
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    mongo.short_links.update_one({'code': code}, {'$set': {'expires_at': past}})
    assert client.get(f'/api/l/{code}').status_code == 404


def test_no_long_links_in_whatsapp(client, auth, mongo):
    client.put('/api/admin/settings', headers=auth, json={'villa_address': 'Via del Lago 12, 00061 Anguillara Sabazia RM'})
    j = _manual(client, auth, ci=1, co=4)
    b = mongo.bookings.find_one({'id': j['booking']['id']}, {'_id': 0})
    _run(client, messaging.invia, 'istruzioni_arrivo', b)
    text = mongo.whatsapp_queue.find_one({'key': 'istruzioni_arrivo'})['text']
    links = re.findall(r'https?://\S+', text)
    assert links and all(len(u) <= 60 for u in links)
    assert 'google.com/maps' in _expand(client, text)


# ------------------------------------------------------------------ 5. promemoria automatici
def _at(hour, minute=5, day=0):
    return (datetime.now(ROME).replace(hour=hour, minute=minute, second=0, microsecond=0)
            + timedelta(days=day)).astimezone(timezone.utc)


def _reminders(client, now):
    import guest_reminders
    return client.portal.call(guest_reminders.run, now)


def test_arrival_instructions_once(client, auth, mongo, sent_emails):
    j = _manual(client, auth, ci=2, co=5)
    assert _reminders(client, _at(9)).get('istruzioni_arrivo') is None             # prima delle 10: niente
    assert _reminders(client, _at(10))['istruzioni_arrivo'] == 1
    assert _reminders(client, _at(11)).get('istruzioni_arrivo') == 0               # già inviate
    mails = [m for m in _guest_mails(sent_emails) if m['subject'] == 'Tutto pronto per il tuo arrivo']
    assert len(mails) == 1
    assert mongo.whatsapp_queue.count_documents({'key': 'istruzioni_arrivo', 'booking_id': j['booking']['id']}) == 1


def test_reminders_parallel_runs_send_once(client, auth, mongo, sent_emails):
    for i in range(3):
        _manual(client, auth, ci=1 + i * 3, co=2 + i * 3, guest_email=f'ospite{i}@example.com')
    now = _at(10, 30)
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda _: _reminders(client, now), range(4)))
    sent = [m for m in sent_emails if m['subject'] == 'Tutto pronto per il tuo arrivo']
    assert sorted(m['to'][0] for m in sent) == ['ospite0@example.com']        # solo chi arriva entro 2 giorni, una volta


def test_reminder_restarts_after_date_change(client, auth, mongo, sent_emails):
    j = _manual(client, auth, ci=2, co=5)
    bid = j['booking']['id']
    _reminders(client, _at(10))
    client.patch(f'/api/admin/bookings/{bid}', headers=auth, json={'check_in': d(1)})
    assert 'reminders' not in mongo.bookings.find_one({'id': bid})
    assert _reminders(client, _at(10, 10))['istruzioni_arrivo'] == 1


def test_reminders_respect_settings(client, auth, mongo, sent_emails):
    _manual(client, auth, ci=2, co=5)
    client.put('/api/admin/settings', headers=auth, json={'reminders': {'istruzioni_arrivo': {'enabled': False, 'days': 2, 'time': '10:00'}}})
    assert 'istruzioni_arrivo' not in _reminders(client, _at(12))
    s = client.get('/api/admin/settings', headers=auth).json()['reminders']
    assert s['promemoria_checkout']['time'] == '18:00'                             # gli altri restano com'erano
    bad = client.put('/api/admin/settings', headers=auth, json={'reminders': {'istruzioni_arrivo': {'time': '25:00', 'days': 2}}})
    assert bad.status_code == 422


def test_balance_checkout_and_review(client, auth, mongo, sent_emails):
    dep = _manual(client, auth, ci=5, co=8, payment_status='deposit_paid', deposit_amount=100,
                  guest_email='saldo@example.com')
    full = _manual(client, auth, ci=10, co=12, payment_status='fully_paid', guest_email='pagato@example.com')
    out = _manual(client, auth, ci=-1, co=1, guest_email='parte@example.com')
    gone = _manual(client, auth, ci=-6, co=-2, guest_email='partito@example.com')
    done = _reminders(client, _at(19))
    assert done['promemoria_saldo'] == 1 and done['promemoria_checkout'] == 1 and done['grazie_recensione'] == 1
    saldo = next(m for m in sent_emails if m['subject'] == 'Promemoria del saldo')
    assert saldo['to'] == ['saldo@example.com'] and '€ 260,00' in saldo['text']
    # check-out: solo WhatsApp (nessuna email)
    assert mongo.whatsapp_queue.find_one({'key': 'promemoria_checkout'})['booking_id'] == out['booking']['id']
    assert not any('check-out' in m['subject'].lower() for m in sent_emails)
    assert next(m for m in sent_emails if m['subject'] == 'Grazie per il tuo soggiorno')['to'] == ['partito@example.com']
    assert full and gone


def test_evening_whatsapp_notice_once_a_day(client, auth, mongo, pushes):
    _manual(client, auth, ci=30, co=33)
    _reminders(client, _at(18))
    assert not any(p['kind'] == 'whatsapp_pending' for p in pushes)
    _reminders(client, _at(19, 1))
    _reminders(client, _at(20))
    sent = [p for p in pushes if p['kind'] == 'whatsapp_pending']
    assert len(sent) == 1 and sent[0]['url'] == '/admin/whatsapp' and '1 messaggio' in sent[0]['body']


def test_external_bookings_get_no_messages(client, auth, mongo, sent_emails):
    mongo.bookings.insert_one({'id': 'ext-airbnb-x', 'status': 'external', 'source': 'airbnb', 'guest_name': 'Ospite Airbnb',
                               'guest_email': 'sync-airbnb-0@lightblue-anguillara.it', 'check_in': d(1), 'check_out': d(3)})
    _reminders(client, _at(23))
    assert sent_emails == [] and mongo.whatsapp_queue.count_documents({}) == 0


# ------------------------------------------------------------------ 6. webhook di Resend
SECRET = 'whsec_' + base64.b64encode(b'chiave-di-prova-del-webhook!!').decode()


def _signed(client, payload: dict, secret=SECRET, ts=None, sig=None):
    body = json.dumps(payload).encode()
    ts = str(int(ts if ts is not None else time.time()))
    key = base64.b64decode(secret.removeprefix('whsec_'))
    good = base64.b64encode(hmac.new(key, f'msg_1.{ts}.'.encode() + body, hashlib.sha256).digest()).decode()
    return client.post('/api/webhook/resend', content=body, headers={
        'svix-id': 'msg_1', 'svix-timestamp': ts, 'svix-signature': sig or f'v1,xxxx v1,{good}'})


def test_resend_webhook(client, auth, mongo, monkeypatch, pushes):
    assert _signed(client, {}).status_code == 503                                  # senza segreto
    monkeypatch.setenv('RESEND_WEBHOOK_SECRET', SECRET)
    j = _manual(client, auth)
    rec = mongo.email_outbox.find_one({'key': 'prenotazione_confermata'})
    ev = lambda t, **data: {'type': t, 'data': {'email_id': rec['provider_id'], **data}}   # noqa: E731
    assert _signed(client, ev('email.delivered'), sig='v1,firmafalsa').status_code == 400
    assert _signed(client, ev('email.delivered'), ts=time.time() - 3600).status_code == 400
    assert _signed(client, ev('email.delivery_delayed')).status_code == 200
    assert _signed(client, ev('email.delivered')).status_code == 200
    assert _signed(client, ev('email.delivery_delayed')).status_code == 200         # non torna indietro
    assert mongo.email_outbox.find_one({'id': rec['id']})['delivery'] == 'consegnata'
    _signed(client, ev('email.bounced', bounce={'type': 'Permanent', 'message': 'non salvare'}))
    after = mongo.email_outbox.find_one({'id': rec['id']})
    assert after['delivery'] == 'tornata_indietro' and after['delivery_detail'] == 'Permanent'
    assert wait_for(lambda: any(p['kind'] == 'email_failed' for p in pushes))
    assert _signed(client, {'type': 'email.opened', 'data': {'email_id': rec['provider_id']}}).json() == {'ignored': True}
    assert j


# ------------------------------------------------------------------ registro e «Rimanda»
def test_booking_email_status_and_resend(client, auth, mongo, sent_emails):
    j = _manual(client, auth)
    bid = j['booking']['id']
    rows = client.get(f'/api/admin/email/booking/{bid}', headers=auth).json()
    assert len(rows) == 1 and rows[0]['key'] == 'prenotazione_confermata' and rows[0]['can_resend'] is True
    assert 'html' not in rows[0]
    r = client.post(f"/api/admin/email/{rows[0]['id']}/resend", headers=auth).json()
    assert r['ok'] and r['email']['resent_from'] == rows[0]['id']
    assert len(_guest_mails(sent_emails)) == 2 and _guest_mails(sent_emails)[1]['attachments'][0]['filename'] == 'prenotazione.ics'
    # la vecchia non si può più rimandare (ce n'è una più recente)
    assert client.post(f"/api/admin/email/{rows[0]['id']}/resend", headers=auth).status_code == 409
    # dopo una modifica dei dati la conferma non è più valida
    newest = client.get(f'/api/admin/email/booking/{bid}', headers=auth).json()[0]
    client.patch(f'/api/admin/bookings/{bid}', headers=auth, json={'payment_status': 'fully_paid'})
    r = client.post(f"/api/admin/email/{newest['id']}/resend", headers=auth)
    assert r.status_code == 409 and 'modificata' in r.json()['detail']
    out = client.get('/api/admin/email/outbox', headers=auth).json()
    assert len(out) == 2 and all('html' not in x for x in out)
    assert client.get(f"/api/admin/email/view/{out[0]['id']}", headers=auth).json()['html'].startswith('<!DOCTYPE html>')


def test_admin_message_endpoints_require_auth(client):
    for method, path in [('get', '/api/admin/messages/templates'), ('get', '/api/admin/whatsapp/queue'),
                         ('get', '/api/admin/email/outbox'), ('post', '/api/admin/whatsapp/link'),
                         ('get', '/api/admin/messages/status')]:
        assert getattr(client, method)(path).status_code in (401, 403), path


# ------------------------------------------------------------------ eventi del sito
def test_request_flow_messages(client, auth, mongo, sent_emails, fake_stripe):
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    assert wait_for(lambda: len(sent_emails) == 2)
    assert mongo.whatsapp_queue.count_documents({}) == 0                          # alla richiesta niente WhatsApp
    r = client.post(f'/api/admin/bookings/{bid}/reject', headers=auth, json={'reason': ''}).json()
    mail = next(m for m in sent_emails if m['subject'] == 'La tua richiesta di soggiorno')
    assert 'Motivo' not in mail['text']                                           # motivo vuoto: riga tolta
    assert mongo.whatsapp_queue.find_one({'id': r['whatsapp_id']})['key'] == 'richiesta_rifiutata'


def test_contact_form_messages(client, sent_emails):
    r = client.post('/api/contact', json={'name': 'Elena Conti', 'email': 'elena@example.com',
                                          'message': 'È libero a dicembre?'})
    assert r.status_code == 200
    assert wait_for(lambda: len(sent_emails) == 2)
    guest = next(m for m in sent_emails if m['to'] == ['elena@example.com'])
    owner = next(m for m in sent_emails if m['to'] == ['notify@example.com'])
    assert guest['subject'] == 'Abbiamo ricevuto il tuo messaggio' and 'È libero a dicembre?' in guest['text']
    assert owner['subject'] == 'Nuovo messaggio da Elena Conti' and owner['reply_to'] == 'elena@example.com'


def test_refund_cancellation_message(client, auth, mongo, monkeypatch, instant, fake_stripe, sent_emails):
    import stripe
    from test_safety import _pay_via_webhook
    monkeypatch.setattr(stripe.Refund, 'create', lambda **kw: type('R', (), {'id': 're_1'})())
    j = client.post('/api/bookings/checkout', json=checkout_payload(guest_phone='3331234567')).json()
    _pay_via_webhook(client, monkeypatch, j['session_id'])
    client.post(f"/api/admin/bookings/{j['booking_id']}/cancel-refund", headers=auth, json={'amount': 200})
    assert wait_for(lambda: any(m['subject'] == 'Prenotazione cancellata' for m in sent_emails))
    mail = next(m for m in sent_emails if m['subject'] == 'Prenotazione cancellata')
    assert 'Ti abbiamo rimborsato € 200,00' in mail['text']
