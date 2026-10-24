import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo
import httpx
from fastapi import FastAPI, Request, HTTPException, UploadFile, BackgroundTasks
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.sessions import SessionMiddleware
from . import config as c
from .db import init, connect, settings, secret, event, runtime
from .planner import plan, conservation, daily_limit, runway_days
from .security import verify_media
from . import instagram as ig, ai, notifications as ntfy
from .worker import tick, finish

# Set the shared installation key before constructing session middleware.
c.bootstrap_runtime()
STATIC = Path(__file__).parent/'static'

@asynccontextmanager
async def lifespan(app):
    init()
    yield

app = FastAPI(title='Reelflow',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)

@app.middleware('http')
async def guard(request: Request, call_next):
    path = request.url.path
    public = path=='/' or path=='/healthz' or path.startswith('/static/') or path=='/api/login' or path.startswith('/media/')
    if not public:
        if c.DEMO:
            if request.client.host not in ('127.0.0.1','::1','testclient'):
                return JSONResponse({'detail':'Preview is available only on this computer.'},403)
        elif not request.session.get('admin'):
            return JSONResponse({'detail':'Sign in to continue.'},401)
        if request.method not in ('GET','HEAD','OPTIONS') and not c.DEMO:
            if not hmac.compare_digest(request.headers.get('x-csrf-token',''),request.session.get('csrf','invalid')):
                return JSONResponse({'detail':'Session expired. Refresh the page.'},403)
    if request.method not in ('GET','HEAD','OPTIONS'):
        origin = request.headers.get('origin')
        allowed = {c.PUBLIC_URL,str(request.base_url).rstrip('/')}
        if origin and origin not in allowed:
            return JSONResponse({'detail':'Request origin is not allowed.'},403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; media-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
    if not path.startswith('/static/'):
        response.headers['Cache-Control'] = 'no-store'
    return response

# Session must wrap guard, so signed session is available to every route.
app.add_middleware(SessionMiddleware,secret_key=c.SECRET or 'not-configured',session_cookie='reelflow',same_site='lax',https_only=c.PUBLIC_URL.startswith('https://'),max_age=12*3600)
app.mount('/static',StaticFiles(directory=STATIC),name='static')

@app.get('/')
def index():
    return FileResponse(STATIC/'index.html')

@app.get('/healthz')
def health():
    try:
        with connect() as db:
            db.execute('SELECT 1').fetchone()
        beat = float(runtime('heartbeat') or 0)
        return {'status':'ok','worker_alive':time.time()-beat<180}
    except Exception:
        return JSONResponse({'status':'unavailable'},503)

class Login(BaseModel):
    password: str = Field(max_length=300)

@app.post('/api/login')
def login(body: Login, request: Request):
    address = request.client.host
    now = time.time()
    with connect(True) as db:
        row = db.execute('SELECT * FROM login_attempts WHERE address=?',(address,)).fetchone()
        if row and row['since']>now-900 and row['attempts']>=10:
            raise HTTPException(429,'Too many attempts. Try again in 15 minutes.')
        if not c.PASSWORD or not hmac.compare_digest(hashlib.sha256(body.password.encode()).digest(),hashlib.sha256(c.PASSWORD.encode()).digest()):
            if not row or row['since']<now-900:
                db.execute('INSERT OR REPLACE INTO login_attempts VALUES(?,1,?)',(address,now))
            else:
                db.execute('UPDATE login_attempts SET attempts=attempts+1 WHERE address=?',(address,))
            # Commit before returning an auth error.
            failed = True
        else:
            db.execute('DELETE FROM login_attempts WHERE address=?',(address,))
            failed = False
    if failed:
        raise HTTPException(401,'Incorrect password.')
    request.session.clear()
    request.session.update({'admin':True,'csrf':secrets.token_urlsafe(32)})
    return {'ok':True}

@app.post('/api/logout')
def logout(request: Request):
    request.session.clear()
    return {'ok':True}

@app.get('/api/state')
def state(request: Request):
    s = settings()
    with connect() as db:
        account = db.execute('SELECT username,expires_at,synced_at FROM account WHERE id=1').fetchone()
        counts = {r['status']:r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM clips GROUP BY status')}
        remaining = db.execute("SELECT COUNT(*) FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.lane='main' AND j.status IN ('queued','retry') AND c.disabled=0").fetchone()[0]
        trial_remaining = db.execute("SELECT COUNT(*) FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.lane='trial' AND j.status IN ('queued','retry') AND c.disabled=0").fetchone()[0]
        published = db.execute("SELECT COUNT(*) FROM jobs WHERE status='published'").fetchone()[0]
        imported = db.execute('SELECT COUNT(*) FROM history').fetchone()[0]
        issues = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('uncertain','failed')").fetchone()[0]
        next_post = db.execute("SELECT j.*,c.name FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.status IN ('queued','retry','processing') AND j.scheduled_at IS NOT NULL AND c.disabled=0 ORDER BY j.scheduled_at LIMIT 1").fetchone()
        metric_count = db.execute("SELECT COUNT(*) FROM history WHERE origin='reelflow' AND reach>0 AND metrics_at>timestamp+86400").fetchone()[0]
    interval = conservation(s,remaining if daily_limit(s,'main') else trial_remaining,time.time())
    return {'settings':s,'account':dict(account) if account else None,'counts':counts,'main_remaining':remaining,'trial_remaining':trial_remaining,
            'runway_days':runway_days(s,remaining,trial_remaining,interval),'interval':interval,'published':published,'imported':imported,'issues':issues,
            'next_post':dict(next_post) if next_post else None,'heartbeat':runtime('heartbeat'),
            'csrf':request.session.get('csrf',''),'demo':c.DEMO,'meta_configured':bool(c.META_ID and c.META_SECRET),
            'ai_configured':bool(ai.api_key()),'ai_base':c.AI_BASE,'ntfy_token_set':bool(secret('ntfy_token')),
            'metric_samples':metric_count,'public_url':c.PUBLIC_URL,'footage_path':str(c.FOOTAGE)}

@app.get('/api/schedule')
def schedule(days: int = None):
    """Read the actual persisted queue, grouped in the audience's local calendar."""
    s = settings()
    days = s['preview_days'] if days is None else days
    if not 1 <= days <= 90:
        raise HTTPException(422,'Choose between 1 and 90 preview days.')
    now = time.time()
    tz = ZoneInfo(s['timezone'])
    start = datetime.fromtimestamp(now,tz).replace(hour=0,minute=0,second=0,microsecond=0)
    end = start + timedelta(days=days)
    with connect() as db:
        rows = [dict(r) for r in db.execute("""SELECT j.*,c.name,c.duration,c.category,c.disabled
            FROM jobs j JOIN clips c ON c.id=j.clip_id
            WHERE j.status!='rejected' AND c.disabled=0
            AND COALESCE(j.published_at,j.scheduled_at)>=? AND COALESCE(j.published_at,j.scheduled_at)<?
            ORDER BY COALESCE(j.published_at,j.scheduled_at),j.id""",(start.timestamp(),end.timestamp()))]
        unscheduled = db.execute("SELECT COUNT(*) FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.status IN ('queued','retry') AND c.disabled=0 AND j.scheduled_at IS NULL").fetchone()[0]
        overdue = db.execute("SELECT COUNT(*) FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.status NOT IN ('published','rejected') AND c.disabled=0 AND j.scheduled_at<?",(start.timestamp(),)).fetchone()[0]
        preparing = db.execute("SELECT COUNT(*) FROM clips WHERE disabled=0 AND status IN ('processing','captioning','needs_caption')").fetchone()[0]
        counts = {r['lane']:r['n'] for r in db.execute("SELECT j.lane,COUNT(*) n FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.status IN ('queued','retry') AND c.disabled=0 GROUP BY j.lane")}
    interval = conservation(s,counts.get('main',0) if daily_limit(s,'main') else counts.get('trial',0),now)
    calendar = {}
    for offset in range(days):
        local = start + timedelta(days=offset)
        key = local.date().isoformat()
        calendar[key] = {'date':key,'timestamp':local.timestamp(),'number':offset+1,'main':[],'trial':[]}
    for job in rows:
        key = datetime.fromtimestamp(job['published_at'] or job['scheduled_at'],tz).date().isoformat()
        calendar[key][job['lane']].append(job)
    return {'days':list(calendar.values()),'timezone':s['timezone'],'interval':interval,
            'limits':{lane:daily_limit(s,lane,interval) for lane in ('main','trial')},
            'unscheduled':unscheduled,'overdue':overdue,'preparing':preparing,
            'totals':{lane:sum(r['lane']==lane and r['status']!='published' for r in rows) for lane in ('main','trial')}}

@app.get('/api/jobs')
def jobs(status: str='upcoming', lane: str='', offset: int=0, limit: int=100):
    if lane not in ('','main','trial') or offset<0 or not 1<=limit<=200:
        raise HTTPException(422,'Invalid filter.')
    filters = {'upcoming':"j.status NOT IN ('published','rejected')",'published':"j.status='published'",'rejected':"j.status='rejected'",'all':'1=1'}
    if status not in filters:
        raise HTTPException(422,'Invalid status.')
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT j.*,c.name,c.duration,c.category,c.disabled FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE '+filters[status]+(' AND j.lane=?' if lane else '')+' ORDER BY COALESCE(j.scheduled_at,99999999999),j.id LIMIT ? OFFSET ?',((lane,) if lane else ())+(limit,offset))]

@app.get('/api/clips')
def clips(offset: int=0, limit: int=100):
    if offset<0 or not 1<=limit<=200:
        raise HTTPException(422,'Invalid page.')
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT id,name,created_at,duration,status,error,notes,caption,caption_source,category,disabled FROM clips ORDER BY created_at DESC LIMIT ? OFFSET ?',(limit,offset))]

@app.get('/api/events')
def events(offset: int=0, limit: int=100):
    if offset<0 or not 1<=limit<=200:
        raise HTTPException(422,'Invalid page.')
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT ? OFFSET ?',(limit,offset))]

@app.get('/api/insights')
def insights():
    with connect() as db:
        history = [dict(r) for r in db.execute('SELECT * FROM history ORDER BY timestamp DESC LIMIT 100')]
    return {'posts':history,'note':'Timing learns from app-published Reels with reach and at least 24 hours of maturity. Previous captions inform voice immediately.'}

class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    values: dict

@app.patch('/api/settings')
def update_settings(body: SettingsPatch):
    s = settings()
    caption_fields = ('ai_model','ai_vision','caption_instructions','voice_notes','business_context','brand_name')
    previous_caption_config = tuple(s[k] for k in caption_fields)
    for key,value in body.values.items():
        if key not in c.DEFAULTS:
            raise HTTPException(422,'Unknown setting: '+key)
        template = c.DEFAULTS[key]
        if type(value) is not type(template):
            raise HTTPException(422,'Invalid type for '+key)
        if isinstance(value,str) and len(value)>4000:
            raise HTTPException(422,'Setting too long.')
        s[key] = value
    try:
        ZoneInfo(s['timezone'])
        if s['drought_until']:
            datetime.strptime(s['drought_until'],'%Y-%m-%d')
        assert 0<=s['trials_per_day']<=10 and 0<=s['main_per_day']<=10
        assert 1<=s['preview_days']<=90 and 0<=s['low_watermark']<=365 and 0<=s['min_gap_days']<=90
        assert 2<=s['conserve_every_days']<=14 and 1<=s['conserve_main_per_day']<=10 and 1<=s['conserve_trials_per_day']<=10
        assert 0<=s['posting_start_hour']<s['posting_end_hour']<=23
        assert s['posting_end_hour']-s['posting_start_hour'] >= (max(s['trials_per_day'],s['main_per_day'])-1)*1.5
        assert len(s['schedule_hours'])==7 and all(type(h) is int and 0<=h<=23 for h in s['schedule_hours'])
        u = urlparse(s['ntfy_url'])
        assert u.scheme=='https' and u.netloc and not u.username and not u.password and not u.query and not u.fragment
        assert not s['ntfy_topic'] or re.fullmatch(r'[A-Za-z0-9_-]{1,120}',s['ntfy_topic'])
    except Exception:
        raise HTTPException(422,'Check timezone, dates, daily limits (0–10), preview days (1–90), and notification URL/topic. The posting window must allow at least 90 minutes between posts to the same destination.')
    if c.DEMO and (not s['paused'] or not s['dry_run']):
        raise HTTPException(409,'Preview cannot publish. Use the Docker deployment to go live.')
    if not s['dry_run']:
        with connect() as db:
            account = db.execute('SELECT * FROM account WHERE id=1').fetchone()
        if not account or account['expires_at']<=time.time() or not c.PUBLIC_URL.startswith('https://'):
            raise HTTPException(409,'Connect Instagram and configure public HTTPS before enabling live publishing.')
    with connect(True) as db:
        db.execute('UPDATE settings SET value=? WHERE id=1',(json.dumps(s),))
        event('Posting settings updated.','info',db=db)
    if previous_caption_config != tuple(s[k] for k in caption_fields):
        runtime('caption_retry_at',0)
    plan()
    return {'ok':True}

class Keys(BaseModel):
    model_config = ConfigDict(extra='forbid')
    ai_key: str = Field(default='',max_length=1000)
    ntfy_token: str = Field(default='',max_length=1000)

@app.post('/api/keys')
def keys(body: Keys):
    if c.DEMO:
        raise HTTPException(409,'Connect providers in the Docker deployment. Preview does not store credentials.')
    if body.ai_key:
        secret('ai_key',body.ai_key)
        runtime('caption_retry_at',0)
    if body.ntfy_token:
        secret('ntfy_token',body.ntfy_token)
    event('Provider credentials updated.')
    return {'ok':True}

@app.get('/api/models')
def models():
    if 'openrouter.ai' not in c.AI_BASE:
        return []
    try:
        return [{'id':m['id'],'name':m['name'],'vision':'image' in m.get('architecture',{}).get('input_modalities',[])} for m in ai.model_catalog()]
    except Exception:
        raise HTTPException(502,'Could not load model catalog. Enter an exact model ID manually.')

@app.post('/api/scan')
def scan(background: BackgroundTasks):
    background.add_task(tick)
    return {'ok':True,'message':'Intake scan started. New files settle for 30 seconds before processing.'}

@app.post('/api/notifications/test')
def notification_test(background: BackgroundTasks):
    if c.DEMO:
        raise HTTPException(409,'Notification delivery is disabled in preview.')
    if not settings()['ntfy_topic']:
        raise HTTPException(422,'Save an ntfy topic first.')
    ntfy.enqueue('test:'+str(time.time()),'Reelflow is connected. Footage and posting alerts will arrive here.')
    background.add_task(ntfy.flush)
    return {'ok':True,'message':'Test notification queued. Delivery appears in Activity.'}

@app.post('/api/upload')
async def upload(file: UploadFile):
    from .media import EXTENSIONS
    suffix = Path(file.filename or '').suffix.lower()
    if suffix not in EXTENSIONS:
        raise HTTPException(422,'Choose an MP4, MOV, M4V, MKV or WebM video.')
    name = re.sub(r'[^\w .()-]','_',Path(file.filename).name)[:180]
    dest = c.DATA/'uploads'/(secrets.token_hex(5)+'-'+name)
    temp = dest.with_suffix('.part')
    size = 0
    try:
        with temp.open('wb') as stream:
            while True:
                chunk = await file.read(1024*1024)
                if not chunk:
                    break
                size += len(chunk)
                if size>5_000_000_000:
                    raise HTTPException(413,'Maximum input size is 5 GB.')
                stream.write(chunk)
        temp.replace(dest)
    finally:
        temp.unlink(missing_ok=True)
        await file.close()
    event('Uploaded '+name+'. Waiting for intake.')
    return {'ok':True}

class JobEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    caption: str = Field(default=None,min_length=1,max_length=2200)
    approved: bool = None
    scheduled_at: float = None
    pinned: bool = None

@app.patch('/api/jobs/{job_id}')
def edit_job(job_id: int, body: JobEdit):
    with connect(True) as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not job:
            raise HTTPException(404,'Post not found.')
        if job['status'] not in ('queued','retry','failed'):
            raise HTTPException(409,'This post is already processing, published or held. Use its available recovery actions.')
        values = body.model_dump(exclude_unset=True)
        if not values:
            return {'ok':True}
        if any(v is None for v in values.values()):
            raise HTTPException(422,'Fields cannot be empty.')
        if 'caption' in values:
            values['caption'] = values['caption'].strip()
            if not values['caption']:
                raise HTTPException(422,'Write a caption first.')
            values['caption_locked'] = 1
            # Any changed caption needs a new container; a container's caption is immutable.
            values['container_id'] = None
        if 'scheduled_at' in values:
            ts = values['scheduled_at']
            if not time.time()+60<=ts<=time.time()+366*86400:
                raise HTTPException(422,'Choose a future date within a year.')
            current = settings(db)
            gap = 20*3600 if job['lane']=='main' else 5400
            if db.execute("SELECT 1 FROM jobs WHERE id!=? AND lane=? AND status NOT IN ('rejected','failed') AND ABS(COALESCE(published_at,scheduled_at)-?)<?",(job_id,job['lane'],ts,gap)).fetchone():
                raise HTTPException(409,'This time conflicts with another post. Choose a more widely spaced time.')
            values['pinned'] = True
        db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in values)+' WHERE id=?',tuple(values.values())+(job_id,))
        event('Upcoming post edited.','info',job_id,db)
    plan()
    return {'ok':True}

class JobAction(BaseModel):
    action: str
    media_id: str = ''

@app.post('/api/jobs/{job_id}/action')
def job_action(job_id: int, body: JobAction):
    with connect(True) as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not job:
            raise HTTPException(404,'Post not found.')
        if body.action=='reject' and job['status'] in ('queued','retry','failed'):
            db.execute("UPDATE jobs SET status='rejected',scheduled_at=NULL WHERE id=?",(job_id,))
        elif body.action=='restore' and job['status']=='rejected':
            db.execute("UPDATE jobs SET status='queued',container_id=NULL,pinned=0 WHERE id=?",(job_id,))
        elif body.action=='retry' and job['status']=='failed':
            db.execute("UPDATE jobs SET status='queued',container_id=NULL,attempts=0,error='',pinned=0,next_attempt=0 WHERE id=?",(job_id,))
        elif body.action=='confirm_not_posted' and job['status']=='uncertain':
            db.execute("UPDATE jobs SET status='queued',container_id=NULL,attempts=0,error='',pinned=0,next_attempt=0 WHERE id=?",(job_id,))
        elif body.action=='confirm_posted' and job['status']=='uncertain':
            if body.media_id and not re.fullmatch(r'\d{5,40}',body.media_id):
                raise HTTPException(422,'Instagram media ID must be numeric.')
            db.execute("UPDATE jobs SET status='published',media_id=?,published_at=?,error='' WHERE id=?",(body.media_id or None,time.time(),job_id))
            if body.media_id:
                db.execute("INSERT OR IGNORE INTO history(media_id,caption,media_type,timestamp,origin,lane) VALUES(?,?,'REELS',?,'reelflow',?)",(body.media_id,job['caption'],time.time(),job['lane']))
        else:
            raise HTTPException(409,'Action is not available in the current post state.')
        event('Post action: '+body.action,'info',job_id,db)
    plan()
    return {'ok':True}

class ClipEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    notes: str = Field(default=None,max_length=3000)
    caption: str = Field(default=None,max_length=2200,min_length=1)
    disabled: bool = None

@app.patch('/api/clips/{clip_id}')
def clip_edit(clip_id: str, body: ClipEdit):
    with connect(True) as db:
        clip = db.execute('SELECT * FROM clips WHERE id=?',(clip_id,)).fetchone()
        if not clip:
            raise HTTPException(404,'Clip not found.')
        values = body.model_dump(exclude_unset=True)
        if not values or any(v is None for v in values.values()):
            raise HTTPException(422,'Supply an editable field.')
        if 'disabled' in values and db.execute("SELECT 1 FROM jobs WHERE clip_id=? AND status IN ('processing','publishing')",(clip_id,)).fetchone():
            raise HTTPException(409,'This clip is being published. Pause posting and wait for it to finish.')
        if 'caption' in values:
            values['caption'] = values['caption'].strip()
            if not values['caption']:
                raise HTTPException(422,'Write a caption.')
            if clip['status'] in ('captioning','needs_caption'):
                values.update(status='ready',caption_source='Manual',error='')
        db.execute('UPDATE clips SET '+','.join(k+'=?' for k in values)+' WHERE id=?',tuple(values.values())+(clip_id,))
        if 'caption' in values:
            db.execute("UPDATE jobs SET caption=?,container_id=NULL WHERE clip_id=? AND caption_locked=0 AND status IN ('queued','retry')",(values['caption'],clip_id))
        event('Footage updated: '+clip['name'],db=db)
    plan()
    return {'ok':True}

@app.post('/api/clips/{clip_id}/regenerate')
def regenerate(clip_id: str):
    with connect() as db:
        clip = db.execute('SELECT * FROM clips WHERE id=?',(clip_id,)).fetchone()
    if not clip or clip['status'] in ('processing','error'):
        raise HTTPException(409,'Wait for footage processing to finish first.')
    try:
        caption,category = ai.generate(clip,settings())
    except ai.CaptionError as exc:
        raise HTTPException(502,str(exc))
    with connect(True) as db:
        db.execute("UPDATE clips SET caption=?,category=?,status='ready',error='',caption_source=? WHERE id=?",(caption,category,settings()['ai_model'],clip_id))
        db.execute("UPDATE jobs SET caption=?,container_id=NULL WHERE clip_id=? AND caption_locked=0 AND status IN ('queued','retry')",(caption,clip_id))
        event('Caption regenerated for '+clip['name'],db=db)
    runtime('caption_retry_at',0)
    plan()
    return {'caption':caption}

@app.get('/api/preview/{clip_id}')
def preview(clip_id: str, video: bool=False):
    if not re.fullmatch(r'[a-f0-9]{64}',clip_id):
        raise HTTPException(404)
    path = c.DATA/('media' if video else 'thumbs')/(clip_id+('.mp4' if video else '.jpg'))
    if not path.is_file():
        raise HTTPException(404,'Preview is not ready.')
    return FileResponse(path,media_type='video/mp4' if video else 'image/jpeg')

@app.get('/media/{clip_id}')
def public_media(clip_id: str, expires: int, signature: str):
    if not re.fullmatch(r'[a-f0-9]{64}',clip_id) or not verify_media(clip_id,expires,signature):
        raise HTTPException(403,'Media link has expired or is invalid.')
    path = c.DATA/'media'/(clip_id+'.mp4')
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path,media_type='video/mp4')

@app.get('/auth/instagram/start')
def instagram_start(request: Request):
    if c.DEMO or not c.META_ID or not c.META_SECRET:
        raise HTTPException(409,'Set Meta app credentials in compose.yaml, then recreate the container. See the setup guide.')
    state = secrets.token_urlsafe(32)
    request.session['ig_state'] = state
    request.session['ig_state_at'] = time.time()
    scope = 'instagram_business_basic,instagram_business_content_publish,instagram_business_manage_insights'
    return RedirectResponse('https://www.instagram.com/oauth/authorize?'+urlencode({'client_id':c.META_ID,'redirect_uri':c.PUBLIC_URL+'/auth/instagram/callback','response_type':'code','scope':scope,'state':state,'enable_fb_login':'false','force_reauth':'true'}))

@app.get('/auth/instagram/callback')
def instagram_callback(request: Request, code: str='', state: str=''):
    expected = request.session.pop('ig_state','')
    started = request.session.pop('ig_state_at',0)
    if not expected or not hmac.compare_digest(state,expected) or time.time()-started>600:
        raise HTTPException(403,'Login state expired. Start again from Settings.')
    if not code:
        raise HTTPException(400,'Instagram login was cancelled.')
    try:
        ig.connect_account(code)
    except ig.MetaError as exc:
        raise HTTPException(502,str(exc))
    return RedirectResponse('/#settings',303)

@app.get('/auth/openrouter/start')
def openrouter_start(request: Request):
    if c.DEMO or c.AI_BASE!='https://openrouter.ai/api/v1':
        raise HTTPException(409,'OpenRouter login is available in the deployment with the OpenRouter provider selected.')
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    request.session['or_state'] = state
    request.session['or_at'] = time.time()
    secret('or_verifier:'+state,verifier)
    callback = c.PUBLIC_URL+'/auth/openrouter/callback?state='+state
    return RedirectResponse('https://openrouter.ai/auth?'+urlencode({'callback_url':callback,'code_challenge':challenge,'code_challenge_method':'S256','key_label':'Reelflow'}))

@app.get('/auth/openrouter/callback')
def openrouter_callback(request: Request, code: str='', state: str=''):
    expected = request.session.pop('or_state','')
    started = request.session.pop('or_at',0)
    if not expected or not hmac.compare_digest(state,expected) or time.time()-started>600:
        raise HTTPException(403,'Provider login expired. Start again.')
    verifier = secret('or_verifier:'+state)
    secret('or_verifier:'+state,'')
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post('https://openrouter.ai/api/v1/auth/keys',json={'code':code,'code_verifier':verifier,'code_challenge_method':'S256'})
            r.raise_for_status()
        secret('ai_key',r.json()['key'])
    except Exception:
        raise HTTPException(502,'OpenRouter authorization failed. Try connecting again.')
    runtime('caption_retry_at',0)
    event('OpenRouter connected. Select a vision model in Settings.')
    return RedirectResponse('/#settings',303)

@app.post('/api/account/disconnect')
def disconnect():
    with connect(True) as db:
        if db.execute("SELECT 1 FROM jobs WHERE status IN ('processing','publishing','uncertain')").fetchone():
            raise HTTPException(409,'Resolve in-progress and uncertain posts before disconnecting.')
        if db.execute("SELECT 1 FROM jobs WHERE status='published'").fetchone():
            raise HTTPException(409,'Use a separate data directory for a different account to preserve posting history. Reconnect the same account through Instagram login.')
        db.execute('DELETE FROM account')
        db.execute('DELETE FROM history')
        s = settings(db)
        s.update(paused=True,dry_run=True)
        db.execute('UPDATE settings SET value=? WHERE id=1',(json.dumps(s),))
        event('Instagram disconnected. Posting is paused.',db=db)
    secret('instagram_token','')
    return {'ok':True}

@app.post('/api/clips/{clip_id}/retry')
def retry_clip(clip_id: str):
    with connect(True) as db:
        if db.execute("UPDATE clips SET status='processing',error='' WHERE id=? AND status='error'",(clip_id,)).rowcount!=1:
            raise HTTPException(409,'Only failed preparation can be retried.')
        event('Footage preparation retry requested.',db=db)
    return {'ok':True}
