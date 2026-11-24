import base64
import hashlib
import hmac
import json
import re
import secrets
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo
import httpx
from fastapi import FastAPI, Request, HTTPException, UploadFile, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.sessions import SessionMiddleware
from . import config as c
from .db import init, connect, settings, secret, event, runtime
from .planner import plan, conservation, daily_limit, runway_days, KIND_SQL
from .security import verify_media
from . import instagram as ig, ai, media, songs, hashtags, youtube, notifications as ntfy
from .worker import tick, send_post_now
from .youtube_api import router as youtube_router

# Set the shared installation key before constructing session middleware.
c.bootstrap_runtime()
STATIC = Path(__file__).parent/'static'
ASSETS = {name:hashlib.sha256((STATIC/name).read_bytes()).hexdigest()[:12] for name in ('app.js','style.css')}
INDEX = (STATIC/'index.html').read_text(encoding='utf-8').replace('{{VERSION}}',escape(c.VERSION)).replace('{{REVISION}}',escape(c.REVISION[:7]))
for name, digest in ASSETS.items():
    INDEX = INDEX.replace('/static/'+name,f'/static/build/{digest}/{name}')

@asynccontextmanager
async def lifespan(app):
    init()
    from . import edits, rewrites, multicam
    edits.recover()
    rewrites.resume_all()
    multicam.recover()
    yield

app = FastAPI(title='Reelflow',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
app.include_router(youtube_router)

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
    if not path.startswith('/static/') or path in ('/static/app.js','/static/style.css'):
        response.headers['Cache-Control'] = 'no-store'
    return response

# Session must wrap guard, so signed session is available to every route.
app.add_middleware(SessionMiddleware,secret_key=c.SECRET or 'not-configured',session_cookie='reelflow',same_site='lax',https_only=c.PUBLIC_URL.startswith('https://'),max_age=12*3600)

@app.get('/static/build/{digest}/{filename}')
def versioned_asset(digest: str, filename: str):
    if ASSETS.get(filename)!=digest:
        raise HTTPException(404)
    return FileResponse(STATIC/filename,headers={'Cache-Control':'public, max-age=31536000, immutable'})

app.mount('/static',StaticFiles(directory=STATIC),name='static')

@app.get('/')
def index():
    return HTMLResponse(INDEX)

@app.get('/healthz')
def health():
    try:
        with connect() as db:
            db.execute('SELECT 1').fetchone()
        beat = float(runtime('heartbeat') or 0)
        return {'status':'ok','worker_alive':time.time()-beat<180,'version':c.VERSION,'revision':c.REVISION}
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
        rebuilding = db.execute("SELECT COUNT(*) FROM clips WHERE media_version=? AND status!='error'",(media.REBUILD_REQUESTED,)).fetchone()[0]
        remaining = db.execute("SELECT COUNT(*) FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.lane='main' AND j.status IN ('queued','retry') AND c.disabled=0").fetchone()[0]
        trial_remaining = db.execute("SELECT COUNT(*) FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.lane='trial' AND j.status IN ('queued','retry') AND c.disabled=0").fetchone()[0]
        published = db.execute("SELECT COUNT(*) FROM jobs WHERE status='published'").fetchone()[0]
        imported = db.execute('SELECT COUNT(*) FROM history').fetchone()[0]
        issues = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('uncertain','failed')").fetchone()[0]
        next_post = db.execute("SELECT j.*,c.name FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.status IN ('queued','retry','processing') AND j.scheduled_at IS NOT NULL AND c.disabled=0 ORDER BY j.scheduled_at LIMIT 1").fetchone()
        metric_count = db.execute("SELECT COUNT(*) FROM history WHERE origin='reelflow' AND reach>0 AND metrics_at>timestamp+86400").fetchone()[0]
    interval = conservation(s,remaining if daily_limit(s,'main') else trial_remaining,time.time())
    return {'settings':s,'account':dict(account) if account else None,'counts':counts,'rebuilding':rebuilding,'main_remaining':remaining,'trial_remaining':trial_remaining,
            'runway_days':runway_days(s,remaining,trial_remaining,interval),'interval':interval,'published':published,'imported':imported,'issues':issues,
            'next_post':dict(next_post) if next_post else None,'heartbeat':runtime('heartbeat'),
            'csrf':request.session.get('csrf',''),'demo':c.DEMO,'meta_configured':bool(c.META_ID and c.META_SECRET),
            'ai_configured':bool(ai.api_key()),'ai_base':c.AI_BASE,'ntfy_token_set':bool(secret('ntfy_token')),
            'metric_samples':metric_count,'public_url':c.PUBLIC_URL,'footage_path':str(c.FOOTAGE),'gpu':media.gpu(),'song_matching':songs.available()}

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
        rows = [dict(r) for r in db.execute("""SELECT j.*,c.name,c.duration,c.category,c.disabled,c.post_on,"""+KIND_SQL+""" AS kind
            FROM jobs j JOIN clips c ON c.id=j.clip_id LEFT JOIN intake_batches b ON b.id=c.batch
            WHERE j.status!='rejected' AND c.disabled=0
            AND COALESCE(j.published_at,j.scheduled_at)>=? AND COALESCE(j.published_at,j.scheduled_at)<?
            ORDER BY COALESCE(j.published_at,j.scheduled_at),j.id""",(start.timestamp(),end.timestamp()))]
        unscheduled = db.execute("SELECT COUNT(*) FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.status IN ('queued','retry') AND c.disabled=0 AND j.scheduled_at IS NULL").fetchone()[0]
        overdue = db.execute("SELECT COUNT(*) FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.status NOT IN ('published','rejected') AND c.disabled=0 AND j.scheduled_at<?",(start.timestamp(),)).fetchone()[0]
        preparing = db.execute("SELECT COUNT(*) FROM clips WHERE disabled=0 AND status IN ('processing','captioning','needs_caption')").fetchone()[0]
        counts = {r['lane']:r['n'] for r in db.execute("SELECT j.lane,COUNT(*) n FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.status IN ('queued','retry') AND c.disabled=0 GROUP BY j.lane")}
        youtube_rows = [dict(r) for r in db.execute("""SELECT id,kind,source_key,title,status,pinned,manual,forced,mirror_of,error,video_id,
            CASE WHEN status='uploaded' THEN uploaded_at ELSE scheduled_at END AS at FROM youtube_jobs
            WHERE status IN ('queued','uploading','uploaded','failed')
            AND CASE WHEN status='uploaded' THEN uploaded_at ELSE scheduled_at END>=?
            AND CASE WHEN status='uploaded' THEN uploaded_at ELSE scheduled_at END<? ORDER BY at,id""",(start.timestamp(),end.timestamp()))]
        youtube_on = bool(db.execute("SELECT 1 FROM youtube_account WHERE id=1").fetchone())
        mirror_on = youtube.preferences(db).mirror_grid
    interval = conservation(s,counts.get('main',0) if daily_limit(s,'main') else counts.get('trial',0),now)
    calendar = {}
    for offset in range(days):
        local = start + timedelta(days=offset)
        key = local.date().isoformat()
        calendar[key] = {'date':key,'timestamp':local.timestamp(),'number':offset+1,'main':[],'trial':[],'youtube':[]}
    for job in rows:
        key = datetime.fromtimestamp(job['published_at'] or job['scheduled_at'],tz).date().isoformat()
        calendar[key][job['lane']].append(job)
    for short in youtube_rows:
        key = datetime.fromtimestamp(short['at'],tz).date().isoformat()
        if key in calendar:
            calendar[key]['youtube'].append(short)
    return {'days':list(calendar.values()),'timezone':s['timezone'],'interval':interval,
            'youtube':{'connected':youtube_on,'mirror':mirror_on},
            'limits':{lane:daily_limit(s,lane,interval) for lane in ('main','trial')},
            'unscheduled':unscheduled,'overdue':overdue,'preparing':preparing,
            'totals':{**{lane:sum(r['lane']==lane and r['status']!='published' for r in rows) for lane in ('main','trial')},
                      'youtube':sum(r['status']!='uploaded' for r in youtube_rows)}}

class PlaceBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['job','clip','youtube']
    id: str = Field(min_length=1,max_length=80)
    lane: Literal['main','trial','youtube']
    day: str = Field(min_length=10,max_length=10)
    after: float | None = None
    before: float | None = None

class HoldBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['job','youtube']
    id: str = Field(min_length=1,max_length=80)

@app.post('/api/schedule/place')
def schedule_place(body: PlaceBody):
    from . import schedule as hand
    try:
        at = hand.place(body.kind,body.id,body.lane,body.day,body.after,body.before)
    except (hand.PlaceError, ValueError) as exc:
        raise HTTPException(422,str(exc))
    return {'ok':True,'at':at}

@app.post('/api/schedule/hold')
def schedule_hold(body: HoldBody):
    from . import schedule as hand
    try:
        hand.hold(body.kind,body.id)
    except (hand.PlaceError, ValueError) as exc:
        raise HTTPException(422,str(exc))
    return {'ok':True}

@app.post('/api/schedule/release')
def schedule_release(body: HoldBody):
    from . import schedule as hand
    try:
        hand.release(body.kind,body.id)
    except (hand.PlaceError, ValueError) as exc:
        raise HTTPException(422,str(exc))
    return {'ok':True}

@app.get('/api/schedule/tray')
def schedule_tray(q: str = '', offset: int = 0):
    from . import schedule as hand
    if offset < 0 or len(q) > 100:
        raise HTTPException(422,'Invalid search.')
    return hand.tray(q,offset)

@app.get('/api/jobs')
def jobs(status: str='upcoming', lane: str='', offset: int=0, limit: int=100):
    if lane not in ('','main','trial') or offset<0 or not 1<=limit<=200:
        raise HTTPException(422,'Invalid filter.')
    filters = {'upcoming':"j.status NOT IN ('published','rejected')",'published':"j.status='published'",'rejected':"j.status='rejected'",'all':'1=1'}
    if status not in filters:
        raise HTTPException(422,'Invalid status.')
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT j.*,c.name,c.duration,c.category,c.disabled,c.post_on,'+KIND_SQL+' AS kind FROM jobs j JOIN clips c ON c.id=j.clip_id LEFT JOIN intake_batches b ON b.id=c.batch WHERE '+filters[status]+(' AND j.lane=?' if lane else '')+' ORDER BY COALESCE(j.scheduled_at,99999999999),j.id LIMIT ? OFFSET ?',((lane,) if lane else ())+(limit,offset))]

@app.get('/api/clips')
def clips(offset: int=0, limit: int=100, view: Literal['all','unposted','posted','same_song','dated','other']='all', only: str=''):
    if offset<0 or not 1<=limit<=200:
        raise HTTPException(422,'Invalid page.')
    filters = {'all':'1=1',
        'posted':"EXISTS (SELECT 1 FROM jobs j WHERE j.clip_id=c.id AND j.status='published')",
        'unposted':"NOT EXISTS (SELECT 1 FROM jobs j WHERE j.clip_id=c.id AND j.status='published')",
        'dated':"(c.post_on!='' OR c.not_before!='' OR c.not_after!='')",
        'other':KIND_SQL+"!='performance'"}
    with connect() as db:
        groups = songs.groups(db)
        sizes = {}
        for key in groups.values():
            sizes[key] = sizes.get(key,0)+1
        where = filters.get(view)
        if view=='same_song':
            ids = [cid for cid,key in groups.items() if sizes[key]>1]
            where = 'c.id IN (%s)' % ','.join('?'*len(ids))
            params = tuple(ids)
        elif only:
            where, params = 'c.id=?', (only,)
        else:
            params = ()
        rows = [dict(r) for r in db.execute("""SELECT c.id,c.name,c.created_at,c.duration,c.status,c.error,c.notes,c.caption,c.caption_source,c.category,c.disabled,
            c.cover_manual_ms,c.cover_auto_ms,c.cover_color_ms,c.song,c.audio_state,c.batch,c.edit_of,b.name AS batch_name,b.mode AS batch_mode,
            c.kind AS own_kind,COALESCE(b.kind,'performance') AS set_kind,"""+KIND_SQL+""" AS kind,c.post_on,c.post_at,c.not_before,c.not_after,
            (c.media_version=? AND c.status!='error') AS rebuilding FROM clips c LEFT JOIN intake_batches b ON b.id=c.batch
            WHERE """+where+""" ORDER BY c.created_at DESC,c.id LIMIT ? OFFSET ?""",(media.REBUILD_REQUESTED,*params,limit,offset))]
        ids = [r['id'] for r in rows]
        marks = ','.join('?'*len(ids))
        posts = db.execute(f"SELECT clip_id,lane,published_at,permalink FROM jobs WHERE status='published' AND clip_id IN ({marks}) ORDER BY published_at",ids).fetchall() if ids else []
        matches = db.execute(f"""SELECT m.a,m.b,m.score,ca.name AS a_name,cb.name AS b_name FROM song_matches m
            JOIN clips ca ON ca.id=m.a JOIN clips cb ON cb.id=m.b WHERE m.a IN ({marks}) OR m.b IN ({marks}) ORDER BY m.score DESC""",ids*2).fetchall() if ids else []
    for r in rows:
        key = groups.get(r['id'])
        r['posts'] = [{'lane':p['lane'],'published_at':p['published_at'],'permalink':p['permalink']} for p in posts if p['clip_id']==r['id']]
        r['song_clips'] = sizes.get(key,1)-1
        r['similar'] = [{'id':m['b'] if m['a']==r['id'] else m['a'],'name':m['b_name'] if m['a']==r['id'] else m['a_name'],'score':m['score']}
                        for m in matches if r['id'] in (m['a'],m['b'])][:12]
        r['top_match'] = r['similar'][0]['score'] if r['similar'] else (100 if r['song_clips'] else 0)
        r['song_named'] = bool(r['song'])
    return rows

@app.get('/api/batches')
def batches():
    with connect() as db:
        return [dict(r) for r in db.execute("""SELECT b.*,COUNT(c.id) AS clips,
            SUM(EXISTS (SELECT 1 FROM jobs j WHERE j.clip_id=c.id AND j.status='published')) AS posted
            FROM intake_batches b JOIN clips c ON c.batch=b.id GROUP BY b.id ORDER BY b.created_at DESC LIMIT 50""")]

class BatchEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default=None,min_length=1,max_length=80)
    mode: Literal['mix','next'] = None
    kind: Literal['performance','talking','bts','promo'] = None

@app.patch('/api/batches/{batch_id:path}')
def batch_edit(batch_id: str, body: BatchEdit):
    values = {k:(v.strip() if isinstance(v,str) else v) for k,v in body.model_dump(exclude_unset=True).items() if v is not None}
    if not values or values.get('name')=='':
        raise HTTPException(422,'Supply a set name, type or posting order.')
    with connect(True) as db:
        if db.execute('UPDATE intake_batches SET '+','.join(k+'=?' for k in values)+' WHERE id=?',(*values.values(),batch_id)).rowcount!=1:
            raise HTTPException(404,'Set not found.')
        event('Set updated: '+db.execute('SELECT name FROM intake_batches WHERE id=?',(batch_id,)).fetchone()[0]+('. Posting it next, in order.' if values.get('mode')=='next' else '. Mixed into the queue.' if values.get('mode')=='mix' else '.'),db=db)
    plan()
    return {'ok':True}

@app.get('/api/clips/caption-selection')
def caption_selection():
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT id,name FROM clips WHERE status IN ('ready','captioning','needs_caption') AND media_version!=? ORDER BY created_at DESC,id",(media.REBUILD_REQUESTED,))]

@app.post('/api/clips/rebuild')
def rebuild_clips():
    with connect(True) as db:
        count = db.execute("UPDATE clips SET media_version=?,status=CASE WHEN status='error' THEN 'processing' ELSE status END,error=''",(media.REBUILD_REQUESTED,)).rowcount
        if count:
            event(f'Rebuild queued for {count} clips. Original footage and posting history are kept.',db=db)
    return {'count':count}

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
    caption_fields = ('ai_model','ai_vision','caption_instructions','voice_notes','business_context','brand_name',
                      'caption_blocklist','caption_signoff','hashtag_groups','hashtag_new_max','hashtags_per_post','caption_avoid_camera','caption_unique')
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
        from . import collab
        collab.validate(s)
    except ValueError as exc:
        raise HTTPException(422,str(exc))
    if s['story_share'] not in ('off','grid','all'):
        raise HTTPException(422,'Choose which Reels are shared to Stories.')
    if s['gpu_mode'] not in ('auto','off'):
        raise HTTPException(422,'Choose GPU or CPU encoding.')
    if s['cover_mode'] not in media.COVER_MODES or s['export_quality'] not in media.EXPORT_QUALITIES:
        raise HTTPException(422,'Choose an available cover and export quality setting.')
    if s['caption_avoid_camera'] not in ('guidance','avoid','allow'):
        raise HTTPException(422,'Choose an available camera and editing rule.')
    endings = [line.strip() for line in s['caption_signoff'].splitlines() if line.strip()]
    if len(endings)>4 or any(len(line)>32 for line in endings):
        raise HTTPException(422,'Enter up to four sign-offs, each no longer than 32 characters.')
    try:
        s['hashtag_groups'] = hashtags.normalise(s['hashtag_groups'])
    except ValueError as exc:
        raise HTTPException(422,str(exc))
    if not 0<=s['hashtags_per_post']<=ai.MAX_TAGS:
        raise HTTPException(422,'Instagram allows up to five hashtags per post. Choose 0 to 5.')
    if not 0<=s['hashtag_new_max']<=ai.MAX_TAGS:
        raise HTTPException(422,'Choose 0 to 5 new hashtags per post.')
    mix = {}
    for kind in ('talking','bts','promo'):
        rule = {**c.DEFAULTS['content_mix'][kind], **(s['content_mix'].get(kind) or {})}
        if rule.get('lanes') not in ('both','main','trial') or type(rule.get('every')) is not int or not 0<=rule['every']<=30 or set(rule)-{'lanes','every'}:
            raise HTTPException(422,'Choose where each clip type posts and a gap of 0–30 posts.')
        mix[kind] = rule
    if set(s['content_mix'])-set(mix):
        raise HTTPException(422,'Unknown clip type.')
    s['content_mix'] = mix
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
async def upload(file: UploadFile, destination: Literal['instagram','youtube']='instagram', batch: str='', batch_name: str='', mode: Literal['mix','next']='mix', kind: Literal['performance','talking','bts','promo']='performance'):
    from .media import EXTENSIONS
    suffix = Path(file.filename or '').suffix.lower()
    if suffix not in EXTENSIONS:
        raise HTTPException(422,'Choose an MP4, MOV, M4V, MKV or WebM video.')
    name = re.sub(r'[^\w .()-]','_',Path(file.filename).stem)[:170]+suffix
    folder = c.DATA/'uploads'
    if destination=='youtube':
        folder /= media.YOUTUBE_SET_FOLDER
        folder.mkdir(exist_ok=True)
    elif batch:
        # One upload action is one set, so the planner can mix it with earlier sets.
        if not re.fullmatch(r'[a-z0-9]{8,32}',batch):
            raise HTTPException(422,'Invalid upload set.')
        label = ' '.join(batch_name.split())[:80] or 'Upload · '+media.local_day(time.time())
        with connect(True) as db:
            db.execute('INSERT OR IGNORE INTO intake_batches(id,name,mode,created_at,kind) VALUES(?,?,?,?,?)',('upload:'+batch,label,mode,time.time(),kind))
        folder /= batch
        folder.mkdir(exist_ok=True)
    dest = folder/(secrets.token_hex(5)+'-'+name)
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
            if values['caption'] != job['caption']:
                values['caption_locked'] = 1
                # A container's caption is immutable.
                values['container_id'] = None
            else:
                values.pop('caption')
        if 'scheduled_at' in values:
            ts = values['scheduled_at']
            if not time.time()+60<=ts<=time.time()+366*86400:
                raise HTTPException(422,'Choose a future date within a year.')
            current = settings(db)
            gap = 20*3600 if job['lane']=='main' else 5400
            if db.execute("SELECT 1 FROM jobs WHERE id!=? AND lane=? AND status NOT IN ('rejected','failed') AND ABS(COALESCE(published_at,scheduled_at)-?)<?",(job_id,job['lane'],ts,gap)).fetchone():
                raise HTTPException(409,'This time conflicts with another post. Choose a more widely spaced time.')
            values['pinned'] = True
        if not values:
            return {'ok':True}
        db.execute('UPDATE jobs SET '+','.join(k+'=?' for k in values)+' WHERE id=?',tuple(values.values())+(job_id,))
        event('Upcoming post edited.','info',job_id,db)
    plan()
    return {'ok':True}

class JobAction(BaseModel):
    action: str
    media_id: str = ''

@app.post('/api/jobs/{job_id}/post-now')
def post_now(job_id: int, background: BackgroundTasks):
    now = time.time()
    with connect(True) as db:
        s = settings(db)
        job = db.execute('SELECT j.*,c.status AS clip_status,c.media_version FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.id=?',(job_id,)).fetchone()
        if not job:
            raise HTTPException(404,'Post not found.')
        if c.DEMO or s['dry_run']:
            raise HTTPException(409,'Turn off Dry run before posting live.')
        if job['status'] not in ('queued','retry') or job['post_now_at']:
            raise HTTPException(409,'This post is already being sent or is no longer ready.')
        if job['clip_status']!='ready' or job['media_version']<media.MEDIA_VERSION or not job['caption'].strip():
            raise HTTPException(409,'This Reel needs a ready export and a saved caption.')
        account = db.execute('SELECT expires_at FROM account WHERE id=1').fetchone()
        if not account or account['expires_at']<=now or not c.PUBLIC_URL.startswith('https://'):
            raise HTTPException(409,'Connect Instagram and configure public HTTPS before posting.')
        db.execute("UPDATE jobs SET post_now_at=?,next_attempt=0,error='' WHERE id=?",(now,job_id))
        event('Post now requested for '+job['lane']+' Reel.',job_id=job_id,db=db)
    background.add_task(send_post_now,job_id)
    return {'ok':True}

@app.post('/api/jobs/{job_id}/action')
def job_action(job_id: int, body: JobAction):
    with connect(True) as db:
        job = db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        if not job:
            raise HTTPException(404,'Post not found.')
        if body.action=='reject' and job['status'] in ('queued','retry','failed'):
            db.execute("UPDATE jobs SET status='rejected',scheduled_at=NULL,post_now_at=NULL WHERE id=?",(job_id,))
        elif body.action=='restore' and job['status']=='rejected':
            db.execute("UPDATE jobs SET status='queued',container_id=NULL,pinned=0 WHERE id=?",(job_id,))
        elif body.action=='retry' and job['status']=='failed':
            db.execute("UPDATE jobs SET status='queued',container_id=NULL,attempts=0,error='',pinned=0,next_attempt=0,post_now_at=NULL WHERE id=?",(job_id,))
        elif body.action=='confirm_not_posted' and job['status']=='uncertain':
            db.execute("UPDATE jobs SET status='queued',container_id=NULL,attempts=0,error='',pinned=0,next_attempt=0,post_now_at=NULL WHERE id=?",(job_id,))
        elif body.action=='confirm_posted' and job['status']=='uncertain':
            if body.media_id and not re.fullmatch(r'\d{5,40}',body.media_id):
                raise HTTPException(422,'Instagram media ID must be numeric.')
            db.execute("UPDATE jobs SET status='published',media_id=?,published_at=?,error='' WHERE id=?",(body.media_id or None,time.time(),job_id))
            if body.media_id:
                db.execute("INSERT OR IGNORE INTO history(media_id,caption,media_type,timestamp,origin,lane) VALUES(?,?,'REELS',?,'reelflow',?)",(body.media_id,job['caption'],time.time(),job['lane']))
        elif body.action=='confirm_removed' and job['status']=='published':
            # The user removed it on Instagram. Keep the old history record, but never reuse its container.
            db.execute("""UPDATE jobs SET status='queued',approved=0,pinned=0,scheduled_at=NULL,container_id=NULL,
                media_id=NULL,permalink=NULL,published_at=NULL,publish_started=NULL,post_now_at=NULL,
                attempts=0,next_attempt=0,error='' WHERE id=?""",(job_id,))
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
    cover_manual_ms: int | None = Field(default=None,ge=0,strict=True)
    song: str = Field(default=None,max_length=120)
    kind: Literal['','performance','talking','bts','promo'] = None
    post_on: str = Field(default=None,max_length=10)
    post_at: str = Field(default=None,max_length=5)
    not_before: str = Field(default=None,max_length=10)
    not_after: str = Field(default=None,max_length=10)

def check_timing(values, clip):
    """Dates are the account's local calendar days. A dated clip ignores the window."""
    today = datetime.now(ZoneInfo(settings()['timezone'])).date()
    merged = {k:values.get(k,clip[k]) for k in ('post_on','post_at','not_before','not_after')}
    parsed = {}
    for key in ('post_on','not_before','not_after'):
        if merged[key]:
            try:
                parsed[key] = datetime.strptime(merged[key],'%Y-%m-%d').date()
            except ValueError:
                raise HTTPException(422,'Use dates like 2026-12-24.')
    if merged['post_at'] and not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d',merged['post_at']):
        raise HTTPException(422,'Use a 24-hour time like 19:30.')
    if 'post_on' in values and values['post_on'] and parsed['post_on'] < today:
        raise HTTPException(422,'Choose today or a later date.')
    if merged['post_at'] and not merged['post_on']:
        raise HTTPException(422,'Choose a date for the posting time.')
    if parsed.get('not_before') and parsed.get('not_after') and parsed['not_after'] < parsed['not_before']:
        raise HTTPException(422,'The window must end on or after the day it starts.')

@app.patch('/api/clips/{clip_id}')
def clip_edit(clip_id: str, body: ClipEdit):
    with connect(True) as db:
        clip = db.execute('SELECT * FROM clips WHERE id=?',(clip_id,)).fetchone()
        if not clip:
            raise HTTPException(404,'Clip not found.')
        values = body.model_dump(exclude_unset=True)
        if any(k in values for k in ('post_on','post_at','not_before','not_after')):
            check_timing({k:v for k,v in values.items() if v is not None},clip)
        if 'song' in values and values['song'] is not None:
            values['song'] = ' '.join(values['song'].split())
        if not values or any(v is None for k,v in values.items() if k!='cover_manual_ms'):
            raise HTTPException(422,'Supply an editable field.')
        if ('disabled' in values or 'cover_manual_ms' in values) and db.execute("SELECT 1 FROM jobs WHERE clip_id=? AND status IN ('processing','publishing')",(clip_id,)).fetchone():
            raise HTTPException(409,'This clip is being published. Pause posting and wait for it to finish.')
        if 'cover_manual_ms' in values:
            offset = values['cover_manual_ms']
            if clip['media_version']!=media.MEDIA_VERSION or (offset is not None and offset>=clip['duration']*1000):
                raise HTTPException(422,'Choose a frame within the prepared video.')
            db.execute("UPDATE jobs SET container_id=NULL WHERE clip_id=? AND status IN ('queued','retry')",(clip_id,))
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

class CaptionRewrite(BaseModel):
    model_config = ConfigDict(extra='forbid')
    instructions: str = Field(default='',max_length=4000)

def rewrite_clip(clip_id, instructions=''):
    with connect() as db:
        clip = db.execute('SELECT * FROM clips WHERE id=?',(clip_id,)).fetchone()
    if not clip or clip['media_version']==media.REBUILD_REQUESTED or clip['status'] in ('processing','error'):
        raise HTTPException(409,'Wait for footage processing to finish first.')
    s = settings()
    if instructions.strip():
        s['caption_instructions'] += ('\n\nFor this rewrite only, these directions take priority over the saved style guidance above:\n'
                                      + instructions.strip())
    try:
        caption,category = ai.generate(clip,s)
    except ai.CaptionError as exc:
        raise HTTPException(502,str(exc))
    with connect(True) as db:
        if db.execute("UPDATE clips SET caption=?,category=?,status='ready',error='',caption_source=? WHERE id=? AND media_version!=? AND status IN ('ready','captioning','needs_caption')",(caption,category,s['ai_model'],clip_id,media.REBUILD_REQUESTED)).rowcount!=1:
            raise HTTPException(409,'Wait for footage processing to finish first.')
        db.execute("UPDATE jobs SET caption=?,caption_locked=0,container_id=NULL WHERE clip_id=? AND (caption_locked=0 OR caption=?) AND status IN ('queued','retry')",(caption,clip_id,clip['caption']))
        event('Caption regenerated for '+clip['name'],db=db)
    runtime('caption_retry_at',0)
    try:
        plan()
    except Exception:
        pass  # The caption is saved; the next worker pass replans.
    return caption

# Rewrites can outlast a reverse proxy's request timeout (Cloudflare stops at 100 s),
# so slow ones finish in the background and the browser polls for the result.
REWRITES = {}
REWRITE_LOCK = threading.Lock()
REWRITE_WAIT = 20

def run_rewrite(token, clip_id, instructions):
    try:
        result = {'state':'done','caption':rewrite_clip(clip_id,instructions)}
    except HTTPException as exc:
        result = {'state':'error','status':exc.status_code,'message':exc.detail}
    except Exception:
        result = {'state':'error','status':500,'message':'Caption rewrite failed unexpectedly. Retry this clip; check the container logs if it keeps failing.'}
    with REWRITE_LOCK:
        REWRITES[token] = {**result,'at':time.time()}

class MulticamNew(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(default='',max_length=80)

@app.get('/api/multicam')
def multicam_overview():
    from . import multicam
    return {'jobs':multicam.jobs(),'sources':multicam.sources(),'source_folder':str(multicam.source_root()),
            'chunk':multicam.CHUNK,'counts':multicam.CLIP_COUNTS}

@app.post('/api/multicam/jobs')
def multicam_create(body: MulticamNew):
    from . import multicam
    if not settings()['multicam_enabled']:
        raise HTTPException(409,'Turn on the multicam editor in Settings first.')
    return {'id':multicam.create(body.name)}

@app.post('/api/multicam/jobs/{job_id}/upload')
async def multicam_upload(job_id: str, request: Request, name: str, offset: int = 0, final: bool = False):
    from . import multicam
    try:
        return await multicam.receive_chunk(job_id,name,offset,request.stream(),final)
    except LookupError as exc:
        raise HTTPException(404,str(exc))
    except ValueError as exc:
        raise HTTPException(422,str(exc))

@app.post('/api/multicam/jobs/{job_id}/start')
def multicam_start(job_id: str, body: dict):
    from . import multicam
    try:
        multicam.get(job_id)
        multicam.start(job_id,body)
    except LookupError as exc:
        raise HTTPException(404,str(exc))
    except ValueError as exc:
        raise HTTPException(422,str(exc))
    return multicam.public(multicam.get(job_id))

@app.post('/api/multicam/jobs/{job_id}/{action}')
def multicam_control(job_id: str, action: Literal['cancel','retry','delete']):
    from . import multicam
    try:
        multicam.control(job_id,action)
    except LookupError as exc:
        raise HTTPException(404,str(exc))
    except ValueError as exc:
        raise HTTPException(409,str(exc))
    return {'ok':True}

@app.get('/api/multicam/jobs/{job_id}/full')
def multicam_full(job_id: str):
    from . import multicam
    try:
        path = multicam.full_path(job_id)
    except (LookupError, ValueError):
        path = None
    if not path:
        raise HTTPException(404,'The full edit is not available here.')
    return FileResponse(path,media_type='video/mp4',filename=path.name)

@app.post('/api/trials/unblock')
def unblock_trials():
    """Try Trial Reels again: clear the pause and requeue Trials refused for permission."""
    with connect(True) as db:
        s = settings(db)
        s['trials_blocked'] = ''
        db.execute('UPDATE settings SET value=? WHERE id=1',(json.dumps(s),))
        refused = re.compile(r'Instagram rejected the request \(HTTP \d+, code (%s)[,):]' % '|'.join(map(str,ig.PERMISSION_CODES)))
        ids = [r['id'] for r in db.execute("SELECT id,error FROM jobs WHERE lane='trial' AND status='failed'") if refused.match(r['error'] or '')]
        for job_id in ids:
            db.execute("UPDATE jobs SET status='queued',attempts=0,next_attempt=0,error='',container_id=NULL WHERE id=?",(job_id,))
        count = len(ids)
        event(f'Trial Reels resumed; {count} refused Trial{"s" if count != 1 else ""} queued again.',db=db)
    plan()
    return {'ok':True,'requeued':count}

class RewriteBatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    clips: list[str] = Field(min_length=1,max_length=2000)
    instructions: str = Field(default='',max_length=4000)

@app.post('/api/rewrite-batches')
def start_rewrite_batch(body: RewriteBatch):
    from . import rewrites
    try:
        batch = rewrites.start(body.clips,body.instructions)
    except RuntimeError as exc:
        raise HTTPException(409,str(exc))
    except ValueError as exc:
        raise HTTPException(422,str(exc))
    return rewrites.summary(batch)

@app.get('/api/rewrite-batches/latest')
def latest_rewrite_batch():
    from . import rewrites
    return {'batch':rewrites.summary()}

@app.post('/api/rewrite-batches/{batch_id}/{action}')
def control_rewrite_batch(batch_id: str, action: Literal['cancel','resume']):
    from . import rewrites
    try:
        rewrites.control(batch_id,action)
    except LookupError as exc:
        raise HTTPException(404,str(exc))
    except ValueError as exc:
        raise HTTPException(409,str(exc))
    return rewrites.summary(batch_id)

class TagSuggest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    count: int = Field(default=12,ge=5,le=60)

@app.post('/api/hashtags/suggest')
def hashtag_suggestions(body: TagSuggest = None):
    try:
        return {'hashtags':ai.suggest_hashtags(settings(),body.count if body else 12)}
    except ai.CaptionError as exc:
        raise HTTPException(502,str(exc))

class TagText(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(max_length=100000)

@app.post('/api/hashtags/parse')
def hashtag_parse(body: TagText):
    """Turn a pasted list (any format) into clean tags, so the page can preview before adding."""
    tags, rejected = hashtags.parse(body.text)
    return {'tags':tags[:hashtags.MAX_LIBRARY_TAGS],'rejected':rejected[:50],'rejected_count':len(rejected)}

@app.get('/api/hashtags/stats')
def hashtag_stats():
    with connect() as db:
        return {'tags':hashtags.stats(db)}

@app.post('/api/clips/{clip_id}/regenerate')
def regenerate(clip_id: str, body: CaptionRewrite = None):
    with connect() as db:
        clip = db.execute('SELECT * FROM clips WHERE id=?',(clip_id,)).fetchone()
    if not clip or clip['media_version']==media.REBUILD_REQUESTED or clip['status'] in ('processing','error'):
        raise HTTPException(409,'Wait for footage processing to finish first.')
    token = secrets.token_urlsafe(16)
    with REWRITE_LOCK:
        for key in [k for k,v in REWRITES.items() if v['at']<time.time()-3600]:
            del REWRITES[key]
        REWRITES[token] = {'state':'running','at':time.time()}
    worker = threading.Thread(target=run_rewrite,args=(token,clip_id,body.instructions if body else ''),daemon=True)
    worker.start()
    worker.join(REWRITE_WAIT)
    return rewrite_status(token)

@app.get('/api/rewrites/{token}')
def rewrite_status(token: str):
    with REWRITE_LOCK:
        result = REWRITES.get(token)
    if not result:
        raise HTTPException(404,'Reelflow restarted before this rewrite finished. Retry the clip.')
    if result['state']=='error':
        raise HTTPException(result['status'],result['message'])
    if result['state']=='running':
        return {'pending':True,'token':token}
    return {'caption':result['caption']}

def editable_clip(clip_id):
    with connect() as db:
        clip = db.execute('SELECT * FROM clips WHERE id=?',(clip_id,)).fetchone()
    if not clip:
        raise HTTPException(404,'Clip not found.')
    if clip['media_version']!=media.MEDIA_VERSION or clip['status'] not in ('ready','captioning','needs_caption') or not (c.DATA/'media'/(clip_id+'.mp4')).is_file():
        raise HTTPException(409,'Wait for this clip to finish preparing.')
    return clip

@app.get('/api/clips/{clip_id}/faces')
def clip_faces(clip_id: str, t: float=0):
    from . import edits
    clip = editable_clip(clip_id)
    try:
        return {'faces':edits.detect_faces(c.DATA/'media'/(clip_id+'.mp4'),min(max(0,t),clip['duration']))}
    except edits.EditError as exc:
        raise HTTPException(422,str(exc))

class EditRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start: float = Field(ge=0)
    box: dict[Literal['x','y','w','h'],float]
    effects: list[Literal['arrow','circle','spotlight','zoom']] = Field(min_length=1,max_length=4)
    label: str = Field(default='',max_length=40)
    length: float = Field(default=5,ge=0,le=60)
    replace: bool = True

@app.post('/api/clips/{clip_id}/edits')
def create_edit(clip_id: str, body: EditRequest):
    from . import edits
    clip = editable_clip(clip_id)
    with connect() as db:
        if db.execute("SELECT 1 FROM jobs WHERE clip_id=? AND status IN ('processing','publishing')",(clip_id,)).fetchone():
            raise HTTPException(409,'This clip is being published. Wait for it to finish.')
    try:
        return {'id':edits.start(clip,body.model_dump(),body.replace)}
    except edits.EditError as exc:
        raise HTTPException(409 if 'Another edit' in str(exc) else 422,str(exc))

@app.get('/api/edits/{edit_id}')
def edit_status(edit_id: str):
    with connect() as db:
        row = db.execute('SELECT id,clip_id,status,progress,message,result_clip FROM edits WHERE id=?',(edit_id,)).fetchone()
    if not row:
        raise HTTPException(404,'Edit not found.')
    return dict(row)

@app.get('/api/preview/{clip_id}')
def preview(clip_id: str, video: bool=False):
    if not re.fullmatch(r'[a-f0-9]{64}',clip_id):
        raise HTTPException(404)
    path = c.DATA/('media' if video else 'thumbs')/(clip_id+('.mp4' if video else '.jpg'))
    if not path.is_file():
        raise HTTPException(404,'Preview is not ready.')
    return FileResponse(path,media_type='video/mp4' if video else 'image/jpeg')

@app.get('/api/clips/{clip_id}/cover')
def cover_preview(clip_id: str):
    if not re.fullmatch(r'[a-f0-9]{64}',clip_id):
        raise HTTPException(404)
    with connect() as db:
        clip = db.execute('SELECT * FROM clips WHERE id=? AND media_version=?',(clip_id,media.MEDIA_VERSION)).fetchone()
        mode = settings(db)['cover_mode']
    offset = media.cover_offset(clip,mode) if clip else None
    if offset is None:
        raise HTTPException(404,'No cover frame is available yet.')
    try:
        jpeg = media.cover_jpeg(c.DATA/'media'/(clip_id+'.mp4'),offset)
    except (ValueError, subprocess.TimeoutExpired):
        raise HTTPException(404,'Could not read the cover frame.') from None
    if not jpeg:
        raise HTTPException(404,'Could not read the cover frame.')
    return Response(jpeg,media_type='image/jpeg')

@app.get('/media/{clip_id}')
def public_media(clip_id: str, expires: int, signature: str):
    if not re.fullmatch(r'[a-f0-9]{64}(-story)?',clip_id) or not verify_media(clip_id,expires,signature):
        raise HTTPException(403,'Media link has expired or is invalid.')
    path = c.DATA/('stories' if clip_id.endswith('-story') else 'media')/(clip_id+'.mp4')
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path,media_type='video/mp4')

@app.get('/auth/instagram/start')
def instagram_start(request: Request, collab: int = 0):
    if c.DEMO or not c.META_ID or not c.META_SECRET:
        raise HTTPException(409,'Set Meta app credentials in compose.yaml, then recreate the container. See the setup guide.')
    state = secrets.token_urlsafe(32)
    request.session['ig_state'] = state
    request.session['ig_state_at'] = time.time()
    request.session['ig_role'] = 'collab' if collab else 'main'
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
    role = request.session.pop('ig_role','main')
    try:
        if role == 'collab':
            from . import collab
            collab.connect_account(code)
        else:
            ig.connect_account(code)
    except ig.MetaError as exc:
        raise HTTPException(502,str(exc))
    return RedirectResponse('/#settings',303)

@app.get('/api/collaborators')
def collaborators():
    from . import collab
    return {'connected':[{k:v for k,v in row.items() if k!='ig_id'} for row in collab.connected().values()]}

@app.post('/api/collaborators/{username}/sign-out')
def collaborator_sign_out(username: str):
    from . import collab
    collab.sign_out(username.lower())
    return {'ok':True}

@app.get('/api/jobs/{job_id}/collaborators')
def job_collaborators(job_id: int):
    from . import collab, stories
    return {'collaborators':collab.for_job(job_id),'story':stories.for_job(job_id)}

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
