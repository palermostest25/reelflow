import json
import time
from datetime import datetime
import httpx
from . import config as c
from .db import connect, secret, event, runtime, settings

class MetaError(Exception):
    def __init__(self, message, code=0, transient=False):
        super().__init__(message)
        self.code, self.transient = code, transient

def request(method, path, token=None, **kwargs):
    token = token or secret('instagram_token')
    with httpx.Client(timeout=60) as client:
        response = client.request(method, 'https://graph.instagram.com/'+c.API_VERSION+'/'+path,
            headers={'Authorization':'Bearer '+token}, **kwargs)
    data = response.json()
    if response.status_code >= 400 or 'error' in data:
        error = data.get('error',{})
        # Do not persist provider messages: they can contain access tokens and signed URLs.
        code = error.get('code',0)
        raise MetaError('Instagram rejected the request (HTTP %s, code %s). Check account eligibility, permissions and connection.'%(response.status_code,code),
                        code, response.status_code>=500 or response.status_code==429 or error.get('is_transient',False))
    return data

def connect_account(code):
    with httpx.Client(timeout=45) as client:
        response = client.post('https://api.instagram.com/oauth/access_token',data={
            'client_id':c.META_ID,'client_secret':c.META_SECRET,'grant_type':'authorization_code',
            'redirect_uri':c.PUBLIC_URL+'/auth/instagram/callback','code':code})
        if response.status_code != 200:
            raise MetaError('Instagram login exchange failed. Check the exact redirect URL and Meta app settings.')
        short = response.json()
        # Current Meta docs wrap the token in data[]; accept earlier flat responses too.
        if isinstance(short, dict) and 'data' in short:
            records = short['data']
            short = records[0] if isinstance(records, list) and len(records) == 1 else None
        if not isinstance(short, dict) or not isinstance(short.get('access_token'), str) or not short['access_token'] or not short.get('user_id'):
            raise MetaError('Instagram login returned an invalid token response. Reconnect from Settings.')
        response = client.get('https://graph.instagram.com/access_token',params={
            'grant_type':'ig_exchange_token','client_secret':c.META_SECRET,'access_token':short['access_token']})
        if response.status_code != 200:
            raise MetaError('Could not obtain a long-lived Instagram token.')
        long = response.json()
    profile = request('GET','me',token=long['access_token'],params={'fields':'user_id,username'})
    ig_id = str(profile.get('user_id') or profile.get('id') or short['user_id'])
    with connect() as db:
        old = db.execute('SELECT ig_id FROM account WHERE id=1').fetchone()
    if old and old['ig_id'] != ig_id:
        raise MetaError('A different account is connected. Disconnect it first to keep queues and voice history separate.')
    secret('instagram_token',long['access_token'])
    with connect(True) as db:
        db.execute('INSERT OR REPLACE INTO account(id,ig_id,username,expires_at) VALUES(1,?,?,?)',
                   (ig_id,profile['username'],time.time()+long.get('expires_in',5184000)))
        current = settings(db)
        if not old and current['brand_name']==c.DEFAULTS['brand_name'] and 'trades' not in profile['username'].lower():
            current['brand_name'] = profile['username']
            db.execute('UPDATE settings SET value=? WHERE id=1',(json.dumps(current),))
    event('Connected Instagram @'+profile['username']+'. Previous captions will import on the next worker pass.')

def refresh_token():
    with connect() as db:
        account = db.execute('SELECT * FROM account WHERE id=1').fetchone()
    if not account or account['expires_at']-time.time()>10*86400:
        return
    if account['expires_at'] <= time.time():
        raise MetaError('Instagram connection expired. Reconnect the account.',190)
    with httpx.Client(timeout=45) as client:
        r = client.get('https://graph.instagram.com/refresh_access_token',params={'grant_type':'ig_refresh_token','access_token':secret('instagram_token')})
    if r.status_code != 200:
        raise MetaError('Instagram token refresh failed. Reconnect before expiry.')
    data = r.json()
    secret('instagram_token',data['access_token'])
    with connect(True) as db:
        db.execute('UPDATE account SET expires_at=? WHERE id=1',(time.time()+data.get('expires_in',5184000),))
    event('Instagram connection refreshed.')

def sync_history(force=False):
    with connect() as db:
        account = db.execute('SELECT * FROM account WHERE id=1').fetchone()
    if not account or (not force and account['synced_at'] > time.time()-6*3600):
        return
    path = account['ig_id']+'/media'
    params = {'fields':'id,caption,media_type,permalink,timestamp,like_count,comments_count','limit':50}
    # At most 100 recent posts; use cursors, never fetch arbitrary paging URLs.
    for page in range(2):
        data = request('GET',path,params=params)
        with connect(True) as db:
            for r in data.get('data',[]):
                timestamp = datetime.fromisoformat(r['timestamp'].replace('Z','+00:00')).timestamp()
                db.execute('INSERT INTO history(media_id,caption,media_type,permalink,timestamp,likes,comments) VALUES(?,?,?,?,?,?,?) ON CONFLICT(media_id) DO UPDATE SET caption=excluded.caption,likes=excluded.likes,comments=excluded.comments,permalink=excluded.permalink',
                    (r['id'],r.get('caption',''),r.get('media_type',''),r.get('permalink',''),timestamp,r.get('like_count'),r.get('comments_count')))
        after = data.get('paging',{}).get('cursors',{}).get('after')
        if not after or not data.get('paging',{}).get('next'):
            break
        params['after'] = after
    with connect(True) as db:
        db.execute('UPDATE account SET synced_at=? WHERE id=1',(time.time(),))
    event('Imported recent Instagram captions and available public counts.')

def sync_metrics():
    with connect() as db:
        rows = db.execute("SELECT * FROM history WHERE media_type IN ('VIDEO','REELS') AND timestamp>? AND (metrics_at IS NULL OR metrics_at<?) ORDER BY CASE WHEN origin='reelflow' THEN 0 ELSE 1 END,timestamp DESC LIMIT 20",(time.time()-90*86400,time.time()-6*3600)).fetchall()
    for row in rows:
        try:
            data = request('GET',row['media_id']+'/insights',params={'metric':'views,reach,likes,comments,saved,shares'})
            vals = {x['name']:x.get('values',[{}])[0].get('value') for x in data.get('data',[])}
            with connect(True) as db:
                db.execute('UPDATE history SET views=?,reach=?,likes=COALESCE(?,likes),comments=COALESCE(?,comments),saves=?,shares=?,metrics_at=?,metrics_error=\'\' WHERE media_id=?',
                    (vals.get('views'),vals.get('reach'),vals.get('likes'),vals.get('comments'),vals.get('saved'),vals.get('shares'),time.time(),row['media_id']))
        except MetaError as exc:
            with connect(True) as db:
                db.execute('UPDATE history SET metrics_at=?,metrics_error=? WHERE media_id=?',(time.time(),str(exc),row['media_id']))
            if exc.code == 190:
                raise

def create_container(account_id, job, url):
    body = {'media_type':'REELS','video_url':url,'caption':job['caption'],'share_to_feed':job['lane']=='main'}
    if job['lane']=='trial':
        body['trial_params'] = {'graduation_strategy':'MANUAL'}
    return request('POST',account_id+'/media',json=body)['id']

def container_status(container_id):
    return request('GET',container_id,params={'fields':'status_code,status'}).get('status_code')

def publish(account_id, container_id):
    return request('POST',account_id+'/media_publish',json={'creation_id':container_id})['id']
