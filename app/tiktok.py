"""TikTok: connect an account and post clips through the Content Posting API.

Like YouTube, every Instagram grid Reel can go to TikTok at the same time (on by default) and follows it
when it moves; posts dragged in by hand keep their own time. Videos are sent as file uploads in
chunks, so TikTok never needs to fetch from this server.

TikTok's Content Sharing Guidelines need the operator to see and confirm every post: the creator's
nickname, a privacy choice with no preset, interaction toggles that start off, commercial content
disclosure and the music usage consent. Posts therefore wait for approval in the TikTok review panel
(several can be approved at once with the same explicit choices). Apps TikTok has not audited can only
post privately (SELF_ONLY).
"""
import base64
import hashlib
import json
import math
import secrets
import time
from urllib.parse import urlencode
import httpx
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from . import config as c
from .db import connect, secret, event, settings

AUTH = 'https://www.tiktok.com/v2/auth/authorize/'
API = 'https://open.tiktokapis.com/v2'
SCOPE = 'user.info.basic,video.publish'
MIN_CHUNK, MAX_CHUNK = 5*1024*1024, 64*1024*1024
PRIVACY = ('SELF_ONLY', 'MUTUAL_FOLLOW_FRIENDS', 'FOLLOWER_OF_CREATOR', 'PUBLIC_TO_EVERYONE')


class Preferences(BaseModel):
    model_config = ConfigDict(extra='ignore', strict=True)  # Older saved preferences had preset privacy fields.
    paused: bool = True
    mirror_grid: bool = True
    caption_suffix: str = Field(default='', max_length=300)


class TikTokError(Exception):
    def __init__(self, message, retry=False):
        super().__init__(message)
        self.retry = retry


def preferences(db):
    row = db.execute("SELECT value FROM runtime WHERE key='tiktok_preferences'").fetchone()
    return Preferences.model_validate_json(row[0]) if row else Preferences()


def save_preferences(db, prefs):
    db.execute("INSERT OR REPLACE INTO runtime VALUES('tiktok_preferences',?)", (prefs.model_dump_json(),))


def app_keys(db=None):
    return json.loads(secret('tiktok_app', db=db) or '{}')


def redirect_uri():
    return c.PUBLIC_URL+'/auth/tiktok/callback'


def authorization_url(client_key, state, verifier):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    return AUTH+'?'+urlencode({'client_key': client_key, 'scope': SCOPE, 'response_type': 'code', 'redirect_uri': redirect_uri(),
                               'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})


def api_error(response):
    try:
        body = response.json()
    except ValueError:
        body = {}
    err = body.get('error') if isinstance(body.get('error'), dict) else body
    code = str(err.get('code') or err.get('error') or response.status_code)
    message = str(err.get('message') or err.get('error_description') or '')[:200]
    retry = response.status_code >= 500 or response.status_code == 429 or code in ('rate_limit_exceeded', 'internal_error')
    return TikTokError(f'TikTok refused the request ({code})' + (f': {message}' if message else '.'), retry)


def token_request(client, **form):
    keys = app_keys()
    response = client.post(API+'/oauth/token/', data={'client_key': keys.get('client_key', ''), 'client_secret': keys.get('client_secret', ''), **form},
                           headers={'Content-Type': 'application/x-www-form-urlencoded'})
    data = response.json() if response.content else {}
    if response.status_code >= 400 or data.get('error'):
        raise TikTokError('TikTok sign-in failed: ' + str(data.get('error_description') or data.get('error') or response.status_code)[:200])
    return data


def store_tokens(db, data, display=None):
    now = time.time()
    secret('tiktok_tokens', json.dumps({'access_token': data['access_token'], 'refresh_token': data['refresh_token']}), db=db)
    db.execute("""INSERT INTO tiktok_account(id,open_id,display_name,expires_at,refresh_expires_at,error) VALUES(1,?,?,?,?,'')
        ON CONFLICT(id) DO UPDATE SET open_id=excluded.open_id,display_name=COALESCE(excluded.display_name,tiktok_account.display_name),
        expires_at=excluded.expires_at,refresh_expires_at=excluded.refresh_expires_at,error=''""",
               (data.get('open_id', ''), display, now+data.get('expires_in', 86400), now+data.get('refresh_expires_in', 365*86400)))


def connect_account(code, verifier):
    with httpx.Client(timeout=45) as client:
        data = token_request(client, code=code, grant_type='authorization_code', redirect_uri=redirect_uri(), code_verifier=verifier)
        info = client.get(API+'/user/info/', params={'fields': 'open_id,display_name'},
                          headers={'Authorization': 'Bearer '+data['access_token']})
        name = (info.json().get('data', {}).get('user', {}) or {}).get('display_name') if info.status_code == 200 else None
    with connect(True) as db:
        store_tokens(db, data, name or 'TikTok account')
        event('Connected TikTok' + (f' ({name})' if name else '') + '. Posts are private until TikTok approves your app.', db=db)


def access_token():
    """A valid access token, refreshed when it is within an hour of expiring (they last a day)."""
    with connect() as db:
        account = db.execute('SELECT * FROM tiktok_account WHERE id=1').fetchone()
        tokens = json.loads(secret('tiktok_tokens', db=db) or '{}')
    if not account or not tokens:
        raise TikTokError('Connect TikTok in Settings.')
    if account['expires_at'] - time.time() > 3600:
        return tokens['access_token']
    with httpx.Client(timeout=45) as client:
        try:
            data = token_request(client, grant_type='refresh_token', refresh_token=tokens['refresh_token'])
        except TikTokError as exc:
            with connect(True) as db:
                db.execute("UPDATE tiktok_account SET error=? WHERE id=1", ('Reconnect TikTok: '+str(exc),))
            raise
    with connect(True) as db:
        store_tokens(db, data)
    return data['access_token']


def disconnect():
    with connect(True) as db:
        db.execute('DELETE FROM tiktok_account')
        db.execute("DELETE FROM secrets WHERE name='tiktok_tokens'")
        db.execute("DELETE FROM tiktok_jobs WHERE status IN ('draft','queued') AND mirror_of IS NOT NULL")


def add_job(db, clip, at=None, mirror_of=None, forced=False):
    existing = db.execute('SELECT * FROM tiktok_jobs WHERE clip_id=?', (clip['id'],)).fetchone()
    if existing:
        return existing['id']
    return db.execute("""INSERT INTO tiktok_jobs(clip_id,caption,status,scheduled_at,pinned,forced,mirror_of,created_at)
        VALUES(?,?,'queued',?,1,?,?,?)""", (clip['id'], clip['caption'], at, int(forced), mirror_of, time.time())).lastrowid


def mirror(db, now, horizon=30*86400):
    """A TikTok post beside every scheduled Instagram grid Reel, at the same time."""
    stale = "DELETE FROM tiktok_jobs WHERE mirror_of IS NOT NULL AND forced=0 AND status IN ('draft','queued')"
    connected = db.execute("SELECT 1 FROM tiktok_account WHERE id=1 AND error=''").fetchone()
    if not preferences(db).mirror_grid or not connected:
        db.execute(stale)
        return
    grid = db.execute("""SELECT j.id,j.clip_id,j.caption,j.status,COALESCE(j.published_at,j.scheduled_at) AS at
        FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.lane='main' AND c.disabled=0 AND j.held=0
        AND j.status IN ('queued','retry','processing','publishing','published')
        AND COALESCE(j.published_at,j.scheduled_at) BETWEEN ? AND ?""", (now-86400, now+horizon)).fetchall()
    keep = set()
    for job in grid:
        post = db.execute('SELECT * FROM tiktok_jobs WHERE clip_id=?', (job['clip_id'],)).fetchone()
        if post:
            if post['mirror_of'] == job['id']:
                keep.add(post['id'])
                if post['status'] in ('draft', 'queued') and not post['forced'] and post['scheduled_at'] != job['at']:
                    db.execute("UPDATE tiktok_jobs SET scheduled_at=?,status='queued' WHERE id=?", (job['at'], post['id']))
            continue  # The clip already has its own TikTok post.
        if job['status'] == 'published' and job['at'] < now-3600:
            continue
        clip = db.execute('SELECT * FROM clips WHERE id=?', (job['clip_id'],)).fetchone()
        if not clip or not 3 <= clip['duration'] <= 600:
            continue
        keep.add(add_job(db, {**dict(clip), 'caption': job['caption'] or clip['caption']}, job['at'], job['id']))
    marks = ','.join('?'*len(keep))
    db.execute(stale + (f' AND id NOT IN ({marks})' if keep else ''), tuple(keep))


_creator = {'at': 0, 'data': None}

def creator_info(force=False):
    """The connected creator's nickname, allowed privacy options, disabled interactions and duration limit."""
    if not force and _creator['data'] and time.time()-_creator['at'] < 60:
        return _creator['data']
    token = access_token()
    with httpx.Client(timeout=30) as client:
        response = client.post(API+'/post/publish/creator_info/query/', json={},
                               headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json; charset=UTF-8'})
    data = response.json() if response.content else {}
    if response.status_code >= 400 or (data.get('error', {}).get('code') not in (None, 'ok')):
        raise api_error(response)
    info = data.get('data', {})
    clean = {'nickname': str(info.get('creator_nickname') or ''), 'username': str(info.get('creator_username') or ''),
             'avatar': str(info.get('creator_avatar_url') or ''),
             'privacy_options': [o for o in info.get('privacy_level_options') or [] if o in PRIVACY],
             'comment_disabled': bool(info.get('comment_disabled')), 'duet_disabled': bool(info.get('duet_disabled')),
             'stitch_disabled': bool(info.get('stitch_disabled')),
             'max_seconds': int(info.get('max_video_post_duration_sec') or 600)}
    _creator.update(at=time.time(), data=clean)
    return clean


def approve(job_ids, choices):
    """Record the operator's explicit choices for one or more TikTok posts."""
    privacy = choices.get('privacy')
    if privacy not in PRIVACY:
        raise TikTokError('Choose who can see the post.')
    own, paid = bool(choices.get('brand_own')), bool(choices.get('brand_paid'))
    disclose = bool(choices.get('disclose'))
    if disclose and not (own or paid):
        raise TikTokError('Choose Your brand, Branded content, or both, or turn off disclosure.')
    if disclose and paid and privacy == 'SELF_ONLY':
        raise TikTokError('Branded content can’t be private. Choose a wider audience.')
    info = creator_info()
    if info['privacy_options'] and privacy not in info['privacy_options']:
        raise TikTokError('That audience isn’t available for this account right now.')
    with connect(True) as db:
        for job_id in job_ids:
            caption_text = choices.get('caption') if len(job_ids) == 1 and isinstance(choices.get('caption'), str) else None
            db.execute("""UPDATE tiktok_jobs SET privacy=?,allow_comment=?,allow_duet=?,allow_stitch=?,brand_own=?,brand_paid=?,
                approved=1,approved_at=?,caption=COALESCE(?,caption) WHERE id=? AND status IN ('queued','draft','failed')""",
                       (privacy, int(bool(choices.get('comment')) and not info['comment_disabled']),
                        int(bool(choices.get('duet')) and not info['duet_disabled']),
                        int(bool(choices.get('stitch')) and not info['stitch_disabled']),
                        int(disclose and own), int(disclose and paid), time.time(),
                        caption_text[:2200] if caption_text is not None else None, job_id))
        event(f'Approved {len(job_ids)} TikTok post{"s" if len(job_ids) != 1 else ""} for @{info["username"] or info["nickname"]}.', db=db)


def chunks(size):
    """TikTok's rules: up to 64 MB goes whole; larger files go in 64 MB pieces, the last one taking the rest."""
    if size <= MAX_CHUNK:
        return size, 1
    return MAX_CHUNK, size // MAX_CHUNK


def caption(prefs, text):
    return ((text or '').strip() + ('\n\n' + prefs.caption_suffix.strip() if prefs.caption_suffix.strip() else ''))[:2200]


def start_upload(job, prefs, token):
    path = c.DATA/'media'/(job['clip_id']+'.mp4')
    if not path.is_file():
        raise TikTokError('The clip export is missing. Rebuild it in the Footage library.')
    size = path.stat().st_size
    chunk, count = chunks(size)
    body = {'post_info': {'title': caption(prefs, job['caption']), 'privacy_level': job['privacy'],
                          'disable_comment': not job['allow_comment'], 'disable_duet': not job['allow_duet'],
                          'disable_stitch': not job['allow_stitch'],
                          'brand_organic_toggle': bool(job['brand_own']), 'brand_content_toggle': bool(job['brand_paid'])},
            'source_info': {'source': 'FILE_UPLOAD', 'video_size': size, 'chunk_size': chunk, 'total_chunk_count': count}}
    with httpx.Client(timeout=60) as client:
        response = client.post(API+'/post/publish/video/init/', json=body,
                               headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json; charset=UTF-8'})
        data = response.json() if response.content else {}
        if response.status_code >= 400 or (data.get('error', {}).get('code') not in (None, 'ok')):
            raise api_error(response)
        upload_url, publish_id = data['data']['upload_url'], data['data']['publish_id']
        with path.open('rb') as stream:
            for index in range(count):
                start = index*chunk
                end = size if index == count-1 else start+chunk
                stream.seek(start)
                piece = stream.read(end-start)
                put = client.put(upload_url, content=piece, headers={'Content-Type': 'video/mp4',
                                 'Content-Range': f'bytes {start}-{end-1}/{size}', 'Content-Length': str(len(piece))}, timeout=300)
                if put.status_code not in (200, 201, 206):
                    raise TikTokError(f'TikTok stopped the upload (HTTP {put.status_code}).', retry=True)
    return publish_id


def check_status(publish_id, token):
    with httpx.Client(timeout=45) as client:
        response = client.post(API+'/post/publish/status/fetch/', json={'publish_id': publish_id},
                               headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json; charset=UTF-8'})
    data = response.json() if response.content else {}
    if response.status_code >= 400:
        raise api_error(response)
    info = data.get('data', {})
    ids = info.get('publicaly_available_post_id') or info.get('publicly_available_post_id') or []
    return info.get('status'), str(info.get('fail_reason') or ''), str(ids[0]) if ids else ''


def publish_due(now=None):
    """One step per pass: start the next due upload, or check one that TikTok is processing."""
    now = now or time.time()
    with connect() as db:
        prefs = preferences(db)
        account = db.execute('SELECT * FROM tiktok_account WHERE id=1').fetchone()
        if not account or account['error'] or c.DEMO:
            return False
        job = db.execute("""SELECT * FROM tiktok_jobs WHERE next_attempt<=? AND (status='processing'
            OR (status='queued' AND approved=1 AND scheduled_at<=? AND (?=0 OR forced=1))) ORDER BY status='processing' DESC,scheduled_at LIMIT 1""",
                         (now, now, int(prefs.paused))).fetchone()
    if not job:
        return False
    update = {}
    try:
        token = access_token()
        if job['status'] == 'queued':
            with connect() as db:
                clip = db.execute('SELECT status,disabled FROM clips WHERE id=?', (job['clip_id'],)).fetchone()
            if not clip or clip['disabled'] or clip['status'] != 'ready':
                raise TikTokError('The footage is excluded or not ready. Review it before retrying.')
            info = creator_info(force=True)
            with connect() as db:
                duration = db.execute('SELECT duration FROM clips WHERE id=?', (job['clip_id'],)).fetchone()[0]
            if duration > info['max_seconds']:
                raise TikTokError(f'This account can post videos up to {info["max_seconds"]} seconds on TikTok.')
            if info['privacy_options'] and job['privacy'] not in info['privacy_options']:
                raise TikTokError('The chosen audience is no longer available for this account. Approve the post again.')
            publish_id = start_upload(dict(job), prefs, token)
            update = {'status': 'processing', 'publish_id': publish_id, 'next_attempt': now+20, 'error': ''}
        else:
            status, reason, post_id = check_status(job['publish_id'], token)
            if status == 'PUBLISH_COMPLETE':
                update = {'status': 'published', 'post_id': post_id, 'published_at': now, 'error': ''}
                with connect(True) as db:
                    event('Posted to TikTok. It can take a few minutes to process before it shows on your profile.', db=db)
            elif status == 'FAILED':
                raise TikTokError('TikTok could not publish this video' + (f': {reason}' if reason else '.'))
            elif status == 'SEND_TO_USER_INBOX':
                update = {'status': 'published', 'error': 'Sent to your TikTok inbox to finish posting in the app.', 'published_at': now}
            else:
                update = {'next_attempt': now+30}
    except TikTokError as exc:
        attempts = job['attempts']+1
        update = {'attempts': attempts, 'error': str(exc)}
        if exc.retry and attempts < 6:
            update.update(next_attempt=now+min(3600, 60*2**attempts), status='queued' if job['status'] == 'queued' else job['status'])
        else:
            update['status'] = 'failed'
            with connect(True) as db:
                event('TikTok post failed: '+str(exc), 'error', db=db)
    except httpx.HTTPError:
        update = {'attempts': job['attempts']+1, 'next_attempt': now+300, 'error': 'TikTok could not be reached. Retrying.'}
    with connect(True) as db:
        db.execute('UPDATE tiktok_jobs SET '+','.join(k+'=?' for k in update)+' WHERE id=?', (*update.values(), job['id']))
    return True


def state():
    with connect() as db:
        account = db.execute('SELECT display_name,expires_at,refresh_expires_at,error FROM tiktok_account WHERE id=1').fetchone()
        counts = dict(db.execute('SELECT status,COUNT(*) FROM tiktok_jobs GROUP BY status').fetchall())
        counts['waiting'] = db.execute("SELECT COUNT(*) FROM tiktok_jobs WHERE approved=0 AND status IN ('queued','failed')").fetchone()[0]
        return {'app': bool(app_keys(db).get('client_key')), 'account': dict(account) if account else None,
                'preferences': preferences(db).model_dump(), 'counts': counts, 'redirect_uri': redirect_uri()}
