"""Draw the GPU path under the native host, including the cases that crash it.

`tools/playtest.py` runs the game inside cmu-graphics' loop, and its renderer
takeover only ever reaches the SDL backend there. So every branch guarded by
`gpu.active()` on the OpenGL backend - which is most of the lighting - was
never drawn by the suite at all. A lit brazier went on referencing two
constants that had been deleted for weeks, and the first thing to notice was
a player walking into one.

This runs the same `Game` through `lumen/host.py`, on whichever backend is
asked for, and puts the frames the suite could not reach on screen:

    python tools/gpu_smoke.py --renderer gl
    python tools/gpu_smoke.py --renderer sdl

It fails loudly. Any exception from a draw ends the run with a non-zero exit
and a traceback, rather than the silent skipped frame cmu-graphics gives.
"""

import argparse
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

parser = argparse.ArgumentParser()
parser.add_argument('--renderer', default='gl', choices=('gl', 'sdl', 'gpu'))
parser.add_argument('--frames', type=int, default=40,
                    help='frames to draw per stage')
parser.add_argument('--quiet', action='store_true')
ARGS = parser.parse_args()

# Must be set before `lumen.gpu` binds its backend at import.
os.environ['LUMEN_RENDERER'] = ARGS.renderer
os.environ.setdefault('LUMEN_SAVE', '/tmp/lumen-gpu-smoke.json')
os.environ.setdefault('CI', '1')

import pygame                                          # noqa: E402
from lumen import gpu, host, palette                   # noqa: E402
from lumen.app import Game                             # noqa: E402

STAGES = []


def click_row(game, app, screen, index):
    """Press a menu row through its own hit rectangle.

    Menus are pointer-driven, so a harness that sends `enter` at the title
    starts nothing and every later stage runs against a world that does not
    exist. The rectangles are filled in when the screen draws, which is why
    this is called after a few frames rather than before them.
    """
    rects = getattr(screen, 'hit_rects', None)
    if not rects or index >= len(rects):
        raise SystemExit(f'[gpu_smoke] no hit rect {index} on {screen}')
    x, y, w, h, _ = rects[index]
    dx, dy = x + w * 0.5, y + h * 0.5
    # `_to_design` multiplies by pointer_scale/scale, so going the other way
    # has to divide by the same thing. On a high-DPI window pointer_scale is
    # two, and getting this wrong lands the click a screen away.
    from lumen import runtime
    k = runtime.pointer_scale() / game.scale
    px, py = dx / k, dy / k
    game.mouse = (dx, dy)
    game.mouse_press(app, px, py, 0)
    game.mouse_release(app, px, py, 0)


def say(msg):
    if not ARGS.quiet:
        sys.stderr.write(f'  {msg}\n')
        sys.stderr.flush()


def main():
    game = Game()
    app = host.NativeApp(900, 560)
    pygame.init()
    game.start(app)
    game.ensure_display(app)
    backend = getattr(gpu, 'BACKEND', '?')
    if not gpu.active():
        sys.stderr.write(
            f'[gpu_smoke] no GPU renderer for --renderer {ARGS.renderer}; '
            f'nothing to smoke\n')
        return 2
    say(f'backend={backend}')

    drawn = [0]

    def frames(n, label):
        for _ in range(n):
            game.step(app, 1 / 240.0)
            gpu.begin_frame(palette.VOID_RGB)
            game.draw(app)
            gpu.present()
            drawn[0] += 1
        STAGES.append(label)

    frames(6, 'title')
    click_row(game, app, game.title_screen, 0)         # DESCEND
    frames(ARGS.frames, 'play')

    world = game.world
    braziers = list(world.level.braziers)
    say(f'{len(braziers)} brazier(s) on this floor')

    # Lit braziers light wall edges through the same path the lantern uses,
    # and nothing else in the suite ever ignites one.
    for b in braziers:
        b.lit = True
        b.ignite_t = 0.0
        b.edges = None
    for b in braziers:
        world.player.x, world.player.y = b.x, b.y
        for i in range(20):
            b.ignite_t = min(1.0, b.ignite_t + 0.05)   # through the fade-in
            game.step(app, 1 / 240.0)
            gpu.begin_frame(palette.VOID_RGB)
            game.draw(app)
            gpu.present()
            drawn[0] += 1
    STAGES.append(f'braziers({len(braziers)})')

    # And from across the floor, which is the off-screen cull.
    world.player.x, world.player.y = 60, 60
    frames(ARGS.frames, 'braziers-distant')

    # A resolution change empties the sprite cache; the frame after it is
    # where a stale baked sprite shows up.
    game.cycle_display(app)
    frames(ARGS.frames, 'after-resize')
    game.cycle_display(app)
    frames(ARGS.frames, 'after-resize-2')

    # Both visual settings, since a brazier caches its lit edges in whichever
    # form was live when it was lit.
    for _ in range(2):
        game.toggle_visuals()
        frames(ARGS.frames, f'visuals={game.visuals_label()}')

    sys.stderr.write(
        f'[gpu_smoke] ok: backend={backend}, {drawn[0]} frames, '
        f'stages: {", ".join(STAGES)}\n')
    return 0


if __name__ == '__main__':
    try:
        code = main()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        sys.stderr.write('[gpu_smoke] FAILED\n')
        code = 3
    sys.stderr.flush()
    sys.stdout.flush()
    os._exit(code)
