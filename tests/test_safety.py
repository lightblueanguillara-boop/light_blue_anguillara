"""Protezioni su soldi e calendario: rimborsi, pagamenti su prenotazioni chiuse, calendari vuoti."""
import stripe

from test_api import ICS, _Resp, _webhook_event, checkout_payload, d, wait_for
from test_booking_requests import request_payload

HOOK = {'Stripe-Signature': 's'}


def _pay_via_webhook(client, monkeypatch, session_id, pi='pi_1'):
    _webhook_event(monkeypatch, 'checkout.session.completed',
                   {'id': session_id, 'payment_status': 'paid', 'payment_intent': pi})
    assert client.post('/api/webhook/stripe', content=b'{}', headers=HOOK).status_code == 200


def _paid_booking(client, monkeypatch, mongo, instant_mode=True, **kw):
    """Prenotazione dal sito pagata con Stripe (modalità immediata)."""
    r = client.post('/api/bookings/checkout', json=checkout_payload(**kw))
    assert r.status_code == 200, r.text
    j = r.json()
    _pay_via_webhook(client, monkeypatch, j['session_id'])
    b = mongo.bookings.find_one({'id': j['booking_id']}, {'_id': 0})
    assert b['status'] == 'confirmed'
    return b


class FakeRefunds:
    def __init__(self):
        self.calls = []
        self.fail = False

    def create(self, **kw):
        if self.fail:
            raise stripe.error.InvalidRequestError('Charge already refunded', 'charge')
        self.calls.append(kw)
        return type('R', (), {'id': f're_{len(self.calls)}'})()


import pytest  # noqa: E402


@pytest.fixture()
def refunds(monkeypatch):
    fr = FakeRefunds()
    monkeypatch.setattr(stripe.Refund, 'create', fr.create)
    return fr


# ------------------------------------------------------------ 4. pagamento su prenotazione chiusa
def test_payment_after_cancel_does_not_reactivate(client, auth, mongo, monkeypatch, instant, fake_stripe, pushes, sent_emails):
    j = client.post('/api/bookings/checkout', json=checkout_payload()).json()
    # il proprietario cancella mentre l'ospite ha ancora aperta la pagina di pagamento...
    mongo.bookings.update_one({'id': j['booking_id']}, {'$set': {'status': 'cancelled'}})
    # ...e un altro ospite prende le stesse date
    mongo.bookings.insert_one({'id': 'altro', 'status': 'confirmed', 'check_in': d(30), 'check_out': d(33),
                               'guest_name': 'Altro', 'guest_email': 'altro@example.com'})
    _pay_via_webhook(client, monkeypatch, j['session_id'])
    b = mongo.bookings.find_one({'id': j['booking_id']})
    assert b['status'] == 'cancelled'                       # NON riattivata: niente doppia prenotazione
    assert b['payment_status'] == 'fully_paid' and b['paid_while_closed'] is True and b['payment_intent_id'] == 'pi_1'
    assert wait_for(lambda: any(p['kind'] == 'payment_on_closed' for p in pushes))
    assert wait_for(lambda: any('pagamento su prenotazione chiusa' in m['subject'] for m in sent_emails))
    assert not any(m['to'] == ['mario@example.com'] and 'confermata' in m['subject'] for m in sent_emails)


def test_late_payment_on_expired_request_confirms_if_dates_free(client, auth, mongo, monkeypatch, fake_stripe):
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    client.post(f'/api/admin/bookings/{bid}/approve', headers=auth)
    sid = mongo.payment_transactions.find_one({'booking_id': bid})['session_id']
    mongo.bookings.update_one({'id': bid}, {'$set': {'status': 'expired'}})
    _pay_via_webhook(client, monkeypatch, sid)
    assert mongo.bookings.find_one({'id': bid})['status'] == 'confirmed'


def test_late_payment_on_expired_request_with_dates_taken(client, auth, mongo, monkeypatch, fake_stripe, pushes):
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    client.post(f'/api/admin/bookings/{bid}/approve', headers=auth)
    sid = mongo.payment_transactions.find_one({'booking_id': bid})['session_id']
    mongo.bookings.update_one({'id': bid}, {'$set': {'status': 'expired'}})
    mongo.bookings.insert_one({'id': 'nuova', 'status': 'external', 'source': 'airbnb', 'check_in': d(41), 'check_out': d(42)})
    _pay_via_webhook(client, monkeypatch, sid)
    b = mongo.bookings.find_one({'id': bid})
    assert b['status'] == 'expired' and b['paid_while_closed'] is True
    assert wait_for(lambda: any(p['kind'] == 'payment_on_closed' for p in pushes))


def test_delete_closes_open_payment_page(client, auth, mongo, instant, fake_stripe):
    j = client.post('/api/bookings/checkout', json=checkout_payload()).json()
    assert client.delete(f"/api/admin/bookings/{j['booking_id']}", headers=auth).status_code == 200
    assert fake_stripe.sessions[j['session_id']]['status'] == 'expired'   # l'ospite non può più pagare


def test_delete_refuses_if_paid_meanwhile(client, auth, mongo, instant, fake_stripe):
    j = client.post('/api/bookings/checkout', json=checkout_payload()).json()
    fake_stripe.pay(j['session_id'])
    assert client.delete(f"/api/admin/bookings/{j['booking_id']}", headers=auth).status_code == 409
    assert mongo.bookings.find_one({'id': j['booking_id']})['status'] == 'confirmed'


# ------------------------------------------------------------ 3. cancellazione con rimborso
def test_delete_paid_online_booking_is_blocked(client, auth, mongo, monkeypatch, instant, fake_stripe):
    b = _paid_booking(client, monkeypatch, mongo)
    r = client.delete(f"/api/admin/bookings/{b['id']}", headers=auth)
    assert r.status_code == 409 and 'Cancella e rimborsa' in r.json()['detail']
    assert mongo.bookings.find_one({'id': b['id']})['status'] == 'confirmed'


def test_refund_preview(client, auth, mongo, monkeypatch, instant, fake_stripe):
    b = _paid_booking(client, monkeypatch, mongo)              # 3 notti x 120 = 360, pagato tutto, >10 giorni
    p = client.get(f"/api/admin/bookings/{b['id']}/refund-preview", headers=auth).json()
    assert p == {'paid': 360.0, 'suggested': 360.0, 'reason': p['reason'], 'online': True,
                 'already_refunded': False, 'email': True}


def test_cancel_refund_full_flow(client, auth, mongo, monkeypatch, instant, fake_stripe, refunds, sent_emails):
    b = _paid_booking(client, monkeypatch, mongo)
    r = client.post(f"/api/admin/bookings/{b['id']}/cancel-refund", headers=auth, json={'amount': 200})
    assert r.status_code == 200 and r.json()['refund_amount'] == 200
    assert refunds.calls == [{'payment_intent': 'pi_1', 'amount': 20000, 'reason': 'requested_by_customer',
                              'idempotency_key': f"refund-{b['id']}"}]
    after = mongo.bookings.find_one({'id': b['id']})
    assert after['status'] == 'cancelled' and after['payment_status'] == 'refunded' and after['refund_id'] == 're_1'
    mail = None
    assert wait_for(lambda: any('cancellata' in m['subject'] and m['to'] == ['mario@example.com'] for m in sent_emails))
    mail = next(m for m in sent_emails if 'cancellata' in m['subject'])
    assert 'Rimborso: € 200' in mail['html'].replace('\xa0', ' ') or '200,00' in mail['html']
    # secondo tentativo: nessun secondo rimborso
    assert client.post(f"/api/admin/bookings/{b['id']}/cancel-refund", headers=auth, json={'amount': 10}).status_code == 409
    assert len(refunds.calls) == 1


@pytest.mark.parametrize('amount', [361, -1])
def test_cancel_refund_amount_limits(client, auth, mongo, monkeypatch, instant, fake_stripe, refunds, amount):
    b = _paid_booking(client, monkeypatch, mongo)
    assert client.post(f"/api/admin/bookings/{b['id']}/cancel-refund", headers=auth,
                       json={'amount': amount}).status_code in (400, 422)
    assert refunds.calls == [] and mongo.bookings.find_one({'id': b['id']})['status'] == 'confirmed'


def test_cancel_refund_stripe_error_keeps_booking(client, auth, mongo, monkeypatch, instant, fake_stripe, refunds):
    b = _paid_booking(client, monkeypatch, mongo)
    refunds.fail = True
    r = client.post(f"/api/admin/bookings/{b['id']}/cancel-refund", headers=auth, json={'amount': 100})
    assert r.status_code == 502 and 'Rimborso non riuscito' in r.json()['detail']
    after = mongo.bookings.find_one({'id': b['id']})
    assert after['status'] == 'confirmed' and 'refund_lock' not in after     # si può riprovare
    refunds.fail = False
    assert client.post(f"/api/admin/bookings/{b['id']}/cancel-refund", headers=auth, json={'amount': 100}).status_code == 200


def test_cancel_without_refund_email_says_no_refund(client, auth, mongo, monkeypatch, instant, fake_stripe, refunds, sent_emails):
    b = _paid_booking(client, monkeypatch, mongo, is_refundable=False)
    assert client.post(f"/api/admin/bookings/{b['id']}/cancel-refund", headers=auth, json={'amount': 0}).status_code == 200
    assert refunds.calls == []
    assert wait_for(lambda: any('cancellata' in m['subject'] for m in sent_emails))
    mail = next(m for m in sent_emails if 'cancellata' in m['subject'])
    assert 'non è previsto alcun rimborso' in mail['html'] and '5 giorni' not in mail['html']


def test_delete_paid_manual_booking_does_not_promise_refund(client, auth, mongo, sent_emails):
    """Prima l'email prometteva 'il rimborso verrà accreditato' anche se nessun rimborso partiva."""
    mongo.bookings.insert_one({'id': 'm1', 'guest_name': 'Anna', 'guest_email': 'anna@example.com', 'check_in': d(20),
                               'check_out': d(22), 'status': 'confirmed', 'payment_status': 'fully_paid',
                               'total_price': 240, 'source': 'manual'})
    assert client.delete('/api/admin/bookings/m1', headers=auth).status_code == 200
    assert wait_for(lambda: sent_emails)
    assert 'rimborso' not in sent_emails[-1]['html'].lower()


def test_no_email_to_placeholder_addresses(client, auth, mongo, sent_emails):
    import time
    for i, email in enumerate(['manual@booking.com', 'sync-airbnb-0@lightblue-anguillara.it']):
        mongo.bookings.insert_one({'id': f'p{i}', 'guest_name': 'X', 'guest_email': email, 'check_in': d(20 + 3 * i),
                                   'check_out': d(21 + 3 * i), 'status': 'confirmed', 'payment_status': 'unpaid'})
        assert client.delete(f'/api/admin/bookings/p{i}', headers=auth).json()['email_sent'] is False
    time.sleep(0.3)
    assert sent_emails == []


# ------------------------------------------------------------ 5. calendario Airbnb/Booking vuoto
EMPTY_ICS = b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Airbnb//IT\r\nEND:VCALENDAR\r\n"


def test_empty_calendar_keeps_future_bookings(client, mongo, auth, monkeypatch):
    import requests
    mongo.bookings.insert_one({'id': 'ext-airbnb-a', 'status': 'external', 'source': 'airbnb',
                               'check_in': d(50), 'check_out': d(55)})
    client.put('/api/admin/settings', headers=auth, json={'ical_airbnb_url': 'https://airbnb.test/cal.ics'})
    monkeypatch.setattr(requests, 'get', lambda url, timeout=15: _Resp(200, EMPTY_ICS))
    r = client.post('/api/admin/ical/sync', headers=auth).json()
    assert mongo.bookings.count_documents({'id': 'ext-airbnb-a'}) == 1          # date ancora bloccate
    assert r['errors'] and 'vuoto' in r['errors'][0]
    assert 'vuoto' in client.get('/api/admin/settings', headers=auth).json()['last_ical_sync_errors'][0]


def test_empty_calendar_without_future_bookings_is_fine(client, mongo, auth, monkeypatch):
    import requests
    mongo.bookings.insert_one({'id': 'ext-airbnb-old', 'status': 'external', 'source': 'airbnb',
                               'check_in': d(-10), 'check_out': d(-5)})
    client.put('/api/admin/settings', headers=auth, json={'ical_airbnb_url': 'https://airbnb.test/cal.ics'})
    monkeypatch.setattr(requests, 'get', lambda url, timeout=15: _Resp(200, EMPTY_ICS))
    r = client.post('/api/admin/ical/sync', headers=auth).json()
    assert not r['errors'] and mongo.bookings.count_documents({'id': 'ext-airbnb-old'}) == 0


def test_normal_calendar_still_removes_cancelled_bookings(client, mongo, auth, monkeypatch):
    import requests
    mongo.bookings.insert_one({'id': 'ext-airbnb-a', 'status': 'external', 'source': 'airbnb',
                               'check_in': d(50), 'check_out': d(55)})
    client.put('/api/admin/settings', headers=auth, json={'ical_airbnb_url': 'https://airbnb.test/cal.ics'})
    monkeypatch.setattr(requests, 'get', lambda url, timeout=15: _Resp(200, ICS))
    r = client.post('/api/admin/ical/sync', headers=auth).json()
    assert not r['errors'] and mongo.bookings.count_documents({'id': 'ext-airbnb-a'}) == 0


def test_empty_calendar_alert_after_one_hour(client, mongo, auth, monkeypatch, pushes):
    import requests
    from datetime import datetime, timedelta, timezone
    mongo.bookings.insert_one({'id': 'ext-airbnb-a', 'status': 'external', 'source': 'airbnb',
                               'check_in': d(50), 'check_out': d(55)})
    client.put('/api/admin/settings', headers=auth, json={'ical_airbnb_url': 'https://airbnb.test/cal.ics'})
    since = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    mongo.settings.update_one({'id': 'global'}, {'$set': {'ical_fail': {'airbnb': {'since': since, 'notified': False}}}})
    monkeypatch.setattr(requests, 'get', lambda url, timeout=15: _Resp(200, EMPTY_ICS))
    client.post('/api/admin/ical/sync', headers=auth)
    assert wait_for(lambda: any(p['kind'] == 'ical_failing' for p in pushes))


# ------------------------------------------------------------ impostazioni: mai documenti doppi
def test_settings_duplicates_are_cleaned_and_prevented(client, mongo):
    from pymongo.errors import DuplicateKeyError
    from db import ensure_settings_unique
    client.get('/api/villa/info')                                   # crea le impostazioni
    first = mongo.settings.find_one({'id': 'global'})
    mongo.settings.drop_index('id_1')
    mongo.settings.insert_one({'id': 'global', 'booking_mode': 'instant'})   # copia doppia "storica"
    client.portal.call(ensure_settings_unique)
    docs = list(mongo.settings.find({'id': 'global'}))
    assert len(docs) == 1 and docs[0]['_id'] == first['_id']        # resta quella già in uso
    with pytest.raises(DuplicateKeyError):
        mongo.settings.insert_one({'id': 'global'})
