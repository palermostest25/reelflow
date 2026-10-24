import time
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app import config as c
from app.db import connect,secret
from app.planner import plan
from app.security import media_url
from conftest import seed

@pytest.fixture
def client():
    with TestClient(app) as client:yield client

def auth(client):
    assert client.post('/api/login',json={'password':c.PASSWORD}).status_code==200
    token=client.get('/api/state').json()['csrf']
    return {'x-csrf-token':token}

def test_auth_csrf_and_login_rate_limit(client):
    assert client.get('/api/state').status_code==401
    headers=auth(client)
    assert client.patch('/api/settings',json={'values':{'brand_name':'Test'}}).status_code==403
    assert client.patch('/api/settings',headers=headers,json={'values':{'brand_name':'Test'}}).status_code==200
    assert client.patch('/api/settings',headers={**headers,'Origin':'https://evil.test'},json={'values':{'brand_name':'Evil'}}).status_code==403
    assert client.post('/api/logout',headers=headers).status_code==200
    assert client.get('/api/state').status_code==401
    for _ in range(10):assert client.post('/api/login',json={'password':'wrong'}).status_code==401
    assert client.post('/api/login',json={'password':'wrong'}).status_code==429

def test_settings_strict_and_live_gate(client):
    h=auth(client)
    for values in ({'timezone':'Mars/Nope'},{'trials_per_day':11},{'paused':'false'},{'ntfy_topic':'../x'},{'schedule_hours':[24]*7},{'drought_until':'never'}):
        assert client.patch('/api/settings',headers=h,json={'values':values}).status_code==422
    assert client.patch('/api/settings',headers=h,json={'values':{'dry_run':False}}).status_code==409

def test_secrets_encrypted_and_not_returned(client):
    h=auth(client)
    assert client.post('/api/keys',headers=h,json={'ai_key':'super-secret-provider-token'}).status_code==200
    assert secret('ai_key')=='super-secret-provider-token'
    with connect() as db:assert 'super-secret-provider-token' not in db.execute('SELECT value FROM secrets').fetchone()[0]
    assert 'super-secret-provider-token' not in client.get('/api/state').text

def test_signed_media_expiry_and_range(client):
    cid='a'*64
    (c.DATA/'media'/(cid+'.mp4')).write_bytes(b'0123456789')
    url=media_url(cid).replace(c.PUBLIC_URL,'')
    assert client.get(url).content==b'0123456789'
    part=client.get(url,headers={'Range':'bytes=2-5'})
    assert part.status_code==206 and part.content==b'2345'
    assert client.get(url.replace('signature=','signature=bad')).status_code==403
    assert client.get('/api/preview/'+cid).status_code==401

def test_edit_reject_restore_and_pinned_time(client):
    h=auth(client)
    seed(3)
    plan()
    j=client.get('/api/jobs').json()[0]
    assert client.patch('/api/jobs/'+str(j['id']),headers=h,json={'caption':'Edited by us.'}).status_code==200
    plan()
    with connect() as db:assert db.execute('SELECT caption,caption_locked FROM jobs WHERE id=?',(j['id'],)).fetchone()[0]=='Edited by us.'
    assert client.post('/api/jobs/'+str(j['id'])+'/action',headers=h,json={'action':'reject'}).status_code==200
    plan()
    assert any(r['id']==j['id'] for r in client.get('/api/jobs?status=rejected').json())
    assert client.post('/api/jobs/'+str(j['id'])+'/action',headers=h,json={'action':'restore'}).status_code==200

def test_no_caption_clip_held_until_manual_caption(client):
    h=auth(client)
    cid=seed(1,status='needs_caption')[0]
    plan()
    assert client.get('/api/jobs').json()==[]
    assert client.patch('/api/clips/'+cid,headers=h,json={'caption':'From our desk to yours.'}).status_code==200
    assert len(client.get('/api/jobs').json())==2

def test_oauth_state_rejection(client,monkeypatch):
    h=auth(client)
    monkeypatch.setattr(c,'META_ID','test-id')
    monkeypatch.setattr(c,'META_SECRET','test-secret')
    r=client.get('/auth/instagram/start',follow_redirects=False)
    assert r.status_code==307 and 'state=' in r.headers['location']
    from urllib.parse import urlparse,parse_qs
    query=parse_qs(urlparse(r.headers['location']).query)
    assert query['client_id']==['test-id']
    assert query['redirect_uri']==[c.PUBLIC_URL+'/auth/instagram/callback']
    assert query['force_reauth']==['true'] and query['enable_fb_login']==['false']
    assert set(query['scope'][0].split(','))=={'instagram_business_basic','instagram_business_content_publish','instagram_business_manage_insights'}
    assert client.get('/auth/instagram/callback?state=forged&code=bad').status_code==403

def test_xss_is_data_and_pagination_bounded(client):
    h=auth(client)
    seed(1)
    assert client.patch('/api/settings',headers=h,json={'values':{'brand_name':'<script>alert(1)</script>'}}).status_code==200
    assert client.get('/api/jobs?limit=999999').status_code==422
    assert "script-src 'self'" in client.get('/').headers['content-security-policy']


def test_model_change_clears_caption_backoff_but_unrelated_settings_do_not(client):
    from app.db import runtime
    h=auth(client)
    retry=time.time()+1800
    runtime('caption_retry_at',retry)
    assert client.patch('/api/settings',headers=h,json={'values':{'brand_name':'TRADES'}}).status_code==200
    assert float(runtime('caption_retry_at'))==retry
    assert client.patch('/api/settings',headers=h,json={'values':{'ai_model':'meta/muse-spark-1.3-contributor'}}).status_code==200
    assert float(runtime('caption_retry_at'))==0


def test_successful_caption_rewrite_resumes_the_waiting_batch(client,monkeypatch):
    from app import ai
    from app.db import runtime
    h=auth(client);cid=seed(1,status='needs_caption')[0]
    runtime('caption_retry_at',time.time()+1800)
    monkeypatch.setattr(ai,'generate',lambda *args:('A real caption from the selected model.','DJ performance'))
    response=client.post('/api/clips/'+cid+'/regenerate',headers=h)
    assert response.status_code==200 and response.json()['caption']=='A real caption from the selected model.'
    assert float(runtime('caption_retry_at'))==0
    with connect() as db:
        assert db.execute('SELECT status FROM clips WHERE id=?',(cid,)).fetchone()[0]=='ready'
        assert db.execute('SELECT COUNT(*) FROM jobs WHERE clip_id=?',(cid,)).fetchone()[0]==2
