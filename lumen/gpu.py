"""The renderer, behind one name.

There were three: cmu-graphics' own rasteriser, SDL's renderer, and OpenGL.
They are gone bar the last one - this game is deferred float lighting, a
filmic tone map and per-pixel volumetrics, and none of that has a fallback
worth the branch it costs. What is left re-exports `glx` so nothing above
this file imports the backend by name.

`from ... import *` rather than a dispatching wrapper on purpose: the names
bind once at import, so a draw call is a plain global lookup and not an
attribute chase through two modules. Draw calls run tens of thousands of
times a frame and that difference is real.
"""

from .glx import *              # noqa: F401,F403
from .glx import _area2, _convex  # noqa: F401

BACKEND = 'gl'
