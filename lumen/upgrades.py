"""Run modifiers - the roguelite layer.

Every upgrade is a small mutation of `Stats`, which the player, the weapons,
and the lantern all read from. Keeping them declarative means the choice
screen, the run summary, and the tooltips all work off one list.
"""

from . import palette


class Stats:
    """The mutable numbers a run accumulates."""

    def __init__(self):
        self.damage_mult = 1.0
        self.fire_rate_mult = 1.0
        self.speed_mult = 1.0
        self.max_hp = 100.0
        self.lantern_mult = 1.0
        self.lantern_efficiency = 1.0
        self.dash_charges = 1
        self.dash_cooldown_mult = 1.0
        self.pierce_bonus = 0
        self.crit_chance = 0.05
        self.crit_mult = 2.0
        self.lifesteal = 0.0
        self.explode_radius = 0.0
        self.explode_damage = 0.0
        self.chain = 0
        self.homing = 0.0
        self.bounces = 0
        self.projectile_speed_mult = 1.0
        self.flare_damage_mult = 1.0
        self.flare_cooldown_mult = 1.0
        self.pickup_radius = 62.0
        self.thorns = 0.0
        self.shield_charges = 0
        self.fuel_max_mult = 1.0
        self.light_damage = 0.0        # damage per second to lit enemies
        self.kill_heal = 0.0
        self.slow_field = 0.0
        self.taken_mult = 1.0

        # ---- axes that change how a run is played, not just its numbers ---
        # Each of these is read somewhere specific; a stat nothing consumes is
        # a lie told to the player on the draft screen.
        self.dark_damage = 0.0      # bonus against enemies outside the light
        self.first_strike = 0.0     # bonus against anything still at full hp
        self.overcharge = 0.0       # bonus that grows as the lantern empties
        self.momentum = 0.0         # bonus while you are moving
        self.bulwark = 0.0          # damage reduction while you are not
        self.siphon = 0.0           # fuel returned per kill
        self.ember_gain = 1.0       # what a mote of ember is worth
        self.dash_damage = 0.0      # a dash that hurts what it passes through
        self.revives = 0            # survive one killing blow per charge
        self.rerolls = 0            # redraws at the offering
        self.swarm = 0              # extra shots per volley
        self.gutter = 0.0           # lantern reach lost, in exchange
        self.curses = 0             # pacts taken, for the run summary
        self.no_heal = False        # HOLLOW PACT: nothing restores health

        # Which weapons this run may carry. The Vigil widens it.
        self.weapons = ['lance']

        self.owned = []
        #: Relic keys this run carries, in the order they were found. Kept
        #: apart from `owned` because a relic is a thing with a name and a
        #: row in the HUD, not a mutation that has already happened.
        self.relics = []

    def describe(self):
        lines = []
        if self.damage_mult != 1.0:
            lines.append(f'Damage x{self.damage_mult:.2f}')
        if self.fire_rate_mult != 1.0:
            lines.append(f'Fire rate x{self.fire_rate_mult:.2f}')
        if self.speed_mult != 1.0:
            lines.append(f'Speed x{self.speed_mult:.2f}')
        if self.crit_chance > 0.05:
            lines.append(f'Crit {self.crit_chance * 100:.0f}%')
        if self.lantern_mult != 1.0:
            lines.append(f'Lantern x{self.lantern_mult:.2f}')
        return lines


# ---------------------------------------------------------------------------
# Rarity
#
# `rarity` used to be the weight itself - 3 common, 1 rare - which meant a
# rare upgrade was a third as likely as a common one on every floor including
# the first. A third is not rare. It also left nowhere to put anything
# stronger, so the ceiling of the pool was whatever "rare" happened to mean.
#
# Six tiers now, each with a weight that is a function of depth. A tier that
# has not started yet weighs nothing, so the early floors cannot offer what
# the player has not descended far enough to have earned - and the top of the
# pool arrives so late and so seldom that seeing one is an event.
# ---------------------------------------------------------------------------
COMMON = 'common'
UNCOMMON = 'uncommon'
RARE = 'rare'
EPIC = 'epic'
LEGENDARY = 'legendary'
MYTHIC = 'mythic'

TIER_ORDER = (COMMON, UNCOMMON, RARE, EPIC, LEGENDARY, MYTHIC)

# tier -> (label, pips, weight(depth))
TIERS = {
    COMMON:    ('COMMON', 1, lambda d: 100.0),
    UNCOMMON:  ('UNCOMMON', 2, lambda d: 10.0 + 5.0 * (d - 1)),
    RARE:      ('RARE', 3, lambda d: 0.8 + 2.2 * max(0, d - 1)),
    EPIC:      ('EPIC', 4, lambda d: 3.0 * max(0, d - 4)),
    LEGENDARY: ('LEGENDARY', 5, lambda d: 1.6 * max(0, d - 7)),
    MYTHIC:    ('MYTHIC', 6, lambda d: 0.5 * max(0, d - 10)),
}


def tier_weight(tier, depth):
    entry = TIERS.get(tier)
    return max(0.0, entry[2](depth)) if entry else 0.0


def tier_odds(depth):
    """What share of offers each tier takes at `depth`. For tuning and docs."""
    weights = {t: tier_weight(t, depth) for t in TIER_ORDER}
    total = sum(weights.values()) or 1.0
    return {t: w / total for t, w in weights.items()}


class Upgrade:
    def __init__(self, key, name, blurb, apply, color=None, rarity=COMMON,
                 repeatable=True, requires=None):
        self.key = key
        self.name = name
        self.blurb = blurb
        self.apply = apply
        self.color = color or palette.UI_ACCENT
        self.rarity = rarity
        self.repeatable = repeatable
        self.requires = requires


def _set(attr, value):
    def apply(stats):
        setattr(stats, attr, value)
    return apply


def _pact(gain, cost):
    """A bargain: something good, and something that is not.

    The flat upgrades are all upside, which makes the offering a question of
    which number you would like raised rather than a decision. A pact costs
    something real, so taking one is a read on the run you are actually in.
    """
    def apply(stats):
        gain(stats)
        cost(stats)
        stats.curses += 1
    return apply


def _both(*fns):
    def apply(stats):
        for fn in fns:
            fn(stats)
    return apply


def _mul(attr, factor):
    def apply(stats):
        setattr(stats, attr, getattr(stats, attr) * factor)
    return apply


def _add(attr, amount):
    def apply(stats):
        setattr(stats, attr, getattr(stats, attr) + amount)
    return apply


def _heal_and_add(attr, amount, heal):
    def apply(stats):
        setattr(stats, attr, getattr(stats, attr) + amount)
        stats.pending_heal = getattr(stats, 'pending_heal', 0.0) + heal
    return apply


ALL = [
    Upgrade('keen', 'KEEN EDGE',
            'All damage increased by 22%.',
            _mul('damage_mult', 1.22), palette.DAMAGE, rarity=COMMON),

    Upgrade('quick', 'QUICKENED',
            'Fire 18% faster.',
            _mul('fire_rate_mult', 1.18), palette.BOLT, rarity=COMMON),

    Upgrade('swift', 'SWIFT FOOT',
            'Move 14% faster.',
            _mul('speed_mult', 1.14), palette.PLAYER_TRIM, rarity=COMMON),

    Upgrade('vessel', 'GREATER VESSEL',
            'Maximum health +28, and heal for that much now.',
            _heal_and_add('max_hp', 28.0, 28.0), palette.HEAL, rarity=COMMON),

    Upgrade('wick', 'LONG WICK',
            'Lantern reaches 20% further.',
            _mul('lantern_mult', 1.20), palette.LIGHT_WARM, rarity=COMMON),

    Upgrade('thrift', 'THRIFT',
            'Lantern burns 30% less fuel.',
            _mul('lantern_efficiency', 0.70), palette.LIGHT_DEEP, rarity=COMMON),

    Upgrade('reservoir', 'DEEP RESERVOIR',
            'Lantern fuel capacity +45%.',
            _mul('fuel_max_mult', 1.45), palette.LIGHT_WARM, rarity=COMMON),

    Upgrade('twinstep', 'TWIN STEP',
            'One additional dash charge.',
            _add('dash_charges', 1), palette.DASH_TRAIL, rarity=COMMON,
            repeatable=True),

    Upgrade('recover', 'FAST RECOVERY',
            'Dash recharges 30% sooner.',
            _mul('dash_cooldown_mult', 0.70), palette.DASH_TRAIL, rarity=COMMON),

    Upgrade('pierce', 'PIERCING SHOT',
            'Shots pass through one more enemy.',
            _add('pierce_bonus', 1), palette.BOLT, rarity=COMMON),

    Upgrade('focus', 'FOCUS',
            'Critical chance +12%.',
            _add('crit_chance', 0.12), palette.CRIT, rarity=COMMON),

    Upgrade('execute', 'EXECUTIONER',
            'Critical hits deal 0.8x more damage.',
            _add('crit_mult', 0.8), palette.CRIT, rarity=COMMON),

    Upgrade('leech', 'EMBER LEECH',
            'Heal for 4% of the damage you deal.',
            _add('lifesteal', 0.04), palette.HEAL, rarity=COMMON),

    Upgrade('reap', 'REAPING',
            'Heal 3 health on every kill.',
            _add('kill_heal', 3.0), palette.HEAL, rarity=COMMON),

    Upgrade('volatile', 'VOLATILE ROUNDS',
            'Shots detonate on impact for area damage.',
            lambda s: (_add('explode_radius', 54.0)(s),
                       _add('explode_damage', 11.0)(s)),
            palette.ENEMY_BOLT, rarity=UNCOMMON, repeatable=False),

    Upgrade('bigger_bang', 'GREATER BLAST',
            'Detonations are 40% larger and hit harder.',
            lambda s: (_mul('explode_radius', 1.4)(s),
                       _add('explode_damage', 9.0)(s)),
            palette.ENEMY_BOLT, rarity=RARE, requires='volatile'),

    Upgrade('seeking', 'SEEKING LIGHT',
            'Your shots curve toward nearby enemies.',
            _add('homing', 3.4), palette.SPITTER_EYE, rarity=UNCOMMON,
            repeatable=False),

    Upgrade('ricochet', 'RICOCHET',
            'Shots bounce off walls once more.',
            _add('bounces', 1), palette.SCATTER, rarity=UNCOMMON),

    Upgrade('velocity', 'HIGH VELOCITY',
            'Projectiles travel 25% faster and reach further.',
            _mul('projectile_speed_mult', 1.25), palette.BOLT, rarity=COMMON),

    Upgrade('sunburst', 'SUNBURST',
            'Lantern flares deal 70% more damage.',
            _mul('flare_damage_mult', 1.70), palette.FLARE, rarity=UNCOMMON),

    Upgrade('quickflare', 'QUICK FLARE',
            'Flare recharges 35% sooner.',
            _mul('flare_cooldown_mult', 0.65), palette.FLARE, rarity=UNCOMMON),

    Upgrade('searing', 'SEARING LIGHT',
            'Enemies standing in your lantern light burn for 9/sec.',
            _add('light_damage', 9.0), palette.LIGHT_CORE, rarity=RARE,
            repeatable=True),

    Upgrade('thorns', 'BRIARSKIN',
            'Attackers take 16 damage when they touch you.',
            _add('thorns', 16.0), palette.CRAWLER_EYE, rarity=UNCOMMON),

    Upgrade('ward', 'WARD',
            'Gain a shield that absorbs one hit. Refills each floor.',
            _add('shield_charges', 1), palette.SHIELD, rarity=UNCOMMON),

    Upgrade('stoic', 'STOIC',
            'Take 15% less damage.',
            _mul('taken_mult', 0.85), palette.UI_GOOD, rarity=UNCOMMON),

    Upgrade('magnet', 'LODESTONE',
            'Pick up embers from much further away.',
            _mul('pickup_radius', 1.9), palette.XP, rarity=COMMON),

    Upgrade('mire', 'MIRE',
            'Enemies near you are slowed by 22%.',
            _add('slow_field', 0.22), palette.WISP_EYE, rarity=UNCOMMON,
            repeatable=False),
]

# ---------------------------------------------------------------------------
# The second half of the pool: things that change how a floor is played.
#
# Everything above this line raises a number. That is fine as a foundation and
# hopeless as a whole design - three offerings of "damage x1.22" in a row is
# not a decision, and a run built entirely out of them plays exactly like a
# run built out of any other three. What follows either asks something of the
# player (a pact costs what it gives), or only pays out under a condition the
# player has to steer the run into.
# ---------------------------------------------------------------------------
ALL += [
    # ---- conditional damage: each one wants a different way of fighting ----
    Upgrade('nightfeed', 'NIGHT-FED',
            'Deal 55% more damage to anything outside your light.',
            _add('dark_damage', 0.55), palette.WARDEN_EYE, rarity=UNCOMMON,
            repeatable=False),

    Upgrade('ambush', 'AMBUSHER',
            'Deal 70% more damage to enemies still at full health.',
            _add('first_strike', 0.70), palette.CRIT, rarity=UNCOMMON,
            repeatable=False),

    Upgrade('overcharge', 'OVERCHARGE',
            'Up to 80% more damage as the lantern empties. Run it dry.',
            _add('overcharge', 0.80), palette.LIGHT_CORE, rarity=RARE,
            repeatable=False),

    Upgrade('momentum', 'MOMENTUM',
            'Deal 30% more damage while you are moving.',
            _add('momentum', 0.30), palette.DASH_TRAIL, rarity=UNCOMMON,
            repeatable=False),

    Upgrade('bulwark', 'BULWARK',
            'Take 35% less damage while you stand still.',
            _add('bulwark', 0.35), palette.UI_ACCENT, rarity=UNCOMMON,
            repeatable=False),

    # ---- the lantern as a resource you spend and win back ------------------
    Upgrade('siphon', 'SIPHON',
            'Every kill returns 2.5 fuel to the lantern.',
            _add('siphon', 2.5), palette.LIGHT_WARM, rarity=UNCOMMON),

    Upgrade('kindler', 'KINDLER',
            'Embers are worth 60% more.',
            _mul('ember_gain', 1.6), palette.XP, rarity=UNCOMMON),

    Upgrade('flarestorm', 'FLARE STORM',
            'The flare recharges 45% sooner and hits 40% harder.',
            _both(_mul('flare_cooldown_mult', 0.55),
                  _mul('flare_damage_mult', 1.40)),
            palette.LIGHT_CORE, rarity=RARE, repeatable=False),

    # ---- movement as a weapon ---------------------------------------------
    Upgrade('cleave', 'DASH CLEAVE',
            'Your dash carves through anything it passes, for 26.',
            _add('dash_damage', 26.0), palette.DASH_TRAIL, rarity=RARE,
            repeatable=False),

    Upgrade('phase', 'PHASE STEP',
            'Two more dash charges, and they return twice as fast.',
            _both(_add('dash_charges', 2), _mul('dash_cooldown_mult', 0.5)),
            palette.DASH_TRAIL, rarity=EPIC, repeatable=False,
            requires='twinstep'),

    # ---- volume ------------------------------------------------------------
    # Doubling a one-shot volley is a flat doubling of damage, which is more
    # than any other single card in the pool does - it was uncommon, which
    # meant it turned up on floor two and decided the run.
    Upgrade('swarm', 'SWARMFIRE',
            'One extra shot per volley.',
            _add('swarm', 1), palette.BOLT, rarity=LEGENDARY,
            repeatable=False),

    Upgrade('arc', 'ARCLIGHT',
            'Hits jump to two more enemies nearby, for less each time.',
            _add('chain', 2), palette.BEAM, rarity=RARE, repeatable=False),

    Upgrade('cascade', 'CASCADE',
            'Two more jumps again.',
            _add('chain', 2), palette.BEAM, rarity=EPIC, repeatable=False,
            requires='arc'),

    # ---- second chances ----------------------------------------------------
    Upgrade('revive', 'LAST LIGHT',
            'Survive one killing blow, at one health.',
            _add('revives', 1), palette.HEAL, rarity=RARE, repeatable=False),

    Upgrade('reroll', 'SECOND SIGHT',
            'Two redraws at the offering, for the rest of the run.',
            _add('rerolls', 2), palette.UI_ACCENT, rarity=UNCOMMON),

    # ---- pacts: every one of these costs something -------------------------
    Upgrade('glasscannon', 'GLASS PACT',
            'Deal 60% more damage. Take 40% more.',
            _pact(_mul('damage_mult', 1.60), _mul('taken_mult', 1.40)),
            palette.DAMAGE, rarity=RARE, repeatable=False),

    Upgrade('gutter', 'GUTTERING PACT',
            'Fire 45% faster. Your lantern reaches a third less far.',
            _pact(_mul('fire_rate_mult', 1.45),
                  _both(_mul('lantern_mult', 0.67), _add('gutter', 0.33))),
            palette.LIGHT_DEEP, rarity=RARE, repeatable=False),

    Upgrade('bloodpact', 'BLOOD PACT',
            'Heal 9% of damage dealt. Maximum health halved.',
            _pact(_add('lifesteal', 0.09), _mul('max_hp', 0.5)),
            palette.HEAL, rarity=RARE, repeatable=False),

    Upgrade('hollowpact', 'HOLLOW PACT',
            'Move 30% faster and dash freely. You cannot be healed.',
            _pact(_both(_mul('speed_mult', 1.30),
                        _mul('dash_cooldown_mult', 0.45)),
                  _set('no_heal', True)),
            palette.WISP_EYE, rarity=RARE, repeatable=False),

    Upgrade('greedpact', 'GREED PACT',
            'Embers are worth double. Take 25% more damage.',
            _pact(_mul('ember_gain', 2.0), _mul('taken_mult', 1.25)),
            palette.XP, rarity=RARE, repeatable=False),

    # ---- synergies: these want something already in the run ---------------
    Upgrade('conflagration', 'CONFLAGRATION',
            'Explosions are half again as large, and twice as fierce.',
            _both(_mul('explode_radius', 1.5), _mul('explode_damage', 2.0)),
            palette.SCATTER, rarity=EPIC, repeatable=False,
            requires='volatile'),

    Upgrade('starve', 'STARVELIGHT',
            'Night-fed again: another 65% against what you cannot see.',
            _add('dark_damage', 0.65), palette.WARDEN_EYE, rarity=EPIC,
            repeatable=False, requires='nightfeed'),

    Upgrade('furnace', 'FURNACE HEART',
            'Overcharge pays out twice as steeply.',
            _add('overcharge', 0.90), palette.LIGHT_CORE, rarity=EPIC,
            repeatable=False, requires='overcharge'),
]

# ---------------------------------------------------------------------------
# The top of the pool. None of this can appear before the floor its tier
# starts on, and the last of it is rarer than anything else in the game: a
# mythic is about one offer in two hundred on the deepest floor, which is
# most of a run's worth of descending for a coin flip at seeing one.
# ---------------------------------------------------------------------------
ALL += [
    Upgrade('heartfire', 'HEARTFIRE',
            'Heal 10% of damage dealt, and 7 more on every kill.',
            _both(_add('lifesteal', 0.10), _add('kill_heal', 7.0)),
            palette.HEAL, rarity=EPIC, repeatable=False),

    Upgrade('stormcall', 'STORMCALL',
            'Hits jump to three more enemies, and reach further doing it.',
            _both(_add('chain', 3), _add('homing', 0.25)),
            palette.BEAM, rarity=EPIC, repeatable=False),

    Upgrade('avalanche', 'AVALANCHE',
            'Two more shots a volley, one more pierce, 25% more fire rate.',
            _both(_add('swarm', 2),
                  _both(_add('pierce_bonus', 1), _mul('fire_rate_mult', 1.25))),
            palette.BOLT, rarity=LEGENDARY, repeatable=False),

    Upgrade('unblinking', 'THE UNBLINKING EYE',
            'Critical chance +40%, and criticals hit for 1.4x more.',
            _both(_add('crit_chance', 0.40), _add('crit_mult', 1.4)),
            palette.CRIT, rarity=LEGENDARY, repeatable=False),

    Upgrade('tidebreak', 'TIDEBREAKER',
            'Everything you kill returns 6 fuel and 12 health.',
            _both(_add('siphon', 6.0), _add('kill_heal', 12.0)),
            palette.LIGHT_WARM, rarity=LEGENDARY, repeatable=False),

    Upgrade('secondsun', 'SECOND SUN',
            'The lantern stops burning fuel. It simply does not go out.',
            _mul('lantern_efficiency', 0.0),
            palette.LIGHT_CORE, rarity=MYTHIC, repeatable=False),

    Upgrade('longdark', 'THE LONG DARK',
            'Triple damage to anything outside your light. Half the light.',
            _pact(_add('dark_damage', 2.0),
                  _both(_mul('lantern_mult', 0.5), _add('gutter', 0.5))),
            palette.WARDEN_EYE, rarity=MYTHIC, repeatable=False),

    Upgrade('doomsayer', 'DOOMSAYER',
            'Deal 130% more damage. You have a quarter of your health.',
            _pact(_mul('damage_mult', 2.30), _mul('max_hp', 0.25)),
            palette.DAMAGE, rarity=MYTHIC, repeatable=False),
]

BY_KEY = {u.key: u for u in ALL}


def offer(stats, rng, count=3, depth=1):
    """Pick `count` distinct upgrades that are legal for this run.

    `rarity` is a weight, so 3 is common and 1 is not. Depth tilts that: on
    the first floors the common, foundational things dominate, and the run
    -defining ones get likelier the further down you are. Without it every
    build is decided on floor one by whatever happened to come up, and the
    remaining eleven floors are arithmetic.
    """
    pool = []
    for up in ALL:
        if up.requires and up.requires not in stats.owned:
            continue
        if not up.repeatable and up.key in stats.owned:
            continue
        weight = tier_weight(up.rarity, depth)
        if weight <= 0.0:
            continue          # this tier has not started yet
        pool.append((up, weight))

    chosen = []
    for _ in range(min(count, len(pool))):
        pick = rng.weighted(pool)
        chosen.append(pick)
        pool = [(u, w) for (u, w) in pool if u.key != pick.key]
    return chosen


def grant(stats, upgrade):
    upgrade.apply(stats)
    stats.owned.append(upgrade.key)
