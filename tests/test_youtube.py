import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app import media, youtube as yt
from app.db import connect, secret


def test_finished_sets_use_separate_folder_and_content_deduplication(monkeypatch):
    root = yt.set_folder()
    root.mkdir()
    (root/'Set One.mp4').write_bytes(b'finished set')
    (root/'Duplicate.mp4').write_bytes(b'finished set')
    (root/'Too short.mp4').write_bytes(b'short set')
    monkeypatch.setattr(media,'video_info',lambda path:(900 if path.name=='Too short.mp4' else 1500,1920,1080))
    now = time.time()
    yt.scan_sets(now)
    assert media.scan(now+31)==0
    yt.scan_sets(now+31)
    yt.scan_sets(now+32)
    with connect() as db:
        assert db.execute('SELECT COUNT(*) FROM clips').fetchone()[0]==0
        assert {tuple(row) for row in db.execute('SELECT duration,status,visibility FROM youtube_jobs')}=={
            (1500,'draft','private'),(900,'invalid','private')}
        assert db.execute('SELECT COUNT(*) FROM youtube_jobs').fetchone()[0]==2


def test_upload_session_survives_lost_completion_response(monkeypatch):
    root = yt.set_folder()
    root.mkdir()
    path = root/'Set.mp4'
    path.write_bytes(b'upload')
    with connect(True) as db:
        job_id = yt.add_draft(db,yt.preferences(db),'set','set-key',path.name,1500,path=str(path))
        db.execute("UPDATE youtube_jobs SET status='queued',manual=1,scheduled_at=1 WHERE id=?",(job_id,))
        db.execute("INSERT INTO youtube_account VALUES(1,'channel','My channel','')")
        secret('youtube_oauth',json.dumps({'client_id':'client','client_secret':'secret','refresh_token':'refresh'}),db=db)
    requests = []
    location = 'https://www.googleapis.com/upload/youtube/v3/videos?upload_id=example'

    def mock(request):
        if request.url.host=='oauth2.googleapis.com':
            return httpx.Response(200,json={'access_token':'test-access'})
        requests.append((request.method,request.headers.get('Content-Range')))
        if request.method=='POST':
            assert json.loads(request.content)['status']['privacyStatus']=='private'
            return httpx.Response(200,headers={'Location':location})
        if request.headers['Content-Range']=='bytes 0-5/6':
            raise httpx.ReadError('lost completion response',request=request)
        assert request.headers['Content-Range']=='bytes */6'
        return httpx.Response(201,json={'id':'abcdefghijk','status':{'privacyStatus':'private'}})

    original = httpx.Client
    monkeypatch.setattr(yt.httpx,'Client',lambda **kwargs:original(transport=httpx.MockTransport(mock),**kwargs))
    assert yt.publish_due(time.time())
    assert secret('youtube_upload:'+str(job_id))==location
    with connect(True) as db:
        assert db.execute('SELECT status FROM youtube_jobs').fetchone()[0]=='uploading'
        db.execute('UPDATE youtube_jobs SET next_attempt=0')
    assert yt.publish_due(time.time())
    with connect() as db:
        assert tuple(db.execute('SELECT status,video_id,actual_visibility FROM youtube_jobs').fetchone())==(
            'uploaded','abcdefghijk','private')
    assert not secret('youtube_upload:'+str(job_id))
    assert requests==[('POST',None),('PUT','bytes 0-5/6'),('PUT','bytes */6')]


def test_queue_slots_follow_format_cadence_and_workspace_timezone():
    zone = ZoneInfo('Australia/Perth')
    now = datetime(2026,11,12,9,tzinfo=zone).timestamp()
    prefs = yt.Preferences(shorts_per_day=2,sets_every_days=7)
    with connect(True) as db:
        for kind,count in (('short',3),('set',2)):
            for i in range(count):
                yt.add_draft(db,prefs,kind,f'{kind}-{i}',f'{kind}-{i}.mp4',30,approve=True)
        yt.plan(db,prefs,now)
        shorts = [datetime.fromtimestamp(row[0],zone) for row in db.execute(
            "SELECT scheduled_at FROM youtube_jobs WHERE kind='short' ORDER BY id")]
        sets = [datetime.fromtimestamp(row[0],zone) for row in db.execute(
            "SELECT scheduled_at FROM youtube_jobs WHERE kind='set' ORDER BY id")]
    assert [(t.day,t.hour,t.minute) for t in shorts]==[(12,17,0),(12,17,5),(13,17,0)]
    assert [(t.day,t.hour) for t in sets]==[(12,18),(19,18)]
