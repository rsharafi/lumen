"""The Vigil - what survives a run.

Every run started from the same place with the same numbers, which makes a
death a total loss and the twelfth attempt identical to the first. That is
the difference between a roguelike and a roguelite, and this is the missing
half: embers you carried out of the vault are banked, and they buy things
that are still there next time.

Two rules keep it from turning the game into a waiting room. The vault itself
is never gated - every floor and the boss at the bottom are reachable on a
first run with the starting weapon, so nothing stands between a good player
and the ending. And no stat node scales without limit, so a hundred banked
runs cannot trivialise floor one; the largest of them is worth about a fifth
of a single mid-run offering.

What the Vigil buys is mostly *options*: the other two weapons, a redraw at
the offering, an upgrade already in hand before the first door.

Each node is a list of costs, one per rank. Buying rank N costs `costs[N]`,
so the shape of a node's cost curve is a design decision rather than a
formula.
"""

from . import palette


class Node:
    """One purchasable line on the Vigil."""

    def __init__(self, key, name, blurb, costs, apply, color=None,
                 unlocks=None):
        self.key = key
        self.name = name
        self.blurb = blurb
        self.costs = costs
        self.apply = apply
        self.color = color or palette.UI_ACCENT
        # A weapon key this node makes selectable, if any.
        self.unlocks = unlocks

    @property
    def ranks(self):
        return len(self.costs)

    def cost(self, rank):
        """What the next rank costs, or None at full rank."""
        if rank >= len(self.costs):
            return None
        return self.costs[rank]


def _add(attr, amount):
    def apply(stats, rank):
        setattr(stats, attr, getattr(stats, attr) + amount * rank)
    return apply


def _mul(attr, factor):
    def apply(stats, rank):
        setattr(stats, attr, getattr(stats, attr) * (factor ** rank))
    return apply


def _nothing(stats, rank):
    """For nodes whose effect is not a stat - a weapon, a starting floor."""


# The order here is the order they appear on the screen.
ALL = [
    Node('vitality', 'VITALITY',
         'Begin each descent with more health.',
         [40, 90, 170], _add('max_hp', 14.0), palette.HEAL),

    Node('reserve', 'DEEP RESERVE',
         'Begin with a larger lantern.',
         [40, 90, 170], _mul('fuel_max_mult', 1.12), palette.LIGHT_WARM),

    Node('keenness', 'KEENNESS',
         'Begin with sharper shots.',
         [55, 120, 230], _mul('damage_mult', 1.06), palette.DAMAGE),

    Node('fleetness', 'FLEETNESS',
         'Begin a little quicker on your feet.',
         [50, 130], _mul('speed_mult', 1.05), palette.PLAYER_TRIM),

    Node('emberwise', 'EMBERWISE',
         'Embers are worth more, in the vault and out of it.',
         [45, 100, 200], _mul('ember_gain', 1.15), palette.XP),

    Node('foresight', 'FORESIGHT',
         'Begin each run with a redraw at the offering.',
         [70, 180], _add('rerolls', 1), palette.UI_ACCENT),

    Node('warding', 'WARDING',
         'Begin with a ward that turns one blow aside.',
         [90, 220], _add('shield_charges', 1), palette.SHIELD),

    Node('lastlight', 'THE LAST LIGHT',
         'Survive one killing blow, once per descent.',
         [260], _add('revives', 1), palette.LIGHT_CORE),

    Node('scatter', 'SCATTERLIGHT',
         'Unseal the scattergun. Seven shards, and no reach at all.',
         [120], _nothing, palette.SCATTER, unlocks='scatter'),

    Node('coil', 'COILBEAM',
         'Unseal the coil. Hold it, and it goes through everything.',
         [200], _nothing, palette.BEAM, unlocks='coil'),

    Node('kindling', 'KINDLING',
         'Begin each descent already holding one offering.',
         [150, 340], _nothing, palette.LIGHT_WARM),
]

BY_KEY = {n.key: n for n in ALL}

# Weapons that need no unlocking. The first one is always yours.
FREE_WEAPONS = ('lance',)


def ranks(save_data):
    """The `{key: rank}` mapping out of a save, ignoring anything unknown."""
    got = save_data.get('vigil') or {}
    out = {}
    for key, rank in got.items():
        node = BY_KEY.get(key)
        if node is None:
            continue
        try:
            out[key] = max(0, min(node.ranks, int(rank)))
        except (TypeError, ValueError):
            continue
    return out


def spent(save_data):
    """How many embers are already committed, for the summary line."""
    total = 0
    for key, rank in ranks(save_data).items():
        total += sum(BY_KEY[key].costs[:rank])
    return total


def apply_to(stats, save_data):
    """Fold every purchased rank into a fresh run's stats."""
    for key, rank in ranks(save_data).items():
        if rank > 0:
            BY_KEY[key].apply(stats, rank)


def unlocked_weapons(save_data):
    """Which weapon keys this save may choose from."""
    keys = list(FREE_WEAPONS)
    for key, rank in ranks(save_data).items():
        node = BY_KEY[key]
        if rank > 0 and node.unlocks and node.unlocks not in keys:
            keys.append(node.unlocks)
    return keys


def starting_offerings(save_data):
    """How many upgrades a run begins already holding."""
    return ranks(save_data).get('kindling', 0)


def can_afford(save_data, key):
    node = BY_KEY.get(key)
    if node is None:
        return False
    cost = node.cost(ranks(save_data).get(key, 0))
    return cost is not None and save_data.get('embers', 0) >= cost


def buy(save_data, key):
    """Spend on one rank. Returns the updated save, or None if it cannot."""
    if not can_afford(save_data, key):
        return None
    node = BY_KEY[key]
    got = dict(save_data)
    rank = ranks(save_data).get(key, 0)
    cost = node.cost(rank)
    vig = dict(got.get('vigil') or {})
    vig[key] = rank + 1
    got['vigil'] = vig
    got['embers'] = int(got.get('embers', 0)) - cost
    return got


def total_ranks(save_data):
    return sum(ranks(save_data).values())


def max_ranks():
    return sum(n.ranks for n in ALL)
