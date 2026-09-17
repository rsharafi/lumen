"""Building the chambers behind a floor's rooms, and holding the right few.

A floor is eight to fourteen rooms and a chamber costs 80-190 ms to generate
and bake, so neither extreme works: building them all on arrival is two
seconds of nothing, and building each one as the player reaches its door is a
tenth of a second of nothing every time they walk through one, which is worse
because it happens over and over.

So they are built *ahead*, on a worker, while the player is busy in the room
they are already in - which is twenty to forty seconds if there is a fight in
it and several even if there is not. By the time a door is opened the room
behind it has usually been sitting ready for half a minute.

Held, too, rather than kept: a baked chamber is four images and several
megabytes, and a floor's worth of them is not a working set. The cache keeps
the room you are in, everything it opens onto, and a couple you were in
recently - so backtracking is free and the floor as a whole is not resident.
"""

import threading

from . import art, ascension
from . import level as level_mod
from . import rng as rng_mod
from .floorplan import (BOSS, CACHE, COMBAT, DESCENT, ELITE, ENTRANCE,
                        GAUNTLET, HEARTH, SHOP, SHRINE)

#: How big a chamber each kind of room gets. A reward room is small because
#: it is a place you stand in for ten seconds, and a fight is bigger because
#: it needs somewhere to back into. The gauntlet gets the largest non-boss
#: footprint on purpose: it is the room that asks for space.
SIZE_FOR_KIND = {
    ENTRANCE: 'small',
    CACHE: 'small',
    SHOP: 'small',
    SHRINE: 'small',
    HEARTH: 'small',
    DESCENT: 'small',
    COMBAT: 'medium',
    ELITE: 'large',
    GAUNTLET: 'large',
    BOSS: 'boss',
}

#: How many braziers a room of each kind is worth. The entrance and the
#: hearth are where the light comes back; a fight has one at most, so holding
#: ground for it is a decision rather than a formality.
BRAZIERS_FOR_KIND = {
    ENTRANCE: 2,
    HEARTH: 3,
    CACHE: 1,
    SHOP: 1,
    SHRINE: 1,
    DESCENT: 1,
    COMBAT: 1,
    ELITE: 1,
    GAUNTLET: 0,
    BOSS: 3,
}

#: Rooms kept baked besides the current one and its neighbours. Two is enough
#: that stepping into a corridor room and straight back out again never waits.
KEEP_RECENT = 2


class RoomBuilder:
    """Builds a floor's chambers, ahead of the player where it can."""

    def __init__(self, plan, seed, rules=()):
        self.plan = plan
        self.seed = seed
        #: Ascension rules in force, so FEWER FIRES can reach the generator.
        self.rules = set(rules)
        self._built = {}                 # room id -> Level
        self._lock = threading.Lock()
        self._queue = []
        self._thread = None
        self._recent = []
        self.built_count = 0
        self.waited_ms = 0.0
        self.waits = 0

    # ------------------------------------------------------------- build --
    def _make(self, room):
        """Generate one chamber. Safe to call from the worker."""
        # Its own generator, seeded from the room, so a room bakes to the
        # same stone whether it was built ahead of time or in a hurry - and
        # so two threads never draw from one stream.
        rng = rng_mod.Rng((self.seed * 1000003 + room.seed) & 0x7FFFFFFF)
        return level_mod.generate(
            self.plan.depth, rng,
            boss=(room.kind == BOSS),
            doors=tuple(room.doors.keys()),
            size=SIZE_FOR_KIND.get(room.kind, 'medium'),
            seed=room.seed,
            braziers=ascension.brazier_count(
                self.rules, BRAZIERS_FOR_KIND.get(room.kind, 1)),
        )

    def ready(self, room):
        """The chamber for `room` if it is already built, else None.

        The non-blocking half of `level_for`, and the one the game plays
        through. Nothing on a frame may wait for a chamber: at the display's
        own pixel density a chamber is between half a second and a second and
        a half of PIL, so a frame that waits for one is not a hitch, it is the
        game stopping.
        """
        with self._lock:
            got = self._built.get(room.id)
        if got is not None and level_mod.needs_bake(got):
            # Built, but for a render scale that has since changed. Not ready:
            # it would draw as an empty room. The worker re-rasterises it.
            self.hurry(room)
            return None
        if got is not None:
            self._touch(room.id)
        return got

    def hurry(self, room):
        """Ask for `room` next, ahead of everything else in the queue."""
        with self._lock:
            built = self._built.get(room.id)
            if built is not None and not level_mod.needs_bake(built):
                return
            if room.id in self._queue:
                self._queue.remove(room.id)
            self._queue.insert(0, room.id)
        self._ensure_worker()

    def level_for(self, room):
        """The chamber for `room`, building it here and now if it is not up.

        The blocking path, kept for the two places that can afford it: the
        floor's entrance, which is built on a worker while the offering is
        still on screen, and the fade-to-black fallback for a doorway that
        cannot be lined up. It is counted so the tools can say how often the
        worker was too slow.
        """
        with self._lock:
            got = self._built.get(room.id)
        if got is not None:
            # The same chamber, re-rasterised if a resize has left it behind -
            # not a new one, which would forget everything that happened in it.
            if level_mod.needs_bake(got):
                level_mod.rebake(got)
            self._touch(room.id)
            return got

        import time
        t0 = time.perf_counter()
        made = self._make(room)
        with self._lock:
            # The worker may have finished the same room while we were in
            # here. Its copy is the one anything else already refers to.
            existing = self._built.get(room.id)
            if existing is not None:
                made = existing
            else:
                self._built[room.id] = made
                self.built_count += 1
        self.waited_ms += (time.perf_counter() - t0) * 1000.0
        self.waits += 1
        self._touch(room.id)
        return made

    def _touch(self, rid):
        if rid in self._recent:
            self._recent.remove(rid)
        self._recent.append(rid)

    # ---------------------------------------------------------- prefetch --
    def prefetch(self, rooms):
        """Queue rooms to be built on the worker. Cheap and idempotent."""
        with self._lock:
            for room in rooms:
                if room.id in self._built or room.id in self._queue:
                    continue
                self._queue.append(room.id)
        self._ensure_worker()

    def refresh(self):
        """Re-rasterise, on the worker, every built chamber a resize left behind.

        Called after the render scale changes. The chambers keep everything
        that happened in them; only their layers are redone.
        """
        with self._lock:
            for rid, level in self._built.items():
                if rid not in self._queue and level_mod.needs_bake(level):
                    self._queue.append(rid)
        self._ensure_worker()

    def _ensure_worker(self):
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            if running or not self._queue:
                return
            self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self):
        while True:
            with self._lock:
                if not self._queue:
                    return
                rid = self._queue.pop(0)
                existing = self._built.get(rid)
                if existing is not None and not level_mod.needs_bake(existing):
                    continue
                room = self.plan.rooms[rid]
            art.begin_bake()
            if existing is not None:
                level_mod.rebake(existing)
                continue
            made = self._make(room)
            with self._lock:
                if rid not in self._built:
                    self._built[rid] = made
                    self.built_count += 1

    def pending(self):
        with self._lock:
            return len(self._queue)

    # ------------------------------------------------------------- evict --
    def keep_set(self, room):
        """Which rooms are worth holding, given the player is in `room`."""
        keep = {room.id}
        keep.update(room.doors.values())
        for rid in reversed(self._recent):
            if len(keep) >= len(room.doors) + 1 + KEEP_RECENT:
                break
            keep.add(rid)
        return keep

    def trim(self, room):
        """Drop chambers outside the keep set, and free their sprites.

        Returns the art keys still in use, which is what
        `art.clear_level_cache` wants: it clears around a chamber rather than
        through it, and a room being drawn behind a menu is still a room.
        """
        keep = self.keep_set(room)
        with self._lock:
            for rid in [k for k in self._built if k not in keep]:
                self._built.pop(rid, None)
                self.plan.rooms[rid].level = None
            self._recent = [r for r in self._recent if r in keep]
            alive = list(self._built.values())
        live_keys = []
        for lv in alive:
            live_keys.extend(lv.art_keys)
        art.clear_level_cache(keep=live_keys)
        return live_keys

    def release(self):
        """Give up everything; the floor is over."""
        with self._lock:
            self._queue = []
            self._built = {}
            self._recent = []
