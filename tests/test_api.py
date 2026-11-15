"""Test dell'API Light Blue (eseguire con: pytest -q)."""
import json
import time
from datetime import date, datetime, timedelta, timezone

import pytest
import stripe

from conftest import ADMIN


def d(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


def wait_for(cond, timeout=3.0):
    """Le email partono in background: attende che la condizione diventi vera."""
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def checkout_payload(ci=30, co=33, **kw):
    p = {
        'guest_name': 'Mario Rossi', 'guest_email': 'mario@example.com',
        'check_in': d(ci), 'check_out': d(co), 'adults': 2, 'children': 0,
        'payment_choice': 'full', 'is_refundable': True, 'accept_terms': True,
        'origin_url': 'http://localhost:3000',
    }
    p.update(kw)
    return p


# ---------------------------------------------------------------- pubbliche
def test_root_and_info(client):
    assert client.get('/api/').json()['status'] == 'ok'
    info = client.get('/api/villa/info').json()
    for k in ('name', 'address', 'email', 'cir', 'legal_owner_name', 'legal_owner_tax_id', 'tourist_tax_note'):
        assert k in info


def test_docs_disabled_by_default(client):
    assert client.get('/docs').status_code == 404
    assert client.get('/openapi.json').status_code == 404


def test_security_headers(client):
    r = client.get('/api/')
    assert r.headers['x-content-type-options'] == 'nosniff'
    assert r.headers['x-frame-options'] == 'DENY'


def test_availability_validates_dates(client):
    assert client.get('/api/availability', params={'start': d(0), 'end': d(30)}).status_code == 200
    assert client.get('/api/availability', params={'start': 'x', 'end': d(30)}).status_code == 400


def test_quote(client):
    r = client.post('/api/quote', params={'check_in': d(60), 'check_out': d(63)})
    assert r.status_code == 200
    j = r.json()
    assert j['nights'] == 3 and j['refundable_total'] == 360.0 and j['non_refundable_total'] == 342.0
    assert j['available'] is True


@pytest.mark.parametrize('ci,co', [
    ('2027-13-01', '2027-13-05'),   # data inesistente
    (d(10), d(10)),                 # zero notti
    (d(10), d(5)),                  # partenza prima dell'arrivo
    (d(-5), d(-2)),                 # nel passato
    (d(10), d(100)),                # oltre il soggiorno massimo
])
def test_quote_rejects_bad_dates(client, ci, co):
    r = client.post('/api/quote', params={'check_in': ci, 'check_out': co})
    assert r.status_code == 400
    assert isinstance(r.json()['detail'], str)


def test_contact_saves_and_escapes_html(client, mongo, sent_emails):
    r = client.post('/api/contact', json={
        'name': '<b>Hacker</b> Rossi', 'email': 'luca@example.com',
        'message': '<a href="http://phish.example">clicca</a>', 'consent_newsletter': True,
    })
    assert r.status_code == 200
    assert mongo.contact_messages.count_documents({}) == 1
    assert mongo.subscribers.count_documents({'email': 'luca@example.com'}) == 1
    assert wait_for(lambda: len(sent_emails) == 2)
    admin_mail = next(m for m in sent_emails if m['to'] == ['notify@example.com'])
    assert '<a href="http://phish.example">' not in admin_mail['html']
    assert '&lt;a href=' in admin_mail['html']
    guest_mail = next(m for m in sent_emails if m['to'] == ['luca@example.com'])
    assert '<b>' not in guest_mail['html']


def test_contact_honeypot_drops_bots(client, mongo, sent_emails):
    r = client.post('/api/contact', json={'name': 'Bot', 'email': 'bot@example.com', 'message': 'spam', 'website': 'x'})
    assert r.status_code == 200
    assert mongo.contact_messages.count_documents({}) == 0
    time.sleep(0.2)
    assert sent_emails == []


def test_contact_rate_limited(client):
    body = {'name': 'Anna', 'email': 'anna@example.com', 'message': 'ciao'}
    codes = [client.post('/api/contact', json={**body, 'email': f'a{i}@example.com'}).status_code for i in range(6)]
    assert codes[:5] == [200] * 5 and codes[5] == 429


def test_contact_limits_confirmations_to_same_address(client):
    body = {'name': 'Anna', 'email': 'vittima@example.com', 'message': 'ciao'}
    codes = [client.post('/api/contact', json=body).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_newsletter_subscribe_endpoint_removed(client):
    assert client.post('/api/newsletter/subscribe', params={'email': 'a@b.it'}).status_code in (404, 405)


def test_unsubscribe(client, mongo):
    from auth import newsletter_token
    mongo.subscribers.insert_one({'id': 's1', 'email': 'iscritto@example.com'})
    assert client.post('/api/newsletter/unsubscribe', json={'token': 'falso'}).status_code == 400
    r = client.post('/api/newsletter/unsubscribe', json={'token': newsletter_token('Iscritto@example.com')})
    assert r.status_code == 200
    assert mongo.subscribers.count_documents({}) == 0
    assert mongo.unsubscribed.count_documents({'email': 'iscritto@example.com'}) == 1


def test_ical_export(client, mongo):
    mongo.bookings.insert_one({'id': 'b1', 'check_in': d(5), 'check_out': d(7), 'status': 'confirmed', 'source': 'website'})
    r = client.get('/api/ical/export.ics')
    assert r.status_code == 200 and b'BEGIN:VCALENDAR' in r.content
    assert b'b1@lightblue-anguillara' in r.content


# ---------------------------------------------------------------- checkout
def test_checkout_requires_terms(client, instant):
    r = client.post('/api/bookings/checkout', json=checkout_payload(accept_terms=False))
    assert r.status_code == 422
    assert 'Condizioni' in r.json()['detail']


def test_checkout_creates_pending_booking(client, mongo, fake_stripe, instant):
    r = client.post('/api/bookings/checkout', json=checkout_payload(is_refundable=False))
    assert r.status_code == 200, r.text
    j = r.json()
    assert j['amount'] == 342.0
    b = mongo.bookings.find_one({'id': j['booking_id']})
    assert b['status'] == 'pending' and b['terms_accepted_at']
    kw = fake_stripe.sessions[j['session_id']]['kw']
    assert kw['line_items'][0]['price_data']['unit_amount'] == 34200
    assert kw['success_url'].startswith('http://localhost:3000/payment/success')


def test_checkout_ignores_foreign_origin(client, fake_stripe, instant):
    r = client.post('/api/bookings/checkout', json=checkout_payload(origin_url='https://evil.example'))
    kw = fake_stripe.sessions[r.json()['session_id']]['kw']
    assert kw['success_url'].startswith('https://www.example-site.test/')


def test_checkout_overlap_is_rejected(client, instant):
    assert client.post('/api/bookings/checkout', json=checkout_payload()).status_code == 200
    r = client.post('/api/bookings/checkout', json=checkout_payload(ci=31, co=35, guest_email='b@example.com'))
    assert r.status_code == 409


def test_checkout_stripe_error_is_generic(client, mongo, fake_stripe, instant):
    fake_stripe.fail_create = True
    r = client.post('/api/bookings/checkout', json=checkout_payload())
    assert r.status_code == 502
    assert 'sk_' not in r.text and 'API Key' not in r.text
    assert mongo.bookings.count_documents({}) == 0


def test_payment_status_confirms_booking(client, mongo, fake_stripe, sent_emails, instant):
    j = client.post('/api/bookings/checkout', json=checkout_payload()).json()
    fake_stripe.pay(j['session_id'])
    r = client.get(f"/api/payments/status/{j['session_id']}")
    assert r.json()['payment_status'] == 'paid'
    b = mongo.bookings.find_one({'id': j['booking_id']})
    assert b['status'] == 'confirmed' and b['payment_status'] == 'fully_paid'
    assert wait_for(lambda: any(m['to'] == ['mario@example.com'] for m in sent_emails))


def test_cancel_checkout_expires_session_and_frees_dates(client, mongo, fake_stripe, instant):
    j = client.post('/api/bookings/checkout', json=checkout_payload()).json()
    r = client.delete(f"/api/bookings/checkout/{j['session_id']}")
    assert r.json()['cancelled'] is True
    assert fake_stripe.sessions[j['session_id']]['status'] == 'expired'  # non più pagabile
    assert mongo.bookings.count_documents({}) == 0


def test_cancel_checkout_after_payment_keeps_booking(client, mongo, fake_stripe, instant):
    j = client.post('/api/bookings/checkout', json=checkout_payload()).json()
    fake_stripe.pay(j['session_id'])
    r = client.delete(f"/api/bookings/checkout/{j['session_id']}")
    assert r.json()['cancelled'] is False
    assert mongo.bookings.find_one({'id': j['booking_id']})['status'] == 'confirmed'


def _age_booking(mongo, booking_id, minutes):
    old = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
    mongo.bookings.update_one({'id': booking_id}, {'$set': {'created_at': old}})


def _run_cleanup(client):
    # esegue il cleanup nello stesso event loop dell'app
    from routers.payments import cleanup_expired_bookings
    client.portal.call(cleanup_expired_bookings)


def test_cleanup_never_deletes_while_stripe_session_is_open(client, mongo, fake_stripe, instant):
    """Bug storico: dopo 10 minuti la prenotazione spariva mentre Stripe accettava ancora il pagamento."""
    j = client.post('/api/bookings/checkout', json=checkout_payload()).json()
    _age_booking(mongo, j['booking_id'], 15)
    _run_cleanup(client)
    assert mongo.bookings.count_documents({'id': j['booking_id']}) == 1
    # l'ospite paga al 15° minuto: la prenotazione viene confermata
    fake_stripe.pay(j['session_id'])
    client.get(f"/api/payments/status/{j['session_id']}")
    assert mongo.bookings.find_one({'id': j['booking_id']})['status'] == 'confirmed'


def test_cleanup_removes_abandoned_after_expiry(client, mongo, fake_stripe, instant):
    j = client.post('/api/bookings/checkout', json=checkout_payload()).json()
    _age_booking(mongo, j['booking_id'], 40)
    _run_cleanup(client)
    assert mongo.bookings.count_documents({}) == 0
    assert fake_stripe.sessions[j['session_id']]['status'] == 'expired'


def test_cleanup_confirms_late_payment_instead_of_deleting(client, mongo, fake_stripe, instant):
    j = client.post('/api/bookings/checkout', json=checkout_payload()).json()
    _age_booking(mongo, j['booking_id'], 40)
    fake_stripe.pay(j['session_id'])
    _run_cleanup(client)
    assert mongo.bookings.find_one({'id': j['booking_id']})['status'] == 'confirmed'


def test_cleanup_does_not_touch_manual_bookings(client, mongo):
    old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    mongo.bookings.insert_one({'id': 'm1', 'status': 'pending', 'payment_status': 'unpaid', 'source': 'manual',
                               'check_in': d(5), 'check_out': d(7), 'created_at': old})
    _run_cleanup(client)
    assert mongo.bookings.count_documents({'id': 'm1'}) == 1


# ---------------------------------------------------------------- webhook
def _webhook_event(monkeypatch, etype, obj):
    event = type('E', (), {'type': etype, 'data': type('D', (), {'object': obj})()})()
    monkeypatch.setattr(stripe.Webhook, 'construct_event', lambda body, sig, secret: event)


def test_webhook_rejects_bad_signature(client):
    r = client.post('/api/webhook/stripe', content=b'{}', headers={'Stripe-Signature': 'bad'})
    assert r.status_code == 400


def test_webhook_requires_secret(client, monkeypatch):
    import routers.payments as p
    monkeypatch.setattr(p, 'STRIPE_WEBHOOK_SECRET', '')
    ev = {'type': 'checkout.session.completed', 'data': {'object': {'id': 'cs_x', 'payment_status': 'paid'}}}
    assert client.post('/api/webhook/stripe', content=json.dumps(ev)).status_code == 503


def test_webhook_confirms_once(client, mongo, monkeypatch, sent_emails, instant):
    j = client.post('/api/bookings/checkout', json=checkout_payload()).json()
    _webhook_event(monkeypatch, 'checkout.session.completed',
                   {'id': j['session_id'], 'payment_status': 'paid', 'payment_intent': 'pi_1'})
    for _ in range(2):  # Stripe può inviare lo stesso evento più volte
        assert client.post('/api/webhook/stripe', content=b'{}', headers={'Stripe-Signature': 's'}).status_code == 200
    b = mongo.bookings.find_one({'id': j['booking_id']})
    assert b['status'] == 'confirmed' and b['payment_intent_id'] == 'pi_1'
    wait_for(lambda: sent_emails)
    time.sleep(0.2)
    assert len([m for m in sent_emails if m['to'] == ['mario@example.com']]) == 1


def test_webhook_expired_frees_dates(client, mongo, monkeypatch, instant):
    j = client.post('/api/bookings/checkout', json=checkout_payload()).json()
    _webhook_event(monkeypatch, 'checkout.session.expired', {'id': j['session_id']})
    client.post('/api/webhook/stripe', content=b'{}', headers={'Stripe-Signature': 's'})
    assert mongo.bookings.count_documents({}) == 0


# ---------------------------------------------------------------- iCal
class _Resp:
    def __init__(self, status, content=b''):
        self.status_code, self.content = status, content


ICS = (b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:new-1\r\n"
       b"DTSTART;VALUE=DATE:20991001\r\nDTEND;VALUE=DATE:20991005\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def test_ical_sync_failure_keeps_blocked_dates(client, mongo, auth, monkeypatch):
    """Bug storico: se Airbnb non rispondeva, le sue date venivano sbloccate."""
    import requests
    mongo.bookings.insert_one({'id': 'ext-airbnb-old', 'status': 'external', 'source': 'airbnb',
                               'check_in': d(50), 'check_out': d(55)})
    client.put('/api/admin/settings', headers=auth, json={'ical_airbnb_url': 'https://airbnb.test/cal.ics'})
    monkeypatch.setattr(requests, 'get', lambda url, timeout=15: _Resp(503))
    r = client.post('/api/admin/ical/sync', headers=auth).json()
    assert r['errors'] and mongo.bookings.count_documents({'id': 'ext-airbnb-old'}) == 1


def test_ical_sync_success_replaces_stale(client, mongo, auth, monkeypatch):
    import requests
    mongo.bookings.insert_one({'id': 'ext-airbnb-old', 'status': 'external', 'source': 'airbnb',
                               'check_in': d(50), 'check_out': d(55)})
    client.put('/api/admin/settings', headers=auth, json={'ical_airbnb_url': 'https://airbnb.test/cal.ics'})
    monkeypatch.setattr(requests, 'get', lambda url, timeout=15: _Resp(200, ICS))
    r = client.post('/api/admin/ical/sync', headers=auth).json()
    assert r['imported'] == 1 and not r['errors']
    assert mongo.bookings.count_documents({'id': 'ext-airbnb-old'}) == 0
    assert mongo.bookings.count_documents({'id': 'ext-airbnb-new-1', 'check_in': '2099-10-01'}) == 1


# ---------------------------------------------------------------- admin
@pytest.mark.parametrize('method,path', [
    ('get', '/api/admin/bookings'), ('get', '/api/admin/settings'), ('post', '/api/admin/ical/sync'),
    ('get', '/api/admin/messages'), ('get', '/api/admin/subscribers'), ('post', '/api/admin/marketing/send'),
    ('get', '/api/admin/gallery'), ('get', '/api/admin/analytics'), ('post', '/api/admin/password'),
])
def test_admin_requires_auth(client, method, path):
    assert getattr(client, method)(path).status_code in (401, 403)


def test_login_wrong_password_and_rate_limit(client):
    codes = [client.post('/api/admin/login', json={**ADMIN, 'password': 'sbagliata'}).status_code for _ in range(11)]
    assert codes[:10] == [401] * 10 and codes[10] == 429


def test_password_change_revokes_old_sessions(client, auth):
    bad = client.post('/api/admin/password', headers=auth, json={'current_password': 'no', 'new_password': 'nuova-password-sicura'})
    assert bad.status_code == 400
    short = client.post('/api/admin/password', headers=auth, json={'current_password': ADMIN['password'], 'new_password': 'corta'})
    assert short.status_code == 422
    r = client.post('/api/admin/password', headers=auth,
                    json={'current_password': ADMIN['password'], 'new_password': 'nuova-password-sicura'})
    assert r.status_code == 200
    new_auth = {'Authorization': f"Bearer {r.json()['token']}"}
    assert client.get('/api/admin/me', headers=auth).status_code == 401       # vecchia sessione chiusa
    assert client.get('/api/admin/me', headers=new_auth).status_code == 200
    # ripristino della password per gli altri test
    client.post('/api/admin/password', headers=new_auth,
                json={'current_password': 'nuova-password-sicura', 'new_password': ADMIN['password']})


def test_settings_legal_fields(client, auth):
    r = client.put('/api/admin/settings', headers=auth, json={
        'legal_owner_name': 'Mario Bianchi', 'legal_owner_tax_id': 'BNCMRA80A01H501Z', 'tourist_tax_note': '€2 a notte',
    })
    assert r.status_code == 200
    info = client.get('/api/villa/info').json()
    assert info['legal_owner_name'] == 'Mario Bianchi' and info['tourist_tax_note'] == '€2 a notte'
    assert client.put('/api/admin/settings', headers=auth, json={'deposit_percent': 150}).status_code == 422


def test_manual_booking_flow(client, auth, sent_emails):
    body = {'guest_name': 'Ospite', 'guest_email': 'ospite@example.com', 'check_in': d(100), 'check_out': d(103),
            'total_price': 400, 'deposit_amount': 120}
    r = client.post('/api/admin/bookings/manual', headers=auth, json=body)
    assert r.status_code == 200, r.text
    bid = r.json()['booking']['id']
    assert client.post('/api/admin/bookings/manual', headers=auth, json=body).status_code == 400   # sovrapposizione
    bad = {**body, 'check_in': '03/05/2027'}
    assert client.post('/api/admin/bookings/manual', headers=auth, json=bad).status_code == 400
    assert wait_for(lambda: len(sent_emails) == 1)  # conferma

    # cambio solo del pagamento: nessuna email "modifica prenotazione"
    client.patch(f'/api/admin/bookings/{bid}', headers=auth, json={'payment_status': 'fully_paid'})
    time.sleep(0.3)
    assert len(sent_emails) == 1
    # cambio date: email all'ospite
    client.patch(f'/api/admin/bookings/{bid}', headers=auth, json={'check_out': d(104)})
    assert wait_for(lambda: len(sent_emails) == 2)
    assert 'aggiornata' in sent_emails[-1]['subject']

    assert client.patch(f'/api/admin/bookings/{bid}', headers=auth, json={'check_in': '01/01/2027'}).status_code == 422
    assert client.delete(f'/api/admin/bookings/{bid}', headers=auth).json()['archived'] is True


def test_cancel_refund_zero(client, auth, mongo):
    mongo.bookings.insert_one({'id': 'r1', 'guest_name': 'X', 'guest_email': 'x@example.com', 'check_in': d(5),
                               'check_out': d(7), 'status': 'confirmed', 'payment_status': 'unpaid', 'total_price': 100})
    r = client.post('/api/admin/bookings/r1/cancel-refund', headers=auth, json={'amount': 0})
    assert r.status_code == 200 and mongo.bookings.find_one({'id': 'r1'})['status'] == 'cancelled'


def test_marketing_only_to_subscribers_with_unsubscribe_link(client, auth, mongo, sent_emails):
    mongo.subscribers.insert_one({'id': 's1', 'email': 'iscritto@example.com'})
    mongo.subscribers.insert_one({'id': 's2', 'email': 'ex@example.com'})
    mongo.unsubscribed.insert_one({'email': 'ex@example.com'})
    r = client.post('/api/admin/marketing/send', headers=auth, json={
        'subject': 'Offerta', 'html_content': '<p>Ciao</p>',
        'recipients': ['iscritto@example.com', 'ex@example.com', 'estraneo@example.com'],
    })
    assert r.status_code == 200, r.text
    assert r.json()['sent'] == 1 and r.json()['skipped'] == 2
    assert [m['to'] for m in sent_emails] == [['iscritto@example.com']]
    mail = sent_emails[0]
    assert '/disiscrizione?token=' in mail['html'] and 'List-Unsubscribe' in mail['headers']


def test_gallery_upload_rejects_non_images(client, auth):
    r = client.post('/api/admin/gallery/upload', headers=auth,
                    files={'file': ('x.html', b'<script>', 'text/html')}, data={'category': 'gallery'})
    assert r.status_code == 400
