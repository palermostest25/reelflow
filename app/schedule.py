"""Hand scheduling: drag posts between days and destinations, hold them, or drop footage in.

A post placed by hand is pinned to its time and marked `forced`: it goes out even if that day is
already at its daily target, and the automatic planner fills the other days around it. Holding a
post takes it out of the schedule until it is placed again (or released back to the planner).
"""
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from . import media, youtube as yt
from .db import connect, settings, event
from .planner import plan

LANES = ('main', 'trial', 'youtube')
GAP = 90*60
EDITABLE = ('queued', 'retry', 'failed')


class PlaceError(ValueError):
    pass


def place_time(s, lane, day, after=None, before=None, now=None, short_hour=17):
    """A time on `day` (YYYY-MM-DD, account timezone) between the posts it was dropped between."""
    now = now or time.time()
    zone = ZoneInfo(s['timezone'])
    try:
        date = datetime.strptime(day, '%Y-%m-%d').date()
    except (TypeError, ValueError):
        raise PlaceError('Drop the post onto a day.') from None
    start = datetime.combine(date, datetime.min.time(), zone)
    first = start + timedelta(hours=s['posting_start_hour'])
    last = start + timedelta(hours=23, minutes=45)
    if (start + timedelta(days=1)).timestamp() <= now:
        raise PlaceError('That day has passed. Drop it on today or later.')
    if after and before:
        at = (after + before) / 2
    elif after:
        at = after + GAP if after + GAP <= last.timestamp() else (after + last.timestamp()) / 2
    elif before:
        at = before - GAP if before - GAP >= first.timestamp() else (before + first.timestamp()) / 2
    else:
        hour = short_hour if lane == 'youtube' else s['schedule_hours'][date.weekday()] if lane == 'main' \
            else (s['posting_start_hour'] + s['posting_end_hour']) / 2
        at = (start + timedelta(minutes=round(hour*60))).timestamp()
    at = round(at / 300) * 300  # Five-minute steps read well in the schedule.
    at = min(max(at, start.timestamp()), (start + timedelta(days=1)).timestamp() - 300)
    if at < now + 300:
        at = now + 600  # Dropped on today after the slot has gone: as soon as is sensible.
    return at


def schedule_job(db, job_id, at):
    db.execute("""UPDATE jobs SET scheduled_at=?,pinned=1,forced=1,held=0,approved=1,
        status=CASE WHEN status='failed' THEN 'queued' ELSE status END,
        attempts=CASE WHEN status='failed' THEN 0 ELSE attempts END,
        error=CASE WHEN status='failed' THEN '' ELSE error END,next_attempt=0 WHERE id=?""", (at, job_id))


def instagram_job(db, clip, lane, caption=None):
    """The clip's post for this destination, created if it has none yet."""
    job = db.execute('SELECT * FROM jobs WHERE clip_id=? AND lane=?', (clip['id'], lane)).fetchone()
    if job:
        if job['status'] == 'published':
            raise PlaceError('This clip was already posted as a %s. Use Remove & repost in Published to post it again.'
                             % ('grid Reel' if lane == 'main' else 'Trial'))
        if job['status'] not in EDITABLE:
            raise PlaceError('This post is being published right now. Wait for it to finish.')
        if job['status'] == 'rejected':
            db.execute("UPDATE jobs SET status='queued' WHERE id=?", (job['id'],))
        return job['id']
    if clip['status'] != 'ready' or clip['disabled']:
        raise PlaceError('This clip is not ready to post yet. Wait for it to be prepared and captioned.')
    return db.execute('INSERT INTO jobs(clip_id,lane,caption,approved) VALUES(?,?,?,1)',
                      (clip['id'], lane, caption if caption is not None else clip['caption'])).lastrowid


def youtube_short(db, clip, at):
    short = db.execute("SELECT * FROM youtube_jobs WHERE kind='short' AND source_key=?", (clip['id'],)).fetchone()
    if short:
        if short['status'] in ('uploaded', 'uploading'):
            raise PlaceError('This clip is already on YouTube.')
        short_id = short['id']
    else:
        try:
            duration = yt.short_info(clip)
        except yt.YouTubeError as exc:
            raise PlaceError(str(exc)) from None
        short_id = yt.add_draft(db, yt.preferences(db), 'short', clip['id'], clip['name'], duration, caption=clip['caption'])
    db.execute("""UPDATE youtube_jobs SET scheduled_at=?,status='queued',pinned=1,forced=1,mirror_of=NULL,error='',
        next_attempt=0 WHERE id=?""", (at, short_id))
    return short_id


def place(kind, item, lane, day, after=None, before=None, now=None):
    """Put a post (or a clip from the footage library) on a day in a destination."""
    if lane not in LANES:
        raise PlaceError('Choose a destination.')
    s = settings()
    now = now or time.time()
    with connect(True) as db:
        prefs = yt.preferences(db)
        at = place_time(s, lane, day, after, before, now, prefs.short_hour)
        source_job = None
        if kind == 'job':
            source_job = db.execute('SELECT * FROM jobs WHERE id=?', (int(item),)).fetchone()
            if not source_job:
                raise PlaceError('Post not found.')
            clip_id = source_job['clip_id']
        elif kind == 'youtube':
            short = db.execute('SELECT * FROM youtube_jobs WHERE id=?', (int(item),)).fetchone()
            if not short:
                raise PlaceError('YouTube post not found.')
            if lane == 'youtube':
                if short['status'] in ('uploaded', 'uploading'):
                    raise PlaceError('This video is already on YouTube.')
                db.execute("""UPDATE youtube_jobs SET scheduled_at=?,status='queued',pinned=1,forced=1,mirror_of=NULL,error='',
                    next_attempt=0 WHERE id=?""", (at, short['id']))
                yt.plan(db, prefs, now)
                event('YouTube post moved by hand.', db=db)
                return at
            if short['kind'] != 'short':
                raise PlaceError('Full sets only go to YouTube.')
            clip_id = short['source_key']
        elif kind == 'clip':
            clip_id = str(item)
        else:
            raise PlaceError('Unknown item.')
        clip = db.execute('SELECT * FROM clips WHERE id=?', (clip_id,)).fetchone()
        if not clip:
            raise PlaceError('Footage not found.')
        if lane == 'youtube':
            youtube_short(db, clip, at)
            yt.plan(db, prefs, now)
        else:
            if source_job is not None and source_job['lane'] == lane:
                if source_job['status'] not in EDITABLE:
                    raise PlaceError('This post is published or being published, so it stays where it is.')
                job_id = source_job['id']
            else:
                job_id = instagram_job(db, clip, lane, source_job['caption'] if source_job is not None else None)
                if source_job is not None and source_job['status'] in EDITABLE:
                    # Moving between destinations: the old post leaves the schedule.
                    db.execute('UPDATE jobs SET held=1,pinned=0,forced=0,scheduled_at=NULL WHERE id=?', (source_job['id'],))
            schedule_job(db, job_id, at)
        event(f'{clip["name"]} placed by hand for ' + datetime.fromtimestamp(at, ZoneInfo(s['timezone'])).strftime('%a %-d %b %H:%M')
              + {'main': ' (grid)', 'trial': ' (Trial)', 'youtube': ' (YouTube)'}[lane] + '.', db=db)
    plan(now)
    return at


def hold(kind, item):
    """Take a post out of the schedule until it is placed again."""
    with connect(True) as db:
        if kind == 'job':
            if db.execute("""UPDATE jobs SET held=1,pinned=0,forced=0,scheduled_at=NULL
                WHERE id=? AND status IN ('queued','retry','failed')""", (int(item),)).rowcount != 1:
                raise PlaceError('Only upcoming posts can be held.')
        elif kind == 'youtube':
            if db.execute("""UPDATE youtube_jobs SET status='draft',pinned=0,forced=0,mirror_of=NULL,scheduled_at=NULL
                WHERE id=? AND status IN ('queued','failed','draft')""", (int(item),)).rowcount != 1:
                raise PlaceError('Only upcoming YouTube posts can be held.')
        else:
            raise PlaceError('Unknown item.')
        event('Post put on hold.', db=db)
    plan()


def release(kind, item):
    """Give a held post back to the automatic planner."""
    with connect(True) as db:
        if kind == 'job':
            db.execute('UPDATE jobs SET held=0,pinned=0,forced=0 WHERE id=?', (int(item),))
        elif kind == 'youtube':
            db.execute("UPDATE youtube_jobs SET status='queued',pinned=0,forced=0 WHERE id=? AND status='draft'", (int(item),))
            yt.plan(db, yt.preferences(db), time.time())
        else:
            raise PlaceError('Unknown item.')
    plan()


def tray(query='', offset=0, limit=40):
    """Held posts, YouTube drafts and footage that can be dragged into the schedule."""
    with connect() as db:
        held = [dict(r) for r in db.execute("""SELECT j.id,j.clip_id,j.lane,j.caption,j.status,c.name FROM jobs j
            JOIN clips c ON c.id=j.clip_id WHERE j.held=1 AND j.status IN ('queued','retry','failed') AND c.disabled=0
            ORDER BY j.id DESC LIMIT 100""")]
        drafts = [dict(r) for r in db.execute("""SELECT id,kind,source_key,title,status FROM youtube_jobs
            WHERE status='draft' ORDER BY created_at DESC LIMIT 100""")]
        like = '%' + query.strip().replace('%', '') + '%'
        clips = [dict(r) for r in db.execute("""SELECT c.id,c.name,c.duration,c.width,c.height,c.caption,
            (SELECT status FROM jobs WHERE clip_id=c.id AND lane='main') AS main,
            (SELECT status FROM jobs WHERE clip_id=c.id AND lane='trial') AS trial,
            (SELECT status FROM youtube_jobs WHERE kind='short' AND source_key=c.id) AS youtube
            FROM clips c WHERE c.status='ready' AND c.disabled=0 AND c.name LIKE ?
            ORDER BY (SELECT COUNT(*) FROM jobs WHERE clip_id=c.id AND status='published'), c.created_at DESC
            LIMIT ? OFFSET ?""", (like, limit, offset))]
    for clip in clips:
        clip['short_ok'] = clip['width'] <= clip['height'] and 0 < clip['duration'] <= 180
    return {'held': held, 'drafts': drafts, 'footage': clips}
