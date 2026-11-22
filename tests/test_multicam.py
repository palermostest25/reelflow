"""Multicam tab: chunked uploads, validation, AI notes, engine command, progress and a real edit."""
import json
import shutil
import subprocess
import time
import wave
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from app import multicam, ai, media, config as c
from app.db import connect
from app.main import app
from conftest import set_settings

needs_ffmpeg = pytest.mark.skipif(not shutil.which('ffmpeg'), reason='ffmpeg required')

def login(client):
    assert client.post('/api/login', json={'password': c.PASSWORD}).status_code == 200
    return {'x-csrf-token': client.get('/api/state').json()['csrf']}

def test_tab_is_off_until_enabled_and_uploads_arrive_in_resumable_chunks():
    with TestClient(app) as client:
        h = login(client)
        assert client.post('/api/multicam/jobs', headers=h, json={'name': 'Night'}).status_code == 409
        set_settings(multicam_enabled=True)
        job = client.post('/api/multicam/jobs', headers=h, json={'name': 'Night'}).json()['id']
        up = lambda name, offset, data, final=False: client.post(f'/api/multicam/jobs/{job}/upload?name={name}&offset={offset}&final={str(final).lower()}',
                                                                 headers={**h, 'content-type': 'application/octet-stream'}, content=data)
        assert up('mix.wav', 0, b'abc').json() == {'received': 3, 'complete': False, 'name': 'mix.wav'}
        # A retried chunk with a stale offset is told where to carry on.
        assert up('mix.wav', 0, b'abc').json()['resend_from'] == 3
        assert up('mix.wav', 3, b'def', True).json()['complete'] is True
        assert (multicam.job_dir(job)/'inputs'/'mix.wav').read_bytes() == b'abcdef'
        assert up('evil.exe', 0, b'x').status_code == 422
        assert up('../../escape.wav', 0, b'x', True).json()['name'] == 'escape.wav'
        bad = client.post(f'/api/multicam/jobs/{job}/start', headers=h, json={'audio': {'upload': 'mix.wav'}, 'cameras': []})
        assert bad.status_code == 422 and 'camera' in bad.json()['detail']
        assert client.post('/api/multicam/jobs/0000000000000000/start', headers=h, json={}).status_code == 404
        listing = client.get('/api/multicam').json()
        assert listing['jobs'][0]['status'] == 'draft' and listing['chunk'] == multicam.CHUNK

def test_files_from_the_footage_multicam_folder_can_be_used_and_are_not_imported_as_clips():
    folder = c.FOOTAGE/'multicam'/'Mojo'
    folder.mkdir(parents=True)
    (folder/'mix.wav').write_bytes(b'x')
    (folder/'wide.mp4').write_bytes(b'x')
    (folder/'notes.txt').write_bytes(b'x')
    assert [s['path'] for s in multicam.sources()] == ['Mojo/mix.wav', 'Mojo/wide.mp4']
    job = multicam.create('From the folder')
    assert multicam.resolve(job, {'source': 'Mojo/wide.mp4'}).name == 'wide.mp4'
    with pytest.raises(ValueError):
        multicam.resolve(job, {'source': '../../etc/passwd'})
    media.scan(time.time()); media.scan(time.time()+60)
    with connect() as db:
        assert db.execute('SELECT COUNT(*) FROM clips').fetchone()[0] == 0

def test_ai_notes_become_engine_settings_within_limits(monkeypatch):
    raw = {'summary': 'Set the drop.', 'main_camera': 'B', 'main_share': 1.4, 'pace': 0.1,
           'force_cameras': [{'start': 10, 'end': 20, 'camera': 'B'}, {'start': 15, 'end': 30, 'camera': 'A'}, {'start': 1, 'end': 2, 'camera': 'Z'}],
           'no_clips': [[0, 300], [5, 5.2]], 'drops': [[2530, 2600]], 'breakdowns': [['x', 3]]}
    guide = multicam.check_guidance(raw, {'A', 'B'}, 3600)
    assert guide['main_camera'] == 'B' and guide['main_share'] == 0.9 and guide['pace'] == 0.4
    assert guide['force_cameras'] == [{'start': 10.0, 'end': 20.0, 'camera_id': 'B'}]  # The overlapping range is dropped.
    assert guide['no_clips'] == [[0.0, 300.0]] and guide['drops'] == [[2530.0, 2600.0]] and guide['breakdowns'] == []
    # Through the caption provider, with the operator's notes as data.
    monkeypatch.setattr(c, 'AI_KEY', 'test-only-key')
    set_settings(ai_model='test/model')
    monkeypatch.setattr(ai, 'model_catalog', lambda: [])
    seen = []
    def provider(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(raw)}}]})
    original = httpx.Client
    monkeypatch.setattr(ai.httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(provider), **kw))
    options = {'notes': 'Big drop at 42:10', 'main': 0, 'pace': 'balanced'}
    out = multicam.guidance(options, [{'id': 'A', 'label': 'Wide'}, {'id': 'B', 'label': 'Booth'}], 3600)
    assert out['drops'] == [[2530.0, 2600.0]] and 'Big drop at 42:10' in seen[0]['messages'][1]['content'][0]['text']
    assert multicam.guidance({**options, 'notes': ''}, [], 10) == {}

def test_engine_command_carries_the_choices(tmp_path):
    job = multicam.create('Late set!')
    options = {'main': 1, 'pace': 'energetic', 'seed': 7, 'quality': '1080', 'looks': 'auto', 'clips': 12, 'full': False,
               '_audio': tmp_path/'mix.wav'}
    cameras = [{'id': 'A', 'label': 'Wide', 'path': '/x/a.mp4'}, {'id': 'B', 'label': 'Booth', 'path': '/x/b.mp4'}]
    guide = {'pace': 2.0, 'drops': [[60, 90]], 'no_clips': [[0, 30]], 'force_cameras': [{'start': 5, 'end': 9, 'camera_id': 'A'}]}
    args = multicam.command(job, options, cameras, guide, tmp_path, multicam.output_name('Late set!'))
    joined = ' '.join(args)
    assert '--main-camera B' in joined and '--cut-scale 0.360' in joined and '--clips-only' in joined
    assert '--drop 60,90' in joined and '--clip-exclude 0,30' in joined and '--clip-count 12' in joined
    assert '--clip-filters auto' in joined and '--engine 2' in joined and '--output-name Late set' in joined
    project = json.loads((tmp_path/'project.json').read_text())
    assert [cam['id'] for cam in project['cameras']] == ['A', 'B'] and project['shot_overrides'][0]['camera_id'] == 'A'

def test_progress_follows_the_engine_output():
    job = multicam.create('P')
    p = multicam.Progress(job)
    p.feed('Preparing audio bounce for correlation …')
    assert p.stage.startswith('Syncing')
    p.feed('Deliverable 1/3 weight=60.000 done=0.000 total=90.000: full edit')
    p.feed('Rendering 10/20: 00:00:10 a.mp4 (1.0s)')
    assert abs(p.value - (0.2 + 0.8*30/90)) < 1e-6 and p.stage == 'Rendering the full edit'
    p.feed('Deliverable 2/3 weight=15.000 done=60.000 total=90.000: Drop clip 1 (00:01:00)')
    assert p.stage == 'Rendering Drop clip 1 (00:01:00)' and p.value > 0.7

def test_interrupted_edits_are_marked_and_can_be_retried():
    job = multicam.create('Interrupted')
    with connect(True) as db:
        db.execute("UPDATE multicam_jobs SET status='running' WHERE id=?", (job,))
    multicam.recover()
    assert multicam.get(job)['status'] == 'interrupted'
    with pytest.raises(ValueError, match='Start this edit from the form'):
        multicam.control(job, 'retry')
    multicam.control(job, 'delete')
    with pytest.raises(LookupError):
        multicam.get(job)

def music(path, seconds, seed=7):
    sr = 22050
    rng = np.random.default_rng(seed)
    roots, melody = rng.integers(0, 12, 8), rng.integers(0, 12, 64)
    t = np.arange(int(seconds*sr))/sr
    beat = 60/124
    chord = roots[((t//(4*beat)) % 8).astype(int)]
    out = sum(np.sin(2*np.pi*220*2**((chord+iv)/12)*t)*.2 for iv in (0, 4, 7))
    out += .25*np.sin(2*np.pi*440*2**(melody[((t//(beat/2)) % 64).astype(int)]/12)*t)*np.exp(-(t % (beat/2))*8)
    out += .5*np.sin(2*np.pi*55*t)*np.exp(-(t % beat)*20)
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((out/np.abs(out).max()*26000).astype(np.int16).tobytes())

@needs_ffmpeg
def test_a_real_multicam_edit_lands_in_the_library(tmp_path):
    set_settings(multicam_enabled=True)
    job = multicam.create('Test night')
    inputs = multicam.job_dir(job)/'inputs'
    music(inputs/'mix.wav', 50)
    for name, src, delay in (('wide.mp4', 'testsrc2=s=320x180:r=30:d=52', 0), ('booth.mp4', 'smptebars=s=180x320:r=30:d=50', 1500)):
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', src, '-i', str(inputs/'mix.wav'), '-map', '0:v', '-map', '1:a',
                        '-af', f'adelay={delay}|{delay}', '-shortest', '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac',
                        str(inputs/name)], check=True)
    multicam.kick = lambda: None
    multicam.start(job, {'audio': {'upload': 'mix.wav'}, 'cameras': [{'upload': 'wide.mp4', 'label': 'Wide'}, {'upload': 'booth.mp4', 'label': 'Booth'}],
                         'main': 0, 'clips': 6, 'full': True, 'youtube': True, 'queue': True, 'mode': 'mix', 'pace': 'balanced',
                         'looks': 'auto', 'quality': '1080', 'notes': 'Recorded at Mojo.'})
    multicam.run(job)
    row = multicam.public(multicam.get(job))
    assert row['status'] == 'done', row['message']+'\n'.join(row['log'])
    result = row['result']
    assert result['bpm'] and abs(result['bpm']-124) < 2 and result['full'] == 'Test night.mp4' and not result['youtube']
    assert multicam.full_path(job).is_file() and 'download' in row['message']
    assert result['clips'] and result['queued']
    with connect() as db:
        clips = [dict(r) for r in db.execute('SELECT * FROM clips')]
        batch = db.execute("SELECT * FROM intake_batches WHERE name='Test night · multicam'").fetchone()
    assert len(clips) == len(result['clips']) and batch['mode'] == 'mix'
    assert all(cl['status'] == 'processing' and cl['batch'] == batch['id'] and 'Recorded at Mojo.' in cl['notes'] for cl in clips)
    # The normal pipeline prepares them like any upload.
    media.scan(time.time())
    with connect() as db:
        assert {r[0] for r in db.execute('SELECT status FROM clips')} <= {'captioning', 'ready'}
    duration, width, height = media.video_info(multicam.full_path(job))
    assert (width, height) == (1920, 1080) and abs(duration-50) < 1
    assert not any((multicam.job_dir(job)/'inputs').iterdir())  # Uploaded sources are cleaned up.
