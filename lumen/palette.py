"""The colour language of the vault.

Every colour is built once at import time. `rgb()` allocates a small object on
each call, and in a 60 Hz redraw loop that runs hundreds of times per frame, so
the whole game draws from these cached constants instead.

The palette is deliberately narrow: a cold blue-black world, a warm amber
lantern, and a small number of saturated accents that only ever appear on
things that matter (damage, pickups, threats).
"""

class rgb:
    """A colour, as the drawing calls want it.

    This came from cmu-graphics, which is no longer a dependency. It is kept
    as a type rather than replaced with a plain tuple because roughly a dozen
    call sites read `.red`/`.green`/`.blue`, and because `art.rgb_tuple` uses
    the distinction between this and a bare tuple to tell a palette colour
    from an already-unpacked one.

    Slotted and built once at import: the game draws hundreds of shapes a
    frame and none of them should be allocating a colour.
    """

    __slots__ = ('red', 'green', 'blue')

    def __init__(self, red, green, blue):
        self.red = int(red)
        self.green = int(green)
        self.blue = int(blue)

    def __iter__(self):
        yield self.red
        yield self.green
        yield self.blue

    def __eq__(self, other):
        return (isinstance(other, rgb) and self.red == other.red
                and self.green == other.green and self.blue == other.blue)

    def __hash__(self):
        return hash((self.red, self.green, self.blue))

    def __repr__(self):
        return f'rgb({self.red}, {self.green}, {self.blue})'



def _c(h):
    """Build a cached colour from a 24-bit hex literal."""
    return rgb((h >> 16) & 255, (h >> 8) & 255, h & 255)


def ramp(a, b, steps):
    """A list of `steps` colours interpolating from colour `a` to colour `b`."""
    out = []
    for i in range(steps):
        t = i / max(1, steps - 1)
        out.append(rgb(
            int(a[0] + (b[0] - a[0]) * t),
            int(a[1] + (b[1] - a[1]) * t),
            int(a[2] + (b[2] - a[2]) * t),
        ))
    return out


# -- the dark ---------------------------------------------------------------
VOID = _c(0x05070C)
VOID_RGB = (0x05, 0x07, 0x0C)
FLOOR_DEEP = _c(0x0B0F18)
# Cast shadows are filled with this at full opacity rather than washed over
# the glow: a translucent wash only removes its own percentage of the light,
# which let roughly half the lantern bleed straight through walls. Tuned to sit
# a little under the unlit floor so shadows read as shadow, not as a hole.
SHADOW = _c(0x0A0D15)
FLOOR_MID = _c(0x121826)
FLOOR_HIGH = _c(0x1B2333)
WALL_FACE = _c(0x141A28)
WALL_TOP = _c(0x232C41)
WALL_EDGE = _c(0x39496B)
WALL_LIT = _c(0x6E7FA8)
# The doors. Worked metal rather than the masonry around them, so a leaf
# reads as a made thing set into a wall and not as more wall - it is a shade
# warmer and a shade lighter than WALL_TOP, which is the smallest difference
# that still separates them at a glance in an unlit room.
WALL_DOOR = _c(0x2E3346)
WALL_DOOR_LIT = _c(0x7C87A6)
WALL_DEEP = _c(0x0E121C)

# -- the lantern ------------------------------------------------------------
LIGHT_CORE = _c(0xFFE9BC)
LIGHT_WARM = _c(0xFFC46B)
LIGHT_DEEP = _c(0xC97A2E)
FLARE = _c(0xFFF4D6)

# The vault's own light, as against the lantern's. Everything cold in this
# game belongs to the building - the way down, and the wards it draws across
# a doorway while something in the room is still alive. Warm is yours and
# means safety; cold is the vault's and means it has decided something.
WARD = _c(0x96D2FF)

# -- the player -------------------------------------------------------------
PLAYER_BODY = _c(0xE8EEF7)
PLAYER_CLOAK = _c(0x2C3E63)
PLAYER_TRIM = _c(0x8FD3FF)
DASH_TRAIL = _c(0x6FB6FF)

# -- threats ----------------------------------------------------------------
CRAWLER = _c(0x7A2540)
CRAWLER_EYE = _c(0xFF4D6D)
HUSK = _c(0x3B3247)
HUSK_EYE = _c(0xC77DFF)
SPITTER = _c(0x1F5245)
SPITTER_EYE = _c(0x4DFFB8)
WISP = _c(0x2A3A6B)
WISP_EYE = _c(0x8AB4FF)
WARDEN = _c(0x5A3A18)
WARDEN_EYE = _c(0xFFB347)
WARDEN_SHIELD = _c(0xFFD98A)
# Dark enough to feel like part of the vault, light enough to read as a
# silhouette against it - the original 0x24102C vanished into the floor.
BOSS = _c(0x4C2456)
BOSS_EYE = _c(0xFF3D7F)
BOSS_FLASH = _c(0x9C5AA8)

# The second bestiary. Each pair is a body dark enough to be part of the
# vault and an eye bright enough to be the only thing you see of it at range -
# the eye-glint is how every one of these is first met, so no two may share a
# hue the eye cannot separate at four pixels.
LURKER = _c(0x231C36)
LURKER_EYE = _c(0xD9C2FF)
PALE = _c(0x6E6A58)
PALE_EYE = _c(0xFFF2B8)
DOUSER = _c(0x102A31)
DOUSER_EYE = _c(0x2FE0D6)
BOLTER = _c(0x5C1E1E)
BOLTER_EYE = _c(0xFF8A3D)
KEENER = _c(0x2B4A2E)
KEENER_EYE = _c(0x9CFF6B)
SPLITTER = _c(0x4A2A55)
SPLITTER_EYE = _c(0xE07BFF)
CINDER = _c(0x5A3410)
CINDER_EYE = _c(0xFFC24D)
MIRROR = _c(0x37424F)
MIRROR_EYE = _c(0xDCEBFF)
DELVER = _c(0x3A2C1B)
DELVER_EYE = _c(0xE8B36B)
CARRION = _c(0x2E3B22)
CARRION_EYE = _c(0xC3E86B)
# What a carrion leaves behind, and what a keener's tether is drawn in.
CARRION_POOL = _c(0x7FA83A)
KEENER_LINK = _c(0x7FE05A)

# The Keeper, at the bottom. Warm and pale rather than hostile-coloured:
# it is not a monster's palette, it is a *lantern's* - which is the whole
# unpleasantness of the thing. Its light looks exactly like safety.
KEEPER = _c(0x4A4030)
KEEPER_EYE = _c(0xFFE9A8)

EYE_GLINT = _c(0xFF6B8A)

# -- ordnance ---------------------------------------------------------------
BOLT = _c(0xBFE9FF)
BOLT_HOT = _c(0xFFFFFF)
SCATTER = _c(0xFFD79A)
BEAM = _c(0xC8A6FF)
ENEMY_BOLT = _c(0xFF7A5C)
ENEMY_BOLT_HOT = _c(0xFF9463)
# Hostile fire must never be mistakable for your own at a glance: yours reads
# as a pale cyan lance, theirs as a warm orb, the boss's as hot magenta.
BOSS_BOLT = _c(0xFF5C9E)
BOSS_BOLT_GLOW = (255, 70, 140)

# -- feedback ---------------------------------------------------------------
DAMAGE = _c(0xFF5E7A)
HEAL = _c(0x67F5B4)
XP = _c(0xFFD65C)
CRIT = _c(0xFFE066)
SHIELD = _c(0x7FD4FF)

# -- interface --------------------------------------------------------------
UI_TEXT = _c(0xD8E2F2)
UI_DIM = _c(0x6E7C96)
UI_FAINT = _c(0x3B455C)
UI_ACCENT = _c(0xFFC46B)
UI_PANEL = _c(0x0A0E17)
UI_LINE = _c(0x27324A)
UI_DANGER = _c(0xFF5E7A)
UI_GOOD = _c(0x67F5B4)

HP_FULL = _c(0x67F5B4)
HP_MID = _c(0xFFD65C)
HP_LOW = _c(0xFF5E7A)

# Health bar ramp, sampled by fraction remaining.
HP_RAMP = ramp((0xFF, 0x4D, 0x5E), (0x67, 0xF5, 0xB4), 24)


def gradient(stops, steps):
    """A ramp through several colour stops rather than just two."""
    out = []
    spans = len(stops) - 1
    for i in range(steps):
        t = i / max(1, steps - 1) * spans
        k = min(spans - 1, int(t))
        f = t - k
        a, b = stops[k], stops[k + 1]
        out.append(rgb(int(a[0] + (b[0] - a[0]) * f),
                       int(a[1] + (b[1] - a[1]) * f),
                       int(a[2] + (b[2] - a[2]) * f)))
    return out


# What the lantern does to masonry. A lit edge runs from a dull ember at the
# very reach of the light, through the lantern's own amber, to near-white
# where the wall is close and square-on to it - so the *colour* carries the
# falloff, not just the opacity. Indexed by an edge's 0..1 strength.
WALL_LIGHT = gradient(((0x4A, 0x25, 0x14),
                       (0x8E, 0x4B, 0x1E),
                       (0xC9, 0x7A, 0x2E),
                       (0xFF, 0xC4, 0x6B),
                       (0xFF, 0xE9, 0xBC)), 40)


def wall_light(strength):
    """The colour a wall edge takes at this strength."""
    if strength <= 0.0:
        return WALL_LIGHT[0]
    if strength >= 1.0:
        return WALL_LIGHT[-1]
    return WALL_LIGHT[int(strength * (len(WALL_LIGHT) - 1))]

FONT_DISPLAY = 'Copperplate'
FONT_UI = 'Menlo'
