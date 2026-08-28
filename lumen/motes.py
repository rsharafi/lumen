"""Dust hanging in the lantern's light.

The shafts made the air visible as a shape - a cone of light with edges where
the walls cut it. What they could not do is make the air feel occupied, and
the reason a real beam reads as a beam is that there is something in it: dust
turning over, catching the light for a moment, going dark again.

Every mote lives in world space rather than on screen, so they hold still
while the camera moves and drift on their own. They are drawn into the albedo
rather than into the light buffer, which is the whole trick: a mote is a
bright speck of nothing, and the lighting decides whether you can see it. In
the dark there is no dust. Walk the lantern into a room and the air fills.
"""

import math

import numpy as np

from . import draw, gpu

# Enough that a lit room looks occupied, few enough that the whole field is
# one buffer write. They are only ever built for the area the camera can see
# plus a margin, so the count is per-screen and not per-level.
COUNT = 7000

# Design units. Big enough to catch a pixel or two at native scale; any
# larger and they stop being dust and start being snow.
SIZE_MIN = 0.7
SIZE_MAX = 2.4

DRIFT = 5.5             # units a second, before the swirl
SWIRL = 3.2             # how far the wander carries them off that line
BRIGHTNESS = 1.25


class Motes:
    """A field of drifting dust, wrapped to a box around the camera."""

    def __init__(self, rng, view_w, view_h):
        self.rng = rng
        self.resize(view_w, view_h)

    def resize(self, view_w, view_h):
        # A margin, so a mote does not appear at the edge of the screen the
        # moment the camera moves toward it.
        self.w = float(view_w) + 260.0
        self.h = float(view_h) + 260.0
        r = self.rng
        n = COUNT
        self.x = np.array([r.uniform(0.0, self.w) for _ in range(n)], np.float32)
        self.y = np.array([r.uniform(0.0, self.h) for _ in range(n)], np.float32)
        self.size = np.array(
            [r.uniform(SIZE_MIN, SIZE_MAX) for _ in range(n)], np.float32)
        self.phase = np.array(
            [r.uniform(0.0, math.tau) for _ in range(n)], np.float32)
        # Each mote drifts its own way, or the whole field slides as a sheet.
        self.vx = np.array([r.uniform(-1.0, 1.0) for _ in range(n)], np.float32)
        self.vy = np.array([r.uniform(-1.0, 1.0) for _ in range(n)], np.float32)
        speed = np.hypot(self.vx, self.vy)
        speed[speed < 1e-3] = 1.0
        self.vx /= speed
        self.vy /= speed
        # A per-mote brightness, so they do not all wink at once.
        self.gain = np.array(
            [r.uniform(0.35, 1.0) for _ in range(n)], np.float32)
        self.t = 0.0

    def step(self, dt):
        self.t += dt
        self.phase += dt * 1.7
        # Drift plus a slow wander, which is what keeps the field from
        # reading as a texture sliding across the room.
        self.x += (self.vx * DRIFT + np.cos(self.phase) * SWIRL) * dt
        self.y += (self.vy * DRIFT + np.sin(self.phase * 0.83) * SWIRL) * dt
        # Wrapped, not respawned: a mote that leaves the right of the box
        # comes back on the left with everything else about it unchanged.
        np.mod(self.x, self.w, out=self.x)
        np.mod(self.y, self.h, out=self.y)

    def draw(self, ox, oy, color, light_x, light_y, reach, strength=1.0):
        """Every mote near enough to the flame to be catching any of it."""
        if not hasattr(gpu, 'glow_points') or not getattr(
                gpu, 'ANALYTIC_LIGHTS', False):
            return 0
        if reach <= 1.0 or strength <= 0.0:
            return 0
        # The box is anchored to the camera, so world position is the offset
        # within it plus wherever the camera happens to be.
        bx = math.floor(ox / self.w) * self.w
        by = math.floor(oy / self.h) * self.h
        wx = self.x + bx
        wy = self.y + by
        # Bring round whichever copy of the field the camera is looking at.
        wx = np.where(wx < ox - 130.0, wx + self.w, wx)
        wy = np.where(wy < oy - 130.0, wy + self.h, wy)

        dx = wx - light_x
        dy = wy - light_y
        d2 = dx * dx + dy * dy
        near = d2 < reach * reach
        if not near.any():
            return 0
        wx, wy = wx[near], wy[near]
        f = 1.0 - np.sqrt(d2[near]) / reach
        # Dimmer than the falloff alone, and only where the light is strong:
        # dust at the edge of the reach is not visible and should not be.
        alpha = (f ** 2.6) * self.gain[near] * BRIGHTNESS * strength
        # A twinkle, from each mote's own phase, so the field is alive even
        # when nothing is moving.
        alpha *= 0.62 + 0.38 * np.sin(self.phase[near] * 2.3)
        scale = draw.SCALE
        gpu.glow_points((wx - ox) * scale, (wy - oy) * scale,
                        self.size[near] * scale, color, alpha, power=1.5)
        return int(near.sum())
