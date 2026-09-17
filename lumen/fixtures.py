"""The things standing in a room that is not a fight.

A cache, a hearth and a shrine are rooms the player chose to walk to, and a
room that pays out the instant you cross its threshold is not a place, it is
a corridor with a number attached. Each of them holds a **fixture** instead:
one object, in the middle, that has to be reached and touched.

That distinction is most of what makes the branch worth taking. Walking into
a spur costs light and time; the payment happening at the far end of the room
rather than at its door means the room itself is part of the price.

The shrine is the one that matters most, because it is the only one that
takes something. Its terms are written on it, in the open, before you touch
it - a bargain you cannot read is not a bargain, it is a trap, and this game
already has quite enough that you cannot see.
"""

import math

from . import art, palette
from .mathx import clamp, ease_out_cubic, pulse

CACHE = 'cache'
HEARTH = 'hearth'
SHRINE = 'shrine'
FERRYMAN = 'ferryman'

#: How close the player has to be for a fixture to answer.
TOUCH = 46.0


class Fixture:
    """One interactable object standing in the middle of a room."""

    __slots__ = ('kind', 'x', 'y', 'taken', 'armed', 't', 'take_t', 'label',
                 'terms', 'payload', 'shape', 'edges')

    def __init__(self, kind, x, y, label='', terms='', payload=None):
        self.kind = kind
        self.x = x
        self.y = y
        self.taken = False
        #: A fixture will not answer until the player has been clear of it
        #: once. Rooms are small and their middle is close to everything, so
        #: without this a player who happens to arrive standing on a shrine
        #: strikes its bargain before they have read it.
        self.armed = False
        #: Seconds since the room was entered, for the idle animation.
        self.t = 0.0
        #: Seconds since it was taken, for the one that plays afterwards.
        self.take_t = 0.0
        self.label = label
        self.terms = terms
        self.payload = payload
        #: Cast once, like every other static light in the game.
        self.shape = None
        self.edges = None

    @property
    def color(self):
        if self.kind == HEARTH:
            return palette.LIGHT_WARM
        if self.kind == SHRINE:
            return palette.WARD
        if self.kind == FERRYMAN:
            return palette.UI_ACCENT
        return palette.XP

    def reach(self, x, y):
        return (x - self.x) ** 2 + (y - self.y) ** 2 <= TOUCH * TOUCH

    def near(self, x, y, radius=210.0):
        return (x - self.x) ** 2 + (y - self.y) ** 2 <= radius * radius

    #: How far above the fixture the name sits, and how far below the name
    #: the terms sit. Flipped as a pair when there is masonry overhead.
    LABEL_RISE = 52.0
    LABEL_GAP = 18.0
    #: Half the width the two lines need clear of stone, in world units. The
    #: names are short and the terms are one phrase, so this is generous
    #: rather than measured - the point is to notice a wall, not to fit.
    LABEL_HALF_W = 76.0

    def label_offset(self, level):
        """How far above the fixture its name should sit, in world units.

        Negative is above, which is where it belongs: a name over the thing
        it names reads as a caption, and the fixture's own light is under it.

        A chamber does not always have room for that. A fixture stood against
        the top wall - which happens whenever the middle of the room is
        blocked and the placement falls back to a spawn point - puts its name
        on the masonry, where a dark caption on dark stone is not a caption,
        it is a smudge. So the name goes *under* the fixture instead when
        there is no clear floor above it, which every chamber has somewhere,
        because a fixture the player can walk to is a fixture with floor
        around it.
        """
        rise = self.LABEL_RISE
        if level is None:
            return -rise
        half = self.LABEL_HALF_W
        top = self.y - rise - self.LABEL_GAP * 0.5
        # Both lines, and the width they take, not a point: a name clears a
        # pillar's left edge and still crosses it.
        for x in (self.x - half, self.x, self.x + half):
            if not level.is_open_at(x, top, 6.0):
                return rise * 0.86
        return -rise

    def draw_terms(self, ox, oy, px, py, level=None, view=None):
        """Write what this thing is, and what it will cost, beside it.

        Only a shrine has terms, and a shrine whose terms you read *after*
        touching it is not a bargain - it is a trap, and this game already
        asks the player to accept quite enough that they cannot see. Shown
        from across the room, and only while there is still a choice.

        Drawn after the lighting rather than into it - see
        `World._draw_fixture_terms`. A caption is something the game is
        telling you, not a thing standing in the room, and the version that
        went through the light buffer was multiplied down to nothing in
        exactly the places it was needed most.
        """
        from .draw import drawLabel
        if self.taken or not self.near(px, py):
            return
        d = math.hypot(px - self.x, py - self.y)
        fade = clamp((210.0 - d) / 90.0, 0.0, 1.0)
        if fade <= 0.02:
            return
        sx, sy = self.x - ox, self.y - oy
        # The name leads and the terms sit under it, above or below - so the
        # reading order is the same whichever way round the pair went.
        rise = self.label_offset(level)
        name_y = sy + rise
        terms_y = name_y + self.LABEL_GAP
        if view is not None:
            # A fixture near the edge of the window - which a wide chamber
            # puts there often enough - should not write off the screen.
            margin = 22.0
            lo, hi = margin, view[1] - margin
            shift = 0.0
            lowest = max(name_y, terms_y if self.terms else name_y)
            if name_y < lo:
                shift = lo - name_y
            elif lowest > hi:
                shift = hi - lowest
            name_y += shift
            terms_y += shift
        drawLabel(self.label, sx, name_y, size=15, bold=True,
                  fill=palette.UI_TEXT, font=palette.FONT_DISPLAY,
                  opacity=int(92 * fade))
        if self.terms:
            drawLabel(self.terms, sx, terms_y, size=11,
                      fill=palette.UI_DIM, font=palette.FONT_UI,
                      opacity=int(84 * fade))

    def update(self, dt):
        self.t += dt
        if self.taken:
            self.take_t += dt

    # ------------------------------------------------------------- drawing --
    def draw(self, ox, oy, run_time):
        sx, sy = self.x - ox, self.y - oy
        if self.taken and self.take_t > 1.4:
            self._draw_spent(sx, sy)
            return
        k = 1.0 if not self.taken else 1.0 - ease_out_cubic(
            clamp(self.take_t / 1.4, 0.0, 1.0))
        rgb = art.rgb_tuple(self.color)
        breathe = 0.86 + 0.14 * pulse(run_time, 1.4)

        art.draw_glow(rgb, sx, sy, 74 * k * breathe, int(40 * k), power=2.1)
        if self.kind == CACHE:
            self._draw_cache(sx, sy, k, run_time)
        elif self.kind == HEARTH:
            self._draw_hearth(sx, sy, k, run_time)
        elif self.kind == FERRYMAN:
            self._draw_ferryman(sx, sy, k, run_time)
        else:
            self._draw_shrine(sx, sy, k, run_time)

    def _draw_spent(self, sx, sy):
        """What is left once it has been taken. Deliberately dim: a room you
        have emptied should read as emptied from the doorway."""
        from .draw import drawPolygon
        r = 11.0
        drawPolygon(sx - r, sy - r * 0.4, sx + r, sy - r * 0.4,
                    sx + r * 0.7, sy + r * 0.5, sx - r * 0.7, sy + r * 0.5,
                    fill=palette.UI_FAINT, opacity=52)

    def _draw_cache(self, sx, sy, k, run_time):
        """A heap of embers, banked up rather than scattered."""
        from .draw import drawPolygon
        for i in range(7):
            a = i * math.tau / 7 + run_time * 0.4
            d = 9.0 + 5.0 * math.sin(run_time * 1.6 + i)
            px = sx + math.cos(a) * d
            py = sy + math.sin(a) * d * 0.6
            s = (3.4 + 1.4 * math.sin(run_time * 2.2 + i * 1.7)) * k
            drawPolygon(px, py - s, px + s, py, px, py + s, px - s, py,
                        fill=palette.XP, opacity=int(88 * k))
        drawPolygon(sx, sy - 7 * k, sx + 6 * k, sy, sx, sy + 7 * k,
                    sx - 6 * k, sy, fill=palette.LIGHT_CORE,
                    opacity=int(70 * k))

    def _draw_hearth(self, sx, sy, k, run_time):
        """A standing fire. Bigger than a brazier, and already lit."""
        from .draw import drawPolygon
        for ring in range(3):
            r = (16 - ring * 4) * k
            wob = 1.0 + 0.14 * math.sin(run_time * 6.0 + ring * 2.1)
            pts = []
            for i in range(6):
                a = i * math.tau / 6 + run_time * (0.5 + ring * 0.3)
                rr = r * wob * (1.0 + 0.16 * math.sin(run_time * 4.0 + i))
                pts.append(sx + math.cos(a) * rr)
                pts.append(sy + math.sin(a) * rr * 0.9)
            drawPolygon(*pts,
                        fill=(palette.LIGHT_CORE if ring == 2
                              else palette.LIGHT_WARM if ring == 1
                              else palette.LIGHT_DEEP),
                        opacity=int((46 + ring * 18) * k))

    def _draw_ferryman(self, sx, sy, k, run_time):
        """A hooded figure holding a lantern out.

        He is the only thing in the vault carrying a light that is not
        yours - which is exactly why he is worth walking to, and why the
        lantern is drawn a little in front of him rather than at his centre.
        You see the light first and the figure second, the same way you would
        in the dark.
        """
        from .draw import drawPolygon
        h = 21.0 * k
        w = 10.0 * k
        sway = math.sin(run_time * 1.1) * 1.6 * k
        # The cloak.
        drawPolygon(sx - w + sway * 0.3, sy + h * 0.6,
                    sx - w * 0.45 + sway, sy - h,
                    sx + w * 0.45 + sway, sy - h,
                    sx + w + sway * 0.3, sy + h * 0.6,
                    fill=palette.PLAYER_CLOAK, opacity=94)
        # The hood's shadow, and the one eye-glint under it.
        drawPolygon(sx - w * 0.42 + sway, sy - h * 0.98,
                    sx + w * 0.42 + sway, sy - h * 0.98,
                    sx + w * 0.30 + sway, sy - h * 0.42,
                    sx - w * 0.30 + sway, sy - h * 0.42,
                    fill=palette.VOID, opacity=86)
        g = 1.9 * k
        drawPolygon(sx + sway, sy - h * 0.72 - g, sx + sway + g, sy - h * 0.72,
                    sx + sway, sy - h * 0.72 + g, sx + sway - g, sy - h * 0.72,
                    fill=palette.UI_ACCENT, opacity=92)
        # The lantern, held out.
        lx = sx + w * 1.25 + sway * 0.5
        ly = sy - h * 0.05 + math.sin(run_time * 1.7) * 1.3 * k
        art.draw_glow(art.rgb_tuple(palette.LIGHT_WARM), lx, ly,
                      34 * k, int(58 * k), power=2.0)
        r = 4.2 * k
        drawPolygon(lx, ly - r, lx + r * 0.75, ly, lx, ly + r, lx - r * 0.75,
                    ly, fill=palette.LIGHT_CORE, opacity=92)

    def _draw_shrine(self, sx, sy, k, run_time):
        """A cold standing stone. Cold because it is the vault's, not yours -
        and because what it offers costs something."""
        from .draw import drawPolygon
        h = 20.0 * k
        w = 9.0 * k
        drawPolygon(sx - w, sy + h * 0.5, sx - w * 0.7, sy - h,
                    sx + w * 0.7, sy - h, sx + w, sy + h * 0.5,
                    fill=palette.UI_PANEL, opacity=92)
        drawPolygon(sx - w, sy + h * 0.5, sx - w * 0.7, sy - h,
                    sx + w * 0.7, sy - h, sx + w, sy + h * 0.5,
                    fill=palette.WARD, opacity=int(26 * k))
        spin = run_time * 1.1
        for i in range(3):
            a = spin + i * math.tau / 3
            r = 7.0 * k
            px = sx + math.cos(a) * r
            py = sy - h * 0.25 + math.sin(a) * r * 0.5
            s = 2.6 * k
            drawPolygon(px, py - s, px + s, py, px, py + s, px - s, py,
                        fill=palette.WARD, opacity=int(92 * k))


# ---------------------------------------------------------------------------
# What each kind is worth
# ---------------------------------------------------------------------------
def cache_value(depth, rng, held=()):
    """What is in a cache: embers, maybe oil, and often a relic.

    Embers are scaled off depth so a spur is worth the same fraction of a
    floor's income all the way down. But embers alone were the whole of it
    for a long time, and they are the least interesting thing a cache can
    hold - the same currency killing already pays, in a room the player went
    out of their way for. Most caches carry a relic now, and the ember pile
    is what a cache holds when it does not.

    Returns (embers, oil, relic_key_or_None).
    """
    from . import relics as relic_mod
    relic = None
    if rng.chance(0.62):
        picks = relic_mod.offer(held, rng, depth, count=1)
        if picks:
            relic = picks[0].key
    # A cache that gave a relic gives fewer embers with it - the relic *is*
    # the reward, and paying twice for one detour makes the other rooms on
    # the floor look like a waste of a walk.
    scale = 0.35 if relic else 1.0
    embers = int(round((14 + depth * 3.2) * rng.uniform(0.85, 1.2) * scale))
    return embers, rng.chance(0.25 if relic else 0.55), relic


#: The bargains a shrine can offer. Each is (label, terms, key) and each one
#: takes something the player actually wanted to keep - a shrine that only
#: gives is a cache with a longer walk.
SHRINE_PACTS = (
    ('THE LONG DARK', 'give a third of your light  -  take embers', 'fuel'),
    ('THE RED TITHE', 'give a fifth of your health  -  take embers', 'blood'),
    ('THE SPENT COIN', 'give your embers  -  take health and light', 'spend'),
    ('THE STEADY HAND', 'give embers  -  take a redraw', 'redraw'),
)


def shrine_pact(depth, rng, embers_held):
    """Pick a bargain this shrine can actually offer.

    A pact demanding embers from a player who has none is not a choice, it is
    a locked door with a sign on it - so the ones that charge in embers are
    only offered to someone carrying enough to pay.
    """
    pool = [p for p in SHRINE_PACTS
            if p[2] not in ('spend', 'redraw') or embers_held >= 25]
    return rng.choice(pool or list(SHRINE_PACTS[:2]))
