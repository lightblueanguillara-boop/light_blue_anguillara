"""Test del calendario admin: blocco date e prezzi per notte."""
import pytest

from test_api import d
from test_booking_requests import request_payload


@pytest.mark.parametrize('method,path', [
    ('get', '/api/admin/calendar?start=2030-01-01&end=2030-02-01'),
    ('put', '/api/admin/calendar/prices'), ('post', '/api/admin/blocks'), ('delete', '/api/admin/blocks/x'),
])
def test_calendar_requires_auth(client, method, path):
    assert getattr(client, method)(path).status_code in (401, 403)


def test_block_dates_lifecycle(client, auth, mongo):
    r = client.post('/api/admin/blocks', headers=auth, json={'check_in': d(20), 'check_out': d(23), 'note': 'Uso personale'})
    assert r.status_code == 200, r.text
    block = r.json()['block']
    # non prenotabili dal sito
    blocked = client.get('/api/availability', params={'start': d(20), 'end': d(25)}).json()['blocked_dates']
    assert blocked == [d(20), d(21), d(22)]
    assert client.post('/api/quote', params={'check_in': d(21), 'check_out': d(24)}).json()['available'] is False
    assert client.post('/api/bookings/request', json=request_payload(21, 24)).status_code == 409
    # esportati come occupati verso Airbnb/Booking
    assert f"{block['id']}@lightblue-anguillara".encode() in client.get('/api/ical/export.ics').content
    # non si sovrappongono ad altri blocchi o prenotazioni
    assert client.post('/api/admin/blocks', headers=auth, json={'check_in': d(22), 'check_out': d(26)}).status_code == 409
    # non compaiono nelle statistiche come prenotazioni
    totals = client.get('/api/admin/analytics', headers=auth).json()['totals']
    assert totals['confirmed_bookings'] == 0 and totals['nights'] == 0
    # rimozione
    assert client.delete(f"/api/admin/blocks/{block['id']}", headers=auth).status_code == 200
    assert client.get('/api/availability', params={'start': d(20), 'end': d(25)}).json()['blocked_dates'] == []
    assert client.delete(f"/api/admin/blocks/{block['id']}", headers=auth).status_code == 404


def test_block_cannot_overlap_request(client, auth):
    client.post('/api/bookings/request', json=request_payload(40, 43))
    r = client.post('/api/admin/blocks', headers=auth, json={'check_in': d(42), 'check_out': d(45)})
    assert r.status_code == 409 and 'Mario Rossi' in r.json()['detail']


@pytest.mark.parametrize('body', [
    {'check_in': d(-3), 'check_out': d(-1)},     # passato
    {'check_in': d(10), 'check_out': d(10)},     # zero notti
    {'check_in': 'x', 'check_out': d(10)},       # formato
])
def test_block_validation(client, auth, body):
    assert client.post('/api/admin/blocks', headers=auth, json=body).status_code == 422


def test_delete_block_only_deletes_blocks(client, auth, mongo):
    mongo.bookings.insert_one({'id': 'real', 'status': 'confirmed', 'check_in': d(5), 'check_out': d(7)})
    assert client.delete('/api/admin/blocks/real', headers=auth).status_code == 404
    assert mongo.bookings.count_documents({'id': 'real'}) == 1


def test_calendar_prices_and_custom_override(client, auth):
    client.put('/api/admin/settings', headers=auth, json={'seasonal_rates': [
        {'name': 'Natale', 'start_date': '12-20', 'end_date': '12-31', 'price_per_night': 200, 'priority': 5}]})
    days = client.get('/api/admin/calendar', headers=auth, params={'start': '2030-12-18', 'end': '2030-12-22'}).json()['days']
    assert [x['price'] for x in days] == [120, 120, 200, 200]
    assert days[2]['source'] == 'seasonal' and days[2]['rate_name'] == 'Natale'

    # prezzo personalizzato per due notti
    r = client.put('/api/admin/calendar/prices', headers=auth, json={'start': '2030-12-19', 'end': '2030-12-21', 'price': 150})
    assert r.json()['nights'] == 2
    days = client.get('/api/admin/calendar', headers=auth, params={'start': '2030-12-18', 'end': '2030-12-22'}).json()['days']
    assert [x['price'] for x in days] == [120, 150, 150, 200]
    assert days[1]['source'] == 'custom' and days[2]['base_price'] == 200

    # ripristino
    client.put('/api/admin/calendar/prices', headers=auth, json={'start': '2030-12-19', 'end': '2030-12-21', 'price': None})
    days = client.get('/api/admin/calendar', headers=auth, params={'start': '2030-12-18', 'end': '2030-12-22'}).json()['days']
    assert [x['price'] for x in days] == [120, 120, 200, 200]


def test_custom_price_used_in_guest_quote(client, auth, mongo):
    mongo.price_overrides.delete_many({})
    client.put('/api/admin/calendar/prices', headers=auth, json={'start': d(60), 'end': d(61), 'price': 300})
    q = client.post('/api/quote', params={'check_in': d(60), 'check_out': d(63)}).json()
    assert q['refundable_total'] == 300 + 120 + 120
    mongo.price_overrides.delete_many({})


@pytest.mark.parametrize('body', [
    {'start': d(5), 'end': d(5), 'price': 100},
    {'start': d(5), 'end': d(6), 'price': -1},
    {'start': d(5), 'end': d(6), 'price': 10 ** 7},
])
def test_price_validation(client, auth, body):
    assert client.put('/api/admin/calendar/prices', headers=auth, json=body).status_code == 422


def test_calendar_range_validation(client, auth):
    assert client.get('/api/admin/calendar', headers=auth, params={'start': '2030-01-01', 'end': '2032-01-01'}).status_code == 400
