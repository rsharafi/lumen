"""The launch screen: a lantern lit in the dark, and the vault waking round it.

Every sound in the game is synthesised, and turning that into something the
mixer can play takes seconds - most of four on a warm cache, seven on the
first launch, and more on a slower machine. Those seconds used to happen
before the window existed. Now they happen behind this, on a worker (see
`audio.SoundBank.prepare_async`), and this is built so that the wait is the
first thing the game shows you rather than something it apologises for:

* **Dark.** Nothing but a low drone and the stone you cannot see yet.
* **The spark.** One ember falls out of the top of the screen, lighting the
  floor as it passes, and catches.
* **The dial.** Twenty sockets are carved round the flame - one for each
  floor of the descent, with the three boss floors set larger. As the sound
  bank comes in, sparks swirl out of the flame and light them one by one,
  each with a chime, climbing through the three acts; a boss floor tolls
  instead, and burns cold. The lantern's light spreads over the stone with
  them. Clicking stokes the flame.
* **The flare.** When the last socket is lit and every sound is ready, the
  dial spins up, the flame goes white, and the flash it ends in is where the
  title screen is cut in underneath - with the shockwave and the embers still
  going over the top of it, and the score coming up as they fade.

The launch screen's own sounds are made before its first frame, in a few tens
of milliseconds, so it is never silent. By the time it hands over, all the
others are there too.

The dial is honest but not literal. It never runs ahead of the real progress
and never finishes before the sound has, but it also never fills in less than
`FILL_MIN`: a machine that loads everything in a second still gets to see the
thing it is looking at happen.
"""

import math

import numpy as np

from . import art, gpu, palette
from .audio import (SAMPLE_RATE, _env, _filter, _mix, _noise, _normalise,
                    _pad, _resample, _room, _soft_clip, _struck, _sweep,
                    _sweep_filter)
from .config import BOSS_FLOORS, FLOORS_PER_RUN
from .draw import drawImage, drawLabel, drawLine, drawPolygon
from .mathx import clamp, ease_in_cubic, ease_out_back, ease_out_cubic, lerp

# ==========================================================================
# Sound
# ==========================================================================
# Loops are exactly this long, and everything periodic in them fits a whole
# number of cycles into it, so they wrap without a click.
LOOP = 4.0

# The chime each ordinary socket plays, in semitones from A4. A minor
# pentatonic, climbing act by act so the dial rises as it fills.
_CHIME = {
    1: 0, 2: 3, 3: 5, 4: 7, 5: 10,
    7: 3, 8: 5, 9: 7, 10: 10, 11: 12, 12: 15,
    14: 7, 15: 10, 16: 12, 17: 15, 18: 17, 19: 19,
}
# And the tolls, from A2: each boss floor lower than the last's.
_TOLL = {6: 0, 13: -2, 20: -5}


def _loop_t():
    n = int(LOOP * SAMPLE_RATE)
    return np.arange(n, dtype=np.float32) / SAMPLE_RATE


def _drone(rng):
    """The vault before anything happens in it: a low fifth and moving air."""
    t = _loop_t()
    tau = 2.0 * math.pi
    swell = 0.82 + 0.18 * np.sin(tau * t * (2.0 / LOOP))
    tone = (0.55 * np.sin(tau * 55.0 * t)
            + 0.30 * np.sin(tau * 82.5 * t + 0.7)
            + 0.12 * np.sin(tau * 110.0 * t + 1.9)
            * (0.6 + 0.4 * np.sin(tau * t / LOOP)))
    # Filtered in the frequency domain over exactly the loop's length, which
    # treats the noise as periodic - so the air wraps as cleanly as the tone.
    air = _normalise(_filter(_noise(t.shape[0], rng), 320.0, 'low', 2.0), 1.0)
    return _normalise((tone * swell + air * 0.32).astype(np.float32), 0.8)


def _wake(rng):
    """What the vault sounds like with a light in it: a slow, breathing chord.

    Faded up under the drone as the dial fills. Every voice is a whole number
    of cycles over the loop, and each is doubled a quarter of a hertz either
    side, so the chord beats slowly and still wraps without a seam.
    """
    t = _loop_t()
    tau = 2.0 * math.pi
    out = np.zeros(t.shape[0], dtype=np.float32)
    for k, freq in enumerate((220.0, 261.75, 329.5, 392.0, 493.75)):
        breathe = 0.65 + 0.35 * np.sin(tau * t * ((k % 2 + 1) / LOOP) + k)
        for detune in (-0.25, 0.25):
            out += (np.sin(tau * (freq + detune) * t + k * 1.3) * breathe
                    / (1.0 + k * 0.5))
    shimmer = _filter(_noise(t.shape[0], rng), 5200.0, 'high', 1.5)
    shimmer = _filter(shimmer, 7800.0, 'low', 1.5)
    out += _normalise(shimmer, 1.0) * 0.05 * (0.5 + 0.5 * np.sin(tau * t / LOOP))
    return _normalise(out, 0.7)


def _strike(rng):
    """A match on stone: the scrape, the catch, and the flame's first breath."""
    scrape_n = int(0.085 * SAMPLE_RATE)
    grit = _filter(_noise(scrape_n, rng), 2800.0, 'high', 1.4)
    k = np.arange(scrape_n, dtype=np.float32) / SAMPLE_RATE
    grit *= np.exp(-k * 22.0) * (0.55 + 0.45 * (np.sin(2 * math.pi * 47.0 * k)
                                                > -0.2))
    body_n = int(1.1 * SAMPLE_RATE)
    body = _filter(_noise(body_n, rng), 1300.0, 'low', 1.4)
    body *= _env(body_n, attack=0.03, decay=0.95, sustain=0.0, release=0.01)
    thump = _sweep(0.32, 118.0, 44.0)
    thump *= np.exp(-np.linspace(0.0, 6.0, thump.shape[0], dtype=np.float32))
    crackle = np.zeros(int(1.4 * SAMPLE_RATE), dtype=np.float32)
    click = _filter(_noise(160, rng), 3000.0, 'high', 1.0)
    for _ in range(30):
        at = rng.uniform(0.1, 1.3)
        pos = int(at * SAMPLE_RATE)
        crackle[pos:pos + 160] += click * rng.uniform(0.2, 1.0) * math.exp(-at * 2.2)
    out = _mix(grit * 0.8, _pad(body * 0.95, 0.055), _pad(thump * 0.8, 0.055),
               _pad(crackle * 0.35, 0.05))
    return _normalise(_soft_clip(_room(out, 1.0, 4.8, 0.24, 2800.0, rng)), 0.9)


def _chime(rng):
    """An ember catching in its socket: struck glass, and a breath of room."""
    bell = _struck(1.1, 440.0, partials=(1.0, 2.0, 2.76, 5.4),
                   decays=(1.0, 1.7, 2.6, 4.4), bright=0.75)
    tick = _filter(_noise(220, rng), 4000.0, 'high', 1.0)
    tick *= np.linspace(1.0, 0.0, 220, dtype=np.float32)
    return _normalise(_room(_mix(bell, tick * 0.3), 0.8, 5.0, 0.28, 3600.0,
                            rng), 0.8)


def _toll(rng):
    """A boss floor: something much larger, a long way down."""
    bell = _struck(3.0, 110.0, partials=(1.0, 2.0, 2.41, 3.0, 4.2, 5.4),
                   decays=(0.55, 0.9, 1.3, 1.7, 2.5, 3.3), bright=0.9)
    sub = _sweep(2.2, 56.0, 54.0)
    sub *= np.exp(-np.linspace(0.0, 3.0, sub.shape[0], dtype=np.float32))
    return _normalise(_soft_clip(_room(_mix(bell, sub * 0.6), 2.0, 2.8, 0.34,
                                       2000.0, rng)), 0.88)


# The flare's sound builds for this long before it lands, and the picture is
# timed to it: the flash is exactly when the boom is.
RISER = 0.45


def _flare(rng):
    """Everything at once: the build, the boom, and the chord it opens into."""
    riser_n = int(RISER * SAMPLE_RATE)
    riser = _sweep_filter(_noise(riser_n, rng), 300.0, 7000.0, 'low', 1.6)
    riser *= np.linspace(0.0, 1.0, riser_n, dtype=np.float32) ** 2.6
    boom = _sweep(1.3, 92.0, 30.0)
    boom *= np.exp(-np.linspace(0.0, 4.2, boom.shape[0], dtype=np.float32))
    burst_n = int(0.9 * SAMPLE_RATE)
    burst = _filter(_noise(burst_n, rng), 850.0, 'low', 1.6)
    burst *= np.exp(-np.linspace(0.0, 6.0, burst_n, dtype=np.float32))
    t = np.arange(int(3.0 * SAMPLE_RATE), dtype=np.float32) / SAMPLE_RATE
    chord = np.zeros(t.shape[0], dtype=np.float32)
    for k, freq in enumerate((110.0, 220.0, 261.63, 329.63, 493.88, 880.0)):
        for detune in (-0.7, 0.7):
            chord += np.sin(2 * math.pi * (freq + detune) * t) / (1.0 + k * 0.35)
    chord *= np.exp(-t * 1.05) * np.minimum(1.0, t / 0.02)
    sparkle = np.zeros(int(1.6 * SAMPLE_RATE), dtype=np.float32)
    ping = [_struck(0.45, f, bright=0.6) for f in (1760.0, 2093.0, 2637.0, 3136.0)]
    for i in range(16):
        at = rng.uniform(0.0, 1.1)
        pos = int(at * SAMPLE_RATE)
        p = ping[i % len(ping)]
        end = min(sparkle.shape[0], pos + p.shape[0])
        sparkle[pos:end] += p[:end - pos] * math.exp(-at * 1.8) * rng.uniform(0.4, 1.0)
    out = _mix(riser * 0.7, _pad(_mix(boom, burst * 0.7), RISER),
               _pad(_normalise(chord, 1.0) * 0.5, RISER),
               _pad(_normalise(sparkle, 1.0) * 0.22, RISER + 0.04))
    return _normalise(_soft_clip(_room(out, 1.8, 2.5, 0.32, 3000.0, rng)), 0.94)


def _gust(rng):
    """Breath on the flame."""
    n = int(0.55 * SAMPLE_RATE)
    air = _filter(_filter(_noise(n, rng), 1600.0, 'low', 1.5), 180.0, 'high',
                  1.2)
    k = np.linspace(0.0, 1.0, n, dtype=np.float32)
    flutter = 0.75 + 0.25 * np.sin(2 * math.pi * 17.0 * k * 0.55)
    shape = np.maximum(np.sin(math.pi * k), 0.0) ** 1.6
    return _normalise((air * shape * flutter).astype(np.float32), 0.7)


def make_sounds():
    """Everything the launch screen plays, as {name: mono float32}.

    Made on the main thread before the first frame, so it has to be quick:
    short signals, and the pitched sets are resampled from one strike each
    rather than synthesised twenty times.
    """
    rng = np.random.default_rng(1729)
    out = {
        'drone': _drone(rng),
        'wake': _wake(rng),
        'strike': _strike(rng),
        'flare': _flare(rng),
        'gust': _gust(rng),
    }
    chime = _chime(rng)
    for notch, semis in _CHIME.items():
        out[f'notch{notch}'] = _resample(chime, 2.0 ** (semis / 12.0))
    toll = _toll(rng)
    for notch, semis in _TOLL.items():
        out[f'notch{notch}'] = _resample(toll, 2.0 ** (semis / 12.0))
    return out


# ==========================================================================
# Picture
# ==========================================================================
DARK = 0.5          # seconds of nothing but the drone
FALL = 0.55         # the spark falling
FILL_DELAY = 0.35   # after the flame catches, before the first socket
FILL_MIN = 2.2      # the dial never fills faster than this
TRAVEL = 0.24       # a spark's flight from the flame to its socket
STAGGER = 0.06      # sparks leave the flame at least this far apart
HOLD = 0.28         # the full dial, held, before the flare
AFTERGLOW = 1.15    # the flash and the shockwave, over the title
PATIENCE = 15.0     # past this, a click goes on without the rest of the sound

R_NOTCH = 168.0     # where the sockets sit, from the dial's centre
LIGHT_HEIGHT = 30.0


def _notch_angle(notch, spin=0.0):
    """Socket 1 just right of the top, running clockwise."""
    return -math.pi * 0.5 + (notch - 0.5) * (2.0 * math.pi / FLOORS_PER_RUN) + spin


def _ring(cx, cy, r_in, r_out, color, opacity, segments=64, start=0.0,
          sweep=2.0 * math.pi):
    step = sweep / segments
    for i in range(segments):
        a0 = start + i * step
        a1 = a0 + step
        c0, s0, c1, s1 = math.cos(a0), math.sin(a0), math.cos(a1), math.sin(a1)
        drawPolygon(cx + c0 * r_in, cy + s0 * r_in, cx + c0 * r_out,
                    cy + s0 * r_out, cx + c1 * r_out, cy + s1 * r_out,
                    cx + c1 * r_in, cy + s1 * r_in, fill=color, opacity=opacity)


def _diamond(x, y, r, color, opacity, stretch=1.0):
    drawPolygon(x, y - r * stretch, x + r, y, x, y + r * stretch, x - r, y,
                fill=color, opacity=int(clamp(opacity, 0, 100)))


def _flame_shape(cx, base_y, height, width, sway):
    """A teardrop: rounded at the root, drawn out to a point that sways."""
    peak = 0.28
    norm = 1.0 / (peak ** 0.45 * (1.0 - peak) ** 1.25)
    left, right = [], []
    steps = 16
    for i in range(steps + 1):
        u = i / steps
        half = width * norm * (u ** 0.45) * ((1.0 - u) ** 1.25)
        x = cx + sway * u * u
        y = base_y - u * height
        left.append((x - half, y))
        right.append((x + half, y))
    pts = left + right[::-1]
    return [v for p in pts for v in p]


class LaunchScreen:
    def __init__(self, view_w, view_h, bank, rng):
        self.w = view_w
        self.h = view_h
        self.bank = bank
        self.rng = rng
        self.t = 0.0
        self.phase = 'dark'
        self.kindle_t = None
        self.hold_t = None
        self.flare_t = None
        self.impact_t = None
        #: Set on the frame the flash lands. The game cuts to the title then,
        #: and clears it; this keeps running as the afterglow over the top.
        self.handoff = False
        self.done = False
        self.shown = 0.0
        self._last_progress = 0.0
        self._creep = 0.0
        self.lit_at = {}                # notch -> when it caught
        self.flying = []                # [notch, seconds in flight]
        self.next_launch = 0.0
        self.embers = []                # [x, y, vx, vy, life, max, size, drag]
        self.ember_debt = 0.0
        self.spin = 0.0
        self.gust = 0.0
        self.last_gust = -1.0
        self.poked = False
        self.drone = None
        self.wake = None
        self.tiles = {}
        self.first_run = bank.first_run()
        self.runes = self._carve_runes()
        # Before the first frame rather than one a frame after it. Each is
        # ~40 ms of PIL, and a frame that runs 40 ms long is one the fixed
        # simulation step cannot catch up on - so paid here, where the screen
        # is black anyway and nothing is moving yet to fall behind.
        for _ in range(3):
            self._bake_tile()
        bank.add_early(make_sounds())

    # ------------------------------------------------------------ setup --
    def _carve_runes(self):
        """The script round the rim: a few strokes each, the same every launch."""
        rng = np.random.default_rng(606)
        runes = []
        count = 64
        for i in range(count):
            a = (i + 0.5) / count * 2.0 * math.pi
            strokes = []
            for _ in range(int(rng.integers(2, 4))):
                x0, y0 = rng.uniform(-3.5, 3.5), rng.uniform(-6.0, 6.0)
                x1, y1 = rng.uniform(-3.5, 3.5), rng.uniform(-6.0, 6.0)
                strokes.append((x0, y0, x1, y1))
            runes.append((a, strokes))
        return runes

    def resize(self, view_w, view_h):
        self.w = view_w
        self.h = view_h
        # The tiles belong to the render scale, which a resize may have
        # changed; they are baked again, one a frame, as they were at first.
        self.tiles = {}

    def _bake_tile(self):
        """One stone tile a frame, until all three are there."""
        for variant in range(3):
            if variant in self.tiles:
                continue
            seed = 5 + variant * 7
            stone = art.floor_tile(seed)
            key = ('launch-tile', variant)
            sprite = art.cached(key) or art.wrap(stone, key)
            nkey = key + ('normal',)
            normal = art.cached(nkey) or art.wrap(
                art.normal_map(stone, keep_half=True), nkey)
            self.tiles[variant] = (sprite, normal)
            return

    # ----------------------------------------------------------- sound ---
    def _play(self, name, volume=1.0, pan=0.0):
        self.bank.play_early(name, volume, pan)

    def _set_volume(self, channel, volume):
        if channel is None:
            return
        try:
            channel.set_volume(max(0.0, min(1.0, volume * self.bank.volume)))
        except Exception:
            pass

    def _fade_out(self, channel, ms):
        if channel is None:
            return
        try:
            channel.fadeout(ms)
        except Exception:
            pass

    # ----------------------------------------------------------- input ---
    def poke(self, pos=None):
        """A click or a key. Stokes the flame; past the flash, gets out of
        the way."""
        if self.phase == 'kindle':
            seen = self.t - self.kindle_t
            if self.bank.progress() >= 1.0 and seen > 1.2:
                # Everything is ready and they have seen the idea: get on
                # with it rather than make them watch the rest of the dial.
                self._begin_flare()
                return
            if seen > PATIENCE:
                self._begin_flare()
                return
            self.poked = True
            self.gust = 1.0
            if self.t - self.last_gust > 0.14:
                self.last_gust = self.t
                pan = 0.0
                if pos is not None:
                    pan = clamp((pos[0] - self.w * 0.5) / (self.w * 0.5), -1, 1)
                self._play('gust', 0.55, pan * 0.6)
            cx, fy = self._flame_base()
            for _ in range(14):
                self._spawn_ember(cx, fy - 12.0, burst=True)
        elif self.phase == 'flare':
            self._impact()
        elif self.phase == 'after':
            self.impact_t = min(self.impact_t, self.t - AFTERGLOW * 0.7)

    # ---------------------------------------------------------- update ---
    def _flame_base(self):
        return self.w * 0.5, self.h * 0.45 + 10.0

    def _notch_pos(self, notch):
        a = _notch_angle(notch, self.spin)
        return (self.w * 0.5 + math.cos(a) * R_NOTCH,
                self.h * 0.45 + math.sin(a) * R_NOTCH)

    def _spawn_ember(self, x, y, burst=False, speed=0.0):
        rng = self.rng
        if speed > 0.0:
            a = rng.uniform(0.0, 2.0 * math.pi)
            v = rng.uniform(speed * 0.35, speed)
            self.embers.append([x, y, math.cos(a) * v, math.sin(a) * v,
                                rng.uniform(0.5, 1.1), 1.1,
                                rng.uniform(1.4, 3.2), 2.6])
            return
        spread = 60.0 if burst else 14.0
        lift = (80.0, 170.0) if burst else (38.0, 92.0)
        life = rng.uniform(1.0, 2.2)
        self.embers.append([x + rng.uniform(-5, 5), y,
                            rng.uniform(-spread, spread),
                            -rng.uniform(*lift), life, life,
                            rng.uniform(1.0, 2.4), 0.6])

    def update(self, dt):
        self.t += dt
        t = self.t
        progress = self.bank.progress()

        if self.drone is None and t > 0.05:
            self.drone = self.bank.loop_early('drone')
        if self.phase in ('dark', 'fall', 'kindle', 'flare'):
            self._set_volume(self.drone, 0.5 * min(1.0, t / 1.4))
        if len(self.tiles) < 3:
            self._bake_tile()      # after a resize, at the new scale

        if self.phase == 'dark' and t >= DARK:
            self.phase = 'fall'
        elif self.phase == 'fall' and t >= DARK + FALL:
            self._ignite()
        elif self.phase == 'kindle':
            self._kindle(dt, progress)
        elif self.phase == 'flare':
            self.spin += dt * 10.0 * ease_in_cubic(
                (t - self.flare_t) / RISER)
            if t - self.flare_t >= RISER:
                self._impact()
        elif self.phase == 'after':
            if t - self.impact_t >= AFTERGLOW:
                self.phase = 'done'
                self.done = True

        self.gust = max(0.0, self.gust - dt * 2.2)
        self._tick_embers(dt)

    def _ignite(self):
        self.phase = 'kindle'
        self.kindle_t = self.t
        self.next_launch = self.t + FILL_DELAY
        self._play('strike', 0.9)
        self.wake = self.bank.loop_early('wake')
        cx, fy = self._flame_base()
        for _ in range(26):
            self._spawn_ember(cx, fy - 8.0, burst=True)

    #: How far ahead of the truth the dial may drift while a single long
    #: piece of work is running. The score is synthesised in four lumps, and
    #: a dial that sits still for a second and a half looks like a game that
    #: has hung; this creeps it forward instead, never past the next lump.
    CREEP = 0.05

    def _kindle(self, dt, progress):
        t = self.t
        cap = clamp((t - self.kindle_t - FILL_DELAY) / FILL_MIN, 0.0, 1.0)
        if progress > self._last_progress:
            self._last_progress = progress
            self._creep = 0.0
        elif progress < 1.0:
            self._creep = min(self.CREEP, self._creep + dt * 0.035)
        target = min(progress + self._creep, cap)
        if self.shown < target:
            self.shown += (target - self.shown) * min(1.0, dt * 7.0)
            if target - self.shown < 1e-3:
                self.shown = target
        elif target < 1.0:
            # Synthesising the score is two seconds that report nothing while
            # they happen, and a dial that has stopped dead reads as a game
            # that has. It creeps, by less than it could be behind by, and
            # never past the last socket - which only the real thing lights.
            self.shown = min(target + 0.05, 0.985,
                             self.shown + dt * 0.03)
        self._set_volume(self.wake, 0.08 + 0.42 * self.shown)

        due = int(self.shown * FLOORS_PER_RUN + 1e-6)
        started = len(self.lit_at) + len(self.flying)
        if started < due and t >= self.next_launch:
            self.flying.append([started + 1, 0.0])
            self.next_launch = t + STAGGER

        for spark in list(self.flying):
            spark[1] += dt
            if spark[1] >= TRAVEL:
                self.flying.remove(spark)
                self._catch(spark[0])

        # The flame breathes out embers, more of them the brighter it burns.
        self.ember_debt += dt * (5.0 + 16.0 * self.shown + 30.0 * self.gust)
        cx, fy = self._flame_base()
        while self.ember_debt >= 1.0:
            self.ember_debt -= 1.0
            self._spawn_ember(cx, fy - 16.0)

        complete = (len(self.lit_at) == FLOORS_PER_RUN and not self.flying
                    and progress >= 1.0)
        if complete:
            if self.hold_t is None:
                self.hold_t = t
            elif t - self.hold_t >= HOLD:
                self._begin_flare()

    def _catch(self, notch):
        self.lit_at[notch] = self.t
        x, _y = self._notch_pos(notch)
        pan = clamp((x - self.w * 0.5) / R_NOTCH, -1.0, 1.0) * 0.5
        boss = notch in BOSS_FLOORS
        self._play(f'notch{notch}', 0.75 if boss else 0.5, pan)
        nx, ny = self._notch_pos(notch)
        for _ in range(10 if boss else 5):
            self._spawn_ember(nx, ny, burst=True)

    def _begin_flare(self):
        if self.phase != 'kindle':
            return
        self.phase = 'flare'
        self.flare_t = self.t
        # Anything still in flight arrives now, quietly: the flare is louder
        # than any of it would have been.
        for notch, _f in self.flying:
            self.lit_at.setdefault(notch, self.t)
        self.flying = []
        for notch in range(1, FLOORS_PER_RUN + 1):
            self.lit_at.setdefault(notch, self.t)
        self.shown = 1.0
        self._play('flare', 1.0)

    def _impact(self):
        if self.phase != 'flare':
            return
        self.phase = 'after'
        self.impact_t = self.t
        self.handoff = True
        self._fade_out(self.drone, 1400)
        self._fade_out(self.wake, 1800)
        cx, fy = self._flame_base()
        for _ in range(90):
            self._spawn_ember(cx, fy - 14.0, speed=950.0)

    def _tick_embers(self, dt):
        alive = []
        t = self.t
        for e in self.embers:
            e[4] -= dt
            if e[4] <= 0.0:
                continue
            drag = math.exp(-e[7] * dt)
            e[2] = e[2] * drag + math.sin(t * 3.1 + e[6] * 7.0) * 10.0 * dt
            e[3] = e[3] * drag - 16.0 * dt * (1.0 if e[7] < 1.0 else 0.0)
            e[0] += e[2] * dt
            e[1] += e[3] * dt
            alive.append(e)
        self.embers = alive[-260:]

    # ------------------------------------------------------------ draw ---
    def _lantern_radius(self):
        t = self.t
        if self.kindle_t is None:
            return 0.0
        radius = lerp(95.0, 440.0, ease_out_cubic(self.shown))
        radius += 170.0 * math.exp(-(t - self.kindle_t) * 4.0)
        radius *= 1.0 + 0.3 * self.gust
        if self.phase == 'flare':
            u = clamp((t - self.flare_t) / RISER, 0.0, 1.0)
            radius = lerp(radius, math.hypot(self.w, self.h) * 0.75,
                          ease_in_cubic(u))
        flicker = (1.0 + 0.025 * math.sin(t * 23.0)
                   + 0.018 * math.sin(t * 37.3 + 1.1))
        return radius * flicker

    def draw(self):
        if gpu.lighting_ready():
            self._draw_lit()
        else:
            self._draw_flat()
        self._draw_emissive()
        drawImage(art.vignette(self.w, self.h, 0.95, 0.45), 0, 0)
        drawImage(art.grain(self.w, self.h, 0.045), 0, 0)
        self._draw_words()
        if self.phase == 'flare':
            u = clamp((self.t - self.flare_t) / RISER, 0.0, 1.0)
            gpu.set_mode(gpu.ADD)
            drawPolygon(0, 0, self.w, 0, self.w, self.h, 0, self.h,
                        fill=palette.LIGHT_WARM, opacity=int(80 * u ** 4))
            gpu.set_mode(gpu.NORMAL)

    def _draw_lit(self):
        w, h = self.w, self.h
        cx, cy = w * 0.5, h * 0.45
        tile = 256

        if gpu.begin_normal():
            for ty in range(0, int(h) + tile, tile):
                for tx in range(0, int(w) + tile, tile):
                    got = self.tiles.get(((tx // tile) * 3 + (ty // tile) * 5) % 3)
                    if got is not None:
                        drawImage(got[1], tx, ty, width=tile, height=tile)

        gpu.begin_scene(palette.VOID_RGB)
        for ty in range(0, int(h) + tile, tile):
            for tx in range(0, int(w) + tile, tile):
                got = self.tiles.get(((tx // tile) * 3 + (ty // tile) * 5) % 3)
                if got is not None:
                    drawImage(got[0], tx, ty, width=tile, height=tile)
        self._draw_carving(cx, cy)
        gpu.amplify_scene(0.58)

        gpu.begin_light()
        gpu.set_mode(gpu.ADD)
        radius = self._lantern_radius()
        fx, fy = self._flame_base()
        if radius > 1.0:
            art.draw_lantern(palette.LIGHT_WARM, fx, fy - 12.0, radius, 96,
                             height=LIGHT_HEIGHT)
        if self.phase == 'fall':
            sx, sy = self._spark_pos()
            art.draw_glow(palette.LIGHT_WARM, sx, sy, 150.0, 80, power=2.0,
                          height=LIGHT_HEIGHT)
        for notch, when in self.lit_at.items():
            nx, ny = self._notch_pos(notch)
            boss = notch in BOSS_FLOORS
            pop = 1.0 + 1.2 * math.exp(-(self.t - when) * 6.0)
            art.draw_glow(palette.WARD if boss else palette.LIGHT_WARM, nx, ny,
                          (95.0 if boss else 58.0) * pop, 70 if boss else 55,
                          power=2.2, height=LIGHT_HEIGHT)
        for notch, f in self.flying:
            sx, sy = self._flight_pos(notch, f)
            art.draw_glow(palette.LIGHT_WARM, sx, sy, 60.0, 60, power=2.0,
                          height=LIGHT_HEIGHT)
        gpu.set_mode(gpu.NORMAL)
        gpu.add_ambient((5, 6, 9))

        gpu.composite(0.24, 0.32, art.dither_tile())

    def _draw_flat(self):
        """No light buffer to draw through: the same picture, less of it."""
        w, h = self.w, self.h
        drawPolygon(0, 0, w, 0, w, h, 0, h, fill=palette.VOID, opacity=100)
        self._draw_carving(w * 0.5, h * 0.45)
        radius = self._lantern_radius()
        fx, fy = self._flame_base()
        gpu.set_mode(gpu.ADD)
        if radius > 1.0:
            art.draw_glow(palette.LIGHT_WARM, fx, fy - 12.0, radius, 55,
                          power=2.4)
        gpu.set_mode(gpu.NORMAL)

    def _draw_carving(self, cx, cy):
        """The dial, cut into the floor. Only the light shows it."""
        groove = (3, 4, 7)
        lip = (58, 64, 82)
        for r, width in ((64.0, 3.0), (142.0, 5.0), (196.0, 5.0), (252.0, 4.0)):
            _ring(cx, cy, r - width * 0.5, r + width * 0.5, groove, 100)
            _ring(cx, cy, r + width * 0.5, r + width * 0.5 + 1.6, lip, 70)
        # The bowl the flame sits in.
        _ring(cx, cy + 10.0, 14.0, 22.0, (30, 32, 40), 100, segments=40)
        _ring(cx, cy + 10.0, 22.0, 23.5, lip, 80, segments=40)
        spin = self.spin
        for notch in range(1, FLOORS_PER_RUN + 1):
            a = _notch_angle(notch, spin)
            ca, sa = math.cos(a), math.sin(a)
            drawLine(cx + ca * 72.0, cy + sa * 72.0, cx + ca * 134.0,
                     cy + sa * 134.0, fill=groove, lineWidth=1.6, opacity=100)
            nx, ny = cx + ca * R_NOTCH, cy + sa * R_NOTCH
            boss = notch in BOSS_FLOORS
            size = 13.0 if boss else 8.5
            _diamond(nx, ny, size + 2.0, lip, 60)
            _diamond(nx, ny, size, groove, 100)
            if boss:
                _ring(nx, ny, 19.0, 21.5, groove, 100, segments=24)
        # The acts, marked between their last floor and the next one's first.
        for floor in BOSS_FLOORS:
            a = _notch_angle(floor + 0.5, spin)
            ca, sa = math.cos(a), math.sin(a)
            drawLine(cx + ca * 200.0, cy + sa * 200.0, cx + ca * 222.0,
                     cy + sa * 222.0, fill=lip, lineWidth=2.0, opacity=80)
        for a, strokes in self.runes:
            a += spin * 0.35
            ca, sa = math.cos(a), math.sin(a)
            rx, ry = cx + ca * 224.0, cy + sa * 224.0
            for x0, y0, x1, y1 in strokes:
                # Radial: a rune's up is away from the centre.
                drawLine(rx - sa * x0 + ca * y0, ry + ca * x0 + sa * y0,
                         rx - sa * x1 + ca * y1, ry + ca * x1 + sa * y1,
                         fill=lip, lineWidth=1.4, opacity=75)

    def _spark_pos(self):
        u = clamp((self.t - DARK) / FALL, 0.0, 1.0)
        fx, fy = self._flame_base()
        y = lerp(-40.0, fy - 10.0, ease_in_cubic(u))
        x = fx + math.sin(u * 5.0) * 10.0 * (1.0 - u)
        return x, y

    def _flight_pos(self, notch, f):
        u = ease_out_cubic(clamp(f / TRAVEL, 0.0, 1.0))
        a = _notch_angle(notch, self.spin) - (1.0 - u) * 1.3
        r = lerp(18.0, R_NOTCH, u)
        return (self.w * 0.5 + math.cos(a) * r, self.h * 0.45 + math.sin(a) * r)

    def _draw_emissive(self):
        t = self.t
        gpu.set_mode(gpu.ADD)

        if self.phase == 'fall':
            sx, sy = self._spark_pos()
            u = clamp((t - DARK) / FALL, 0.0, 1.0)
            tail = 30.0 + 90.0 * u
            drawLine(sx, sy - tail, sx, sy, fill=palette.LIGHT_WARM,
                     lineWidth=2.2, opacity=45)
            drawLine(sx, sy - tail * 0.4, sx, sy, fill=palette.FLARE,
                     lineWidth=1.4, opacity=80)
            _diamond(sx, sy, 3.5, palette.FLARE, 100)

        # The dial filling: the arc from the top round to the last socket lit.
        count = len(self.lit_at)
        if count:
            cx, cy = self.w * 0.5, self.h * 0.45
            start = _notch_angle(0.5, self.spin)
            sweep = count / FLOORS_PER_RUN * 2.0 * math.pi
            _ring(cx, cy, R_NOTCH - 1.2, R_NOTCH + 1.2, palette.LIGHT_WARM, 30,
                  segments=max(2, count * 4), start=start, sweep=sweep)
        for notch, when in self.lit_at.items():
            nx, ny = self._notch_pos(notch)
            boss = notch in BOSS_FLOORS
            pop = ease_out_back(clamp((t - when) / 0.35, 0.0, 1.0))
            size = (7.0 if boss else 4.2) * pop
            color = palette.WARD if boss else palette.LIGHT_CORE
            _diamond(nx, ny, size + 3.0, color, 35)
            _diamond(nx, ny, size, palette.FLARE if not boss else color, 95)
        for notch, f in self.flying:
            for k in range(5):
                sx, sy = self._flight_pos(notch, max(0.0, f - k * 0.018))
                _diamond(sx, sy, 3.0 - k * 0.45, palette.FLARE if k == 0
                         else palette.LIGHT_WARM, 95 - k * 18)

        if self.kindle_t is not None:
            self._draw_flame()

        for x, y, _vx, _vy, life, total, size, drag in self.embers:
            k = life / total
            fast = drag > 1.0
            color = palette.LIGHT_CORE if k > 0.6 else palette.LIGHT_WARM
            if k < 0.3:
                color = palette.LIGHT_DEEP
            _diamond(x, y, size * (0.6 + 0.4 * k), color,
                     (90 if fast else 80) * min(1.0, k * 1.8))
        gpu.set_mode(gpu.NORMAL)

    def _draw_flame(self):
        t = self.t
        fx, fy = self._flame_base()
        grow = 1.0 + 0.3 * self.shown + 0.55 * self.gust
        grow *= 1.0 + 0.9 * math.exp(-(t - self.kindle_t) * 7.0)
        if self.phase == 'flare':
            grow *= 1.0 + 1.8 * ease_in_cubic((t - self.flare_t) / RISER)
        flicker = 1.0 + 0.07 * math.sin(t * 17.0) + 0.05 * math.sin(t * 29.0 + 2.0)
        height = 40.0 * grow * flicker
        width = 13.0 * (1.0 + 0.4 * (grow - 1.0))
        sway = (3.5 * math.sin(t * 5.1) + 2.0 * math.sin(t * 8.7 + 1.0)
                + self.gust * 9.0 * math.sin(t * 21.0))
        for scale_h, scale_w, color, opacity in (
                (1.0, 1.0, palette.LIGHT_DEEP, 70),
                (0.8, 0.72, palette.LIGHT_WARM, 85),
                (0.52, 0.44, palette.LIGHT_CORE, 95),
                (0.28, 0.24, palette.FLARE, 100)):
            drawPolygon(*_flame_shape(fx, fy, height * scale_h,
                                      width * scale_w, sway * scale_h),
                        fill=color, opacity=opacity)
        # A little blue at the root, where a real flame is hottest.
        _diamond(fx, fy - 2.0, 5.0, palette.WARD, 30, stretch=0.6)

    def _draw_words(self):
        if self.kindle_t is None:
            return
        t = self.t
        w, h = self.w, self.h
        fade = clamp((t - self.kindle_t - 0.4) / 0.8, 0.0, 1.0)
        if self.phase == 'flare':
            fade *= 1.0 - clamp((t - self.flare_t) / 0.25, 0.0, 1.0)
        if fade <= 0.0:
            return
        y = h * 0.45 + 300.0
        drawLabel('WAKING THE VAULT', w * 0.5, y, size=14, bold=True,
                  fill=palette.UI_ACCENT, font=palette.FONT_DISPLAY,
                  opacity=int(70 * fade))
        what = ('synthesising every sound, the first time only'
                if self.first_run else 'tuning the dark')
        drawLabel(f'{what}   {int(round(self.shown * 100)):3d}%', w * 0.5,
                  y + 22, size=11, fill=palette.UI_DIM, font=palette.FONT_UI,
                  opacity=int(62 * fade))
        waited = t - self.kindle_t
        if waited > PATIENCE and self.bank.progress() < 1.0:
            hint = 'click to go on - the rest of the sound will follow'
        elif self.bank.progress() >= 1.0 and waited > 1.2:
            hint = 'click to descend'
        elif waited > 2.0 and not self.poked:
            hint = 'click to stoke the flame'
        else:
            hint = ''
        if hint:
            drawLabel(hint, w * 0.5, h - 26, size=10, fill=palette.UI_FAINT,
                      font=palette.FONT_UI, opacity=int(58 * fade))

    def draw_afterglow(self):
        """The flash and the shockwave, over whatever is on screen now."""
        if self.impact_t is None or self.done:
            return
        ta = self.t - self.impact_t
        w, h = self.w, self.h
        gpu.set_mode(gpu.ADD)
        # Warm rather than white, and gone in half a second: it is the
        # lantern's light arriving, not a camera flash.
        flash = math.exp(-ta * 6.0)
        if flash > 0.01:
            drawPolygon(0, 0, w, 0, w, h, 0, h, fill=palette.LIGHT_CORE,
                        opacity=int(92 * flash))
        cx, cy = self._flame_base()
        reach = math.hypot(w, h)
        for delay, color, thick, strength in ((0.0, palette.LIGHT_WARM, 34.0, 60),
                                              (0.07, palette.WARD, 12.0, 34)):
            u = clamp((ta - delay) / 1.0, 0.0, 1.0)
            if not 0.0 < u < 1.0:
                continue
            r = 40.0 + reach * ease_out_cubic(u)
            band = thick * (1.0 - u) + 2.0
            fade = (1.0 - u) ** 1.5
            # A bright leading edge with a soft wake behind it, built from
            # bands that thin out toward the centre.
            for inner, share in ((0.0, 0.18), (0.35, 0.3), (0.7, 0.55),
                                 (0.9, 1.0)):
                _ring(cx, cy, max(0.0, r - band * (1.0 - inner)),
                      r - band * (1.0 - inner) + band * 0.2 + 1.0, color,
                      int(strength * share * fade), segments=120)
        for x, y, _vx, _vy, life, total, size, drag in self.embers:
            k = life / total
            _diamond(x, y, size * (0.6 + 0.4 * k),
                     palette.LIGHT_CORE if k > 0.5 else palette.LIGHT_WARM,
                     90 * min(1.0, k * 1.8))
        gpu.set_mode(gpu.NORMAL)
