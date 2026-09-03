"""What the floor itself does, in the acts that have an opinion about it.

A grade makes an act look like somewhere else. A hazard makes it *play* like
somewhere else, and without one the second and third acts are the first act
with a filter on. So each of the lower two acts owns one thing on the ground,
and both of them are about light, because that is the only subject this game
has.

## The cisterns are flooded

Standing water. It **slows anything walking through it** - you and the things
chasing you alike - and it **carries your light further than stone does**,
because it reflects. That is the trade, and it is a real one: the fastest way
across a flooded room is also the way that shows you least, and the pool you
can see the far wall from is the pool you cannot run out of.

Fliers are untouched by it, which is most of what a flier is for.

## The vault burns

Vents in the floor that gout fire on a cycle. They telegraph - a glow, then a
rumble, then the gout - and they are **the only light in the last act that is
not yours**. That is the whole idea of them: for nineteen floors the vault has
withheld light, and here it hands you some, on its own schedule, in a shape
that will hurt you if you are standing in it.

They burn enemies too, and a vent going off in a crowd is the best thing that
can happen to you down there.
"""

import math

from . import acts, art, audio, palette
from .config import TILE
from .draw import drawPolygon
from .mathx import clamp

WATER = acts.WATER
VENTS = acts.VENTS

#: How much of your speed standing water takes. Enough to feel, not enough to
#: make a flooded room a punishment for entering it.
WATER_SLOW = 0.34

#: And how much further the lantern reaches over it. Water reflects; this is
#: the reward half of the trade.
WATER_REACH = 1.16

#: A vent's cycle: quiet, then the glow, then the gout, then quiet again.
VENT_PERIOD = 4.6
VENT_TELL = 1.05
VENT_BURN = 0.85
VENT_RADIUS = 62.0
VENT_DPS = 34.0


class Pool:
    """Standing water. A polygon on the floor, and a region that slows."""

    __slots__ = ('x', 'y', 'r', 'shape', 'phase')

    def __init__(self, x, y, r, rng):
        self.x = x
        self.y = y
        self.r = r
        self.phase = rng.uniform(0.0, 6.28)
        # An irregular outline, baked once. Water that is a circle reads as a
        # decal; water with a coastline reads as water.
        pts = []
        n = 11
        for i in range(n):
            a = i * math.tau / n
            wob = 0.74 + rng.uniform(0.0, 0.5)
            pts.append((math.cos(a) * r * wob, math.sin(a) * r * wob))
        self.shape = tuple(pts)

    def contains(self, x, y):
        dx = x - self.x
        dy = y - self.y
        # Against the mean radius rather than the outline: a body half in a
        # puddle should be slowed, and testing the polygon exactly would have
        # the effect flicker on and off as the player walks the coast.
        return dx * dx + dy * dy <= self.r * self.r


class Vent:
    """A grate in the floor that breathes fire on a cycle."""

    __slots__ = ('x', 'y', 't', 'period', 'fired')

    def __init__(self, x, y, rng):
        self.x = x
        self.y = y
        self.period = VENT_PERIOD * rng.uniform(0.86, 1.2)
        # Staggered, so a room of them is a rhythm to move through rather
        # than one big pulse to stand still for.
        self.t = rng.uniform(0.0, self.period)
        self.fired = False

    @property
    def telling(self):
        """In the warning window: glowing, not yet burning."""
        left = self.period - self.t
        return VENT_BURN < left <= VENT_BURN + VENT_TELL

    @property
    def burning(self):
        return (self.period - self.t) <= VENT_BURN

    @property
    def tell_k(self):
        """0 at the start of the warning, 1 the instant before it fires."""
        left = self.period - self.t
        if not (VENT_BURN < left <= VENT_BURN + VENT_TELL):
            return 0.0
        return 1.0 - (left - VENT_BURN) / VENT_TELL

    @property
    def burn_k(self):
        """1 at ignition, falling to 0 as the gout dies."""
        if not self.burning:
            return 0.0
        return clamp((self.period - self.t) / VENT_BURN, 0.0, 1.0)


class Field:
    """Every hazard in one room, and what it does to whoever is standing in it."""

    def __init__(self, kind=None):
        self.kind = kind
        self.pools = []
        self.vents = []

    # ------------------------------------------------------------- build --
    @classmethod
    def build(cls, level, depth, rng):
        """Scatter this act's hazard over a chamber.

        Placed on open floor away from the doors, because a hazard in a
        doorway is a hazard you cannot choose not to walk into - and the
        whole point of both of these is that they are a choice about where to
        put your feet.
        """
        act = acts.of(depth)
        field = cls(act.hazard)
        if act.hazard is None or act.hazard_density <= 0.0:
            return field

        spots = []
        for col, row in level.open_tiles(2):
            x, y = level.tile_center(col, row)
            if not level.is_open_at(x, y, TILE * 0.62):
                continue
            if any(_near_door(level, side, x, y) for side in level.doors):
                continue
            spots.append((x, y))
        if not spots:
            return field

        want = max(1, int(len(spots) * act.hazard_density * 0.16))
        placed = []
        for x, y in rng.shuffled(spots):
            if len(placed) >= want:
                break
            if all((x - px) ** 2 + (y - py) ** 2 > (TILE * 2.4) ** 2
                   for px, py in placed):
                placed.append((x, y))

        if act.hazard == WATER:
            for x, y in placed:
                field.pools.append(
                    Pool(x, y, TILE * rng.uniform(0.95, 1.7), rng))
        elif act.hazard == VENTS:
            for x, y in placed:
                field.vents.append(Vent(x, y, rng))
        return field

    # ------------------------------------------------------------ queries --
    def slow_at(self, x, y):
        """How much of a body's speed the ground here takes, 0 to 1."""
        if not self.pools:
            return 0.0
        for pool in self.pools:
            if pool.contains(x, y):
                return WATER_SLOW
        return 0.0

    def reach_at(self, x, y):
        """What the ground here does to the lantern's reach, as a multiplier."""
        if not self.pools:
            return 1.0
        for pool in self.pools:
            if pool.contains(x, y):
                return WATER_REACH
        return 1.0

    # ------------------------------------------------------------- update --
    def update(self, dt, world):
        if self.vents:
            self._update_vents(dt, world)

    def _update_vents(self, dt, world):
        player = world.player
        for vent in self.vents:
            vent.t += dt
            if vent.t >= vent.period:
                vent.t -= vent.period
                vent.fired = False
            if not vent.burning:
                continue
            if not vent.fired:
                vent.fired = True
                audio.play_at('vent', vent.x, vent.y, 0.55)
                world.effects.add_shake(1.6)
                for _ in range(9):
                    a = world.fxrng.angle()
                    sp = world.fxrng.uniform(40, 180)
                    world.particles.emit(
                        1, vent.x, vent.y, math.cos(a) * sp,
                        math.sin(a) * sp - 60.0, 0.6, 3.4,
                        palette.LIGHT_CORE, end_size=0.5, opacity=90,
                        drag=0.7)
            # The gout is a real light while it lasts - the only one in the
            # last act that is not the player's.
            k = vent.burn_k
            world.effects.add_light(vent.x, vent.y, VENT_RADIUS * 2.6,
                                    dt * 2.4 * k, palette.LIGHT_WARM)
            rr = VENT_RADIUS * (0.6 + 0.4 * k)
            if player.alive:
                dx = player.x - vent.x
                dy = player.y - vent.y
                if dx * dx + dy * dy <= rr * rr:
                    player.scorch(VENT_DPS * dt, world.effects,
                                  world.particles, world.fxrng)
            for enemy in world.enemies:
                if not enemy.alive or enemy.flies:
                    continue
                dx = enemy.x - vent.x
                dy = enemy.y - vent.y
                if dx * dx + dy * dy <= rr * rr:
                    enemy.damage_by(VENT_DPS * dt, world)

    # --------------------------------------------------------------- draw --
    def draw(self, world, ox, oy):
        """Under the bodies, over the floor."""
        if self.pools:
            self._draw_pools(world, ox, oy)
        if self.vents:
            self._draw_vents(world, ox, oy)

    def _draw_pools(self, world, ox, oy):
        t = world.run_time
        for pool in self.pools:
            sx, sy = pool.x - ox, pool.y - oy
            if (sx < -140 or sy < -140 or sx > world.view_w + 140
                    or sy > world.view_h + 140):
                continue
            pts = []
            for i, (px, py) in enumerate(pool.shape):
                # A slow breathing wobble, so it is liquid and not a decal.
                k = 1.0 + 0.035 * math.sin(t * 0.9 + pool.phase + i * 0.8)
                pts.append(sx + px * k)
                pts.append(sy + py * k)
            drawPolygon(*pts, fill=palette.WATER, opacity=52)
            drawPolygon(*pts, fill=None, border=palette.WATER_RIM,
                        borderWidth=1.6, opacity=40)

    def _draw_vents(self, world, ox, oy):
        t = world.run_time
        for vent in self.vents:
            sx, sy = vent.x - ox, vent.y - oy
            if (sx < -120 or sy < -120 or sx > world.view_w + 120
                    or sy > world.view_h + 120):
                continue
            # The grate is always there, so the room can be read before
            # anything happens in it.
            r = 13.0
            drawPolygon(sx - r, sy - r * 0.6, sx + r, sy - r * 0.6,
                        sx + r, sy + r * 0.6, sx - r, sy + r * 0.6,
                        fill=palette.VENT_IRON, opacity=88)
            for i in (-1, 0, 1):
                bx = sx + i * r * 0.5
                drawPolygon(bx - 1.6, sy - r * 0.6, bx + 1.6, sy - r * 0.6,
                            bx + 1.6, sy + r * 0.6, bx - 1.6, sy + r * 0.6,
                            fill=palette.VOID, opacity=70)

            tell = vent.tell_k
            if tell > 0.0:
                # The warning draws the reach it will actually have, so what
                # the ring covers when it closes is exactly what will burn.
                rad = VENT_RADIUS * (1.5 - 0.5 * tell)
                pts = []
                for i in range(12):
                    a = i * math.tau / 12
                    pts.append(sx + math.cos(a) * rad)
                    pts.append(sy + math.sin(a) * rad)
                drawPolygon(*pts, fill=None, border=palette.VENT_TELL,
                            borderWidth=2.0, opacity=int(26 + 54 * tell))
                art.draw_glow(art.rgb_tuple(palette.VENT_TELL), sx, sy,
                              20 + 26 * tell, int(20 + 40 * tell), power=2.6)

            burn = vent.burn_k
            if burn > 0.0:
                flick = 0.82 + 0.18 * math.sin(t * 21.0 + vent.x * 0.05)
                rad = VENT_RADIUS * (0.6 + 0.4 * burn)
                art.draw_glow((255, 190, 110), sx, sy, rad * 1.5 * flick,
                              int(74 * burn), power=2.0)
                h = rad * 1.15 * burn * flick
                drawPolygon(sx, sy - h, sx + rad * 0.42, sy,
                            sx, sy + rad * 0.30, sx - rad * 0.42, sy,
                            fill=palette.LIGHT_CORE, opacity=int(92 * burn))


def _near_door(level, side, x, y):
    cx, cy = level.door_center(side)
    return (x - cx) ** 2 + (y - cy) ** 2 < (TILE * 3.2) ** 2
