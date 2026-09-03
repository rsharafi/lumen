"""What a run actually earns, floor by floor, so prices can be set from it.

    .venv/bin/python tools/economy_report.py

Every price in the game is a claim about how much an ember is worth, and an
ember is worth whatever a floor pays. Guessing that number is how a shop ends
up either free or ornamental, so this counts it: it walks whole floors with
the real spawn tables at the real depths and reports the income, split by
where it came from.
"""

import argparse
import os
import statistics
import sys

os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
os.environ.setdefault('CI', '1')
os.environ.setdefault('LUMEN_HEADLESS', '1')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


from lumen import enemies as enemy_mod           # noqa: E402
from lumen import fixtures as fixture_mod        # noqa: E402
from lumen import floorplan as fp                # noqa: E402
from lumen import rng as rng_mod                 # noqa: E402
from lumen.config import BOSS_FLOORS, FLOORS_PER_RUN   # noqa: E402
from lumen.world import World                    # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument('--runs', type=int, default=220)
ARGS = parser.parse_args()


def _kill_worth(depth, rng, weight, elite_rate, elite_forced=0):
    """Embers from one room's fight, without simulating the fight.

    Counting the spawn table is exact and costs nothing; simulating it would
    add the bot's competence as a variable, which is not what is being asked.
    """
    total = 0
    keys = enemy_mod.wave_for_depth(depth, rng, weight=weight)
    for i, key in enumerate(keys):
        cls = enemy_mod.SPECIES[key]
        value = cls.ember_value
        elite = i < elite_forced or rng.chance(elite_rate)
        if elite:
            # `make_elite` multiplies, and EMBER-FED multiplies again.
            value = value * 3 + 2
            if rng.chance(0.25):
                value = value * 2 + 6
        total += value
    return total


def main():
    per_depth = {}
    for run in range(ARGS.runs):
        rng = rng_mod.Rng(run * 3301 + 17)
        for depth in range(1, FLOORS_PER_RUN + 1):
            if depth in BOSS_FLOORS:
                continue
            plan = fp.generate(depth, rng)
            elite_rate = enemy_mod.elite_chance(depth)
            kills = 0
            caches = 0
            for room in plan.rooms.values():
                if room.kind == fp.COMBAT:
                    kills += _kill_worth(depth, rng, 0.46, elite_rate)
                elif room.kind == fp.ELITE:
                    kills += _kill_worth(depth, rng, 0.52, elite_rate, 1)
                elif room.kind == fp.GAUNTLET:
                    kills += _kill_worth(depth, rng, 0.85,
                                         min(0.75, elite_rate * 2.6))
                elif room.kind == fp.CACHE:
                    caches += fixture_mod.cache_value(depth, rng)[0]
            slot = per_depth.setdefault(depth, {'kills': [], 'cache': []})
            slot['kills'].append(kills)
            slot['cache'].append(caches)

    print('embers a floor pays, if every room in it is taken')
    print()
    print('depth   fights   caches    total     act total')
    running = 0.0
    for depth in sorted(per_depth):
        s = per_depth[depth]
        k = statistics.mean(s['kills'])
        c = statistics.mean(s['cache'])
        running += k + c
        print(f'{depth:>5}   {k:6.1f}   {c:6.1f}   {k + c:6.1f}     {running:7.0f}')
    print()
    print(f'a whole run, every room taken: {running:.0f} embers')
    print(f'a floor, on average:           {running / len(per_depth):.0f}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
