"""Batch caption rewrites run on the server, use few provider calls, and keep going until done."""
import json
import time
import httpx
import pytest
from fastapi.testclient import TestClient
from app import ai, rewrites, config as c
from app.db import connect, settings
from app.main import app
from conftest import seed, set_settings

@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(rewrites, 'pause', lambda s: None)
    monkeypatch.setattr(c, 'AI_KEY', 'test-only-key')
    set_settings(ai_model='test/model')

def wait(batch, timeout=10):
    end = time.time()+timeout
    while time.time() < end:
        s = rewrites.summary(batch)
        if s['status'] != 'running':
            return s
        time.sleep(.02)
    raise AssertionError('batch did not finish')

def captions():
    with connect() as db:
        return dict(db.execute('SELECT id,caption FROM clips'))

def test_batches_caption_five_per_request_and_retry_misses_alone(monkeypatch):
    ids = seed(7)
    calls = {'many': [], 'one': []}
    def many(clips, s):
        calls['many'].append(len(clips))
        # The model gets one clip wrong in the first request.
        return {clip['id']: ('Fresh caption %s.' % clip['name'], 'DJ set') for clip in clips if clip['id'] != ids[1]}
    def one(clip, s):
        calls['one'].append(clip['id'])
        return 'On its own %s.' % clip['name'], 'DJ set'
    monkeypatch.setattr(ai, 'generate_many', many)
    monkeypatch.setattr(ai, 'generate', one)
    result = wait(rewrites.start(ids))
    assert result['status'] == 'done' and result['done'] == 7 and result['failed'] == 0
    assert calls == {'many': [5, 2], 'one': [ids[1]]}
    assert captions()[ids[1]].startswith('On its own')

def test_temporary_failures_retry_until_everything_is_done(monkeypatch):
    ids = seed(3)
    attempts = []
    def flaky(clips, s):
        attempts.append(1)
        if len(attempts) < 4:
            raise ai.provider_error(502)
        return {clip['id']: ('Back again %s.' % clip['name'], 'DJ set') for clip in clips}
    monkeypatch.setattr(ai, 'generate_many', flaky)
    result = wait(rewrites.start(ids))
    assert result['status'] == 'done' and result['done'] == 3 and len(attempts) == 4

def test_problems_retrying_cannot_fix_pause_and_resume_carries_on(monkeypatch):
    ids = seed(2)
    broken = [True]
    def provider(clips, s):
        if broken[0]:
            raise ai.provider_error(402)
        return {clip['id']: ('Paid up %s.' % clip['name'], 'DJ set') for clip in clips}
    monkeypatch.setattr(ai, 'generate_many', provider)
    batch = rewrites.start(ids)
    paused = wait(batch)
    assert paused['status'] == 'paused' and 'credit' in paused['message'] and paused['done'] == 0
    with pytest.raises(RuntimeError):
        rewrites.start(ids)  # One batch at a time.
    broken[0] = False
    rewrites.control(batch, 'resume')
    assert wait(batch)['done'] == 2

def test_clips_that_keep_breaking_rules_fail_without_stopping_the_rest(monkeypatch):
    ids = seed(3)
    monkeypatch.setattr(ai, 'generate_many', lambda clips, s: {clip['id']: ('Fine %s.' % clip['name'], 'DJ') for clip in clips if clip['id'] != ids[0]})
    def stubborn(clip, s):
        raise ai.CaptionError('The model broke a caption rule twice. No new caption was saved.', 300)
    monkeypatch.setattr(ai, 'generate', stubborn)
    result = wait(rewrites.start(ids))
    assert result['status'] == 'done' and result['done'] == 2 and result['failed'] == 1
    assert 'still need a rewrite' in result['message']

def test_interrupted_batches_resume_on_start(monkeypatch):
    ids = seed(2)
    monkeypatch.setattr(ai, 'generate_many', lambda clips, s: {clip['id']: ('Resumed %s.' % clip['name'], 'DJ') for clip in clips})
    with connect(True) as db:
        db.execute("INSERT INTO rewrite_batches(id,created_at,updated_at,status,total) VALUES('old',0,0,'running',2)")
        db.executemany("INSERT INTO rewrite_items(batch_id,clip_id,position) VALUES('old',?,?)", [(cid, i) for i, cid in enumerate(ids)])
    rewrites.resume_all()
    assert wait('old')['done'] == 2

def test_cancel_keeps_finished_captions(monkeypatch):
    ids = seed(1)
    with connect(True) as db:
        db.execute("INSERT INTO rewrite_batches(id,created_at,updated_at,status,total) VALUES('b',0,0,'paused',1)")
        db.execute("INSERT INTO rewrite_items(batch_id,clip_id,position) VALUES('b',?,0)", (ids[0],))
    rewrites.control('b', 'cancel')
    assert rewrites.summary('b')['status'] == 'cancelled'

def test_one_request_captions_several_clips_with_the_usual_rules(monkeypatch):
    ids = seed(3, status='captioning')
    with connect(True) as db:
        clips = [dict(r) for r in db.execute('SELECT * FROM clips')]
    s = settings()
    s.update(caption_blocklist='trading', hashtags_per_post=0)
    monkeypatch.setattr(ai, 'model_catalog', lambda: [])
    seen = []
    def provider(request):
        body = json.loads(request.content); seen.append(body)
        entries = [{'clip': 1, 'caption': 'Late one, worth it.', 'category': 'DJ set', 'hashtags': []},
                   {'clip': 2, 'caption': 'Trading places tonight.', 'category': 'DJ set', 'hashtags': []},
                   {'clip': 3, 'caption': 'Late one, worth it.', 'category': 'DJ set', 'hashtags': []},
                   {'clip': 9, 'caption': 'Not a real clip.', 'category': 'x', 'hashtags': []}]
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps({'captions': entries})}}]})
    original = httpx.Client
    monkeypatch.setattr(ai.httpx, 'Client', lambda **kw: original(transport=httpx.MockTransport(provider), **kw))
    results = ai.generate_many(clips, s)
    # Clip 2 breaks the word ban and clip 3 repeats clip 1, so only clip 1 is accepted.
    assert list(results) == [clips[0]['id']] and len(seen) == 1
    assert 'captioning 3 separate clips' in seen[0]['messages'][0]['content']

def test_api_starts_and_reports_a_batch(monkeypatch):
    ids = seed(2)
    monkeypatch.setattr(ai, 'generate_many', lambda clips, s: {clip['id']: ('Via API %s.' % clip['name'], 'DJ') for clip in clips})
    with TestClient(app) as client:
        assert client.post('/api/login', json={'password': c.PASSWORD}).status_code == 200
        h = {'x-csrf-token': client.get('/api/state').json()['csrf']}
        started = client.post('/api/rewrite-batches', headers=h, json={'clips': ids, 'instructions': 'Be brief.'})
        assert started.status_code == 200
        batch = started.json()['id']
        assert wait(batch)['done'] == 2
        latest = client.get('/api/rewrite-batches/latest').json()['batch']
        assert latest['id'] == batch and latest['status'] == 'done' and [i['status'] for i in latest['items']] == ['done', 'done']
        assert client.post(f'/api/rewrite-batches/{batch}/cancel', headers=h).status_code == 409
        assert client.post('/api/rewrite-batches/nope/resume', headers=h).status_code == 404
