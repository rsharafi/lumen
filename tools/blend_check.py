"""Prove the two renderers composite identically.

The GPU backend and cmu-graphics have to agree on what `opacity` means, and a
systematic disagreement would be almost invisible on a normal frame - it would
just make the game a little darker or a little brighter and stay that way.
This draws a ladder of known opacities over known backgrounds with each
backend and compares the pixels.

    python tools/blend_check.py

Exits non-zero if any channel differs by more than one step, which is all
rounding should ever account for. Note the two are *not* expected to agree
everywhere: the library does not antialias polygon edges and the GPU does, and
text is baked with PIL on one path and rasterised by the library on the other.
Flat interiors are what this samples, and those must match.
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = os.path.join(ROOT, 'tools', '.blend')
TOLERANCE = 1

SCENE = r'''
import os, sys
sys.path.insert(0, %(root)r)
import threading, os as _os
threading.Timer(60, lambda: _os._exit(9)).start()
from cmu_graphics import app, runApp, rgb
from lumen import draw, art, gpu, runtime
from PIL import Image

N = {'n': 0}
SHOT = os.environ['SHOT']
RED = rgb(200, 40, 40)
WHITE = rgb(255, 255, 255)

def onAppStart(a):
    a.background = rgb(0, 0, 0)
    a.stepsPerSecond = 60
    a.setMaxShapeCount(60000)

def onResize(a):
    pass

def onStep(a):
    N['n'] += 1
    if N['n'] == 1:
        # The backends only diverge once the window has been taken over: the
        # framework's own window has no renderer behind it.
        runtime.install_gpu_hooks(a)
        runtime.install_resize_hook(a)
        runtime.set_video_mode(a, (500, 300), False, 1.0)
        draw.set_scale(1.0)
        art.set_scale(1.0)
        if not gpu.active():
            runtime.enable()
    if N['n'] >= 30:
        a._app.getScreenshot(SHOT)
        _os._exit(0)

def redrawAll(a):
    for i, op in enumerate(%(steps)r):
        x = 20 + i * 90
        draw.drawPolygon(x, 20, x+70, 20, x+70, 90, x, 90, fill=RED, opacity=op)
    for i, op in enumerate(%(steps)r):
        x = 20 + i * 90
        draw.drawPolygon(x, 110, x+70, 110, x+70, 180, x, 180, fill=RED, opacity=100)
        draw.drawPolygon(x, 110, x+70, 110, x+70, 180, x, 180, fill=WHITE, opacity=op)
    spr = art.wrap(Image.new('RGBA', (70, 70), (60, 200, 255, 255)), ('blend', 1))
    for i, op in enumerate(%(steps)r):
        draw.drawImage(spr, 20 + i * 90, 200, opacity=op)

runApp(500, 300)
'''

STEPS = (100, 75, 50, 25, 10)
ROWS = (('polygon over black', 55),
        ('translucent white over opaque red', 145),
        ('premultiplied sprite over black', 235))


def render(backend):
    os.makedirs(SCRATCH, exist_ok=True)
    scene = os.path.join(SCRATCH, 'scene.py')
    with open(scene, 'w') as f:
        f.write(SCENE % {'root': ROOT, 'steps': STEPS})
    shot = os.path.join(SCRATCH, f'{backend}.png')
    env = dict(os.environ)
    env.update({'CI': '1', 'SHOT': shot, 'LUMEN_RENDERER': backend,
                'LUMEN_SAVE': os.path.join(SCRATCH, 'save.json')})
    proc = subprocess.run([sys.executable, scene], env=env, cwd=ROOT,
                          capture_output=True, text=True)
    if not os.path.exists(shot):
        sys.stderr.write(f'{backend}: no screenshot\n{proc.stderr[-800:]}\n')
        return None
    return shot


def main():
    from PIL import Image
    shots = {b: render(b) for b in ('cpu', 'gpu')}
    if not all(shots.values()):
        return 2
    a = Image.open(shots['cpu']).convert('RGB')
    b = Image.open(shots['gpu']).convert('RGB')
    if a.size != b.size:
        sys.stderr.write(f'size mismatch {a.size} vs {b.size}\n')
        return 2

    worst = 0
    for label, y in ROWS:
        print(f'  {label}')
        for i, op in enumerate(STEPS):
            x = 20 + i * 90 + 35
            pa, pb = a.getpixel((x, y)), b.getpixel((x, y))
            delta = max(abs(p - q) for p, q in zip(pa, pb))
            worst = max(worst, delta)
            flag = '' if delta <= TOLERANCE else '   <-- MISMATCH'
            print(f'    opacity {op:3d}:  cmu={pa}  gpu={pb}  delta={delta}{flag}')

    print(f'\nworst channel delta: {worst} (tolerance {TOLERANCE})')
    if worst > TOLERANCE:
        print('FAIL: the renderers do not composite the same way')
        return 1
    print('OK: both renderers composite identically')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
