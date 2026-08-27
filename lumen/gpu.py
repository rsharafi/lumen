"""The same drawing, issued to the GPU instead of a CPU rasteriser.

cmu-graphics rasterises every frame on one core, and above cache size that is
memory-bandwidth bound: at 3600x2260 a frame touches a 32 MB buffer about eight
times over, which is ~55 GB/s at 120 Hz and about three times what a single
core delivers. Measured, a busy chamber costs 24.7 ms there. The identical
drawing issued to SDL's renderer costs **0.09 ms of CPU** and holds the vsync
period, with roughly forty times the workload still to spare.

Nothing about the game changes to use this. Every module already draws through
`lumen/draw.py`, so this is a backend swapped in behind an interface that was
already there, and `LUMEN_RENDERER=cpu` puts the old one back for comparison.

Two details are load-bearing:

* **Premultiplied alpha.** Every sprite the game bakes is premultiplied,
  because that is how cmu-graphics' renderer composites. SDL's default blend
  expects straight alpha, so textures use a custom blend mode composed to
  `src + dst * (1 - srcA)`. Fading one then needs *both* a colour modulation
  and an alpha modulation, since SDL's alpha mod scales only the alpha channel
  and premultiplied colour has to come down with it.
* **Geometry is not premultiplied.** `draw_color` is straight RGBA, so shapes
  are drawn under ordinary alpha blending, which is what cmu-graphics' own
  `opacity` means.
"""

import os
import sys

_renderer = None
_generation = 0             # bumped whenever the renderer is replaced
_premul = None              # blend mode for premultiplied textures
_frame_open = False
_error_banner = None

# Set once the backend has been asked for and either taken or refused.
_wanted = None


def wanted():
    """Whether the GPU backend was asked for. `LUMEN_RENDERER=cpu` opts out."""
    global _wanted
    if _wanted is None:
        choice = (os.environ.get('LUMEN_RENDERER') or 'gpu').strip().lower()
        _wanted = choice not in ('cpu', 'cmu', 'wyvern', 'off', '0')
    return _wanted


def give_up():
    """Abandon the GPU backend for this run.

    Called when a renderer cannot be created. `art` decides which kind of
    sprite to bake from `wanted()`, so anything already baked is the wrong
    kind and the caller has to empty the cache straight after this.
    """
    global _wanted
    _wanted = False
    detach()


def active():
    return _renderer is not None


def renderer():
    return _renderer


def generation():
    return _generation


# Draw modes. `set_mode` switches both geometry and sprites together, because
# a light is drawn as both - a glow texture and the polygons of its reach.
NORMAL = 0
ADD = 1
MOD = 2

_mode = NORMAL
_blend = {}                 # mode -> (geometry blend, sprite blend)


def attach(new_renderer):
    """Adopt a renderer. Every texture built for the previous one is void."""
    global _renderer, _generation, _premul, _frame_open, _error_banner, _mode
    _renderer = new_renderer
    _generation += 1
    _frame_open = False
    _error_banner = None
    _premul = None
    _mode = NORMAL
    _blend.clear()
    _drop_targets()
    if new_renderer is None:
        return
    try:
        import pygame
        _premul = new_renderer.compose_custom_blend_mode(
            (pygame.BLENDFACTOR_ONE, pygame.BLENDFACTOR_ONE_MINUS_SRC_ALPHA,
             pygame.BLENDOPERATION_ADD),
            (pygame.BLENDFACTOR_ONE, pygame.BLENDFACTOR_ONE_MINUS_SRC_ALPHA,
             pygame.BLENDOPERATION_ADD))
        # Premultiplied additive: dst += src, with no second multiply by the
        # alpha. `BLENDMODE_ADD` scales the source by its own alpha, which for
        # premultiplied pixels would apply the alpha twice and swallow the
        # glow.
        premul_add = new_renderer.compose_custom_blend_mode(
            (pygame.BLENDFACTOR_ONE, pygame.BLENDFACTOR_ONE,
             pygame.BLENDOPERATION_ADD),
            (pygame.BLENDFACTOR_ONE, pygame.BLENDFACTOR_ONE,
             pygame.BLENDOPERATION_ADD))
        _blend[NORMAL] = (pygame.BLENDMODE_BLEND, _premul)
        _blend[ADD] = (pygame.BLENDMODE_ADD, premul_add)
        _blend[MOD] = (pygame.BLENDMODE_MOD, pygame.BLENDMODE_MOD)
        new_renderer.draw_blend_mode = pygame.BLENDMODE_BLEND
    except Exception as exc:
        if os.environ.get('LUMEN_DEBUG'):
            sys.stderr.write(f'[lumen] custom blend unavailable: {exc!r}\n')
        _premul = None


def set_mode(mode):
    """How subsequent draws combine with what is already there."""
    global _mode
    if _renderer is None or mode == _mode or not _blend:
        _mode = mode
        return
    _mode = mode
    try:
        _renderer.draw_blend_mode = _blend[mode][0]
    except Exception:
        pass


def sprite_blend():
    entry = _blend.get(_mode)
    return entry[1] if entry else _premul


def detach():
    attach(None)


# --------------------------------------------------------------------------
# Sprites
# --------------------------------------------------------------------------
class Sprite:
    """A baked image, uploaded to the GPU the first time it is drawn.

    The upload is deferred because sprites are baked while the game is still
    starting up, before there is a renderer to upload them to. `art` holds
    these exactly where it used to hold a `CMUImage`.
    """

    __slots__ = ('pixels', 'size', '_texture', '_generation')

    def __init__(self, pixels, size):
        self.pixels = pixels        # premultiplied RGBA bytes
        self.size = size
        self._texture = None
        self._generation = -1

    def texture(self):
        if self._texture is not None and self._generation == _generation:
            return self._texture
        if _renderer is None or self.pixels is None:
            return None
        try:
            import pygame
            from pygame._sdl2 import video as sdl2
            surface = pygame.image.frombuffer(self.pixels, self.size, 'RGBA')
            tex = sdl2.Texture.from_surface(_renderer, surface)
            self._texture = tex
            self._generation = _generation
            # The bytes are dead weight once the texture holds them, and a
            # chamber layer is 81 MB of them at native resolution. Whoever
            # replaces the renderer is responsible for emptying `art`'s cache
            # so every sprite is baked again - see `runtime.set_video_mode`.
            self.pixels = None
            return tex
        except Exception:
            return None

    def release(self):
        self.pixels = None
        self._texture = None
        self._generation = -1


def sprite_from_pil(premultiplied):
    """Wrap an already-premultiplied PIL image as a GPU sprite."""
    return Sprite(premultiplied.tobytes(), premultiplied.size)


# --------------------------------------------------------------------------
# Frame
# --------------------------------------------------------------------------
def begin_frame(background):
    global _frame_open
    if _renderer is None:
        return False
    r, g, b = background
    _renderer.draw_color = (r, g, b, 255)
    _renderer.clear()
    _frame_open = True
    return True


def present():
    global _frame_open
    if _renderer is None:
        return False
    _renderer.present()
    _frame_open = False
    return True


def frame_open():
    return _frame_open


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------
def _alpha(opacity):
    if opacity is None:
        return 255
    a = int(opacity * 2.55 + 0.5)
    return 0 if a < 0 else (255 if a > 255 else a)


def blit(sprite, left, top, width=None, height=None, opacity=None):
    """Draw a baked sprite. `left`/`top`/`width`/`height` are real pixels."""
    tex = sprite.texture() if sprite is not None else None
    if tex is None:
        return
    if width is None:
        width = sprite.size[0]
    if height is None:
        height = sprite.size[1]
    a = _alpha(opacity)
    if a != 255:
        # Premultiplied colour has to be scaled along with the alpha, and
        # SDL's alpha modulation only touches the alpha channel - so the
        # colour modulation carries the other half.
        tex.color = (a, a, a)
        tex.alpha = a
    else:
        tex.color = (255, 255, 255)
        tex.alpha = 255
    blend = sprite_blend()
    if blend is not None:
        tex.blend_mode = blend
    tex.draw(dstrect=(left, top, width, height))


def blit_rot(sprite, cx, cy, width, height, degrees, color=None, opacity=None):
    """Draw a sprite rotated about its own centre, in real pixels.

    SDL rotates the destination rectangle for free, which is what lets a
    single baked gradient serve as the light lying along a wall at any angle.
    """
    tex = sprite.texture() if sprite is not None else None
    if tex is None:
        return
    a = _alpha(opacity)
    if color is not None:
        r, g, b = color
        tex.color = (r * a // 255, g * a // 255, b * a // 255)
    else:
        tex.color = (a, a, a)
    tex.alpha = a
    blend = sprite_blend()
    if blend is not None:
        tex.blend_mode = blend
    tex.draw(dstrect=(cx - width * 0.5, cy - height * 0.5, width, height),
             angle=degrees)
    tex.color = (255, 255, 255)
    tex.alpha = 255


def _convex(points):
    """True if the polygon turns the same way at every vertex."""
    n = len(points)
    if n < 4:
        return True
    sign = 0
    for i in range(n):
        ax, ay = points[i]
        bx, by = points[(i + 1) % n]
        cx, cy = points[(i + 2) % n]
        cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
        if cross > 1e-9:
            if sign < 0:
                return False
            sign = 1
        elif cross < -1e-9:
            if sign > 0:
                return False
            sign = -1
    return True


def _area2(points):
    total = 0.0
    n = len(points)
    for i in range(n):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total


def _inside(ax, ay, bx, by, cx, cy, px_, py_):
    d1 = (px_ - bx) * (ay - by) - (ax - bx) * (py_ - by)
    d2 = (px_ - cx) * (by - cy) - (bx - cx) * (py_ - cy)
    d3 = (px_ - ax) * (cy - ay) - (cx - ax) * (py_ - ay)
    neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (neg and pos)


def triangulate(points):
    """Fan for convex polygons, ear clipping otherwise.

    The game draws plenty of concave shapes - the star-bodied enemies most
    obviously - and `fill_triangle` only takes triangles, so anything that is
    not convex has to be cut up first.
    """
    n = len(points)
    if n < 3:
        return []
    if n == 3:
        return [(points[0], points[1], points[2])]
    if _convex(points):
        p0 = points[0]
        return [(p0, points[i], points[i + 1]) for i in range(1, n - 1)]

    # Ear clipping. Work on a copy wound counter-clockwise.
    verts = list(points)
    if _area2(verts) < 0:
        verts.reverse()
    out = []
    guard = 0
    while len(verts) > 3 and guard < 4 * n:
        guard += 1
        clipped = False
        count = len(verts)
        for i in range(count):
            ax, ay = verts[(i - 1) % count]
            bx, by = verts[i]
            cx, cy = verts[(i + 1) % count]
            if (bx - ax) * (cy - by) - (by - ay) * (cx - bx) <= 0:
                continue                     # reflex vertex, not an ear
            blocked = False
            for j in range(count):
                if j in ((i - 1) % count, i, (i + 1) % count):
                    continue
                if _inside(ax, ay, bx, by, cx, cy, *verts[j]):
                    blocked = True
                    break
            if blocked:
                continue
            out.append(((ax, ay), (bx, by), (cx, cy)))
            del verts[i]
            clipped = True
            break
        if not clipped:
            break                            # degenerate; fan the remainder
    for i in range(1, len(verts) - 1):
        out.append((verts[0], verts[i], verts[i + 1]))
    return out


def polygon(points, fill=None, opacity=None, border=None, border_width=1.0):
    """Fill and/or outline a polygon. Points are real pixels."""
    if _renderer is None or len(points) < 2:
        return
    a = _alpha(opacity)
    n = len(points)
    if fill is not None and n >= 3:
        _renderer.draw_color = (fill[0], fill[1], fill[2], a)
        if n == 4 and _convex(points):
            # Quads are most of what this game draws - particles, HUD bars,
            # and one per ray step of every cast shadow - so going straight to
            # `fill_quad` skips both the triangulation and half the draw calls.
            _renderer.fill_quad(*points)
        elif n == 3:
            _renderer.fill_triangle(*points)
        else:
            for tri in triangulate(points):
                _renderer.fill_triangle(*tri)
    if border is not None:
        _renderer.draw_color = (border[0], border[1], border[2], a)
        if border_width <= 1.2:
            n = len(points)
            for i in range(n):
                _renderer.draw_line(points[i], points[(i + 1) % n])
        else:
            n = len(points)
            for i in range(n):
                _thick_line(points[i], points[(i + 1) % n], border_width)


def _thick_line(p0, p1, width):
    """A line with real width is a quad, which is how the old renderer did it."""
    x1, y1 = p0
    x2, y2 = p1
    dx, dy = x2 - x1, y2 - y1
    length = (dx * dx + dy * dy) ** 0.5
    if length < 1e-6:
        return
    nx, ny = -dy / length * width * 0.5, dx / length * width * 0.5
    _renderer.fill_quad((x1 + nx, y1 + ny), (x2 + nx, y2 + ny),
                        (x2 - nx, y2 - ny), (x1 - nx, y1 - ny))


def line(x1, y1, x2, y2, fill=None, width=1.0, opacity=None):
    if _renderer is None or fill is None:
        return
    _renderer.draw_color = (fill[0], fill[1], fill[2], _alpha(opacity))
    if width <= 1.2:
        _renderer.draw_line((x1, y1), (x2, y2))
    else:
        _thick_line((x1, y1), (x2, y2), width)


# --------------------------------------------------------------------------
# The framework's error screen, which we would otherwise swallow
# --------------------------------------------------------------------------
def draw_error_banner(width, height):
    """cmu-graphics' 'Exception! App Stopped!' banner, drawn on the GPU.

    The framework builds that screen out of its own shapes and rasterises them,
    which is exactly the path this backend replaces - so without this a crash
    would leave the last good frame on screen and no sign anything was wrong.
    """
    global _error_banner
    if _renderer is None:
        return
    if _error_banner is None:
        _error_banner = _bake_error_text()
    # The bar is sized to the text rather than to a fixed number of pixels:
    # `width`/`height` here are real framebuffer pixels, so a fixed 50 would
    # be a sliver on one display and a slab on another.
    tw, th = _error_banner.size if _error_banner is not None else (0, 0)
    pad = max(8, th // 4)
    bar_h = th + pad * 2
    bar = (10, height - bar_h - 10, width - 20, bar_h)

    _renderer.draw_color = (255, 0, 0, 255)
    for i in range(3):
        _renderer.draw_rect((i, i, width - i * 2, height - i * 2))
    _renderer.draw_color = (255, 255, 255, 255)
    _renderer.fill_rect(bar)
    _renderer.draw_color = (255, 0, 0, 255)
    for i in range(4):
        _renderer.draw_rect((bar[0] + i, bar[1] + i,
                             bar[2] - i * 2, bar[3] - i * 2))
    if _error_banner is not None:
        blit(_error_banner, width * 0.5 - tw * 0.5, bar[1] + pad, tw, th)


def _bake_error_text():
    try:
        import pygame
        pygame.font.init()
        font = pygame.font.SysFont('Arial', 22, bold=True)
        lines = ['Exception! App Stopped!', 'See console for details']
        rendered = [font.render(t, True, (255, 0, 0)) for t in lines]
        w = max(s.get_width() for s in rendered)
        h = sum(s.get_height() for s in rendered)
        sheet = pygame.Surface((w, h), pygame.SRCALPHA, 32)
        y = 0
        for s in rendered:
            sheet.blit(s, (w // 2 - s.get_width() // 2, y))
            y += s.get_height()
        # Straight alpha from pygame; premultiply so the texture blend matches.
        sheet = sheet.premul_alpha()
        return Sprite(pygame.image.tobytes(sheet, 'RGBA'), (w, h))
    except Exception:
        return None


# --------------------------------------------------------------------------
# Deferred lighting
# --------------------------------------------------------------------------
# The game used to light the world by drawing the glow first and then laying
# the floor back over it at partial opacity. That works, but only the floor
# ever receives light: walls and entities are drawn opaque on top of it, so
# they had to fake being lit - a rim stroke along each wall edge, and a hard
# `lit` flag that popped an enemy into existence the moment it crossed into
# the lantern's reach.
#
# With a target texture per pass there is no need to fake it. Everything the
# chamber contains is drawn once, unlit, into a scene buffer. Every light is
# added into a second buffer, shadows are multiplied back out of it, and the
# two are combined at the end. Walls, enemies, pickups and the player are then
# lit by exactly the same falloff as the ground they stand on, which is both
# more correct and far less code than lighting each of them by hand.
#
# The whole chain measured 0.36 ms at 3600x2260 - about 4% of a 120 Hz frame.

BLOOM_LEVELS = 5

_targets = {}               # name -> Texture
_targets_size = None
_prev_target = None


def _drop_targets():
    global _targets, _targets_size
    _targets = {}
    _targets_size = None


def lighting_ready(size=None):
    """Make sure the offscreen buffers exist at the drawable's size."""
    global _targets, _targets_size
    if _renderer is None or not _blend:
        return False
    if size is None:
        try:
            size = tuple(_renderer.get_viewport().size)
        except Exception:
            return False
    if _targets_size == size and _targets:
        return True
    try:
        import pygame
        from pygame._sdl2 import video as sdl2
        w, h = size
        made = {'scene': sdl2.Texture(_renderer, (w, h), target=True),
                'light': sdl2.Texture(_renderer, (w, h), target=True),
                'final': sdl2.Texture(_renderer, (w, h), target=True)}
        bw, bh = w, h
        for i in range(BLOOM_LEVELS):
            bw, bh = max(4, bw // 2), max(4, bh // 2)
            made[f'bloom{i}'] = sdl2.Texture(_renderer, (bw, bh), target=True)
        for tex in made.values():
            tex.blend_mode = pygame.BLENDMODE_BLEND
        _targets = made
        _targets_size = size
        return True
    except Exception as exc:
        if os.environ.get('LUMEN_DEBUG'):
            sys.stderr.write(f'[lumen] light buffers unavailable: {exc!r}\n')
        _drop_targets()
        return False


def _use(name):
    global _prev_target
    _prev_target = name
    _renderer.target = _targets[name] if name else None


def begin_scene(background):
    """Start the unlit pass. Everything solid in the chamber goes here."""
    set_mode(NORMAL)
    _use('scene')
    r, g, b = background
    _renderer.draw_color = (r, g, b, 255)
    _renderer.clear()


def begin_light():
    """Start the light pass, black: light is what gets added to it."""
    set_mode(NORMAL)
    _use('light')
    _renderer.draw_color = (0, 0, 0, 255)
    _renderer.clear()


def add_ambient(color):
    """Lift the whole light buffer by a floor value.

    Added *after* the shadows are multiplied out, so a shadowed corner settles
    at ambient rather than at nothing - which is what stops occlusion reading
    as a hole cut in the world.
    """
    set_mode(ADD)
    w, h = _targets_size
    r, g, b = color
    _renderer.draw_color = (r, g, b, 255)
    _renderer.fill_quad((0, 0), (w, 0), (w, h), (0, h))
    set_mode(NORMAL)


def amplify_scene(gain):
    """Brighten the albedo in place, keeping its contrast.

    The chamber art is baked dark on purpose: the old renderer lit the floor
    by drawing the glow underneath and laying the floor back over it at 46%,
    so a lit floor was more light than ground. Multiplying that albedo by a
    light buffer can only darken it further. Adding the light back on top
    instead brightens it, but uniformly - which flattens a wall and the floor
    beside it to the same value and makes the masonry vanish. Scaling the
    albedo itself keeps the difference between them.
    """
    import pygame
    if gain <= 0.0:
        return
    w, h = _targets_size
    scene, scratch = _targets['scene'], _targets['final']
    # Via a scratch target: a texture cannot be its own render target.
    _use('final')
    scene.blend_mode = pygame.BLENDMODE_NONE
    scene.color = (255, 255, 255)
    scene.alpha = 255
    scene.draw(dstrect=(0, 0, w, h))
    _use('scene')
    a = max(0, min(255, int(gain * 255)))
    scratch.blend_mode = pygame.BLENDMODE_ADD
    scratch.color = (a, a, a)
    scratch.alpha = 255
    scratch.draw(dstrect=(0, 0, w, h))
    scratch.color = (255, 255, 255)


def composite(bloom=0.0, bleed=0.0):
    """Combine the two buffers, and bloom the result.

    `scene * light` alone would leave the game far darker than it was, because
    the art is baked dark on purpose: the old renderer got its brightness by
    drawing the glow first and laying the floor back over it at 46%, so more
    than half of a lit floor's brightness was the *light*, not the ground. A
    multiply can only ever darken the albedo, so the light buffer is also
    added on top at `bleed` - that is the part of the image that is the light
    itself rather than a surface reflecting it.
    """
    import pygame
    w, h = _targets_size
    light = _targets['light']

    _use('final')
    set_mode(NORMAL)
    _targets['scene'].alpha = 255
    _targets['scene'].color = (255, 255, 255)
    _targets['scene'].blend_mode = pygame.BLENDMODE_NONE
    _targets['scene'].draw(dstrect=(0, 0, w, h))
    # `MUL` is dst*src, which is what lighting an albedo means.
    light.blend_mode = pygame.BLENDMODE_MUL
    light.alpha = 255
    light.color = (255, 255, 255)
    light.draw(dstrect=(0, 0, w, h))
    if bleed > 0.0:
        a = max(0, min(255, int(bleed * 255)))
        light.blend_mode = pygame.BLENDMODE_ADD
        light.color = (a, a, a)
        light.alpha = 255
        light.draw(dstrect=(0, 0, w, h))
        light.color = (255, 255, 255)

    if bloom > 0.0:
        _bloom_chain(pygame, w, h, bloom)
    else:
        _use(None)
        _targets['final'].blend_mode = pygame.BLENDMODE_NONE
        _targets['final'].draw(dstrect=(0, 0, w, h))
    set_mode(NORMAL)


def _bloom_chain(pygame, w, h, strength):
    """Bright-pass, blur by successive halving, and add back.

    There is no shader to threshold with, so the bright pass squares the image
    instead: drawing it onto itself with `MUL` sends anything dim towards
    nothing while leaving the lantern and the flare almost untouched. On a
    game this dark that separates light from lit surface well enough, and it
    costs one half-resolution blit.
    """
    first = _targets['bloom0']
    _use('bloom0')
    _renderer.draw_color = (0, 0, 0, 255)
    _renderer.clear()
    src = _targets['final']
    src.blend_mode = pygame.BLENDMODE_NONE
    src.draw(dstrect=(0, 0, first.width, first.height))
    src.blend_mode = pygame.BLENDMODE_MUL
    src.draw(dstrect=(0, 0, first.width, first.height))

    prev = first
    for i in range(1, BLOOM_LEVELS):
        tex = _targets[f'bloom{i}']
        _use(f'bloom{i}')
        _renderer.draw_color = (0, 0, 0, 255)
        _renderer.clear()
        prev.blend_mode = pygame.BLENDMODE_NONE
        prev.draw(dstrect=(0, 0, tex.width, tex.height))
        prev = tex

    _use(None)
    _targets['final'].blend_mode = pygame.BLENDMODE_NONE
    _targets['final'].alpha = 255
    _targets['final'].draw(dstrect=(0, 0, w, h))

    a = max(0, min(255, int(strength * 255)))
    for i in range(BLOOM_LEVELS):
        tex = _targets[f'bloom{i}']
        tex.blend_mode = pygame.BLENDMODE_ADD
        tex.alpha = a
        tex.color = (255, 255, 255)
        tex.draw(dstrect=(0, 0, w, h))
        tex.alpha = 255
