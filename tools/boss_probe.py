"""Fight each boss with the same player, and report what it does to them.

    .venv/bin/python tools/boss_probe.py

This exists because the vault already shipped a final boss that was easier
than the one halfway up, and nobody could see it. The Snuffer had two and a
half times the Choir's health, so thresholds measured as *fractions* of it
arrived far later in seconds; it opened with two attacks where the Choir
opened with three; and its signature move set up an advantage it never took.
All three were invisible by inspection and obvious in a table.

So: a fixed kiting policy, a build drafted one offering a floor from the real
pool at the real depth, and the player made immortal so every fight runs to
the end and the numbers are comparable. What is measured is what the boss
does to the player - damage a second, and how often it connects - not how
long the player takes to kill it, which is mostly a statement about the
build.
"""

import argparse
import math
import os
import statistics
import sys

os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
os.environ.setdefault('CI', '1')
os.environ.setdefault('LUMEN_HEADLESS', '1')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


from lumen import boss as boss_mod                   # noqa: E402
from lumen import floorplan as fp                    # noqa: E402
from lumen import rng as rng_mod                     # noqa: E402
from lumen import upgrades                           # noqa: E402
from lumen.config import BOSS_FLOORS                 # noqa: E402
from lumen.world import World                        # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument('--seeds', type=int, default=8)
parser.add_argument('--cap', type=float, default=300.0,
                    help='seconds before a fight is called unfinished')
ARGS = parser.parse_args()

DT = 1.0 / 60.0
KEEP_AWAY = 320.0


def _build(stats, depth, rng):
    """One offering a floor, from the real pool at the real depth."""
    for d in range(1, depth + 1):
        picks = upgrades.offer(stats, rng, count=1, depth=d)
        if picks:
            upgrades.grant(stats, picks[0])


def _kite(world):
    """Back away from the boss, strafe, and keep it in front. Fixed policy.

    Deliberately mediocre and deliberately identical between bosses. It is a
    ruler, not a player.
    """
    player = world.player
    b = world.boss_ref
    if b is None:
        return set(), (player.x + 100.0, player.y)
    dx = player.x - b.x
    dy = player.y - b.y
    d = math.hypot(dx, dy) or 1.0
    # Away if too close, sideways otherwise.
    if d < KEEP_AWAY:
        wx, wy = dx / d, dy / d
    else:
        wx, wy = -dy / d, dx / d
    keys = set()
    if wx > 0.35:
        keys.add('d')
    elif wx < -0.35:
        keys.add('a')
    if wy > 0.35:
        keys.add('s')
    elif wy < -0.35:
        keys.add('w')
    return keys, (b.x, b.y)


def fight(depth, seed):
    rng = rng_mod.Rng(seed)
    fxr = rng_mod.Rng(seed + 7919)
    stats = upgrades.Stats()
    _build(stats, depth, rng)
    world = World(stats, rng, fxr, 1280, 720)
    world.enter_floor(depth)
    room = next((r for r in world.plan.rooms.values() if r.kind == fp.BOSS),
                None)
    if room is None:
        return None
    world.enter_room(room)
    if world.boss_ref is None:
        return None

    player = world.player
    taken = 0.0
    hits = 0
    phase_at = {}
    ticks = 0
    max_ticks = int(ARGS.cap / DT)
    last_hp = player.hp
    bleeding = False

    while ticks < max_ticks and world.boss_ref.alive:
        keys, aim = _kite(world)
        world.update(DT, keys, aim, True)
        ticks += 1
        # Immortal, but every blow is still counted. Topping the health up
        # after the fact keeps invulnerability frames behaving normally -
        # setting `alive` and walking away would let the boss land every
        # tick of a sweep instead of one.
        if player.hp < last_hp:
            taken += last_hp - player.hp
            # One hit, not sixty. Standing rot applies its damage every
            # frame and bypasses invulnerability on purpose, so counting
            # every frame that lost health reported the Keeper landing eight
            # blows a second - a number about the tick rate rather than
            # about the fight.
            if not bleeding:
                hits += 1
            bleeding = True
            player.hp = stats.max_hp
        else:
            bleeding = False
        last_hp = player.hp
        if not player.alive:
            player.alive = True
            player.hp = stats.max_hp
        tier = world.boss_ref.tier
        phase_at.setdefault(tier, ticks * DT)

    seconds = ticks * DT
    return {
        'seconds': seconds,
        'finished': not world.boss_ref.alive,
        'dps': taken / max(seconds, 1e-6),
        'total': taken,
        'hits_s': hits / max(seconds, 1e-6),
        'phases': phase_at,
    }


def main():
    print(f'{"boss":<20} {"floor":>5} {"fight":>7} {"dmg/s":>7} '
          f'{"total":>8} {"hits/s":>7}   phases reached at')
    rows = []
    for depth in BOSS_FLOORS:
        kind = boss_mod.for_depth(depth)
        name = boss_mod.NAMES.get(kind, kind.__name__)
        runs = [fight(depth, 1000 + i * 31) for i in range(ARGS.seeds)]
        runs = [r for r in runs if r]
        if not runs:
            print(f'{name:<20} {depth:>5}   no boss room generated')
            continue
        dps = statistics.mean(r['dps'] for r in runs)
        sec = statistics.mean(r['seconds'] for r in runs)
        tot = statistics.mean(r['total'] for r in runs)
        hps = statistics.mean(r['hits_s'] for r in runs)
        err = (statistics.stdev(r['dps'] for r in runs) / len(runs) ** 0.5
               if len(runs) > 1 else 0.0)
        # When each phase first showed, averaged over the seeds that got there.
        marks = []
        for tier in (2, 3, 4):
            got = [r['phases'][tier] for r in runs if tier in r['phases']]
            marks.append(f'{statistics.mean(got):.0f}s' if got else '-')
        done = sum(1 for r in runs if r['finished'])
        print(f'{name:<20} {depth:>5} {sec:>6.0f}s {dps:>7.2f} '
              f'{tot:>8.0f} {hps:>7.3f}   {"  ".join(marks)}'
              f'   ({done}/{len(runs)} killed)')
        rows.append((name, depth, dps, err))

    print()
    print('standard error on dmg/s: '
          + ', '.join(f'{n} +/-{e:.1f}' for n, _d, _p, e in rows))
    # The one claim this tool exists to check.
    if len(rows) >= 2:
        deepest = max(rows, key=lambda r: r[1])
        hardest = max(rows, key=lambda r: r[2])
        if deepest[0] == hardest[0]:
            print(f'the last boss is the hardest: {deepest[0]} '
                  f'at {deepest[2]:.1f} dmg/s')
        else:
            print(f'** {hardest[0]} (floor {hardest[1]}) hits harder than '
                  f'{deepest[0]} (floor {deepest[1]}), which is the wrong '
                  f'way round **')
    return 0


if __name__ == '__main__':
    sys.exit(main())
