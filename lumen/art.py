"""Procedural art. No asset files - every pixel in LUMEN is generated here.

Why images at all, in a vector-shape library? Because of how cmu-graphics
actually performs: a `drawImage` call costs about the same no matter how big
the image is (~21us, essentially all of it Python-side shape construction),
while a `drawCircle` costs ~36us for a handful of pixels. So anything that is
static, soft, or expensive to express as polygons - glows, gradients, grain,
stone texture, the entire baked floor - is far cheaper as one pre-rendered
image than as a pile of shapes.

Everything is built lazily and cached, so a sprite is only ever rasterised
once per run.
"""

import math
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from cmu_graphics import CMUImage, getImageSize

from . import draw, gpu
from .draw import drawImage

from . import noise

# --------------------------------------------------------------------------
# Fonts. macOS ships these; if a lookup fails we fall back gracefully so the
# game still runs on a machine with a different font set.
# --------------------------------------------------------------------------
_FONT_CANDIDATES = {
    'display': [
        '/System/Library/Fonts/Supplemental/Copperplate.ttc',
        '/System/Library/Fonts/Supplemental/Futura.ttc',
        '/System/Library/Fonts/Avenir Next Condensed.ttc',
        '/System/Library/Fonts/Supplemental/Impact.ttf',
    ],
    'ui': [
        '/System/Library/Fonts/Menlo.ttc',
        '/System/Library/Fonts/Monaco.ttf',
        '/System/Library/Fonts/Supplemental/Courier New.ttf',
    ],
}
_font_cache = {}


def font(role, size, bold=False):
    """The face for a role, at a pixel size.

    Bold asks the font collection for its real bold face rather than
    thickening the regular one: these are .ttc collections and the bold face
    is at a different index in each, so the index is found by name.
    """
    key = (role, size, bold)
    hit = _font_cache.get(key)
    if hit is not None:
        return hit
    chosen = None
    for path in _FONT_CANDIDATES.get(role, []):
        try:
            if bold:
                chosen = _bold_face(path, size)
                if chosen is not None:
                    break
                continue
            chosen = ImageFont.truetype(path, size)
            break
        except Exception:
            continue
    if chosen is None and bold:
        # No bold face anywhere in the candidates: fall back to the regular
        # one. The caller thickens it with a hairline stroke instead.
        chosen = font(role, size)
    if chosen is None:
        try:
            chosen = ImageFont.load_default(size)
        except Exception:
            chosen = ImageFont.load_default()
    _font_cache[key] = chosen
    return chosen


def _bold_face(path, size):
    """The bold member of a font collection, or None if it has none."""
    for index in range(8):
        try:
            face = ImageFont.truetype(path, size, index=index)
        except Exception:
            return None
        try:
            style = face.getname()[1] or ''
        except Exception:
            style = ''
        if 'bold' in style.lower() and 'italic' not in style.lower():
            return face
    return None


def has_bold_face(role):
    """Whether `font(role, size, bold=True)` returns a genuine bold face."""
    for path in _FONT_CANDIDATES.get(role, []):
        if _bold_face(path, 12) is not None:
            return True
    return False


# --------------------------------------------------------------------------
# Sprite cache
# --------------------------------------------------------------------------
# Render scale. Sprites are rasterised at design-size x SCALE so they land on
# screen at exactly one texel per pixel; every public size here stays in design
# units, so callers never have to think about it.
SCALE = 1.0


def px(value):
    """Design units -> raster pixels."""
    return max(1, int(round(value * SCALE)))


def set_scale(scale):
    """Change the render scale, discarding art built for the old one."""
    global SCALE
    scale = float(scale) or 1.0
    if abs(scale - SCALE) < 1e-6:
        return
    SCALE = scale
    clear_all()


_cache = {}
_pil_cache = {}
# Pixel dimensions keyed by sprite identity, so callers that need to position
# a baked sprite do not have to search the cache for it.
_dims = {}
# Where the glyphs start inside a baked text sprite, so callers can align to
# the text rather than to its transparent padding.
_insets = {}


def premultiply(pil):
    """Multiply colour by alpha.

    cmu-graphics hands image bytes to its renderer untouched, and that renderer
    composites them as *premultiplied* alpha. Feeding it ordinary straight-alpha
    PNG data makes every translucent pixel render at full strength - a 5%-alpha
    white grain overlay comes out as solid white noise. Everything built here
    goes through this function on the way in.
    """
    arr = np.asarray(pil.convert('RGBA'))
    alpha = arr[..., 3]

    lo = int(alpha.min())
    if lo == 255:
        # Fully opaque: premultiplying is the identity, and skipping it saves
        # ~17 ms on a full chamber bake.
        return pil if pil.mode == 'RGBA' else Image.fromarray(arr, 'RGBA')

    out = np.empty(arr.shape, dtype=np.uint8)
    out[..., 3] = alpha
    if lo == 0 and int(alpha.max()) == 255 and not ((alpha > 0) & (alpha < 255)).any():
        # Binary alpha (masked layers such as the wall bake): a multiply by
        # 0 or 1 is all that is needed.
        keep = (alpha == 255)[..., None]
        out[..., :3] = arr[..., :3] * keep
        return Image.fromarray(out, 'RGBA')

    wide = arr[..., :3].astype(np.uint16)
    out[..., :3] = ((wide * alpha[..., None].astype(np.uint16) + 127) // 255
                    ).astype(np.uint8)
    return Image.fromarray(out, 'RGBA')


_TRACE = bool(os.environ.get('LUMEN_TRACE_ART'))


def _store(key, pil, keep_source=True):
    """Publish a PIL image as a sprite for whichever backend is drawing.

    On the GPU backend that is a `gpu.Sprite`, which holds the premultiplied
    bytes and uploads itself to a texture the first time it is drawn - sprites
    are baked while the game is still starting, before a renderer exists.

    On the cmu-graphics backend it is a `CMUImage`, whose PIL -> renderer
    conversion is forced here rather than deferred to the first `drawImage`,
    to keep that cost out of the first frame. Once converted the wrapper's own
    copies are dead weight - the renderer only ever reads the converted image
    from its internal table - so they are released. A chamber layer is 8 MB a
    copy, and this is the difference between holding it four times and twice.
    """
    started = time.perf_counter()
    if gpu.wanted():
        img = gpu.sprite_from_pil(premultiply(pil))
    else:
        img = CMUImage(premultiply(pil))
        converted = False
        try:
            getImageSize(img)
            converted = True
        except Exception:
            pass
        if converted:
            try:
                img.image = None
                img._imageParams = None
            except Exception:
                pass
    if keep_source:
        _pil_cache[key] = pil
    _cache[key] = img
    _dims[id(img)] = (pil.size[0] / SCALE, pil.size[1] / SCALE)
    if _TRACE:
        ms = (time.perf_counter() - started) * 1000.0
        sys.stderr.write(f'[art] built {key!r} in {ms:.1f}ms\n')
    return img


def rgb_tuple(color):
    """Accept either a plain (r, g, b) tuple or a cmu-graphics `rgb` object.

    The palette stores `rgb` objects because that is what the drawing calls
    want; the sprite generators need raw numbers. Normalising here means
    callers never have to care which they are holding.
    """
    if isinstance(color, tuple):
        return color
    return (color.red, color.green, color.blue)


# Ordered dither, added to alpha before it is rounded to 8 bits. A glow is a
# gradient hundreds of pixels wide and 8-bit alpha has 256 steps to spend on
# it, so its faint tail lands in plateaus - measured, up to 16 pixels of
# identical alpha at a time, which is a visible ring. Half a level of dither
# turns each of those boundaries into a blend between the two levels either
# side. The pattern is a 8x8 Bayer matrix rather than random noise because it
# averages out evenly and does not shimmer when the sprite is scaled.
_BAYER8 = np.array([
    [0, 32, 8, 40, 2, 34, 10, 42], [48, 16, 56, 24, 50, 18, 58, 26],
    [12, 44, 4, 36, 14, 46, 6, 38], [60, 28, 52, 20, 62, 30, 54, 22],
    [3, 35, 11, 43, 1, 33, 9, 41], [51, 19, 59, 27, 49, 17, 57, 25],
    [15, 47, 7, 39, 13, 45, 5, 37], [63, 31, 55, 23, 61, 29, 53, 21],
], dtype=np.float32)
# Rather more than the half-level that would just break the boundary between
# two adjacent values. The lantern's tail is so nearly flat that half a level
# still leaves plateaus ten pixels wide; a little over one level spreads each
# transition properly. It reads as no noise at all against the film grain that
# is already on the frame.
_DITHER_LEVELS = 2.4
# Kept in 0..1 as well: the frame-wide dither tile needs the raw pattern, not
# the signed version the sprite bakes use.
_BAYER_RAW = (_BAYER8 + 0.5) / 64.0
_BAYER8 = (_BAYER_RAW - 0.5) * _DITHER_LEVELS


def _dither(shape):
    h, w = shape
    return np.tile(_BAYER8, (h // 8 + 1, w // 8 + 1))[:h, :w]


def _rgba(arr_rgb, alpha, dither=True):
    """Compose float32 colour planes + alpha plane into a PIL RGBA image."""
    h, w = alpha.shape
    out = np.empty((h, w, 4), dtype=np.uint8)
    out[..., 0] = np.clip(arr_rgb[0], 0, 255).astype(np.uint8)
    out[..., 1] = np.clip(arr_rgb[1], 0, 255).astype(np.uint8)
    out[..., 2] = np.clip(arr_rgb[2], 0, 255).astype(np.uint8)
    a = alpha * 255.0
    if dither:
        a = a + _dither(alpha.shape)
    out[..., 3] = np.clip(a, 0, 255).astype(np.uint8)
    return Image.fromarray(out, 'RGBA')


# --------------------------------------------------------------------------
# Glows - the workhorse of the whole look
# --------------------------------------------------------------------------
def glow(color, size, power=2.2, core=0.0):
    """A soft radial glow sprite, `size` px square, centred.

    `power` controls falloff sharpness; `core` adds a solid bright centre.
    """
    color = rgb_tuple(color)
    size = int(size)
    key = ('glow', color, size, round(power, 2), round(core, 2))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    raster = px(size)
    a = noise.radial_falloff(raster, power)
    if core > 0.0:
        a = np.clip(a + noise.radial_falloff(raster, 1.0, inner=1.0 - core) * 1.4, 0.0, 1.0)
    r, g, b = color
    return _store(key, _rgba((np.full(a.shape, r, np.float32),
                              np.full(a.shape, g, np.float32),
                              np.full(a.shape, b, np.float32)), a))


def two_tone_glow(inner_color, outer_color, size, power=2.2):
    """A glow whose centre and rim differ - reads as a hot core in a halo."""
    inner_color = rgb_tuple(inner_color)
    outer_color = rgb_tuple(outer_color)
    size = int(size)
    key = ('glow2', inner_color, outer_color, size, round(power, 2))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    a = noise.radial_falloff(px(size), power)
    t = np.clip(a * 1.35, 0.0, 1.0)
    planes = []
    for i in range(3):
        planes.append(outer_color[i] + (inner_color[i] - outer_color[i]) * t)
    return _store(key, _rgba(planes, a))


def ring(color, size, thickness=0.16, softness=1.4):
    """A soft annulus, used for shockwaves and charge tells."""
    color = rgb_tuple(color)
    size = int(size)
    key = ('ring', color, size, round(thickness, 3), round(softness, 2))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    raster = px(size)
    axis = (np.arange(raster, dtype=np.float32) + 0.5) / raster * 2.0 - 1.0
    d = np.sqrt(axis[:, None] ** 2 + axis[None, :] ** 2)
    band = np.clip(1.0 - np.abs(d - (1.0 - thickness)) / max(thickness, 1e-4), 0.0, 1.0)
    a = np.power(band, softness, dtype=np.float32) * (d <= 1.05)
    r, g, b = color
    return _store(key, _rgba((np.full(a.shape, r, np.float32),
                              np.full(a.shape, g, np.float32),
                              np.full(a.shape, b, np.float32)), a))


def beam(color, length_px, width_px, softness=1.8):
    """A horizontal soft-edged beam sprite, drawn rotated by the caller."""
    color = rgb_tuple(color)
    key = ('beam', color, int(length_px), int(width_px), round(softness, 2))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    w, h = px(length_px), px(width_px)
    across = np.abs((np.arange(h, dtype=np.float32) + 0.5) / h * 2.0 - 1.0)
    along = (np.arange(w, dtype=np.float32) + 0.5) / w
    prof = np.power(1.0 - across, softness, dtype=np.float32)[:, None]
    taper = np.clip(np.sin(along * math.pi) * 1.3, 0.0, 1.0)[None, :]
    a = prof * taper
    r, g, b = color
    return _store(key, _rgba((np.full(a.shape, r, np.float32),
                              np.full(a.shape, g, np.float32),
                              np.full(a.shape, b, np.float32)), a))


GLOW_BASE = 256


def lantern_glow(color, size=GLOW_BASE):
    """The lantern's own falloff profile.

    A single power curve either reads as a hard spotlight (low exponent) or a
    tiny bright dot (high one). Summing three terms gives what a flame in a
    dark room actually looks like: a hot core, a broad usable pool, and a long
    tail that fades to nothing instead of ending at a visible rim.

    Every exponent is above 1, which is what makes the tail smooth. A term
    near f**1 falls off linearly, so it still has slope where it reaches the
    edge of the sprite and stops - and a falloff that stops with slope left in
    it draws a circle. That was fine when the glow was a wash under the floor;
    once it became the light buffer itself, its shape is on screen directly
    and the circle showed.
    """
    key = ('lantern', rgb_tuple(color), int(size))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    f = noise.radial_falloff(px(size), 1.0)
    a = np.clip(0.44 * f ** 1.7 + 0.34 * f ** 3.2 + 0.26 * f ** 6.5, 0.0, 1.0)
    r, g, b = rgb_tuple(color)
    return _store(key, _rgba((np.full(a.shape, r, np.float32),
                              np.full(a.shape, g, np.float32),
                              np.full(a.shape, b, np.float32)), a))


def scorch(seed, size=110):
    """A burn left on the floor, as an irregular sooty blotch.

    Two things make a decal look painted on rather than left behind: a round
    outline, and an edge that stops. This one is a radial falloff pushed
    around by low-frequency noise, so its outline wanders, and its alpha runs
    to nothing well before the sprite does.
    """
    key = ('scorch', int(seed) % 16, int(size))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    n = px(size)
    rng = np.random.default_rng(int(seed) % 16 + 7717)
    f = noise.radial_falloff(n, 1.0)
    warp = noise.fbm(n, n, 3, 5, rng, tileable=False)
    grit = noise.fbm(n, n, 4, 17, rng, tileable=False)
    # The outline wanders by up to a third of the radius.
    edge = np.clip(f * (0.72 + 0.55 * warp) - 0.18, 0.0, 1.0)
    a = np.clip(edge ** 1.5 * (0.55 + 0.65 * grit), 0.0, 1.0) * 0.92
    v = np.clip(0.18 + 0.42 * grit * edge, 0.0, 1.0)
    r = np.clip(26.0 + v * 46.0, 0, 255)
    g = np.clip(20.0 + v * 34.0, 0, 255)
    b = np.clip(20.0 + v * 30.0, 0, 255)
    return _store(key, _rgba((r.astype(np.float32), g.astype(np.float32),
                              b.astype(np.float32)), a.astype(np.float32)))


def scorch_normal(seed, size=110):
    """The same burn as a surface: soot sits in a shallow hollow."""
    key = ('scorchn', int(seed) % 16, int(size))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    n = px(size)
    rng = np.random.default_rng(int(seed) % 16 + 7717)
    f = noise.radial_falloff(n, 1.0)
    warp = noise.fbm(n, n, 3, 5, rng, tileable=False)
    grit = noise.fbm(n, n, 4, 17, rng, tileable=False)
    edge = np.clip(f * (0.72 + 0.55 * warp) - 0.18, 0.0, 1.0)
    a = np.clip(edge ** 1.5 * (0.55 + 0.65 * grit), 0.0, 1.0) * 0.92
    # Height falls into the middle of the burn, so the lantern catches its
    # far rim and loses its near one, the way a scoop out of stone does.
    height = np.clip(1.0 - edge * (0.75 + 0.45 * grit), 0.0, 1.0)
    h255 = (height * 255.0).astype(np.uint8)
    rgba = np.dstack([h255, h255, h255,
                      (a * 255.0).astype(np.uint8)])
    nm = normal_map(Image.fromarray(rgba, 'RGBA'), 1.5)
    return _store(key, nm)


def normal_map(pil, strength=1.0):
    """A surface normal for every pixel, read out of the art's own shading.

    The stone was drawn with its mortar courses and its blotches already in
    it, and those are exactly the places the surface is not flat. Rather than
    author a second set of art by hand, the height is taken to be the
    luminance that is already there and the normal is its slope - which is the
    oldest trick there is for this, and it works because the art was drawn as
    if lit from nowhere in particular, so its light and dark *are* its relief.

    The result is a normal in the usual packing (a flat surface is
    (0.5, 0.5, 1)), carrying the source's alpha so that a layer only claims
    the pixels it actually covers.
    """
    rgba = pil.convert('RGBA')
    a = np.asarray(rgba, dtype=np.float32)
    lum = (a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722) / 255.0
    # A wide slope, not a one-pixel difference: at native scale the art is
    # already several pixels per design unit, and a tight kernel picks up the
    # bake's own noise instead of the shapes drawn into it.
    k = 2
    gx = np.zeros_like(lum)
    gy = np.zeros_like(lum)
    gx[:, k:-k] = lum[:, 2 * k:] - lum[:, :-2 * k]
    gy[k:-k, :] = lum[2 * k:, :] - lum[:-2 * k, :]
    scale = 3.4 * float(strength)
    nx = -gx * scale
    ny = -gy * scale
    nz = np.ones_like(nx)
    inv = 1.0 / np.sqrt(nx * nx + ny * ny + nz * nz)
    out = np.empty(a.shape, dtype=np.uint8)
    out[..., 0] = np.clip((nx * inv) * 127.5 + 127.5, 0, 255)
    out[..., 1] = np.clip((ny * inv) * 127.5 + 127.5, 0, 255)
    out[..., 2] = np.clip((nz * inv) * 127.5 + 127.5, 0, 255)
    out[..., 3] = a[..., 3]
    return Image.fromarray(out, 'RGBA')


def dither_tile(size=256):
    """A tile of ordered dither, laid over the finished frame.

    Dithering the sprites is only half the job. A glow is drawn into an 8-bit
    light buffer, multiplied by an 8-bit albedo and added back at a fraction -
    and every one of those steps rounds again. Worse, the fractional add
    *shrinks* whatever dither survived: scaled by a third, a dither of one
    level becomes a third of one and stops breaking anything.

    So the frame gets a last pass of its own. It cannot recover detail that
    was already rounded away, but it makes the boundary between two output
    levels ragged instead of a clean contour, and a ragged boundary is not a
    ring. Two levels is enough to do that and stays under the film grain.
    """
    key = ('dither', size)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    tile = np.tile(_BAYER_RAW, (size // 8 + 1, size // 8 + 1))[:size, :size]
    values = np.floor(tile * 2.999).astype(np.float32)
    alpha = np.ones((size, size), np.float32)
    return _store(key, _rgba((values, values, values), alpha, dither=False),
                  keep_source=False)


# Where the lit edge itself sits inside the profile below, as a fraction of
# its depth: a quarter of it spills forward onto the floor, the rest carries
# back across the stone.
# The cross-section of light lying on a wall, split either side of the lit
# edge itself. What is in front spills onto the floor; what is behind is the
# wall's top surface, and a wall is a whole tile thick - so sizing that part
# in tiles rather than in a fixed handful of units is the difference between
# seeing the top of a wall and seeing a bright line with blackness above it.
# Measured from the brightest line, which sits on the arris where the wall's
# side face meets its top - the same place the brightest line on a real lit
# block is. In front of that is the face itself (a tile's worth of vertical
# stone, lit but falling away) and then a little spill onto the floor; behind
# it is the top, reaching about a tile back.
EDGE_LIGHT_FLOOR = 23.5                 # face + floor spill, ahead of the arris
EDGE_LIGHT_STONE = 45.0                 # the top, behind it
EDGE_LIGHT_DEPTH = EDGE_LIGHT_FLOOR + EDGE_LIGHT_STONE
EDGE_LIGHT_PEAK = EDGE_LIGHT_FLOOR / EDGE_LIGHT_DEPTH
# Tall enough that the tail has a sample every half design unit at native
# scale; the texture is stretched across `EDGE_LIGHT_DEPTH` units.
_EDGE_LIGHT_DEPTH = 256


def edge_light():
    """The cross-section of light lying on a lit wall edge.

    One texture, stretched and rotated along each piece of edge, in place of
    the half-dozen stacked strokes this used to take. Strokes give a stepped
    profile with a hard line at the top of each one, which against the soft
    falloff everything else now has read as drawn-on rather than lit. A
    gradient stretched across the same depth is smooth by construction, and
    one quad instead of six.
    """
    key = ('edgelight',)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    n = _EDGE_LIGHT_DEPTH
    t = np.linspace(0.0, 1.0, n, dtype=np.float32)
    peak = EDGE_LIGHT_PEAK
    a = np.empty(n, dtype=np.float32)
    front = t < peak
    # Across the face and onto the floor in front of it. Gentle rather than
    # steep: the face is a real surface a tile high, and a steep ramp leaves
    # it black with a bright line on top of it.
    a[front] = np.clip(t[front] / peak, 0.0, 1.0) ** 1.45
    # Into the stone: a long tail, so the wall face is lit and not just its
    # corner.
    back = ~front
    # Across the wall top. Two terms: a bright shoulder just behind the edge,
    # where the stone is nearly facing the flame, and a long dim reach over
    # the rest of the tile so the top reads as a surface with light falling
    # across it rather than as a strip. A single exponent cannot do both -
    # low, and the light runs off the far side of the wall; high, and it is
    # back to a line at the edge.
    u = np.clip(1.0 - (t[back] - peak) / (1.0 - peak), 0.0, 1.0)
    a[back] = np.clip(0.62 * u ** 3.4 + 0.38 * u ** 1.25, 0.0, 1.0)
    plane = np.repeat(a[:, None], 4, axis=1)
    white = np.full(plane.shape, 255.0, np.float32)
    return _store(key, _rgba((white, white, white), plane), keep_source=False)


# Largest lantern sprite kept at its exact size. Anything bigger (a flare at
# full stretch, say) falls back to scaling a smaller one, which costs about a
# millisecond but only for the moment it is on screen.
LANTERN_EXACT_MAX = 900


def draw_lantern(color, cx, cy, radius, opacity, height=0.0):
    """The lantern's pool of light.

    Where the renderer can evaluate the falloff per pixel it does, and the
    result is exact: no bake, no 8-bit alpha, no resample from one baked size
    to another, and so no rings at any radius. The sprite path below is what
    the SDL renderer still uses, and what CLASSIC visuals ask for.
    """
    if radius <= 1.0 or opacity <= 0:
        return
    if getattr(gpu, 'ANALYTIC_LIGHTS', False) and gpu.active():
        gpu.radial_glow(cx * draw.SCALE, cy * draw.SCALE, radius * draw.SCALE,
                        rgb_tuple(color), min(100, int(opacity)),
                        profile=gpu.LANTERN_PROFILE,
                        height=height * draw.SCALE)
        return
    size = int(radius * 2.0)
    # The *size* is quantised, because only baked sizes may be used. The
    # position must not be: rounding it to whole design units moved the glow
    # in ~3-pixel steps at native scale while everything else moved smoothly,
    # which read as the whole screen juddering whenever the player walked.
    left = cx - radius
    top = cy - radius
    alpha = min(100, int(opacity))

    # Only ever use a sprite that already exists. Baking one here would put a
    # 4-7 ms rasterise inside the frame, which is exactly what a lantern flare
    # used to do as its radius swept down through unbaked sizes.
    if size <= LANTERN_EXACT_MAX:
        sprite = _cache.get(('lantern', rgb_tuple(color), size))
        if sprite is not None:
            drawImage(sprite, left, top, opacity=alpha)
            return

    d = float(size)
    drawImage(lantern_glow(color, GLOW_BASE), left, top, width=d, height=d,
              opacity=alpha)


def prewarm_lantern(color, radii):
    """Bake the lantern sizes a run will actually use, up front."""
    for radius in radii:
        size = int(radius * 2.0)
        if 0 < size <= LANTERN_EXACT_MAX:
            lantern_glow(color, size)


def draw_glow(color, cx, cy, radius, opacity, power=2.2, core=0.0,
              height=0.0):
    """Draw a radial glow of arbitrary radius from one cached sprite.

    Rasterising a fresh sprite for every radius is what a naive implementation
    does, and it is ruinous: a 1200px glow costs ~13 ms to build and over a
    megabyte to keep, and the lantern's radius changes continuously as fuel
    burns. Measured, the renderer scales a 256px sprite to 1200px in 0.08 ms -
    the same as an exact-size blit - so one sprite serves every size.
    """
    if radius <= 1.0 or opacity <= 0:
        return
    if getattr(gpu, 'ANALYTIC_LIGHTS', False) and gpu.active():
        # Same reasoning as the lantern: a glow stretched from one 256px
        # sprite to twelve hundred is a resampled 8-bit ramp, and every light
        # in the game is one of these.
        gpu.radial_glow(cx * draw.SCALE, cy * draw.SCALE, radius * draw.SCALE,
                        rgb_tuple(color), min(100, int(opacity)),
                        profile=gpu.POWER_PROFILE, power=power, core=core,
                        height=height * draw.SCALE)
        return
    # Size is quantised so one cached sprite serves many radii; the position
    # is not - rounding it to whole design units makes a moving light judder
    # against everything drawn at exact coordinates. See `draw_lantern`.
    d = float(int(radius * 2.0))
    left = cx - radius
    top = cy - radius
    drawImage(glow(color, GLOW_BASE, power, core), left, top,
              width=d, height=d, opacity=min(100, int(opacity)))


def draw_ring(color, cx, cy, radius, opacity, thickness=0.16, softness=1.4):
    """Same trick for annuli - charge tells, shockwaves, boss telegraphs."""
    if radius <= 1.0 or opacity <= 0:
        return
    d = float(int(radius * 2.0))
    drawImage(ring(color, GLOW_BASE, thickness, softness),
              float(int(cx - radius)), float(int(cy - radius)),
              width=d, height=d, opacity=min(100, int(opacity)))


# --------------------------------------------------------------------------
# Full-screen overlays
# --------------------------------------------------------------------------
def _vignette_pil(width, height, strength, radius, tint):
    width, height = px(width), px(height)
    ys = (np.arange(height, dtype=np.float32) + 0.5) / height * 2.0 - 1.0
    xs = (np.arange(width, dtype=np.float32) + 0.5) / width * 2.0 - 1.0
    d = np.sqrt((xs[None, :] * 1.02) ** 2 + (ys[:, None] * 1.16) ** 2)
    a = np.clip((d - radius) / max(1e-6, 1.45 - radius), 0.0, 1.0) ** 1.5 * strength
    r, g, b = tint
    return _rgba((np.full(a.shape, r, np.float32),
                  np.full(a.shape, g, np.float32),
                  np.full(a.shape, b, np.float32)), a)


def vignette(width, height, strength=0.92, radius=0.78, tint=(2, 4, 9)):
    key = ('vig', width, height, round(strength, 2), round(radius, 2), tint)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    return _store(key, _vignette_pil(width, height, strength, radius, tint))


def grain(width, height, amount=0.055, seed=11):
    """A static film-grain overlay. One blit, and it ties the palette together."""
    key = ('grain', width, height, round(amount, 3), seed)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    return _store(key, _grain_pil(width, height, amount, seed))


def _grain_pil(width, height, amount, seed):
    width, height = px(width), px(height)
    rng = np.random.default_rng(seed)
    n = rng.random((height, width)).astype(np.float32)
    a = np.abs(n - 0.5) * 2.0 * amount
    v = np.where(n > 0.5, 255.0, 0.0)
    return _rgba((v, v, v), a)


def scanlines(width, height, alpha=0.16, period=3):
    key = ('scan', width, height, round(alpha, 3), period)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    return _store(key, _scanlines_pil(width, height, alpha, period))


def _scanlines_pil(width, height, alpha, period):
    width, height = px(width), px(height)
    period = max(1, px(period))
    rows = ((np.arange(height) % period) == 0).astype(np.float32) * alpha
    a = np.repeat(rows[:, None], width, axis=1)
    z = np.zeros((height, width), np.float32)
    return _rgba((z, z, z), a)


def screen_overlay(width, height, vignette_strength=0.94, vignette_radius=0.6,
                   grain_amount=0.05, scan_alpha=0.1, scan_period=4):
    """Vignette, scanlines and grain composited into a single sprite.

    Each full-screen `drawImage` costs about 0.4 ms once the renderer's blit
    is counted - measured, not assumed - so three separate static overlays
    were over a millisecond a frame for something that never changes. They are
    flattened here into one.
    """
    key = ('overlay', width, height, round(vignette_strength, 2),
           round(vignette_radius, 2), round(grain_amount, 3),
           round(scan_alpha, 3), scan_period)
    hit = _cache.get(key)
    if hit is not None:
        return hit

    layers = [
        _pil_cache.get(('vig', width, height, round(vignette_strength, 2),
                        round(vignette_radius, 2), (2, 4, 9)))
        or _vignette_pil(width, height, vignette_strength, vignette_radius,
                         (2, 4, 9)),
        _scanlines_pil(width, height, scan_alpha, scan_period),
        _grain_pil(width, height, grain_amount, 11),
    ]
    out = layers[0]
    for layer in layers[1:]:
        out = Image.alpha_composite(out, layer)
    return _store(key, out)


def darkness(width, height, level=0.86, tint=(3, 5, 11)):
    """A flat wash used to sink everything the lantern does not reach."""
    key = ('dark', width, height, round(level, 3), tint)
    hit = _cache.get(key)
    if hit is not None:
        return hit
    a = np.full((px(height), px(width)), level, np.float32)
    r, g, b = tint
    return _store(key, _rgba((np.full(a.shape, r, np.float32),
                              np.full(a.shape, g, np.float32),
                              np.full(a.shape, b, np.float32)), a))


# --------------------------------------------------------------------------
# Text baked to sprites - crisper and far cheaper than many drawLabel calls
# --------------------------------------------------------------------------
def text_sprite(message, role, size, color, tracking=0, glow_color=None,
                glow_radius=0, bold=False):
    key = ('text', message, role, size, color, tracking, glow_color,
           glow_radius, bold)
    hit = _cache.get(key)
    if hit is not None:
        return hit

    f = font(role, px(size), bold=bold)
    tracking = px(tracking) if tracking else 0
    glow_radius = px(glow_radius) if glow_radius else 0
    spaced = message
    if tracking:
        spaced = (' ' * 0).join(message)

    probe = Image.new('RGBA', (8, 8))
    d = ImageDraw.Draw(probe)
    if tracking:
        widths = [d.textlength(ch, font=f) for ch in message]
        total_w = int(sum(widths) + tracking * max(0, len(message) - 1)) + 8
    else:
        total_w = int(d.textlength(spaced, font=f)) + 8
    ascent, descent = f.getmetrics() if hasattr(f, 'getmetrics') else (size, size // 4)
    total_h = ascent + descent + 8

    pad = glow_radius * 2 + 4 + (2 if bold else 0)   # room for a bolder face
    img = Image.new('RGBA', (total_w + pad * 2, total_h + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # A real bold face does the work. Only if the role has none does this
    # fall back to a hairline stroke, and a hairline is all it should ever be:
    # anything heavier closes the counters and runs the letters together.
    stroke = 1 if (bold and not has_bold_face(role)) else 0
    extra = dict(stroke_width=stroke, stroke_fill=color + (255,)) if stroke else {}
    if tracking:
        x = float(pad + 4)
        for ch in message:
            d.text((x, pad + 4), ch, font=f, fill=color + (255,), **extra)
            x += d.textlength(ch, font=f) + tracking
    else:
        d.text((pad + 4, pad + 4), spaced, font=f, fill=color + (255,), **extra)

    if glow_color and glow_radius:
        halo = img.copy()
        halo = halo.filter(ImageFilter.GaussianBlur(glow_radius))
        tinted = Image.new('RGBA', img.size, glow_color + (0,))
        tinted.putalpha(halo.getchannel('A').point(lambda v: min(255, int(v * 2.1))))
        out = Image.alpha_composite(tinted, img)
        img = out

    sprite = _store(key, img)
    _insets[id(sprite)] = ((pad + 4) / SCALE, (pad + 4) / SCALE)
    return sprite


# Baked short strings (damage numbers, HUD labels). Values repeat constantly -
# the same weapon hitting the same enemy prints the same number over and over -
# so a small cache turns each one into a single blit instead of a `drawLabel`,
# which costs a shape plus several font-face selections inside the library.
_LABEL_CACHE = {}
_LABEL_CACHE_MAX = 512


def label_sprite(message, role, size, color, bold=False):
    key = (message, role, int(size), color, bold)
    hit = _LABEL_CACHE.get(key)
    if hit is not None:
        return hit
    if len(_LABEL_CACHE) >= _LABEL_CACHE_MAX:
        _LABEL_CACHE.clear()
    sprite = text_sprite(message, role, int(size), color, bold=bold)
    entry = (sprite, sprite_size(sprite))
    _LABEL_CACHE[key] = entry
    return entry


def draw_label_sprite(message, x, y, role, size, color, align='center',
                      opacity=100):
    """Blit baked text, aligned on the glyphs rather than the sprite's padding."""
    sprite, (sw, sh) = label_sprite(message, role, size, color)
    inset = _insets.get(id(sprite), (0, 0))[0]
    if align == 'center':
        left = x - sw * 0.5
    elif align == 'right':
        left = x - sw + inset
    else:
        left = x - inset
    drawImage(sprite, int(left), int(y - sh * 0.5),
              opacity=max(0, min(100, int(opacity))))


def sprite_size(sprite):
    """Design-unit dimensions of an already-built sprite."""
    return _dims.get(id(sprite), (0, 0))


def sprite_pixel_size(sprite):
    """The sprite's real pixel dimensions, whatever the render scale is."""
    if isinstance(sprite, gpu.Sprite):
        return sprite.size
    w, h = _dims.get(id(sprite), (0, 0))
    return (w * SCALE, h * SCALE)


def label_inset(sprite):
    """Where the glyphs start inside a baked text sprite's padding, in pixels."""
    return _insets.get(id(sprite), (0, 0))[0] * SCALE


def text_size(message, role, size, tracking=0):
    """Pixel dimensions the matching `text_sprite` will occupy."""
    img = _pil_cache.get(('text', message, role, size))
    if img is not None:
        return img.size
    f = font(role, size)
    d = ImageDraw.Draw(Image.new('RGBA', (8, 8)))
    if tracking:
        w = sum(d.textlength(ch, font=f) for ch in message) + tracking * max(0, len(message) - 1)
    else:
        w = d.textlength(message, font=f)
    ascent, descent = f.getmetrics() if hasattr(f, 'getmetrics') else (size, size // 4)
    return int(w), ascent + descent


# --------------------------------------------------------------------------
# Stone
# --------------------------------------------------------------------------
FLOOR_TILE_VARIANTS = 5
WALL_TILE_VARIANTS = 4


def floor_tile(seed, size=256):
    """A seamless stone floor tile.

    Seeds are folded into a small fixed set: a chamber's identity comes from
    its layout, stains and sigils, not from re-deriving stone grain every
    floor, and generating these is ~9 ms each.
    """
    seed = int(seed) % FLOOR_TILE_VARIANTS
    key = ('floor', seed, size)
    hit = _pil_cache.get(key)
    if hit is not None:
        return hit
    size = px(size)
    rng = np.random.default_rng(seed)
    # Mostly high-frequency grit. Low-frequency noise at any real amplitude
    # makes stone read as marble or water, so it stays as a faint undertone.
    base = noise.fbm(size, size, 3, 5, rng, tileable=True)
    grit = noise.fbm(size, size, 3, 22, rng, tileable=True)
    speck = noise.value_noise(size, size, 64, rng, tileable=True)
    crack = noise.ridged(size, size, 2, 7, rng, tileable=True)
    crack = np.clip((crack - 0.86) / 0.14, 0.0, 1.0)

    v = 0.25 * base + 0.5 * grit + 0.25 * speck
    r = 9.0 + v * 10.0
    g = 12.0 + v * 12.0
    b = 20.0 + v * 16.0
    r -= crack * 5.0
    g -= crack * 6.0
    b -= crack * 9.0

    out = np.empty((size, size, 4), np.uint8)
    out[..., 0] = np.clip(r, 0, 255).astype(np.uint8)
    out[..., 1] = np.clip(g, 0, 255).astype(np.uint8)
    out[..., 2] = np.clip(b, 0, 255).astype(np.uint8)
    out[..., 3] = 255
    img = Image.fromarray(out, 'RGBA')
    _pil_cache[key] = img
    return img


def wall_tile(seed, size=128):
    """A seamless tile for wall tops: courses of laid stone, not a noise field.

    What was here before was two octaves of fbm and nothing else, so a wall
    came out as an evenly speckled slab with a line drawn round it - which is
    what made a room read as a set of cardboard boxes rather than as masonry.
    Stone laid by hand has courses, staggered joints, and blocks that differ
    from their neighbours, and it is those three things the eye uses to tell
    stone from paper.

    Each block also gets a lit top arris and a shaded bottom one. That reads
    as relief by itself, and it feeds `normal_map`, so the lantern rakes
    across the individual stones as the player walks past them.
    """
    seed = int(seed) % WALL_TILE_VARIANTS
    key = ('walltile', seed, size)
    hit = _pil_cache.get(key)
    if hit is not None:
        return hit
    n = px(size)
    rng = np.random.default_rng(seed)

    # Both counts must be even, or the half-block stagger does not line up
    # with itself across the seam and the courses jog where tiles meet.
    rows, cols = 6, 4
    ch, cw = n / rows, n / cols
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32)
    row = np.floor(yy / ch)
    stagger = (row % 2.0) * (cw * 0.5)
    sx = np.mod(xx + stagger, float(n))
    col = np.floor(sx / cw)

    # Position within a block, 0..1 on each axis.
    fy = np.mod(yy / ch, 1.0)
    fx = np.mod(sx / cw, 1.0)

    # A per-block value that is stable for the block and unrelated to its
    # neighbours, so no two beside each other read as the same stone.
    h = np.mod(row * 131.0 + col * 977.0, 1013.0)
    tone = np.mod(h * 0.61803399, 1.0) - 0.5

    base = noise.fbm(n, n, 3, 4, rng, tileable=True)
    grit = noise.fbm(n, n, 4, 22, rng, tileable=True)
    v = 0.40 * base + 0.60 * grit

    joint_y = ch * 0.055 / ch          # mortar, as a fraction of a block
    joint_x = cw * 0.055 / cw
    mortar = np.minimum(np.minimum(fy / joint_y, (1.0 - fy) / joint_y),
                        np.minimum(fx / joint_x, (1.0 - fx) / joint_x))
    mortar = np.clip(mortar, 0.0, 1.0)          # 0 in the joint, 1 in the face

    # The arrises: bright where the stone turns up into the light, dark where
    # it turns away. Narrow, or the blocks look inflated rather than cut.
    lip = np.clip(1.0 - (fy - joint_y) / 0.20, 0.0, 1.0) * (fy > joint_y)
    heel = np.clip((fy - 0.80) / 0.20, 0.0, 1.0)

    shade = (0.50 + 0.34 * v                     # the stone's own mottling
             + 0.30 * tone                       # this block against the next
             + 0.30 * lip - 0.30 * heel)         # cut edges
    shade = shade * (0.34 + 0.66 * mortar)       # sink the joints
    shade = np.clip(shade, 0.0, 1.35)

    out = np.empty((n, n, 4), np.uint8)
    # Cooler in the joints, warmer on the faces, which is what damp stone
    # does and what keeps a wall from being one flat hue.
    warm = 0.35 + 0.65 * mortar
    out[..., 0] = np.clip(30.0 + shade * 84.0 * (0.82 + 0.30 * warm), 0, 255)
    out[..., 1] = np.clip(38.0 + shade * 88.0 * (0.86 + 0.22 * warm), 0, 255)
    out[..., 2] = np.clip(54.0 + shade * 92.0, 0, 255)
    out[..., 3] = 255
    img = Image.fromarray(out, 'RGBA')
    _pil_cache[key] = img
    return img


def wrap(pil, key, keep_source=False):
    """Publish an already-built PIL image as a drawable sprite.

    Level layers do not keep their source: nothing reads it back, and at
    1792x1152 it is 8 MB apiece.
    """
    return _store(key, pil, keep_source=keep_source)


def cached(key):
    return _cache.get(key)


def _release(sprite):
    """Free every copy of a sprite's pixels.

    A chamber layer is 1792x1152 and ends up held four times over: the source
    PIL image, the premultiplied one inside the wrapper, the wrapper's cached
    byte buffer, and the converted image in cmu-graphics' own `activeDrawing`
    table - which is keyed by a per-wrapper uuid and never evicted. Dropping
    only our reference leaks about 66 MB per floor, so the library's entry has
    to go too.
    """
    if isinstance(sprite, gpu.Sprite):
        sprite.release()
        return
    try:
        from cmu_graphics import shape_logic
        drawing = getattr(shape_logic, 'activeDrawing', None)
        images = getattr(drawing, 'images', None)
        uid = getattr(sprite, 'uuid', None)
        if images is not None and uid is not None:
            images.pop(uid, None)
        sprite._imageParams = None
        sprite.image = None
    except Exception:
        pass


# Bumped every time the cache is emptied. Anything holding a baked sprite can
# compare against this to find out that its copy is void.
GENERATION = 0


def clear_all():
    """Drop every cached sprite; used when the render scale changes."""
    global GENERATION
    GENERATION += 1
    for sprite in list(_cache.values()):
        _release(sprite)
    _cache.clear()
    _pil_cache.clear()
    _dims.clear()
    _insets.clear()
    _LABEL_CACHE.clear()
    _font_cache.clear()


def clear_level_cache():
    """Drop baked level images between floors so memory stays flat."""
    for key in [k for k in _cache if isinstance(k, tuple) and k and k[0] == 'level']:
        stale = _cache.pop(key, None)
        _pil_cache.pop(key, None)
        if stale is not None:
            _dims.pop(id(stale), None)
            _insets.pop(id(stale), None)
            _release(stale)
