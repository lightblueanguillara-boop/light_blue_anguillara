"""Backup dei dati: download, contenuto completo, ripristino, invio automatico per email."""
import base64
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import backup
from test_api import d

ROME = ZoneInfo('Europe/Rome')
LOGO = b'\x89PNG\r\n\x1a\n' + bytes(range(256))


def _seed(mongo):
    mongo.bookings.insert_one({'id': 'b1', 'guest_name': 'Lucia Verdi', 'guest_email': 'lucia@example.com',
                               'check_in': d(10), 'check_out': d(13), 'status': 'confirmed', 'total_price': 360.5})
    mongo.contact_messages.insert_one({'id': 'm1', 'name': 'Elena', 'message': 'Ciao, è libero?'})
    mongo.brand_assets.insert_one({'kind': 'logo', 'version': 'abc', 'files': {'logo_png': LOGO}})
    mongo.push_outbox.insert_one({'id': 'temp', 'message': {}})


@pytest.fixture()
def restore_db(mongo):
    name = os.environ['DB_NAME'].removesuffix('_test') + '_restore_test'
    target = mongo.client[name]
    mongo.client.drop_database(name)
    yield target
    mongo.client.drop_database(name)


def test_backup_requires_auth(client):
    assert client.get('/api/admin/backup').status_code in (401, 403)
    assert client.post('/api/admin/backup/email').status_code in (401, 403)


def test_download_contains_everything(client, auth, mongo):
    _seed(mongo)
    r = client.get('/api/admin/backup', headers=auth)
    assert r.status_code == 200 and r.headers['content-type'] == 'application/gzip'
    assert 'attachment; filename="light-blue-backup-' in r.headers['content-disposition']
    doc = backup.read_backup(r.content)
    cols = doc['collections']
    assert cols['bookings'][0]['guest_name'] == 'Lucia Verdi' and cols['bookings'][0]['total_price'] == 360.5
    assert cols['contact_messages'][0]['message'] == 'Ciao, è libero?'
    assert cols['brand_assets'][0]['files']['logo_png'] == LOGO          # immagini conservate byte per byte
    assert 'admins' in cols and 'settings' in cols
    assert 'push_outbox' not in cols
    assert doc['meta']['counts']['bookings'] == 1


def test_restore_roundtrip(client, auth, mongo, restore_db):
    from scripts.restore_backup import restore
    _seed(mongo)
    data = client.get('/api/admin/backup', headers=auth).content
    counts = restore(data, restore_db)
    assert counts['bookings'] == 1
    for col in ('bookings', 'contact_messages', 'brand_assets', 'settings', 'admins'):
        assert list(restore_db[col].find().sort('_id')) == list(mongo[col].find().sort('_id')), col


def test_restore_refuses_to_overwrite(client, auth, mongo, restore_db):
    from scripts.restore_backup import restore
    _seed(mongo)
    data = client.get('/api/admin/backup', headers=auth).content
    restore_db.bookings.insert_one({'id': 'esistente'})
    with pytest.raises(RuntimeError, match='bookings'):
        restore(data, restore_db)
    assert restore_db.bookings.count_documents({}) == 1                  # nulla toccato
    restore(data, restore_db, replace=True)
    assert [b['id'] for b in restore_db.bookings.find()] == ['b1']


def test_restore_rejects_other_files():
    import gzip
    with pytest.raises(ValueError):
        backup.read_backup(gzip.compress(b'{"hello": 1}'))


def test_email_backup_with_attachment(client, auth, mongo, sent_emails):
    _seed(mongo)
    r = client.post('/api/admin/backup/email', headers=auth).json()
    assert r['ok'] is True and r['size'] > 0
    mail = sent_emails[-1]
    assert mail['to'] == ['notify@example.com'] and 'Backup' in mail['subject']
    att = mail['attachments'][0]
    assert att['filename'].endswith('.json.gz')
    assert backup.read_backup(base64.b64decode(att['content']))['collections']['bookings'][0]['id'] == 'b1'
    s = client.get('/api/admin/settings', headers=auth).json()
    assert s['last_backup_email_at'] and s['last_backup_error'] is None and s['last_backup_size'] == r['size']


def test_email_backup_failure_is_recorded(client, auth, mongo, monkeypatch):
    import resend

    def boom(params):
        raise RuntimeError('resend down')
    monkeypatch.setattr(resend.Emails, 'send', boom)
    r = client.post('/api/admin/backup/email', headers=auth).json()
    assert r['ok'] is False
    assert client.get('/api/admin/settings', headers=auth).json()['last_backup_error']


def _at(hour, day=0):
    """Un istante in ora italiana, `day` giorni da oggi."""
    base = datetime.now(ROME).replace(hour=hour, minute=10, second=0, microsecond=0) + timedelta(days=day)
    return base.astimezone(timezone.utc)


def _run(client, now):
    return client.portal.call(backup.run_scheduled_backup, now)


def test_scheduled_backup_weekly(client, mongo, sent_emails):
    assert _run(client, _at(2)) is False                   # prima delle 3 di notte: niente
    assert _run(client, _at(4)) is True                    # primo backup
    assert _run(client, _at(4, 1)) is False                # il giorno dopo: no (settimanale)
    assert _run(client, _at(4, 6)) is False
    assert _run(client, _at(4, 7)) is True                 # una settimana dopo: sì
    assert len([m for m in sent_emails if 'Backup' in m['subject']]) == 2


def test_scheduled_backup_daily_and_off(client, mongo, sent_emails):
    mongo.settings.update_one({'id': 'global'}, {'$set': {'backup_email_frequency': 'daily'}}, upsert=True)
    assert _run(client, _at(3)) is True
    assert _run(client, _at(5)) is False                   # stesso giorno: no
    assert _run(client, _at(3, 1)) is True                 # giorno dopo: sì
    mongo.settings.update_one({'id': 'global'}, {'$set': {'backup_email_frequency': 'off'}})
    assert _run(client, _at(3, 9)) is False


def test_scheduled_backup_retries_after_failure(client, mongo, monkeypatch):
    import resend
    monkeypatch.setattr(resend.Emails, 'send', lambda p: (_ for _ in ()).throw(RuntimeError('x')))
    assert _run(client, _at(3)) is True                    # tentativo fallito
    assert _run(client, _at(5)) is False                   # dopo 2 ore: ancora no
    assert _run(client, _at(10)) is True                   # dopo 7 ore: riprova (non aspetta una settimana)


def test_backup_frequency_validation(client, auth):
    assert client.put('/api/admin/settings', headers=auth, json={'backup_email_frequency': 'hourly'}).status_code == 422
    r = client.put('/api/admin/settings', headers=auth, json={'backup_email_frequency': 'daily'})
    assert r.status_code == 200 and r.json()['backup_email_frequency'] == 'daily'


def test_restore_script_works_without_server_settings(client, auth, mongo, restore_db, tmp_path):
    """In un'emergenza si ha solo il file: il ripristino non deve chiedere JWT_SECRET & co."""
    import subprocess
    import sys
    from pathlib import Path
    _seed(mongo)
    f = tmp_path / 'backup.json.gz'
    f.write_bytes(client.get('/api/admin/backup', headers=auth).content)
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'restore_backup.py'
    run = lambda *extra: subprocess.run(
        [sys.executable, str(script), str(f), '--mongo-url', os.environ['MONGO_URL'], '--db', restore_db.name, *extra],
        capture_output=True, text=True, env={'PATH': os.environ.get('PATH', '')}, timeout=60)
    first = run()
    assert first.returncode == 0, first.stdout + first.stderr
    assert restore_db.bookings.count_documents({}) == 1
    second = run()                                         # non sovrascrive
    assert second.returncode == 1 and 'Fermato' in second.stdout
    bad = tmp_path / 'altro.gz'
    bad.write_bytes(b'non sono un backup')
    out = subprocess.run([sys.executable, str(script), str(bad), '--mongo-url', 'mongodb://x', '--db', 'x'],
                         capture_output=True, text=True, env={'PATH': os.environ.get('PATH', '')}, timeout=60)
    assert out.returncode == 1 and 'non è un file di backup' in out.stdout and 'Traceback' not in out.stderr
