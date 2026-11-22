"""Quick funny edits: point at someone and keep pointing as they move.

The operator marks a person on one frame. OpenCV's CSRT tracker (opencv-contrib)
follows that box through the clip, a Haar face detector re-finds the face if the
tracker drifts, and the overlay is drawn on every frame before FFmpeg re-encodes
the video with the original audio. The result becomes a new clip in the library;
the original footage is never modified.
"""
import math
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from . import config as c
from .db import connect, event
from .media import video_encoder

EFFECTS = ('arrow', 'circle', 'spotlight', 'zoom')
RED, WHITE = (43, 56, 232), (255, 255, 255)  # BGR
TRACK_SIZE = 480  # Track on a small copy: fast, and steadier than full resolution.
FONT = Path(__file__).parent/'static'/'fonts'/'Barlow-Condensed.ttf'
RENDER_LOCK = threading.Lock()


class EditError(ValueError):
    pass


def new_tracker():
    for name in ('TrackerCSRT_create', 'TrackerKCF_create', 'TrackerMIL_create'):
        factory = getattr(cv2, name, None) or getattr(getattr(cv2, 'legacy', None), name, None)
        if factory:
            return factory()
    raise EditError('Head tracking is not available in this installation.')


_cascade = None
def faces(frame):
    """Face boxes (x, y, w, h) in frame pixels, largest first."""
    global _cascade
    if _cascade is None:
        _cascade = cv2.CascadeClassifier(cv2.data.haarcascades+'haarcascade_frontalface_default.xml')
    gray = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
    side = min(gray.shape[:2])
    found = _cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(max(16, side//25),)*2)
    return sorted((tuple(int(v) for v in f) for f in found), key=lambda f: -f[2]*f[3])


def read_frame(path, seconds):
    capture = cv2.VideoCapture(str(path))
    try:
        capture.set(cv2.CAP_PROP_POS_MSEC, max(0, seconds)*1000)
        ok, frame = capture.read()
    finally:
        capture.release()
    if not ok:
        raise EditError('Could not read that moment of the video.')
    return frame


def detect_faces(path, seconds):
    """Suggested targets at one moment, as fractions of the frame."""
    frame = read_frame(path, seconds)
    h, w = frame.shape[:2]
    scale = min(1, 960/max(w, h))
    small = cv2.resize(frame, (round(w*scale), round(h*scale))) if scale < 1 else frame
    return [{'x': x/small.shape[1], 'y': y/small.shape[0], 'w': fw/small.shape[1], 'h': fh/small.shape[0]}
            for x, y, fw, fh in faces(small)[:6]]


def validate(spec, duration):
    try:
        start = float(spec['start'])
        box = [float(spec['box'][k]) for k in ('x', 'y', 'w', 'h')]
        effects = [e for e in spec['effects'] if e in EFFECTS]
        length = float(spec.get('length', 5))
        label = ' '.join(str(spec.get('label', '')).split())[:40]
    except (KeyError, TypeError, ValueError):
        raise EditError('Mark a person on the video and choose at least one effect.') from None
    x, y, w, h = box
    if not (0 <= start < duration) or not effects:
        raise EditError('Mark a person on the video and choose at least one effect.')
    if not (0 <= x < 1 and 0 <= y < 1 and .01 <= w <= 1 and .01 <= h <= 1 and x+w <= 1.001 and y+h <= 1.001):
        raise EditError('Draw the box inside the video frame.')
    if not (length == 0 or 1 <= length <= 60):
        raise EditError('Choose how long the effect lasts.')
    return {'start': start, 'box': box, 'effects': effects, 'label': label,
            'end': duration if length == 0 else min(duration, start+length)}


class Smoother:
    """Steady the tracked box so the overlay glides rather than jitters."""
    def __init__(self, box, alpha=.35):
        self.state, self.alpha = np.array(box, float), alpha

    def __call__(self, box):
        self.state += self.alpha*(np.array(box, float)-self.state)
        return self.state.copy()


def track(path, spec, fps, size, progress=lambda f: None):
    """Smoothed box per frame index between start and end, in full-frame pixels."""
    w, h = size
    scale = min(1, TRACK_SIZE/max(w, h))
    first, last = round(spec['start']*fps), round(spec['end']*fps)
    x, y, bw, bh = spec['box']
    box = (x*w, y*h, bw*w, bh*h)
    capture = cv2.VideoCapture(str(path))
    boxes, lost = {}, 0
    try:
        capture.set(cv2.CAP_PROP_POS_FRAMES, first)
        index = int(capture.get(cv2.CAP_PROP_POS_FRAMES))
        tracker, smooth = None, Smoother(box)
        while index <= last:
            ok, frame = capture.read()
            if not ok:
                break
            small = cv2.resize(frame, (max(1, round(w*scale)), max(1, round(h*scale)))) if scale < 1 else frame
            if tracker is None:
                tracker = new_tracker()
                tracker.init(small, tuple(int(round(v*scale)) for v in box))
                found = True
            else:
                found, guess = tracker.update(small)
                if found:
                    box = tuple(v/scale for v in guess)
            if not found or (index-first) % max(1, round(fps)) == 0:
                # Re-anchor on the nearest detected face when tracking drifts or fails.
                cx, cy = box[0]+box[2]/2, box[1]+box[3]/2
                near = [f for f in faces(small) if abs((f[0]+f[2]/2)/scale-cx) < box[2]*1.2 and abs((f[1]+f[3]/2)/scale-cy) < box[3]*1.2]
                if near:
                    face = min(near, key=lambda f: abs((f[0]+f[2]/2)/scale-cx)+abs((f[1]+f[3]/2)/scale-cy))
                    keep = box[2]*box[3]
                    fx, fy, fw, fh = (v/scale for v in face)
                    # Keep the operator's framing size, centred on the face.
                    ratio = math.sqrt(keep/(fw*fh)) if fw*fh else 1
                    box = (fx+fw/2-fw*ratio/2, fy+fh/2-fh*ratio/2, fw*ratio, fh*ratio)
                    tracker = new_tracker()
                    tracker.init(small, tuple(int(round(v*scale)) for v in box))
                    found = True
            lost = 0 if found else lost+1
            boxes[index] = (smooth(box), lost)
            index += 1
            if index % 15 == 0:
                progress((index-first)/max(1, last-first))
    finally:
        capture.release()
    return boxes


def label_sprite(text, height):
    """Meme-style label: bold white text with a dark outline, as BGRA."""
    try:
        font = ImageFont.truetype(str(FONT), max(14, int(height)))
        try:
            font.set_variation_by_name('Bold')
        except Exception:
            pass
    except OSError:
        font = ImageFont.load_default()
    stroke = max(2, int(height/9))
    probe = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
    left, top, right, bottom = probe.textbbox((0, 0), text.upper(), font=font, stroke_width=stroke)
    image = Image.new('RGBA', (right-left+4, bottom-top+4), (0, 0, 0, 0))
    ImageDraw.Draw(image).text((2-left, 2-top), text.upper(), font=font, fill=(255, 255, 255, 255),
                               stroke_width=stroke, stroke_fill=(20, 20, 20, 255))
    rgba = np.array(image)
    return rgba[:, :, [2, 1, 0, 3]]


def blend(frame, sprite, x, y, opacity=1.0):
    h, w = sprite.shape[:2]
    fh, fw = frame.shape[:2]
    x, y = int(x), int(y)
    x0, y0, x1, y1 = max(0, x), max(0, y), min(fw, x+w), min(fh, y+h)
    if x0 >= x1 or y0 >= y1:
        return
    part = sprite[y0-y:y1-y, x0-x:x1-x]
    alpha = part[:, :, 3:4].astype(np.float32)/255*opacity
    region = frame[y0:y1, x0:x1].astype(np.float32)
    frame[y0:y1, x0:x1] = (region*(1-alpha)+part[:, :, :3]*alpha).astype(np.uint8)


def zoom_view(box, size, amount):
    """Crop rectangle (x, y, w, h) that punches in on the box by `amount` (1 = none)."""
    w, h = size
    cw, ch = w/amount, h/amount
    cx, cy = box[0]+box[2]/2, box[1]+box[3]/2
    return (min(max(0, cx-cw/2), w-cw), min(max(0, cy-ch/2), h-ch), cw, ch)


def draw(frame, box, spec, t, strength, sprite, side):
    """Apply the chosen effects to one frame. `strength` fades effects in and out (0–1)."""
    h, w = frame.shape[:2]
    base = min(w, h)
    x, y, bw, bh = box
    effects = spec['effects']
    if 'zoom' in effects:
        amount = 1+.8*strength
        vx, vy, vw, vh = zoom_view(box, (w, h), amount)
        crop = frame[int(vy):int(vy+vh), int(vx):int(vx+vw)]
        frame[:] = cv2.resize(crop, (w, h), interpolation=cv2.INTER_LINEAR)
        factor = w/vw
        x, y, bw, bh = (x-vx)*factor, (y-vy)*factor, bw*factor, bh*factor
    cx, cy = x+bw/2, y+bh/2
    if 'spotlight' in effects and strength > 0:
        mask = np.zeros((h//4, w//4), np.float32)
        cv2.ellipse(mask, (int(cx/4), int(cy/4)), (int(bw*.75/4)+1, int(bh*.85/4)+1), 0, 0, 360, 1, -1)
        mask = cv2.GaussianBlur(mask, (0, 0), max(1, base/160))
        mask = cv2.resize(mask, (w, h))[:, :, None]
        dim = 1-.65*strength
        frame[:] = (frame*(dim+(1-dim)*mask)).astype(np.uint8)
    thick = max(3, int(base*.011))
    if 'circle' in effects and strength > .05:
        axes = (int(bw*.62*(.6+.4*strength)), int(bh*.68*(.6+.4*strength)))
        cv2.ellipse(frame, (int(cx), int(cy)), axes, 0, 0, 360, WHITE, thick+4, cv2.LINE_AA)
        cv2.ellipse(frame, (int(cx), int(cy)), axes, 0, 0, 360, RED, thick, cv2.LINE_AA)
    tail = None
    if 'arrow' in effects and strength > .05:
        bob = math.sin(t*2*math.pi*1.6)*base*.012  # A small bounce reads as "look here".
        reach = base*.22*strength
        dx, dy = side
        tip = (cx-dx*(bw*.72+base*.015+bob), cy-dy*(bh*.72+base*.015+bob))
        tail = (tip[0]-dx*reach, tip[1]-dy*reach)
        p1, p2 = tuple(int(v) for v in tail), tuple(int(v) for v in tip)
        cv2.arrowedLine(frame, p1, p2, WHITE, thick*2+4, cv2.LINE_AA, tipLength=.32)
        cv2.arrowedLine(frame, p1, p2, RED, thick*2, cv2.LINE_AA, tipLength=.3)
    if sprite is not None and strength > .05:
        sh, sw = sprite.shape[:2]
        if tail:
            lx = tail[0]-sw/2-side[0]*sw*.35
            ly = tail[1]-sh/2-side[1]*sh*.7
        else:
            lx, ly = cx-sw/2, y-sh-base*.02
        blend(frame, sprite, min(max(0, lx), w-sw), min(max(0, ly), h-sh), min(1, strength*1.4))


def arrow_side(box, size):
    """Point from wherever there is the most room, as a unit direction toward the target."""
    w, h = size
    cx, cy = (box[0]+box[2]/2)/w, (box[1]+box[3]/2)/h
    dx = 1 if cx > .5 else -1
    dy = 1 if cy > .45 else -1
    return (dx*.6, dy*.8)


def render(source, output, spec, progress=lambda f: None):
    capture = cv2.VideoCapture(str(source))
    fps = capture.get(cv2.CAP_PROP_FPS) or 30
    w, h = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    capture.release()
    if not w or not h:
        raise EditError('Could not read the prepared video.')
    boxes = track(source, spec, fps, (w, h), lambda f: progress(f*.4))
    if not boxes:
        raise EditError('Could not follow that person. Try marking them on a clearer frame.')
    first, last = min(boxes), max(boxes)
    fade = max(1, round(fps*.25))
    sprite = label_sprite(spec['label'], min(w, h)*.075) if spec['label'] else None
    side = arrow_side(boxes[first][0], (w, h))
    encoder = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
                                '-s', f'{w}x{h}', '-r', str(fps), '-i', '-', '-i', str(source), '-map', '0:v:0', '-map', '1:a:0?',
                                *video_encoder('detail'), '-pix_fmt', 'yuv420p',
                                '-c:a', 'copy', '-shortest', '-movflags', '+faststart', '-threads', '2', str(output)],
                               stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    capture = cv2.VideoCapture(str(source))
    total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) or 1
    index = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            if first <= index <= last and index in boxes:
                box, lost = boxes[index]
                # Ease in at the start and out at the end; fade if the person is lost for a second.
                strength = min(1, (index-first+1)/fade, (last-index+1)/fade, max(0, 1-(lost-fps)/fade) if lost > fps else 1)
                strength = strength*strength*(3-2*strength)
                draw(frame, box, spec, index/fps, strength, sprite, side)
            encoder.stdin.write(frame.tobytes())
            index += 1
            if index % 15 == 0:
                progress(.4+.6*index/total)
        encoder.stdin.close()
        if encoder.wait(timeout=600):
            raise EditError('Could not encode the edited video.')
    except BrokenPipeError:
        encoder.wait(timeout=60)
        raise EditError('Could not encode the edited video.') from None
    finally:
        capture.release()
        if encoder.poll() is None:
            encoder.kill()


def set_progress(edit_id, **values):
    with connect(True) as db:
        db.execute('UPDATE edits SET '+','.join(k+'=?' for k in values)+' WHERE id=?', (*values.values(), edit_id))


def run(edit_id, clip, spec, replace):
    from .media import digest
    folder = c.DATA/'edits'
    folder.mkdir(exist_ok=True)
    temp = folder/('.'+edit_id+'.mp4')
    try:
        last = [0.0]
        def progress(fraction):
            if fraction-last[0] >= .03:
                last[0] = fraction
                set_progress(edit_id, progress=round(min(.99, fraction), 3))
        render(c.DATA/'media'/(clip['id']+'.mp4'), temp, spec, progress)
        clip_id = digest(temp)
        final = folder/(clip_id+'.mp4')
        temp.replace(final)
        stem = Path(clip['name']).stem
        effects = ' + '.join(spec['effects'])
        with connect(True) as db:
            if not db.execute('SELECT 1 FROM clips WHERE id=?', (clip_id,)).fetchone():
                # The original's caption carries over, so the edit is ready once prepared.
                db.execute("""INSERT INTO clips(id,name,source,created_at,notes,batch,song,edit_of,status,caption,category,caption_source)
                    VALUES(?,?,?,?,?,?,?,?,'processing',?,?,?)""",
                    (clip_id, f'{stem} · edit.mp4', str(final), time.time(), clip['notes'], clip['batch'], clip['song'], clip['id'],
                     clip['caption'], clip['category'], 'Copied from original' if clip['caption'] else ''))
                # Same audio as the original, so the planner keeps them apart.
                db.execute('INSERT OR REPLACE INTO song_matches(a,b,score,found_at) VALUES(?,?,100,?)', (*sorted((clip_id, clip['id'])), time.time()))
            if replace:
                db.execute('UPDATE clips SET disabled=1 WHERE id=?', (clip['id'],))
            db.execute("UPDATE edits SET status='done',progress=1,result_clip=?,message=? WHERE id=?",
                       (clip_id, 'Edit saved. It joins the queue once it is prepared.', edit_id))
            event(f'Funny edit saved for {clip["name"]} ({effects}).'+(' The original is excluded from future posting.' if replace else ''), db=db)
    except EditError as exc:
        set_progress(edit_id, status='error', message=str(exc))
    except Exception:
        set_progress(edit_id, status='error', message='The edit could not be rendered. Try a shorter effect or a clearer frame.')
    finally:
        temp.unlink(missing_ok=True)
        RENDER_LOCK.release()


def start(clip, raw_spec, replace=True):
    if not shutil.which('ffmpeg'):
        raise EditError('FFmpeg is required for edits.')
    spec = validate(raw_spec, clip['duration'])
    if not RENDER_LOCK.acquire(blocking=False):
        raise EditError('Another edit is rendering. Wait for it to finish.')
    edit_id = secrets.token_hex(8)
    try:
        with connect(True) as db:
            db.execute("INSERT INTO edits(id,clip_id,created_at,status) VALUES(?,?,?,'running')", (edit_id, clip['id'], time.time()))
        threading.Thread(target=run, args=(edit_id, dict(clip), spec, replace), daemon=True).start()
    except Exception:
        RENDER_LOCK.release()
        raise
    return edit_id


def recover():
    # A restart interrupts any render in progress; say so rather than leaving it spinning.
    with connect(True) as db:
        db.execute("UPDATE edits SET status='error',message='Reelflow restarted while this edit was rendering. Start it again.' WHERE status='running'")
