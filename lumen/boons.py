"""What killing a boss is worth.

A run's build came from one place: three cards, twenty times, out of a single
pool. Fifty-eight upgrades is a good pool and it is still one pool, so two
runs that drew similar cards played similarly, and nothing in a run ever
committed to anything - there was no point at which you chose *what kind of
run this is* rather than which number to raise next.

So the three bosses are that point. Beating one offers a **boon**: three
choices from a pool that exists nowhere else, and every one of them is a
commitment rather than an increment.

Three kinds, and the split is deliberate.

**Shaping the lantern.** Three of them, and they are the only things in the
game allowed to touch it, because the lantern is the game. REACH throws it
further, DEPTH makes it last, and EDGE turns its rim into a weapon - which is
the one that changes how you stand, because it asks you to keep things at the
*boundary* of your light rather than in the middle of it.

**Modding a weapon.** A weapon takes one mod, for the run, and the mod is
always a trade - more rate for less damage, more reach for less punch. Six
weapons that are answers to different questions was the design; a mod is what
lets a run answer one of them *harder* instead of carrying all six evenly.

**One large upgrade.** A boon that is only ever a system change would make
every boss a puzzle about your build. Sometimes the right reward is simply
being much stronger, and the pool keeps a few of those so the others are
chosen over something rather than in a vacuum.
"""

from . import palette
from .projectiles import WEAPONS, WEAPONS_BY_KEY, Weapon

LANTERN = 'lantern'
MOD = 'mod'
POWER = 'power'


# ==========================================================================
# Weapon mods
# ==========================================================================
class Mod:
    """A per-run alteration to one weapon.

    Expressed as multipliers and offsets on `Weapon`'s own fields rather than
    as behaviour, so applying one is building a new `Weapon` once and the
    firing path never learns that mods exist. A shot costs the same whether
    the weapon is modded or not.
    """

    __slots__ = ('key', 'name', 'blurb', 'mul', 'add')

    def __init__(self, key, name, blurb, mul=None, add=None):
        self.key = key
        self.name = name
        self.blurb = blurb
        self.mul = mul or {}
        self.add = add or {}

    def applied(self, weapon):
        """`weapon` with this mod baked in, as a new object."""
        fields = dict(
            key=weapon.key, name=weapon.name, blurb=weapon.blurb,
            cooldown=weapon.cooldown, damage=weapon.damage,
            speed=weapon.speed, pellets=weapon.pellets,
            spread=weapon.spread, life=weapon.life, pierce=weapon.pierce,
            radius=weapon.radius, length=weapon.length, width=weapon.width,
            color=weapon.color, glow=weapon.glow, knockback=weapon.knockback,
            charge_time=weapon.charge_time, charge_scale=weapon.charge_scale,
            sound=weapon.sound, recoil=weapon.recoil, shake=weapon.shake,
            light=weapon.light, volume=weapon.volume,
        )
        for name, factor in self.mul.items():
            fields[name] = fields[name] * factor
        for name, amount in self.add.items():
            fields[name] = fields[name] + amount
        # Pellet counts and pierce are counts, not quantities.
        fields['pellets'] = max(1, int(round(fields['pellets'])))
        fields['pierce'] = max(0, int(round(fields['pierce'])))
        return Weapon(**fields)


MODS = [
    Mod('hairtrigger', 'HAIRTRIGGER',
        'Half again the rate. Each shot lands lighter.',
        mul={'cooldown': 0.66, 'damage': 0.74}),

    Mod('heavyshot', 'HEAVY SHOT',
        'Much harder, much slower.',
        mul={'damage': 1.62, 'cooldown': 1.4, 'knockback': 1.5,
             'recoil': 1.3}),

    Mod('longbarrel', 'LONG BARREL',
        'It carries further and flies flatter.',
        mul={'life': 1.6, 'speed': 1.35, 'damage': 0.86, 'spread': 0.5}),

    Mod('scattered', 'SCATTERED',
        'Two more in the volley, spread wider, each worth less.',
        mul={'damage': 0.7, 'spread': 1.8}, add={'pellets': 2}),

    Mod('throughandthrough', 'THROUGH AND THROUGH',
        'It goes through two more things before it stops.',
        mul={'damage': 0.82}, add={'pierce': 2}),

    Mod('hammerhead', 'HAMMERHEAD',
        'It moves what it hits, a great deal. It hits a little softer.',
        # Recoil is not a reliable cost - shoving yourself backwards is a
        # repositioning tool as often as it is a nuisance - so this pays for
        # its knockback in damage, which is unambiguous.
        mul={'knockback': 2.6, 'recoil': 1.6, 'damage': 0.9}),

    Mod('brightrounds', 'BRIGHT ROUNDS',
        'Every shot carries light with it.',
        mul={'light': 2.4, 'damage': 1.1, 'cooldown': 1.12}),

    Mod('fatrounds', 'FAT ROUNDS',
        'Bigger, heavier, and harder to miss with.',
        mul={'radius': 1.7, 'width': 1.5, 'damage': 1.2, 'speed': 0.82,
             'cooldown': 1.15}),
]

MODS_BY_KEY = {m.key: m for m in MODS}


def weapon_for(stats, key):
    """The weapon `key` as this run carries it, mod included.

    Cached on `stats` so the object is built once per mod rather than once
    per shot, and looked up by the same key the unmodded path uses - which is
    what keeps `Player.weapon` a dictionary lookup either way.
    """
    base = WEAPONS_BY_KEY.get(key)
    if base is None:
        return None
    mods = getattr(stats, 'weapon_mods', None)
    if not mods or key not in mods:
        return base
    cache = getattr(stats, '_modded', None)
    if cache is None:
        cache = stats._modded = {}
    hit = cache.get(key)
    if hit is None:
        mod = MODS_BY_KEY.get(mods[key])
        hit = cache[key] = mod.applied(base) if mod else base
    return hit


# ==========================================================================
# The boons themselves
# ==========================================================================
class Boon:
    __slots__ = ('key', 'name', 'blurb', 'kind', 'color', 'apply', 'weapon')

    def __init__(self, key, name, blurb, kind, color, apply, weapon=None):
        self.key = key
        self.name = name
        self.blurb = blurb
        self.kind = kind
        self.color = color
        self.apply = apply
        self.weapon = weapon


def _lantern(attr, factor):
    def apply(stats):
        setattr(stats, attr, getattr(stats, attr) * factor)
    return apply


def _edge(stats):
    stats.edge_damage += 26.0


def _mod_apply(weapon_key, mod_key):
    def apply(stats):
        mods = getattr(stats, 'weapon_mods', None)
        if mods is None:
            mods = stats.weapon_mods = {}
        mods[weapon_key] = mod_key
        # Anything built from the old weapon is now wrong.
        if getattr(stats, '_modded', None):
            stats._modded.pop(weapon_key, None)
    return apply


LANTERN_BOONS = [
    Boon('reach', 'REACH',
         'The lantern throws half again as far.',
         LANTERN, palette.LIGHT_WARM, _lantern('lantern_mult', 1.5)),

    Boon('depth', 'DEPTH',
         'It holds twice what it did.',
         LANTERN, palette.LIGHT_DEEP, _lantern('fuel_max_mult', 2.0)),

    Boon('edge', 'EDGE',
         'The rim of your light burns what stands in it.',
         LANTERN, palette.LIGHT_CORE, _edge),
]

POWER_BOONS = [
    Boon('bulwark', 'THE WEIGHT OF IT',
         'Half again your health, and it stays with you.',
         POWER, palette.HEAL,
         lambda s: setattr(s, 'max_hp', s.max_hp * 1.5)),

    Boon('edgeofnight', 'THE EDGE OF NIGHT',
         'A third more damage to everything you carry.',
         POWER, palette.DAMAGE,
         lambda s: setattr(s, 'damage_mult', s.damage_mult * 1.34)),

    Boon('surefoot', 'SUREFOOT',
         'Another dash, and they come back quicker.',
         POWER, palette.DASH_TRAIL,
         lambda s: (setattr(s, 'dash_charges', s.dash_charges + 1),
                    setattr(s, 'dash_cooldown_mult',
                            s.dash_cooldown_mult * 0.72))),
]


def offer(stats, rng, count=3):
    """Three boons for a boss just killed.

    Weighted so a lantern boon is the likeliest thing to appear and a weapon
    mod only shows for a weapon the run is actually carrying - a mod for a
    gun you do not have is a wasted slot, and with six weapons in the game
    that would be most of them.
    """
    pool = []
    taken = set(getattr(stats, 'boons', ()))

    for boon in LANTERN_BOONS:
        if boon.key not in taken:
            pool.append((boon, 3.2))

    modded = set(getattr(stats, 'weapon_mods', {}) or {})
    carried = [k for k in (getattr(stats, 'weapons', None) or ['lance'])
               if k not in modded]
    for weapon_key in carried:
        weapon = WEAPONS_BY_KEY.get(weapon_key)
        if weapon is None:
            continue
        for mod in MODS:
            key = f'{weapon_key}:{mod.key}'
            if key in taken:
                continue
            pool.append((Boon(key, f'{weapon.name} - {mod.name}', mod.blurb,
                              MOD, weapon.color, _mod_apply(weapon_key,
                                                            mod.key),
                              weapon=weapon_key), 1.0))

    for boon in POWER_BOONS:
        if boon.key not in taken:
            pool.append((boon, 2.0))

    out = []
    for _ in range(min(count, len(pool))):
        pick = rng.weighted(pool)
        out.append(pick)
        pool = [(b, w) for b, w in pool if b is not pick]
        # Only one weapon mod on offer at a time. Three mods for three
        # different guns is a choice about which gun, not about the run.
        if pick.kind == MOD:
            pool = [(b, w) for b, w in pool if b.kind != MOD]
    return out


def grant(stats, boon):
    boon.apply(stats)
    if not hasattr(stats, 'boons'):
        stats.boons = []
    stats.boons.append(boon.key)
