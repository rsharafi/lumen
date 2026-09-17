"""Chamber generation, collision geometry, and the baked floor image.

A chamber is authored on a tile grid, then the solid tiles are merged into as
few rectangles as possible. That merge matters twice over: rectangles are what
the physics resolves against, and their edges are the segment soup the
shadowcaster traces. Fewer, larger rectangles means both run faster.
"""

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import acts, art, noise
from . import palette
from .config import SHADOW_FLOOR_MIX, TILE
from .mathx import clamp

WALL = 1
FLOOR = 0

ARCHETYPES = ('pillars', 'cross', 'rings', 'shards', 'gauntlet', 'spiral')

# --------------------------------------------------------------------------
# Doors
# --------------------------------------------------------------------------
# The border is two tiles thick, and a door is a gap in the *inner* ring
# only. That is what makes a doorway safe as well as legible: the player
# standing in the gap is still enclosed by the outer ring, so there is no
# hole in the chamber for them to walk out of and no edge for the light to
# spill past into nothing. It also just looks like a door - a recess in a
# thick wall rather than a bite taken out of a thin one.
BORDER = 2
DOOR_WIDTH = 2                  # tiles across the opening
DOOR_CLEAR = 3                  # tiles of floor guaranteed inside it

#: Which way is "into the room" from each side's doorway.
INWARD = {'n': (0, 1), 's': (0, -1), 'w': (1, 0), 'e': (-1, 0)}

# Room footprints in tiles, border included. Most of a floor is now rooms
# rather than one arena, so they are far smaller than the single chamber
# was: a `small` room is a screenful with margin to spare and reads as a
# place, where the old 28x18 read as a field. `large` is about one screen
# exactly, which is as big as a fight wants to be when there are ten more
# rooms behind it.
ROOM_SIZES = {
    'small': (15, 11),
    'medium': (19, 13),
    'large': (23, 15),
    'boss': (28, 18),
}


class Rect:
    __slots__ = ('x', 'y', 'w', 'h', 'right', 'bottom', 'cx', 'cy')

    def __init__(self, x, y, w, h):
        self.x = float(x)
        self.y = float(y)
        self.w = float(w)
        self.h = float(h)
        self.right = self.x + self.w
        self.bottom = self.y + self.h
        self.cx = self.x + self.w * 0.5
        self.cy = self.y + self.h * 0.5

    def __repr__(self):
        return f'Rect({self.x:.0f},{self.y:.0f},{self.w:.0f},{self.h:.0f})'


class Brazier:
    """A static light the player can ignite by walking into it."""

    __slots__ = ('x', 'y', 'lit', 'flicker', 'ignite_t', 'edges',
                 'edges_rich', 'shape')

    def __init__(self, x, y):
        self.x = x
        self.y = y
        self.lit = False
        self.flicker = 0.0
        self.ignite_t = 0.0
        # Wall edges this brazier lights. Braziers and walls are both static,
        # so this is resolved once on ignition instead of every frame.
        self.edges = None
        # Which renderer that cache was resolved for; the two forms carry
        # different tuples and the setting can change while it is lit.
        self.edges_rich = False
        # The polygon its light can actually reach. A brazier and the walls
        # are both nailed down, so this is cast once and kept.
        self.shape = None


class Level:
    def __init__(self, cols, rows, depth, archetype):
        self.cols = cols
        self.rows = rows
        self.depth = depth
        self.archetype = archetype
        self.grid = [[FLOOR] * cols for _ in range(rows)]
        self.width = cols * TILE
        self.height = rows * TILE
        self.rects = []
        self.segments = []          # flat numpy arrays, built by _build_segments
        self.seg_ax = None
        self.seg_ay = None
        self.seg_bx = None
        self.seg_by = None
        self.corners = None         # numpy (N, 2) of wall corners
        self.floor_image = None
        self.floor_normal = None
        # Every art key this chamber owns, so the cache can be cleared
        # around it rather than through it.
        self.art_keys = []
        #: The art cache generation these layers were rasterised in. A resize
        #: that changes the render scale empties the cache and every layer
        #: with it, and this is how a chamber finds out; see `needs_bake`.
        self.art_generation = -1
        self.floor_key = None
        self.wall_image = None
        self.wall_normal = None
        self.wall_key = None
        self.minimap_image = None
        self.minimap_rect = None    # (offset_x, offset_y, width, height)
        self.braziers = []
        self.spawn_points = []
        self.player_start = (self.width * 0.5, self.height * 0.5)
        #: side -> (col0, col1, row0, row1), the tiles of the opening.
        self.doors = {}
        #: `doors.Door` objects, one per opening. Built after the geometry,
        #: because a door needs the world rectangle of its span.
        self.door_objects = {}
        #: The chamber's own edges, before any door's are folded in. Kept so
        #: the arrays the lighting reads can be rebuilt from a clean base
        #: every time a leaf moves - see `doors.collect_segments`.
        self.base_segments = None
        #: The floorplan seed this chamber was baked under; `rebake` needs it
        #: to reproduce the same stone after a render-scale change.
        self.bake_room = 0

    # ------------------------------------------------------------ queries --
    def is_wall_tile(self, col, row):
        if col < 0 or row < 0 or col >= self.cols or row >= self.rows:
            return True
        return self.grid[row][col] == WALL

    def is_open_at(self, x, y, margin=0.0):
        """Is a disc of radius `margin` at (x, y) clear of walls?"""
        if x - margin < TILE * 0.5 or y - margin < TILE * 0.5:
            return False
        if x + margin > self.width - TILE * 0.5 or y + margin > self.height - TILE * 0.5:
            return False
        c0 = int((x - margin) // TILE)
        c1 = int((x + margin) // TILE)
        r0 = int((y - margin) // TILE)
        r1 = int((y + margin) // TILE)
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                if self.is_wall_tile(c, r):
                    return False
        return True

    def collide_circle(self, x, y, radius):
        """Push a disc out of every rectangle it overlaps. Returns (x, y)."""
        solid = self.rects if not self.door_objects else \
            self.rects + self.door_rects()
        for _ in range(2):
            moved = False
            for rect in solid:
                qx = clamp(x, rect.x, rect.right)
                qy = clamp(y, rect.y, rect.bottom)
                dx = x - qx
                dy = y - qy
                d2 = dx * dx + dy * dy
                if d2 >= radius * radius:
                    continue
                if d2 > 1e-9:
                    d = math.sqrt(d2)
                    push = radius - d
                    x += dx / d * push
                    y += dy / d * push
                else:
                    # Centre is inside the rectangle: eject along the shallowest axis.
                    left = x - rect.x
                    right = rect.right - x
                    top = y - rect.y
                    bottom = rect.bottom - y
                    least = min(left, right, top, bottom)
                    if least == left:
                        x = rect.x - radius
                    elif least == right:
                        x = rect.right + radius
                    elif least == top:
                        y = rect.y - radius
                    else:
                        y = rect.bottom + radius
                moved = True
            if not moved:
                break
        return x, y

    def blocked(self, x, y, radius):
        for rect in self.rects if not self.door_objects else \
                self.rects + self.door_rects():
            qx = clamp(x, rect.x, rect.right)
            qy = clamp(y, rect.y, rect.bottom)
            dx = x - qx
            dy = y - qy
            if dx * dx + dy * dy < radius * radius:
                return True
        return False

    def ray_blocked(self, ax, ay, bx, by):
        """Coarse line-of-sight test used by enemy AI (not by the lighting)."""
        dx = bx - ax
        dy = by - ay
        steps = int(math.hypot(dx, dy) / (TILE * 0.5)) + 1
        if steps <= 1:
            return False
        inv = 1.0 / steps
        for i in range(1, steps):
            t = i * inv
            px = ax + dx * t
            py = ay + dy * t
            if self.is_wall_tile(int(px // TILE), int(py // TILE)):
                return True
        return False

    # ------------------------------------------------------------- build ---
    def _carve_border(self):
        for c in range(self.cols):
            self.grid[0][c] = WALL
            self.grid[self.rows - 1][c] = WALL
        for r in range(self.rows):
            self.grid[r][0] = WALL
            self.grid[r][self.cols - 1] = WALL

    def _carve_thick_border(self):
        """Two solid rings all the way round. Doors are cut afterwards."""
        for r in range(self.rows):
            for c in range(self.cols):
                if (r < BORDER or c < BORDER
                        or r >= self.rows - BORDER or c >= self.cols - BORDER):
                    self.grid[r][c] = WALL

    def _door_span(self, side):
        """The tiles of the opening on `side`, centred on that wall."""
        mc = self.cols // 2
        mr = self.rows // 2
        half = DOOR_WIDTH // 2
        if side == 'n':
            return (mc - half, mc - half + DOOR_WIDTH - 1, BORDER - 1, BORDER - 1)
        if side == 's':
            row = self.rows - BORDER
            return (mc - half, mc - half + DOOR_WIDTH - 1, row, row)
        if side == 'w':
            return (BORDER - 1, BORDER - 1, mr - half, mr - half + DOOR_WIDTH - 1)
        col = self.cols - BORDER
        return (col, col, mr - half, mr - half + DOOR_WIDTH - 1)

    def _carve_doors(self, sides):
        """Open the inner ring on each side, and clear the way in.

        The clearance is not decoration. A layout is free to put a pillar
        wherever it likes, and several of them like putting one exactly where
        a door wants to be - so the approach is carved *after* the layout
        runs and overrides it. A door a chamber has walled off is a floor the
        player cannot finish.
        """
        self.doors = {}
        for side in sides:
            c0, c1, r0, r1 = self._door_span(side)
            # Through the *whole* wall, not just its inner ring. The border is
            # BORDER tiles thick, and carving only the innermost one left a
            # doorway that was an alcove: you could stand in it and never pass
            # through it. That went unnoticed for as long as walking through a
            # door teleported you - the player was placed on the far side and
            # never had to make the trip.
            dc, dr = INWARD[side]
            for step in range(0, BORDER):
                for r in range(r0, r1 + 1):
                    for c in range(c0, c1 + 1):
                        rr, cc = r - dr * step, c - dc * step
                        if 0 <= rr < self.rows and 0 <= cc < self.cols:
                            self.grid[rr][cc] = FLOOR
            self.doors[side] = (c0, c1, r0, r1)

            dc, dr = INWARD[side]
            for step in range(1, DOOR_CLEAR + 1):
                for r in range(r0, r1 + 1):
                    for c in range(c0, c1 + 1):
                        rr, cc = r + dr * step, c + dc * step
                        if BORDER <= rr < self.rows - BORDER and \
                                BORDER <= cc < self.cols - BORDER:
                            self.grid[rr][cc] = FLOOR

    def _merge_rects(self):
        """Greedy maximal-rectangle cover of the solid tiles."""
        used = [[False] * self.cols for _ in range(self.rows)]
        rects = []
        for r in range(self.rows):
            for c in range(self.cols):
                if self.grid[r][c] != WALL or used[r][c]:
                    continue
                # Extend right.
                c1 = c
                while (c1 + 1 < self.cols and self.grid[r][c1 + 1] == WALL
                       and not used[r][c1 + 1]):
                    c1 += 1
                # Extend down while the whole span stays solid.
                r1 = r
                while r1 + 1 < self.rows:
                    ok = True
                    for cc in range(c, c1 + 1):
                        if self.grid[r1 + 1][cc] != WALL or used[r1 + 1][cc]:
                            ok = False
                            break
                    if not ok:
                        break
                    r1 += 1
                for rr in range(r, r1 + 1):
                    for cc in range(c, c1 + 1):
                        used[rr][cc] = True
                rects.append(Rect(c * TILE, r * TILE,
                                  (c1 - c + 1) * TILE, (r1 - r + 1) * TILE))
        self.rects = rects

    def _build_segments(self):
        """The outline of the stone, as seen from the floor.

        These are what the light sweeps against and what it lands on, and they
        used to be every edge of every rectangle the generator produced -
        including the ones where two rectangles sit flush against each other.
        An interior edge is not a surface: it has floor on neither side, so
        lighting it puts a lit crevice down the middle of what is plainly one
        wall, and sweeping against it lets light find its way into the join.

        Built from the tile grid instead, and only where the neighbour on that
        side is open. Runs of collinear exposed edges are merged, so a long
        wall is a handful of segments rather than one per tile - the sweep
        costs time per segment, and the piece machinery wants long edges to
        cut up rather than short ones to stitch together.
        """
        ax, ay, bx, by = [], [], [], []
        corners = []

        def stone(r, c):
            return (0 <= r < self.rows and 0 <= c < self.cols
                    and self.grid[r][c] != FLOOR)

        # Winding matches the old clockwise rectangles, so the outward normal
        # is still (ey, -ex) and everything downstream is unchanged.
        for r in range(self.rows):
            run = None                      # top edges, left to right
            for c in range(self.cols + 1):
                open_top = (c < self.cols and stone(r, c)
                            and not stone(r - 1, c))
                if open_top and run is None:
                    run = c
                elif not open_top and run is not None:
                    ax.append(run * TILE); ay.append(r * TILE)
                    bx.append(c * TILE); by.append(r * TILE)
                    corners.append((run * TILE, r * TILE))
                    corners.append((c * TILE, r * TILE))
                    run = None
            run = None                      # bottom edges, right to left
            for c in range(self.cols + 1):
                open_bot = (c < self.cols and stone(r, c)
                            and not stone(r + 1, c))
                if open_bot and run is None:
                    run = c
                elif not open_bot and run is not None:
                    y = (r + 1) * TILE
                    ax.append(c * TILE); ay.append(y)
                    bx.append(run * TILE); by.append(y)
                    corners.append((c * TILE, y))
                    corners.append((run * TILE, y))
                    run = None
        for c in range(self.cols):
            run = None                      # right edges, top to bottom
            for r in range(self.rows + 1):
                open_right = (r < self.rows and stone(r, c)
                              and not stone(r, c + 1))
                if open_right and run is None:
                    run = r
                elif not open_right and run is not None:
                    x = (c + 1) * TILE
                    ax.append(x); ay.append(run * TILE)
                    bx.append(x); by.append(r * TILE)
                    corners.append((x, run * TILE))
                    corners.append((x, r * TILE))
                    run = None
            run = None                      # left edges, bottom to top
            for r in range(self.rows + 1):
                open_left = (r < self.rows and stone(r, c)
                             and not stone(r, c - 1))
                if open_left and run is None:
                    run = r
                elif not open_left and run is not None:
                    x = c * TILE
                    ax.append(x); ay.append(r * TILE)
                    bx.append(x); by.append(run * TILE)
                    corners.append((x, r * TILE))
                    corners.append((x, run * TILE))
                    run = None
        self.seg_ax = np.array(ax, dtype=np.float64)
        self.seg_ay = np.array(ay, dtype=np.float64)
        self.seg_bx = np.array(bx, dtype=np.float64)
        self.seg_by = np.array(by, dtype=np.float64)
        self.corners = np.array(corners, dtype=np.float64) if corners else np.zeros((0, 2))
        self.segments = list(zip(ax, ay, bx, by))
        self.base_segments = (self.seg_ax, self.seg_ay, self.seg_bx,
                              self.seg_by, self.corners, self.segments)

    def open_tiles(self, margin_tiles=1):
        out = []
        for r in range(margin_tiles, self.rows - margin_tiles):
            for c in range(margin_tiles, self.cols - margin_tiles):
                if self.grid[r][c] == FLOOR:
                    out.append((c, r))
        return out

    def tile_center(self, col, row):
        return (col + 0.5) * TILE, (row + 0.5) * TILE

    def build_doors(self):
        """Make a `Door` for each carved opening.

        Late, and separately from `_carve_doors`, because a door is a world
        rectangle and the carve works in tiles - and because `doors` imports
        `Rect` from here, so the import has to go the other way.
        """
        from . import doors as door_mod
        self.door_objects = {}
        for side in self.doors:
            x0, y0, x1, y1 = self.door_zone(side)
            self.door_objects[side] = door_mod.Door(side, x0, y0, x1, y1)
        return self.door_objects

    def door_rects(self):
        """Collision rectangles for every leaf currently in an opening."""
        out = []
        for door in self.door_objects.values():
            out.extend(door.rects())
        return out

    def refresh_occluders(self):
        """Rebuild the arrays the lighting reads from the doors' current state."""
        from . import doors as door_mod
        door_mod.collect_segments(
            self, [d for d in self.door_objects.values() if d.blocking])

    # -------------------------------------------------------------- doors --
    def door_center(self, side):
        """World centre of the opening on `side`."""
        c0, c1, r0, r1 = self.doors[side]
        return ((c0 + c1 + 1) * 0.5 * TILE, (r0 + r1 + 1) * 0.5 * TILE)

    def door_entry(self, side, inset=1.6):
        """Where a player arriving through `side` should be standing.

        Far enough in that they are clear of the trigger they just came
        through - re-entering the room you left the instant you arrive is the
        one bug a door system is guaranteed to have if nobody thinks about it.
        """
        dc, dr = INWARD[side]
        cx, cy = self.door_center(side)
        return cx + dc * inset * TILE, cy + dr * inset * TILE

    def door_zone(self, side):
        """(x0, y0, x1, y1) the player must be inside to use this door."""
        c0, c1, r0, r1 = self.doors[side]
        return (c0 * TILE, r0 * TILE, (c1 + 1) * TILE, (r1 + 1) * TILE)

    def door_at(self, x, y):
        """The side whose opening contains (x, y), or None."""
        for side in self.doors:
            x0, y0, x1, y1 = self.door_zone(side)
            if x0 <= x <= x1 and y0 <= y <= y1:
                return side
        return None

    def near_door(self, x, y, tiles=DOOR_CLEAR + 1):
        """Is (x, y) inside the approach to any door?

        Used to keep spawns and braziers off the mat: arriving through a door
        into a spitter's face is not a fight, it is an ambush the player had
        no way to see coming.
        """
        reach = tiles * TILE
        for side in self.doors:
            cx, cy = self.door_center(side)
            dc, dr = INWARD[side]
            # A box that runs inward from the opening, not a disc: the
            # dangerous ground is the corridor in front of the door.
            along = (x - cx) * dc + (y - cy) * dr
            across = abs((x - cx) * dr - (y - cy) * dc)
            if -TILE <= along <= reach and across <= TILE * 1.6:
                return True
        return False


# --------------------------------------------------------------------------
# Chamber layouts
# --------------------------------------------------------------------------
def _lay_pillars(level, rng):
    step = rng.choice((3, 4))
    size = 1 if step == 3 else rng.choice((1, 2))
    for r in range(2, level.rows - 2, step):
        for c in range(2, level.cols - 2, step):
            if rng.chance(0.22):
                continue
            for dr in range(size):
                for dc in range(size):
                    if r + dr < level.rows - 2 and c + dc < level.cols - 2:
                        level.grid[r + dr][c + dc] = WALL


def _lay_cross(level, rng):
    mc = level.cols // 2
    mr = level.rows // 2
    thickness = rng.choice((1, 2))
    gap = rng.choice((2, 3))
    for r in range(2, level.rows - 2):
        if abs(r - mr) <= gap:
            continue
        for t in range(thickness):
            level.grid[r][mc + t] = WALL
    for c in range(2, level.cols - 2):
        if abs(c - mc) <= gap:
            continue
        for t in range(thickness):
            level.grid[mr + t][c] = WALL
    for corner_r, corner_c in ((3, 3), (3, level.cols - 5),
                               (level.rows - 5, 3), (level.rows - 5, level.cols - 5)):
        if rng.chance(0.7):
            level.grid[corner_r][corner_c] = WALL
            level.grid[corner_r + 1][corner_c] = WALL
            level.grid[corner_r][corner_c + 1] = WALL


def _lay_rings(level, rng):
    mc = level.cols / 2.0 - 0.5
    mr = level.rows / 2.0 - 0.5
    radii = [3.0, 6.0] if level.rows < 17 else [3.0, 6.0, 9.0]
    for radius in radii:
        gaps = [rng.angle() for _ in range(rng.randint(2, 3))]
        for r in range(1, level.rows - 1):
            for c in range(1, level.cols - 1):
                d = math.hypot((c - mc) * 0.86, (r - mr))
                if abs(d - radius) > 0.55:
                    continue
                a = math.atan2(r - mr, c - mc)
                if any(abs((a - g + math.pi) % (2 * math.pi) - math.pi) < 0.45 for g in gaps):
                    continue
                level.grid[r][c] = WALL


def _lay_shards(level, rng):
    count = rng.randint(7, 11)
    for _ in range(count):
        w = rng.randint(1, 4)
        h = rng.randint(1, 4)
        c = rng.randint(2, max(2, level.cols - 3 - w))
        r = rng.randint(2, max(2, level.rows - 3 - h))
        for dr in range(h):
            for dc in range(w):
                level.grid[r + dr][c + dc] = WALL


def _lay_gauntlet(level, rng):
    lanes = rng.randint(3, 5)
    spacing = max(3, (level.cols - 4) // (lanes + 1))
    for i in range(1, lanes + 1):
        c = 2 + i * spacing
        if c >= level.cols - 2:
            break
        gap_start = rng.randint(2, level.rows - 6)
        for r in range(2, level.rows - 2):
            if gap_start <= r <= gap_start + 2:
                continue
            level.grid[r][c] = WALL


def _lay_spiral(level, rng):
    c0, r0 = 2, 2
    c1, r1 = level.cols - 3, level.rows - 3
    turn = 0
    while c1 - c0 > 3 and r1 - r0 > 3:
        if turn % 4 == 0:
            for c in range(c0, c1 - 2):
                level.grid[r0][c] = WALL
            r0 += 3
        elif turn % 4 == 1:
            for r in range(r0, r1 - 2):
                level.grid[r][c1] = WALL
            c1 -= 3
        elif turn % 4 == 2:
            for c in range(c0 + 3, c1 + 1):
                level.grid[r1][c] = WALL
            r1 -= 3
        else:
            for r in range(r0 + 1, r1 + 1):
                level.grid[r][c0] = WALL
            c0 += 3
        turn += 1


def _lay_sanctum(level, rng):
    """Boss arena: open centre, heavy corners, a little cover."""
    for corner_r, corner_c in ((2, 2), (2, level.cols - 5),
                               (level.rows - 5, 2), (level.rows - 5, level.cols - 5)):
        for dr in range(3):
            for dc in range(3):
                if dr == 1 and dc == 1:
                    continue
                level.grid[corner_r + dr][corner_c + dc] = WALL
    mc, mr = level.cols // 2, level.rows // 2
    for dc in (-1, 0, 1):
        level.grid[2][mc + dc] = WALL
        level.grid[level.rows - 3][mc + dc] = WALL


_LAYOUTS = {
    'pillars': _lay_pillars,
    'cross': _lay_cross,
    'rings': _lay_rings,
    'shards': _lay_shards,
    'gauntlet': _lay_gauntlet,
    'spiral': _lay_spiral,
    'sanctum': _lay_sanctum,
}


# --------------------------------------------------------------------------
# Connectivity
# --------------------------------------------------------------------------
def _flood_open(level, start):
    seen = set()
    stack = [start]
    while stack:
        cell = stack.pop()
        if cell in seen:
            continue
        c, r = cell
        if c < 0 or r < 0 or c >= level.cols or r >= level.rows:
            continue
        if level.grid[r][c] == WALL:
            continue
        seen.add(cell)
        stack.append((c + 1, r))
        stack.append((c - 1, r))
        stack.append((c, r + 1))
        stack.append((c, r - 1))
    return seen


def _ensure_connected(level, rng):
    """Punch holes until every open tile is reachable from the centre."""
    for _ in range(60):
        open_cells = [(c, r) for r in range(level.rows) for c in range(level.cols)
                      if level.grid[r][c] == FLOOR]
        if not open_cells:
            return
        mc, mr = level.cols // 2, level.rows // 2
        if level.grid[mr][mc] == WALL:
            level.grid[mr][mc] = FLOOR
        reached = _flood_open(level, (mc, mr))
        stranded = [cell for cell in open_cells if cell not in reached]
        if not stranded:
            return
        # Carve from a stranded cell toward the centre until we meet the region.
        c, r = rng.choice(stranded)
        while (c, r) not in reached:
            level.grid[r][c] = FLOOR
            reached.add((c, r))
            if c != mc and (r == mr or rng.chance(0.5)):
                c += 1 if mc > c else -1
            elif r != mr:
                r += 1 if mr > r else -1
            else:
                break


# --------------------------------------------------------------------------
# Floor baking
# --------------------------------------------------------------------------
# How much of the wall tile's brightness survives into the level image.
# The tops used to be at 0.34 of it, which is darker than the floor
# beside them, and is why they read as black shapes however much light
# landed on them.
WALL_ALBEDO = 0.50

# How much of each wall's footprint is given over to the side face you can
# actually see from above, in design units, and which way that face points.
# The normal is mostly down-screen with a little lift, which is what a
# vertical surface looks like to a camera hanging over the room.
WALL_FACE = 19.0
WALL_FACE_NORMAL = (0.0, 0.90, 0.44)


def _grade_rgba(image, act):
    """Grade an RGBA layer without touching its alpha.

    The wall layer carries the shape of the masonry in its alpha channel, and
    running a colour grade over that would eat the edges of every block.
    """
    if image.mode != 'RGBA':
        return acts.grade_image(image, act)
    rgb, alpha = image.convert('RGB'), image.getchannel('A')
    graded = acts.grade_image(rgb, act)
    graded.putalpha(alpha)
    return graded


def _affine_lut(offsets, offset_scale, divisor, bands=4):
    """A `PIL.Image.point` table for `clip((x - offset*offset_scale) / divisor)`.

    Evaluated through the same float32 arithmetic, in the same order, as the
    whole-array version it replaces - float32 rather than Python's float64,
    and a divide rather than a multiply by the reciprocal, because either
    difference moves the odd byte by one and a lookup table is only worth
    having if it is the same picture. Verified byte-for-byte against the
    array version over every chamber size and archetype.
    """
    v = np.arange(256, dtype=np.float32)
    shifts = np.asarray(offsets, dtype=np.float32) * offset_scale
    table = []
    for band in range(bands):
        if band >= len(shifts):
            table.extend(range(256))            # alpha, untouched
            continue
        out = (v - shifts[band]) / divisor
        table.extend(np.clip(out, 0, 255).astype(np.uint8).tolist())
    return table


def _scale_lut(gain, bands=3):
    """A `point` table for a flat multiply, clipped and truncated as before."""
    v = np.arange(256, dtype=np.float32) * gain
    band = np.clip(v, 0, 255).astype(np.uint8).tolist()
    return band * bands


def _bake_layers(level, rng, seed):
    """Bake the chamber into two images: floor beneath the light, walls above.

    Splitting them is what makes the lighting read correctly. The lantern's
    visibility polygon is drawn between the two layers, so it pools on the
    floor and stops at the masonry instead of washing over it - the walls then
    only brighten where `lighting.lit_wall_edges` says light actually falls.

    Everything here is authored in design units and multiplied by the render
    scale on the way into the canvas, so the stone is rasterised at the display's
    real pixel count rather than being upscaled at draw time.

    Note both canvases are built in RGB: PIL's ImageDraw only alpha-blends when
    the target is RGB, so drawing translucent detail straight onto an RGBA
    canvas would punch holes instead of shading. Alpha is attached at the end.
    """
    generation = art.bake_generation()
    level.art_keys = []
    scale = art.SCALE
    w = art.px(level.width)
    h = art.px(level.height)

    def q(value):
        return value * scale

    tile_variants = [art.floor_tile(seed * 7 + i) for i in range(3)]
    tile_px = tile_variants[0].size[0]

    # ---- floor -----------------------------------------------------------
    floor = Image.new('RGB', (w, h), (9, 12, 19))
    for ty in range(0, h, tile_px):
        for tx in range(0, w, tile_px):
            variant = tile_variants[((tx // tile_px) * 3 + (ty // tile_px) * 5) % 3]
            floor.paste(variant.convert('RGB'), (tx, ty))

    draw = ImageDraw.Draw(floor, 'RGBA')

    grout = max(1, int(round(q(2))))
    step = max(1, int(round(q(TILE * 2))))
    for gx in range(0, w, step):
        draw.line([(gx, 0), (gx, h)], fill=(4, 6, 11, 80), width=grout)
    for gy in range(0, h, step):
        draw.line([(0, gy), (w, gy)], fill=(4, 6, 11, 80), width=grout)

    for _ in range(int(level.width * level.height / 30000)):
        cx = q(rng.uniform(0, level.width))
        cy = q(rng.uniform(0, level.height))
        rr = q(rng.uniform(30, 110))
        if rng.chance(0.5):
            tint = (4, 7, 13, rng.randint(30, 70))
        else:
            tint = (32, 40, 58, rng.randint(22, 46))
        draw.ellipse([cx - rr, cy - rr * 0.66, cx + rr, cy + rr * 0.66], fill=tint)

    for _ in range(rng.randint(2, 3)):
        cx = q(rng.uniform(level.width * 0.25, level.width * 0.75))
        cy = q(rng.uniform(level.height * 0.25, level.height * 0.75))
        rr = q(rng.uniform(55, 105))
        col = (46, 60, 92, rng.randint(38, 60))
        line_w = max(1, int(round(q(1))))
        draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], outline=col,
                     width=max(1, int(round(q(2)))))
        inner = rr * 0.6
        draw.ellipse([cx - inner, cy - inner, cx + inner, cy + inner],
                     outline=col, width=line_w)
        spokes = rng.randint(6, 10)
        for i in range(spokes):
            a = i / spokes * math.tau
            draw.line([(cx + math.cos(a) * inner, cy + math.sin(a) * inner),
                       (cx + math.cos(a) * rr, cy + math.sin(a) * rr)],
                      fill=col, width=line_w)

    # Contact shadow, built at a fraction of the canvas and scaled back up.
    shrink = max(1, int(round(3 * scale)))
    sw, sh = max(1, w // shrink), max(1, h // shrink)
    shadow = Image.new('L', (sw, sh), 0)
    sdraw = ImageDraw.Draw(shadow)
    for rc in level.rects:
        sdraw.rectangle([q(rc.x - 10) / shrink, q(rc.y - 6) / shrink,
                         q(rc.right + 10) / shrink, q(rc.bottom + 20) / shrink],
                        fill=190)
    shadow = shadow.filter(ImageFilter.GaussianBlur(12 * scale / shrink))
    shadow = shadow.resize((w, h), Image.BILINEAR)
    shadow_rgba = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    shadow_rgba.putalpha(shadow)
    floor = Image.alpha_composite(floor.convert('RGBA'), shadow_rgba)

    # The floor is composited over the light at SHADOW_FLOOR_MIX, so pre-divide
    # to land back on the intended brightness: result = void*(1-k) + baked*k.
    #
    # Through a lookup table rather than through the array. This is an affine
    # map on each channel independently, which is the one thing a table does
    # exactly, and it used to be four full-chamber float32 temporaries - at
    # native scale on a large chamber, better than half a gigabyte of memory
    # traffic for an operation with 256 distinct answers in it.
    k = SHADOW_FLOOR_MIX / 100.0
    floor = floor.point(_affine_lut(palette.VOID_RGB, 1.0 - k, max(k, 1e-3)))

    key_floor = ('level', 'floor', seed, level.cols, level.rows, level.archetype)
    # The act's grade, applied once to the finished layer. Everything the
    # bake drew is in here - tiles, grout, cracks, scorch - so all of it
    # shifts together and none of it can be missed. See `lumen/acts.py`.
    floor = acts.grade_image(floor, acts.of(level.depth))
    level.floor_key = key_floor
    level.floor_image = art.wrap(floor, key_floor)
    level.art_keys.append(key_floor)
    # The relief the lantern picks out of the stone. Derived from the layer
    # that was just baked, so it lines up with it exactly. Kept at the half
    # resolution it is built at and stretched over the chamber by the GPU
    # instead - see `art.normal_map`; the drawing has to name the size.
    level.floor_normal = art.wrap(art.normal_map(floor, keep_half=True),
                                  key_floor + ('normal',))
    level.art_keys.append(key_floor + ('normal',))

    # ---- walls -----------------------------------------------------------
    wall_tex = art.wall_tile(seed * 13 + 5).convert('RGB')
    wall_px = wall_tex.size[0]

    body = Image.new('RGB', (w, h), (0, 0, 0))
    for ty in range(0, h, wall_px):
        for tx in range(0, w, wall_px):
            body.paste(wall_tex, (tx, ty))

    # Bright enough that the top of a wall is a surface you can see rather
    # than a black shape with a lit line on its near edge. Through a table,
    # for the same reason the floor's pre-divide is.
    body = body.point(_scale_lut(WALL_ALBEDO))

    # A wall is a block of stone, and a block has a side. Seen from above the
    # only side you can see is the one facing down the screen, so the bottom
    # of each wall's *exposed* footprint is given over to it.
    #
    # Exposed is the operative word. This used to draw a rim, a chamfer and a
    # face around every rectangle the generator produced, which meant two
    # blocks sitting flush against each other got a seam down the join - an
    # outline, a highlight and a shadow, in the middle of what is plainly one
    # wall. The edges are worked out per tile now, from whether the neighbour
    # on that side is stone: a shared edge is not an edge, so a run of blocks
    # reads as one solid mass, and only the outside of the mass is drawn.
    wdraw = ImageDraw.Draw(body, 'RGBA')
    rim = max(1, int(round(q(1.5))))
    cham = max(1, int(round(q(4.0))))

    def stone(r, c):
        return (0 <= r < level.rows and 0 <= c < level.cols
                and level.grid[r][c] != FLOOR)

    exposed = []
    for r in range(level.rows):
        for c in range(level.cols):
            if not stone(r, c):
                continue
            x0, y0 = q(c * TILE), q(r * TILE)
            x1, y1 = q((c + 1) * TILE) - 1, q((r + 1) * TILE) - 1
            up, down = not stone(r - 1, c), not stone(r + 1, c)
            left, right = not stone(r, c - 1), not stone(r, c + 1)
            if down:
                exposed.append((x0, x1, y1))
            # The arris, along whichever sides are actually outside faces.
            for i in range(cham):
                f = 1.0 - i / max(1.0, cham - 1.0)
                a = int(30 + 84 * f * f)
                if up:
                    wdraw.rectangle([x0, y0 + rim + i, x1, y0 + rim + i],
                                    fill=(74, 88, 118, a))
                if down:
                    wdraw.rectangle([x0, y1 - rim - i, x1, y1 - rim - i],
                                    fill=(74, 88, 118, a))
                if left:
                    wdraw.rectangle([x0 + rim + i, y0, x0 + rim + i, y1],
                                    fill=(74, 88, 118, a))
                if right:
                    wdraw.rectangle([x1 - rim - i, y0, x1 - rim - i, y1],
                                    fill=(74, 88, 118, a))
            # And a hard outer rim, again only where the mass actually ends.
            if up:
                wdraw.rectangle([x0, y0, x1, y0 + rim - 1], fill=(2, 3, 8, 255))
            if down:
                wdraw.rectangle([x0, y1 - rim + 1, x1, y1], fill=(2, 3, 8, 255))
            if left:
                wdraw.rectangle([x0, y0, x0 + rim - 1, y1], fill=(2, 3, 8, 255))
            if right:
                wdraw.rectangle([x1 - rim + 1, y0, x1, y1], fill=(2, 3, 8, 255))

    # ---- the side face ---------------------------------------------------
    # A face is `WALL_FACE` units deep along the bottom of an exposed block,
    # which is two or three percent of a chamber - so the shading is done on
    # those strips and nowhere else. It used to be four full-chamber float32
    # passes (a copy of the layer, a height field, the shade, and the
    # `np.where` that put it back) to change a fortieth of the picture.
    #
    # The strips cannot overlap, which is what makes this exact rather than
    # merely close: a strip is at the bottom of a block whose neighbour below
    # is floor, so two of them in one column are at least two tiles apart,
    # and `WALL_FACE` is a third of a tile.
    face_px = max(3, int(round(q(WALL_FACE))))
    arr = np.array(body, dtype=np.uint8)
    #: Where the faces ended up, so the normal map can be told about them.
    #: Kept as rectangles rather than as a full-size mask: the normal is
    #: built at half resolution, and a mask painted at full size and then
    #: decimated picks up or loses whichever edge row it lands on.
    faces = []
    course_period = 2.0 * np.pi / max(2.0, q(9.0))
    for x0, x1, yb in exposed:
        x0, x1, yb = int(x0), int(x1) + 1, int(yb) + 1
        yt = max(0, yb - face_px)
        if yb - yt < 2:
            continue
        faces.append((yt, yb, x0, x1))
        # 0 at the top of the face, 1 where it meets the floor.
        t = np.linspace(0.0, 1.0, yb - yt, dtype=np.float32)[:, None]
        # Courses running along the face, so it is stone rather than a
        # gradient.
        yy = np.arange(yt, yb, dtype=np.float32)[:, None]
        course = 0.5 + 0.5 * np.cos(yy * course_period)
        # Nothing painted here decides where the light is. The face used to
        # carry a hard highlight along its top edge, which was a bright line
        # drawn a face's width inside the wall - a second, permanent version
        # of exactly the artefact the light was moved off. What is left is
        # what a vertical surface has whatever falls on it: a little darker
        # than the top it belongs to, its own courses, and a contact shadow
        # where it meets the floor. Its shape comes from its normal, and the
        # lights find it.
        shade = 0.78 + 0.10 * course - 0.10 * t
        shade = shade * (1.0 - 0.55 * np.clip((t - 0.80) / 0.20, 0.0, 1.0))
        strip = arr[yt:yb, x0:x1, :3].astype(np.float32) * shade[..., None]
        arr[yt:yb, x0:x1, :3] = np.clip(strip, 0, 255).astype(np.uint8)
    body = Image.fromarray(arr, 'RGB')

    mask = Image.new('L', (w, h), 0)
    mdraw = ImageDraw.Draw(mask)
    for rc in level.rects:
        mdraw.rectangle([q(rc.x), q(rc.y), q(rc.right) - 1, q(rc.bottom) - 1],
                        fill=255)

    walls = body.convert('RGBA')
    walls.putalpha(mask)

    key_wall = ('level', 'wall', seed, level.cols, level.rows, level.archetype)
    walls = _grade_rgba(walls, acts.of(level.depth))
    level.wall_key = key_wall
    level.wall_image = art.wrap(walls, key_wall)
    level.art_keys.append(key_wall)
    # Shallower than the floor: a wall top is dressed stone, and the courses
    # in it are joints rather than the broken surface a floor has.
    # The top's relief comes out of its own shading, but the face is a
    # vertical surface and its normal cannot be derived from a picture of it:
    # it points down the screen, away from the block, and that is what makes
    # a wall light up when the lantern comes round in front of it.
    wall_n = np.asarray(art.normal_map(walls, 0.7, keep_half=True),
                        dtype=np.float32).copy()
    fn = np.array(WALL_FACE_NORMAL, dtype=np.float32)
    fn = fn / np.linalg.norm(fn)
    # Painted at the normal's own resolution from the rectangles the faces
    # were shaded in, rather than at the layer's and then decimated - a mask
    # built at full size and sampled every other row keeps or drops each edge
    # depending on which parity it happens to land on.
    hh, hw = wall_n.shape[0], wall_n.shape[1]
    face_half = np.zeros((hh, hw), bool)
    for yt, yb, x0, x1 in faces:
        face_half[min(hh, yt // 2):min(hh, (yb + 1) // 2),
                  min(hw, x0 // 2):min(hw, (x1 + 1) // 2)] = True
    for i in range(3):
        wall_n[..., i] = np.where(face_half, fn[i] * 127.5 + 127.5,
                                  wall_n[..., i])
    level.wall_normal = art.wrap(
        Image.fromarray(np.clip(wall_n, 0, 255).astype(np.uint8), 'RGBA'),
        key_wall + ('normal',))
    level.art_keys.append(key_wall + ('normal',))

    _bake_minimap(level, seed)
    # Last, so a chamber is only ever current once every layer is in place.
    level.art_generation = generation


def _bake_minimap(level, seed, size=148):
    """Pre-render the minimap's static geometry.

    The chamber's walls never move, so drawing twenty-odd rectangles into the
    corner every frame was pure waste - at ~35 us a shape end to end that is
    most of a millisecond. One small image replaces the lot; only the player,
    enemies, braziers and rift are still drawn live on top.
    """
    fit = min(size / level.width, size / level.height)
    mw = max(1, art.px(level.width * fit))
    mh = max(1, art.px(level.height * fit))
    raster = art.SCALE * fit

    canvas = Image.new('RGBA', (mw, mh), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas, 'RGBA')
    for rc in level.rects:
        draw.rectangle([rc.x * raster, rc.y * raster,
                        max(rc.x * raster, rc.right * raster - 1),
                        max(rc.y * raster, rc.bottom * raster - 1)],
                       fill=(74, 92, 128, 122))

    key = ('level', 'minimap', seed, level.cols, level.rows, level.archetype)
    level.minimap_image = art.wrap(canvas, key)
    level.art_keys.append(key)
    # Kept in design units: the HUD positions it, the draw layer scales it.
    level.minimap_rect = ((size - level.width * fit) * 0.5,
                          (size - level.height * fit) * 0.5,
                          level.width * fit, level.height * fit, fit)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def needs_bake(level):
    """Whether a chamber's layers are missing or belong to another scale.

    Either is a chamber that draws as nothing at all: its sprites were
    released with the cache, so the floor and the walls are simply not there
    and only the lights are. That is what walking into a room built before a
    resize used to look like.
    """
    if level.art_generation != art.GENERATION:
        return True
    for sprite in (level.floor_image, level.floor_normal, level.wall_image,
                   level.wall_normal, level.minimap_image):
        if sprite is not None and not sprite.alive():
            return True
    return False


def rebake(level):
    """Re-render a chamber's baked layers, e.g. after a render-scale change."""
    from . import rng as rng_mod
    seed = _bake_seed(level.depth, level.cols, level.rows, level.archetype,
                      level.bake_room)
    _bake_layers(level, rng_mod.Rng(seed), seed)


def _bake_seed(depth, cols, rows, archetype, room=0):
    """Deterministic seed for a chamber's cosmetic bake.

    `room` is the floorplan's own per-room seed. Without it every room of a
    size on a floor shares a depth, a footprint and often an archetype, and
    bakes to the identical stone - which reads, correctly, as the player
    walking in a circle.
    """
    seed = (depth * 7919 + cols * 131 + rows * 17 + (room & 0xFFFF) * 2311) % 100000
    for i, ch in enumerate(archetype):
        seed = (seed * 31 + ord(ch) + i) % 100000
    return seed


def generate(depth, rng, boss=False, doors=(), size='medium', seed=0,
             braziers=None, bake=True):
    """One chamber.

    `doors` is the sides that open onto neighbouring rooms; `size` names a
    footprint in `ROOM_SIZES`; `seed` distinguishes rooms that would
    otherwise bake identically - a floor now holds a dozen chambers of the
    same depth, and without it half of them came out the same room twice.
    """
    if boss:
        size = 'boss'
        archetype = 'sanctum'
    else:
        # Weighted by act rather than picked flat. The first act favours
        # shapes you can read at a glance, the second opens out into long
        # sightlines, and the last is short of anywhere to back into.
        archetype = rng.weighted(list(acts.of(depth).archetypes))

    base_cols, base_rows = ROOM_SIZES.get(size, ROOM_SIZES['medium'])
    # A little jitter so two rooms of a size are not the same room. Kept
    # even so the door, which sits on the middle of a wall, stays on a whole
    # tile: an odd chamber and an even one put their centre in different
    # places and the opening slides half a tile with it.
    cols = base_cols + rng.randint(0, 2) * 2
    rows = base_rows + rng.randint(0, 1) * 2

    level = Level(cols, rows, depth, archetype)
    level._carve_thick_border()
    _LAYOUTS[archetype](level, rng)
    # The layout is free to draw over the border and several of them do, so
    # both rings go back down before the doors are cut through them.
    level._carve_thick_border()
    level._carve_doors(doors)
    _ensure_connected(level, rng)
    # `_ensure_connected` punches holes toward the centre and can take the
    # border with it. Re-assert, then re-open the doors it may have closed.
    level._carve_thick_border()
    level._carve_doors(doors)
    level._merge_rects()
    level._build_segments()
    level.build_doors()

    # Player starts on the most open tile near the centre. Only the room a
    # floor begins in actually uses this; every other room is entered
    # through a door and places the player with `door_entry`.
    open_cells = level.open_tiles(BORDER)
    if not open_cells:
        open_cells = level.open_tiles(1)
    mc, mr = cols / 2.0, rows / 2.0
    open_cells.sort(key=lambda cr: (cr[0] - mc) ** 2 + (cr[1] - mr) ** 2)
    start_col, start_row = open_cells[0]
    level.player_start = level.tile_center(start_col, start_row)

    # Spawn points: open tiles far from the middle of the room, and never on
    # a doorway's approach.
    cx, cy = level.width * 0.5, level.height * 0.5
    scored = []
    for c, r in open_cells:
        x, y = level.tile_center(c, r)
        if not level.is_open_at(x, y, TILE * 0.55):
            continue
        if level.near_door(x, y):
            continue
        scored.append((math.hypot(x - cx, y - cy), x, y))
    scored.sort(reverse=True)
    if not scored:
        # A room small enough that every open tile is somebody's doormat.
        # Better a spawn on the mat than a room that cannot be populated.
        for c, r in open_cells:
            x, y = level.tile_center(c, r)
            scored.append((math.hypot(x - cx, y - cy), x, y))
        scored.sort(reverse=True)
    level.spawn_points = [(x, y) for _, x, y in scored[:max(6, len(scored) // 2)]]

    # Braziers in roomy spots, spaced out from one another.
    candidates = [(x, y) for _, x, y in scored]
    placed = []
    if braziers is None:
        # Fire gets scarcer the further down you are, which is the fuel
        # budget stated as level design rather than as a number on a curve.
        lo, hi = acts.of(depth).braziers
        braziers = 3 if boss else rng.randint(lo, hi)
    for x, y in rng.shuffled(candidates):
        if len(placed) >= braziers:
            break
        if all(math.hypot(x - bx, y - by) > TILE * 4 for bx, by in placed):
            placed.append((x, y))
    level.braziers = [Brazier(x, y) for x, y in placed]

    # A stable seed: Python randomises string hashing per process, so
    # hash((depth, cols, rows, archetype)) gave a different floor texture on
    # every launch and made rendered frames impossible to reproduce.
    level.bake_room = seed
    # The bake is ~100 ms and produces nothing the geometry depends on, so
    # the checking tools skip it and generate thousands of rooms a second.
    if bake:
        _bake_layers(level, rng, _bake_seed(depth, cols, rows, archetype, seed))
    return level
