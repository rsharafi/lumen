"""The shape of a floor: rooms on a grid, and the doors between them.

A floor used to be one chamber. It is a graph now, and this module is that
graph and nothing else - no geometry, no sprites, no simulation. A `Room`
knows where it sits on an integer grid, what it is for, and which of its four
sides has a door. The chamber behind a room is built later and elsewhere
(`level.generate`), which is what keeps this file cheap enough to generate ten
thousand floors in a test and eyeball the results.

The generator builds a **critical path** from the entrance to the descent and
hangs **branches** off it. That is a deliberate choice over a maze. A maze
offers the player no decision, because you cannot tell a detour from the way
forward until you have already walked it; a path with visible branches asks
the only question this game wants to ask - *is that dead end worth the fuel?*

Two invariants hold for every floor this returns, and `validate` checks them:

* every room is reachable from the entrance, and the descent is among them;
* every dead end is a reward, never a fight. A spur that costs light and pays
  nothing is a trap, and the map is unreadable if some spurs are traps.
"""

from .config import BOSS_FLOORS

# --------------------------------------------------------------------- kinds
ENTRANCE = 'entrance'
COMBAT = 'combat'
ELITE = 'elite'
CACHE = 'cache'
SHOP = 'shop'
SHRINE = 'shrine'
HEARTH = 'hearth'
GAUNTLET = 'gauntlet'
DESCENT = 'descent'
BOSS = 'boss'

#: Kinds that seal their doors until the last thing in them is dead.
HOSTILE = frozenset((COMBAT, ELITE, GAUNTLET, BOSS))

#: Kinds that pay the player for walking to them. Every dead end is one of
#: these; see the module docstring.
REWARD = frozenset((CACHE, SHOP, SHRINE, HEARTH, GAUNTLET))

# --------------------------------------------------------------------- sides
NORTH, SOUTH, EAST, WEST = 'n', 's', 'e', 'w'
SIDES = (NORTH, SOUTH, EAST, WEST)
STEP = {NORTH: (0, -1), SOUTH: (0, 1), EAST: (1, 0), WEST: (-1, 0)}
OPPOSITE = {NORTH: SOUTH, SOUTH: NORTH, EAST: WEST, WEST: EAST}

#: How wide the room grid is allowed to get. Seven squared holds the largest
#: floor twice over, and bounding it keeps the drawn map a predictable shape.
BOUND = 7


def act_of(depth):
    """1, 2 or 3. Acts close on a boss; see `config.BOSS_FLOORS`."""
    for i, floor in enumerate(BOSS_FLOORS):
        if depth <= floor:
            return i + 1
    return len(BOSS_FLOORS)


class Room:
    __slots__ = ('id', 'col', 'row', 'kind', 'doors', 'seed', 'on_path',
                 'level', 'cleared', 'visited', 'seen', 'spent', 'payload')

    def __init__(self, rid, col, row, seed):
        self.id = rid
        self.col = col
        self.row = row
        #: Set once, by `_assign_kinds`, which is the only authority on it.
        self.kind = None
        #: side -> neighbouring room id.
        self.doors = {}
        #: Fixed at plan time so a room bakes identically whenever it is
        #: built - the bake runs on a worker, and "whenever" is not a
        #: rhetorical flourish.
        self.seed = seed
        self.on_path = False

        #: Filled in by the world as the floor is played.
        self.level = None
        self.cleared = False
        self.visited = False
        self.seen = False
        #: Rewards are once-only; a shop that restocks on re-entry is not a
        #: shop. Set when the room's contents have been taken.
        self.spent = False
        #: Kind-specific contents, decided when the room is first entered so
        #: an offer reflects the build that walks in rather than the one that
        #: started the floor.
        self.payload = None

    @property
    def hostile(self):
        return self.kind in HOSTILE

    @property
    def degree(self):
        return len(self.doors)

    def __repr__(self):
        return f'Room({self.id}, {self.col},{self.row}, {self.kind})'


class FloorPlan:
    def __init__(self, depth):
        self.depth = depth
        self.act = act_of(depth)
        self.rooms = {}
        self.by_cell = {}
        self.entrance = None
        self.descent = None
        self.current = None

    # --------------------------------------------------------------- build --
    def add(self, col, row, seed):
        rid = len(self.rooms)
        room = Room(rid, col, row, seed)
        self.rooms[rid] = room
        self.by_cell[(col, row)] = rid
        return room

    def link(self, a, b):
        """Put a door between two grid-adjacent rooms, both ways."""
        dc = b.col - a.col
        dr = b.row - a.row
        for side, (sc, sr) in STEP.items():
            if (sc, sr) == (dc, dr):
                a.doors[side] = b.id
                b.doors[OPPOSITE[side]] = a.id
                return True
        return False

    # -------------------------------------------------------------- queries --
    def room(self, rid):
        return self.rooms[rid]

    def at(self, col, row):
        rid = self.by_cell.get((col, row))
        return self.rooms[rid] if rid is not None else None

    def neighbour(self, room, side):
        rid = room.doors.get(side)
        return self.rooms[rid] if rid is not None else None

    def extent(self):
        """(min_col, min_row, cols, rows) covering every room."""
        cols = [r.col for r in self.rooms.values()]
        rows = [r.row for r in self.rooms.values()]
        return (min(cols), min(rows),
                max(cols) - min(cols) + 1, max(rows) - min(rows) + 1)

    def distances(self, start_id):
        """Rooms-away from `start_id`, over doors."""
        out = {start_id: 0}
        frontier = [start_id]
        while frontier:
            nxt = []
            for rid in frontier:
                here = out[rid] + 1
                for other in self.rooms[rid].doors.values():
                    if other not in out:
                        out[other] = here
                        nxt.append(other)
            frontier = nxt
        return out

    def reveal_from(self, room):
        """Entering a room shows it, and hints at what it touches.

        The map is a record of what you have seen, not a floorplan handed to
        you at the door - so a neighbour becomes a marked outline with no kind
        attached, and only turns into a labelled room once you walk in.
        """
        room.visited = True
        room.seen = True
        for rid in room.doors.values():
            self.rooms[rid].seen = True

    @property
    def cleared_count(self):
        return sum(1 for r in self.rooms.values()
                   if r.hostile and r.cleared)

    @property
    def hostile_count(self):
        return sum(1 for r in self.rooms.values() if r.hostile)

    def fully_cleared(self):
        """Every room entered and every fight finished - the clear bonus."""
        return all(r.visited and (not r.hostile or r.cleared)
                   for r in self.rooms.values())


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------
def _walk(rng, length, occupied, start):
    """A self-avoiding walk of exactly `length` cells, or None.

    Depth-first with backtracking rather than a greedy walk, because a greedy
    one paints itself into a corner and returns a path shorter than asked
    for - and a floor whose critical path came out two rooms long is a floor
    that is over before it starts.

    Turns are weighted slightly above straights so the result bends. A path
    that runs in a line reads as a corridor on the map and gives the eye
    nothing to remember it by.
    """
    path = [start]
    seen = set(occupied) | {start}

    def step(prev_side):
        if len(path) >= length:
            return True
        c, r = path[-1]
        table = []
        for side in SIDES:
            dc, dr = STEP[side]
            cell = (c + dc, r + dr)
            if cell in seen:
                continue
            if not (0 <= cell[0] < BOUND and 0 <= cell[1] < BOUND):
                continue
            table.append((side, 2.0 if side == prev_side else 3.0))
        while table:
            side = rng.weighted(table)
            table = [t for t in table if t[0] != side]
            dc, dr = STEP[side]
            cell = (c + dc, r + dr)
            path.append(cell)
            seen.add(cell)
            if step(side):
                return True
            path.pop()
            seen.discard(cell)
        return False

    return path if step(None) else None


def _critical_path(rng, length):
    """A walk of `length` cells, retried from fresh starts until one fits."""
    for _ in range(24):
        start = (rng.randint(1, BOUND - 2), rng.randint(1, BOUND - 2))
        path = _walk(rng, length, set(), start)
        if path is not None:
            return path
    # Unreachable in practice on a 7x7 grid, but a floor must exist.
    return [(i % BOUND, i // BOUND) for i in range(length)]


def _grow_branches(plan, rng, budget, want_dead_ends):
    """Hang spurs off the floor, one room at a time.

    Chains of one, two or three. Short spurs are preferred while the floor
    still owes the player dead ends, because a three-room chain costs three
    rooms and yields a single reward at the end of it.
    """
    placed = 0
    guard = 0
    while placed < budget and guard < 200:
        guard += 1
        dead_ends = sum(1 for r in plan.rooms.values()
                        if r.degree == 1 and r.id != plan.entrance
                        and r.id != plan.descent)
        if dead_ends < want_dead_ends:
            table = [(1, 5.0), (2, 1.5)]
        else:
            table = [(1, 2.0), (2, 2.5), (3, 1.2)]
        length = min(rng.weighted(table), budget - placed)

        # Root the spur anywhere with a free side, except the two rooms whose
        # meaning depends on their position: a branch off the entrance is a
        # detour before the floor has started, and one off the descent is a
        # detour the player will never take.
        roots = [r for r in plan.rooms.values()
                 if r.id not in (plan.entrance, plan.descent)
                 and any((r.col + STEP[s][0], r.row + STEP[s][1])
                         not in plan.by_cell
                         and 0 <= r.col + STEP[s][0] < BOUND
                         and 0 <= r.row + STEP[s][1] < BOUND
                         for s in SIDES)]
        if not roots:
            break
        room = rng.choice(roots)
        for _ in range(length):
            free = [s for s in SIDES
                    if (room.col + STEP[s][0], room.row + STEP[s][1])
                    not in plan.by_cell
                    and 0 <= room.col + STEP[s][0] < BOUND
                    and 0 <= room.row + STEP[s][1] < BOUND]
            if not free:
                break
            side = rng.choice(free)
            cell = (room.col + STEP[side][0], room.row + STEP[side][1])
            nxt = plan.add(cell[0], cell[1], rng.randint(0, 2 ** 31 - 1))
            plan.link(room, nxt)
            room = nxt
            placed += 1
    return placed


def _add_shortcuts(plan, rng, critical_len):
    """Join a few rooms that touch on the grid but not through a door.

    Loops are what stop a floor being a queue. Knowing the map well enough to
    route around a fight should be worth something - but only something: a
    shortcut is refused if it would bring the descent more than one room
    closer than the critical path meant it to be.
    """
    pairs = []
    for room in plan.rooms.values():
        for side in (EAST, SOUTH):          # each pair considered once
            cell = (room.col + STEP[side][0], room.row + STEP[side][1])
            other = plan.at(*cell)
            if other is None or side in room.doors:
                continue
            pairs.append((room, other, side))

    added = 0
    # `distances` counts doors, not rooms: a spine of N rooms is N-1 doors
    # from end to end. Allowing N-2 lets a shortcut save exactly one room,
    # which is the whole of what it is for - knowing the floor well enough to
    # skip a fight should be worth something, and worth only something.
    floor_min = max(1, critical_len - 2)
    for room, other, _side in rng.shuffled(pairs):
        if added >= 2:
            break
        if not rng.chance(0.42):
            continue
        plan.link(room, other)
        if plan.distances(plan.entrance).get(plan.descent, 0) < floor_min:
            # Too generous - take it back out.
            for side, rid in list(room.doors.items()):
                if rid == other.id:
                    del room.doors[side]
                    del other.doors[OPPOSITE[side]]
            continue
        added += 1
    return added


def _assign_kinds(plan, rng, depth):
    """Give every room a job.

    Dead ends are rewards without exception - that is the invariant the map's
    readability rests on. The critical path is the floor's spine and is
    fights, because the way through should be earned.
    """
    entrance = plan.room(plan.entrance)
    descent = plan.room(plan.descent)
    entrance.kind = ENTRANCE
    descent.kind = DESCENT

    dead_ends = [r for r in plan.rooms.values()
                 if r.degree == 1 and r.id not in (plan.entrance, plan.descent)]
    rng_ends = rng.shuffled(dead_ends)

    # --- what this floor is allowed to offer -------------------------------
    # Two lists, because "the floor should usually have a shrine" and "the
    # floor before a boss *will* have a hearth" are different promises and
    # one of them was quietly being broken. A guaranteed kind takes a spur
    # first, and takes a room on the spine if there is no spur to take.
    promised = []
    optional = []

    # Guaranteed on the floor before a boss. Walking into an act boss on
    # fumes is a fight decided before it starts.
    pre_boss = (depth + 1) in BOSS_FLOORS
    if pre_boss:
        promised.append(HEARTH)

    # Not every floor, and never the first: embers are also the between-run
    # currency, and a Ferryman on every floor turns "bank it or spend it"
    # into "spend it", which is not a decision.
    if depth >= 2 and rng.chance(0.45):
        optional.append(SHOP)
    # Ranked above things likelier than it, because it is the only kind that
    # cannot fall back to the spine when it misses out on a spur. Everything
    # below it gets a second chance; this does not.
    if act_of(depth) >= 2 and rng.chance(0.48):
        optional.append(GAUNTLET)
    if not pre_boss and rng.chance(0.18 + 0.015 * depth):
        optional.append(HEARTH)
    if depth >= 3 and rng.chance(0.50):
        optional.append(SHRINE)

    # One dead end always stays a cache. A floor whose every spur is a
    # special room has no baseline, and the player stops being able to guess
    # what a spur is worth before walking it. A promise outranks that: if the
    # only spur on the floor has to be the hearth, it is the hearth.
    spurs = len(rng_ends)
    for kind in promised:
        if rng_ends:
            rng_ends.pop().kind = kind
    fits = max(0, spurs - len(promised) - 1)
    placed_optional = optional[:fits]
    for kind in placed_optional:
        rng_ends.pop().kind = kind
    for room in rng_ends:
        room.kind = CACHE

    #: Kinds this floor owes the player and found no spur for.
    overflow = [k for k in promised if k not in
                {r.kind for r in dead_ends}] + optional[fits:]

    # --- the spine ----------------------------------------------------------
    middle = [r for r in plan.rooms.values()
              if r.on_path and r.id not in (plan.entrance, plan.descent)]

    # A reward that found no spur stands on the way through instead. The
    # invariant this file rests on is that every *dead end* is a reward - not
    # that every reward is a dead end, which is a much stronger claim and the
    # one that was quietly strangling shrines and gauntlets. A Ferryman you
    # walk past on the way down is a perfectly good Ferryman.
    #
    # The gauntlet is the one thing that must never land here: it is a hard
    # room the player *chose*, and a hard room on the only way down is not an
    # offer, it is a toll.
    spillable = [k for k in overflow if k != GAUNTLET]
    # A promise is spilled however tight the floor is; an optional room only
    # when there is fighting left over afterwards, because the spine is what
    # the floor's required content *is*.
    room_for = len(middle) - 2
    for kind in spillable:
        if room_for <= 0 and kind not in promised:
            break
        if not middle:
            break
        spot = rng.choice(middle)
        spot.kind = kind
        middle = [r for r in middle if r is not spot]
        room_for -= 1

    elites = 0
    if depth >= 3:
        elites = 1
    if depth >= 12:
        elites = 2
    for room in rng.shuffled(middle)[:elites]:
        room.kind = ELITE
    for room in middle:
        if room.kind not in (ELITE,):
            room.kind = COMBAT

    # Rooms that are neither on the path nor a dead end - the middle of a
    # two- or three-room spur. They are walked through on the way to a
    # reward, so they are fights, which is what makes the deep spur cost
    # something the shallow one does not.
    for room in plan.rooms.values():
        if room.kind is None:
            room.kind = COMBAT


def generate(depth, rng):
    """A floor's rooms and doors. Deterministic in `rng`."""
    plan = FloorPlan(depth)

    if depth in BOSS_FLOORS:
        # Two rooms, and the first is a threshold rather than a fight: a
        # brazier to stand in and a moment to decide you are ready. The
        # arrival needs somewhere to be arrived at.
        a = plan.add(0, 1, rng.randint(0, 2 ** 31 - 1))
        b = plan.add(0, 0, rng.randint(0, 2 ** 31 - 1))
        plan.link(a, b)
        a.kind = ENTRANCE
        b.kind = BOSS
        a.on_path = b.on_path = True
        plan.entrance = a.id
        plan.descent = b.id
        plan.current = a.id
        return plan

    # The spine grows by a room an act, and jitters by one either side of
    # that. The jitter is the point: a spine that is always exactly four
    # rooms teaches the player how far the descent is before they have taken
    # a step, and a floor whose length is known is a floor with no tension in
    # it. The floor around the spine grows faster than the spine does,
    # because what should change most between floor 1 and floor 20 is how
    # much you are choosing to walk past.
    act = act_of(depth)
    critical_len = 3 + act + rng.weighted([(0, 2.4), (1, 2.0), (2, 0.7)])
    total = critical_len + 3 + depth // 6 + rng.randint(0, 2)
    cells = _critical_path(rng, critical_len)

    for col, row in cells:
        room = plan.add(col, row, rng.randint(0, 2 ** 31 - 1))
        room.on_path = True
    for i in range(len(cells) - 1):
        plan.link(plan.at(*cells[i]), plan.at(*cells[i + 1]))

    plan.entrance = plan.at(*cells[0]).id
    plan.descent = plan.at(*cells[-1]).id
    plan.current = plan.entrance

    # Dead ends are the floor's whole offer, and they were the bottleneck:
    # with under two of them a floor could not fit a shop, a shrine and a
    # cache, so the rarer rooms were being squeezed out of existence rather
    # than being rare by design.
    _grow_branches(plan, rng, total - critical_len,
                   want_dead_ends=2 + depth // 6)
    _add_shortcuts(plan, rng, critical_len)
    _assign_kinds(plan, rng, depth)
    return plan


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------
def validate(plan):
    """Every way a floor can be malformed, as a list of strings.

    Called by the test harness on every floor it generates. An empty list is
    a floor worth playing; anything else is a bug that would otherwise show
    up as a player standing in a room with no way out.
    """
    faults = []
    rooms = plan.rooms

    if plan.entrance is None or plan.descent is None:
        return ['no entrance or no descent']
    if plan.entrance == plan.descent and len(rooms) > 1:
        faults.append('entrance is the descent')

    reach = plan.distances(plan.entrance)
    if len(reach) != len(rooms):
        faults.append(f'{len(rooms) - len(reach)} room(s) unreachable')
    if plan.descent not in reach:
        faults.append('descent unreachable')

    for room in rooms.values():
        if not room.doors:
            faults.append(f'room {room.id} has no doors')
        if room.kind is None:
            faults.append(f'room {room.id} has no kind')
        for side, rid in room.doors.items():
            other = rooms.get(rid)
            if other is None:
                faults.append(f'room {room.id} door {side} dangles')
                continue
            if other.doors.get(OPPOSITE[side]) != room.id:
                faults.append(f'door {room.id}->{rid} is one-way')
            dc, dr = STEP[side]
            if (room.col + dc, room.row + dr) != (other.col, other.row):
                faults.append(f'door {room.id}->{rid} is not between neighbours')

        if (room.degree == 1 and room.id not in (plan.entrance, plan.descent)
                and room.kind not in REWARD):
            faults.append(f'dead end {room.id} is a {room.kind}, not a reward')

    cells = [(r.col, r.row) for r in rooms.values()]
    if len(set(cells)) != len(cells):
        faults.append('two rooms share a cell')

    # A descent you can see from the entrance is a floor that did not happen.
    # Boss floors are exactly that on purpose - a threshold room and the
    # thing itself - so they are two rooms and exempt.
    if len(rooms) > 2:
        if plan.descent in rooms[plan.entrance].doors.values():
            faults.append('the descent opens off the entrance')
        if reach.get(plan.descent, 0) < 2:
            faults.append('the descent is fewer than two doors in')

    # Kinds there can only be one of. A second Ferryman on a floor is a
    # second chance to spend, which is not what the currency is for.
    counts = {}
    for room in rooms.values():
        counts[room.kind] = counts.get(room.kind, 0) + 1
    for kind in (ENTRANCE, DESCENT, SHOP, HEARTH, BOSS, SHRINE, GAUNTLET):
        if counts.get(kind, 0) > 1:
            faults.append(f'{counts[kind]} {kind} rooms on one floor')

    if not any(r.hostile for r in rooms.values()):
        faults.append('nothing on the floor to fight')
    return faults


def ascii_map(plan, mark=None):
    """The floor as text, for eyeballing a few hundred of them at once."""
    glyph = {ENTRANCE: 'I', COMBAT: '.', ELITE: 'E', CACHE: '$', SHOP: 'F',
             SHRINE: 'S', HEARTH: 'H', GAUNTLET: 'G', DESCENT: 'V',
             BOSS: 'B', None: '?'}
    c0, r0, cols, rows = plan.extent()
    out = []
    for row in range(r0, r0 + rows):
        top = ''
        mid = ''
        for col in range(c0, c0 + cols):
            room = plan.at(col, row)
            if room is None:
                top += '    '
                mid += '    '
                continue
            north = '|' if NORTH in room.doors else ' '
            top += f' {north}  '
            ch = glyph.get(room.kind, '?')
            if mark is not None and room.id == mark:
                ch = ch.lower() if ch.isupper() else '*'
            mid += f'[{ch}]' + ('-' if EAST in room.doors else ' ')
        out.append(top)
        out.append(mid)
    return '\n'.join(out)
