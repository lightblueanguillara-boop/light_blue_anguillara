"""Test della prenotazione su richiesta (modalità predefinita del sito)."""
from datetime import datetime, timedelta, timezone

from test_api import d, wait_for, checkout_payload


def request_payload(ci=40, co=43, **kw):
    p = checkout_payload(ci, co, guest_phone='333 1234567', contact_whatsapp=True)
    p.pop('origin_url')
    p.update(kw)
    return p


def _run(client, fn):
    client.portal.call(fn)


def test_default_mode_is_request(client):
    info = client.get('/api/villa/info').json()
    assert info['booking_mode'] == 'request' and info['request_hold_hours'] == 24


def test_instant_checkout_refused_in_request_mode(client):
    r = client.post('/api/bookings/checkout', json=checkout_payload())
    assert r.status_code == 403


def test_request_refused_in_instant_mode(client, instant):
    assert client.post('/api/bookings/request', json=request_payload()).status_code == 403


def test_request_requires_phone_and_terms(client):
    p = request_payload(); p.pop('guest_phone')
    assert client.post('/api/bookings/request', json=p).status_code == 422
    assert client.post('/api/bookings/request', json=request_payload(accept_terms=False)).status_code == 422


def test_request_created_blocks_dates_and_notifies(client, mongo, sent_emails, fake_stripe):
    r = client.post('/api/bookings/request', json=request_payload())
    assert r.status_code == 200, r.text
    bid = r.json()['booking_id']
    b = mongo.bookings.find_one({'id': bid})
    assert b['status'] == 'request' and b['contact_whatsapp'] is True and b['payment_status'] == 'unpaid'
    expires = datetime.fromisoformat(b['request_expires_at'])
    assert timedelta(hours=23, minutes=59) < expires - datetime.now(timezone.utc) <= timedelta(hours=24)
    assert fake_stripe.sessions == {}                        # nessun pagamento alla richiesta
    blocked = client.get('/api/availability', params={'start': d(40), 'end': d(43)}).json()['blocked_dates']
    assert d(40) in blocked                                    # date riservate
    assert client.post('/api/bookings/request', json=request_payload(41, 44)).status_code == 409
    assert wait_for(lambda: len(sent_emails) == 2)
    subjects = {m['to'][0]: m['subject'] for m in sent_emails}
    assert 'ricevuto la tua richiesta' in subjects['mario@example.com']
    assert 'Nuova richiesta da Mario Rossi' == subjects['notify@example.com']
    owner = next(m for m in sent_emails if m['to'] == ['notify@example.com'])
    assert owner['reply_to'] == 'mario@example.com'             # rispondendo si scrive all'ospite


def test_hold_hours_from_settings(client, auth, mongo):
    client.put('/api/admin/settings', headers=auth, json={'request_hold_hours': 48})
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    exp = datetime.fromisoformat(mongo.bookings.find_one({'id': bid})['request_expires_at'])
    assert exp - datetime.now(timezone.utc) > timedelta(hours=47)
    assert client.put('/api/admin/settings', headers=auth, json={'payment_link_hours': 30}).status_code == 422


def test_approve_sends_payment_link_then_payment_confirms(client, auth, mongo, fake_stripe, sent_emails):
    bid = client.post('/api/bookings/request', json=request_payload(is_refundable=False)).json()['booking_id']
    r = client.post(f'/api/admin/bookings/{bid}/approve', headers=auth)
    assert r.status_code == 200, r.text
    j = r.json()
    b = mongo.bookings.find_one({'id': bid})
    assert b['status'] == 'awaiting_payment' and b['payment_url'].startswith('https://checkout.stripe.test/')
    sid = next(iter(fake_stripe.sessions))
    kw = fake_stripe.sessions[sid]['kw']
    assert kw['line_items'][0]['price_data']['unit_amount'] == 34200
    assert kw['success_url'].startswith('https://www.example-site.test/payment/success')
    # WhatsApp in coda (l'ospite ha accettato di essere contattato) con il link corto al pagamento
    assert j['email_sent'] is True and j['whatsapp_id']
    wa = mongo.whatsapp_queue.find_one({'id': j['whatsapp_id']})
    assert wa['key'] == 'richiesta_accettata' and wa['phone'] == '393331234567' and 'accettato' in wa['text']
    assert b['payment_url'] in _expand(client, wa['text'])     # (link finto corto: resta com'è)
    assert wait_for(lambda: any('accettata' in m['subject'] for m in sent_emails))
    approved_mail = next(m for m in sent_emails if 'accettata' in m['subject'])
    assert 'Paga e conferma' in approved_mail['html'] and b['payment_url'] in _expand(client, approved_mail['html'])
    # già decisa: non si può approvare due volte
    assert client.post(f'/api/admin/bookings/{bid}/approve', headers=auth).status_code == 409
    # l'ospite paga dal link
    fake_stripe.pay(sid)
    client.get(f'/api/payments/status/{sid}')
    b = mongo.bookings.find_one({'id': bid})
    assert b['status'] == 'confirmed' and b['payment_status'] == 'fully_paid'
    # pagato: il WhatsApp "paga qui" non serve più e arriva la conferma
    assert mongo.whatsapp_queue.find_one({'id': j['whatsapp_id']})['status'] == 'annullato'
    assert wait_for(lambda: mongo.whatsapp_queue.find_one({'booking_id': bid, 'key': 'prenotazione_confermata'}))


def _expand(client, text):
    import short_links
    return client.portal.call(short_links.expand, text)


def test_approve_fails_cleanly_if_stripe_down(client, auth, mongo, fake_stripe):
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    fake_stripe.fail_create = True
    r = client.post(f'/api/admin/bookings/{bid}/approve', headers=auth)
    assert r.status_code == 502
    assert mongo.bookings.find_one({'id': bid})['status'] == 'request'


def test_reject_frees_dates_and_notifies(client, auth, mongo, sent_emails):
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    r = client.post(f'/api/admin/bookings/{bid}/reject', headers=auth, json={'reason': 'Casa non disponibile per lavori'})
    assert r.status_code == 200
    wa = mongo.whatsapp_queue.find_one({'id': r.json()['whatsapp_id']})
    assert wa['key'] == 'richiesta_rifiutata' and 'lavori' in wa['text']
    assert mongo.bookings.find_one({'id': bid})['status'] == 'rejected'
    blocked = client.get('/api/availability', params={'start': d(40), 'end': d(43)}).json()['blocked_dates']
    assert blocked == []
    assert wait_for(lambda: any('lavori' in m['html'] for m in sent_emails))


def test_reject_after_approve_closes_payment_link(client, auth, mongo, fake_stripe):
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    client.post(f'/api/admin/bookings/{bid}/approve', headers=auth)
    sid = next(iter(fake_stripe.sessions))
    assert client.post(f'/api/admin/bookings/{bid}/reject', headers=auth, json={}).status_code == 200
    assert fake_stripe.sessions[sid]['status'] == 'expired'


def test_reject_refused_if_already_paid(client, auth, mongo, fake_stripe):
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    client.post(f'/api/admin/bookings/{bid}/approve', headers=auth)
    fake_stripe.pay(next(iter(fake_stripe.sessions)))
    assert client.post(f'/api/admin/bookings/{bid}/reject', headers=auth, json={}).status_code == 409


def test_unanswered_request_expires(client, mongo, sent_emails):
    from routers.booking_requests import expire_requests
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    mongo.bookings.update_one({'id': bid}, {'$set': {'request_expires_at': past}})
    _run(client, expire_requests)
    assert mongo.bookings.find_one({'id': bid})['status'] == 'expired'
    assert client.get('/api/availability', params={'start': d(40), 'end': d(43)}).json()['blocked_dates'] == []
    assert wait_for(lambda: any('chiusa automaticamente' in m['html'] for m in sent_emails))


def test_unpaid_approved_request_expires(client, auth, mongo, fake_stripe):
    from routers.booking_requests import expire_requests
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    client.post(f'/api/admin/bookings/{bid}/approve', headers=auth)
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    mongo.bookings.update_one({'id': bid}, {'$set': {'payment_due_at': past}})
    _run(client, expire_requests)
    assert mongo.bookings.find_one({'id': bid})['status'] == 'expired'
    assert fake_stripe.sessions[next(iter(fake_stripe.sessions))]['status'] == 'expired'


def test_late_payment_wins_over_expiry(client, auth, mongo, fake_stripe):
    from routers.booking_requests import expire_requests
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    client.post(f'/api/admin/bookings/{bid}/approve', headers=auth)
    fake_stripe.pay(next(iter(fake_stripe.sessions)))
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    mongo.bookings.update_one({'id': bid}, {'$set': {'payment_due_at': past}})
    _run(client, expire_requests)
    assert mongo.bookings.find_one({'id': bid})['status'] == 'confirmed'


def test_whatsapp_link_endpoint(client, auth):
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    r = client.post('/api/admin/whatsapp/link', headers=auth, json={'booking_id': bid}).json()
    assert r['phone'] == '393331234567' and 'Mario' in r['text'] and r['link'].startswith('https://api.whatsapp.com/send')


def test_analytics_counts_requests(client, auth):
    client.post('/api/bookings/request', json=request_payload())
    assert client.get('/api/admin/analytics', headers=auth).json()['totals']['open_requests'] == 1


def test_manual_booking_ignores_rejected_requests(client, auth):
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    client.post(f'/api/admin/bookings/{bid}/reject', headers=auth, json={})
    body = {'guest_name': 'Altro', 'guest_email': 'altro@example.com', 'check_in': d(40), 'check_out': d(43), 'total_price': 300}
    assert client.post('/api/admin/bookings/manual', headers=auth, json=body).status_code == 200
