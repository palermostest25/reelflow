"""Cheap visual quality and activity scores for camera recordings.

Only keyframes are decoded (about one per second on phones), at 96x96 grey,
so a long 4K recording is scanned in a fraction of its length. Each sample
gets:

    usable    0-1: penalises dark/covered lenses, blur, a moved or bumped
              camera and something standing in front of it
    activity  0-1: movement inside the frame (hands on the decks, a crowd),
              relative to that camera's own typical level

Scores are relative to each recording, so a dark club camera is not punished
for being a dark club camera. The native macOS app implements the same scores
in macos/Sources/MulticamCore/VisionAnalysis.swift.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SIZE = 96
VERSION = 3


def hardware_decoding():
    """Hardware decoding arguments: VideoToolbox on macOS, CUDA with an NVIDIA GPU, else none."""
    import hardware
    return hardware.decode_args()


def keyframes(path, hwaccel=None):
    """(times, frames) for every keyframe, frames as uint8 [n, SIZE, SIZE]."""
    import numpy as np
    hwaccel = hardware_decoding() if hwaccel is None else hwaccel
    cmd = ['ffmpeg', '-hide_banner', '-nostdin', '-loglevel', 'info', *hwaccel,
           '-skip_frame', 'nokey', '-i', str(path), '-map', '0:v:0', '-an', '-sn', '-dn',
           '-vf', f'scale={SIZE}:{SIZE}:flags=area,format=gray,showinfo',
           '-fps_mode', 'passthrough', '-f', 'rawvideo', '-']
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode and hwaccel:
        return keyframes(path, [])  # some files only decode in software
    if proc.returncode:
        raise RuntimeError('Cannot scan video: ' + proc.stderr.decode('utf-8', 'replace')[-1200:])
    times = [float(m) for m in re.findall(rb'pts_time:\s*([-0-9.e]+)', proc.stderr)]
    frames = np.frombuffer(proc.stdout, dtype=np.uint8)
    count = len(frames) // (SIZE * SIZE)
    frames = frames[:count * SIZE * SIZE].reshape(count, SIZE, SIZE)
    times = times[:count]
    if len(times) < count:
        frames = frames[:len(times)]
    return np.array(times, dtype=float), frames


def global_shift(a, b):
    """Translation between two frames by phase correlation, in pixels."""
    import numpy as np
    fa, fb = np.fft.rfft2(a), np.fft.rfft2(b)
    cross = fa * np.conj(fb)
    cross /= np.abs(cross) + 1e-9
    corr = np.fft.irfft2(cross, s=a.shape)
    y, x = np.unravel_index(int(np.argmax(corr)), corr.shape)
    if y > a.shape[0] // 2:
        y -= a.shape[0]
    if x > a.shape[1] // 2:
        x -= a.shape[1]
    return float(np.hypot(x, y))


def score_frames(times, frames):
    """Per-sample usable/activity scores (lists aligned with times)."""
    import numpy as np
    n = len(frames)
    if n == 0:
        return {'times': [], 'usable': [], 'activity': [], 'focus': [0.5, 0.5]}
    f = frames.astype(np.float32) / 255
    brightness = f.mean(axis=(1, 2))
    lap = np.abs(4 * f[:, 1:-1, 1:-1] - f[:, :-2, 1:-1] - f[:, 2:, 1:-1] - f[:, 1:-1, :-2] - f[:, 1:-1, 2:])
    sharpness = lap.mean(axis=(1, 2))
    reference = np.median(f, axis=0)
    deviation = np.abs(f - reference).mean(axis=(1, 2))
    motion = np.zeros(n)
    shift = np.zeros(n)
    window = np.outer(np.hanning(SIZE), np.hanning(SIZE)).astype(np.float32)
    for i in range(1, n):
        motion[i] = float(np.abs(f[i] - f[i - 1]).mean())
        shift[i] = global_shift(f[i] * window, f[i - 1] * window)
    if n > 1:
        motion[0], shift[0] = motion[1], shift[1]

    def ratio(x):
        return x / max(float(np.median(x)), 1e-6)

    b, s, d = ratio(brightness), ratio(sharpness), deviation / max(float(np.percentile(deviation, 75)), 1e-6)
    usable = np.ones(n)
    usable *= np.clip((b - 0.25) / 0.35, 0, 1)            # far darker than usual: lens covered, lights out
    usable *= np.clip((s - 0.35) / 0.35, 0, 1)            # far softer than usual: focus hunting, motion blur
    usable *= np.clip(1 - (d - 1.6) / 1.4, 0, 1)          # view unlike usual: blocked or knocked
    usable *= np.clip(1 - (shift - 3) / 6, 0, 1)          # whole picture moved: camera handled or panned
    activity = np.clip(ratio(motion + 1e-4) / 2.5, 0, 1)
    activity *= np.clip(1 - (shift - 2) / 6, 0, 1)        # handling the camera is not on-stage action
    # Where the action is: centroid of frame-to-frame change over the usable
    # samples, pulled 30% toward the centre. Used for automatic crop framing.
    focus = [0.5, 0.5]
    good = [i for i in range(1, n) if usable[i] > 0.5 and shift[i] < 3]
    if len(good) >= 5:
        change = np.mean([np.abs(f[i] - f[i - 1]) for i in good], axis=0)
        change = np.clip(change - np.percentile(change, 60), 0, None)
        total = float(change.sum())
        if total > 1e-6:
            ys, xs = np.mgrid[0:SIZE, 0:SIZE]
            cx = float((change * xs).sum() / total + 0.5) / SIZE
            cy = float((change * ys).sum() / total + 0.5) / SIZE
            focus = [round(0.7 * cx + 0.15, 3), round(0.7 * cy + 0.15, 3)]
    return {'times': [round(float(t), 3) for t in times],
            'usable': [round(float(x), 3) for x in usable],
            'activity': [round(float(x), 3) for x in activity],
            'focus': focus}


def analyse(path, cache_dir=None, hwaccel=None):
    """Scores for one recording, cached by file identity."""
    path = Path(path)
    stat = path.stat()
    key = hashlib.sha256(f'v{VERSION}:{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}'.encode()).hexdigest()[:24]
    cached = Path(cache_dir) / f'vision-{key}.json' if cache_dir else None
    if cached and cached.is_file():
        try:
            return json.loads(cached.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
    times, frames = keyframes(path, hwaccel)
    result = score_frames(times, frames)
    result['path'] = str(path)
    if cached:
        cached.parent.mkdir(parents=True, exist_ok=True)
        temporary = cached.with_name(cached.name + f'.{os.getpid()}.tmp')
        temporary.write_text(json.dumps(result), encoding='utf-8')
        temporary.replace(cached)
    return result


def main():
    if len(sys.argv) < 2:
        sys.exit('Usage: python3 vision.py VIDEO [VIDEO ...]')
    for item in sys.argv[1:]:
        result = analyse(item)
        usable = result['usable']
        bad = sum(1 for x in usable if x < 0.5)
        print(f'{Path(item).name}: {len(usable)} samples, {bad} poor ({bad / max(1, len(usable)):.0%})')


if __name__ == '__main__':
    main()
