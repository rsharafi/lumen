"""Draw the same frame with both renderers and put the results side by side.

The headless playtest harness can only exercise the cmu-graphics renderer -
the GPU backend needs a real accelerated renderer, and the dummy video driver
has none - so this drives two real windowed runs instead, pinned to the same
seed and a fixed timestep, and screenshots the same frame from each.

    python tools/compare_backends.py --frames 240 --out shots/ab.png

The two will not be pixel-identical and are not meant to be: text is baked
with PIL on the GPU path and rasterised by the library on the other, and the
library does not antialias polygon edges where the GPU does. What this is for
is catching things that are actually wrong - a layer in the wrong order, a
sprite blended at the wrong strength, geometry that has gone missing.
"""

import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

parser = argparse.ArgumentParser()
parser.add_argument('--frames', type=int, default=240)
parser.add_argument('--seed', type=int, default=1234)
parser.add_argument('--floor', type=int, default=1)
parser.add_argument('--play', action='store_true', default=True,
                    help='start a run rather than sitting on the title screen')
parser.add_argument('--title', action='store_true',
                    help='stay on the title screen instead')
parser.add_argument('--window', default='1280x720')
parser.add_argument('--out', default='shots/backends.png')
ARGS = parser.parse_args()


def run(backend, shot):
    env = dict(os.environ)
    env.update({
        'CI': '1',
        'LUMEN_RENDERER': backend,
        'LUMEN_SEED': str(ARGS.seed),
        # A fixed timestep makes the two runs reach the same game state on the
        # same frame; without it the faster backend simulates further.
        'LUMEN_FIXED_DT': '1',
        'LUMEN_NO_POINTER': '1',
        'LUMEN_START_FLOOR': str(ARGS.floor),
        'LUMEN_WINDOW': ARGS.window,
        # Pin the sharpness dial: it only applies to the cmu-graphics path, so
        # leaving it on the default would have the two render at different
        # sizes and compare a resample.
        'LUMEN_QUALITY': 'NATIVE',
        'LUMEN_SELFTEST': str(ARGS.frames),
        'LUMEN_SELFTEST_SHOT': shot,
        'LUMEN_SAVE': os.path.join(ROOT, 'tools', '.compare_save.json'),
    })
    if not ARGS.title:
        env['LUMEN_SELFTEST_PLAY'] = '1'
    env.pop('LUMEN_FULLSCREEN', None)
    proc = subprocess.run([sys.executable, os.path.join(ROOT, 'main.py')],
                          env=env, cwd=ROOT, capture_output=True, text=True)
    tail = [ln for ln in proc.stderr.splitlines() if 'selftest ok' in ln]
    print(f'  {backend:3s}: {tail[-1] if tail else proc.stderr.strip()[-300:]}')
    return os.path.exists(shot)


def main():
    out = os.path.abspath(ARGS.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    shots = {}
    for backend in ('cpu', 'gpu'):
        path = os.path.join(os.path.dirname(out), f'_backend_{backend}.png')
        if run(backend, path):
            shots[backend] = path
    if len(shots) != 2:
        sys.stderr.write('one of the runs produced no screenshot\n')
        return 1

    from PIL import Image, ImageChops
    a = Image.open(shots['cpu']).convert('RGB')
    b = Image.open(shots['gpu']).convert('RGB')
    if a.size != b.size:
        b = b.resize(a.size, Image.LANCZOS)
    w, h = a.size
    sheet = Image.new('RGB', (w, h * 2 + 8), (0, 0, 0))
    sheet.paste(a, (0, 0))
    sheet.paste(b, (0, h + 8))
    sheet.save(out)

    diff = ImageChops.difference(a, b)
    bbox = diff.getbbox()
    hist = diff.convert('L').histogram()
    same = hist[0] / float(w * h)
    print(f'\ntop = cmu-graphics, bottom = GPU  -> {out}')
    print(f'identical pixels: {same * 100:.1f}%   changed region: {bbox}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
