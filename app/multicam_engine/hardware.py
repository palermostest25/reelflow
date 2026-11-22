"""Hardware video encoding and decoding available to FFmpeg on this machine.

    python3 hardware.py        # prints what was found

Standard library only (server.py and the UI demo import it).

* macOS: VideoToolbox (h264_videotoolbox, hevc_videotoolbox, prores_videotoolbox),
  with VideoToolbox decoding.
* Linux / Docker with an NVIDIA GPU (for example a T1000): NVENC (h264_nvenc,
  hevc_nvenc) with CUDA decoding. In Docker this needs the NVIDIA Container Toolkit
  on the host and the GPU passed to the container (see docker-compose.nvidia.yml).

Every option is checked with a real one-frame encode, so a build that lists NVENC
without a working driver quietly falls back to software encoding.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys

_CACHE = None

ENCODERS = {
    'videotoolbox': {'h264': 'h264_videotoolbox', 'hevc': 'hevc_videotoolbox', 'prores': 'prores_videotoolbox'},
    'nvenc': {'h264': 'h264_nvenc', 'hevc': 'hevc_nvenc'},
}
DECODE = {'videotoolbox': ['-hwaccel', 'videotoolbox'], 'nvenc': ['-hwaccel', 'cuda']}


def _run(args, timeout=20):
    try:
        return subprocess.run(args, capture_output=True, timeout=timeout, text=True,
                              encoding='utf-8', errors='replace')
    except (OSError, subprocess.SubprocessError):
        return None


def _works(encoder):
    """True when FFmpeg can really encode one small frame with this encoder."""
    p = _run(['ffmpeg', '-hide_banner', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i',
              'testsrc=size=256x144:rate=30:duration=0.1', '-frames:v', '2', '-c:v', encoder,
              '-pix_fmt', 'yuv420p' if 'prores' not in encoder else 'yuv422p10le', '-f', 'null', '-'])
    return bool(p and p.returncode == 0)


def detect():
    """{'name': 'videotoolbox'|'nvenc'|None, 'encoders': {codec: name}, 'decode': [...], 'gpu': str}"""
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    result = {'name': None, 'encoders': {}, 'decode': [], 'gpu': ''}
    if not shutil.which('ffmpeg'):
        _CACHE = result
        return result
    listed = _run(['ffmpeg', '-hide_banner', '-encoders'])
    hwaccels = _run(['ffmpeg', '-hide_banner', '-hwaccels'])
    listed = listed.stdout if listed else ''
    hwaccels = hwaccels.stdout if hwaccels else ''
    family = 'videotoolbox' if sys.platform == 'darwin' else 'nvenc'
    working = {codec: name for codec, name in ENCODERS[family].items() if name in listed and _works(name)}
    if working:
        result.update(name=family, encoders=working)
        accel = DECODE[family][1]
        if accel in hwaccels:
            result['decode'] = list(DECODE[family])
        if family == 'nvenc':
            smi = _run(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'], timeout=10)
            result['gpu'] = smi.stdout.strip().splitlines()[0] if smi and smi.returncode == 0 and smi.stdout.strip() else 'NVIDIA GPU'
        else:
            result['gpu'] = 'Apple VideoToolbox'
    _CACHE = result
    return result


def encoder(codec):
    """FFmpeg encoder name for a codec on this machine's hardware, or None."""
    return detect()['encoders'].get(codec)


def decode_args():
    return list(detect()['decode'])


def summary():
    info = detect()
    if not info['name']:
        return 'Software encoding (no hardware encoder found)'
    return f"{info['gpu']}: " + ', '.join(sorted(info['encoders'])) + (' with hardware decoding' if info['decode'] else '')


if __name__ == '__main__':
    print(json.dumps(detect(), indent=2))
    print(summary())
