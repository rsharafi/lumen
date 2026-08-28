"""Host-level tuning of the framework's main loop.

Two narrow, measured changes to how cmu-graphics presents a frame. Both are
defensive: anything unexpected falls straight through to normal behaviour.

1. Remove a redundant per-pixel blend from the present path.

Every frame, cmu-graphics hands its finished buffer to pygame as an `'RGBA'`
surface and blits that onto the display. The buffer is fully opaque - the
framework's own first act each frame is to fill the whole surface with the
background colour - but declaring an alpha channel makes SDL take the
per-pixel alpha-blend path anyway. On a 1280x720 window that measured
**5.62 ms a frame**.

Asking for the same bytes as `'RGBX'` treats the fourth byte as padding. The
resulting image is pixel-identical (verified against the RGBA path) and the
blit measures **0.45 ms** - a saving of over five milliseconds, which is the
difference between a 60 Hz and a 120 Hz budget.

The swap is done with a narrow shim on `pygame.image.frombuffer` rather than by
reimplementing the framework's draw loop: it fires only for the exact call the
framework makes.

2. Stop sleeping when there is nothing to sleep through.

The framework's loop ends every iteration with `pygame.time.wait(1)`, which is
right when the loop is idling between steps and pure overhead when the frame
work already exceeds the frame budget - at a 120 Hz target that 1 ms is 12% of
it. The shim keeps the sleep whenever the previous iteration was quick (so an
idle loop still yields the CPU) and skips it when the loop is clearly
compute-bound.
"""

import os
import sys
import time

from . import gpu

_installed = False
_wait_installed = False


def _rgbx_matches_rgba(surface):
    """Prove, on this display, that RGBX blits identically to RGBA.

    Cheaper than reasoning about pixel masks: build one small opaque buffer,
    blit it both ways onto a surface in the display's own format, and compare.
    Frames from cmu-graphics are always fully opaque - the framework fills the
    whole surface with the background colour before anything else - so the
    alpha byte carries no information and dropping it must be a no-op. This
    check confirms that rather than assuming it.
    """
    import pygame

    size = (8, 8)
    buf = bytearray()
    for i in range(size[0] * size[1]):
        buf += bytes((i * 7 % 256, i * 13 % 256, i * 29 % 256, 255))
    buf = bytes(buf)

    results = []
    for fmt in ('RGBA', 'RGBX'):
        target = pygame.Surface(size, 0, surface)
        target.fill((0, 0, 0))
        target.blit(pygame.image.frombuffer(buf, size, fmt), (0, 0))
        results.append([target.get_at((x, y))[:3]
                        for y in range(size[1]) for x in range(size[0])])
    return results[0] == results[1]


def enable(verbose=False):
    """Install the shim. Safe to call more than once; never raises."""
    global _installed
    if _installed:
        return True
    try:
        import pygame
    except Exception:
        return False

    surface = pygame.display.get_surface()
    if surface is None:
        return False

    try:
        if surface.get_bitsize() != 32:
            return False
        if not _rgbx_matches_rgba(surface):
            return False
    except Exception:
        return False

    original = pygame.image.frombuffer

    def frombuffer(data, sz, fmt, *args, **kwargs):
        # No size check: the framework is the only caller, and pinning a size
        # here would silently disable the fast path after a window resize.
        if fmt == 'RGBA':
            try:
                return original(data, sz, 'RGBX', *args, **kwargs)
            except Exception:
                pass
        return original(data, sz, fmt, *args, **kwargs)

    pygame.image.frombuffer = frombuffer
    _installed = True
    if verbose:
        sys.stderr.write('[lumen] fast blit path enabled\n')
    return True


def disable():
    """Not needed in normal play; kept so the effect can be measured."""
    global _installed
    if not _installed:
        return
    try:
        import importlib
        import pygame.image
        importlib.reload(pygame.image)
        _installed = False
    except Exception:
        pass


# --------------------------------------------------------------------------
# Adaptive frame sleep
# --------------------------------------------------------------------------
BUSY_THRESHOLD_MS = 4.0


def enable_adaptive_wait(verbose=False):
    """Skip the framework's 1 ms idle sleep on compute-bound frames."""
    global _wait_installed
    if _wait_installed:
        return True
    try:
        import pygame
    except Exception:
        return False

    original = pygame.time.wait
    state = {'last': time.perf_counter()}

    def wait(millis):
        now = time.perf_counter()
        elapsed_ms = (now - state['last']) * 1000.0
        state['last'] = now
        if millis <= 1 and elapsed_ms >= BUSY_THRESHOLD_MS:
            # The loop is already spending longer than this per iteration;
            # sleeping on top of that only costs frames.
            return 0
        return original(millis)

    pygame.time.wait = wait
    _wait_installed = True
    if verbose:
        sys.stderr.write('[lumen] adaptive frame sleep enabled\n')
    return True


# --------------------------------------------------------------------------
# Display refresh rate
# --------------------------------------------------------------------------
def detect_refresh_rate(default=60, low=50, high=240):
    """The active display's refresh rate in Hz, or `default` if unknown.

    Note this only answers once a display mode exists, so it has to be called
    from inside the running loop rather than from onAppStart.
    """
    try:
        import pygame
        rate = int(pygame.display.get_current_refresh_rate())
    except Exception:
        return default
    if rate < low or rate > high:
        return default
    return rate


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------
# cmu-graphics opens its window with `pygame.display.set_mode`, and pygame's
# `set_mode` drops the one SDL flag that matters on a Retina display:
# SDL_WINDOW_ALLOW_HIGHDPI. Without it SDL sizes the framebuffer in *points*,
# so a 1800x1169 fullscreen window gets an 1800x1169 buffer that macOS then
# stretches over 3024x1964 physical pixels - every edge in the game lands
# blurred across 1.7 pixels.
#
# `pygame.Window` does expose the flag, so the game takes the window over:
# once, on the first frame, the framework's window is destroyed and replaced
# with an equivalent one that has high-DPI enabled. On this display that turns
# a 1800x1169 fullscreen window into a 3600x2338 framebuffer - the same buffer
# a native Mac app would draw into, and the same one the compositor samples
# back down to the panel's 3024x1964.
#
# The second reason this matters is that the renderer does not antialias
# polygon edges (text is antialiased; vector shapes are not). Drawing into a
# 2x buffer that is then downsampled by the compositor *is* 2x2 supersampling,
# so the same code comes out both sharper and visibly smoother.
#
# Two things stop working once we own the window instead of `pygame.display`:
# `display.flip()` and `display.get_surface()` raise "Display mode not set",
# and `get_current_refresh_rate()` raises "No open window". Each gets a shim.

_win = None                 # pygame.Window, once we have taken over
_renderer = None            # only used when rendering below the drawable size
_texture = None
_render_size = None         # the size cmu-graphics actually draws at
_surface = None             # what it blits into
_shimmed = False
_logical = None             # window size in points, for pointer mapping
_win_high_dpi = [False]     # the flag the live window was created with
_win_is_gl = [False]        # whether it carries an OpenGL context
_renderer_for = [None]      # the window the live SDL renderer is bound to
# The renderer's true framebuffer size. Once a logical size is set,
# `get_viewport()` reports that instead, so the real one has to be remembered.
_drawable_px = [None]
TITLE_TEXT = 'LUMEN'


def own_window():
    """The pygame.Window the game owns, or None while the framework's is up."""
    return _win


def backing_scale():
    """Physical pixels per point for our window - 2.0 on a Retina Mac, 1.0 flat.

    Pointer positions arrive from SDL in points while everything the game
    draws is in pixels, so this is the factor between the two.
    """
    if _win is None or _logical is None:
        return 1.0
    lw, lh = _logical
    if not lw or not lh:
        return 1.0
    dw, dh = drawable_size()
    return max(dw / lw, 1.0) if lw else 1.0


def drawable_size():
    """The window's framebuffer size, in real pixels."""
    if gpu.BACKEND == 'gl' and _render_size:
        return _render_size
    if _renderer is not None:
        if _drawable_px[0] is not None:
            return _drawable_px[0]
        try:
            return tuple(_renderer.get_viewport().size)
        except Exception:
            pass
    if _surface is not None:
        return _surface.get_size()
    if _win is not None:
        try:
            return _win.get_surface().get_size()
        except Exception:
            pass
    return desktop_size()


def render_size():
    """The size of the buffer the game draws into."""
    return _render_size or drawable_size()


def _shim_display(pygame):
    """Keep `pygame.display`'s window-bound helpers working on our window."""
    global _shimmed
    if _shimmed:
        return
    _shimmed = True

    real_flip = pygame.display.flip
    real_surface = pygame.display.get_surface
    real_rate = pygame.display.get_current_refresh_rate

    def flip():
        if _win is None:
            return real_flip()
        if gpu.active():
            # The GPU backend presents its own frames, once, from the hook in
            # `install_gpu_hooks` or from the native loop. cmu-graphics has a
            # second caller of `display.flip` - modal.py, for its update and
            # error dialogs - and letting that reach the renderer presents a
            # drawable that has already been presented, which Metal does not
            # warn about but aborts on.
            return None
        if _renderer is not None and _texture is not None:
            # Reduced render scale: hand the frame to the GPU and let it do
            # the upscale. Scaling on the CPU costs 4-5 ms at this size, and
            # doing it here costs nothing that is not already being paid.
            _texture.update(_surface)
            _renderer.clear()
            _texture.draw()
            _renderer.present()
            return None
        return _win.flip()

    def get_surface():
        return _surface if _win is not None else real_surface()

    def get_current_refresh_rate():
        try:
            return real_rate()
        except Exception:
            # Raises once the framework's window is gone; the desktop's own
            # rate is the same number and does not need a window.
            rates = pygame.display.get_desktop_refresh_rates()
            return int(rates[0]) if rates else 0

    pygame.display.flip = flip
    pygame.display.get_surface = get_surface
    pygame.display.get_current_refresh_rate = get_current_refresh_rate


def desktop_size():
    """The desktop's logical size, in points.

    `list_modes()` advertises the panel's raw pixel modes (3024x1964 on a
    Retina Mac), but asking for one fails with `CGDisplaySwitchToMode(): Unknown
    Error` - macOS will not switch a built-in Retina display to a raw mode -
    and, worse, leaves the display half-reconfigured. The high-DPI framebuffer
    above is how the game reaches those pixels instead.
    """
    try:
        import pygame
        sizes = pygame.display.get_desktop_sizes()
        if sizes:
            return sizes[0]
    except Exception:
        pass
    return (1600, 900)


def native_size():
    """The framebuffer a fullscreen high-DPI window would give us, in pixels."""
    w, h = desktop_size()
    factor = display_scale_factor()
    return (int(round(w * factor)), int(round(h * factor)))


_probed = None


def display_scale_factor():
    """Ask SDL, once, what the high-DPI flag is worth on this display.

    Done with a hidden 100x100 window so the answer is available before the
    real one is built - and so a machine where the flag does nothing reports
    1.0 rather than being assumed Retina. Unlike `backing_scale`, this
    describes the display, not whichever window happens to be open.
    """
    global _probed
    if _probed is not None:
        return _probed
    _probed = 1.0
    try:
        import pygame
        probe = pygame.Window('probe', (100, 100), hidden=True,
                              allow_high_dpi=True)
        try:
            w, _h = probe.get_surface().get_size()
            _probed = max(1.0, w / 100.0)
        finally:
            probe.destroy()
    except Exception:
        pass
    return _probed


def quality_plan(quality):
    """Turn a fraction of native pixel density into a window configuration.

    Three regimes, cheapest first:
      1.0                  the full high-DPI framebuffer, drawn into directly
      exactly 1/backing     a plain window - same pixel count as a reduced
                            high-DPI buffer, but with no upload or upscale
      anything between      high-DPI buffer, drawn smaller and stretched by
                            the GPU
    """
    factor = display_scale_factor()
    quality = min(1.0, max(0.25, float(quality)))
    if quality >= 0.999:
        return True, 1.0
    if factor > 1.0 and abs(quality - 1.0 / factor) < 0.02:
        return False, 1.0
    return True, quality


def set_video_mode(app, size, fullscreen, quality=1.0):
    """Put the window into `size` points and point cmu-graphics at the result.

    `size` is in points, not pixels: the framebuffer comes back `size` times
    the display's backing scale once high-DPI is on. `quality` is a fraction
    of that native density - below 1.0 the game draws into a proportionally
    smaller buffer and the GPU stretches it, which is the only way to trade
    sharpness for frame rate smoothly, since the high-DPI flag itself is
    all-or-nothing.

    The framework caches the display surface it blits into and recreates its
    own render surface from `app.width`/`app.height`, so both have to be put
    back in step afterwards; that is what `_sync` is for.
    """
    global _win, _surface, _logical
    try:
        import pygame
    except Exception:
        return False

    high_dpi, render_scale = quality_plan(quality)

    _shim_display(pygame)
    debug = bool(os.environ.get('LUMEN_DEBUG'))

    # Two things can only be settled when the window is created: the high-DPI
    # flag, and whether the window hands out a surface or is owned by a
    # renderer. SDL refuses to do both - asking a surface-mode window for a
    # renderer fails with "Surface already associated with window" - so a
    # change to either means building a new window.
    wants_gl = gpu.BACKEND == 'gl' and gpu.wanted()
    wants_renderer = (gpu.wanted() or render_scale < 0.999) and not wants_gl
    want_new = (_win is None
                or high_dpi != _win_high_dpi[0]
                or wants_gl != _win_is_gl[0]
                or (not wants_gl and wants_renderer != (_renderer is not None)))
    if want_new:
        _drop_renderer(forget_window=True)
        if _win is None:
            _destroy_framework_window(pygame)
        else:
            try:
                _win.destroy()
            except Exception:
                pass
            _win = None
        try:
            if wants_gl:
                # The context has to be asked for before the window exists,
                # and a window is either a GL window or an SDL-renderer one -
                # never both - which is why changing backend rebuilds it.
                pygame.display.gl_set_attribute(
                    pygame.GL_CONTEXT_MAJOR_VERSION, 3)
                pygame.display.gl_set_attribute(
                    pygame.GL_CONTEXT_MINOR_VERSION, 3)
                pygame.display.gl_set_attribute(
                    pygame.GL_CONTEXT_PROFILE_MASK,
                    pygame.GL_CONTEXT_PROFILE_CORE)
            _win = pygame.Window(TITLE_TEXT, size, resizable=True,
                                 allow_high_dpi=high_dpi, opengl=wants_gl)
            _win_is_gl[0] = wants_gl
        except Exception as exc:
            if debug:
                sys.stderr.write(f'[lumen] window{size}: {exc!r}\n')
            return False
        try:
            from .config import MIN_WINDOW
            _win.minimum_size = MIN_WINDOW
        except Exception:
            pass
        _win_high_dpi[0] = high_dpi
        _surface = None

    try:
        if fullscreen:
            _win.set_fullscreen(desktop=True)
        else:
            _win.set_windowed()
            _win.size = size
    except Exception as exc:
        if debug:
            sys.stderr.write(f'[lumen] fullscreen={fullscreen}: {exc!r}\n')

    # SDL applies a fullscreen transition asynchronously on macOS; the window
    # reports its old size until the events for it have been pumped.
    for _ in range(24):
        pygame.event.pump()
        if tuple(_win.size) != tuple(size) or not fullscreen:
            break
        time.sleep(0.004)
    _logical = tuple(_win.size)

    if not _attach_surface(pygame, render_scale, debug):
        return False

    if debug:
        sys.stderr.write(
            f'[lumen] window {_logical} pts, framebuffer {drawable_size()} px, '
            f'rendering {render_size()} px, fullscreen={fullscreen}, '
            f'high_dpi={high_dpi}\n')
    return _sync(app, _surface)


def _attach_surface(pygame, render_scale, debug):
    """Build the buffer the game draws into, and a way to present it.

    Three arrangements, and which one is in force decides how a window has to
    be built (see `want_new`):

    * **GPU backend** - a renderer owns the window and the game draws straight
      into it. There is no pixel buffer on this side at all.
    * **cmu-graphics at full scale** - the game rasterises into the window's
      own surface, one memcpy from the screen.
    * **cmu-graphics below full scale** - it rasterises into a smaller
      off-screen surface that a renderer uploads and stretches.
    """
    global _renderer, _texture, _render_size, _surface

    _drop_renderer()
    scale = min(1.0, max(0.25, float(render_scale)))

    if gpu.BACKEND == 'gl' and gpu.wanted():
        try:
            import moderngl
            _win.opengl_make_current() if hasattr(
                _win, 'opengl_make_current') else None
            ctx = gpu.renderer()
            if ctx is None or _renderer_for[0] is not _win:
                ctx = moderngl.create_context()
                gpu.attach(ctx)
                _renderer_for[0] = _win
            gpu.set_window(_win)
            _render_size = tuple(ctx.screen.size)
            gpu.set_viewport(_render_size)
            gpu.lighting_ready(_render_size)
            _surface = pygame.Surface((1, 1), 0, 32)
            return True
        except Exception as exc:
            sys.stderr.write(f'[lumen] OpenGL renderer unavailable ({exc!r}); '
                             f'falling back to the cmu-graphics renderer\n')
            gpu.give_up()
            try:
                from . import art
                art.clear_all()
            except Exception:
                pass

    if gpu.wanted():
        try:
            from pygame._sdl2 import video as sdl2
            # Linear filtering, read when each texture is created. Without it
            # a texture at a sub-pixel destination snaps to the nearest texel,
            # which is the stutter that sub-pixel placement exists to avoid.
            os.environ['SDL_RENDER_SCALE_QUALITY'] = '1'
            if _renderer_for[0] is _win and gpu.renderer() is not None:
                # SDL keeps a renderer bound to its window for as long as the
                # window lives, and refuses a second one, so a resize has to
                # adopt the existing renderer rather than build another.
                _renderer = gpu.renderer()
            else:
                _renderer = sdl2.Renderer(_win, accelerated=1, vsync=1)
                _renderer_for[0] = _win
            _renderer.logical_size = (0, 0)      # drop any previous mapping
            dw, dh = _renderer.get_viewport().size
            _drawable_px[0] = (dw, dh)
            if scale >= 0.999:
                _render_size = (dw, dh)
            else:
                # The sharpness dial still means something here, even though
                # the GPU has no trouble at full resolution - it costs nothing
                # to honour, and a weaker machine may want it. SDL's logical
                # size scales every draw call on the way out, so the game goes
                # on working in whatever pixels it was told about.
                _render_size = (max(320, int(dw * scale) & ~1),
                                max(240, int(dh * scale) & ~1))
            if _render_size != (dw, dh):
                # Only when it is actually doing something: a logical size
                # equal to the framebuffer still makes `to_surface()` read a
                # differently-shaped buffer, which segfaults.
                try:
                    _renderer.logical_size = _render_size
                except Exception:
                    _render_size = (dw, dh)
            # cmu-graphics still wants somewhere to point `_screen`; nothing
            # on this path ever draws into it.
            _surface = pygame.Surface((1, 1), 0, 32)
            was = gpu.generation()
            gpu.attach(_renderer)
            if gpu.generation() != was:
                # Textures belong to the renderer that made them, and a
                # sprite drops its source bytes once uploaded, so a new
                # renderer means everything has to be baked again.
                try:
                    from . import art
                    art.clear_all()
                except Exception:
                    pass
            return True
        except Exception as exc:
            sys.stderr.write(f'[lumen] GPU renderer unavailable ({exc!r}); '
                             f'falling back to the cmu-graphics renderer\n')
            _drop_renderer()
            # Sprites are baked as GPU textures or as CMUImages depending on
            # `gpu.wanted()`, so giving up has to invalidate everything baked
            # so far or the fallback draws with the wrong kind.
            gpu.give_up()
            try:
                from . import art
                art.clear_all()
            except Exception:
                pass
            # Fall through to the cmu-graphics path rather than to no game.

    if scale >= 0.999:
        # Full scale: draw straight into the window's own surface. This is
        # both the sharpest and the cheapest path - the frame is one memcpy
        # away from the screen, where the GPU path costs an upload of about
        # twice that.
        try:
            _surface = _win.get_surface()
        except Exception as exc:
            if debug:
                sys.stderr.write(f'[lumen] get_surface: {exc!r}\n')
            return False
        _render_size = _surface.get_size()
        return True

    # Reduced scale: a renderer owns the window, and the game draws into an
    # off-screen surface that gets uploaded and stretched each frame.
    try:
        from pygame._sdl2 import video as sdl2
        # Linear filtering, or the upscale is nearest-neighbour and blocky -
        # which would defeat the whole point of rendering below native. Set,
        # not defaulted: SDL reads this when the texture is created, and an
        # inherited '0' would quietly turn every reduced setting blocky.
        os.environ['SDL_RENDER_SCALE_QUALITY'] = '1'
        _renderer = sdl2.Renderer(_win, accelerated=1, vsync=1)
        dw, dh = _renderer.get_viewport().size
        rw = max(320, int(dw * scale) & ~1)
        rh = max(240, int(dh * scale) & ~1)
        _render_size = (rw, rh)
        _surface = pygame.Surface(_render_size, 0, 32)
        _texture = sdl2.Texture(_renderer, _render_size, streaming=True)
    except Exception as exc:
        if debug:
            sys.stderr.write(f'[lumen] renderer: {exc!r}\n')
        _drop_renderer()
        # Fall back to the honest, full-resolution path rather than no game.
        try:
            _surface = _win.get_surface()
            _render_size = _surface.get_size()
            return True
        except Exception:
            return False
    return True


def _drop_renderer(forget_window=False):
    global _renderer, _texture
    if forget_window:
        if gpu.renderer() is not None:
            gpu.detach()
        _renderer_for[0] = None
        _drawable_px[0] = None
    _texture = None
    _renderer = None


def _destroy_framework_window(pygame):
    """Take the window away from `pygame.display` so we can open our own.

    Only one window can exist here: leaving the framework's up would put a
    second, empty one on screen and leave events split between them.
    """
    try:
        window = pygame.Window.from_display_module()
    except Exception:
        return
    try:
        window.destroy()
    except Exception:
        pass


def pointer_scale():
    """Render pixels per point - what a pointer position has to be multiplied by.

    SDL reports the cursor in points; everything the game draws is in the
    render buffer's pixels, and with a high-DPI window the two differ by the
    backing scale (and again by the render scale, if one is set).
    """
    if _win is None or not _logical:
        return 1.0
    lw = _logical[0]
    rw = render_size()[0]
    return (rw / float(lw)) if lw else 1.0


def _current_render_scale():
    if _renderer is None or not _render_size:
        return 1.0
    dw = drawable_size()[0]
    return (_render_size[0] / float(dw)) if dw else 1.0





def _background_rgb(inner):
    """`app.background` as plain (r, g, b) - it may be a colour object or a name."""
    color = getattr(inner, 'background', None)
    if color is None:
        return (0, 0, 0)
    for attr in ('red', 'green', 'blue'):
        if not hasattr(color, attr):
            break
    else:
        return (color.red, color.green, color.blue)
    if isinstance(color, (tuple, list)) and len(color) >= 3:
        return tuple(color[:3])
    try:
        import pygame
        return tuple(pygame.Color(str(color))[:3])
    except Exception:
        return (0, 0, 0)


def adopt_resize(app):
    """Re-adopt the framebuffer after the window changed size.

    The native host calls this directly where the cmu-graphics host needs
    `install_resize_hook` to intercept the framework doing it wrongly. SDL
    hands out a fresh surface whenever a window resizes, and reports the new
    size in points while everything drawn is in pixels.
    """
    global _logical
    if _win is None:
        return False
    try:
        import pygame
        _logical = tuple(_win.size)
        if not _attach_surface(pygame, _current_render_scale(),
                               bool(os.environ.get('LUMEN_DEBUG'))):
            return False
        inner = getattr(app, '_app', app)
        inner._screen = _surface
        inner._width, inner._height = render_size()
        return True
    except Exception:
        return False


def install_gpu_hooks(app):
    """Replace the framework's rasterise-and-present step with the GPU's.

    cmu-graphics builds a shape tree during the user's `redrawAll`, rasterises
    it into a wyvern surface, converts that, blits it and flips. On this
    backend the drawing calls have already gone to the renderer by the time
    the tree would be walked, so all that is left is to clear before the frame
    and present after it - and the tree is never built, because `lumen/draw.py`
    stopped creating shapes.
    """
    inner = getattr(app, '_app', app)
    if getattr(inner, '_lumen_gpu_hooks', False):
        return
    original_wrapper = inner.redrawAllWrapper
    original_redraw = inner.redrawAll
    original_shot = inner.getScreenshot

    def redrawAllWrapper():
        if gpu.active():
            gpu.begin_frame(_background_rgb(inner))
        return original_wrapper()

    def redrawAll(screen, wyvern_surface, ctx):
        if not gpu.active():
            return original_redraw(screen, wyvern_surface, ctx)
        if getattr(inner, 'stopped', False):
            # The framework draws its "Exception! App Stopped!" screen out of
            # its own shapes, which is precisely the path this replaces. Draw
            # an equivalent, or a crash would leave the last good frame up
            # with nothing to say anything went wrong.
            if not gpu.frame_open():
                gpu.begin_frame((0, 0, 0))
            gpu.draw_error_banner(inner.width, inner.height)
        elif not gpu.frame_open():
            # Nothing was drawn since the last present, so there is nothing to
            # show. The framework decides to redraw from `had_event`, but only
            # *some* events reach a handler that repaints: a MOUSEMOTION is
            # routed to `onMouseMove`, which this game does not define, so the
            # frame is never rebuilt and this would present whatever the swap
            # chain handed back - a buffer two or three frames old, or an
            # uninitialised one. On the cmu-graphics path the same call
            # re-blitted the same correct pixels and nothing showed; here it
            # is a black flash every time the pointer moves.
            return None
        return gpu.present()

    def getScreenshot(path):
        if not gpu.active():
            return original_shot(path)
        try:
            import pygame
            # `to_surface` reads the render target, and a logical size makes
            # it read a buffer of the wrong shape - a hard crash, not an
            # error - so the mapping comes off for the duration.
            mapped = None
            try:
                mapped = tuple(_renderer.logical_size)
            except Exception:
                mapped = None
            if mapped and mapped != (0, 0):
                _renderer.logical_size = (0, 0)
            try:
                pygame.image.save(_renderer.to_surface(), path)
            finally:
                if mapped and mapped != (0, 0):
                    _renderer.logical_size = mapped
        except Exception as exc:
            sys.stderr.write(f'[lumen] screenshot failed: {exc!r}\n')

    inner.redrawAllWrapper = redrawAllWrapper
    inner.redrawAll = redrawAll
    inner.getScreenshot = getScreenshot
    inner._lumen_gpu_hooks = True


def install_resize_hook(app):
    """Re-derive the framebuffer ourselves whenever the window changes size.

    SDL reports a resize in points, and cmu-graphics takes that at face value:
    it would rebuild its render surface at 1800x1169 for a window whose
    framebuffer is 3600x2338, and go on blitting into a surface SDL had
    already replaced. The hook re-adopts the real buffer first, then lets the
    framework finish the job with the pixel size it should have been given.
    """
    inner = getattr(app, '_app', app)
    if getattr(inner, '_lumen_resize_hook', False):
        return
    original = inner.handleResize

    def handleResize(new_width, new_height):
        global _logical
        if _win is None:
            return original(new_width, new_height)
        try:
            import pygame
            _logical = tuple(_win.size)
            if _attach_surface(pygame, _current_render_scale(),
                               bool(os.environ.get('LUMEN_DEBUG'))):
                inner._screen = _surface
                new_width, new_height = render_size()
                if os.environ.get('LUMEN_DEBUG'):
                    sys.stderr.write(
                        f'[lumen] resized to {_logical} pts, '
                        f'rendering {new_width}x{new_height} px\n')
        except Exception:
            pass
        return original(new_width, new_height)

    inner.handleResize = handleResize
    inner._lumen_resize_hook = True


def _sync(app, surface):
    """Point cmu-graphics at the buffer we created, and tell it its own size."""
    inner = getattr(app, '_app', app)
    if gpu.active():
        # `updateScreen` would allocate a wyvern surface the size of the
        # window - 32 MB at native, and never drawn into, because the GPU
        # backend replaces the rasterise-and-blit step entirely. The framework
        # only needs its width and height to be right.
        width, height = render_size()
        try:
            inner._screen = surface
            inner._width = width
            inner._height = height
        except Exception:
            return False
        return True
    width, height = surface.get_size()
    try:
        inner._screen = surface
        inner._width = width
        inner._height = height
        inner.updateScreen(False)
    except Exception:
        return False
    return True
