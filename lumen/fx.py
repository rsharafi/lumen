"""Camera, screen shake, hit-stop, floating numbers, and transient lights.

These are the systems that make a hit feel like a hit. None of them touch game
logic - if every one were switched off the game would play identically, it
would just feel dead.
"""

import math

from . import gpu
from .draw import drawImage, drawLine, drawPolygon

from . import art, palette
from .config import CAMERA_LERP, CAMERA_LOOKAHEAD, HITSTOP_MAX, SHAKE_DECAY
from .mathx import clamp, ease_out_cubic


class Camera:
    def __init__(self, view_w, view_h):
        self.x = 0.0
        self.y = 0.0
        self.view_w = view_w
        self.view_h = view_h
        self.shake = 0.0
        self.shake_x = 0.0
        self.shake_y = 0.0
        self.bounds_w = view_w
        self.bounds_h = view_h
        self._t = 0.0

    def snap_to(self, x, y):
        self.x = x - self.view_w * 0.5
        self.y = y - self.view_h * 0.5
        self._clamp()

    def set_bounds(self, w, h):
        self.bounds_w = w
        self.bounds_h = h

    def _clamp(self):
        max_x = max(0.0, self.bounds_w - self.view_w)
        max_y = max(0.0, self.bounds_h - self.view_h)
        self.x = clamp(self.x, 0.0, max_x)
        self.y = clamp(self.y, 0.0, max_y)
        # Centre the chamber when it is smaller than the window.
        if self.bounds_w < self.view_w:
            self.x = (self.bounds_w - self.view_w) * 0.5
        if self.bounds_h < self.view_h:
            self.y = (self.bounds_h - self.view_h) * 0.5

    def follow(self, tx, ty, aim_x, aim_y, dt):
        goal_x = tx + aim_x * CAMERA_LOOKAHEAD - self.view_w * 0.5
        goal_y = ty + aim_y * CAMERA_LOOKAHEAD - self.view_h * 0.5
        k = 1.0 - math.exp(-(CAMERA_LERP * 60.0) * dt)
        self.x += (goal_x - self.x) * k
        self.y += (goal_y - self.y) * k
        self._clamp()

        self._t += dt
        if self.shake > 0.01:
            self.shake = max(0.0, self.shake - SHAKE_DECAY * self.shake * dt
                             - 0.55 * dt)
            amp = self.shake
            # Two out-of-phase sines read as a jolt rather than random jitter.
            self.shake_x = math.sin(self._t * 61.0) * amp + math.sin(self._t * 37.0) * amp * 0.6
            self.shake_y = math.cos(self._t * 53.0) * amp + math.cos(self._t * 29.0) * amp * 0.6
        else:
            self.shake = 0.0
            self.shake_x = self.shake_y = 0.0

    def add_shake(self, amount):
        self.shake = min(26.0, self.shake + amount)

    @property
    def ox(self):
        # Integer-aligned on the cmu-graphics renderer, on purpose: it blits an
        # image ~15x faster when the destination lands on whole pixels, and at
        # a fractional offset falls back to resampling - the two full-chamber
        # layers alone cost about 4 ms a frame that way.
        #
        # On the GPU it is exactly the wrong thing. Sampling between texels is
        # free there, while snapping quantises the whole world to a design
        # unit - about three physical pixels at native scale - so the ground
        # lurches in steps while the player, drawn at exact coordinates, moves
        # smoothly. At 120 fps that reads as the screen juddering as you walk.
        if gpu.active():
            return self.x + self.shake_x
        return float(int(self.x + self.shake_x))

    @property
    def oy(self):
        if gpu.active():
            return self.y + self.shake_y
        return float(int(self.y + self.shake_y))


class FloatingText:
    __slots__ = ('x', 'y', 'vx', 'vy', 'life', 'max_life', 'text', 'color',
                 'size', 'bold')

    def __init__(self, x, y, text, color, size, vx, vy, life, bold):
        self.x, self.y = x, y
        self.vx, self.vy = vx, vy
        self.text = text
        self.color = color
        self.size = size
        self.life = self.max_life = life
        self.bold = bold


class Effects:
    """Everything transient that is not a particle."""

    def __init__(self, rng):
        self.rng = rng
        self.texts = []
        self.lights = []       # [x, y, radius, life, max_life, color]
        self.beams = []        # [x0, y0, x1, y1, life, max_life, color]
        self.hitstop = 0.0
        self.flash = 0.0
        self.flash_color = palette.LIGHT_CORE
        self.flash_wash = False
        self.chroma = 0.0
        self.time_scale = 1.0
        self.shake_request = 0.0
        self._slowmo = 0.0
        self._slowmo_target = 1.0

    def clear(self):
        self.texts.clear()
        self.lights.clear()
        self.beams.clear()
        self.hitstop = 0.0
        self.flash = 0.0
        self.chroma = 0.0
        self.shake_request = 0.0
        self._slowmo = 0.0
        self.time_scale = 1.0

    # ------------------------------------------------------------ inputs --
    def add_text(self, x, y, text, color, size=18, bold=False, rise=64.0):
        self.texts.append(FloatingText(
            x + self.rng.uniform(-6, 6), y, text, color, size,
            self.rng.uniform(-26, 26), -rise, 0.85, bold))
        if len(self.texts) > 40:
            del self.texts[0]

    def add_beam(self, x0, y0, x1, y1, color=palette.BEAM, life=0.14):
        """A short-lived line between two points - a chained arc.

        Emissive, so it is drawn with the lighting rather than under it, and
        it throws a little light of its own at each end: an arc that lights
        nothing reads as a decal rather than as electricity.
        """
        self.beams.append([x0, y0, x1, y1, life, life, color])
        if len(self.beams) > 40:
            del self.beams[0]

    def add_light(self, x, y, radius, life, color=palette.LIGHT_WARM):
        self.lights.append([x, y, radius, life, life, color])
        if len(self.lights) > 24:
            del self.lights[0]

    def add_hitstop(self, seconds):
        self.hitstop = min(HITSTOP_MAX, max(self.hitstop, seconds))

    def add_flash(self, strength, color=palette.LIGHT_CORE, wash=False):
        """Screen feedback. `wash` covers the frame (light bursts); the
        default is a rim pulse, which reads as impact without hiding the
        thing that just hit you."""
        self.flash = min(1.0, max(self.flash, strength))
        self.flash_color = color
        self.flash_wash = wash

    def add_shake(self, amount):
        """Queue camera shake; the game applies it to the live camera."""
        self.shake_request = max(self.shake_request, amount)

    def take_shake(self):
        amount = self.shake_request
        self.shake_request = 0.0
        return amount

    def add_chroma(self, strength):
        self.chroma = min(1.0, max(self.chroma, strength))

    def slowmo(self, seconds, scale=0.28):
        self._slowmo = max(self._slowmo, seconds)
        self._slowmo_target = scale

    # ------------------------------------------------------------ update --
    def update(self, dt):
        """Advance effects on *unscaled* time and report the gameplay scale."""
        if self.hitstop > 0.0:
            self.hitstop -= dt
            scale = 0.0
        elif self._slowmo > 0.0:
            self._slowmo -= dt
            scale = self._slowmo_target
        else:
            scale = 1.0
        self.time_scale = scale

        self.flash = max(0.0, self.flash - dt * 5.4)
        self.chroma = max(0.0, self.chroma - dt * 3.0)

        for t in self.texts:
            t.life -= dt
            t.x += t.vx * dt
            t.y += t.vy * dt
            t.vy += 150.0 * dt
        if self.texts:
            self.texts = [t for t in self.texts if t.life > 0.0]

        for beam in self.beams:
            beam[4] -= dt
        self.beams = [b for b in self.beams if b[4] > 0.0]

        for light in self.lights:
            light[3] -= dt
        if self.lights:
            self.lights = [l for l in self.lights if l[3] > 0.0]

        return scale

    # -------------------------------------------------------------- draw --
    def draw_lights(self, ox, oy, view_w, view_h):
        for x, y, radius, life, max_life, color in self.lights:
            t = life / max_life
            r = radius * (0.55 + 0.45 * ease_out_cubic(1.0 - t))
            sx, sy = x - ox, y - oy
            if sx < -r or sy < -r or sx > view_w + r or sy > view_h + r:
                continue
            art.draw_glow(color, sx, sy, r, clamp(72 * t, 0, 100), power=2.4)

    def draw_beams(self, ox, oy):
        """Arcs, over the lighting: they are their own light source."""
        for x0, y0, x1, y1, life, max_life, color in self.beams:
            t = clamp(life / max_life, 0.0, 1.0)
            drawLine(x0 - ox, y0 - oy, x1 - ox, y1 - oy, fill=color,
                     lineWidth=1.4 + 2.6 * t,
                     opacity=int(clamp(96 * t, 0, 100)))

    def draw_texts(self, ox, oy):
        for t in self.texts:
            frac = t.life / t.max_life
            opacity = int(clamp(frac * 130, 0, 100))
            if opacity <= 2:
                continue
            art.draw_label_sprite(t.text, t.x - ox, t.y - oy, 'ui', t.size,
                                  art.rgb_tuple(t.color), opacity=opacity)

    def draw_flash(self, view_w, view_h):
        if self.flash <= 0.01:
            return
        if self.flash_wash:
            drawPolygon(0, 0, view_w, 0, view_w, view_h, 0, view_h,
                        fill=self.flash_color,
                        opacity=int(clamp(self.flash * 46, 0, 100)))
            return
        sprite = art.vignette(view_w, view_h, 1.0, 0.26,
                              art.rgb_tuple(self.flash_color))
        drawImage(sprite, 0, 0, opacity=int(clamp(self.flash * 78, 0, 100)))



