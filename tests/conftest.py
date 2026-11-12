"""Configurazione dei test: app avviata in-process su un database Mongo LOCALE di test.

Stripe e Resend sono sostituiti da finti oggetti: nessun pagamento o email reale.
"""
import os
import sys
from pathlib import Path

# --- Ambiente di test (prima di importare l'app) ------------------------------
os.environ.setdefault('MONGO_URL', 'mongodb://localhost:27017')
os.environ.setdefault('DB_NAME', 'light_blue_test')
os.environ['JWT_SECRET'] = 'test-secret-' + 'x' * 40
os.environ['STRIPE_WEBHOOK_SECRET'] = 'whsec_test'
os.environ['STRIPE_API_KEY'] = 'sk_test_fake'
os.environ['ADMIN_SEED_EMAIL'] = 'admin@example.com'
os.environ['ADMIN_SEED_PASSWORD'] = 'password-di-test-123'
os.environ['ADMIN_NOTIFY_EMAIL'] = 'notify@example.com'
os.environ['FRONTEND_URL'] = 'https://www.example-site.test'
os.environ['CORS_ORIGINS'] = 'http://localhost:3000'

# Protezione: mai eseguire i test su un database che non sia locale e di test.
_url, _name = os.environ['MONGO_URL'], os.environ['DB_NAME']
if not (('localhost' in _url or '127.0.0.1' in _url) and _name.endswith('_test')):
    raise SystemExit(f'Test bloccati: database non locale o non di test ({_name})')

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
import stripe  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pymongo import MongoClient  # noqa: E402

ADMIN = {'email': 'admin@example.com', 'password': 'password-di-test-123'}


class FakeStripe:
    """Simula le API Stripe usate dal backend."""

    def __init__(self):
        self.sessions = {}
        self.fail_create = False
        self.counter = 0

    def create(self, **kw):
        if self.fail_create:
            raise stripe.error.AuthenticationError('Invalid API Key provided: sk_test_****fake')
        self.counter += 1
        sid = f'cs_test_{self.counter}'
        s = {'id': sid, 'url': f'https://checkout.stripe.test/{sid}', 'status': 'open',
             'payment_status': 'unpaid', 'payment_intent': None, 'kw': kw}
        self.sessions[sid] = s
        return _Obj(s)

    def retrieve(self, sid):
        if sid not in self.sessions:
            raise stripe.error.InvalidRequestError('No such session', 'id')
        return _Obj(self.sessions[sid])

    def expire(self, sid):
        s = self.sessions.get(sid)
        if not s or s['status'] != 'open':
            raise stripe.error.InvalidRequestError('Session not open', 'id')
        s['status'] = 'expired'
        return _Obj(s)

    def pay(self, sid):
        self.sessions[sid].update(status='complete', payment_status='paid', payment_intent=f'pi_{sid}')


class _Obj:
    def __init__(self, d):
        self.__dict__.update(d)


@pytest.fixture(scope='session')
def mongo():
    m = MongoClient(os.environ['MONGO_URL'])[os.environ['DB_NAME']]
    m.client.drop_database(os.environ['DB_NAME'])
    yield m
    m.client.drop_database(os.environ['DB_NAME'])


@pytest.fixture(scope='session')
def app_client(mongo):
    from server import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def fake_stripe(monkeypatch):
    fs = FakeStripe()
    monkeypatch.setattr(stripe.checkout.Session, 'create', fs.create)
    monkeypatch.setattr(stripe.checkout.Session, 'retrieve', fs.retrieve)
    monkeypatch.setattr(stripe.checkout.Session, 'expire', fs.expire)
    return fs


@pytest.fixture()
def sent_emails(monkeypatch):
    import resend
    box = []
    monkeypatch.setattr(resend.Emails, 'send', lambda params: box.append(params) or {'id': 'x'})
    return box


@pytest.fixture()
def client(app_client, mongo, fake_stripe, sent_emails):
    """Client con database pulito (tranne admin/settings) e limiti di richieste azzerati."""
    import ratelimit
    ratelimit.reset_all()
    for col in ('bookings', 'payment_transactions', 'contact_messages', 'subscribers',
                'unsubscribed', 'marketing_logs', 'gallery'):
        mongo[col].delete_many({})
    mongo.settings.delete_many({})
    mongo.admins.update_many({}, {'$set': {'token_version': 0}})
    return app_client


@pytest.fixture()
def auth(client):
    r = client.post('/api/admin/login', json=ADMIN)
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}
