"""Musical structure of a DJ set: tempo, beat grid, bars and sections.

    python3 music.py set.wav            # prints a JSON summary

The analysis is tuned for four-on-the-floor dance music (house, techno, trance,
drum & bass). It finds beats with a dynamic-programming tracker, places bar
lines from phrase changes, and labels sections from kick-drum presence and
loudness: breakdowns (the kick stops), builds (the bars before it returns) and
drops (the bars after it returns). Results are suggestions for cutting; music
without a steady beat returns a low confidence and the editor falls back to
time-based cuts. NumPy is required; SciPy is not.

The same algorithm is implemented in the native macOS app
(macos/Sources/MulticamCore/MusicAnalysis.swift). Keep the two in step.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

SR = 8000
HOP = 80            # 10 ms analysis frames at 8 kHz
FRAME = 512         # 64 ms window
FPS = SR / HOP      # 100 onset frames per second
MIN_BPM, MAX_BPM = 70.0, 190.0
TIGHTNESS = 120.0   # how strongly the beat tracker keeps a steady tempo


def decode(path, sample_rate=SR):
    """Mono float32 samples of any audio or video file, via FFmpeg."""
    import numpy as np
    result = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(path), '-map', '0:a:0',
                             '-vn', '-ac', '1', '-ar', str(sample_rate), '-f', 'f32le', '-'],
                            capture_output=True)
    if result.returncode:
        raise RuntimeError('Cannot decode audio: ' + result.stderr.decode('utf-8', 'replace')[-1500:])
    return np.frombuffer(result.stdout, dtype='<f4')


def band_flux(samples):
    """Log-magnitude spectral flux in three bands: kick, body and top."""
    import numpy as np
    samples = np.asarray(samples, dtype=np.float32)
    count = max(0, (len(samples) - FRAME) // HOP + 1)
    if count < 10:
        return np.zeros((3, 0), dtype=np.float32), np.zeros(0, dtype=np.float32), np.zeros((3, 0), dtype=np.float32)
    window = np.hanning(FRAME).astype(np.float32)
    freqs = np.fft.rfftfreq(FRAME, 1 / SR)
    bands = [(freqs >= 30) & (freqs < 150), (freqs >= 150) & (freqs < 2000), freqs >= 2000]
    energy = np.zeros((3, count), dtype=np.float32)
    rms = np.zeros(count, dtype=np.float32)
    previous = None
    flux = np.zeros((3, count), dtype=np.float32)
    step = 4096
    for first in range(0, count, step):
        last = min(count, first + step)
        index = (np.arange(first, last) * HOP)[:, None] + np.arange(FRAME)[None, :]
        frames = samples[index] * window
        rms[first:last] = np.sqrt(np.mean(frames * frames, axis=1))
        spectrum = np.log1p(100 * np.abs(np.fft.rfft(frames, axis=1)))
        if previous is None:
            previous = spectrum[:1]
        diff = np.maximum(0, np.diff(np.vstack([previous, spectrum]), axis=0))
        previous = spectrum[-1:]
        for b, mask in enumerate(bands):
            flux[b, first:last] = diff[:, mask].mean(axis=1)
            energy[b, first:last] = spectrum[:, mask].mean(axis=1)
    return flux, rms, energy


def smooth(values, size):
    import numpy as np
    size = max(1, int(size))
    if size <= 1 or len(values) == 0:
        return np.asarray(values, dtype=float)
    kernel = np.ones(size) / size
    padded = np.pad(np.asarray(values, dtype=float), (size // 2, size - 1 - size // 2), mode='edge')
    return np.convolve(padded, kernel, mode='valid')


def normalise(onset):
    """Remove slow loudness changes so quiet passages still have beats."""
    import numpy as np
    local = smooth(onset, FPS * 3) + 1e-6
    value = onset / local
    return np.clip(value - smooth(value, FPS // 2), 0, None)


def tempo_curve(onset):
    """Beat period (in onset frames) per frame, from windowed autocorrelation.

    A log-normal prior centred on 128 BPM resolves octave errors; values are
    median-filtered so a mix transition does not flip the tempo for one window.
    """
    import numpy as np
    window, hop = int(FPS * 12), int(FPS * 3)
    lags = np.arange(int(FPS * 60 / MAX_BPM), int(FPS * 60 / MIN_BPM) + 1)
    bpm = 60 * FPS / lags
    prior = np.exp(-0.5 * (np.log2(bpm / 128) / 0.7) ** 2)
    centres, periods, strengths = [], [], []
    for start in range(0, max(1, len(onset) - window + 1), hop):
        x = onset[start:start + window]
        x = x - x.mean()
        if len(x) < window // 2 or not np.any(x):
            continue
        spectrum = np.fft.rfft(x, 2 * len(x))
        acf = np.fft.irfft(spectrum * np.conj(spectrum))[:len(x)]
        if acf[0] <= 0:
            continue
        acf = acf / acf[0]
        # Combine each lag with its double so a half-tempo hat pattern does not win.
        comb = acf[lags] + 0.5 * acf[np.minimum(2 * lags, len(acf) - 1)]
        score = comb * prior
        k = int(np.argmax(score))
        lag = float(lags[k])
        if 0 < k < len(lags) - 1:  # parabolic sub-frame refinement
            a, b, c = score[k - 1], score[k], score[k + 1]
            denominator = a - 2 * b + c
            if denominator < 0:
                lag += 0.5 * (a - c) / denominator
        centres.append(start + window / 2)
        periods.append(lag)
        strengths.append(float(acf[lags[k]]))
    if not periods:
        return None, 0.0
    periods = np.array(periods)
    # Median over five windows (about 15 seconds).
    padded = np.pad(periods, 2, mode='edge')
    periods = np.array([np.median(padded[i:i + 5]) for i in range(len(periods))])
    curve = np.interp(np.arange(len(onset)), centres, periods)
    return curve, float(np.median(strengths))


def track_beats(onset, period):
    """Ellis-style dynamic programming: strong onsets at a near-steady period."""
    import numpy as np
    n = len(onset)
    score = onset.astype(float).copy()
    back = np.full(n, -1, dtype=np.int64)
    for t in range(n):
        p = period[t]
        lo, hi = int(t - 2 * p), int(t - p / 2)
        if hi <= 0:
            continue
        lo = max(0, lo)
        previous = np.arange(lo, hi)
        candidates = score[lo:hi] - TIGHTNESS * np.log((t - previous) / p) ** 2
        k = int(np.argmax(candidates))
        if candidates[k] > 0:
            score[t] = onset[t] + candidates[k]
            back[t] = previous[k]
    # Start the backtrace from the best score in the final beat period.
    tail = max(1, int(period[-1]))
    t = n - tail + int(np.argmax(score[-tail:]))
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    beats.reverse()
    return np.array(beats, dtype=float)


def bar_phase(beats, onset_frames, flux, energy):
    """Which beat of four starts each bar, chosen per 32-beat window.

    Dance tracks change sound on the bar line (crashes, bass changes, new
    loops), so spectral novelty between consecutive beats peaks on downbeats.
    """
    import numpy as np
    if len(beats) < 8:
        return np.zeros(len(beats), dtype=int)
    index = np.clip(beats.astype(int), 0, onset_frames - 1)
    # Novelty: change of the band energy profile across each beat boundary.
    profile = np.stack([smooth(e, FPS // 8) for e in energy])
    novelty = np.zeros(len(beats))
    for i in range(1, len(beats) - 1):
        a, b, c = index[i - 1], index[i], index[i + 1]
        before = profile[:, a:b].mean(axis=1)
        after = profile[:, b:c].mean(axis=1)
        novelty[i] = float(np.sum(np.abs(after - before))) + 0.5 * float(flux[0, b])
    phases = np.zeros(len(beats), dtype=int)
    window = 32
    choice = []
    for start in range(0, len(beats), window // 2):
        segment = slice(max(0, start - window // 2), min(len(beats), start + window))
        ids = np.arange(len(beats))[segment]
        sums = [novelty[ids[(ids % 4) == k]].mean() if np.any((ids % 4) == k) else 0 for k in range(4)]
        choice.append((start, int(np.argmax(sums))))
    # Keep a phase until another is clearly preferred for two windows in a row.
    current = choice[0][1]
    pending = None
    for (start, phase) in choice:
        if phase != current:
            if pending == phase:
                current = phase
                pending = None
            else:
                pending = phase
        else:
            pending = None
        phases[start:start + window // 2] = current
    return phases


def sections_from_bars(bass):
    """Label every bar as breakdown, build, drop or normal.

    In dance music a breakdown removes the kick and bass, so the low band
    falls well below its level in the surrounding two minutes. The bars after
    the bass returns are the drop; the last bars before it are the build.
    """
    import numpy as np
    count = len(bass)
    labels = ['normal'] * count
    if count < 8:
        return labels
    context = np.array([np.percentile(bass[max(0, i - 48):i + 48], 75) for i in range(count)])
    quiet = (context - bass) > 1.5
    # Ignore single bars either way (fills, one loud hit).
    for i in range(1, count - 1):
        if quiet[i - 1] == quiet[i + 1] != quiet[i]:
            quiet[i] = quiet[i - 1]
    i = 0
    while i < count:
        if not quiet[i]:
            i += 1
            continue
        j = i
        while j < count and quiet[j]:
            j += 1
        if j - i >= 4:
            for k in range(i, j):
                labels[k] = 'breakdown'
            build = min(4, max(1, (j - i) // 3))
            for k in range(j - build, j):
                labels[k] = 'build'
            if j < count and j - i >= 6:
                for k in range(j, min(count, j + 16)):
                    labels[k] = 'drop'
        i = j
    return labels


def refine(beats):
    """Fit a straight line through each beat's 17 neighbours: sub-frame times."""
    import numpy as np
    n = len(beats)
    out = beats.astype(float).copy()
    for i in range(n):
        lo, hi = max(0, i - 8), min(n, i + 9)
        k = np.arange(lo, hi)
        slope, intercept = np.polyfit(k, beats[lo:hi], 1)
        predicted = slope * i + intercept
        # Keep genuine tempo changes: only adjust small jitter.
        if abs(predicted - beats[i]) < 0.15 * slope:
            out[i] = predicted
    return out


def analyse(samples, duration=None):
    """Return the beat grid and sections for mono 8 kHz samples."""
    import numpy as np
    samples = np.asarray(samples, dtype=np.float32)
    duration = float(duration if duration is not None else len(samples) / SR)
    empty = {'version': 1, 'bpm': None, 'confidence': 0.0, 'beats': [], 'downbeats': [],
             'sections': [], 'drops': [], 'bar_energy': [], 'duration': duration}
    if duration < 8:
        return empty
    flux, rms, energy = band_flux(samples)
    onset = normalise(flux[0] * 1.5 + flux[1] + 0.5 * flux[2])
    period, strength = tempo_curve(onset)
    if period is None:
        return empty
    raw = track_beats(onset, period)
    if len(raw) < 16:
        return empty
    beats = refine(raw)
    intervals = np.diff(beats)
    bpm = float(60 * FPS / np.median(intervals))
    # Confidence: regular intervals and onsets that line up with the beats.
    regularity = float(np.mean(np.abs(intervals / np.median(intervals) - 1) < 0.08))
    on_beat = float(np.mean(onset[raw.astype(int)]))
    off_beat = float(np.mean(onset[np.clip((raw[:-1] + np.diff(raw) / 2).astype(int), 0, len(onset) - 1)]))
    contrast = on_beat / (off_beat + 1e-6)
    confidence = max(0.0, min(1.0, regularity * min(1.0, (contrast - 1) / 1.5) * min(1.0, strength * 3)))
    phases = bar_phase(beats, len(onset), flux, energy)
    is_down = (np.arange(len(beats)) % 4) == phases
    beat_times = beats / FPS
    downbeats = beat_times[is_down]
    # Per-bar kick presence and loudness.
    bar_starts = list(downbeats)
    if not bar_starts or bar_starts[0] > 0.5:
        bar_starts = [0.0] + bar_starts
    bar_edges = bar_starts + [duration]
    bass, loudness, busy = [], [], []
    db = 20 * np.log10(np.maximum(rms, 1e-6))
    frames = energy.shape[1]
    for a, b in zip(bar_edges, bar_edges[1:]):
        lo = min(frames - 1, int(a * FPS))
        hi = min(frames, max(lo + 1, int(b * FPS)))
        bass.append(float(np.mean(energy[0, lo:hi])))
        loudness.append(float(np.mean(db[lo:hi])))
        busy.append(float(np.mean(onset[lo:hi])))
    bass, loudness, busy = np.array(bass), np.array(loudness), np.array(busy)
    labels = sections_from_bars(bass)
    sections = []
    for a, b, label in zip(bar_edges, bar_edges[1:], labels):
        if sections and sections[-1]['kind'] == label:
            sections[-1]['end'] = round(b, 3)
        else:
            sections.append({'start': round(a, 3), 'end': round(b, 3), 'kind': label})
    drops = [s['start'] for s in sections if s['kind'] == 'drop'
             and any(p['kind'] in ('build', 'breakdown') and abs(p['end'] - s['start']) < 0.01 for p in sections)]
    # 0-1 intensity per bar: loudness, bass and rhythmic activity together.
    def unit(x):
        lo, hi = np.percentile(x, [5, 95])
        return np.clip((x - lo) / max(1e-6, hi - lo), 0, 1)
    level = (unit(loudness) + unit(bass) + unit(busy)) / 3
    return {'version': 1, 'bpm': round(bpm, 2), 'confidence': round(confidence, 3),
            'beats': [round(float(t), 3) for t in beat_times],
            'downbeats': [round(float(t), 3) for t in downbeats],
            'sections': sections, 'drops': [round(float(t), 3) for t in drops],
            'bar_starts': [round(float(t), 3) for t in bar_starts],
            'bar_energy': [round(float(x), 3) for x in level], 'duration': duration}


def snap(time, grid, tolerance):
    """Nearest grid time within tolerance, else the original time."""
    import bisect
    if not grid:
        return time
    i = bisect.bisect_left(grid, time)
    best = min((g for g in grid[max(0, i - 1):i + 1]), key=lambda g: abs(g - time))
    return best if abs(best - time) <= tolerance else time


def main():
    if len(sys.argv) != 2:
        sys.exit('Usage: python3 music.py AUDIO_FILE')
    result = analyse(decode(Path(sys.argv[1])))
    summary = {k: result[k] for k in ('bpm', 'confidence', 'duration', 'drops')}
    summary['beats'] = len(result['beats'])
    summary['bars'] = len(result['downbeats'])
    summary['sections'] = result['sections']
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
