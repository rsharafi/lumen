"""The renderer with shaders, and with somewhere to put values above 1.0.

SDL's renderer draws this game at 120 fps and there is nothing wrong with it.
What it cannot do is hold a colour brighter than white, and almost every hard
problem in the lighting came back to that:

* Sprites are dithered, and the finished frame is dithered again, because a
  gradient hundreds of pixels wide has 256 levels to land in.
* The bloom bright-pass squares the image with a multiply, because there is no
  way to write `if (luminance > threshold)`.
* The lantern body and the rift core had to be dimmed by hand, from 98 to 60,
  because sitting at the centre of the light they clipped to flat white.
* `ALBEDO_GAIN` and `BLEED` are two hand-tuned constants standing in for an
  exposure control.

A float16 target holds 2.5 as easily as 0.5, so light can simply be brighter
than the screen and a tone map decides at the end what that looks like. None
of the workarounds above are needed here, and the frame dither is deliberately
ignored: `composite` takes the argument so the call site does not have to
care, and then does not use it.

Everything is batched. Solid geometry carries its colour per vertex, so an
entire frame of shadow quads, particles and wall light is one draw call rather
than one per colour change; textured draws flush when the texture changes, to
keep painter's order.
"""

import math
import os
import sys
from array import array

import numpy as np

NORMAL = 0
ADD = 1
MOD = 2
MAX = 3

BLOOM_LEVELS = 5

_ctx = None
_generation = 0
_mode = NORMAL
_frame_open = False
_wanted = None
_error_banner = None

_solid_prog = None
_tex_prog = None
_post_prog = None
_solid_vbo = _solid_vao = None
_tex_vbo = _tex_vao = None
_quad_vao = None

# C-backed float arrays, not Python lists. A busy frame pushes a few hundred
# thousand floats through here, and `np.asarray` on a list of that many is
# tens of milliseconds on its own - more than the entire frame budget.
# `array('f')` extends at C speed and hands its bytes straight to the buffer.
_solid = array('f')         # pending solid vertices
_tex = array('f')           # pending textured vertices
_tex_current = None         # texture the pending batch belongs to

_targets = {}
_targets_size = None
_target_name = None
_viewport = (1, 1)

# One draw of a full-screen triangle pair, for the post passes.
_SCREEN_QUAD = np.array([-1, -1, 0, 0, 3, -1, 2, 0, -1, 3, 0, 2], dtype='f4')

_SOLID_VS = '''#version 330
uniform vec2 viewport;
in vec2 in_pos;
in vec4 in_col;
out vec4 v_col;
void main() {
    vec2 ndc = vec2(in_pos.x / viewport.x * 2.0 - 1.0,
                    1.0 - in_pos.y / viewport.y * 2.0);
    gl_Position = vec4(ndc, 0.0, 1.0);
    v_col = in_col;
}
'''

_SOLID_FS = '''#version 330
in vec4 v_col;
out vec4 frag;
void main() { frag = v_col; }
'''

_TEX_VS = '''#version 330
uniform vec2 viewport;
in vec2 in_pos;
in vec2 in_uv;
in vec4 in_col;
out vec2 v_uv;
out vec4 v_col;
void main() {
    vec2 ndc = vec2(in_pos.x / viewport.x * 2.0 - 1.0,
                    1.0 - in_pos.y / viewport.y * 2.0);
    gl_Position = vec4(ndc, 0.0, 1.0);
    v_uv = in_uv;
    v_col = in_col;
}
'''

# Sprites are premultiplied, so the tint scales colour and alpha together -
# the same thing SDL needed a colour modulation *and* an alpha modulation to
# express.
_TEX_FS = '''#version 330
uniform sampler2D tex0;
in vec2 v_uv;
in vec4 v_col;
out vec4 frag;
void main() { frag = texture(tex0, v_uv) * v_col; }
'''

# The whole reason for this backend. The scene is lit in linear light with no
# ceiling; this is where it becomes a picture.
_POST_VS = '''#version 330
in vec2 in_pos;
in vec2 in_uv;
out vec2 v_uv;
void main() { gl_Position = vec4(in_pos, 0.0, 1.0); v_uv = in_uv; }
'''

_POST_FS = '''#version 330
uniform sampler2D scene;
uniform sampler2D bloom;
uniform float bloom_amount;
uniform float exposure;
uniform int mode;          // 0 tone map, 1 bright pass, 2 blur, 3 copy
uniform vec2 texel;
uniform float threshold;
in vec2 v_uv;
out vec4 frag;

vec3 tonemap(vec3 x) {
    // The ACES filmic approximation. Plain Reinhard - c / (1 + c) - rolls
    // highlights off nicely but has no toe, so it lifts every dark value as
    // well: on a game that is mostly darkness that turns black into haze.
    // This keeps the bottom of the range where it was and still stops a flame
    // at the centre of the light clipping to a flat white slab.
    vec3 c = x * exposure;
    return clamp((c * (2.51 * c + 0.03)) / (c * (2.43 * c + 0.59) + 0.14),
                 0.0, 1.0);
}

void main() {
    if (mode == 1) {
        // A real threshold, rather than squaring the image and hoping.
        vec3 c = texture(scene, v_uv).rgb;
        float l = dot(c, vec3(0.2126, 0.7152, 0.0722));
        frag = vec4(c * max(l - threshold, 0.0) / max(l, 1e-4), 1.0);
    } else if (mode == 2) {
        // Nine taps of a separable-ish blur, cheap and smooth enough at the
        // sizes the chain runs at.
        vec3 acc = vec3(0.0);
        float wsum = 0.0;
        for (int y = -2; y <= 2; ++y) {
            for (int x = -2; x <= 2; ++x) {
                float w = exp(-float(x * x + y * y) * 0.35);
                acc += texture(scene, v_uv + vec2(x, y) * texel).rgb * w;
                wsum += w;
            }
        }
        frag = vec4(acc / wsum, 1.0);
    } else if (mode == 3) {
        frag = texture(scene, v_uv);
    } else {
        vec3 c = texture(scene, v_uv).rgb;
        c += texture(bloom, v_uv).rgb * bloom_amount;
        frag = vec4(tonemap(c), 1.0);
    }
}
'''


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------
def wanted():
    global _wanted
    if _wanted is None:
        choice = (os.environ.get('LUMEN_RENDERER') or 'gpu').strip().lower()
        _wanted = choice not in ('cpu', 'cmu', 'wyvern', 'off', '0')
    return _wanted


def give_up():
    global _wanted
    _wanted = False
    detach()


def active():
    return _ctx is not None


def renderer():
    return _ctx


def generation():
    return _generation


def attach(context):
    """Adopt a GL context. Every texture built for the previous one is void."""
    global _ctx, _generation, _mode, _frame_open, _error_banner
    global _solid_prog, _tex_prog, _post_prog
    global _solid_vbo, _solid_vao, _tex_vbo, _tex_vao, _quad_vao
    _ctx = context
    _generation += 1
    _mode = NORMAL
    _frame_open = False
    _error_banner = None
    del _solid[:]
    del _tex[:]
    _drop_targets()
    if context is None:
        _solid_prog = _tex_prog = _post_prog = None
        return
    context.enable(context.BLEND)
    _solid_prog = context.program(vertex_shader=_SOLID_VS,
                                  fragment_shader=_SOLID_FS)
    _tex_prog = context.program(vertex_shader=_TEX_VS, fragment_shader=_TEX_FS)
    _post_prog = context.program(vertex_shader=_POST_VS,
                                 fragment_shader=_POST_FS)
    _solid_vbo = context.buffer(reserve=6 * 4 * 60000)
    _solid_vao = context.vertex_array(
        _solid_prog, [(_solid_vbo, '2f 4f', 'in_pos', 'in_col')])
    _tex_vbo = context.buffer(reserve=8 * 4 * 60000)
    _tex_vao = context.vertex_array(
        _tex_prog, [(_tex_vbo, '2f 2f 4f', 'in_pos', 'in_uv', 'in_col')])
    quad = context.buffer(_SCREEN_QUAD.tobytes())
    _quad_vao = context.vertex_array(
        _post_prog, [(quad, '2f 2f', 'in_pos', 'in_uv')])


def detach():
    attach(None)


def sprite_blend():
    return _mode


def set_mode(mode):
    """How subsequent draws combine with what is already there."""
    global _mode
    if mode == _mode:
        return
    flush()
    _mode = mode
    _apply_blend()


def _apply_blend():
    if _ctx is None:
        return
    if _mode == REPLACE:
        # A straight copy. Alpha blending a tint above 1.0 would make
        # `1 - srcA` negative, which is not a thing.
        _ctx.blend_equation = _ctx.FUNC_ADD
        _ctx.blend_func = (_ctx.ONE, _ctx.ZERO)
    elif _mode == ADD:
        _ctx.blend_equation = _ctx.FUNC_ADD
        _ctx.blend_func = (_ctx.ONE, _ctx.ONE)
    elif _mode == MOD:
        _ctx.blend_equation = _ctx.FUNC_ADD
        _ctx.blend_func = (_ctx.ZERO, _ctx.SRC_COLOR)
    elif _mode == MAX:
        _ctx.blend_equation = _ctx.MAX
        _ctx.blend_func = (_ctx.ONE, _ctx.ONE)
    else:
        _ctx.blend_equation = _ctx.FUNC_ADD
        _ctx.blend_func = (_ctx.ONE, _ctx.ONE_MINUS_SRC_ALPHA)


# --------------------------------------------------------------------------
# Sprites
# --------------------------------------------------------------------------
class Sprite:
    """A baked image, uploaded on first use.

    The upload is deferred because sprites are baked while the game is still
    starting, before there is a context to upload them to.
    """

    __slots__ = ('pixels', 'size', '_texture', '_generation')

    def __init__(self, pixels, size):
        self.pixels = pixels
        self.size = size
        self._texture = None
        self._generation = -1

    def texture(self):
        if self._texture is not None and self._generation == _generation:
            return self._texture
        if _ctx is None or self.pixels is None:
            return None
        try:
            tex = _ctx.texture(self.size, 4, self.pixels)
            tex.filter = (_ctx.LINEAR, _ctx.LINEAR)
            tex.repeat_x = tex.repeat_y = False
            self._texture = tex
            self._generation = _generation
            self.pixels = None
            return tex
        except Exception:
            return None

    def release(self):
        self.pixels = None
        self._texture = None
        self._generation = -1


def sprite_from_pil(premultiplied):
    return Sprite(premultiplied.tobytes(), premultiplied.size)


# --------------------------------------------------------------------------
# Batching
# --------------------------------------------------------------------------
STATS = {'flush': 0, 'verts': 0, 'frames': 0}


def flush():
    """Send whatever has accumulated. Called before any state change."""
    global _tex_current
    if _ctx is None:
        return
    if len(_solid) or len(_tex):
        STATS['flush'] += 1
        STATS['verts'] += len(_solid) // 6 + len(_tex) // 8
    if len(_solid):
        _solid_vbo.write(_solid.tobytes())
        _solid_prog['viewport'].value = _viewport
        _solid_vao.render(vertices=len(_solid) // 6)
        del _solid[:]
    if len(_tex) and _tex_current is not None:
        _tex_vbo.write(_tex.tobytes())
        _tex_prog['viewport'].value = _viewport
        _tex_current.use(0)
        _tex_prog['tex0'].value = 0
        _tex_vao.render(vertices=len(_tex) // 8)
        del _tex[:]
    _tex_current = None


def _alpha(opacity):
    if opacity is None:
        return 1.0
    a = opacity / 100.0
    return 0.0 if a < 0.0 else (1.0 if a > 1.0 else a)


def _push_solid_tri(a, b, c, col):
    r, g, bl, al = col
    for (x, y) in (a, b, c):
        _solid.extend((x, y, r, g, bl, al))


def _push_tex_quad(tex, corners, col):
    global _tex_current
    if tex is not _tex_current:
        flush()
        _tex_current = tex
    r, g, b, a = col
    (p0, p1, p2, p3) = corners
    for (x, y), (u, v) in ((p0, (0.0, 0.0)), (p1, (1.0, 0.0)), (p2, (1.0, 1.0)),
                           (p0, (0.0, 0.0)), (p2, (1.0, 1.0)), (p3, (0.0, 1.0))):
        _tex.extend((x, y, u, v, r, g, b, a))


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------
def blit(sprite, left, top, width=None, height=None, opacity=None):
    tex = sprite.texture() if sprite is not None else None
    if tex is None:
        return
    if width is None:
        width = sprite.size[0]
    if height is None:
        height = sprite.size[1]
    a = _alpha(opacity)
    _push_tex_quad(tex, ((left, top), (left + width, top),
                         (left + width, top + height), (left, top + height)),
                   (a, a, a, a))


def blit_rot(sprite, cx, cy, width, height, degrees, color=None, opacity=None):
    tex = sprite.texture() if sprite is not None else None
    if tex is None:
        return
    a = _alpha(opacity)
    if color is not None:
        col = (color[0] / 255.0 * a, color[1] / 255.0 * a,
               color[2] / 255.0 * a, a)
    else:
        col = (a, a, a, a)
    rad = math.radians(degrees)
    ca, sa = math.cos(rad), math.sin(rad)
    hw, hh = width * 0.5, height * 0.5
    pts = []
    for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)):
        pts.append((cx + dx * ca - dy * sa, cy + dx * sa + dy * ca))
    _push_tex_quad(tex, pts, col)


def _convex(points):
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
    """Fan for convex polygons, ear clipping otherwise."""
    n = len(points)
    if n < 3:
        return []
    if n == 3:
        return [(points[0], points[1], points[2])]
    if _convex(points):
        p0 = points[0]
        return [(p0, points[i], points[i + 1]) for i in range(1, n - 1)]
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
                continue
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
            break
    for i in range(1, len(verts) - 1):
        out.append((verts[0], verts[i], verts[i + 1]))
    return out


def polygon(points, fill=None, opacity=None, border=None, border_width=1.0):
    if _ctx is None or len(points) < 2:
        return
    a = _alpha(opacity)
    if fill is not None and len(points) >= 3:
        col = (fill[0] / 255.0 * a, fill[1] / 255.0 * a, fill[2] / 255.0 * a, a)
        if len(points) == 3:
            _push_solid_tri(points[0], points[1], points[2], col)
        elif len(points) == 4 and _convex(points):
            _push_solid_tri(points[0], points[1], points[2], col)
            _push_solid_tri(points[0], points[2], points[3], col)
        else:
            for tri in triangulate(points):
                _push_solid_tri(*tri, col)
    if border is not None:
        col = (border[0] / 255.0 * a, border[1] / 255.0 * a,
               border[2] / 255.0 * a, a)
        n = len(points)
        for i in range(n):
            _thick_line(points[i], points[(i + 1) % n],
                        max(border_width, 1.0), col)


def _thick_line(p0, p1, width, col):
    x1, y1 = p0
    x2, y2 = p1
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return
    nx, ny = -dy / length * width * 0.5, dx / length * width * 0.5
    a = (x1 + nx, y1 + ny)
    b = (x2 + nx, y2 + ny)
    c = (x2 - nx, y2 - ny)
    d = (x1 - nx, y1 - ny)
    _push_solid_tri(a, b, c, col)
    _push_solid_tri(a, c, d, col)


def line(x1, y1, x2, y2, fill=None, width=1.0, opacity=None):
    if _ctx is None or fill is None:
        return
    a = _alpha(opacity)
    col = (fill[0] / 255.0 * a, fill[1] / 255.0 * a, fill[2] / 255.0 * a, a)
    _thick_line((x1, y1), (x2, y2), max(width, 1.0), col)


# --------------------------------------------------------------------------
# Targets and the frame
# --------------------------------------------------------------------------
def _drop_targets():
    global _targets, _targets_size
    _targets = {}
    _targets_size = None


def lighting_ready(size=None):
    global _targets, _targets_size
    if _ctx is None:
        return False
    if size is None:
        size = _viewport
    size = (max(1, int(size[0])), max(1, int(size[1])))
    if _targets_size == size and _targets:
        return True
    try:
        made = {}
        for name in ('scene', 'light', 'final', 'edge'):
            tex = _ctx.texture(size, 4, dtype='f2')
            tex.filter = (_ctx.LINEAR, _ctx.LINEAR)
            made[name] = (tex, _ctx.framebuffer(color_attachments=[tex]))
        w, h = size
        for i in range(BLOOM_LEVELS):
            w, h = max(4, w // 2), max(4, h // 2)
            tex = _ctx.texture((w, h), 4, dtype='f2')
            tex.filter = (_ctx.LINEAR, _ctx.LINEAR)
            made[f'bloom{i}'] = (tex, _ctx.framebuffer(color_attachments=[tex]))
        _targets = made
        _targets_size = size
        return True
    except Exception as exc:
        if os.environ.get('LUMEN_DEBUG'):
            sys.stderr.write(f'[lumen] gl targets unavailable: {exc!r}\n')
        _drop_targets()
        return False


def _use(name):
    global _target_name, _viewport
    flush()
    _target_name = name
    if name is None:
        _ctx.screen.use()
        _viewport = _ctx.screen.size
    else:
        tex, fbo = _targets[name]
        fbo.use()
        _viewport = tex.size


def set_viewport(size):
    """Told by the host what the drawable is."""
    global _viewport
    _viewport = (max(1, int(size[0])), max(1, int(size[1])))


def begin_frame(background):
    global _frame_open
    if _ctx is None:
        return False
    _use(None)
    set_mode(NORMAL)
    r, g, b = background
    _ctx.clear(r / 255.0, g / 255.0, b / 255.0, 1.0)
    _frame_open = True
    return True


_window = None


def set_window(window):
    """The window whose buffers `present` swaps."""
    global _window
    _window = window


def present():
    global _frame_open
    if _ctx is None:
        return False
    flush()
    STATS['frames'] += 1
    if _window is not None:
        _window.flip()
    _frame_open = False
    return True


def frame_open():
    return _frame_open


def read_frame():
    if _ctx is None:
        return None
    import pygame
    flush()
    w, h = _ctx.screen.size
    raw = _ctx.screen.read(components=3, alignment=1)
    surf = pygame.image.frombuffer(raw, (w, h), 'RGB')
    return pygame.transform.flip(surf, False, True)


def begin_scene(background):
    _use('scene')
    set_mode(NORMAL)
    r, g, b = background
    _ctx.clear(r / 255.0, g / 255.0, b / 255.0, 1.0)


def begin_light():
    _use('light')
    set_mode(NORMAL)
    _ctx.clear(0.0, 0.0, 0.0, 1.0)


def add_ambient(color):
    set_mode(ADD)
    w, h = _viewport
    r, g, b = (c / 255.0 for c in color)
    _push_solid_tri((0, 0), (w, 0), (w, h), (r, g, b, 1.0))
    _push_solid_tri((0, 0), (w, h), (0, h), (r, g, b, 1.0))
    set_mode(NORMAL)


def amplify_scene(gain):
    """Kept for interface parity; here it is a uniform, applied at composite."""
    global _albedo_gain
    _albedo_gain = 1.0 + max(0.0, gain)


_albedo_gain = 1.0


def begin_edge_light():
    _use('edge')
    _ctx.clear(0.0, 0.0, 0.0, 1.0)
    set_mode(MAX)


def end_edge_light(opacity=100):
    flush()
    set_mode(NORMAL)
    _use(None)
    _blit_target('edge', ADD, _alpha(opacity))


REPLACE = 4


def _blit_target(name, mode, tint=1.0, target_size=None):
    """Draw one render target over whatever is bound, as a full-screen quad."""
    tex, _fbo = _targets[name]
    set_mode(mode)
    w, h = target_size or _viewport
    _push_tex_quad(tex, ((0, 0), (w, 0), (w, h), (0, h)),
                   (tint, tint, tint, tint))
    flush()
    set_mode(NORMAL)


def tile_over(sprite, opacity=100):
    """Unused here: the frame dither exists only to hide 8-bit banding."""


def composite(bloom=0.0, bleed=0.0, dither=None):
    """Light the albedo, bloom it, and tone map - all in float.

    `dither` is accepted and ignored. It exists because the SDL renderer
    quantises to 8 bits at every step and a gradient turns into contours; here
    the buffers are float16 and there is nothing to hide.
    """
    if _ctx is None:
        return
    flush()
    size = _targets_size

    # scene * light, plus the light itself so a lit floor reads as lit. In
    # float this can go above 1.0 and the tone map deals with it, which is
    # what makes the hand-dimmed emissives unnecessary.
    _use('final')
    _ctx.clear(0.0, 0.0, 0.0, 1.0)
    _blit_target('scene', REPLACE, _albedo_gain, size)
    _blit_target('light', MOD, 1.0, size)
    if bleed > 0.0:
        _blit_target('light', ADD, bleed, size)

    if bloom > 0.0:
        _bloom_chain()
        _use(None)
        _post(scene='final', bloom='bloom0', mode=0, amount=bloom)
    else:
        _use(None)
        _post(scene='final', bloom='final', mode=0, amount=0.0)


def _post(scene, bloom, mode, amount=0.0, threshold=1.0, texel=(0.0, 0.0)):
    flush()
    _ctx.blend_equation = _ctx.FUNC_ADD
    _ctx.blend_func = (_ctx.ONE, _ctx.ZERO)
    _targets[scene][0].use(0)
    _targets[bloom][0].use(1)
    _post_prog['scene'].value = 0
    _post_prog['bloom'].value = 1
    _post_prog['mode'].value = mode
    _post_prog['bloom_amount'].value = amount
    _post_prog['exposure'].value = _EXPOSURE
    _post_prog['threshold'].value = threshold
    _post_prog['texel'].value = texel
    _quad_vao.render(vertices=3)
    _apply_blend()


# In float there is no clipping to design around, so exposure is a real
# control rather than the two hand-tuned constants the 8-bit path needs.
_EXPOSURE = 1.05
_BLOOM_THRESHOLD = 0.75


def _bloom_chain():
    """Bright pass, then blur down and back up. A real threshold this time."""
    src = 'final'
    for i in range(BLOOM_LEVELS):
        name = f'bloom{i}'
        tex, fbo = _targets[name]
        fbo.use()
        _viewport_set(tex.size)
        _ctx.clear(0.0, 0.0, 0.0, 1.0)
        if i == 0:
            _post(scene=src, bloom=src, mode=1, threshold=_BLOOM_THRESHOLD)
        else:
            prev = _targets[f'bloom{i - 1}'][0]
            _post(scene=f'bloom{i - 1}', bloom=f'bloom{i - 1}', mode=2,
                  texel=(1.0 / prev.size[0], 1.0 / prev.size[1]))
    # Fold the small levels back into the first, which is what gets added.
    for i in range(BLOOM_LEVELS - 1, 0, -1):
        tex, fbo = _targets['bloom0']
        fbo.use()
        _viewport_set(tex.size)
        _blit_target(f'bloom{i}', ADD, 1.0, tex.size)


def _viewport_set(size):
    global _viewport
    _viewport = size


def draw_error_banner(width, height):
    if _ctx is None:
        return
    set_mode(NORMAL)
    col = (1.0, 0.0, 0.0, 1.0)
    for i in range(3):
        _thick_line((i, i), (width - i, i), 2.0, col)
        _thick_line((i, height - i), (width - i, height - i), 2.0, col)
        _thick_line((i, i), (i, height - i), 2.0, col)
        _thick_line((width - i, i), (width - i, height - i), 2.0, col)
    flush()
