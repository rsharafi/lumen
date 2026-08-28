"""Which renderer the game draws through.

Three of them now, all behind one name so nothing above this file has to
know which is live:

* `sdlx`  - SDL's own renderer. Textured quads and fixed-function blending,
            no shaders, 8-bit render targets. This is what the game shipped
            on and it holds 120 fps at native resolution.
* `glx`   - OpenGL via moderngl. Float16 render targets and real shaders,
            which is what makes tone mapping, a proper bloom threshold and
            per-pixel lighting possible at all.
* neither - `LUMEN_RENDERER=cpu` leaves cmu-graphics' own rasteriser to do
            the drawing, as it always did.

`from ... import *` rather than a dispatching wrapper on purpose: the names
bind once at import, so `gpu.active()` in the middle of a draw call is a
plain global lookup and not an attribute chase through two modules. Draw
calls run tens of thousands of times a frame and that difference is real.
"""

import os as _os

_choice = (_os.environ.get('LUMEN_RENDERER') or 'gpu').strip().lower()

if _choice in ('gl', 'opengl', 'moderngl', 'shader'):
    from .glx import *          # noqa: F401,F403
    from .glx import _area2, _convex     # noqa: F401
    BACKEND = 'gl'
else:
    from .sdlx import *         # noqa: F401,F403
    from .sdlx import _area2, _convex    # noqa: F401
    BACKEND = 'sdl'
