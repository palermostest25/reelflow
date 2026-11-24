"""Collaborators: who gets invited, when signed-in accounts accept, and the sign-in route."""
import time
import pytest
from fastapi.testclient import TestClient
from app import collab, worker, instagram as ig, config as c
from app.db import connect, settings, secret
from app.main import app
from conftest import seed, set_settings, connected

REAL_CREATE = ig.create_container

def sign_in(username, ig_id):
    secret('collab_token:'+ig_id, 'token-'+username)
    with connect(True) as db:
        db.execute("INSERT INTO collab_accounts(username,ig_id,expires_at,connected_at) VALUES(?,?,?,?)", (username, ig_id, time.time()+50*86400, time.time()))

def test_settings_are_cleaned_and_checked():
    s = {**settings(), 'collab_accounts': [{'username': '@Second.Acct'}, {'username': 'second.acct'}, {'username': 'third_acct', 'auto_accept': False}]}
    collab.validate(s)
    assert s['collab_accounts'] == [{'username': 'second.acct', 'auto_accept': True}, {'username': 'third_acct', 'auto_accept': False}]
    for bad in ({'collab_accounts': [{'username': 'not a name'}]}, {'collab_per_post': 4}, {'collab_order': 'alphabetical'}, {'collab_gap_hours': -1}):
        with pytest.raises(ValueError):
            collab.validate({**settings(), **bad})
    connected()
    with pytest.raises(ValueError, match='its own collaborator'):
        collab.validate({**settings(), 'collab_accounts': [{'username': 'trades'}]})

def test_each_reel_invites_the_next_accounts_in_turn():
    set_settings(collab_enabled=True, collab_per_post=2, collab_accounts=[{'username': u, 'auto_accept': True} for u in ('a1', 'b2', 'c3')])
    with connect(True) as db:
        picks = [collab.pick(settings(db), db) for _ in range(3)]
    assert picks == [['a1', 'b2'], ['c3', 'a1'], ['b2', 'c3']]
    set_settings(collab_enabled=False)
    with connect(True) as db:
        assert collab.pick(settings(db), db) == []

def test_signed_in_accounts_accept_in_order_after_the_delays(monkeypatch):
    set_settings(collab_enabled=True, collab_first_hours=24, collab_gap_hours=12, collab_order='listed',
                 collab_accounts=[{'username': 'one', 'auto_accept': True}, {'username': 'two', 'auto_accept': True},
                                  {'username': 'three', 'auto_accept': False}])
    sign_in('one', '111'); sign_in('two', '222')
    published = 1_000_000.0
    collab.schedule_accepts(7, 'M1', ['one', 'two', 'three'], published)
    rows = {r['username']: r for r in collab.for_job(7)}
    assert rows['one']['due_at'] == published+24*3600 and rows['two']['due_at'] == published+36*3600
    assert rows['three']['status'] == 'manual'
    calls = []
    def fake(method, path, token=None, **kw):
        calls.append((method, path, token, kw))
        if method == 'GET':
            return {'data': [{'id': 'inv', 'media_id': 'M1'}]} if token == 'token-one' else {'data': []}
        return {'success': True}
    monkeypatch.setattr(ig, 'request', fake)
    collab.accept_due(now=published+24*3600+1)
    rows = {r['username']: r for r in collab.for_job(7)}
    assert rows['one']['status'] == 'accepted' and rows['two']['status'] == 'pending'
    assert ('POST', '111/collaboration_invites', 'token-one', {'data': {'media_id': 'M1', 'accept': 'true'}}) in calls
    # The second invite hasn't arrived yet: it waits and tries again.
    collab.accept_due(now=published+36*3600+1)
    row = {r['username']: r for r in collab.for_job(7)}['two']
    assert row['status'] == 'pending' and 'Waiting' in row['error']
    def refused(method, path, token=None, **kw):
        raise ig.MetaError('Instagram rejected the request (HTTP 400, code 10): not allowed.', 10)
    monkeypatch.setattr(ig, 'request', refused)
    collab.accept_due(now=published+40*3600)
    assert {r['username']: r for r in collab.for_job(7)}['two']['status'] == 'failed'

def test_random_order_still_spaces_the_accepts(monkeypatch):
    set_settings(collab_order='random', collab_first_hours=0, collab_gap_hours=1,
                 collab_accounts=[{'username': u, 'auto_accept': True} for u in ('x1', 'x2', 'x3')])
    for i, u in enumerate(('x1', 'x2', 'x3')):
        sign_in(u, str(900+i))
    collab.schedule_accepts(9, 'M9', ['x1', 'x2', 'x3'], 0.0)
    assert sorted(r['due_at'] for r in collab.for_job(9)) == [0.0, 3600.0, 7200.0]

def test_grid_reels_carry_collaborators_and_schedule_acceptance(monkeypatch):
    cid = seed(1)[0]
    connected()
    set_settings(paused=False, dry_run=False, conserve=False, collab_enabled=True, collab_per_post=2,
                 collab_accounts=[{'username': 'one', 'auto_accept': True}, {'username': 'two', 'auto_accept': True}])
    sign_in('one', '111')
    with connect(True) as db:
        db.execute('UPDATE clips SET media_version=? WHERE id=?', (worker.media.MEDIA_VERSION, cid))
        main_id = db.execute("INSERT INTO jobs(clip_id,lane,caption,scheduled_at) VALUES(?,'main','Cap',?)", (cid, time.time()-30)).lastrowid
        trial_id = db.execute("INSERT INTO jobs(clip_id,lane,caption,scheduled_at) VALUES(?,'trial','Cap',?)", (cid, time.time()-30)).lastrowid
    bodies = []
    monkeypatch.setattr(ig, 'create_container', lambda account, job, url: bodies.append(job) or 'container')
    monkeypatch.setattr(ig, 'container_status', lambda *a: 'FINISHED')
    monkeypatch.setattr(ig, 'publish', lambda *a: 'MEDIA1')
    monkeypatch.setattr(ig, 'request', lambda *a, **k: {})
    worker.advance(main_id, time.time())
    worker.advance(trial_id, time.time())
    assert bodies[0]['collaborators'] == ['one', 'two'] and bodies[1]['collaborators'] == []
    assert {r['username']: r['status'] for r in collab.for_job(main_id)} == {'one': 'pending', 'two': 'manual'}
    # The real request body includes them for grid Reels only.
    sent = []
    monkeypatch.setattr(ig, 'request', lambda method, path, **kw: sent.append(kw['json']) or {'id': 'c'})
    REAL_CREATE('acct', {'caption': 'A', 'lane': 'main', 'collaborators': ['one']}, 'https://x.test/v')
    REAL_CREATE('acct', {'caption': 'B', 'lane': 'trial', 'collaborators': ['one']}, 'https://x.test/v')
    assert sent[0]['collaborators'] == ['one'] and 'collaborators' not in sent[1]

def test_collaborator_sign_in_never_replaces_the_main_account(monkeypatch):
    connected()
    monkeypatch.setattr(c, 'META_ID', 'app'); monkeypatch.setattr(c, 'META_SECRET', 'secret')
    monkeypatch.setattr(ig, 'exchange', lambda code: {'token': 'tok', 'expires_at': time.time()+86400, 'ig_id': '555', 'username': 'Side.Account'})
    with TestClient(app) as client:
        assert client.post('/api/login', json={'password': c.PASSWORD}).status_code == 200
        start = client.get('/auth/instagram/start?collab=1', follow_redirects=False)
        state = start.headers['location'].split('state=')[1].split('&')[0]
        assert client.get(f'/auth/instagram/callback?code=x&state={state}', follow_redirects=False).status_code == 303
        h = {'x-csrf-token': client.get('/api/state').json()['csrf']}
        assert [a['username'] for a in client.get('/api/collaborators').json()['connected']] == ['side.account']
        with connect() as db:
            assert db.execute('SELECT username FROM account').fetchone()[0] == 'trades'
        assert client.post('/api/collaborators/side.account/sign-out', headers=h).status_code == 200
        assert client.get('/api/collaborators').json()['connected'] == []
