"""Persistent records. One small JSON file beside the game."""

import json
import os

from . import paths

# `load` only restores keys listed here, so a setting missing from this dict
# is written on every change and silently dropped on the next launch.
DEFAULT = {
    'best_score': 0,
    'best_floor': 0,
    'runs': 0,
    'wins': 0,
    'total_kills': 0,
    'sound': True,
    # Display preferences: the sharpness dial, where AUTO last settled, and
    # whether the game was left fullscreen. -1 means the player has never
    # touched the dial, so the right starting rung depends on which renderer
    # is live - see `Game.default_quality_index`. Any other value is a
    # deliberate choice and is left alone.
    'display': -1,
    'auto': 1,
    'fullscreen': False,
    # The window's size in points when it was last a window, so the next
    # launch opens where this one left off. None until the window has been
    # opened once, which means "pick one that suits the display".
    'window': None,
    # 'lit' is the light-buffer pipeline; 'classic' is the original
    # single-pass look, kept because it is a different aesthetic rather than
    # merely a worse one. `volumetric` is the air in the lit cone.
    'visuals': 'lit',
    'volumetric': True,
    # How much light the masonry keeps regardless of the lantern: 0 off,
    # 1 dim, 2 full. Readability rather than atmosphere - some players
    # want to see the shape of the room they are in.
    'wall_glow': 0,
    # ---- what survives a run ------------------------------------------
    # Embers carried out of the vault, and what they have been spent on.
    # `vigil` is {node key: rank}; see lumen/vigil.py. Both are listed here
    # or `load` would drop them, which is a trap this file has sprung before.
    'embers': 0,
    'vigil': {},
    'banked': 0,        # lifetime embers earned, for the records screen
    'deepest': 0,       # deepest floor reached, distinct from best_floor
    # ---- the deeper dark ----------------------------------------------
    # Highest ascension tier beaten (-1 for none), and the one selected for
    # the next descent. See lumen/ascension.py.
    'ascension_cleared': -1,
    'ascension': 0,
}


def _path():
    # Tests point this elsewhere so a headless run never touches real records.
    override = os.environ.get('LUMEN_SAVE')
    if override:
        return override
    return os.path.join(paths.data_dir(), 'lumen_save.json')


def load():
    data = dict(DEFAULT)
    try:
        with open(_path(), 'r') as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            for key in DEFAULT:
                if key in stored:
                    data[key] = stored[key]
    except Exception:
        pass
    return data


def save(data):
    try:
        with open(_path(), 'w') as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def record_run(data, score, floor, kills, won, embers=0):
    """Close out a run, banking whatever it carried out of the vault.

    Embers are banked whether the run was won or lost. A death that returns
    nothing is a total loss, and a total loss is what makes the twelfth
    attempt feel like the first; carrying something out is the whole point of
    going back down.
    """
    data['runs'] = data.get('runs', 0) + 1
    data['total_kills'] = data.get('total_kills', 0) + kills
    data['best_score'] = max(data.get('best_score', 0), score)
    data['best_floor'] = max(data.get('best_floor', 0), floor)
    data['deepest'] = max(data.get('deepest', 0), floor)
    earned = max(0, int(embers))
    data['embers'] = int(data.get('embers', 0)) + earned
    data['banked'] = int(data.get('banked', 0)) + earned
    if won:
        data['wins'] = data.get('wins', 0) + 1
    save(data)
    return data
