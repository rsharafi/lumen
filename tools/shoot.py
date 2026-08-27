"""Headless screenshot harness.

Runs any scene object under SDL's dummy video driver, steps it a fixed number
of frames, and writes a PNG. This is how the game's visuals get checked
without a human at the keyboard.

    python tools/shoot.py <module:attr> --frames 40 --out shot.png [--keys "space@10,w@12"]

`<module:attr>` names a zero-argument factory returning an object with
`start(app)`, `step(app, dt)`, `draw(app)`, and optionally `key(app, k)`.
For driving the game itself, use `playtest.py` instead - this is the generic
harness, kept for prototyping a scene in isolation.
"""

import argparse
import importlib
import os
import sys

os.environ.setdefault('SDL_VIDEODRIVER', 'dummy')
os.environ.setdefault('CI', '1')
os.environ.setdefault('LUMEN_HEADLESS', '1')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

parser = argparse.ArgumentParser()
parser.add_argument('target')
parser.add_argument('--frames', type=int, default=40)
parser.add_argument('--out', default='shot.png')
parser.add_argument('--keys', default='')
parser.add_argument('--width', type=int, default=1280)
parser.add_argument('--height', type=int, default=720)
parser.add_argument('--fps', type=int, default=60)
ARGS = parser.parse_args()

mod_name, _, attr = ARGS.target.partition(':')
factory = getattr(importlib.import_module(mod_name), attr or 'scene')

# "space@10,w@12" -> {10: ['space'], 12: ['w']}
SCRIPT = {}
for chunk in filter(None, ARGS.keys.split(',')):
    key, _, when = chunk.partition('@')
    SCRIPT.setdefault(int(when or 0), []).append(key)

from cmu_graphics import *  # noqa: E402,F403  (must follow the env setup above)

SCENE = factory()
FRAME = {'n': 0}
DT = 1.0 / ARGS.fps


def onAppStart(app):
    app.background = 'black'
    app.setMaxShapeCount(400000)
    app.stepsPerSecond = 10000
    app.inspectorEnabled = False
    SCENE.start(app)


def onStep(app):
    n = FRAME['n']
    for key in SCRIPT.get(n, ()):
        handler = getattr(SCENE, 'key', None)
        if handler:
            handler(app, key)
    SCENE.step(app, DT)
    FRAME['n'] = n + 1
    if FRAME['n'] >= ARGS.frames:
        app._app.getScreenshot(ARGS.out)
        sys.stderr.write(f'wrote {ARGS.out} after {FRAME["n"]} frames\n')
        app.quit()


def redrawAll(app):
    SCENE.draw(app)


runApp(ARGS.width, ARGS.height)
