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

# This backend evaluates lights per pixel in float; see `radial_glow`.
ANALYTIC_LIGHTS = True
# ...and collects occlusion where it cannot compound; see `begin_shadow`.
SATURATING_SHADOWS = True

_ctx = None
_generation = 0
_mode = NORMAL
_frame_open = False
_wanted = None
_error_banner = None

_solid_prog = None
_tex_prog = None
_post_prog = None
_glow_prog = None
_solid_vbo = _solid_vao = None
_tex_vbo = _tex_vao = None
_glow_vbo = _glow_vao = None
_quad_vao = None
_shadow_open = False

# C-backed float arrays, not Python lists. A busy frame pushes a few hundred
# thousand floats through here, and `np.asarray` on a list of that many is
# tens of milliseconds on its own - more than the entire frame budget.
# `array('f')` extends at C speed and hands its bytes straight to the buffer.
_solid = array('f')         # pending solid vertices
_tex = array('f')           # pending textured vertices
_glow = array('f')          # pending analytic lights
_tex_current = None         # texture the pending batch belongs to

_targets = {}
_targets_size = None
_target_name = None
_viewport = (1, 1)
# The real drawable, as the host measured it. `ctx.screen.size` is stale from
# the moment the window is resized; see `set_viewport`.
_screen = (1, 1)

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
# A light drawn as maths rather than as a picture.
#
# The lantern used to be a baked sprite: a radial ramp rasterised into 8-bit
# RGBA and stretched to the radius. Eight bits over a 250-pixel radius is
# about one level of alpha every three pixels, and the eye finds those steps
# without any trouble at all - they are the rings. Dithering the bake scatters
# the step but cannot add levels that are not there, and stretching one sprite
# to many radii resamples whatever contour it has.
#
# Here the falloff is evaluated at every pixel in float, from the pixel's own
# distance to the light. There is no texture, no quantisation and no resample,
# so there is nothing left to band: the profile is exactly the curve, to the
# precision of the render target. The curve itself is unchanged.
_GLOW_VS = '''#version 330
uniform vec2 viewport;
in vec2 in_pos;
in vec2 in_local;        // -1..1 across the light's own square
in vec4 in_col;
in vec4 in_shape;        // profile, power, core, height/radius
out vec2 v_local;
out vec4 v_col;
out vec4 v_shape;
void main() {
    vec2 ndc = vec2(in_pos.x / viewport.x * 2.0 - 1.0,
                    1.0 - in_pos.y / viewport.y * 2.0);
    gl_Position = vec4(ndc, 0.0, 1.0);
    v_local = in_local;
    v_col = in_col;
    v_shape = in_shape;
}
'''

_GLOW_FS = '''#version 330
uniform sampler2D normals;
uniform vec2 target_px;
uniform float relief;
in vec2 v_local;
in vec4 v_col;
in vec4 v_shape;
out vec4 frag;
void main() {
    float d = length(v_local);
    if (d >= 1.0) discard;
    float f = 1.0 - d;
    float a;
    if (v_shape.x < 0.5) {
        // The lantern. Three terms: a hot core, the usable pool, and a tail
        // that reaches zero with zero slope - which is what keeps the light
        // from ending in a visible circle.
        a = 0.44 * pow(f, 1.7) + 0.34 * pow(f, 3.2) + 0.26 * pow(f, 6.5);
    } else {
        // Everything else: one power curve, with an optional flat core.
        float core = v_shape.z;
        float t = core > 0.0 ? clamp((1.0 - d - core) / max(1e-4, 1.0 - core),
                                     0.0, 1.0) + (d < core ? 1.0 : 0.0)
                             : f;
        a = pow(clamp(t, 0.0, 1.0), v_shape.y);
    }
    a = clamp(a, 0.0, 1.0) * v_col.a;

    // How this light meets the surface it lands on. `v_local` is already the
    // offset from the light in units of its own radius, so the direction back
    // to the flame is just its negation, and the fourth shape term is how
    // high the light hangs in the same units. Every light does this for
    // itself, which is the point: a brazier off to one side rakes the stone
    // from the side, and the courses it picks out are not the ones the
    // lantern picks out from where the player is standing.
    //
    // A height of zero opts out - dust hanging in the air is not lying on
    // anything, and neither is a bolt in flight.
    if (v_shape.w > 0.0 && relief > 0.0) {
        vec4 nt = texture(normals, gl_FragCoord.xy / target_px);
        if (nt.a > 0.02) {
            vec3 nrm = nt.xyz * 2.0 - 1.0;
            if (dot(nrm, nrm) > 0.04) {
                nrm = normalize(nrm);
                vec3 L = normalize(vec3(-v_local, v_shape.w));
                // Against what a flat surface at the same spot would take, so
                // unshaped ground comes back at exactly 1.0 and the exposure
                // of the room does not move with this.
                float ratio = clamp(dot(nrm, L) / max(L.z, 1e-3), 0.0, 2.2);
                a *= mix(1.0, ratio, relief);
            }
        }
    }
    frag = vec4(v_col.rgb * a, a);      // premultiplied, like every sprite
}
'''

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
uniform int mode;          // 0 tone map, 1 bright, 2 blur, 3 copy,
                           // 4 shadow, 6 masked bleed, 7 tent
uniform vec2 texel;
uniform float threshold;
uniform float dither;
uniform vec2 light_xy;     // key light, in this target's pixels
uniform float light_h;     // and how far above the floor it hangs
uniform float relief;      // how much of the surface slope to believe
uniform vec3 grade_shadow;    // what the bottom of the range is tinted toward
uniform vec3 grade_high;      // and the top
uniform float saturation;
in vec2 v_uv;
out vec4 frag;

// Interleaved gradient noise: one cheap hash whose output is spatially well
// spread, so the error it scatters looks like fine grain rather than like a
// pattern laid over the picture.
float ign(vec2 p) {
    return fract(52.9829189 * fract(dot(p, vec2(0.06711056, 0.00583715))));
}

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
    } else if (mode == 7) {
        // A tent, for folding a small bloom level back into a larger one. A
        // straight bilinear blit leaves the box the hardware sampled with;
        // nine taps in a 3x3 tent is the standard fix and costs nothing at
        // these sizes.
        vec3 acc = texture(scene, v_uv).rgb * 4.0;
        acc += texture(scene, v_uv + vec2(texel.x, 0.0)).rgb * 2.0;
        acc += texture(scene, v_uv - vec2(texel.x, 0.0)).rgb * 2.0;
        acc += texture(scene, v_uv + vec2(0.0, texel.y)).rgb * 2.0;
        acc += texture(scene, v_uv - vec2(0.0, texel.y)).rgb * 2.0;
        acc += texture(scene, v_uv + texel).rgb;
        acc += texture(scene, v_uv - texel).rgb;
        acc += texture(scene, v_uv + vec2(texel.x, -texel.y)).rgb;
        acc += texture(scene, v_uv + vec2(-texel.x, texel.y)).rgb;
        frag = vec4(acc / 16.0, 1.0);
    } else if (mode == 6) {
        // `scene` is the light here and `bloom` is the albedo buffer, whose
        // alpha says whether a solid thing stands at this pixel.
        float solid = texture(bloom, v_uv).a;
        float k = mix(1.0, threshold, clamp(solid, 0.0, 1.0));
        frag = vec4(texture(scene, v_uv).rgb * bloom_amount * k, 1.0);
    } else if (mode == 4) {
        // The shadow buffer holds one pass per channel, each already
        // saturated by MAX blending, so overlapping occluders cannot darken
        // a pixel more than one of them would. The sum is how many of the
        // three offset passes reached this pixel - 3 in the umbra, 1 or 2
        // through the penumbra, 0 in the open - and `threshold` carries the
        // darkening one pass is worth.
        vec3 s = texture(scene, v_uv).rgb;
        float n = s.r + s.g + s.b;
        float f = pow(threshold, n);
        frag = vec4(f, f, f, 1.0);
    } else {
        vec3 c = texture(scene, v_uv).rgb;
        c += texture(bloom, v_uv).rgb * bloom_amount;
        vec3 col = tonemap(c);
        // Grade. Split-toning the ends of the range against each other is
        // what gives a picture a temperature rather than a tint: the dark of
        // this game goes further blue, the lantern's own light further amber,
        // and the distance between them is the whole mood.
        float l = dot(col, vec3(0.2126, 0.7152, 0.0722));
        col = mix(vec3(l), col, saturation);
        col *= mix(grade_shadow, grade_high, smoothstep(0.0, 0.72, l));
        col = clamp(col, 0.0, 1.0);
        // Everything up to here is float, and none of it bands. The screen
        // is not: it takes eight bits, and the lantern's falloff crosses a
        // level every few pixels near its reach, which is exactly the
        // condition that draws a contour ring. Rounding is what makes the
        // ring - every pixel in a band commits the same error - so the error
        // is scattered instead, by less than one level, before the hardware
        // rounds. Two samples summed give a triangular distribution, which
        // leaves no residual pattern of its own.
        float r0 = ign(gl_FragCoord.xy);
        float r1 = ign(gl_FragCoord.xy + vec2(37.0, 17.0));
        col += vec3((r0 + r1 - 1.0) * dither);
        frag = vec4(col, 1.0);
    }
}
'''


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------
def active():
    return _ctx is not None


def renderer():
    return _ctx


def generation():
    return _generation


def attach(context):
    """Adopt a GL context. Every texture built for the previous one is void."""
    global _ctx, _generation, _mode, _frame_open, _error_banner
    global _solid_prog, _tex_prog, _post_prog, _glow_prog
    global _solid_vbo, _solid_vao, _tex_vbo, _tex_vao, _quad_vao
    global _glow_vbo, _glow_vao, _shadow_open
    _ctx = context
    _generation += 1
    _mode = NORMAL
    _frame_open = False
    _error_banner = None
    del _solid[:]
    del _tex[:]
    del _glow[:]
    _shadow_open = False
    _drop_targets()
    if context is None:
        _solid_prog = _tex_prog = _post_prog = _glow_prog = None
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
    _glow_prog = context.program(vertex_shader=_GLOW_VS,
                                 fragment_shader=_GLOW_FS)
    # Sized for the dust field, which is the only thing that fills it:
    # thousands of points, six vertices each.
    _glow_vbo = context.buffer(reserve=12 * 4 * 6 * 12000)
    _glow_vao = context.vertex_array(
        _glow_prog, [(_glow_vbo, '2f 2f 4f 4f',
                      'in_pos', 'in_local', 'in_col', 'in_shape')])
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
    if len(_glow):
        STATS['flush'] += 1
        _glow_vbo.write(_glow.tobytes())
        _glow_prog['viewport'].value = _viewport
        # Lights read the surface they are landing on, so the normal buffer
        # has to be bound whenever one is drawn.
        if _targets:
            _targets['normal'][0].use(3)
            try:
                _glow_prog['normals'].value = 3
                _glow_prog['target_px'].value = (float(_targets_size[0]),
                                                 float(_targets_size[1]))
                _glow_prog['relief'].value = RELIEF
            except KeyError:
                pass
        _glow_vao.render(vertices=len(_glow) // 12)
        del _glow[:]
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


def _push_tex_quad(tex, corners, col, flip_v=False):
    """A textured quad. `flip_v` is for sampling a render target.

    A sprite and a render target do not agree on which way up they are. A
    sprite is uploaded with its first row first, and GL calls that row v=0, so
    mapping v=0 to the top of the quad draws it the right way up. A target is
    *rendered into* through this same shader, which puts screen-y 0 at the top
    of the viewport - and the top of the viewport is v=1. Sampling it with the
    sprite convention therefore draws the world upside down.
    """
    global _tex_current
    if tex is not _tex_current:
        flush()
        _tex_current = tex
    r, g, b, a = col
    (p0, p1, p2, p3) = corners
    v0, v1 = (1.0, 0.0) if flip_v else (0.0, 1.0)
    for (x, y), (u, v) in ((p0, (0.0, v0)), (p1, (1.0, v0)), (p2, (1.0, v1)),
                           (p0, (0.0, v0)), (p2, (1.0, v1)), (p3, (0.0, v1))):
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


LANTERN_PROFILE = 0.0
POWER_PROFILE = 1.0

# How much of a surface's slope each light believes. All of it lights stone
# like corrugated iron; none of it is the flat pool this replaces.
RELIEF = 0.45

# One light's square is 6 vertices; beyond this many in a frame the batch is
# flushed early rather than grown.
_GLOW_BATCH_MAX = 500


def radial_glow(cx, cy, radius, color, opacity=100, profile=LANTERN_PROFILE,
                power=2.0, core=0.0, height=0.0):
    """A light as an analytic falloff rather than a stretched sprite.

    Draws the light's bounding square and lets the fragment shader work out
    every pixel's distance for itself, so the profile has as many levels as
    the render target does instead of the 256 an 8-bit sprite can hold. That
    is the whole difference between this and `blit`: same curve, no rings.
    """
    if _ctx is None or radius <= 0.5 or opacity <= 0:
        return
    a = _alpha(opacity)
    if a <= 0.0:
        return
    r, g, b = (c / 255.0 for c in color[:3])
    left, top = cx - radius, cy - radius
    right, bottom = cx + radius, cy + radius
    if len(_glow) >= _GLOW_BATCH_MAX * 12 * 6:
        flush()
    # In units of the light's own radius, which is the frame the shader works
    # in; zero means this light does not shade what it lands on.
    k_h = (height / radius) if (height > 0.0 and radius > 0.0) else 0.0
    corners = ((left, top, -1.0, -1.0), (right, top, 1.0, -1.0),
               (right, bottom, 1.0, 1.0), (left, bottom, -1.0, 1.0))
    for i, j, k in ((0, 1, 2), (0, 2, 3)):
        for idx in (i, j, k):
            x, y, lx, ly = corners[idx]
            _glow.extend((x, y, lx, ly, r, g, b, a, profile, power, core, k_h))


def radial_fan(cx, cy, radius, points, color, opacity=100, power=2.4,
               height=0.0):
    """The lit cone, as one triangle per ray of the visibility sweep.

    The CPU version of this fills the cone with forty concentric bands of
    quads, because a flat polygon is the only thing that renderer can draw and
    a single flat fill would end in a hard circle at the light's reach. That
    costs forty quads per ray - measured at 3.3 ms of a 9.3 ms frame - and it
    still bands, because each band is one integer opacity and the whole
    profile spans about eight of them.

    Here each ray is one triangle carrying its own distance from the flame,
    and the falloff is evaluated per pixel by the same shader the lights use.
    One fortieth of the geometry, and continuous rather than in eight steps.
    """
    if _ctx is None or radius <= 0.5 or opacity <= 0 or len(points) < 2:
        return
    a = _alpha(opacity)
    if a <= 0.0:
        return
    r, g, b = (c / 255.0 for c in color[:3])
    inv = 1.0 / radius
    k_h = (height / radius) if height > 0.0 else 0.0
    ext = _glow.extend
    # Round the whole circle, last point back to first. The sweep hands its
    # rays back sorted from angle zero, so stopping one short leaves the
    # wedge that straddles zero undrawn - a hard-edged bite out of the light
    # on the right-hand side of every static light in the game.
    n = len(points)
    for i in range(n):
        ax, ay = points[i]
        bx, by = points[(i + 1) % n]
        lax, lay = (ax - cx) * inv, (ay - cy) * inv
        lbx, lby = (bx - cx) * inv, (by - cy) * inv
        ext((cx, cy, 0.0, 0.0, r, g, b, a, POWER_PROFILE, power, 0.0, k_h,
             ax, ay, lax, lay, r, g, b, a, POWER_PROFILE, power, 0.0, k_h,
             bx, by, lbx, lby, r, g, b, a, POWER_PROFILE, power, 0.0, k_h))
    if len(_glow) >= _GLOW_BATCH_MAX * 12 * 6:
        flush()


def glow_points(xs, ys, radii, color, alphas, power=2.0, core=0.0,
                height=0.0):
    """Thousands of small lights in one call.

    `radial_glow` is fine for the handful of lights a chamber has, but a
    Python-level call per mote is the whole cost when there are two thousand
    of them. The vertex data is built with numpy here and handed to the batch
    as bytes, which is the same trick the draw batches use on the arrays they
    already keep.
    """
    if _ctx is None or len(xs) == 0:
        return
    x = np.asarray(xs, dtype=np.float32)
    y = np.asarray(ys, dtype=np.float32)
    r = np.asarray(radii, dtype=np.float32)
    a = np.clip(np.asarray(alphas, dtype=np.float32), 0.0, 1.0)
    keep = (r > 0.35) & (a > 0.004)
    if not keep.any():
        return
    x, y, r, a = x[keep], y[keep], r[keep], a[keep]
    n = x.size
    cr, cg, cb = (c / 255.0 for c in color[:3])

    # Two triangles per point: corners in the order (0,1,2) and (0,2,3).
    sx = np.array([-1.0, 1.0, 1.0, -1.0, -1.0, 1.0], dtype=np.float32)
    sy = np.array([-1.0, -1.0, 1.0, 1.0, -1.0, 1.0], dtype=np.float32)
    order = np.array([0, 1, 2, 0, 2, 3])
    lx = sx[order][None, :]
    ly = sy[order][None, :]

    v = np.empty((n, 6, 12), dtype=np.float32)
    v[:, :, 0] = x[:, None] + lx * r[:, None]
    v[:, :, 1] = y[:, None] + ly * r[:, None]
    v[:, :, 2] = lx
    v[:, :, 3] = ly
    v[:, :, 4] = cr
    v[:, :, 5] = cg
    v[:, :, 6] = cb
    v[:, :, 7] = a[:, None]
    v[:, :, 8] = POWER_PROFILE
    v[:, :, 9] = power
    v[:, :, 10] = core
    # Dust is in the air, not lying on the floor: it takes no surface term.
    v[:, :, 11] = np.where(r > 0.0, height / np.maximum(r, 1e-4), 0.0)[:, None] \
        if height > 0.0 else 0.0
    if len(_glow):
        flush()
    # One buffer write is the point of this call, but the buffer has a size;
    # anything past it goes in a second pass rather than being dropped.
    cap = 12000
    for lo in range(0, n, cap):
        del _glow[:]
        _glow.frombytes(v[lo:lo + cap].tobytes())
        flush()


# --------------------------------------------------------------------------
# Shadow coverage
# --------------------------------------------------------------------------
# Shadows used to be drawn straight into the light buffer, three offset copies
# of every occluder multiplying it down as they went. That is right for one
# occluder and wrong for two: where two shadows overlap the multiply compounds,
# so a sliver covered by six passes came out `shade**6` instead of `shade**3` -
# measured at fourteen times too dark, and clearly visible as a dark wedge
# running out of every corner where two shadows met.
#
# Occlusion does not compound. Two walls blocking the same patch of floor leave
# it exactly as dark as one of them would. So coverage is collected here
# instead, one pass per colour channel, with MAX blending - a channel that is
# already 1 stays 1 however many occluders land on it - and the light is
# multiplied down once, by `shade` raised to the number of passes that reached
# it. Three offset passes still give the penumbra; overlapping them no longer
# gives a hole.
_SHADOW_CHANNELS = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
_shadow_channel = _SHADOW_CHANNELS[0]
_shadow_shade = 1.0

SHADOW_PASSES = len(_SHADOW_CHANNELS)


def begin_shadow(shade):
    """Start collecting occlusion, away from the light buffer."""
    global _shadow_open, _shadow_shade
    if _ctx is None or not _targets:
        return False
    _shadow_shade = max(0.0, min(1.0, float(shade)))
    _use('shadow')
    _ctx.clear(0.0, 0.0, 0.0, 1.0)
    set_mode(MAX)
    _shadow_open = True
    return True


def shadow_pass(index):
    """Which of the offset passes the quads that follow belong to."""
    global _shadow_channel
    _shadow_channel = _SHADOW_CHANNELS[index % len(_SHADOW_CHANNELS)]


def shadow_quad(points):
    """One occluded quad, in pixels, into the current pass."""
    if _ctx is None or not _shadow_open or len(points) < 3:
        return
    r, g, b = _shadow_channel
    col = (r, g, b, 1.0)
    for i in range(1, len(points) - 1):
        _push_solid_tri(points[0], points[i], points[i + 1], col)


def end_shadow():
    """Apply the collected occlusion to the light buffer, once."""
    global _shadow_open
    if _ctx is None or not _shadow_open:
        return
    flush()
    _shadow_open = False
    _use('light')
    _ctx.blend_equation = _ctx.FUNC_ADD
    _ctx.blend_func = (_ctx.ZERO, _ctx.SRC_COLOR)
    tex = _targets['shadow'][0]
    tex.use(0)
    tex.use(1)
    _post_prog['scene'].value = 0
    _post_prog['bloom'].value = 1
    _post_prog['mode'].value = 4
    _post_prog['bloom_amount'].value = 0.0
    _post_prog['exposure'].value = _EXPOSURE
    _post_prog['threshold'].value = _shadow_shade
    _post_prog['texel'].value = (0.0, 0.0)
    _post_prog['dither'].value = 0.0
    _quad_vao.render(vertices=3)
    set_mode(NORMAL)
    _apply_blend()


def _set(name, value):
    """Set a post uniform, tolerating one the compiler dropped."""
    try:
        _post_prog[name].value = value
    except KeyError:
        pass


def begin_normal():
    """Start the surface-normal buffer.

    Cleared to nothing rather than to a flat normal: a pixel no layer covers
    has no surface, and the relief pass leaves those alone instead of lighting
    an imaginary floor behind them.
    """
    if _ctx is None or not _targets:
        return False
    _use('normal')
    set_mode(NORMAL)
    _ctx.clear(0.0, 0.0, 0.0, 0.0)
    return True


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
        for name in ('scene', 'light', 'final', 'edge', 'shadow',
                     'normal'):
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
        # Deliberately *not* `_ctx.screen.size`, and not whatever viewport
        # `use()` just restored from it - see `set_viewport`.
        _viewport = _screen
        try:
            _ctx.screen.viewport = (0, 0, _screen[0], _screen[1])
        except Exception:
            pass
    else:
        tex, fbo = _targets[name]
        fbo.use()
        _viewport = tex.size


def set_viewport(size):
    """Told by the host what the drawable is - and the only word for it.

    moderngl reads the default framebuffer's size once, when the context is
    created, and never revises it. `ctx.screen` therefore goes on describing
    the window the context was born in: 2560x1440 for a 1280x720 window that
    has since gone fullscreen into a 3600x2338 one.

    Believing it meant `screen.use()` restored that stale viewport on every
    frame, so the game drew into a 2560x1440 corner of a 3600x2338
    framebuffer and never touched the rest. OpenGL's origin is bottom-left,
    which is why the part it never reached showed up as a band across the
    *top* of the screen.
    """
    global _viewport, _screen
    _screen = (max(1, int(size[0])), max(1, int(size[1])))
    _viewport = _screen


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
    """Show the frame. Refuses to present one that was not drawn.

    Same reasoning as the SDL backend: more than one thing can reach a
    present, and showing a buffer nobody drew into is at best a stale frame.
    """
    global _frame_open
    if _ctx is None or not _frame_open:
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
    w, h = _screen
    raw = _ctx.screen.read(viewport=(0, 0, w, h), components=3, alignment=1)
    surf = pygame.image.frombuffer(raw, (w, h), 'RGB')
    return pygame.transform.flip(surf, False, True)


def begin_scene(background):
    _use('scene')
    set_mode(NORMAL)
    r, g, b = background
    # Alpha starts at nothing and is used as a coverage mask - see
    # `scene_coverage`. It is not the scene's opacity; the scene is opaque.
    # The mask has to be opened before the clear or the alpha the last frame
    # left behind survives into this one and never resets.
    scene_coverage(True)
    _ctx.clear(r / 255.0, g / 255.0, b / 255.0, 0.0)
    scene_coverage(False)


def scene_coverage(on):
    """Whether what is drawn next counts as a solid thing standing in the room.

    The lit floor is mostly not floor. Measured in a lit pool, two thirds of
    what you see there is the light added over the stone rather than the stone
    itself - which is the point of that term, and why a lit floor reads as lit
    instead of merely visible. The trouble is that the same amount is added
    over everything else too, and a figure whose whole design is a near-black
    silhouette has almost no albedo of its own to compete with it. It comes
    out as a warm haze in the shape of a person, with the floor's carving
    still legible through it.

    So the scene buffer's alpha, which nothing else was using, records whether
    a solid thing was drawn at each pixel. The stone layers leave it alone;
    people, walls' furniture and projectiles set it; and the composite holds
    the added light back where it is set. Everything keeps its own light.
    """
    if _ctx is None or not _targets:
        return
    flush()
    fbo = _targets['scene'][1]
    fbo.color_mask = (True, True, True, bool(on))
    # moderngl applies a colour mask when the framebuffer is bound, not when
    # it is set - verified: setting it and drawing without rebinding writes
    # alpha anyway. Rebinding is what makes it take.
    if _target_name == 'scene':
        fbo.use()


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
                   (tint, tint, tint, tint), flip_v=True)
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
        _bleed_pass(bleed, size)

    if bloom > 0.0:
        _bloom_chain()
        _use(None)
        _post(scene='final', bloom='bloom0', mode=0, amount=bloom)
    else:
        _use(None)
        _post(scene='final', bloom='final', mode=0, amount=0.0)


# What share of the added light still lands on a solid thing. Not zero: a
# figure standing in the middle of a lit room is in that light too, and
# cutting it out entirely puts a hole in the pool in the shape of the player.
BLEED_ON_SOLIDS = 0.10


def _bleed_pass(amount, size):
    """Add the light over the room, but not over what is standing in it."""
    flush()
    _ctx.blend_equation = _ctx.FUNC_ADD
    _ctx.blend_func = (_ctx.ONE, _ctx.ONE)
    _targets['light'][0].use(0)
    _targets['scene'][0].use(1)
    _set('scene', 0)
    _set('bloom', 1)
    _set('mode', 6)
    _set('bloom_amount', amount)
    _set('threshold', BLEED_ON_SOLIDS)
    _set('exposure', _EXPOSURE)
    _set('texel', (0.0, 0.0))
    _set('dither', 0.0)
    _quad_vao.render(vertices=3)
    _apply_blend()


def _post(scene, bloom, mode, amount=0.0, threshold=1.0, texel=(0.0, 0.0),
          additive=False):
    flush()
    _ctx.blend_equation = _ctx.FUNC_ADD
    # Most passes own their target outright; folding a bloom level back into
    # a bigger one has to add to what is already there.
    _ctx.blend_func = ((_ctx.ONE, _ctx.ONE) if additive
                       else (_ctx.ONE, _ctx.ZERO))
    _targets[scene][0].use(0)
    _targets[bloom][0].use(1)
    _post_prog['scene'].value = 0
    _post_prog['bloom'].value = 1
    _post_prog['mode'].value = mode
    _post_prog['bloom_amount'].value = amount
    _post_prog['exposure'].value = _EXPOSURE
    _post_prog['threshold'].value = threshold
    _post_prog['texel'].value = texel
    _post_prog['dither'].value = _DITHER
    _set('grade_shadow', GRADE_SHADOW)
    _set('grade_high', GRADE_HIGH)
    _set('saturation', SATURATION)
    _quad_vao.render(vertices=3)
    _apply_blend()


# In float there is no clipping to design around, so exposure is a real
# control rather than the two hand-tuned constants the 8-bit path needs.
_EXPOSURE = 1.05
_BLOOM_THRESHOLD = 0.75
# Sub-level noise applied just before the screen's own 8-bit rounding. One
# level is 1/255; a touch over half that either side is enough to destroy a
# contour without being visible as grain.
_DITHER = 0.6 / 255.0

# The grade. Shadows toward slate, highlights toward the lantern's amber, and
# a touch under full saturation so nothing in the palette shouts.
GRADE_SHADOW = (0.86, 0.94, 1.12)
GRADE_HIGH = (1.06, 1.00, 0.93)
SATURATION = 0.94

# There was an anamorphic streak here: a long horizontal blur of the bright
# pass, on the reasoning that a lens in front of something this much brighter
# than everything around it would smear it sideways. Two problems. There is no
# lens - the camera is a top-down abstraction, not a thing in the room - and a
# coherent horizontal band across a smooth radial gradient is visible far
# below the contrast the numbers suggest. Measured it was four to eight per
# cent of the vertical brightness at the same radius, and it read as a beam of
# light lying across the floor through the player.


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
        src = _targets[f'bloom{i}'][0]
        _post(scene=f'bloom{i}', bloom=f'bloom{i}', mode=7, additive=True,
              texel=(1.0 / src.size[0], 1.0 / src.size[1]))


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
