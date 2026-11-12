import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from . import config as c
from .db import connect, event, settings

EXTENSIONS = {'.mp4', '.mov', '.m4v', '.mkv', '.webm'}
MEDIA_VERSION = 1
REBUILD_REQUESTED = -1
COVER_MODES = ('auto', 'color', 'instagram')
EXPORT_QUALITIES = {
    'standard': ('22', '12M', '24M'),
    'detail': ('18', '20M', '25M'),
}

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

def fit_frame(limit):
    # FFmpeg applies display rotation before filtering; account for non-square source pixels too.
    width = 'iw*if(gt(sar,0),sar,1)'
    factor = f'min(1,{limit}/max({width},ih))'
    return f"scale=w='max(2,trunc({width}*{factor}/2)*2)':h='max(2,trunc(ih*{factor}/2)*2)',setsar=1"

def frame_at(path, offset, filters, output):
    return run(['ffmpeg','-hide_banner','-loglevel','error','-ss',str(offset/1000),'-threads','1','-i',str(path),
                '-filter_threads','1','-frames:v','1','-vf',filters,'-threads','1',*output,'pipe:1'],15)

def cover_scores(rgb):
    size = 64
    if len(rgb) != size*size*3:
        raise ValueError('Could not read cover frame.')
    pixels = list(zip(rgb[::3],rgb[1::3],rgb[2::3]))
    light = [(54*r+183*g+19*b)/256 for r,g,b in pixels]
    centers = [y*size+x for y in range(8,56) for x in range(8,56)]
    count = len(centers)
    detail = sum(abs(light[i]-light[i+1])+abs(light[i]-light[i+size]) for i in centers)/(2*count)
    exposure = sum(min(1,light[i]/40,(255-light[i])/35) for i in centers)/count
    color = sum((max(pixels[i])-min(pixels[i]))*min(1,light[i]/45) for i in centers)/(255*count)
    # A few hard edges in a mostly black frame should not outrank a legible subject.
    balanced = exposure**2 * (0.55 + 0.45*min(1,detail/20))
    return balanced, balanced*(1+color*2)

def choose_covers(path, duration):
    midpoint = round(duration*500)
    candidates = []
    for fraction in (.12,.24,.38,.5,.62,.76,.88):
        offset = round(duration*fraction*1000)
        try:
            rgb = frame_at(path,offset,'scale=64:64',['-pix_fmt','rgb24','-f','rawvideo'])
            balanced, colorful = cover_scores(rgb)
        except (ValueError, subprocess.TimeoutExpired):
            continue
        candidates.append((offset,balanced,colorful))
    if not candidates:
        return midpoint,midpoint
    return tuple(max(candidates,key=lambda row:(row[i],-abs(row[0]-midpoint)))[0] for i in (1,2))

def cover_offset(clip, mode):
    if clip['cover_manual_ms'] is not None:
        return clip['cover_manual_ms']
    if mode == 'instagram':
        return None
    return clip['cover_color_ms'] if mode == 'color' else clip['cover_auto_ms']

def cover_jpeg(path, offset):
    return frame_at(path,offset,fit_frame(720),['-c:v','mjpeg','-q:v','3','-f','image2pipe'])

def prepare_cover(clip_id, duration):
    path = c.DATA/'media'/(clip_id+'.mp4')
    if not path.is_file():
        return
    auto, color = choose_covers(path,duration)
    with connect(True) as db:
        db.execute('UPDATE clips SET cover_auto_ms=?,cover_color_ms=? WHERE id=? AND media_version=? AND cover_auto_ms IS NULL',
                   (auto,color,clip_id,MEDIA_VERSION))

def backfill_covers():
    with connect() as db:
        clips = db.execute("""SELECT id,duration FROM clips WHERE cover_auto_ms IS NULL AND media_version=?
            AND status IN ('ready','captioning','needs_caption') AND disabled=0
            ORDER BY COALESCE((SELECT MIN(scheduled_at) FROM jobs WHERE clip_id=clips.id
                AND status IN ('queued','retry','processing')),1e20),created_at""",(MEDIA_VERSION,)).fetchall()
    prepared = 0
    for clip in clips:
        if (c.DATA/'media'/(clip['id']+'.mp4')).is_file():
            prepare_cover(clip['id'],clip['duration'])
            prepared += 1
            if prepared == 4:
                break

def process(path, clip_id):
    duration, width, height = probe(path)
    crf, bitrate, buffer = EXPORT_QUALITIES[settings()['export_quality']]
    with TemporaryDirectory(prefix='.encode-',dir=c.DATA) as workspace:
        temp = Path(workspace) / (clip_id + '.mp4')
        run(['ffmpeg','-hide_banner','-loglevel','error','-y','-i',str(path),'-map','0:v:0','-map','0:a:0?',
             '-vf',fit_frame(1920),'-r','30','-c:v','libx264','-preset','veryfast','-crf',crf,
             '-maxrate',bitrate,'-bufsize',buffer,'-pix_fmt','yuv420p','-c:a','aac','-b:a','128k','-ar','48000',
             '-movflags','+faststart','-threads','2',str(temp)])
        if temp.stat().st_size > 1_000_000_000:
            raise ValueError('Converted clip exceeds Instagram\'s 1 GB limit.')
        frames = []
        for i, fraction in enumerate((.15,.5,.85)):
            frame = temp.parent / f'{clip_id}-{i}.jpg'
            run(['ffmpeg','-hide_banner','-loglevel','error','-y','-ss',str(duration*fraction),'-i',str(temp),'-frames:v','1','-vf',fit_frame(640),'-threads','1',str(frame)], 60)
            frames.append(frame)
        thumb = temp.with_suffix('.jpg')
        shutil.copyfile(frames[1],thumb)
        for frame in frames:
            os.replace(frame,c.DATA/'frames'/frame.name)
        os.replace(thumb,c.DATA/'thumbs'/thumb.name)
        auto, color = choose_covers(temp,duration)
        os.replace(temp,c.DATA/'media'/temp.name)
    with connect(True) as db:
        db.execute("""UPDATE clips SET media_version=?,duration=?,width=?,height=?,error='',
            cover_auto_ms=?,cover_color_ms=?,cover_manual_ms=CASE WHEN cover_manual_ms < ? THEN cover_manual_ms END,
            status=CASE WHEN status!='processing' THEN status WHEN caption!='' THEN 'ready' ELSE 'captioning' END
            WHERE id=?""", (MEDIA_VERSION,duration,width,height,auto,color,duration*1000,clip_id))
        # A pending Instagram container still points at the old encode.
        db.execute("UPDATE jobs SET container_id=NULL WHERE clip_id=? AND status IN ('queued','retry','processing')",(clip_id,))
        event('Footage prepared in its original aspect ratio: '+path.name,db=db)

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
    # Rebuild legacy exports from the original, never from the already padded copy.
    with connect() as db:
        pending = db.execute("""SELECT c.* FROM clips c WHERE
            NOT EXISTS (SELECT 1 FROM jobs j WHERE j.clip_id=c.id AND j.status IN ('publishing','uncertain'))
            AND (c.status='processing' OR (c.status IN ('ready','captioning','needs_caption')
                AND (c.media_version=? OR (c.media_version<? AND c.disabled=0
                    AND (NOT EXISTS (SELECT 1 FROM jobs j WHERE j.clip_id=c.id)
                        OR EXISTS (SELECT 1 FROM jobs j WHERE j.clip_id=c.id AND j.status NOT IN ('published','rejected')))))))
            ORDER BY COALESCE((SELECT MIN(j.scheduled_at) FROM jobs j WHERE j.clip_id=c.id
                AND j.status IN ('queued','retry','processing')),1e20), c.created_at LIMIT 4""", (REBUILD_REQUESTED,MEDIA_VERSION)).fetchall()
    for clip in pending:
        try:
            path = Path(clip['source'])
            if not path.is_file():
                raise ValueError('Original footage is missing. Restore it to its import location, then retry preparation.')
            if digest(path) != clip['id']:
                raise ValueError('Source changed after import. Restore the original or import the updated clip.')
            process(path,clip['id'])
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else 'Could not process footage. Check the source path and worker logs.'
            with connect(True) as db:
                db.execute("UPDATE clips SET status='error',error=? WHERE id=?",(message,clip['id']))
            event(clip['name']+': '+message,'error')
    return imported
