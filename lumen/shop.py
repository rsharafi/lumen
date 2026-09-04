"""The Ferryman: what he carries, and what he asks for it.

Embers were a score that happened to be spendable *between* runs. This is the
other half of them, and the point of it is one decision, made eight or nine
times a descent:

    **spend it now, or bank it for next time.**

One currency does that; two would let the player have both and decide
nothing. So the Ferryman's prices are quoted in the same embers the Vigil
takes, and every purchase is a rank of something permanent not bought.

## Pricing

An ember is worth whatever a floor pays for one, and that number is not a
guess - `tools/economy_report.py` counts it against the real spawn tables at
the real depths. It came out steeply curved: a floor of act one pays about
fifty embers if you take every room in it, and a floor of act three pays
seven hundred. A flat price list is therefore useless. It would be a
mortgage on floor two and a rounding error on floor eighteen.

So nothing here is priced in embers. Everything is priced as a fraction of
what the floor it is standing on is worth, and `floor_unit` is the curve
fitted to that measurement.

The fractions are deliberately large. A shop that shaves a tenth off your
banking is a shop you always buy from, which is not a decision either.
"""

from . import palette, upgrades
from .projectiles import WEAPONS, WEAPONS_BY_KEY

OFFERING = 'offering'
WEAPON = 'weapon'
RESTORE = 'restore'
REDRAW = 'redraw'


def floor_unit(depth):
    """Roughly what one floor pays, taken whole. See the module docstring.

    Fitted to `tools/economy_report.py`, which measured 54 embers on floor
    one, 388 on floor twelve and about 700 on floor nineteen. Re-run it after
    changing spawn tables or ember values: if this drifts away from what a
    floor actually pays, every price in the game drifts with it.
    """
    return 30.0 + 15.0 * depth + 1.25 * depth * depth


#: What each thing costs, as a share of one floor's income. A rare offering
#: at 0.55 is over half a floor's full-clear takings - which is the intended
#: weight of it: buying one should be felt at the Vigil afterwards.
SHARE = {
    upgrades.COMMON: 0.26,
    upgrades.UNCOMMON: 0.34,
    upgrades.RARE: 0.48,
    upgrades.EPIC: 0.62,
    upgrades.LEGENDARY: 0.80,
    upgrades.MYTHIC: 1.05,
    WEAPON: 0.72,
    RESTORE: 0.24,
    REDRAW: 0.30,
}

#: A reroll of the whole stock, and what each successive one costs on top.
REROLL_BASE = 0.14
REROLL_STEP = 0.09


def _round_price(value):
    """Prices land on fives. A price of 137 reads as a number a machine
    produced; 135 reads as a price somebody is asking."""
    return max(5, int(round(value / 5.0)) * 5)


class Slot:
    """One thing on the Ferryman's shelf."""

    __slots__ = ('kind', 'price', 'upgrade', 'weapon', 'name', 'blurb',
                 'color', 'sold')

    def __init__(self, kind, price, name, blurb, color,
                 upgrade=None, weapon=None):
        self.kind = kind
        self.price = price
        self.name = name
        self.blurb = blurb
        self.color = color
        self.upgrade = upgrade
        self.weapon = weapon
        self.sold = False

    def affordable(self, embers):
        return not self.sold and embers >= self.price


def _offering_slot(up, depth):
    return Slot(OFFERING, _round_price(floor_unit(depth) * SHARE.get(
        up.rarity, 0.3)), up.name, up.blurb,
        up.color or palette.UI_ACCENT, upgrade=up)


def _weapon_slot(key, depth):
    weapon = WEAPONS_BY_KEY[key]
    return Slot(WEAPON, _round_price(floor_unit(depth) * SHARE[WEAPON]),
                weapon.name, weapon.blurb, weapon.color, weapon=key)


def _restore_slot(depth):
    return Slot(RESTORE, _round_price(floor_unit(depth) * SHARE[RESTORE]),
                'OIL AND BANDAGE',
                'Fills the lantern, and closes half of what is open.',
                palette.HEAL)


def _redraw_slot(depth):
    return Slot(REDRAW, _round_price(floor_unit(depth) * SHARE[REDRAW]),
                'A SECOND LOOK',
                'One more redraw, at the offering between floors.',
                palette.UI_ACCENT)


def _scaled(price, stats):
    """THE TOLL, applied at the one place every price passes through."""
    from . import ascension
    return _round_price(price * ascension.price_scale(
        getattr(stats, 'rules', ())))


def stock(depth, stats, rng, slots=3):
    """What the Ferryman has today.

    Always at least one offering, because that is the reason to stop. A
    weapon only if there is one the run cannot already carry - a shelf
    offering something you are holding is a shelf with a gap in it.
    """
    out = []
    # One slot is always an offering, so the extras below may only ever fill
    # `slots - 1`. Without that reservation a lucky roll of weapon, restore
    # and redraw filled the shelf and the offering - the reason to stop at
    # all - was truncated straight back off it.
    room_for_extras = max(0, slots - 1)

    # A weapon the run is not carrying. The Vigil unseals a weapon for
    # good; the Ferryman rents you one for the descent - which is the more
    # interesting of the two, because it lets a run reach for something it
    # has not earned yet and find out whether it wants to.
    #
    # First on the shelf, because it is the rarest thing on it and should
    # not be crowded out by the rolls below.
    carried = set(getattr(stats, 'weapons', ()) or ('lance',))
    available = [w.key for w in WEAPONS if w.key not in carried]
    if available and len(out) < room_for_extras and rng.chance(0.55):
        out.append(_weapon_slot(rng.choice(available), depth))

    if len(out) < room_for_extras and rng.chance(0.62):
        out.append(_restore_slot(depth))
    if len(out) < room_for_extras and rng.chance(0.30):
        out.append(_redraw_slot(depth))

    # Offerings fill whatever is left, and there is always at least one.
    want = max(1, slots - len(out))
    picks = upgrades.offer(stats, rng, count=want, depth=depth)
    for up in picks:
        out.append(_offering_slot(up, depth))

    # `shuffled` returns a new list rather than shuffling in place, so the
    # order was previously insertion order and the shelf read the same way
    # every single time.
    for slot in out:
        slot.price = _scaled(slot.price, stats)
    return rng.shuffled(out)[:slots]


def reroll_price(depth, times):
    return _round_price(floor_unit(depth)
                        * (REROLL_BASE + REROLL_STEP * times))
