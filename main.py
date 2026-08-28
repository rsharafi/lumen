"""LUMEN - Descent into the Vault.

Run with:  python main.py

pip install pillow

pip install numpy

pip install cmu-graphics

A top-down roguelite built on the CMU CS Academy graphics library, with
real-time 2D shadowcasting, procedurally generated chambers, procedurally
generated art, and procedurally synthesised sound. No asset files.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The OpenGL backend takes the window over outright, and cmu-graphics' own
# redraw cannot survive that: it goes on blitting into a surface that stops
# existing the moment the window is rebuilt for a resolution change, and the
# frame after a resize dies with "Surface is not initialized". `--gl` runs
# `native.py` for exactly that reason. Asked for it here anyway, say so and
# use the SDL backend, which shares this window properly.
if (os.environ.get('LUMEN_RENDERER') or '').strip().lower() in (
        'gl', 'opengl', 'moderngl', 'shader'):
    sys.stderr.write(
        'LUMEN: the OpenGL renderer needs its own loop - run `./run.sh --gl`\n'
        '  or `python native.py`. Drawing through SDL here instead.\n')
    os.environ['LUMEN_RENDERER'] = 'gpu'

# `app` must exist in this module's globals: cmu-graphics' setupMvc()
# deletes it from __main__, assuming the usual `from cmu_graphics import *`.
from cmu_graphics import app, runApp  # noqa: E402,F401

from lumen.app import Game  # noqa: E402
from lumen.config import HEIGHT, WIDTH  # noqa: E402

GAME = Game()


def onAppStart(app):
    GAME.start(app)


def onStep(app):
    GAME.step(app)


def onKeyPress(app, key):
    GAME.key_press(app, key)


def onKeyRelease(app, key):
    GAME.key_release(app, key)


def onMousePress(app, mouseX, mouseY, button):
    GAME.mouse_press(app, mouseX, mouseY, button)


def onMouseRelease(app, mouseX, mouseY, button):
    GAME.mouse_release(app, mouseX, mouseY, button)


def onResize(app):
    GAME.resize(app)


def redrawAll(app):
    GAME.draw(app)


def _window_size():
    """Initial window size; LUMEN_WINDOW=1920x1080 overrides it."""
    override = os.environ.get('LUMEN_WINDOW')
    if override:
        try:
            w, h = override.lower().split('x')
            return int(w), int(h)
        except Exception:
            pass
    return WIDTH, HEIGHT


runApp(*_window_size())