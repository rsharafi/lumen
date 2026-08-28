"""Running the game without cmu-graphics.

The framework's own loop was fine while it also did the drawing. Once the
rendering moved to `lumen/gpu.py` its remaining job was to dispatch events and
decide when to repaint - and it decides that from `had_event`, which is not
the same thing as "the frame was rebuilt". That mismatch is what put a black
flash on screen every time the pointer moved, and it cost seven shims in
`runtime.py` to work around the rest of it.

So this is the same game, hosted directly on pygame: about a hundred lines
that own the loop outright. What that buys, beyond deleting the shims:

* **A fixed simulation step.** The framework steps once per event batch and
  hands you whatever wall-clock time had passed, so a slow frame is a longer
  physics tick and the game behaves differently at 40 fps than at 120. Here
  the simulation advances in fixed increments and the renderer draws whenever
  it can, which is the arrangement that makes a dash cover the same ground on
  every machine.
* **Errors that surface.** cmu-graphics wraps every callback so an exception
  stops the app and prints; more than once during this project that meant a
  silent exit with no output at all.
* **No REPL thread.** The framework starts an interactive console on stdin and
  calls `os._exit` when that reaches EOF, which is why every scripted run of
  this game has had to set `CI=1`.

`main.py` still runs the cmu-graphics version, unchanged. This is a second
front end over the same `Game`, not a replacement for it.
"""

import os
import sys
import time

from . import gpu, palette, runtime

# The simulation advances in steps of this size regardless of frame rate.
# Small enough that a dash or a bullet never tunnels, large enough that a slow
# frame does not spiral into a hundred catch-up steps.
FIXED_DT = 1.0 / 240.0
MAX_CATCHUP = 8


class NativeApp:
    """Stands in for cmu-graphics' `App`.

    `Game` and `runtime` both talk to an app object - width, height, title,
    a screen to draw into, a way to quit. Presenting the same surface here
    means neither of them has to know which host is running, and the
    cmu-graphics front end keeps working untouched.
    """

    is_native = True

    def __init__(self, width, height, title='LUMEN'):
        self._width = width
        self._height = height
        self._screen = None
        self._running = True
        self.background = None
        self.stepsPerSecond = 60
        self.inspectorEnabled = False
        self._title = title

    # -- the shape cmu-graphics' App presents ------------------------------
    @property
    def _app(self):
        return self

    @property
    def width(self):
        return self._width

    @width.setter
    def width(self, value):
        self._width = int(value)

    @property
    def height(self):
        return self._height

    @height.setter
    def height(self, value):
        self._height = int(value)

    @property
    def title(self):
        return self._title

    @title.setter
    def title(self, value):
        self._title = value
        window = runtime.own_window()
        if window is not None:
            try:
                window.title = value
            except Exception:
                pass

    def setMaxShapeCount(self, value):
        """Nothing here counts shapes; the GPU does not care."""

    def updateScreen(self, new_screen=False):
        """`runtime` calls this after it changes the video mode."""

    def handleResize(self, width, height):
        self._width = int(width)
        self._height = int(height)

    def quit(self):
        self._running = False

    def getScreenshot(self, path):
        renderer = gpu.renderer()
        if renderer is None:
            return
        try:
            import pygame
            pygame.image.save(gpu.read_frame(), path)
        except Exception as exc:
            sys.stderr.write(f'[lumen] screenshot failed: {exc!r}\n')


def _background_rgb(app):
    color = getattr(app, 'background', None)
    if color is None:
        return palette.VOID_RGB
    for attr in ('red', 'green', 'blue'):
        if not hasattr(color, attr):
            return palette.VOID_RGB
    return (color.red, color.green, color.blue)


def run(game, width, height, title='LUMEN'):
    """The whole loop. Returns when the game or the player asks to stop."""
    import pygame

    pygame.init()
    app = NativeApp(width, height, title)
    game.start(app)
    # The window and renderer come up here rather than on the first tick, so
    # that the check below is meaningful.
    game.ensure_display(app)

    if not gpu.active():
        # `LUMEN_RENDERER=cpu` asks for cmu-graphics' own rasteriser, and that
        # only exists inside cmu-graphics' own loop. Say so rather than
        # presenting empty frames.
        sys.stderr.write(
            'LUMEN: the native host draws through lumen/gpu.py, and no GPU\n'
            '  renderer could be started. LUMEN_RENDERER=cpu selects\n'
            "  cmu-graphics' rasteriser, which only runs under main.py.\n"
            '  Use `python main.py` for that, or drop LUMEN_RENDERER.\n')
        pygame.quit()
        return

    # `start` asks runtime for the window, so by now there is one to pump.
    clock_last = time.perf_counter()
    accumulator = 0.0
    selftest = game.selftest_frames
    frames = 0
    last_present = None
    periods = []

    while app._running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                app._running = False
            elif event.type == pygame.KEYDOWN:
                key = _key_name(pygame, event)
                if key:
                    game.key_press(app, key)
            elif event.type == pygame.KEYUP:
                key = _key_name(pygame, event)
                if key:
                    game.key_release(app, key)
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button <= 3:
                game.mouse_press(app, *event.pos, event.button - 1)
            elif event.type == pygame.MOUSEBUTTONUP and event.button <= 3:
                game.mouse_release(app, *event.pos, event.button - 1)
            elif event.type == pygame.WINDOWSIZECHANGED:
                if runtime.adopt_resize(app):
                    game.resize(app)

        now = time.perf_counter()
        elapsed = min(now - clock_last, 0.25)
        clock_last = now
        accumulator += elapsed

        # Fixed steps, with a ceiling so a stall cannot spiral into a hundred
        # catch-up ticks and make the game lurch.
        steps = 0
        while accumulator >= FIXED_DT and steps < MAX_CATCHUP:
            game.step(app, FIXED_DT)
            accumulator -= FIXED_DT
            steps += 1
        if steps >= MAX_CATCHUP:
            accumulator = 0.0
        if not app._running:
            break

        gpu.begin_frame(_background_rgb(app))
        game.draw(app)
        gpu.present()

        # The frame period the player actually sees, which is what the
        # quality dial's AUTO mode needs - not the fixed simulation step.
        frame_now = time.perf_counter()
        if last_present is not None:
            period = (frame_now - last_present) * 1000.0
            game.note_frame_period(period)
            periods.append(period)
        last_present = frame_now
        frames += 1
        if selftest and frames >= selftest:
            shot = os.environ.get('LUMEN_SELFTEST_SHOT')
            if shot:
                app.getScreenshot(shot)
            ordered = sorted(periods[20:]) or [0.0]
            mid = ordered[len(ordered) // 2]
            sys.stderr.write(
                f'[lumen] native selftest ok: {frames} frames, '
                f'state={game.state}, frame={mid:.2f}ms '
                f'({1000.0 / max(mid, 1e-6):.0f} fps), '
                f'p95={ordered[int(len(ordered) * 0.95)]:.2f}ms\n')
            break

    pygame.quit()


_KEYMAP = None


def _key_name(pygame, event):
    """cmu-graphics' key names, so the game's handlers are unchanged."""
    global _KEYMAP
    if _KEYMAP is None:
        _KEYMAP = {
            pygame.K_TAB: 'tab', pygame.K_RETURN: 'enter',
            pygame.K_BACKSPACE: 'backspace', pygame.K_DELETE: 'delete',
            pygame.K_ESCAPE: 'escape', pygame.K_SPACE: 'space',
            pygame.K_RIGHT: 'right', pygame.K_LEFT: 'left',
            pygame.K_UP: 'up', pygame.K_DOWN: 'down',
            pygame.K_LCTRL: 'ctrl', pygame.K_RCTRL: 'ctrl',
            pygame.K_LSHIFT: 'shift', pygame.K_RSHIFT: 'shift',
            pygame.K_F11: 'f11',
        }
    name = _KEYMAP.get(event.key)
    if name:
        return name
    if 32 < event.key < 127:
        return chr(event.key).lower()
    return None
