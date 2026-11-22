"""Multicam Studio's engine tests (vendored with the engine in app/multicam_engine)."""
import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent/'app'/'multicam_engine'))
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import wave

import numpy as np

import multicam_edit as engine
import music
import planner


def dance_track(bpm=128.0, seconds=96, breakdown=(32, 48), sr=8000, seed=3):
    """Kick on every beat, hats off-beat, a breakdown with no kick or bass."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * sr)) / sr
    x = 0.004 * rng.normal(size=len(t))
    beat = 60 / bpm
    for k in range(int(seconds / beat)):
        start = k * beat
        i = int(start * sr)
        n = int(0.12 * sr)
        tt = np.arange(n) / sr
        in_breakdown = breakdown[0] <= start < breakdown[1]
        if not in_breakdown:
            x[i:i + n] += 0.9 * np.sin(2 * np.pi * 55 * tt) * np.exp(-tt * 25)[:len(x[i:i + n])]
        j = int((start + beat / 2) * sr)
        m = int(0.03 * sr)
        x[j:j + m] += 0.25 * rng.normal(size=len(x[j:j + m])) * np.exp(-np.arange(len(x[j:j + m])) / sr * 120)
        if k % 4 == 0 and not in_breakdown:  # bass stab on every bar line
            b = int(0.4 * sr)
            x[i:i + b] += 0.4 * np.sin(2 * np.pi * 82 * np.arange(len(x[i:i + b])) / sr)
    pad = 0.15 * np.sin(2 * np.pi * 440 * t) * ((t >= breakdown[0]) & (t < breakdown[1]))
    return (x + pad).astype(np.float32)


class Cam:
    def __init__(self, angle, first, last):
        self.angle, self.first, self.last = angle, first, last


class MusicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = music.analyse(dance_track())

    def test_tempo_and_beat_grid(self):
        self.assertAlmostEqual(self.result['bpm'], 128.0, delta=1.0)
        self.assertGreater(self.result['confidence'], 0.5)
        beats = np.array(self.result['beats'])
        steady = np.abs(np.diff(beats) - 60 / 128) < 0.03
        self.assertGreater(steady.mean(), 0.9)  # only the kick-less breakdown edges may wobble

    def test_breakdown_build_and_drop(self):
        kinds = {s['kind'] for s in self.result['sections']}
        self.assertIn('breakdown', kinds)
        self.assertTrue(self.result['drops'])
        self.assertAlmostEqual(self.result['drops'][0], 48.0, delta=2.0)

    def test_snap(self):
        self.assertEqual(music.snap(10.02, [9.5, 10.0, 10.5], 0.1), 10.0)
        self.assertEqual(music.snap(10.3, [9.5, 10.0], 0.1), 10.3)


class PlannerTests(unittest.TestCase):
    fps = 30

    def music(self):
        return music.analyse(dance_track())

    def test_no_flash_frames_and_cuts_on_beats(self):
        info = self.music()
        total = 96 * self.fps
        cams = [Cam('A', 0, total), Cam('B', 0, total), Cam('C', 0, total)]
        shots = planner.plan(cams, total, self.fps, info, [s for s in info['sections'] if s['kind'] != 'normal'],
                             seed=7, main_camera='A', rhythm=planner.Rhythm(cut_scale=0.5))
        self.assertEqual(shots[0].start, 0)
        self.assertEqual(shots[-1].end, total)
        self.assertTrue(all(a.end == b.start for a, b in zip(shots, shots[1:])))
        self.assertTrue(all((s.end - s.start) / self.fps >= 0.6 for s in shots[:-1]))
        self.assertFalse(any(s.camera is None for s in shots))
        beats = set(round(t * self.fps) for t in info['beats'])
        on_beat = sum(1 for s in shots[1:] if any(abs(s.start - b) <= 1 for b in beats))
        self.assertGreaterEqual(on_beat / max(1, len(shots) - 1), 0.8)

    def test_hands_over_when_a_camera_ends_and_black_only_without_footage(self):
        total = 3000
        cams = [Cam('A', 0, 900), Cam('B', 0, 1800)]
        shots = planner.plan(cams, total, self.fps, None, seed=1, main_camera='A')
        self.assertTrue(all(s.camera == 1 for s in shots if 900 <= s.start < 1800))
        self.assertEqual([(s.start, s.end) for s in shots if s.camera is None], [(1800, 3000)])

    def test_poor_picture_is_avoided(self):
        total = 60 * self.fps
        cams = [Cam('A', 0, total), Cam('B', 0, total)]
        steps = int(np.ceil(total / self.fps / planner.Scores.STEP)) + 2
        bad_a = ([1.0] * 40 + [0.0] * 40 + [1.0] * (steps - 80), [0.5] * steps)
        scores = planner.build_scores(cams, total, self.fps, [bad_a, None])
        shots = planner.plan(cams, total, self.fps, None, seed=2, main_camera='A', scores=scores)
        a_in_bad = sum(min(s.end, 40 * self.fps) - max(s.start, 20 * self.fps)
                       for s in shots if s.camera == 0 and s.end > 20 * self.fps and s.start < 40 * self.fps)
        self.assertLess(a_in_bad, 0.25 * 20 * self.fps)

    def test_clip_windows_are_bar_aligned_and_fully_covered(self):
        info = self.music()
        total = 96 * self.fps
        cams = [Cam('A', 0, 70 * self.fps)]
        windows = planner.window_scores(info, cams, total, self.fps, 15, ending='bar')
        self.assertTrue(windows)
        downbeats = info['downbeats']
        for w in windows:
            self.assertLessEqual(w['end'], 70 + 1e-6)
            self.assertTrue(any(abs(w['start'] - d) < 1e-3 for d in downbeats))
            self.assertAlmostEqual(w['end'] - w['start'], 8 * 4 * 60 / 128, delta=0.6)
        best = planner.choose_clips(windows, 2, 'best')
        self.assertEqual(len(best), 2)
        self.assertLessEqual(best[0]['end'], best[1]['start'] + 1e-6)
        sequence = planner.choose_clips(windows, 0, 'sequence')
        self.assertTrue(all(a['end'] <= b['start'] + 1e-6 for a, b in zip(sequence, sequence[1:])))

    def test_clip_excludes_keep_windows_out(self):
        info = self.music()
        total = 96 * self.fps
        cams = [Cam('A', 0, total)]
        windows = planner.window_scores(info, cams, total, self.fps, 15, excludes=[(20, 40)])
        self.assertTrue(windows)
        self.assertTrue(all(w['end'] <= 20 or w['start'] >= 40 for w in windows))
        everything = planner.window_scores(info, cams, total, self.fps, 15)
        self.assertGreater(len(everything), len(windows))

    def test_clips_end_on_the_beat_or_at_the_bar_line(self):
        info = self.music()
        total = 96 * self.fps
        cams = [Cam('A', 0, total)]
        beat = planner.window_scores(info, cams, total, self.fps, 15, ending='beat')
        bar = planner.window_scores(info, cams, total, self.fps, 15, ending='bar')
        bar_ends = {w['start']: w['end'] for w in bar}
        self.assertTrue(beat)
        for w in beat:
            # One beat past the bar line, so the next downbeat is heard.
            self.assertAlmostEqual(w['tail'], 60 / 128, delta=0.3 * 60 / 128)
            if w['start'] in bar_ends:
                self.assertAlmostEqual(w['end'] - w['tail'], bar_ends[w['start']], delta=1e-3)
        self.assertTrue(all(w['tail'] == 0 for w in bar))


class ClipFramingTests(unittest.TestCase):
    def camera(self, w, h, framing=None):
        c = engine.Camera(Path('x.mov'), 'A', 10, 0, 0, 0, True, width=w, height=h)
        c.framing = framing
        return c

    def test_smart_fits_wide_shots_and_crops_close_matches(self):
        crop = {'mode': 'crop', 'center': [0.3, 0.5], 'zoom': 1, 'auto': False}
        self.assertEqual(engine.clip_framing(self.camera(1920, 1080, crop), '9:16', 'smart')['mode'], 'fit')
        self.assertEqual(engine.clip_framing(self.camera(1080, 1920, crop), '9:16', 'smart')['mode'], 'crop')
        self.assertEqual(engine.clip_framing(self.camera(1920, 1080, crop), '16:9', 'smart')['mode'], 'crop')
        self.assertEqual(engine.clip_framing(self.camera(1920, 1080, {'mode': 'fit'}), '9:16', 'crop')['mode'], 'crop')
        self.assertEqual(engine.clip_framing(self.camera(1920, 1080, crop), '9:16', 'same'), crop)

    def test_clip_lengths_parse(self):
        self.assertEqual(engine.clip_lengths('30, 15,15,60'), [15.0, 30.0, 60.0])
        for bad in ('', '2', 'x', '15,9999'):
            with self.assertRaises(Exception):
                engine.clip_lengths(bad)


class FormatTests(unittest.TestCase):
    def test_frame_sizes(self):
        self.assertEqual(engine.frame_size('16:9', 2160), (3840, 2160))
        self.assertEqual(engine.frame_size('9:16', 1080), (1080, 1920))
        self.assertEqual(engine.frame_size('4:5', 1080), (1080, 1350))
        self.assertEqual(engine.frame_size('1:1', 720), (720, 720))

    def test_fit_uses_blurred_fill_not_black_bars(self):
        blurred = engine.framing_filter({'mode': 'fit'}, (1080, 1920))
        self.assertIn('boxblur', blurred)
        self.assertNotIn('pad=', blurred)
        self.assertIn('pad=', engine.framing_filter({'mode': 'pad'}, (1080, 1920)))

    def test_auto_framing_uses_focus(self):
        f = engine.framing_filter({'mode': 'crop', 'center': [0.5, 0.5], 'auto': True}, (1080, 1920), [0.25, 0.4])
        self.assertIn('iw*0.25', f)
        self.assertIn('ih*0.4', f)

class MusicalClipTests(unittest.TestCase):
    fps = 30

    @classmethod
    def setUpClass(cls):
        # Three breakdowns and drops over seven minutes at 126 BPM.
        parts = [dance_track(126, 140, (60, 90), seed=1), dance_track(126, 140, (40, 75), seed=2),
                 dance_track(126, 140, (20, 50), seed=4)]
        cls.info = music.analyse(np.concatenate(parts))

    def set_music(self):
        return self.info

    def test_musical_clips_land_on_the_drop_bars(self):
        info = self.set_music()
        total = round(info['duration'] * self.fps)
        cams = [Cam('A', 0, total), Cam('B', 0, total)]
        candidates = planner.musical_clips(info, cams, total, self.fps, planner.build_scores(cams, total, self.fps))
        self.assertTrue(info['drops'])
        beats = info['beats']
        kinds = {c['kind'] for c in candidates}
        self.assertTrue({'rise', 'drop', 'build', 'groove'} <= kinds)
        for c in candidates:
            k0 = min(range(len(beats)), key=lambda i: abs(beats[i] - c['start']))
            land = c['end'] - c['tail']
            k1 = min(range(len(beats)), key=lambda i: abs(beats[i] - land))
            self.assertEqual(k1 - k0, 4 * c['bars'])                    # whole bars, landing on a beat
            self.assertTrue(planner.MIN_CLIP <= c['end'] - c['start'] <= planner.MAX_CLIP)
            if c['kind'] == 'rise':
                drop = next(d for d in info['drops'] if c['start'] < d < land)
                self.assertIn(round((land - drop) / (240 / info['bpm'])), (4, 8))  # 4th or 8th bar of the drop
            if c['kind'] == 'groove':
                self.assertFalse(any(c['start'] < d < land for d in info['drops']))

    def test_musical_choice_is_varied_and_spread(self):
        info = self.set_music()
        total = round(info['duration'] * self.fps)
        cams = [Cam('A', 0, total)]
        candidates = planner.musical_clips(info, cams, total, self.fps)
        chosen = planner.choose_musical(candidates, 6, 'best', info['duration'])
        self.assertEqual(len(chosen), 6)
        drops = [c['moment'] for c in chosen if c['moment'].startswith('drop')]
        self.assertEqual(len(drops), len(set(drops)))                  # no drop used twice
        self.assertGreaterEqual(len({c['kind'] for c in chosen}), 3)
        self.assertTrue(any(c['kind'] == 'groove' for c in chosen))   # some without a drop at all
        for a, b in zip(chosen, chosen[1:]):
            self.assertLessEqual(a['end'], b['start'] + 1e-6)              # not a single shared frame
        thirds = {int(3 * (c['start'] + c['end']) / 2 / info['duration']) for c in chosen}
        self.assertEqual(thirds, {0, 1, 2})                             # from all through the set
        sequence = planner.choose_musical(candidates, 0, 'sequence', info['duration'])
        self.assertTrue(all(a['end'] <= b['start'] + 1e-6 for a, b in zip(sequence, sequence[1:])))
        self.assertEqual(chosen, planner.choose_musical(candidates, 6, 'best', info['duration']))
        # As many as fit: short and long clips together, and still no frame used twice.
        everything = planner.choose_musical(candidates, 0, 'best', info['duration'])
        self.assertGreater(len({round(c['end'] - c['start']) for c in everything}), 1)
        for i, a in enumerate(everything):
            for b in everything[i + 1:]:
                self.assertTrue(a['end'] <= b['start'] + 1e-6 or b['end'] <= a['start'] + 1e-6)

    def test_clips_lead_with_each_camera_in_turn(self):
        cams = [Cam('A', 0, 3000), Cam('B', 0, 3000), Cam('C', 1500, 3000)]
        self.assertEqual([planner.lead_camera(cams, 2000, n, 'B') for n in range(4)], ['B', 'C', 'A', 'B'])
        self.assertEqual([planner.lead_camera(cams, 100, n, 'B') for n in range(3)], ['B', 'A', 'A'])  # C not recording yet
        info = self.set_music()
        total = round(info['duration'] * self.fps)
        cams = [Cam('A', 0, total), Cam('B', 0, total), Cam('C', 0, total)]
        short = planner.Rhythm(**{**planner.SHORT_FORM.__dict__, 'candidates': 8})
        sections = [s for s in info['sections'] if s['kind'] != 'normal']
        opening = []
        for n, c in enumerate(planner.social_clips(info, cams, total, self.fps, 3, 'best', [15])):
            window = (round(c['start'] * self.fps), round(c['end'] * self.fps))
            lead = planner.lead_camera(cams, window[0], n, 'A')
            shots = planner.plan(cams, total, self.fps, info, sections, (), 42 + n, lead, short, window=window)
            opening.append(cams[shots[0].camera].angle)
        self.assertEqual(opening, ['A', 'B', 'C'])

    def test_social_clips_fall_back_to_lengths_without_a_beat(self):
        total = 120 * self.fps
        cams = [Cam('A', 0, total)]
        chosen = planner.social_clips(None, cams, total, self.fps, 3, 'best', [15])
        self.assertEqual(len(chosen), 3)
        self.assertTrue(all(c['kind'] == 'moment' and abs(c['end'] - c['start'] - 15) < 1e-6 for c in chosen))


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'Requires FFmpeg')
class ClipExportTests(unittest.TestCase):
    def test_vertical_clips_render_from_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            samples = (dance_track(seconds=40, breakdown=(12, 20)) * 32767 * 0.5).astype('<i2')
            with wave.open(str(root / 'set.wav'), 'wb') as f:
                f.setnchannels(1); f.setsampwidth(2); f.setframerate(8000); f.writeframes(samples.tobytes())
            for name, colour, size in [('a', 'red', '320x180'), ('b', 'blue', '320x180'), ('c', 'green', '180x320')]:
                subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 'lavfi', '-i', f'color=c={colour}:s={size}:r=30:d=40',
                                '-i', str(root / 'set.wav'), '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'libx264', '-preset', 'ultrafast',
                                '-c:a', 'pcm_s16le', '-t', '40', str(root / f'{name}.mov')], check=True)
            project = root / 'project.json'
            project.write_text(json.dumps({'cameras': [
                {'id': 'A', 'clips': [{'path': str(root / 'a.mov'), 'framing': {'mode': 'crop'}}]},
                {'id': 'B', 'clips': [{'path': str(root / 'b.mov'), 'framing': {'mode': 'fit'}}]},
                {'id': 'C', 'clips': [{'path': str(root / 'c.mov'), 'framing': {'mode': 'pad'}}]}]}), encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                engine.main(['--project', str(project), '--audio', str(root / 'set.wav'), '--output-dir', str(root / 'out'),
                             '--resolution', '720', '--clips', '7,5', '--clip-count', '2', '--clip-resolution', '720', '--engine', '1',
                             '--clips-only', '--no-drift', '--preset', 'ultrafast', '--encoder', 'software', '--vision', 'off'])
            report = json.loads((root / 'out' / 'Project files' / 'set' / 'Report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'rendered')
            self.assertIsNone(report['outputs']['video'])
            self.assertEqual(len(report['clips']), 4)
            self.assertEqual(sorted({c['length'] for c in report['clips']}), [5, 7])
            for clip in report['clips']:
                self.assertIn(f"/Social media/set/{round(clip['length'])}s/set - {round(clip['length'])}s - ", clip['path'])
                self.assertIn(clip['look'], engine.looks.STYLES)
                info = engine.probe(Path(clip['path']))
                video = next(s for s in info['streams'] if s['codec_type'] == 'video')
                self.assertEqual((video['width'], video['height']), (720, 1280))
                self.assertAlmostEqual(float(info['format']['duration']), clip['end'] - clip['start'], delta=0.1)
            self.assertEqual(len([v for v in report['outputs']['videos'] if not v.get('alternative')]), 4)
            # Clips that use the vertical camera C also get a 9:16 copy in Social media alternatives.
            alternatives = [c for c in report['clips'] if c.get('alternative_paths')]
            self.assertTrue(alternatives)
            sizes = {'16:9': (1280, 720), '9:16': (720, 1280)}
            for clip in alternatives:
                self.assertEqual(set(clip['alternative_paths']), {'16:9', '9:16'})
                for shape, path in clip['alternative_paths'].items():
                    self.assertIn(f"/Social media alternatives/{engine.ALTERNATIVE_SHAPES[shape]}/set/{round(clip['length'])}s/", path)
                    self.assertEqual(Path(path).name, Path(clip['path']).name)
                    info = engine.probe(Path(path))
                    video = next(s for s in info['streams'] if s['codec_type'] == 'video')
                    self.assertEqual((video['width'], video['height']), sizes[shape])

    def test_musical_clips_share_one_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            samples = (dance_track(seconds=64, breakdown=(16, 31)) * 32767 * 0.5).astype('<i2')
            with wave.open(str(root / 'set.wav'), 'wb') as f:
                f.setnchannels(1); f.setsampwidth(2); f.setframerate(8000); f.writeframes(samples.tobytes())
            subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 'lavfi', '-i', 'color=c=red:s=320x180:r=30:d=64',
                            '-i', str(root / 'set.wav'), '-map', '0:v:0', '-map', '1:a:0', '-c:v', 'libx264', '-preset', 'ultrafast',
                            '-c:a', 'pcm_s16le', '-t', '64', str(root / 'a.mov')], check=True)
            shutil.copy(root / 'a.mov', root / 'b.mov')
            with contextlib.redirect_stdout(io.StringIO()):
                engine.main(['--cam-a', str(root / 'a.mov'), '--cam-b', str(root / 'b.mov'), '--audio', str(root / 'set.wav'),
                             '--output-dir', str(root / 'out'), '--resolution', '720', '--clips', '15,30', '--clip-count', '0',
                             '--clip-resolution', '720', '--clips-only', '--no-drift', '--preset', 'ultrafast',
                             '--encoder', 'software', '--vision', 'off', '--no-clip-alternatives'])
            report = json.loads((root / 'out' / 'Project files' / 'set' / 'Report.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'rendered')
            self.assertTrue(report['clips'])
            self.assertTrue(any(c['kind'] == 'rise' for c in report['clips']))
            beats = report['beats']
            for number, clip in enumerate(report['clips'], 1):
                path = Path(clip['path'])
                self.assertEqual(path.parent, (root / 'out' / 'Social media' / 'set').resolve())
                self.assertTrue(path.name.startswith(f'set - {number:02d} - '))
                self.assertTrue(path.name.endswith(f" - {clip['title']}.mp4"))
                self.assertTrue(any(abs(clip['start'] - b) < 1e-3 for b in beats))
                self.assertTrue(any(abs(clip['end'] - clip['tail'] - b) < 2e-3 for b in beats))
                info = engine.probe(path)
                self.assertAlmostEqual(float(info['format']['duration']), clip['end'] - clip['start'], delta=0.1)

    def test_auto_clip_looks_mix_black_and_white(self):
        import looks
        clips = [{'start': i * 30, 'score': s} for i, s in enumerate([2.0, 0.4, 1.5, 0.2, 1.8, 1.1])]
        auto = looks.assign_clip_looks(clips, 'auto', seed=3)
        self.assertEqual(sum(l in looks.GREYSCALE for l in auto), 2)       # a third, the calmest two
        self.assertIn(auto[3], looks.GREYSCALE); self.assertIn(auto[1], looks.GREYSCALE)
        self.assertTrue(all(l in looks.COLOUR_MIX for i, l in enumerate(auto) if i not in (1, 3)))
        self.assertEqual(auto, looks.assign_clip_looks(clips, 'auto', seed=3))  # deterministic
        self.assertEqual(looks.assign_clip_looks(clips, 'off'), ['none'] * 6)
        self.assertEqual(looks.assign_clip_looks(clips, 'sepia'), ['sepia'] * 6)
        self.assertEqual(engine.clip_name('26-09-26-1', 15, 3, 3725.4, 'mp4'), '26-09-26-1 - 15s - 03 - 1h02m05s.mp4')
        self.assertEqual(engine.readable_name('26/09: set*1'), '26 09 set 1')

    def test_social_media_alternatives_for_clips_with_vertical_footage(self):
        base = dict(duration=100, video_start=0, format_start=0, audio_start=0, has_audio=True)
        cams = [engine.Camera(Path('a.mov'), 'A', width=1920, height=1080, framing={'mode': 'fit'}, **base),
                engine.Camera(Path('c.mov'), 'C', width=1080, height=1920, framing={'mode': 'pad'}, **base)]
        self.assertEqual(engine.alternative_framing(cams[0], '9:16')['mode'], 'pad')
        tall = engine.alternative_framing(cams[1], '9:16')
        self.assertEqual((tall['mode'], tall['zoom']), ('crop', 1))
        self.assertEqual(engine.alternative_framing(cams[1], '16:9')['mode'], 'pad')
        self.assertEqual(engine.alternative_framing(cams[0], '16:9')['mode'], 'crop')
        self.assertEqual(engine.clip_framing(cams[0], '9:16', 'bars')['mode'], 'pad')
        self.assertFalse(engine.is_vertical(cams[0])); self.assertTrue(engine.is_vertical(cams[1]))

# ServerTests, AIGuidanceTests and SignInTests cover Multicam Studio's own web app, which Reelflow does not ship.

if __name__ == '__main__':
    unittest.main()
