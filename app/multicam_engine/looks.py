"""Colour looks shared by the Effects tool and the editing engine (social clip filters).

Mirrors Look.Style in the Swift app (macos/Sources/MulticamCore/Compositor.swift, Effects.apply).
"""

STYLES = ('none', 'warm', 'cool', 'mono', 'vintage', 'vivid', 'noir', 'silver', 'sepia', 'super8', 'faded')
GREYSCALE = ('noir', 'silver')
# Colour looks that "Auto mix" rotates through for clips that stay in colour.
COLOUR_MIX = ('none', 'vivid', 'vintage', 'warm', 'super8', 'faded')

LOOK_FILTERS = {
    'none': [],
    'warm': ['colorbalance=rs=0.035:rm=0.04:bs=-0.035:bm=-0.025'],
    'cool': ['colorbalance=rs=-0.025:rm=-0.02:bs=0.045:bm=0.035'],
    'mono': ['hue=s=0'],
    'vintage': ['eq=contrast=0.94:saturation=0.7:brightness=0.025',
                'colorbalance=rm=0.04:bm=-0.03'],
    'vivid': ['eq=contrast=1.08:saturation=1.22'],
    'noir': ['hue=s=0', 'eq=contrast=1.35:brightness=-0.02'],
    'silver': ['hue=s=0', 'eq=contrast=0.85:brightness=0.03', 'colorbalance=bm=0.02'],
    'sepia': ['hue=s=0', 'colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131'],
    'super8': ['eq=contrast=0.88:saturation=0.75:brightness=0.03', 'colorbalance=rm=0.06:bm=-0.08',
               'noise=alls=14:allf=t', 'vignette=PI/4.5'],
    'faded': ['eq=contrast=0.8:saturation=0.8:brightness=0.06', 'colorbalance=rm=0.02:bm=-0.03'],
}


def look_filters(style):
    """FFmpeg video filters for a look (empty for 'none' or unknown)."""
    return list(LOOK_FILTERS.get(style, []))


def assign_clip_looks(clips, mode, seed=0):
    """Pick a look for each social clip. `clips` are dicts with 'start' and 'score'.

    'off' leaves every clip as it is; a style name gives every clip that look; 'auto' makes about a
    third of the clips black and white (the calmest moments, alternating Noir and Silver) and
    rotates the rest through COLOUR_MIX in time order. Mirrors Engine.assignClipLooks in Swift."""
    if mode in (None, '', 'off'):
        return ['none'] * len(clips)
    if mode != 'auto':
        return [mode] * len(clips)
    n = len(clips)
    looks = ['none'] * n
    grey = round(n / 3) if n >= 2 else 0
    calm = sorted(range(n), key=lambda i: (clips[i]['score'], clips[i]['start']))[:grey]
    for rank, i in enumerate(calm):
        looks[i] = GREYSCALE[rank % len(GREYSCALE)]
    colour = sorted((i for i in range(n) if i not in set(calm)), key=lambda i: clips[i]['start'])
    for k, i in enumerate(colour):
        looks[i] = COLOUR_MIX[(seed + k) % len(COLOUR_MIX)]
    return looks
