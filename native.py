"""LUMEN, hosted directly on pygame instead of on cmu-graphics.

Run with:  python native.py

The same game as `main.py` - same `Game`, same rendering - but with the loop
owned outright. See `lumen/host.py` for what that buys. `main.py` still runs
the cmu-graphics version and is unchanged.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lumen import host  # noqa: E402
from lumen.app import Game  # noqa: E402
from lumen.config import HEIGHT, WIDTH  # noqa: E402


def _window_size():
    override = os.environ.get('LUMEN_WINDOW')
    if override:
        try:
            w, h = override.lower().split('x')
            return int(w), int(h)
        except Exception:
            pass
    return WIDTH, HEIGHT


if __name__ == '__main__':
    host.run(Game(), *_window_size())
