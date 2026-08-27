"""Persistent records. One small JSON file beside the game."""

import json
import os

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
}


def _path():
    # Tests point this elsewhere so a headless run never touches real records.
    override = os.environ.get('LUMEN_SAVE')
    if override:
        return override
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(here, 'lumen_save.json')


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


def record_run(data, score, floor, kills, won):
    data['runs'] = data.get('runs', 0) + 1
    data['total_kills'] = data.get('total_kills', 0) + kills
    data['best_score'] = max(data.get('best_score', 0), score)
    data['best_floor'] = max(data.get('best_floor', 0), floor)
    if won:
        data['wins'] = data.get('wins', 0) + 1
    save(data)
    return data
