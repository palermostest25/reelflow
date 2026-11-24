"""Drag-and-drop scheduling, holds, footage drops and YouTube mirroring."""
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest
from fastapi.testclient import TestClient
from app import schedule as hand, worker, media, youtube as yt, config as c
from app.db import connect, settings
from app.main import app
from app.planner import plan
from conftest import seed, set_settings

TZ = ZoneInfo('Australia/Perth')
NOW = datetime(2026, 11, 20, 8, tzinfo=TZ).timestamp()

def local(ts):
    return datetime.fromtimestamp(ts, TZ)

def job(clip_id, lane):
    with connect() as db:
        row = db.execute('SELECT * FROM jobs WHERE clip_id=? AND lane=?', (clip_id, lane)).fetchone()
    return dict(row) if row else None

def test_drop_times_follow_the_neighbouring_posts():
    s = settings()
    at = lambda **kw: local(hand.place_time(s, kw.pop('lane', 'main'), '2026-11-22', now=NOW, **kw))
    assert at().hour == s['schedule_hours'][6] and at().minute == 0
    nine, eleven = datetime(2026, 11, 22, 9, tzinfo=TZ).timestamp(), datetime(2026, 11, 22, 11, tzinfo=TZ).timestamp()
    assert at(after=nine, before=eleven).strftime('%H:%M') == '10:00'
    assert at(after=nine).strftime('%H:%M') == '10:30'
    assert at(before=nine).strftime('%H:%M') == '08:30' and at(before=nine).hour >= s['posting_start_hour']
    with pytest.raises(hand.PlaceError, match='passed'):
        hand.place_time(s, 'main', '2026-11-19', now=NOW)

def test_placing_by_hand_pins_the_post_and_beats_the_daily_limit(monkeypatch):
    ids = seed(6)
    set_settings(conserve=False, min_gap_days=0)
    plan(NOW)
    first, second = job(ids[0], 'main'), job(ids[1], 'main')
    # Two grid Reels on one day even though the daily limit is one.
    hand.place('job', first['id'], 'main', '2026-11-23', now=NOW)
    hand.place('job', second['id'], 'main', '2026-11-23', after=job(ids[0], 'main')['scheduled_at'], now=NOW)
    a, b = job(ids[0], 'main'), job(ids[1], 'main')
    assert local(a['scheduled_at']).date() == local(b['scheduled_at']).date() == datetime(2026, 11, 23).date()
    assert b['scheduled_at'] - a['scheduled_at'] == 90*60 and a['pinned'] == a['forced'] == 1
    plan(NOW)
    assert job(ids[0], 'main')['scheduled_at'] == a['scheduled_at']  # The planner leaves it alone.
    with connect() as db:
        others = [r[0] for r in db.execute("SELECT scheduled_at FROM jobs WHERE lane='main' AND forced=0 AND scheduled_at IS NOT NULL")]
        assert not any(local(t).date() == datetime(2026, 11, 23).date() for t in others)
        assert worker.publish_limit(db, settings(), {**b, 'forced': 1}, b['scheduled_at']) == ''

def test_dragging_between_destinations_and_from_footage(monkeypatch):
    ids = seed(3)
    set_settings(conserve=False, min_gap_days=0, trials_per_day=1)
    plan(NOW)
    trial = job(ids[0], 'trial')
    with connect(True) as db:
        db.execute("DELETE FROM jobs WHERE clip_id=? AND lane='main'", (ids[0],))
    hand.place('job', trial['id'], 'main', '2026-11-24', now=NOW)
    assert job(ids[0], 'main')['forced'] == 1 and job(ids[0], 'trial')['held'] == 1
    assert job(ids[0], 'trial')['scheduled_at'] is None
    # From the footage tray, a clip with no post yet.
    with connect(True) as db:
        db.execute('DELETE FROM jobs WHERE clip_id=?', (ids[2],))
    hand.place('clip', ids[2], 'trial', '2026-11-25', now=NOW)
    assert job(ids[2], 'trial')['forced'] == 1
    with connect(True) as db:
        db.execute("UPDATE jobs SET status='published',published_at=? WHERE clip_id=? AND lane='main'", (NOW, ids[1]))
    with pytest.raises(hand.PlaceError, match='already posted'):
        hand.place('clip', ids[1], 'main', '2026-11-25', now=NOW)

def test_hold_takes_a_post_out_and_release_hands_it_back():
    ids = seed(4)
    set_settings(conserve=False, min_gap_days=0)
    plan(NOW)
    target = job(ids[0], 'main')
    hand.hold('job', target['id'])
    plan(NOW)
    held = job(ids[0], 'main')
    assert held['held'] == 1 and held['scheduled_at'] is None
    tray = hand.tray()
    assert [h['id'] for h in tray['held']] == [target['id']] and {f['id'] for f in tray['footage']} == set(ids)
    hand.release('job', target['id'])
    plan(NOW)
    assert job(ids[0], 'main')['held'] == 0 and job(ids[0], 'main')['scheduled_at']

needs_ffmpeg = pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg required')

@needs_ffmpeg
def test_grid_reels_go_to_youtube_as_shorts_and_follow_when_moved():
    ids = seed(2)
    with connect(True) as db:
        db.execute("INSERT INTO youtube_account(id,channel_id,title) VALUES(1,'UC123','Channel')")
        for cid in ids:
            db.execute('UPDATE clips SET width=180,height=320,duration=3,media_version=? WHERE id=?', (media.MEDIA_VERSION, cid))
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=180x320:d=3', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                            str(c.DATA/'media'/(cid+'.mp4'))], check=True)
    set_settings(conserve=False, min_gap_days=0)
    plan(time.time())
    grid = job(ids[0], 'main')
    with connect() as db:
        short = dict(db.execute("SELECT * FROM youtube_jobs WHERE source_key=?", (ids[0],)).fetchone())
    assert short['mirror_of'] == grid['id'] and short['scheduled_at'] == grid['scheduled_at'] and short['status'] == 'queued'
    day = (datetime.now(TZ) + timedelta(days=3)).date().isoformat()
    hand.place('job', grid['id'], 'main', day)
    with connect() as db:
        moved = db.execute("SELECT scheduled_at FROM youtube_jobs WHERE id=?", (short['id'],)).fetchone()[0]
    assert moved == job(ids[0], 'main')['scheduled_at']
    # Holding the Reel stops its Short; a Short moved by hand stops following.
    hand.hold('job', grid['id'])
    with connect() as db:
        assert not db.execute("SELECT 1 FROM youtube_jobs WHERE id=?", (short['id'],)).fetchone()
        other = db.execute("SELECT id FROM youtube_jobs WHERE source_key=?", (ids[1],)).fetchone()[0]
    hand.place('youtube', other, 'youtube', day)
    with connect() as db:
        row = db.execute('SELECT mirror_of,forced FROM youtube_jobs WHERE id=?', (other,)).fetchone()
    assert row['mirror_of'] is None and row['forced'] == 1
    # Mirroring can be switched off.
    with connect(True) as db:
        db.execute("INSERT OR REPLACE INTO runtime VALUES('youtube_preferences',?)", (yt.Preferences(mirror_grid=False).model_dump_json(),))
    hand.release('job', grid['id'])
    with connect() as db:
        assert not db.execute("SELECT 1 FROM youtube_jobs WHERE source_key=? AND mirror_of IS NOT NULL", (ids[0],)).fetchone()

def test_schedule_api_places_holds_and_lists():
    ids = seed(2)
    set_settings(conserve=False, min_gap_days=0)
    plan()
    with TestClient(app) as client:
        assert client.post('/api/login', json={'password': c.PASSWORD}).status_code == 200
        h = {'x-csrf-token': client.get('/api/state').json()['csrf']}
        target = job(ids[0], 'main')
        day = (datetime.now(TZ) + timedelta(days=2)).date().isoformat()
        r = client.post('/api/schedule/place', headers=h, json={'kind': 'job', 'id': str(target['id']), 'lane': 'main', 'day': day})
        assert r.status_code == 200 and r.json()['at']
        assert client.post('/api/schedule/place', headers=h, json={'kind': 'job', 'id': '999', 'lane': 'main', 'day': day}).status_code == 422
        assert client.post('/api/schedule/hold', headers=h, json={'kind': 'job', 'id': str(target['id'])}).status_code == 200
        assert client.get('/api/schedule/tray').json()['held'][0]['id'] == target['id']
        cal = client.get('/api/schedule').json()
        assert 'youtube' in cal['days'][0] and cal['youtube'] == {'connected': False, 'mirror': True}
