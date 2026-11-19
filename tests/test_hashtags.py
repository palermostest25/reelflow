"""The hashtag library: always/pool/never groups, bulk import, picking and stats."""
import json
import re
import time
import pytest
from fastapi.testclient import TestClient
from app import hashtags as h, config as c
from app.db import connect, init, settings
from app.main import app

def group(name, mode, tags, low=0, high=5, kinds=()):
    return {'name':name,'mode':mode,'tags':list(tags),'min':low,'max':high,'kinds':list(kinds)}

BIG = ['#Tag%03d' % i for i in range(200)]

def test_pasted_lists_in_any_shape_become_clean_tags():
    text = '''Here are 200 hashtags for a Perth DJ duo:
1. #PerthDJs
2) PerthNightlife
- #TechHouse, #DeepHouse; #techhouse
• "UndergroundDanceMusic"
#WarehouseParty #ClubLife | #DJDuo
not-a-tag 123 #ok
'''
    tags, rejected = h.parse(text)
    assert tags == ['#PerthDJs','#PerthNightlife','#TechHouse','#DeepHouse','#UndergroundDanceMusic','#WarehouseParty','#ClubLife','#DJDuo','#ok']
    assert not rejected  # The '#ok' line only takes its hashtag; the prose line is skipped.
    assert h.parse('good_tag bad-tag 1234')[1] == ['bad-tag','1234']

def test_always_tags_lead_every_post_and_count_toward_five():
    plan = h.Plan([group('Brand','always',['#TRADES']), group('Big','pool',BIG)])
    tags = h.pick(['#Tag005','#Tag010','#TRADES','#Tag020','#Tag030','#Tag040'], plan, 5, new_max=0)
    assert tags == ['#TRADES','#Tag005','#Tag010','#Tag020','#Tag030']

def test_the_model_picks_from_a_200_tag_list_and_new_tags_are_capped():
    plan = h.Plan([group('Brand','always',['#TRADES']), group('Big','pool',BIG)])
    tags = h.pick(['#FreshOne','#FreshTwo','#tag199','#Tag150'], plan, 5, new_max=1)
    assert tags[0] == '#TRADES' and '#FreshOne' in tags and '#FreshTwo' not in tags
    assert '#Tag199' in tags and '#Tag150' in tags and len(tags) == 5  # Canonical spelling from the library.

def test_group_minimums_and_maximums_are_enforced():
    plan = h.Plan([group('Perth','pool',['#PerthDJs','#PerthMusic','#PerthNightlife'],low=1,high=1),
                   group('Genre','pool',['#TechHouse','#DeepHouse','#Minimal','#Disco'],high=2)])
    tags = h.pick(['#TechHouse','#DeepHouse','#Minimal','#Disco','#BrandNew'], plan, 4, new_max=1)
    assert sum(t in ('#PerthDJs','#PerthMusic','#PerthNightlife') for t in tags) == 1
    assert sum(t in ('#TechHouse','#DeepHouse','#Minimal','#Disco') for t in tags) <= 2
    assert len(tags) == 4 and '#BrandNew' in tags

def test_never_tags_and_generic_tags_are_blocked_and_rotation_fills_gaps():
    plan = h.Plan([group('Big','pool',['#A1','#B2','#C3','#D4','#E5','#F6']), group('No','never',['#Nope','#B2'])])
    recent = h.usage(['x\n\n#A1 #C3','x\n\n#A1'])
    tags = h.pick(['#Nope','#fyp','#viral'], plan, 5, new_max=5, recent=recent)
    assert '#Nope' not in tags and '#B2' not in tags and '#fyp' not in tags
    assert tags == ['#D4','#E5','#F6','#C3','#A1']  # Least recently used first.

def test_blocked_words_apply_to_new_tags_read_as_words():
    plan = h.Plan([])
    blocked = [re.compile(r'(?<!\w)trading(?!\w)',re.I)]
    assert h.pick(['#Trading','#DayTrading','#HouseMusic','#TRADINGPOST'], plan, 5, 5, blocked=blocked) == ['#HouseMusic','#TRADINGPOST']

def test_groups_can_apply_to_some_clip_types_only():
    groups = [group('Gig promo','pool',['#TicketsOnSale','#GigAlert'],low=1,kinds=['promo']), group('Big','pool',BIG[:10])]
    assert '#TicketsOnSale' not in h.Plan(groups,'performance').library_tags()
    promo = h.pick([], h.Plan(groups,'promo'), 5)
    assert any(t in ('#TicketsOnSale','#GigAlert') for t in promo) and len(promo) == 5

def test_validation_explains_problems():
    with pytest.raises(ValueError, match='five hashtags per post'):
        h.normalise([group('A','always',['#a1','#b2','#c3','#d4','#e5','#f6'])])
    with pytest.raises(ValueError, match='not a valid hashtag'):
        h.normalise([group('A','pool',['#ok','#not-ok'])])
    with pytest.raises(ValueError, match='minimum no higher'):
        h.normalise([group('A','pool',['#ok'],low=3,high=2)])
    with pytest.raises(ValueError, match='500'):
        h.normalise([group('A','pool',['#t%d' % i for i in range(501)])])
    clean = h.normalise([{'name':'  My   list ','mode':'pool','tags':['ok','#OK','#Two'],'min':1,'max':3,'kinds':['promo','karaoke']}])
    assert clean == [{'name':'My list','mode':'pool','tags':['#ok','#Two'],'min':1,'max':3,'kinds':['promo']}]

def test_brief_shows_groups_limits_and_best_tags():
    plan = h.Plan([group('Brand','always',['#TRADES']), group('Big','pool',BIG[:3],low=1,high=2), group('No','never',['#Nope'])])
    text = h.brief(plan, 5, 1, ['#Tag001'], ['#Tag002'])
    assert 'exactly 4 hashtags' in text and '#TRADES' in text and 'choose 1–2' in text
    assert 'up to 1 relevant tag of your own' in text and '#nope' in text and '#Tag002' in text
    assert 'empty hashtags array' in h.brief(h.Plan([group('B','always',BIG[:5])]), 5, 1)

def test_stats_track_uses_and_average_reach():
    with connect(True) as db:
        db.execute("INSERT INTO history(media_id,caption,timestamp,reach) VALUES('1','a\n\n#Good #Meh',100,1000)")
        db.execute("INSERT INTO history(media_id,caption,timestamp,reach) VALUES('2','b\n\n#Good',200,3000)")
        db.execute("INSERT INTO history(media_id,caption,timestamp,reach) VALUES('3','c\n\n#Meh',300,NULL)")
        table = h.stats(db)
    assert table['#good'] == {'tag':'#Good','uses':2,'last':200,'measured':2,'avg_reach':2000}
    assert table['#meh']['uses'] == 2 and table['#meh']['avg_reach'] == 1000
    assert h.best_tags(table, ['#Good','#Meh']) == ['#Good']

def test_old_favourites_become_a_library_group(tmp_path, monkeypatch):
    with connect(True) as db:
        stored = json.loads(db.execute('SELECT value FROM settings').fetchone()[0])
        del stored['hashtag_groups']
        stored['caption_hashtags'] = '#PerthDJs #DJDuo'
        db.execute('UPDATE settings SET value=?',(json.dumps(stored),))
    init()
    s = settings()
    assert s['hashtag_groups'] == [{'name':'Favourites','mode':'pool','tags':['#PerthDJs','#DJDuo'],'min':2,'max':5,'kinds':[]}]
    assert s['caption_hashtags'] == ''

def test_library_api_saves_parses_and_reports():
    with TestClient(app) as client:
        assert client.post('/api/login',json={'password':c.PASSWORD}).status_code==200
        hd = {'x-csrf-token':client.get('/api/state').json()['csrf']}
        groups = [group('Brand','always',['TRADES']), group('Big','pool',BIG,high=4), group('No','never',['#fyp'])]
        assert client.patch('/api/settings',headers=hd,json={'values':{'hashtag_groups':groups,'hashtag_new_max':2}}).status_code==200
        saved = client.get('/api/state').json()['settings']
        assert saved['hashtag_groups'][0]['tags']==['#TRADES'] and len(saved['hashtag_groups'][1]['tags'])==200 and saved['hashtag_new_max']==2
        bad = client.patch('/api/settings',headers=hd,json={'values':{'hashtag_groups':[group('A','always',BIG[:6])]}})
        assert bad.status_code==422 and 'five' in bad.json()['detail']
        assert client.patch('/api/settings',headers=hd,json={'values':{'hashtag_new_max':9}}).status_code==422
        parsed = client.post('/api/hashtags/parse',headers=hd,json={'text':'1. #One\n2. Two\n3. bad-tag'}).json()
        assert parsed['tags']==['#One','#Two'] and parsed['rejected_count']==1
        assert client.get('/api/hashtags/stats').json()=={'tags':{}}
