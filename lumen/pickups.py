"""Drops: embers, oil, and mercy.

Pickups drift, then home in once you are close enough - the magnetism is what
makes clearing a room feel like collecting rather than vacuuming.
"""

import math

from .draw import drawImage, drawPolygon

from . import art, audio, palette
from .mathx import clamp

EMBER = 'ember'
OIL = 'oil'
HEART = 'heart'

_SPEC = {
    EMBER: ((255, 214, 92), palette.XP, 7.0),
    OIL: ((255, 170, 80), palette.LIGHT_WARM, 9.0),
    HEART: ((103, 245, 180), palette.HEAL, 9.0),
}


class Pickup:
    __slots__ = ('kind', 'x', 'y', 'vx', 'vy', 'alive', 'life', 'phase',
                 'value', 'homing')

    def __init__(self, kind, x, y, vx, vy, value):
        self.kind = kind
        self.x, self.y = x, y
        self.vx, self.vy = vx, vy
        self.value = value
        self.alive = True
        self.life = 26.0
        self.phase = 0.0
        self.homing = False


class PickupField:
    def __init__(self):
        self.items = []

    def clear(self):
        self.items.clear()

    def spawn(self, kind, x, y, value, rng, count=1, speed=(40, 150)):
        for _ in range(count):
            a = rng.angle()
            sp = rng.uniform(*speed)
            self.items.append(Pickup(kind, x, y, math.cos(a) * sp,
                                     math.sin(a) * sp, value))
        if len(self.items) > 260:
            del self.items[:len(self.items) - 260]

    def update(self, dt, player, level, particles, rng, on_collect, flow=None):
        radius = player.stats.pickup_radius
        px, py = player.x, player.y
        keep = []
        for it in self.items:
            it.life -= dt
            it.phase += dt
            if it.life <= 0.0:
                continue

            dx = px - it.x
            dy = py - it.y
            d2 = dx * dx + dy * dy

            if d2 < radius * radius:
                it.homing = True
            if it.homing:
                d = math.sqrt(d2) if d2 > 1e-6 else 1e-3
                # Embers route around walls too: an ember that drifted behind
                # a pillar used to press into it forever instead of coming to
                # you.
                hx, hy = dx / d, dy / d
                if flow is not None and d > 70.0 and level.ray_blocked(
                        it.x, it.y, px, py):
                    step = flow.direction_at(it.x, it.y)
                    if step is not None:
                        hx, hy = step
                pull = clamp(760.0 / max(d, 26.0), 3.0, 42.0)
                it.vx += hx * pull * 60.0 * dt
                it.vy += hy * pull * 60.0 * dt
            else:
                damp = math.exp(-3.2 * dt)
                it.vx *= damp
                it.vy *= damp

            nx = it.x + it.vx * dt
            ny = it.y + it.vy * dt
            if level.blocked(nx, ny, 5.0):
                nx, ny = level.collide_circle(nx, ny, 5.0)
                it.vx *= 0.4
                it.vy *= 0.4
            it.x = clamp(nx, 10, level.width - 10)
            it.y = clamp(ny, 10, level.height - 10)

            if d2 < (player.radius + 11.0) ** 2:
                on_collect(it)
                particles.burst(it.x, it.y, 6, _SPEC[it.kind][1], rng,
                                speed=(60, 190), life=(0.14, 0.32),
                                size=(1.6, 3.2))
                audio.play_at('pickup', it.x, it.y,
                              0.32 if it.kind == EMBER else 0.5)
                continue

            keep.append(it)
        self.items = keep

    def draw(self, ox, oy, view_w, view_h):
        for it in self.items:
            sx = it.x - ox
            sy = it.y - oy
            if sx < -40 or sy < -40 or sx > view_w + 40 or sy > view_h + 40:
                continue
            glow_rgb, core, size = _SPEC[it.kind]
            bob = math.sin(it.phase * 3.4) * 2.0
            sy += bob
            fade = clamp(it.life / 3.0, 0.25, 1.0)

            g = 48
            drawImage(art.glow(glow_rgb, g, power=2.2),
                      int(sx - g * 0.5), int(sy - g * 0.5),
                      opacity=int(40 * fade))

            if it.kind == HEART:
                s = size
                drawPolygon(sx, sy - s * 0.5, sx + s * 0.5, sy - s,
                            sx + s, sy - s * 0.2, sx, sy + s,
                            sx - s, sy - s * 0.2, sx - s * 0.5, sy - s,
                            fill=core, opacity=int(95 * fade))
            elif it.kind == OIL:
                s = size
                drawPolygon(sx, sy - s, sx + s * 0.62, sy, sx, sy + s,
                            sx - s * 0.62, sy, fill=core, opacity=int(95 * fade))
            else:
                s = size * (0.86 + 0.14 * math.sin(it.phase * 5.0))
                a = it.phase * 1.6
                ca, sa = math.cos(a), math.sin(a)
                drawPolygon(sx + ca * s, sy + sa * s,
                            sx - sa * s * 0.6, sy + ca * s * 0.6,
                            sx - ca * s, sy - sa * s,
                            sx + sa * s * 0.6, sy - ca * s * 0.6,
                            fill=core, opacity=int(96 * fade))
