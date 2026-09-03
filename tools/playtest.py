"""Drive the real game headlessly with a scripted input sequence.

    python tools/playtest.py --scenario combat --out shots/combat.png

Scenarios are ordinary Python lists of (frame, action) pairs, so anything the
player can do can be reproduced deterministically for a screenshot or a
performance measurement.
"""

import argparse
import math
import os
import statistics
import sys
import time

os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
os.environ.setdefault('CI', '1')
# There is one renderer now, and it needs a real GL context - so this opens a
# real window and measures the renderer the game actually ships on, rather
# than a rasteriser nobody plays through. The window is small and short-lived
# and every run is still deterministic: fixed timestep, pinned seeds.
os.environ.setdefault('LUMEN_FIXED_DT', '1')
os.environ.setdefault('LUMEN_NO_POINTER', '1')
os.environ.setdefault(
    'LUMEN_SAVE',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '.playtest_save.json'))
for _i, _a in enumerate(sys.argv):
    if _a == '--seed' and _i + 1 < len(sys.argv):
        os.environ['LUMEN_SEED'] = sys.argv[_i + 1]
    if _a == '--floor' and _i + 1 < len(sys.argv):
        os.environ['LUMEN_START_FLOOR'] = sys.argv[_i + 1]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

parser = argparse.ArgumentParser()
parser.add_argument('--scenario', default='title')
parser.add_argument('--out', default='')
parser.add_argument('--frames', type=int, default=0)
parser.add_argument('--seed', type=int, default=1234)
parser.add_argument('--perf', action='store_true')
parser.add_argument('--auto', action='store_true',
                    help='let the built-in autopilot play')
parser.add_argument('--floor', type=int, default=0,
                    help='start the run on this floor')
parser.add_argument('--stop-at', default='',
                    help='stop as soon as the game reaches this state')
parser.add_argument('--settle', type=int, default=0,
                    help='extra frames to run after --stop-at matches')
parser.add_argument('--embers', type=int, default=0,
                    help='seed the purse, so the shelf can be read')
parser.add_argument('--room', default='',
                    help='jump straight to a room of this kind, if the '
                         'floor has one (cache, hearth, shrine, elite, ...)')
parser.add_argument('--explore', action='store_true',
                    help='clear every room rather than heading for the way down')
parser.add_argument('--click-at', default='',
                    help='STATE:X:Y - click once at X,Y when STATE is reached')
ARGS = parser.parse_args()

from lumen import gpu, host, rng  # noqa: E402
from lumen.app import Game  # noqa: E402
from lumen.config import HEIGHT, WIDTH  # noqa: E402

GAME = Game()


def press(key):
    return ('press', key)


def release(key):
    return ('release', key)


def mouse(x, y):
    return ('mouse', x, y)


def click():
    """A real press+release through the game's handlers, at the current pointer."""
    return ('click',)


def cycle():
    """Step the render resolution, exactly as the DISPLAY menu item does."""
    return ('cycle',)


def braziers(walk=True):
    """Light every brazier on the floor, and stand on one.

    Walking into a brazier is the only way to light one in play, and no
    scenario here ever happened to do it - which is how a lit brazier came to
    reference two constants that had been deleted, and crash the frame it
    first appeared in. Lighting them on demand puts that path in the suite.
    """
    return ('braziers', walk)


def tap(key, frame, hold=2):
    return [(frame, press(key)), (frame + hold, release(key))]


def flatten(*groups):
    out = []
    for g in groups:
        out.extend(g)
    return out


def row(screen, index):
    """Click row `index` of a screen, using its own hit rectangles.

    Menus are pointer-driven now, so a harness that presses keys at them is
    testing nothing. Resolved at dispatch time rather than when the scenario
    is written, because the rectangles only exist once the screen has drawn
    itself once.
    """
    return ('row', screen, index)


def menu(index):
    """Click row `index` of the title menu."""
    return row('title', index)


def park():
    """Move the pointer off the menu before driving it from the keyboard.

    `_hover` sets the selection from whatever the pointer is over, every
    frame, so a harness whose mouse defaults to the top of the screen pins the
    menu to its first item and every keyboard press after that does nothing.
    Real players hit this too, if more forgivingly: nudge the mouse and the
    keyboard selection jumps back under the cursor.
    """
    return [(0, mouse(12, 12))]


def start_run(at=4):
    """Title -> DESCEND."""
    return [(at, menu(0))]


def wander(start, end, step=26):
    """Alternate movement keys so the player actually explores."""
    events = []
    keys = ['d', 's', 'a', 'w']
    f = start
    i = 0
    while f < end:
        k = keys[i % 4]
        events.append((f, press(k)))
        events.append((min(end, f + step - 2), release(k)))
        f += step
        i += 1
    return events


SCENARIOS = {
    'title': ([], 40),
    # Resolution changes empty the sprite cache: anything holding a baked
    # sprite has to rebuild it, or the next frame draws a released husk.
    'resize-title': ([(6, cycle()), (14, cycle()), (22, cycle()),
                      (30, cycle())], 40),
    'resize-help': (flatten(park(), [(6, menu(3))],
                            [(18, cycle()), (26, cycle()), (34, cycle())]),
                    44),
    'resize-play': (flatten(start_run(4), wander(24, 120),
                            [(40, cycle()), (70, cycle()), (100, cycle())]),
                    130),
    'help': (flatten(park(), [(6, menu(3))]), 40),
    # The Vigil: open it, walk the ledger, try to buy the top line.
    'vigil': (flatten(park(), [(6, menu(1)), (20, row('vigil', 2)),
                               (30, row('vigil', 7))]), 60),
    'vigil-buy': (flatten(park(), [(6, menu(1)), (20, row('vigil', 0)),
                                   (30, row('vigil', 0))],
                          tap('escape', 46)), 60),
    'settings': (flatten(park(), [(6, menu(2)), (18, row('settings', 3)),
                                  (26, row('settings', 3)),
                                  (34, row('settings', 2))]), 52),
    'help2': (flatten(park(), [(6, menu(3))], tap('right', 22)), 46),
    'help3': (flatten(park(), [(6, menu(3))], tap('right', 22),
                      tap('right', 34)), 58),
    'firstframe': (start_run(4), 30),
    'combat': (flatten(
        start_run(4),
        wander(24, 150),
        [(60, mouse(880, 300)), (62, ('down',)), (150, ('up',))],
        [(150, mouse(700, 250))],
    ), 190),
    'flare': (flatten(
        start_run(4),
        wander(24, 120),
        [(120, press('l')), (123, release('l'))],
    ), 132),
    'dash': (flatten(
        start_run(4),
        [(30, press('d'))],
        [(60, press('space')), (63, release('space'))],
    ), 76),
    # A lit brazier draws light onto every wall edge within reach of it,
    # through the same path the lantern uses; `far` leaves the player across
    # the floor from them so the off-screen cull is exercised too.
    'brazier': (flatten(start_run(4), wander(24, 90),
                        [(90, braziers())]), 150),
    'brazier-far': (flatten(start_run(4), [(30, braziers(walk=False))],
                            wander(40, 200)), 220),
    'brazier-resize': (flatten(start_run(4), [(30, braziers())],
                               [(50, cycle()), (80, cycle())]), 110),
    'lowfuel': (start_run(4), 900),
    'draft': (flatten(
        start_run(4),
        wander(24, 1400),
        [(30, mouse(900, 360)), (32, ('down',))],
    ), 1500),
    'deep': (flatten(
        start_run(4),
        wander(24, 3000),
        [(30, mouse(900, 360)), (32, ('down',))],
        [(f, press('enter')) for f in range(200, 3000, 40)],
        [(f + 3, release('enter')) for f in range(200, 3000, 40)],
    ), 3000),
    'weapon2': (flatten(
        start_run(4),
        tap('2', 20),
        [(30, mouse(900, 360)), (32, ('down',)), (200, ('up',))],
    ), 210),
    'weapon3': (flatten(
        start_run(4),
        tap('3', 20),
        [(30, mouse(900, 360)), (40, ('down',)), (110, ('up',)),
         (140, ('down',)), (200, ('up',))],
    ), 220),
    # Mouse-driven navigation: hover an entry, then click it.
    'mouse_menu': (flatten(
        [(6, mouse(640, 449)), (14, click())],
    ), 40),
    'mouse_display': (flatten(
        [(6, mouse(640, 487)), (14, click())],
    ), 40),
    'mouse_draft': (flatten(
        start_run(4),
        wander(24, 1400),
        [(30, mouse(900, 360)), (32, ('down',))],
    ), 1500),
    'pause': (flatten(
        start_run(4),
        wander(20, 60),
        tap('escape', 70),
    ), 100),
    'victory': (flatten(
        start_run(4),
        [(40, ('win',))],
    ), 200),
    'death': (flatten(
        start_run(4),
        [(30, mouse(640, 360))],
    ), 2600),
}

script, default_frames = SCENARIOS[ARGS.scenario]
FRAMES = ARGS.frames or default_frames

TIMELINE = {}
for frame, action in script:
    TIMELINE.setdefault(frame, []).append(action)

STATE = {'n': 0, 'draw_ms': [], 'step_ms': [], 'held': set(),
         'floors': set(), 'deaths': 0, 'peak': {}, 'loop_ms': [],
         'last_loop': None}


def _hold(app, key, want):
    """Press or release a key so its held state matches `want`."""
    held = STATE['held']
    if want and key not in held:
        GAME.key_press(app, key)
        held.add(key)
    elif not want and key in held:
        GAME.key_release(app, key)
        held.discard(key)


def click_row(app, which, index):
    """Click row `index` of a screen, through its own hit rectangles.

    Every menu in the game is the pointer's, so this is the only way to
    choose on one - the harness has no keyboard path to fall back on and
    should not grow one. Returns True if a row was actually hit; the
    rectangles do not exist until the screen has drawn itself once, so an
    early call is a miss rather than an error.
    """
    screen = {'title': GAME.title_screen,
              'draft': GAME.draft_screen,
              'shop': GAME.shop_screen,
              'vigil': GAME.vigil_screen,
              'settings': GAME.settings_screen}.get(which)
    rects = getattr(screen, 'hit_rects', None) if screen else None
    if not rects or not (0 <= index < len(rects)):
        return False
    x, y, rw, rh, _i = rects[index]
    dx, dy = x + rw * 0.5, y + rh * 0.5
    from lumen import runtime as _rt
    k = _rt.pointer_scale() / GAME.scale
    GAME.mouse = (dx, dy)
    GAME.mouse_press(app, dx / k, dy / k, 0)
    GAME.mouse_release(app, dx / k, dy / k, 0)
    return True


def _route_side(world, want):
    """The door to take from this room to get nearer a room matching `want`.

    A breadth-first search over the floor graph, not the tiles - the tile
    pathing inside a room is `_bot_step`'s job and the two should not be
    confused. Returns None when nothing matching is reachable.
    """
    from collections import deque
    plan, room = world.plan, world.room
    if plan is None or room is None:
        return None
    came = {room.id: (None, None)}
    q = deque([room.id])
    found = None
    while q:
        rid = q.popleft()
        here = plan.rooms[rid]
        if rid != room.id and want(here):
            found = rid
            break
        for side, other in here.doors.items():
            if other not in came:
                came[other] = (rid, side)
                q.append(other)
    if found is None:
        return None
    # Walk the chain back to the step taken out of the room we are in.
    rid = found
    while came[rid][0] is not None and came[rid][0] != room.id:
        rid = came[rid][0]
    return came[rid][1]


def _floor_goal(world):
    """Where the bot wants to be, as a world point in the current room.

    Nothing to fight and nothing to take means it is time to leave, so this
    resolves to a doorway - or, in the room with the way down in it, to the
    way down.
    """
    from lumen import floorplan as fp
    room = world.room
    if room is None:
        return None
    if world.warded:
        return None

    side = None
    if ARGS.explore:
        # Everything else first. Standing on the rift ends the floor, so a
        # bot that takes it the moment it sees it is not exploring, it is
        # leaving through the first room that lets it.
        #
        # A door straight into somewhere new beats a route to somewhere new,
        # and the difference is not cosmetic: routing alone let the bot walk
        # A to B, decide the nearest unvisited room was back through A, walk
        # back, and repeat - nineteen door crossings and two rooms seen in
        # seven thousand frames. Taking an unvisited neighbour when there is
        # one makes exploration monotonic.
        for candidate, other in world.room.doors.items():
            if (not world.plan.rooms[other].visited
                    and candidate in world.level.doors):
                side = candidate
                break
        if side is None:
            side = _route_side(world, lambda r: not r.visited)
        if side is None and world.rift is not None:
            return (world.rift.x, world.rift.y)
    elif world.rift is not None:
        return (world.rift.x, world.rift.y)
    if side is None:
        side = _route_side(world, lambda r: r.kind == fp.DESCENT)
    if side is None:
        side = _route_side(world, lambda r: not r.visited)
    if side is None or side not in world.level.doors:
        return None
    return world.level.door_center(side)


def _bot_step(world, player, goal):
    """A unit vector one tile downhill toward `goal`, or None if it is moot.

    None means "walk straight at it": the goal is in this tile already, or
    nothing open connects to it. Both are cases where the caller's own
    straight line is as good an answer as any.
    """
    from lumen import flow as flow_mod
    field = STATE.get('bot_flow')
    if field is None or field.level is not world.level:
        field = flow_mod.FlowField(world.level)
        STATE['bot_flow'] = field
        STATE['bot_goal'] = None
    tile = (int(goal[0] // 64), int(goal[1] // 64))
    if STATE.get('bot_goal') != tile:
        STATE['bot_goal'] = tile
        field.rebuild(goal[0], goal[1])
    return field.direction_at(player.x, player.y)


def jump_to_room(kind):
    """Stand the player in the first room of `kind` on this floor.

    Reward rooms are off the critical path by design, so a bot walking the
    floor may not reach one for a thousand frames - which is a long way to
    go to look at a shrine.
    """
    world = GAME.world
    if world is None or world.plan is None:
        return False
    for room in world.plan.rooms.values():
        if room.kind == kind:
            world.enter_room(room)
            if ARGS.embers:
                world.embers = ARGS.embers
            # A shop room is worth nothing to look at with the Ferryman
            # still waiting to be walked into, so open the shelf too.
            if kind == 'shop':
                GAME.open_shop()
            return True
    return False


def autopilot(app):
    """A crude bot: aim at the nearest threat, keep away from it, and take
    every prompt. Enough to drive a whole run end to end for testing."""
    from lumen import app as app_mod

    state = GAME.state
    n = STATE['n']

    if ARGS.stop_at and state == ARGS.stop_at:
        return
    if state in (app_mod.TITLE, app_mod.ENDED):
        if n % 24 == 0:
            GAME.key_press(app, 'enter')
            GAME.key_release(app, 'enter')
        return
    if state == app_mod.SHOP:
        # Buy whatever is affordable, cheapest first, then leave. A bot that
        # bought nothing would never exercise the purchase paths, and one
        # that never left would end the run standing at a shelf.
        if n % 14 == 0:
            slots = GAME.shop_screen.slots
            embers = GAME.world.embers
            can = [i for i, sl in enumerate(slots)
                   if not sl.sold and sl.price <= embers]
            if can:
                can.sort(key=lambda i: slots[i].price)
                STATE['bought'] = STATE.get('bought', 0) + 1
                click_row(app, 'shop', can[0])
            else:
                GAME.key_press(app, 'escape')
                GAME.key_release(app, 'escape')
        return
    if state == app_mod.DRAFT:
        # The offering is chosen with the pointer and nothing else - pressing
        # enter at it does nothing, which is how the bot came to sit on floor
        # one forever. Give it a frame to lay its cards out, then take one.
        if n % 18 == 0:
            STATE['drafts'] = STATE.get('drafts', 0) + 1
            click_row(app, 'draft', STATE['drafts'] % 3)
        return
    if state != app_mod.PLAYING:
        return

    world = GAME.world
    if world is None:
        return
    if world.depth not in STATE['floors']:
        STATE['floors'].add(world.depth)
        STATE.setdefault('floor_frames', []).append((world.depth, STATE['n']))
    if world.rooms_entered != STATE.get('_last_entered'):
        STATE['_last_entered'] = world.rooms_entered
        STATE['rooms_entered'] = STATE.get('rooms_entered', 0) + 1
    player = world.player
    cam = world.camera

    target = None
    best = 1e18
    for e in world.enemies:
        if not e.alive:
            continue
        d2 = (e.x - player.x) ** 2 + (e.y - player.y) ** 2
        if d2 < best:
            best = d2
            target = e

    goal = None
    if target is not None:
        GAME.mouse = (target.x - cam.ox, target.y - cam.oy)
        GAME.mouse_down = True
        dist = math.sqrt(best)
        blocked = world.level.ray_blocked(player.x, player.y, target.x, target.y)
        if blocked:
            goal = (target.x, target.y)
        elif dist < 190:
            goal = (player.x - (target.x - player.x),
                    player.y - (target.y - player.y))
        elif dist > 420:
            goal = (target.x, target.y)
    else:
        GAME.mouse_down = False
        goal = _floor_goal(world)

    if goal is None:
        goal = (player.x, player.y)

    # Refuel: head for the nearest brazier when the lantern is failing,
    # preferring an unlit one (lighting it is worth a big one-off top-up).
    if player.fuel < player.fuel_max * 0.45 and world.level.braziers:
        best_b = min(world.level.braziers,
                     key=lambda b: ((b.x - player.x) ** 2 + (b.y - player.y) ** 2)
                     * (0.5 if not b.lit else 1.0))
        goal = (best_b.x, best_b.y)

    # Leaving always wins once the room is clear - the way down if this is
    # the room that has one, the right doorway otherwise.
    if target is None:
        onward = _floor_goal(world)
        if onward is not None:
            goal = onward

    # Walk there, around the stone rather than into it. Steering straight at
    # the goal is what left the bot pressed against a pillar for nineteen
    # thousand frames with the rift open on the far side of it: the world's
    # own flow field is built toward the *player*, so it cannot route the
    # player anywhere. This is a second field, owned by the harness and
    # aimed at wherever the bot currently wants to be. A rebuild is a BFS
    # over one chamber - well under a millisecond - and it only happens when
    # the goal crosses a tile.
    step = _bot_step(world, player, goal)
    if step is None:
        dx = goal[0] - player.x
        dy = goal[1] - player.y
    else:
        dx, dy = step[0] * 64.0, step[1] * 64.0
    _hold(app, 'd', dx > 8)
    _hold(app, 'a', dx < -8)
    _hold(app, 's', dy > 8)
    _hold(app, 'w', dy < -8)

    if target is not None and best < 90 ** 2 and n % 40 == 0:
        GAME.key_press(app, 'space')
        GAME.key_release(app, 'space')
    if target is not None and best < 150 ** 2 and n % 150 == 0:
        GAME.key_press(app, 'l')
        GAME.key_release(app, 'l')


def onAppStart(app):
    GAME.start(app)
    rng.world.reseed(ARGS.seed)
    rng.fx.reseed(ARGS.seed + 1)


def onStep(app):
    n = STATE['n']
    if ARGS.room and n == 8 and not STATE.get('jumped'):
        STATE['jumped'] = jump_to_room(ARGS.room)
        if not STATE['jumped']:
            sys.stderr.write(
                f'[playtest] no {ARGS.room} room on this floor\n')
    if ARGS.auto:
        autopilot(app)
    for action in TIMELINE.get(n, ()):
        kind = action[0]
        if kind == 'press':
            GAME.key_press(app, action[1])
        elif kind == 'release':
            GAME.key_release(app, action[1])
        elif kind == 'mouse':
            GAME.mouse = (action[1], action[2])
        elif kind == 'down':
            GAME.mouse_down = True
        elif kind == 'up':
            GAME.mouse_down = False
        elif kind == 'click':
            px = GAME.mouse[0] * GAME.scale
            py = GAME.mouse[1] * GAME.scale
            GAME.mouse_press(app, px, py, 0)
            GAME.mouse_release(app, px, py, 0)
        elif kind == 'win':
            GAME.transition(lambda: GAME.finish_run(won=True))
        elif kind == 'cycle':
            GAME.cycle_display(GAME._app_ref)
        elif kind == 'row':
            click_row(app, action[1], action[2])
        elif kind == 'braziers':
            world = getattr(GAME, 'world', None)
            lit = 0
            for b in getattr(world, 'level', None).braziers if world else ():
                b.lit = True
                b.ignite_t = 0.35
                b.edges = None
                lit += 1
            if action[1] and lit:
                first = world.level.braziers[0]
                world.player.x, world.player.y = first.x, first.y
            STATE['braziers'] = lit
            sys.stderr.write(f'[playtest] lit {lit} brazier(s)\n')

    t0 = time.perf_counter()
    try:
        GAME.step(app)
    except Exception:
        import traceback
        traceback.print_exc(file=sys.stderr)
        os._exit(3)
    STATE['step_ms'].append((time.perf_counter() - t0) * 1000.0)
    if ARGS.click_at and not STATE.get('clicked'):
        want, cx, cy = ARGS.click_at.split(':')
        if GAME.state == want:
            STATE['clicked'] = True
            GAME.mouse = (float(cx), float(cy))
            GAME.mouse_press(app, float(cx) * GAME.scale,
                             float(cy) * GAME.scale, 0)
            GAME.mouse_release(app, float(cx) * GAME.scale,
                               float(cy) * GAME.scale, 0)

    STATE['n'] = n + 1

    if ARGS.stop_at and STATE.get('stop_frame') is None:
        if GAME.state == ARGS.stop_at:
            STATE['stop_frame'] = STATE['n'] + ARGS.settle
    if STATE.get('stop_frame') is not None and STATE['n'] >= STATE['stop_frame']:
        STATE['n'] = FRAMES

    if STATE['n'] >= FRAMES:
        if ARGS.out:
            os.makedirs(os.path.dirname(os.path.abspath(ARGS.out)), exist_ok=True)
            app.getScreenshot(ARGS.out)
            sys.stderr.write(f'wrote {ARGS.out}\n')
        report(app)
        app.quit()


def report(app):
    draws = STATE['draw_ms'][10:]
    steps = STATE['step_ms'][10:]
    world = GAME.world
    info = [f'scenario={ARGS.scenario}', f'frames={STATE["n"]}',
            f'state={GAME.state}']
    if STATE['floors']:
        info.append(f'floors_seen={sorted(STATE["floors"])}')
    if world is not None:
        info.append(f'floor={world.depth}')
        info.append(f'enemies={len([e for e in world.enemies if e.alive])}')
        info.append(f'score={world.score}')
        info.append(f'kills={world.kills}')
        info.append(f'dmg={world.player.damage_dealt:.0f}')
        info.append(f'shots={getattr(world.player, "shots_fired", -1)}')
        info.append(f'hp={world.player.hp:.0f}')
        info.append(f'fuel={world.player.fuel:.0f}')
        if world.plan is not None:
            plan = world.plan
            info.append(f'room={world.room_kind}')
            info.append(f'rooms={sum(1 for r in plan.rooms.values() if r.visited)}'
                        f'/{len(plan.rooms)}')
            info.append(f'entered={STATE.get("rooms_entered", 0)}')
            info.append(f'embers={world.embers}')
            info.append(f'bought={STATE.get("bought", 0)}')
        if world.builder is not None:
            # How often a doorway had to be answered by building the room
            # behind it there and then. Every one of these is a stall the
            # player sees, so it is the number the bake-ahead exists to keep
            # at zero.
            info.append(f'bake_waits={world.builder.waits}'
                        f'@{world.builder.waited_ms:.0f}ms')
            info.append(f'held={len(world.builder._built)}'
                        f'/built{world.builder.built_count}')
    if draws:
        info.append(f'draw_med={statistics.median(draws):.2f}ms')
        info.append(f'draw_p95={sorted(draws)[int(len(draws) * 0.95)]:.2f}ms')
        info.append(f'draw_max={max(draws):.2f}ms')
    loops = STATE['loop_ms'][10:]
    if loops:
        ordered = sorted(loops)
        info.append(f'FRAME_med={statistics.median(ordered):.2f}ms')
        info.append(f'FRAME_p95={ordered[int(len(ordered) * 0.95)]:.2f}ms')
        info.append('pct=' + '/'.join(
            f'{ordered[int(len(ordered) * q)]:.1f}'
            for q in (0.5, 0.75, 0.9, 0.99)))
        info.append(f'over8.3ms={sum(1 for v in ordered if v > 8.33)}'
                    f'/{len(ordered)}')
        info.append(f'fps_med={1000.0 / max(statistics.median(ordered), 1e-6):.0f}')
    if steps:
        info.append(f'step_med={statistics.median(steps):.2f}ms')
        info.append(f'step_max={max(steps):.2f}ms')
    shapes = STATE.get('shapes', [])[10:]
    if shapes:
        info.append(f'shapes_med={statistics.median(shapes):.0f}')
        info.append(f'shapes_max={max(shapes)}')
    if STATE.get('floor_frames'):
        info.append('floor_at=' + ','.join(f'{d}@{f}' for d, f in
                                          STATE['floor_frames']))
    if STATE['peak']:
        info.append('peak=' + ','.join(f'{k}:{v}' for k, v in
                                       sorted(STATE['peak'].items())))
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # ru_maxrss is bytes on macOS and kilobytes on Linux. There is no
        # portable flag for it; the platform is the flag.
        div = 1024.0 * 1024.0 if sys.platform == 'darwin' else 1024.0
        info.append(f'peak_rss={peak / div:.0f}MB')
    except Exception:
        pass
    info.append(f'fade={GAME.fade:.2f}')
    if world is not None and getattr(world, 'draw_marks', None):
        parts = sorted(world.draw_marks.items(), key=lambda kv: -kv[1][0])
        info.append('DRAW=' + ','.join(
            f'{k}:{v[0] / max(1, v[1]):.2f}' for k, v in parts if k))
    if draws:
        worst = sorted(range(len(draws)), key=lambda i: -draws[i])[:4]
        info.append('slow_frames=' + ','.join(
            f'{i + 10}:{draws[i]:.1f}' for i in worst))
    sys.stderr.write('  '.join(info) + '\n')


def onKeyPress(app, key):
    pass


def redrawAll(app):
    t0 = time.perf_counter()
    # Full loop period, present included: the frame the player would see.
    prev = STATE['last_loop']
    if prev is not None:
        STATE['loop_ms'].append((t0 - prev) * 1000.0)
    STATE['last_loop'] = t0
    try:
        if os.environ.get('LUMEN_TRACE_ART'):
            sys.stderr.write(f'[frame] {STATE["n"]}\n')
        GAME.draw(app)
        _w = GAME.world
        # LUMEN_TRACE_FRAMES=180-200 dumps per-frame counters for a range,
        # which is how the mid-frame rasterisation stalls were tracked down.
        if _w is not None and os.environ.get('LUMEN_TRACE_FRAMES'):
            lo, hi = (int(v) for v in
                      os.environ['LUMEN_TRACE_FRAMES'].split('-'))
            if lo <= STATE['n'] <= hi:
                sys.stderr.write(
                    f"[f{STATE['n']}] draw={STATE['draw_ms'][-1] if STATE['draw_ms'] else 0:.2f} "
                    f"shapes={STATE['shapes'][-1]} part={_w.particles.live} "
                    f"en={len([e for e in _w.enemies if e.alive])} "
                    f"proj={len([q for q in _w.projectiles.pool if q.alive])} "
                    f"wedge={_w.last_wedges} edge={_w.last_edges} "
                    f"lights={len(_w.effects.lights)} texts={len(_w.effects.texts)} "
                    f"pick={len(_w.pickups.items)}\n")
        if _w is not None:
            pk = STATE['peak']
            pk['wedges'] = max(pk.get('wedges', 0), _w.last_wedges)
            pk['edges'] = max(pk.get('edges', 0), _w.last_edges)
            pk['particles'] = max(pk.get('particles', 0), _w.particles.live)
            pk['enemies'] = max(pk.get('enemies', 0),
                                len([e for e in _w.enemies if e.alive]))
            pk['proj'] = max(pk.get('proj', 0),
                             len([q for q in _w.projectiles.pool if q.alive]))
    except Exception:
        import traceback
        traceback.print_exc(file=sys.stderr)
        os._exit(3)
    STATE['draw_ms'].append((time.perf_counter() - t0) * 1000.0)


def _size():
    """Render size for this run; LUMEN_WINDOW=3600x2338 overrides the default."""
    override = os.environ.get('LUMEN_WINDOW')
    if override:
        w, h = override.lower().split('x')
        return int(w), int(h)
    return WIDTH, HEIGHT


def drive():
    """The loop, in place of the framework's.

    This used to be four callbacks handed to `runApp`. The framework that
    called them is gone, so the harness owns its own loop - which is the same
    arrangement `lumen/host.py` uses, minus the input pumping and the
    wall-clock pacing. Frames advance one per iteration at a fixed step, so a
    run of N frames is exactly N frames of simulation on any machine.
    """
    import pygame

    pygame.init()
    app = host.NativeApp(*_size(), title='LUMEN playtest')
    onAppStart(app)
    GAME.ensure_display(app)
    if not gpu.active():
        sys.stderr.write('[playtest] no GL context; cannot run\n')
        return 3
    if os.environ.get('LUMEN_DEBUG'):
        from lumen import draw as _d
        sys.stderr.write(f'[playtest] GAME.scale={GAME.scale} draw.SCALE={_d.SCALE} '
                         f'design={GAME.width}x{GAME.height} '
                         f'render={__import__("lumen.runtime", fromlist=["x"]).render_size()}\n')
    # Keep the window from stealing focus for the whole run where the platform
    # allows it; the harness never wants the pointer or the keyboard.
    while app._running:
        pygame.event.pump()
        onStep(app)
        if not app._running:
            break
        gpu.begin_frame(host._background_rgb(app))
        redrawAll(app)
        gpu.present()
    pygame.quit()
    return 0


sys.exit(drive())
