"""TikTok: sign-in, mirroring grid Reels, chunked uploads and status checks (against a mock API)."""
import json
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo
import httpx
import pytest
from fastapi.testclient import TestClient
from app import tiktok, schedule as hand, config as c
from app.db import connect, secret
from app.main import app
from app.planner import plan
from conftest import seed, set_settings

REAL_CLIENT = httpx.Client

class FakeTikTok:
    def __init__(self):
        self.requests, self.status = [], 'PROCESSING_UPLOAD'
    def __call__(self, request):
        url = str(request.url)
        self.requests.append((request.method, url, request.headers, request.content))
        if url.endswith('/oauth/token/'):
            form = parse_qs(request.content.decode())
            return httpx.Response(200, json={'access_token': 'at-'+form['grant_type'][0], 'refresh_token': 'rt', 'open_id': 'open1',
                                             'expires_in': 86400, 'refresh_expires_in': 31536000})
        if '/user/info/' in url:
            return httpx.Response(200, json={'data': {'user': {'display_name': 'TRADES'}}})
        if url.endswith('/creator_info/query/'):
            return httpx.Response(200, json={'data': {'creator_nickname': 'TRADES', 'creator_username': 'trades', 'privacy_level_options':
                ['PUBLIC_TO_EVERYONE', 'SELF_ONLY'], 'comment_disabled': False, 'duet_disabled': False, 'stitch_disabled': False,
                'max_video_post_duration_sec': 600}, 'error': {'code': 'ok'}})
        if url.endswith('/post/publish/video/init/'):
            return httpx.Response(200, json={'data': {'publish_id': 'pub1', 'upload_url': 'https://upload.tiktok.test/u1'}, 'error': {'code': 'ok'}})
        if url.startswith('https://upload.tiktok.test/'):
            return httpx.Response(201)
        if url.endswith('/post/publish/status/fetch/'):
            return httpx.Response(200, json={'data': {'status': self.status, 'publicaly_available_post_id': [7123]}, 'error': {'code': 'ok'}})
        return httpx.Response(404)

@pytest.fixture
def fake(monkeypatch):
    api = FakeTikTok()
    original = httpx.Client
    monkeypatch.setattr(tiktok.httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(api), **kw))
    return api

def connect_fake():
    secret('tiktok_app', json.dumps({'client_key': 'ck123', 'client_secret': 'secret123'}))
    with connect(True) as db:
        tiktok.store_tokens(db, {'access_token': 'at', 'refresh_token': 'rt', 'open_id': 'o', 'expires_in': 86400, 'refresh_expires_in': 9e6}, 'TRADES')

def test_chunk_rules():
    MB = 1024*1024
    assert tiktok.chunks(3*MB) == (3*MB, 1) and tiktok.chunks(64*MB) == (64*MB, 1)
    assert tiktok.chunks(200*MB) == (64*MB, 3)  # The last piece takes the remaining 8 MB too.

def test_sign_in_uses_pkce_and_stores_the_account(monkeypatch):
    with TestClient(app) as client:
        assert client.post('/api/login', json={'password': c.PASSWORD}).status_code == 200
        h = {'x-csrf-token': client.get('/api/state').json()['csrf']}
        assert client.get('/auth/tiktok/start', follow_redirects=False).status_code == 409
        assert client.put('/api/tiktok/app', headers=h, json={'client_key': 'ck123', 'client_secret': 'secret123'}).json()['app'] is True
        start = client.get('/auth/tiktok/start', follow_redirects=False)
        query = parse_qs(urlparse(start.headers['location']).query)
        assert query['client_key'] == ['ck123'] and query['code_challenge_method'] == ['S256'] and 'video.publish' in query['scope'][0]
        api = FakeTikTok()
        original = httpx.Client
        monkeypatch.setattr(tiktok.httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(api), **kw))
        r = client.get(f"/auth/tiktok/callback?code=abc&state={query['state'][0]}", follow_redirects=False)
        assert r.status_code == 303
        token_form = parse_qs(api.requests[0][3].decode())
        assert token_form['code_verifier'][0] and token_form['client_secret'] == ['secret123']
        state = client.get('/api/tiktok').json()
        assert state['account']['display_name'] == 'TRADES' and 'privacy_level' not in state['preferences']
        assert client.put('/api/tiktok/preferences', headers=h, json={**state['preferences'], 'paused': 'yes'}).status_code == 422

def test_grid_reels_are_mirrored_and_follow_moves(fake):
    ids = seed(2)
    with connect(True) as db:
        db.execute('UPDATE clips SET duration=20')
    connect_fake()
    set_settings(conserve=False, min_gap_days=0)
    plan()
    with connect() as db:
        grid = db.execute("SELECT * FROM jobs WHERE lane='main' AND clip_id=?", (ids[0],)).fetchone()
        post = db.execute('SELECT * FROM tiktok_jobs WHERE clip_id=?', (ids[0],)).fetchone()
    assert post['mirror_of'] == grid['id'] and post['scheduled_at'] == grid['scheduled_at']
    day = (datetime.now(ZoneInfo('Australia/Perth')) + timedelta(days=4)).date().isoformat()
    hand.place('job', grid['id'], 'main', day)
    with connect() as db:
        assert db.execute('SELECT scheduled_at FROM tiktok_jobs WHERE id=?', (post['id'],)).fetchone()[0] == \
               db.execute('SELECT scheduled_at FROM jobs WHERE id=?', (grid['id'],)).fetchone()[0]
    hand.place('tiktok', post['id'], 'tiktok', day)
    with connect() as db:
        moved = db.execute('SELECT mirror_of,forced FROM tiktok_jobs WHERE id=?', (post['id'],)).fetchone()
    assert moved['mirror_of'] is None and moved['forced'] == 1
    hand.hold('tiktok', post['id'])
    assert hand.tray()['drafts'][0]['kind'] == 'tiktok'

def test_upload_in_chunks_then_publish(fake):
    cid = seed(1)[0]
    (c.DATA/'media'/(cid+'.mp4')).write_bytes(b'x'*1000)
    connect_fake()
    with connect(True) as db:
        tiktok.save_preferences(db, tiktok.Preferences(paused=False, caption_suffix='Link in bio'))
        db.execute("INSERT INTO tiktok_jobs(clip_id,caption,status,scheduled_at,created_at) VALUES(?,?,'queued',?,?)", (cid, 'Late one.', time.time()-10, time.time()))
    assert not tiktok.publish_due()  # Nothing goes to TikTok until it is approved.
    with connect() as db:
        job_id = db.execute('SELECT id FROM tiktok_jobs').fetchone()[0]
    with pytest.raises(tiktok.TikTokError, match='Choose who can see'):
        tiktok.approve([job_id], {'privacy': ''})
    with pytest.raises(tiktok.TikTokError, match='can’t be private'):
        tiktok.approve([job_id], {'privacy': 'SELF_ONLY', 'disclose': True, 'brand_paid': True})
    tiktok.approve([job_id], {'privacy': 'SELF_ONLY', 'comment': True, 'disclose': True, 'brand_own': True})
    assert tiktok.publish_due()
    init = next(r for r in fake.requests if r[1].endswith('/video/init/'))
    body = json.loads(init[3])
    assert body['post_info'] == {'title': 'Late one.\n\nLink in bio', 'privacy_level': 'SELF_ONLY', 'disable_comment': False,
                                 'disable_duet': True, 'disable_stitch': True, 'brand_organic_toggle': True, 'brand_content_toggle': False}
    assert body['source_info'] == {'source': 'FILE_UPLOAD', 'video_size': 1000, 'chunk_size': 1000, 'total_chunk_count': 1}
    put = next(r for r in fake.requests if r[0] == 'PUT')
    assert put[2]['content-range'] == 'bytes 0-999/1000' and len(put[3]) == 1000
    with connect(True) as db:
        db.execute('UPDATE tiktok_jobs SET next_attempt=0')
    fake.status = 'PUBLISH_COMPLETE'
    tiktok.publish_due()
    with connect() as db:
        row = db.execute('SELECT * FROM tiktok_jobs').fetchone()
    assert row['status'] == 'published' and row['post_id'] == '7123'

def test_refused_uploads_say_why_and_tokens_refresh(fake, monkeypatch):
    cid = seed(1)[0]
    (c.DATA/'media'/(cid+'.mp4')).write_bytes(b'x')
    connect_fake()
    with connect(True) as db:
        db.execute('UPDATE tiktok_account SET expires_at=?', (time.time()+60,))
        tiktok.save_preferences(db, tiktok.Preferences(paused=False))
        db.execute("INSERT INTO tiktok_jobs(clip_id,caption,status,scheduled_at,created_at,approved,privacy) VALUES(?,'c','queued',?,?,1,'SELF_ONLY')", (cid, time.time()-1, time.time()))
    assert tiktok.access_token() == 'at-refresh_token'
    def refuse(request):
        if str(request.url).endswith('/video/init/'):
            return httpx.Response(403, json={'error': {'code': 'unaudited_client_can_only_post_to_private_accounts', 'message': 'Use private.'}})
        return fake(request)
    monkeypatch.setattr(tiktok.httpx, 'Client', lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(refuse), **kw))
    tiktok.publish_due()
    with connect() as db:
        row = db.execute('SELECT status,error FROM tiktok_jobs').fetchone()
    assert row['status'] == 'failed' and 'unaudited_client' in row['error']


@pytest.fixture(autouse=True)
def fresh_creator_cache():
    tiktok._creator.update(at=0, data=None)
    yield
    tiktok._creator.update(at=0, data=None)

def test_videos_over_the_accounts_limit_are_refused_before_upload(fake, monkeypatch):
    cid = seed(1)[0]
    (c.DATA/'media'/(cid+'.mp4')).write_bytes(b'x')
    connect_fake()
    with connect(True) as db:
        db.execute('UPDATE clips SET duration=900 WHERE id=?', (cid,))
        tiktok.save_preferences(db, tiktok.Preferences(paused=False))
        db.execute("INSERT INTO tiktok_jobs(clip_id,caption,status,scheduled_at,created_at,approved,privacy) VALUES(?,'c','queued',?,?,1,'SELF_ONLY')", (cid, time.time()-1, time.time()))
    tiktok.publish_due()
    with connect() as db:
        row = db.execute('SELECT status,error FROM tiktok_jobs').fetchone()
    assert row['status'] == 'failed' and 'up to 600 seconds' in row['error']
    assert not any(r[1].endswith('/video/init/') for r in fake.requests)

def test_public_pages_and_the_verification_file_need_no_sign_in():
    set_settings(legal_name='TRADES', legal_email='hello@trades.test', tiktok_verify_file='tiktokAbC123.txt',
                 tiktok_verify_text='tiktok-developers-site-verification=AbC123')
    with TestClient(app) as client:
        for path, text in (('/about', 'TRADES publishing desk'), ('/legal/terms', 'Terms of Service'), ('/legal/privacy', 'video.publish')):
            r = client.get(path)
            assert r.status_code == 200 and text in r.text and 'hello@trades.test' in r.text
        r = client.get('/tiktokAbC123.txt')
        assert r.status_code == 200 and r.text == 'tiktok-developers-site-verification=AbC123'
        assert client.get('/tiktokOther.txt').status_code == 404
        assert client.get('/api/state').status_code == 401  # Everything else still needs the password.
        assert client.post('/api/login', json={'password': c.PASSWORD}).status_code == 200
        h = {'x-csrf-token': client.get('/api/state').json()['csrf']}
        assert client.patch('/api/settings', headers=h, json={'values': {'legal_email': 'not-an-email'}}).status_code == 422
        assert client.patch('/api/settings', headers=h, json={'values': {'tiktok_verify_file': '../etc/passwd'}}).status_code == 422
