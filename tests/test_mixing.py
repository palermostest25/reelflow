"""Sets mix together, the same song is spaced out, and the library shows both."""
import hashlib
import shutil
import subprocess
import time
import wave
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import pytest
from fastapi.testclient import TestClient
from app import config as c, songs
from app.db import connect
from app.main import app
from app.planner import plan
from conftest import set_settings

NOW = datetime(2026,10,20,8,tzinfo=ZoneInfo('Australia/Perth')).timestamp()
needs_ffmpeg = pytest.mark.skipif(not shutil.which('ffmpeg'),reason='ffmpeg required')

def add_clips(batch, count, mode='mix', song=None, created=None):
    ids = []
    created = created or time.time()
    with connect(True) as db:
        db.execute('INSERT OR IGNORE INTO intake_batches(id,name,mode,created_at) VALUES(?,?,?,?)',(batch,batch,mode,created))
        for i in range(count):
            cid = hashlib.sha256(f'{batch}-{i}'.encode()).hexdigest()
            db.execute("INSERT INTO clips(id,name,source,created_at,status,caption,category,batch,song) VALUES(?,?,?,?,'ready','A caption.','DJ set',?,?)",
                       (cid,f'{batch}-{i:02}.mp4','/test/'+batch,created,batch,song(i) if song else ''))
            ids.append(cid)
    return ids

def lane_order(lane='main'):
    with connect() as db:
        return [r[0] for r in db.execute("SELECT clip_id FROM jobs WHERE lane=? AND scheduled_at IS NOT NULL ORDER BY scheduled_at",(lane,))]

def longest_run(values):
    best = run = 1
    for a,b in zip(values,values[1:]):
        run = run+1 if a==b else 1
        best = max(best,run)
    return best

def test_two_uploaded_sets_are_mixed_not_played_back_to_back():
    first = add_clips('upload:first',12)
    second = add_clips('upload:second',12)
    set_settings(conserve=False,min_gap_days=0,trials_per_day=3)
    plan(NOW)
    for lane in ('main','trial'):
        order = ['first' if cid in first else 'second' for cid in lane_order(lane)]
        assert len(order)==24 and longest_run(order)<=2
        assert 'second' in order[:4] and 'first' in order[:4]

def test_keep_together_set_posts_next_in_filename_order():
    add_clips('upload:old',8)
    promo = add_clips('upload:promo',4,mode='next')
    set_settings(conserve=False,min_gap_days=0)
    plan(NOW)
    assert lane_order()[:4]==promo

def test_same_song_is_never_back_to_back_when_there_is_an_alternative():
    # Seven clips of one track, five other tracks, all from one set.
    ids = add_clips('upload:night',12,song=lambda i:'Big Tune' if i<7 else '')
    set_settings(conserve=False,min_gap_days=0,trials_per_day=3)
    plan(NOW)
    for lane in ('main','trial'):
        order = ['big' if ids.index(cid)<7 else cid for cid in lane_order(lane)]
        # 7 of 12 cannot all be separated; it must alternate as far as the stock allows.
        repeats = sum(a==b=='big' for a,b in zip(order,order[1:]))
        assert repeats<=2, order

def test_audio_matches_group_songs_but_operator_names_win():
    a,b,d = add_clips('upload:x',3)
    with connect(True) as db:
        db.execute("INSERT INTO song_matches(a,b,score) VALUES(?,?,90)",tuple(sorted((a,b))))
        assert songs.groups(db)[a]==songs.groups(db)[b]!=songs.groups(db)[d]
        db.execute("UPDATE clips SET song='Track One' WHERE id=?",(a,))
        db.execute("UPDATE clips SET song='Track Two' WHERE id=?",(b,))
        g = songs.groups(db)
        assert g[a]!=g[b]
        db.execute("UPDATE clips SET song=' track   one ' WHERE id=?",(d,))
        assert songs.groups(db)[d]==songs.groups(db)[a]

def synth(path, seed, seconds, start, noise):
    sr = 22050
    rng = np.random.default_rng(seed)
    roots, melody = rng.integers(0,12,8), rng.integers(0,12,64)
    t = np.arange(int(seconds*sr))/sr + start
    beat = 60/124
    chord = roots[((t//(4*beat))%8).astype(int)]
    out = sum(np.sin(2*np.pi*220*2**((chord+iv)/12)*t)*.2 for iv in (0,4,7))
    out += .25*np.sin(2*np.pi*440*2**(melody[((t//(beat/2))%64).astype(int)]/12)*t)*np.exp(-(t%(beat/2))*8)
    out += .5*np.sin(2*np.pi*55*t)*np.exp(-(t%beat)*20) + noise*np.random.default_rng(seed+9).normal(0,1,len(t))
    with wave.open(str(path),'wb') as w:
        w.setnchannels(1);w.setsampwidth(2);w.setframerate(sr)
        w.writeframes((out/np.abs(out).max()*26000).astype(np.int16).tobytes())

@needs_ffmpeg
def test_fingerprints_find_the_same_song_in_different_moments(tmp_path):
    if b'chromaprint' not in subprocess.run(['ffmpeg','-hide_banner','-muxers'],capture_output=True).stdout:
        pytest.skip('FFmpeg built without Chromaprint')
    ids = add_clips('upload:audio',4)
    with connect(True) as db:
        db.execute('UPDATE clips SET media_version=1')
    for cid,(seed,start,noise) in zip(ids,[(1,0,0),(1,70,.3),(2,0,0),(1,33,.6)]):
        wav = tmp_path/(cid+'.wav')
        synth(wav,seed,20,start,noise)
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=64x64:d=20','-i',str(wav),'-shortest',
                        '-c:v','libx264','-c:a','aac',str(c.DATA/'media'/(cid+'.mp4'))],check=True)
    songs.analyse_pending()
    with connect() as db:
        pairs = {(r['a'],r['b']):r['score'] for r in db.execute('SELECT * FROM song_matches')}
        groups = songs.groups(db)
    same = {ids[0],ids[1],ids[3]}
    assert {groups[x] for x in same}=={groups[ids[0]]} and groups[ids[2]]!=groups[ids[0]]
    assert all(set(k)<=same for k in pairs) and all(60<=v<=100 for v in pairs.values())

def test_silence_is_not_a_song():
    assert songs.fingerprint('/nonexistent.mp4') is None
    assert songs.match_score(None,np.arange(100,dtype='<u4'))==0

def login(client):
    assert client.post('/api/login',json={'password':c.PASSWORD}).status_code==200
    return {'x-csrf-token':client.get('/api/state').json()['csrf']}

def test_library_shows_posted_clips_and_same_song_matches():
    a,b,d = add_clips('upload:lib',3)
    with connect(True) as db:
        db.execute("INSERT INTO song_matches(a,b,score) VALUES(?,?,87)",tuple(sorted((a,b))))
    plan(NOW)
    with connect(True) as db:
        db.execute("UPDATE jobs SET status='published',published_at=?,permalink='https://instagram.test/p/1' WHERE clip_id=? AND lane='main'",(NOW,a))
    with TestClient(app) as client:
        h = login(client)
        rows = {r['id']:r for r in client.get('/api/clips').json()}
        assert rows[a]['posts'][0]['lane']=='main' and not rows[b]['posts']
        assert rows[a]['similar']==[{'id':b,'name':'upload:lib-01.mp4','score':87}] and rows[a]['song_clips']==1
        assert {r['id'] for r in client.get('/api/clips?view=posted').json()}=={a}
        assert {r['id'] for r in client.get('/api/clips?view=unposted').json()}=={b,d}
        assert {r['id'] for r in client.get('/api/clips?view=same_song').json()}=={a,b}
        assert client.patch('/api/clips/'+d,headers=h,json={'song':'  Big   Tune '}).status_code==200
        assert client.get('/api/clips?only='+d).json()[0]['song']=='Big Tune'
        sets = client.get('/api/batches').json()
        assert sets[0]['clips']==3 and sets[0]['posted']==1
        assert client.patch('/api/batches/upload:lib',headers=h,json={'mode':'next'}).status_code==200
        assert client.patch('/api/batches/upload:nope',headers=h,json={'mode':'next'}).status_code==404

def test_upload_creates_a_named_set_and_scan_files_it():
    with TestClient(app) as client:
        h = login(client)
        r = client.post('/api/upload?batch=abc123def4&mode=next&batch_name=Mojo%27s',headers=h,files={'file':('set.mp4',b'not really a video','video/mp4')})
        assert r.status_code==200
        assert client.post('/api/upload?batch=../../x',headers=h,files={'file':('set.mp4',b'x','video/mp4')}).status_code==422
    with connect() as db:
        row = db.execute("SELECT * FROM intake_batches WHERE id='upload:abc123def4'").fetchone()
    assert row['name']=="Mojo's" and row['mode']=='next'
    assert len(list((c.DATA/'uploads'/'abc123def4').iterdir()))==1

def test_folder_subdirectories_are_sets():
    from app import media
    assert media.batch_for(c.FOOTAGE/'Mojo 14 Nov'/'a.mp4',NOW)==('folder:Mojo 14 Nov','Mojo 14 Nov')
    assert media.batch_for(c.FOOTAGE/'a.mp4',NOW)[0].startswith('folder:')
    assert media.batch_for(c.DATA/'uploads'/'abc'/'a.mp4',NOW)[0]=='upload:abc'


def kinds_in_order(lane, ids_by_kind):
    lookup = {cid:kind for kind,ids in ids_by_kind.items() for cid in ids}
    return [lookup[cid] for cid in lane_order(lane)]

def test_talking_clips_are_sprinkled_in_with_their_gap():
    dj = add_clips('upload:dj',14)
    talk = add_clips('upload:talk',4)
    with connect(True) as db:
        db.execute("UPDATE intake_batches SET kind='talking' WHERE id='upload:talk'")
    set_settings(conserve=False,min_gap_days=0,trials_per_day=3,content_mix={'talking':{'lanes':'both','every':3},'bts':{'lanes':'both','every':3},'promo':{'lanes':'main','every':4}})
    plan(NOW)
    for lane in ('main','trial'):
        order = kinds_in_order(lane,{'dj':dj,'talk':talk})
        spots = [i for i,k in enumerate(order) if k=='talk']
        assert len(spots)==4 and all(b-a>=3 for a,b in zip(spots,spots[1:])), order
        assert spots[0] < 9  # Sprinkled through, not dumped at the end.

def test_promo_goes_to_the_grid_only_and_lane_changes_follow_settings():
    add_clips('upload:dj',4)
    promo = add_clips('upload:promo',2)
    with connect(True) as db:
        db.execute("UPDATE intake_batches SET kind='promo' WHERE id='upload:promo'")
    set_settings(conserve=False,min_gap_days=0)
    plan(NOW)
    with connect() as db:
        lanes = lambda cid:{r[0] for r in db.execute('SELECT lane FROM jobs WHERE clip_id=?',(cid,))}
        assert lanes(promo[0])=={'main'}
    set_settings(content_mix={'talking':{'lanes':'both','every':3},'bts':{'lanes':'both','every':3},'promo':{'lanes':'both','every':4}})
    plan(NOW)
    with connect() as db:
        assert {r[0] for r in db.execute('SELECT lane FROM jobs WHERE clip_id=?',(promo[0],))}=={'main','trial'}
        db.execute("UPDATE jobs SET caption_locked=1,caption='Hand edited' WHERE clip_id=? AND lane='trial'",(promo[1],))
        db.commit()
    set_settings(content_mix={'talking':{'lanes':'both','every':3},'bts':{'lanes':'both','every':3},'promo':{'lanes':'main','every':4}})
    plan(NOW)
    with connect() as db:
        assert {r[0] for r in db.execute('SELECT lane FROM jobs WHERE clip_id=?',(promo[0],))}=={'main'}
        # An edited post is never thrown away, but it is no longer scheduled.
        kept = db.execute("SELECT scheduled_at FROM jobs WHERE clip_id=? AND lane='trial'",(promo[1],)).fetchone()
        assert kept is not None and kept[0] is None

def test_a_dated_clip_posts_on_its_day_and_time():
    dj = add_clips('upload:dj',10)
    gig = add_clips('upload:gig',1)[0]
    with connect(True) as db:
        db.execute("UPDATE clips SET post_on='2026-10-24',post_at='19:30' WHERE id=?",(gig,))
    set_settings(conserve=False,min_gap_days=0)
    plan(NOW)
    tz = ZoneInfo('Australia/Perth')
    with connect() as db:
        rows = db.execute('SELECT lane,scheduled_at,reason FROM jobs WHERE clip_id=?',(gig,)).fetchall()
        main_that_day = [r[0] for r in db.execute("SELECT scheduled_at FROM jobs WHERE lane='main' AND scheduled_at IS NOT NULL")
                         if datetime.fromtimestamp(r[0],tz).date().isoformat()=='2026-10-24']
    for lane,ts,reason in rows:
        assert datetime.fromtimestamp(ts,tz).strftime('%Y-%m-%d %H:%M')=='2026-10-24 19:30' and reason.startswith('Dated')
    assert len(main_that_day)==1  # The dated post takes the day's single grid slot.

def test_posting_windows_hold_clips_until_they_open_and_explain_when_they_close():
    add_clips('upload:dj',10)
    later, expired = add_clips('upload:promo',2)
    with connect(True) as db:
        db.execute("UPDATE clips SET not_before='2026-10-27' WHERE id=?",(later,))
        db.execute("UPDATE clips SET not_after='2026-10-19' WHERE id=?",(expired,))
    set_settings(conserve=False,min_gap_days=0)
    plan(NOW)
    tz = ZoneInfo('Australia/Perth')
    with connect() as db:
        for ts, in db.execute('SELECT scheduled_at FROM jobs WHERE clip_id=?',(later,)):
            assert ts and datetime.fromtimestamp(ts,tz).date().isoformat()>='2026-10-27'
        for ts,reason in db.execute('SELECT scheduled_at,reason FROM jobs WHERE clip_id=?',(expired,)):
            assert ts is None and 'window ended' in reason

def test_timing_and_type_validation():
    cid = add_clips('upload:x',1)[0]
    with TestClient(app) as client:
        h = login(client)
        patch = lambda body:client.patch('/api/clips/'+cid,headers=h,json=body)
        assert patch({'post_on':'2020-01-01'}).status_code==422
        assert patch({'post_on':'2099-12-24','post_at':'25:00'}).status_code==422
        assert patch({'post_at':'19:00'}).status_code==422
        assert patch({'not_before':'2099-12-10','not_after':'2099-12-01'}).status_code==422
        assert patch({'kind':'karaoke'}).status_code==422
        assert patch({'post_on':'2099-12-24','post_at':'19:00','kind':'promo'}).status_code==200
        row = client.get('/api/clips?only='+cid).json()[0]
        assert (row['kind'],row['own_kind'],row['set_kind'],row['post_on'])==('promo','promo','performance','2099-12-24')
        assert {r['id'] for r in client.get('/api/clips?view=dated').json()}=={cid}
        assert {r['id'] for r in client.get('/api/clips?view=other').json()}=={cid}
        assert patch({'kind':'','post_on':'','post_at':''}).status_code==200
        assert client.get('/api/clips?only='+cid).json()[0]['kind']=='performance'
        assert client.patch('/api/batches/upload:x',headers=h,json={'kind':'talking'}).status_code==200
        assert client.get('/api/clips?only='+cid).json()[0]['kind']=='talking'
        bad = {'talking':{'lanes':'everywhere','every':3},'bts':{'lanes':'both','every':3},'promo':{'lanes':'main','every':4}}
        assert client.patch('/api/settings',headers=h,json={'values':{'content_mix':bad}}).status_code==422
        assert client.patch('/api/settings',headers=h,json={'values':{'content_mix':{'talking':{'lanes':'main','every':5}}}}).status_code==200
        mix = client.get('/api/state').json()['settings']['content_mix']
        assert mix['talking']=={'lanes':'main','every':5} and mix['promo']['lanes']=='main'

def test_folder_names_suggest_clip_types():
    from app.media import guess_kind
    assert [guess_kind(n) for n in ('Talking to camera','BTS Mojo','Promo NYE','Mojo 14 Nov')]==['talking','bts','promo','performance']
