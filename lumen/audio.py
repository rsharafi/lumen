"""Procedurally synthesised sound.

cmu-graphics' `Sound` can only load a file, so every effect is synthesised with
numpy at first run and written to a small WAV cache next to the game. Nothing
is shipped as an asset, and after the first launch startup just reads the
cache.

Each effect gets a few independent voices because `Sound.play()` reuses a
single channel per Sound - without a voice pool, rapid fire would retrigger one
channel and stutter instead of overlapping.
"""

import math
import os
import struct
import sys
import wave

import numpy as np

SAMPLE_RATE = 44100
CACHE_VERSION = 3

_bank = None


# --------------------------------------------------------------------------
# Synthesis primitives
# --------------------------------------------------------------------------
def _t(duration):
    return np.linspace(0.0, duration, int(SAMPLE_RATE * duration), endpoint=False,
                       dtype=np.float32)


def _env(n, attack=0.005, decay=0.1, sustain=0.0, release=0.05, hold=0.0):
    """A simple ADSR shaped over `n` samples."""
    a = max(1, int(attack * SAMPLE_RATE))
    d = max(1, int(decay * SAMPLE_RATE))
    h = int(hold * SAMPLE_RATE)
    r = max(1, int(release * SAMPLE_RATE))
    out = np.zeros(n, dtype=np.float32)
    i = 0
    seg = min(a, n - i)
    if seg > 0:
        out[i:i + seg] = np.linspace(0.0, 1.0, seg, dtype=np.float32)
        i += seg
    seg = min(d, n - i)
    if seg > 0:
        out[i:i + seg] = np.linspace(1.0, sustain, seg, dtype=np.float32)
        i += seg
    seg = min(h, n - i)
    if seg > 0:
        out[i:i + seg] = sustain
        i += seg
    seg = min(r, n - i)
    if seg > 0:
        out[i:i + seg] = np.linspace(sustain, 0.0, seg, dtype=np.float32)
        i += seg
    return out


def _noise(n, rng):
    return rng.uniform(-1.0, 1.0, n).astype(np.float32)


def _lowpass(sig, cutoff):
    """One-pole low-pass; `cutoff` in Hz."""
    if cutoff >= SAMPLE_RATE * 0.45:
        return sig
    alpha = 1.0 - math.exp(-2.0 * math.pi * cutoff / SAMPLE_RATE)
    out = np.empty_like(sig)
    acc = 0.0
    for i in range(sig.shape[0]):
        acc += alpha * (sig[i] - acc)
        out[i] = acc
    return out


def _lowpass_fast(sig, cutoff):
    """Vectorised approximation of a low-pass via a moving average."""
    if cutoff >= SAMPLE_RATE * 0.45:
        return sig
    width = max(1, int(SAMPLE_RATE / max(cutoff, 1.0)))
    if width <= 1:
        return sig
    kernel = np.ones(width, dtype=np.float32) / width
    return np.convolve(sig, kernel, mode='same').astype(np.float32)


def _sweep(duration, f0, f1, shape='sine', curve=1.0):
    t = _t(duration)
    if len(t) == 0:
        return t
    frac = (t / max(t[-1], 1e-6)) ** curve
    freq = f0 + (f1 - f0) * frac
    phase = np.cumsum(freq) * (2.0 * math.pi / SAMPLE_RATE)
    if shape == 'sine':
        return np.sin(phase).astype(np.float32)
    if shape == 'square':
        return np.sign(np.sin(phase)).astype(np.float32)
    if shape == 'saw':
        return (2.0 * ((phase / (2.0 * math.pi)) % 1.0) - 1.0).astype(np.float32)
    return np.sin(phase).astype(np.float32)


def _tone(duration, freq, shape='sine'):
    return _sweep(duration, freq, freq, shape)


def _mix(*parts):
    n = max(len(p) for p in parts)
    out = np.zeros(n, dtype=np.float32)
    for p in parts:
        out[:len(p)] += p
    return out


def _normalise(sig, peak=0.86):
    m = float(np.abs(sig).max())
    if m < 1e-6:
        return sig
    return (sig / m * peak).astype(np.float32)


def _soft_clip(sig):
    return np.tanh(sig * 1.4).astype(np.float32)


# --------------------------------------------------------------------------
# The effects themselves
# --------------------------------------------------------------------------
def _make_sounds():
    rng = np.random.default_rng(20240)
    out = {}

    # Sparklance: a tight, bright pew with a noise transient.
    n = int(0.17 * SAMPLE_RATE)
    body = _sweep(0.17, 1500, 340, 'square', 0.55) * _env(n, 0.002, 0.05, 0.22, 0.1)
    tick = _noise(n, rng) * _env(n, 0.001, 0.02, 0.0, 0.01) * 0.5
    out['shoot'] = _normalise(_soft_clip(_mix(body * 0.6, tick)), 0.55)

    # Scatterlight: a wider, dirtier burst.
    n = int(0.26 * SAMPLE_RATE)
    burst = _lowpass_fast(_noise(n, rng), 2600) * _env(n, 0.002, 0.09, 0.14, 0.13)
    thump = _sweep(0.26, 420, 90, 'sine', 0.6) * _env(n, 0.002, 0.07, 0.1, 0.13)
    out['shoot_scatter'] = _normalise(_soft_clip(_mix(burst, thump * 0.8)), 0.62)

    # Coilbeam: a charged release with a long ringing tail.
    n = int(0.55 * SAMPLE_RATE)
    beam = _sweep(0.55, 220, 1750, 'saw', 1.6) * _env(n, 0.02, 0.16, 0.35, 0.34)
    ring = _tone(0.55, 880, 'sine') * _env(n, 0.01, 0.3, 0.14, 0.24) * 0.4
    out['shoot_beam'] = _normalise(_soft_clip(_mix(beam * 0.7, ring)), 0.7)

    # Charging hum for the beam.
    n = int(0.75 * SAMPLE_RATE)
    hum = _sweep(0.75, 90, 700, 'saw', 1.8) * _env(n, 0.08, 0.4, 0.6, 0.25)
    out['charge'] = _normalise(_soft_clip(hum), 0.4)

    # Impacts.
    n = int(0.13 * SAMPLE_RATE)
    hit = _mix(_lowpass_fast(_noise(n, rng), 5200) * _env(n, 0.001, 0.05, 0.0, 0.06),
               _sweep(0.13, 700, 200, 'sine', 0.5) * _env(n, 0.001, 0.05, 0.0, 0.06))
    out['hit'] = _normalise(_soft_clip(hit), 0.5)

    n = int(0.2 * SAMPLE_RATE)
    crit = _mix(_tone(0.2, 1320, 'sine') * _env(n, 0.001, 0.06, 0.1, 0.12),
                _tone(0.2, 1980, 'sine') * _env(n, 0.001, 0.04, 0.05, 0.1) * 0.6,
                _noise(n, rng) * _env(n, 0.001, 0.02, 0.0, 0.02) * 0.4)
    out['crit'] = _normalise(_soft_clip(crit), 0.6)

    # Taking damage: an ugly low crunch.
    n = int(0.34 * SAMPLE_RATE)
    hurt = _mix(_sweep(0.34, 300, 60, 'saw', 0.7) * _env(n, 0.002, 0.12, 0.2, 0.2),
                _lowpass_fast(_noise(n, rng), 1400) * _env(n, 0.002, 0.1, 0.1, 0.2))
    out['hurt'] = _normalise(_soft_clip(hurt), 0.72)

    # Enemy death: a wet snap into a fading hiss.
    n = int(0.42 * SAMPLE_RATE)
    death = _mix(_lowpass_fast(_noise(n, rng), 3000) * _env(n, 0.002, 0.16, 0.12, 0.24),
                 _sweep(0.42, 520, 70, 'square', 0.5) * _env(n, 0.002, 0.1, 0.08, 0.28))
    out['kill'] = _normalise(_soft_clip(death), 0.6)

    # Explosion.
    n = int(0.8 * SAMPLE_RATE)
    boom = _mix(_lowpass_fast(_noise(n, rng), 900) * _env(n, 0.004, 0.3, 0.22, 0.48),
                _sweep(0.8, 180, 34, 'sine', 0.5) * _env(n, 0.004, 0.25, 0.2, 0.5))
    out['boom'] = _normalise(_soft_clip(boom), 0.85)

    # Lantern flare: bright rising whoosh.
    n = int(0.62 * SAMPLE_RATE)
    flare = _mix(_lowpass_fast(_noise(n, rng), 6000) * _env(n, 0.01, 0.18, 0.3, 0.42),
                 _sweep(0.62, 300, 2200, 'sine', 1.4) * _env(n, 0.01, 0.2, 0.22, 0.38))
    out['flare'] = _normalise(_soft_clip(flare), 0.72)

    # Dash: short airy whoosh.
    n = int(0.28 * SAMPLE_RATE)
    dash = _lowpass_fast(_noise(n, rng), 3400) * _env(n, 0.01, 0.1, 0.16, 0.16)
    dash *= np.linspace(0.4, 1.2, n, dtype=np.float32)
    out['dash'] = _normalise(dash, 0.42)

    # Pickups and rewards.
    n = int(0.4 * SAMPLE_RATE)
    pick = _mix(_tone(0.4, 880, 'sine') * _env(n, 0.004, 0.1, 0.2, 0.2),
                _tone(0.4, 1320, 'sine') * _env(n, 0.05, 0.12, 0.14, 0.2) * 0.7)
    out['pickup'] = _normalise(pick, 0.45)

    n = int(0.7 * SAMPLE_RATE)
    up = _mix(_tone(0.7, 523, 'sine') * _env(n, 0.01, 0.2, 0.24, 0.3),
              _tone(0.7, 784, 'sine') * _env(n, 0.12, 0.2, 0.2, 0.3) * 0.8,
              _tone(0.7, 1046, 'sine') * _env(n, 0.24, 0.2, 0.16, 0.24) * 0.6)
    out['upgrade'] = _normalise(up, 0.5)

    # Descending to the next floor.
    n = int(1.3 * SAMPLE_RATE)
    desc = _mix(_sweep(1.3, 420, 70, 'sine', 1.2) * _env(n, 0.05, 0.4, 0.35, 0.7),
                _lowpass_fast(_noise(n, rng), 700) * _env(n, 0.1, 0.4, 0.2, 0.7) * 0.6)
    out['descend'] = _normalise(_soft_clip(desc), 0.6)

    # Interface.
    n = int(0.08 * SAMPLE_RATE)
    out['ui_move'] = _normalise(_tone(0.08, 660, 'sine') * _env(n, 0.002, 0.03, 0.0, 0.04), 0.3)
    n = int(0.2 * SAMPLE_RATE)
    out['ui_select'] = _normalise(_mix(
        _tone(0.2, 784, 'sine') * _env(n, 0.003, 0.06, 0.12, 0.1),
        _tone(0.2, 1176, 'sine') * _env(n, 0.02, 0.06, 0.08, 0.1) * 0.6), 0.42)
    n = int(0.5 * SAMPLE_RATE)
    out['ui_back'] = _normalise(_sweep(0.5, 500, 180, 'sine', 0.8) * _env(n, 0.005, 0.2, 0.1, 0.28), 0.36)

    # Boss.
    n = int(1.6 * SAMPLE_RATE)
    roar = _mix(_sweep(1.6, 60, 150, 'saw', 0.8) * _env(n, 0.1, 0.5, 0.5, 0.9),
                _lowpass_fast(_noise(n, rng), 480) * _env(n, 0.12, 0.6, 0.35, 0.8),
                _sweep(1.6, 300, 90, 'square', 1.2) * _env(n, 0.2, 0.5, 0.2, 0.8) * 0.4)
    out['roar'] = _normalise(_soft_clip(roar), 0.9)

    n = int(0.5 * SAMPLE_RATE)
    out['enemy_shoot'] = _normalise(_soft_clip(_mix(
        _sweep(0.5, 700, 180, 'saw', 0.7) * _env(n, 0.004, 0.12, 0.1, 0.3),
        _lowpass_fast(_noise(n, rng), 1800) * _env(n, 0.004, 0.08, 0.06, 0.3) * 0.6)), 0.45)

    n = int(0.34 * SAMPLE_RATE)
    out['brazier'] = _normalise(_soft_clip(_mix(
        _lowpass_fast(_noise(n, rng), 2400) * _env(n, 0.01, 0.14, 0.2, 0.16),
        _sweep(0.34, 200, 620, 'sine', 1.3) * _env(n, 0.01, 0.14, 0.18, 0.16))), 0.5)

    n = int(0.9 * SAMPLE_RATE)
    out['game_over'] = _normalise(_soft_clip(_mix(
        _sweep(0.9, 200, 42, 'saw', 1.0) * _env(n, 0.02, 0.35, 0.3, 0.5),
        _lowpass_fast(_noise(n, rng), 500) * _env(n, 0.05, 0.4, 0.2, 0.45))), 0.75)

    return out


def _write_wav(path, mono):
    """Write a float32 mono signal out as 16-bit stereo."""
    data = np.clip(mono, -1.0, 1.0)
    pcm = (data * 32767.0).astype('<i2')
    stereo = np.repeat(pcm[:, None], 2, axis=1).tobytes()
    with wave.open(path, 'wb') as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(stereo)


# --------------------------------------------------------------------------
# Bank
# --------------------------------------------------------------------------
class SoundBank:
    VOICES = {
        'shoot': 4, 'shoot_scatter': 3, 'hit': 4, 'crit': 3, 'kill': 3,
        'enemy_shoot': 3, 'boom': 2, 'pickup': 2,
    }

    def __init__(self, cache_dir, enabled=True):
        self.cache_dir = cache_dir
        self.enabled = enabled
        self.volume = 0.7
        self._sounds = {}
        self._cursor = {}
        self._ready = False
        self._failed = False

    def build(self):
        """Generate any missing WAVs. Safe to call more than once."""
        stamp = os.path.join(self.cache_dir, f'.v{CACHE_VERSION}')
        os.makedirs(self.cache_dir, exist_ok=True)
        if os.path.exists(stamp):
            return
        for name, signal in _make_sounds().items():
            _write_wav(os.path.join(self.cache_dir, f'{name}.wav'), signal)
        with open(stamp, 'w') as f:
            f.write('ok')

    def load(self):
        if self._ready or self._failed:
            return
        try:
            import pygame
            if not pygame.mixer.get_init():
                # Initialise before cmu-graphics does it for us, so we can ask
                # for a small buffer - the default is large enough to put an
                # audible delay on every shot.
                pygame.mixer.pre_init(SAMPLE_RATE, -16, 2, 512)
                pygame.mixer.init()
        except Exception as exc:
            sys.stderr.write(f'[lumen] audio device unavailable: {exc}\n')
            self._failed = True
            return
        try:
            from cmu_graphics import Sound
        except Exception:
            self._failed = True
            return
        try:
            for entry in sorted(os.listdir(self.cache_dir)):
                if not entry.endswith('.wav'):
                    continue
                name = entry[:-4]
                path = os.path.join(self.cache_dir, entry)
                voices = self.VOICES.get(name, 1)
                self._sounds[name] = [Sound(path) for _ in range(voices)]
                self._cursor[name] = 0
            self._ready = True
        except Exception as exc:  # pragma: no cover - audio device problems
            sys.stderr.write(f'[lumen] audio unavailable: {exc}\n')
            self._failed = True

    def play(self, name, volume=1.0):
        if not self.enabled or self._failed:
            return
        if not self._ready:
            self.load()
            if not self._ready:
                return
        voices = self._sounds.get(name)
        if not voices:
            return
        idx = self._cursor[name]
        self._cursor[name] = (idx + 1) % len(voices)
        snd = voices[idx]
        try:
            snd.setVolume(max(0.0, min(1.0, volume * self.volume)))
            snd.play(restart=True)
        except Exception:
            pass

    def set_enabled(self, value):
        self.enabled = bool(value)


def bank():
    global _bank
    if _bank is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _bank = SoundBank(os.path.join(here, '.sound_cache'),
                          enabled=not os.environ.get('LUMEN_HEADLESS'))
    return _bank


def play(name, volume=1.0):
    bank().play(name, volume)
