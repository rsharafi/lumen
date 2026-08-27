"""Projectiles for both sides, and the weapon definitions that spawn them."""

import math

from .draw import drawImage, drawPolygon

from . import art, palette
from .mathx import clamp

PLAYER = 0
ENEMY = 1

_HEX_STEP = math.tau / 6


class Projectile:
    __slots__ = ('alive', 'owner', 'x', 'y', 'vx', 'vy', 'radius', 'damage',
                 'life', 'max_life', 'pierce', 'hit', 'color', 'glow_color',
                 'length', 'width', 'knockback', 'homing', 'explode',
                 'chain', 'bounces', 'crit', 'spin', 'wobble', 'phase')

    def __init__(self):
        self.alive = False
        self.hit = set()

    def reset(self, owner, x, y, vx, vy, damage, **kw):
        self.alive = True
        self.owner = owner
        self.x, self.y = x, y
        self.vx, self.vy = vx, vy
        self.damage = damage
        self.radius = kw.get('radius', 5.0)
        self.life = self.max_life = kw.get('life', 1.4)
        self.pierce = kw.get('pierce', 0)
        self.hit = set()
        self.color = kw.get('color', palette.BOLT_HOT)
        self.glow_color = kw.get('glow_color', (150, 220, 255))
        self.length = kw.get('length', 20.0)
        self.width = kw.get('width', 5.0)
        self.knockback = kw.get('knockback', 130.0)
        self.homing = kw.get('homing', 0.0)
        self.explode = kw.get('explode', 0.0)
        self.chain = kw.get('chain', 0)
        self.bounces = kw.get('bounces', 0)
        self.crit = kw.get('crit', False)
        self.spin = kw.get('spin', 0.0)
        self.wobble = kw.get('wobble', 0.0)
        self.phase = 0.0
        return self


class ProjectilePool:
    def __init__(self, capacity=220):
        self.pool = [Projectile() for _ in range(capacity)]
        self.capacity = capacity
        self._cursor = 0

    def clear(self):
        for p in self.pool:
            p.alive = False

    def spawn(self, owner, x, y, vx, vy, damage, **kw):
        pool = self.pool
        n = self.capacity
        for i in range(n):
            idx = (self._cursor + i) % n
            if not pool[idx].alive:
                self._cursor = (idx + 1) % n
                return pool[idx].reset(owner, x, y, vx, vy, damage, **kw)
        # Full: recycle the one closest to expiring.
        idx = min(range(n), key=lambda i: pool[i].life)
        return pool[idx].reset(owner, x, y, vx, vy, damage, **kw)

    def live(self):
        return [p for p in self.pool if p.alive]

    def update(self, dt, level, targets_for_homing=None):
        """Advance projectiles. Returns a list of (projectile, x, y) wall hits."""
        wall_hits = []
        for p in self.pool:
            if not p.alive:
                continue
            p.life -= dt
            if p.life <= 0.0:
                p.alive = False
                continue

            if p.homing > 0.0 and targets_for_homing:
                best = None
                best_d2 = 460.0 ** 2
                for t in targets_for_homing:
                    if not t.alive:
                        continue
                    d2 = (t.x - p.x) ** 2 + (t.y - p.y) ** 2
                    if d2 < best_d2:
                        best_d2 = d2
                        best = t
                if best is not None:
                    speed = math.hypot(p.vx, p.vy)
                    want = math.atan2(best.y - p.y, best.x - p.x)
                    have = math.atan2(p.vy, p.vx)
                    diff = (want - have + math.pi) % math.tau - math.pi
                    turn = clamp(diff, -p.homing * dt, p.homing * dt)
                    have += turn
                    p.vx = math.cos(have) * speed
                    p.vy = math.sin(have) * speed

            if p.wobble:
                p.phase += dt * 14.0
                speed = math.hypot(p.vx, p.vy)
                a = math.atan2(p.vy, p.vx) + math.sin(p.phase) * p.wobble * dt
                p.vx = math.cos(a) * speed
                p.vy = math.sin(a) * speed

            nx = p.x + p.vx * dt
            ny = p.y + p.vy * dt

            if level.blocked(nx, ny, p.radius * 0.5):
                if p.bounces > 0:
                    p.bounces -= 1
                    # Reflect off whichever axis actually blocked us.
                    if level.blocked(p.x, ny, p.radius * 0.5):
                        p.vy = -p.vy
                    if level.blocked(nx, p.y, p.radius * 0.5):
                        p.vx = -p.vx
                    if p.vx == 0.0 and p.vy == 0.0:
                        p.alive = False
                    nx, ny = p.x, p.y
                else:
                    wall_hits.append((p, nx, ny))
                    p.alive = False
                    continue

            p.x, p.y = nx, ny
        return wall_hits

    def draw_lights(self, ox, oy, view_w, view_h):
        """Every live bolt as a light, for the deferred pass.

        A projectile is a moving light source in a game about carrying the
        only one, so it lights the walls it flies past rather than merely
        being drawn bright.
        """
        for p in self.pool:
            if not p.alive:
                continue
            sx = p.x - ox
            sy = p.y - oy
            r = max(34.0, p.width * 7.0)
            if sx < -r or sy < -r or sx > view_w + r or sy > view_h + r:
                continue
            fade = clamp(p.life / max(p.max_life, 1e-6) * 3.0, 0.25, 1.0)
            art.draw_glow(p.glow_color, sx, sy, r, 54 * fade, power=2.4)

    def draw(self, ox, oy, view_w, view_h):
        for p in self.pool:
            if not p.alive:
                continue
            sx = p.x - ox
            sy = p.y - oy
            if sx < -80 or sy < -80 or sx > view_w + 80 or sy > view_h + 80:
                continue

            speed = math.hypot(p.vx, p.vy)
            if speed > 1.0:
                ca, sa = p.vx / speed, p.vy / speed
            else:
                ca, sa = 1.0, 0.0

            fade = clamp(p.life / max(p.max_life, 1e-6) * 3.0, 0.25, 1.0)

            halo = 58 if p.owner == PLAYER else 74
            size = _glow_size(p.width * (5.2 if p.owner == PLAYER else 6.4))
            sprite = art.glow(p.glow_color, size, power=2.3)
            half = size * 0.5
            drawImage(sprite, int(sx - half), int(sy - half),
                      opacity=int(halo * fade))

            a = p.length * 0.5
            b = p.width * 0.5
            if p.length >= p.width * 1.6:
                # Long and thin: a tapered hexagon reads as a lance, where a
                # plain quad reads as a floating brick.
                hx, hy = ca * a, sa * a
                mx, my = ca * a * 0.45, sa * a * 0.45
                wx, wy = -sa * b, ca * b
                drawPolygon(sx + hx, sy + hy,
                            sx + mx + wx, sy + my + wy,
                            sx - mx + wx, sy - my + wy,
                            sx - hx, sy - hy,
                            sx - mx - wx, sy - my - wy,
                            sx + mx - wx, sy + my - wy,
                            fill=p.color, opacity=int(96 * fade))
            else:
                # Stubby: a quad would read as a square block, so trace a
                # hexagon around the same ellipse instead - it reads as an orb.
                pts = []
                for i in range(6):
                    ang = i * _HEX_STEP
                    lx = math.cos(ang) * a
                    ly = math.sin(ang) * b
                    pts.append(sx + lx * ca - ly * sa)
                    pts.append(sy + lx * sa + ly * ca)
                drawPolygon(*pts, fill=p.color, opacity=int(96 * fade))


def _glow_size(px, step=16):
    return max(step, int(round(px / step)) * step)


# --------------------------------------------------------------------------
# Weapons
# --------------------------------------------------------------------------
class Weapon:
    def __init__(self, key, name, blurb, cooldown, damage, speed, pellets=1,
                 spread=0.0, life=1.2, pierce=0, radius=5.0, length=22.0,
                 width=5.0, color=None, glow=None, knockback=140.0,
                 charge_time=0.0, charge_scale=3.0, sound='shoot',
                 recoil=0.0, shake=0.9, light=110.0):
        self.key = key
        self.name = name
        self.blurb = blurb
        self.cooldown = cooldown
        self.damage = damage
        self.speed = speed
        self.pellets = pellets
        self.spread = spread
        self.life = life
        self.pierce = pierce
        self.radius = radius
        self.length = length
        self.width = width
        self.color = color or palette.BOLT_HOT
        self.glow = glow or (150, 220, 255)
        self.knockback = knockback
        self.charge_time = charge_time
        self.charge_scale = charge_scale
        self.sound = sound
        self.recoil = recoil
        self.shake = shake
        self.light = light


WEAPONS = [
    Weapon('lance', 'SPARKLANCE',
           'Rapid piercing bolts. Reliable, and it never leaves you dark.',
           cooldown=0.115, damage=9.5, speed=980, spread=0.028, life=1.05,
           pierce=1, radius=5.0, length=26.0, width=4.6,
           color=palette.BOLT_HOT, glow=(150, 220, 255),
           knockback=120.0, recoil=48.0, shake=0.7, light=105.0),

    Weapon('scatter', 'SCATTERLIGHT',
           'Seven shards in a cone. Devastating up close, wasted at range.',
           cooldown=0.56, damage=6.2, speed=760, pellets=7, spread=0.30,
           life=0.34, pierce=0, radius=4.4, length=18.0, width=4.2,
           color=palette.SCATTER, glow=(255, 190, 120),
           knockback=280.0, sound='shoot_scatter', recoil=190.0, shake=2.4,
           light=180.0),

    Weapon('coil', 'COILBEAM',
           'Hold to charge. Releases a lance that passes through everything.',
           cooldown=0.28, damage=15.0, speed=1320, spread=0.0, life=0.9,
           pierce=99, radius=8.0, length=64.0, width=11.0,
           color=palette.BEAM, glow=(180, 150, 255),
           knockback=340.0, charge_time=0.72, charge_scale=3.4,
           sound='shoot_beam', recoil=120.0, shake=2.6, light=260.0),
]

WEAPONS_BY_KEY = {w.key: w for w in WEAPONS}
