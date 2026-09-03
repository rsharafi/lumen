"""Drive every species in the bestiary, and prove each one does its thing.

    .venv/bin/python tools/bestiary_check.py

A species that spawns without crashing is not a species that works. Each
case here puts one of them in a real chamber with a real player and asserts
the specific behaviour it was built for - a lurker must actually stop when
lit, a pale must actually shrug off a shot in the dark, a splitter must
actually leave two things behind. Those are the claims the design document
makes, and this is what holds them to it.
"""

import math
import os
import sys

os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
os.environ.setdefault('CI', '1')
os.environ.setdefault('LUMEN_HEADLESS', '1')
os.environ.setdefault('LUMEN_RENDERER', 'cpu')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cmu_graphics.cmu_graphics as _cg          # noqa: E402,F401

from lumen import enemies as E                   # noqa: E402
from lumen import floorplan as fp                # noqa: E402
from lumen import projectiles as P               # noqa: E402
from lumen import rng as rng_mod                 # noqa: E402
from lumen import upgrades                       # noqa: E402
from lumen.world import World                    # noqa: E402

DT = 1.0 / 60.0


def fresh(depth=12):
    """A world sitting in a plain combat room, with nothing else in it."""
    rng = rng_mod.Rng(4242)
    world = World(upgrades.Stats(), rng, rng_mod.Rng(99), 1280, 720)
    world.enter_floor(depth)
    room = next(r for r in world.plan.rooms.values() if r.kind == fp.COMBAT)
    world.enter_room(room)
    world.enemies = []
    world.wards = []
    return world


def place(world, cls, want=140.0, sight=True, **kw):
    """Put one species on genuinely open ground `want` px from the player.

    Nudging a body out of the stone with `collide_circle` is not enough: a
    shot fired at something standing flush against a wall dies on the wall
    before it arrives, which looked exactly like a mirror failing to reflect.
    So this walks the chamber's own open tiles and picks the one nearest the
    distance asked for that has clear line of sight to the player.
    """
    px, py = world.player.x, world.player.y
    # Clearance is relaxed in stages. Demanding a clear disc of radius + 22
    # around a tile centre means demanding the neighbouring tiles too - the
    # disc is wider than a tile - and in a pillared chamber most tiles fail
    # that. Insisting on it silently returned whatever was nearest instead,
    # which is how a lurker meant to be standing in the dark ended up 244 px
    # from a lantern that reaches 288.
    best = None
    for pad in (22.0, 10.0, 2.0):
        best_err = 1e18
        for col, row in world.level.open_tiles(1):
            x, y = world.level.tile_center(col, row)
            if not world.level.is_open_at(x, y, cls.radius + pad):
                continue
            if sight and world.level.ray_blocked(px, py, x, y):
                continue
            err = abs(math.hypot(x - px, y - py) - want)
            if err < best_err:
                best_err = err
                best = (x, y)
        # Good enough only if it actually landed near where it was asked for.
        if best is not None and best_err < 90.0:
            break
    assert best is not None, f'no open ground for {cls.__name__}'
    e = cls(best[0], best[1], world.depth, world.rng, **kw)
    e.spawn_t = 0.0
    world.enemies.append(e)
    return e


def step(world, frames, firing=False, aim=None):
    aim = aim or (world.player.x + 200, world.player.y)
    for _ in range(frames):
        world.update(DT, set(), aim, firing)


RESULTS = []


def check(name, ok, detail):
    RESULTS.append((name, bool(ok), detail))


# -- every species spawns, updates and draws --------------------------------
def case_all_spawn():
    for key, cls in E.SPECIES.items():
        world = fresh()
        e = place(world, cls)
        try:
            step(world, 90)
        except Exception as exc:                  # noqa: BLE001
            check(f'{key} runs', False, f'{type(exc).__name__}: {exc}')
            continue
        check(f'{key} runs', True,
              f'hp {e.hp:.0f}/{e.max_hp:.0f}, moved '
              f'{math.hypot(e.vx, e.vy):.0f} px/s')


# -- the lurker stops when it is lit ----------------------------------------
def case_lurker():
    """Two lurkers, one lit and one not, in the same room on the same frame.

    Setting `e.lit` by hand proves nothing - the lighting pass recomputes it
    from the real lantern every frame and overwrites whatever the test said.
    So this uses the lantern itself: one lurker close enough to be inside it
    and one far outside, and asserts the near one seizes up while the far one
    keeps coming.
    """
    """One lurker, sampled every frame as it walks out of the dark.

    Measured at a single instant this proves nothing, because a lurker
    placed outside the lantern spends the next second walking into it - the
    first version of this test read its speed after eighty frames, by which
    point it had closed to 244 px of a 288 px lantern and was correctly
    frozen. So the claim is checked over time instead: it must be moving on
    the frames it is dark, and stopped on the frames it is lit.
    """
    world = fresh()
    world.player.fuel = world.player.fuel_max
    world.update(DT, set(), (world.player.x + 200, world.player.y), False)
    e = place(world, E.Lurker, want=world.light_radius + 180.0, sight=False)

    dark_top = 0.0
    lit_top = 0.0
    dark_frames = lit_frames = 0
    for _ in range(240):
        world.update(DT, set(), (world.player.x + 200, world.player.y), False)
        v = math.hypot(e.vx, e.vy)
        if e.lit:
            lit_frames += 1
            # Ignore the moment it is caught: it decelerates over a few
            # frames rather than stopping dead, which is deliberate.
            if lit_frames > 30:
                lit_top = max(lit_top, v)
        else:
            dark_frames += 1
            dark_top = max(dark_top, v)

    check('lurker freezes in the light',
          dark_frames > 20 and lit_frames > 40
          and dark_top > 120.0 and lit_top < dark_top * 0.1,
          f'{dark_top:.0f} px/s over {dark_frames} dark frames, '
          f'{lit_top:.0f} px/s over {lit_frames} lit frames')


# -- the pale can only be hurt while lit ------------------------------------
def case_pale():
    world = fresh()
    lit = place(world, E.Pale)
    lit.lit = True
    dealt_lit = lit.damage_by(100.0, world)
    world2 = fresh()
    dark = place(world2, E.Pale)
    dark.lit = False
    dealt_dark = dark.damage_by(100.0, world2)
    check('pale shrugs off the dark', dealt_dark < dealt_lit * 0.15,
          f'{dealt_lit:.0f} lit vs {dealt_dark:.0f} unlit, of 100')


# -- the douser takes fuel and chokes the flame -----------------------------
def case_douser():
    world = fresh()
    e = place(world, E.Douser, want=60.0)
    world.player.fuel = 90.0
    before = world.player.fuel
    step(world, 60)
    check('douser bites the lantern',
          world.player.fuel < before - 10.0 and world.player.choke > 0.0,
          f'fuel {before:.0f} -> {world.player.fuel:.0f}, '
          f'choke {world.player.choke:.2f}s')


# -- the splitter leaves two behind -----------------------------------------
def case_splitter():
    world = fresh()
    e = place(world, E.Splitter)
    e.damage_by(9999.0, world)
    kids = [x for x in world.enemies if x.species == E.SPLITTER and x.alive]
    gens = sorted(k.generation for k in kids)
    check('splitter splits', len(kids) == 2 and gens == [1, 1],
          f'{len(kids)} children, generations {gens}')
    # And the last generation must not.
    world2 = fresh()
    last = place(world2, E.Splitter, generation=E.Splitter.GENERATIONS)
    last.damage_by(9999.0, world2)
    left = [x for x in world2.enemies if x.alive]
    check('splitter stops splitting', not left,
          f'{len(left)} left after the last generation died')


# -- the carrion leaves a pool that hurts -----------------------------------
def case_carrion():
    world = fresh()
    e = place(world, E.Carrion, want=60.0)
    e.damage_by(9999.0, world)
    pools = len(world.pools)
    hp = world.player.hp
    step(world, 60)
    check('carrion leaves rot', pools == 1 and world.player.hp < hp,
          f'{pools} pool, player {hp:.0f} -> {world.player.hp:.0f} hp')
    # And it dries up.
    step(world, int(E.Carrion.POOL_TIME * 60) + 30)
    check('rot dries up', not world.pools, f'{len(world.pools)} pools left')


# -- the mirror sends a shot back as hostile fire ---------------------------
def case_mirror():
    world = fresh()
    e = place(world, E.Mirror, want=150.0)
    e.facing = math.pi                            # facing the player
    hp = e.hp
    world.projectiles.spawn(
        P.PLAYER, e.x - 40.0, e.y, 800.0, 0.0, 20.0, radius=5.0, life=1.0)
    # Checked as soon as it has turned round, not twelve frames later: the
    # reflected bolt is travelling back across the room and will have hit
    # something by then, which is the point of it.
    hostile = []
    for _ in range(6):
        step(world, 1)
        hostile = [p for p in world.projectiles.pool
                   if p.alive and p.owner == P.ENEMY]
        if hostile:
            break
    check('mirror turns a shot round',
          e.hp >= hp - 0.01 and len(hostile) == 1,
          f'took {hp - e.hp:.1f} damage, {len(hostile)} hostile bolt(s) back')
    # From behind, it takes the hit like anything else.
    world2 = fresh()
    b = place(world2, E.Mirror, want=150.0)
    b.facing = math.pi
    hp2 = b.hp
    world2.projectiles.spawn(
        P.PLAYER, b.x + 40.0, b.y, -800.0, 0.0, 20.0, radius=5.0, life=1.0)
    step(world2, 12)
    check('mirror is open from behind', b.hp < hp2 - 1.0,
          f'took {hp2 - b.hp:.1f} damage from the back')


# -- the delver cannot be touched under the floor ---------------------------
def case_delver():
    world = fresh()
    e = place(world, E.Delver, want=150.0)
    e.above_t = 0.0
    step(world, 10)                               # let it dive
    check('delver dives', e.submerged and e.immune(),
          f'buried {e.buried:.2f}s, immune {e.immune()}')
    hp = e.hp
    world.projectiles.spawn(
        P.PLAYER, e.x - 40.0, e.y, 800.0, 0.0, 30.0, radius=5.0, life=1.0)
    step(world, 12)
    check('shots pass over a delver', e.hp >= hp - 0.01,
          f'took {hp - e.hp:.1f} damage while buried')
    # It must come back up, and be hittable again.
    step(world, 200)
    check('delver surfaces', not e.submerged,
          f'buried {e.buried:.2f}s, tell {e.tell:.2f}s')


# -- the keener mends what it is tethered to --------------------------------
def case_keener():
    world = fresh()
    k = place(world, E.Keener, want=200.0)
    hurt = place(world, E.Crawler, want=170.0)
    hurt.hp = hurt.max_hp * 0.4
    before = hurt.hp
    step(world, 60)
    check('keener mends', hurt.hp > before + 3.0 and k.link is hurt,
          f'crawler {before:.0f} -> {hurt.hp:.0f} hp, linked {k.link is hurt}')


# -- the bolter telegraphs before it charges --------------------------------
def case_bolter():
    world = fresh()
    e = place(world, E.Bolter, want=300.0)
    e.attack_cd = 0.0
    saw_wind = False
    saw_charge = False
    for _ in range(240):
        world.update(DT, set(), (world.player.x + 200, world.player.y), False)
        if e.wind > 0.0:
            saw_wind = True
        if e.charging > 0.0:
            saw_charge = True
            break
    check('bolter winds up before it charges', saw_wind and saw_charge,
          f'wind-up seen {saw_wind}, charge seen {saw_charge}')


def main():
    for case in (case_all_spawn, case_lurker, case_pale, case_douser,
                 case_splitter, case_carrion, case_mirror, case_delver,
                 case_keener, case_bolter):
        try:
            case()
        except Exception as exc:                  # noqa: BLE001
            import traceback
            traceback.print_exc(file=sys.stderr)
            check(case.__name__, False, f'{type(exc).__name__}: {exc}')

    width = max(len(n) for n, _ok, _d in RESULTS)
    bad = 0
    for name, ok, detail in RESULTS:
        mark = ' ok ' if ok else 'FAIL'
        if not ok:
            bad += 1
        print(f'  {mark}  {name:<{width}}  {detail}')
    print()
    total = len(RESULTS)
    print(f'{total - bad} of {total} checks passed')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
