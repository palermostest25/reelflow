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
        db.execute('UPDATE clips SET media_version=? WHERE id=?',(worker.media.MEDIA_VERSION,cid))
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

def test_old_encode_waits_for_rebuild_before_publishing(job,monkeypatch):
    with connect(True) as db:db.execute('UPDATE clips SET media_version=0')
    monkeypatch.setattr(ig,'create_container',lambda *a:pytest.fail('Must not upload a padded encode'))
    worker.advance(job,time.time())
    assert row(job)['status']=='queued'

def test_rebuild_requested_during_upload_holds_publish(job,monkeypatch):
    def create(*args):
        with connect(True) as db:
            db.execute('UPDATE clips SET media_version=?',(worker.media.REBUILD_REQUESTED,))
        return 'old-container'
    monkeypatch.setattr(ig,'create_container',create)
    monkeypatch.setattr(ig,'publish',lambda *a:pytest.fail('Must wait for the rebuilt export'))
    worker.advance(job,time.time())
    assert row(job)['status']=='processing'

def test_post_now_publishes_only_requested_reel_while_paused(job,monkeypatch):
    set_settings(paused=True,trials_enabled=False)
    other_clip=seed(1,prefix='other')[0]
    now=time.time()
    with connect(True) as db:
        db.execute('UPDATE clips SET media_version=?',(worker.media.MEDIA_VERSION,))
        db.execute('UPDATE jobs SET post_now_at=?,approved=0,scheduled_at=? WHERE id=?',(now,now+86400,job))
        db.execute('UPDATE clips SET disabled=1 WHERE id=(SELECT clip_id FROM jobs WHERE id=?)',(job,))
        other=db.execute("INSERT INTO jobs(clip_id,lane,caption,scheduled_at) VALUES(?,'main','Other caption',?)",(other_clip,now-60)).lastrowid
    for name in ('refresh_token','sync_history','sync_metrics'):
        monkeypatch.setattr(ig,name,lambda:None)
    monkeypatch.setattr(worker.media,'scan',lambda:0)
    monkeypatch.setattr(worker.ai,'caption_pending',lambda:None)
    sent=[]
    monkeypatch.setattr(ig,'publish',lambda *args:sent.append(args) or '98765')
    worker.tick()
    assert row(job)['status']=='published' and row(job)['post_now_at']==now
    assert row(other)['status']=='queued' and len(sent)==1

def test_post_now_fills_the_freed_slot_and_does_not_spend_the_scheduled_quota(job,monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now=datetime(2026,10,20,8,tzinfo=ZoneInfo('Australia/Perth')).timestamp()
    monkeypatch.setattr(worker.time,'time',lambda:now)
    seed(1,prefix='other')
    set_settings(learn_timing=False,min_gap_days=0,main_per_day=1)
    with connect(True) as db:
        db.execute("UPDATE jobs SET lane='main',scheduled_at=NULL WHERE id=?",(job,))
        db.execute('UPDATE clips SET media_version=?',(worker.media.MEDIA_VERSION,))
    worker.plan(now)
    with connect(True) as db:
        first, next_job=db.execute("SELECT id,scheduled_at FROM jobs WHERE lane='main' ORDER BY scheduled_at").fetchall()
        db.execute('UPDATE jobs SET post_now_at=? WHERE id=?',(now,first['id']))
    sent=[]
    monkeypatch.setattr(ig,'publish',lambda *a:sent.append(a) or str(len(sent)))
    worker.send_post_now(first['id'])
    assert row(first['id'])['status']=='published'
    assert row(next_job['id'])['scheduled_at']==first['scheduled_at']
    worker.advance(next_job['id'],first['scheduled_at'])
    assert row(next_job['id'])['status']=='published' and len(sent)==2

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
    ig.create_container('account',{'caption':'A','lane':'trial','cover_ms':0},'https://example.test/file')
    ig.create_container('account',{'caption':'B','lane':'main'},'https://example.test/file')
    assert seen[0]['trial_params']=={'graduation_strategy':'MANUAL'} and seen[0]['share_to_feed'] is False
    assert 'trial_params' not in seen[1] and seen[1]['share_to_feed'] is True
    assert seen[0]['thumb_offset']==0 and 'thumb_offset' not in seen[1]

def test_cover_mode_change_refreshes_an_unpublished_container(job,monkeypatch):
    with connect(True) as db:
        db.execute('UPDATE clips SET cover_auto_ms=2000,cover_color_ms=3000')
        db.execute("UPDATE jobs SET status='processing',container_id='old-container',container_cover_ms=2000 WHERE id=?",(job,))
    set_settings(cover_mode='color')
    offsets=[]
    monkeypatch.setattr(ig,'create_container',lambda account,post,url:offsets.append(post['cover_ms']) or 'new-container')
    monkeypatch.setattr(ig,'publish',lambda *a:'98765')
    worker.advance(job,time.time())
    assert offsets==[3000] and row(job)['status']=='published'

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
