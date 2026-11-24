import contextlib
import json
import sqlite3
import time
from . import config as c

SCHEMA = '''
CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK(id=1), ig_id TEXT NOT NULL, username TEXT NOT NULL, expires_at REAL NOT NULL, synced_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS clips (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL,
 duration REAL DEFAULT 0, width INTEGER DEFAULT 0, height INTEGER DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'processing', error TEXT DEFAULT '', notes TEXT DEFAULT '',
 media_version INTEGER NOT NULL DEFAULT 0,
 cover_auto_ms INTEGER, cover_color_ms INTEGER, cover_manual_ms INTEGER,
 caption TEXT DEFAULT '', caption_source TEXT DEFAULT '', category TEXT DEFAULT '', disabled INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id TEXT NOT NULL REFERENCES clips(id),
 lane TEXT NOT NULL CHECK(lane IN ('main','trial')), status TEXT NOT NULL DEFAULT 'queued',
 scheduled_at REAL, caption TEXT NOT NULL DEFAULT '', approved INTEGER DEFAULT 1,
 pinned INTEGER DEFAULT 0, caption_locked INTEGER DEFAULT 0, reason TEXT DEFAULT '',
 container_id TEXT, media_id TEXT, permalink TEXT, attempts INTEGER DEFAULT 0,
 next_attempt REAL DEFAULT 0, publish_started REAL, published_at REAL, error TEXT DEFAULT '', post_now_at REAL, container_cover_ms INTEGER,
 UNIQUE(clip_id,lane)
);
CREATE INDEX IF NOT EXISTS queue_due ON jobs(status,scheduled_at);
CREATE TABLE IF NOT EXISTS history (
 media_id TEXT PRIMARY KEY, caption TEXT DEFAULT '', media_type TEXT DEFAULT '', permalink TEXT DEFAULT '',
 timestamp REAL, reach INTEGER, views INTEGER, likes INTEGER, comments INTEGER, saves INTEGER, shares INTEGER,
 metrics_at REAL, metrics_error TEXT DEFAULT '', origin TEXT DEFAULT 'imported', lane TEXT DEFAULT 'main'
);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL, job_id INTEGER);
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, seen_at REAL, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS notices (key TEXT PRIMARY KEY, sent_at REAL, message TEXT, status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, last_attempt REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS runtime (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS login_attempts (address TEXT PRIMARY KEY, attempts INTEGER DEFAULT 0, since REAL);
CREATE TABLE IF NOT EXISTS youtube_account (
 id INTEGER PRIMARY KEY CHECK(id=1), channel_id TEXT NOT NULL, title TEXT NOT NULL, error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS youtube_jobs (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('short','set')), source_key TEXT NOT NULL,
 source_path TEXT NOT NULL DEFAULT '', source_name TEXT NOT NULL, duration REAL NOT NULL,
 title TEXT NOT NULL, description TEXT NOT NULL, visibility TEXT NOT NULL, made_for_kids INTEGER NOT NULL, category TEXT NOT NULL,
 created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'draft', scheduled_at REAL, pinned INTEGER NOT NULL DEFAULT 0,
 manual INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', next_attempt REAL NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0, source_stamp TEXT, total_bytes INTEGER NOT NULL DEFAULT 0,
 uploaded_bytes INTEGER NOT NULL DEFAULT 0, video_id TEXT, uploaded_at REAL, actual_visibility TEXT,
 UNIQUE(kind,source_key)
);
CREATE INDEX IF NOT EXISTS youtube_due ON youtube_jobs(status,scheduled_at);
CREATE TABLE IF NOT EXISTS song_matches (a TEXT NOT NULL, b TEXT NOT NULL, score REAL NOT NULL DEFAULT 0, found_at REAL, PRIMARY KEY(a,b));
CREATE TABLE IF NOT EXISTS intake_batches (id TEXT PRIMARY KEY, name TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'mix' CHECK(mode IN ('mix','next')), created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS edits (
 id TEXT PRIMARY KEY, clip_id TEXT NOT NULL, created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'running',
 progress REAL NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '', result_clip TEXT
);
CREATE TABLE IF NOT EXISTS rewrite_batches (
 id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL, status TEXT NOT NULL,
 instructions TEXT NOT NULL DEFAULT '', total INTEGER NOT NULL, message TEXT NOT NULL DEFAULT '', retry_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS rewrite_items (
 batch_id TEXT NOT NULL, clip_id TEXT NOT NULL, position INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', PRIMARY KEY(batch_id,clip_id)
);
CREATE TABLE IF NOT EXISTS multicam_jobs (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, status TEXT NOT NULL,
 stage TEXT NOT NULL DEFAULT '', progress REAL NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
 config TEXT NOT NULL DEFAULT '{}', result TEXT NOT NULL DEFAULT '{}', log TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS collab_accounts (
 username TEXT PRIMARY KEY, ig_id TEXT NOT NULL, expires_at REAL NOT NULL, connected_at REAL NOT NULL, error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS collab_accepts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, media_id TEXT NOT NULL, username TEXT NOT NULL,
 due_at REAL NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, accepted_at REAL, error TEXT NOT NULL DEFAULT '',
 UNIQUE(job_id,username)
);
CREATE TABLE IF NOT EXISTS story_posts (
 job_id INTEGER PRIMARY KEY, clip_id TEXT NOT NULL, status TEXT NOT NULL, container_id TEXT, media_id TEXT,
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tiktok_account (
 id INTEGER PRIMARY KEY CHECK(id=1), open_id TEXT NOT NULL, display_name TEXT, expires_at REAL NOT NULL,
 refresh_expires_at REAL NOT NULL, error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS tiktok_jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id TEXT NOT NULL UNIQUE, caption TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
 scheduled_at REAL, pinned INTEGER NOT NULL DEFAULT 1, forced INTEGER NOT NULL DEFAULT 0, mirror_of INTEGER,
 publish_id TEXT, post_id TEXT, published_at REAL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS youtube_short_checks (clip_id TEXT PRIMARY KEY REFERENCES clips(id));
'''

@contextlib.contextmanager
def connect(write=False):
    db = sqlite3.connect(str(c.DB), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=30000')
    if write:
        db.execute('BEGIN IMMEDIATE')
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()

def init():
    c.validate_runtime()
    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)
    with connect(True) as db:
        clip_columns = {row['name'] for row in db.execute('PRAGMA table_info(clips)')}
        job_columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
        if 'media_version' not in clip_columns:
            db.execute('ALTER TABLE clips ADD COLUMN media_version INTEGER NOT NULL DEFAULT 0')
        for column in ('cover_auto_ms','cover_color_ms','cover_manual_ms'):
            if column not in clip_columns:
                db.execute(f'ALTER TABLE clips ADD COLUMN {column} INTEGER')
        for column,kind in (('audio_fp','BLOB'),('audio_state','INTEGER NOT NULL DEFAULT 0'),("song","TEXT NOT NULL DEFAULT ''"),
                            ("batch","TEXT NOT NULL DEFAULT ''"),('edit_of','TEXT'),("kind","TEXT NOT NULL DEFAULT ''"),
                            ("post_on","TEXT NOT NULL DEFAULT ''"),("post_at","TEXT NOT NULL DEFAULT ''"),
                            ("not_before","TEXT NOT NULL DEFAULT ''"),("not_after","TEXT NOT NULL DEFAULT ''")):
            if column not in clip_columns:
                db.execute(f'ALTER TABLE clips ADD COLUMN {column} {kind}')
        if 'batch' not in clip_columns:
            from .media import assign_legacy_batches
            assign_legacy_batches(db)
        if 'kind' not in {row['name'] for row in db.execute('PRAGMA table_info(intake_batches)')}:
            db.execute("ALTER TABLE intake_batches ADD COLUMN kind TEXT NOT NULL DEFAULT 'performance'")
        if 'collaborators' not in job_columns:
            db.execute("ALTER TABLE jobs ADD COLUMN collaborators TEXT NOT NULL DEFAULT ''")
        tt_columns = {row['name'] for row in db.execute('PRAGMA table_info(tiktok_jobs)')}
        for column,kind in (("privacy","TEXT NOT NULL DEFAULT ''"),('allow_comment','INTEGER NOT NULL DEFAULT 0'),
                            ('allow_duet','INTEGER NOT NULL DEFAULT 0'),('allow_stitch','INTEGER NOT NULL DEFAULT 0'),
                            ('brand_own','INTEGER NOT NULL DEFAULT 0'),('brand_paid','INTEGER NOT NULL DEFAULT 0'),
                            ('approved','INTEGER NOT NULL DEFAULT 0'),('approved_at','REAL')):
            if column not in tt_columns:
                db.execute(f'ALTER TABLE tiktok_jobs ADD COLUMN {column} {kind}')
        for column in ('held','forced'):
            if column not in job_columns:
                db.execute(f'ALTER TABLE jobs ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0')
        yt_columns = {row['name'] for row in db.execute('PRAGMA table_info(youtube_jobs)')}
        if 'mirror_of' not in yt_columns:
            db.execute('ALTER TABLE youtube_jobs ADD COLUMN mirror_of INTEGER')
        if 'forced' not in yt_columns:
            db.execute('ALTER TABLE youtube_jobs ADD COLUMN forced INTEGER NOT NULL DEFAULT 0')
        if 'post_now_at' not in job_columns:
            db.execute('ALTER TABLE jobs ADD COLUMN post_now_at REAL')
        if 'container_cover_ms' not in job_columns:
            db.execute('ALTER TABLE jobs ADD COLUMN container_cover_ms INTEGER')
        db.execute('INSERT OR IGNORE INTO settings VALUES(1,?)', (json.dumps(c.DEFAULTS),))
        stored = json.loads(db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])
        if 'hashtag_groups' not in stored:
            # Earlier versions kept one favourites list; it becomes the first library group.
            from .hashtags import legacy_groups
            stored['hashtag_groups'] = legacy_groups(stored.get('caption_hashtags',''))
            stored['caption_hashtags'] = ''
            db.execute('UPDATE settings SET value=? WHERE id=1', (json.dumps(stored),))

def settings(db=None):
    if db is not None:
        return {**c.DEFAULTS, **json.loads(db.execute('SELECT value FROM settings WHERE id=1').fetchone()[0])}
    with connect() as db:
        return settings(db)

def event(message, level='info', job_id=None, db=None):
    if db is not None:
        db.execute('INSERT INTO events(at,level,message,job_id) VALUES(?,?,?,?)', (time.time(), level, message, job_id))
    else:
        with connect(True) as conn:
            event(message, level, job_id, conn)

def secret(name, value=None, *, db=None):
    if db is None:
        with connect(value is not None) as conn:
            return secret(name,value,db=conn)
    from cryptography.fernet import Fernet
    cipher = Fernet(c.TOKEN_KEY.encode())
    if value is not None:
        db.execute('INSERT OR REPLACE INTO secrets VALUES(?,?)', (name, cipher.encrypt(value.encode()).decode()))
        return
    row = db.execute('SELECT value FROM secrets WHERE name=?', (name,)).fetchone()
    return cipher.decrypt(row[0].encode()).decode() if row else ''

def runtime(key, value=None):
    with connect(value is not None) as db:
        if value is not None:
            db.execute('INSERT OR REPLACE INTO runtime VALUES(?,?)', (key, str(value)))
            return
        row = db.execute('SELECT value FROM runtime WHERE key=?', (key,)).fetchone()
        return row[0] if row else None
