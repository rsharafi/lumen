"""Doors: a way through, and a thing that stops you.

The vault used to have holes in its walls. A doorway was a two-tile gap that
was always open, always passable and always transparent to light, and being
sealed into a room was communicated by drawing a few translucent blue bars
across the gap. It did not read as a door in either state - shut, it looked
like a gap with a stripe over it; open, it looked like the same gap without
one - and neither state did anything to the light, so a "sealed" room lit the
corridor beyond it exactly as brightly as an open one.

So a door is an object now, and it is a real occluder.

## What a door is made of

Two leaves that meet in the middle of the opening and retract into the jambs,
plus the frame around them. While the leaves are out they are **solid**: they
collide, and they cast shadow. That is the whole point. A shut door in a game
about carrying the only light has to be a thing the light stops at, or the
room behind it is not sealed, it is merely notional.

The collision and the occlusion come from the same rectangles, so the two can
never disagree - a door you can see past but not walk through, or the reverse,
is the kind of bug that takes a day to find.

## The seal

The lock is drawn *on* the leaves rather than floating in the gap: a seam of
cold light down the join and a set of bars struck across the face, brightest
where the two leaves meet. That is the reading the old wards were reaching
for and could not get, because light hanging in an empty doorway has nothing
to be light *on*.

When the room is cleared the seam flares, the bars break, and the leaves
withdraw into the jambs over about half a second - and because they are the
occluders, the room beyond genuinely opens up to the lantern as they go.
"""

import math

import numpy as np

from . import art, palette
from .config import TILE
from .draw import drawPolygon
from .mathx import clamp, ease_out_cubic
from .level import Rect

#: The states a door moves through. `SHUT` and `OPEN` are rests; the other two
#: are the animation, and only `OPEN` lets anything through.
SHUT = 'shut'
OPENING = 'opening'
OPEN = 'open'
CLOSING = 'closing'

#: How long the leaves take to withdraw, and to come back.
OPEN_TIME = 0.55
SHUT_TIME = 0.30

#: How deep the frame sits into the room, in design units. The leaves are
#: thinner than the wall they live in so the jamb reads as a recess.
LEAF_DEPTH = 30.0

SEAL = palette.WARD
STONE = palette.WALL_DOOR
STONE_LIT = palette.WALL_DOOR_LIT


class Door:
    """One doorway, and whatever is filling it.

    `span` is the tile rectangle the level carved, `axis` is which way the
    opening runs ('h' for a doorway in a north or south wall, whose leaves
    part left and right; 'v' for east and west).
    """

    #: `shape` and `edges` are the world's cache of what this door's own
    #: light can reach - see `World._static_light`. They live here because
    #: that cache is keyed by the object that owns the light, and they are
    #: dropped on every frame a leaf moves: a door is its own occluder, so a
    #: polygon cast while it was shut describes a room it no longer seals.
    __slots__ = ('side', 'axis', 'x0', 'y0', 'x1', 'y1', 'cx', 'cy',
                 'state', 't', 'open_t', 'seam', 'phase', 'shape', 'edges',
                 'edges_rich')

    def __init__(self, side, x0, y0, x1, y1):
        self.side = side
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.cx = (x0 + x1) * 0.5
        self.cy = (y0 + y1) * 0.5
        # A north or south doorway is wider than it is tall, and its leaves
        # part sideways.
        self.axis = 'h' if (x1 - x0) >= (y1 - y0) else 'v'
        self.state = SHUT
        self.t = 0.0
        #: 0 fully shut, 1 fully withdrawn. Eased, so the leaves ease off the
        #: seal rather than snapping away from it.
        self.open_t = 0.0
        #: Flares to 1 when the seal breaks, and falls away.
        self.seam = 0.0
        self.phase = (abs(hash((side, int(x0), int(y0)))) % 1000) / 1000.0 * 6.28
        self.shape = None
        self.edges = None
        self.edges_rich = False

    # ------------------------------------------------------------- state --
    @property
    def passable(self):
        """Can anything get through? Only once the leaves are clear."""
        return self.open_t > 0.86

    @property
    def blocking(self):
        """Is there enough leaf left to stop light and bodies?"""
        return self.open_t < 0.98

    def open(self, flare=True):
        if self.state in (OPEN, OPENING):
            return False
        self.state = OPENING
        self.t = 0.0
        if flare:
            self.seam = 1.0
        return True

    def shut(self):
        if self.state in (SHUT, CLOSING):
            return False
        self.state = CLOSING
        self.t = 0.0
        return True

    def snap(self, is_open):
        """Put the door straight into a rest state, with no animation."""
        self.state = OPEN if is_open else SHUT
        self.open_t = 1.0 if is_open else 0.0
        self.t = 0.0
        self.seam = 0.0
        self.shape = None
        self.edges = None

    def update(self, dt):
        """Advance the animation. True if the blocking state changed."""
        was = self.blocking
        moving = self.state in (OPENING, CLOSING)
        if moving:
            # The cast polygon describes the room as it was a frame ago, and
            # this door was part of what shaped it.
            self.shape = None
            self.edges = None
        self.seam = max(0.0, self.seam - dt * 1.6)
        if self.state == OPENING:
            self.t += dt
            self.open_t = ease_out_cubic(clamp(self.t / OPEN_TIME, 0.0, 1.0))
            if self.t >= OPEN_TIME:
                self.state = OPEN
                self.open_t = 1.0
        elif self.state == CLOSING:
            self.t += dt
            self.open_t = 1.0 - ease_out_cubic(
                clamp(self.t / SHUT_TIME, 0.0, 1.0))
            if self.t >= SHUT_TIME:
                self.state = SHUT
                self.open_t = 0.0
        return was != self.blocking

    # ---------------------------------------------------------- geometry --
    def leaves(self):
        """The two leaves, as (x0, y0, x1, y1) in world units.

        They meet in the middle when shut and withdraw into the jambs as
        `open_t` rises. An empty list once they are clear of the opening,
        which is what makes `blocking` and the collision agree by
        construction.
        """
        k = self.open_t
        if k >= 0.98:
            return ()
        if self.axis == 'h':
            span = (self.x1 - self.x0) * 0.5
            travel = span * k
            mid = self.cx
            return (
                (self.x0 + travel, self.y0, mid, self.y1),
                (mid, self.y0, self.x1 - travel, self.y1),
            )
        span = (self.y1 - self.y0) * 0.5
        travel = span * k
        mid = self.cy
        return (
            (self.x0, self.y0 + travel, self.x1, mid),
            (self.x0, mid, self.x1, self.y1 - travel),
        )

    def rects(self):
        """Collision rectangles for whatever is filling the opening."""
        return [Rect(a, b, c - a, d - b) for a, b, c, d in self.leaves()]

    def segments(self):
        """Occluder edges, wound like the level's own.

        Only the two faces that look into a room are given: the edges along
        the leaf's own travel are inside the jamb and can never be seen, and
        handing the sweep edges nobody can look at is work for nothing.
        """
        out = []
        for a, b, c, d in self.leaves():
            if self.axis == 'h':
                out.append((a, b, c, b))        # the face toward -y
                out.append((c, d, a, d))        # and toward +y
            else:
                out.append((c, b, c, d))
                out.append((a, d, a, b))
        return out


def collect_segments(level, doors):
    """Fold every shut door's occluders into the level's own arrays.

    The lighting reads `level.seg_*` and `level.corners` directly and knows
    nothing about doors, so rather than teach it, the arrays it reads are
    rebuilt whenever a door's blocking state changes. That is a concatenate
    of at most a few dozen edges onto a few dozen, and it happens twice per
    door per floor.
    """
    base = level.base_segments
    extra = []
    for door in doors:
        extra.extend(door.segments())
    if not extra:
        level.seg_ax, level.seg_ay, level.seg_bx, level.seg_by = base[:4]
        level.corners = base[4]
        level.segments = base[5]
        return

    ax = np.concatenate([base[0], np.array([s[0] for s in extra], dtype=np.float64)])
    ay = np.concatenate([base[1], np.array([s[1] for s in extra], dtype=np.float64)])
    bx = np.concatenate([base[2], np.array([s[2] for s in extra], dtype=np.float64)])
    by = np.concatenate([base[3], np.array([s[3] for s in extra], dtype=np.float64)])
    pts = []
    for s in extra:
        pts.append((s[0], s[1]))
        pts.append((s[2], s[3]))
    corners = np.concatenate([base[4], np.array(pts, dtype=np.float64)])
    level.seg_ax, level.seg_ay, level.seg_bx, level.seg_by = ax, ay, bx, by
    level.corners = corners
    level.segments = list(base[5]) + list(extra)


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------
def draw_frame(door, ox, oy):
    """The jambs and threshold, which are there in every state.

    Drawn under everything else in the opening so a withdrawn leaf slides
    behind its own jamb rather than over it.
    """
    x0, y0 = door.x0 - ox, door.y0 - oy
    x1, y1 = door.x1 - ox, door.y1 - oy
    j = 7.0
    if door.axis == 'h':
        # Jambs stand either side of the opening, running with the wall.
        for jx in (x0, x1):
            drawPolygon(jx - j, y0 - 2, jx + j, y0 - 2,
                        jx + j, y1 + 2, jx - j, y1 + 2,
                        fill=STONE, opacity=100)
        # The threshold, so the floor of the opening reads as worked stone
        # rather than as more of the room.
        drawPolygon(x0, y0 + 3, x1, y0 + 3, x1, y1 - 3, x0, y1 - 3,
                    fill=palette.WALL_DEEP, opacity=58)
    else:
        for jy in (y0, y1):
            drawPolygon(x0 - 2, jy - j, x1 + 2, jy - j,
                        x1 + 2, jy + j, x0 - 2, jy + j,
                        fill=STONE, opacity=100)
        drawPolygon(x0 + 3, y0, x1 - 3, y0, x1 - 3, y1, x0 + 3, y1,
                    fill=palette.WALL_DEEP, opacity=58)


def draw_leaves(door, ox, oy, run_time, lit):
    """The moving parts, and the seal across them.

    `lit` is whether the player's light is reaching this doorway. An unlit
    door still shows its seal - a sealed door is information the player needs
    from across a dark room, and in this game the only way to deliver that is
    to make the seal itself luminous - but the stone of the leaves is only
    drawn where there is light to see it by.
    """
    leaves = door.leaves()
    if not leaves:
        return
    shut = 1.0 - door.open_t

    for index, (a, b, c, d) in enumerate(leaves):
        sx0, sy0 = a - ox, b - oy
        sx1, sy1 = c - ox, d - oy
        if sx1 - sx0 < 1.0 or sy1 - sy0 < 1.0:
            continue

        # The slab. Brighter along its leading edge, which is what makes it
        # read as a thing with a thickness rather than a filled rectangle.
        body = STONE_LIT if lit else STONE
        drawPolygon(sx0, sy0, sx1, sy0, sx1, sy1, sx0, sy1,
                    fill=body, opacity=100)
        # A brighter lip along the leading edge, so the leaf reads as a slab
        # with a thickness rather than a filled rectangle - and so the eye
        # can see it travel, which a flat fill does not show.
        lip = 3.0
        if door.axis == 'h':
            ex = sx1 if index == 0 else sx0
            drawPolygon(ex - lip, sy0, ex + lip, sy0,
                        ex + lip, sy1, ex - lip, sy1,
                        fill=palette.WALL_EDGE, opacity=64 if lit else 40)
        else:
            ey = sy1 if index == 0 else sy0
            drawPolygon(sx0, ey - lip, sx1, ey - lip,
                        sx1, ey + lip, sx0, ey + lip,
                        fill=palette.WALL_EDGE, opacity=64 if lit else 40)

        # Banding across the face, along the door's long axis.
        bands = 4
        for i in range(1, bands):
            f = i / bands
            if door.axis == 'h':
                by = sy0 + (sy1 - sy0) * f
                drawPolygon(sx0, by - 1.0, sx1, by - 1.0,
                            sx1, by + 1.0, sx0, by + 1.0,
                            fill=palette.WALL_DEEP,
                            opacity=int(46 if lit else 30))
            else:
                bx = sx0 + (sx1 - sx0) * f
                drawPolygon(bx - 1.0, sy0, bx + 1.0, sy0,
                            bx + 1.0, sy1, bx - 1.0, sy1,
                            fill=palette.WALL_DEEP,
                            opacity=int(46 if lit else 30))

    if shut <= 0.02:
        return

    # ---- the seal ------------------------------------------------------
    # Down the join, where the two leaves meet. It is the brightest thing on
    # the door and the last thing to go.
    breathe = 0.72 + 0.28 * math.sin(run_time * 2.6 + door.phase)
    flare = door.seam
    glow = clamp(shut, 0.0, 1.0) * breathe + flare
    cx, cy = door.cx - ox, door.cy - oy
    if door.axis == 'h':
        half = (door.y1 - door.y0) * 0.5
        gap = (door.x1 - door.x0) * 0.5 * door.open_t
        for side in (-1, 1):
            jx = cx + side * gap
            drawPolygon(jx - 2.4, cy - half, jx + 2.4, cy - half,
                        jx + 2.4, cy + half, jx - 2.4, cy + half,
                        fill=SEAL, opacity=int(clamp(62 * glow, 0, 100)))
        art.draw_glow(art.rgb_tuple(SEAL), cx, cy,
                      (26 + 34 * flare) * max(shut, 0.35),
                      int(clamp(26 * glow, 0, 88)), power=2.4)
        # Bars struck across the face, brightest at the join. They break
        # outward as the door opens rather than simply fading.
        bars = 3
        for i in range(bars):
            f = (i + 1) / (bars + 1)
            by = cy - half + (half * 2) * f
            reach = (door.x1 - door.x0) * 0.42 * (1.0 - door.open_t)
            op = int(clamp(34 * shut * breathe, 0, 100))
            if op <= 0 or reach <= 1.0:
                continue
            drawPolygon(cx - reach, by - 1.6, cx + reach, by - 1.6,
                        cx + reach, by + 1.6, cx - reach, by + 1.6,
                        fill=SEAL, opacity=op)
    else:
        half = (door.x1 - door.x0) * 0.5
        gap = (door.y1 - door.y0) * 0.5 * door.open_t
        for side in (-1, 1):
            jy = cy + side * gap
            drawPolygon(cx - half, jy - 2.4, cx + half, jy - 2.4,
                        cx + half, jy + 2.4, cx - half, jy + 2.4,
                        fill=SEAL, opacity=int(clamp(62 * glow, 0, 100)))
        art.draw_glow(art.rgb_tuple(SEAL), cx, cy,
                      (26 + 34 * flare) * max(shut, 0.35),
                      int(clamp(26 * glow, 0, 88)), power=2.4)
        bars = 3
        for i in range(bars):
            f = (i + 1) / (bars + 1)
            bx = cx - half + (half * 2) * f
            reach = (door.y1 - door.y0) * 0.42 * (1.0 - door.open_t)
            op = int(clamp(34 * shut * breathe, 0, 100))
            if op <= 0 or reach <= 1.0:
                continue
            drawPolygon(bx - 1.6, cy - reach, bx + 1.6, cy - reach,
                        bx + 1.6, cy + reach, bx - 1.6, cy + reach,
                        fill=SEAL, opacity=op)
