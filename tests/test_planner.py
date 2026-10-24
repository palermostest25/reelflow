from datetime import datetime
from zoneinfo import ZoneInfo
import time
from app.db import connect
from app.planner import plan
from conftest import seed,set_settings

NOW=datetime(2026,10,20,8,tzinfo=ZoneInfo('Australia/Perth')).timestamp()

def jobs():
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT * FROM jobs ORDER BY scheduled_at')]

def test_each_clip_has_two_unique_lanes_and_caps():
    seed(30)
    set_settings(conserve=False,min_gap_days=0,trials_per_day=3)
    plan(NOW)
    plan(NOW)
    all_jobs=jobs()
    assert len(all_jobs)==60
    assert len({(j['clip_id'],j['lane']) for j in all_jobs})==60
    for lane,cap in [('main',1),('trial',3)]:
        counts={}
        for j in all_jobs:
            if j['lane']!=lane:continue
            day=datetime.fromtimestamp(j['scheduled_at'],ZoneInfo('Australia/Perth')).date()
            counts[day]=counts.get(day,0)+1
        assert max(counts.values())<=cap

def test_conservation_spaces_both_lanes_and_alerts_runway():
    seed(5)
    set_settings(drought_until='2026-11-20')
    result=plan(NOW)
    assert result['interval']==2 and result['runway_days']==10 and result['drought_target_days']==31
    for lane in ('main','trial'):
        lane_jobs=[j for j in jobs() if j['lane']==lane]
        assert all((datetime.fromtimestamp(b['scheduled_at'],ZoneInfo('Australia/Perth')).date()-datetime.fromtimestamp(a['scheduled_at'],ZoneInfo('Australia/Perth')).date()).days>=2 for a,b in zip(lane_jobs,lane_jobs[1:]))

def test_replanning_preserves_due_posts_and_pins():
    seed(12)
    set_settings(conserve=False)
    plan(NOW)
    first=next(j for j in jobs() if j['lane']=='main')
    plan(first['scheduled_at']+1)
    assert next(j for j in jobs() if j['id']==first['id'])['scheduled_at']==first['scheduled_at']
    with connect(True) as db:
        db.execute('UPDATE jobs SET pinned=1,caption_locked=1,caption=? WHERE id=?',('My edited caption',first['id']))
    seed(5,prefix='new')
    plan(NOW)
    fixed=next(j for j in jobs() if j['id']==first['id'])
    assert fixed['scheduled_at']==first['scheduled_at'] and fixed['caption']=='My edited caption'

def test_new_footage_mingles_and_rejections_stay_rejected():
    original=seed(30)
    set_settings(conserve=False,min_gap_days=0)
    plan(NOW)
    old=jobs()
    with connect(True) as db:
        db.execute("UPDATE jobs SET status='rejected' WHERE id=?",(old[0]['id'],))
    new=seed(15,prefix='fresh')
    plan(NOW)
    ordered=[j for j in jobs() if j['lane']=='main' and j['status']=='queued']
    positions=[i for i,j in enumerate(ordered) if j['clip_id'] in new]
    assert min(positions)<len(original)//2
    assert next(j for j in jobs() if j['id']==old[0]['id'])['status']=='rejected'

def test_same_clip_gap_and_timezone_dst():
    seed(12)
    set_settings(timezone='America/New_York',conserve=False,min_gap_days=7)
    plan(datetime(2026,10,29,8,tzinfo=ZoneInfo('America/New_York')).timestamp())
    by_clip={}
    for j in jobs():by_clip.setdefault(j['clip_id'],[]).append(j['scheduled_at'])
    assert all(abs(v[1]-v[0])>=7*86400 for v in by_clip.values())
    for j in jobs():assert 8<=datetime.fromtimestamp(j['scheduled_at'],ZoneInfo('America/New_York')).hour<=22

def test_manual_approval_is_required_for_new_jobs():
    seed(2)
    set_settings(require_approval=True)
    plan(NOW)
    assert all(not j['approved'] for j in jobs())
