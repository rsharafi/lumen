"""Seeded randomness.

`from cmu_graphics import *` shadows the stdlib `random` module with a bare
function, so the whole game funnels through this module instead of importing
`random` anywhere it could be clobbered.
"""

import random as _random
import math as _math


class Rng:
    """A thin, explicit wrapper around `random.Random`."""

    def __init__(self, seed=None):
        self._r = _random.Random(seed)
        self.seed_value = seed

    def reseed(self, seed):
        self.seed_value = seed
        self._r.seed(seed)

    def random(self):
        return self._r.random()

    def uniform(self, a, b):
        return self._r.uniform(a, b)

    def randint(self, a, b):
        return self._r.randint(a, b)

    def choice(self, seq):
        return self._r.choice(seq)

    def sample(self, seq, k):
        k = min(k, len(seq))
        return self._r.sample(list(seq), k)

    def shuffled(self, seq):
        out = list(seq)
        self._r.shuffle(out)
        return out

    def chance(self, p):
        return self._r.random() < p

    def sign(self):
        return 1.0 if self._r.random() < 0.5 else -1.0

    def angle(self):
        return self._r.random() * _math.pi * 2.0

    def in_disc(self, radius):
        """Uniform point inside a disc of the given radius."""
        a = self._r.random() * _math.pi * 2.0
        r = radius * _math.sqrt(self._r.random())
        return _math.cos(a) * r, _math.sin(a) * r

    def spread(self, base_angle, spread_radians):
        return base_angle + self._r.uniform(-spread_radians, spread_radians)

    def weighted(self, pairs):
        """Pick from [(item, weight), ...]."""
        total = 0.0
        for _, w in pairs:
            total += w
        if total <= 0.0:
            return pairs[0][0]
        roll = self._r.random() * total
        acc = 0.0
        for item, w in pairs:
            acc += w
            if roll <= acc:
                return item
        return pairs[-1][0]


# Shared instances: `world` drives level generation (reproducible from a run
# seed) and `fx` drives cosmetic noise (never affects gameplay).
world = Rng()
fx = Rng()
