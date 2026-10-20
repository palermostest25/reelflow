"""Durable two-lane planner. Replans only unpinned pending jobs."""
import hashlib
import math
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .db import connect, settings, event

ACTIVE = ('queued', 'retry')

def window_scores(db, tz, lane):
    # Mature, same-lane observations only. Smoothed by a neutral prior.
    scores = {}
    rows = db.execute('SELECT * FROM history WHERE origin=\'reelflow\' AND lane=? AND reach>0 AND metrics_at>timestamp+86400', (lane,)).fetchall()
    for r in rows:
        dt = datetime.fromtimestamp(r['timestamp'], tz)
        key = (dt.weekday(), dt.hour)
        interactions = sum(r[k] or 0 for k in ('likes', 'comments', 'saves', 'shares'))
        scores.setdefault(key, []).append(min(interactions / r['reach'], 1))
    return scores

def hours_for(db, s, day, lane):
    scores = window_scores(db, ZoneInfo(s['timezone']), lane)
    baseline = int(s['schedule_hours'][day.weekday()])
    candidates = sorted(set([baseline, 12, 15, 18, 20, 21]))
    # Deterministic exploration on 1/5 days; otherwise learn with shrinkage.
    explore = day.toordinal() % 5 == 0
    def score(h):
        vals = scores.get((day.weekday(), h), [])
        return (sum(vals) + .04 * 5) / (len(vals) + 5)
    observed = sum(len(v) for k, v in scores.items() if k[0] == day.weekday())
    best = max(candidates, key=lambda h: (score(h), h == baseline)) if observed >= 5 and not explore else baseline
    if explore:
        best = candidates[(day.toordinal() // 5) % len(candidates)]
    reason = 'Learning from mature Reel engagement' if observed >= 5 and not explore else 'Test window · audience timing still learning'
    if lane == 'main':
        return [best], reason
    alternatives = sorted([h for h in candidates if abs(h-best) >= 2], key=lambda h: (-score(h), h))
    picks = [best]
    for h in alternatives:
        if all(abs(h-x) >= 2 for x in picks):
            picks.append(h)
        if len(picks) >= s['trials_per_day']:
            break
    return sorted(picks[:s['trials_per_day']]), reason

def conservation(s, main_count, now):
    days = max(0, (datetime.fromisoformat(s['drought_until']).date() - datetime.fromtimestamp(now, ZoneInfo(s['timezone'])).date()).days) if s['drought_until'] else 0
    if not s['conserve']:
        return 1
    return 2 if main_count <= s['low_watermark'] or (days and main_count < days) else 1

def plan(now=None):
    now = now or time.time()
    with connect(True) as db:
        s = settings(db)
        tz = ZoneInfo(s['timezone'])
        for r in db.execute('SELECT id,caption FROM clips WHERE status=\'ready\' AND disabled=0').fetchall():
            for lane in ('main', 'trial'):
                db.execute('INSERT OR IGNORE INTO jobs(clip_id,lane,caption,approved) VALUES(?,?,?,?)', (r['id'], lane, r['caption'], int(not s['require_approval'])))
        jobs = db.execute("SELECT j.*,c.created_at,c.category FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.status IN ('queued','retry') AND c.disabled=0 AND c.status='ready'").fetchall()
        main_count = sum(j['lane'] == 'main' for j in jobs)
        interval = conservation(s, main_count, now)
        run_day = datetime.fromtimestamp(now, tz).date()
        seed = run_day.isoformat()
        # Recompute all flexible jobs so a new intake batch is mingled, never just appended.
        for lane in ('main', 'trial'):
            protected = {j['id'] for j in jobs if j['scheduled_at'] and now-1800 <= j['scheduled_at'] <= now+1800}
            pending = [j for j in jobs if j['lane'] == lane and not j['pinned'] and j['id'] not in protected]
            for j in pending:
                db.execute('UPDATE jobs SET scheduled_at=NULL WHERE id=?',(j['id'],))
            if lane == 'trial' and not s['trials_enabled']:
                continue
            fixed = db.execute("SELECT clip_id,scheduled_at,published_at FROM jobs WHERE lane=? AND (status='published' OR (pinned=1 AND status IN ('queued','retry')) OR status IN ('processing','publishing','uncertain'))", (lane,)).fetchall()
            fixed = list(fixed) + [j for j in jobs if j['lane']==lane and j['id'] in protected and not j['pinned']]
            occupied = [r['published_at'] or r['scheduled_at'] for r in fixed if r['published_at'] or r['scheduled_at']]
            rank = lambda j: hashlib.sha256((seed + lane + j['clip_id']).encode()).hexdigest()
            pending.sort(key=rank)
            last_category = None
            for offset in range(366):
                if not pending:
                    break
                day = datetime.combine(run_day + timedelta(days=offset), datetime.min.time(), tz)
                hours, reason = hours_for(db, s, day, lane)
                # In conservation mode Trial cadence also drops to one on regular-post days.
                if lane == 'trial' and interval > 1:
                    hours = hours[:1]
                for hour in hours:
                    ts = day.replace(hour=hour).timestamp()
                    if ts < now + 300 or not pending:
                        continue
                    same_day = [x for x in occupied if datetime.fromtimestamp(x, tz).date() == day.date()]
                    if len(same_day) >= (1 if lane == 'main' or interval > 1 else s['trials_per_day']):
                        continue
                    if interval > 1 and any(abs((day.date()-datetime.fromtimestamp(x, tz).date()).days) < 2 for x in occupied):
                        continue
                    if any(abs(ts-x) < 5400 for x in occupied):
                        continue
                    for j in sorted(pending, key=lambda x: (x['category'] == last_category and bool(last_category), rank(x))):
                        other = db.execute("SELECT scheduled_at,published_at FROM jobs WHERE clip_id=? AND lane!=? AND status NOT IN ('rejected','failed')", (j['clip_id'], lane)).fetchone()
                        other_at = (other['published_at'] or other['scheduled_at']) if other else None
                        if other_at and abs(ts-other_at) < s['min_gap_days'] * 86400:
                            continue
                        break
                    else:
                        continue
                    db.execute('UPDATE jobs SET scheduled_at=?,reason=? WHERE id=?', (ts, ('Footage conservation · every second day. ' if interval > 1 else '') + reason, j['id']))
                    occupied.append(ts)
                    last_category = j['category']
                    pending.remove(j)
        return {'interval': interval, 'main_remaining': main_count, 'runway_days': main_count * interval, 'drought_target_days': max(0, (datetime.fromisoformat(s['drought_until']).date() - datetime.fromtimestamp(now,tz).date()).days) if s['drought_until'] else 0}
