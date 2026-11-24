"""Durable two-lane planner. Replans only unpinned pending jobs."""
import hashlib
import importlib
import math
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from . import config as c
from .db import connect, settings, event
from .songs import groups as song_groups

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
    if lane == 'trial' and (not s['trials_enabled'] or s.get('trials_blocked')):
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

LOOKAHEAD = 8
SONG_MEMORY = 4

def natural(name):
    return [(0,int(x),'') if x.isdigit() else (1,0,x.casefold()) for x in re.split(r'(\d+)',name)]

def running_order(pending, meta, seed, lane):
    """Target positions: mixed sets spread evenly through the queue; 'post next' sets lead, in order."""
    def fraction(key):
        return int(hashlib.sha256((seed+lane+key).encode()).hexdigest()[:12],16)/16**12
    by_batch = {}
    for j in pending:
        by_batch.setdefault(meta[j['clip_id']]['batch'],[]).append(j)
    position = {}
    leading = sorted((b for b in by_batch if meta[by_batch[b][0]['clip_id']]['mode']=='next'),
                     key=lambda b:(meta[by_batch[b][0]['clip_id']]['batch_at'],b))
    for rank,batch in enumerate(leading):
        for i,j in enumerate(sorted(by_batch[batch],key=lambda j:natural(meta[j['clip_id']]['name']))):
            position[j['id']] = -1000000 + rank*10000 + i
    for batch,members in by_batch.items():
        if batch in leading:
            continue
        offset = fraction('batch:'+batch)
        members.sort(key=lambda j:fraction(j['clip_id']))
        for i,j in enumerate(members):
            position[j['id']] = (i+offset)/len(members)
    return position

def candidates(pending, position, meta, recent_songs, last, recent_kinds=(), spacing=None):
    """Clips to try for the next slot, best first.

    1. Never repeat a song sooner than its share of the stock allows (up to five posts apart),
       and keep talking, behind-the-scenes and promo clips at least their configured gap apart.
    2. A song holding half of what is left goes whenever it may, or it would bunch up at the end.
    3. Near the front of the mix, prefer a different set, then a different scene.
    """
    ordered = sorted(pending,key=lambda j:position[j['id']])
    if ordered and position[ordered[0]['id']] < 0:
        return ordered  # A set the operator asked to post next keeps its order.
    remaining = {}
    for j in ordered:
        song = meta[j['clip_id']]['song']
        remaining[song] = remaining.get(song,0)+1
    total = len(ordered)
    last_seen = {song:len(recent_songs)-i for i,song in enumerate(recent_songs)}
    kind_seen = {kind:len(recent_kinds)-i for i,kind in enumerate(recent_kinds)}
    spacing = spacing or {}
    sets = {meta[j['clip_id']]['batch'] for j in ordered}
    front = {j['id'] for j in ordered[:LOOKAHEAD]}
    def key(j):
        m = meta[j['clip_id']]
        count = remaining[m['song']]
        need = min(SONG_MEMORY+1, max(2, total//count)) if len(remaining)>1 else 0
        too_soon = last_seen.get(m['song'],10**6) < need or kind_seen.get(m['kind'],10**6) < spacing.get(m['kind'],0)
        dominant = count*2 >= total+1
        if j['id'] in front:
            local = (0, len(sets)>1 and m['batch']==last.get('batch'),
                     bool(last.get('category')) and j['category']==last.get('category'), position[j['id']])
        else:
            local = (1, False, False, position[j['id']])
        return (too_soon, not dominant, local)
    return sorted(ordered,key=key)

KIND_SQL = "COALESCE(NULLIF(c.kind,''),b.kind,'performance')"

def mix_for(s, kind):
    """(lanes, every) for a content type. DJ footage goes everywhere with no extra gap."""
    if kind == 'performance':
        return ('main','trial'), 0
    rule = {**c.DEFAULTS['content_mix'].get(kind,{'lanes':'both','every':3}), **(s.get('content_mix') or {}).get(kind,{})}
    lanes = {'both':('main','trial'),'main':('main',),'trial':('trial',)}.get(rule['lanes'],('main','trial'))
    return lanes, int(rule['every'])

def day_or_none(text):
    try:
        return date.fromisoformat(text) if text else None
    except ValueError:
        return None

def clip_meta(db):
    songs = song_groups(db)
    rows = db.execute('SELECT c.id,c.name,c.batch,COALESCE(b.mode,\'mix\') mode,COALESCE(b.created_at,c.created_at) batch_at,'
                      +KIND_SQL+' kind,c.post_on,c.post_at,c.not_before,c.not_after FROM clips c LEFT JOIN intake_batches b ON b.id=c.batch').fetchall()
    return {r['id']:{'name':r['name'],'batch':r['batch'] or 'set:'+r['id'],'mode':r['mode'],'batch_at':r['batch_at'],
                     'song':songs.get(r['id'],r['id']),'kind':r['kind'],'post_on':day_or_none(r['post_on']),'post_at':r['post_at'],
                     'not_before':day_or_none(r['not_before']),'not_after':day_or_none(r['not_after'])} for r in rows}

def in_window(m, day):
    return not ((m['not_before'] and day < m['not_before']) or (m['not_after'] and day > m['not_after']))

def plan(now=None):
    now = now or time.time()
    with connect(True) as db:
        s = settings(db)
        tz = ZoneInfo(s['timezone'])
        meta = clip_meta(db)
        for r in db.execute('SELECT id,caption FROM clips WHERE status=\'ready\' AND disabled=0').fetchall():
            lanes, _ = mix_for(s, meta[r['id']]['kind'])
            for lane in lanes:
                db.execute('INSERT OR IGNORE INTO jobs(clip_id,lane,caption,approved) VALUES(?,?,?,?)', (r['id'], lane, r['caption'], int(not s['require_approval'])))
            for lane in {'main','trial'}-set(lanes):
                # This type no longer goes to this destination: drop untouched, never-attempted posts there.
                db.execute("""DELETE FROM jobs WHERE clip_id=? AND lane=? AND status IN ('queued','retry') AND pinned=0 AND caption_locked=0
                    AND attempts=0 AND container_id IS NULL AND post_now_at IS NULL""",(r['id'],lane))
        jobs = db.execute("SELECT j.*,c.created_at,c.category FROM jobs j JOIN clips c ON c.id=j.clip_id WHERE j.status IN ('queued','retry') AND c.disabled=0 AND c.status='ready'").fetchall()
        main_count = sum(j['lane'] == 'main' for j in jobs)
        trial_count = sum(j['lane'] == 'trial' for j in jobs)
        stock_count = main_count if daily_limit(s, 'main') else trial_count
        interval = conservation(s, stock_count, now)
        run_day = datetime.fromtimestamp(now, tz).date()
        seed = run_day.isoformat()
        spacing = {kind:mix_for(s,kind)[1] for kind in c.KINDS}
        # Recompute all flexible jobs so a new intake batch is mingled, never just appended.
        for lane in ('main', 'trial'):
            protected = {j['id'] for j in jobs if j['post_now_at'] or (j['scheduled_at'] and now-1800 <= j['scheduled_at'] <= now+1800)}
            pending = [j for j in jobs if j['lane'] == lane and not j['pinned'] and not j['held'] and j['id'] not in protected]
            for j in pending:
                db.execute('UPDATE jobs SET scheduled_at=NULL WHERE id=?',(j['id'],))
            # Dated clips take their day first; everything else fills in around them.
            dated = [j for j in pending if meta[j['clip_id']]['post_on'] and lane in mix_for(s,meta[j['clip_id']]['kind'])[0]]
            dated_ids = {j['id'] for j in dated}
            pending = [j for j in pending if j['id'] not in dated_ids and lane in mix_for(s,meta[j['clip_id']]['kind'])[0]]
            dated_at = []
            by_day = {}
            for j in sorted(dated,key=lambda j:(meta[j['clip_id']]['post_at'] or '99',meta[j['clip_id']]['name'])):
                by_day.setdefault(meta[j['clip_id']]['post_on'],[]).append(j)
            for when,group in by_day.items():
                day = datetime.combine(when, datetime.min.time(), tz)
                slots = hours_for(db, s, day, lane, max(len(group), daily_limit(s, lane) or 1))[0] or [s['schedule_hours'][when.weekday()]]
                free = iter(sorted(slots))
                for j in group:
                    at = meta[j['clip_id']]['post_at']
                    if at:
                        ts = day.replace(hour=int(at[:2]),minute=int(at[3:5])).timestamp()
                    else:
                        hour = next(free, slots[-1])
                        ts = day.replace(hour=int(hour), minute=round(hour%1*60)).timestamp()
                    db.execute('UPDATE jobs SET scheduled_at=?,reason=? WHERE id=?',(ts,'Dated · you chose '+when.strftime('%-d %b')+(' at '+at if at else ''),j['id']))
                    dated_at.append(ts)
            cap = daily_limit(s, lane, interval)
            if not cap:
                continue
            # Post now is extra to the scheduled cadence, so its slot can be filled again.
            fixed = db.execute("SELECT clip_id,scheduled_at,published_at FROM jobs WHERE lane=? AND ((status='published' AND post_now_at IS NULL) OR (pinned=1 AND status IN ('queued','retry')) OR status IN ('processing','publishing','uncertain'))", (lane,)).fetchall()
            fixed = list(fixed) + [j for j in jobs if j['lane']==lane and j['id'] in protected and not j['pinned']]
            occupied = [r['published_at'] or r['scheduled_at'] for r in fixed if r['published_at'] or r['scheduled_at']] + dated_at
            position = running_order(pending, meta, seed, lane)
            history = db.execute("""SELECT clip_id FROM jobs WHERE lane=? AND COALESCE(published_at,scheduled_at)<=?
                AND (status IN ('published','processing','publishing','uncertain') OR id IN (%s))
                ORDER BY COALESCE(published_at,scheduled_at) DESC LIMIT ?""" % ','.join('?'*len(protected)),
                (lane,now+1800,*protected,SONG_MEMORY)).fetchall()
            recent_songs = [meta[r['clip_id']]['song'] for r in reversed(history) if r['clip_id'] in meta]
            recent_kinds = [meta[r['clip_id']]['kind'] for r in reversed(history) if r['clip_id'] in meta]
            last = {'batch':meta[history[0]['clip_id']]['batch'] if history and history[0]['clip_id'] in meta else None,'category':None}
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
                    for j in candidates([j for j in pending if in_window(meta[j['clip_id']], day.date())], position, meta, recent_songs, last, recent_kinds, spacing):
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
                    recent_songs.append(meta[j['clip_id']]['song'])
                    recent_kinds.append(meta[j['clip_id']]['kind'])
                    last = {'batch':meta[j['clip_id']]['batch'],'category':j['category']}
                    pending.remove(j)
            for j in pending:
                m = meta[j['clip_id']]
                if m['not_after'] and m['not_after'] < run_day:
                    db.execute('UPDATE jobs SET reason=? WHERE id=?',('Its posting window ended '+m['not_after'].strftime('%-d %b')+'. Change the dates or exclude the clip.',j['id']))
                elif m['not_before'] or m['not_after']:
                    db.execute('UPDATE jobs SET reason=? WHERE id=?',('Waiting for a free slot in its posting window.',j['id']))
        for module in ('youtube', 'tiktok'):
            try:
                importlib.import_module('.'+module, __package__).mirror(db, now)
            except Exception:
                pass  # Mirroring never holds up the Instagram plan.
        return {'interval': interval, 'main_remaining': main_count, 'stock_remaining': stock_count,
                'runway_days': runway_days(s, main_count, trial_count, interval), 'drought_target_days': max(0, (datetime.fromisoformat(s['drought_until']).date() - datetime.fromtimestamp(now,tz).date()).days) if s['drought_until'] else 0}
