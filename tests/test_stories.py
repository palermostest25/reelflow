"""Sharing published Reels to Instagram Stories."""
import shutil
import subprocess
import time
import pytest
from fastapi.testclient import TestClient
from app import stories, worker, instagram as ig, config as c
from app.db import connect
from app.main import app
from app.security import media_url
from conftest import seed, set_settings, connected

@pytest.fixture
def live(monkeypatch):
    cid = seed(1)[0]
    connected()
    set_settings(paused=False, dry_run=False, conserve=False)
    with connect(True) as db:
        db.execute('UPDATE clips SET media_version=?,duration=20 WHERE id=?', (worker.media.MEDIA_VERSION, cid))
    monkeypatch.setattr(ig, 'create_container', lambda *a: 'reel-container')
    monkeypatch.setattr(ig, 'container_status', lambda *a: 'FINISHED')
    monkeypatch.setattr(ig, 'publish', lambda account, container: 'MEDIA-'+container)
    return cid

def publish(cid, lane):
    with connect(True) as db:
        job_id = db.execute("INSERT INTO jobs(clip_id,lane,caption,scheduled_at) VALUES(?,?,'Cap',?)", (cid, lane, time.time()-30)).lastrowid
    worker.advance(job_id, time.time())
    return job_id

def test_grid_reels_are_shared_to_stories_and_trials_are_not_by_default(live, monkeypatch):
    sent = []
    monkeypatch.setattr(ig, 'request', lambda method, path, **kw: sent.append((method, path, kw)) or {'id': 'story-container', 'permalink': ''})
    grid = publish(live, 'main')
    trial = publish(live, 'trial')
    assert stories.for_job(grid)['status'] == 'pending' and stories.for_job(trial) is None
    stories.share_due(time.time()+1)
    create = [s for s in sent if s[1].endswith('/media')]
    assert create and create[0][2]['json']['media_type'] == 'STORIES' and '/media/'+live in create[0][2]['json']['video_url']
    stories.share_due(time.time()+60)
    row = stories.for_job(grid)
    assert row['status'] == 'published' and row['media_id'] == 'MEDIA-story-container'

def test_story_sharing_follows_the_setting(live, monkeypatch):
    set_settings(story_share='off')
    assert stories.for_job(publish(live, 'main')) is None
    with connect(True) as db:
        db.execute('DELETE FROM jobs')
    set_settings(story_share='all')
    assert stories.for_job(publish(live, 'trial'))['status'] == 'pending'

def test_refused_stories_stop_and_say_why(live, monkeypatch):
    def refuse(method, path, **kw):
        if path.endswith('/media'):
            raise ig.MetaError('Instagram rejected the request (HTTP 400, code 10): no permission.', 10)
        return {}
    monkeypatch.setattr(ig, 'request', refuse)
    job = publish(live, 'main')
    stories.share_due(time.time()+1)
    row = stories.for_job(job)
    assert row['status'] == 'failed' and 'no permission' in row['error']

@pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg required')
def test_long_clips_are_cut_to_the_story_limit_and_served_signed():
    cid = seed(1)[0]
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=64x64:d=65', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                    str(c.DATA/'media'/(cid+'.mp4'))], check=True)
    assert stories.story_file(cid, 20) == cid
    name = stories.story_file(cid, 65)
    assert name == cid+'-story'
    duration = float(subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0',
                                     str(c.DATA/'stories'/(name+'.mp4'))], capture_output=True, text=True).stdout)
    assert duration <= 60
    with TestClient(app) as client:
        url = media_url(name).replace(c.PUBLIC_URL, '')
        assert client.get(url).status_code == 200
        assert client.get(url.replace('-story', '-storx')).status_code == 403
