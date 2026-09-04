"""Prove a dead boss is worth something, and that the something works.

    .venv/bin/python tools/boon_check.py

Weapon mods are the part of this most likely to be quietly broken: a mod is a
new `Weapon` built from an old one by name, and a typo in a field name is a
`KeyError` at best and a silently unchanged gun at worst. Every mod here is
applied to every weapon and the result is compared against the original.
"""

import os
import sys

os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
os.environ.setdefault('CI', '1')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lumen import boons                               # noqa: E402
from lumen import floorplan as fp                     # noqa: E402
from lumen import rng as rng_mod                      # noqa: E402
from lumen import upgrades                            # noqa: E402
from lumen.projectiles import WEAPONS, WEAPONS_BY_KEY  # noqa: E402
from lumen.world import World                         # noqa: E402

RESULTS = []


def check(name, ok, detail):
    RESULTS.append((name, bool(ok), detail))


def a_run(weapons=('lance', 'scatter', 'coil')):
    stats = upgrades.Stats()
    stats.weapons = list(weapons)
    return stats


def case_every_mod_applies_to_every_weapon():
    bad = []
    for weapon in WEAPONS:
        for mod in boons.MODS:
            try:
                out = mod.applied(weapon)
            except Exception as exc:                  # noqa: BLE001
                bad.append(f'{weapon.key}+{mod.key}: {type(exc).__name__}')
                continue
            if out.pellets < 1 or out.pierce < 0 or out.cooldown <= 0.0:
                bad.append(f'{weapon.key}+{mod.key}: nonsense output')
    check('every mod applies to every weapon', not bad,
          f'{len(WEAPONS)} weapons x {len(boons.MODS)} mods'
          if not bad else '; '.join(bad[:3]))


def case_every_mod_changes_something():
    """A mod whose fields all happen to be x1.0 is a mod that does nothing."""
    lance = WEAPONS_BY_KEY['lance']
    same = []
    for mod in boons.MODS:
        out = mod.applied(lance)
        moved = any(abs(getattr(out, f) - getattr(lance, f)) > 1e-9
                    for f in ('cooldown', 'damage', 'speed', 'pellets',
                              'spread', 'life', 'pierce', 'radius',
                              'knockback', 'recoil', 'light', 'width'))
        if not moved:
            same.append(mod.key)
    check('every mod changes something', not same,
          f'{len(boons.MODS)} mods, all of them'
          if not same else f'inert: {", ".join(same)}')


def case_every_mod_is_a_trade():
    """Nothing here may be strictly better than the gun it modifies.

    A mod that only gives is not a decision, it is a reward with extra steps,
    and once one exists it is the only one anybody takes.
    """
    lance = WEAPONS_BY_KEY['lance']
    free = []
    for mod in boons.MODS:
        out = mod.applied(lance)
        # Damage per second is the summary stat; a mod that raises it must
        # give something else up, and one that lowers it must pay elsewhere.
        dps_before = lance.damage * lance.pellets / lance.cooldown
        dps_after = out.damage * out.pellets / out.cooldown
        gave_up = (out.cooldown > lance.cooldown + 1e-9
                   or out.damage < lance.damage - 1e-9
                   or out.speed < lance.speed - 1e-9
                   or out.spread > lance.spread + 1e-9)
        if dps_after > dps_before * 1.02 and not gave_up:
            free.append(f'{mod.key} (+{(dps_after / dps_before - 1) * 100:.0f}% dps, free)')
    check('every mod is a trade', not free,
          'nothing is strictly better' if not free else '; '.join(free))


def case_a_mod_reaches_the_weapon_the_player_holds():
    """The whole chain: grant a boon, then ask the player for its weapon."""
    stats = a_run()
    base = WEAPONS_BY_KEY['lance']
    boon = boons.Boon('lance:heavyshot', 'x', 'y', boons.MOD, base.color,
                      boons._mod_apply('lance', 'heavyshot'))
    boons.grant(stats, boon)
    got = boons.weapon_for(stats, 'lance')
    check('a mod reaches the weapon in hand',
          got is not base and got.damage > base.damage * 1.4,
          f'damage {base.damage} -> {got.damage:.1f}')


def case_an_unmodded_weapon_is_the_shared_one():
    """No copying when there is nothing to copy - a modded lookup on a plain
    weapon must hand back the shared object, not a clone per call."""
    stats = a_run()
    a = boons.weapon_for(stats, 'lance')
    b = boons.weapon_for(stats, 'lance')
    check('an unmodded weapon is not copied',
          a is b and a is WEAPONS_BY_KEY['lance'],
          'the shared object, both times')


def case_offers_are_three_distinct_things():
    stats = a_run()
    rng = rng_mod.Rng(12)
    bad = []
    for i in range(60):
        picks = boons.offer(stats, rng, 3)
        if len(picks) != 3:
            bad.append(f'got {len(picks)}')
        if len({p.key for p in picks}) != len(picks):
            bad.append('duplicate')
        if sum(1 for p in picks if p.kind == boons.MOD) > 1:
            bad.append('two weapon mods at once')
    check('an offer is three distinct things', not bad,
          '60 offers, all clean' if not bad else '; '.join(bad[:3]))


def case_a_mod_is_only_offered_for_a_carried_weapon():
    stats = a_run(weapons=('lance',))
    rng = rng_mod.Rng(5)
    wrong = []
    for _ in range(120):
        for pick in boons.offer(stats, rng, 3):
            if pick.kind == boons.MOD and pick.weapon != 'lance':
                wrong.append(pick.weapon)
    check('a mod is only offered for a weapon you carry', not wrong,
          'only the lance, over 120 offers'
          if not wrong else f'offered for {set(wrong)}')


def case_a_weapon_is_only_modded_once():
    stats = a_run(weapons=('lance',))
    rng = rng_mod.Rng(7)
    boons.grant(stats, boons.Boon('lance:heavyshot', 'x', 'y', boons.MOD,
                                  None, boons._mod_apply('lance',
                                                         'heavyshot')))
    again = []
    for _ in range(120):
        for pick in boons.offer(stats, rng, 3):
            if pick.kind == boons.MOD:
                again.append(pick.key)
    check('a modded weapon is not offered another', not again,
          'the lance is done' if not again else f'{len(again)} more offered')


def case_edge_burns_at_the_rim():
    """EDGE has to damage what is at the boundary and not what is beside you.

    That distinction is the entire boon - a version that burned everything
    lit would be a flat damage aura with a poetic name.
    """
    stats = a_run()
    stats.edge_damage = 40.0
    rng = rng_mod.Rng(21)
    world = World(stats, rng, rng_mod.Rng(22), 1280, 720)
    world.enter_floor(5)
    room = next((r for r in world.plan.rooms.values()
                 if r.kind == fp.COMBAT), None)
    if room is None:
        check('EDGE burns the rim, not the middle', False, 'no combat room')
        return
    world.enter_room(room)
    world.light_radius = 300.0

    from lumen import enemies as enemy_mod
    cls = enemy_mod.SPECIES[enemy_mod.CRAWLER]
    near = cls(world.player.x + 40, world.player.y, 5, rng)
    far = cls(world.player.x + 250, world.player.y, 5, rng)
    for e in (near, far):
        e.lit = True
        e.spawn_t = 0.0
        e.max_hp = e.hp = 4000.0
    world.enemies = [near, far]
    for _ in range(60):
        world.light_radius = 300.0
        world._update_enemies(1.0 / 120.0)
    check('EDGE burns the rim, not the middle',
          far.hp < near.hp - 5.0,
          f'at the rim {4000 - far.hp:.0f} damage, '
          f'beside you {4000 - near.hp:.0f}')


def main():
    for case in (case_every_mod_applies_to_every_weapon,
                 case_every_mod_changes_something,
                 case_every_mod_is_a_trade,
                 case_a_mod_reaches_the_weapon_the_player_holds,
                 case_an_unmodded_weapon_is_the_shared_one,
                 case_offers_are_three_distinct_things,
                 case_a_mod_is_only_offered_for_a_carried_weapon,
                 case_a_weapon_is_only_modded_once,
                 case_edge_burns_at_the_rim):
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
