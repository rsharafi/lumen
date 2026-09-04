"""The deeper dark: what the vault does once you have beaten it.

Winning ended the game. The Vigil caps by design - no node scales without
limit, because a hundred banked runs must not trivialise floor one - so a
player who reached the bottom had nothing left to reach for, and a roguelite
whose reward for mastery is that it stops is a roguelite people play once.

So the vault offers to be worse. Beat a tier and the next one unlocks; each
adds **one named rule** on top of everything below it, so tier six is playing
under six of them at once.

## Why rules and not multipliers

The obvious ladder is a difficulty slider - enemies with more health, doing
more damage, at every rung. It is also the worst one, for a reason worth
stating: *you cannot see it*. Twenty per cent more enemy health is
indistinguishable from a slightly unlucky run, so a player climbing that
ladder is not learning anything, they are grinding a number they cannot
perceive against a wall they cannot see.

Every tier here is a rule the player can name after one room. THE LONGER DARK
is not "-25% fuel", it is *the lantern burns down faster and you can feel it
by the second room*. FEWER FIRES is a brazier that is not there. THE VAULT
REMEMBERS is a door shutting behind you that did not shut last time.

And every one of them is about light or about what light costs, because that
is the only subject this game has. A tier that added more bullets would be a
tier from a different game.
"""

from . import palette

#: The highest tier the ladder goes to. Eight is chosen so that the last two
#: are genuinely unreasonable rather than merely hard - there should be a rung
#: most players never clear.
MAX_TIER = 8


class Tier:
    __slots__ = ('index', 'name', 'blurb', 'color')

    def __init__(self, index, name, blurb, color=None):
        self.index = index
        self.name = name
        self.blurb = blurb
        self.color = color or palette.UI_ACCENT


TIERS = [
    Tier(0, 'THE VAULT', 'As it was built.', palette.UI_DIM),

    Tier(1, 'THE LONGER DARK',
         'The lantern burns a quarter faster.', palette.LIGHT_WARM),

    Tier(2, 'FEWER FIRES',
         'One less brazier in every room.', palette.LIGHT_DEEP),

    Tier(3, 'THE VAULT REMEMBERS',
         'A room you go back into seals itself again.', palette.WARD),

    Tier(4, 'HUNGRIER',
         'Elites from the first floor, and twice as many.',
         palette.WARDEN_EYE),

    Tier(5, 'THE TOLL',
         "The Ferryman's prices rise by half.", palette.XP),

    Tier(6, 'NOTHING WASTED',
         'Descending no longer refills the lantern.', palette.LIGHT_CORE),

    Tier(7, 'DEEPER STILL',
         'Every floor spawns as though it were five lower.',
         palette.CRAWLER_EYE),

    Tier(8, 'THE LAST DARK',
         'The lantern can collapse to almost nothing.', palette.UI_DANGER),
]

BY_INDEX = {t.index: t for t in TIERS}


def tier(index):
    return BY_INDEX.get(max(0, min(MAX_TIER, int(index))), TIERS[0])


def rules_for(index):
    """Every rule active at `index`, as a set of tier numbers.

    Cumulative: tier six is playing under one through six. Returned as a set
    so every site below reads as `if 3 in rules` rather than as an inequality
    nobody can check at a glance.
    """
    return set(range(1, max(0, min(MAX_TIER, int(index))) + 1))


# --------------------------------------------------------------------------
# What each rule actually does
#
# Gathered here rather than scattered across the modules they touch, so the
# whole difficulty of a run can be read in one place - and so a tier that
# stops doing anything is visible as an unused branch rather than as a rule
# that quietly went missing.
# --------------------------------------------------------------------------
def lantern_drain(rules, base):
    """THE LONGER DARK."""
    return base * (1.25 if 1 in rules else 1.0)


def brazier_count(rules, count):
    """FEWER FIRES. Never below zero, and never below one in a boss room."""
    return max(0, count - 1) if 2 in rules else count


def reseals(rules):
    """THE VAULT REMEMBERS."""
    return 3 in rules


def elite_chance(rules, base, depth):
    """HUNGRIER."""
    if 4 not in rules:
        return base
    # Doubled, and the floor-two gate lifted: the first room of the run can
    # carry one.
    return min(0.85, max(base, 0.08) * 2.0)


def price_scale(rules):
    """THE TOLL."""
    return 1.5 if 5 in rules else 1.0


def floor_entry_fuel(rules, base):
    """NOTHING WASTED."""
    return 0.0 if 6 in rules else base


def spawn_depth(rules, depth):
    """DEEPER STILL."""
    return depth + 5 if 7 in rules else depth


def lantern_floor(rules, base):
    """THE LAST DARK. The minimum the lantern can shrink to."""
    return base * 0.45 if 8 in rules else base


def describe(index):
    """Every rule in force at `index`, newest first. For the title screen."""
    return [BY_INDEX[i] for i in sorted(rules_for(index), reverse=True)]


def unlocked(save_data):
    """The highest tier this save may choose.

    One above the highest cleared, so beating a tier is what opens the next -
    and capped, so the ladder ends.
    """
    cleared = int(save_data.get('ascension_cleared', -1))
    return max(0, min(MAX_TIER, cleared + 1))


def record_win(save_data, index):
    """Note that `index` was beaten. Returns True if it opened a new rung."""
    index = max(0, min(MAX_TIER, int(index)))
    before = int(save_data.get('ascension_cleared', -1))
    if index > before:
        save_data['ascension_cleared'] = index
        return True
    return False
