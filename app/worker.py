"""One nonoverlapping worker; run continuously or use --once from cron."""
import fcntl
import json
import logging
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
from . import config as c
from .db import init, connect, settings, event, runtime
from . import media, ai, instagram as ig, notifications as ntfy
from .planner import plan, daily_limit, conservation
from .security import media_url

log = logging.getLogger('reelflow')

@contextmanager
def worker_lock():
    with (c.DATA/'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock,fcntl.LOCK_UN)

def finish(job, media_id=None):
    now = time.time()
    with connect(True) as db:
        db.execute("UPDATE jobs SET status='published',media_id=?,published_at=?,error='' WHERE id=?",(media_id,now,job['id']))
        if media_id:
            db.execute("INSERT OR IGNORE INTO history(media_id,caption,media_type,timestamp,origin,lane) VALUES(?,?,'REELS',?,'reelflow',?)",(media_id,job['caption'],now,job['lane']))
        event('Published '+job['lane']+' Reel.',job_id=job['id'],db=db)
    # Publishing committed before noncritical permalink fetch.
    if media_id:
        try:
            data = ig.request('GET',media_id,params={'fields':'permalink'})
            with connect(True) as db:
                db.execute('UPDATE jobs SET permalink=? WHERE id=?',(data.get('permalink'),job['id']))
                db.execute('UPDATE history SET permalink=? WHERE media_id=?',(data.get('permalink'),media_id))
        except Exception:
            pass

def mark_uncertain(job, message):
    with connect(True) as db:
        db.execute("UPDATE jobs SET status='uncertain',error=? WHERE id=?",(message,job['id']))
        event(message,'error',job['id'],db)
    ntfy.enqueue('uncertain:'+str(job['id']),message+' Open Reelflow and reconcile this post.')

def advance(job_id, now):
    with connect() as db:
        job = db.execute('SELECT j.*,c.disabled,c.status AS clip_status FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.id=?',(job_id,)).fetchone()
        account = db.execute('SELECT * FROM account WHERE id=1').fetchone()
    s = settings()
    if s['paused'] or s['dry_run'] or c.DEMO or not account or not job or job['disabled'] or job['clip_status']!='ready':
        return
    if account['expires_at'] <= now:
        ntfy.enqueue('auth-expired','Instagram connection expired. Reconnect in Settings.')
        return
    if job['status'] in ('published','rejected','failed'):
        return
    if not daily_limit(s,job['lane']):
        return
    if not job['approved']:
        return
    if not c.PUBLIC_URL.startswith('https://'):
        ntfy.enqueue('https-required','Set a public HTTPS URL. Instagram must be able to retrieve the signed footage URL.')
        return
    if job['status'] in ('publishing','uncertain'):
        # Never retry a publish without resolving the saved container first.
        if job['container_id']:
            try:
                if ig.container_status(job['container_id'])=='PUBLISHED':
                    finish(job)
                    return
            except Exception:
                pass
        if job['status']=='publishing':
            mark_uncertain(job,'Instagram publishing response is unresolved. Automatic retries are stopped to prevent a duplicate.')
        return
    try:
        if not job['container_id']:
            with connect(True) as db:
                if db.execute("UPDATE jobs SET status='processing' WHERE id=? AND status IN ('queued','retry','processing')",(job_id,)).rowcount!=1:
                    return
            # Safe to retry container creation after a lost response: it does not publish.
            container_id = ig.create_container(account['ig_id'],job,media_url(job['clip_id']))
            with connect(True) as db:
                db.execute('UPDATE jobs SET container_id=? WHERE id=?',(container_id,job_id))
        else:
            container_id = job['container_id']
        status = ig.container_status(container_id)
        if status=='PUBLISHED':
            finish(job)
            return
        if status in ('ERROR','EXPIRED'):
            raise ig.MetaError('Instagram could not process this Reel container. Check the clip and Trial eligibility.')
        if status!='FINISHED':
            if now-(job['scheduled_at'] or now)>3600:
                raise ig.MetaError('Instagram media processing exceeded an hour. Review this post.')
            return
        # Recheck pause/approval after upload before the external publish boundary.
        with connect(True) as db:
            fresh = db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            current = settings(db)
            disabled = db.execute('SELECT disabled FROM clips WHERE id=?',(job['clip_id'],)).fetchone()[0]
            if disabled:
                return
            if not daily_limit(current,job['lane']):
                return
            if current['paused'] or current['dry_run'] or not fresh['approved'] or fresh['status'] not in ('processing','retry','queued'):
                return
            if fresh['caption'] != job['caption']:
                db.execute("UPDATE jobs SET container_id=NULL,status='queued' WHERE id=?",(job_id,))
                return
            # Stale jobs roll forward via replanning; don't burst catch-up posts.
            start = datetime.fromtimestamp(now,ZoneInfo(current['timezone'])).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
            recent = db.execute("SELECT COUNT(*) FROM jobs WHERE lane=? AND status='published' AND published_at>=?",(job['lane'],start)).fetchone()[0]
            stock_lane = 'main' if daily_limit(current,'main') else 'trial'
            remaining = db.execute("SELECT COUNT(*) FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.lane=? AND j.status IN ('queued','retry') AND c.disabled=0",(stock_lane,)).fetchone()[0]
            cap = daily_limit(current,job['lane'],conservation(current,remaining,now))
            if recent >= cap:
                return
            db.execute("UPDATE jobs SET status='publishing',publish_started=? WHERE id=?",(now,job_id))
        try:
            media_id = ig.publish(account['ig_id'],container_id)
        except Exception:
            mark_uncertain(job,'Instagram may have accepted this Reel, but its publish response was lost or rejected. Check before retrying.')
            return
        finish(job,media_id)
    except ig.MetaError as exc:
        attempts = job['attempts']+1
        retry = exc.transient and attempts<5
        with connect(True) as db:
            db.execute('UPDATE jobs SET status=?,next_attempt=?,error=?,attempts=? WHERE id=?',('retry' if retry else 'failed',now+min(3600,60*2**attempts),str(exc),attempts,job_id))
            event(str(exc),'warning' if retry else 'error',job_id,db)
        ntfy.enqueue('post-error:'+str(job_id),str(exc)+' Open Reelflow to review.')
    except Exception:
        # A storage failure after the publish response must remain held as uncertain.
        with connect(True) as db:
            current = db.execute('SELECT status FROM jobs WHERE id=?',(job_id,)).fetchone()
            if current and current['status']=='publishing':
                db.execute("UPDATE jobs SET status='uncertain',error='Publishing result could not be saved. Reconcile before retrying.' WHERE id=?",(job_id,))
            else:
                attempts = job['attempts']+1
                db.execute("UPDATE jobs SET status=?,attempts=?,next_attempt=?,error='Connection interrupted before publishing. Check the connection and retry after fixing it.' WHERE id=? AND status!='published'",('failed' if attempts>=5 else 'retry',attempts,now+min(3600,60*2**attempts),job_id))
        ntfy.enqueue('post-connection:'+str(job_id),'A Reel encountered a connection or storage problem. Check Reelflow Activity before retrying.')

def tick():
    with worker_lock() as owned:
        if not owned:
            return {'busy':True}
        runtime('heartbeat',time.time())
        # Recovery comes before planning: unresolved publishes must occupy their slots.
        with connect() as db:
            recover = [r[0] for r in db.execute("SELECT id FROM jobs WHERE status IN ('publishing','uncertain','processing')")]
        for job_id in recover:
            advance(job_id,time.time())
        if not c.DEMO:
            if float(runtime('account_retry_at') or 0) < time.time():
                try:
                    ig.refresh_token()
                    ig.sync_history()
                    ig.sync_metrics()
                except Exception:
                    runtime('account_retry_at',time.time()+1800)
                    ntfy.enqueue('sync:'+time.strftime('%Y-%m-%d'),'Instagram sync failed. Check connection and permissions in Reelflow.')
        media.scan()
        ai.caption_pending()
        summary = plan()
        ntfy.stock_alert(summary)
        s = settings()
        if not s['paused'] and not s['dry_run'] and not c.DEMO:
            with connect() as db:
                due = [r[0] for r in db.execute("SELECT id FROM jobs WHERE status IN ('queued','retry') AND approved=1 AND scheduled_at<=? AND next_attempt<=? ORDER BY scheduled_at LIMIT 4",(time.time(),time.time()))]
            for job_id in due:
                advance(job_id,time.time())
        ntfy.flush()
        runtime('heartbeat',time.time())
        return summary

def main():
    logging.basicConfig(level=logging.INFO)
    init()
    once = '--once' in sys.argv
    while True:
        try:
            result = tick()
            log.info('Worker pass complete: %s',result)
        except Exception:
            # Never log request objects or provider responses containing credentials.
            log.error('Worker pass failed; see health state and data integrity. Retrying in 60 seconds.')
            runtime('worker_error_at',time.time())
            if once:
                raise SystemExit(1)
        if once:
            break
        time.sleep(60)

if __name__=='__main__':
    main()
