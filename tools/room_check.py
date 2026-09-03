"""Generate rooms in every door configuration and prove you can cross them.

    .venv/bin/python tools/room_check.py

A door a chamber has walled off is a floor that cannot be finished, and it is
invisible until a player happens to draw that layout with that door on that
floor. There are six layouts, four sizes and fifteen useful door combinations,
so the space is small enough to simply cover it - thousands of times over.
"""

import os
import sys
from collections import Counter, deque

os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
os.environ.setdefault('CI', '1')
os.environ.setdefault('LUMEN_HEADLESS', '1')
os.environ.setdefault('LUMEN_RENDERER', 'cpu')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


from lumen import level as L                 # noqa: E402
from lumen import rng as rng_mod             # noqa: E402
from lumen.config import TILE                # noqa: E402

SIDES = ('n', 's', 'e', 'w')


def combos():
    out = []
    for mask in range(1, 16):
        out.append(tuple(s for i, s in enumerate(SIDES) if mask & (1 << i)))
    return out


def reachable(level, start):
    seen = set()
    q = deque([start])
    while q:
        c, r = q.popleft()
        if (c, r) in seen:
            continue
        if not (0 <= c < level.cols and 0 <= r < level.rows):
            continue
        if level.grid[r][c] == L.WALL:
            continue
        seen.add((c, r))
        q.extend(((c + 1, r), (c - 1, r), (c, r + 1), (c, r - 1)))
    return seen


def main():
    faults = []
    checked = 0
    by_layout = Counter()

    for trial in range(120):
        rng = rng_mod.Rng(trial * 613 + 7)
        for size in ('small', 'medium', 'large'):
            for doors in combos():
                level = L.generate(3 + trial % 17, rng, doors=doors,
                                   size=size, seed=trial, bake=False)
                checked += 1
                by_layout[level.archetype] += 1
                tag = f'{level.archetype}/{size}/{"".join(doors)}'

                # Every opening is actually open.
                for side in doors:
                    if side not in level.doors:
                        faults.append(f'{tag}: door {side} missing')
                        continue
                    c0, c1, r0, r1 = level.doors[side]
                    for r in range(r0, r1 + 1):
                        for c in range(c0, c1 + 1):
                            if level.grid[r][c] == L.WALL:
                                faults.append(f'{tag}: door {side} is walled')

                # Every opening reaches every other, and the start.
                first = doors[0]
                c0, c1, r0, r1 = level.doors[first]
                seen = reachable(level, (c0, r0))
                for side in doors[1:]:
                    d0, d1, e0, e1 = level.doors[side]
                    if (d0, e0) not in seen:
                        faults.append(f'{tag}: {first} cannot reach {side}')
                sc = int(level.player_start[0] // TILE)
                sr = int(level.player_start[1] // TILE)
                if (sc, sr) not in seen:
                    faults.append(f'{tag}: start unreachable from {first}')

                # Arriving through a door must not land you inside stone.
                for side in doors:
                    ex, ey = level.door_entry(side)
                    if not level.is_open_at(ex, ey, 13.0):
                        faults.append(f'{tag}: entry through {side} is solid')

                # And there has to be somewhere to put things.
                if not level.spawn_points:
                    faults.append(f'{tag}: no spawn points')

    print(f'{checked} rooms checked '
          f'({", ".join(f"{k} {v}" for k, v in sorted(by_layout.items()))})')
    if faults:
        print(f'\n!! {len(faults)} FAULTS')
        seen = Counter()
        for f in faults:
            seen[f.split(':')[0].split('/')[0] + ': ' + f.split(': ')[1]] += 1
        for what, n in seen.most_common(20):
            print(f'  {n:>5}x  {what}')
        return 1
    print('every door reachable, every entry clear, every room populatable')
    return 0


if __name__ == '__main__':
    sys.exit(main())
