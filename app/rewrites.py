"""Caption rewrites for many clips, run on the server until every clip is done.

The browser only starts a batch and watches it. The server groups clips of the same type,
captions up to five per request (far fewer provider calls than one each), retries anything
the model got wrong one clip at a time, and backs off and keeps going through temporary
provider or network failures. Problems retrying cannot fix (a rejected key, no credit) pause
the batch with the reason; Resume carries on. A batch interrupted by a restart resumes on start.
"""
import secrets
import threading
import time
from . import ai, media
from .db import connect, settings, event, runtime

ITEM_ATTEMPTS = 4          # Per clip: the model breaking caption rules this often marks it failed.
MAX_WAIT = 300             # Longest pause between retries of a temporary failure.
GIVE_UP_AFTER = 3*3600     # Pause, rather than retry forever, after hours of consecutive failures.
FATAL = ('API key', 'credit', 'blocked the request', 'not found', 'rejected the model', 'Connect an AI provider')
_lock = threading.Lock()


def pause(seconds):
    time.sleep(seconds)

_running = set()


def eligible(db, clip_id):
    clip = db.execute('SELECT * FROM clips WHERE id=?', (clip_id,)).fetchone()
    if not clip:
        return None, 'Clip no longer exists.'
    if clip['media_version'] == media.REBUILD_REQUESTED or clip['status'] in ('processing', 'error'):
        return None, 'Footage is still being prepared.'
    return clip, ''


def save(clip, caption, category, model):
    with connect(True) as db:
        if db.execute("""UPDATE clips SET caption=?,category=?,status='ready',error='',caption_source=?
            WHERE id=? AND media_version!=? AND status IN ('ready','captioning','needs_caption')""",
                      (caption, category, model, clip['id'], media.REBUILD_REQUESTED)).rowcount != 1:
            return False
        # Posts whose caption was edited by hand keep it.
        db.execute("""UPDATE jobs SET caption=?,caption_locked=0,container_id=NULL WHERE clip_id=?
            AND (caption_locked=0 OR caption=?) AND status IN ('queued','retry')""", (caption, clip['id'], clip['caption']))
        event('Caption regenerated for '+clip['name'], db=db)
    return True


def start(clip_ids, instructions=''):
    clip_ids = list(dict.fromkeys(clip_ids))
    if not clip_ids:
        raise ValueError('Select clips to rewrite.')
    with connect(True) as db:
        if db.execute("SELECT 1 FROM rewrite_batches WHERE status IN ('running','paused')").fetchone():
            raise RuntimeError('A caption batch is already in progress. Wait for it, or cancel it first.')
        batch = secrets.token_hex(8)
        db.execute("INSERT INTO rewrite_batches(id,created_at,updated_at,status,instructions,total) VALUES(?,?,?,'running',?,?)",
                   (batch, time.time(), time.time(), instructions.strip(), len(clip_ids)))
        db.executemany('INSERT INTO rewrite_items(batch_id,clip_id,position) VALUES(?,?,?)',
                       [(batch, cid, i) for i, cid in enumerate(clip_ids)])
        event(f'Caption rewrite started for {len(clip_ids)} clips.', db=db)
    launch(batch)
    return batch


def launch(batch):
    with _lock:
        if batch in _running:
            return
        _running.add(batch)
    threading.Thread(target=run, args=(batch,), daemon=True, name='rewrite-'+batch).start()


def resume_all():
    with connect() as db:
        batches = [r[0] for r in db.execute("SELECT id FROM rewrite_batches WHERE status='running'")]
    for batch in batches:
        launch(batch)


def set_batch(batch, **values):
    values['updated_at'] = time.time()
    with connect(True) as db:
        db.execute('UPDATE rewrite_batches SET '+','.join(k+'=?' for k in values)+' WHERE id=?', (*values.values(), batch))


def set_item(batch, clip_id, **values):
    with connect(True) as db:
        db.execute('UPDATE rewrite_items SET '+','.join(k+'=?' for k in values)+' WHERE batch_id=? AND clip_id=?',
                   (*values.values(), batch, clip_id))


def state(batch):
    with connect() as db:
        return db.execute('SELECT status FROM rewrite_batches WHERE id=?', (batch,)).fetchone()[0]


def pause_or_wait(batch, exc, failing_since):
    """Temporary failures wait and retry; ones retrying cannot fix pause the batch."""
    message = str(exc)
    if any(word in message for word in FATAL) or time.time()-failing_since > GIVE_UP_AFTER:
        set_batch(batch, status='paused', message='Paused: '+message+' Fix it, then press Resume.')
        event('Caption batch paused: '+message, 'warning')
        return False
    wait = min(MAX_WAIT, max(10, getattr(exc, 'retry_after', 60)))
    set_batch(batch, message=f'{message} Retrying automatically in {wait} seconds.', retry_at=time.time()+wait)
    for _ in range(int(wait)):
        if state(batch) != 'running':
            return False
        pause(1)
    set_batch(batch, message='', retry_at=0)
    return True


def run(batch):
    from .planner import plan
    try:
        failing_since = None
        while state(batch) == 'running':
            with connect() as db:
                row = db.execute('SELECT * FROM rewrite_batches WHERE id=?', (batch,)).fetchone()
                pending = db.execute("""SELECT clip_id,attempts FROM rewrite_items WHERE batch_id=? AND status='pending'
                    ORDER BY position""", (batch,)).fetchall()
                clips, kinds = [], {}
                for item in pending:
                    clip, why = eligible(db, item['clip_id'])
                    if clip is None:
                        db.execute("UPDATE rewrite_items SET status='skipped',error=? WHERE batch_id=? AND clip_id=?", (why, batch, item['clip_id']))
                        continue
                    clips.append((dict(clip), item['attempts']))
                db.commit()
            if not clips:
                break
            s = settings()
            if row['instructions']:
                s['caption_instructions'] += ('\n\nFor this rewrite only, these directions take priority over the saved style guidance above:\n'
                                              + row['instructions'])
            for clip, _ in clips:
                kinds.setdefault(ai.clip_kind(clip['id']), []).append(clip)
            # A first pass captions several clips per request; anything left goes one at a time.
            kind, group = next(iter(kinds.items()))
            fresh = [clip for clip, attempts in clips if attempts == 0 and clip in group][:ai.BATCH_SIZE]
            chunk = fresh if len(fresh) > 1 else [next(clip for clip, _ in clips if clip in group)]
            try:
                if len(chunk) > 1:
                    results = ai.generate_many(chunk, s)
                else:
                    results = {chunk[0]['id']: ai.generate(chunk[0], s)}
            except ai.CaptionError as exc:
                rule_break = 'broke a caption rule' in str(exc) or 'invalid caption fields' in str(exc) or 'overlong' in str(exc)
                if rule_break and len(chunk) == 1:
                    attempts = dict((c['id'], a) for c, a in clips)[chunk[0]['id']] + 1
                    set_item(batch, chunk[0]['id'], attempts=attempts, error=str(exc),
                             status='failed' if attempts >= ITEM_ATTEMPTS else 'pending')
                    continue
                failing_since = failing_since or time.time()
                if not pause_or_wait(batch, exc, failing_since):
                    return
                continue
            failing_since = None
            for clip in chunk:
                if clip['id'] in results:
                    caption, category = results[clip['id']]
                    if save(clip, caption, category, s['ai_model']):
                        set_item(batch, clip['id'], status='done', error='')
                    else:
                        set_item(batch, clip['id'], status='skipped', error='Footage changed while rewriting.')
                else:
                    # Not usable from the shared request: next time this clip goes on its own.
                    attempts = dict((c['id'], a) for c, a in clips)[clip['id']] + 1
                    set_item(batch, clip['id'], attempts=attempts,
                             status='failed' if attempts >= ITEM_ATTEMPTS else 'pending',
                             error='The model broke a caption rule; trying again on its own.')
            runtime('caption_retry_at', 0)
        if state(batch) == 'running':
            with connect() as db:
                counts = dict(db.execute('SELECT status,COUNT(*) FROM rewrite_items WHERE batch_id=? GROUP BY status', (batch,)).fetchall())
            done, failed, skipped = counts.get('done', 0), counts.get('failed', 0), counts.get('skipped', 0)
            note = f'{done} caption{"s" if done != 1 else ""} rewritten.'
            if failed:
                note += f' {failed} kept breaking caption rules and still need a rewrite.'
            if skipped:
                note += f' {skipped} skipped (footage not ready).'
            set_batch(batch, status='done', message=note)
            event('Caption batch finished: '+note)
            try:
                plan()
            except Exception:
                pass
    except Exception:
        set_batch(batch, status='paused', message='Paused after an unexpected error. Press Resume to carry on.')
    finally:
        with _lock:
            _running.discard(batch)


def summary(batch=None):
    with connect() as db:
        row = (db.execute('SELECT * FROM rewrite_batches WHERE id=?', (batch,)).fetchone() if batch else
               db.execute('SELECT * FROM rewrite_batches ORDER BY created_at DESC LIMIT 1').fetchone())
        if not row:
            return None
        counts = dict(db.execute('SELECT status,COUNT(*) FROM rewrite_items WHERE batch_id=? GROUP BY status', (row['id'],)).fetchall())
        items = [dict(r) for r in db.execute("""SELECT i.clip_id,i.status,i.error,c.name FROM rewrite_items i
            LEFT JOIN clips c ON c.id=i.clip_id WHERE i.batch_id=? ORDER BY i.position""", (row['id'],))]
    return {'id': row['id'], 'status': row['status'], 'message': row['message'], 'total': row['total'],
            'done': counts.get('done', 0), 'failed': counts.get('failed', 0), 'skipped': counts.get('skipped', 0),
            'pending': counts.get('pending', 0), 'retry_at': row['retry_at'], 'created_at': row['created_at'],
            'items': items}


def control(batch, action):
    with connect(True) as db:
        row = db.execute('SELECT status FROM rewrite_batches WHERE id=?', (batch,)).fetchone()
        if not row:
            raise LookupError('Batch not found.')
        if action == 'cancel' and row['status'] in ('running', 'paused'):
            db.execute("UPDATE rewrite_batches SET status='cancelled',message='Cancelled. Finished captions are kept.',updated_at=? WHERE id=?", (time.time(), batch))
            return
        if action == 'resume' and row['status'] in ('paused', 'done'):
            # Resume also retries clips that failed before.
            db.execute("UPDATE rewrite_items SET status='pending',attempts=0 WHERE batch_id=? AND status='failed'", (batch,))
            db.execute("UPDATE rewrite_batches SET status='running',message='',updated_at=? WHERE id=?", (time.time(), batch))
        else:
            raise ValueError('This batch cannot be '+('cancelled' if action == 'cancel' else 'resumed')+' now.')
    launch(batch)
