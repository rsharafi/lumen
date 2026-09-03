"""The score, which is not a tune.

Eighty-four sound effects and silence underneath them. Nothing signals
"prototype" faster, and nothing is easier to get wrong: a run is half an hour
long, so anything with a melody in it becomes a thing the player is enduring
by floor six.

So this is drones. Sustained tones that move slowly, layered, with the layers
faded in and out by what is happening rather than sequenced. There is no beat
and no tune anywhere in it, and the only thing that ever arrives on a schedule
is a swell you cannot quite time.

## How it is put together

Two stems per act and one for a boss fight, each a seamless loop of about
half a minute:

* **bed** - always audible, at a level you stop noticing. The room tone of
  the act.
* **tension** - faded up as things get dangerous. A cluster a semitone or a
  third off the bed's root, which is what makes the two together feel wrong
  in a way one of them alone does not.
* **boss** - replaces both. Lower, denser, and the only stem with anything
  like forward motion in it.

The director crossfades between them over a couple of seconds. Nothing ever
cuts, because a cut is the one thing that would make the player notice there
is a system here.

## Why it is synthesised like the effects are

Same reason nothing else ships as an asset, and the same toolkit: struck
bodies, big rooms, filters that move. A score made of sampled pads would sit
on top of this game rather than in it - the effects are all resonant metal and
stone, and the music has to be the same building.

Loops are made seamless by generating a little extra and crossfading the tail
back over the head, which is the only way a drone can loop without a seam that
the ear finds within two passes.
"""

import math
import os

import numpy as np

from . import audio
from .audio import (SAMPLE_RATE, _cluster, _filter, _mix, _normalise, _room,
                    _soft_clip, _tone)

#: How long a stem runs before it repeats. Long enough that the loop is not
#: a texture in its own right, short enough to generate and hold in memory:
#: nine stems at this length is about 25 MB of int16.
LOOP_SECONDS = 26.0

#: How much is generated beyond the loop and folded back over its head. Long,
#: because these are drones with reverb tails and a short crossfade leaves a
#: dip rather than a join.
BLEND_SECONDS = 4.0

#: How long the director takes to move between two mixes.
FADE = 2.4

#: Ceiling for the whole music bus, before the player's own volume.
BUS = 0.34

BED = 'bed'
TENSION = 'tension'
BOSS = 'boss'


def _seamless(sig, seconds=LOOP_SECONDS, blend=BLEND_SECONDS):
    """Fold a signal's tail back over its head so it loops without a seam.

    A drone that simply restarts has a discontinuity the ear finds on the
    second pass, and fading to silence and back has a hole in it. Crossfading
    the overrun into the opening means the loop point is the *sum* of two
    parts of the same material, which is inaudible.
    """
    n = int(seconds * SAMPLE_RATE)
    b = int(blend * SAMPLE_RATE)
    if len(sig) < n + b:
        sig = np.pad(sig, (0, n + b - len(sig)))
    head = sig[:n].copy()
    tail = sig[n:n + b]
    ramp = np.linspace(0.0, 1.0, b, dtype=np.float32)
    head[:b] = head[:b] * ramp + tail * (1.0 - ramp)
    return head


def _drift(n, rate, depth, phase=0.0):
    """A slow sine over `n` samples, for moving a filter or a level.

    Rates here are fractions of a hertz on purpose. Anything faster reads as
    a tremolo, which is an effect; this should read as a room breathing.
    """
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    return 1.0 + depth * np.sin(t * rate * math.tau + phase)


def _bowed(seconds, freq, voices=9, detune=0.008, bright=1.0, rng=None):
    """A sustained, slightly unstable tone with body.

    `_cluster` gives the detuned stack; the drift on top is what stops it
    sounding like an oscillator left running. Real sustained sound - bowed
    metal, a struck bell held under a mallet - never holds a level.

    **Detune is a tempo.** The widest pair in the stack beats at `freq * 2 *
    detune` hertz, and on a bed rooted at 49 Hz a detune of 0.014 puts that
    at 1.4 Hz - which is not a texture, it is eighty-four beats a minute, and
    `tools/music_report.py` found it as a rhythm in three stems. Anything
    low and sustained wants its beating slower than about a third of a hertz,
    so a low root needs a *narrower* stack, not a wider one. The liveliness
    that buys back comes from the drift below, which is far too slow to tap
    to.
    """
    # `falloff` is how fast the partials thin out, so a low number is a
    # brighter tone. Inverted from `bright` here because every caller below
    # thinks in terms of how present the thing is, not how quickly it dies.
    # Harmonic partials, and only three. The stack used to run 1, 2, 3.01,
    # 4.02 - borrowed from the effects, where a little inharmonicity is what
    # makes a struck body sound like metal rather than a sine. Held for half a
    # minute it is something else entirely: a partial at 4.02x beats four
    # times faster than the fundamentals do, so a detune slow enough to be
    # texture down at the root arrives as a 2 Hz tremolo up there. Measured,
    # 23% modulation depth at half a second, which is a pulse.
    sig = _cluster(seconds, freq, voices=voices, detune=detune,
                   partials=(1.0, 2.0, 3.0),
                   falloff=2.6 - 0.9 * bright)
    n = len(sig)
    sig = sig * _drift(n, 0.037, 0.22, phase=freq * 0.01)
    return sig


def _breath(seconds, cutoff, rng, rate=0.05, depth=0.55):
    """Filtered noise that swells and falls. Air in a big empty place."""
    n = int(seconds * SAMPLE_RATE)
    noise = rng.standard_normal(n).astype(np.float32)
    band = _filter(_filter(noise, cutoff, 'low', 2.4), cutoff * 0.35, 'high',
                   1.4)
    return band * _drift(n, rate, depth) * 0.5


def _act_one(rng):
    """The Undercroft: dry stone, and a fifth that never resolves."""
    length = LOOP_SECONDS + BLEND_SECONDS
    root = 55.0                                   # A1
    bed = _mix(
        _bowed(length, root, voices=9, detune=0.004, bright=0.7, rng=rng),
        _bowed(length, root * 1.5, voices=7, detune=0.005, bright=0.5,
               rng=rng) * 0.34,
        _breath(length, 900.0, rng, rate=0.041) * 0.20,
    )
    bed = _filter(bed, 640.0, 'low', 1.6)
    bed = _room(_soft_clip(bed * 0.7), 3.4, 2.2, 0.44, damp=1800.0, rng=rng)

    # A minor third above the root, which is the smallest interval that makes
    # a drone sound like it is worried about something.
    tension = _mix(
        _bowed(length, root * 1.189, voices=11, detune=0.004, bright=1.15,
               rng=rng),
        _bowed(length, root * 2.0, voices=7, detune=0.0025, bright=0.9,
               rng=rng) * 0.28,
        _breath(length, 2200.0, rng, rate=0.09, depth=0.7) * 0.3,
    )
    tension = _filter(tension, 1500.0, 'low', 1.4)
    tension = _room(_soft_clip(tension * 0.8), 2.6, 3.0, 0.38, damp=2600.0,
                    rng=rng)
    return bed, tension


def _act_two(rng):
    """The Cisterns: the same building with water in it.

    A tone lower, wider detune so the stack beats against itself the way
    sound does over standing water, and a much longer, damper room.
    """
    length = LOOP_SECONDS + BLEND_SECONDS
    root = 49.0                                   # G1
    bed = _mix(
        _bowed(length, root, voices=11, detune=0.0035, bright=0.55, rng=rng),
        _bowed(length, root * 2.0, voices=7, detune=0.004, bright=0.4,
               rng=rng) * 0.3,
        _breath(length, 700.0, rng, rate=0.028, depth=0.7) * 0.26,
    )
    bed = _filter(bed, 520.0, 'low', 1.8)
    bed = _room(_soft_clip(bed * 0.7), 5.2, 1.7, 0.52, damp=1300.0, rng=rng)

    tension = _mix(
        _bowed(length, root * 1.414, voices=13, detune=0.006, bright=1.0,
               rng=rng),                          # a tritone; unsettled
        _bowed(length, root * 1.5, voices=7, detune=0.005, bright=0.8,
               rng=rng) * 0.45,
        _breath(length, 3000.0, rng, rate=0.11, depth=0.8) * 0.34,
    )
    tension = _filter(tension, 1900.0, 'low', 1.3)
    tension = _room(_soft_clip(tension * 0.82), 4.0, 2.4, 0.46, damp=2200.0,
                    rng=rng)
    return bed, tension


def _act_three(rng):
    """The Sealed Vault: lower, and a semitone that will not sit still."""
    length = LOOP_SECONDS + BLEND_SECONDS
    root = 41.2                                   # E1
    bed = _mix(
        _bowed(length, root, voices=13, detune=0.0035, bright=0.45, rng=rng),
        _bowed(length, root * 1.059, voices=9, detune=0.004, bright=0.35,
               rng=rng) * 0.42,                   # a semitone up: it grinds
        _breath(length, 560.0, rng, rate=0.023, depth=0.6) * 0.3,
    )
    bed = _filter(bed, 430.0, 'low', 2.0)
    bed = _room(_soft_clip(bed * 0.74), 4.4, 1.9, 0.5, damp=1100.0, rng=rng)

    tension = _mix(
        _bowed(length, root * 1.059, voices=13, detune=0.006, bright=1.2,
               rng=rng),
        _bowed(length, root * 1.498, voices=9, detune=0.005, bright=0.95,
               rng=rng) * 0.5,
        _breath(length, 3600.0, rng, rate=0.14, depth=0.9) * 0.38,
    )
    tension = _filter(tension, 2400.0, 'low', 1.2)
    tension = _room(_soft_clip(tension * 0.86), 3.2, 2.6, 0.44, damp=3000.0,
                    rng=rng)
    return bed, tension


def _boss_stem(rng):
    """One stem for all three fights.

    The only piece of this with forward motion in it - a slow rise that
    resets, which is what a fight feels like from inside. Still no beat: the
    bosses all telegraph by ear already, and a pulse under them would fight
    the tells the fights are built on.
    """
    length = LOOP_SECONDS + BLEND_SECONDS
    root = 36.7                                   # D1
    n = int(length * SAMPLE_RATE)
    core = _mix(
        _bowed(length, root, voices=15, detune=0.004, bright=0.8, rng=rng),
        _bowed(length, root * 1.059, voices=11, detune=0.0045, bright=0.7,
               rng=rng) * 0.55,
        # Quietly. A voice three octaves up beats three times faster than
        # the root does for the same detune, so this is the one that decides
        # whether the stem shimmers or throbs.
        _bowed(length, root * 2.997, voices=7, detune=0.0025, bright=1.3,
               rng=rng) * 0.10,
    )
    # A swell every eleven seconds or so - not a bar line, and deliberately
    # not a divisor of the loop, so it never lands in the same place twice
    # relative to anything else.
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    rise = 0.62 + 0.38 * (0.5 - 0.5 * np.cos(t / 11.3 * math.tau)) ** 1.6
    core = core * rise
    core = _filter(core, 1700.0, 'low', 1.5)
    # The air over it is a *hint*, not a layer. At 4.2 kHz and a third of the
    # level it dominated the spectrum outright - measured, a boss stem with a
    # centroid of 3857 Hz, which is a bright stem, which is the opposite of
    # what the heaviest thing in the game should be.
    core = _mix(core, _breath(length, 1500.0, rng, rate=0.17, depth=1.0) * 0.07)
    return _room(_soft_clip(core * 0.9), 3.8, 2.2, 0.42, damp=2400.0, rng=rng)


def make_stems():
    """Every loop, as {name: mono float32}. Slow; the cache exists for this."""
    rng = np.random.default_rng(4242)
    out = {}
    for index, build in ((1, _act_one), (2, _act_two), (3, _act_three)):
        bed, tension = build(rng)
        out[f'mus_{index}_{BED}'] = _normalise(_seamless(bed), 0.62)
        out[f'mus_{index}_{TENSION}'] = _normalise(_seamless(tension), 0.58)
    out[f'mus_{BOSS}'] = _normalise(_seamless(_boss_stem(rng)), 0.7)
    return out


# --------------------------------------------------------------------------
# The director
# --------------------------------------------------------------------------
class Director:
    """Decides what should be audible, and gets there without anyone noticing.

    Every stem is playing, looped, all the time; what changes is the volume of
    each. That is far simpler than starting and stopping them, and it is the
    only way to guarantee that a stem faded up mid-run arrives already in
    phase with the one it is joining.
    """

    def __init__(self):
        self.enabled = True
        self.act = 1
        self.boss = False
        #: 0 while exploring, 1 in a fight worth worrying about. Driven by
        #: the world; see `World.music_intensity`.
        self.intensity = 0.0
        self.master = 1.0
        self._levels = {}
        self._channels = {}
        self._started = False

    # ------------------------------------------------------------ control --
    def start(self):
        """Begin every loop, silently. Safe to call more than once."""
        if self._started or not self.enabled:
            return
        bank = audio.bank()
        if not bank.ready():
            return
        for name in self._names():
            channel = bank.loop(name)
            if channel is None:
                continue
            self._channels[name] = channel
            self._levels[name] = 0.0
        self._started = bool(self._channels)

    def stop(self):
        for channel in self._channels.values():
            try:
                channel.stop()
            except Exception:
                pass
        self._channels.clear()
        self._levels.clear()
        self._started = False

    def _names(self):
        out = [f'mus_{BOSS}']
        for index in (1, 2, 3):
            out.append(f'mus_{index}_{BED}')
            out.append(f'mus_{index}_{TENSION}')
        return out

    def set_scene(self, act=None, boss=None, intensity=None):
        if act is not None:
            self.act = max(1, min(3, int(act)))
        if boss is not None:
            self.boss = bool(boss)
        if intensity is not None:
            self.intensity = max(0.0, min(1.0, float(intensity)))

    def set_enabled(self, value):
        self.enabled = bool(value)
        if not self.enabled:
            self.stop()

    # ------------------------------------------------------------- update --
    def target(self):
        """What each stem should be at, right now."""
        want = {name: 0.0 for name in self._names()}
        if not self.enabled:
            return want
        if self.boss:
            want[f'mus_{BOSS}'] = 1.0
            # The act's bed stays underneath at a whisper, so a boss room is
            # still recognisably in the act it belongs to.
            want[f'mus_{self.act}_{BED}'] = 0.22
            return want
        want[f'mus_{self.act}_{BED}'] = 1.0
        # Tension comes up on a curve rather than linearly: the first enemy
        # in a room should barely move it, and the difference between a hard
        # fight and a desperate one should be audible.
        want[f'mus_{self.act}_{TENSION}'] = self.intensity ** 1.7
        return want

    def update(self, dt):
        if not self._started:
            self.start()
            if not self._started:
                return
        want = self.target()
        k = 1.0 - math.exp(-dt / max(FADE, 1e-3) * 3.0)
        bank = audio.bank()
        gain = BUS * self.master * (bank.volume if bank else 1.0)
        for name, channel in self._channels.items():
            now = self._levels.get(name, 0.0)
            now += (want.get(name, 0.0) - now) * k
            self._levels[name] = now
            try:
                channel.set_volume(max(0.0, min(1.0, now * gain)))
            except Exception:
                pass


_director = None


def director():
    global _director
    if _director is None:
        _director = Director()
    return _director
