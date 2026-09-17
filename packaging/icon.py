"""The app icon, generated the way everything else in this game is.

    .venv/bin/python packaging/icon.py build/icon

Writes `<out>.png`, `<out>.ico` and, on macOS, `<out>.icns`. Nothing is
loaded from disk: the stone is the same tile the chambers are laid with, and
the rest is the launch screen's dial with the lantern lit in the middle of it,
drawn here with PIL because an icon has no OpenGL context to ask for a light.
"""

import math
import os
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from lumen import art, palette  # noqa: E402
from lumen.config import BOSS_FLOORS, FLOORS_PER_RUN  # noqa: E402

SIZE = 1024


def _radial(size, cx, cy, radius, power):
    """A falloff, as a float array - the same curve `glx` evaluates per pixel."""
    ys = np.arange(size, dtype=np.float32)[:, None] - cy
    xs = np.arange(size, dtype=np.float32)[None, :] - cx
    d = np.sqrt(xs * xs + ys * ys) / max(radius, 1e-6)
    f = np.clip(1.0 - d, 0.0, 1.0)
    return f ** power


def _add_light(rgb, mask, color, strength):
    for i, channel in enumerate(color):
        rgb[..., i] += mask * (channel / 255.0) * strength
    return rgb


def render(size=SIZE):
    art.set_scale(1.0)
    stone = art.floor_tile(5, size=256).convert('RGB').resize(
        (size, size), Image.LANCZOS)
    rgb = np.asarray(stone, dtype=np.float32) / 255.0 * 1.6

    cx = cy = size * 0.5
    # The pool of light the lantern throws, and the hot core inside it.
    rgb = _add_light(rgb, _radial(size, cx, cy * 1.04, size * 0.46, 2.6),
                     art.rgb_tuple(palette.LIGHT_WARM), 0.8)
    rgb = _add_light(rgb, _radial(size, cx, cy * 1.02, size * 0.17, 2.8),
                     art.rgb_tuple(palette.LIGHT_CORE), 0.95)

    plate = Image.fromarray(
        np.clip(rgb * 255.0, 0, 255).astype(np.uint8), 'RGB')
    draw = ImageDraw.Draw(plate, 'RGBA')

    def ring(radius, width, color):
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
                     outline=color, width=int(width))

    slate = (92, 104, 132, 190)
    ring(size * 0.40, size * 0.008, slate)
    ring(size * 0.335, size * 0.005, (70, 80, 104, 150))

    # The twenty floors, and the three that are somebody's door.
    for floor in range(1, FLOORS_PER_RUN + 1):
        a = -math.pi * 0.5 + (floor - 0.5) * (2.0 * math.pi / FLOORS_PER_RUN)
        r = size * 0.368
        x, y = cx + math.cos(a) * r, cy + math.sin(a) * r
        boss = floor in BOSS_FLOORS
        k = size * (0.021 if boss else 0.013)
        color = ((150, 210, 255, 255) if boss
                 else art.rgb_tuple(palette.LIGHT_CORE) + (255,))
        draw.polygon([(x, y - k), (x + k, y), (x, y + k), (x - k, y)],
                     fill=color)

    # The flame: the launch screen's teardrop, in four narrowing layers.
    def flame(height, width, color, alpha):
        pts_l, pts_r = [], []
        peak = 0.28
        norm = 1.0 / (peak ** 0.45 * (1.0 - peak) ** 1.25)
        for i in range(25):
            u = i / 24
            half = width * norm * (u ** 0.45) * ((1.0 - u) ** 1.25)
            y = cy + size * 0.11 - u * height
            pts_l.append((cx - half, y))
            pts_r.append((cx + half, y))
        draw.polygon(pts_l + pts_r[::-1], fill=tuple(color) + (alpha,))

    flame(size * 0.32, size * 0.070, art.rgb_tuple(palette.LIGHT_DEEP), 205)
    flame(size * 0.25, size * 0.050, art.rgb_tuple(palette.LIGHT_WARM), 235)
    flame(size * 0.17, size * 0.031, art.rgb_tuple(palette.LIGHT_CORE), 250)
    flame(size * 0.10, size * 0.017, art.rgb_tuple(palette.FLARE), 255)

    # Bloom, the cheap way: the bright part of the picture, blurred back over
    # itself. The renderer does this properly; an icon does not need it to be.
    arr = np.asarray(plate, dtype=np.float32) / 255.0
    bright = np.clip(arr - 0.62, 0.0, None) * 1.9
    glow = Image.fromarray((np.clip(bright, 0, 1) * 255).astype(np.uint8),
                           'RGB').filter(ImageFilter.GaussianBlur(size * 0.02))
    arr = np.clip(arr + np.asarray(glow, dtype=np.float32) / 255.0 * 0.55,
                  0.0, 1.0)

    # The vignette the game closes every frame with.
    ys = (np.arange(size, dtype=np.float32)[:, None] + 0.5) / size * 2.0 - 1.0
    xs = (np.arange(size, dtype=np.float32)[None, :] + 0.5) / size * 2.0 - 1.0
    d = np.sqrt((xs * 1.02) ** 2 + (ys * 1.16) ** 2)
    shade = 1.0 - np.clip((d - 0.38) / 0.9, 0.0, 1.0) ** 1.4 * 0.97
    arr *= shade[..., None]

    out = Image.fromarray((arr * 255.0).astype(np.uint8), 'RGB').convert('RGBA')

    # A rounded square, because every platform draws one round it anyway.
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1],
                                           radius=int(size * 0.22), fill=255)
    out.putalpha(mask)
    return out


def write(out_base):
    os.makedirs(os.path.dirname(os.path.abspath(out_base)) or '.',
                exist_ok=True)
    icon = render()
    png = out_base + '.png'
    icon.save(png)

    ico = out_base + '.ico'
    icon.save(ico, sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])

    icns = None
    if sys.platform == 'darwin':
        icns = out_base + '.icns'
        with tempfile.TemporaryDirectory() as tmp:
            iconset = os.path.join(tmp, 'lumen.iconset')
            os.makedirs(iconset)
            for base in (16, 32, 128, 256, 512):
                icon.resize((base, base), Image.LANCZOS).save(
                    os.path.join(iconset, f'icon_{base}x{base}.png'))
                icon.resize((base * 2, base * 2), Image.LANCZOS).save(
                    os.path.join(iconset, f'icon_{base}x{base}@2x.png'))
            subprocess.run(['iconutil', '-c', 'icns', iconset, '-o', icns],
                           check=True)
    return png, ico, icns


if __name__ == '__main__':
    base = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'build',
                                                              'icon')
    for path in write(base):
        if path:
            print(path)
