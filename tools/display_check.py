"""Resize the real window every way a player can, and prove the frame follows.

    .venv/bin/python tools/display_check.py

Opens a real window - the renderer needs a GL context, and a resize is only
worth testing against the thing that actually resizes - and checks:

* **Shapes.** A range of window sizes, from a square to past 32:9. After each
  settles, the frame is the drawable (or the largest rectangle inside it the
  game is composed for), the design view covers every pixel of it, and a
  pointer position survives the round trip into the render buffer and back.
* **The dial.** Every sharpness rung draws at its own fraction of the window,
  sharpest first, and none of them rebuilds the window to do it.
* **Fullscreen.** On and off again lands back on the same windowed size.
* **Rooms built before a resize.** Mid-floor, the scale changes and then the
  player walks through every door. A chamber whose layers belong to the old
  scale draws as nothing - the floor and the walls are simply not there - so
  none may be entered in that state.
* **Overlays.** The vignette and scanlines drawn per pixel against the numpy
  bakes they replaced, which they have to match to the level; the grain,
  whose pattern cannot match, to its statistics.
* **Cost.** A resize on the title screen is well under a frame's worth of
  hitch, since there is nothing on it left to bake.

Exit status is the number of failures.
"""

import os
import sys
import time

os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
os.environ.setdefault('LUMEN_NO_POINTER', '1')
os.environ.setdefault('LUMEN_SKIP_LAUNCH', '1')
os.environ.setdefault('LUMEN_WINDOW', '1280x720')
os.environ.setdefault(
    'LUMEN_SAVE',
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 '.display_save.json'))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np                              # noqa: E402

from lumen import art, gpu, host, runtime      # noqa: E402
from lumen import app as app_mod               # noqa: E402
from lumen import level as level_mod           # noqa: E402

FAILS = []


def check(ok, what):
    print(('  ok    ' if ok else '  FAIL  ') + what)
    if not ok:
        FAILS.append(what)


def pump(game, app, seconds):
    """Run the loop for a while: events, display sync, a step, a frame."""
    import pygame
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        for event in pygame.event.get():
            if event.type == pygame.WINDOWMINIMIZED:
                game.set_minimized(True)
            elif event.type in (pygame.WINDOWRESTORED, pygame.WINDOWSHOWN):
                game.set_minimized(False)
        game.sync_display(app, time.perf_counter())
        game.step(app, host.FIXED_DT)
        gpu.begin_frame(host._background_rgb(app))
        game.draw(app)
        gpu.present()


def settle(game, app):
    pump(game, app, game.DISPLAY_SETTLE + 0.35)


def check_output(game, label):
    drawable, rect, render = runtime.committed()
    live = runtime.drawable_size()
    check(drawable == live, f'{label}: drew at the live drawable {live}')
    aspect = rect[2] / float(rect[3])
    check(runtime.MIN_ASPECT - 0.01 <= aspect <= runtime.MAX_ASPECT + 0.01,
          f'{label}: frame {rect[2]}x{rect[3]} is inside the aspect range')
    full = (rect[2], rect[3]) == tuple(drawable)
    fits = (rect[0] + rect[2] <= drawable[0] and rect[1] + rect[3] <= drawable[1]
            and rect[0] >= 0 and rect[1] >= 0)
    check(fits, f'{label}: frame lies inside the drawable')
    if not full:
        barred = rect[0] == 0 or rect[1] == 0
        check(barred, f'{label}: barred on one axis only')
    check(game.height == 720 and game.width * game.scale >= render[0] - 0.5,
          f'{label}: design view {game.width}x{game.height} covers {render}')
    for px, py in ((0.0, 0.0), (123.5, 77.25), (400.0, 300.0)):
        rx, ry = runtime.pointer_to_render(px, py)
        bx, by = runtime.render_to_pointer(rx, ry)
        if abs(bx - px) > 1e-6 or abs(by - py) > 1e-6:
            check(False, f'{label}: pointer ({px}, {py}) round trip')
            break
    else:
        check(True, f'{label}: pointer round trip')
    pts = runtime.window_points()
    cx, cy = runtime.pointer_to_render(pts[0] * 0.5, pts[1] * 0.5)
    check(abs(cx - render[0] * 0.5) < 1.0 and abs(cy - render[1] * 0.5) < 1.0,
          f'{label}: window centre is the frame centre')


def shapes(game, app):
    print('shapes')
    win = runtime.own_window()
    for size in ((1280, 720), (1100, 1000), (700, 700), (640, 900),
                 (1400, 420), (1024, 768), (1280, 720)):
        win.size = size
        settle(game, app)
        check_output(game, f'window {size}')


def dial(game, app):
    print('dial')
    rect = runtime.committed()[1]
    sizes = []
    for index, (name, value) in enumerate(game.QUALITY_MODES):
        if value is app_mod.AUTO:
            continue
        game.display_index = index
        game._commit_display(app)
        settle(game, app)
        render = runtime.committed()[2]
        want = (max(1, round(rect[2] * value)), max(1, round(rect[3] * value)))
        check(render == want, f'{name}: renders {render}, wants {want}')
        sizes.append(render[0] * render[1])
    check(sizes == sorted(sizes, reverse=True) and len(set(sizes)) == len(sizes),
          'every rung is its own size, sharpest first')
    game.display_index = 1
    game._commit_display(app)
    settle(game, app)


def fullscreen(game, app):
    print('fullscreen')
    before = runtime.window_points()
    game.toggle_fullscreen(app)
    pump(game, app, 1.2)
    check(game.fullscreen and runtime.is_fullscreen(), 'went fullscreen')
    check_output(game, 'fullscreen')
    game.toggle_fullscreen(app)
    pump(game, app, 1.2)
    check(not game.fullscreen and not runtime.is_fullscreen(), 'came back')
    after = runtime.window_points()
    check(after == before, f'windowed size restored: {before} -> {after}')


def rooms(game, app):
    print('rooms built before a resize')
    game.new_run()
    game.state = app_mod.PLAYING
    pump(game, app, 1.5)          # let the builder get the neighbours up
    world = game.world
    built = world.builder._built
    check(len(built) > 1, f'{len(built)} chambers built ahead of the resize')
    win = runtime.own_window()
    w, h = runtime.window_points()
    win.size = (w, h + 60)        # a height change is a scale change
    generation = art.GENERATION
    settle(game, app)
    check(art.GENERATION != generation, 'the resize changed the render scale')
    check(not level_mod.needs_bake(world.level), 'the room on screen is current')
    pump(game, app, 2.0)          # the worker redoes the rest
    entrance = world.plan.room(world.plan.entrance)
    for side in list(entrance.doors):
        room = world.plan.room(entrance.doors[side])
        entered = world.use_door(side)
        pump(game, app, 0.05)
        check(entered is room and not level_mod.needs_bake(world.level),
              f'walked into the {room.kind} room and it has its floor and walls')
        world.enter_room(entrance)
    game.state = app_mod.TITLE
    game.world = None


def overlays(game, app):
    print('overlays')
    W, H = 1011, 603
    scale = art.SCALE
    dw, dh = W / scale, H / scale
    size = (art.px(dw), art.px(dh))

    def bake_vignette(strength, radius, tint):
        w, h = size
        ys = (np.arange(h, dtype=np.float32) + 0.5) / h * 2.0 - 1.0
        xs = (np.arange(w, dtype=np.float32) + 0.5) / w * 2.0 - 1.0
        d = np.sqrt((xs[None, :] * 1.02) ** 2 + (ys[:, None] * 1.16) ** 2)
        a = np.clip((d - radius) / max(1e-6, 1.45 - radius), 0.0,
                    1.0) ** 1.5 * strength
        return art._rgba(tuple(np.full(a.shape, c, np.float32) for c in tint), a)

    def bake_scanlines(alpha, period):
        w, h = size
        period = max(1, art.px(period))
        rows = ((np.arange(h) % period) == 0).astype(np.float32) * alpha
        z = np.zeros((h, w), np.float32)
        return art._rgba((z, z, z), np.repeat(rows[:, None], w, axis=1))

    def bake_grain(amount, seed=11):
        w, h = size
        n = np.random.default_rng(seed).random((h, w)).astype(np.float32)
        v = np.where(n > 0.5, 255.0, 0.0)
        return art._rgba((v, v, v), np.abs(n - 0.5) * 2.0 * amount)

    def frame(sprite, bg):
        gpu.begin_frame(bg)
        gpu.blit(sprite, 0, 0, opacity=None)
        gpu.flush()
        import pygame
        arr = pygame.surfarray.array3d(gpu.read_frame()).transpose(1, 0, 2)
        return arr[:size[1], :size[0]].astype(int)

    cases = (
        ('flash vignette', bake_vignette(1.0, 0.26, (230, 60, 40)),
         art.vignette(dw, dh, 1.0, 0.26, (230, 60, 40)), 'exact'),
        ('backdrop vignette', bake_vignette(0.95, 0.45, (2, 4, 9)),
         art.vignette(dw, dh, 0.95, 0.45), 'exact'),
        ('scanlines', bake_scanlines(0.1, 4), art.scanlines(dw, dh, 0.1, 4),
         'exact'),
        ('grain', bake_grain(0.05), art.grain(dw, dh, 0.05), 'stats'),
    )
    for name, baked, drawn, how in cases:
        sprite = gpu.sprite_from_pil(art.premultiply(baked))
        for bg in ((128, 128, 128), (240, 200, 90)):
            a, b = frame(sprite, bg), frame(drawn, bg)
            if how == 'exact':
                off = (np.abs(a - b).max(axis=2) > 1).mean()
                near = (np.abs(a - b).max(axis=2) > 0).mean()
                check(off == 0.0 and near < 1e-3,
                      f'{name} on {bg}: {near * 100:.4f}% of pixels a level '
                      f'off, none further')
            else:
                check(abs(a.mean() - b.mean()) < 0.05
                      and abs(a.std() - b.std()) < 0.05,
                      f'{name} on {bg}: mean {a.mean():.2f}/{b.mean():.2f}, '
                      f'spread {a.std():.2f}/{b.std():.2f}')


def cost(game, app):
    print('cost')
    win = runtime.own_window()
    times = []
    plain = game.resize

    def timed(app_):
        t0 = time.perf_counter()
        plain(app_)
        times.append((time.perf_counter() - t0) * 1000.0)

    game.resize = timed
    try:
        for size in ((1200, 700), (1000, 820), (1280, 720)):
            win.size = size
            settle(game, app)
    finally:
        del game.resize
    check(len(times) >= 3, f'{len(times)} resizes drawn at, one per size')
    worst = max(times) if times else 0.0
    check(worst < 60.0, f'title-screen resize at worst {worst:.0f} ms')


def main():
    import pygame
    pygame.init()
    app = host.NativeApp(1280, 720, title='LUMEN display check')
    game = app_mod.Game()
    game.start(app)
    game.ensure_display(app)
    if not gpu.active():
        print('no GL context; cannot run')
        return 1
    if game.fullscreen:
        game.toggle_fullscreen(app)
    game.display_index = 1
    game._commit_display(app)
    settle(game, app)
    for part in (shapes, dial, fullscreen, cost, overlays, rooms):
        part(game, app)
    pygame.quit()
    print(f'\n{len(FAILS)} failure(s)')
    for what in FAILS:
        print('  ' + what)
    return len(FAILS)


if __name__ == '__main__':
    sys.exit(main())
