"""Vectorised value noise used to texture the vault.

Everything here runs once per level (or once at startup) and never inside the
frame loop, so clarity beats micro-optimisation - but it is all numpy, so even
a full 1792x1152 floor bake stays well under a second.
"""

import numpy as np


def _smooth(t):
    return t * t * (3.0 - 2.0 * t)


def value_noise(height, width, cells, rng, tileable=False):
    """A single octave of smoothed value noise in [0, 1].

    `cells` is the size of the lattice; when `tileable` is set, the lattice
    wraps so the resulting texture can be repeated seamlessly.
    """
    gh = gw = int(max(2, cells))
    grid = rng.random((gh + 1, gw + 1)).astype(np.float32)
    if tileable:
        grid[-1, :] = grid[0, :]
        grid[:, -1] = grid[:, 0]

    ys = np.linspace(0.0, gh, height, endpoint=False, dtype=np.float32)
    xs = np.linspace(0.0, gw, width, endpoint=False, dtype=np.float32)

    y0 = np.floor(ys).astype(np.int32)
    x0 = np.floor(xs).astype(np.int32)
    ty = _smooth(ys - y0)[:, None]
    tx = _smooth(xs - x0)[None, :]

    g00 = grid[y0[:, None], x0[None, :]]
    g10 = grid[y0[:, None] + 1, x0[None, :]]
    g01 = grid[y0[:, None], x0[None, :] + 1]
    g11 = grid[y0[:, None] + 1, x0[None, :] + 1]

    top = g00 + (g01 - g00) * tx
    bottom = g10 + (g11 - g10) * tx
    return top + (bottom - top) * ty


def fbm(height, width, octaves, base_cells, rng, tileable=False, gain=0.5):
    """Fractal sum of value noise octaves, normalised to [0, 1]."""
    total = np.zeros((height, width), dtype=np.float32)
    amplitude = 1.0
    norm = 0.0
    cells = base_cells
    for _ in range(octaves):
        total += value_noise(height, width, cells, rng, tileable) * amplitude
        norm += amplitude
        amplitude *= gain
        cells *= 2
    total /= max(norm, 1e-6)
    lo, hi = float(total.min()), float(total.max())
    if hi - lo > 1e-6:
        total = (total - lo) / (hi - lo)
    return total


def ridged(height, width, octaves, base_cells, rng, tileable=False):
    """Ridged noise - good for cracks and veining."""
    n = fbm(height, width, octaves, base_cells, rng, tileable)
    return 1.0 - np.abs(n * 2.0 - 1.0)


def radial_falloff(size, power=2.0, inner=0.0):
    """A size x size array, 1 at the centre falling to 0 at the edge."""
    axis = (np.arange(size, dtype=np.float32) + 0.5) / size * 2.0 - 1.0
    dist = np.sqrt(axis[:, None] ** 2 + axis[None, :] ** 2)
    if inner > 0.0:
        dist = np.clip((dist - inner) / max(1e-6, 1.0 - inner), 0.0, 1.0)
    else:
        dist = np.clip(dist, 0.0, 1.0)
    return np.power(1.0 - dist, power, dtype=np.float32)


def directional_falloff(height, width, power=1.6):
    """1 at the top edge falling to 0 at the bottom - used for wall faces."""
    col = np.linspace(1.0, 0.0, height, dtype=np.float32) ** power
    return np.repeat(col[:, None], width, axis=1)
