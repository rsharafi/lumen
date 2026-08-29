"""Every stat an upgrade sets has to change something. This checks that.

An upgrade that raises a number nothing reads is worse than useless: it takes
a slot on the offering, reads as a real choice, and does nothing. So each stat
is pushed far from its default, a fixed scripted fight is run twice, and an
observable has to move.

Several of these only pay out under a condition - `bulwark` while standing
still, `revives` only if something kills you - so a case can ask for the
scenario it needs.

A stat that does not move its observable is reported, but it is not
automatically a bug: it may simply mean this harness could not manufacture the
situation the stat needs. Those are listed as UNPROVEN and do not fail the
run. `MUST_MOVE` is the set the harness genuinely can exercise, and one of
those going quiet is a real regression.

It has already earned itself twice: `chain` was a stat with an upgrade, a
field, and no implementation anywhere, and `light_damage` bypassed
`damage_by`, so burning an enemy down with the lantern counted for nothing in
the run summary and fed no lifesteal.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('LUMEN_RENDERER', 'cpu')
os.environ.setdefault('CI', '1')
os.environ.setdefault('LUMEN_SAVE', '/tmp/lumen-stat-probe.json')

from lumen import rng, upgrades                       # noqa: E402
from lumen.world import World                         # noqa: E402

# Short by default: total damage dealt saturates once the floor is dead, and
# a saturated observable cannot tell a doubled stat from an untouched one.
TICKS = 260

CASES = {
    'damage_mult':      dict(value=3.0,   obs='damage'),
    'dark_damage':      dict(value=4.0,   obs='damage'),
    'first_strike':     dict(value=4.0,   obs='damage'),
    'overcharge':       dict(value=4.0,   obs='damage', fuel=0.05),
    'momentum':         dict(value=3.0,   obs='damage'),
    'crit_chance':      dict(value=1.0,   obs='damage'),
    'crit_mult':        dict(value=8.0,   obs='damage', crit=True),
    'fire_rate_mult':   dict(value=4.0,   obs='damage'),
    'swarm':            dict(value=6,     obs='shots'),
    'pierce_bonus':     dict(value=8,     obs='damage', ticks=200),
    'projectile_speed_mult': dict(value=2.2, obs='damage'),
    'dash_damage':      dict(value=400.0, obs='damage', dash=True, chase=True,
                             hold_fire=True, ticks=700),
    'siphon':           dict(value=40.0,  obs='fuel'),
    'ember_gain':       dict(value=8.0,   obs='embers', pickup=1200.0, ticks=1500),
    'pickup_radius':    dict(value=1200.0, obs='embers', ticks=1500),
    'kill_heal':        dict(value=40.0,  obs='healed'),
    'lifesteal':        dict(value=0.9,   obs='healed'),
    'bulwark':          dict(value=0.9,   obs='taken', still=True, ticks=1500),
    'taken_mult':       dict(value=4.0,   obs='taken', still=True, ticks=1500),
    'revives':          dict(value=4,     obs='alive', hp=10.0, ticks=2600),
    'shield_charges':   dict(value=6,     obs='taken', still=True, ticks=1500),
    'speed_mult':       dict(value=2.5,   obs='moved'),
    'lantern_mult':     dict(value=2.5,   obs='radius'),
    'fuel_max_mult':    dict(value=3.0,   obs='fuel'),
    'lantern_efficiency': dict(value=6.0, obs='fuel'),
    'thorns':           dict(value=60.0,  obs='damage', chase=True,
                             hold_fire=True, ticks=900),
    'light_damage':     dict(value=90.0,  obs='damage'),
    'slow_field':       dict(value=0.95,  obs='taken', still=True,
                             hold_fire=True, ticks=900),
    'homing':           dict(value=1.0,   obs='damage'),
    'explode_radius':   dict(value=140.0, obs='damage'),
    'chain':            dict(value=4,     obs='damage'),
    'bounces':          dict(value=4,     obs='damage'),
}


def run(stat=None, value=None, still=False, dash=False, hp=None, fuel=None,
        pickup=None, crit=False, chase=False, hold_fire=False,
        ticks=TICKS):
    rng.world.reseed(4242)
    rng.fx.reseed(4243)
    stats = upgrades.Stats()
    if crit:
        stats.crit_chance = 1.0
    if hp is not None:
        stats.max_hp = hp
    if pickup is not None:
        stats.pickup_radius = pickup
    if stat is not None:
        setattr(stats, stat, value)
    world = World(stats, rng.world, rng.fx, 1280, 720)
    world.enter_floor(3)
    p = world.player
    p.refresh_from_stats()
    p.hp = stats.max_hp
    if fuel is not None:
        p.fuel = p.fuel_max * fuel

    healed = [0.0]
    real_heal = p.heal

    def heal(amount):
        healed[0] += amount
        return real_heal(amount)
    p.heal = heal

    moved = 0.0
    deaths = 0
    cleared_at = ticks
    for i in range(ticks):
        if cleared_at == ticks and not any(e.alive for e in world.enemies):
            cleared_at = i
        px, py = p.x, p.y
        target, best = None, 1e18
        for e in world.enemies:
            if not e.alive:
                continue
            d = (e.x - p.x) ** 2 + (e.y - p.y) ** 2
            if d < best:
                best, target = d, e
        aim = (target.x, target.y) if target else (p.x + 400.0, p.y)
        if still:
            keys = set()
        elif chase and target is not None:
            # Walk into the fight. A stat that only pays out on contact -
            # a cleaving dash, thorns - never fires against a probe that
            # paces back and forth out of reach.
            keys = set()
            keys.add('d' if target.x > p.x + 8 else
                     ('a' if target.x < p.x - 8 else 'd'))
            keys.add('s' if target.y > p.y + 8 else
                     ('w' if target.y < p.y - 8 else 's'))
        else:
            keys = {'d'} if (i // 60) % 2 == 0 else {'a'}
        if dash and i % 40 == 0 and target is not None:
            # Dash *at* something, or a cleaving dash has nothing to cleave.
            tk = set()
            tk.add('d' if target.x > p.x else 'a')
            tk.add('s' if target.y > p.y else 'w')
            p.try_dash(tk, world.particles, rng.fx)
        world.update(1 / 120.0, keys, aim, not hold_fire)
        moved += abs(p.x - px) + abs(p.y - py)
        if not p.alive:
            deaths += 1
            break
    return {
        'damage': p.damage_dealt,
        'shots': p.shots_fired,
        'fuel': p.fuel,
        'embers': world.embers,
        'healed': healed[0],
        'taken': stats.max_hp - p.hp,
        'moved': moved,
        'radius': world.light_radius,
        'alive': float(ticks - deaths * ticks) if deaths else float(ticks),
        'kills': p.kills,
        # Total damage saturates the moment the floor is dead - it is bounded
        # by the enemies' health, so a doubled stat and an untouched one look
        # identical. How long the floor took cannot saturate.
        'clear': float(cleared_at),
    }


# The cases this harness can reliably create the conditions for. Anything
# here going quiet is a regression; anything outside it is a maybe.
MUST_MOVE = {
    'damage_mult', 'dark_damage', 'first_strike', 'overcharge', 'momentum',
    'crit_chance', 'crit_mult', 'fire_rate_mult', 'swarm', 'chain',
    'projectile_speed_mult', 'siphon', 'ember_gain', 'pickup_radius',
    'kill_heal', 'lifesteal', 'taken_mult', 'shield_charges', 'revives',
    'speed_mult', 'lantern_mult', 'fuel_max_mult', 'lantern_efficiency',
    'homing', 'explode_radius', 'bounces', 'dash_damage',
}


def main():
    bad = []
    unproven = []
    print(f'  {"":4s} {"stat":24s} {"observable":10s} {"off":>10s} {"on":>10s}')
    for stat, case in sorted(CASES.items()):
        obs = case['obs']
        setup = {k: v for k, v in case.items() if k not in ('value', 'obs')}
        base = run(**setup)
        got = run(stat, case['value'], **setup)
        a, b = base[obs], got[obs]
        moved = abs(b - a) > max(1e-6, abs(a) * 0.02)
        if not moved:
            (bad if stat in MUST_MOVE else unproven).append(stat)
        print(f'  {"ok " if moved else "DEAD"} {stat:24s} {obs:10s} '
              f'{a:10.2f} {b:10.2f}')
    live = len(CASES) - len(bad) - len(unproven)
    print(f'\n  {live} of {len(CASES)} stats moved their observable')
    if unproven:
        print('  UNPROVEN (condition not reached here): '
              + ', '.join(unproven))
    if bad:
        print('  DEAD: ' + ', '.join(bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
