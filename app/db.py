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
 caption TEXT DEFAULT '', caption_source TEXT DEFAULT '', category TEXT DEFAULT '', disabled INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, clip_id TEXT NOT NULL REFERENCES clips(id),
 lane TEXT NOT NULL CHECK(lane IN ('main','trial')), status TEXT NOT NULL DEFAULT 'queued',
 scheduled_at REAL, caption TEXT NOT NULL DEFAULT '', approved INTEGER DEFAULT 1,
 pinned INTEGER DEFAULT 0, caption_locked INTEGER DEFAULT 0, reason TEXT DEFAULT '',
 container_id TEXT, media_id TEXT, permalink TEXT, attempts INTEGER DEFAULT 0,
 next_attempt REAL DEFAULT 0, publish_started REAL, published_at REAL, error TEXT DEFAULT '',
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
        db.execute('INSERT OR IGNORE INTO settings VALUES(1,?)', (json.dumps(c.DEFAULTS),))

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

def secret(name, value=None):
    from cryptography.fernet import Fernet
    cipher = Fernet(c.TOKEN_KEY.encode())
    with connect(value is not None) as db:
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
