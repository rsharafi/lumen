"""Things you find, as opposed to things you choose.

An offering is three cards and a decision. A relic is an object sitting in a
room you did not have to walk to, and that difference should be felt - so
these are deliberately not more upgrades. An upgrade is a mutation of `Stats`
and vanishes into the numbers; a relic keeps its name, keeps a row in the HUD,
and mostly changes a *rule* rather than a quantity.

Before this, a cache - the reward for taking a dead-end branch off the
critical path, at the cost of fuel and health - paid out embers. The least
interesting thing it could possibly give: the same currency killing already
gives, in a room you had to go out of your way for.

## What makes a good one

Three tests, and a candidate has to pass all of them.

**It changes a rule, not a number.** `+12% damage` is an offering. "Your first
shot into an unlit room is a critical" is a relic. The second one changes how
you enter rooms; the first changes a multiplier.

**You can tell it is working.** Every relic here does something the player can
*see* happen - a flash, a sound, a thing that visibly does not die. A passive
that quietly adjusts a coefficient is indistinguishable from not having picked
it up, and a reward you cannot perceive is not a reward.

**It is about the light, where it can be.** That is what this game has. Six of
the sixteen are about the lantern directly, and most of the rest touch it
somewhere.

## How they attach

Each relic has an `apply` that may mutate `Stats` once on pickup, and any of a
small set of hooks the world calls at the right moment. Hooks are looked up by
name and missing ones cost nothing, so a relic that only wants `on_room_enter`
declares only that.
"""

from . import palette

#: Hooks a relic may implement. The world calls these; anything not declared
#: is simply absent from the dict and never looked for twice.
ON_KILL = 'on_kill'
ON_ROOM_ENTER = 'on_room_enter'
ON_HURT = 'on_hurt'
ON_FIRE = 'on_fire'
ON_FLARE = 'on_flare'
ON_FLOOR = 'on_floor'

COMMON = 'common'
RARE = 'rare'
LEGENDARY = 'legendary'

#: How often each tier turns up, by depth. Relics are much rarer than
#: offerings - you see maybe five in a run - so the tiers are wide apart.
TIER_WEIGHT = {
    COMMON: lambda d: 100.0,
    RARE: lambda d: 14.0 + 4.0 * max(0, d - 3),
    LEGENDARY: lambda d: 2.0 * max(0, d - 8),
}

TIER_COLOR = {
    COMMON: palette.UI_TEXT,
    RARE: palette.WISP_EYE,
    LEGENDARY: palette.XP,
}


class Relic:
    __slots__ = ('key', 'name', 'blurb', 'tier', 'color', 'apply', 'hooks',
                 'unique')

    def __init__(self, key, name, blurb, tier=COMMON, color=None, apply=None,
                 unique=True, **hooks):
        self.key = key
        self.name = name
        self.blurb = blurb
        self.tier = tier
        self.color = color or TIER_COLOR[tier]
        self.apply = apply
        self.unique = unique
        self.hooks = {k: v for k, v in hooks.items() if v is not None}


def _add(attr, amount):
    def apply(stats):
        setattr(stats, attr, getattr(stats, attr) + amount)
    return apply


def _mul(attr, factor):
    def apply(stats):
        setattr(stats, attr, getattr(stats, attr) * factor)
    return apply


# ==========================================================================
# The lantern six
# ==========================================================================
def _wick_room(world, relic):
    """THE LONG WICK: entering a room tops the lantern up a little."""
    world.player.add_fuel(14.0)
    world.effects.add_text(world.player.x, world.player.y - 30, 'WICK',
                           palette.LIGHT_WARM, 13, False)


def _tinder_kill(world, relic, enemy):
    """TINDERBOX: things that die inside your light give fuel back."""
    if getattr(enemy, 'lit', False):
        world.player.add_fuel(2.2)


def _ashglass_enter(world, relic):
    """ASHGLASS: the first room you enter on a floor is already lit.

    Not a permanent buff - a *moment*. Walking into a room and seeing all of
    it at once, exactly once per floor, is worth more than a small radius
    bonus everywhere, and it is the only relic that changes what a room is
    like to arrive in.
    """
    if world.room is not None and world.rooms_entered <= 1:
        for brazier in world.level.braziers:
            brazier.lit = True
            brazier.ignite_t = 1.0
            brazier.edges = None


def _gutterlamp_hurt(world, relic, amount):
    """GUTTERLAMP: being hit flares the lantern instead of dimming it."""
    world.player.add_fuel(9.0)
    world.effects.add_light(world.player.x, world.player.y, 320.0, 0.4,
                            palette.LIGHT_CORE)


def _coldstar_flare(world, relic):
    """COLDSTAR: the flare leaves a light behind where it went off."""
    world.effects.add_light(world.player.x, world.player.y, 420.0, 2.6,
                            palette.WARD)


def _nightseed_floor(world, relic):
    """NIGHTSEED: arriving on a new floor lights every brazier on the way in."""
    world.player.add_fuel(30.0)


# ==========================================================================
# The rest
# ==========================================================================
def _sighted_kill(world, relic, enemy):
    """DEAD RECKONING: a kill marks the nearest unlit thing, briefly."""
    best = None
    best_d = 1e18
    for other in world.enemies:
        if not other.alive or other is enemy or getattr(other, 'lit', False):
            continue
        d = (other.x - world.player.x) ** 2 + (other.y - world.player.y) ** 2
        if d < best_d:
            best_d, best = d, other
    if best is not None:
        world.effects.add_light(best.x, best.y, 90.0, 0.9, palette.UI_ACCENT)


def _kindling_kill(world, relic, enemy):
    """PYRE: everything that dies leaves a small burning mark."""
    if world.fxrng.chance(0.34):
        world.add_pool(enemy.x, enemy.y, 46.0, 2.6, 16.0)


def _second_wind_hurt(world, relic, amount):
    """SECOND WIND: being hurt makes you briefly faster."""
    world.player.haste = max(getattr(world.player, 'haste', 0.0), 1.6)


ALL = [
    # ---- the lantern ----------------------------------------------------
    Relic('longwick', 'THE LONG WICK',
          'Every room you enter feeds the lantern a little.',
          COMMON, palette.LIGHT_WARM, on_room_enter=_wick_room),

    Relic('tinderbox', 'TINDERBOX',
          'Anything that dies inside your light gives some of it back.',
          COMMON, palette.LIGHT_CORE, on_kill=_tinder_kill),

    Relic('ashglass', 'ASHGLASS',
          'The first room of every floor is already burning.',
          RARE, palette.LIGHT_WARM, on_room_enter=_ashglass_enter),

    Relic('gutterlamp', 'GUTTERLAMP',
          'A blow that lands makes the lantern flare, not gutter.',
          RARE, palette.LIGHT_CORE, on_hurt=_gutterlamp_hurt),

    Relic('coldstar', 'COLDSTAR',
          'Your flare leaves a cold light burning where it broke.',
          RARE, palette.WARD, on_flare=_coldstar_flare),

    Relic('nightseed', 'NIGHTSEED',
          'You arrive on every floor with the lantern fuller.',
          COMMON, palette.LIGHT_DEEP, on_floor=_nightseed_floor),

    # ---- rules ----------------------------------------------------------
    Relic('reckoning', 'DEAD RECKONING',
          'Each kill shows you where the next thing is standing.',
          RARE, palette.UI_ACCENT, on_kill=_sighted_kill),

    # A cleared room already hands you what is lying in it - that is what
    # `PickupField.collect_all` is for - so this had to be about the moment
    # rather than the tidy-up. Embers crossing a room to reach you mid-fight
    # is a thing you watch happen.
    Relic('magpie', 'MAGPIE',
          'Embers come to you from clear across the room.',
          COMMON, palette.XP, apply=_mul('pickup_radius', 2.6)),

    Relic('pyre', 'PYRE',
          'What you kill goes on burning for a while.',
          RARE, palette.DAMAGE, on_kill=_kindling_kill),

    Relic('secondwind', 'SECOND WIND',
          'Being hit puts speed in your legs.',
          COMMON, palette.PLAYER_TRIM, on_hurt=_second_wind_hurt),

    # ---- quantities worth having anyway ---------------------------------
    # Not every relic can be a rule, and a pool of nothing but rules is a
    # pool where every pickup demands the player rethink their run. A few
    # that simply make you sturdier are what the rest are read against.
    Relic('ironcore', 'IRON CORE',
          'You are built heavier. More health, and it stays.',
          COMMON, palette.HEAL, apply=_add('max_hp', 34.0)),

    Relic('keenglass', 'KEEN GLASS',
          'Everything you fire lands harder.',
          COMMON, palette.DAMAGE, apply=_mul('damage_mult', 1.16)),

    Relic('deepreserve', 'THE DEEP RESERVE',
          'The lantern holds a great deal more than it did.',
          RARE, palette.LIGHT_WARM, apply=_mul('fuel_max_mult', 1.5)),

    Relic('quicksilver', 'QUICKSILVER',
          'You move as though the floor were downhill.',
          RARE, palette.DASH_TRAIL, apply=_mul('speed_mult', 1.14)),

    # ---- the two that end runs ------------------------------------------
    Relic('lastlight', 'THE LAST LIGHT',
          'Once, the dark will let you go.',
          LEGENDARY, palette.LIGHT_CORE, apply=_add('revives', 1)),

    Relic('hollowheart', 'THE HOLLOW HEART',
          'Half again the damage. Nothing will ever heal you.',
          LEGENDARY, palette.CRIT,
          apply=lambda stats: (setattr(stats, 'damage_mult',
                                       stats.damage_mult * 1.5),
                               setattr(stats, 'no_heal', True))),
]

BY_KEY = {r.key: r for r in ALL}


def offer(held, rng, depth, count=1):
    """Pick `count` relics the run does not already carry."""
    pool = []
    for relic in ALL:
        if relic.unique and relic.key in held:
            continue
        weight = TIER_WEIGHT[relic.tier](depth)
        if weight <= 0.0:
            continue
        pool.append((relic, weight))

    out = []
    for _ in range(min(count, len(pool))):
        pick = rng.weighted(pool)
        out.append(pick)
        pool = [(r, w) for r, w in pool if r is not pick]
    return out


def tier_odds(depth):
    """What share of relic drops each tier takes. For tuning and docs."""
    weights = {t: max(0.0, fn(depth)) for t, fn in TIER_WEIGHT.items()}
    total = sum(weights.values()) or 1.0
    return {t: w / total for t, w in weights.items()}
