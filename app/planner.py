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

def daily_limit(s, lane, interval=1):
    if lane == 'trial' and not s['trials_enabled']:
        return 0
    cap = s['main_per_day'] if lane == 'main' else s['trials_per_day']
    return min(cap, s['conserve_main_per_day'] if lane == 'main' else s['conserve_trials_per_day']) if interval > 1 else cap

def hours_for(db, s, day, lane, limit=None):
    count = daily_limit(s, lane) if limit is None else limit
    if not count:
        return [], 'This destination is switched off'
    scores = window_scores(db, ZoneInfo(s['timezone']), lane)
    start, end = s['posting_start_hour'], s['posting_end_hour']
    baseline = max(start, min(end, int(s['schedule_hours'][day.weekday()])))
    candidates = list(range(start, end+1))
    # Deterministic exploration on 1/5 days; otherwise learn with shrinkage.
    explore = s['learn_timing'] and day.toordinal() % 5 == 0
    def score(h):
        vals = scores.get((day.weekday(), h), [])
        return (sum(vals) + .04 * 5) / (len(vals) + 5)
    observed = sum(len(v) for k, v in scores.items() if k[0] == day.weekday())
    learned = s['learn_timing'] and observed >= 5 and not explore
    best = max(candidates, key=lambda h: (score(h), h == baseline)) if learned else baseline
    if explore:
        best = candidates[(day.toordinal() // 5) % len(candidates)]
    reason = ('Learning from mature Reel engagement' if learned else 'Test window · audience timing still learning') if s['learn_timing'] else 'Your configured posting window'
    # Half-hour slots let a wide window support ten posts without bunching them up.
    candidates = [minute/60 for minute in range(start*60, end*60+1, 30)]
    picks = [best]
    while len(picks) < count:
        available = [h for h in candidates if all(abs(h-x) >= 1.5 for x in picks)]
        if not available:
            break
        picks.append(max(available, key=lambda h: (min(abs(h-x) for x in picks), score(int(h)), -h)))
    # A centred first pick can strand room at both ends; evenly spread if needed.
    if len(picks) < count:
        picks = [start + (end-start)*i/(count-1) for i in range(count)]
    return sorted(picks), reason

def conservation(s, main_count, now):
    days = max(0, (datetime.fromisoformat(s['drought_until']).date() - datetime.fromtimestamp(now, ZoneInfo(s['timezone'])).date()).days) if s['drought_until'] else 0
    if not s['conserve']:
        return 1
    cap = daily_limit(s, 'main') or daily_limit(s, 'trial')
    return s['conserve_every_days'] if cap and (main_count <= s['low_watermark'] or (days and main_count < days*cap)) else 1

def runway_days(s, main_count, trial_count, interval):
    lane, count = ('main', main_count) if daily_limit(s, 'main') else ('trial', trial_count)
    cap = daily_limit(s, lane, interval)
    return math.ceil(count/cap)*interval if cap else 0

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
        trial_count = sum(j['lane'] == 'trial' for j in jobs)
        stock_count = main_count if daily_limit(s, 'main') else trial_count
        interval = conservation(s, stock_count, now)
        run_day = datetime.fromtimestamp(now, tz).date()
        seed = run_day.isoformat()
        # Recompute all flexible jobs so a new intake batch is mingled, never just appended.
        for lane in ('main', 'trial'):
            protected = {j['id'] for j in jobs if j['scheduled_at'] and now-1800 <= j['scheduled_at'] <= now+1800}
            pending = [j for j in jobs if j['lane'] == lane and not j['pinned'] and j['id'] not in protected]
            for j in pending:
                db.execute('UPDATE jobs SET scheduled_at=NULL WHERE id=?',(j['id'],))
            cap = daily_limit(s, lane, interval)
            if not cap:
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
                hours, reason = hours_for(db, s, day, lane, cap)
                for hour in hours:
                    minute = round(hour*60)
                    ts = day.replace(hour=minute//60, minute=minute%60).timestamp()
                    if ts < now + 300 or not pending:
                        continue
                    same_day = [x for x in occupied if datetime.fromtimestamp(x, tz).date() == day.date()]
                    if len(same_day) >= cap:
                        continue
                    if interval > 1 and any(0 < abs((day.date()-datetime.fromtimestamp(x, tz).date()).days) < interval for x in occupied):
                        continue
                    if any(abs(ts-x) < 5400 for x in occupied):
                        continue
                    for j in sorted(pending, key=lambda x: (x['category'] == last_category and bool(last_category), rank(x))):
                        other = db.execute("SELECT scheduled_at,published_at FROM jobs WHERE clip_id=? AND lane!=? AND status NOT IN ('rejected','failed')", (j['clip_id'], lane)).fetchone()
                        other_at = (other['published_at'] or other['scheduled_at']) if other else None
                        other_enabled = daily_limit(s,'trial' if lane=='main' else 'main')
                        if other_enabled and other_at and abs(ts-other_at) < s['min_gap_days'] * 86400:
                            continue
                        break
                    else:
                        continue
                    db.execute('UPDATE jobs SET scheduled_at=?,reason=? WHERE id=?', (ts, ('Footage conservation · every %s days. ' % interval if interval > 1 else '') + reason, j['id']))
                    occupied.append(ts)
                    last_category = j['category']
                    pending.remove(j)
        return {'interval': interval, 'main_remaining': main_count, 'stock_remaining': stock_count,
                'runway_days': runway_days(s, main_count, trial_count, interval), 'drought_target_days': max(0, (datetime.fromisoformat(s['drought_until']).date() - datetime.fromtimestamp(now,tz).date()).days) if s['drought_until'] else 0}
