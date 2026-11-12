"""YouTube intake, scheduling, and resumable uploads."""
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, time as wall_time, timedelta
from pathlib import Path
from subprocess import TimeoutExpired
from typing import Literal
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field

from . import config as c, media, notifications
from .db import connect, event, init, secret, settings

GOOGLE_AUTH = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN = 'https://oauth2.googleapis.com/token'
GOOGLE_API = 'https://www.googleapis.com'
SCOPE = 'https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly'
VIDEO_ID = re.compile(r'[A-Za-z0-9_-]{11}')
CLIENT_ID = re.compile(r'[A-Za-z0-9_-]+\.apps\.googleusercontent\.com')
CHUNK_BYTES = 8 * 1024 * 1024
MAX_SET_BYTES = 256_000_000_000
Visibility = Literal['private', 'unlisted', 'public']


class Preferences(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    paused: bool = True
    auto_shorts: bool = False
    auto_approve: bool = False
    shorts_per_day: int = Field(default=1, ge=0, le=10)
    sets_every_days: int = Field(default=7, ge=0, le=90)
    short_hour: int = Field(default=17, ge=0, le=23)
    set_hour: int = Field(default=18, ge=0, le=23)
    visibility: Visibility = 'private'
    made_for_kids: bool = False
    category: Literal['10', '22', '24'] = '22'
    description_footer: str = Field(default='', max_length=2000)


class YouTubeError(Exception):
    def __init__(self, message, *, retry=False, uncertain=False, auth=False):
        super().__init__(message)
        self.retry = retry
        self.uncertain = uncertain
        self.auth = auth


def preferences(db):
    row = db.execute("SELECT value FROM runtime WHERE key='youtube_preferences'").fetchone()
    return Preferences.model_validate_json(row[0]) if row else Preferences()


def credentials(db):
    return json.loads(secret('youtube_oauth', db=db) or '{}')


def set_folder():
    return c.FOOTAGE / media.YOUTUBE_SET_FOLDER


def upload_folder():
    return c.DATA / 'uploads' / media.YOUTUBE_SET_FOLDER


def redirect_uri():
    return c.PUBLIC_URL + '/auth/youtube/callback'


def authorization_url(client_id, state, challenge):
    return GOOGLE_AUTH + '?' + urlencode({
        'client_id': client_id, 'redirect_uri': redirect_uri(), 'response_type': 'code',
        'access_type': 'offline', 'prompt': 'consent select_account', 'scope': SCOPE,
        'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256',
    })


def google_token(client, creds, **grant):
    response = client.post(GOOGLE_TOKEN, data={
        'client_id': creds['client_id'], 'client_secret': creds['client_secret'], **grant,
    })
    if response.status_code in (400, 401, 403):
        raise YouTubeError('Google rejected the authorization. Check your OAuth settings or reconnect YouTube.', auth=True)
    if not response.is_success:
        raise YouTubeError('Google could not issue an access token. Reelflow will try again.', retry=True)
    try:
        token = response.json()
        if isinstance(token.get('access_token'), str) and token['access_token']:
            return token
    except (ValueError, AttributeError):
        pass
    raise YouTubeError('Google returned an incomplete authorization response.', retry=True)


def connect_channel(code, verifier, creds):
    with httpx.Client(timeout=20) as client:
        token = google_token(client, creds, grant_type='authorization_code', code=code,
                             code_verifier=verifier, redirect_uri=redirect_uri())
        if not isinstance(token.get('refresh_token'),str) or not token['refresh_token']:
            raise YouTubeError('Google did not grant offline access. Reconnect and grant the requested permissions.')
        response = client.get(GOOGLE_API+'/youtube/v3/channels',
                              params={'part':'snippet','mine':'true','maxResults':50},
                              headers={'Authorization':'Bearer '+token['access_token']})
        if not response.is_success:
            raise YouTubeError('Could not read your YouTube channel. Enable YouTube Data API v3 and grant both permissions.')
        try:
            data = response.json()
            channels = data['items']
            if len(channels) != 1 or data.get('nextPageToken'):
                raise YouTubeError('Google did not return one channel. Reconnect and choose the intended YouTube channel.')
            channel_id, title = channels[0]['id'], channels[0]['snippet']['title']
        except (KeyError, TypeError, ValueError, AttributeError):
            raise YouTubeError('Google did not return a usable YouTube channel.') from None
    with connect(True) as db:
        if credentials(db) != creds:
            raise YouTubeError('Your Google credentials changed during setup. Start the connection again.')
        old = db.execute('SELECT channel_id FROM youtube_account WHERE id=1').fetchone()
        if old and old['channel_id'] != channel_id:
            raise YouTubeError('This workspace is linked to a different YouTube channel. Choose that channel when signing in.')
        secret('youtube_oauth',json.dumps({**creds,'refresh_token':token['refresh_token']}),db=db)
        db.execute("INSERT OR REPLACE INTO youtube_account VALUES(1,?,?,'')",(channel_id,title))
        event('YouTube channel connected: '+title,db=db)


def title_text(text):
    return re.sub(r'\s+', ' ', text.replace('<','').replace('>','')).strip()[:100]


def source_stamp(stat):
    return f'{stat.st_size}:{stat.st_mtime_ns}'


def job_path(job):
    if job['kind']=='short':
        return c.DATA/'media'/(job['source_key']+'.mp4')
    path = Path(job['source_path'])
    resolved = path.resolve()
    if path.is_symlink() or not any(not root.is_symlink() and resolved.is_relative_to(root.resolve())
                                   for root in (set_folder(),upload_folder())):
        raise YouTubeError('The set is outside the watched folder. Restore it to its original location.')
    return path


def short_info(clip):
    if clip['status']!='ready' or clip['disabled'] or clip['media_version']<media.MEDIA_VERSION:
        raise YouTubeError('Choose an included clip with a finished export and caption.')
    path = c.DATA/'media'/(clip['id']+'.mp4')
    try:
        duration, width, height = media.video_info(path)
    except (ValueError, OSError, TimeoutExpired):
        raise YouTubeError('The clip export is unavailable. Rebuild it from the Footage library.') from None
    if not 0 < duration <= 180 or width > height:
        raise YouTubeError('YouTube Shorts need a square or vertical export up to 3 minutes.')
    return duration


def add_draft(db, prefs, kind, key, name, duration, *, path='', caption='', stamp=None, error='', approve=False):
    default_title = re.sub(r'^\w{10}-', '', Path(name).stem) if path and Path(path).is_relative_to(upload_folder()) else Path(name).stem
    hook = re.split(r'[.!?\n]',caption,maxsplit=1)[0] if caption else default_title.replace('_',' ')
    title = title_text(hook) or ('Short' if kind=='short' else 'Full set')
    description = '\n\n'.join(part for part in (caption,prefs.description_footer.strip()) if part)
    description = description.replace('<','').replace('>','').encode()[:5000].decode(errors='ignore')
    status = 'invalid' if error else 'queued' if approve else 'draft'
    row = db.execute('''INSERT INTO youtube_jobs(kind,source_key,source_path,source_name,duration,title,description,
        visibility,made_for_kids,category,created_at,status,source_stamp,error)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(kind,source_key) DO NOTHING RETURNING id''',
        (kind,key,path,name,duration,title,description,prefs.visibility,int(prefs.made_for_kids),prefs.category,
         time.time(),status,stamp,error)).fetchone()
    if row:
        event(('YouTube set needs attention: ' if error else 'YouTube draft added: ')+name,db=db)
        return row['id']
    return db.execute('SELECT id FROM youtube_jobs WHERE kind=? AND source_key=?',(kind,key)).fetchone()['id']


def scan_sets(now):
    prepared = 0
    for root in (set_folder(),upload_folder()):
        if root.is_symlink():
            continue
        for path in sorted(root.rglob('*')):
            relative = path.relative_to(root)
            if any(part.startswith('.') for part in relative.parts) or path.suffix.lower() not in media.EXTENSIONS:
                continue
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
                continue
            try:
                stat = media.settled_file(path,now)
                if stat is None:
                    continue
                error = ''
                duration = 0
                if not 0 < stat.st_size <= MAX_SET_BYTES:
                    error = 'Choose a playable video no larger than 256 GB.'
                    key = 'invalid:'+str(path)+':'+source_stamp(stat)
                else:
                    key = media.digest(path)
                    try:
                        duration, _, _ = media.video_info(path)
                        if not 1200 <= duration <= 1800:
                            error = 'Full sets must be 20–30 minutes. Export a trimmed copy into the watched folder.'
                    except (ValueError, OSError, TimeoutExpired):
                        error = 'Could not read this video. Export a playable copy into the watched folder.'
                if source_stamp(stat)!=source_stamp(path.stat()):
                    continue
            except OSError:
                continue
            with connect(True) as db:
                prefs = preferences(db)
                add_draft(db,prefs,'set',key,path.name,duration,path=str(path),stamp=source_stamp(stat),
                          error=error,approve=prefs.auto_approve)
                db.execute('UPDATE files SET done=1 WHERE path=?',(str(path),))
            prepared += 1
            if prepared>=2:
                return


def scan_shorts():
    with connect() as db:
        if not preferences(db).auto_shorts:
            return
        clips = db.execute("""SELECT c.* FROM clips c WHERE c.status='ready' AND c.disabled=0 AND c.media_version>=?
            AND c.duration<=180 AND c.duration>0 AND NOT EXISTS (SELECT 1 FROM youtube_short_checks WHERE clip_id=c.id)
            AND NOT EXISTS (SELECT 1 FROM youtube_jobs WHERE kind='short' AND source_key=c.id)
            ORDER BY c.created_at DESC LIMIT 12""",(media.MEDIA_VERSION,)).fetchall()
    for clip in clips:
        if not (c.DATA/'media'/(clip['id']+'.mp4')).is_file():
            continue
        try:
            duration = short_info(clip)
        except YouTubeError:
            duration = None
        with connect(True) as db:
            prefs = preferences(db)
            if not prefs.auto_shorts:
                return
            if duration is not None:
                add_draft(db,prefs,'short',clip['id'],clip['name'],duration,caption=clip['caption'],approve=prefs.auto_approve)
            db.execute('INSERT OR IGNORE INTO youtube_short_checks VALUES(?)',(clip['id'],))


def plan(db, prefs, now):
    zone = ZoneInfo(settings(db)['timezone'])
    today = datetime.fromtimestamp(now,zone).date()
    for kind, interval, cap, hour in (('short',1,prefs.shorts_per_day,prefs.short_hour),('set',prefs.sets_every_days,1,prefs.set_hour)):
        queued = db.execute("""SELECT id,scheduled_at FROM youtube_jobs WHERE kind=? AND status='queued' AND pinned=0 AND manual=0
            ORDER BY created_at,id""",(kind,)).fetchall()
        if not queued:
            continue
        if not interval or not cap:
            db.execute("UPDATE youtube_jobs SET scheduled_at=NULL WHERE kind=? AND status='queued' AND pinned=0 AND manual=0",(kind,))
            continue
        existing = db.execute("""SELECT CASE WHEN status='uploaded' THEN uploaded_at ELSE scheduled_at END AS at
            FROM youtube_jobs WHERE kind=? AND ((status='uploaded' AND uploaded_at>=?) OR status='uploading'
            OR (status='queued' AND pinned=1 AND manual=0))""",(kind,now-91*86400)).fetchall()
        days = Counter(datetime.fromtimestamp(row['at'],zone).date() for row in existing if row['at'] is not None)
        date = today
        for job in queued:
            while days[date]>=cap or (interval>1 and any(abs((date-other).days)<interval for other in days if days[other])):
                date += timedelta(days=1)
            start = datetime.combine(date,wall_time(hour),zone)
            slot = (start+timedelta(minutes=min(5*days[date],55))).timestamp()
            slot = max(slot,now) if date==today else slot
            if slot != job['scheduled_at']:
                db.execute('UPDATE youtube_jobs SET scheduled_at=? WHERE id=?',(slot,job['id']))
            days[date] += 1


def session_url(value):
    try:
        url = urlsplit(value)
        allowed = (url.scheme=='https' and url.hostname in ('www.googleapis.com','upload.youtube.com','youtube.googleapis.com')
                   and url.netloc==url.hostname and not url.fragment and url.path.startswith('/upload/youtube/'))
    except ValueError:
        allowed = False
    if not allowed:
        raise YouTubeError('Google returned an invalid upload session.', retry=True)
    return value


def check_upload(response, total):
    if response.status_code in (200, 201):
        try:
            data = response.json()
            video = data['id']
            if VIDEO_ID.fullmatch(video):
                visibility = data.get('status',{}).get('privacyStatus')
                return video,total,visibility if visibility in ('private','unlisted','public') else None
        except (ValueError, KeyError, TypeError, AttributeError):
            pass
        raise YouTubeError('YouTube accepted the transfer but returned no video ID. Reelflow will check the session again.', retry=True)
    if response.status_code==308:
        header = response.headers.get('Range','')
        match = re.fullmatch(r'bytes=0-(\d+)',header)
        offset = int(match[1])+1 if match else 0
        if (header and not match) or offset>=total:
            raise YouTubeError('YouTube returned an invalid upload position. Reelflow will check again.', retry=True)
        return None,offset,None
    if response.status_code in (404, 410):
        raise YouTubeError('The upload session expired. Check YouTube Studio before uploading again.', uncertain=True)
    if response.status_code==401:
        raise YouTubeError('YouTube authorization expired. Reconnect if the next attempt fails.', retry=True)
    if response.status_code in (408, 429) or response.status_code>=500:
        raise YouTubeError('YouTube is temporarily unavailable. Reelflow will resume this upload.', retry=True)
    raise YouTubeError('YouTube declined the upload. Check channel eligibility, permissions and upload limits in Studio.')


def start_upload(client, headers, job, total):
    response = client.post(GOOGLE_API+'/upload/youtube/v3/videos',
        params={'uploadType':'resumable','part':'snippet,status','notifySubscribers':'false'},
        headers={**headers,'X-Upload-Content-Type':'application/octet-stream','X-Upload-Content-Length':str(total)},
        json={'snippet':{'title':job['title'],'description':job['description'],'categoryId':job['category']},
              'status':{'privacyStatus':job['visibility'],'selfDeclaredMadeForKids':bool(job['made_for_kids'])}})
    if not response.is_success:
        check_upload(response,total)
        raise YouTubeError('Google did not start the upload session.', retry=True)
    url = session_url(response.headers.get('Location',''))
    with connect(True) as db:
        secret('youtube_upload:'+str(job['id']),url,db=db)
    return url


def complete(job, video_id, visibility, db):
    db.execute("""UPDATE youtube_jobs SET status='uploaded',video_id=?,uploaded_at=?,actual_visibility=?,error='',
        next_attempt=0,uploaded_bytes=total_bytes,manual=0 WHERE id=?""",(video_id,time.time(),visibility,job['id']))
    db.execute('DELETE FROM secrets WHERE name=?',('youtube_upload:'+str(job['id']),))
    event('YouTube upload complete: '+job['title'],db=db)


def transfer(client, headers, job):
    path = job_path(job)
    session_key = 'youtube_upload:'+str(job['id'])
    url = secret(session_key)
    with path.open('rb') as stream:
        stat = os.fstat(stream.fileno())
        stamp = source_stamp(stat)
        if job['source_stamp'] and job['source_stamp']!=stamp:
            raise YouTubeError('The source changed. Restore the original file before resuming this upload.')
        if not 0 < stat.st_size <= MAX_SET_BYTES:
            raise YouTubeError('The video file is empty or exceeds the upload size limit.')
        total = stat.st_size
        if not url:
            with connect(True) as db:
                db.execute('UPDATE youtube_jobs SET total_bytes=?,source_stamp=? WHERE id=?',(total,stamp,job['id']))
            url = start_upload(client,headers,job,total)
            offset = 0
        else:
            response = client.put(session_url(url),headers={**headers,'Content-Range':f'bytes */{total}'},content=b'')
            video, offset, visibility = check_upload(response,total)
            if video:
                with connect(True) as db:
                    complete(job,video,visibility,db)
                return
        deadline = time.monotonic()+30
        for _ in range(16):
            with connect() as db:
                if preferences(db).paused and not job['manual']:
                    return
            stream.seek(offset)
            data = stream.read(min(CHUNK_BYTES,total-offset))
            if not data or source_stamp(os.fstat(stream.fileno()))!=stamp:
                raise YouTubeError('The source changed. Restore the original file before resuming this upload.')
            end = offset+len(data)-1
            response = client.put(url,headers={**headers,'Content-Type':'application/octet-stream',
                'Content-Range':f'bytes {offset}-{end}/{total}'},content=data)
            video, next_offset, visibility = check_upload(response,total)
            with connect(True) as db:
                if video:
                    complete(job,video,visibility,db)
                    return
                db.execute('UPDATE youtube_jobs SET uploaded_bytes=?,attempts=0,error=?,next_attempt=0 WHERE id=?',
                           (next_offset,'',job['id']))
            if next_offset<=offset or time.monotonic()>=deadline:
                break
            offset = next_offset


def fail_job(job, error):
    with connect(True) as db:
        current = db.execute('SELECT attempts FROM youtube_jobs WHERE id=?',(job['id'],)).fetchone()
        attempts = current['attempts']+1
        state = 'uncertain' if error.uncertain else 'uploading' if error.retry and attempts<6 else 'failed'
        delay = min(30*2**attempts,1800) if state=='uploading' else 0
        db.execute('UPDATE youtube_jobs SET status=?,error=?,attempts=?,next_attempt=? WHERE id=?',
                   (state,str(error),attempts,time.time()+delay,job['id']))
        if error.auth:
            db.execute('UPDATE youtube_account SET error=? WHERE id=1',(str(error),))
        if state!='uploading' or attempts==1:
            event('YouTube upload #'+str(job['id'])+': '+str(error),'warning',db=db)
    if state in ('failed','uncertain'):
        notifications.enqueue('youtube:'+str(job['id'])+':'+state,'YouTube upload needs attention in Reelflow. '+str(error))


def publish_due(now):
    if c.DEMO:
        return False
    with connect(True) as db:
        prefs = preferences(db)
        creds = credentials(db)
        account = db.execute('SELECT error FROM youtube_account WHERE id=1').fetchone()
        if not creds.get('refresh_token') or not account or account['error']:
            return False
        job = db.execute("""SELECT * FROM youtube_jobs WHERE next_attempt<=? AND (manual=1 OR ?=0)
            AND (status='uploading' OR (status='queued' AND scheduled_at<=?))
            AND (manual=1 OR status='uploading' OR (kind='short' AND ?>0) OR (kind='set' AND ?>0))
            ORDER BY status='uploading' DESC,manual DESC,scheduled_at,id LIMIT 1""",
            (now,prefs.paused,now,prefs.shorts_per_day,prefs.sets_every_days)).fetchone()
        if not job:
            return False
        if job['kind']=='short' and job['status']=='queued':
            clip = db.execute('SELECT disabled,media_version,status FROM clips WHERE id=?',(job['source_key'],)).fetchone()
            if not clip or clip['disabled'] or clip['status']!='ready' or clip['media_version']<media.MEDIA_VERSION:
                db.execute("UPDATE youtube_jobs SET status='failed',error=? WHERE id=?",
                           ('The footage is excluded or its export is being rebuilt. Review it before retrying.',job['id']))
                return False
        db.execute("UPDATE youtube_jobs SET status='uploading' WHERE id=?",(job['id'],))
    try:
        with httpx.Client(timeout=45) as client:
            token = google_token(client,creds,grant_type='refresh_token',refresh_token=creds['refresh_token'])
            transfer(client,{'Authorization':'Bearer '+token['access_token']},job)
    except YouTubeError as error:
        fail_job(job,error)
    except httpx.HTTPError:
        fail_job(job,YouTubeError('Connection to Google interrupted. Reelflow will resume the saved session.',retry=True))
    except OSError:
        fail_job(job,YouTubeError('The video file is unavailable. Restore it to resume this upload.'))
    return True


def tick():
    from .worker import worker_lock
    with worker_lock('youtube.lock') as owned:
        if not owned:
            return False
        now = time.time()
        scan_sets(now)
        scan_shorts()
        with connect(True) as db:
            plan(db,preferences(db),now)
        return publish_due(now)


def main():
    logging.basicConfig(level=logging.INFO)
    init()
    once = '--once' in sys.argv
    while True:
        try:
            active = tick()
        except Exception:
            logging.getLogger(__name__).error('YouTube worker pass failed. Retrying on the next pass.')
            if once:
                raise SystemExit(1)
            active = False
        if once:
            return
        time.sleep(5 if active else 20)


if __name__=='__main__':
    main()
