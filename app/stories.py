"""Share published Reels to Instagram Stories.

Instagram's API can't add the "Reel sticker" the app uses, so the Story is the Reel's own video posted
as a Story. Stories are at most 60 seconds, so longer clips are cut to their first 59 seconds. Grid
Reels are shared by default; Trials stay off Stories unless chosen, because a Story shows them to
followers and the point of a Trial is to reach non-followers first.
"""
import subprocess
import time
from . import config as c, instagram as ig
from .db import connect, event, settings
from .security import media_url

MAX_SECONDS = 60
ATTEMPTS = 6


def lanes(s):
    return {'off': (), 'grid': ('main',), 'all': ('main', 'trial')}.get(s.get('story_share'), ('main',))


def queue(db, job, media_id):
    """Called when a Reel publishes: share it to Stories if the setting covers its destination."""
    if media_id and job['lane'] in lanes(settings(db)):
        db.execute("""INSERT OR IGNORE INTO story_posts(job_id,clip_id,status,created_at,next_attempt)
            VALUES(?,?,'pending',?,?)""", (job['id'], job['clip_id'], time.time(), time.time()))


def story_file(clip_id, duration):
    """The video Instagram fetches for the Story: the export itself, or its first 59 seconds."""
    if duration <= MAX_SECONDS - 0.5:
        return clip_id
    folder = c.DATA/'stories'
    folder.mkdir(exist_ok=True)
    target = folder/(clip_id+'-story.mp4')
    if not target.is_file():
        temp = target.with_suffix('.part.mp4')
        result = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(c.DATA/'media'/(clip_id+'.mp4')),
                                 '-t', '59', '-c', 'copy', '-movflags', '+faststart', str(temp)], capture_output=True, timeout=300)
        if result.returncode:
            temp.unlink(missing_ok=True)
            raise ig.MetaError('Could not cut this clip to Instagram’s 60-second Story limit.')
        temp.replace(target)
    return clip_id+'-story'


def advance(row, account):
    """One step: create the Story container, wait for Instagram to process it, then publish."""
    update = {}
    try:
        if not row['container_id']:
            with connect() as db:
                clip = db.execute('SELECT duration FROM clips WHERE id=?', (row['clip_id'],)).fetchone()
            name = story_file(row['clip_id'], clip['duration'] if clip else 0)
            container = ig.request('POST', account['ig_id']+'/media',
                                   json={'media_type': 'STORIES', 'video_url': media_url(name)})['id']
            update = {'container_id': container, 'status': 'processing', 'next_attempt': time.time()+30}
        else:
            status = ig.container_status(row['container_id'])
            if status == 'FINISHED':
                media_id = ig.publish(account['ig_id'], row['container_id'])
                update = {'status': 'published', 'media_id': media_id, 'error': ''}
                event('Shared to Instagram Stories.', job_id=row['job_id'])
            elif status in ('ERROR', 'EXPIRED'):
                update = {'container_id': None, 'attempts': row['attempts']+1, 'next_attempt': time.time()+600,
                          'error': 'Instagram could not process the Story video.'}
            else:
                update = {'next_attempt': time.time()+30}
    except ig.MetaError as exc:
        attempts = row['attempts']+1
        final = attempts >= ATTEMPTS or exc.code in ig.PERMISSION_CODES
        update = {'attempts': attempts, 'error': str(exc), 'next_attempt': time.time()+min(3600, 120*2**attempts)}
        if final:
            update['status'] = 'failed'
            event('Could not share to Stories: '+str(exc), 'warning', job_id=row['job_id'])
    with connect(True) as db:
        db.execute('UPDATE story_posts SET '+','.join(k+'=?' for k in update)+' WHERE job_id=?', (*update.values(), row['job_id']))


def share_due(now=None, limit=3):
    now = now or time.time()
    s = settings()
    if s['paused'] or s['dry_run'] or c.DEMO:
        return
    with connect() as db:
        account = db.execute('SELECT * FROM account WHERE id=1').fetchone()
        rows = db.execute("""SELECT * FROM story_posts WHERE status IN ('pending','processing') AND next_attempt<=?
            ORDER BY created_at LIMIT ?""", (now, limit)).fetchall()
    if not account or account['expires_at'] <= now:
        return
    for row in rows:
        advance(dict(row), account)


def for_job(job_id):
    with connect() as db:
        row = db.execute('SELECT status,error,media_id FROM story_posts WHERE job_id=?', (job_id,)).fetchone()
    return dict(row) if row else None
