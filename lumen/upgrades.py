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

        # Which weapons this run may carry. The Vigil widens it.
        self.weapons = ['lance']

        self.owned = []

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


class Upgrade:
    def __init__(self, key, name, blurb, apply, color=None, rarity=1,
                 repeatable=True, requires=None):
        self.key = key
        self.name = name
        self.blurb = blurb
        self.apply = apply
        self.color = color or palette.UI_ACCENT
        self.rarity = rarity
        self.repeatable = repeatable
        self.requires = requires


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
            _mul('damage_mult', 1.22), palette.DAMAGE, rarity=3),

    Upgrade('quick', 'QUICKENED',
            'Fire 18% faster.',
            _mul('fire_rate_mult', 1.18), palette.BOLT, rarity=3),

    Upgrade('swift', 'SWIFT FOOT',
            'Move 14% faster.',
            _mul('speed_mult', 1.14), palette.PLAYER_TRIM, rarity=3),

    Upgrade('vessel', 'GREATER VESSEL',
            'Maximum health +28, and heal for that much now.',
            _heal_and_add('max_hp', 28.0, 28.0), palette.HEAL, rarity=3),

    Upgrade('wick', 'LONG WICK',
            'Lantern reaches 20% further.',
            _mul('lantern_mult', 1.20), palette.LIGHT_WARM, rarity=3),

    Upgrade('thrift', 'THRIFT',
            'Lantern burns 30% less fuel.',
            _mul('lantern_efficiency', 0.70), palette.LIGHT_DEEP, rarity=2),

    Upgrade('reservoir', 'DEEP RESERVOIR',
            'Lantern fuel capacity +45%.',
            _mul('fuel_max_mult', 1.45), palette.LIGHT_WARM, rarity=2),

    Upgrade('twinstep', 'TWIN STEP',
            'One additional dash charge.',
            _add('dash_charges', 1), palette.DASH_TRAIL, rarity=1,
            repeatable=True),

    Upgrade('recover', 'FAST RECOVERY',
            'Dash recharges 30% sooner.',
            _mul('dash_cooldown_mult', 0.70), palette.DASH_TRAIL, rarity=2),

    Upgrade('pierce', 'PIERCING SHOT',
            'Shots pass through one more enemy.',
            _add('pierce_bonus', 1), palette.BOLT, rarity=2),

    Upgrade('focus', 'FOCUS',
            'Critical chance +12%.',
            _add('crit_chance', 0.12), palette.CRIT, rarity=2),

    Upgrade('execute', 'EXECUTIONER',
            'Critical hits deal 0.8x more damage.',
            _add('crit_mult', 0.8), palette.CRIT, rarity=1),

    Upgrade('leech', 'EMBER LEECH',
            'Heal for 4% of the damage you deal.',
            _add('lifesteal', 0.04), palette.HEAL, rarity=1),

    Upgrade('reap', 'REAPING',
            'Heal 3 health on every kill.',
            _add('kill_heal', 3.0), palette.HEAL, rarity=2),

    Upgrade('volatile', 'VOLATILE ROUNDS',
            'Shots detonate on impact for area damage.',
            lambda s: (_add('explode_radius', 54.0)(s),
                       _add('explode_damage', 11.0)(s)),
            palette.ENEMY_BOLT, rarity=1, repeatable=False),

    Upgrade('bigger_bang', 'GREATER BLAST',
            'Detonations are 40% larger and hit harder.',
            lambda s: (_mul('explode_radius', 1.4)(s),
                       _add('explode_damage', 9.0)(s)),
            palette.ENEMY_BOLT, rarity=1, requires='volatile'),

    Upgrade('seeking', 'SEEKING LIGHT',
            'Your shots curve toward nearby enemies.',
            _add('homing', 3.4), palette.SPITTER_EYE, rarity=1,
            repeatable=False),

    Upgrade('ricochet', 'RICOCHET',
            'Shots bounce off walls once more.',
            _add('bounces', 1), palette.SCATTER, rarity=1),

    Upgrade('velocity', 'HIGH VELOCITY',
            'Projectiles travel 25% faster and reach further.',
            _mul('projectile_speed_mult', 1.25), palette.BOLT, rarity=2),

    Upgrade('sunburst', 'SUNBURST',
            'Lantern flares deal 70% more damage.',
            _mul('flare_damage_mult', 1.70), palette.FLARE, rarity=2),

    Upgrade('quickflare', 'QUICK FLARE',
            'Flare recharges 35% sooner.',
            _mul('flare_cooldown_mult', 0.65), palette.FLARE, rarity=2),

    Upgrade('searing', 'SEARING LIGHT',
            'Enemies standing in your lantern light burn for 9/sec.',
            _add('light_damage', 9.0), palette.LIGHT_CORE, rarity=1,
            repeatable=True),

    Upgrade('thorns', 'BRIARSKIN',
            'Attackers take 16 damage when they touch you.',
            _add('thorns', 16.0), palette.CRAWLER_EYE, rarity=1),

    Upgrade('ward', 'WARD',
            'Gain a shield that absorbs one hit. Refills each floor.',
            _add('shield_charges', 1), palette.SHIELD, rarity=1),

    Upgrade('stoic', 'STOIC',
            'Take 15% less damage.',
            _mul('taken_mult', 0.85), palette.UI_GOOD, rarity=1),

    Upgrade('magnet', 'LODESTONE',
            'Pick up embers from much further away.',
            _mul('pickup_radius', 1.9), palette.XP, rarity=2),

    Upgrade('mire', 'MIRE',
            'Enemies near you are slowed by 22%.',
            _add('slow_field', 0.22), palette.WISP_EYE, rarity=1,
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
            _add('dark_damage', 0.55), palette.WARDEN_EYE, rarity=2,
            repeatable=False),

    Upgrade('ambush', 'AMBUSHER',
            'Deal 70% more damage to enemies still at full health.',
            _add('first_strike', 0.70), palette.CRIT, rarity=2,
            repeatable=False),

    Upgrade('overcharge', 'OVERCHARGE',
            'Up to 80% more damage as the lantern empties. Run it dry.',
            _add('overcharge', 0.80), palette.LIGHT_CORE, rarity=1,
            repeatable=False),

    Upgrade('momentum', 'MOMENTUM',
            'Deal 30% more damage while you are moving.',
            _add('momentum', 0.30), palette.DASH_TRAIL, rarity=2,
            repeatable=False),

    Upgrade('bulwark', 'BULWARK',
            'Take 35% less damage while you stand still.',
            _add('bulwark', 0.35), palette.UI_ACCENT, rarity=2,
            repeatable=False),

    # ---- the lantern as a resource you spend and win back ------------------
    Upgrade('siphon', 'SIPHON',
            'Every kill returns 2.5 fuel to the lantern.',
            _add('siphon', 2.5), palette.LIGHT_WARM, rarity=2),

    Upgrade('kindler', 'KINDLER',
            'Embers are worth 60% more.',
            _mul('ember_gain', 1.6), palette.XP, rarity=2),

    Upgrade('flarestorm', 'FLARE STORM',
            'The flare recharges 45% sooner and hits 40% harder.',
            _both(_mul('flare_cooldown_mult', 0.55),
                  _mul('flare_damage_mult', 1.40)),
            palette.LIGHT_CORE, rarity=1, repeatable=False),

    # ---- movement as a weapon ---------------------------------------------
    Upgrade('cleave', 'DASH CLEAVE',
            'Your dash carves through anything it passes, for 26.',
            _add('dash_damage', 26.0), palette.DASH_TRAIL, rarity=2,
            repeatable=False),

    Upgrade('phase', 'PHASE STEP',
            'Two more dash charges, and they return twice as fast.',
            _both(_add('dash_charges', 2), _mul('dash_cooldown_mult', 0.5)),
            palette.DASH_TRAIL, rarity=1, repeatable=False,
            requires='twinstep'),

    # ---- volume ------------------------------------------------------------
    Upgrade('swarm', 'SWARMFIRE',
            'One extra shot per volley.',
            _add('swarm', 1), palette.BOLT, rarity=1),

    Upgrade('arc', 'ARCLIGHT',
            'Hits jump to two more enemies nearby, for less each time.',
            _add('chain', 2), palette.BEAM, rarity=2, repeatable=False),

    Upgrade('cascade', 'CASCADE',
            'Two more jumps again.',
            _add('chain', 2), palette.BEAM, rarity=1, repeatable=False,
            requires='arc'),

    # ---- second chances ----------------------------------------------------
    Upgrade('revive', 'LAST LIGHT',
            'Survive one killing blow, at one health.',
            _add('revives', 1), palette.HEAL, rarity=1, repeatable=False),

    Upgrade('reroll', 'SECOND SIGHT',
            'Two redraws at the offering, for the rest of the run.',
            _add('rerolls', 2), palette.UI_ACCENT, rarity=2),

    # ---- pacts: every one of these costs something -------------------------
    Upgrade('glasscannon', 'GLASS PACT',
            'Deal 60% more damage. Take 40% more.',
            _pact(_mul('damage_mult', 1.60), _mul('taken_mult', 1.40)),
            palette.DAMAGE, rarity=2, repeatable=False),

    Upgrade('gutter', 'GUTTERING PACT',
            'Fire 45% faster. Your lantern reaches a third less far.',
            _pact(_mul('fire_rate_mult', 1.45),
                  _both(_mul('lantern_mult', 0.67), _add('gutter', 0.33))),
            palette.LIGHT_DEEP, rarity=2, repeatable=False),

    Upgrade('bloodpact', 'BLOOD PACT',
            'Heal 9% of damage dealt. Maximum health halved.',
            _pact(_add('lifesteal', 0.09), _mul('max_hp', 0.5)),
            palette.HEAL, rarity=1, repeatable=False),

    Upgrade('hollowpact', 'HOLLOW PACT',
            'Move 30% faster and dash freely. You cannot be healed.',
            _pact(_both(_mul('speed_mult', 1.30),
                        _mul('dash_cooldown_mult', 0.45)),
                  _both(_add('kill_heal', -99.0), _add('lifesteal', -9.0))),
            palette.WISP_EYE, rarity=1, repeatable=False),

    Upgrade('greedpact', 'GREED PACT',
            'Embers are worth double. Take 25% more damage.',
            _pact(_mul('ember_gain', 2.0), _mul('taken_mult', 1.25)),
            palette.XP, rarity=2, repeatable=False),

    # ---- synergies: these want something already in the run ---------------
    Upgrade('conflagration', 'CONFLAGRATION',
            'Explosions are half again as large, and twice as fierce.',
            _both(_mul('explode_radius', 1.5), _mul('explode_damage', 2.0)),
            palette.SCATTER, rarity=1, repeatable=False,
            requires='volatile'),

    Upgrade('starve', 'STARVELIGHT',
            'Night-fed again: another 65% against what you cannot see.',
            _add('dark_damage', 0.65), palette.WARDEN_EYE, rarity=1,
            repeatable=False, requires='nightfeed'),

    Upgrade('furnace', 'FURNACE HEART',
            'Overcharge pays out twice as steeply.',
            _add('overcharge', 0.90), palette.LIGHT_CORE, rarity=1,
            repeatable=False, requires='overcharge'),
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
    tilt = min(1.0, max(0.0, (depth - 1) / 8.0))
    pool = []
    for up in ALL:
        if up.requires and up.requires not in stats.owned:
            continue
        if not up.repeatable and up.key in stats.owned:
            continue
        # A rarity-1 upgrade goes from a third of a common's weight to
        # slightly above it; a common drifts down to meet it.
        weight = float(up.rarity) + (2.0 - float(up.rarity)) * tilt
        pool.append((up, max(0.15, weight)))

    chosen = []
    for _ in range(min(count, len(pool))):
        pick = rng.weighted(pool)
        chosen.append(pick)
        pool = [(u, w) for (u, w) in pool if u.key != pick.key]
    return chosen


def grant(stats, upgrade):
    upgrade.apply(stats)
    stats.owned.append(upgrade.key)
