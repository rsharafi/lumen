"""Describe every synthesised effect numerically.

Sound is the one part of this game that cannot be checked by looking at it,
so the qualities that were wrong - thin, buzzy, dry - are measured instead.

  centroid   where the energy sits, in Hz. High is bright and thin.
  odd/even   ratio of odd to even harmonic energy. A square or saw wave is
             almost all odd harmonics, which is the chiptune signature.
  tail       seconds from the peak until the signal falls 40 dB below it.
             A dry sound stops; one in a stone room keeps ringing.

It also checks that every name a call site asks for is actually in the bank.
`SoundBank.play` looks a name up, finds nothing and returns, so a typo or a
sound that was renamed is not an error - it is silence, in exactly the place
someone meant to put a sound. Two call sites had been asking for `low` for
some time: the moment a boss dies, and the Snuffer's signature attack.
"""

import os
import re
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read(path):
    with wave.open(path, 'rb') as f:
        n = f.getnframes()
        raw = np.frombuffer(f.readframes(n), dtype='<i2')
    return raw.reshape(-1, 2)[:, 0].astype(np.float32) / 32768.0


def describe(sig, rate=44100):
    if sig.size < 64:
        return None
    spec = np.abs(np.fft.rfft(sig * np.hanning(sig.size)))
    freq = np.fft.rfftfreq(sig.size, 1.0 / rate)
    power = spec ** 2
    total = power.sum() + 1e-12
    centroid = float((freq * power).sum() / total)
    high = float(power[freq > 4000].sum() / total)

    # Harmonic character, measured against the strongest partial.
    peak = int(np.argmax(power[1:])) + 1
    f0 = freq[peak]
    odd = even = 0.0
    if f0 > 20:
        for k in range(1, 13):
            band = (freq > f0 * k * 0.94) & (freq < f0 * k * 1.06)
            e = float(power[band].sum())
            if k % 2 == 1:
                odd += e
            else:
                even += e
    ratio = odd / max(even, 1e-9)

    env = np.abs(sig)
    win = max(1, rate // 200)
    env = np.convolve(env, np.ones(win) / win, mode='same')
    top = int(np.argmax(env))
    floor_ = env[top] * 10 ** (-40 / 20.0)
    below = np.nonzero(env[top:] < floor_)[0]
    tail = float(below[0] / rate) if below.size else float((env.size - top) / rate)
    return dict(centroid=centroid, high=high, odd_even=ratio, tail=tail,
                length=sig.size / rate)


# `audio.play('x')`, `audio.play_at('x', ...)`, and the f-string form the
# bosses use to pick their half of the bank: `audio.play(f'{b.voice}_eye')`.
_CALL = re.compile(r"""audio\.play(?:_at)?\(\s*"""
                   r"""(?:f?['"]([a-z_]+)['"]|f['"]\{[^}]*\}([a-z_]+)['"])""")
_VOICES = ('choir', 'snuff', 'keep')


def coverage(root, names):
    """Every name played, and whether the bank has it."""
    asked = {}
    for here, _, files in os.walk(os.path.join(root, 'lumen')):
        for fn in sorted(files):
            if not fn.endswith('.py'):
                continue
            path = os.path.join(here, fn)
            for i, line in enumerate(open(path), 1):
                for literal, suffix in _CALL.findall(line):
                    for name in ([literal] if literal else
                                 [v + suffix for v in _VOICES]):
                        asked.setdefault(name, f'{os.path.relpath(path, root)}:{i}')
    return asked


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cache = os.path.join(root, '.sound_cache')
    names = sorted(f[:-4] for f in os.listdir(cache) if f.endswith('.wav'))
    print(f'{"effect":16s} {"len":>5s} {"centroid":>9s} {">4kHz":>6s} '
          f'{"odd/even":>9s} {"tail":>6s}')
    agg = []
    for name in names:
        d = describe(read(os.path.join(cache, name + '.wav')))
        if d is None:
            continue
        agg.append(d)
        print(f'{name:16s} {d["length"]:5.2f} {d["centroid"]:9.0f} '
              f'{d["high"] * 100:5.1f}% {d["odd_even"]:9.2f} {d["tail"]:6.3f}')
    if agg:
        print(f'\n{"MEAN":16s} {"":5s} '
              f'{np.mean([d["centroid"] for d in agg]):9.0f} '
              f'{np.mean([d["high"] for d in agg]) * 100:5.1f}% '
              f'{np.mean([d["odd_even"] for d in agg]):9.2f} '
              f'{np.mean([d["tail"] for d in agg]):6.3f}')

    asked = coverage(root, names)
    missing = sorted((n, where) for n, where in asked.items() if n not in names)
    print()
    for name, where in missing:
        print(f'SILENT  {where}  plays {name!r}, which is not in the bank')
    # Names reached indirectly - `weapon.sound`, or a conditional expression -
    # cannot be found by reading the source, so an unplayed name is a hint
    # rather than a finding.
    # The score is reached by a name the director builds, so no call site
    # spells any of it out. Counted separately rather than listed as
    # suspicious: seven stems in the "might be dead" column is seven things
    # to re-check by hand every time this runs.
    stems = sorted(n for n in names if n.startswith('mus_'))
    unplayed = sorted(set(names) - set(asked) - set(stems))
    print(f'{len(names)} in the bank ({len(stems)} of them the score), '
          f'{len(asked)} named by a call site, {len(missing)} silent')
    if unplayed:
        print(f'not named directly (may be played indirectly): '
              f'{", ".join(unplayed)}')
    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main())
