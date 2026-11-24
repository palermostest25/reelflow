"""Collaborator accounts: invite them on grid Reels and, for accounts signed in here, accept on a schedule.

Settings (all editable in Settings → Collaborators):
    collab_enabled       invite collaborators at all
    collab_accounts      [{'username', 'auto_accept'}], in the operator's order
    collab_per_post      how many to invite on each Reel (Instagram allows up to 3); with more accounts
                         listed, each Reel invites the next ones in turn so every account gets posts
    collab_order         'listed' or 'random': the order the signed-in accounts accept in
    collab_first_hours   delay from publishing to the first acceptance
    collab_gap_hours     delay between one acceptance and the next

Accepting uses the Instagram API's collaboration invites for the invited account, so that account must
be signed in to Reelflow (the same Meta app, with the account added as a tester while the app is in
development mode). Accounts that are not signed in are still invited and accept in the Instagram app.
"""
import json
import random
import re
import time
import httpx
from . import config as c, instagram as ig
from .db import connect, secret, event, settings

USERNAME = re.compile(r'[A-Za-z0-9._]{1,30}')
MAX_PER_POST = 3
INVITES = '{ig_id}/collaboration_invites'   # Instagram API with Instagram Login


def clean_username(name):
    name = str(name or '').strip().lstrip('@').lower()
    if not USERNAME.fullmatch(name) or '..' in name or name.endswith('.'):
        raise ValueError(f'“{str(name)[:40]}” is not an Instagram username.')
    return name


def validate(s):
    """Normalise the collaborator settings; raises ValueError with a readable message."""
    accounts, seen = [], set()
    if not isinstance(s['collab_accounts'], list) or len(s['collab_accounts']) > 20:
        raise ValueError('List up to 20 collaborator accounts.')
    for item in s['collab_accounts']:
        if not isinstance(item, dict) or set(item) - {'username', 'auto_accept'}:
            raise ValueError('Each collaborator needs a username.')
        name = clean_username(item.get('username'))
        if name in seen:
            continue
        seen.add(name)
        accounts.append({'username': name, 'auto_accept': bool(item.get('auto_accept', True))})
    s['collab_accounts'] = accounts
    if not 1 <= s['collab_per_post'] <= MAX_PER_POST:
        raise ValueError('Instagram allows up to 3 collaborators per post.')
    if s['collab_order'] not in ('listed', 'random'):
        raise ValueError('Choose listed or random order.')
    for key in ('collab_first_hours', 'collab_gap_hours'):
        if not 0 <= s[key] <= 24*60:
            raise ValueError('Delays can be 0 to 1,440 hours (60 days).')
    with connect() as db:
        main = db.execute('SELECT username FROM account WHERE id=1').fetchone()
    if main and main['username'].lower() in seen:
        raise ValueError('The main account cannot be its own collaborator.')
    return s


def pick(s, db):
    """Usernames to invite on the next grid Reel, rotating through the list when it is longer than allowed."""
    if not s['collab_enabled'] or not s['collab_accounts']:
        return []
    names = [a['username'] for a in s['collab_accounts']]
    count = min(s['collab_per_post'], len(names))
    turn = int((db.execute("SELECT value FROM runtime WHERE key='collab_turn'").fetchone() or [0])[0])
    chosen = [names[(turn + i) % len(names)] for i in range(count)]
    db.execute("INSERT OR REPLACE INTO runtime VALUES('collab_turn',?)", (str((turn + count) % len(names)),))
    return chosen


def connected():
    with connect() as db:
        return {r['username']: dict(r) for r in db.execute('SELECT * FROM collab_accounts')}


def connect_account(code):
    """Sign in a collaborator account (never replaces the main account)."""
    login = ig.exchange(code)
    with connect() as db:
        main = db.execute('SELECT ig_id FROM account WHERE id=1').fetchone()
    if main and main['ig_id'] == login['ig_id']:
        raise ig.MetaError('That is the main account. Sign in to Instagram as the collaborator account, then try again.')
    username = login['username'].lower()
    secret('collab_token:'+login['ig_id'], login['token'])
    with connect(True) as db:
        db.execute("""INSERT INTO collab_accounts(username,ig_id,expires_at,connected_at,error) VALUES(?,?,?,?,'')
            ON CONFLICT(username) DO UPDATE SET ig_id=excluded.ig_id,expires_at=excluded.expires_at,error=''""",
                   (username, login['ig_id'], login['expires_at'], time.time()))
        event(f'Collaborator @{username} signed in. Reelflow can accept its invites.', db=db)
    return username


def sign_out(username):
    with connect(True) as db:
        row = db.execute('SELECT ig_id FROM collab_accounts WHERE username=?', (username,)).fetchone()
        if row:
            db.execute('DELETE FROM secrets WHERE name=?', ('collab_token:'+row['ig_id'],))
            db.execute('DELETE FROM collab_accounts WHERE username=?', (username,))


def schedule_accepts(job_id, media_id, invited, published_at):
    """After a Reel with collaborators publishes, plan when each signed-in account accepts."""
    s = settings()
    wanted = {a['username']: a['auto_accept'] for a in s['collab_accounts']}
    signed = connected()
    order = [u for u in invited if wanted.get(u) and u in signed]
    manual = [u for u in invited if u not in order]
    if s['collab_order'] == 'random':
        random.shuffle(order)
    with connect(True) as db:
        for step, username in enumerate(order):
            due = published_at + s['collab_first_hours']*3600 + step*s['collab_gap_hours']*3600
            db.execute("""INSERT OR IGNORE INTO collab_accepts(job_id,media_id,username,due_at,status)
                VALUES(?,?,?,?,'pending')""", (job_id, media_id, username, due))
        for username in manual:
            db.execute("""INSERT OR IGNORE INTO collab_accepts(job_id,media_id,username,due_at,status,error)
                VALUES(?,?,?,?,'manual',?)""", (job_id, media_id, username, published_at,
                                                 'Accept in the Instagram app' if username in wanted else 'Invited'))


def find_invite(token, ig_id, media_id):
    data = ig.request('GET', INVITES.format(ig_id=ig_id), token=token, params={'fields': 'id,media_id,media_owner_username', 'limit': 50})
    for invite in data.get('data', []):
        if str(invite.get('media_id') or invite.get('id')) == str(media_id):
            return invite
    return None


def accept_due(now=None, limit=5):
    """Accept invites that have come due. Missing invites are retried hourly for a day."""
    now = now or time.time()
    with connect() as db:
        due = db.execute("""SELECT a.*,k.ig_id FROM collab_accepts a LEFT JOIN collab_accounts k ON k.username=a.username
            WHERE a.status='pending' AND a.due_at<=? ORDER BY a.due_at LIMIT ?""", (now, limit)).fetchall()
    for row in due:
        update = {}
        try:
            if not row['ig_id']:
                raise ig.MetaError(f'@{row["username"]} is no longer signed in. Sign in again in Settings → Collaborators, or accept in the app.')
            token = secret('collab_token:'+row['ig_id'])
            if not find_invite(token, row['ig_id'], row['media_id']):
                if row['attempts'] >= 24:
                    raise ig.MetaError('The invite never appeared for this account. Accept it in the Instagram app.')
                update = {'attempts': row['attempts']+1, 'due_at': now+3600, 'error': 'Waiting for the invite to reach the account.'}
            else:
                ig.request('POST', INVITES.format(ig_id=row['ig_id']), token=token,
                           data={'media_id': row['media_id'], 'accept': 'true'})
                update = {'status': 'accepted', 'accepted_at': now, 'error': ''}
                event(f'@{row["username"]} accepted the collaboration.', job_id=row['job_id'])
        except ig.MetaError as exc:
            retry = exc.transient and row['attempts'] < 8
            update = {'attempts': row['attempts']+1, 'error': str(exc)}
            if retry:
                update['due_at'] = now + min(6*3600, 600*2**row['attempts'])
            else:
                update['status'] = 'failed'
                event(f'@{row["username"]} could not accept automatically: {exc}', 'warning', job_id=row['job_id'])
        except httpx.HTTPError:
            update = {'attempts': row['attempts']+1, 'due_at': now+1800, 'error': 'Instagram could not be reached. Retrying.'}
        with connect(True) as db:
            db.execute('UPDATE collab_accepts SET '+','.join(k+'=?' for k in update)+' WHERE id=?', (*update.values(), row['id']))


def refresh_tokens():
    """Long-lived tokens last 60 days; refresh each signed-in collaborator well before then."""
    with connect() as db:
        rows = db.execute('SELECT * FROM collab_accounts WHERE expires_at-?<10*86400', (time.time(),)).fetchall()
    for row in rows:
        try:
            with httpx.Client(timeout=45) as client:
                r = client.get('https://graph.instagram.com/refresh_access_token',
                               params={'grant_type': 'ig_refresh_token', 'access_token': secret('collab_token:'+row['ig_id'])})
            if r.status_code != 200:
                raise ValueError
            data = r.json()
            secret('collab_token:'+row['ig_id'], data['access_token'])
            with connect(True) as db:
                db.execute("UPDATE collab_accounts SET expires_at=?,error='' WHERE username=?",
                           (time.time()+data.get('expires_in', 5184000), row['username']))
        except Exception:
            with connect(True) as db:
                db.execute("UPDATE collab_accounts SET error='Sign-in expiring. Sign in again.' WHERE username=?", (row['username'],))


def for_job(job_id):
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT username,status,due_at,accepted_at,error FROM collab_accepts WHERE job_id=? ORDER BY due_at', (job_id,))]
