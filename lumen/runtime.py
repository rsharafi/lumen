"""The window, and how the frame the game draws reaches it.

Three sizes are in play and they are not the same thing:

* the window, in **points** - what the OS sizes and places windows in, and
  what the pointer is reported in;
* the **drawable**, in real pixels - the window's framebuffer, twice the
  points on a Retina Mac and equal to them on a flat panel;
* the **render** size - the buffer the game actually draws into, which is the
  part of the drawable the frame lands in, times the sharpness dial.

The drawable is asked of SDL directly rather than worked out. This file used
to multiply the window's points by a backing scale it probed once at startup,
which is right until the window is dragged onto a display with a different
one: a Retina laptop's 2x applied to an external 1x monitor draws a frame
twice the size of the framebuffer, and only a corner of it is on screen.

There is one window for the life of the game. It always asks for a high-DPI
framebuffer, and the sharpness dial is applied by drawing into a smaller
offscreen canvas that the GPU stretches over the window (see `glx.set_output`)
- so changing it never tears the window down, never loses the GL context, and
never makes the window blink out and back in somewhere else.

The frame is composed for aspect ratios between `MIN_ASPECT` and
`MAX_ASPECT`. A window outside that range gets bars rather than a layout that
was never designed for it.
"""

import os
import sys

from . import gpu

# The shapes the game is composed for. Everything between a square and a
# 32:9 super-ultrawide was checked screen by screen: the offering's cards
# shrink to fit a square, and an ultrawide simply shows more of the chamber.
# Narrower than a square the title runs off the sides; wider than 32:9 the
# HUD's corners are further apart than anyone can watch at once.
MIN_ASPECT = 1.0
MAX_ASPECT = 32.0 / 9.0

# macOS puts a *desktop* fullscreen window inside the display's safe area. On
# a notched MacBook that is 38 points short of the top of the panel, and macOS
# fills the difference with black - a band across the full width that was
# never ours to draw into. Turning off fullscreen Spaces gets the whole panel.
# It has to be set before the window exists; SDL reads it when it picks the
# window's collection behaviour. `LUMEN_MAC_SPACES=1` takes the band back for
# anyone who would rather have a separate Space.
if sys.platform == 'darwin':
    os.environ.setdefault('SDL_VIDEO_MAC_FULLSCREEN_SPACES',
                          os.environ.get('LUMEN_MAC_SPACES', '0'))

# Without this, Windows treats the game as DPI-unaware and bitmap-stretches
# the whole window on any display scaled past 100% - a 1080p laptop at 150%
# gets every frame drawn at two thirds of its pixels and blurred up. Per-
# monitor v2 is the mode that also stays sharp when the window is dragged to a
# display with a different scale. SDL reads it when video starts.
if sys.platform == 'win32':
    os.environ.setdefault('SDL_WINDOWS_DPI_AWARENESS', 'permonitorv2')

TITLE_TEXT = 'LUMEN'

_win = None                 # the pygame.Window, once it exists
_fullscreen = [False]       # what we last asked the window to be
_context_for = [None]       # the window the live GL context belongs to

# (drawable, rect, render) the game is drawing at - changed only by `commit`.
_committed = [None]
# (drawable, rect, render) that is actually on screen. Usually the same as
# the committed output; during a resize that has not settled yet it is the
# committed render fitted into the new drawable. The pointer maps through
# this one, because it is what the player can see.
_shown = [None]


def own_window():
    """The pygame.Window the game draws into, or None before it exists."""
    return _win


# --------------------------------------------------------------------------
# SDL, directly
# --------------------------------------------------------------------------
# pygame-ce does not expose the drawable size of an OpenGL window, which
# display a window is on, or the usable area of that display. SDL does, and
# SDL is already loaded - so this finds the copy pygame is using and asks it.
# It has to be *that* copy: a second one would have no windows in it. If the
# lookup fails the functions below fall back to the old arithmetic, which is
# right on every single-display setup.
_sdl_state = {'tried': False, 'lib': None}


def _sdl_candidates():
    import ctypes
    import pygame
    here = os.path.dirname(os.path.abspath(pygame.__file__))
    found = []
    if sys.platform == 'win32':
        try:
            kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel32.GetModuleHandleW.restype = ctypes.c_void_p
            kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
            handle = kernel32.GetModuleHandleW('SDL2.dll')
            if handle:
                found.append(('SDL2.dll', handle))
        except Exception:
            pass
        found.append((os.path.join(here, 'SDL2.dll'), None))
    elif sys.platform == 'darwin':
        try:
            system = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
            system._dyld_image_count.restype = ctypes.c_uint32
            system._dyld_get_image_name.restype = ctypes.c_char_p
            system._dyld_get_image_name.argtypes = [ctypes.c_uint32]
            for i in range(system._dyld_image_count()):
                name = system._dyld_get_image_name(i) or b''
                if os.path.basename(name).startswith(b'libSDL2-2.0'):
                    found.append((name.decode(), None))
        except Exception:
            pass
        found.append((os.path.join(here, '.dylibs', 'libSDL2-2.0.0.dylib'),
                      None))
    else:
        try:
            with open('/proc/self/maps') as maps:
                for line in maps:
                    path = line.split()[-1]
                    if os.path.basename(path).startswith('libSDL2-2.0'):
                        found.append((path, None))
        except Exception:
            pass
    return found


def _sdl():
    """pygame's own SDL2 as a ctypes library, or None."""
    if _sdl_state['tried']:
        return _sdl_state['lib']
    _sdl_state['tried'] = True
    try:
        import ctypes
    except Exception:
        return None
    for path, handle in _sdl_candidates():
        try:
            lib = (ctypes.CDLL(path, handle=handle) if handle
                   else ctypes.CDLL(path))
            _declare(lib)
        except Exception:
            continue
        _sdl_state['lib'] = lib
        break
    return _sdl_state['lib']


def _declare(lib):
    import ctypes
    ptr, cint, u32 = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32
    pint = ctypes.POINTER(cint)
    lib.SDL_GetWindowFromID.argtypes = [u32]
    lib.SDL_GetWindowFromID.restype = ptr
    lib.SDL_GL_GetDrawableSize.argtypes = [ptr, pint, pint]
    lib.SDL_GL_GetDrawableSize.restype = None
    lib.SDL_GetWindowSize.argtypes = [ptr, pint, pint]
    lib.SDL_GetWindowSize.restype = None
    lib.SDL_GetWindowFlags.argtypes = [ptr]
    lib.SDL_GetWindowFlags.restype = u32
    lib.SDL_GetWindowDisplayIndex.argtypes = [ptr]
    lib.SDL_GetWindowDisplayIndex.restype = cint
    lib.SDL_GetWindowBordersSize.argtypes = [ptr, pint, pint, pint, pint]
    lib.SDL_GetWindowBordersSize.restype = cint
    lib.SDL_GetDisplayBounds.argtypes = [cint, ctypes.POINTER(_SDLRect())]
    lib.SDL_GetDisplayBounds.restype = cint
    lib.SDL_GetDisplayUsableBounds.argtypes = [cint,
                                               ctypes.POINTER(_SDLRect())]
    lib.SDL_GetDisplayUsableBounds.restype = cint


_rect_type = []


def _SDLRect():
    if not _rect_type:
        import ctypes

        class SDLRect(ctypes.Structure):
            _fields_ = [('x', ctypes.c_int), ('y', ctypes.c_int),
                        ('w', ctypes.c_int), ('h', ctypes.c_int)]

        _rect_type.append(SDLRect)
    return _rect_type[0]


def _sdl_window():
    """(lib, SDL_Window*) for our window, or (None, None)."""
    lib = _sdl()
    if lib is None or _win is None:
        return None, None
    try:
        handle = lib.SDL_GetWindowFromID(int(_win.id))
    except Exception:
        return None, None
    return (lib, handle) if handle else (None, None)


_WINDOW_FULLSCREEN = 0x00000001
_WINDOW_MINIMIZED = 0x00000040


def is_minimized():
    lib, handle = _sdl_window()
    if handle is None:
        return False
    return bool(lib.SDL_GetWindowFlags(handle) & _WINDOW_MINIMIZED)


def is_fullscreen():
    """Whether the window is covering its display - by our doing or macOS's."""
    if _mac_space_fullscreen():
        return True
    lib, handle = _sdl_window()
    if handle is None:
        return _fullscreen[0]
    return bool(lib.SDL_GetWindowFlags(handle) & _WINDOW_FULLSCREEN)


def display_index():
    """Which display the window is on - the one it is mostly over."""
    lib, handle = _sdl_window()
    if handle is None:
        return 0
    index = lib.SDL_GetWindowDisplayIndex(handle)
    return index if index >= 0 else 0


def display_bounds(usable=False, index=None):
    """(x, y, w, h) of a display in window coordinates.

    `usable` leaves out the menu bar, the Dock and the taskbar. Defaults to
    the display the window is on, or the primary one before there is one.
    """
    if index is None:
        index = display_index()
    lib = _sdl()
    if lib is not None:
        rect = _SDLRect()()
        import ctypes
        call = (lib.SDL_GetDisplayUsableBounds if usable
                else lib.SDL_GetDisplayBounds)
        try:
            if call(index, ctypes.byref(rect)) == 0 and rect.w > 0:
                return (rect.x, rect.y, rect.w, rect.h)
        except Exception:
            pass
    try:
        import pygame
        sizes = pygame.display.get_desktop_sizes()
        w, h = sizes[index if index < len(sizes) else 0]
        return (0, 0, w, h)
    except Exception:
        return (0, 0, 1600, 900)


def desktop_size():
    """The size of the window's display, in the units windows are sized in."""
    return display_bounds()[2:]


def _borders():
    """The window frame's (top, left, bottom, right), or zeros."""
    lib, handle = _sdl_window()
    if handle is None:
        return (0, 0, 0, 0)
    import ctypes
    top, left, bottom, right = (ctypes.c_int(), ctypes.c_int(),
                                ctypes.c_int(), ctypes.c_int())
    try:
        if lib.SDL_GetWindowBordersSize(handle, ctypes.byref(top),
                                        ctypes.byref(left),
                                        ctypes.byref(bottom),
                                        ctypes.byref(right)) != 0:
            return (0, 0, 0, 0)
    except Exception:
        return (0, 0, 0, 0)
    return (top.value, left.value, bottom.value, right.value)


_probed = None


def display_scale_factor():
    """What the high-DPI flag is worth on the primary display.

    Only the fallback now, for when SDL cannot be asked about the window
    itself. Done with a hidden 100x100 window so the answer is available
    before the real one exists.
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


def drawable_size():
    """The window's framebuffer, in real pixels. (0, 0) while minimised."""
    if _win is None:
        return (0, 0)
    lib, handle = _sdl_window()
    if handle is not None:
        import ctypes
        w, h = ctypes.c_int(), ctypes.c_int()
        lib.SDL_GL_GetDrawableSize(handle, ctypes.byref(w), ctypes.byref(h))
        return (max(0, w.value), max(0, h.value))
    factor = display_scale_factor()
    w, h = _win.size
    return (int(round(w * factor)), int(round(h * factor)))


def backing_scale(drawable=None):
    """Drawable pixels per window point - 2.0 on a Retina Mac, 1.0 flat.

    Asked of AppKit on a Mac rather than divided out. While macOS animates a
    window into or out of a fullscreen Space the drawable is already its new
    size and SDL goes on reporting the window's old one for half a second,
    and a ratio of the two puts the pointer somewhere else entirely.
    """
    if _win is None:
        return 1.0
    factor = _mac_backing_scale()
    if factor:
        return factor
    w = _win.size[0]
    dw = (drawable or drawable_size())[0]
    return (dw / float(w)) if w and dw else 1.0


def window_points():
    """The window's size in points, as it is now.

    From the drawable rather than from SDL's own idea of the window, which
    runs half a second behind while macOS animates a window out of a
    fullscreen Space - long enough to save the fullscreen size as the size to
    reopen at.
    """
    if _win is None:
        return None
    drawable = drawable_size()
    if drawable[0] <= 0 or drawable[1] <= 0:
        return None
    k = backing_scale(drawable)
    return (int(round(drawable[0] / k)), int(round(drawable[1] / k)))


def render_size():
    """The size of the buffer the game draws into."""
    if _committed[0] is not None:
        return _committed[0][2]
    return drawable_size()


# --------------------------------------------------------------------------
# Display refresh rate
# --------------------------------------------------------------------------
def detect_refresh_rate(default=60, low=50, high=240):
    """The refresh rate of the display the window is on, or `default`."""
    try:
        import pygame
        rates = pygame.display.get_desktop_refresh_rates()
        rate = int(rates[min(display_index(), len(rates) - 1)])
    except Exception:
        return default
    if rate < low or rate > high:
        return default
    return rate


# --------------------------------------------------------------------------
# AppKit, for the three things SDL does not say
# --------------------------------------------------------------------------
# The window's backing scale, whether macOS has taken it into a fullscreen
# Space of its own (the green button does that, and SDL does not report it),
# and the notch. Each is one message to an Objective-C object, sent through
# `ctypes` rather than a dependency on pyobjc. Anything that goes wrong answers
# as if there were nothing to say.
_objc_state = {'tried': False, 'lib': None}


def _objc():
    if _objc_state['tried']:
        return _objc_state['lib']
    _objc_state['tried'] = True
    if sys.platform != 'darwin':
        return None
    try:
        import ctypes
        import ctypes.util
        ctypes.CDLL(ctypes.util.find_library('AppKit'))
        lib = ctypes.CDLL(ctypes.util.find_library('objc'))
        lib.objc_getClass.restype = ctypes.c_void_p
        lib.objc_getClass.argtypes = [ctypes.c_char_p]
        lib.sel_registerName.restype = ctypes.c_void_p
        lib.sel_registerName.argtypes = [ctypes.c_char_p]
        _objc_state['lib'] = lib
    except Exception:
        pass
    return _objc_state['lib']


def _send(obj, selector, restype, arg=None):
    """`[obj selector]` or `[obj selector:arg]`. Not for struct returns."""
    import ctypes
    lib = _objc()
    if lib is None or not obj:
        return None
    fn = _objc_state.get('send')
    if fn is None:
        fn = _objc_state['send'] = ctypes.CDLL(None).objc_msgSend
    fn.restype = restype
    fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    return fn(ctypes.c_void_p(obj),
              ctypes.c_void_p(lib.sel_registerName(selector)),
              ctypes.c_void_p(arg))


def _ns_window():
    if sys.platform != 'darwin' or _win is None:
        return None
    try:
        return int(_win.handle) or None
    except Exception:
        return None


# NSWindowStyleMaskFullScreen
_NS_FULLSCREEN = 1 << 14


def _mac_space_fullscreen():
    """Whether macOS has the window in a fullscreen Space of its own."""
    import ctypes
    try:
        mask = _send(_ns_window(), b'styleMask', ctypes.c_uint64)
    except Exception:
        return False
    return bool(mask and mask & _NS_FULLSCREEN)


def _mac_backing_scale():
    """The window's backing scale as AppKit has it, or None."""
    import ctypes
    try:
        factor = _send(_ns_window(), b'backingScaleFactor', ctypes.c_double)
    except Exception:
        return None
    return float(factor) if factor and factor > 0.0 else None


def safe_area_top():
    """The top inset of the display with keyboard focus, in points.

    Fullscreen covers the whole panel, so the strip behind a MacBook's camera
    housing is part of our framebuffer: real pixels either side of it, and
    nothing at all behind it. Only asked on Apple silicon - every Mac with a
    notch is one, and the call returns a structure, which on Intel would need
    a different message send.
    """
    if sys.platform != 'darwin':
        return 0.0
    import platform
    if platform.machine() != 'arm64':
        return 0.0
    try:
        import ctypes

        class _Insets(ctypes.Structure):
            _fields_ = [('top', ctypes.c_double), ('left', ctypes.c_double),
                        ('bottom', ctypes.c_double), ('right', ctypes.c_double)]

        lib = _objc()
        if lib is None:
            return 0.0
        screen = _send(lib.objc_getClass(b'NSScreen'), b'mainScreen',
                       ctypes.c_void_p)
        if screen:
            return max(0.0, float(_send(screen, b'safeAreaInsets',
                                        _Insets).top))
    except Exception:
        pass
    return 0.0


def safe_top_fraction():
    """How much of the frame's height, from its top, is behind the notch.

    A fraction rather than a count of pixels, because the render buffer can
    be smaller than the drawable and only a ratio is the same number in both;
    multiplying it by the design height gives the design units the HUD has to
    come down by.

    Only when the window is actually standing on that strip: fullscreen, and
    as tall as its display. In a window macOS has already put us below the
    menu bar, and under `LUMEN_MAC_SPACES=1` SDL keeps clear of the strip
    itself. Bars above the frame count against the inset too - a frame that
    starts below the notch has nothing to move out of the way of.
    """
    output = _shown[0]
    if (output is None or _win is None or not _fullscreen[0]
            or sys.platform != 'darwin'):
        return 0.0
    drawable, rect, _render = output
    k = backing_scale(drawable)
    if drawable[1] / k < display_bounds()[3] - 1:
        return 0.0
    inset = safe_area_top()
    if inset <= 0.0:
        return 0.0
    inset_px = inset * k
    covered = inset_px - rect[1]
    if covered <= 0.0 or rect[3] <= 0:
        return 0.0
    return max(0.0, min(0.5, covered / float(rect[3])))


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------
def fit_rect(drawable, lo=MIN_ASPECT, hi=MAX_ASPECT):
    """Where the frame goes in a drawable: all of it, unless the shape is
    outside what the game is composed for, in which case the largest centred
    rectangle that is not. (x, y, w, h), top-left origin, whole pixels."""
    dw, dh = max(1, int(drawable[0])), max(1, int(drawable[1]))
    aspect = dw / float(dh)
    if aspect < lo:
        w, h = dw, max(1, int(round(dw / lo)))
    elif aspect > hi:
        w, h = max(1, int(round(dh * hi))), dh
    else:
        return (0, 0, dw, dh)
    return ((dw - w) // 2, (dh - h) // 2, w, h)


def plan(quality, drawable=None):
    """The output a drawable should get at a sharpness: (drawable, rect, render)."""
    if drawable is None:
        drawable = drawable_size()
    rect = fit_rect(drawable)
    q = min(1.0, max(0.25, float(quality)))
    render = (max(1, int(round(rect[2] * q))), max(1, int(round(rect[3] * q))))
    return (tuple(drawable), rect, render)


def committed():
    return _committed[0]


def commit(app, output):
    """Draw at `output` from the next frame on, and tell the app its size."""
    drawable, rect, render = output
    _committed[0] = output
    _show(output)
    gpu.lighting_ready(render)
    _tell_app_size(app)


def show_interim(drawable):
    """The window changed size and the game has not caught up yet.

    Until it does, the last committed frame is shown fitted into the new
    drawable at its own aspect ratio - bars rather than a stretch - so a
    window mid-resize never shows the frame squashed, cropped, or drawn into
    a corner of a framebuffer it no longer matches.
    """
    output = _committed[0]
    if output is None:
        return
    render = output[2]
    rect = fit_rect(drawable, render[0] / float(render[1]),
                    render[0] / float(render[1]))
    _show((tuple(drawable), rect, render))


def _show(output):
    drawable, rect, render = output
    _shown[0] = output
    gpu.set_output(render, drawable, rect)


def pointer_to_render(px, py):
    """A pointer position in window points -> render-buffer pixels."""
    output = _shown[0]
    if _win is None or output is None:
        return (float(px), float(py))
    drawable, rect, render = output
    k = backing_scale(drawable)
    if not rect[2] or not rect[3]:
        return (float(px), float(py))
    x = (px * k - rect[0]) * render[0] / float(rect[2])
    y = (py * k - rect[1]) * render[1] / float(rect[3])
    return (x, y)


def render_to_pointer(x, y):
    """The inverse of `pointer_to_render`."""
    output = _shown[0]
    if _win is None or output is None:
        return (float(x), float(y))
    drawable, rect, render = output
    k = backing_scale(drawable)
    if not render[0] or not render[1]:
        return (float(x), float(y))
    px = (x * rect[2] / float(render[0]) + rect[0]) / k
    py = (y * rect[3] / float(render[1]) + rect[1]) / k
    return (px, py)


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------
def default_window_size():
    """A first window that suits the display it opens on.

    A fixed 1280x720 is most of a small laptop's screen and a postage stamp
    on a 4K monitor - more so on Windows, where a DPI-aware window is sized
    in pixels rather than in points. Four-fifths of the usable area, at
    16:9, reads as the same window everywhere.
    """
    from .config import MIN_WINDOW
    _x, _y, uw, uh = display_bounds(usable=True, index=0)
    w = uw * 0.8
    h = w * 9.0 / 16.0
    if h > uh * 0.8:
        h = uh * 0.8
        w = h * 16.0 / 9.0
    return (max(MIN_WINDOW[0], int(w)), max(MIN_WINDOW[1], int(h)))


def fit_window_size(size):
    """A windowed size that fits on the window's display, frame and all."""
    from .config import MIN_WINDOW
    _x, _y, uw, uh = display_bounds(usable=True)
    top, left, bottom, right = _borders()
    max_w = max(MIN_WINDOW[0], uw - left - right)
    max_h = max(MIN_WINDOW[1], uh - top - bottom)
    w = int(min(max(int(size[0]), MIN_WINDOW[0]), max_w))
    h = int(min(max(int(size[1]), MIN_WINDOW[1]), max_h))
    return (w, h)


def center_window():
    """Centre the window, frame included, in its display's usable area."""
    if _win is None:
        return
    x, y, uw, uh = display_bounds(usable=True)
    top, left, bottom, right = _borders()
    w, h = _win.size
    fx = x + (uw - (w + left + right)) // 2 + left
    fy = y + (uh - (h + top + bottom)) // 2 + top
    try:
        _win.position = (max(x + left, fx), max(y + top, fy))
    except Exception:
        pass


def open_window(app, size, fullscreen):
    """Create the one window and its GL context. False if either fails."""
    global _win
    debug = bool(os.environ.get('LUMEN_DEBUG'))
    if _win is None:
        try:
            import pygame
            # The context has to be asked for before the window exists.
            pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MAJOR_VERSION, 3)
            pygame.display.gl_set_attribute(pygame.GL_CONTEXT_MINOR_VERSION, 3)
            pygame.display.gl_set_attribute(pygame.GL_CONTEXT_PROFILE_MASK,
                                            pygame.GL_CONTEXT_PROFILE_CORE)
            # Hidden until it is the right size in the right place, so it
            # does not appear in one spot and jump to another.
            _win = pygame.Window(TITLE_TEXT, fit_size_primary(size),
                                 position=pygame.WINDOWPOS_CENTERED,
                                 resizable=True, allow_high_dpi=True,
                                 opengl=True, hidden=True)
        except Exception as exc:
            sys.stderr.write(f'[lumen] could not open a window: {exc!r}\n')
            return False
        try:
            from .config import MIN_WINDOW
            _win.minimum_size = MIN_WINDOW
        except Exception:
            pass
        _win.size = fit_window_size(size)
        center_window()
        if fullscreen:
            set_fullscreen(True, size)
        _win.show()
    if not _attach_context(debug):
        return False
    if debug:
        sys.stderr.write(
            f'[lumen] window {tuple(_win.size)} pts, drawable '
            f'{drawable_size()} px, fullscreen={fullscreen}, '
            f'display {display_index()} {display_bounds()}\n')
    return True


def fit_size_primary(size):
    """`fit_window_size` for a window that does not exist yet."""
    from .config import MIN_WINDOW
    _x, _y, uw, uh = display_bounds(usable=True, index=0)
    return (int(min(max(int(size[0]), MIN_WINDOW[0]), max(MIN_WINDOW[0], uw))),
            int(min(max(int(size[1]), MIN_WINDOW[1]), max(MIN_WINDOW[1], uh))))


def set_fullscreen(on, windowed_size):
    """Cover the window's display, or go back to a window of `windowed_size`.

    Nothing is re-measured here. On macOS the transition finishes a frame or
    two later than the call returns, so the game picks up the new drawable
    the same way it picks up any other resize - see `Game.sync_display`.
    """
    if _win is None:
        return False
    try:
        if not on and _mac_space_fullscreen():
            # macOS put it there, with the green button, and SDL does not
            # know: asking SDL for a window is a no-op. Leave the Space the
            # way it was entered, and macOS puts the window back as it was.
            _send(_ns_window(), b'toggleFullScreen:', None)
        elif on and _mac_space_fullscreen():
            pass
        elif on:
            _win.set_fullscreen(desktop=True)
        else:
            _win.set_windowed()
            _win.size = fit_window_size(windowed_size)
            center_window()
    except Exception as exc:
        if os.environ.get('LUMEN_DEBUG'):
            sys.stderr.write(f'[lumen] fullscreen={on}: {exc!r}\n')
        return False
    _fullscreen[0] = bool(on)
    return True


def _attach_context(debug=False):
    """Make the window's GL context current, and hand it to the renderer."""
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
        return True
    except Exception as exc:
        sys.stderr.write(f'[lumen] OpenGL renderer unavailable: {exc!r}\n')
        if debug:
            import traceback
            traceback.print_exc(file=sys.stderr)
        return False


def _tell_app_size(app):
    """Put the app's idea of its size in step with the render buffer.

    The game derives its whole design scale from `app.width`/`app.height`, so
    these have to be the buffer it draws into, in pixels.
    """
    inner = getattr(app, '_app', app)
    width, height = render_size()
    try:
        inner._width, inner._height = width, height
    except Exception:
        pass
