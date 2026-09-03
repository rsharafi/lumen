"""Generate a great many floors and say whether they are worth walking.

    .venv/bin/python tools/floor_report.py
    .venv/bin/python tools/floor_report.py --show 6 --depth 14

A floor generator is the one part of a roguelite that cannot be checked by
playing it: the run you played is one sample, and the floor that strands the
player is the one you did not see. So this makes ten thousand of them, asserts
the invariants on every single one, and reports the distributions - because a
generator that is *correct* can still be *boring*, and only the shape of the
numbers shows that.
"""

import argparse
import os
import statistics
import sys
import time
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lumen import floorplan as fp          # noqa: E402
from lumen import rng as rng_mod           # noqa: E402
from lumen.config import BOSS_FLOORS, FLOORS_PER_RUN   # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument('--runs', type=int, default=800,
                    help='how many whole runs of floors to generate')
parser.add_argument('--show', type=int, default=4,
                    help='how many maps to print')
parser.add_argument('--depth', type=int, default=0,
                    help='print maps from this depth (0 = a spread)')
ARGS = parser.parse_args()


def main():
    faults = []
    per_depth = {}
    kinds = Counter()
    presence = Counter()
    shortcut_floors = 0
    t0 = time.perf_counter()
    made = 0

    for run in range(ARGS.runs):
        rng = rng_mod.Rng(run * 7919 + 13)
        for depth in range(1, FLOORS_PER_RUN + 1):
            plan = fp.generate(depth, rng)
            made += 1
            bad = fp.validate(plan)
            if bad:
                faults.append((run, depth, bad))

            rooms = len(plan.rooms)
            path = sum(1 for r in plan.rooms.values() if r.on_path)
            ends = sum(1 for r in plan.rooms.values()
                       if r.degree == 1
                       and r.id not in (plan.entrance, plan.descent))
            doors = sum(r.degree for r in plan.rooms.values()) // 2
            # A tree has rooms-1 doors; anything more is a loop.
            loops = doors - (rooms - 1)
            if loops > 0:
                shortcut_floors += 1
            walk = plan.distances(plan.entrance).get(plan.descent, 0)

            slot = per_depth.setdefault(depth, {
                'rooms': [], 'path': [], 'ends': [], 'walk': [], 'loops': []})
            slot['rooms'].append(rooms)
            slot['path'].append(path)
            slot['ends'].append(ends)
            slot['walk'].append(walk)
            slot['loops'].append(loops)
            here = set()
            for room in plan.rooms.values():
                kinds[room.kind] += 1
                here.add(room.kind)
            if depth not in BOSS_FLOORS:
                for kind in here:
                    presence[kind] += 1

    ms = (time.perf_counter() - t0) * 1000.0
    print(f'{made} floors in {ms:.0f} ms  ({ms / made:.3f} ms each)')
    print()

    if faults:
        print(f'!! {len(faults)} INVALID FLOORS')
        for run, depth, bad in faults[:12]:
            print(f'   run {run} depth {depth}: {"; ".join(bad)}')
        print()
    else:
        print(f'all {made} floors valid')
        print()

    print('depth  rooms      spine      dead-ends  walk       loops')
    for depth in sorted(per_depth):
        s = per_depth[depth]
        tag = '  <- boss' if depth in BOSS_FLOORS else ''

        def col(key, width=11):
            vals = s[key]
            return f'{statistics.mean(vals):4.1f}±{statistics.pstdev(vals):3.1f}'.ljust(width)

        print(f'{depth:>5}  {col("rooms")}{col("path")}{col("ends")}'
              f'{col("walk")}{col("loops")}{tag}')
    print()

    total = sum(kinds.values())
    print('room kinds, over every floor generated')
    for kind, n in kinds.most_common():
        bar = '#' * int(round(n / total * 60))
        print(f'  {str(kind):<9} {n / total * 100:5.1f}%  {bar}')
    print()

    # Share of all rooms is the wrong number to design against. What a player
    # experiences is "did this floor have a Ferryman", so report that.
    print('how often a normal floor carries one at all')
    normal = made - ARGS.runs * len(BOSS_FLOORS)
    for kind in (fp.SHOP, fp.HEARTH, fp.SHRINE, fp.GAUNTLET, fp.CACHE,
                 fp.ELITE):
        n = presence[kind]
        bar = '#' * int(round(n / normal * 50))
        print(f'  {str(kind):<9} {n / normal * 100:5.1f}%  {bar}')
    print()
    print(f'floors with at least one shortcut: '
          f'{shortcut_floors / made * 100:.0f}%')

    if ARGS.show:
        print()
        depths = ([ARGS.depth] * ARGS.show if ARGS.depth
                  else [1, 4, 9, 14, 19, 20][:ARGS.show])
        for i, depth in enumerate(depths):
            rng = rng_mod.Rng(4242 + i * 101)
            plan = fp.generate(depth, rng)
            print(f'--- depth {depth} '
                  f'({len(plan.rooms)} rooms) ' + '-' * 30)
            print(fp.ascii_map(plan))
            print()
        print('  I entrance   . combat   E elite   $ cache   F ferryman')
        print('  S shrine     H hearth   G gauntlet   V descent   B boss')

    return 1 if faults else 0


if __name__ == '__main__':
    sys.exit(main())
