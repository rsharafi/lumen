"""Do the renderers agree which way up the world is?

A render target and a sprite disagree about which row is row zero, and
getting that wrong flips the world without flipping the HUD - so the game
still looks like a game, the controls feel inverted, and everything drawn
after the composite appears to have a mirrored twin. It is subtle enough to
ship, so it is checked.

    python tools/orientation_check.py
"""

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def shoot(renderer, path):
    env = dict(os.environ)
    env.update({
        'LUMEN_RENDERER': renderer, 'LUMEN_SEED': '1234',
        'LUMEN_WINDOW': '900x560', 'LUMEN_START_FLOOR': '1',
        'LUMEN_SELFTEST': '90', 'LUMEN_SELFTEST_PLAY': '1',
        'LUMEN_SELFTEST_SHOT': path,
        'LUMEN_SAVE': os.path.join(tempfile.gettempdir(), 'orient_save.json'),
    })
    subprocess.run([sys.executable, os.path.join(ROOT, 'native.py')],
                   env=env, cwd=ROOT, capture_output=True, timeout=180)
    return os.path.exists(path)


def lantern_y(path):
    """Where the brightest point of the world sits, as a fraction of height."""
    from PIL import Image
    im = Image.open(path).convert('L')
    w, h = im.size
    best = (-1, 0)
    for y in range(int(h * 0.10), int(h * 0.80), 4):
        for x in range(int(w * 0.10), int(w * 0.90), 4):
            v = im.getpixel((x, y))
            if v > best[0]:
                best = (v, y)
    return best[1] / h


def main():
    out = tempfile.mkdtemp()
    found = {}
    for renderer in ('gpu', 'gl'):
        path = os.path.join(out, f'{renderer}.png')
        if not shoot(renderer, path):
            print(f'  {renderer}: could not render (skipped)')
            continue
        found[renderer] = lantern_y(path)
        print(f'  {renderer}: lantern at y {found[renderer]:.3f}')

    if len(found) < 2:
        print('need both renderers to compare')
        return 0
    a, b = found['gpu'], found['gl']
    if abs(a - b) < 0.04:
        print(f'OK: both agree within {abs(a - b):.3f}')
        return 0
    if abs((1.0 - a) - b) < 0.06:
        print(f'FAIL: mirrored about the centre - the world is upside down '
              f'on one of them ({a:.3f} against {b:.3f})')
    else:
        print(f'FAIL: they disagree ({a:.3f} against {b:.3f})')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
