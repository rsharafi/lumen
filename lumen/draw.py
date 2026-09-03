"""Drawing at a render scale, without a canvas transform.

The game is authored against a 720-unit-tall view. To fill a high-DPI display
sharply, every coordinate and size is multiplied on its way into the drawing
call, and every sprite is rasterised at the matching pixel size (see
`art.set_scale`). The canvas transform is left at identity.

Everything draws through here rather than reaching into `glx` directly, which
keeps the design-units-to-pixels multiply in exactly one place. There used to
be a second reason - three renderers hid behind these four functions - and
every call carried a branch to pick between them. There is one renderer now,
so the branches are gone and these are thin.
"""

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
    pts = [(coords[i] * s, coords[i + 1] * s)
           for i in range(0, len(coords) - 1, 2)]
    width = kwargs.get('borderWidth') or 1.0
    return gpu.polygon(pts, fill=_rgb(kwargs.get('fill')),
                       opacity=kwargs.get('opacity'),
                       border=_rgb(kwargs.get('border')),
                       border_width=width * s)


def drawImage(image, left, top, **kwargs):
    # Sub-pixel destinations, deliberately. The GPU samples between texels for
    # free, so a scrolling layer advances smoothly; snapping it to whole
    # pixels instead makes the world stutter against everything drawn at exact
    # coordinates, which is very visible at 120 fps.
    s = SCALE
    return gpu.blit(image, left * s, top * s,
                    kwargs['width'] * s if 'width' in kwargs else None,
                    kwargs['height'] * s if 'height' in kwargs else None,
                    kwargs.get('opacity'))


def drawLine(x1, y1, x2, y2, **kwargs):
    s = SCALE
    return gpu.line(x1 * s, y1 * s, x2 * s, y2 * s,
                    fill=_rgb(kwargs.get('fill')),
                    width=(kwargs.get('lineWidth') or 1.0) * s,
                    opacity=kwargs.get('opacity'))


def drawLabel(value, cx, cy, size=12, fill=None, font='Menlo', align='center',
              opacity=None, bold=False, **_ignored):
    """Text as a baked sprite.

    There is no glyph rasteriser on this backend, and there should not be one:
    the game already bakes text everywhere it matters, and a baked label is a
    single textured quad. Sprites are cached by content, so repeated strings -
    a damage number, a HUD readout - are built once.
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
