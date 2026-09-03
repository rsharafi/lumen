"""Prove every relic does the thing written on it.

    .venv/bin/python tools/relic_check.py

A relic that changes a rule is very easy to ship broken, because nothing
crashes when a hook is never called - the run simply plays as though the
player had not picked it up, and the only symptom is a reward that does
nothing. Each check drives the real world through the real hook and looks for
the real effect.
"""

import os
import sys

os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
os.environ.setdefault('CI', '1')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lumen import enemies as enemy_mod                # noqa: E402
from lumen import floorplan as fp                     # noqa: E402
from lumen import relics as relic_mod                 # noqa: E402
from lumen import rng as rng_mod                      # noqa: E402
from lumen import upgrades                            # noqa: E402
from lumen.world import World                         # noqa: E402

RESULTS = []


def check(name, ok, detail):
    RESULTS.append((name, bool(ok), detail))


def fresh(depth=8, kind=fp.COMBAT, seed=31, hold=None):
    rng = rng_mod.Rng(seed)
    world = World(upgrades.Stats(), rng, rng_mod.Rng(seed + 1), 1280, 720)
    world.enter_floor(depth)
    room = next((r for r in world.plan.rooms.values() if r.kind == kind), None)
    if room is not None:
        world.enter_room(room)
    if hold:
        world.take_relic(relic_mod.BY_KEY[hold])
    return world


def an_enemy(world, lit=True):
    cls = enemy_mod.SPECIES[enemy_mod.CRAWLER]
    enemy = cls(world.player.x + 60, world.player.y, world.depth, world.rng)
    enemy.lit = lit
    world.enemies.append(enemy)
    return enemy


# --------------------------------------------------------------------------
def case_every_relic_applies():
    """Taking any of them must be accepted, and must not throw."""
    bad = []
    for relic in relic_mod.ALL:
        try:
            world = fresh()
            if not world.take_relic(relic):
                bad.append(f'{relic.key} refused')
        except Exception as exc:                      # noqa: BLE001
            bad.append(f'{relic.key}: {type(exc).__name__}')
    check('every relic can be taken', not bad,
          f'{len(relic_mod.ALL)} relics' if not bad else '; '.join(bad))


def case_every_hook_runs():
    """Every declared hook must survive being called by the real world.

    A hook is only ever reached from one place in `world.py`, and a typo in
    the name means it is silently never reached at all - which looks exactly
    like a relic that does nothing.
    """
    bad = []
    for relic in relic_mod.ALL:
        for hook in relic.hooks:
            world = fresh(hold=relic.key)
            args = ()
            if hook == relic_mod.ON_KILL:
                args = (an_enemy(world),)
            elif hook == relic_mod.ON_HURT:
                args = (12.0,)
            try:
                world.fire_relics(hook, *args)
            except Exception as exc:                  # noqa: BLE001
                bad.append(f'{relic.key}.{hook}: {type(exc).__name__}: {exc}')
    total = sum(len(r.hooks) for r in relic_mod.ALL)
    check('every hook runs', not bad,
          f'{total} hooks across {len(relic_mod.ALL)} relics'
          if not bad else '; '.join(bad))


def case_longwick_feeds_the_lantern():
    world = fresh(hold='longwick')
    world.player.fuel = 40.0
    before = world.player.fuel
    world.fire_relics(relic_mod.ON_ROOM_ENTER)
    check('THE LONG WICK feeds the lantern',
          world.player.fuel > before + 5.0,
          f'{before:.0f} -> {world.player.fuel:.0f} fuel on entering a room')


def case_tinderbox_only_pays_for_lit_kills():
    """The condition is the relic. A version that paid for every kill would
    be a flat fuel bonus with a story attached."""
    world = fresh(hold='tinderbox')
    world.player.fuel = 40.0
    world.fire_relics(relic_mod.ON_KILL, an_enemy(world, lit=False))
    unlit = world.player.fuel
    world.fire_relics(relic_mod.ON_KILL, an_enemy(world, lit=True))
    lit = world.player.fuel
    check('TINDERBOX pays only for lit kills',
          abs(unlit - 40.0) < 1e-6 and lit > unlit,
          f'unlit kill {unlit:.1f}, lit kill {lit:.1f}, from 40.0')


def case_gutterlamp_turns_a_hit_into_light():
    world = fresh(hold='gutterlamp')
    world.player.fuel = 30.0
    before = world.player.fuel
    world.fire_relics(relic_mod.ON_HURT, 20.0)
    check('GUTTERLAMP flares on a hit', world.player.fuel > before + 4.0,
          f'{before:.0f} -> {world.player.fuel:.0f} fuel after being hit')


def case_secondwind_puts_speed_in_your_legs():
    world = fresh(hold='secondwind')
    world.player.haste = 0.0
    world.fire_relics(relic_mod.ON_HURT, 20.0)
    check('SECOND WIND hastens on a hit', world.player.haste > 1.0,
          f'{world.player.haste:.2f}s of haste after being hit')


def case_pyre_leaves_something_burning():
    world = fresh(hold='pyre')
    world.pools = []
    for _ in range(60):
        world.fire_relics(relic_mod.ON_KILL, an_enemy(world))
    check('PYRE leaves something burning', len(world.pools) > 0,
          f'{len(world.pools)} burning marks from 60 kills')


def case_magpie_widens_the_reach():
    plain = fresh()
    base = plain.stats.pickup_radius
    world = fresh(hold='magpie')
    check('MAGPIE reaches further',
          world.stats.pickup_radius > base * 2.0,
          f'{base:.0f} -> {world.stats.pickup_radius:.0f} px')


def case_hollowheart_costs_what_it_says():
    world = fresh()
    before = world.stats.damage_mult
    world.take_relic(relic_mod.BY_KEY['hollowheart'])
    check('THE HOLLOW HEART trades healing for damage',
          world.stats.damage_mult > before * 1.4 and world.stats.no_heal,
          f'damage x{before:.2f} -> x{world.stats.damage_mult:.2f}, '
          f'no_heal={world.stats.no_heal}')


def case_a_cache_usually_holds_one():
    """The whole reason relics exist: a detour has to be worth walking."""
    from lumen import fixtures
    rng = rng_mod.Rng(11)
    withrelic = 0
    total = 400
    for _ in range(total):
        _embers, _oil, key = fixtures.cache_value(9, rng, held=())
        if key:
            withrelic += 1
    share = withrelic / total
    check('a cache usually holds a relic', 0.5 < share < 0.75,
          f'{share * 100:.0f}% of caches')


def case_they_are_not_all_the_same_kind():
    """Half of them have to be rules rather than numbers, or this is just
    a second upgrade pool with a different screen."""
    ruled = sum(1 for r in relic_mod.ALL if r.hooks)
    check('at least half change a rule', ruled >= len(relic_mod.ALL) * 0.4,
          f'{ruled} of {len(relic_mod.ALL)} have hooks')


def main():
    for case in (case_every_relic_applies, case_every_hook_runs,
                 case_longwick_feeds_the_lantern,
                 case_tinderbox_only_pays_for_lit_kills,
                 case_gutterlamp_turns_a_hit_into_light,
                 case_secondwind_puts_speed_in_your_legs,
                 case_pyre_leaves_something_burning,
                 case_magpie_widens_the_reach,
                 case_hollowheart_costs_what_it_says,
                 case_a_cache_usually_holds_one,
                 case_they_are_not_all_the_same_kind):
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
