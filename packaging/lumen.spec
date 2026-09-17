# -*- mode: python ; coding: utf-8 -*-
"""How LUMEN is packaged into one thing you can send somebody.

Driven by `packaging/build.py`, which makes the icon first and then hands the
result here through `LUMEN_ICON`. On macOS this produces `LUMEN.app`, a
directory bundle that starts fast; on Windows a single `LUMEN.exe`, because
one file is the whole point there.

What ships: the game, the two typefaces it carries for platforms that do not
have the ones it is set in (see `lumen/art.py`), and the four libraries it
draws and sounds itself with. What does not: the sound cache, which is built
on the machine that plays it - the game is a synthesiser, not a sample pack,
and a cache made here would be 100 MB of something the player's own first
launch makes in a few seconds.
"""

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(os.getcwd()))
if not os.path.exists(os.path.join(ROOT, 'lumen')):
    ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(SPEC)),
                                        '..'))

sys.path.insert(0, ROOT)
from lumen import __version__ as VERSION  # noqa: E402

ICON = os.environ.get('LUMEN_ICON') or None

# The typefaces, and the licences that have to travel with them.
FONTS = [(os.path.join(ROOT, 'lumen', 'fonts', name),
          os.path.join('lumen', 'fonts'))
         for name in sorted(os.listdir(os.path.join(ROOT, 'lumen', 'fonts')))]

# Nothing here is imported by the game, and several are large.
EXCLUDES = [
    'cmu_graphics', 'tkinter', 'unittest', 'pydoc_data', 'lib2to3',
    'pytest', 'setuptools', 'pip', 'wheel', 'distutils',
    'numpy.f2py', 'numpy.distutils', 'numpy.testing',
    'PIL.ImageQt', 'PIL.ImageTk', 'PIL.ImageShow',
    'matplotlib', 'scipy', 'pandas', 'IPython',
]

analysis = Analysis(
    [os.path.join(ROOT, 'native.py')],
    pathex=[ROOT],
    binaries=[],
    datas=FONTS,
    # Named here because nothing static mentions them: moderngl loads its
    # context backend by name, and numpy's random module reaches for
    # `secrets` from inside a compiled extension, where the analysis cannot
    # see it - which the game finds out about on its very first line of
    # synthesis.
    hiddenimports=collect_submodules('glcontext') + ['secrets'],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)

if sys.platform == 'darwin':
    exe = EXE(
        pyz,
        analysis.scripts,
        [],
        exclude_binaries=True,
        name='LUMEN',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=ICON,
    )
    collected = COLLECT(
        exe,
        analysis.binaries,
        analysis.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name='LUMEN',
    )
    app = BUNDLE(
        collected,
        name='LUMEN.app',
        icon=ICON,
        bundle_identifier='com.lumen.descent',
        version=VERSION,
        info_plist={
            'CFBundleName': 'LUMEN',
            'CFBundleDisplayName': 'LUMEN',
            'CFBundleShortVersionString': VERSION,
            'CFBundleVersion': VERSION,
            # Without this the window is drawn at point resolution and
            # stretched over the panel, which is exactly what the renderer
            # spends its time not doing.
            'NSHighResolutionCapable': True,
            'LSApplicationCategoryType': 'public.app-category.games',
            'LSMinimumSystemVersion': '11.0',
            'NSHumanReadableCopyright': 'LUMEN',
        },
    )
else:
    # One file: it unpacks itself into a temporary folder and runs from
    # there. `lumen/paths.py` is why that is safe - nothing the game writes
    # goes anywhere near it.
    exe = EXE(
        pyz,
        analysis.scripts,
        analysis.binaries,
        analysis.datas,
        [],
        name='LUMEN',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        # What Windows shows in the file's Properties, and the first thing
        # anybody checks when a download warns them about an unsigned .exe.
        version=(os.path.join(ROOT, 'packaging', 'windows_version.txt')
                 if sys.platform == 'win32' else None),
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=ICON,
    )
