"""A pooled particle system.

Particles are the single largest draw-call consumer in the game, so the pool
is fixed-size and every particle draws as exactly one shape - a quad via
`drawPolygon`, which benchmarked cheapest per call of anything that can be
arbitrarily rotated. When the pool is full the oldest particle is recycled
rather than the list growing.
"""

import math

from .draw import drawPolygon

from . import art

from .config import MAX_PARTICLES, MAX_PARTICLES_DRAWN
from .mathx import clamp

_RING_POINTS = 18
_RING_STEP = math.tau / _RING_POINTS
_RING_COS = [math.cos(i * _RING_STEP) for i in range(_RING_POINTS)]
_RING_SIN = [math.sin(i * _RING_STEP) for i in range(_RING_POINTS)]

SPARK = 0      # thin fast streak, aligned to velocity
EMBER = 1      # drifting glowing mote, rises then fades
SMOKE = 2      # expanding soft puff
SHARD = 3      # spinning debris
DUST = 4       # slow ambient mote
RIPPLE = 5     # expanding square outline


class Particle:
    __slots__ = ('alive', 'kind', 'x', 'y', 'vx', 'vy', 'life', 'max_life',
                 'size', 'end_size', 'color', 'opacity', 'spin', 'angle',
                 'drag', 'gravity', 'stretch')

    def __init__(self):
        self.alive = False
        self.kind = SPARK
        self.x = self.y = 0.0
        self.vx = self.vy = 0.0
        self.life = self.max_life = 0.0
        self.size = self.end_size = 1.0
        self.color = None
        self.opacity = 100
        self.spin = self.angle = 0.0
        self.drag = 0.0
        self.gravity = 0.0
        self.stretch = 1.0


class ParticleSystem:
    def __init__(self, capacity=MAX_PARTICLES):
        self.pool = [Particle() for _ in range(capacity)]
        self.capacity = capacity
        self._cursor = 0
        self.live = 0

    def clear(self):
        for p in self.pool:
            p.alive = False
        self.live = 0

    def _take(self):
        pool = self.pool
        n = self.capacity
        start = self._cursor
        for i in range(n):
            idx = (start + i) % n
            if not pool[idx].alive:
                self._cursor = (idx + 1) % n
                return pool[idx]
        # Everything is busy: steal the slot with the least life left.
        idx = min(range(n), key=lambda i: pool[i].life)
        self._cursor = (idx + 1) % n
        return pool[idx]

    def emit(self, kind, x, y, vx, vy, life, size, color, end_size=None,
             opacity=100, spin=0.0, drag=2.0, gravity=0.0, stretch=1.0,
             angle=None):
        p = self._take()
        if not p.alive:
            self.live += 1
        p.alive = True
        p.kind = kind
        p.x, p.y = x, y
        p.vx, p.vy = vx, vy
        p.life = p.max_life = life
        p.size = size
        p.end_size = size * 0.15 if end_size is None else end_size
        p.color = color
        p.opacity = opacity
        p.spin = spin
        p.angle = math.atan2(vy, vx) if angle is None else angle
        p.drag = drag
        p.gravity = gravity
        p.stretch = stretch
        return p

    # ---------------------------------------------------------- emitters --
    def burst(self, x, y, count, color, rng, speed=(90, 320), life=(0.22, 0.55),
              size=(2.0, 4.6), kind=SPARK, stretch=3.4, spread=math.tau,
              direction=0.0, drag=3.4, gravity=0.0):
        for _ in range(count):
            a = direction + rng.uniform(-spread * 0.5, spread * 0.5)
            sp = rng.uniform(*speed)
            self.emit(kind, x, y, math.cos(a) * sp, math.sin(a) * sp,
                      rng.uniform(*life), rng.uniform(*size), color,
                      stretch=stretch, drag=drag, gravity=gravity)

    def embers(self, x, y, count, color, rng, spread=math.tau, direction=0.0,
               speed=(20, 90), life=(0.5, 1.3)):
        for _ in range(count):
            a = direction + rng.uniform(-spread * 0.5, spread * 0.5)
            sp = rng.uniform(*speed)
            self.emit(EMBER, x, y, math.cos(a) * sp, math.sin(a) * sp,
                      rng.uniform(*life), rng.uniform(1.6, 3.4), color,
                      end_size=0.5, drag=1.1, gravity=-26.0, stretch=1.0)

    def smoke(self, x, y, count, color, rng, speed=(8, 46), life=(0.5, 1.1),
              size=(9, 20)):
        for _ in range(count):
            a = rng.angle()
            sp = rng.uniform(*speed)
            self.emit(SMOKE, x, y, math.cos(a) * sp, math.sin(a) * sp,
                      rng.uniform(*life), rng.uniform(*size), color,
                      end_size=rng.uniform(24, 46), opacity=26, drag=1.6,
                      spin=rng.uniform(-1.2, 1.2))

    def shards(self, x, y, count, color, rng, speed=(60, 260)):
        for _ in range(count):
            a = rng.angle()
            sp = rng.uniform(*speed)
            self.emit(SHARD, x, y, math.cos(a) * sp, math.sin(a) * sp,
                      rng.uniform(0.35, 0.8), rng.uniform(2.5, 5.5), color,
                      end_size=1.0, drag=2.6, spin=rng.uniform(-14, 14),
                      stretch=1.5)

    def ripple(self, x, y, color, size, life=0.34, opacity=70):
        self.emit(RIPPLE, x, y, 0.0, 0.0, life, size * 0.25, color,
                  end_size=size, opacity=opacity, drag=0.0)

    def dust(self, x, y, rng, color):
        a = rng.angle()
        sp = rng.uniform(3, 16)
        self.emit(DUST, x, y, math.cos(a) * sp, math.sin(a) * sp,
                  rng.uniform(2.5, 6.0), rng.uniform(1.0, 2.1), color,
                  end_size=0.6, opacity=40, drag=0.25)

    # ------------------------------------------------------------ update --
    def update(self, dt, level=None):
        live = 0
        for p in self.pool:
            if not p.alive:
                continue
            p.life -= dt
            if p.life <= 0.0:
                p.alive = False
                continue
            damp = math.exp(-p.drag * dt)
            p.vx *= damp
            p.vy *= damp
            p.vy += p.gravity * dt
            nx = p.x + p.vx * dt
            ny = p.y + p.vy * dt
            if level is not None and p.kind in (SPARK, SHARD):
                if level.is_wall_tile(int(nx // 64), int(ny // 64)):
                    # Cheap bounce off masonry: kill most of the energy.
                    p.vx *= -0.35
                    p.vy *= -0.35
                    nx, ny = p.x, p.y
                    p.life *= 0.6
            p.x, p.y = nx, ny
            if p.spin:
                p.angle += p.spin * dt
            elif p.kind == SPARK:
                sp2 = p.vx * p.vx + p.vy * p.vy
                if sp2 > 1.0:
                    p.angle = math.atan2(p.vy, p.vx)
            live += 1
        self.live = live

    # -------------------------------------------------------------- draw --
    def draw(self, ox, oy, view_w, view_h, lit_test=None):
        """Draw live particles, camera-relative, culled to the viewport.

        Above `MAX_PARTICLES_DRAWN` the most-faded ones are dropped: they are
        already nearly transparent, so the burst still reads at full strength
        while the frame cost stays bounded.
        """
        fade_floor = 0.0
        if self.live > MAX_PARTICLES_DRAWN:
            fade_floor = 1.0 - MAX_PARTICLES_DRAWN / self.live

        for p in self.pool:
            if not p.alive:
                continue
            sx = p.x - ox
            sy = p.y - oy
            if sx < -60 or sy < -60 or sx > view_w + 60 or sy > view_h + 60:
                continue

            t = p.life / p.max_life if p.max_life > 0 else 0.0
            if t < fade_floor and p.kind != RIPPLE:
                continue
            size = p.end_size + (p.size - p.end_size) * t
            if size < 0.35:
                continue

            fade = t * t if p.kind in (SPARK, SHARD) else t
            opacity = int(clamp(p.opacity * fade, 0, 100))
            if opacity <= 1:
                continue

            if p.kind == RIPPLE:
                # A stroked polygon: one shape, and round. Four bars forming a
                # square read as a UI box, and a scaled ring sprite costs a
                # rescale per ripple - several at once during a multi-kill was
                # enough to drop a frame.
                r = p.end_size + (p.size - p.end_size) * (1.0 - t)
                pts = []
                for k in range(_RING_POINTS):
                    a = k * _RING_STEP
                    pts.append(sx + _RING_COS[k] * r)
                    pts.append(sy + _RING_SIN[k] * r)
                drawPolygon(*pts, fill=None, border=p.color,
                            borderWidth=max(1.5, r * 0.07), opacity=opacity)
                continue

            length = size * (p.stretch if p.kind == SPARK else 1.0)
            if p.kind == SPARK:
                speed = math.hypot(p.vx, p.vy)
                length = size * (1.0 + p.stretch * clamp(speed / 420.0, 0.0, 1.0))

            ca = math.cos(p.angle)
            sa = math.sin(p.angle)
            hx, hy = ca * length * 0.5, sa * length * 0.5
            wx, wy = -sa * size * 0.5, ca * size * 0.5
            drawPolygon(sx - hx - wx, sy - hy - wy,
                        sx + hx - wx, sy + hy - wy,
                        sx + hx + wx, sy + hy + wy,
                        sx - hx + wx, sy - hy + wy,
                        fill=p.color, opacity=opacity)
