"""Test delle notifiche sul telefono (le 14 notifiche decise con il proprietario)."""
import re
from datetime import datetime, timedelta, timezone

import pytest

import notifications
from conftest import ADMIN
from test_api import checkout_payload, d, wait_for
from test_booking_requests import request_payload

# Testi senza emoji o simboli: lettere (anche accentate), cifre, punteggiatura semplice, euro
PLAIN = re.compile(r"^[\w\s.,:;'\"()€/?!@\-àèéìòùÀÈÉÌÒÙ]+$")


def plain(text: str) -> bool:
    return bool(PLAIN.match(text)) and not any(ch in text for ch in '·→—–•')


def last(pushes, kind):
    assert wait_for(lambda: any(p['kind'] == kind for p in pushes)), f'notifica {kind} non arrivata: {pushes}'
    return [p for p in pushes if p['kind'] == kind][-1]


def _run(client, fn, *a):
    client.portal.call(fn, *a)


# ------------------------------------------------------------------ dispositivi
def test_push_config_and_auth(client, auth):
    assert client.get('/api/admin/push/config').status_code in (401, 403)
    cfg = client.get('/api/admin/push/config', headers=auth).json()
    assert cfg['enabled'] is True and len(cfg['public_key']) > 80


def test_subscribe_list_delete(client, auth, mongo):
    body = {'endpoint': 'https://fcm.googleapis.com/fcm/send/xyz', 'keys': {'p256dh': 'k', 'auth': 'a'}}
    r = client.post('/api/admin/push/subscribe', headers={**auth, 'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari/604.1'}, json=body)
    assert r.status_code == 200 and r.json()['label'] == 'iPhone, Safari'
    again = client.post('/api/admin/push/subscribe', headers=auth, json=body)        # stesso telefono: nessun doppione
    assert again.json()['id'] == r.json()['id'] and mongo.push_subscriptions.count_documents({}) == 1
    subs = client.get('/api/admin/push/subscriptions', headers=auth).json()
    assert len(subs) == 1 and 'keys' not in subs[0]
    assert client.delete(f"/api/admin/push/subscriptions/{r.json()['id']}", headers=auth).status_code == 200
    assert mongo.push_subscriptions.count_documents({}) == 0


@pytest.mark.parametrize('endpoint', ['http://fcm.googleapis.com/x', 'https://evil.example/push', 'https://169.254.169.254/latest'])
def test_subscribe_rejects_unknown_push_services(client, auth, endpoint):
    r = client.post('/api/admin/push/subscribe', headers=auth, json={'endpoint': endpoint, 'keys': {'p256dh': 'k', 'auth': 'a'}})
    assert r.status_code == 400


def test_test_notification(client, auth, pushes, mongo):
    r = client.post('/api/admin/push/test', headers=auth, json={})
    assert r.status_code == 200 and r.json()['sent'] == 1
    p = pushes[-1]
    assert p['title'] == 'Notifiche attive' and p['url'] == '/admin/settings#notifiche' and plain(p['body'])
    mongo.push_subscriptions.delete_many({})
    assert client.post('/api/admin/push/test', headers=auth, json={}).status_code == 400


def test_expired_device_is_removed(client, auth, mongo, monkeypatch):
    class Gone(Exception):
        response = type('R', (), {'status_code': 410})()
    monkeypatch.setattr(notifications, '_send_one', lambda sub, payload: (_ for _ in ()).throw(Gone()))
    mongo.push_subscriptions.insert_one({'id': 'old', 'endpoint': 'https://fcm.googleapis.com/x', 'keys': {}, 'label': 'vecchio'})
    client.post('/api/admin/push/test', headers=auth, json={})
    assert mongo.push_subscriptions.count_documents({'id': 'old'}) == 0


# ------------------------------------------------------------------ A. prenotazioni
def test_1_new_request(client, pushes):
    bid = client.post('/api/bookings/request', json=request_payload(40, 43)).json()['booking_id']
    p = last(pushes, 'new_request')
    assert p['title'] == 'Nuova richiesta di prenotazione'
    assert 'Mario Rossi' in p['body'] and '3 notti' in p['body'] and '€ 360' in p['body'] and 'Rispondi entro' in p['body']
    assert p['url'] == f'/admin/bookings?open={bid}' and plain(p['title'] + p['body'])
    assert p['badge'] == 1 and p['icon'].endswith('/api/brand/logo-192.png?v=default')


def test_2_request_expiring_once(client, pushes, mongo):
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    soon = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    mongo.bookings.update_one({'id': bid}, {'$set': {'request_expires_at': soon}})
    _run(client, notifications.run_scheduled)
    _run(client, notifications.run_scheduled)                       # seconda passata: niente doppioni
    sent = [p for p in pushes if p['kind'] == 'request_expiring']
    assert len(sent) == 1 and 'scade tra 3 ore' in sent[0]['body'] and sent[0]['url'] == f'/admin/bookings?open={bid}'
    assert sent[0]['title'] == 'Richiesta in scadenza' and plain(sent[0]['body'])


def test_3_request_expired(client, pushes, mongo):
    from routers.booking_requests import expire_requests
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    mongo.bookings.update_one({'id': bid}, {'$set': {'request_expires_at': (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}})
    _run(client, expire_requests)
    p = last(pushes, 'request_expired')
    assert "L'ospite è stato avvisato" in p['body'] and p['url'] == f'/admin/bookings?open={bid}' and plain(p['body'])


def test_4_booking_confirmed(client, auth, pushes, fake_stripe):
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    client.post(f'/api/admin/bookings/{bid}/approve', headers=auth)
    sid = next(iter(fake_stripe.sessions))
    fake_stripe.pay(sid)
    client.get(f'/api/payments/status/{sid}')
    p = last(pushes, 'booking_confirmed')
    assert p['title'] == 'Prenotazione confermata' and 'ha pagato € 360' in p['body'] and p['url'] == f'/admin/bookings?open={bid}'


def test_4_instant_booking_confirmed(client, pushes, fake_stripe, instant):
    j = client.post('/api/bookings/checkout', json=checkout_payload()).json()
    fake_stripe.pay(j['session_id'])
    client.get(f"/api/payments/status/{j['session_id']}")
    assert last(pushes, 'booking_confirmed')['url'] == f"/admin/bookings?open={j['booking_id']}"


def test_5_payment_expired(client, auth, pushes, mongo):
    from routers.booking_requests import expire_requests
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    client.post(f'/api/admin/bookings/{bid}/approve', headers=auth)
    mongo.bookings.update_one({'id': bid}, {'$set': {'payment_due_at': (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}})
    _run(client, expire_requests)
    p = last(pushes, 'payment_expired')
    assert p['title'] == 'Pagamento non completato' and 'sono di nuovo libere' in p['body'] and plain(p['body'])


# ------------------------------------------------------------------ B. Airbnb / Booking
class _Resp:
    def __init__(self, status, content=b''):
        self.status_code, self.content = status, content


def _ics(*events):
    body = b''.join(b'BEGIN:VEVENT\r\nUID:%s\r\nDTSTART;VALUE=DATE:%s\r\nDTEND;VALUE=DATE:%s\r\nEND:VEVENT\r\n'
                    % (uid.encode(), a.replace('-', '').encode(), b.replace('-', '').encode()) for uid, a, b in events)
    return b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\n' + body + b'END:VCALENDAR\r\n'


def test_6_7_external_new_and_removed(client, auth, pushes, monkeypatch):
    import requests
    client.put('/api/admin/settings', headers=auth, json={'ical_airbnb_url': 'https://airbnb.test/c.ics'})
    feed = {'data': _ics(('old', d(10), d(12)))}
    monkeypatch.setattr(requests, 'get', lambda url, timeout=15: _Resp(200, feed['data']))
    client.post('/api/admin/ical/sync', headers=auth)                     # prima sincronizzazione: nessun avviso
    assert not [p for p in pushes if p['kind'].startswith('external')]
    feed['data'] = _ics(('new', d(20), d(23)))                            # 'old' sparisce, 'new' compare
    client.post('/api/admin/ical/sync', headers=auth)
    n = last(pushes, 'external_new')
    assert n['title'] == 'Nuova prenotazione Airbnb' and '3 notti' in n['body']
    assert n['url'] == f"/admin/calendar?date={d(20)}&open=ext-airbnb-new" and plain(n['title'] + n['body'])
    r = last(pushes, 'external_removed')
    assert r['title'] == 'Prenotazione Airbnb rimossa' and 'tornate libere' in r['body'] and r['url'] == f'/admin/calendar?date={d(10)}'


def test_8_ical_failing_after_one_hour(client, auth, pushes, mongo, monkeypatch):
    import requests
    client.put('/api/admin/settings', headers=auth, json={'ical_booking_url': 'https://booking.test/c.ics'})
    monkeypatch.setattr(requests, 'get', lambda url, timeout=15: _Resp(503))
    client.post('/api/admin/ical/sync', headers=auth)
    assert not [p for p in pushes if p['kind'] == 'ical_failing']          # appena fallito: ancora niente
    past = (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat()
    mongo.settings.update_one({'id': 'global'}, {'$set': {'ical_fail.booking.since': past}})
    client.post('/api/admin/ical/sync', headers=auth)
    client.post('/api/admin/ical/sync', headers=auth)                     # una sola notifica
    sent = [p for p in pushes if p['kind'] == 'ical_failing']
    assert len(sent) == 1 and sent[0]['title'] == 'Calendario Booking.com non aggiornato' and sent[0]['url'] == '/admin/settings#ical'


# ------------------------------------------------------------------ C. messaggi
def test_9_new_message(client, pushes):
    client.post('/api/contact', json={'name': 'Elena Gallo', 'email': 'elena@example.com', 'message': 'La casa è disponibile ad aprile?'})
    p = last(pushes, 'new_message')
    assert p['title'] == 'Nuovo messaggio da Elena Gallo' and 'disponibile ad aprile' in p['body']
    assert p['url'].startswith('/admin/inbox?open=')


# ------------------------------------------------------------------ D. promemoria
class _FakeNow(datetime):
    fixed = None

    @classmethod
    def now(cls, tz=None):
        return cls.fixed.astimezone(tz) if tz else cls.fixed


def _at_local(hour):
    local = datetime.now(notifications.ROME).replace(hour=hour, minute=5, second=0, microsecond=0)
    _FakeNow.fixed = local.astimezone(timezone.utc)
    return local.date()


def test_10_arrival_tomorrow_from_18(client, pushes, mongo, monkeypatch):
    monkeypatch.setattr(notifications, 'datetime', _FakeNow)
    today = _at_local(17)
    tomorrow = (today + timedelta(days=1)).isoformat()
    mongo.bookings.insert_one({'id': 'arr1', 'guest_name': 'Marco Bianchi', 'status': 'confirmed', 'source': 'website',
                               'check_in': tomorrow, 'check_out': (today + timedelta(days=4)).isoformat(),
                               'adults': 2, 'children': 0, 'notes': 'Arrivo verso le 20'})
    _run(client, notifications.run_scheduled)
    assert not [p for p in pushes if p['kind'] == 'arrival_tomorrow']      # alle 17 ancora no
    _at_local(18)
    _run(client, notifications.run_scheduled)
    _run(client, notifications.run_scheduled)
    sent = [p for p in pushes if p['kind'] == 'arrival_tomorrow']
    assert len(sent) == 1 and sent[0]['title'] == 'Domani arriva Marco Bianchi'
    assert '3 notti, 2 adulti' in sent[0]['body'] and 'Note: Arrivo verso le 20' in sent[0]['body']
    assert sent[0]['url'] == '/admin/bookings?open=arr1' and plain(sent[0]['body'])


def test_11_departure_today_from_8(client, pushes, mongo, monkeypatch):
    monkeypatch.setattr(notifications, 'datetime', _FakeNow)
    today = _at_local(8)
    mongo.bookings.insert_many([
        {'id': 'dep1', 'guest_name': 'Marco Bianchi', 'status': 'confirmed', 'source': 'website',
         'check_in': (today - timedelta(days=3)).isoformat(), 'check_out': today.isoformat()},
        {'id': 'ext-airbnb-n', 'guest_name': 'Ospite Airbnb', 'status': 'external', 'source': 'airbnb',
         'check_in': today.isoformat(), 'check_out': (today + timedelta(days=2)).isoformat()},
    ])
    _run(client, notifications.run_scheduled)
    p = last(pushes, 'departure_today')
    assert p['title'] == 'Oggi parte Marco Bianchi' and p['body'] == 'Prossimo arrivo: Ospite Airbnb, oggi.'
    assert p['url'] == '/admin/bookings?open=dep1'


# ------------------------------------------------------------------ E. sicurezza e problemi
def test_12_payment_orphan(client, pushes, mongo, fake_stripe, instant):
    j = client.post('/api/bookings/checkout', json=checkout_payload()).json()
    mongo.bookings.delete_one({'id': j['booking_id']})
    fake_stripe.pay(j['session_id'])
    client.get(f"/api/payments/status/{j['session_id']}")
    p = last(pushes, 'payment_orphan')
    assert p['title'] == 'Pagamento da verificare' and '€ 360' in p['body'] and plain(p['body'])


def test_13_email_failed(client, pushes, monkeypatch):
    import resend

    def boom(params):
        if params['to'] != ['notify@example.com']:
            raise RuntimeError('rifiutata')
    monkeypatch.setattr(resend.Emails, 'send', boom)
    bid = client.post('/api/bookings/request', json=request_payload()).json()['booking_id']
    p = last(pushes, 'email_failed')
    assert p['title'] == 'Email non consegnata' and 'mario@example.com' in p['body']
    assert p['url'] == f'/admin/bookings?open={bid}'


def test_14_new_device_login(client, pushes):
    login = lambda dev: client.post('/api/admin/login', json={**ADMIN, 'device_id': dev},
                                    headers={'User-Agent': 'Mozilla/5.0 (Linux; Android 14) Chrome/130 Mobile'})
    login('telefono-1')                                  # primo dispositivo in assoluto: nessun avviso
    login('telefono-1')                                  # già conosciuto
    assert not [p for p in pushes if p['kind'] == 'new_login']
    login('pc-sconosciuto')
    p = last(pushes, 'new_login')
    assert p['title'] == 'Nuovo accesso al pannello' and 'Android, Chrome' in p['body'] and 'cambia subito la password' in p['body']
    assert p['url'] == '/admin/settings#password'


def test_14_login_without_device_id_is_reported(client, pushes):
    client.post('/api/admin/login', json=ADMIN)
    assert last(pushes, 'new_login')


# ------------------------------------------------------------------ impostazioni
def test_discreet_mode(client, auth, pushes):
    client.put('/api/admin/settings', headers=auth, json={'notify_detail': 'discreet'})
    client.post('/api/bookings/request', json=request_payload())
    p = last(pushes, 'new_request')
    assert p['body'] == 'Hai una nuova richiesta di prenotazione da valutare.' and 'Mario' not in p['title'] + p['body']


def test_group_switched_off_but_security_always_on(client, auth, pushes):
    client.put('/api/admin/settings', headers=auth, json={'notify_groups': {'A': False, 'B': True, 'C': False, 'D': True}})
    client.post('/api/bookings/request', json=request_payload())
    client.post('/api/contact', json={'name': 'Anna', 'email': 'anna@example.com', 'message': 'ciao'})
    client.post('/api/admin/login', json=ADMIN)          # gruppo E: sempre attivo
    last(pushes, 'new_login')
    assert not [p for p in pushes if p['kind'] in ('new_request', 'new_message')]


def test_quiet_hours_hold_and_release(client, pushes, mongo, monkeypatch):
    monkeypatch.setattr(notifications, '_in_quiet_hours', lambda settings, now: True)
    client.post('/api/bookings/request', json=request_payload())
    client.post('/api/admin/login', json=ADMIN)          # urgente: arriva comunque
    last(pushes, 'new_login')
    assert not [p for p in pushes if p['kind'] == 'new_request']
    assert wait_for(lambda: mongo.push_outbox.count_documents({}) == 1)
    mongo.push_outbox.update_many({}, {'$set': {'deliver_after': (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}})
    _run(client, notifications.flush_outbox)
    assert last(pushes, 'new_request') and mongo.push_outbox.count_documents({}) == 0


def test_quiet_hours_logic():
    s = {'notify_quiet_enabled': True, 'notify_quiet_start': '23:00', 'notify_quiet_end': '08:00'}
    at = lambda h, m=0: datetime.now(notifications.ROME).replace(hour=h, minute=m).astimezone(timezone.utc)
    assert notifications._in_quiet_hours(s, at(23, 30)) and notifications._in_quiet_hours(s, at(3))
    assert not notifications._in_quiet_hours(s, at(8)) and not notifications._in_quiet_hours(s, at(12))
    assert not notifications._in_quiet_hours({**s, 'notify_quiet_enabled': False}, at(3))


@pytest.mark.parametrize('bad', [{'notify_quiet_start': '25:00'}, {'notify_detail': 'tutto'}])
def test_notification_settings_validation(client, auth, bad):
    assert client.put('/api/admin/settings', headers=auth, json=bad).status_code == 422


def test_all_texts_plain():
    """Nessuna notifica contiene emoji o simboli decorativi."""
    b = {'id': 'x', 'guest_name': 'Sara Conti', 'check_in': d(5), 'check_out': d(8), 'adults': 2, 'children': 1,
         'total_price': 360, 'deposit_amount': 108, 'payment_status': 'fully_paid', 'source': 'airbnb',
         'request_expires_at': datetime.now(timezone.utc).isoformat()}
    ctxs = {'ical_failing': {'source': 'airbnb'}, 'new_message': {'message': {'id': 'm', 'name': 'Elena', 'message': 'Ciao'}},
            'payment_orphan': {'amount': 360}, 'email_failed': {'to': 'a@b.it', 'subject': 'Conferma'},
            'new_login': {'at': datetime.now(timezone.utc).isoformat(), 'device': 'iPhone, Safari'}, 'departure_today': {'booking': b, 'next': None}}
    for kind in notifications.GROUP_OF:
        n = notifications.build(kind, ctxs.get(kind, {'booking': b}))
        for field in ('title', 'body', 'discreet'):
            assert plain(n[field]), f'{kind}.{field}: {n[field]}'
        assert n['url'].startswith('/admin')
