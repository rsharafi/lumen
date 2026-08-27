"""Tile-level pathfinding: one breadth-first distance field to the player.

Steering straight at the player is what made enemies press into walls and
embers stall on the far side of a pillar. A BFS over the chamber's open tiles
gives every tile a distance-to-player, and anything that wants to reach the
player just walks downhill.

The field is rebuilt only when the player crosses into a new tile - a couple
of times a second - and a whole 28x18 chamber resolves in well under a
millisecond, so this is far cheaper than per-agent path search.
"""

from collections import deque

from .config import TILE

# 8-way, with the diagonals last so ties prefer straight moves.
_NEIGHBOURS = ((1, 0), (-1, 0), (0, 1), (0, -1),
               (1, 1), (1, -1), (-1, 1), (-1, -1))
_UNREACHED = 1 << 30


class FlowField:
    def __init__(self, level):
        self.level = level
        self.cols = level.cols
        self.rows = level.rows
        self.dist = [_UNREACHED] * (self.cols * self.rows)
        self.target = None
        self.rebuilds = 0

    # ------------------------------------------------------------- build --
    def rebuild(self, x, y):
        """Recompute distances from the tile containing world point (x, y)."""
        col = int(x // TILE)
        row = int(y // TILE)
        col = max(0, min(self.cols - 1, col))
        row = max(0, min(self.rows - 1, row))
        if self.level.is_wall_tile(col, row):
            col, row = self._nearest_open(col, row)
            if col is None:
                return
        self.target = (col, row)
        self.rebuilds += 1

        cols = self.cols
        rows = self.rows
        grid = self.level.grid
        dist = self.dist
        for i in range(len(dist)):
            dist[i] = _UNREACHED

        start = row * cols + col
        dist[start] = 0
        queue = deque(((col, row),))
        while queue:
            c, r = queue.popleft()
            here = dist[r * cols + c] + 1
            for dc, dr in _NEIGHBOURS:
                nc = c + dc
                nr = r + dr
                if nc < 0 or nr < 0 or nc >= cols or nr >= rows:
                    continue
                if grid[nr][nc]:
                    continue
                if dc and dr:
                    # No cutting corners through a diagonal gap.
                    if grid[r][nc] or grid[nr][c]:
                        continue
                idx = nr * cols + nc
                if dist[idx] <= here:
                    continue
                dist[idx] = here
                queue.append((nc, nr))

    def _nearest_open(self, col, row):
        for radius in range(1, 5):
            for dr in range(-radius, radius + 1):
                for dc in range(-radius, radius + 1):
                    c = col + dc
                    r = row + dr
                    if 0 <= c < self.cols and 0 <= r < self.rows:
                        if not self.level.is_wall_tile(c, r):
                            return c, r
        return None, None

    def maybe_rebuild(self, x, y):
        """Rebuild only when the target has moved to a different tile."""
        col = int(x // TILE)
        row = int(y // TILE)
        if self.target != (col, row):
            self.rebuild(x, y)

    # ------------------------------------------------------------ sample --
    def distance_at(self, x, y):
        col = int(x // TILE)
        row = int(y // TILE)
        if col < 0 or row < 0 or col >= self.cols or row >= self.rows:
            return None
        d = self.dist[row * self.cols + col]
        return None if d >= _UNREACHED else d

    def direction_at(self, x, y):
        """Unit vector downhill toward the target, or None if unreachable.

        The result aims at the centre of the chosen neighbour rather than
        along the raw tile offset, which keeps agents off the wall faces as
        they round a corner.
        """
        cols = self.cols
        rows = self.rows
        col = int(x // TILE)
        row = int(y // TILE)
        if col < 0 or row < 0 or col >= cols or row >= rows:
            return None

        dist = self.dist
        here = dist[row * cols + col]
        if here >= _UNREACHED:
            return None
        if here == 0:
            return None

        best = here
        best_c = best_r = None
        grid = self.level.grid
        for dc, dr in _NEIGHBOURS:
            nc = col + dc
            nr = row + dr
            if nc < 0 or nr < 0 or nc >= cols or nr >= rows:
                continue
            if grid[nr][nc]:
                continue
            if dc and dr and (grid[row][nc] or grid[nr][col]):
                continue
            d = dist[nr * cols + nc]
            if d < best:
                best = d
                best_c = nc
                best_r = nr
        if best_c is None:
            return None

        tx = (best_c + 0.5) * TILE
        ty = (best_r + 0.5) * TILE
        dx = tx - x
        dy = ty - y
        length = (dx * dx + dy * dy) ** 0.5
        if length < 1e-6:
            return None
        return dx / length, dy / length
