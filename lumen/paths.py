"""Where the game keeps what it writes.

Run from a checkout, that is beside the game, as it always was: the save is
`lumen_save.json` in the repository and the sound cache is `.sound_cache`.

Run as a packaged app it cannot be. A macOS app bundle is signed, and writing
inside one breaks the signature; a single-file Windows build unpacks itself
into a temporary folder that is deleted when the game quits, which would take
every save with it. So a packaged game writes where each platform keeps an
application's own files, and the records survive both the game closing and
the player downloading a newer build.
"""

import os
import sys

NAME = 'LUMEN'


def packaged():
    """True inside a PyInstaller build."""
    return bool(getattr(sys, 'frozen', False))


def _repo():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ensure(path):
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        import tempfile
        fallback = os.path.join(tempfile.gettempdir(), NAME)
        os.makedirs(fallback, exist_ok=True)
        return fallback


def data_dir():
    """Records and settings."""
    if not packaged():
        return _repo()
    home = os.path.expanduser('~')
    if sys.platform == 'darwin':
        base = os.path.join(home, 'Library', 'Application Support', NAME)
    elif sys.platform == 'win32':
        base = os.path.join(os.environ.get('APPDATA')
                            or os.path.join(home, 'AppData', 'Roaming'), NAME)
    else:
        base = os.path.join(os.environ.get('XDG_DATA_HOME')
                            or os.path.join(home, '.local', 'share'),
                            NAME.lower())
    return _ensure(base)


def sound_cache():
    """The synthesised sound, kept so it is only synthesised once a machine."""
    override = os.environ.get('LUMEN_SOUND_CACHE')
    if override:
        return _ensure(override)
    if not packaged():
        return os.path.join(_repo(), '.sound_cache')
    home = os.path.expanduser('~')
    if sys.platform == 'darwin':
        base = os.path.join(home, 'Library', 'Caches', NAME)
    elif sys.platform == 'win32':
        base = os.path.join(os.environ.get('LOCALAPPDATA')
                            or os.path.join(home, 'AppData', 'Local'),
                            NAME, 'Cache')
    else:
        base = os.path.join(os.environ.get('XDG_CACHE_HOME')
                            or os.path.join(home, '.cache'), NAME.lower())
    return _ensure(os.path.join(base, 'sound'))
