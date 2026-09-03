"""Procedurally synthesised sound.

Nothing ships as an asset, so every effect is synthesised with
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
CACHE_VERSION = 18

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


def _reverse(sig):
    """A signal backwards. Half of "about to happen" is just this."""
    return np.ascontiguousarray(sig[::-1])


def _pad(sig, seconds):
    """Silence in front of a sound.

    Two uses: putting a hole ahead of an impact so it lands in silence rather
    than on top of whatever led into it, and placing one part of an effect
    later than another, since `_mix` lines everything up at zero.
    """
    k = int(seconds * SAMPLE_RATE)
    if k <= 0:
        return sig
    return np.concatenate([np.zeros(k, dtype=np.float32), sig])


def _sweep_filter(sig, c0, c1, kind='low', slope=1.6):
    """A filter whose cutoff moves across the sound.

    `_filter` shapes the whole spectrum at once, which is the wrong tool for
    anything that opens or closes - a riser, a mass going past. Crossfading
    between two fixed filterings is not a real time-varying filter, but over
    something this short the ear cannot tell, and it costs two FFTs rather
    than a Python loop over every sample.
    """
    if abs(c0 - c1) < 1.0:
        return _filter(sig, c0, kind, slope)
    lo = _filter(sig, c0, kind, slope)
    hi = _filter(sig, c1, kind, slope)
    ramp = np.linspace(0.0, 1.0, sig.shape[0], dtype=np.float32)
    return (lo * (1.0 - ramp) + hi * ramp).astype(np.float32)


def _gate(sig, at, fall=0.012):
    """Cut a sound dead, tail and all.

    Only one thing in the game does this, and it is the point of it: see
    `snuff_choke`.
    """
    n = sig.shape[0]
    k = min(n, max(1, int(at * SAMPLE_RATE)))
    f = max(1, int(fall * SAMPLE_RATE))
    out = sig.copy()
    end = min(n, k + f)
    if end > k:
        out[k:end] *= np.linspace(1.0, 0.0, end - k, dtype=np.float32)
    out[end:] = 0.0
    return out


def _cluster(duration, freq, voices=7, detune=0.011, partials=(1.0, 2.0, 3.0),
             falloff=1.7, glide=1.0):
    """Several voices at nearly the same pitch.

    One tone is a thing making a noise; a stack of them a few cents apart is
    a crowd of them, and the slow beating between the detunings is what makes
    the stack sound alive rather than merely thick. That is the Hollow
    Choir's whole identity - and two cold voices at a hollow fifth, which is
    the same function with different arguments, is the Snuffer's.

    `glide` slides the stack to that multiple of its pitch over the sound: up
    for something gathering itself, down for something coming apart.
    """
    t = _t(duration)
    n = t.shape[0]
    if n == 0:
        return t
    out = np.zeros(n, dtype=np.float32)
    ramp = t / max(t[-1], 1e-6)
    for v in range(voices):
        # Spread evenly either side of centre, so the set covers its range
        # instead of clustering wherever the seed happened to land.
        offset = detune * (2.0 * v / max(voices - 1, 1) - 1.0)
        freqs = freq * (1.0 + offset) * (1.0 + (glide - 1.0) * ramp)
        phase = np.cumsum(freqs) * (2.0 * math.pi / SAMPLE_RATE)
        # The golden angle, so no two voices start in step.
        seed = (v * 2.39996) % math.tau
        for k, mult in enumerate(partials):
            out += (np.sin(phase * mult + seed * (k + 1))
                    / (voices * (1.0 + k) ** falloff))
    return out.astype(np.float32)


def _swell(sig, seconds, decay, mix, damp, rng):
    """The room, arriving before the sound instead of after it.

    Running `_room` backwards puts the reflections in front of the event, so
    the thing sounds pulled together out of the dark rather than simply
    switched on - which is exactly what the gathering beat of a boss arrival
    looks like on screen.
    """
    return _reverse(_room(_reverse(sig), seconds, decay, mix, damp, rng))


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

    # A heavy slug leaving the barrel: low, short, and percussive, with none
    # of the lance's crackle. It should sound like something expensive.
    n = int(0.42 * SAMPLE_RATE)
    slug = _mix(_filter(_sweep(0.42, 190, 46, 'saw', 1.2), 620, 'low', 2.2)
                * _env(n, 0.001, 0.10, 0.12, 0.24),
                _filter(_noise(n, rng), 1500, 'low', 1.4)
                * _env(n, 0.0006, 0.05, 0.0, 0.1) * 0.7,
                _struck(0.42, 128, (1.0, 2.1), (1.0, 2.4)) * 0.5)
    out['shoot_heavy'] = _normalise(
        _room(_soft_clip(slug), 0.55, 6.5, 0.26, rng=rng), 0.62)

    # EMBERSTITCH fires nineteen times a second, which is nineteen chances a
    # second to grate. The lance's crackle at that rate is a dentist's drill,
    # so this is the opposite of a gunshot: very short, soft-edged, almost no
    # low end, and pitched high enough to sit above the mix rather than
    # punching through it. Its real defence is variation - eight takes with a
    # wide pitch spread, so the ear never hears the same tick twice running.
    n = int(0.11 * SAMPLE_RATE)
    tick = _mix(_struck(0.11, 1750, (1.0, 2.05), (3.0, 5.2), bright=0.7) * 0.8,
                _filter(_noise(n, rng), 2600, 'high', 0.9)
                * _env(n, 0.0004, 0.012, 0.0, 0.03) * 0.30)
    tick = _filter(tick, 7200, 'low', 0.8) * _env(n, 0.0006, 0.03, 0.0, 0.06)
    out['shoot_stitch'] = _normalise(
        _room(tick, 0.22, 8.0, 0.12, rng=rng), 0.30)

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

    # A warden's shield taking a hit. Struck metal - high, inharmonic, and
    # ringing rather than thudding, so a blocked shot is obviously a different
    # event from a landed one without anyone having to read a word.
    n = int(0.30 * SAMPLE_RATE)
    shield = _mix(_struck(0.30, 940, (1.0, 2.44, 4.19, 6.87, 9.9),
                          (1.1, 2.7, 4.4, 6.8, 9.2), bright=1.15),
                  _struck(0.16, 1580, (1.0, 3.14), (2.2, 4.8)) * 0.42,
                  _filter(_noise(n, rng), 5200, 'high', 1.0)
                  * _env(n, 0.0004, 0.018, 0.0, 0.02) * 0.22)
    out['shield'] = _normalise(_room(shield, 0.42, 5.2, 0.24, rng=rng), 0.5)

    # And the moment it gives way: the same metal, lower and coming apart.
    n = int(0.55 * SAMPLE_RATE)
    broke = _mix(_struck(0.55, 430, (1.0, 2.38, 4.02, 6.6),
                         (0.9, 2.1, 3.6, 5.4), bright=0.9),
                 _filter(_noise(n, rng), 3000, 'high', 1.2)
                 * _env(n, 0.001, 0.10, 0.08, 0.3) * 0.4)
    out['shield_break'] = _normalise(
        _room(_soft_clip(broke), 0.62, 5.6, 0.32, rng=rng), 0.58)

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

    # ---- a relic ------------------------------------------------------
    # Something old being picked up. A struck bell with a long tail and a
    # second note that arrives underneath it late - the sound of a thing that
    # has been in the dark a long while and is still ringing.
    n = int(2.2 * SAMPLE_RATE)
    bell = _struck(2.2, 294, (1.0, 2.76, 5.4, 8.9), (0.9, 1.8, 3.0, 4.4),
                   bright=1.05) * _env(n, 0.001, 0.9, 0.22, 1.1)
    under = _struck(2.2, 98, (1.0, 2.0), (0.8, 1.4), bright=0.5) \
        * _env(n, 0.18, 0.7, 0.1, 0.9)
    shine = _struck(2.2, 1176, bright=1.2) * _env(n, 0.02, 0.4, 0.0, 0.6)
    out['relic'] = _normalise(
        _room(_soft_clip(_mix(bell, under * 0.6, shine * 0.3)),
              2.6, 2.4, 0.44, rng=rng), 0.62)

    # ---- the vault's own fire ------------------------------------------
    # A vent letting go. Gas catching rather than a bang: a soft rush that
    # arrives late and a low body under it, because it has to be readable as
    # a *place* from across a dark room and not as a hit.
    n = int(1.4 * SAMPLE_RATE)
    rush = _filter(_noise(n, rng), 2600, 'low', 1.2) * _env(n, 0.05, 0.4, 0.3, 0.5)
    body = _struck(1.4, 62, (1.0, 1.9), (1.0, 1.6), bright=0.35) \
        * _env(n, 0.01, 0.35, 0.15, 0.5)
    out['vent'] = _normalise(
        _room(_soft_clip(_mix(rush * 0.85, body * 0.7)), 1.6, 3.2, 0.4,
              rng=rng), 0.5)

    # ---- the doors ----------------------------------------------------
    # A door is stone and metal moving in a stone frame, so all of this is
    # struck and scraped rather than tonal - and long, because the leaves
    # take half a second to travel and a sound that finishes first makes the
    # animation look like it is coasting.

    # Leaves withdrawing. A low grind that gets out of its own way, with the
    # seal's own note ringing off the top of it as the lock gives.
    n = int(1.5 * SAMPLE_RATE)
    grind = _filter(_noise(n, rng), 1500, 'low', 1.5) * _env(n, 0.02, 0.55, 0.25, 0.5)
    grind *= 0.6 + 0.4 * np.sin(np.linspace(0.0, 34.0, n))     # stone on stone
    ring = _struck(1.5, 523, (1.0, 2.0, 3.01), (1.2, 2.2, 3.6), bright=1.15) \
        * _env(n, 0.001, 0.5, 0.05, 0.7)
    thud = _struck(1.5, 74, bright=0.4) * _env(n, 0.002, 0.22, 0.0, 0.3)
    out['door_open'] = _normalise(
        _room(_soft_clip(_mix(grind * 0.7, ring * 0.5, thud * 0.8)),
              1.8, 3.0, 0.42, rng=rng), 0.62)

    # And coming back. The same grind reversed - it arrives rather than
    # departs - and it ends on the lock rather than starting from it.
    n = int(1.1 * SAMPLE_RATE)
    close = _filter(_noise(n, rng), 1200, 'low', 1.6) * _env(n, 0.02, 0.4, 0.2, 0.3)
    close *= 0.6 + 0.4 * np.sin(np.linspace(0.0, 26.0, n))
    latch = _struck(1.1, 98, (1.0, 2.0), (1.0, 1.8), bright=0.6) \
        * _env(n, 0.001, 0.3, 0.0, 0.4)
    seal = _struck(1.1, 392, bright=0.9) * _env(n, 0.55, 0.24, 0.0, 0.3)
    out['door_shut'] = _normalise(
        _room(_soft_clip(_mix(close * 0.6, latch * 0.9, seal * 0.45)),
              1.4, 3.6, 0.36, rng=rng), 0.6)

    # ---- the Keeper ---------------------------------------------------
    # The other two bosses sound like things. This one has to sound like a
    # *place* - a cathedral full of lamps - because what you are fighting is
    # the room. So everything here is long, bright and heavily reverberant,
    # where the Snuffer's set is dry and close.
    #
    # It is also the only voice in the game built on consonant intervals.
    # The Choir is a cluster and the Snuffer is noise; the Keeper is nearly
    # beautiful, which is the unpleasant part.

    # Waking. A struck bell that keeps arriving - one note, then the room
    # answering it, then the room answering that.
    n = int(4.0 * SAMPLE_RATE)
    bell = _mix(
        _struck(4.0, 98, (1.0, 2.0, 3.0, 4.02), (0.8, 1.4, 2.0, 3.0), bright=0.9)
        * _env(n, 0.004, 1.6, 0.24, 2.0),
        _struck(4.0, 147, bright=0.7) * _env(n, 0.35, 1.4, 0.2, 1.8) * 0.55,
        _struck(4.0, 196, bright=0.6) * _env(n, 0.9, 1.2, 0.16, 1.6) * 0.4)
    out['keep_wake'] = _normalise(
        _room(_soft_clip(bell), 3.6, 1.5, 0.52, damp=3400.0, rng=rng), 0.78)

    # The tell before every attack: a lamp hood lifting. Bright, rising, and
    # short enough to be a warning rather than an event.
    n = int(0.8 * SAMPLE_RATE)
    out['keep_tell'] = _normalise(
        _room(_soft_clip(_mix(
            _sweep(0.8, 330, 660, 'sine', curve=1.3) * _env(n, 0.01, 0.3, 0.3, 0.3),
            _struck(0.8, 880, bright=1.0) * _env(n, 0.002, 0.14, 0.0, 0.2) * 0.5)),
            1.1, 3.6, 0.36, rng=rng), 0.5)

    # Branding the floor. A struck match, many times over, low and warm.
    n = int(1.0 * SAMPLE_RATE)
    out['keep_brand'] = _normalise(
        _room(_soft_clip(_mix(
            _filter(_noise(n, rng), 3200, 'high', 1.4)
            * _env(n, 0.002, 0.12, 0.0, 0.16),
            _struck(1.0, 165, (1.0, 2.0, 3.0), (1.4, 2.2, 3.4), bright=0.8)
            * _env(n, 0.004, 0.4, 0.1, 0.5))), 1.3, 3.0, 0.4, rng=rng), 0.58)

    # A held lantern, lit and firing. Small, glassy, and it has to survive
    # four of itself going off at once.
    n = int(0.5 * SAMPLE_RATE)
    out['keep_lantern'] = _normalise(
        _room(_soft_clip(
            _struck(0.5, 587, (1.0, 2.01, 3.0), (1.8, 3.0, 4.4), bright=1.1)
            * _env(n, 0.002, 0.16, 0.0, 0.2)), 0.7, 4.4, 0.3, rng=rng), 0.4)

    # The glare sweeping. A long bowed tone - the sound of something being
    # drawn across the room rather than fired into it.
    n = int(1.4 * SAMPLE_RATE)
    out['keep_glare'] = _normalise(
        _room(_soft_clip(_cluster(1.4, 262, voices=5, detune=0.006,
                                  partials=(1.0, 2.0, 3.0))
                         * _env(n, 0.06, 0.5, 0.4, 0.6)),
              1.6, 2.6, 0.44, rng=rng), 0.5)

    # The reap, gathering. An inhale: everything in the room going the wrong
    # way. Built by reversing a decay, which is the only honest way to make
    # a sound that arrives instead of departing.
    n = int(1.6 * SAMPLE_RATE)
    gather = _reverse(
        _mix(_struck(1.6, 131, bright=0.8) * _env(n, 0.003, 1.1, 0.0, 0.5),
             _filter(_noise(n, rng), 2200, 'low', 1.6)
             * _env(n, 0.003, 1.0, 0.0, 0.5) * 0.5))
    out['keep_reap'] = _normalise(
        _room(_soft_clip(gather), 1.4, 3.0, 0.3, rng=rng), 0.66)

    # And the release. The loudest thing in the game, and the only one with
    # the whole room in it - it is every lantern in the vault at once.
    n = int(2.6 * SAMPLE_RATE)
    release = _mix(
        _struck(2.6, 262, (1.0, 1.5, 2.0, 3.0, 4.0), (0.6, 0.9, 1.3, 2.0, 2.8),
                bright=1.25) * _env(n, 0.001, 0.9, 0.12, 1.4),
        _struck(2.6, 392, bright=1.0) * _env(n, 0.001, 0.6, 0.08, 1.0) * 0.6,
        _filter(_noise(n, rng), 5000, 'high', 1.2)
        * _env(n, 0.001, 0.2, 0.0, 0.3) * 0.5)
    out['keep_release'] = _normalise(
        _room(_soft_clip(release), 3.0, 1.8, 0.5, damp=4200.0, rng=rng), 0.92)

    # A held lantern broken. Glass, and a small light going out. Named for
    # the lantern rather than for the Keeper, because `{voice}_break` is the
    # shared death sequence's and a boss coming apart must not sound like a
    # bulb going.
    n = int(0.7 * SAMPLE_RATE)
    out['keep_lantern_break'] = _normalise(
        _room(_soft_clip(_mix(
            _filter(_noise(n, rng), 5600, 'high', 2.2)
            * _env(n, 0.001, 0.18, 0.0, 0.22),
            _sweep(0.7, 880, 220, 'sine', curve=2.0)
            * _env(n, 0.002, 0.2, 0.0, 0.28) * 0.7)), 0.9, 4.0, 0.32,
            rng=rng), 0.52)

    # Crossing a phase. The room getting brighter, stated as a chord that
    # rises rather than falls - it is not losing, it is opening.
    n = int(2.2 * SAMPLE_RATE)
    out['keep_phase'] = _normalise(
        _room(_soft_clip(_mix(
            _struck(2.2, 196, bright=0.9) * _env(n, 0.004, 0.8, 0.14, 1.1),
            _struck(2.2, 294, bright=0.8) * _env(n, 0.16, 0.7, 0.12, 1.0) * 0.7,
            _struck(2.2, 392, bright=0.7) * _env(n, 0.34, 0.6, 0.1, 0.9) * 0.5)),
            2.6, 2.0, 0.46, rng=rng), 0.8)

    # Its eye, and its footfall, so the shared boss machinery has them.
    n = int(1.1 * SAMPLE_RATE)
    out['keep_eye'] = _normalise(
        _room(_soft_clip(_struck(1.1, 330, bright=1.0)
                         * _env(n, 0.006, 0.4, 0.1, 0.5)), 1.3, 3.2, 0.38,
              rng=rng), 0.46)
    n = int(1.8 * SAMPLE_RATE)
    out['keep_gather'] = _normalise(
        _room(_soft_clip(_cluster(1.8, 147, voices=6, detune=0.008)
                         * _env(n, 0.2, 0.7, 0.3, 0.8)), 2.0, 2.4, 0.44,
              rng=rng), 0.6)
    n = int(2.4 * SAMPLE_RATE)
    out['keep_toll'] = _normalise(
        _room(_soft_clip(_struck(2.4, 82, (1.0, 2.0, 3.0), (0.7, 1.2, 1.8),
                                 bright=0.7)
                         * _env(n, 0.004, 1.0, 0.16, 1.2)), 2.8, 1.9, 0.5,
              rng=rng), 0.74)
    n = int(2.0 * SAMPLE_RATE)
    out['keep_break'] = _normalise(
        _room(_soft_clip(_mix(
            _filter(_noise(n, rng), 4000, 'low', 1.4)
            * _env(n, 0.002, 0.8, 0.0, 1.0),
            _struck(2.0, 110, bright=0.6) * _env(n, 0.003, 0.7, 0.0, 1.0))),
            2.4, 2.2, 0.46, rng=rng), 0.8)

    # ---- the second bestiary ------------------------------------------
    # Each of these has to be identifiable with the lantern pointed the other
    # way, because most of the time it will be. So they are separated by
    # register rather than by timbre: the douser is the only cold high thing,
    # the bolter the only rising one, the delver the only sound that comes
    # from below.

    # A douser reaching you. The one sound in the game that has to feel like
    # a loss rather than a hit: a bright thing swallowed, so a short hiss
    # falling into nothing at all.
    n = int(1.1 * SAMPLE_RATE)
    hiss = _filter(_noise(n, rng), 5200, 'high', 1.8) * _env(n, 0.004, 0.28, 0.0, 0.3)
    swallow = _sweep(1.1, 720, 90, 'sine', curve=2.4) * _env(n, 0.01, 0.5, 0.0, 0.4)
    out['douse'] = _normalise(
        _room(_soft_clip(_mix(hiss * 0.55, swallow * 0.8)), 1.2, 3.0, 0.34,
              rng=rng), 0.56)

    # A bolter winding up: a rising whine that stops. It must be legible
    # across a busy room, so it is the only rising pitch in the bank.
    n = int(0.66 * SAMPLE_RATE)
    out['bolter_tell'] = _normalise(
        _room(_soft_clip(_sweep(0.66, 150, 520, 'saw', curve=1.5)
                         * _env(n, 0.02, 0.1, 0.75, 0.08)), 0.7, 5.0, 0.24,
              rng=rng), 0.44)
    # And releasing. A hard transient, all body, no tail - it is already gone.
    n = int(0.4 * SAMPLE_RATE)
    out['bolter_go'] = _normalise(
        _soft_clip(_mix(
            _struck(0.4, 118, (1.0, 1.9), (2.2, 4.0), bright=1.1)
            * _env(n, 0.001, 0.11, 0.0, 0.12),
            _filter(_noise(n, rng), 2400, 'low', 2.2)
            * _env(n, 0.001, 0.06, 0.0, 0.07) * 0.7)), 0.6)

    # A splitter coming apart. Wet, and doubled, because two things leave.
    n = int(0.6 * SAMPLE_RATE)
    tear = _filter(_noise(n, rng), 1500, 'low', 1.6) * _env(n, 0.002, 0.2, 0.0, 0.2)
    out['split'] = _normalise(
        _room(_soft_clip(_mix(
            tear * 0.8,
            _struck(0.6, 196, bright=0.8) * _env(n, 0.002, 0.14, 0.0, 0.2),
            _struck(0.6, 262, bright=0.8) * _env(n, 0.05, 0.14, 0.0, 0.2) * 0.7)),
            0.8, 4.6, 0.3, rng=rng), 0.5)

    # A delver going down, the mark it leaves, and coming back up. All three
    # are low and dry: the vault is between you and it.
    n = int(0.7 * SAMPLE_RATE)
    out['delve_down'] = _normalise(
        _room(_soft_clip(_mix(
            _sweep(0.7, 260, 60, 'sine', curve=1.8) * _env(n, 0.01, 0.3, 0.0, 0.3),
            _filter(_noise(n, rng), 700, 'low', 2.4)
            * _env(n, 0.005, 0.24, 0.0, 0.24) * 0.6)), 0.7, 5.5, 0.22,
            rng=rng), 0.44)
    n = int(1.0 * SAMPLE_RATE)
    # The tell is a pulse, not a tone: something knocking on the underside of
    # the floor. Three knocks in the second you have to move.
    knocks = np.zeros(n)
    for i, at in enumerate((0.02, 0.34, 0.64)):
        k = int(at * SAMPLE_RATE)
        body = _struck(0.3, 84 + i * 8, bright=0.4)
        seg = (body * _env(len(body), 0.002, 0.09, 0.0, 0.1))[:n - k]
        knocks[k:k + len(seg)] += seg * (0.7 + 0.15 * i)
    out['delve_tell'] = _normalise(
        _room(_soft_clip(knocks), 0.8, 6.0, 0.2, rng=rng), 0.42)
    n = int(0.75 * SAMPLE_RATE)
    out['delve_up'] = _normalise(
        _room(_soft_clip(_mix(
            _sweep(0.75, 70, 300, 'sine', curve=0.7) * _env(n, 0.002, 0.2, 0.0, 0.3),
            _filter(_noise(n, rng), 1800, 'low', 1.8)
            * _env(n, 0.001, 0.14, 0.0, 0.2))), 0.9, 4.4, 0.3, rng=rng), 0.56)

    # A carrion bursting. Low, wide and unpleasant, and it has to say
    # *something is on the floor now* rather than *something died*.
    n = int(1.3 * SAMPLE_RATE)
    burst = _filter(_noise(n, rng), 900, 'low', 1.4) * _env(n, 0.003, 0.5, 0.1, 0.6)
    out['carrion_burst'] = _normalise(
        _room(_soft_clip(_mix(
            burst * 0.9,
            _sweep(1.3, 150, 52, 'sine', curve=2.0) * _env(n, 0.005, 0.6, 0.0, 0.6))),
            1.4, 3.2, 0.38, rng=rng), 0.54)

    # A shot coming back off a mirror. Glassy and bright, and short: it is a
    # warning, and the thing it warns about is already in the air.
    n = int(0.45 * SAMPLE_RATE)
    out['reflect'] = _normalise(
        _room(_soft_clip(_mix(
            _struck(0.45, 1480, (1.0, 2.0, 3.01), (2.0, 3.4, 5.0), bright=1.3)
            * _env(n, 0.001, 0.12, 0.0, 0.16),
            _filter(_noise(n, rng), 6000, 'high', 2.0)
            * _env(n, 0.001, 0.04, 0.0, 0.05) * 0.5)), 0.6, 5.0, 0.28,
            rng=rng), 0.46)

    # ---- the Ferryman -------------------------------------------------
    # He is the one warm thing in the vault that is not yours, so his sounds
    # sit between the two: struck bodies like the player's, but pitched low
    # and slow like the building's.

    # The shelf opening. A lantern hood lifting: a soft knock, then a warm
    # chord that arrives late, the way a light does when it is uncovered
    # rather than lit.
    n = int(1.2 * SAMPLE_RATE)
    knock = _filter(_noise(n, rng), 800, 'low', 2.6) * _env(n, 0.001, 0.05, 0.0, 0.06)
    warmth = _mix(
        _struck(1.2, 233, bright=0.7) * _env(n, 0.10, 0.35, 0.4, 0.5),
        _struck(1.2, 349, bright=0.6) * _env(n, 0.16, 0.35, 0.3, 0.5) * 0.6)
    out['shop_open'] = _normalise(
        _room(_soft_clip(_mix(knock * 0.7, warmth)), 1.3, 3.4, 0.38, rng=rng),
        0.5)

    # A purchase. Embers changing hands: bright, short, and final - a rising
    # pair rather than a falling one, because it is a thing gained.
    n = int(0.7 * SAMPLE_RATE)
    coin = _mix(
        _struck(0.7, 740, (1.0, 2.7, 5.1), (1.6, 3.0, 5.0), bright=1.15)
        * _env(n, 0.001, 0.15, 0.18, 0.3),
        _struck(0.7, 1110, bright=0.95) * _env(n, 0.06, 0.16, 0.1, 0.3) * 0.5)
    out['shop_buy'] = _normalise(
        _room(_soft_clip(coin), 0.9, 4.4, 0.32, rng=rng), 0.5)

    # A refusal. Not a buzzer - a low double knock, the sound of a hand put
    # over something. Short enough to spam without becoming a nuisance.
    n = int(0.34 * SAMPLE_RATE)
    dull = _struck(0.34, 130, bright=0.35) * _env(n, 0.003, 0.1, 0.0, 0.14)
    tap = _filter(_noise(n, rng), 620, 'low', 3.0) * _env(n, 0.001, 0.05, 0.0, 0.08)
    out['shop_deny'] = _normalise(
        _room(_mix(dull * 0.85, tap * 0.5), 0.6, 6.0, 0.24, rng=rng), 0.36)

    # ---- the vault answering ------------------------------------------
    # Six sounds for the rooms. The rule they all obey: the vault's own
    # noises are *cold* and the player's are warm, the same split the light
    # already makes. Cold here means glassy inharmonic partials and a long
    # bright tail; warm means a struck body with even harmonics and a short
    # one.

    # Doors sealing behind you. A rising hum that arrives rather than
    # decays - the only sound in the game whose envelope goes up - because
    # what it is reporting is something closing, and a decay would read as
    # something finishing instead.
    n = int(1.0 * SAMPLE_RATE)
    rise = _sweep(1.0, 90, 380, 'sine', 2.2) * _env(n, 0.34, 0.2, 0.55, 0.3)
    glass = _struck(1.0, 780, (1.0, 2.76, 5.40, 8.93), (0.9, 1.8, 3.0, 4.4),
                    bright=1.25)
    glass *= _env(n, 0.22, 0.3, 0.35, 0.36) * 0.55
    grind = _filter(_noise(n, rng), 900, 'low', 2.4) * _env(n, 0.3, 0.3, 0.2, 0.3)
    out['ward_seal'] = _normalise(
        _room(_soft_clip(_mix(rise * 0.8, glass, grind * 0.35)),
              1.5, 3.0, 0.40, rng=rng), 0.52)

    # And giving. A shatter, downward: the same glass, struck hard and
    # falling apart, with the hum cut out from under it.
    n = int(0.85 * SAMPLE_RATE)
    shards = _filter(_noise(n, rng), 6200, 'high', 1.6) * _env(n, 0.001, 0.16, 0.06, 0.5)
    fall = _sweep(0.85, 620, 130, 'sine', 1.6) * _env(n, 0.004, 0.2, 0.12, 0.5)
    bell = _struck(0.85, 520, (1.0, 2.41, 4.16), (1.4, 2.8, 4.6), bright=1.1)
    bell *= _env(n, 0.002, 0.24, 0.16, 0.44) * 0.7
    out['ward_break'] = _normalise(
        _room(_soft_clip(_mix(shards * 0.7, fall * 0.6, bell)),
              1.3, 3.6, 0.36, rng=rng), 0.58)

    # Crossing a threshold. This one plays ten to twenty times a floor, so
    # it is built to be *almost* not there: a short low thud and a breath of
    # air, no tone at all. A door with a note in it would become a tune
    # nobody asked for by the third room.
    n = int(0.22 * SAMPLE_RATE)
    thud = _sweep(0.22, 150, 62, 'sine', 1.4) * _env(n, 0.004, 0.09, 0.0, 0.1)
    air = _filter(_noise(n, rng), 1100, 'low', 1.8) * _env(n, 0.02, 0.08, 0.0, 0.1)
    out['door_through'] = _normalise(
        _room(_mix(thud * 0.8, air * 0.3), 0.5, 6.0, 0.22, rng=rng), 0.3)

    # A cache opening. Warm, bright and busy - embers are the one thing in
    # this vault that is unambiguously good news.
    n = int(0.9 * SAMPLE_RATE)
    chime = _mix(
        _struck(0.9, 620, bright=1.1) * _env(n, 0.002, 0.2, 0.25, 0.42),
        _struck(0.9, 930, bright=0.9) * _env(n, 0.05, 0.22, 0.2, 0.4) * 0.6,
        _struck(0.9, 1245, bright=0.8) * _env(n, 0.1, 0.2, 0.15, 0.38) * 0.4)
    spill = _filter(_noise(n, rng), 3400, 'high', 1.4) * _env(n, 0.004, 0.3, 0.1, 0.4)
    out['cache_open'] = _normalise(
        _room(_soft_clip(_mix(chime, spill * 0.32)), 1.1, 4.2, 0.34, rng=rng),
        0.5)

    # A hearth taking you in. The warmest thing in the game: a low swell and
    # the broadband breath of a fire, and no attack to speak of, so it feels
    # like arriving somewhere rather than triggering something.
    n = int(1.6 * SAMPLE_RATE)
    warm = _mix(_struck(1.6, 196, bright=0.55) * _env(n, 0.12, 0.5, 0.5, 0.8),
                _struck(1.6, 294, bright=0.45) * _env(n, 0.2, 0.5, 0.4, 0.8) * 0.55)
    fire = _sweep_filter(_noise(n, rng), 2600, 700, 'low', 1.6)
    fire *= _env(n, 0.25, 0.6, 0.35, 0.7)
    out['hearth'] = _normalise(
        _room(_soft_clip(_mix(warm, fire * 0.4)), 1.8, 2.6, 0.44, rng=rng), 0.5)

    # A bargain struck. Two low bells a whisker apart, so they beat against
    # each other - the sound of something not quite agreeing with itself,
    # which is what a pact is - over stone dragging on stone.
    n = int(1.5 * SAMPLE_RATE)
    toll = _mix(_struck(1.5, 174, bright=0.7) * _env(n, 0.004, 0.45, 0.4, 0.7),
                _struck(1.5, 179, bright=0.7) * _env(n, 0.004, 0.45, 0.4, 0.7))
    stone = _filter(_noise(n, rng), 600, 'low', 2.6) * _env(n, 0.03, 0.5, 0.12, 0.6)
    out['shrine'] = _normalise(
        _room(_soft_clip(_mix(toll * 0.9, stone * 0.45)), 2.0, 2.4, 0.46,
              rng=rng), 0.56)

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

    out.update(_boss_sounds(rng))
    return out


# --------------------------------------------------------------------------
# The bosses
# --------------------------------------------------------------------------
# The rooms these effects live in. Everything about the two fights is
# opposed, and the room is where that starts: the Choir stands in a long,
# warm, dark-topped stone hall, and the Snuffer is in a short cold one made
# of glass. Every boss effect is put through one of these two, so each fight
# sounds like it is happening somewhere different even when the events are
# the same shape.
CHOIR_ROOM = dict(seconds=2.3, decay=2.2, mix=0.46, damp=2200.0)
SNUFF_ROOM = dict(seconds=1.5, decay=3.6, mix=0.34, damp=5200.0)


def _boss_sounds(rng):
    """A boss gets a sequence, not an effect.

    Everything else in the bank is a single event - a shot, a hit, a pickup.
    A boss arrives in three beats, fights through five or six of its own, and
    then takes nearly three seconds to come apart, and all of that is already
    driven off one clock in `World._tick_boss_intro` and
    `World._tick_boss_death`. So each beat gets its own one-shot fired as the
    countdown crosses it, and the sound and the light cannot drift apart.

    The two bosses share almost nothing. The Choir is a crowd of voices in a
    warm room; it arrives as a chord swelling up out of the floor, its
    attacks are pressure and crackle, and it dies groaning. The Snuffer is
    one cold thing that eats light; it arrives as a glass tone falling, its
    attacks are thin and unsteady, and its signature attack is the only sound
    in this game with no room on it at all.
    """
    out = {}

    # ===================================================== THE HOLLOW CHOIR ==
    # 1. The room answers. Nothing is visible yet - rings run outward from
    #    where it will stand and the floor shakes a little. So: sub-bass
    #    climbing out of nothing with the stone rattling on top of it, and
    #    the first hint of the voices only at the very end. The whole thing
    #    breathes at 0.9 Hz, which is slower than a resting pulse and reads
    #    as something very large being asleep.
    # 3.1 s of body, which is `World.BOSS_INTRO_TIME` exactly: this is the
    # bed the whole arrival lies on, and it should be swallowed by the slam
    # rather than still going afterwards. It peaks at 1.4 s, where the tolls
    # stop and the gathering starts.
    d = 3.1
    n = int(d * SAMPLE_RATE)
    sub = _sweep(d, 24, 43, 'sine', 1.3) * _env(n, 1.4, 0.5, 0.62, 1.2)
    grit = _sweep_filter(_noise(n, rng), 90, 260, 'low', 2.2) \
        * _env(n, 1.1, 0.6, 0.5, 1.4) * 0.5
    hint = _cluster(d, 81.5, 7, 0.012) * _env(n, 1.9, 0.4, 0.55, 0.8) * 0.34
    breathe = (0.78 + 0.22 * np.sin(_t(d) * math.tau * 0.9)).astype(np.float32)
    out['choir_wake'] = _normalise(
        _room(_soft_clip(_mix(sub, grit, hint) * breathe),
              **CHOIR_ROOM, rng=rng), 0.62)

    # 1b. The room counting it in. Five strikes on stone at closing
    #     intervals, one per ring that runs outward across the floor - and
    #     the reason those rings stopped being a random scatter. A drone can
    #     only sit under a sequence of discrete events; a bell can hit them.
    #     Baked across a pitch spread so the accelerando rises as it closes.
    d = 0.85
    n = int(d * SAMPLE_RATE)
    strike = _filter(_noise(n, rng), 900, 'low', 2.0) \
        * _env(n, 0.0008, 0.04, 0.0, 0.06)
    stone = _struck(d, 58.0, (1.0, 2.14, 3.42, 5.05), (0.9, 1.9, 3.1, 4.6),
                    bright=0.85) * _env(n, 0.002, 0.3, 0.22, 0.5)
    swell = _cluster(d, 87.0, 5, 0.01, (1.0, 2.0), falloff=1.6, glide=0.97) \
        * _env(n, 0.01, 0.25, 0.2, 0.5) * 0.5
    out['choir_toll'] = _normalise(
        _room(_soft_clip(_mix(strike * 0.8, stone, swell)),
              1.3, 3.2, 0.40, 2300.0, rng=rng), 0.72)

    # 2. It gathers: motes falling inward and its own light growing. The
    #    sound has to *arrive* rather than decay, so the riser goes through
    #    the room backwards - reflections in front of the event, the way the
    #    motes come in ahead of the thing they are building.
    #
    #    1.10 s of riser behind 0.50 s of pre-verb is 1.60 s in total,
    #    against the 1.705 s the second beat lasts. That is deliberate: it
    #    climaxes a tenth of a second *before* the eye opens, and the hole it
    #    leaves is what makes the slam land.
    d = 1.10
    n = int(d * SAMPLE_RATE)
    rise = _sweep_filter(_noise(n, rng), 220, 4200, 'low', 1.8) \
        * _env(n, 0.9, 0.12, 0.66, 0.06) * 0.8
    climb = _cluster(d, 96.0, 9, 0.016, (1.0, 2.0, 3.0, 4.0), glide=1.19) \
        * _env(n, 0.8, 0.18, 0.55, 0.12)
    lift = _sweep(d, 55, 165, 'sine', 2.2) * _env(n, 0.85, 0.18, 0.62, 0.09)
    out['choir_gather'] = _normalise(
        _swell(_soft_clip(_mix(rise, climb * 0.7, lift * 0.7)),
               0.5, 5.0, 0.5, 2600.0, rng), 0.62)

    # 3. It opens its eye. On screen: a white-out, a hitstop, a shake of
    #    thirteen and seventy particles leaving at once. The twenty
    #    milliseconds of silence at the front are deliberate - the impact
    #    lands in a hole rather than on top of the riser that preceded it,
    #    which is the same trick the hitstop plays on your eyes.
    d = 2.8
    n = int(d * SAMPLE_RATE)
    slam = _filter(_noise(n, rng), 1400, 'low', 2.2) \
        * _env(n, 0.0008, 0.05, 0.0, 0.05)
    drop = _sweep(d, 128, 27, 'sine', 0.35) * _env(n, 0.001, 0.35, 0.22, 1.4)
    # A minor chord, all of it sagging slightly: weight.
    chord = _mix(_cluster(d, 82.0, 9, 0.014, glide=0.985),
                 _cluster(d, 123.0, 7, 0.017, glide=0.985) * 0.7,
                 _cluster(d, 164.0, 5, 0.020, glide=0.985) * 0.45)
    chord *= _env(n, 0.02, 0.5, 0.42, 1.6)
    out['choir_eye'] = _normalise(
        _pad(_room(_soft_clip(_mix(slam * 0.9, drop, chord * 0.85,
                                   _struck(d, 61.0, (1.0, 2.19, 3.4),
                                           (0.8, 1.6, 2.6)) * 0.5)),
                   **CHOIR_ROOM, rng=rng), 0.02), 0.94)

    # The wind-up. Every boss attack draws an expanding ring before it lands;
    # this is that ring. A struck body played backwards swells *into* the
    # strike, which is what "about to" sounds like. Baked across a wide pitch
    # spread, because `begin_attack` asks for a higher take the angrier the
    # thing is - the warning you hear tightens as the warning you see gets
    # shorter, and unlike the ring it still works with your back to it.
    d = 0.46
    n = int(d * SAMPLE_RATE)
    ring = _reverse(_struck(d, 233.0, (1.0, 2.02, 3.05, 4.51),
                            (1.0, 1.9, 3.0, 4.4), bright=0.85)
                    * _env(n, 0.004, 0.35, 0.18, 0.24))
    air = _reverse(_sweep_filter(_noise(n, rng), 400, 2600, 'low', 1.6)
                   * _env(n, 0.01, 0.3, 0.2, 0.28)) * 0.32
    out['choir_tell'] = _normalise(
        _room(_mix(ring, air), 0.7, 5.0, 0.30, 2400.0, rng=rng), 0.44)

    # A tier boundary: the screen washes, time slows and it recoils. Its
    # voice cracks, then comes back a tone and a half lower with the floor
    # going with it.
    d = 2.0
    n = int(d * SAMPLE_RATE)
    crack = _mix(_filter(_noise(n, rng), 2600, 'high', 1.2)
                 * _env(n, 0.0006, 0.035, 0.0, 0.05),
                 _struck(0.8, 196.0, (1.0, 2.63, 4.11), (1.0, 2.2, 3.6)) * 0.7)
    fall = _cluster(d, 110.0, 9, 0.02, glide=0.89) * _env(n, 0.01, 0.5, 0.45, 1.0)
    under = _filter(_sweep(d, 96, 34, 'saw', 0.7), 380, 'low', 2.2) \
        * _env(n, 0.004, 0.4, 0.3, 1.0)
    out['choir_phase'] = _normalise(
        _room(_soft_clip(_mix(crack, fall * 0.9, under * 0.8)),
              **CHOIR_ROOM, rng=rng), 0.80)

    # The moment it dies, heard under two and a bit seconds of slow motion -
    # so it is long, and it sags. A structure failing rather than an
    # explosion, with the voices sliding away from each other in opposite
    # directions as it goes: the chord tearing itself in half.
    d = 3.0
    n = int(d * SAMPLE_RATE)
    groan = _filter(_sweep(d, 74, 21, 'saw', 0.5), 300, 'low', 2.4) \
        * _env(n, 0.02, 0.7, 0.4, 1.8)
    grind = _sweep_filter(_noise(n, rng), 900, 130, 'low', 1.8) \
        * _env(n, 0.01, 0.6, 0.3, 2.0) * 0.7
    unravel = _mix(_cluster(d, 104.0, 5, 0.006, glide=0.72),
                   _cluster(d, 104.0, 5, 0.006, glide=1.31) * 0.6)
    unravel *= _env(n, 0.05, 0.8, 0.3, 1.8)
    out['choir_break'] = _normalise(
        _room(_soft_clip(_mix(groan, grind, unravel * 0.75)),
              **CHOIR_ROOM, rng=rng), 0.88)

    # RING: a full circle of bolts and a flash of its own light. A pressure
    # release rather than a shot, so the transient is wide and the top end
    # falls away as it expands.
    d = 0.7
    n = int(d * SAMPLE_RATE)
    whump = _sweep(d, 210, 44, 'sine', 0.45) * _env(n, 0.002, 0.12, 0.16, 0.4)
    edge = _sweep_filter(_noise(n, rng), 3200, 700, 'low', 1.6) \
        * _env(n, 0.001, 0.09, 0.1, 0.42) * 0.8
    voice = _cluster(d, 165.0, 5, 0.02, (1.0, 2.0, 3.5)) \
        * _env(n, 0.004, 0.14, 0.12, 0.4) * 0.45
    out['choir_ring'] = _normalise(
        _room(_soft_clip(_mix(whump, edge, voice)),
              0.9, 4.2, 0.34, 2400.0, rng=rng), 0.60)

    # SPIRAL: eighteen bolts a second for a second and a half. This follows
    # `shoot_stitch`'s rule rather than the other attacks' - short,
    # soft-edged, no low end at all, and given one of the widest pitch
    # spreads in the bank, because the only real defence against a sound
    # that repeats that fast is never hearing the same one twice.
    d = 0.24
    n = int(d * SAMPLE_RATE)
    turn = _struck(d, 494.0, (1.0, 2.07, 3.44), (2.4, 4.0, 5.8), bright=0.7)
    hiss = _filter(_noise(n, rng), 1800, 'high', 1.0) \
        * _env(n, 0.002, 0.05, 0.0, 0.09) * 0.22
    out['choir_spiral'] = _normalise(
        _room(_filter(_mix(turn * 0.8, hiss), 5200, 'low', 1.0)
              * _env(n, 0.003, 0.08, 0.06, 0.12),
              0.35, 7.0, 0.20, 2600.0, rng=rng), 0.34)

    # SUMMON: something is pulled through into the room. A rip, a suck, and
    # then - a fifth of a second later, once it is actually here - a wet
    # snap. Played at the spawn point rather than at the boss, because that
    # is where the burst of particles is.
    d = 0.62
    n = int(d * SAMPLE_RATE)
    rip = _sweep_filter(_noise(n, rng), 5000, 900, 'high', 1.4) \
        * _env(n, 0.004, 0.12, 0.1, 0.3) * 0.75
    suck = _reverse(_sweep_filter(_noise(n, rng), 300, 2400, 'low', 1.8)
                    * _env(n, 0.02, 0.2, 0.3, 0.24)) * 0.6
    m = int(0.3 * SAMPLE_RATE)
    snap = _mix(_struck(0.3, 147.0, (1.0, 2.44, 3.9), (1.2, 2.6, 4.2)),
                _sweep(0.3, 320, 62, 'sine', 0.4) * _env(m, 0.001, 0.1, 0.08, 0.16))
    out['choir_summon'] = _normalise(
        _room(_soft_clip(_mix(rip, suck, _pad(snap, 0.2) * 0.8)),
              0.75, 5.0, 0.32, 2200.0, rng=rng), 0.52)

    # LASH: five fast bolts straight at you, a sixth of a second apart. A
    # crack of light - almost all transient, bright, and gone.
    d = 0.38
    n = int(d * SAMPLE_RATE)
    snapc = _filter(_noise(n, rng), 2200, 'high', 1.6) \
        * _env(n, 0.0004, 0.02, 0.0, 0.05)
    tail = _filter(_sweep(d, 940, 180, 'saw', 0.4), 2600, 'low', 2.0) \
        * _env(n, 0.0006, 0.06, 0.08, 0.2)
    out['choir_lash'] = _normalise(
        _room(_soft_clip(_mix(snapc * 0.9, tail)),
              0.5, 6.5, 0.26, 2400.0, rng=rng), 0.50)

    # CHARGE: forty tonnes of it crossing the room at seven hundred a
    # second. The player's `dash` was standing in for this and is far too
    # light - this is air being shoved, with the floor complaining
    # underneath, and the filter closes as the mass goes past.
    d = 1.0
    n = int(d * SAMPLE_RATE)
    airm = _sweep_filter(_noise(n, rng) * _env(n, 0.06, 0.22, 0.35, 0.5),
                         700, 240, 'low', 1.6) * 0.9
    mass = _sweep(d, 82, 38, 'sine', 0.6) * _env(n, 0.03, 0.3, 0.3, 0.55)
    scrape = _filter(_noise(n, rng), 180, 'low', 2.4) * _env(n, 0.04, 0.35, 0.25, 0.5)
    out['choir_charge'] = _normalise(
        _room(_soft_clip(_mix(airm, mass, scrape * 0.8)),
              **CHOIR_ROOM, rng=rng), 0.66)

    # ========================================================= THE SNUFFER ==
    # A warning, learned from `tools/sound_report.py`: the first pass at this
    # half of the bank reached for "cold" and got there with high-pass noise,
    # which measured at 7.6 kHz and 57% of its energy above 4 kHz on
    # `snuff_sweep` alone - against a bank mean of 438 Hz and 0.2%. It sounded
    # exactly like what the whole set had already been dragged away from once:
    # thin, hissing, machine-like. Cold has to come from *what is ringing* -
    # glass partials, hollow fifths, a short bright room - and every effect
    # here keeps a low-pass roof over it so that it can.
    #
    # 1. The room answers by going cold. The sub underneath is the same idea
    #    as the Choir's; everything above it is inverted. A glass tone
    #    falling instead of voices rising, and air being drawn off rather
    #    than stirred up.
    d = 3.1
    n = int(d * SAMPLE_RATE)
    sub = _sweep(d, 30, 21, 'sine', 0.8) * _env(n, 1.4, 0.5, 0.66, 1.2)
    glass = _cluster(d, 660.0, 3, 0.004, (1.0, 2.76, 4.2), falloff=1.4,
                     glide=0.55) * _env(n, 1.7, 0.45, 0.5, 0.95)
    drawn = _reverse(_sweep_filter(_noise(n, rng), 700, 2400, 'low', 1.6)
                     * _env(n, 0.8, 0.6, 0.4, 1.3)) * 0.42
    out['snuff_wake'] = _normalise(
        _room(_filter(_soft_clip(_mix(sub, glass * 0.5, drawn)),
                      3200, 'low', 1.6), **SNUFF_ROOM, rng=rng), 0.60)

    # 1b. Its count-in. The Choir strikes stone; this strikes something
    #     hollow and frozen, and each one takes a little of the room with it.
    d = 0.85
    n = int(d * SAMPLE_RATE)
    strike = _filter(_noise(n, rng), 2600, 'low', 1.8) \
        * _env(n, 0.0006, 0.03, 0.0, 0.05)
    ice = _struck(d, 174.0, (1.0, 2.71, 4.86, 7.2), (1.0, 2.2, 3.6, 5.4),
                  bright=1.0) * _env(n, 0.002, 0.26, 0.2, 0.5)
    hollow = _cluster(d, 116.0, 4, 0.006, (1.0, 1.4983), falloff=1.5,
                      glide=0.94) * _env(n, 0.01, 0.22, 0.18, 0.5) * 0.55
    out['snuff_toll'] = _normalise(
        _room(_filter(_soft_clip(_mix(strike * 0.7, ice, hollow)),
                      3400, 'low', 1.6), 1.0, 4.2, 0.32, 5000.0, rng=rng), 0.70)

    # 2. It gathers. Same backwards room as the Choir's, opposite motion:
    #    the intensity climbs while the pitch falls, which is the shape of
    #    something getting closer rather than merely louder. Same 1.60 s as
    #    the Choir's, and for the same reason.
    d = 1.10
    n = int(d * SAMPLE_RATE)
    rise = _sweep_filter(_noise(n, rng), 500, 3000, 'low', 1.6) \
        * _env(n, 0.9, 0.13, 0.62, 0.05) * 0.75
    fall = _cluster(d, 660.0, 5, 0.007, (1.0, 1.4983, 2.66), falloff=1.3,
                    glide=0.42) * _env(n, 0.8, 0.18, 0.58, 0.12)
    press = _sweep(d, 40, 96, 'sine', 2.0) * _env(n, 0.85, 0.18, 0.62, 0.09)
    out['snuff_gather'] = _normalise(
        _swell(_filter(_soft_clip(_mix(rise, fall * 0.8, press * 0.8)),
                       3600, 'low', 1.6), 0.5, 6.0, 0.46, 5200.0, rng), 0.60)

    # 3. It opens its eye. Where the Choir lands as a chord, this lands as
    #    something breaking: a hard strike on glass over a sub drop, and one
    #    cold hollow fifth left ringing where the Choir leaves a crowd.
    d = 2.8
    n = int(d * SAMPLE_RATE)
    strike = _filter(_noise(n, rng), 4200, 'low', 2.0) \
        * _env(n, 0.0006, 0.04, 0.0, 0.04)
    shatter = _mix(_struck(1.4, 1240.0, (1.0, 2.83, 5.44, 8.91, 12.4),
                           (1.2, 2.6, 4.2, 6.4, 9.0), bright=1.1),
                   _struck(1.0, 1960.0, (1.0, 3.21), (2.4, 5.0)) * 0.4)
    fall = _sweep(d, 150, 24, 'sine', 0.3) * _env(n, 0.001, 0.3, 0.2, 1.5)
    hold = _cluster(d, 220.0, 3, 0.005, (1.0, 1.4983, 3.0), falloff=1.4,
                    glide=0.97) * _env(n, 0.04, 0.6, 0.34, 1.7)
    out['snuff_eye'] = _normalise(
        _pad(_room(_soft_clip(_mix(strike, shatter * 0.8, fall, hold * 0.6)),
                   **SNUFF_ROOM, rng=rng), 0.02), 0.94)

    # The Snuffer's wind-up: the same backwards strike as the Choir's, higher
    # and made of glass rather than stone.
    d = 0.42
    n = int(d * SAMPLE_RATE)
    ring = _reverse(_struck(d, 466.0, (1.0, 2.71, 4.9), (1.4, 2.8, 4.6),
                            bright=1.05) * _env(n, 0.003, 0.32, 0.16, 0.2))
    air = _reverse(_sweep_filter(_noise(n, rng), 600, 2600, 'low', 1.4)
                   * _env(n, 0.01, 0.3, 0.2, 0.24)) * 0.30
    out['snuff_tell'] = _normalise(
        _room(_filter(_mix(ring, air), 4000, 'low', 1.6),
              0.5, 6.0, 0.26, 5200.0, rng=rng), 0.42)

    # Its tier boundary: glass giving way, and the cold fifth underneath it
    # dropping a third.
    d = 1.8
    n = int(d * SAMPLE_RATE)
    crack = _mix(_filter(_noise(n, rng), 4000, 'high', 1.4)
                 * _env(n, 0.0005, 0.03, 0.0, 0.06),
                 _struck(0.7, 784.0, (1.0, 2.88, 5.1), (1.2, 2.6, 4.4)) * 0.8)
    sag = _cluster(d, 294.0, 5, 0.008, (1.0, 1.4983, 2.0), falloff=1.4,
                   glide=0.84) * _env(n, 0.008, 0.45, 0.4, 0.95)
    under = _filter(_sweep(d, 110, 30, 'sine', 0.6), 320, 'low', 2.2) \
        * _env(n, 0.004, 0.35, 0.3, 0.85)
    out['snuff_phase'] = _normalise(
        _room(_soft_clip(_mix(crack, sag * 0.8, under * 0.9)),
              **SNUFF_ROOM, rng=rng), 0.78)

    # It comes apart the way it fought: the cold tone holding on far too
    # long and then sliding away under a structure failing.
    d = 3.0
    n = int(d * SAMPLE_RATE)
    groan = _filter(_sweep(d, 88, 19, 'saw', 0.45), 260, 'low', 2.4) \
        * _env(n, 0.02, 0.7, 0.4, 1.8)
    shear = _sweep_filter(_noise(n, rng), 2600, 300, 'low', 1.6) \
        * _env(n, 0.008, 0.5, 0.3, 2.1) * 0.55
    slide = _cluster(d, 392.0, 4, 0.005, (1.0, 2.76), falloff=1.2, glide=0.31) \
        * _env(n, 0.03, 0.8, 0.35, 1.8)
    out['snuff_break'] = _normalise(
        _room(_filter(_soft_clip(_mix(groan, shear, slide * 0.7)),
                      3000, 'low', 1.8), **SNUFF_ROOM, rng=rng), 0.88)

    # SWEEP: a turning arm of bolts, twenty a second. Something cold passing
    # overhead, on a beat.
    d = 0.38
    n = int(d * SAMPLE_RATE)
    passing = _sweep_filter(_noise(n, rng), 2400, 700, 'low', 1.4) \
        * _env(n, 0.01, 0.12, 0.14, 0.2) * 0.6
    tone = _struck(d, 587.0, (1.0, 2.42, 4.3), (1.8, 3.4, 5.2), bright=0.8) \
        * _env(n, 0.004, 0.1, 0.1, 0.22)
    out['snuff_sweep'] = _normalise(
        _room(_filter(_mix(passing, tone * 0.85), 3800, 'low', 1.6),
              0.42, 7.0, 0.22, 5200.0, rng=rng), 0.36)

    # MOTES: three bolts that follow you. Deliberately unsteady - the pitch
    # wobbles rather than holding, which is what a thing still deciding where
    # to go sounds like.
    d = 0.55
    n = int(d * SAMPLE_RATE)
    wob = 1.0 + 0.06 * np.sin(_t(d) * math.tau * 11.0)
    k = 2.0 * math.pi / SAMPLE_RATE
    core = np.sin(np.cumsum(349.0 * wob) * k).astype(np.float32) \
        * _env(n, 0.02, 0.18, 0.2, 0.3)
    fifth = np.sin(np.cumsum(523.0 * wob) * k).astype(np.float32) \
        * _env(n, 0.05, 0.2, 0.14, 0.28) * 0.45
    breath = _filter(_noise(n, rng), 1600, 'high', 1.0) \
        * _env(n, 0.02, 0.12, 0.1, 0.3) * 0.25
    out['snuff_motes'] = _normalise(
        _room(_mix(core, fifth, breath), 0.6, 5.5, 0.30, 5000.0, rng=rng), 0.46)

    # RUSH: small, fast and cold - a cut rather than the Choir's shove.
    # Almost no low end, and over before you have finished hearing it.
    d = 0.6
    n = int(d * SAMPLE_RATE)
    cut = _sweep_filter(_noise(n, rng) * _env(n, 0.02, 0.1, 0.3, 0.24),
                        800, 2800, 'low', 1.5)
    whine = _filter(_sweep(d, 320, 1180, 'saw', 1.6), 2200, 'low', 1.8) \
        * _env(n, 0.03, 0.14, 0.2, 0.22) * 0.55
    out['snuff_rush'] = _normalise(
        _room(_filter(_soft_clip(_mix(cut, whine)), 3400, 'low', 1.8),
              **SNUFF_ROOM, rng=rng), 0.56)

    # CHOKE: the signature, and the only effect in this bank with no room on
    # it.
    #
    # Everything else in the game goes through `_room`, because that is most
    # of what makes it sound like it is happening in the vault at all. This
    # attack puts your lantern out - so the sound of it is the vault itself
    # being taken away. A wide bright wash swells with a full tail behind it,
    # is cut dead at the instant the screen washes to black, and what lands
    # in the hole is a dry, airless thump with nothing behind it. It is the
    # one moment the game sounds like it is happening nowhere, and it costs
    # one call to `_gate` to say so.
    d = 1.0
    n = int(d * SAMPLE_RATE)
    wash = _sweep_filter(_noise(n, rng) * _env(n, 0.25, 0.18, 0.5, 0.2),
                         5600, 1400, 'low', 1.4)
    swell = _cluster(d, 262.0, 7, 0.01, (1.0, 1.4983, 2.0, 3.0), falloff=1.4,
                     glide=1.18) * _env(n, 0.3, 0.2, 0.5, 0.18)
    inhale = _room(_soft_clip(_mix(wash, swell * 0.8)),
                   1.2, 3.2, 0.42, 3000.0, rng=rng)
    inhale = _gate(inhale, 0.72, 0.018)[:int(0.78 * SAMPLE_RATE)]
    m = int(0.9 * SAMPLE_RATE)
    thump = _mix(_filter(_noise(m, rng), 420, 'low', 2.2)
                 * _env(m, 0.001, 0.09, 0.04, 0.3),
                 _sweep(0.9, 96, 26, 'sine', 0.4) * _env(m, 0.001, 0.12, 0.06, 0.4))
    out['snuff_choke'] = _normalise(
        _mix(inhale, _pad(_soft_clip(thump), 0.72)), 0.86)

    # ============================================================== ENDINGS ==
    # A piece of it breaking off, fired over and over across the two and a
    # half seconds it takes to come apart - so it gets five takes and a wide
    # spread, like every other sound the game plays in bursts.
    d = 0.5
    n = int(d * SAMPLE_RATE)
    snapd = _filter(_noise(n, rng), 1800, 'high', 1.4) \
        * _env(n, 0.0005, 0.03, 0.0, 0.06)
    chunk = _mix(_struck(d, 168.0, (1.0, 2.31, 3.86, 6.2),
                         (1.0, 2.2, 3.8, 5.6), bright=0.9),
                 _sweep(d, 260, 52, 'sine', 0.4) * _env(n, 0.001, 0.1, 0.08, 0.24))
    out['boss_crack'] = _normalise(
        _room(_soft_clip(_mix(snapd * 0.8, chunk)),
              0.8, 4.6, 0.34, 2500.0, rng=rng), 0.58)

    # The last collapse, and the largest thing in the bank. The chamber goes
    # white, the way down opens, and the light you have been nursing for six
    # floors comes back all at once - so this is the only effect in the game
    # that resolves *upward*. An impact, and then a warm chord blooming out
    # of it for three seconds. Both bosses die into it: whatever was in the
    # room, what is left afterwards is the same light.
    d = 3.4
    n = int(d * SAMPLE_RATE)
    hit = _filter(_noise(n, rng), 900, 'low', 2.0) * _env(n, 0.001, 0.09, 0.0, 0.12)
    fall = _sweep(d, 120, 24, 'sine', 0.3) * _env(n, 0.002, 0.4, 0.16, 1.9)
    bloom = _mix(_cluster(d, 131.0, 7, 0.008, (1.0, 2.0, 3.0, 4.0), glide=1.005),
                 _cluster(d, 196.0, 5, 0.009, (1.0, 2.0, 3.0), glide=1.004) * 0.7,
                 _cluster(d, 262.0, 5, 0.010, (1.0, 2.0), glide=1.003) * 0.5)
    bloom *= _env(n, 0.5, 0.9, 0.5, 1.8)
    bell = _struck(d, 262.0, (1.0, 2.02, 3.01, 4.07, 6.1),
                   (0.6, 1.2, 2.0, 3.0, 4.4), bright=0.9) \
        * _env(n, 0.05, 0.9, 0.4, 1.8)
    out['boss_gone'] = _normalise(
        _room(_soft_clip(_mix(hit, fall, bloom * 0.9, bell * 0.6)),
              2.6, 1.9, 0.50, 2800.0, rng=rng), 0.95)

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

    CHANNELS = 56
    #: Channels held back for the score. Music loops forever, so a stem that
    #: lost its channel to a burst of gunfire would simply stop and never come
    #: back - reserving them means `Sound.play` for an effect can never take
    #: one. See `lumen/music.py`.
    MUSIC_CHANNELS = 8

    # (takes, pitch spread, how much colour varies). Sounds you hear over and
    # over get the most; a one-shot like the boss roar wants to be the same
    # sound every time you hear it.
    VARIATION = {
        'shoot': (6, 0.085, 0.30),
        'shoot_scatter': (5, 0.075, 0.30),
        'shoot_heavy': (5, 0.060, 0.26),
        # Eight takes and a wide spread: this one repeats more than
        # anything else in the game and has to not wear out.
        'shoot_stitch': (8, 0.170, 0.42),
        'shoot_beam': (4, 0.050, 0.20),
        'enemy_shoot': (5, 0.090, 0.35),
        'hit': (6, 0.110, 0.35),
        'crit': (4, 0.060, 0.25),
        'shield': (5, 0.070, 0.28),
        'shield_break': (3, 0.050, 0.20),
        'kill': (5, 0.080, 0.30),
        'hurt': (4, 0.070, 0.25),
        'dash': (4, 0.070, 0.30),
        'pickup': (4, 0.055, 0.20),
        'boom': (3, 0.060, 0.25),
        'brazier': (3, 0.070, 0.25),
        'flare': (3, 0.045, 0.15),
        'charge': (2, 0.030, 0.10),
        'ui_move': (1, 0.0, 0.0),

        # Boss ceremony. An arrival, a phase break and a death are heard once
        # each per fight, and want to be the *same* sound every time you hear
        # them - a boss that announced itself slightly differently on every
        # floor would read as several bosses.
        'choir_wake': (1, 0.0, 0.0),
        'choir_gather': (1, 0.0, 0.0),
        'choir_eye': (1, 0.0, 0.0),
        'choir_phase': (1, 0.0, 0.0),
        'choir_break': (1, 0.0, 0.0),
        'snuff_wake': (1, 0.0, 0.0),
        'snuff_gather': (1, 0.0, 0.0),
        'snuff_eye': (1, 0.0, 0.0),
        'snuff_phase': (1, 0.0, 0.0),
        'snuff_break': (1, 0.0, 0.0),
        'snuff_choke': (1, 0.0, 0.0),
        'boss_gone': (1, 0.0, 0.0),

        # The wind-up is the exception among the one-shots: `begin_attack`
        # asks for a specific take by rage rather than a random one, so the
        # spread here is not variety but *range* - seven rungs from the
        # sound it makes at full health to the one it makes in its last
        # stand, tightening as its telegraph shortens.
        'choir_tell': (7, 0.200, 0.24),
        'snuff_tell': (7, 0.200, 0.24),
        # Same again for the arrival's count-in: five strikes at closing
        # intervals, each one a rung higher than the last.
        'choir_toll': (5, 0.190, 0.22),
        'snuff_toll': (5, 0.190, 0.22),

        # Attacks, on the same rule as everything else you hear repeatedly.
        # The spiral fires eighteen times a second, so it gets what
        # `shoot_stitch` gets.
        'choir_ring': (4, 0.070, 0.26),
        'choir_spiral': (8, 0.180, 0.44),
        'choir_summon': (5, 0.100, 0.30),
        'choir_lash': (5, 0.090, 0.32),
        'choir_charge': (3, 0.055, 0.20),
        'snuff_sweep': (7, 0.140, 0.36),
        'snuff_motes': (5, 0.095, 0.30),
        'snuff_rush': (3, 0.055, 0.20),
        'boss_crack': (5, 0.130, 0.34),
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
        self._music_next = 0

    def build(self):
        """Generate any missing WAVs. Safe to call more than once."""
        stamp = os.path.join(self.cache_dir, f'.v{CACHE_VERSION}')
        os.makedirs(self.cache_dir, exist_ok=True)
        if os.path.exists(stamp):
            return
        for name, signal in _make_sounds().items():
            _write_wav(os.path.join(self.cache_dir, f'{name}.wav'), signal)
        # The score. Separate from the effects because it is much slower to
        # build - half-minute drones with long reverb tails - and because
        # `music` imports from here, so the import has to go the other way.
        from . import music
        for name, signal in music.make_stems().items():
            _write_wav(os.path.join(self.cache_dir, f'{name}.wav'), signal)
        # Only ever one stamp, so a cache that has been through several
        # versions does not accumulate one marker per version it has seen.
        # The WAVs themselves are left alone: names do not collide, and a
        # stale one costs a few kilobytes and nothing else.
        for entry in os.listdir(self.cache_dir):
            if entry.startswith('.v') and entry != f'.v{CACHE_VERSION}':
                try:
                    os.remove(os.path.join(self.cache_dir, entry))
                except OSError:
                    pass
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
            # The reserved block is at the bottom of the range, and
            # `Sound.play()` with no channel never chooses from it.
            pygame.mixer.set_reserved(self.MUSIC_CHANNELS)
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
        """`pan` is -1 hard left to +1 hard right.

        `pitch` picks *which* take rather than resampling one. The variants
        are already baked in order from lowest and longest to highest and
        shortest, so -1 asks for the deepest of them and +1 for the
        tightest, at the cost of an index. It is how a boss's wind-up rises
        as the thing enrages, and it is free because the takes had to exist
        anyway.
        """
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
            if pitch is None:
                sound = takes[int(self._rng.integers(len(takes)))]
            else:
                k = (max(-1.0, min(1.0, pitch)) + 1.0) * 0.5 * (len(takes) - 1)
                sound = takes[int(round(k))]
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

    def ready(self):
        return self._ready and self.enabled and not self._failed

    def loop(self, name):
        """Start `name` looping forever on a reserved channel.

        Returns the channel so the caller can ride its volume, or None if
        there is no sound by that name or no channel left to give it. Started
        silent: the director fades it in, and a stem that arrived at full
        level would be the one thing in the score anybody noticed.
        """
        if not self.ready():
            return None
        takes = self._takes.get(name)
        if not takes:
            return None
        try:
            import pygame
            index = self._music_next % self.MUSIC_CHANNELS
            self._music_next += 1
            channel = pygame.mixer.Channel(index)
            channel.set_volume(0.0)
            channel.play(takes[0], loops=-1)
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


def play(name, volume=1.0, pan=0.0, pitch=None):
    bank().play(name, volume, pan, pitch)


# Beyond this much of a screen-width away, a sound is not worth hearing.
_EARSHOT = 1.35

# Where the view is, so a call site only has to know its own position.
# (camera x, camera y, view width, view height) in design units.
_listener = (0.0, 0.0, 1280.0, 720.0)


def set_listener(ox, oy, view_w, view_h):
    """Told once a frame by the world, so `play_at` can take world coords."""
    global _listener
    _listener = (ox, oy, max(view_w, 1.0), max(view_h, 1.0))


def play_at(name, x, y, volume=1.0, pitch=None):
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
                pan=max(-1.0, min(1.0, dx * 0.85)), pitch=pitch)
