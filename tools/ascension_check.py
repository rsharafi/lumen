"""Prove every rung of the deeper dark actually changes the run.

    .venv/bin/python tools/ascension_check.py

A difficulty ladder is the easiest system in a game to ship broken, because
every rung looks identical from the outside: the run is a bit harder, or it
is not, and nobody can tell which. So each tier here is driven through the
real code and the difference it claims to make is measured.
"""

import os
import sys

os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
os.environ.setdefault('CI', '1')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lumen import ascension                           # noqa: E402
from lumen import floorplan as fp                     # noqa: E402
from lumen import rng as rng_mod                      # noqa: E402
from lumen import shop                                # noqa: E402
from lumen import upgrades                            # noqa: E402
from lumen.config import FLOOR_ENTRY_FUEL, LANTERN_DRAIN  # noqa: E402
from lumen.world import World                         # noqa: E402

RESULTS = []


def check(name, ok, detail):
    RESULTS.append((name, bool(ok), detail))


def world_at(tier, depth=6, seed=17):
    rng = rng_mod.Rng(seed)
    stats = upgrades.Stats()
    stats.rules = ascension.rules_for(tier)
    world = World(stats, rng, rng_mod.Rng(seed + 1), 1280, 720)
    world.enter_floor(depth)
    return world


def case_the_ladder_is_named():
    """Every tier has a rule with a name and a sentence."""
    bad = [t.index for t in ascension.TIERS
           if not t.name or not t.blurb or t.blurb.endswith('.') is False]
    check('every tier is a named rule', not bad,
          f'{ascension.MAX_TIER} rungs, all named'
          if not bad else f'unnamed: {bad}')


def case_rules_accumulate():
    check('rules accumulate',
          ascension.rules_for(0) == set()
          and ascension.rules_for(3) == {1, 2, 3}
          and len(ascension.rules_for(ascension.MAX_TIER))
          == ascension.MAX_TIER,
          f'tier 3 carries {sorted(ascension.rules_for(3))}, '
          f'tier {ascension.MAX_TIER} carries '
          f'{len(ascension.rules_for(ascension.MAX_TIER))}')


def case_longer_dark_burns_faster():
    off = ascension.lantern_drain(set(), LANTERN_DRAIN)
    on = ascension.lantern_drain({1}, LANTERN_DRAIN)
    check('1 THE LONGER DARK burns the lantern faster', on > off * 1.2,
          f'{off:.2f} -> {on:.2f} fuel a second')


def case_fewer_fires():
    """Measured on the real generator, not on the helper."""
    counts = {}
    for tier in (0, 2):
        world = world_at(tier, depth=3, seed=23)
        total = rooms = 0
        for room in list(world.plan.rooms.values())[:8]:
            world.enter_room(room)
            total += len(world.level.braziers)
            rooms += 1
        counts[tier] = total / max(rooms, 1)
    check('2 FEWER FIRES leaves fewer braziers',
          counts[2] < counts[0],
          f'{counts[0]:.2f} -> {counts[2]:.2f} braziers a room')


def case_vault_remembers():
    check('3 THE VAULT REMEMBERS reseals',
          not ascension.reseals({1, 2}) and ascension.reseals({1, 2, 3}),
          'off below tier 3, on at and above it')


def case_hungrier():
    base_first = 0.0                       # floor one has no elites normally
    on_first = ascension.elite_chance({4}, base_first, 1)
    off_deep = ascension.elite_chance(set(), 0.18, 14)
    on_deep = ascension.elite_chance({4}, 0.18, 14)
    check('4 HUNGRIER puts elites on floor one', on_first > 0.0
          and on_deep > off_deep,
          f'floor 1: {base_first:.0%} -> {on_first:.0%}; '
          f'floor 14: {off_deep:.0%} -> {on_deep:.0%}')


def case_the_toll():
    """Through the real shelf, so a price that skips `_scaled` is caught."""
    rng = rng_mod.Rng(5)
    plain = upgrades.Stats()
    dear = upgrades.Stats()
    dear.rules = ascension.rules_for(5)
    a = sum(s.price for s in shop.stock(9, plain, rng_mod.Rng(5)))
    b = sum(s.price for s in shop.stock(9, dear, rng_mod.Rng(5)))
    check('5 THE TOLL raises the shelf', b > a * 1.35,
          f'{a} -> {b} embers for the same shelf')


def case_nothing_wasted():
    off = ascension.floor_entry_fuel(set(), FLOOR_ENTRY_FUEL)
    on = ascension.floor_entry_fuel({6}, FLOOR_ENTRY_FUEL)
    check('6 NOTHING WASTED stops the refill', off > 0 and on == 0.0,
          f'{off:.0f} -> {on:.0f} fuel on arriving')


def case_deeper_still():
    """The rule has to reach the roster, not just a number."""
    from lumen import enemies as enemy_mod
    rng = rng_mod.Rng(3)
    plain = enemy_mod.wave_for_depth(4, rng, weight=1.0)
    deep = enemy_mod.wave_for_depth(ascension.spawn_depth({7}, 4), rng,
                                    weight=1.0)
    legal_plain = {k for k in plain}
    legal_deep = {k for k in deep}
    check('7 DEEPER STILL spawns from further down',
          ascension.spawn_depth({7}, 4) == 9
          and len(deep) >= len(plain),
          f'floor 4 spawns as floor {ascension.spawn_depth({7}, 4)}: '
          f'{len(plain)} -> {len(deep)} things, '
          f'{len(legal_plain)} -> {len(legal_deep)} species')


def case_last_dark():
    from lumen.config import LANTERN_RADIUS_MIN
    off = ascension.lantern_floor(set(), LANTERN_RADIUS_MIN)
    on = ascension.lantern_floor({8}, LANTERN_RADIUS_MIN)
    check('8 THE LAST DARK drops the lantern floor', on < off * 0.6,
          f'{off:.0f} -> {on:.0f} px minimum reach')


def case_the_ladder_opens_one_rung_at_a_time():
    data = {}
    opened = []
    for tier in range(0, 4):
        before = ascension.unlocked(data)
        ascension.record_win(data, tier)
        opened.append((tier, before, ascension.unlocked(data)))
    ok = all(after == t + 1 for t, _b, after in opened)
    # And beating a tier you have already beaten opens nothing.
    again = ascension.record_win(data, 1)
    check('the ladder opens one rung at a time', ok and not again,
          '; '.join(f'won {t} -> {a} open' for t, _b, a in opened)
          + '; re-winning tier 1 opens nothing')


def case_a_fresh_save_starts_flat():
    check('a fresh save starts at the vault as built',
          ascension.unlocked({}) == 0,
          'nothing unlocked until the vault is beaten once')


def main():
    for case in (case_the_ladder_is_named, case_rules_accumulate,
                 case_longer_dark_burns_faster, case_fewer_fires,
                 case_vault_remembers, case_hungrier, case_the_toll,
                 case_nothing_wasted, case_deeper_still, case_last_dark,
                 case_the_ladder_opens_one_rung_at_a_time,
                 case_a_fresh_save_starts_flat):
        try:
            case()
        except Exception as exc:                      # noqa: BLE001
            import traceback
            traceback.print_exc(file=sys.stderr)
            check(case.__name__, False, f'{type(exc).__name__}: {exc}')

    width = max(len(n) for n, _o, _d in RESULTS)
    bad = sum(1 for _n, ok, _d in RESULTS if not ok)
    print()
    for name, ok, detail in RESULTS:
        print(f'  {" ok " if ok else "FAIL"}  {name:<{width}}  {detail}')
    print()
    print(f'{len(RESULTS) - bad} of {len(RESULTS)} checks passed')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
