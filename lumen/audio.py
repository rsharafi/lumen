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
CACHE_VERSION = 4

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


def _filter(sig, cutoff, kind='low', slope=2.0):
    """A real filter, done in the frequency domain.

    `_lowpass_fast` is a box average, which rolls off at only 6 dB/octave and
    leaves ripples in the stopband - so a "filtered" square wave keeps most of
    the buzz that made these read as chiptune. Shaping the spectrum directly
    is both cleaner and faster than looping a one-pole filter in Python.
    """
    n = sig.shape[0]
    if n < 8:
        return sig
    spec = np.fft.rfft(sig)
    freq = np.fft.rfftfreq(n, 1.0 / SAMPLE_RATE)
    ratio = np.maximum(freq, 1e-6) / max(cutoff, 1e-6)
    if kind == 'low':
        gain = 1.0 / np.sqrt(1.0 + ratio ** (2.0 * slope))
    else:
        gain = ratio ** slope / np.sqrt(1.0 + ratio ** (2.0 * slope))
    return np.fft.irfft(spec * gain, n).astype(np.float32)


def _room(sig, seconds=0.9, decay=5.5, mix=0.30, damp=2600.0, rng=None):
    """Put the sound in the vault.

    Every effect used to be bone dry, which is most of why they sounded like
    they came from a machine rather than from a large stone room. The impulse
    response is decaying filtered noise with a few early reflections in front
    of it - not a real hall, but enough that a shot has somewhere to go.
    """
    if mix <= 0.0:
        return sig
    rng = rng or np.random.default_rng(7)
    n = int(seconds * SAMPLE_RATE)
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    ir = rng.uniform(-1.0, 1.0, n).astype(np.float32) * np.exp(-decay * t)
    # Stone: the tail loses its top end faster than its body.
    ir = _filter(ir, damp, 'low', 1.5)
    for delay, gain in ((0.011, 0.5), (0.019, 0.38), (0.031, 0.26),
                        (0.043, 0.18)):
        k = int(delay * SAMPLE_RATE)
        if k < n:
            ir[k] += gain
    ir /= max(float(np.abs(ir).max()), 1e-6)

    size = int(2 ** math.ceil(math.log2(sig.shape[0] + n)))
    wet = np.fft.irfft(np.fft.rfft(sig, size) * np.fft.rfft(ir, size),
                       size)[:sig.shape[0] + n].astype(np.float32)
    wet /= max(float(np.abs(wet).max()), 1e-6)
    out = np.zeros(sig.shape[0] + n, dtype=np.float32)
    out[:sig.shape[0]] = sig * (1.0 - mix)
    out += wet * mix
    return out


def _struck(duration, freq, partials=None, decays=None, bright=1.0):
    """A struck body rather than a tone.

    A bare sine at a musical frequency is the sound of a machine beeping, and
    is what made picking something up feel like an arcade cabinet. Real struck
    things - stone, glass, metal - ring on partials that are not whole
    multiples of the fundamental, and the higher ones die first.
    """
    partials = partials or (1.0, 2.41, 3.87, 5.13, 7.31)
    decays = decays or (1.0, 2.0, 3.2, 4.6, 6.5)
    n = int(duration * SAMPLE_RATE)
    t = _t(duration)
    out = np.zeros(n, dtype=np.float32)
    for k, (mult, fall) in enumerate(zip(partials, decays)):
        amp = bright ** k / (1.0 + k * 1.4)
        out += (np.sin(2.0 * math.pi * freq * mult * t)
                * np.exp(-fall * t / max(duration, 1e-6) * 3.2) * amp)
    return out.astype(np.float32)


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
    """Every effect, built to sound like it happened in a stone vault.

    The first version of these leaned on raw square and saw waves and on bare
    sine tones at musical intervals, all of it dry. Measured, the jingles had
    no even-harmonic content whatsoever - the signature of a pure tone, which
    the ear reads as a machine beeping rather than as a thing being struck.
    Nothing had a tail longer than its own envelope, so nothing sounded like
    it was in a room at all.

    So: the buzz is filtered rather than raw, the tones are struck bodies with
    inharmonic partials, and almost everything is put through `_room`. The
    exception is `ui_move`, which is a 40 ms tick and was already right - a
    tail on that would only smear the menu.
    """
    rng = np.random.default_rng(20240)
    out = {}

    # Sparklance: a tight discharge, not a bright pew. The old sweep started
    # at 1500 Hz and was pure square; this starts lower and is filtered, so
    # the transient is a snap rather than a chirp.
    n = int(0.17 * SAMPLE_RATE)
    body = _filter(_sweep(0.17, 860, 210, 'square', 0.55), 2100, 'low', 2.5)
    body *= _env(n, 0.001, 0.05, 0.18, 0.1)
    tick = _filter(_noise(n, rng), 5200, 'low', 1.5) * _env(n, 0.0005, 0.014, 0.0, 0.01)
    out['shoot'] = _normalise(_room(_soft_clip(_mix(body * 0.75, tick * 0.6)),
                                    0.42, 8.0, 0.24, rng=rng), 0.5)

    # Scatterlight: a wider, dirtier burst.
    n = int(0.26 * SAMPLE_RATE)
    burst = _filter(_noise(n, rng), 1700, 'low', 2.0) * _env(n, 0.002, 0.09, 0.12, 0.13)
    thump = _sweep(0.26, 330, 70, 'sine', 0.6) * _env(n, 0.002, 0.07, 0.1, 0.13)
    out['shoot_scatter'] = _normalise(
        _room(_soft_clip(_mix(burst, thump * 0.95)), 0.5, 7.0, 0.26, rng=rng), 0.58)

    # Coilbeam: a charged release with a long ringing tail.
    n = int(0.55 * SAMPLE_RATE)
    beam = _filter(_sweep(0.55, 150, 1050, 'saw', 1.6), 1900, 'low', 2.2)
    beam *= _env(n, 0.02, 0.16, 0.32, 0.34)
    ring = _struck(0.55, 330, bright=0.8) * _env(n, 0.01, 0.3, 0.2, 0.24) * 0.5
    out['shoot_beam'] = _normalise(
        _room(_soft_clip(_mix(beam * 0.7, ring)), 0.8, 5.0, 0.32, rng=rng), 0.62)

    # Charging hum for the beam.
    n = int(0.75 * SAMPLE_RATE)
    hum = _filter(_sweep(0.75, 70, 420, 'saw', 1.8), 900, 'low', 2.0)
    hum *= _env(n, 0.08, 0.4, 0.6, 0.25)
    out['charge'] = _normalise(_room(_soft_clip(hum), 0.5, 7.0, 0.2, rng=rng), 0.36)

    # Impacts.
    n = int(0.13 * SAMPLE_RATE)
    hit = _mix(_filter(_noise(n, rng), 3000, 'low', 1.6) * _env(n, 0.001, 0.05, 0.0, 0.06),
               _sweep(0.13, 520, 150, 'sine', 0.5) * _env(n, 0.001, 0.05, 0.0, 0.06))
    out['hit'] = _normalise(_room(_soft_clip(hit), 0.34, 9.0, 0.22, rng=rng), 0.46)

    # A crit is glass breaking, not two sine tones stacked.
    n = int(0.24 * SAMPLE_RATE)
    crit = _mix(_struck(0.24, 620, (1.0, 2.76, 5.41, 8.93), (1.0, 2.4, 4.0, 6.0)),
                _filter(_noise(n, rng), 7000, 'high', 1.2)
                * _env(n, 0.0005, 0.02, 0.0, 0.02) * 0.35)
    out['crit'] = _normalise(_room(_soft_clip(crit), 0.5, 6.0, 0.3, rng=rng), 0.54)

    # Taking damage: an ugly low crunch.
    n = int(0.34 * SAMPLE_RATE)
    hurt = _mix(_filter(_sweep(0.34, 230, 48, 'saw', 0.7), 700, 'low', 2.0)
                * _env(n, 0.002, 0.12, 0.2, 0.2),
                _filter(_noise(n, rng), 900, 'low', 2.0) * _env(n, 0.002, 0.1, 0.1, 0.2))
    out['hurt'] = _normalise(_room(_soft_clip(hurt), 0.55, 6.5, 0.24, rng=rng), 0.7)

    # Enemy death: a wet snap into a fading hiss.
    n = int(0.42 * SAMPLE_RATE)
    death = _mix(_filter(_noise(n, rng), 1900, 'low', 1.8) * _env(n, 0.002, 0.16, 0.12, 0.24),
                 _filter(_sweep(0.42, 380, 55, 'square', 0.5), 1200, 'low', 2.4)
                 * _env(n, 0.002, 0.1, 0.08, 0.28))
    out['kill'] = _normalise(_room(_soft_clip(death), 0.7, 5.5, 0.3, rng=rng), 0.56)

    # Explosion: the room does most of the work.
    n = int(0.8 * SAMPLE_RATE)
    boom = _mix(_filter(_noise(n, rng), 600, 'low', 2.0) * _env(n, 0.004, 0.3, 0.22, 0.48),
                _sweep(0.8, 150, 28, 'sine', 0.5) * _env(n, 0.004, 0.25, 0.2, 0.5))
    out['boom'] = _normalise(_room(_soft_clip(boom), 1.3, 3.4, 0.38, rng=rng), 0.85)

    # Lantern flare: a rising whoosh, warmed and given somewhere to bloom.
    n = int(0.62 * SAMPLE_RATE)
    flare = _mix(_filter(_noise(n, rng), 3200, 'low', 1.4) * _env(n, 0.012, 0.18, 0.28, 0.42),
                 _sweep(0.62, 220, 1250, 'sine', 1.4) * _env(n, 0.012, 0.2, 0.22, 0.38))
    out['flare'] = _normalise(_room(_soft_clip(flare), 0.9, 4.5, 0.34, rng=rng), 0.68)

    # Dash: short airy whoosh, darker than it was.
    n = int(0.28 * SAMPLE_RATE)
    dash = _filter(_noise(n, rng), 1500, 'low', 1.6) * _env(n, 0.012, 0.1, 0.16, 0.16)
    dash *= np.linspace(0.4, 1.2, n, dtype=np.float32)
    out['dash'] = _normalise(_room(dash, 0.42, 7.5, 0.24, rng=rng), 0.4)

    # Pickups and rewards: struck, not beeped.
    n = int(0.5 * SAMPLE_RATE)
    pick = _struck(0.5, 470, (1.0, 2.33, 3.71, 6.02), (1.2, 2.6, 4.2, 6.4))
    pick *= _env(n, 0.003, 0.14, 0.4, 0.3)
    out['pickup'] = _normalise(_room(pick, 0.7, 5.0, 0.34, rng=rng), 0.44)

    # An offering taken: a low bell, two voices a fifth apart.
    n = int(0.95 * SAMPLE_RATE)
    up = _mix(_struck(0.95, 262, bright=0.85) * _env(n, 0.006, 0.3, 0.45, 0.4),
              _struck(0.95, 392, bright=0.7) * _env(n, 0.09, 0.3, 0.3, 0.4) * 0.6)
    out['upgrade'] = _normalise(_room(up, 1.4, 3.2, 0.4, rng=rng), 0.5)

    # Descending to the next floor.
    n = int(1.3 * SAMPLE_RATE)
    desc = _mix(_sweep(1.3, 330, 52, 'sine', 1.2) * _env(n, 0.05, 0.4, 0.35, 0.7),
                _filter(_noise(n, rng), 450, 'low', 2.0) * _env(n, 0.1, 0.4, 0.2, 0.7) * 0.7)
    out['descend'] = _normalise(_room(_soft_clip(desc), 1.6, 2.8, 0.42, rng=rng), 0.6)

    # Interface. `ui_move` is left exactly as it was: it is a 40 ms tick, it
    # already sits right under the eye, and a room on it would smear the menu.
    n = int(0.08 * SAMPLE_RATE)
    out['ui_move'] = _normalise(
        _tone(0.08, 660, 'sine') * _env(n, 0.002, 0.03, 0.0, 0.04), 0.3)
    n = int(0.3 * SAMPLE_RATE)
    sel = _struck(0.3, 540, (1.0, 2.18, 3.62), (1.4, 2.8, 4.4))
    out['ui_select'] = _normalise(
        _room(sel * _env(n, 0.003, 0.1, 0.25, 0.16), 0.45, 6.5, 0.26, rng=rng), 0.4)
    n = int(0.5 * SAMPLE_RATE)
    back = _sweep(0.5, 380, 130, 'sine', 0.8) * _env(n, 0.005, 0.2, 0.1, 0.28)
    out['ui_back'] = _normalise(_room(back, 0.5, 6.0, 0.24, rng=rng), 0.34)

    # Boss.
    n = int(1.6 * SAMPLE_RATE)
    roar = _mix(_filter(_sweep(1.6, 48, 120, 'saw', 0.8), 340, 'low', 2.0)
                * _env(n, 0.1, 0.5, 0.5, 0.9),
                _filter(_noise(n, rng), 380, 'low', 2.0) * _env(n, 0.12, 0.6, 0.35, 0.8),
                _filter(_sweep(1.6, 240, 72, 'square', 1.2), 620, 'low', 2.4)
                * _env(n, 0.2, 0.5, 0.2, 0.8) * 0.45)
    out['roar'] = _normalise(_room(_soft_clip(roar), 1.8, 2.4, 0.44, rng=rng), 0.9)

    n = int(0.5 * SAMPLE_RATE)
    enemy = _mix(_filter(_sweep(0.5, 520, 130, 'saw', 0.7), 1300, 'low', 2.2)
                 * _env(n, 0.004, 0.12, 0.1, 0.3),
                 _filter(_noise(n, rng), 1200, 'low', 1.8)
                 * _env(n, 0.004, 0.08, 0.06, 0.3) * 0.6)
    out['enemy_shoot'] = _normalise(
        _room(_soft_clip(enemy), 0.6, 6.0, 0.28, rng=rng), 0.42)

    # A brazier catching: breath, then flame.
    n = int(0.45 * SAMPLE_RATE)
    braz = _mix(_filter(_noise(n, rng), 1500, 'low', 1.4) * _env(n, 0.02, 0.16, 0.3, 0.22),
                _sweep(0.45, 150, 430, 'sine', 1.3) * _env(n, 0.02, 0.16, 0.2, 0.22) * 0.8)
    out['brazier'] = _normalise(_room(_soft_clip(braz), 0.9, 4.5, 0.34, rng=rng), 0.48)

    n = int(0.9 * SAMPLE_RATE)
    over = _mix(_filter(_sweep(0.9, 160, 34, 'saw', 1.0), 420, 'low', 2.0)
                * _env(n, 0.02, 0.35, 0.3, 0.5),
                _filter(_noise(n, rng), 380, 'low', 2.0) * _env(n, 0.05, 0.4, 0.2, 0.45))
    out['game_over'] = _normalise(_room(_soft_clip(over), 2.0, 2.2, 0.46, rng=rng), 0.75)

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
def _read_wav(path):
    """A cached effect back as float32 mono."""
    with wave.open(path, 'rb') as f:
        frames = f.getnframes()
        raw = np.frombuffer(f.readframes(frames), dtype='<i2')
    if f.getnchannels() == 2:
        raw = raw.reshape(-1, 2)[:, 0]
    return raw.astype(np.float32) / 32768.0


def _resample(sig, factor):
    """Shift pitch by resampling. Above 1.0 is higher and shorter."""
    if abs(factor - 1.0) < 1e-4:
        return sig
    n = int(len(sig) / factor)
    if n < 8:
        return sig
    src = np.arange(len(sig), dtype=np.float32)
    dst = np.linspace(0.0, len(sig) - 1.0, n, dtype=np.float32)
    return np.interp(dst, src, sig).astype(np.float32)


class SoundBank:
    """Effects, their variants, and the channels they play on.

    Every effect used to be one fixed recording played on a fixed channel, so
    forty shots in a row were forty identical waveforms - which is most of
    what makes repeated fire sound mechanical rather than alive. Now each one
    is baked into several takes at slightly different pitch and colour, and a
    play picks one at random, sets its own gain, and pans it to wherever the
    thing that made it was standing.

    Channels come from a pool rather than one per sound: `find_channel(True)`
    takes a free one or steals the oldest, which is both simpler than the
    hand-sized voice pools this had before and correct when tails got longer.
    """

    CHANNELS = 48

    # (takes, pitch spread, how much colour varies). Sounds you hear over and
    # over get the most; a one-shot like the boss roar wants to be the same
    # sound every time you hear it.
    VARIATION = {
        'shoot': (6, 0.085, 0.30),
        'shoot_scatter': (5, 0.075, 0.30),
        'shoot_beam': (4, 0.050, 0.20),
        'enemy_shoot': (5, 0.090, 0.35),
        'hit': (6, 0.110, 0.35),
        'crit': (4, 0.060, 0.25),
        'kill': (5, 0.080, 0.30),
        'hurt': (4, 0.070, 0.25),
        'dash': (4, 0.070, 0.30),
        'pickup': (4, 0.055, 0.20),
        'boom': (3, 0.060, 0.25),
        'brazier': (3, 0.070, 0.25),
        'flare': (3, 0.045, 0.15),
        'charge': (2, 0.030, 0.10),
        'ui_move': (1, 0.0, 0.0),
    }
    DEFAULT_VARIATION = (2, 0.030, 0.10)

    def __init__(self, cache_dir, enabled=True):
        self.cache_dir = cache_dir
        self.enabled = enabled
        self.volume = 0.7
        self._takes = {}
        self._ready = False
        self._failed = False
        self._rng = np.random.default_rng(90210)

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

    def _variants(self, name, signal):
        takes, spread, colour = self.VARIATION.get(name,
                                                   self.DEFAULT_VARIATION)
        if takes <= 1:
            return [signal]
        out = []
        for i in range(takes):
            # Spread evenly rather than at random, so the set actually covers
            # its range instead of clustering wherever the seed happened to
            # land.
            f = 1.0 + spread * (2.0 * i / (takes - 1) - 1.0)
            take = _resample(signal, f)
            if colour > 0.0:
                # A little filtering as well as pitch. Two takes at the same
                # loudness and different brightness read as two events; two at
                # different pitch alone can still read as one sound stuttering.
                cut = 2200.0 * (1.0 + colour * (2.0 * i / (takes - 1) - 1.0))
                take = take * (1.0 - colour * 0.5) + \
                    _filter(take, cut, 'low', 1.5) * (colour * 0.5)
            out.append(_normalise(take, 0.9))
        return out

    def load(self):
        if self._ready or self._failed:
            return
        try:
            import pygame
            if not pygame.mixer.get_init():
                # Before cmu-graphics gets to it, so the buffer is small
                # enough not to put an audible delay on every shot.
                pygame.mixer.pre_init(SAMPLE_RATE, -16, 2, 512)
                pygame.mixer.init()
            pygame.mixer.set_num_channels(self.CHANNELS)
        except Exception as exc:
            sys.stderr.write(f'[lumen] audio device unavailable: {exc}\n')
            self._failed = True
            return
        try:
            import pygame
            for entry in sorted(os.listdir(self.cache_dir)):
                if not entry.endswith('.wav'):
                    continue
                name = entry[:-4]
                signal = _read_wav(os.path.join(self.cache_dir, entry))
                takes = []
                for variant in self._variants(name, signal):
                    pcm = np.clip(variant, -1.0, 1.0)
                    stereo = np.repeat((pcm * 32767.0).astype('<i2')[:, None],
                                       2, axis=1)
                    takes.append(pygame.sndarray.make_sound(
                        np.ascontiguousarray(stereo)))
                self._takes[name] = takes
            self._ready = True
        except Exception as exc:  # pragma: no cover - audio device problems
            sys.stderr.write(f'[lumen] audio unavailable: {exc}\n')
            self._failed = True

    def play(self, name, volume=1.0, pan=0.0, pitch=None):
        """`pan` is -1 hard left to +1 hard right."""
        if not self.enabled or self._failed:
            return None
        if not self._ready:
            self.load()
            if not self._ready:
                return None
        takes = self._takes.get(name)
        if not takes:
            return None
        try:
            import pygame
            sound = takes[int(self._rng.integers(len(takes)))]
            channel = pygame.mixer.find_channel(True)
            if channel is None:
                return None
            gain = max(0.0, min(1.0, volume * self.volume))
            # Constant-power either side of centre, so panning something does
            # not make it quieter as it crosses the middle.
            pan = max(-1.0, min(1.0, pan))
            angle = (pan + 1.0) * 0.25 * math.pi
            channel.set_volume(gain * math.cos(angle), gain * math.sin(angle))
            channel.play(sound)
            return channel
        except Exception:
            return None

    def set_enabled(self, value):
        self.enabled = bool(value)
        if not value:
            try:
                import pygame
                pygame.mixer.stop()
            except Exception:
                pass


def bank():
    global _bank
    if _bank is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _bank = SoundBank(os.path.join(here, '.sound_cache'),
                          enabled=not os.environ.get('LUMEN_HEADLESS'))
    return _bank


def play(name, volume=1.0, pan=0.0):
    bank().play(name, volume, pan)


# Beyond this much of a screen-width away, a sound is not worth hearing.
_EARSHOT = 1.35

# Where the view is, so a call site only has to know its own position.
# (camera x, camera y, view width, view height) in design units.
_listener = (0.0, 0.0, 1280.0, 720.0)


def set_listener(ox, oy, view_w, view_h):
    """Told once a frame by the world, so `play_at` can take world coords."""
    global _listener
    _listener = (ox, oy, max(view_w, 1.0), max(view_h, 1.0))


def play_at(name, x, y, volume=1.0):
    """An effect placed where it happened, in world coordinates.

    Panned by how far it is from the middle of the view and quieter with
    distance, so a shot from the left arrives from the left and something
    dying off-screen is a thing you notice rather than a thing that startles
    you at full volume.
    """
    ox, oy, view_w, view_h = _listener
    half_w = max(view_w * 0.5, 1.0)
    half_h = max(view_h * 0.5, 1.0)
    dx = ((x - ox) - half_w) / half_w
    dy = ((y - oy) - half_h) / half_h
    distance = math.hypot(dx, dy)
    if distance > _EARSHOT:
        return
    # Falls off with distance and stops entirely at the edge of earshot,
    # rather than cutting out at some arbitrary volume.
    falloff = max(0.0, 1.0 - (distance / _EARSHOT) ** 1.6)
    if falloff <= 0.01:
        return
    bank().play(name, volume * (0.35 + 0.65 * falloff),
                pan=max(-1.0, min(1.0, dx * 0.85)))
