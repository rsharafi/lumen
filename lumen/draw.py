"""Drawing at a render scale, without a canvas transform.

The game is authored against a 720-unit-tall view. To fill a high-DPI display
sharply, every coordinate and size is multiplied on its way into the drawing
call, and every sprite is rasterised at the matching pixel size (see
`art.set_scale`). The canvas transform is left at identity.

That last part is the whole point on the cmu-graphics backend. Applying one
scale matrix to the canvas is far less code, but the renderer takes a
resampling path for every image and every fill while a transform is active:
measured, 1280x720 -> 1282x722 (0.3% more pixels) took the frame from 8.4 ms to
15.4 ms. With the transform gone, resolution is close to free at small sizes -
1800x1033 costs 8.35 ms against 8.25 ms for 1280x720 - because that renderer is
bound by per-shape overhead, not fill rate.

Every module draws through here rather than importing from cmu_graphics, which
is what makes the backend swappable: with `lumen/gpu.py` active these calls go
to SDL's renderer instead, and the rest of the game does not know the
difference. `LUMEN_RENDERER=cpu` puts cmu-graphics back.
"""

from cmu_graphics import drawImage as _drawImage
from cmu_graphics import drawLabel as _drawLabel
from cmu_graphics import drawLine as _drawLine
from cmu_graphics import drawPolygon as _drawPolygon

from . import gpu

SCALE = 1.0

# Font names -> the roles `art` bakes with. Only these two are ever used.
_ROLE = {'Copperplate': 'display', 'Menlo': 'ui'}


def set_scale(scale):
    global SCALE
    SCALE = float(scale) or 1.0


def _rgb(color):
    """A palette colour as plain (r, g, b), or None."""
    if color is None:
        return None
    if isinstance(color, tuple):
        return color
    return (color.red, color.green, color.blue)


def drawPolygon(*coords, **kwargs):
    s = SCALE
    if gpu.active():
        pts = [(coords[i] * s, coords[i + 1] * s)
               for i in range(0, len(coords) - 1, 2)]
        width = kwargs.get('borderWidth') or 1.0
        return gpu.polygon(pts, fill=_rgb(kwargs.get('fill')),
                           opacity=kwargs.get('opacity'),
                           border=_rgb(kwargs.get('border')),
                           border_width=width * s)
    if s == 1.0:
        return _drawPolygon(*coords, **kwargs)
    width = kwargs.get('borderWidth')
    if width:
        kwargs['borderWidth'] = width * s
    return _drawPolygon(*[c * s for c in coords], **kwargs)


def drawImage(image, left, top, **kwargs):
    s = SCALE
    if gpu.active():
        # Sub-pixel destinations, deliberately. The GPU samples between texels
        # for free, so a scrolling layer advances smoothly; snapping it to
        # whole pixels instead makes the world stutter against everything
        # drawn at exact coordinates, which is very visible at 120 fps.
        return gpu.blit(image, left * s, top * s,
                        kwargs['width'] * s if 'width' in kwargs else None,
                        kwargs['height'] * s if 'height' in kwargs else None,
                        kwargs.get('opacity'))
    if s != 1.0:
        # Whole pixels, always. The cmu-graphics renderer blits ~15x faster to
        # an integer destination and falls back to resampling otherwise, and
        # multiplying integer design coordinates by a fractional scale lands
        # every single image off-grid - which is exactly what made the first
        # attempt at this four times slower than the canvas transform it
        # replaced.
        left = float(int(left * s + 0.5))
        top = float(int(top * s + 0.5))
        if 'width' in kwargs:
            kwargs['width'] = float(int(kwargs['width'] * s + 0.5))
        if 'height' in kwargs:
            kwargs['height'] = float(int(kwargs['height'] * s + 0.5))
    if isinstance(image, gpu.Sprite):
        # A GPU sprite with no renderer to draw it: the backend was wanted and
        # then lost. Skipping beats raising mid-frame; the cache is emptied and
        # rebaked as CMUImages on the next resize.
        return None
    return _drawImage(image, left, top, **kwargs)


def drawLine(x1, y1, x2, y2, **kwargs):
    s = SCALE
    if gpu.active():
        return gpu.line(x1 * s, y1 * s, x2 * s, y2 * s,
                        fill=_rgb(kwargs.get('fill')),
                        width=(kwargs.get('lineWidth') or 1.0) * s,
                        opacity=kwargs.get('opacity'))
    if s != 1.0:
        x1 *= s
        y1 *= s
        x2 *= s
        y2 *= s
        width = kwargs.get('lineWidth')
        if width:
            kwargs['lineWidth'] = width * s
    return _drawLine(x1, y1, x2, y2, **kwargs)


def drawLabel(value, cx, cy, **kwargs):
    if gpu.active():
        return _gpu_label(value, cx, cy, **kwargs)
    s = SCALE
    if s != 1.0:
        cx *= s
        cy *= s
        size = kwargs.get('size')
        if size:
            kwargs['size'] = max(1.0, size * s)
        width = kwargs.get('borderWidth')
        if width:
            kwargs['borderWidth'] = width * s
    return _drawLabel(value, cx, cy, **kwargs)


def _gpu_label(value, cx, cy, size=12, fill=None, font='Menlo', align='center',
               opacity=None, bold=False, **_ignored):
    """Text as a baked sprite.

    There is no glyph rasteriser on this backend, and there should not be one:
    the game already bakes text everywhere it matters, and a baked label is a
    single textured quad where `drawLabel` cost a shape plus several font-face
    selections. Sprites are cached by content, so repeated strings - a damage
    number, a HUD readout - are built once.
    """
    from . import art
    role = _ROLE.get(font, 'ui')
    color = _rgb(fill) or (255, 255, 255)
    sprite, _design = art.label_sprite(str(value), role, int(size), color,
                                       bold=bool(bold))
    if sprite is None:
        return None
    s = SCALE
    x, y = cx * s, cy * s
    pw, ph = art.sprite_pixel_size(sprite)
    ix = art.label_inset(sprite)

    if align == 'center':
        left = x - pw * 0.5
        top = y - ph * 0.5
    elif align == 'right':
        left = x - pw + ix
        top = y - ph * 0.5
    elif align == 'left-top':
        left = x - ix
        top = y - ix
    else:                                   # 'left'
        left = x - ix
        top = y - ph * 0.5
    return gpu.blit(sprite, int(left + 0.5), int(top + 0.5), pw, ph, opacity)
