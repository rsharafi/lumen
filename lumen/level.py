"""Chamber generation, collision geometry, and the baked floor image.

A chamber is authored on a tile grid, then the solid tiles are merged into as
few rectangles as possible. That merge matters twice over: rectangles are what
the physics resolves against, and their edges are the segment soup the
shadowcaster traces. Fewer, larger rectangles means both run faster.
"""

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from . import art, noise
from . import palette
from .config import SHADOW_FLOOR_MIX, TILE
from .mathx import clamp

WALL = 1
FLOOR = 0

ARCHETYPES = ('pillars', 'cross', 'rings', 'shards', 'gauntlet', 'spiral')


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
        self.floor_key = None
        self.wall_image = None
        self.wall_normal = None
        self.wall_key = None
        self.minimap_image = None
        self.minimap_rect = None    # (offset_x, offset_y, width, height)
        self.braziers = []
        self.spawn_points = []
        self.player_start = (self.width * 0.5, self.height * 0.5)

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
        for _ in range(2):
            moved = False
            for rect in self.rects:
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
        for rect in self.rects:
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
        ax, ay, bx, by = [], [], [], []
        corners = []
        for rc in self.rects:
            x0, y0, x1, y1 = rc.x, rc.y, rc.right, rc.bottom
            pts = ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
            for i in range(4):
                p = pts[i]
                q = pts[(i + 1) % 4]
                ax.append(p[0]); ay.append(p[1])
                bx.append(q[0]); by.append(q[1])
            corners.extend(pts)
        self.seg_ax = np.array(ax, dtype=np.float64)
        self.seg_ay = np.array(ay, dtype=np.float64)
        self.seg_bx = np.array(bx, dtype=np.float64)
        self.seg_by = np.array(by, dtype=np.float64)
        self.corners = np.array(corners, dtype=np.float64) if corners else np.zeros((0, 2))
        self.segments = list(zip(ax, ay, bx, by))

    def open_tiles(self, margin_tiles=1):
        out = []
        for r in range(margin_tiles, self.rows - margin_tiles):
            for c in range(margin_tiles, self.cols - margin_tiles):
                if self.grid[r][c] == FLOOR:
                    out.append((c, r))
        return out

    def tile_center(self, col, row):
        return (col + 0.5) * TILE, (row + 0.5) * TILE


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
    k = SHADOW_FLOOR_MIX / 100.0
    void = np.array(palette.VOID_RGB, dtype=np.float32)
    px_arr = np.asarray(floor, dtype=np.float32)
    px_arr[..., :3] = (px_arr[..., :3] - void * (1.0 - k)) / max(k, 1e-3)
    floor = Image.fromarray(np.clip(px_arr, 0, 255).astype(np.uint8), 'RGBA')

    key_floor = ('level', 'floor', seed, level.cols, level.rows, level.archetype)
    level.floor_key = key_floor
    level.floor_image = art.wrap(floor, key_floor)
    # The relief the lantern picks out of the stone. Derived from the layer
    # that was just baked, so it lines up with it exactly.
    level.floor_normal = art.wrap(art.normal_map(floor),
                                  key_floor + ('normal',))

    # ---- walls -----------------------------------------------------------
    wall_tex = art.wall_tile(seed * 13 + 5).convert('RGB')
    wall_px = wall_tex.size[0]

    body = Image.new('RGB', (w, h), (0, 0, 0))
    for ty in range(0, h, wall_px):
        for tx in range(0, w, wall_px):
            body.paste(wall_tex, (tx, ty))

    # Bright enough that the top of a wall is a surface you can see rather
    # than a black shape with a lit line on its near edge.
    pixels = np.asarray(body, dtype=np.float32) * WALL_ALBEDO
    body = Image.fromarray(np.clip(pixels, 0, 255).astype(np.uint8), 'RGB')

    # A wall is a block of stone, and a block has a side. Seen from above the
    # only side you can see is the one facing down the screen, so the bottom
    # of each wall's footprint is given over to it: the top face stops short,
    # and the band below it is the face falling away to the floor. That band
    # is what turns a lid into a wall - and because it is given a normal that
    # points down-screen rather than up, it takes light from a completely
    # different direction than the top does, so the lantern picks out whichever
    # faces it happens to be standing in front of.
    wdraw = ImageDraw.Draw(body, 'RGBA')
    rim = max(1, int(round(q(1.5))))
    cham = max(1, int(round(q(4.0))))
    for rc in level.rects:
        x0, y0 = q(rc.x), q(rc.y)
        x1, y1 = q(rc.right) - 1, q(rc.bottom) - 1
        # The arris along the top edge, brightest at the edge itself.
        for i in range(cham):
            f = 1.0 - i / max(1.0, cham - 1.0)
            wdraw.rectangle([x0 + rim + i, y0 + rim + i,
                             x1 - rim - i, y1 - rim - i],
                            outline=(74, 88, 118, int(30 + 84 * f * f)),
                            width=1)
        wdraw.rectangle([x0, y0, x1, y1], outline=(2, 3, 8, 255), width=rim)

    # ---- the side face ---------------------------------------------------
    face_px = max(3, int(round(q(WALL_FACE))))
    arr = np.asarray(body, dtype=np.float32).copy()
    face_t = np.zeros(arr.shape[:2], np.float32)     # 0 at the top of the face
    is_face = np.zeros(arr.shape[:2], bool)
    for rc in level.rects:
        x0, x1 = int(q(rc.x)), int(q(rc.right))
        yb = int(q(rc.bottom))
        yt = max(int(q(rc.y)), yb - face_px)
        if yb - yt < 2:
            continue
        col = np.linspace(0.0, 1.0, yb - yt, dtype=np.float32)[:, None]
        face_t[yt:yb, x0:x1] = col
        is_face[yt:yb, x0:x1] = True

    t = face_t[..., None]
    # Courses running along the face, so it is stone rather than a gradient.
    yy = np.arange(arr.shape[0], dtype=np.float32)[:, None, None]
    course = 0.5 + 0.5 * np.cos(yy * (2.0 * np.pi / max(2.0, q(9.0))))
    lit = np.clip(1.0 - t * 1.35, 0.0, 1.0) ** 1.4        # falls into shadow
    shade = 0.20 + 0.62 * lit + 0.10 * course * (1.0 - t)
    faced = arr[..., :3] * shade
    # The arris where the face meets the top, and the dark line at the floor.
    faced += 92.0 * np.clip(1.0 - t / 0.10, 0.0, 1.0)
    faced *= np.clip(0.35 + 0.65 * (1.0 - t) / 0.22, 0.35, 1.0) ** 0.5
    arr[..., :3] = np.where(is_face[..., None], np.clip(faced, 0, 255),
                            arr[..., :3])
    body = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), 'RGB')

    mask = Image.new('L', (w, h), 0)
    mdraw = ImageDraw.Draw(mask)
    for rc in level.rects:
        mdraw.rectangle([q(rc.x), q(rc.y), q(rc.right) - 1, q(rc.bottom) - 1],
                        fill=255)

    walls = body.convert('RGBA')
    walls.putalpha(mask)

    key_wall = ('level', 'wall', seed, level.cols, level.rows, level.archetype)
    level.wall_key = key_wall
    level.wall_image = art.wrap(walls, key_wall)
    # Shallower than the floor: a wall top is dressed stone, and the courses
    # in it are joints rather than the broken surface a floor has.
    # The top's relief comes out of its own shading, but the face is a
    # vertical surface and its normal cannot be derived from a picture of it:
    # it points down the screen, away from the block, and that is what makes
    # a wall light up when the lantern comes round in front of it.
    wall_n = np.asarray(art.normal_map(walls, 0.7), dtype=np.float32).copy()
    fn = np.array(WALL_FACE_NORMAL, dtype=np.float32)
    fn = fn / np.linalg.norm(fn)
    for i in range(3):
        wall_n[..., i] = np.where(is_face, fn[i] * 127.5 + 127.5,
                                  wall_n[..., i])
    level.wall_normal = art.wrap(
        Image.fromarray(np.clip(wall_n, 0, 255).astype(np.uint8), 'RGBA'),
        key_wall + ('normal',))

    _bake_minimap(level, seed)


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
    # Kept in design units: the HUD positions it, the draw layer scales it.
    level.minimap_rect = ((size - level.width * fit) * 0.5,
                          (size - level.height * fit) * 0.5,
                          level.width * fit, level.height * fit, fit)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def rebake(level):
    """Re-render a chamber's baked layers, e.g. after a render-scale change."""
    from . import rng as rng_mod
    seed = _bake_seed(level.depth, level.cols, level.rows, level.archetype)
    _bake_layers(level, rng_mod.Rng(seed), seed)


def _bake_seed(depth, cols, rows, archetype):
    """Deterministic seed for a chamber's cosmetic bake."""
    seed = (depth * 7919 + cols * 131 + rows * 17) % 100000
    for i, ch in enumerate(archetype):
        seed = (seed * 31 + ord(ch) + i) % 100000
    return seed


def generate(depth, rng, boss=False):
    from .config import (CHAMBER_MAX_H, CHAMBER_MAX_W, CHAMBER_MIN_H,
                         CHAMBER_MIN_W)

    if boss:
        cols, rows = CHAMBER_MAX_W, CHAMBER_MAX_H
        archetype = 'sanctum'
    else:
        cols = rng.randint(CHAMBER_MIN_W, CHAMBER_MAX_W)
        rows = rng.randint(CHAMBER_MIN_H, CHAMBER_MAX_H)
        archetype = rng.choice(ARCHETYPES)

    level = Level(cols, rows, depth, archetype)
    level._carve_border()
    _LAYOUTS[archetype](level, rng)
    _ensure_connected(level, rng)
    level._merge_rects()
    level._build_segments()

    # Player starts on the most open tile near the centre.
    open_cells = level.open_tiles(2)
    if not open_cells:
        open_cells = level.open_tiles(1)
    mc, mr = cols / 2.0, rows / 2.0
    open_cells.sort(key=lambda cr: (cr[0] - mc) ** 2 + (cr[1] - mr) ** 2)
    start_col, start_row = open_cells[0]
    level.player_start = level.tile_center(start_col, start_row)

    # Spawn points: open tiles far from the player start.
    px, py = level.player_start
    scored = []
    for c, r in open_cells:
        x, y = level.tile_center(c, r)
        if not level.is_open_at(x, y, TILE * 0.55):
            continue
        scored.append((math.hypot(x - px, y - py), x, y))
    scored.sort(reverse=True)
    level.spawn_points = [(x, y) for _, x, y in scored[:max(8, len(scored) // 2)]]

    # Braziers in roomy spots, spaced out from one another.
    candidates = [(x, y) for _, x, y in scored]
    placed = []
    want = 3 if boss else rng.randint(2, 4)
    for x, y in rng.shuffled(candidates):
        if len(placed) >= want:
            break
        if all(math.hypot(x - bx, y - by) > TILE * 5 for bx, by in placed):
            if math.hypot(x - px, y - py) > TILE * 3:
                placed.append((x, y))
    level.braziers = [Brazier(x, y) for x, y in placed]

    # A stable seed: Python randomises string hashing per process, so
    # hash((depth, cols, rows, archetype)) gave a different floor texture on
    # every launch and made rendered frames impossible to reproduce.
    _bake_layers(level, rng, _bake_seed(depth, cols, rows, archetype))
    return level
