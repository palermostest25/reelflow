"""Recognise clips that carry the same song, so the planner can spread them out.

Audio fingerprints come from Chromaprint (the AcoustID fingerprinter, built into
FFmpeg). Two clips match when a stretch of their fingerprints lines up at some time
offset: the same recording, or a repeated loop or chorus of the same track.
An operator's song name always wins over audio matching.
"""
import subprocess
import time
import numpy as np
from . import config as c
from .db import connect, event

FP_SECONDS = 120          # Enough audio to cover a clip without scanning long sets.
CLOSE_BITS = 6            # Per-frame bit errors (of 32) that still count as the same audio.
MIN_RUN = 40              # ~5 s of aligned frames.
MIN_SHARE = .25           # ...and at least a quarter of the overlap.
MIN_FRAMES = 16          # ~2 s: shorter audio cannot be compared reliably.
PENDING, DONE, NO_AUDIO = 0, 1, 2

_available = None
def available():
    """Chromaprint ships with Debian's FFmpeg; a custom build may leave it out."""
    global _available
    if _available is None:
        try:
            muxers = subprocess.run(['ffmpeg','-hide_banner','-muxers'],capture_output=True,timeout=20).stdout
            _available = b'chromaprint' in muxers
        except (OSError, subprocess.TimeoutExpired):
            _available = False
    return _available

def fingerprint(path):
    """Raw Chromaprint values (~8 per second), or None when the clip has no usable audio."""
    try:
        result = subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-i',str(path),'-vn','-map','0:a:0?',
                                 '-t',str(FP_SECONDS),'-ac','1','-f','chromaprint','-fp_format','raw','-'],
                                capture_output=True,timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    data = result.stdout[:len(result.stdout)//4*4]
    if result.returncode or len(data) < 4*MIN_FRAMES:
        return None
    values = np.frombuffer(data,dtype='<u4')
    # Silence fingerprints as a constant; it is not a song.
    return values if len(np.unique(values)) > 8 else None

def similarity(a, b):
    """Longest aligned agreement between two fingerprints, as (frames, share of overlap)."""
    if a is None or b is None or not len(a) or not len(b):
        return 0, 0.0
    errors = np.bitwise_count(a[:,None] ^ b[None,:])
    rows, cols = np.nonzero(errors <= CLOSE_BITS)
    if not len(rows):
        return 0, 0.0
    shifts = cols - rows + len(a) - 1
    counts = np.bincount(shifts, minlength=len(a)+len(b)-1)
    best = int(counts.argmax())
    shift = best - (len(a) - 1)
    overlap = min(len(a), len(b)-shift) - max(0, -shift)
    return int(counts[best]), counts[best]/max(1, overlap)

def match_score(a, b):
    """Audio match as a percentage, or 0 when the clips are not the same song."""
    frames, share = similarity(a, b)
    # Short clips can't reach five seconds of agreement; ask for most of their length instead.
    needed = min(MIN_RUN, max(MIN_FRAMES, .6*min(len(a) if a is not None else 0, len(b) if b is not None else 0)))
    if frames < needed or share < MIN_SHARE:
        return 0
    # A quarter of aligned frames is already a clear repeat; scale so a repeated loop reads high.
    return round(min(100, 60 + 40*(share-MIN_SHARE)/(.75-MIN_SHARE)))

def same_song(a, b):
    return match_score(a, b) > 0

def load(blob):
    return np.frombuffer(blob,dtype='<u4') if blob else None

def analyse_pending(limit=8):
    """Fingerprint a few prepared clips per worker pass and record their matches."""
    if not available():
        return
    with connect() as db:
        pending = db.execute("""SELECT id,name FROM clips WHERE audio_state=? AND status IN ('ready','captioning','needs_caption')
            AND media_version>0 ORDER BY created_at LIMIT ?""",(PENDING,limit)).fetchall()
    for clip in pending:
        path = c.DATA/'media'/(clip['id']+'.mp4')
        if not path.is_file():
            continue
        fp = fingerprint(path)
        with connect() as db:
            known = db.execute('SELECT id,audio_fp FROM clips WHERE audio_state=? AND id!=?',(DONE,clip['id'])).fetchall()
        matches = []
        if fp is not None:
            for row in known:
                score = match_score(fp,load(row['audio_fp']))
                if score:
                    matches.append((row['id'],score))
        with connect(True) as db:
            db.execute('UPDATE clips SET audio_fp=?,audio_state=? WHERE id=?',
                       (fp.astype('<u4').tobytes() if fp is not None else None, DONE if fp is not None else NO_AUDIO, clip['id']))
            for other,score in matches:
                db.execute('INSERT OR REPLACE INTO song_matches(a,b,score,found_at) VALUES(?,?,?,?)',(*sorted((clip['id'],other)),score,time.time()))
            if matches:
                event(f'Same song heard in {clip["name"]} and {len(matches)} other clip'+('s' if len(matches)>1 else '')+'. Reelflow will space them apart.',db=db)

def groups(db):
    """Map every clip to a song key. Operator names win; audio links clips without conflicting names."""
    rows = db.execute('SELECT id,song FROM clips').fetchall()
    parent = {r['id']:r['id'] for r in rows}
    label = {}
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        la, lb = label.get(ra), label.get(rb)
        if la and lb and la != lb:
            return  # Two differently named songs never merge on audio alone.
        parent[rb] = ra
        if lb and not la:
            label[ra] = lb
    by_name = {}
    for r in rows:
        name = ' '.join((r['song'] or '').split()).casefold()
        if name:
            label[r['id']] = name
            if name in by_name:
                union(by_name[name], r['id'])
            else:
                by_name[name] = r['id']
    for a, b in db.execute('SELECT a,b FROM song_matches'):
        if a in parent and b in parent:
            union(a, b)
    return {cid: 'song:'+(label.get(find(cid)) or find(cid)) for cid in parent}
