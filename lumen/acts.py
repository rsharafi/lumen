"""What each third of the vault is, and how it differs from the others.

Twenty floors used to be one floor twenty times. `act_of` existed and decided
exactly one thing - how many rooms a floor had - so floor three and floor
eighteen were the same stone, the same palette, the same layouts and the same
empty air, with bigger numbers in them. A descent that never changes is a
corridor.

An act is defined here by four things, in rough order of how much they
matter:

* **a hazard**, which is the only one that changes how the floor is *played*
* **a grade**, which is what makes it a different place at a glance
* **the layouts it prefers**, which changes what fighting in it is like
* **how much fire it leaves lying around**, which is the fuel budget

## Why a grade rather than new tiles

Every texture in this game is generated, and generating three sets of them
would be three times the bake for three times the memory. A grade is one pass
over the finished image, so it costs almost nothing and - more usefully - it
catches *everything* consistently: tiles, grout, cracks, wall rims, the
scorch decals, all of it shifts together. Hand-tinting the twenty-odd
hardcoded colours in `level._bake_layers` would have missed some, and the
ones it missed would be the ones that looked wrong.

The grades are deliberately restrained. This is a game about a small warm
light in a large cold dark, and an act that turns the stone green stops being
that game. What changes is the *cast* of the dark and how much contrast the
stone holds, not its hue outright.
"""

import numpy as np

from . import palette

UNDERCROFT = 1
CISTERNS = 2
SEALED = 3

#: Hazard kinds. `None` is a real answer: act one teaches, and teaching floors
#: should not also be arguing with the player about the ground.
NONE = None
WATER = 'water'
VENTS = 'vents'


class Act:
    """One third of the descent."""

    __slots__ = ('index', 'name', 'subtitle', 'grade', 'archetypes',
                 'braziers', 'hazard', 'hazard_density', 'mote_color',
                 'floor_darken')

    def __init__(self, index, name, subtitle, grade, archetypes, braziers,
                 hazard, hazard_density, mote_color, floor_darken=1.0):
        self.index = index
        self.name = name
        self.subtitle = subtitle
        self.grade = grade
        self.archetypes = archetypes
        self.braziers = braziers
        self.hazard = hazard
        self.hazard_density = hazard_density
        self.mote_color = mote_color
        self.floor_darken = floor_darken


#: (gain, lift, saturation). Gain multiplies per channel, lift is added after,
#: saturation pulls toward or away from the pixel's own luminance. Applied in
#: that order, in float, once per baked layer.
_NEUTRAL = ((1.0, 1.0, 1.0), (0.0, 0.0, 0.0), 1.0)

ACTS = {
    UNDERCROFT: Act(
        UNDERCROFT, 'THE UNDERCROFT', 'dry stone, and something in it',
        # The baseline the whole game was authored against. Left alone on
        # purpose: an act that grades the reference is an act that makes the
        # other two look like corrections of it rather than departures.
        _NEUTRAL,
        # Layouts you can read at a glance. The first act is where the player
        # learns what a pillar is for.
        (('pillars', 3.0), ('cross', 2.2), ('rings', 1.4), ('shards', 0.8),
         ('gauntlet', 0.6), ('spiral', 0.6)),
        (2, 4), NONE, 0.0, (150, 170, 210)),

    CISTERNS: Act(
        CISTERNS, 'THE CISTERNS', 'it has been flooded a long time',
        # Cooler and harder. Blue holds, red is pulled down, and the lift on
        # blue keeps the darks from going to pure black - standing water
        # never quite lets a room be completely dark, and that is most of
        # what makes this act read as wet.
        ((0.86, 1.0, 1.14), (0.0, 2.0, 7.0), 0.88),
        # Open shapes and long sightlines. A spitter is a different problem
        # in a ring than it is behind a pillar.
        (('rings', 2.8), ('spiral', 2.4), ('cross', 1.8), ('pillars', 1.4),
         ('gauntlet', 1.2), ('shards', 0.9)),
        (1, 3), WATER, 0.30, (130, 190, 205)),

    SEALED: Act(
        SEALED, 'THE SEALED VAULT', 'the dark here is kept, not left',
        # Warm, ashen, and darker overall. Everything the light touches down
        # here looks scorched, and everything it does not is further away
        # than it was - the floor is knocked down so the lantern's pool is a
        # smaller island in it.
        ((1.12, 0.94, 0.80), (4.0, 1.0, 0.0), 0.82),
        # Tight and awkward. Shards and gauntlets give the least room to back
        # into, which is what the last act should be short of.
        (('shards', 2.8), ('gauntlet', 2.4), ('pillars', 1.6), ('cross', 1.2),
         ('spiral', 1.0), ('rings', 0.8)),
        (0, 2), VENTS, 0.22, (225, 165, 120), floor_darken=0.86),
}


def of(depth):
    """The act a floor belongs to."""
    from .floorplan import act_of
    return ACTS[act_of(depth)]


def grade_array(pixels, grade, darken=1.0):
    """Apply (gain, lift, saturation) to a float RGB array, in place-ish.

    Order matters and is the usual one: gain scales, lift offsets, saturation
    pulls toward luminance last so it acts on the graded colour rather than
    the original. `darken` is a final flat multiply, which is separate from
    gain because it is a statement about how much light the act has in it
    rather than about its colour.
    """
    gain, lift, sat = grade
    # Colour only. A baked layer may carry the shape of the masonry in a
    # fourth channel, and grading that eats the edge of every block.
    alpha = pixels[..., 3:] if pixels.shape[-1] == 4 else None
    out = pixels[..., :3] * np.asarray(gain, dtype=np.float32)
    out += np.asarray(lift, dtype=np.float32)
    if abs(sat - 1.0) > 1e-3:
        # Rec. 709 luminance: green carries most of the perceived brightness,
        # so an even average would lighten anything green as it desaturates.
        lum = (out[..., 0] * 0.2126 + out[..., 1] * 0.7152
               + out[..., 2] * 0.0722)[..., None]
        out = lum + (out - lum) * sat
    if abs(darken - 1.0) > 1e-3:
        out *= darken
    out = np.clip(out, 0.0, 255.0)
    if alpha is not None:
        out = np.concatenate([out, alpha], axis=-1)
    return out


def grade_image(image, act):
    """Grade one baked PIL layer for `act`, alpha untouched."""
    from PIL import Image
    if act.grade is _NEUTRAL and abs(act.floor_darken - 1.0) < 1e-3:
        return image
    arr = np.asarray(image, dtype=np.float32)
    out = grade_array(arr, act.grade, act.floor_darken)
    return Image.fromarray(out.astype(np.uint8), image.mode)


def grade_color(color, act):
    """Grade a single palette colour the same way the stone is graded.

    So a thing drawn on top of the floor - a decal, a mote, a brazier's
    bowl - sits in the same light as the floor does, rather than looking
    like it was pasted in from the act next door.
    """
    rgb = (color.red, color.green, color.blue) if hasattr(color, 'red') \
        else tuple(color)
    arr = np.asarray([[rgb]], dtype=np.float32)
    out = grade_array(arr, act.grade, act.floor_darken)[0][0][:3]
    return palette.rgb(int(out[0]), int(out[1]), int(out[2]))
