"""The hashtag library: grouped tags the caption model picks from, plus rules.

A group is a dict:
    name   short label shown in Settings
    mode   'always' (added to every post), 'pool' (the model picks from it) or 'never' (blocked)
    tags   list of '#Tag' strings
    min    pool only: at least this many from the group per post (filled automatically if the model skips it)
    max    pool only: at most this many from the group per post
    kinds  clip types the group applies to; empty means every type

Instagram allows five hashtags per post, so 'always' tags count toward that total.
"""
import json
import re

MODES = ('always', 'pool', 'never')
KINDS = ('performance', 'talking', 'bts', 'promo')
MAX_PER_POST = 5
MAX_GROUPS = 20
MAX_GROUP_TAGS = 500
MAX_LIBRARY_TAGS = 1500
HASHTAG = re.compile(r'(?<!\w)#\w+')
# Broad engagement tags say nothing about the clip and read as spam.
GENERIC = {t.casefold() for t in (
    'fyp','foryou','foryoupage','viral','viralvideo','viralreels','explore','explorepage','instagood',
    'instagram','insta','instadaily','reels','reel','reelsinstagram','reelitfeelit','reelsvideo','trending','trend',
    'trendingreels','followme','follow','follow4follow','followforfollow','like4like','likeforlike','l4l','f4f',
    'photooftheday','picoftheday','instamood','love','tbt','video','videos','music','dj','party','fun','happy')}


def clean(tag):
    """'#Tag' for anything that is a valid hashtag, with or without the '#'; '' otherwise."""
    tag = '#'+str(tag).strip().strip('"\'“”‘’`').lstrip('#').strip()
    return tag if re.fullmatch(r'#\w{2,50}', tag) and not tag[1:].isdigit() else ''


def parse(text):
    """Pull hashtags out of pasted text in any common shape.

    Handles '#a #b', 'a, b', one per line, numbered or bulleted lists ('1. #a', '- b', '• c'),
    and AI answers with a sentence before the list. Returns (tags, rejected) with duplicates removed.
    """
    tags, rejected, seen = [], [], set()
    for line in str(text).splitlines():
        line = re.sub(r'^\s*(?:\d+[.)]|[-*•·–])\s+', '', line)
        if not line.strip():
            continue
        found = HASHTAG.findall(line)
        # A line with hashtags in it: take just the hashtags, ignoring any prose around them.
        parts = found if found else re.split(r'[\s,;|]+', line)
        if not found and len(parts) > 3 and line.rstrip().endswith(('.', ':', '?', '!')):
            continue  # A sentence such as "Here are 200 hashtags:".
        for part in parts:
            if not part.strip(',;|'):
                continue
            tag = clean(part.strip(',;|.'))
            if not tag:
                rejected.append(part[:52])
            elif tag.casefold() not in seen:
                seen.add(tag.casefold())
                tags.append(tag)
    return tags, rejected


def normalise(raw):
    """Validate groups from the settings form. Raises ValueError with a readable message."""
    if not isinstance(raw, list) or len(raw) > MAX_GROUPS:
        raise ValueError(f'Use up to {MAX_GROUPS} hashtag groups.')
    groups, total, always = [], 0, []
    for index, group in enumerate(raw, 1):
        if not isinstance(group, dict) or set(group) - {'name', 'mode', 'tags', 'min', 'max', 'kinds'}:
            raise ValueError('Hashtag group %d is not valid.' % index)
        name = ' '.join(str(group.get('name') or '').split())[:40] or 'Group %d' % index
        mode = group.get('mode', 'pool')
        if mode not in MODES:
            raise ValueError(f'Choose how “{name}” is used.')
        tags, seen = [], set()
        for tag in group.get('tags') or []:
            clean_tag = clean(tag)
            if not clean_tag:
                raise ValueError(f'“{str(tag)[:52]}” in “{name}” is not a valid hashtag. Use letters, numbers and underscores.')
            if clean_tag.casefold() not in seen:
                seen.add(clean_tag.casefold())
                tags.append(clean_tag)
        if len(tags) > MAX_GROUP_TAGS:
            raise ValueError(f'“{name}” has {len(tags)} tags. Keep each group to {MAX_GROUP_TAGS}; split it into two groups.')
        total += len(tags)
        kinds = [k for k in group.get('kinds') or [] if k in KINDS]
        low, high = group.get('min', 0), group.get('max', MAX_PER_POST)
        if type(low) is not int or type(high) is not int or not 0 <= low <= high <= MAX_PER_POST:
            raise ValueError(f'For “{name}”, choose 0–{MAX_PER_POST} per post, with the minimum no higher than the maximum.')
        if mode == 'always':
            always += tags
        groups.append({'name': name, 'mode': mode, 'tags': tags, 'min': low if mode == 'pool' else 0,
                       'max': high if mode == 'pool' else MAX_PER_POST, 'kinds': kinds})
    if total > MAX_LIBRARY_TAGS:
        raise ValueError(f'The hashtag library holds up to {MAX_LIBRARY_TAGS} tags.')
    if len({t.casefold() for t in always}) > MAX_PER_POST:
        raise ValueError('Instagram allows five hashtags per post, so keep “Always include” to five tags.')
    return groups


def legacy_groups(favourites):
    """Settings from before the library: one favourites list, about half of each post."""
    tags = [t for t in (clean(x) for x in favourites.split()) if t]
    return [{'name': 'Favourites', 'mode': 'pool', 'tags': tags, 'min': 2, 'max': MAX_PER_POST, 'kinds': []}] if tags else []


class Plan:
    """The library as it applies to one clip type."""
    def __init__(self, groups, kind='performance'):
        active = [g for g in groups if not g.get('kinds') or kind in g['kinds']]
        self.never = {t.casefold() for g in active if g['mode'] == 'never' for t in g['tags']}
        seen = set(self.never)
        self.always = []
        for g in active:
            if g['mode'] == 'always':
                for t in g['tags']:
                    if t.casefold() not in seen:
                        seen.add(t.casefold())
                        self.always.append(t)
        self.pools = []
        for g in active:
            if g['mode'] == 'pool':
                tags = [t for t in g['tags'] if t.casefold() not in seen]
                seen.update(t.casefold() for t in tags)
                if tags:
                    self.pools.append({'name': g['name'], 'tags': tags, 'min': min(g['min'], len(tags)), 'max': g['max']})

    def library_tags(self):
        return [t for p in self.pools for t in p['tags']]


def usage(captions):
    """Recent use per tag, newest weighted most, so a rested tag comes back first."""
    weights = {}
    for age, caption in enumerate(captions):
        for tag in HASHTAG.findall(caption or ''):
            weights[tag.casefold()] = weights.get(tag.casefold(), 0) + 1/(1+age/10)
    return weights


def pick(suggested, plan, count, new_max=0, recent=None, blocked=()):
    """Exactly `count` tags (when the library allows): always tags, then the model's picks within each
    group's limits and up to `new_max` of its own finds, then least-recently-used library tags."""
    if count <= 0:
        return []
    recent = recent or {}
    count = min(count, MAX_PER_POST)
    always = plan.always[:count]
    seen = {t.casefold() for t in always} | plan.never
    owner, canonical = {}, {}
    for i, pool in enumerate(plan.pools):
        for t in pool['tags']:
            owner.setdefault(t.casefold(), i)
            canonical.setdefault(t.casefold(), t)
    used = [0]*len(plan.pools)
    picks, new = [], 0

    def allowed_new(tag):
        body = tag[1:]
        spaced = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', body)
        return body.casefold() not in GENERIC and not any(p.search(body) or p.search(spaced) for p in blocked)

    for raw in suggested:
        tag = clean(raw)
        key = tag.casefold()
        if not tag or key in seen:
            continue
        if key in owner:
            group = owner[key]
            if used[group] >= plan.pools[group]['max']:
                continue
            used[group] += 1
            picks.append((canonical[key], group))
        else:
            if new >= new_max or not allowed_new(tag):
                continue
            new += 1
            picks.append((tag, None))
        seen.add(key)

    def rested(group):
        return sorted((t for t in plan.pools[group]['tags'] if t.casefold() not in seen),
                      key=lambda t: (recent.get(t.casefold(), 0), plan.pools[group]['tags'].index(t)))

    # Every group gets its minimum, even if the model passed it over.
    for group, pool in enumerate(plan.pools):
        for t in rested(group)[:max(0, pool['min']-used[group])]:
            picks.append((t, group))
            used[group] += 1
            seen.add(t.casefold())
    slots = count - len(always)
    # Too many: drop the model's own finds first, then tags from groups above their minimum.
    while len(picks) > slots:
        drop = next((i for i in range(len(picks)-1, -1, -1) if picks[i][1] is None), None)
        if drop is None:
            drop = next((i for i in range(len(picks)-1, -1, -1) if used[picks[i][1]] > plan.pools[picks[i][1]]['min']), len(picks)-1)
        if picks[drop][1] is not None:
            used[picks[drop][1]] -= 1
        picks.pop(drop)
    # Too few: rotate in the least-used library tags, spreading across groups.
    while len(picks) < slots:
        options = [(recent.get(t.casefold(), 0), used[g], g, t) for g, pool in enumerate(plan.pools)
                   if used[g] < pool['max'] for t in rested(g)[:1]]
        if not options:
            break
        _, _, group, tag = min(options)
        picks.append((tag, group))
        used[group] += 1
        seen.add(tag.casefold())
    return always + [t for t, _ in picks]


def brief(plan, count, new_max, recent_tags=(), best=()):
    """Prompt text describing the library to the caption model."""
    slots = max(0, min(count, MAX_PER_POST) - len(plan.always))
    if not slots:
        return ('\nReturn an empty hashtags array. Reelflow adds the account\'s fixed hashtags '
                + ' '.join(plan.always) + ' itself.')
    text = (f'\nAlso return hashtags: exactly {slots} hashtags chosen for this specific clip, in the hashtags array. '
            'Make them relevant: the music style, the scene, the kind of event or audience, and places named in the supplied facts. '
            'Never use generic engagement tags such as #fyp, #viral, #explore, #reels or #instagood. '
            'Do not invent places, events or track names in hashtags. Vary the set from recent posts.')
    if plan.always:
        text += ' Reelflow always adds ' + ' '.join(plan.always) + ' itself; do not repeat them.'
    for pool in plan.pools:
        text += (f' From the operator\'s “{pool["name"]}” list choose {pool["min"]}–{min(pool["max"], slots)}: '
                 + json.dumps(pool['tags']) + '.')
    if plan.pools:
        text += (f' You may add up to {new_max} relevant tag{"s" if new_max != 1 else ""} of your own.' if new_max
                 else ' Use only tags from these lists.')
    if plan.never:
        text += ' Never use: ' + json.dumps(sorted(plan.never)) + '.'
    if best:
        text += ' Tags on this account\'s best-reaching posts, best first: ' + json.dumps(list(best)[:15]) + '.'
    if recent_tags:
        text += ' Recently used hashtags, most used first: ' + json.dumps(list(recent_tags)[:20]) + '.'
    return text


def stats(db):
    """Per tag: how often it was posted, when last, and the average reach of those posts."""
    out = {}
    rows = db.execute("SELECT caption,timestamp,reach FROM history WHERE caption!=''").fetchall()
    for caption, timestamp, reach in rows:
        for tag in {t for t in HASHTAG.findall(caption)}:
            entry = out.setdefault(tag.casefold(), {'tag': tag, 'uses': 0, 'last': 0, 'reach': 0, 'measured': 0})
            entry['uses'] += 1
            entry['last'] = max(entry['last'], timestamp or 0)
            if reach:
                entry['reach'] += reach
                entry['measured'] += 1
    for entry in out.values():
        entry['avg_reach'] = round(entry['reach']/entry['measured']) if entry['measured'] else None
        del entry['reach']
    return out


def best_tags(table, library, limit=15):
    """Library tags that have reached people, best average first (at least two measured posts)."""
    known = {t.casefold() for t in library}
    ranked = [e for k, e in table.items() if k in known and e['measured'] >= 2 and e['avg_reach']]
    return [e['tag'] for e in sorted(ranked, key=lambda e: -e['avg_reach'])[:limit]]

