import time
from urllib.parse import quote
import httpx
from .db import connect, settings, secret, event

def enqueue(key, message):
    with connect(True) as db:
        db.execute("INSERT OR IGNORE INTO notices(key,message) VALUES(?,?)",(key,message))

def flush():
    s = settings()
    if not s['ntfy_topic']:
        return
    with connect() as db:
        rows = db.execute("SELECT * FROM notices WHERE status='pending' AND last_attempt<? ORDER BY rowid LIMIT 5",(time.time()-300,)).fetchall()
    token = secret('ntfy_token')
    for r in rows:
        from . import config as c
        headers = {'Click':c.PUBLIC_URL,'Title':'Reelflow needs your attention','Tags':'movie_camera'}
        if token:
            headers['Authorization'] = 'Bearer '+token
        try:
            with httpx.Client(timeout=15) as client:
                response = client.post(s['ntfy_url'].rstrip('/')+'/'+quote(s['ntfy_topic'],safe=''),content=r['message'].encode(),headers=headers)
                response.raise_for_status()
            with connect(True) as db:
                db.execute("UPDATE notices SET status='sent',sent_at=?,attempts=attempts+1,last_attempt=? WHERE key=?",(time.time(),time.time(),r['key']))
            event('Notification sent: '+r['message'])
        except Exception:
            with connect(True) as db:
                db.execute('UPDATE notices SET attempts=attempts+1,last_attempt=? WHERE key=?',(time.time(),r['key']))

def stock_alert(summary):
    s = settings()
    if summary.get('stock_remaining',summary['main_remaining']) <= s['low_watermark'] and (s['main_per_day'] or (s['trials_enabled'] and s['trials_per_day'])):
        day = time.strftime('%Y-%m-%d',time.gmtime())
        enqueue('stock:'+day,'Footage is running low: %s clips left for the active posting rhythm, approximately %s days at the current pace. Open Reelflow to set when more footage is coming.'%(summary.get('stock_remaining',summary['main_remaining']),summary['runway_days']))
    if (s['main_per_day'] or (s['trials_enabled'] and s['trials_per_day'])) and summary['drought_target_days'] > summary['runway_days']:
        enqueue('drought:'+s['drought_until'],'Even the configured conservation pace cannot cover the footage drought. Add clips or pause posting before the library runs out.')
