import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from . import config as c
from .db import connect, event

EXTENSIONS = {'.mp4', '.mov', '.m4v', '.mkv', '.webm'}

def run(args, timeout=1800):
    result = subprocess.run(args, capture_output=True, timeout=timeout)
    if result.returncode:
        raise ValueError('Video conversion failed. Check the source file is a playable video.')
    return result.stdout

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def probe(path):
    data = json.loads(run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(path)], 60))
    video = next((x for x in data['streams'] if x['codec_type']=='video'), None)
    if not video:
        raise ValueError('No video stream found.')
    duration = float(data['format'].get('duration', 0))
    if not 3 <= duration <= 900:
        raise ValueError('Reels must be between 3 seconds and 15 minutes. Trim this clip, then import it again.')
    return duration, video['width'], video['height']

def process(path, clip_id):
    target = c.DATA / 'media' / (clip_id + '.mp4')
    temp = target.with_suffix('.tmp.mp4')
    duration, width, height = probe(path)
    # Letterbox instead of cropping out people, decks, or equipment. Preserve source audio.
    run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(path),'-map','0:v:0','-map','0:a:0?',
         '-vf','scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1',
         '-r','30','-c:v','libx264','-preset','veryfast','-crf','22','-maxrate','12M','-bufsize','24M','-pix_fmt','yuv420p',
         '-c:a','aac','-b:a','128k','-ar','48000','-movflags','+faststart','-threads','2',str(temp)])
    if temp.stat().st_size > 1_000_000_000:
        temp.unlink()
        raise ValueError('Converted clip exceeds Instagram\'s 1 GB limit.')
    os.replace(temp, target)
    for i, fraction in enumerate((.15,.5,.85)):
        frame = c.DATA / 'frames' / (clip_id + '-' + str(i) + '.jpg')
        run(['ffmpeg','-hide_banner','-loglevel','error','-y','-ss',str(duration*fraction),'-i',str(target),'-frames:v','1','-vf','scale=360:640','-threads','1',str(frame)], 60)
    shutil.copyfile(c.DATA/'frames'/(clip_id+'-1.jpg'), c.DATA/'thumbs'/(clip_id+'.jpg'))
    with connect(True) as db:
        db.execute("UPDATE clips SET status='captioning',duration=?,width=?,height=?,error='' WHERE id=?", (duration,width,height,clip_id))
    event('Footage prepared: '+path.name)

def scan(now=None):
    now = now or time.time()
    imported = 0
    roots = (c.FOOTAGE, c.DATA/'uploads')
    for root in roots:
        for path in sorted(root.rglob('*')):
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in EXTENSIONS or any(x.startswith('.') for x in path.relative_to(root).parts):
                continue
            stat = path.stat()
            if stat.st_size > 5_000_000_000:
                continue
            # Observe size AND mtime twice across 30s so NAS copies never ingest halfway.
            with connect(True) as db:
                r = db.execute('SELECT * FROM files WHERE path=?',(str(path),)).fetchone()
                if not r or r['size']!=stat.st_size or r['mtime']!=stat.st_mtime:
                    db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,0)',(str(path),stat.st_size,stat.st_mtime,now))
                    continue
                if r['done'] or now-r['seen_at'] < 30:
                    continue
            clip_id = digest(path)
            after = path.stat()
            if (after.st_size,after.st_mtime)!=(stat.st_size,stat.st_mtime):
                continue
            with connect(True) as db:
                existing = db.execute('SELECT status FROM clips WHERE id=?',(clip_id,)).fetchone()
                db.execute('UPDATE files SET done=1 WHERE path=?',(str(path),))
                if existing:
                    continue
                db.execute('INSERT INTO clips(id,name,source,created_at) VALUES(?,?,?,?)',(clip_id,path.name,str(path),now))
            imported += 1
            event('New footage found: '+path.name)
    # Recover interrupted conversions using the recorded source path.
    with connect() as db:
        pending = db.execute("SELECT * FROM clips WHERE status='processing' LIMIT 4").fetchall()
    for clip in pending:
        try:
            path = Path(clip['source'])
            if digest(path) != clip['id']:
                raise ValueError('Source changed after import. Restore the original or import the updated clip.')
            process(path,clip['id'])
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else 'Could not process footage. Check the source path and worker logs.'
            with connect(True) as db:
                db.execute("UPDATE clips SET status='error',error=? WHERE id=?",(message,clip['id']))
            event(clip['name']+': '+message,'error')
    return imported
