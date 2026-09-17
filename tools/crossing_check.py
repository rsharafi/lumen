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

from lumen import art                               # noqa: E402
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
        _try_crossing(world)
    return trail


def _try_crossing(world):
    """Answer a flagged doorway the way the game does.

    `begin_crossing` has three answers, not two. None means the chamber
    behind that door has not been baked yet: the game leaves the flag set and
    asks again on the next frame rather than building it on this one, because
    at the display's own pixel density that is up to a second and a half with
    everything stopped. A harness that clears the flag on a None throws the
    crossing away.
    """
    side = world.pending_door
    if side is None:
        return None
    started = world.begin_crossing(side)
    if started is None:
        return None
    world.pending_door = None
    return started


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
    # A walking player covers 2.23 px in a frame at 120 Hz, and everything
    # above that is the camera moving on its own.
    #
    # This used to allow twenty, on the reasoning that a cut is a room-sized
    # jump and a pan is not a cut. That was true and it was not enough: the
    # framing was animated from one room's rectangle to two and back, and
    # while the frame is near the window's own size the camera is pinned to
    # it rather than to the player - so the collapse at the far end of a
    # crossing dragged the view at 15.8 px a frame, seven times a walk, and
    # every door in the game ended in a shove. Twenty passed it happily.
    #
    # The framing is not animated any more; what is eased is the correction
    # it implies, at a bounded speed. See `fx.Camera`. Measured over
    # forty-eight crossings the worst single frame is 5.6, so this is a real
    # bound on that and not a restatement of it.
    check('nothing cuts on screen', worst < 7.0,
          f'largest on-screen step {worst:.2f} px at frame {at}')
    check('the world does move under the player', biggest_rebase > 100.0,
          f'largest rebase {biggest_rebase:.0f} px - the swap happened')


def case_both_rooms_are_solid():
    world = a_floor()
    side = next(iter(world.room.doors))
    waited = 0
    for _ in range(400):
        cx, cy = world.level.door_center(side)
        world.update(DT, set(), (cx, cy), False)
        world.player.x += (cx - world.player.x) * 0.25
        world.player.y += (cy - world.player.y) * 0.25
        if world.pending_door is not None:
            if _try_crossing(world) is None:
                waited += 1
                continue
            break
    check('a crossing starts', world.crossing is not None,
          f'crossing={world.crossing is not None}'
          + (f', after {waited} frames waiting on the chamber' if waited else ''))
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


def case_no_frame_builds_a_chamber():
    """Walking through a door must not bake anything on the frame it happens.

    This is the one that used to stop the game dead. Generating and baking a
    chamber is 80-200 ms at the design size and between half a second and a
    second and a half at a retina display's own pixel density, and it was
    being done inline, on the frame the player stepped into a doorway. The
    builder's worker is the only thing allowed to do it now; the doorway
    waits for the worker instead of doing its job for it.

    Measured against the builder's own count of how often it was made to
    build something in a hurry, which is exactly the number this exists to
    hold at zero - plus the wall clock, because a frame is a frame however
    the work got onto it.
    """
    import time
    art.set_scale(2.0)                     # a retina window, where it hurt
    try:
        world = a_floor(seed=31, depth=7)
        # One built inline, on purpose: the floor's entrance, behind the
        # fade, before the player is anywhere.
        base = world.builder.waits
        worst = 0.0
        for side in list(world.room.doors):
            if side not in world.level.doors:
                continue
            world.player.x, world.player.y = world.level.door_entry(side, 2.4)
            world.player.vx = world.player.vy = 0.0
            world.door_lock = 0.0
            if world.warded:
                world.enemies = []
                world.break_wards()
            heading = {'n': (0.0, -1.0), 's': (0.0, 1.0),
                       'w': (-1.0, 0.0), 'e': (1.0, 0.0)}[side]
            for _ in range(1500):
                goal = (world.player.x + heading[0] * 400.0,
                        world.player.y + heading[1] * 400.0)
                keys = set()
                if goal[0] - world.player.x > 6:
                    keys.add('d')
                elif goal[0] - world.player.x < -6:
                    keys.add('a')
                if goal[1] - world.player.y > 6:
                    keys.add('s')
                elif goal[1] - world.player.y < -6:
                    keys.add('w')
                t0 = time.perf_counter()
                world.update(DT, keys, (world.player.x + 100,
                                        world.player.y), False)
                worst = max(worst, (time.perf_counter() - t0) * 1000.0)
                _try_crossing(world)
                if world.crossing is None and world.room.id != world.plan.entrance:
                    break
            break
        check('a doorway never bakes on the frame', world.builder.waits == base,
              f'{world.builder.waits - base} chamber(s) built inline')
        # Generous, because this is a bound on a class of stall and not a
        # frame-time budget: the freeze it rules out was 340 ms at the design
        # size and over three seconds at a retina one.
        check('no frame stops the game', worst < 60.0,
              f'slowest single update {worst:.1f} ms')
    finally:
        art.set_scale(1.0)


def main():
    for case in (case_walks_not_teleports, case_both_rooms_are_solid,
                 case_can_turn_back, case_no_frame_builds_a_chamber):
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
