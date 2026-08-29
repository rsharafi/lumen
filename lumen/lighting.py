"""Real-time 2D shadowcasting.

The lantern casts a true visibility polygon: rays are fired at every wall
corner within reach (plus a hair either side, which is what lets a ray slip
past a corner and produce the shadow's edge), plus a ring of filler angles so
that unobstructed light still resolves as a smooth arc.

The whole sweep is one batched numpy expression - a (rays x segments) matrix
of ray/segment intersections - which is what makes it affordable at 60 Hz in
Python. A 40-segment chamber with ~50 corners resolves in well under a
millisecond.
"""

import math
import os

import numpy as np

from .config import LIGHT_EPSILON, LIGHT_MAX_CORNERS

_FILLER_RAYS = 44
_FILLER_ANGLES = np.linspace(0.0, math.tau, _FILLER_RAYS, endpoint=False)
_EPS = 1e-9

# Wedges narrower than this are sub-pixel at any playable light radius.
MIN_WEDGE_SPAN = 0.0045

# Longest run of blocked rays merged into one shadow polygon. Tuned by
# measurement: see the note in `shadow_bands`.
SHADOW_BAND_MAX_RUN = int(os.environ.get('LUMEN_BAND_RUN', '10'))


def cull(level, ox, oy, radius):
    """Segments whose bounding box touches the light disc, plus their corners."""
    if level.seg_ax is None or len(level.seg_ax) == 0:
        return None

    ax, ay, bx, by = level.seg_ax, level.seg_ay, level.seg_bx, level.seg_by
    lo_x = np.minimum(ax, bx) - radius
    hi_x = np.maximum(ax, bx) + radius
    lo_y = np.minimum(ay, by) - radius
    hi_y = np.maximum(ay, by) + radius
    keep = (ox >= lo_x) & (ox <= hi_x) & (oy >= lo_y) & (oy <= hi_y)
    if not keep.any():
        return None
    return ax[keep], ay[keep], bx[keep], by[keep]


def _ray_distances(angles, ox, oy, segs, radius):
    """Nearest hit distance along each ray, clipped to `radius`."""
    dx = np.cos(angles)
    dy = np.sin(angles)
    if segs is None:
        return np.full(angles.shape, radius), dx, dy

    ax, ay, bx, by = segs
    ex = bx - ax
    ey = by - ay
    aox = ax - ox
    aoy = ay - oy

    # cross(D, E) for every (ray, segment) pair.
    denom = dx[:, None] * ey[None, :] - dy[:, None] * ex[None, :]
    safe = np.abs(denom) > _EPS
    denom = np.where(safe, denom, 1.0)

    # cross(AO, E) does not depend on the ray, so it stays one-dimensional.
    t = (aox * ey - aoy * ex)[None, :] / denom
    u = (aox[None, :] * dy[:, None] - aoy[None, :] * dx[:, None]) / denom

    hit = safe & (t > 1e-6) & (u >= 0.0) & (u <= 1.0)
    t = np.where(hit, t, np.inf)
    nearest = t.min(axis=1)
    np.minimum(nearest, radius, out=nearest)
    return nearest, dx, dy


class LightFan:
    """The result of one sweep: ray angles, hit distances, and hit points.

    Keeping the angles and distances (not just the polygon outline) is what
    lets the renderer draw the *shadows* as wedges instead of approximating
    the light with concentric polygons, which visibly bands.
    """

    __slots__ = ('angles', 'dists', 'points', 'ox', 'oy', 'radius')

    def __init__(self, angles, dists, points, ox, oy, radius):
        self.angles = angles
        self.dists = dists
        self.points = points
        self.ox = ox
        self.oy = oy
        self.radius = radius

    def _shadow_runs(self, tolerance, max_run):
        """Index ranges of consecutive blocked rays. Shared by both emitters."""
        dists = self.dists
        limit = self.radius * tolerance
        n = len(self.points)
        if n == 0:
            return [], 0

        blocked = [d < limit for d in dists]
        if not any(blocked):
            return [], n
        if all(blocked):
            runs = []
            k = 0
            step = max_run or n
            while k < n:
                end = min(n - 1, k + step - 1)
                runs.append((k, end))
                k = end + 1
        else:
            # Rotate so index 0 starts unblocked; then runs never wrap.
            start = next(i for i in range(n) if not blocked[i])
            runs = []
            i = 0
            while i < n:
                idx = (start + i) % n
                if not blocked[idx]:
                    i += 1
                    continue
                j = i
                while j + 1 < n and blocked[(start + j + 1) % n]:
                    j += 1
                # Very long runs are split: one enormous concave polygon is
                # cheaper in call count but markedly more expensive to
                # rasterise than a few moderate ones.
                length = j - i + 1
                if max_run and length > max_run:
                    chunks = (length + max_run - 1) // max_run
                    size = (length + chunks - 1) // chunks
                    k = i
                    while k <= j:
                        end = min(j, k + size - 1)
                        runs.append(((start + k) % n, (start + end) % n))
                        k = end + 1
                else:
                    runs.append(((start + i) % n, (start + j) % n))
                i = j + 1

        return runs, n

    def _band_indices(self, a, b, n):
        """The rays in one run, plus the unblocked neighbour at each end.

        The neighbours are what let the shadow close flush against the lit
        region instead of leaving a hairline of light along its edge.
        """
        idx = [(a - 1) % n]
        i = a
        while True:
            idx.append(i)
            if i == b:
                break
            i = (i + 1) % n
        idx.append((b + 1) % n)
        return idx

    def shadow_bands(self, tolerance=0.99, max_run=SHADOW_BAND_MAX_RUN):
        """Occluded regions as a few merged polygons instead of many quads.

        A run of consecutive blocked rays forms one continuous shadow, so it
        can be drawn as a single polygon: in along the visibility boundary,
        then back out along the light's rim at the same angular resolution.

        This matters a lot on the cmu-graphics renderer. Every shape costs
        roughly 35 us end to end once its rasterise-and-blit is counted, so a
        pillar-heavy chamber emitting 120 separate quads spent ~4 ms per frame
        on shadows alone. Merged, the same shadows are a handful of polygons.

        The merge has a cost of its own - see `shadow_ribbons`.
        """
        pts = self.points
        angles = self.angles
        radius = self.radius
        ox, oy = self.ox, self.oy
        runs, n = self._shadow_runs(tolerance, max_run)

        out = []
        for a, b in runs:
            idx = self._band_indices(a, b, n)
            flat = []
            for i in idx:
                flat.append(pts[i][0])
                flat.append(pts[i][1])
            for i in reversed(idx):
                ang = angles[i]
                flat.append(ox + math.cos(ang) * radius)
                flat.append(oy + math.sin(ang) * radius)
            if len(flat) >= 6:
                out.append(flat)
        return out

    def shadow_ribbons(self, tolerance=0.99, max_run=SHADOW_BAND_MAX_RUN):
        """The same shadows, as (inner, outer) chains rather than one outline.

        A shadow band is a ribbon: in along the visibility boundary, back out
        along the light's rim. Handed over as a single closed polygon that
        outline can cross itself around a concave corner - measured, about one
        band in two hundred does, and a further two per cent enclose the wrong
        area by up to 124%. A triangulator that assumes a simple polygon then
        fills the wrong region, which is what put black wedges across lit floor
        at the inside corners of a room.

        As two chains there is nothing to infer: the shadow is the strip
        between them, and every quad of it is its own small convex polygon.
        Only worth it where quads are cheap, which is the GPU.
        """
        pts = self.points
        angles = self.angles
        radius = self.radius
        ox, oy = self.ox, self.oy
        runs, n = self._shadow_runs(tolerance, max_run)

        out = []
        for a, b in runs:
            idx = self._band_indices(a, b, n)
            if len(idx) < 2:
                continue
            inner = [pts[i] for i in idx]
            outer = [(ox + math.cos(angles[i]) * radius,
                      oy + math.sin(angles[i]) * radius) for i in idx]
            out.append((inner, outer))
        return out

    def shadow_wedges(self, tolerance=0.99, min_span=MIN_WEDGE_SPAN):
        """Quads covering everything inside the light radius that is occluded.

        Drawn over the smooth glow, these carve real shadows out of it. Only
        the rays that actually hit something contribute, so an open chamber
        costs almost nothing.

        Wedges narrower than `min_span` are dropped. Each wall corner is
        bracketed by a pair of rays a hair either side of it, and the slivers
        between those rays are a fraction of a pixel wide - in a pillar-heavy
        chamber they were two thirds of every quad drawn, for no visible
        difference.
        """
        angles = self.angles
        dists = self.dists
        pts = self.points
        radius = self.radius
        ox, oy = self.ox, self.oy
        limit = radius * tolerance
        n = len(pts)
        out = []
        for i in range(n):
            j = (i + 1) % n
            if dists[i] >= limit and dists[j] >= limit:
                continue
            ai = angles[i]
            aj = angles[j]
            if j == 0:
                aj += math.tau
            span = aj - ai
            if span < min_span:
                continue
            if span > 0.9:
                # A gap this wide means the sweep wrapped oddly; skip it
                # rather than smear a wedge across the whole chamber.
                continue
            out.append((
                pts[i][0], pts[i][1],
                pts[j][0], pts[j][1],
                ox + math.cos(aj) * radius, oy + math.sin(aj) * radius,
                ox + math.cos(ai) * radius, oy + math.sin(ai) * radius,
            ))
        return out


def visibility_fan(level, ox, oy, radius):
    """Cast the full sweep and return it as a `LightFan`."""
    segs = cull(level, ox, oy, radius)

    angles = [_FILLER_ANGLES]
    if segs is not None and level.corners is not None and len(level.corners):
        cx = level.corners[:, 0] - ox
        cy = level.corners[:, 1] - oy
        d2 = cx * cx + cy * cy
        within = d2 <= (radius * 1.35) ** 2
        near_x = cx[within]
        near_y = cy[within]
        if near_x.size > LIGHT_MAX_CORNERS:
            # Keep the closest corners: those are the occluders whose shadows
            # dominate the frame, and dropping them at random looks broken.
            keep = np.argpartition(d2[within], LIGHT_MAX_CORNERS)[:LIGHT_MAX_CORNERS]
            near_x = near_x[keep]
            near_y = near_y[keep]
        corner_angles = np.arctan2(near_y, near_x)
        if corner_angles.size:
            angles.append(corner_angles - LIGHT_EPSILON)
            angles.append(corner_angles)
            angles.append(corner_angles + LIGHT_EPSILON)

    all_angles = np.concatenate(angles) % math.tau
    all_angles.sort()

    dist, dx, dy = _ray_distances(all_angles, ox, oy, segs, radius)
    xs = ox + dx * dist
    ys = oy + dy * dist
    points = list(zip(xs.tolist(), ys.tolist()))
    return LightFan(all_angles.tolist(), dist.tolist(), points, ox, oy, radius)


def visibility_polygon(level, ox, oy, radius):
    """The lit region around (ox, oy) as an angle-sorted list of points."""
    segs = cull(level, ox, oy, radius)

    angles = [_FILLER_ANGLES]
    if segs is not None and level.corners is not None and len(level.corners):
        cx = level.corners[:, 0] - ox
        cy = level.corners[:, 1] - oy
        d2 = cx * cx + cy * cy
        within = d2 <= (radius * 1.35) ** 2
        near_x = cx[within]
        near_y = cy[within]
        if near_x.size > LIGHT_MAX_CORNERS:
            # Keep the closest corners: those are the occluders whose shadows
            # dominate the frame, and dropping them at random looks broken.
            keep = np.argpartition(d2[within], LIGHT_MAX_CORNERS)[:LIGHT_MAX_CORNERS]
            near_x = near_x[keep]
            near_y = near_y[keep]
        corner_angles = np.arctan2(near_y, near_x)
        if corner_angles.size:
            angles.append(corner_angles - LIGHT_EPSILON)
            angles.append(corner_angles)
            angles.append(corner_angles + LIGHT_EPSILON)

    all_angles = np.concatenate(angles)
    order = np.argsort(all_angles)
    all_angles = all_angles[order]

    dist, dx, dy = _ray_distances(all_angles, ox, oy, segs, radius)
    xs = ox + dx * dist
    ys = oy + dy * dist
    return list(zip(xs.tolist(), ys.tolist()))


def visible_points(level, ox, oy, radius, points):
    """Batched line-of-sight test from the light to many points at once.

    Returns a list of booleans, one per point: True when the point is inside
    the light radius and no wall segment lies between it and the light.
    """
    n = len(points)
    if n == 0:
        return []

    px = np.empty(n)
    py = np.empty(n)
    for i, (x, y) in enumerate(points):
        px[i] = x
        py[i] = y

    rx = px - ox
    ry = py - oy
    target = np.hypot(rx, ry)
    inside = target <= radius
    if not inside.any():
        return [False] * n

    segs = cull(level, ox, oy, radius)
    if segs is None:
        return inside.tolist()

    ax, ay, bx, by = segs
    ex = bx - ax
    ey = by - ay
    aox = ax - ox
    aoy = ay - oy

    denom = rx[:, None] * ey[None, :] - ry[:, None] * ex[None, :]
    safe = np.abs(denom) > _EPS
    denom = np.where(safe, denom, 1.0)

    # With D as the full offset vector, a hit blocks the point when t < 1.
    t = (aox * ey - aoy * ex)[None, :] / denom
    u = (aox[None, :] * ry[:, None] - aoy[None, :] * rx[:, None]) / denom

    blocked = (safe & (t > 1e-4) & (t < 0.999) & (u >= 0.0) & (u <= 1.0)).any(axis=1)
    return (inside & ~blocked).tolist()


def shrink_polygon(points, cx, cy, factor):
    """Scale a polygon toward its light origin - used to layer light falloff."""
    out = []
    for x, y in points:
        out.append((cx + (x - cx) * factor, cy + (y - cy) * factor))
    return out


def polygon_flat(points, offset_x=0.0, offset_y=0.0):
    """Flatten to the coordinate list `drawPolygon` expects, camera-relative."""
    flat = []
    for x, y in points:
        flat.append(x - offset_x)
        flat.append(y - offset_y)
    return flat


# A piece of lit wall edge is drawn as one flat-coloured quad, so this is the
# length of one step of the gradient along the wall. Fixing the *count* per
# edge instead - which is what this used to do - makes a short wall smooth and
# a long one banded, because the piece size then grows with the wall.
WALL_PIECE_LENGTH = 2.6
WALL_PIECE_OVERLAP = 0.6


def lit_wall_segments(level, ox, oy, radius,
                      piece_length=WALL_PIECE_LENGTH, height=0.0):
    """Lit wall edges cut into equal pieces, each with its own brightness.

    Two things have to be true at once. A long edge running away from the
    light has to darken along its length, so it cannot be lit as one flat
    strip. And every wall has to be cut the *same* way, so a chamber's
    hundred-tile border does not end up with coarser steps than a pillar
    beside it - which is what a cap on the piece count per edge does, and it
    is exactly as visible as it sounds.

    So each edge is first clipped to the part of it the light can actually
    reach, and only that part is cut up, at a fixed length. The work is then
    bounded by the lantern's radius rather than by how long the wall is, and
    a wall of any length is cut into pieces of the same size as every other.
    """
    if level.seg_ax is None or len(level.seg_ax) == 0:
        return []

    ax, ay, bx, by = level.seg_ax, level.seg_ay, level.seg_bx, level.seg_by
    ex = bx - ax
    ey = by - ay

    # Where each edge crosses the circle of the light's reach, as the
    # parameter range [t0, t1] along it.
    fx = ax - ox
    fy = ay - oy
    aa = ex * ex + ey * ey
    bb = 2.0 * (fx * ex + fy * ey)
    cc = fx * fx + fy * fy - radius * radius
    with np.errstate(invalid='ignore', divide='ignore'):
        disc = bb * bb - 4.0 * aa * cc
        root = np.sqrt(np.maximum(disc, 0.0))
        t0 = np.clip((-bb - root) / np.where(aa == 0, 1.0, 2.0 * aa), 0.0, 1.0)
        t1 = np.clip((-bb + root) / np.where(aa == 0, 1.0, 2.0 * aa), 0.0, 1.0)
    reachable = (disc > 0.0) & (aa > 0.0) & (t1 > t0)
    if not reachable.any():
        return []
    idx = np.nonzero(reachable)[0]

    # Rectangle edges wind clockwise, so the outward normal is (ey, -ex).
    nx = ey[idx].copy()
    ny = -ex[idx].copy()
    nlen = np.hypot(nx, ny)
    nlen[nlen == 0] = 1.0
    nx /= nlen
    ny /= nlen

    out = []
    for k, e in enumerate(idx):
        span = (t1[e] - t0[e]) * nlen[k]
        if span < 0.5:
            continue
        pieces = int(max(1, math.ceil(span / piece_length)))
        edge = t0[e] + (t1[e] - t0[e]) * (
            np.arange(pieces + 1, dtype=np.float64) / pieces)
        mid = (edge[:-1] + edge[1:]) * 0.5

        px_ = ax[e] + ex[e] * mid
        py_ = ay[e] + ey[e] * mid
        dx = px_ - ox
        dy = py_ - oy
        d = np.hypot(dx, dy)
        d[d == 0] = 1.0
        nxe, nye = float(nx[k]), float(ny[k])
        facing = -(dx / d) * nxe - (dy / d) * nye
        if height > 0.0:
            # A wall is a vertical surface, so how much of a light it catches
            # depends on how high that light is held as well as where it
            # stands. The direction to the flame is (-dx, -dy, height); against
            # a normal lying flat in the plane that works out as the flat
            # facing scaled by the cosine of the light's elevation. A lantern
            # carried low rakes a wall; the same lamp held high slides off it.
            # At height zero this is exactly the flat term it replaces.
            facing = facing * d / np.hypot(d, height)
        falloff = np.clip(1.0 - d / radius, 0.0, 1.0) ** 1.5
        strength = np.clip(facing, 0.0, 1.0) ** 0.7 * falloff

        lit = np.nonzero(strength > 0.015)[0]
        if lit.size == 0:
            continue
        x0 = ax[e] + ex[e] * edge[:-1]
        y0 = ay[e] + ey[e] * edge[:-1]
        x1 = ax[e] + ex[e] * edge[1:]
        y1 = ay[e] + ey[e] * edge[1:]
        # The outward normal comes back with each piece: the caller needs it
        # to push the light *into* the stone as well as along its rim.
        for i in lit:
            out.append((float(x0[i]), float(y0[i]), float(x1[i]), float(y1[i]),
                        float(strength[i]), nxe, nye))
    return out


def lit_wall_edges(level, ox, oy, radius):
    """Wall edges facing the light, with a 0..1 brightness for each.

    These get stroked as thin bright lines so masonry catches the lantern -
    it is what stops the chamber reading as flat cut-outs.
    """
    if level.seg_ax is None or len(level.seg_ax) == 0:
        return []

    ax, ay, bx, by = level.seg_ax, level.seg_ay, level.seg_bx, level.seg_by
    mx = (ax + bx) * 0.5
    my = (ay + by) * 0.5
    dx = mx - ox
    dy = my - oy
    d2 = dx * dx + dy * dy
    near = d2 <= radius * radius
    if not near.any():
        return []

    ex = bx - ax
    ey = by - ay
    # Rectangle edges wind clockwise, so the outward normal is (ey, -ex).
    nx = ey
    ny = -ex
    nlen = np.hypot(nx, ny)
    nlen[nlen == 0] = 1.0
    nx /= nlen
    ny /= nlen

    d = np.sqrt(d2)
    d[d == 0] = 1.0
    facing = -(dx / d) * nx - (dy / d) * ny
    lit = near & (facing > 0.04)
    if not lit.any():
        return []

    falloff = np.clip(1.0 - d / radius, 0.0, 1.0) ** 1.5
    strength = np.clip(facing, 0.0, 1.0) ** 0.7 * falloff

    idx = np.nonzero(lit)[0]
    out = []
    for i in idx:
        s = float(strength[i])
        if s < 0.03:
            continue
        out.append((float(ax[i]), float(ay[i]), float(bx[i]), float(by[i]), s))
    return out
