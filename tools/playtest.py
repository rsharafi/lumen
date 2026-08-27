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

os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
os.environ.setdefault('CI', '1')
os.environ.setdefault('LUMEN_HEADLESS', '1')
# The GPU backend needs a real accelerated renderer, which the dummy video
# driver cannot give it, so headless runs measure the cmu-graphics renderer.
# A/B the two with real windows instead - see tools/compare_backends.py.
os.environ.setdefault('LUMEN_RENDERER', 'cpu')
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
parser.add_argument('--click-at', default='',
                    help='STATE:X:Y - click once at X,Y when STATE is reached')
ARGS = parser.parse_args()

import cmu_graphics.cmu_graphics as _cg  # noqa: E402
from cmu_graphics import app, runApp  # noqa: E402,F401

from lumen import rng  # noqa: E402
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


def tap(key, frame, hold=2):
    return [(frame, press(key)), (frame + hold, release(key))]


def flatten(*groups):
    out = []
    for g in groups:
        out.extend(g)
    return out


def start_run(at=4):
    """Title -> DESCEND."""
    return tap('enter', at)


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
    'resize-help': (flatten(tap('down', 4), tap('enter', 10),
                            [(18, cycle()), (26, cycle()), (34, cycle())]),
                    44),
    'resize-play': (flatten(start_run(4), wander(24, 120),
                            [(40, cycle()), (70, cycle()), (100, cycle())]),
                    130),
    'help': (flatten(tap('down', 4), tap('enter', 10)), 40),
    'help2': (flatten(tap('down', 4), tap('enter', 10), tap('right', 22)), 46),
    'help3': (flatten(tap('down', 4), tap('enter', 10), tap('right', 22),
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
    if state == app_mod.DRAFT:
        if n % 18 == 0:
            GAME.key_press(app, 'enter')
            GAME.key_release(app, 'enter')
        return
    if state != app_mod.PLAYING:
        return

    world = GAME.world
    if world is None:
        return
    if world.depth not in STATE['floors']:
        STATE['floors'].add(world.depth)
        STATE.setdefault('floor_frames', []).append((world.depth, STATE['n']))
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
        if world.rift is not None:
            goal = (world.rift.x, world.rift.y)

    if goal is None:
        goal = (player.x, player.y)

    # Refuel: head for the nearest brazier when the lantern is failing,
    # preferring an unlit one (lighting it is worth a big one-off top-up).
    if player.fuel < player.fuel_max * 0.45 and world.level.braziers:
        best_b = min(world.level.braziers,
                     key=lambda b: ((b.x - player.x) ** 2 + (b.y - player.y) ** 2)
                     * (0.5 if not b.lit else 1.0))
        goal = (best_b.x, best_b.y)

    # The rift always wins once the room is clear.
    if world.rift is not None and target is None:
        goal = (world.rift.x, world.rift.y)

    dx = goal[0] - player.x
    dy = goal[1] - player.y
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
            app._app.getScreenshot(ARGS.out)
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
    if draws:
        worst = sorted(range(len(draws)), key=lambda i: -draws[i])[:4]
        info.append('slow_frames=' + ','.join(
            f'{i + 10}:{draws[i]:.1f}' for i in worst))
    sys.stderr.write('  '.join(info) + '\n')


def onKeyPress(app, key):
    pass


def redrawAll(app):
    STATE['shapes_before'] = _cg.SHAPES_CREATED
    t0 = time.perf_counter()
    # Full loop period: our shape construction plus the framework's own
    # rasterise/convert/blit, which happens after this callback returns.
    prev = STATE['last_loop']
    if prev is not None:
        STATE['loop_ms'].append((t0 - prev) * 1000.0)
    STATE['last_loop'] = t0
    try:
        if os.environ.get('LUMEN_TRACE_ART'):
            sys.stderr.write(f'[frame] {STATE["n"]}\n')
        GAME.draw(app)
        STATE.setdefault('shapes', []).append(
            _cg.SHAPES_CREATED - STATE['shapes_before'])
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


runApp(*_size())
