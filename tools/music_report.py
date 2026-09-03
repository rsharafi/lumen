"""What the score is, and whether it does what it claims.

    .venv/bin/python tools/music_report.py

A drone is unusually easy to get subtly wrong in ways nobody notices until
the twentieth minute of a run: a loop that clicks every twenty-six seconds, a
"tension" stem indistinguishable from the bed it is supposed to darken, a
swell that turns out to be a rhythm. Each check here is one of those.
"""

import math
import os
import sys

os.environ.setdefault('SDL_AUDIODRIVER', 'dummy')
os.environ.setdefault('CI', '1')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np                                   # noqa: E402

from lumen import music                              # noqa: E402
from lumen.audio import SAMPLE_RATE                  # noqa: E402

RESULTS = []


def check(name, ok, detail):
    RESULTS.append((name, bool(ok), detail))


def rms(sig):
    return float(np.sqrt(np.mean(np.square(sig, dtype=np.float64))))


def centroid(sig):
    """Spectral centroid in Hz - where the weight of the sound sits."""
    window = sig[:SAMPLE_RATE * 8]
    spec = np.abs(np.fft.rfft(window * np.hanning(len(window))))
    freqs = np.fft.rfftfreq(len(window), 1.0 / SAMPLE_RATE)
    total = spec.sum()
    return float((spec * freqs).sum() / total) if total > 0 else 0.0


def seam(sig):
    """How big a step the loop point makes, against the material's own.

    A drone's samples move very little frame to frame, so a discontinuity at
    the wrap stands out enormously against the interior - which is exactly
    what makes this measurable rather than a matter of taste.
    """
    interior = np.abs(np.diff(sig[SAMPLE_RATE:SAMPLE_RATE * 6]))
    typical = float(np.percentile(interior, 99.9))
    wrap = abs(float(sig[0]) - float(sig[-1]))
    return wrap, typical


def main():
    stems = music.make_stems()

    print(f'{len(stems)} stems, {music.LOOP_SECONDS:.0f}s each\n')
    print(f'{"stem":<18} {"seconds":>8} {"rms":>7} {"centroid":>9} '
          f'{"peak":>6} {"seam":>7}')
    stats = {}
    for name in sorted(stems):
        sig = stems[name]
        wrap, typical = seam(sig)
        stats[name] = {
            'rms': rms(sig), 'centroid': centroid(sig),
            'peak': float(np.max(np.abs(sig))),
            'wrap': wrap, 'typical': typical,
        }
        st = stats[name]
        print(f'{name:<18} {len(sig) / SAMPLE_RATE:>8.1f} {st["rms"]:>7.3f} '
              f'{st["centroid"]:>9.0f} {st["peak"]:>6.2f} '
              f'{st["wrap"]:>7.4f}')
    print()

    # ---- nothing is silent ------------------------------------------------
    quiet = [n for n, st in stats.items() if st['rms'] < 0.01]
    check('every stem makes a sound', not quiet,
          'all audible' if not quiet else f'silent: {", ".join(quiet)}')

    # ---- every loop is seamless ------------------------------------------
    worst = max(stats.items(), key=lambda kv: kv[1]['wrap'] / max(
        kv[1]['typical'], 1e-9))
    ratio = worst[1]['wrap'] / max(worst[1]['typical'], 1e-9)
    check('every loop is seamless', ratio < 4.0,
          f'worst wrap is {ratio:.1f}x the material\'s own step ({worst[0]})')

    # ---- tension is brighter and busier than its bed ----------------------
    ok = True
    notes = []
    for act in (1, 2, 3):
        bed = stats[f'mus_{act}_bed']
        ten = stats[f'mus_{act}_tension']
        brighter = ten['centroid'] > bed['centroid'] * 1.15
        notes.append(f'act {act}: {bed["centroid"]:.0f} -> {ten["centroid"]:.0f} Hz')
        if not brighter:
            ok = False
    check('tension is brighter than its bed', ok, '; '.join(notes))

    # ---- the three acts do not sound like each other ----------------------
    beds = [stats[f'mus_{a}_bed']['centroid'] for a in (1, 2, 3)]
    spread = max(beds) - min(beds)
    check('the acts sound different', spread > 20.0,
          f'bed centroids {beds[0]:.0f} / {beds[1]:.0f} / {beds[2]:.0f} Hz')

    # ---- the boss stem is the heaviest thing here -------------------------
    # Heavier than anything else that plays *during a fight*, which is the
    # comparison that matters - the beds are the quietest material in the
    # game and being darker than those is not a useful claim.
    boss = stats['mus_boss']
    tensions = [stats[f'mus_{a}_tension']['centroid'] for a in (1, 2, 3)]
    check('the boss stem is the heaviest thing in a fight',
          boss['centroid'] < min(tensions),
          f'boss {boss["centroid"]:.0f} Hz against tensions from '
          f'{min(tensions):.0f} Hz up')

    # ---- nothing in it is a rhythm ---------------------------------------
    # A rhythm is *periodic*, which is not the same as uneven - filtered
    # noise moves a great deal block to block and is not a beat. So the level
    # envelope is autocorrelated, and what is looked for is a peak at a lag
    # anywhere a pulse could plausibly live. A drone has none; anything with
    # a beat in it has a tall one.
    beaty = []
    scores = []
    block = SAMPLE_RATE // 20                       # 50 ms
    for name, sig in stems.items():
        n = len(sig) // block
        env = np.abs(sig[:n * block].reshape(n, block)).mean(axis=1)
        # Detrended before it is correlated. These stems breathe on purpose,
        # over tens of seconds, and a slowly drifting envelope autocorrelates
        # highly at *every* short lag - which is what made the first version
        # of this check report a 1.00 s beat in three stems that have none.
        # Subtracting a three-second moving average leaves only structure
        # fast enough to be a pulse.
        span = int(3.0 / 0.05)
        pad = np.pad(env, (span // 2, span // 2), mode='edge')
        trend = np.convolve(pad, np.ones(span) / span, mode='valid')[:len(env)]
        env = env - trend
        if float(np.dot(env, env)) < 1e-9:
            continue
        corr = np.correlate(env, env, mode='full')[len(env) - 1:]
        corr = corr / corr[0]
        lo = int(0.2 / 0.05)                        # 200 ms
        hi = int(2.5 / 0.05)                        # 2.5 s
        peak = float(np.max(corr[lo:hi]))
        lag = (int(np.argmax(corr[lo:hi])) + lo) * 0.05
        # How *deep* the modulation is, not just how regular. A sustained
        # tone made of detuned voices always beats - that is what makes it
        # sound bowed rather than generated, and `_cluster` exists to do it -
        # so periodicity on its own is not a fault. What would be a fault is
        # beating deep enough to read as a pulse rather than as texture, and
        # that is a ratio: how much the level moves against how loud it is.
        base = float(np.abs(sig[:n * block].reshape(n, block)).mean())
        depth = float(np.sqrt(np.mean(env ** 2))) / max(base, 1e-9)
        # Depth alone is not the answer either: these stems all carry a
        # layer of filtered noise, whose envelope fluctuates by 20% or more
        # simply because it is noise, and noise is not a pulse. What makes a
        # pulse is being deep *and* regular, so the two are multiplied. Band
        # -limited noise scores near zero however much it moves; a tremolo
        # scores high.
        strength = depth * max(peak, 0.0)
        scores.append((strength, f'{name.replace("mus_", "")} '
                                 f'{strength * 100:.0f}% ({depth * 100:.0f}% '
                                 f'deep x {peak:.2f} regular) @{lag:.2f}s'))
        # Twenty per cent. Below that the movement reads as a sustained
        # thing breathing, which is the whole point of the material; above it
        # you can count along, which is the thing this game must never do to
        # a player half an hour into a run.
        if strength > 0.20:
            beaty.append(name)
    scores.sort(reverse=True)
    check('no beating deep enough to be a pulse', not beaty,
          'strongest: ' + '; '.join(t for _d, t in scores[:2])
          if scores else 'no material')

    # ---- the director asks for the right things --------------------------
    director = music.Director()
    director.set_scene(act=2, boss=False, intensity=0.0)
    calm = director.target()
    director.set_scene(intensity=1.0)
    loud = director.target()
    director.set_scene(boss=True)
    fight = director.target()
    ok = (calm['mus_2_bed'] == 1.0 and calm['mus_2_tension'] == 0.0
          and loud['mus_2_tension'] == 1.0
          and fight['mus_boss'] == 1.0 and fight['mus_2_tension'] == 0.0
          and 0.0 < fight['mus_2_bed'] < 0.5)
    check('the director mixes what it should', ok,
          f'calm bed {calm["mus_2_bed"]:.2f}, loud tension '
          f'{loud["mus_2_tension"]:.2f}, boss {fight["mus_boss"]:.2f} '
          f'over bed {fight["mus_2_bed"]:.2f}')

    # ---- and the act's own stems are the only ones up --------------------
    director.set_scene(act=3, boss=False, intensity=0.5)
    mix = director.target()
    stray = [n for n, v in mix.items() if v > 0.0 and not n.startswith('mus_3')]
    check('only one act is ever audible', not stray,
          'act 3 alone' if not stray else f'also up: {", ".join(stray)}')

    print()
    width = max(len(n) for n, _o, _d in RESULTS)
    bad = sum(1 for _n, ok, _d in RESULTS if not ok)
    for name, ok, detail in RESULTS:
        print(f'  {" ok " if ok else "FAIL"}  {name:<{width}}  {detail}')
    print()
    print(f'{len(RESULTS) - bad} of {len(RESULTS)} checks passed')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
