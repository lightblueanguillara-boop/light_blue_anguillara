"""Test di logo, immagine di anteprima e manifest dell'app."""
import io

import pytest
from PIL import Image


def _img(fmt='PNG', size=(600, 600), color=(30, 120, 200)):
    buf = io.BytesIO()
    Image.new('RGB', size, color).save(buf, fmt)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _clean_brand(mongo):
    mongo.brand_assets.delete_many({})
    yield
    mongo.brand_assets.delete_many({})


@pytest.mark.parametrize('name,ctype,size', [
    ('favicon.ico', 'image/x-icon', None), ('logo-32.png', 'image/png', 32), ('logo-180.png', 'image/png', 180),
    ('logo-192.png', 'image/png', 192), ('logo-512.png', 'image/png', 512), ('logo-maskable-512.png', 'image/png', 512),
    ('share.jpg', 'image/jpeg', None),
])
def test_default_files_served(client, name, ctype, size):
    r = client.get(f'/api/brand/{name}')
    assert r.status_code == 200 and r.headers['content-type'] == ctype
    img = Image.open(io.BytesIO(r.content))
    if size:
        assert img.size == (size, size)
    if name == 'share.jpg':
        assert img.size == (1200, 630)          # formato anteprima WhatsApp/Facebook


def test_unknown_brand_file_404(client):
    assert client.get('/api/brand/../../etc/passwd').status_code == 404
    assert client.get('/api/brand/secret.png').status_code == 404


def test_upload_logo_changes_everything(client, auth, mongo):
    before = client.get('/api/brand/logo-192.png').content
    r = client.post('/api/admin/brand/logo', headers=auth, files={'file': ('logo.png', _img(), 'image/png')})
    assert r.status_code == 200, r.text
    v = r.json()['version']
    assert client.get('/api/villa/info').json()['logo_version'] == v
    after = client.get(f'/api/brand/logo-192.png?v={v}')
    assert after.content != before
    assert 'immutable' in after.headers['cache-control']        # con versione: cache lunga
    assert Image.open(io.BytesIO(after.content)).getpixel((96, 96))[:3] == (30, 120, 200)
    # le email usano la nuova versione
    from messaging import compose_email
    html, _ = compose_email(client.get('/api/admin/settings', headers=auth).json(), 'Prova', 'Ciao X')
    assert f'/api/brand/logo-256.png?v={v}' in html
    # ripristino del logo predefinito
    assert client.delete('/api/admin/brand/logo', headers=auth).status_code == 200
    assert client.get('/api/villa/info').json()['logo_version'] == 'default'
    assert client.get('/api/brand/logo-192.png').content == before


def test_upload_share_image(client, auth):
    r = client.post('/api/admin/brand/share', headers=auth, files={'file': ('casa.jpg', _img('JPEG', (2000, 1500)), 'image/jpeg')})
    assert r.status_code == 200
    img = Image.open(io.BytesIO(client.get('/api/brand/share.jpg').content))
    assert img.size == (1200, 630) and not img.info.get('exif')   # niente metadati della foto


@pytest.mark.parametrize('kind,payload,ctype', [
    ('logo', b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', 'image/svg+xml'),
    ('logo', b'non sono un\'immagine', 'image/png'),
    ('logo', None, 'image/gif'),
    ('logo', 'small', 'image/png'),
    ('share', 'small', 'image/png'),
])
def test_upload_rejects_bad_images(client, auth, kind, payload, ctype):
    if payload is None:
        payload = _img('GIF')
    elif payload == 'small':
        payload = _img('PNG', (100, 100))
    r = client.post(f'/api/admin/brand/{kind}', headers=auth, files={'file': ('x', payload, ctype)})
    assert r.status_code == 400
    assert isinstance(r.json()['detail'], str)


def test_upload_rejects_huge_file(client, auth):
    big = b'\x89PNG' + b'0' * (8 * 1024 * 1024 + 10)
    assert client.post('/api/admin/brand/logo', headers=auth, files={'file': ('x.png', big, 'image/png')}).status_code == 413


@pytest.mark.parametrize('method,path', [
    ('post', '/api/admin/brand/logo'), ('post', '/api/admin/brand/share'), ('delete', '/api/admin/brand/logo'),
])
def test_brand_admin_requires_auth(client, method, path):
    assert getattr(client, method)(path).status_code in (401, 403)


def test_brand_versions_not_editable_from_settings(client, auth):
    client.put('/api/admin/settings', headers=auth, json={'logo_version': 'hack', 'villa_name': 'Light Blue'})
    assert client.get('/api/villa/info').json()['logo_version'] != 'hack'


def test_splash_images(client):
    import brand
    r = client.get('/api/brand/splash-1179x2556.png')
    assert r.status_code == 200 and r.headers['content-type'] == 'image/png'
    img = Image.open(io.BytesIO(r.content)).convert('RGB')
    assert img.size == (1179, 2556)
    assert img.getpixel((10, 10)) == brand.APP_BG_COLOR            # niente sfondo bianco o nero
    assert img.getpixel((589, 1278)) != brand.APP_BG_COLOR         # logo al centro
    assert client.get('/api/brand/splash-100x100.png').status_code == 404


def test_splash_follows_logo(client, auth):
    before = client.get('/api/brand/splash-750x1334.png').content
    client.post('/api/admin/brand/logo', headers=auth, files={'file': ('l.png', _img(), 'image/png')})
    assert client.get('/api/brand/splash-750x1334.png').content != before
