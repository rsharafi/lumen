"""Host-level window and display plumbing.

What is left here after the framework went: opening the one high-DPI OpenGL
window the game draws into, working out how many real pixels that is against
how many points the pointer speaks in, and asking the display what it can do -
its refresh rate, its scale factor, the notch's height on the machines that
have one.

This used to be twice the size, and most of what is gone was two measured
workarounds for how cmu-graphics presented a frame: asking for `'RGBX'`
instead of `'RGBA'` to dodge a redundant per-pixel blend (5.62 ms a frame at
1280x720, down to 0.45 ms), and skipping the framework loop's unconditional
one-millisecond sleep when the frame was already over budget. Neither has
anything to hook now - `lumen/host.py` owns the loop outright and presents
through OpenGL - so both went with it.

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

# macOS puts a *desktop* fullscreen window inside the display's safe area. On
# a notched MacBook that is 38 points short of the top of the panel, so the
# game got an 1800x1130 window on an 1800x1169 screen and macOS filled the
# difference with black - a band across the full width that was never ours to
# draw into. The comments above, and the 3600x2338 measurements the rest of
# this file is built on, all assume the whole panel; this is how we get it
# back. It has to be set before the window is created - SDL reads it when it
# picks the window's collection behaviour, and setting it afterwards does
# nothing - which is why it lives at import time rather than in
# `set_video_mode`.
#
# The trade is macOS's own fullscreen behaviour: no separate Space, and
# Mission Control treats the window as an ordinary one. That is the usual
# bargain for a game, and `LUMEN_MAC_SPACES=1` takes the black band back for
# anyone who would rather have it.
if sys.platform == 'darwin':
    os.environ.setdefault('SDL_VIDEO_MAC_FULLSCREEN_SPACES',
                          os.environ.get('LUMEN_MAC_SPACES', '0'))

_win = None                 # pygame.Window, once we have taken over
_fullscreen = [False]       # whether the live window is covering the display
_render_size = None         # the size cmu-graphics actually draws at
_logical = None             # window size in points, for pointer mapping
_win_high_dpi = [False]     # the flag the live window was created with
_context_for = [None]       # the window the live GL context belongs to
# The renderer's true framebuffer size. Once a logical size is set,
# `get_viewport()` reports that instead, so the real one has to be remembered.
_drawable_px = [None]
TITLE_TEXT = 'LUMEN'


def own_window():
    """The pygame.Window the game owns, or None while the framework's is up."""
    return _win


_safe_top = [None]


def safe_area_top():
    """The display's unusable top inset, in points.

    Now that fullscreen covers the whole panel, the strip behind a MacBook's
    camera housing is part of our framebuffer: real pixels either side of it,
    and nothing at all behind it. Anything the HUD pins to the very top of the
    screen has to sit below that, or the boss's name spends the fight inside
    the notch.

    macOS knows the number and SDL does not expose it, so this asks AppKit
    directly through `ctypes` rather than taking a dependency on pyobjc for
    one float. Anything that goes wrong - a different platform, an older
    macOS, no screen - answers zero, which is the right answer everywhere
    there is no notch anyway.
    """
    if _safe_top[0] is not None:
        return _safe_top[0]
    _safe_top[0] = 0.0
    if sys.platform != 'darwin':
        return 0.0
    try:
        import ctypes
        import ctypes.util

        class _Insets(ctypes.Structure):
            _fields_ = [('top', ctypes.c_double), ('left', ctypes.c_double),
                        ('bottom', ctypes.c_double), ('right', ctypes.c_double)]

        ctypes.CDLL(ctypes.util.find_library('AppKit'))
        objc = ctypes.CDLL(ctypes.util.find_library('objc'))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]

        def send(obj, selector, restype):
            fn = ctypes.CDLL(None).objc_msgSend
            fn.restype = restype
            fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            return fn(ctypes.c_void_p(obj),
                      ctypes.c_void_p(objc.sel_registerName(selector)))

        screen = send(objc.objc_getClass(b'NSScreen'), b'mainScreen',
                      ctypes.c_void_p)
        if screen:
            _safe_top[0] = float(send(screen, b'safeAreaInsets', _Insets).top)
    except Exception:
        pass
    return _safe_top[0]


def safe_top_fraction():
    """The inset as a fraction of the window's height, where it applies.

    A fraction rather than a count of pixels, because there are three
    different pixels in play - points, the framebuffer, and the possibly
    smaller buffer the game draws into below full sharpness - and only a
    ratio is the same number in all of them. Multiplying it by the design
    height gives the design units the HUD has to come down by, at any
    setting.

    Three ways it does not apply. In a window, because macOS has already
    placed us below the menu bar. On a screen with no notch, because the
    inset is zero. And under `LUMEN_MAC_SPACES=1`, because SDL is then
    keeping the window clear of the strip itself - so the test is not "are we
    fullscreen" but "is our window actually standing on the part of the panel
    the notch is in", which is true only when it is as tall as the desktop.
    """
    if not _fullscreen[0] or _logical is None:
        return 0.0
    desktop = desktop_size()
    if not desktop or _logical[1] < desktop[1] - 1:
        return 0.0
    height = float(_logical[1])
    if height <= 0.0:
        return 0.0
    return max(0.0, min(0.5, safe_area_top() / height))


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

    """
    global _win, _logical
    try:
        import pygame
    except Exception:
        return False

    high_dpi, _render_scale = quality_plan(quality)
    debug = bool(os.environ.get('LUMEN_DEBUG'))

    # The high-DPI flag can only be settled when the window is created, so
    # changing quality across that boundary means building a new window.
    if _win is None or high_dpi != _win_high_dpi[0]:
        if _win is not None:
            try:
                _win.destroy()
            except Exception:
                pass
            _win = None
        try:
            # The context has to be asked for before the window exists.
            pygame.display.gl_set_attribute(
                pygame.GL_CONTEXT_MAJOR_VERSION, 3)
            pygame.display.gl_set_attribute(
                pygame.GL_CONTEXT_MINOR_VERSION, 3)
            pygame.display.gl_set_attribute(
                pygame.GL_CONTEXT_PROFILE_MASK,
                pygame.GL_CONTEXT_PROFILE_CORE)
            _win = pygame.Window(TITLE_TEXT, size, resizable=True,
                                 allow_high_dpi=high_dpi, opengl=True)
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

    _fullscreen[0] = bool(fullscreen)
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

    if not _attach_context(debug):
        return False
    _tell_app_size(app)

    if debug:
        sys.stderr.write(
            f'[lumen] window {_logical} pts, framebuffer {drawable_size()} px, '
            f'rendering {render_size()} px, fullscreen={fullscreen}, '
            f'high_dpi={high_dpi}\n')
    return True


def _attach_context(debug=False):
    """Make the window's GL context current, and hand it to the backend.

    There is no pixel buffer on this side at all: the renderer owns the
    window and the game draws straight into it. This used to be one of three
    arrangements - the other two rasterised into a surface for cmu-graphics,
    at full scale or below it - and picking between them was most of why
    creating a window was as involved as it was.
    """
    global _render_size
    try:
        import moderngl
        if hasattr(_win, 'opengl_make_current'):
            _win.opengl_make_current()
        ctx = gpu.renderer()
        if ctx is None or _context_for[0] is not _win:
            ctx = moderngl.create_context()
            gpu.attach(ctx)
            _context_for[0] = _win
        gpu.set_window(_win)
        # From the window, not from `ctx.screen`: moderngl fixes the default
        # framebuffer's size when the context is made and never revises it,
        # so after a resize - and after the window goes fullscreen, which is
        # every time - it still describes the window the context was born in.
        # The window's own size in points times the display's backing scale
        # is the drawable, and it agrees with `glGetIntegerv(GL_VIEWPORT)`
        # exactly.
        factor = display_scale_factor() if _win_high_dpi[0] else 1.0
        _render_size = (max(1, int(round(_win.size[0] * factor))),
                        max(1, int(round(_win.size[1] * factor))))
        _drawable_px[0] = _render_size
        gpu.set_viewport(_render_size)
        gpu.lighting_ready(_render_size)
        return True
    except Exception as exc:
        sys.stderr.write(f'[lumen] OpenGL renderer unavailable: {exc!r}\n')
        if debug:
            import traceback
            traceback.print_exc(file=sys.stderr)
        return False








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
        _logical = tuple(_win.size)
        if not _attach_context(bool(os.environ.get('LUMEN_DEBUG'))):
            return False
        _tell_app_size(app)
        return True
    except Exception:
        return False


def _tell_app_size(app):
    """Put the app's idea of its size in step with the framebuffer.

    The game derives its whole design scale from `app.width`/`app.height`, so
    these have to be the drawable in *pixels* and not the window in points -
    a two-times display otherwise draws a 1280x720 game into the top-left
    quarter of a 2560x1440 buffer, which is exactly what it did.

    Done here rather than only on a resize event, because the first frame has
    not had one: the game came up correct only because going fullscreen
    happened to raise one on the way.
    """
    inner = getattr(app, '_app', app)
    width, height = render_size()
    try:
        inner._width, inner._height = width, height
    except Exception:
        pass






