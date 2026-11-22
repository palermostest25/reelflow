"""Smart multicamera shot planning for DJ sets.

The planner cuts on the music and chooses angles from what each camera shows:

* Cuts land on beats, preferring bar lines and phrase starts (every 4 bars).
  A drop always gets a cut on its first downbeat.
* Hold lengths follow the section: long in breakdowns, shorter in normal
  passages, quick in drops, and accelerating through a build.
* Every candidate angle is scored for the coming shot: visual quality
  (not dark, blurred, blocked or being handled), on-screen activity, the main
  camera's screen-time target, and variety (no A-B-A-B ping-pong, rested
  angles come back). A seeded random choice among the best keeps edits lively
  but repeatable.
* No flash frames: every shot is at least a minimum length, and a camera that
  runs out of footage hands over to whichever angle still has coverage, so the
  picture only goes black when no camera recorded that moment.

Many candidate plans are generated per seed and the best one by share, pace
and picture quality is kept. The macOS app's Planner.swift is a port of this
file; keep them in step.
"""
from __future__ import annotations

import bisect
import math
import random
from dataclasses import dataclass


@dataclass
class Shot:
    start: int
    end: int
    camera: int | None
    reason: str


@dataclass
class Rhythm:
    main_hold: tuple = (20, 60)
    cutaway_hold: tuple = (4, 12)
    drop_hold: tuple = (3, 6)
    breakdown_hold: tuple = (15, 30)
    cut_scale: float = 1.0
    main_share: float = 0.7
    min_shot: float = 1.2           # seconds, before cut_scale
    candidates: int = 48


# Short-form (Reels, Shorts, TikTok) pacing: quick, but still on the music.
SHORT_FORM = Rhythm(main_hold=(3, 7), cutaway_hold=(2, 5), drop_hold=(1.5, 4),
                    breakdown_hold=(4, 8), main_share=0.5, min_shot=0.9)


class Scores:
    """Prefix sums of per-step visual scores for fast window averages."""
    STEP = 0.5  # seconds

    def __init__(self, values, steps):
        self.prefix = [0.0]
        self.values = []
        for i in range(steps):
            v = values[i] if i < len(values) and values[i] is not None else 1.0
            self.values.append(v)
            self.prefix.append(self.prefix[-1] + v)
        self.steps = steps

    def bad_from(self, a, b, threshold=0.35, run=3):
        """Start (seconds) of the first sustained poor stretch in [a, b), or None."""
        i, j = max(0, int(a / self.STEP)), min(self.steps, int(math.ceil(b / self.STEP)))
        count = 0
        for k in range(i, j):
            count = count + 1 if self.values[k] < threshold else 0
            if count >= run:
                return (k - run + 1) * self.STEP
        return None

    def mean(self, a, b):
        i = max(0, min(self.steps, int(a / self.STEP)))
        j = max(i + 1, min(self.steps, int(math.ceil(b / self.STEP))))
        if j > self.steps:
            return 1.0
        return (self.prefix[j] - self.prefix[i]) / (j - i)


def section_at(sections, t):
    for s in sections:
        if s['start'] <= t < s['end']:
            return s['kind']
    return 'normal'


def cut_grid(music, total, fps):
    """Candidate cut frames and their musical weight."""
    weights = {}
    if music and music.get('confidence', 0) >= 0.35 and len(music.get('beats', [])) > 16:
        downbeats = music.get('downbeats', [])
        down = set(round(t * fps) for t in downbeats)
        phrase = set(round(t * fps) for t in downbeats[::4])
        drops = set(round(t * fps) for t in music.get('drops', []))
        for t in music['beats']:
            f = round(t * fps)
            if 0 < f < total:
                weights[f] = 0.35
        for f in down:
            if 0 < f < total:
                weights[f] = 1.0
        for f in phrase:
            if 0 < f < total:
                weights[f] = 1.6
        for f in drops:
            if 0 < f < total:
                weights[f] = 3.0
    else:
        step = max(1, round(fps / 2))
        for f in range(step, total, step):
            weights[f] = 1.0
    frames = sorted(weights)
    return frames, [weights[f] for f in frames]


def build_scores(cameras, total, fps, vision=None):
    """(usable, activity) Scores per camera; vision[i] holds per-step lists or None.

    Cameras only need .angle, .first and .last (frames on the output grid).
    """
    steps = int(math.ceil(total / fps / Scores.STEP)) + 2
    vision = vision or [None] * len(cameras)
    return [(Scores(v[0] if v else [], steps), Scores(v[1] if v else [], steps)) for v in vision]


def plan(cameras, total, fps, music=None, sections=(), activities=(), seed=42,
         main_camera='A', rhythm=Rhythm(), window=(0, None), vision=None, scores=None):
    """Return the best of several candidate plans over [window) frames."""
    start, end = window[0], total if window[1] is None else window[1]
    rng = random.Random(seed)
    grid, weights = cut_grid(music, total, fps)
    scores = scores or build_scores(cameras, total, fps, vision)
    sections = list(sections)
    activities = sorted(activities)
    on_grid = set(grid)
    best = None
    for n in range(rhythm.candidates):
        shots = plan_once(cameras, start, end, fps, grid, weights, scores, sections, activities,
                          random.Random(rng.random()), main_camera, rhythm,
                          temperature=0.35 if n else 0.0, on_grid=on_grid)
        cost = plan_cost(shots, cameras, fps, scores, main_camera, rhythm)
        if best is None or cost < best[0]:
            best = (cost, shots)
    return best[1]


def angles_at(cameras, frame, minimum=0):
    """Best recording per angle covering frame (longest remaining coverage)."""
    result = {}
    for i, c in enumerate(cameras):
        if c.first <= frame < c.last and c.last - frame >= minimum:
            if c.angle not in result or c.last > cameras[result[c.angle]].last:
                result[c.angle] = i
    return result


def plan_once(cameras, start, end, fps, grid, weights, scores, sections, activities, rng,
              main_camera, rhythm, temperature, on_grid=frozenset()):
    scale = rhythm.cut_scale
    min_frames = max(2, round(rhythm.min_shot * min(1.0, max(scale, 0.6)) * fps))
    angle_ids = list(dict.fromkeys(c.angle for c in cameras))
    shots = []
    used = {a: 0 for a in angle_ids}
    last_seen = {a: -10 ** 9 for a in angle_ids}
    history = []
    frame = start
    # Mandatory cuts: section changes into drops and marked activity moments.
    boundaries = sorted({round(s['start'] * fps) for s in sections if s['kind'] == 'drop'
                         and start < round(s['start'] * fps) < end})
    while frame < end:
        t = frame / fps
        mode = section_at(sections, t)
        covering = angles_at(cameras, frame, 1)
        if not covering:
            nxt = min([c.first for c in cameras if c.first > frame] + [end])
            if shots and shots[-1].camera is None:
                shots[-1].end = nxt
            else:
                shots.append(Shot(frame, nxt, None, 'no camera coverage; black'))
            frame = nxt
            continue
        previous = history[-1] if history else None
        before = history[-2] if len(history) > 1 else None
        covered = sum(used.values()) or 1
        share = used.get(main_camera, 0) / covered
        options = []
        for angle, index in covering.items():
            if angle == previous and len(covering) > 1:
                continue
            is_main = angle == main_camera
            hold = target_hold(mode, is_main, rhythm)
            horizon = min(end, frame + round(hold * fps), cameras[index].last)
            quality = scores[index][0].mean(t, horizon / fps)
            soon = scores[index][0].mean(t, min(horizon / fps, t + 2))
            quality = min(quality, soon)  # a picture that is bad right now is bad
            activity = scores[index][1].mean(t, horizon / fps)
            value = 2.2 * quality
            if is_main:
                value += 2.0 * (rhythm.main_share - share) + (0.6 if mode in ('breakdown', 'normal') else 0)
            else:
                value += 1.0 * (share - rhythm.main_share) + (0.5 if mode in ('drop', 'build') else 0)
            value += (0.9 if mode in ('drop', 'build') else 0.3) * activity
            if angle == before and len(covering) > 2:
                value -= 0.5  # avoid A-B-A-B with three or more angles
            rest = (frame - last_seen[angle]) / fps
            value += min(rest, 60) / 120
            if cameras[index].last - frame < min_frames:
                value -= 3
            if quality < 0.3:
                value -= 2
            options.append((value, angle, index, hold))
        if not options:  # only the previous angle covers this time: continue it
            angle, index = next(iter(covering.items()))
            options = [(0, angle, index, target_hold(mode, angle == main_camera, rhythm))]
        options.sort(reverse=True)
        if temperature and len(options) > 1:
            top = options[0][0]
            probabilities = [math.exp((v - top) / temperature) for v, *_ in options]
            pick = rng.random() * sum(probabilities)
            for chosen, p in zip(options, probabilities):
                pick -= p
                if pick <= 0:
                    break
        else:
            chosen = options[0]
        _, angle, index, hold = chosen
        hold *= rng.uniform(0.75, 1.25)
        # Leave a camera before its picture goes bad (lens covered, knocked, someone in front).
        bad = scores[index][0].bad_from(t + min_frames / fps, min(end, cameras[index].last) / fps)
        limit = cameras[index].last
        if bad is not None:
            limit = max(frame + min_frames, round(bad * fps))
        stop = choose_end(frame, hold, fps, min_frames, end, limit, grid, weights,
                          boundaries, activities if angle == main_camera and mode == 'normal' else ())
        reason = mode + (' (beat-synced)' if stop in on_grid else '')
        if shots and shots[-1].camera == index and shots[-1].end == frame:
            shots[-1].end = stop
        else:
            shots.append(Shot(frame, stop, index, reason))
        used[angle] += stop - frame
        last_seen[angle] = stop
        history.append(angle)
        frame = stop
    return absorb_short(shots, min_frames, cameras)


def target_hold(mode, is_main, rhythm):
    if mode == 'breakdown':
        low, high = rhythm.breakdown_hold if is_main else rhythm.cutaway_hold
    elif mode == 'drop':
        low, high = rhythm.drop_hold
    elif mode == 'build':
        low, high = rhythm.drop_hold[0], rhythm.cutaway_hold[0]
    else:
        low, high = rhythm.main_hold if is_main else rhythm.cutaway_hold
    return (low + high) / 2 * rhythm.cut_scale


def choose_end(frame, hold, fps, min_frames, end, coverage_end, grid, weights, boundaries, activities):
    """The best musical cut near frame + hold, never before a minimum shot."""
    limit = min(end, coverage_end)
    if limit - frame <= min_frames:
        return limit
    target = frame + round(hold * fps)
    # A drop starting inside this shot cuts it there (if not a flash frame).
    i = bisect.bisect_right(boundaries, frame + min_frames - 1)
    if i < len(boundaries) and boundaries[i] < min(limit, target + round(fps)):
        return boundaries[i]
    for a in activities:
        f = round(a * fps)
        if frame + min_frames <= f <= min(limit, frame + round(hold * 1.5 * fps)):
            return f
    lo = frame + max(min_frames, round(hold * 0.6 * fps))
    hi = min(limit, frame + round(hold * 1.5 * fps))
    if hi <= lo:
        return max(frame + min_frames, min(limit, target)) if limit - frame >= min_frames else limit
    a, b = bisect.bisect_left(grid, lo), bisect.bisect_right(grid, hi)
    best, best_value = None, -1e9
    span = max(1, hi - lo)
    for k in range(a, b):
        value = weights[k] - 1.2 * abs(grid[k] - target) / span
        if value > best_value:
            best, best_value = grid[k], value
    stop = best if best is not None else min(limit, max(lo, target))
    # Don't leave a sliver of coverage that would become a flash frame.
    if 0 < limit - stop < min_frames:
        stop = limit
    return stop


def absorb_short(shots, min_frames, cameras):
    """Merge any remaining sub-minimum shot into a neighbour with coverage."""
    changed = True
    while changed:
        changed = False
        for i, s in enumerate(shots):
            if s.end - s.start >= min_frames or len(shots) == 1:
                continue
            for j in (i - 1, i + 1):
                if 0 <= j < len(shots) and shots[j].camera is not None and s.camera is not None:
                    c = cameras[shots[j].camera]
                    if c.first <= min(s.start, shots[j].start) and max(s.end, shots[j].end) <= c.last:
                        shots[j].start, shots[j].end = min(s.start, shots[j].start), max(s.end, shots[j].end)
                        del shots[i]
                        changed = True
                        break
            if changed:
                break
    merged = []
    for s in shots:
        if merged and merged[-1].camera == s.camera and merged[-1].end == s.start:
            merged[-1].end = s.end
        else:
            merged.append(s)
    return merged


def plan_cost(shots, cameras, fps, scores, main_camera, rhythm):
    covered = sum(s.end - s.start for s in shots if s.camera is not None) or 1
    main = sum(s.end - s.start for s in shots if s.camera is not None and cameras[s.camera].angle == main_camera)
    share_cost = ((main / covered - rhythm.main_share) / 0.06) ** 2
    quality = 0.0
    for s in shots:
        if s.camera is not None:
            quality += (1 - scores[s.camera][0].mean(s.start / fps, s.end / fps)) * (s.end - s.start)
    quality_cost = 25 * quality / covered
    durations = [(s.end - s.start) / fps for s in shots if s.camera is not None]
    flashes = sum(1 for d in durations if d < rhythm.min_shot * 0.6)
    black = sum(s.end - s.start for s in shots if s.camera is None) / fps
    return share_cost + quality_cost + 10 * flashes + black


def window_scores(music, cameras, total, fps, length, scores=None, ending='beat', excludes=()):
    """Score every bar-aligned window of about `length` seconds for a short clip.

    ending='beat' plays through the next downbeat and lets it ring for one beat, so the
    clip finishes on the "one"; ending='bar' stops right at the next bar line. Windows that touch
    an excluded (start, end) range are skipped."""
    duration = total / fps
    downbeats = music.get('downbeats', []) if music and music.get('confidence', 0) >= 0.35 else []
    if len(downbeats) > 8:
        bar = (downbeats[-1] - downbeats[0]) / max(1, len(downbeats) - 1)
        bars = max(1, round(length / bar))
        # Prefer a multiple of four bars when that is within 15% of the request.
        four = max(4, round(bars / 4) * 4)
        if abs(four * bar - length) <= 0.15 * length:
            bars = four
        beats = music.get('beats', [])
        beat_index = {round(t, 3): k for k, t in enumerate(beats)}

        def span_of(i):
            # Count whole beats from the bar line, so a misread bar phase never
            # shortens or stretches a clip.
            a = downbeats[i]
            k = beat_index.get(round(a, 3))
            if k is None:
                k = min(range(len(beats)), key=lambda j: abs(beats[j] - a)) if beats else None
            n = None if k is None else k + 4 * bars
            b = beats[n] if n is not None and n < len(beats) else a + bars * bar
            tail = 0.0
            if ending == 'beat':
                step = beats[n + 1] - beats[n] if n is not None and n + 1 < len(beats) else bar / 4
                tail = step if abs(step - bar / 4) <= 0.3 * bar / 4 else bar / 4
            return a, b + tail, tail
        indices = range(len(downbeats))
    else:
        bars = None
        starts = [i * 2.0 for i in range(int(max(0, duration - length) / 2) + 1)]
        span_of = lambda i: (starts[i], starts[i] + length, 0.0)
        indices = range(len(starts))
    energy = music.get('bar_energy', []) if music else []
    bar_starts = music.get('bar_starts', []) if music else []
    drops = music.get('drops', []) if music else []
    result = []
    for i in indices:
        a, b, tail = span_of(i)
        if b > duration + 0.01:
            break
        if any(a < e_end and e_start < b for e_start, e_end in excludes):
            continue
        fa, fb = round(a * fps), round(b * fps)
        # Clips must be fully covered by footage: no black in a reel.
        if not fully_covered(cameras, fa, fb):
            continue
        if bar_starts and energy:
            lo = bisect.bisect_left(bar_starts, a)
            hi = max(lo + 1, bisect.bisect_left(bar_starts, b))
            level = sum(energy[lo:hi]) / max(1, len(energy[lo:hi]))
        else:
            level = 0.5
        value = level
        for d in drops:
            position = (d - a) / max(1e-6, b - a)
            if 0.12 <= position <= 0.55:
                value += 0.8  # the build-up then the drop: the classic reel
            elif 0 <= position < 0.12:
                value += 0.4
        if scores:
            best = max(scores[j][1].mean(a, b) * scores[j][0].mean(a, b) for j in range(len(cameras)))
            value += 0.3 * best
        phrase = bars is not None and downbeats and (i % 4 == 0)
        value += 0.1 if phrase else 0
        result.append({'start': round(a, 3), 'end': round(b, 3), 'score': round(value, 3), 'tail': round(tail, 3)})
    return result


def fully_covered(cameras, a, b):
    """True when some camera (or a hand-over between cameras) covers [a, b)."""
    frame = a
    while frame < b:
        reach = max((c.last for c in cameras if c.first <= frame < c.last), default=None)
        if reach is None:
            return False
        frame = reach
    return True


def choose_clips(windows, count, mode='best'):
    """Pick non-overlapping clip windows: the best `count`, or the whole set in order."""
    if mode == 'sequence':
        chosen, last = [], -1
        for w in windows:
            if w['start'] >= last - 1e-6:
                chosen.append(w)
                last = w['end']
        return chosen[:count] if count else chosen
    chosen = []
    for w in sorted(windows, key=lambda w: -w['score']):
        if all(w['end'] <= c['start'] + 1e-6 or w['start'] >= c['end'] - 1e-6 for c in chosen):
            chosen.append(w)
            if count and len(chosen) >= count:
                break
    return sorted(chosen, key=lambda w: w['start'])


# ---------------------------------------------------------------------------
# Musical social clips (Engine 2). Mirrors Planner.musicalClips/chooseMusical in Swift.
#
# Clips are built from the set's structure instead of a fixed length: whole
# phrases that start on a bar line and land on the downbeat after their last bar
# (ringing for one beat). A build-up and drop clip, for example, starts 4, 8 or 16
# bars before the drop and lands on the downbeat after the drop's 4th or 8th bar.
# ---------------------------------------------------------------------------

CLIP_TITLES = {'rise': 'Build-up and drop', 'drop': 'Drop', 'build': 'Build-up', 'groove': 'Groove',
               'breakdown': 'Breakdown', 'moment': 'Highlight'}
CLIP_BASE = {'rise': 1.2, 'drop': 0.8, 'groove': 0.55, 'build': 0.5, 'breakdown': 0.35, 'moment': 0.5}
MIN_CLIP, MAX_CLIP = 6.0, 60.0   # seconds; shorter phrases feel cut off, longer ones lose people


def nearest_index(values, t):
    """Index of the value closest to t (the earlier one on a tie)."""
    i = bisect.bisect_left(values, t)
    if i <= 0:
        return 0
    if i >= len(values):
        return len(values) - 1
    return i if values[i] - t < t - values[i - 1] else i - 1


def musical_clips(music, cameras, total, fps, scores=None, excludes=()):
    """Every candidate clip the music offers, scored. Kinds:

    rise       the build-up then the drop, landing after the drop's 4th or 8th bar
    drop       straight in on the drop, 8 or 16 bars
    build      the build-up, landing on the drop's first downbeat (the tease)
    breakdown  the start of a long breakdown, 8 or 16 bars
    groove     8 or 16 loud bars on the phrase grid with no drop in them

    Empty without a confident beat grid."""
    if not music or music.get('confidence', 0) < 0.35:
        return []
    beats = music.get('beats') or []
    downbeats = music.get('downbeats') or []
    if len(beats) <= 16 or len(downbeats) <= 8:
        return []
    duration = total / fps
    bar = (downbeats[-1] - downbeats[0]) / (len(downbeats) - 1)
    beat = bar / 4
    energy = music.get('bar_energy') or []
    bar_starts = music.get('bar_starts') or []
    sections = music.get('sections') or []
    drops = music.get('drops') or []

    def level(a, b):
        if not energy or not bar_starts:
            return 0.5
        lo = bisect.bisect_left(bar_starts, a)
        hi = max(lo + 1, bisect.bisect_left(bar_starts, b))
        part = energy[lo:hi]
        return sum(part) / len(part) if part else 0.0

    def picture(a, b):
        if not scores:
            return 0.0
        return max(scores[j][1].mean(a, b) * scores[j][0].mean(a, b) for j in range(len(scores)))

    def tail_after(n):
        step = beats[n + 1] - beats[n] if n + 1 < len(beats) else beat
        return step if abs(step - beat) <= 0.3 * beat else beat

    result = []

    def add(kind, k0, k1, moment, extra=0.0, loud_from=None):
        # k0: first beat; k1: the downbeat the clip lands on (a whole number of bars later).
        # loud_from: energy is judged from there (the drop), not over the quiet build.
        if k0 < 0 or k1 >= len(beats) or k1 <= k0:
            return
        a, land = beats[k0], beats[k1]
        tail = tail_after(k1)
        b = land + tail
        length = b - a
        if length < MIN_CLIP or length > MAX_CLIP or b > duration + 0.01:
            return
        if any(a < e_end and e_start < b for e_start, e_end in excludes):
            return
        if not fully_covered(cameras, round(a * fps), round(b * fps)):
            return
        value = CLIP_BASE[kind] + 0.6 * level(a if loud_from is None else loud_from, land) + 0.4 * picture(a, b) + extra
        if length > 40:
            value -= 0.15
        if length < 9:
            value -= 0.1
        result.append({'start': round(a, 3), 'end': round(b, 3), 'score': round(value, 3), 'tail': round(tail, 3),
                       'kind': kind, 'title': CLIP_TITLES[kind], 'bars': (k1 - k0) // 4, 'moment': moment})

    drop_beats = []
    for n, d in enumerate(drops):
        k = nearest_index(beats, d)
        drop_beats.append(k)
        i = next((j for j, s in enumerate(sections) if abs(s['start'] - d) < 0.01), None)
        # The breakdown and build before the drop, and the drop section itself.
        run_start, j = d, (i if i is not None else 0) - 1
        while i is not None and j >= 0 and sections[j]['kind'] in ('build', 'breakdown'):
            run_start, j = sections[j]['start'], j - 1
        quiet = max(0, (k - nearest_index(beats, run_start)) // 4)
        drop_end = sections[i]['end'] if i is not None else d + 16 * bar
        loud = max(0, (nearest_index(beats, drop_end) - k) // 4)
        contrast = max(0.0, level(d, d + 4 * bar) - level(d - 4 * bar, d))
        moment = f'drop {n}'
        for pre, pre_bonus in ((4, 0.0), (8, 0.15), (16, 0.05)):
            if pre > max(4, quiet + 1):
                continue
            for post, post_bonus in ((4, 0.1), (8, 0.0)):
                if post <= max(4, loud):
                    add('rise', k - 4 * pre, k + 4 * post, moment, 0.5 * contrast + pre_bonus + post_bonus, d)
        for post in (8, 16):
            add('drop', k, k + 4 * post, moment)
        for pre in (8, 16):
            if pre <= max(8, quiet + 1):
                add('build', k - 4 * pre, k, moment, 0.3 * contrast)
        if quiet >= 8:
            start = nearest_index(beats, run_start)
            for length in (8, 16):
                if length <= quiet:
                    add('breakdown', start, start + 4 * length, moment)

    # Phrase grid: 8-bar steps from each section's start and back from the next section's start.
    anchors = set()
    first = downbeats[0]
    for idx, s in enumerate(sections):
        ka = nearest_index(beats, max(s['start'], first))
        kb = nearest_index(beats, max(s['end'], first))
        k = ka
        while k < kb:
            anchors.add(k)
            k += 32
        if idx + 1 < len(sections):
            k = kb - 32
            while k >= ka:
                anchors.add(k)
                k -= 32
    if not sections:
        anchors = set(range(nearest_index(beats, first), len(beats), 32))
    for k in sorted(anchors):
        a = beats[k]
        if section_at(sections, a + 1e-3) not in ('normal', 'drop') or any(abs(k - kd) < 2 for kd in drop_beats):
            continue
        for length in (8, 16):
            k1 = k + 4 * length
            if k1 >= len(beats):
                continue
            land = beats[k1]
            if any(a < d < land - 1e-3 for d in drops):
                continue  # a drop inside makes it a build-up and drop clip
            if any(s['kind'] in ('breakdown', 'build') and s['start'] < land and a < s['end'] for s in sections):
                continue
            if level(a, land) < 0.45:
                continue  # only the loud, busy stretches
            add('groove', k, k1, f'groove {k // 128}')
    return result


def highlight_clips(music, cameras, total, fps, lengths, scores=None, excludes=()):
    """Fallback without a beat grid: the classic windows at the requested lengths."""
    result = []
    for length in lengths or [15]:
        for w in window_scores(music, cameras, total, fps, length, scores, 'beat', excludes):
            result.append({**w, 'kind': 'moment', 'title': CLIP_TITLES['moment'],
                           'bars': 0, 'moment': f'moment {int(w["start"] // 60)}'})
    return result


def choose_musical(candidates, count, mode='best', duration=None):
    """A varied pick: each drop is used once before any is reused, clips spread over the whole set,
    the kinds are mixed, and no two share a single frame (not even the last ringing beat), so clips of
    different lengths are never the same scene.
    sequence walks through the set in order instead. count 0 = as many as fit."""
    if not candidates:
        return []
    span = duration or max(c['end'] for c in candidates)
    spacing = span / count if count else 90.0
    want = count or len(candidates)
    chosen = []

    def free(c):
        return all(c['end'] <= o['start'] + 1e-6 or c['start'] >= o['end'] - 1e-6 for o in chosen)

    def adjusted(c, spread=True):
        value = c['score']
        value -= 1.5 * sum(1 for o in chosen if o['moment'] == c['moment'])
        value -= 0.3 * sum(1 for o in chosen if o['kind'] == c['kind'])
        value -= 0.15 * sum(1 for o in chosen if o['kind'] == c['kind'] and o['bars'] == c['bars'])
        if spread and chosen:
            centre = (c['start'] + c['end']) / 2
            gap = min(abs(centre - (o['start'] + o['end']) / 2) for o in chosen)
            value -= 0.8 * max(0.0, 1 - gap / spacing)
        return value

    def best_of(pool, spread):
        best, key = None, None
        for c in pool:
            k = (adjusted(c, spread), -c['start'])
            if key is None or k > key:
                best, key = c, k
        return best

    if mode == 'sequence':
        cursor = -1.0
        while len(chosen) < want:
            pool = [c for c in candidates if c['start'] >= cursor - 1e-6]
            if not pool:
                break
            soonest = min(c['start'] for c in pool)
            best = best_of([c for c in pool if c['start'] <= soonest + 30], False)
            chosen.append(best)
            cursor = best['end']
        return chosen
    while len(chosen) < want:
        pool = [c for c in candidates if free(c)]
        if not pool:
            break
        chosen.append(best_of(pool, True))
    return sorted(chosen, key=lambda c: c['start'])


def social_clips(music, cameras, total, fps, count, mode='best', lengths=(), scores=None, excludes=()):
    """Engine 2's social clips: musical candidates (or timed highlights without a beat grid), chosen for variety."""
    candidates = musical_clips(music, cameras, total, fps, scores, excludes)
    if not candidates:
        candidates = highlight_clips(music, cameras, total, fps, list(lengths), scores, excludes)
    return choose_musical(candidates, count, mode, total / fps)


def lead_camera(cameras, frame, n, main):
    """Engine 2: the n-th clip in the set (from 0) opens on, and favours, the n-th angle in turn,
    starting with the main camera, so clips side by side on a profile grid don't all look alike.
    Angles with no footage at the clip's start are skipped."""
    angles = list(dict.fromkeys(c.angle for c in cameras))
    if not angles:
        return main
    i = angles.index(main) if main in angles else 0
    order = angles[i:] + angles[:i]
    covering = angles_at(cameras, frame, 1)
    for k in range(len(order)):
        angle = order[(n + k) % len(order)]
        if angle in covering:
            return angle
    return main
