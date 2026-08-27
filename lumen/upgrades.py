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

BY_KEY = {u.key: u for u in ALL}


def offer(stats, rng, count=3):
    """Pick `count` distinct upgrades that are legal for this run."""
    pool = []
    for up in ALL:
        if up.requires and up.requires not in stats.owned:
            continue
        if not up.repeatable and up.key in stats.owned:
            continue
        pool.append((up, float(up.rarity)))

    chosen = []
    for _ in range(min(count, len(pool))):
        pick = rng.weighted(pool)
        chosen.append(pick)
        pool = [(u, w) for (u, w) in pool if u.key != pick.key]
    return chosen


def grant(stats, upgrade):
    upgrade.apply(stats)
    stats.owned.append(upgrade.key)
