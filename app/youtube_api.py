"""Authenticated web UI endpoints for the YouTube workspace."""
import base64
import hashlib
import hmac
import json
import mimetypes
import secrets
import time
from typing import Literal

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from urllib.parse import urlencode, urlsplit

from . import config as c, youtube as yt
from .db import connect, event, secret, settings

router = APIRouter()
JOB_FIELDS = '''id,kind,source_name,duration,title,description,visibility,made_for_kids,category,status,
    scheduled_at,pinned,manual,error,total_bytes,uploaded_bytes,video_id,uploaded_at,actual_visibility,
    EXISTS(SELECT 1 FROM secrets WHERE name='youtube_upload:'||youtube_jobs.id) AS saved_session'''


@router.get('/api/youtube')
def workspace(view: Literal['queue','drafts','scheduled','uploaded','skipped']='queue', offset: int=0):
    if offset<0:
        raise HTTPException(422,'Invalid page.')
    where = {'queue':"status NOT IN ('uploaded','skipped')",
             'drafts':"status IN ('draft','failed','uncertain','invalid')",
             'scheduled':"status IN ('queued','uploading')",
             'uploaded':"status='uploaded'",'skipped':"status='skipped'"}[view]
    order = 'uploaded_at DESC,id DESC' if view=='uploaded' else "status NOT IN ('uncertain','failed','invalid','uploading'),scheduled_at IS NULL,scheduled_at,id"
    with connect() as db:
        prefs = yt.preferences(db)
        account = db.execute('SELECT * FROM youtube_account WHERE id=1').fetchone()
        creds = yt.credentials(db)
        jobs = [dict(row) for row in db.execute('SELECT '+JOB_FIELDS+' FROM youtube_jobs WHERE '+where+
            ' ORDER BY '+order+' LIMIT 51 OFFSET ?',(offset,))]
        queue_count = db.execute("SELECT COUNT(*) FROM youtube_jobs WHERE status NOT IN ('uploaded','skipped')").fetchone()[0]
        timing = settings(db)['timezone']
    return {
        'preferences':prefs.model_dump(),'account':dict(account) if account else None,
        'client_id':creds.get('client_id',''),'connected':bool(account and creds.get('refresh_token') and not account['error']),
        'callback':yt.redirect_uri(),'folder':str(yt.set_folder()),'timezone':timing,
        'jobs':jobs[:50],'more':len(jobs)>50,'queue_count':queue_count,
    }


@router.put('/api/youtube/preferences')
def save_preferences(body: yt.Preferences):
    if '<' in body.description_footer or '>' in body.description_footer:
        raise HTTPException(422,'Remove angle brackets from the description footer.')
    with connect(True) as db:
        db.execute("INSERT OR REPLACE INTO runtime VALUES('youtube_preferences',?)",(body.model_dump_json(),))
        yt.plan(db,body,time.time())
    return {'ok':True}


class Credentials(BaseModel):
    model_config = ConfigDict(extra='forbid')
    client_id: str = Field(max_length=300)
    client_secret: str = Field(default='',max_length=1000)


@router.put('/api/youtube/credentials')
def save_credentials(body: Credentials):
    if c.DEMO:
        raise HTTPException(409,'Connect a channel from your installed Reelflow workspace.')
    client_id, client_secret = body.client_id.strip(), body.client_secret.strip()
    if not yt.CLIENT_ID.fullmatch(client_id):
        raise HTTPException(422,'Enter the OAuth client ID for a Google Web application.')
    with connect(True) as db:
        old = yt.credentials(db)
        if client_id==old.get('client_id') and (not client_secret or client_secret==old.get('client_secret')):
            return {'ok':True}
        if not client_secret:
            raise HTTPException(422,'Enter the client secret for this OAuth client.')
        if db.execute("SELECT 1 FROM youtube_jobs WHERE status IN ('uploading','uncertain') LIMIT 1").fetchone():
            raise HTTPException(409,'Finish or resolve in-progress uploads before changing Google credentials.')
        if db.execute("SELECT 1 FROM secrets WHERE name LIKE 'youtube_upload:%' LIMIT 1").fetchone():
            raise HTTPException(409,'Resolve saved upload sessions before changing Google credentials.')
        secret('youtube_oauth',json.dumps({'client_id':client_id,'client_secret':client_secret}),db=db)
        db.execute("UPDATE youtube_account SET error='' WHERE id=1")
    return {'ok':True}


@router.post('/api/youtube/connect')
def start_connection(request: Request):
    origin = urlsplit(c.PUBLIC_URL)
    if c.DEMO or (origin.scheme!='https' and origin.hostname not in ('localhost','127.0.0.1','::1')):
        raise HTTPException(409,'Connect from your configured HTTPS address or a localhost development installation.')
    with connect() as db:
        creds = yt.credentials(db)
    if not creds.get('client_id') or not creds.get('client_secret'):
        raise HTTPException(409,'Save the Google OAuth client ID and secret first.')
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    request.session['youtube_oauth'] = {'state':state,'at':time.time(),'verifier':verifier,'client_id':creds['client_id']}
    return {'url':yt.authorization_url(creds['client_id'],state,challenge)}


@router.get('/auth/youtube/callback')
def callback(request: Request, code: str='', state: str=''):
    flow = request.session.pop('youtube_oauth',None)
    if not flow or not hmac.compare_digest(flow['state'].encode(),state.encode()) or time.time()-flow['at']>600:
        error = 'The YouTube connection expired. Start again from the YouTube page.'
    elif not code:
        error = 'YouTube authorization was cancelled.'
    else:
        with connect() as db:
            creds = yt.credentials(db)
        try:
            if creds.get('client_id')!=flow['client_id']:
                raise yt.YouTubeError('Google credentials changed. Start the connection again.')
            yt.connect_channel(code,flow['verifier'],creds)
            return RedirectResponse('/#youtube',303)
        except yt.YouTubeError as exc:
            error = str(exc)
        except httpx.HTTPError:
            error = 'Could not reach Google. Try connecting again.'
    return RedirectResponse('/?'+urlencode({'youtube_error':error})+'#youtube',303)

@router.post('/api/youtube/disconnect')
def disconnect():
    with connect(True) as db:
        if db.execute("SELECT 1 FROM youtube_jobs WHERE status IN ('uploading','uncertain') LIMIT 1").fetchone():
            raise HTTPException(409,'Finish or resolve in-progress uploads before disconnecting.')
        if db.execute("SELECT 1 FROM secrets WHERE name LIKE 'youtube_upload:%' LIMIT 1").fetchone():
            raise HTTPException(409,'Resolve saved upload sessions before disconnecting.')
        creds = yt.credentials(db)
        creds.pop('refresh_token',None)
        secret('youtube_oauth',json.dumps(creds),db=db)
        prefs = yt.preferences(db).model_copy(update={'paused':True})
        db.execute("INSERT OR REPLACE INTO runtime VALUES('youtube_preferences',?)",(prefs.model_dump_json(),))
        if not db.execute("SELECT 1 FROM youtube_jobs WHERE status='uploaded' LIMIT 1").fetchone():
            db.execute('DELETE FROM youtube_account')
        event('YouTube disconnected. Automatic uploads paused.',db=db)
    return {'ok':True}


@router.post('/api/youtube/scan')
def scan(background: BackgroundTasks):
    background.add_task(yt.tick)
    return {'message':'Scan requested. New files must stay unchanged for 30 seconds before intake.'}


class ShortDraft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    clip_id: str = Field(pattern=r'^[a-f0-9]{64}$')


@router.post('/api/youtube/shorts')
def add_short(body: ShortDraft):
    with connect() as db:
        existing = db.execute("SELECT id FROM youtube_jobs WHERE kind='short' AND source_key=?",(body.clip_id,)).fetchone()
        clip = db.execute('SELECT * FROM clips WHERE id=?',(body.clip_id,)).fetchone()
    if existing:
        return {'id':existing['id']}
    if not clip:
        raise HTTPException(404,'Clip not found.')
    try:
        duration = yt.short_info(clip)
    except yt.YouTubeError as error:
        raise HTTPException(422,str(error)) from None
    with connect(True) as db:
        clip = db.execute('SELECT * FROM clips WHERE id=?',(body.clip_id,)).fetchone()
        if clip['disabled'] or clip['status']!='ready' or clip['media_version']<yt.media.MEDIA_VERSION:
            raise HTTPException(409,'This clip changed. Refresh the library and try again.')
        job_id = yt.add_draft(db,yt.preferences(db),'short',clip['id'],clip['name'],duration,caption=clip['caption'])
    return {'id':job_id}


def find_job(db, job_id):
    row = db.execute('SELECT * FROM youtube_jobs WHERE id=?',(job_id,)).fetchone()
    if not row:
        raise HTTPException(404,'YouTube upload not found.')
    return row


@router.get('/api/youtube/jobs/{job_id}')
def job_detail(job_id: int):
    with connect() as db:
        row = db.execute('SELECT '+JOB_FIELDS+' FROM youtube_jobs WHERE id=?',(job_id,)).fetchone()
        if not row:
            raise HTTPException(404,'YouTube upload not found.')
        return dict(row)


@router.get('/api/youtube/jobs/{job_id}/video')
def preview(job_id: int):
    with connect() as db:
        job = find_job(db,job_id)
    try:
        path = yt.job_path(job)
    except yt.YouTubeError as error:
        raise HTTPException(404,str(error)) from None
    if not path.is_file() or (job['source_stamp'] and yt.source_stamp(path.stat())!=job['source_stamp']):
        raise HTTPException(404,'The source video is unavailable.')
    return FileResponse(path,media_type=mimetypes.guess_type(path)[0] or 'application/octet-stream')


class Edit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(min_length=1,max_length=100)
    description: str = Field(max_length=5000)
    visibility: yt.Visibility
    made_for_kids: bool
    category: Literal['10','22','24']
    scheduled_at: float | None = Field(default=None, ge=0, le=4_102_444_800)


@router.patch('/api/youtube/jobs/{job_id}')
def edit(job_id: int, body: Edit):
    title = yt.title_text(body.title)
    if not title or len(body.description.encode())>5000 or any(ch in body.description for ch in '<>'):
        raise HTTPException(422,'Enter a title and a description under 5,000 bytes without angle brackets.')
    with connect(True) as db:
        job = find_job(db,job_id)
        if job['status'] not in ('draft','queued','failed') or secret('youtube_upload:'+str(job_id),db=db):
            raise HTTPException(409,'This upload is no longer editable.')
        db.execute('''UPDATE youtube_jobs SET title=?,description=?,visibility=?,made_for_kids=?,category=?,
            scheduled_at=?,pinned=?,manual=0 WHERE id=?''',
            (title,body.description.strip(),body.visibility,body.made_for_kids,body.category,
             body.scheduled_at,body.scheduled_at is not None,job_id))
        yt.plan(db,yt.preferences(db),time.time())
    return {'ok':True}


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['approve','hold','skip','restore','upload_now','retry','confirm_uploaded','confirm_absent']
    video_id: str = Field(default='',max_length=11)


@router.post('/api/youtube/jobs/{job_id}/action')
def action(job_id: int, body: Action, background: BackgroundTasks):
    now = time.time()
    with connect(True) as db:
        job = find_job(db,job_id)
        status = job['status']
        intent = body.action
        session_key = 'youtube_upload:'+str(job_id)
        if intent=='upload_now' and status in ('draft','queued'):
            account = db.execute('SELECT error FROM youtube_account WHERE id=1').fetchone()
            if c.DEMO or not account or account['error'] or not yt.credentials(db).get('refresh_token'):
                raise HTTPException(409,'Connect YouTube before requesting an upload.')
            db.execute("UPDATE youtube_jobs SET status='queued',manual=1,pinned=0,scheduled_at=? WHERE id=?",(now,job_id))
        elif intent=='approve' and status=='draft':
            db.execute("UPDATE youtube_jobs SET status='queued' WHERE id=?",(job_id,))
        elif intent=='hold' and status=='queued':
            db.execute("UPDATE youtube_jobs SET status='draft',manual=0,scheduled_at=CASE WHEN pinned=1 THEN scheduled_at END WHERE id=?",(job_id,))
        elif intent=='skip' and status in ('draft','queued','invalid'):
            db.execute("UPDATE youtube_jobs SET status='skipped',manual=0 WHERE id=?",(job_id,))
        elif intent=='restore' and status=='skipped' and not job['error']:
            db.execute("UPDATE youtube_jobs SET status='draft' WHERE id=?",(job_id,))
        elif intent=='retry' and status=='failed':
            next_status = 'uploading' if secret(session_key,db=db) else 'queued'
            db.execute('UPDATE youtube_jobs SET status=?,attempts=0,next_attempt=0,error=? WHERE id=?',(next_status,'',job_id))
        elif intent=='confirm_uploaded' and status in ('uncertain','failed') and yt.VIDEO_ID.fullmatch(body.video_id):
            yt.complete(job,body.video_id,None,db)
        elif intent=='confirm_absent' and status in ('uncertain','failed'):
            db.execute('DELETE FROM secrets WHERE name=?',(session_key,))
            db.execute("""UPDATE youtube_jobs SET status='draft',error='',attempts=0,next_attempt=0,manual=0,scheduled_at=NULL,pinned=0,
                uploaded_bytes=0,total_bytes=0,source_stamp=CASE WHEN kind='set' THEN source_stamp END
                WHERE id=?""",(job_id,))
        else:
            raise HTTPException(409,'This action is unavailable for the current upload state.')
        yt.plan(db,yt.preferences(db),now)
        event('YouTube upload #'+str(job_id)+': '+intent.replace('_',' ')+'.',db=db)
    if intent in ('upload_now','retry'):
        background.add_task(yt.tick)
    return {'ok':True}
