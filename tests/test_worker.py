import time
import pytest
from app import worker,instagram as ig
from app.db import connect
from conftest import seed,set_settings,connected

@pytest.fixture
def job(monkeypatch):
    cid=seed(1)[0]
    connected()
    set_settings(paused=False,dry_run=False,conserve=False)
    now=time.time()
    with connect(True) as db:
        job_id=db.execute("INSERT INTO jobs(clip_id,lane,caption,scheduled_at) VALUES(?,'trial','Test caption',?)",(cid,now-30)).lastrowid
    monkeypatch.setattr(ig,'create_container',lambda *a:'container-1')
    monkeypatch.setattr(ig,'container_status',lambda *a:'FINISHED')
    monkeypatch.setattr(ig,'request',lambda *a,**kw:{'permalink':'https://www.instagram.com/reel/example/'})
    return job_id

def row(job_id):
    with connect() as db:return dict(db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())

def test_success_once_only(job,monkeypatch):
    calls=[]
    monkeypatch.setattr(ig,'publish',lambda *a:calls.append(a) or '987654321')
    worker.advance(job,time.time())
    worker.advance(job,time.time())
    assert len(calls)==1 and row(job)['status']=='published'
    with connect() as db:assert db.execute('SELECT lane FROM history').fetchone()[0]=='trial'

def test_ambiguous_publish_never_blind_retries(job,monkeypatch):
    calls=[]
    def fail(*a):calls.append(a);raise TimeoutError()
    monkeypatch.setattr(ig,'publish',fail)
    worker.advance(job,time.time())
    assert row(job)['status']=='uncertain'
    worker.advance(job,time.time()+1000)
    assert len(calls)==1
    monkeypatch.setattr(ig,'container_status',lambda *a:'PUBLISHED')
    worker.advance(job,time.time()+2000)
    assert row(job)['status']=='published' and len(calls)==1

def test_crash_after_publishing_mark_is_held(job,monkeypatch):
    with connect(True) as db:db.execute("UPDATE jobs SET status='publishing',container_id='container-1' WHERE id=?",(job,))
    monkeypatch.setattr(ig,'publish',lambda *a:pytest.fail('Must not retry'))
    worker.advance(job,time.time())
    assert row(job)['status']=='uncertain'

def test_no_publish_when_paused_disabled_or_unapproved(job,monkeypatch):
    monkeypatch.setattr(ig,'publish',lambda *a:pytest.fail('Must not publish'))
    set_settings(paused=True)
    worker.advance(job,time.time())
    set_settings(paused=False)
    with connect(True) as db:db.execute('UPDATE jobs SET approved=0 WHERE id=?',(job,))
    worker.advance(job,time.time())
    with connect(True) as db:
        db.execute('UPDATE jobs SET approved=1 WHERE id=?',(job,))
        db.execute('UPDATE clips SET disabled=1')
    worker.advance(job,time.time())
    assert row(job)['status']=='queued'

def test_trial_failure_never_falls_back_to_regular(job,monkeypatch):
    def fail(*a):raise ig.MetaError('Trial ineligible')
    monkeypatch.setattr(ig,'create_container',fail)
    monkeypatch.setattr(ig,'publish',lambda *a:pytest.fail('Must not publish'))
    worker.advance(job,time.time())
    assert row(job)['status']=='failed' and row(job)['lane']=='trial'

def test_token_expiry_blocks_publish(job,monkeypatch):
    with connect(True) as db:db.execute('UPDATE account SET expires_at=0')
    monkeypatch.setattr(ig,'publish',lambda *a:pytest.fail('Must not publish'))
    worker.advance(job,time.time())
    assert row(job)['status']=='queued'

def test_paused_during_container_creation_blocks_publish(job,monkeypatch):
    def create(*a):set_settings(paused=True);return 'container-1'
    monkeypatch.setattr(ig,'create_container',create)
    monkeypatch.setattr(ig,'publish',lambda *a:pytest.fail('Must not publish'))
    worker.advance(job,time.time())
    assert row(job)['status']=='processing'

def test_trial_parameters_and_main_feed_are_distinct(monkeypatch):
    seen=[]
    monkeypatch.setattr(ig,'request',lambda *a,**kw:seen.append(kw['json']) or {'id':'container'})
    ig.create_container('account',{'caption':'A','lane':'trial'},'https://example.test/file')
    ig.create_container('account',{'caption':'B','lane':'main'},'https://example.test/file')
    assert seen[0]['trial_params']=={'graduation_strategy':'MANUAL'} and seen[0]['share_to_feed'] is False
    assert 'trial_params' not in seen[1] and seen[1]['share_to_feed'] is True

def test_processing_container_survives_worker_restart(job,monkeypatch):
    with connect(True) as db:db.execute("UPDATE jobs SET status='processing',container_id='saved-container' WHERE id=?",(job,))
    monkeypatch.setattr(ig,'create_container',lambda *a:pytest.fail('Must reuse container'))
    monkeypatch.setattr(ig,'publish',lambda *a:'98765')
    worker.advance(job,time.time())
    assert row(job)['status']=='published'

def test_transient_failures_stop_after_five_attempts(job,monkeypatch):
    def fail(*a):raise ig.MetaError('Temporary outage',transient=True)
    monkeypatch.setattr(ig,'container_status',fail)
    for i in range(5):worker.advance(job,time.time()+i*4000)
    assert row(job)['status']=='failed' and row(job)['attempts']==5

def test_storage_failure_after_external_success_stays_uncertain(job,monkeypatch):
    monkeypatch.setattr(ig,'publish',lambda *a:'98765')
    monkeypatch.setattr(worker,'finish',lambda *a,**kw:(_ for _ in ()).throw(OSError('disk problem')))
    worker.advance(job,time.time())
    assert row(job)['status']=='uncertain'

def test_trial_disable_during_upload_blocks_publish(job,monkeypatch):
    def create(*a):set_settings(trials_enabled=False);return 'saved'
    monkeypatch.setattr(ig,'create_container',create)
    monkeypatch.setattr(ig,'publish',lambda *a:pytest.fail('Must not publish'))
    worker.advance(job,time.time())
    assert row(job)['status']=='processing'

def test_nonoverlap_lock():
    with worker.worker_lock() as first:
        assert first
        with worker.worker_lock() as second:assert not second

def test_tick_publishes_a_due_job_instead_of_rescheduling(job,monkeypatch):
    calls=[]
    monkeypatch.setattr(ig,'publish',lambda *a:calls.append(a) or '98765')
    monkeypatch.setattr(ig,'refresh_token',lambda:None)
    monkeypatch.setattr(ig,'sync_history',lambda:None)
    monkeypatch.setattr(ig,'sync_metrics',lambda:None)
    monkeypatch.setattr(worker.media,'scan',lambda:0)
    monkeypatch.setattr(worker.ai,'caption_pending',lambda:None)
    worker.tick()
    assert row(job)['status']=='published' and len(calls)==1

def test_publisher_uses_official_instagram_http_endpoints(monkeypatch):
    import httpx
    import json
    from app import config as c
    from app.db import secret
    secret('instagram_token','test-only-instagram-access-token')
    seen=[]
    def handler(request):
        seen.append(request)
        if request.url.path.endswith('/media_publish'):
            return httpx.Response(200,json={'id':'media-123'})
        if request.url.path.endswith('/media'):
            return httpx.Response(200,json={'id':'container-123'})
        return httpx.Response(200,json={'status_code':'FINISHED'})
    original_client=httpx.Client
    monkeypatch.setattr(ig.httpx,'Client',lambda **kwargs:original_client(transport=httpx.MockTransport(handler),**kwargs))
    cid=ig.create_container('ig-account',{'caption':'Real footage caption','lane':'main'},'https://reels.example/media/signed')
    assert ig.container_status(cid)=='FINISHED'
    assert ig.publish('ig-account',cid)=='media-123'
    assert [r.method for r in seen]==['POST','GET','POST']
    assert [r.url.path for r in seen]==['/'+c.API_VERSION+'/ig-account/media','/'+c.API_VERSION+'/container-123','/'+c.API_VERSION+'/ig-account/media_publish']
    assert all(r.url.host=='graph.instagram.com' for r in seen)
    assert json.loads(seen[0].content)['video_url']=='https://reels.example/media/signed'
    assert json.loads(seen[2].content)=={'creation_id':'container-123'}
    assert all(r.headers['Authorization'].startswith('Bearer ') for r in seen)
