"""Prove a room change is a walk and not a cut.

    .venv/bin/python tools/crossing_check.py

The claim this holds is that the two rooms are genuinely in one space while
the player is between them: both solid, both drawn, both occluding, and the
swap underneath the player leaving nothing on screen out of place. It is easy
to get a crossing that *looks* right in one screenshot and teleports the
camera on the frame the rooms exchange, so what is measured here is
continuity - of the player, and of where the camera is pointing.
"""

import math
import os
import sys

os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
os.environ.setdefault('CI', '1')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lumen import floorplan as fp                   # noqa: E402
from lumen import rng as rng_mod                    # noqa: E402
from lumen import upgrades                          # noqa: E402
from lumen.world import Span, World                 # noqa: E402

DT = 1.0 / 120.0
RESULTS = []


def check(name, ok, detail):
    RESULTS.append((name, bool(ok), detail))


def a_floor(seed=11, depth=4):
    rng = rng_mod.Rng(seed)
    world = World(upgrades.Stats(), rng, rng_mod.Rng(seed + 5), 1280, 720)
    world.enter_floor(depth)
    return world


def walk_through(world, side, steps=900):
    """Drive the player at a doorway and out the far side.

    Started from the doorway rather than from wherever the room put them:
    this is a test of the crossing, and a straight-line walk across a
    chamber full of pillars is a test of pathfinding, which would fail it for
    the wrong reason.

    Returns the per-frame trail of (player, camera, room id), which is what
    every continuity claim below is made against.
    """
    world.player.x, world.player.y = world.level.door_entry(side, inset=2.4)
    world.player.vx = world.player.vy = 0.0
    world.camera.snap_to(world.player.x, world.player.y)
    world.door_lock = 0.0
    trail = []
    heading = {'n': (0.0, -1.0), 's': (0.0, 1.0),
               'w': (-1.0, 0.0), 'e': (1.0, 0.0)}[side]
    started = world.room.id
    for _ in range(steps):
        if world.crossing is None and world.room.id != started:
            break                     # through, and settled in the new room
        far = fp.OPPOSITE[side]
        if world.crossing is not None and world.crossing['from'] is not None:
            cx, cy = world.level.door_center(far)
        elif side in world.level.doors:
            cx, cy = world.level.door_center(side)
        else:
            break
        goal = (cx + heading[0] * 420.0, cy + heading[1] * 420.0)
        dx = goal[0] - world.player.x
        dy = goal[1] - world.player.y
        keys = set()
        if dx > 6:
            keys.add('d')
        elif dx < -6:
            keys.add('a')
        if dy > 6:
            keys.add('s')
        elif dy < -6:
            keys.add('w')
        world.update(DT, keys, (world.player.x + 100, world.player.y), False)
        trail.append((world.player.x, world.player.y,
                      world.camera.x, world.camera.y, world.room.id))
        if world.pending_door is not None:
            world.begin_crossing(world.pending_door)
            world.pending_door = None
    return trail


def case_walks_not_teleports():
    world = a_floor()
    start = world.room.id
    side = next(iter(world.room.doors))
    trail = walk_through(world, side)
    ended = world.room.id

    # The room did change...
    check('a doorway leads somewhere', ended != start,
          f'room {start} -> {ended}')

    # ...and nothing jumped on screen to get there.
    #
    # Measured against the camera, not against the world. The swap moves
    # every coordinate in play by the offset the neighbour was drawn at -
    # a room-sized step in the raw numbers, on purpose - and the whole point
    # is that the player's position *relative to the camera* does not move
    # at all when it happens. That difference is the trick, so it is the
    # thing worth testing.
    worst = 0.0
    at = -1
    biggest_rebase = 0.0
    for i in range(1, len(trail)):
        px, py, cx, cy, _ = trail[i]
        qx, qy, dx, dy, _ = trail[i - 1]
        step = math.hypot((px - cx) - (qx - dx), (py - cy) - (qy - dy))
        biggest_rebase = max(biggest_rebase, math.hypot(cx - dx, cy - dy))
        if step > worst:
            worst, at = step, i
    # A dash is the fastest the player legitimately moves: 900 px/s at a
    # 120 Hz step is 7.5 px in a frame. Twelve is generous and still an order
    # of magnitude below a room-sized jump.
    # What is being ruled out is a *cut* - a room-sized step in a single
    # frame. The camera legitimately pans while the framing widens from one
    # room to two, and that moves the world under a standing player on
    # purpose; twenty pixels at 120 Hz is a brisk pan, and a cut is fifty
    # times that.
    check('nothing cuts on screen', worst < 20.0,
          f'largest on-screen step {worst:.2f} px at frame {at}')
    check('the world does move under the player', biggest_rebase > 100.0,
          f'largest rebase {biggest_rebase:.0f} px - the swap happened')


def case_both_rooms_are_solid():
    world = a_floor()
    side = next(iter(world.room.doors))
    for _ in range(400):
        cx, cy = world.level.door_center(side)
        world.update(DT, set(), (cx, cy), False)
        world.player.x += (cx - world.player.x) * 0.25
        world.player.y += (cy - world.player.y) * 0.25
        if world.pending_door is not None:
            world.begin_crossing(world.pending_door)
            world.pending_door = None
            break
    check('a crossing starts', world.crossing is not None,
          f'crossing={world.crossing is not None}')
    if world.crossing is None:
        return
    solids = world.solids()
    check('both chambers collide', isinstance(solids, Span)
          and len(solids.parts) == 2,
          f'{type(solids).__name__} over '
          f'{len(getattr(solids, "parts", []))} chamber(s)')
    # And the light can see into both of them.
    merged = len(world.level.seg_ax)
    base = len(world.level.base_segments[0])
    check('the light sees into the next room', merged > base,
          f'{base} edges alone, {merged} while crossing')


def case_can_turn_back():
    """Walk in, turn round, walk out. The room you left is still there."""
    world = a_floor()
    first = world.room.id
    side = next(iter(world.room.doors))
    walk_through(world, side)
    second = world.room.id
    if second == first:
        check('you can walk back', False, 'never left the first room')
        return
    # Now back the way we came. The room sealed behind us if it was
    # hostile - which is correct, and not what this is testing - so break
    # the seal first and then leave the way we came in.
    back = fp.OPPOSITE[side]
    if back not in world.room.doors:
        check('you can walk back', False, f'no {back} door to return through')
        return
    if world.warded:
        world.enemies = []
        world.break_wards()
        for _ in range(120):
            world.update(DT, set(), (world.player.x, world.player.y), False)
    walk_through(world, back)
    check('you can walk back', world.room.id == first,
          f'{first} -> {second} -> {world.room.id}')


def main():
    for case in (case_walks_not_teleports, case_both_rooms_are_solid,
                 case_can_turn_back):
        try:
            case()
        except Exception as exc:                     # noqa: BLE001
            import traceback
            traceback.print_exc(file=sys.stderr)
            check(case.__name__, False, f'{type(exc).__name__}: {exc}')

    width = max(len(n) for n, _o, _d in RESULTS)
    bad = 0
    for name, ok, detail in RESULTS:
        if not ok:
            bad += 1
        print(f'  {" ok " if ok else "FAIL"}  {name:<{width}}  {detail}')
    print()
    print(f'{len(RESULTS) - bad} of {len(RESULTS)} checks passed')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
