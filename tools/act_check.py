"""Prove the three acts are actually three different places.

    .venv/bin/python tools/act_check.py

`act_of` existed for a long time and decided exactly one thing - how many
rooms a floor had - so floor three and floor eighteen were the same stone with
bigger numbers in them. It is very easy for that to quietly come back: a grade
that is too subtle to see, a hazard that generates and never fires, a layout
table that is weighted but never consulted. Each check here is one claim about
a difference a player could actually notice, held to a number.
"""

import os
import statistics
import sys

os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
os.environ.setdefault('CI', '1')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np                                   # noqa: E402

from lumen import acts                               # noqa: E402
from lumen import floorplan as fp                    # noqa: E402
from lumen import hazards                            # noqa: E402
from lumen import level as level_mod                 # noqa: E402
from lumen import rng as rng_mod                     # noqa: E402
from lumen import upgrades                           # noqa: E402
from lumen.world import World                        # noqa: E402

#: One floor from each act, comfortably inside it.
SAMPLE = (3, 10, 18)

RESULTS = []


def check(name, ok, detail):
    RESULTS.append((name, bool(ok), detail))


def _room_of(depth, kind, seed):
    rng = rng_mod.Rng(seed)
    world = World(upgrades.Stats(), rng, rng_mod.Rng(seed + 1), 1280, 720)
    world.enter_floor(depth)
    room = next((r for r in world.plan.rooms.values() if r.kind == kind), None)
    if room is None:
        return None
    world.enter_room(room)
    return world


# --------------------------------------------------------------------------
def case_the_stone_looks_different():
    """Each act's floor has to be a visibly different colour.

    Measured on the graded pixels rather than by eye. A grade nobody can see
    is a grade that is not there, so the bar is a real one: the mean colour
    of two acts' stone must differ by more than a couple of levels, which is
    roughly where a difference stops being dismissable as dither.
    """
    # A baked layer does not keep its source - it is 8 MB apiece and nothing
    # in the game reads it back - so the pixels are caught on their way into
    # `art.wrap`. That is deliberately the *real* bake rather than a grade
    # applied to a swatch here: the claim being tested is that the chamber
    # comes out graded, not that the grade function works.
    from lumen import art
    means = {}
    real_wrap = art.wrap
    caught = {}

    def spy(pil, key, keep_source=False):
        if isinstance(key, tuple) and 'floor' in repr(key) \
                and 'normal' not in repr(key):
            caught.setdefault('floor', pil.copy())
        return real_wrap(pil, key, keep_source)

    try:
        art.wrap = spy
        for depth in SAMPLE:
            caught.clear()
            rng = rng_mod.Rng(depth * 17 + 5)
            level_mod.generate(depth, rng, doors=('n',), size='medium')
            pil = caught.get('floor')
            if pil is None:
                check('the stone looks different', False,
                      'the bake published no floor layer')
                return
            arr = np.asarray(pil.convert('RGB'), dtype=np.float32)
            means[depth] = arr.reshape(-1, 3).mean(axis=0)
    finally:
        art.wrap = real_wrap

    pairs = []
    ok = True
    keys = sorted(means)
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            a, b = means[keys[i]], means[keys[j]]
            dist = float(np.abs(a - b).max())
            pairs.append(f'{keys[i]}v{keys[j]} {dist:.1f}')
            if dist < 3.0:
                ok = False
    check('the stone looks different', ok,
          'largest channel gap: ' + ', '.join(pairs))


def case_acts_prefer_different_shapes():
    """The layout tables must actually be consulted."""
    top = {}
    for depth in SAMPLE:
        rng = rng_mod.Rng(depth * 29 + 7)
        seen = {}
        for _ in range(200):
            level = level_mod.generate(depth, rng, doors=('n',), bake=False)
            seen[level.archetype] = seen.get(level.archetype, 0) + 1
        top[depth] = max(seen, key=seen.get)
    ok = len(set(top.values())) == len(SAMPLE)
    check('acts prefer different shapes', ok,
          ', '.join(f'floor {d}: {a}' for d, a in sorted(top.items())))


def case_fire_gets_scarcer():
    """Braziers thin out with depth - the fuel budget, as level design."""
    avg = {}
    for depth in SAMPLE:
        rng = rng_mod.Rng(depth * 41 + 3)
        counts = [len(level_mod.generate(depth, rng, doors=('n',),
                                         bake=False).braziers)
                  for _ in range(120)]
        avg[depth] = statistics.mean(counts)
    ok = avg[SAMPLE[0]] > avg[SAMPLE[1]] > avg[SAMPLE[2]]
    check('fire gets scarcer', ok,
          ', '.join(f'floor {d}: {v:.1f}' for d, v in sorted(avg.items())))


def case_each_act_has_its_ground():
    """Act one is bare; the other two are not, and not in the same way."""
    found = {}
    for depth in SAMPLE:
        pools = vents = 0
        for seed in range(6):
            world = _room_of(depth, fp.COMBAT, depth * 100 + seed)
            if world is None:
                continue
            pools += len(world.hazards.pools)
            vents += len(world.hazards.vents)
        found[depth] = (pools, vents)
    ok = (found[3] == (0, 0)
          and found[10][0] > 0 and found[10][1] == 0
          and found[18][1] > 0 and found[18][0] == 0)
    check('each act has its own ground', ok,
          ', '.join(f'floor {d}: {p} pools / {v} vents'
                    for d, (p, v) in sorted(found.items())))


def case_water_slows_and_shows():
    """Standing water has to cost speed and pay in reach, or it is decoration."""
    world = None
    for seed in range(12):
        world = _room_of(10, fp.COMBAT, 900 + seed)
        if world is not None and world.hazards.pools:
            break
    if world is None or not world.hazards.pools:
        check('water slows and shows', False, 'no flooded room found')
        return
    pool = world.hazards.pools[0]
    inside = world.hazards.slow_at(pool.x, pool.y)
    outside = world.hazards.slow_at(pool.x + 4000, pool.y)
    reach_in = world.hazards.reach_at(pool.x, pool.y)
    reach_out = world.hazards.reach_at(pool.x + 4000, pool.y)
    check('water slows and shows',
          inside > 0.2 and outside == 0.0 and reach_in > 1.05
          and reach_out == 1.0,
          f'in: -{inside * 100:.0f}% speed, +{(reach_in - 1) * 100:.0f}% reach; '
          f'out: -{outside * 100:.0f}% / +{(reach_out - 1) * 100:.0f}%')


def case_vents_warn_before_they_burn():
    """A vent must telegraph, then fire, and the warning must lead the gout.

    The failure this rules out is the one that makes a hazard feel unfair:
    a vent whose warning window is shorter than a player's reaction, or one
    that burns without warning at all.
    """
    world = None
    for seed in range(12):
        world = _room_of(18, fp.COMBAT, 700 + seed)
        if world is not None and world.hazards.vents:
            break
    if world is None or not world.hazards.vents:
        check('vents warn before they burn', False, 'no vented room found')
        return
    vent = world.hazards.vents[0]
    vent.t = 0.0
    dt = 1.0 / 120.0
    tell_first = burn_first = None
    for i in range(int(vent.period * 130)):
        vent.t += dt
        if vent.t >= vent.period:
            break
        if vent.telling and tell_first is None:
            tell_first = i * dt
        if vent.burning and burn_first is None:
            burn_first = i * dt
    ok = (tell_first is not None and burn_first is not None
          and tell_first < burn_first
          and (burn_first - tell_first) > 0.5)
    lead = (burn_first - tell_first) if (tell_first is not None
                                         and burn_first is not None) else 0.0
    check('vents warn before they burn', ok,
          f'warning leads the gout by {lead:.2f}s')


def case_vents_burn_what_stands_in_them():
    """And the gout has to actually cost the player something."""
    world = None
    for seed in range(12):
        world = _room_of(18, fp.COMBAT, 700 + seed)
        if world is not None and world.hazards.vents:
            break
    if world is None or not world.hazards.vents:
        check('vents burn what stands in them', False, 'no vented room found')
        return
    vent = world.hazards.vents[0]
    world.player.x, world.player.y = vent.x, vent.y
    world.player.hp = world.stats.max_hp
    before = world.player.hp
    # Wind it to the moment of ignition and hold the player there.
    vent.t = vent.period - hazards.VENT_BURN * 0.9
    for _ in range(40):
        world.player.x, world.player.y = vent.x, vent.y
        world.hazards.update(1.0 / 120.0, world)
    lost = before - world.player.hp
    check('vents burn what stands in them', lost > 3.0,
          f'{lost:.1f} health lost standing in one')


def main():
    for case in (case_the_stone_looks_different,
                 case_acts_prefer_different_shapes,
                 case_fire_gets_scarcer,
                 case_each_act_has_its_ground,
                 case_water_slows_and_shows,
                 case_vents_warn_before_they_burn,
                 case_vents_burn_what_stands_in_them):
        try:
            case()
        except Exception as exc:                     # noqa: BLE001
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
