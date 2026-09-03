"""One floor of a run: simulation and world rendering.

`World` doubles as the context object passed to enemies, so their AI can reach
the player, the level, the projectile pool and the effects systems without
importing any of them.
"""

import gc
import math
import os

from . import draw, gpu
from .draw import drawImage, drawLine, drawPolygon

from . import acts as act_mod
from . import doors as door_mod
from . import hazards as hazard_mod
from . import level as level_mod
from . import rng as rng_mod
from . import motes as motes_mod
from . import (art, audio, boss, enemies as enemy_mod, fixtures as fixture_mod,
               flow as flow_mod, floorplan as plan_mod, level as level_mod,
               lighting, palette, particles as particle_mod,
               pickups as pickup_mod, projectiles as projectile_mod,
               rooms as rooms_mod)
from .config import (BOSS_FLOORS, BRAZIER_IGNITE_FUEL, BRAZIER_REFILL_RANGE,
                     BRAZIER_REFILL_RATE, FLOOR_ENTRY_FUEL,
                     LANTERN_FLARE_DAMAGE, LANTERN_FLARE_KNOCKBACK,
                     LANTERN_FLARE_TIME, SHADOW_FLOOR_MIX, TILE)
from .fx import Camera, Effects
from .mathx import clamp, ease_out_cubic, from_angle, lerp, pulse
from .player import Player

# Seconds you must stand in an open rift before it takes you down.
RIFT_HOLD = 0.45
LANTERN_GLOW = (255, 198, 126)
SHADOW_OPACITY = int(os.environ.get('LUMEN_SHADOW_OPACITY', '100'))

# Test hook: render a frame with no lantern, to diff for light leaks.
NO_LANTERN = bool(os.environ.get('LUMEN_NO_LANTERN'))
# Test hook: leave the wards out of the frame, to price them.
NO_WARDS = bool(os.environ.get('LUMEN_NO_WARDS'))


class Span:
    """Several chambers, offered to the player as one thing to bump into.

    Only alive during a crossing. It presents the same three names the player
    asks a level for - `collide_circle`, `width`, `height` - over the union
    of every chamber currently on screen, so walking from one room into the
    next is not a special case anywhere in the movement code.

    The clamp is the interesting one. A level clamps the player inside its own
    rectangle, which mid-crossing would pin them to the room they are trying
    to leave; the union's rectangle covers both, and the walls do the rest.
    """

    __slots__ = ('parts', 'x0', 'y0', 'width', 'height')

    def __init__(self, parts):
        self.parts = parts
        self.x0 = min(ox for _lv, ox, _oy in parts)
        self.y0 = min(oy for _lv, _ox, oy in parts)
        x1 = max(ox + lv.width for lv, ox, _oy in parts)
        y1 = max(oy + lv.height for lv, _ox, oy in parts)
        self.width = x1
        self.height = y1

    def collide_circle(self, x, y, radius):
        for level, ox, oy in self.parts:
            # Only the chamber whose rectangle actually contains the disc
            # gets a say. Asking all of them would have each one eject the
            # player out of the *other* room's floor, since a point outside a
            # chamber is outside every rectangle it owns.
            if (ox - radius <= x <= ox + level.width + radius
                    and oy - radius <= y <= oy + level.height + radius):
                nx, ny = level.collide_circle(x - ox, y - oy, radius)
                x, y = nx + ox, ny + oy
        return x, y


class Rift:
    """The way down. Opens once the chamber is clear."""

    def __init__(self, x, y):
        self.x = x
        self.y = y
        self.open_t = 0.0
        self.hold = 0.0
        self.entered = False
        # What its light can reach, cast once - it does not move either.
        self.shape = None
        self.edges = None


class World:
    def __init__(self, stats, rng, fxrng, view_w, view_h):
        self.stats = stats
        self.rng = rng
        self.fxrng = fxrng
        self.view_w = view_w
        self.view_h = view_h

        self.player = Player(stats)
        self.particles = particle_mod.ParticleSystem()
        self.projectiles = projectile_mod.ProjectilePool()
        self.pickups = pickup_mod.PickupField()
        self.effects = Effects(fxrng)
        self.camera = Camera(view_w, view_h)

        self.level = None
        self.flow = None
        self.enemies = []
        self.rift = None
        self.depth = 0
        self.is_boss = False
        self.cleared = False
        self.boss_ref = None

        # A floor is a graph of rooms now; `plan` is that graph, `room` is
        # the one being stood in, and `builder` owns the chambers behind
        # them. `cleared` above is this room's, not the floor's.
        self.plan = None
        self.room = None
        self.builder = None
        #: Seconds left before a doorway will answer again. Arriving in a
        #: room puts the player near the door they came through, and without
        #: this a single step back is enough to bounce between two rooms.
        self.door_lock = 0.0
        #: True while a fight has this room's doors sealed.
        self.warded = False
        #: side -> Ward, rebuilt with the room. They hold a cast sweep each.
        #: The one interactable object in a reward room, or None.
        self.fixture = None
        #: Set for one frame when the player has walked into the Ferryman.
        #: Read by the game, which owns screens; the world owns rooms.
        self.pending_shop = False
        #: Set for one frame when the player has walked into a doorway; the
        #: game reads it, plays the transition, and calls `enter_room`.
        self.pending_door = None
        self.rooms_entered = 0
        #: Set while the player is walking out of one room and into the next.
        #: See `begin_crossing` for what is in it and why it exists.
        self.crossing = None

        self.score = 0
        self.embers = 0
        self.kills = 0
        self.run_time = 0.0
        self.floor_time = 0.0
        self.best_streak = 0
        self.streak = 0
        self.streak_timer = 0.0

        self.light_fan = None
        self.light_poly = []
        self.light_radius = 0.0
        self.last_wedges = 0
        self.last_edges = 0
        self.last_motes = 0
        # Marks a fight leaves on the floor. Bounded, oldest first out: a
        # long fight in one room should not end up drawing a hundred sprites
        # over the same square metre.
        self.decals = []
        #: Standing rot: [x, y, radius, left, total, dps]. See `add_pool`.
        self.pools = []
        #: What this act puts on the floor. See `lumen/hazards.py`.
        self.hazards = hazard_mod.Field()
        self.hunt_timer = 0.0
        self.banner = ''
        self.banner_t = 0.0

        self.draw_marks = {}
        self.motes = motes_mod.Motes(fxrng, view_w, view_h)
        self.overlay = art.screen_overlay(view_w, view_h, 0.94, 0.6, 0.05,
                                          0.1, 4)
        # Set by `Game` from the player's settings.
        self.deferred = True        # the light-buffer pipeline
        self.volumetric = True      # air in the lit cone
        self.wall_glow = 0          # masonry lit regardless of the lantern
        # A boss arrives and dies as an event rather than as a spawn and a
        # corpse. Both are driven from here so the two bosses share them.
        self.boss_intro = 0.0
        self.boss_death = 0.0
        self.boss_corpse = None
        self.prewarm_lantern_sizes()

    def resize(self, view_w, view_h):
        """Adopt a new view size after the window changed."""
        self.view_w = view_w
        self.view_h = view_h
        self.camera.view_w = view_w
        self.camera.view_h = view_h
        if self.level is not None:
            self.camera.set_bounds(self.level.width, self.level.height)
            self.camera.snap_to(self.player.x, self.player.y)
        self.overlay = art.screen_overlay(view_w, view_h, 0.94, 0.6, 0.05,
                                          0.1, 4)
        self.motes.resize(view_w, view_h)
        # A resize that changed the render scale emptied the sprite cache, so
        # every lantern size has to be baked again here. Leaving it to the
        # first frame that needs one costs a 4-7 ms rasterisation mid-frame.
        self.prewarm_lantern_sizes()

    def prewarm_lantern_sizes(self):
        """Bake every lantern size this run can reach, so none is built mid-frame."""
        from .config import (LANTERN_RADIUS, LANTERN_RADIUS_MIN,
                             LANTERN_RADIUS_STEP)
        mult = self.stats.lantern_mult
        lo = LANTERN_RADIUS_MIN * mult
        hi = LANTERN_RADIUS * mult
        radii = []
        r = int(lo / LANTERN_RADIUS_STEP) * LANTERN_RADIUS_STEP
        while r <= hi + LANTERN_RADIUS_STEP:
            radii.append(r)
            r += LANTERN_RADIUS_STEP
        art.prewarm_lantern(LANTERN_GLOW, radii)

    # ------------------------------------------------------------- floors --
    def prepare_floor(self, depth):
        """Plan the next floor ahead of time, and build the room it starts in.

        Generating and baking a chamber costs 80-190 ms - several frames -
        and it used to happen at the instant the player chose an offering,
        which is the one moment they are watching for a response. Done while
        the offering is still on screen the same work lands on a static page
        nobody is looking at for motion, and the descent itself is immediate.

        Only the entrance is built here. The rest of the floor is built by
        the builder's own worker once the player is standing in it, which is
        both cheaper and better timed - the floor's shape is not known to be
        worth building until it is being walked.
        """
        # No clearing here: the offering is drawn over the room you just
        # cleared, so releasing its sprites while it is still on screen makes
        # the room vanish behind the cards. The old floor is dropped when the
        # new one is adopted instead.
        #
        # Its own generator, seeded from the world's, so this can run off the
        # main thread without two streams interleaving.
        gen = rng_mod.Rng(self.rng.randint(0, 2 ** 31 - 1))
        plan = plan_mod.generate(depth, gen)
        builder = rooms_mod.RoomBuilder(plan, gen.randint(0, 2 ** 31 - 1))
        first = plan.room(plan.entrance)
        first.level = builder.level_for(first)
        return (depth, depth in BOSS_FLOORS, plan, builder)

    def enter_floor(self, depth, prepared=None):
        """Arrive on a new floor, in the room it starts in."""
        self.depth = depth
        self.is_boss = depth in BOSS_FLOORS

        if self.builder is not None:
            self.builder.release()

        if prepared is not None and prepared[0] == depth:
            _, _, self.plan, self.builder = prepared
        else:
            gen = rng_mod.Rng(self.rng.randint(0, 2 ** 31 - 1))
            self.plan = plan_mod.generate(depth, gen)
            self.builder = rooms_mod.RoomBuilder(
                self.plan, gen.randint(0, 2 ** 31 - 1))

        # Per-floor, as opposed to per-room: the things that should not reset
        # every time the player walks through a door.
        self.player.on_floor_start()
        self.player.refresh_from_stats()
        self.player.add_fuel(FLOOR_ENTRY_FUEL)
        self.floor_time = 0.0
        self.rooms_entered = 0

        # The floor announces itself; the *boss* announces itself when you
        # open its door. Naming it on arrival gave the thing away in a
        # threshold room two chambers early, which spends the arrival before
        # the arrival happens.
        self.set_banner(f'FLOOR {depth}', seconds=2.4)
        self.enter_room(self.plan.room(self.plan.entrance), from_side=None)

        # Once a floor, behind the fade. `unfreeze` first or the previous
        # floor's chambers stay pinned for the rest of the run.
        gc.unfreeze()
        gc.collect()
        gc.freeze()

    # ------------------------------------------------------------- rooms --
    def enter_room(self, room, from_side=None):
        """Stand the player in `room`, building its chamber if need be.

        `from_side` is the side of *this* room the player comes in through,
        which is the opposite of the one they left by. None means they
        arrived from above, on a rift, and start where the chamber says.
        """
        self.room = room
        self.plan.current = room.id
        if room.level is None:
            room.level = self.builder.level_for(room)
        self.level = room.level

        if from_side is not None and from_side in self.level.doors:
            self.player.x, self.player.y = self.level.door_entry(from_side)
        else:
            self.player.x, self.player.y = self.level.player_start
        self.player.vx = self.player.vy = 0.0

        # Everything in the old room belonged to it. Loose embers do not:
        # they follow the light, so anything still on the floor comes with
        # the player rather than being abandoned - which also spares the
        # floor having to remember what was dropped where.
        self.pickups.collect_all(self._collect)
        self.enemies = []
        self.projectiles.clear()
        self.particles.clear()
        self.effects.clear()
        self.decals = []
        self.pools = []
        self.rift = None
        self.hunt_timer = 0.0
        self.boss_ref = None
        self.door_lock = 0.55
        self.pending_door = None

        self.hazards = hazard_mod.Field.build(self.level, self.depth,
                                              self.rng)

        self.flow = flow_mod.FlowField(self.level)
        self.flow.rebuild(self.player.x, self.player.y)

        # Upgrades change the lantern's reach, so re-bake the sizes this
        # room can use while nothing is looking.
        self.prewarm_lantern_sizes()
        self.camera.set_bounds(self.level.width, self.level.height)
        self.camera.snap_to(self.player.x, self.player.y)
        self._arrive(room, from_side)

    def _arrive(self, room, from_side=None):
        """The half of arriving in a room that is not about placing anyone.

        Split out because a crossing does everything else itself - the player
        walked in, so there is nobody to stand anywhere and no camera to snap -
        and then needs exactly this: the room revealed, populated, sealed if
        it is hostile, and announced.
        """
        first_time = not room.visited
        self.plan.reveal_from(room)
        self.rooms_entered += 1

        self._populate_room(room, first_time)
        self.cleared = room.cleared or not room.hostile
        self.warded = room.hostile and not room.cleared
        # A hostile room seals itself behind the player. The door they came
        # through is shut with the rest of them, which is the whole drama of
        # walking into one - and it can only happen now, on arrival, because
        # until they were through it that door had to stay open for them.
        for door in self.level.door_objects.values():
            if self.warded:
                door.shut()
            else:
                door.snap(True)
        self.level.refresh_occluders()
        if self.warded:
            audio.play('ward_seal', 0.7)
            audio.play('door_shut', 0.5)

        self._place_fixture(room)

        # What this room is, said once, on the way in. Only for the rooms
        # where knowing changes what you do next - a combat room announces
        # itself perfectly well by having things in it trying to kill you.
        if first_time:
            if room.kind == plan_mod.BOSS:
                self.set_banner(self.boss_name, 3.4)
            elif room.kind == plan_mod.GAUNTLET:
                self.set_banner('A GAUNTLET', 2.6)
            elif room.kind == plan_mod.ELITE:
                self.set_banner('SOMETHING CARRIES ITS OWN LIGHT', 2.2)

        # The descent is open the moment it is reached. A floor is a budget,
        # not a checklist: what the player leaves behind is the price of
        # going down early, and that only reads as a choice if the way down
        # is never withheld.
        if room.kind == plan_mod.DESCENT:
            self._open_rift()

        # Build what this room opens onto while the player is busy in it.
        self.builder.prefetch([self.plan.room(rid)
                               for rid in room.doors.values()])
        self.builder.trim(room)

        # No collection here. Freezing the heap out of the collector's way
        # was right when a floor was one chamber entered once; a dozen room
        # changes a floor makes it wrong twice over - it pins every chamber
        # the builder has since dropped so nothing baked is ever freed, and
        # the collect in front of it is a third of a second of stall on a
        # heap this size. Both moved to `enter_floor`, which happens twenty
        # times a run rather than two hundred.

        self.boss_intro = self.BOSS_INTRO_TIME if self.boss_ref else 0.0
        self.boss_death = 0.0
        self.boss_corpse = None
        if self.boss_ref is not None:
            # The first beat of the arrival, before anything is visible: the
            # room answering. The other two hang off `boss_intro` in
            # `_tick_boss_intro`, and this is where that clock starts.
            audio.play(f'{self.boss_ref.voice}_wake', 0.85)

    def _place_fixture(self, room):
        """Stand the room's offer in the middle of it, if it has one.

        The middle, and not by the door, on purpose: a reward that pays out
        as you cross the threshold makes the room a formality. Making the
        player walk to it is what charges them the light and the seconds that
        taking the branch was supposed to cost.
        """
        self.fixture = None
        self.pending_shop = False
        kind = room.kind
        if kind not in (plan_mod.CACHE, plan_mod.HEARTH, plan_mod.SHRINE,
                        plan_mod.SHOP):
            return
        cx, cy = self.level.width * 0.5, self.level.height * 0.5
        if not self.level.is_open_at(cx, cy, 30.0):
            spots = self.level.spawn_points
            if spots:
                cx, cy = spots[len(spots) // 2]

        if room.payload is None:
            # Decided on first entry rather than at plan time, so a shrine
            # offers terms that make sense for the run walking in.
            if kind == plan_mod.SHOP:
                pass                      # the shelf is the game's business
            elif kind == plan_mod.CACHE:
                room.payload = fixture_mod.cache_value(self.depth, self.rng)
            elif kind == plan_mod.SHRINE:
                room.payload = fixture_mod.shrine_pact(
                    self.depth, self.rng, self.embers)

        if kind == plan_mod.SHOP:
            self.fixture = fixture_mod.Fixture(
                fixture_mod.FERRYMAN, cx, cy, label='THE FERRYMAN',
                terms='he takes embers')
            # A shop is not consumed by being visited; its payload is the
            # shelf, and the game fills it the first time it is opened.
            return
        if kind == plan_mod.CACHE:
            embers = room.payload[0]
            self.fixture = fixture_mod.Fixture(
                fixture_mod.CACHE, cx, cy, label='A CACHE',
                terms=f'{embers} embers' + (
                    ' and oil' if room.payload[1] else ''),
                payload=room.payload)
        elif kind == plan_mod.HEARTH:
            self.fixture = fixture_mod.Fixture(
                fixture_mod.HEARTH, cx, cy, label='A HEARTH',
                terms='stand in it')
            # A hearth is a room made of light. Everything in it is already
            # burning before the player arrives.
            for b in self.level.braziers:
                b.lit = True
                b.ignite_t = 1.0
                b.edges = None
        else:
            name, terms, key = room.payload
            self.fixture = fixture_mod.Fixture(
                fixture_mod.SHRINE, cx, cy, label=name, terms=terms,
                payload=key)
        self.fixture.taken = room.spent
        if room.spent:
            self.fixture.take_t = 99.0

    def _touch_fixture(self, dt):
        """Tick the room's fixture, and answer it if the player is on it."""
        fixture = self.fixture
        if fixture is None:
            return
        fixture.update(dt)
        if fixture.taken:
            return
        close = fixture.reach(self.player.x, self.player.y)
        if not fixture.armed:
            # Arms the moment the player is clear of it, never before.
            fixture.armed = not close
            return
        if not close:
            return
        if fixture.kind == fixture_mod.FERRYMAN:
            # He is not spent by being spoken to. The game opens the shelf
            # and disarms the fixture on the way out; walking back into him
            # opens it again, because a shop you can only enter once is a
            # vending machine.
            self.pending_shop = True
            return
        fixture.taken = True
        self.room.spent = True
        if fixture.kind == fixture_mod.CACHE:
            self._take_cache(fixture)
        elif fixture.kind == fixture_mod.HEARTH:
            self._take_hearth(fixture)
        else:
            self._take_shrine(fixture)

    def _take_cache(self, fixture):
        embers, oil = fixture.payload
        self.pickups.spawn(pickup_mod.EMBER, fixture.x, fixture.y, 1,
                           self.fxrng, count=embers, speed=(90, 260))
        if oil:
            self.pickups.spawn(pickup_mod.OIL, fixture.x, fixture.y, 26,
                               self.fxrng, count=2, speed=(70, 180))
        self.set_banner('THE CACHE OPENS', 1.8)
        audio.play('cache_open', 0.75)
        self.effects.add_light(fixture.x, fixture.y, 260, 0.7,
                               palette.LIGHT_WARM)

    def _take_hearth(self, fixture):
        player = self.player
        player.add_fuel(player.fuel_max)
        player.heal(player.stats.max_hp * 0.30)
        self.set_banner('THE HEARTH TAKES YOU IN', 2.2)
        audio.play('hearth', 0.8)
        self.effects.add_light(fixture.x, fixture.y, 360, 1.1,
                               palette.LIGHT_WARM)

    def _take_shrine(self, fixture):
        """Strike the bargain. Every one of these costs something real."""
        player = self.player
        key = fixture.payload
        depth = self.depth
        if key == 'fuel':
            player.fuel = max(0.0, player.fuel - player.fuel_max / 3.0)
            gained = int(30 + depth * 4.0)
            self.pickups.spawn(pickup_mod.EMBER, fixture.x, fixture.y, 1,
                               self.fxrng, count=gained, speed=(90, 260))
        elif key == 'blood':
            player.hp = max(1.0, player.hp - player.stats.max_hp * 0.20)
            gained = int(36 + depth * 4.6)
            self.pickups.spawn(pickup_mod.EMBER, fixture.x, fixture.y, 1,
                               self.fxrng, count=gained, speed=(90, 260))
        elif key == 'spend':
            paid = self.embers
            self.embers = 0
            player.heal(player.stats.max_hp * 0.45)
            player.add_fuel(player.fuel_max)
            self.effects.add_text(fixture.x, fixture.y - 30,
                                  f'-{paid} EMBERS', palette.UI_DANGER)
        elif key == 'redraw':
            cost = 25
            self.embers = max(0, self.embers - cost)
            self.stats.rerolls += 1
            self.effects.add_text(fixture.x, fixture.y - 30,
                                  f'-{cost} EMBERS', palette.UI_DANGER)
        self.set_banner(fixture.label, 2.2)
        audio.play('shrine', 0.8)
        self.effects.add_light(fixture.x, fixture.y, 300, 0.9, palette.WARD)
        self.effects.add_shake(2.2)

    def rearm_fixture(self):
        """Disarm the room's fixture until the player steps clear of it.

        Called when the shop closes. Without it the player is still standing
        on the Ferryman when the screen goes away, and walks straight back
        into him on the next frame.
        """
        if self.fixture is not None:
            self.fixture.armed = False

    # ----------------------------------------------------------- crossing --
    def begin_crossing(self, side):
        """Start walking through the door on `side` into the next room.

        A room change used to be a cut: fade to black, swap the chamber,
        stand the player at the far door, fade back. That is three quarters
        of a second in which the game stops being a place and becomes a menu,
        and it happens more often than anything else in a run.

        So the two rooms are simply put in the same space instead. The
        neighbour's matching door is aligned with this one, which makes the
        two border walls coincide - one wall, one opening, exactly as adjacent
        rooms ought to be - and from then until the player is through, both
        chambers are drawn, both are solid, and both occlude the lantern.

        The swap still happens; it just happens *underneath* the player. When
        they pass the threshold the neighbour becomes the live room and every
        coordinate in play - the player's, the camera's - shifts by the same
        offset. Nothing moves on screen. See `_finish_crossing`.
        """
        rid = self.room.doors.get(side)
        if rid is None or self.crossing is not None:
            return False
        room = self.plan.room(rid)
        if room.level is None:
            room.level = self.builder.level_for(room)
        far = plan_mod.OPPOSITE[side]
        if far not in room.level.doors:
            # Nothing to line up with. Fall back to the old cut rather than
            # dropping the player into a wall.
            return False

        # Line the two doorways up on each other. The rooms then share their
        # border wall, and its single opening is the one the player is
        # standing in.
        ax, ay = self.level.door_center(side)
        bx, by = room.level.door_center(far)
        self.crossing = {
            'side': side,
            'room': room,
            'level': room.level,
            'ox': ax - bx,
            'oy': ay - by,
            'trailing': None,
            'from': None,
        }
        # Both doors have to be out of the way for the whole walk.
        for door in (self.level.door_objects.get(side),
                     room.level.door_objects.get(far)):
            if door is not None:
                door.snap(True)
        self.level.refresh_occluders()
        room.level.refresh_occluders()
        self._frame_crossing()
        self._merge_occluders()
        audio.play('door_through', 0.5)
        return True

    def _other_levels(self):
        """Live chambers that are not the one the player is standing in."""
        if self.crossing is None:
            return ()
        return self._crossing_levels()[1:]

    def solids(self):
        """What the player collides against this frame.

        The live chamber normally, and a `Span` over every live one while a
        crossing is in progress - the player has to be able to walk out of
        one room's geometry and into the next without the two ever letting go
        of them.
        """
        if self.crossing is None:
            return self.level
        return Span(self._crossing_levels())

    def _crossing_levels(self):
        """Every chamber that is live right now, with its draw offset.

        One entry normally. Three at most mid-crossing: the room being left,
        the room being entered, and - just after the swap - the one now
        behind the player, which is still on screen.
        """
        out = [(self.level, 0.0, 0.0)]
        c = self.crossing
        if c is None:
            return out
        # By identity, not by equality: after the swap `c['level']` *is* the
        # live room, and listing it twice had the lighting concatenate the
        # room's own edges onto itself - measured, 1374 edges in a sweep that
        # wants about a hundred.
        seen = {id(self.level)}
        for level, ox, oy in ((c['level'], c['ox'], c['oy']),
                              (c['trailing'], c.get('tox', 0.0),
                               c.get('toy', 0.0))):
            if level is None or id(level) in seen:
                continue
            seen.add(id(level))
            out.append((level, ox, oy))
        return out

    def _frame_crossing(self):
        """Widen the camera's frame to hold every live chamber."""
        xs0 = ys0 = 1e18
        xs1 = ys1 = -1e18
        for level, ox, oy in self._crossing_levels():
            xs0 = min(xs0, ox)
            ys0 = min(ys0, oy)
            xs1 = max(xs1, ox + level.width)
            ys1 = max(ys1, oy + level.height)
        self.camera.set_bounds(xs1 - xs0, ys1 - ys0, xs0, ys0, ease=True)

    def _merge_occluders(self):
        """Let the lantern see into the room it is walking towards.

        The visibility sweep reads one set of arrays, so mid-crossing they
        are the union of every live chamber's - the neighbour's translated by
        its draw offset. Without this the light stops dead at a doorway it is
        standing in, and the room beyond an open door is drawn but unlit,
        which looks exactly like a bug.
        """
        c = self.crossing
        if c is None:
            self.level.refresh_occluders()
            return
        import numpy as np
        base = self.level
        base.refresh_occluders()
        ax = [base.seg_ax]
        ay = [base.seg_ay]
        bx = [base.seg_bx]
        by = [base.seg_by]
        pts = [base.corners]
        for level, ox, oy in self._crossing_levels()[1:]:
            level.refresh_occluders()
            ax.append(level.seg_ax + ox)
            ay.append(level.seg_ay + oy)
            bx.append(level.seg_bx + ox)
            by.append(level.seg_by + oy)
            if len(level.corners):
                pts.append(level.corners + (ox, oy))
        base.seg_ax = np.concatenate(ax)
        base.seg_ay = np.concatenate(ay)
        base.seg_bx = np.concatenate(bx)
        base.seg_by = np.concatenate(by)
        base.corners = np.concatenate(pts)

    def _update_crossing(self, dt):
        """Watch for the player passing the threshold, and for them landing."""
        c = self.crossing
        if c is None:
            return
        side = c['side']
        px, py = self.player.x, self.player.y

        if c['from'] is None:
            # Still in the room being left. The threshold is the far face of
            # the shared wall's opening - past it, the player is in the
            # neighbour and everything rebases.
            x0, y0, x1, y1 = self.level.door_zone(side)
            through = {'n': py < y0, 's': py > y1,
                       'w': px < x0, 'e': px > x1}[side]
            if through:
                self._finish_crossing()
            return

        # Rebased. The crossing ends once the player is clear of the shared
        # wall and properly inside the new room, which is when the room they
        # came from can stop being drawn.
        far = plan_mod.OPPOSITE[side]
        x0, y0, x1, y1 = self.level.door_zone(far)
        clear = {'n': py > y1 + 24.0, 's': py < y0 - 24.0,
                 'w': px > x1 + 24.0, 'e': px < x0 - 24.0}[far]
        if clear:
            self.crossing = None
            self.camera.set_bounds(self.level.width, self.level.height,
                                   ease=True)
            self._arrive(c['room'], plan_mod.OPPOSITE[side])

    def _finish_crossing(self):
        """The player has crossed the threshold. Swap the world under them.

        Everything shifts by the offset the neighbour was being drawn at, so
        every number changes and the screen does not: the player, the camera
        and its frame, and anything loose on the floor.
        """
        c = self.crossing
        ox, oy = c['ox'], c['oy']
        old_level = self.level

        self.level = c['level']
        self.room = c['room']
        self.plan.current = self.room.id

        self.player.x -= ox
        self.player.y -= oy
        self.camera.shift(-ox, -oy)
        for item in self.pickups.items:
            item.x -= ox
            item.y -= oy
        for p in self.particles.pool:
            if p.life > 0.0:
                p.x -= ox
                p.y -= oy

        # The room just left keeps being drawn until the player is clear of
        # the doorway, at the mirror of the offset that was moving it.
        c['from'] = old_level
        c['trailing'] = old_level
        c['tox'] = -ox
        c['toy'] = -oy
        c['level'] = self.level
        c['ox'] = 0.0
        c['oy'] = 0.0
        self.flow = flow_mod.FlowField(self.level)
        self.flow.rebuild(self.player.x, self.player.y)
        self._frame_crossing()
        self._merge_occluders()

    def use_door(self, side):
        """Walk through the door on `side`. Returns the room arrived in."""
        rid = self.room.doors.get(side)
        if rid is None:
            return None
        self.enter_room(self.plan.room(rid),
                        from_side=plan_mod.OPPOSITE[side])
        audio.play('door_through', 0.5)
        return self.room

    def _check_doors(self):
        """Has the player stepped into a doorway that will answer?"""
        if self.door_lock > 0.0 or self.pending_door or self.crossing:
            return
        side = self.level.door_at(self.player.x, self.player.y)
        if side is None or side not in self.room.doors:
            return
        # The leaves decide, not the room. A door still sliding open is a
        # door you are standing in, and letting the player through one before
        # it has finished is the thing that would give the game away as a
        # state machine rather than a place.
        door = self.level.door_objects.get(side)
        if door is not None and not door.passable:
            return
        self.pending_door = side

    def _update_doors(self, dt):
        """Advance every leaf, and re-derive the light's occluders if one
        crossed between blocking and clear.

        Only on the crossing: the arrays the lighting reads are rebuilt from
        the chamber's own, and doing that every frame would be a concatenate
        per frame for no change in the result.
        """
        if self.level is None:
            return
        if any(door.update(dt) for door in
               list(self.level.door_objects.values())):
            self.level.refresh_occluders()

    def break_wards(self):
        """The last thing in the room is dead; the doors give."""
        if not self.warded:
            return
        self.warded = False
        self.room.cleared = True
        self.cleared = True
        for door in self.level.door_objects.values():
            door.open()
        audio.play('ward_break', 0.8)
        audio.play('door_open', 0.6)
        self.effects.add_shake(3.4)
        # Cold, like the rift and unlike the lantern. The ward is the
        # vault's light, not yours, and the two should never be confused.
        for side in self.level.doors:
            cx, cy = self.level.door_center(side)
            self.effects.add_light(cx, cy, 190.0, 0.55, palette.PLAYER_TRIM)
            for _ in range(10):
                a = self.fxrng.angle()
                sp = self.fxrng.uniform(60, 240)
                self.particles.emit(1, cx, cy,
                                    math.cos(a) * sp, math.sin(a) * sp,
                                    0.5, 2.4, palette.PLAYER_TRIM,
                                    end_size=0.3, opacity=85, drag=0.5)

    def _populate_room(self, room, first_time):
        """Fill a room with whatever it is for, once.

        A cleared room stays cleared. Walking back through a fight you have
        already won to reach a spur you skipped should cost you the walk and
        nothing else - respawning it would make the map a punishment for
        reading it.
        """
        if not first_time or room.cleared or not room.hostile:
            return
        spots = list(self.level.spawn_points)

        if room.kind == plan_mod.BOSS:
            far = spots[0] if spots else (self.level.width * 0.5,
                                          self.level.height * 0.25)
            kind = boss.for_depth(self.depth)
            self.boss_ref = kind(far[0], far[1], self.depth, self.rng)
            self.enemies.append(self.boss_ref)
            for key in enemy_mod.wave_for_depth(
                    max(1, self.depth - 3), self.rng)[:5]:
                self._spawn_at_spot(enemy_mod.SPECIES[key], spots)
            return

        for key, elite in self._roster_for(room):
            enemy = self._spawn_at_spot(enemy_mod.SPECIES[key], spots)
            if elite and enemy is not None:
                enemy_mod.make_elite(
                    enemy, self.rng.choice(enemy_mod.ELITE_AFFIXES),
                    self.depth)

    #: How much of a floor's worth of enemies a room of each kind is due.
    #: A floor used to be one wave; it is four to seven fights now, so a
    #: single room cannot be a whole floor's budget or a floor would be five
    #: times the fight it was. These are shares of the old whole-floor wave.
    ROOM_WEIGHT = {
        plan_mod.COMBAT: 0.46,
        plan_mod.ELITE: 0.52,
        plan_mod.GAUNTLET: 0.85,
    }

    def _roster_for(self, room):
        """(species, is_elite) pairs for one room's fight."""
        weight = self.ROOM_WEIGHT.get(room.kind, 0.46)
        keys = enemy_mod.wave_for_depth(self.depth, self.rng, weight=weight)

        out = [(k, False) for k in keys]
        if not out:
            return out
        chance = enemy_mod.elite_chance(self.depth)
        if room.kind == plan_mod.ELITE:
            # The room is named for it: one is guaranteed, and the rest of
            # the room rolls as usual.
            out[0] = (out[0][0], True)
            rest = [(k, self.rng.chance(chance)) for k, _ in out[1:]]
            return out[:1] + rest
        if room.kind == plan_mod.GAUNTLET:
            # The optional hard room. Elites are the difficulty, and being
            # able to see them coming is what makes it a fair offer.
            return [(k, self.rng.chance(min(0.75, chance * 2.6)))
                    for k, _ in out]
        return [(k, self.rng.chance(chance)) for k, _ in out]

    def _spawn_at_spot(self, cls, spots):
        """Put one enemy somewhere sensible. Returns it, for the caller to
        decorate - whether a spawn is an elite is the room's business now,
        not the spawner's."""
        if not spots:
            spots = list(self.level.spawn_points) or [self.level.player_start]
        x, y = self.rng.choice(spots)
        x += self.rng.uniform(-TILE * 0.4, TILE * 0.4)
        y += self.rng.uniform(-TILE * 0.4, TILE * 0.4)
        x, y = self.level.collide_circle(x, y, cls.radius)
        enemy = cls(x, y, self.depth, self.rng)
        self.enemies.append(enemy)
        return enemy

    @property
    def boss_name(self):
        """What is waiting on this floor, or the floor's own name."""
        if not self.is_boss:
            return f'FLOOR {self.depth}'
        return boss.NAMES.get(boss.for_depth(self.depth), 'THE HOLLOW CHOIR')

    @property
    def room_kind(self):
        return self.room.kind if self.room is not None else None

    def set_banner(self, text, seconds=2.4):
        self.banner = text
        self.banner_t = seconds

    # ------------------------------------------- enemy-facing context API --
    def spawn_enemy(self, cls, x, y):
        if len(self.enemies) > 90:
            return
        x, y = self.level.collide_circle(x, y, cls.radius)
        self.enemies.append(cls(x, y, self.depth, self.rng))

    def adopt_enemy(self, enemy):
        """Take an enemy an enemy made. Used by anything that splits.

        Separate from `spawn_enemy` because the caller has already built the
        thing, positioned it and given it a velocity - a splitter's children
        inherit a direction, and re-deriving that here would lose it.
        """
        if len(self.enemies) > 120:
            return None
        enemy.x, enemy.y = self.level.collide_circle(
            enemy.x, enemy.y, enemy.radius)
        self.enemies.append(enemy)
        return enemy

    def add_pool(self, x, y, radius, seconds, dps, color=None, arm=0.0):
        """Standing rot on the floor, left by something that died here.

        Bounded like the decals are: a long fight in one room should not end
        up with forty overlapping pools, each of them being tested against
        the player every frame.
        """
        # `arm` is a delay before it bites, for anything that marks the floor
        # before it lights it. The mark is drawn the whole time, so what the
        # circle covers when it arms is exactly what will burn.
        self.pools.append([x, y, radius, seconds, seconds, dps,
                           color or palette.CARRION_POOL, arm])
        if len(self.pools) > 22:
            self.pools.pop(0)

    def _update_pools(self, dt):
        player = self.player
        for pool in self.pools:
            if pool[7] > 0.0:
                pool[7] -= dt
                continue                # still only a mark on the floor
            pool[3] -= dt
            if pool[3] <= 0.0:
                continue
            dx = player.x - pool[0]
            dy = player.y - pool[1]
            if dx * dx + dy * dy <= pool[2] * pool[2] and player.alive:
                # Standing in it, not walking through it: the damage is a
                # rate, so a player who keeps moving pays almost nothing and
                # one who stands and shoots pays for the whole eight seconds.
                player.scorch(pool[5] * dt, self.effects, self.particles,
                              self.fxrng)
            if self.fxrng.chance(7.0 * dt):
                a = self.fxrng.angle()
                d = self.fxrng.uniform(0, pool[2])
                self.particles.emit(
                    1, pool[0] + math.cos(a) * d, pool[1] + math.sin(a) * d,
                    0.0, -22.0, 0.8, 2.6, pool[6],
                    end_size=0.4, opacity=64, drag=0.6)
        self.pools = [p for p in self.pools if p[3] > 0.0]

    def _draw_pools(self, ox, oy):
        for x, y, radius, left, total, _dps, color, arm in self.pools:
            sx, sy = x - ox, y - oy
            if arm > 0.0:
                # Not yet burning. Drawn at full reach and brightening as it
                # arms, so the ring you are standing outside of when it
                # closes is the ring that will not catch you.
                pts = []
                for i in range(11):
                    a = i * math.tau / 11
                    pts.append(sx + math.cos(a) * radius)
                    pts.append(sy + math.sin(a) * radius)
                drawPolygon(*pts, fill=None, border=color, borderWidth=2.0,
                            opacity=int(28 + 46 * (1.0 - min(arm, 1.0))))
                continue
            k = clamp(left / max(total, 1e-6), 0.0, 1.0)
            # It shrinks as it dries, so what the circle covers is always
            # what will actually hurt you.
            r = radius * (0.55 + 0.45 * k)
            pts = []
            for i in range(11):
                a = i * math.tau / 11
                wob = 1.0 + 0.12 * math.sin(self.run_time * 1.6 + i * 1.7)
                pts.append(sx + math.cos(a) * r * wob)
                pts.append(sy + math.sin(a) * r * wob)
            drawPolygon(*pts, fill=color, opacity=int(34 * k))
            drawPolygon(*pts, fill=None, border=color,
                        borderWidth=1.6, opacity=int(46 * k))

    def chase_dir(self, x, y):
        """Unit vector from (x, y) toward the player, routed around walls.

        Line of sight wins when it exists - following the tile field in the
        open makes movement visibly snap to the grid. Only when the player is
        actually hidden does the flow field take over.
        """
        px = self.player.x
        py = self.player.y
        dx = px - x
        dy = py - y
        d = math.hypot(dx, dy)
        if d < 1e-6:
            return 0.0, 0.0
        if d < 96.0 or not self.level.ray_blocked(x, y, px, py):
            return dx / d, dy / d
        step = self.flow.direction_at(x, y) if self.flow is not None else None
        if step is None:
            return dx / d, dy / d
        return step

    def enemy_bullet_damage(self, base):
        return base * (1.0 + 0.07 * (self.depth - 1))

    def on_kill(self, enemy, angle=None):
        enemy.death_burst(self, angle)
        self.kills += 1
        self.player.kills += 1
        self.streak += 1
        self.streak_timer = 3.0
        self.best_streak = max(self.best_streak, self.streak)

        bonus = 1.0 + min(1.5, self.streak * 0.04)
        self.score += int(enemy.score * bonus)

        self.effects.add_shake(1.6 if enemy.species != 'choir' else 12.0)
        self.effects.add_hitstop(0.03 if enemy.species != 'choir' else 0.09)

        if self.stats.kill_heal:
            self.player.heal(self.stats.kill_heal)
        if self.stats.siphon:
            # The lantern is the clock on a run, so a build that feeds it by
            # killing is a genuinely different way to play the same floor.
            self.player.add_fuel(self.stats.siphon)

        if enemy.elite:
            self.effects.add_shake(5.0)
            self.effects.add_flash(0.5, enemy.elite_color)
            self.effects.add_light(enemy.x, enemy.y, 210.0, 0.4,
                                   enemy.elite_color)
            self.particles.burst(enemy.x, enemy.y, 40, enemy.elite_color,
                                 self.fxrng, speed=(160, 460),
                                 life=(0.3, 0.7), size=(2.2, 5.2))
        self.pickups.spawn(pickup_mod.EMBER, enemy.x, enemy.y, 1, self.rng,
                           count=enemy.ember_value)
        if self.rng.chance(0.16):
            self.pickups.spawn(pickup_mod.OIL, enemy.x, enemy.y, 20, self.rng)
        if self.rng.chance(0.055):
            self.pickups.spawn(pickup_mod.HEART, enemy.x, enemy.y, 14, self.rng)

        if enemy is self.boss_ref:
            self.begin_boss_death(enemy)
            self.effects.add_flash(0.9, palette.BOSS_EYE, wash=True)
            self.pickups.spawn(pickup_mod.HEART, enemy.x, enemy.y, 18, self.rng,
                               count=3, speed=(90, 220))
            audio.play_at('boom', enemy.x, enemy.y, 0.6)
            self.add_decal(enemy.x, enemy.y, 1.6)
        else:
            audio.play_at('kill', enemy.x, enemy.y, 0.4)
            if self.rng.chance(0.34):
                self.add_decal(enemy.x, enemy.y, 0.72)

    # A flat surface, in the normal map's own packing: straight up out of
    # the floor. Anything standing on the ground gets one, or every light in
    # the room rakes it with the courses of the stone underneath it - which
    # is the floor's texture printed across a person.
    FLAT_NORMAL = (128, 128, 255)

    def _draw_entity_normals(self, ox, oy):
        """Give everything standing on the floor a surface of its own."""
        flat = palette.rgb(*self.FLAT_NORMAL)
        gpu.set_mode(gpu.REPLACE)
        for e in self.enemies:
            if not e.alive:
                continue
            sx, sy = e.x - ox, e.y - oy
            if sx < -90 or sy < -90 or sx > self.view_w + 90 or sy > self.view_h + 90:
                continue
            self._normal_disc(sx, sy, e.radius * 1.35, flat)
        for b in self.level.braziers:
            self._normal_disc(b.x - ox, b.y - oy, 15.0, flat)
        if self.player.alive:
            self._normal_disc(self.player.x - ox, self.player.y - oy,
                              self.player.radius * 1.9, flat)
        gpu.set_mode(gpu.NORMAL)

    @staticmethod
    def _normal_disc(sx, sy, r, fill):
        """A ten-sided stand-in for a body. Nobody sees this buffer."""
        pts = []
        for i in range(10):
            a = i * math.tau / 10.0
            pts.append(sx + math.cos(a) * r)
            pts.append(sy + math.sin(a) * r)
        drawPolygon(*pts, fill=fill, opacity=100)

    def add_decal(self, x, y, scale=1.0):
        """Leave a burn where something went off."""
        lo, hi = self.DECAL_SIZE
        size = self.fxrng.uniform(lo, hi) * scale
        self.decals.append((float(x), float(y), size,
                            self.fxrng.randint(0, 15)))
        if len(self.decals) > self.DECAL_MAX:
            del self.decals[0]

    def _draw_decals(self, ox, oy, normals=False):
        if not self.decals:
            return
        for x, y, size, seed in self.decals:
            sx = x - ox - size * 0.5
            sy = y - oy - size * 0.5
            if (sx < -size or sy < -size
                    or sx > self.view_w or sy > self.view_h):
                continue
            sprite = (art.scorch_normal(seed) if normals
                      else art.scorch(seed))
            drawImage(sprite, sx, sy, width=size, height=size)

    # ------------------------------------------------------------- update --
    def update(self, dt, keys, aim_world, firing):
        scale = self.effects.update(dt)
        sdt = dt * scale
        self.run_time += dt
        self.motes.step(dt)
        if self.boss_intro > 0.0:
            self._tick_boss_intro(dt)
        if self.boss_death > 0.0:
            self._tick_boss_death(dt)
        self.floor_time += dt
        self.banner_t = max(0.0, self.banner_t - dt)

        shake = self.effects.take_shake()
        if shake:
            self.camera.add_shake(shake)

        # So anything that makes a noise only has to know where it is.
        audio.set_listener(self.camera.ox, self.camera.oy,
                           self.view_w, self.view_h)

        if sdt <= 0.0:
            self.camera.follow(self.player.x, self.player.y, 0.0, 0.0, dt)
            return

        if self.streak_timer > 0.0:
            self.streak_timer -= sdt
            if self.streak_timer <= 0.0:
                self.streak = 0

        player = self.player
        ax, ay = aim_world
        dx = ax - player.x
        dy = ay - player.y
        if abs(dx) + abs(dy) < 1e-6:
            dx, dy = from_angle(player.aim)

        player.update(sdt, keys, dx, dy, self.solids(), self.particles,
                      self.fxrng)

        # One BFS per tile the player crosses feeds every chaser and every
        # drifting ember for the rest of that tile.
        self.flow.maybe_rebuild(player.x, player.y)

        # Firing (charge weapons hold, others repeat).
        weapon = player.weapon
        if firing:
            if weapon.charge_time > 0.0:
                if not player.charging:
                    # A charging hum has existed in the sound bank since the
                    # coil was written and nothing ever played it, so holding
                    # the trigger on the one weapon that asks you to hold it
                    # was silent until the shot went off.
                    audio.play('charge', 0.55)
                player.charging = True
                was = player.charge
                player.charge = min(weapon.charge_time, player.charge + sdt)
                if was < weapon.charge_time <= player.charge:
                    # And a short cue at the top, so a full charge is
                    # something you hear rather than something you count.
                    audio.play('crit', 0.32)
                    self.effects.add_light(player.x, player.y, 120.0, 0.18,
                                           weapon.glow)
            elif player.can_fire():
                player.fire(self.projectiles, self.particles, self.fxrng,
                            self.effects)
        else:
            if weapon.charge_time > 0.0 and player.charging:
                if player.charge > 0.12:
                    player.fire(self.projectiles, self.particles, self.fxrng,
                                self.effects)
                else:
                    player.charging = False
                    player.charge = 0.0

        self._update_lighting()
        self._update_enemies(sdt)
        self._update_projectiles(sdt)
        self._update_braziers(sdt)
        self._update_pools(sdt)

        self.pickups.update(sdt, player, self.level, self.particles, self.fxrng,
                            self._collect, self.flow)
        self.particles.update(sdt, self.level)
        self._ambient_dust(sdt)

        if self.rift is not None:
            self.rift.open_t = min(1.0, self.rift.open_t + sdt * 1.6)
            if self.rift_reached():
                self.rift.hold = min(RIFT_HOLD, self.rift.hold + sdt)
                if self.fxrng.chance(28.0 * sdt):
                    a = self.fxrng.angle()
                    d = self.fxrng.uniform(30, 64)
                    self.particles.emit(
                        1, self.rift.x + math.cos(a) * d,
                        self.rift.y + math.sin(a) * d,
                        -math.cos(a) * 90, -math.sin(a) * 90,
                        0.5, 2.6, palette.PLAYER_TRIM, end_size=0.4,
                        opacity=80, drag=0.4)
            else:
                self.rift.hold = max(0.0, self.rift.hold - sdt * 2.2)

        aim_nx, aim_ny = 0.0, 0.0
        d = math.hypot(dx, dy)
        if d > 1e-6:
            aim_nx, aim_ny = dx / d, dy / d
        # An arrival you cannot see is not an arrival. The view swings across
        # to whatever is assembling and comes back as it finishes, and it
        # stays on the corpse while that comes apart - the two moments the
        # game most wants you looking somewhere other than at your own feet.
        fx_, fy_ = player.x, player.y
        if self.boss_intro > 0.0 and self.boss_ref is not None:
            t = clamp(self.boss_intro / max(self.BOSS_INTRO_TIME, 1e-6),
                      0.0, 1.0)
            swing = math.sin(math.pi * (1.0 - t)) ** 0.7 * 0.92
            fx_ = lerp(player.x, self.boss_ref.x, swing)
            fy_ = lerp(player.y, self.boss_ref.y, swing)
        elif self.boss_death > 0.0 and self.boss_corpse:
            t = clamp(self.boss_death / max(self.BOSS_DEATH_TIME, 1e-6),
                      0.0, 1.0)
            hold = min(1.0, t * 1.8) * 0.8
            fx_ = lerp(player.x, self.boss_corpse[0], hold)
            fy_ = lerp(player.y, self.boss_corpse[1], hold)
        self.camera.follow(fx_, fy_, aim_nx, aim_ny, dt)

        if self.stats.dash_damage and player.dash_time > 0.0:
            self._dash_cleave(player)

        # The room is won when the last thing in it stops moving. That
        # breaks the wards; it does not open a way down, because the way down
        # is a room of its own now and it was never closed.
        if (self.warded and self.boss_death <= 0.0
                and not any(e.alive for e in self.enemies)):
            self.break_wards()

        self._touch_fixture(sdt)
        if self.door_lock > 0.0:
            self.door_lock = max(0.0, self.door_lock - dt)
        self.hazards.update(sdt, self)
        player.ground_slow = self.hazards.slow_at(player.x, player.y)
        self._update_doors(dt)
        self._update_crossing(dt)
        self._check_doors()

    # How far a chained arc will reach for its next target, and what share of
    # the original hit it carries there. Short and lossy on purpose: chaining
    # should thin a crowd, not delete one.
    CHAIN_RANGE = 132.0
    CHAIN_FALLOFF = 0.62

    def _arc_from(self, source, damage, projectile):
        """Jump the hit along a short chain of nearby enemies."""
        seen = {id(source)}
        x, y = source.x, source.y
        left = projectile.chain
        carry = damage * self.CHAIN_FALLOFF
        reach2 = self.CHAIN_RANGE * self.CHAIN_RANGE
        while left > 0 and carry > 0.5:
            best, best_d = None, reach2
            for e in self.enemies:
                if not e.alive or id(e) in seen:
                    continue
                d = (e.x - x) ** 2 + (e.y - y) ** 2
                if d < best_d:
                    best, best_d = e, d
            if best is None:
                return
            seen.add(id(best))
            dealt = best.damage_by(carry, self,
                                   math.atan2(best.y - y, best.x - x),
                                   40.0, False)
            if dealt > 0.0:
                self.player.damage_dealt += dealt
                if self.stats.lifesteal:
                    self.player.heal(dealt * self.stats.lifesteal)
            self.effects.add_beam(x, y, best.x, best.y, palette.BEAM, 0.14)
            self.particles.burst(best.x, best.y, 5, palette.BEAM, self.fxrng,
                                 speed=(90, 240), life=(0.1, 0.26),
                                 size=(1.6, 3.4))
            x, y = best.x, best.y
            carry *= self.CHAIN_FALLOFF
            left -= 1

    def _dash_cleave(self, player):
        """A dash that carves what it passes through.

        Dashing is the only movement in the game with weight behind it, and
        until now it did nothing but relocate you. Each enemy is cut once per
        dash - tracked on the dash itself, not on a timer - so the damage is a
        property of passing through something rather than of standing in it.
        """
        cut = player.dash_hits
        reach = player.radius + 16.0
        for e in self.enemies:
            if not e.alive or id(e) in cut:
                continue
            dx = e.x - player.x
            dy = e.y - player.y
            rr = reach + e.radius
            if dx * dx + dy * dy > rr * rr:
                continue
            cut.add(id(e))
            dealt = e.damage_by(
                self.stats.dash_damage * self.stats.damage_mult, self,
                math.atan2(dy, dx), 220.0, False)
            if dealt > 0.0:
                player.damage_dealt += dealt
            self.particles.burst(e.x, e.y, 12, palette.DASH_TRAIL, self.fxrng,
                                 speed=(180, 420), life=(0.14, 0.34),
                                 size=(2.0, 4.4))
            self.effects.add_hitstop(0.02)

    def _collect(self, item):
        if item.kind == pickup_mod.EMBER:
            gain = max(1, int(round(item.value * self.stats.ember_gain)))
            self.embers += gain
            self.score += 5
        elif item.kind == pickup_mod.OIL:
            self.player.add_fuel(item.value)
        else:
            self.player.heal(item.value)

    def _update_lighting(self):
        player = self.player
        # Standing water reflects, so the lantern carries further over it -
        # the reward half of a trade whose cost is that you cannot run.
        self.light_radius = player.lantern_radius * self.hazards.reach_at(
            player.x, player.y)
        self.light_fan = lighting.visibility_fan(
            self.level, player.x, player.y, self.light_radius)
        self.light_poly = self.light_fan.points

        live = [e for e in self.enemies if e.alive]
        if live:
            pts = [(e.x, e.y) for e in live]
            lit = lighting.visible_points(self.level, player.x, player.y,
                                          self.light_radius, pts)
            for e, flag in zip(live, lit):
                e.lit = flag
            for brazier in self.level.braziers:
                if not brazier.lit:
                    continue
                blit = lighting.visible_points(self.level, brazier.x, brazier.y,
                                               200.0, pts)
                for e, flag in zip(live, blit):
                    if flag:
                        e.lit = True

    HUNT_AFTER = 14.0

    def _update_enemies(self, dt):
        player = self.player

        # A floor should never end in a slow chase. Once only a couple of
        # stragglers are left, give them a while and then send them at you.
        remaining = sum(1 for e in self.enemies if e.alive)
        if 0 < remaining <= 2:
            self.hunt_timer += dt
            if self.hunt_timer > self.HUNT_AFTER:
                for e in self.enemies:
                    if e.alive and not e.hunting:
                        e.hunting = True
                        self.particles.ripple(e.x, e.y, palette.CRIT,
                                              e.radius * 4.0, 0.5, 70)
        else:
            self.hunt_timer = 0.0

        slow = self.stats.slow_field
        light_dps = self.stats.light_damage
        alive = []
        for e in self.enemies:
            if not e.alive:
                continue

            # Wisps fly over walls, which means they can end up hovering
            # inside one - where player shots cannot reach them and a floor
            # can never be cleared. Pull any flier that lingers back out.
            if e.flies:
                # This used to ask whether the flier's *centre tile* was a
                # wall, and then wait eight tenths of a second. A wisp whose
                # centre sat on open floor with most of its body inside the
                # stone never triggered it at all - measured, they were
                # sitting eleven units deep, which is a target you cannot hit
                # and a floor that cannot be cleared.
                #
                # Real overlap now, and a steady push rather than a timer:
                # crossing a wall is still possible, resting inside one is
                # not. The teleport stays as a backstop for anything that
                # manages to get properly stuck.
                ex, ey = self.level.collide_circle(e.x, e.y, e.radius)
                if ex != e.x or ey != e.y:
                    e.wall_time += dt
                    # Crossing is the whole point of a flier, so a fast one
                    # is left alone. Coming to rest in the stone is the bug -
                    # you cannot shoot it and the floor cannot be cleared -
                    # so anything slow inside a wall, or anything that has
                    # been in there too long however fast, is put back out.
                    slow = (e.vx * e.vx + e.vy * e.vy) < self.FLIER_REST_SPEED ** 2
                    if slow or e.wall_time > self.FLIER_MAX_INSIDE:
                        e.wall_time = 0.0
                        e.x, e.y = self.level.collide_circle(
                            e.x, e.y, e.radius + 6.0)
                        self.particles.burst(e.x, e.y, 8, e.eye_color,
                                             self.fxrng, speed=(60, 200),
                                             life=(0.15, 0.35), size=(1.6, 3.2))
                else:
                    e.wall_time = 0.0

            # Water slows whatever is walking in it, and a flier is not.
            e.slow = 0.0 if e.flies else self.hazards.slow_at(e.x, e.y)
            if slow:
                d2 = (e.x - player.x) ** 2 + (e.y - player.y) ** 2
                if d2 < 210.0 ** 2:
                    e.slow = max(e.slow, slow)
            if e is self.boss_ref and self.boss_intro > 0.0:
                # Still assembling. It is drawn, and it is not yet a fight.
                e.spawn_t = max(e.spawn_t, self.boss_intro)
                alive.append(e)
                continue
            e.update(dt, self)

            if light_dps and e.lit and e.alive and not e.immune():
                # Through the same door as everything else, so it counts
                # toward the run's damage and feeds lifesteal like any other
                # hit. Subtracting hp directly skipped both.
                before = e.hp
                e.hp -= light_dps * dt
                dealt = before - max(e.hp, 0.0)
                if dealt > 0.0:
                    self.player.damage_dealt += dealt
                    if self.stats.lifesteal:
                        self.player.heal(dealt * self.stats.lifesteal)
                if self.fxrng.chance(2.0 * dt):
                    self.particles.embers(e.x, e.y, 1, palette.LIGHT_CORE,
                                          self.fxrng)
                if e.hp <= 0.0:
                    e.die(self)

            if e.alive and player.alive and e.touch_cd <= 0.0 and not e.immune():
                rr = e.radius + player.radius
                dx = player.x - e.x
                dy = player.y - e.y
                if dx * dx + dy * dy <= rr * rr:
                    angle = math.atan2(dy, dx)
                    if player.hurt(e.damage, self.effects, self.particles,
                                   self.fxrng, angle):
                        # Anything that takes something other than health
                        # takes it here - a douser's bite at the lantern.
                        # Gated on the hit landing, so invulnerability frames
                        # protect the flame as well as the body.
                        e.on_touch(self)
                        e.touch_cd = 0.65
                        self.camera.add_shake(4.5)
                        push = 240.0
                        player.vx += math.cos(angle) * push
                        player.vy += math.sin(angle) * push
                        e.vx -= math.cos(angle) * 160.0 / max(e.mass, 0.4)
                        e.vy -= math.sin(angle) * 160.0 / max(e.mass, 0.4)
                        if self.stats.thorns:
                            back = e.damage_by(self.stats.thorns, self,
                                               angle + math.pi, 120.0)
                            if back > 0.0:
                                player.damage_dealt += back
                    if isinstance(e, enemy_mod.Wisp):
                        player.fuel = max(0.0, player.fuel - e.fuel_drain)
            if e.alive:
                alive.append(e)
        self.enemies = alive

    def _update_projectiles(self, dt):
        player = self.player
        live_enemies = [e for e in self.enemies if e.alive]
        wall_hits = self.projectiles.update(dt, self.level, live_enemies
                                            if self.stats.homing else None)

        for p, hx, hy in wall_hits:
            self.particles.burst(hx, hy, 5, p.color, self.fxrng,
                                 speed=(60, 220), life=(0.1, 0.26),
                                 size=(1.4, 3.0))
            if p.owner == projectile_mod.PLAYER and p.explode:
                self._detonate(hx, hy, p)

        for p in self.projectiles.pool:
            if not p.alive:
                continue
            if p.owner == projectile_mod.PLAYER:
                for e in live_enemies:
                    if not e.alive or id(e) in p.hit:
                        continue
                    rr = e.radius + p.radius
                    dx = e.x - p.x
                    dy = e.y - p.y
                    if dx * dx + dy * dy > rr * rr:
                        continue
                    angle = math.atan2(p.vy, p.vx)
                    if e.immune():
                        continue          # under the floor; the shot passes
                    # A mirror turns anything that lands on its face back the
                    # way it came, and it becomes hostile fire - which is the
                    # point of it. Shooting one from the front is not merely
                    # useless, it is a mistake.
                    if getattr(e, 'reflects', None) is not None \
                            and e.reflects(angle):
                        self._reflect(p, e, angle)
                        break
                    # Bonuses that depend on what was hit rather than on who
                    # fired, so they cannot be settled at the muzzle.
                    dmg = p.damage
                    st = self.stats
                    if st.dark_damage and not e.lit:
                        dmg *= 1.0 + st.dark_damage
                    if st.first_strike and e.hp >= e.max_hp - 1e-6:
                        dmg *= 1.0 + st.first_strike
                    dealt = e.damage_by(dmg, self, angle, p.knockback,
                                        p.crit)
                    if dealt > 0.0:
                        self.player.damage_dealt += dealt
                        if self.stats.lifesteal:
                            player.heal(dealt * self.stats.lifesteal)
                    if p.chain > 0:
                        self._arc_from(e, dmg, p)
                    p.hit.add(id(e))
                    if p.explode:
                        self._detonate(p.x, p.y, p)
                        p.alive = False
                        break
                    if p.pierce <= 0:
                        p.alive = False
                        break
                    p.pierce -= 1
            else:
                if not player.alive:
                    continue
                rr = player.radius + p.radius
                dx = player.x - p.x
                dy = player.y - p.y
                if dx * dx + dy * dy <= rr * rr:
                    angle = math.atan2(p.vy, p.vx)
                    if player.hurt(p.damage, self.effects, self.particles,
                                   self.fxrng, angle):
                        self.camera.add_shake(3.4)
                    p.alive = False

    def _reflect(self, p, mirror, angle):
        """Send a shot back off a mirror's face, as hostile fire.

        It keeps its damage and loses its pierce and its chain: a reflected
        coilbeam that still passed through everything would be lethal in a
        way nothing else in the game is, and the reflection is meant to be a
        warning rather than a death sentence.
        """
        back = angle + math.pi + self.fxrng.uniform(-0.10, 0.10)
        speed = math.hypot(p.vx, p.vy) * 0.86
        p.alive = False
        mirror.shield_flash = 1.0
        self.projectiles.spawn(
            projectile_mod.ENEMY,
            mirror.x + math.cos(back) * (mirror.radius + 4.0),
            mirror.y + math.sin(back) * (mirror.radius + 4.0),
            math.cos(back) * speed, math.sin(back) * speed,
            self.enemy_bullet_damage(p.damage * 0.7),
            radius=p.radius, life=1.4, color=palette.MIRROR_EYE,
            glow_color=(220, 236, 255), length=p.length, width=p.width,
            knockback=90.0)
        self.particles.burst(mirror.x, mirror.y, 7, palette.MIRROR_EYE,
                             self.fxrng, speed=(120, 300), life=(0.1, 0.3),
                             size=(1.6, 3.4), direction=back, spread=1.2)
        self.effects.add_light(mirror.x, mirror.y, 110.0, 0.14,
                               art.rgb_tuple(palette.MIRROR_EYE))
        audio.play_at('reflect', mirror.x, mirror.y, 0.55)

    def _detonate(self, x, y, source):
        radius = self.stats.explode_radius
        if radius <= 0.0:
            return
        damage = self.stats.explode_damage * self.stats.damage_mult
        self.particles.burst(x, y, 18, palette.ENEMY_BOLT, self.fxrng,
                             speed=(140, 420), life=(0.2, 0.5), size=(2.4, 5.2))
        self.particles.ripple(x, y, palette.SCATTER, radius * 2.0, 0.3, 74)
        self.effects.add_light(x, y, radius * 2.4, 0.2, (255, 170, 90))
        self.effects.add_shake(2.6)
        audio.play('boom', 0.42)
        self.add_decal(x, y, 1.15)
        for e in self.enemies:
            if not e.alive:
                continue
            dx = e.x - x
            dy = e.y - y
            d2 = dx * dx + dy * dy
            if d2 > radius * radius:
                continue
            falloff = 1.0 - math.sqrt(d2) / radius
            e.damage_by(damage * (0.4 + 0.6 * falloff), self,
                        math.atan2(dy, dx), 220.0 * falloff)

    def _update_braziers(self, dt):
        player = self.player
        for b in self.level.braziers:
            b.flicker += dt
            if b.lit:
                b.ignite_t = min(1.0, b.ignite_t + dt * 2.2)
                if self.fxrng.chance(9.0 * dt):
                    self.particles.embers(b.x, b.y - 6, 1, palette.LIGHT_WARM,
                                          self.fxrng, speed=(6, 26))
                # A lit brazier is a refuelling point - the one place you can
                # recover a collapsed lantern, and worth fighting to hold.
                d2 = (player.x - b.x) ** 2 + (player.y - b.y) ** 2
                if d2 < BRAZIER_REFILL_RANGE ** 2 and player.fuel < player.fuel_max:
                    player.add_fuel(BRAZIER_REFILL_RATE * dt)
                    if self.fxrng.chance(7.0 * dt):
                        a = self.fxrng.angle()
                        self.particles.emit(
                            1, b.x + math.cos(a) * 10, b.y + math.sin(a) * 10,
                            (player.x - b.x) * 0.9, (player.y - b.y) * 0.9,
                            0.55, 2.4, palette.LIGHT_WARM, end_size=0.4,
                            opacity=78, drag=0.6)
                continue
            d2 = (player.x - b.x) ** 2 + (player.y - b.y) ** 2
            if d2 < (player.radius + 26.0) ** 2:
                b.lit = True
                player.add_fuel(BRAZIER_IGNITE_FUEL)
                self.effects.add_light(b.x, b.y, 260, 0.5, palette.LIGHT_WARM)
                self.effects.add_shake(1.4)
                self.particles.burst(b.x, b.y, 22, palette.LIGHT_WARM,
                                     self.fxrng, speed=(80, 260),
                                     life=(0.3, 0.7), size=(2, 4.4))
                self.score += 25
                audio.play_at('brazier', b.x, b.y, 0.55)

    def _ambient_dust(self, dt):
        if self.fxrng.chance(12.0 * dt):
            ox, oy = self.camera.ox, self.camera.oy
            x = ox + self.fxrng.uniform(0, self.view_w)
            y = oy + self.fxrng.uniform(0, self.view_h)
            self.particles.dust(x, y, self.fxrng, palette.UI_FAINT)

    def flare(self):
        """Lantern flare: a burst of light that also shoves the dark back."""
        player = self.player
        if not player.try_flare():
            return False
        radius = 300.0 * self.stats.lantern_mult
        damage = LANTERN_FLARE_DAMAGE * self.stats.flare_damage_mult
        self.effects.add_flash(0.62, palette.FLARE, wash=True)
        self.effects.add_light(player.x, player.y, radius * 1.7,
                               LANTERN_FLARE_TIME, palette.FLARE)
        self.effects.add_shake(5.0)
        self.particles.ripple(player.x, player.y, palette.FLARE, radius * 2.0,
                              0.45, 88)
        self.particles.burst(player.x, player.y, 34, palette.LIGHT_CORE,
                             self.fxrng, speed=(180, 520), life=(0.25, 0.6),
                             size=(2.2, 5.0))
        for e in self.enemies:
            if not e.alive:
                continue
            dx = e.x - player.x
            dy = e.y - player.y
            d2 = dx * dx + dy * dy
            if d2 > radius * radius:
                continue
            falloff = 1.0 - math.sqrt(d2) / radius
            angle = math.atan2(dy, dx)
            dealt = e.damage_by(damage * (0.45 + 0.55 * falloff), self, angle,
                                LANTERN_FLARE_KNOCKBACK * falloff)
            player.damage_dealt += dealt
        audio.play('flare', 0.7)
        return True

    def _open_rift(self):
        """Put the way down in the middle of the room that is the way down.

        It used to be placed off to one side of a cleared chamber, because
        the chamber was the whole floor and the rift had to go somewhere in
        it. A descent room exists only to hold it, so it goes in the middle
        where it can be seen from every door.
        """
        self.cleared = True
        self.rift = Rift(self.level.width * 0.5, self.level.height * 0.5)
        self.effects.add_light(self.rift.x, self.rift.y, 320, 1.2,
                               palette.PLAYER_TRIM)
        audio.play('upgrade', 0.5)

    def rift_reached(self):
        if self.rift is None or self.rift.open_t < 0.6:
            return False
        d2 = (self.player.x - self.rift.x) ** 2 + (self.player.y - self.rift.y) ** 2
        return d2 < 40.0 ** 2

    def rift_ready(self):
        return self.rift is not None and self.rift.hold >= RIFT_HOLD

    # --------------------------------------------------------------- draw --
    # What a shadowed corner settles to once the lantern has been multiplied
    # out of it. Not black: a chamber with nothing in it at all reads as a
    # hole cut in the screen rather than as darkness.
    AMBIENT = (15, 18, 28)
    # How much of the light buffer is added on top of the multiply. The old
    # renderer laid the floor back over the glow at 46%, so a bit over half of
    # a lit floor's brightness was the light itself; this is that share.
    # A lit floor is mostly light, not ground: the old renderer laid the floor
    # back over the glow at 46%, so 54% of what you saw was the lantern. This
    # is that share, and it is what keeps the pool warm instead of merely
    # revealing the stone.
    # Split between the two: the gain scales the albedo, so a wall and the
    # floor beside it keep the difference between them, and the bleed supplies
    # the warmth that makes a lit floor read as lit rather than merely
    # visible. All bleed and the masonry washes out; all gain and the pool
    # goes grey.
    BLEED = 0.32
    BLOOM = 0.16
    ALBEDO_GAIN = 0.58

    # How far above the floor the lantern hangs, in design units. It sets how
    # steeply its light rakes across the stone: low, and every blemish throws
    # a hard edge; high, and the surface flattens out again. About a third of
    # a tile reads as a lamp carried at waist height. Every light has one -
    # see `BRAZIER_HEIGHT` and `RIFT_HEIGHT` - and they differ, which is why
    # a brazier off to one side picks out courses the lantern does not.
    LIGHT_HEIGHT = 22.0


    # The rift's own light. Cold, so it reads against the lantern at a
    # glance, and far-reaching, because its whole job is to be findable.
    # Dust is only worth drawing where the light is strong enough to pick it
    # out, so it reaches less far than the light itself does.
    MOTE_REACH = 0.82
    MOTE_STRENGTH = 1.0

    # Burns left on the floor. Few enough to stay cheap, large enough that a
    # room you have fought in looks like it.
    DECAL_MAX = 26
    DECAL_SIZE = (52.0, 104.0)

    RIFT_LIGHT = (122, 190, 255)
    RIFT_LIGHT_RADIUS = 300.0
    RIFT_LIGHT_STRENGTH = 54.0
    # Cooler than the lantern and further off, so it lands on masonry softly.
    RIFT_ON_WALLS = 0.72

    # What an elite throws. Enough to be seen across a room and not enough to
    # light the room: the floor stays dark, the thing in it does not.
    # How long a boss takes to arrive, and to come apart.
    BOSS_INTRO_TIME = 3.1
    BOSS_DEATH_TIME = 2.8

    # The arrival, written as a score instead of as a run of probabilities.
    #
    # Its first beat used to be `fxrng.chance(7.0 * dt)` - a random scatter of
    # rings across a second and a half. Nothing could be synchronised to that,
    # because there was nothing to synchronise *to*: the audio under it could
    # only ever be a drone. These are five strikes at closing intervals,
    # fractions of `BOSS_INTRO_TIME` so that retiming the arrival retimes the
    # sound with it and the two cannot drift. Each one is a ring, a shake, a
    # light and a toll, and they accelerate into the gathering.
    INTRO_TOLLS = (0.02, 0.14, 0.245, 0.33, 0.40)
    # Where the motes start falling inward - and where the riser starts, cut
    # to climax a tenth of a second before the eye opens rather than a second
    # after it, which is what it used to do.
    INTRO_GATHER = 0.45

    ELITE_GLOW = 118.0
    ELITE_GLOW_STRENGTH = 26.0
    ELITE_LIGHT_HEIGHT = 16.0

    # Below this speed a flier inside a wall counts as resting there rather
    # than crossing, and is put back out at once; above it, it gets this long
    # to finish the crossing.
    # Opacity of the wall layer added into the light, by setting level.
    WALL_GLOW_LEVELS = (0, 26, 62)

    FLIER_REST_SPEED = 90.0
    FLIER_MAX_INSIDE = 0.45

    def draw(self, app):
        if self.deferred and gpu.lighting_ready():
            self._draw_lit(app)
        else:
            self._draw_flat(app)

    def _draw_lit(self, app):
        """Albedo, then light, then the two multiplied together.

        Everything solid goes into the scene buffer unlit; every light goes
        into the light buffer; the composite lights all of it at once. That is
        what makes an enemy fade up as the lantern reaches it instead of
        popping on, and what lets the masonry catch light without a rim stroke
        painted along each edge by hand.
        """
        cam = self.camera
        ox, oy = cam.ox, cam.oy
        lv = self.level
        player = self.player
        flicker = self.lantern_flicker()

        # ---- what shape the surfaces are ----------------------------------
        # First, because the relief is folded into the stone's own albedo
        # before anything is standing on it.
        scale = draw.SCALE
        has_normals = gpu.begin_normal() and lv.floor_normal is not None
        if has_normals:
            for other, dx, dy in self._other_levels():
                if other.floor_normal is not None:
                    drawImage(other.floor_normal, dx - ox, dy - oy)
                if other.wall_normal is not None:
                    drawImage(other.wall_normal, dx - ox, dy - oy)
            drawImage(lv.floor_normal, -ox, -oy)
            # Burns are part of the surface, so they shape the light too.
            self._draw_decals(ox, oy, normals=True)
            if lv.wall_normal is not None:
                drawImage(lv.wall_normal, -ox, -oy)
            self._draw_entity_normals(ox, oy)

        # ---- what is there ------------------------------------------------
        gpu.begin_scene(palette.VOID_RGB)
        # Any room being crossed into, first and whole - floor and walls
        # together - so the live room lands on top of it and the shared wall
        # between them is drawn once, by whichever owns it.
        for other, dx, dy in self._other_levels():
            drawImage(other.floor_image, dx - ox, dy - oy)
            if other.wall_image is not None:
                drawImage(other.wall_image, dx - ox, dy - oy)
        # The room itself. It takes the added light in full, because that
        # light is in the air above it and this is what it lands on.
        drawImage(lv.floor_image, -ox, -oy)
        self._draw_decals(ox, oy)
        self._draw_rift(ox, oy)
        self.pickups.draw(ox, oy, self.view_w, self.view_h)
        drawImage(lv.wall_image, -ox, -oy)
        # Everything from here on is a thing standing in the room rather than
        # the room, and is marked as such so the added light does not wash it
        # out. See `gpu.scene_coverage`.
        gpu.scene_coverage(True)
        self._draw_braziers(ox, oy)
        self._draw_fixture(ox, oy)
        self.hazards.draw(self, ox, oy)
        self._draw_doors(ox, oy)

        for e in self.enemies:
            if not e.alive:
                continue
            sx = e.x - ox
            sy = e.y - oy
            if sx < -90 or sy < -90 or sx > self.view_w + 90 or sy > self.view_h + 90:
                continue
            # Always the body. The light decides how much of it you see.
            e.draw_body(sx, sy)
            if e.lit and e.species != 'choir':
                e.draw_health(sx, sy)

        if player.alive:
            player.draw(ox, oy)
        # A bolt and a spark are light rather than things standing in the
        # room, so they go back to taking the room's share of it.
        gpu.scene_coverage(False)
        self.projectiles.draw(ox, oy, self.view_w, self.view_h)
        self.particles.draw(ox, oy, self.view_w, self.view_h)
        # Dust, last of all and in front of everything, because it is hanging
        # in the air between the room and the eye. Drawn into the albedo, so
        # the lighting decides whether any of it is visible: in the dark there
        # is no dust, and the beam fills as the lantern comes into a room.
        self.last_motes = self.motes.draw(
            ox, oy, LANTERN_GLOW, player.x, player.y,
            self.light_radius * self.MOTE_REACH, flicker * self.MOTE_STRENGTH)

        gpu.amplify_scene(self.ALBEDO_GAIN)

        # ---- what lights it -----------------------------------------------
        gpu.begin_light()
        self._draw_light(ox, oy)
        self._draw_point_lights(ox, oy, flicker)
        gpu.add_ambient(self.AMBIENT)

        # Masonry lit whatever the lantern is doing. Added into the light
        # buffer through the wall layer's own alpha, so it lands on stone and
        # nowhere else, and carries the courses with it rather than flooding
        # the room with a flat wash.
        if self.wall_glow and lv.wall_image is not None:
            gpu.set_mode(gpu.ADD)
            drawImage(lv.wall_image, -ox, -oy,
                      opacity=self.WALL_GLOW_LEVELS[
                          min(self.wall_glow, len(self.WALL_GLOW_LEVELS) - 1)])
            gpu.set_mode(gpu.NORMAL)

        # ---- and the two together -----------------------------------------
        gpu.composite(self.BLOOM, self.BLEED, art.dither_tile())

        # ---- light that lands on surfaces, not in the air ------------------
        # The masonry is baked very dark, so multiplying it by the light
        # buffer leaves it black however close the lantern gets. What a wall
        # actually shows is light *on* it, so that is added after the
        # composite where nothing can wash it out again.
        gpu.begin_edge_light()
        self._draw_wall_light(ox, oy, flicker)
        gpu.end_edge_light()

        self._draw_boss_corpse(ox, oy)
        self.effects.draw_beams(ox, oy)
        # ---- things that are not part of the world -------------------------
        # Eyes are emissive, so they belong on top of the lighting rather than
        # under it: an unlit enemy is a pair of eyes in the dark.
        for e in self.enemies:
            if e.alive and not e.lit:
                sx, sy = e.x - ox, e.y - oy
                if -90 < sx < self.view_w + 90 and -90 < sy < self.view_h + 90:
                    e.draw_glint(sx, sy)
        drawImage(self.overlay, 0, 0)
        self.effects.draw_texts(ox, oy)
        self.effects.draw_flash(self.view_w, self.view_h)

    def _draw_point_lights(self, ox, oy, flicker):
        """Every other light in the chamber, added into the light buffer.

        None of these cast shadows. They are small and short-lived, and the
        cost of a visibility sweep each is not worth an occlusion nobody would
        notice against the lantern's own.
        """
        gpu.set_mode(gpu.ADD)
        # The way down. It was drawn into the albedo only, which means it was
        # lit rather than lighting - and since it opens across the chamber
        # from wherever the fight ended, it sat in the dark and could not be
        # found without walking into it. As a light it announces itself from
        # the far side of the room, and cold against the lantern's warmth,
        # which is the one colour in the game that means somewhere to go.
        rift = self.rift
        if rift is not None and rift.open_t > 0.01:
            sx, sy = rift.x - ox, rift.y - oy
            if (-self.RIFT_LIGHT_RADIUS < sx < self.view_w + self.RIFT_LIGHT_RADIUS
                    and -self.RIFT_LIGHT_RADIUS < sy
                    < self.view_h + self.RIFT_LIGHT_RADIUS):
                t = ease_out_cubic(rift.open_t)
                breathe = 0.84 + 0.16 * math.sin(self.run_time * 2.1)
                self._static_light(rift, rift.x, rift.y, ox, oy,
                                   self.RIFT_LIGHT_RADIUS, self.RIFT_LIGHT,
                                   self.RIFT_LIGHT_STRENGTH * t * breathe,
                                   power=2.0, spread=t * breathe,
                                   height=self.RIFT_HEIGHT)
        for b in self.level.braziers:
            if not b.lit:
                continue
            sx, sy = b.x - ox, b.y - oy
            if sx < -260 or sy < -260 or sx > self.view_w + 260 or sy > self.view_h + 260:
                continue
            wobble = 0.85 + 0.15 * math.sin(self.run_time * 7.0 + b.x)
            self._static_light(b, b.x, b.y, ox, oy, self.BRAZIER_REACH,
                               palette.LIGHT_DEEP,
                               64 * b.ignite_t * flicker,
                               power=2.2, spread=wobble,
                               height=self.BRAZIER_HEIGHT)
        self._draw_fixture_light(ox, oy)
        self._draw_ward_lights(ox, oy)
        self.projectiles.draw_lights(ox, oy, self.view_w, self.view_h)
        self.effects.draw_lights(ox, oy, self.view_w, self.view_h)
        # Eyes throw just enough light to catch the ground under them. Any
        # more and a chamber full of enemies lights itself, which takes the
        # dark away from a game whose whole subject is the dark.
        #
        # An elite is the exception, and deliberately: it carries a low pool
        # of its own colour, so it is the one thing in a dark room you can see
        # coming. That turns clearing a floor into a decision about which
        # problem to take first, instead of waiting for the lantern to find
        # each one in turn.
        for e in self.enemies:
            if not e.alive:
                continue
            sx, sy = e.x - ox, e.y - oy
            if sx < -40 or sy < -40 or sx > self.view_w + 40 or sy > self.view_h + 40:
                continue
            art.draw_glow(art.rgb_tuple(e.eye_color), sx, sy, 15.0, 13,
                          power=3.0)
            if e.elite:
                breathe = 0.78 + 0.22 * math.sin(self.run_time * 3.4 + e.phase)
                art.draw_glow(art.rgb_tuple(e.elite_color), sx, sy,
                              self.ELITE_GLOW * breathe,
                              self.ELITE_GLOW_STRENGTH * breathe, power=2.3,
                              height=self.ELITE_LIGHT_HEIGHT)
            # Anything that carries its own light says so here. Only the
            # Keeper does, and it is the whole of that fight: its reach grows
            # as it dies, so the last room in the game fills up with light
            # until there is nowhere left to stand in the dark.
            glow = getattr(e, 'glow_radius', None)
            if glow is not None:
                reach = glow()
                breathe = 0.9 + 0.1 * math.sin(self.run_time * 1.7)
                art.draw_glow(art.rgb_tuple(e.eye_color), sx, sy,
                              reach * breathe, 58, power=2.0,
                              height=getattr(e, 'LIGHT_HEIGHT', 24.0))
        gpu.set_mode(gpu.NORMAL)

    # How far a brazier throws. Fixed, because its shadow is cast once at
    # this radius and then reused; the flame's wobble scales the brightness
    # and the visible reach, not the geometry.
    BRAZIER_REACH = 172.0
    # How high each light hangs above the floor, in design units. It decides
    # how steeply that light rakes: a brazier's bowl sits higher than a
    # carried lantern, and the rift is a hole in the floor, so its light comes
    # from very nearly floor level and picks out every ridge it crosses.
    BRAZIER_HEIGHT = 30.0
    RIFT_HEIGHT = 9.0

    def _static_light(self, owner, wx, wy, ox, oy, reach, color, strength,
                      power=2.2, spread=1.0, height=0.0):
        """A light that does not move, cast through the walls that block it.

        Braziers and the rift used to be plain radial glows, which meant they
        shone straight through masonry: a brazier on the far side of a wall
        lit the floor on this side of it. Only the lantern had ever cast a
        shadow, because only the lantern was worth a visibility sweep every
        frame.

        Neither of these moves, though, and neither do the walls, so the sweep
        is cast once the first time the light is drawn and kept. After that it
        costs one triangle per ray, which is the same thing the shafts do.
        """
        strength = clamp(strength, 0.0, 100.0)
        if strength <= 0.0:
            return
        if not hasattr(gpu, 'radial_fan') or not getattr(
                gpu, 'ANALYTIC_LIGHTS', False):
            art.draw_glow(art.rgb_tuple(color), wx - ox, wy - oy,
                          reach * spread, strength, power=power,
                          height=height)
            return
        shape = owner.shape
        if shape is None:
            fan = lighting.visibility_fan(self.level, wx, wy, reach)
            shape = owner.shape = fan.points
        if len(shape) < 3:
            return
        scale = draw.SCALE
        screen = [((x - ox) * scale, (y - oy) * scale) for x, y in shape]
        gpu.radial_fan((wx - ox) * scale, (wy - oy) * scale, reach * scale,
                       screen, art.rgb_tuple(color), strength, power=power,
                       height=height * scale)

    #: Temporary section timing for the draw path. `LUMEN_TRACE_DRAW=1`
    #: accumulates milliseconds per stage into `draw_marks`.
    TRACE_DRAW = bool(os.environ.get('LUMEN_TRACE_DRAW'))

    def _mark(self, name):
        if not self.TRACE_DRAW:
            return
        import time as _t
        now = _t.perf_counter()
        prev = getattr(self, '_mark_last', None)
        if prev is not None:
            slot = self.draw_marks.setdefault(self._mark_name, [0.0, 0])
            slot[0] += (now - prev) * 1000.0
            slot[1] += 1
        self._mark_last = now
        self._mark_name = name

    def _draw_flat(self, app):
        """The original single-pass path, for the cmu-graphics renderer."""
        cam = self.camera
        ox, oy = cam.ox, cam.oy
        lv = self.level
        player = self.player

        self._mark('light')
        # Light first, then the floor over it. See SHADOW_FLOOR_MIX.
        self._draw_light(ox, oy)
        self._mark('floor_blit')
        drawImage(lv.floor_image, -ox, -oy, opacity=SHADOW_FLOOR_MIX)
        self._mark('rift+pickups')
        self._draw_rift(ox, oy)
        self.pickups.draw(ox, oy, self.view_w, self.view_h)
        self._mark('wall_blit')
        drawImage(lv.wall_image, -ox, -oy)
        self._mark('wall_light')
        self._draw_wall_light(ox, oy, self.lantern_flicker())
        self._mark('braziers+wards')
        self._draw_braziers(ox, oy)
        self._draw_fixture(ox, oy)
        self.hazards.draw(self, ox, oy)
        self._draw_doors(ox, oy)
        # Pools, and then keeners' tethers, both under the bodies: they are
        # things on the floor and things between things, and either drawn
        # over a silhouette reads as being in front of it.
        self._draw_pools(ox, oy)
        for e in self.enemies:
            if e.alive and e.species == enemy_mod.KEENER:
                e.draw_link(ox, oy)
        self._mark('entities')

        for e in self.enemies:
            if not e.alive:
                continue
            sx = e.x - ox
            sy = e.y - oy
            if sx < -90 or sy < -90 or sx > self.view_w + 90 or sy > self.view_h + 90:
                continue
            e.draw(ox, oy, e.lit)
            if e.lit and e.species != 'choir':
                e.draw_health(sx, sy)

        if player.alive:
            player.draw(ox, oy)

        self._mark('proj+particles')
        self.projectiles.draw(ox, oy, self.view_w, self.view_h)
        self.particles.draw(ox, oy, self.view_w, self.view_h)
        self.effects.draw_lights(ox, oy, self.view_w, self.view_h)
        self._mark('overlay')
        drawImage(self.overlay, 0, 0)
        self.effects.draw_texts(ox, oy)
        self.effects.draw_flash(self.view_w, self.view_h)
        self._mark(None)

    def lantern_flicker(self):
        """The lantern's breathing, as a multiplier around 1.0.

        Shared, so the glow and the light it throws on the masonry breathe
        together instead of drifting apart.
        """
        return (1.0 + 0.03 * math.sin(self.run_time * 11.0)
                + 0.018 * math.sin(self.run_time * 27.0))

    def _draw_light(self, ox, oy):
        """Lantern light: a smooth glow, then the shadows carved back out.

        Layering shrunken copies of the visibility polygon (the obvious
        approach) produces visible concentric banding, because every layer
        boundary is a hard polygon edge. Instead the falloff comes from one
        pre-rendered radial sprite - genuinely smooth - and the occlusion
        comes from filling the wedges the sweep found to be blocked.
        """
        fan = self.light_fan
        if fan is None:
            return
        px, py = self.player.x, self.player.y
        radius = self.light_radius
        flicker = self.lantern_flicker()

        # A near-linear outer falloff so the lantern genuinely reaches its
        # radius, with two tighter sprites stacked on top for the hot core.
        # One sprite, not three. Scaling a large glow costs ~0.5 ms per call
        # because CPCS mode rebuilds the shape every frame and so never hits
        # the renderer's scaled-image cache; the falloff and the hot core are
        # baked into a single profile instead.
        if gpu.lighting_ready():
            self._draw_light_deferred(fan, px, py, radius, flicker, ox, oy)
            return

        if not NO_LANTERN:
            art.draw_lantern(LANTERN_GLOW, px - ox, py - oy, radius,
                             clamp(96 * flicker, 0, 100))

        if gpu.active():
            # One quad per ray step rather than one merged outline. The merge
            # exists to keep the shape count down for a renderer that charges
            # ~35 us a shape; here quads are nearly free, and drawing the
            # ribbon directly avoids having to triangulate an outline that can
            # cross itself at a concave corner - which was filling black
            # wedges across lit floor at the inside corners of a room.
            ribbons = fan.shadow_ribbons()
            self.last_wedges = len(ribbons)
            for inner, outer in ribbons:
                for i in range(len(inner) - 1):
                    ax, ay = inner[i]
                    bx, by = inner[i + 1]
                    cx, cy = outer[i + 1]
                    dx, dy = outer[i]
                    drawPolygon(ax - ox, ay - oy, bx - ox, by - oy,
                                cx - ox, cy - oy, dx - ox, dy - oy,
                                fill=palette.VOID, opacity=SHADOW_OPACITY)
            return

        bands = fan.shadow_bands()
        self.last_wedges = len(bands)
        for band in bands:
            shifted = []
            for i in range(0, len(band), 2):
                shifted.append(band[i] - ox)
                shifted.append(band[i + 1] - oy)
            drawPolygon(*shifted, fill=palette.VOID, opacity=SHADOW_OPACITY)



    # How many pieces a lit wall edge is cut into for the GPU's version of
    # the masonry light. Each piece is a flat-coloured quad, so this is the
    # resolution of the gradient along the wall: too few and the light reads
    # as a row of bricks rather than a glow. Pieces share endpoints exactly,
    # so they tile with neither gaps nor overlapping seams, and the ones the
    # light does not reach are culled before they are ever drawn.
    WALL_LIGHT_PIECES = 32

    def _draw_wall_light(self, ox, oy, flicker=1.0):
        """The lantern catching the edges of the masonry.

        On the GPU the light is built out of three strokes per piece of edge:
        a wide dim spill that bleeds off the stone, the lit rim itself, and a
        hot filament where the wall is close and square-on to the flame. The
        colour comes from `palette.wall_light`, so an edge runs from
        near-white beside the lantern down through amber to a dull ember at
        its reach, instead of being one warm tone at varying opacity.

        On the cmu-graphics renderer that is far out of budget - `drawLine`
        builds a rotated quad and is among its pricier calls - so it keeps the
        single flat stroke it was written for.
        """
        if gpu.active():
            self._draw_wall_light_rich(ox, oy, flicker)
        else:
            self._draw_wall_light_flat(ox, oy)
        self._draw_brazier_light(ox, oy)
        self._draw_rift_light(ox, oy)

    # A cast shadow is drawn three times, the outer end of each swung a hair
    # around the lantern. Where all three overlap the ground is fully dark;
    # along the edges only some do, and the partial products are the penumbra.
    # Swinging the *outer* end and leaving the inner one on the occluder is
    # what makes the softness grow with distance from whatever is casting it,
    # which is how a real shadow behaves.
    SHADOW_SPREAD = 0.016
    SHADOW_PASS = 104           # 0.41 per pass, so ~0.07 where all three land

    def _draw_light_deferred(self, fan, px, py, radius, flicker, ox, oy):
        """The lantern, into the light buffer rather than onto the floor."""
        gpu.set_mode(gpu.ADD)
        if not NO_LANTERN:
            art.draw_lantern(LANTERN_GLOW, px - ox, py - oy, radius,
                             clamp(96 * flicker, 0, 100),
                             height=self.LIGHT_HEIGHT)
            if self.volumetric:
                self._draw_shafts(fan, ox, oy, flicker)

        ribbons = fan.shadow_ribbons()
        self.last_wedges = len(ribbons)
        # Occlusion is collected rather than multiplied in as it is drawn, so
        # that two shadows crossing leave the floor as dark as one of them
        # would rather than compounding into a black wedge. See
        # `gpu.begin_shadow`.
        scale = draw.SCALE
        gpu.begin_shadow(self.SHADOW_PASS / 255.0)
        spreads = (-self.SHADOW_SPREAD, 0.0, self.SHADOW_SPREAD)
        for k, spread in enumerate(spreads):
            gpu.shadow_pass(k)
            cos_s, sin_s = math.cos(spread), math.sin(spread)
            for inner, outer in ribbons:
                for i in range(len(inner) - 1):
                    ax, ay = inner[i]
                    bx, by = inner[i + 1]
                    cx, cy = self._swing(outer[i + 1], px, py, cos_s, sin_s)
                    dx, dy = self._swing(outer[i], px, py, cos_s, sin_s)
                    gpu.shadow_quad((((ax - ox) * scale, (ay - oy) * scale),
                                     ((bx - ox) * scale, (by - oy) * scale),
                                     ((cx - ox) * scale, (cy - oy) * scale),
                                     ((dx - ox) * scale, (dy - oy) * scale)))
        gpu.end_shadow()
        gpu.set_mode(gpu.NORMAL)

    @staticmethod
    def _swing(point, px, py, cos_s, sin_s):
        """Rotate a point about the light by a small angle."""
        rx, ry = point[0] - px, point[1] - py
        return (px + rx * cos_s - ry * sin_s, py + rx * sin_s + ry * cos_s)

    # Radial bands the lit cone is filled in, from the flame outwards, and
    # what share of the shaft brightness each carries. Filling the cone flat
    # instead - one polygon at one opacity - lifts the whole visible region by
    # the same amount and leaves a hard circle at the lantern's reach, which
    # reads as a disc painted on the floor rather than as air catching light.
    # The lit cone is filled from the flame outwards in bands. Enough of them
    # that a step is a fraction of a level - and the frame-wide dither takes
    # care of what is left, which is why this can exist at all: at 22 bands it
    # put a visible ring every few percent of the radius.
    SHAFT_BAND_COUNT = 40
    SHAFT_STRENGTH = 7.0
    # How fast the air stops catching light with distance from the flame.
    # This was the band weighting; on the GPU it is the profile itself.
    SHAFT_FALLOFF = 2.4

    def _draw_shafts(self, fan, ox, oy, flicker):
        """The lit cone, added faintly - light with some air in it.

        Each band is a ring of quads between two fractions along the rays the
        visibility sweep already cast, so the fill stops exactly where the
        light does. Where the cone squeezes through a doorway the bands
        squeeze with it, and that is the shaft.
        """
        pts = fan.points
        n = len(pts)
        if n < 3:
            return
        px, py = fan.ox, fan.oy
        tint = palette.LIGHT_WARM
        if getattr(gpu, 'ANALYTIC_LIGHTS', False):
            # One triangle per ray, with the falloff evaluated per pixel. The
            # bands below exist only because the other renderer cannot do
            # that; this is the same profile without the forty steps.
            scale = draw.SCALE
            reach = max(1.0, self.light_radius)
            strength = clamp(self.SHAFT_STRENGTH * flicker, 0.0, 100.0)
            screen = [((x - ox) * scale, (y - oy) * scale) for x, y in pts]
            gpu.radial_fan((px - ox) * scale, (py - oy) * scale, reach * scale,
                           screen, (tint.red, tint.green, tint.blue),
                           strength, power=self.SHAFT_FALLOFF)
            return
        bands = self.SHAFT_BAND_COUNT
        step = 2
        for b in range(bands):
            lo = b / bands
            hi = (b + 1) / bands
            weight = (1.0 - lo) ** 2.4
            opacity = int(clamp(self.SHAFT_STRENGTH * weight * flicker, 0, 100))
            if opacity <= 0:
                continue
            for i in range(0, n - step, step):
                ax, ay = pts[i]
                bx, by = pts[i + step]
                drawPolygon(px + (ax - px) * lo - ox, py + (ay - py) * lo - oy,
                            px + (bx - px) * lo - ox, py + (by - py) * lo - oy,
                            px + (bx - px) * hi - ox, py + (by - py) * hi - oy,
                            px + (ax - px) * hi - ox, py + (ay - py) * hi - oy,
                            fill=tint, opacity=opacity)

    # How deep the light lying on a wall reaches, in design units. The split
    # either side of the edge lives with the profile itself, in `art`.
    EDGE_LIGHT_DEPTH = art.edge_light_span(0.0)[0]
    # How much of the painted-on wall light survives. It was written when a
    # wall had no shape for a light to find; now every light shades by the
    # surface it lands on, and most of this is doing that job twice.
    EDGE_LIGHT_STRENGTH = 1.0

    def _draw_wall_light_rich(self, ox, oy, flicker):
        """The light lying along a lit wall edge, as one gradient per piece.

        The profile across the wall is baked once (`art.edge_light`) and
        stretched and rotated onto each piece, so it is smooth by
        construction. Stacking half a dozen strokes at different widths - what
        this did before - leaves a hard line at the top of every one of them,
        and against the soft falloff the rest of the lighting now has, those
        read as lines drawn on the wall rather than as light landing on it.
        """
        pieces = lighting.lit_wall_segments(
            self.level, self.player.x, self.player.y, self.light_radius,
            height=self.LIGHT_HEIGHT)
        self.last_edges = len(pieces)
        if not gpu.active():
            return self._draw_wall_light_flat(ox, oy)

        self._draw_edge_pieces(pieces, ox, oy, flicker,
                               height=self.LIGHT_HEIGHT)

    def _draw_edge_pieces(self, pieces, ox, oy, gain=1.0, height=0.0,
                          tint=None):
        """One stretched gradient per lit piece of wall edge.

        Every light in the game lands on masonry through here, and that is the
        point: the profile is a baked texture stretched to the piece, so a
        second copy of this loop written slightly differently would put two
        visibly different kinds of light on the same stretch of wall. A
        brazier and the lantern differ only in `gain` - the flame's flicker,
        or how far through its ignition the brazier is.
        """
        scale = draw.SCALE
        # The brightest line goes on the edge, on every side.
        #
        # It was worth trying the other way. A wall's south edge has a side
        # face standing in front of its top, and on a real block the brightest
        # line is the arris where those two meet - a face's height into the
        # stone - so that is where the light was put. It measures correctly
        # and it looks wrong, because the face is nineteen design units and
        # the block behind it is a tile or more: at any real render scale that
        # is a thin strip, and a bright line a strip's width inside a wall
        # reads as a line floating in the masonry rather than as an arris on a
        # surface too small to register as a surface. The edge is what the eye
        # is tracking, so the light goes there.
        self._draw_edge_group(pieces, ox, oy, gain, self.wall_reach(height),
                              scale, tint)

    # How far into the stone a light carries, against how high it is held.
    # Quantised, because the profile is a baked texture and there is no point
    # keeping one per hundredth of a unit; three or four lights on a floor
    # share two or three of them.
    WALL_REACH_REF = 44.0
    WALL_REACH_MIN = 38.0
    WALL_REACH_MAX = 78.0

    @classmethod
    def wall_reach(cls, height):
        t = clamp(height / cls.WALL_REACH_REF, 0.0, 1.0)
        reach = cls.WALL_REACH_MIN + (cls.WALL_REACH_MAX
                                      - cls.WALL_REACH_MIN) * t
        return round(reach / 4.0) * 4.0

    def _draw_edge_group(self, pieces, ox, oy, gain, reach, scale,
                         tint=None):
        """One profile's worth of wall light, for edges that share a shape."""
        profile = art.edge_light(reach)
        depth, peak = art.edge_light_span(reach)
        # The quad is placed so the profile's bright line lands on the edge:
        # its front end then sits the spill's width out on the floor.
        offset = depth * (peak - 0.5)
        # A quad reaches `depth` out from its edge and half a piece along it,
        # so this margin cannot clip one that would have been visible.
        margin = depth + lighting.WALL_PIECE_LENGTH + 2.0
        for ax, ay, bx, by, s, nx, ny in pieces:
            s = s * gain
            if s <= 0.0:
                continue
            if s > 1.0:
                s = 1.0
            mx = (ax + bx) * 0.5 - ox + nx * offset
            my = (ay + by) * 0.5 - oy + ny * offset
            # Braziers light walls anywhere on the floor, including well off
            # the side of the screen; the lantern's own pieces are all near
            # the player and this costs them nothing.
            if (mx < -margin or my < -margin
                    or mx > self.view_w + margin or my > self.view_h + margin):
                continue
            length = math.hypot(bx - ax, by - ay)
            if length < 1e-6:
                continue
            color = palette.wall_light(s)
            if tint is None:
                cr, cg, cb = color.red, color.green, color.blue
            else:
                # `wall_light` runs from near-white down to ember, which is
                # the lantern's own ramp. A light of another colour keeps the
                # ramp's brightness and takes its hue from here instead.
                lum = (color.red + color.green + color.blue) / 765.0
                cr = int(tint[0] * lum)
                cg = int(tint[1] * lum)
                cb = int(tint[2] * lum)
            # The texture runs bright-edge-first down its own height, so the
            # quad is turned to put that axis along the outward normal.
            degrees = math.degrees(math.atan2(-ny, -nx)) - 90.0
            # Deliberately longer than the piece. The edge buffer keeps the
            # brighter of two overlapping draws rather than summing them, so
            # the overlap costs nothing and guarantees there is no sliver of
            # unlit wall between one piece and the next.
            gpu.blit_rot(profile, mx * scale, my * scale,
                         (length + lighting.WALL_PIECE_OVERLAP) * scale,
                         depth * scale, degrees,
                         color=(cr, cg, cb),
                         opacity=int((6 + 74 * s) * self.EDGE_LIGHT_STRENGTH))

    def _draw_wall_light_flat(self, ox, oy):
        # One stroke per edge, not two. `drawLine` builds a rotated quad and
        # is among the pricier calls in the library, and a dense chamber can
        # light forty edges at once.
        edges = lighting.lit_wall_edges(self.level, self.player.x, self.player.y,
                                        self.light_radius)
        self.last_edges = len(edges)
        for ax, ay, bx, by, s in edges:
            drawLine(ax - ox, ay - oy, bx - ox, by - oy,
                     fill=palette.wall_light(s), lineWidth=3,
                     opacity=int(6 + 62 * s))

    def _draw_brazier_light(self, ox, oy):
        """The same treatment for a lit brazier's own pool of light.

        A brazier and the walls are both static, so its lit edges are resolved
        once on ignition rather than every frame - which is why it can afford
        the same subdivision the lantern gets. Lighting whole edges at one
        strength was leaving a bright block on a wall with hard edges where
        the block ended, which is the segmentation that survived cutting the
        lantern's own edges up.
        """
        rich = gpu.active()
        for b in self.level.braziers:
            if not b.lit:
                continue
            # The cache is resolved for whichever renderer was live at
            # ignition, and only the subdivided form carries a normal. The
            # visual-quality setting can change under a lit brazier, so the
            # form it was built in is checked rather than assumed.
            if b.edges is None or b.edges_rich != rich:
                if rich:
                    b.edges = lighting.lit_wall_segments(
                        self.level, b.x, b.y, self.BRAZIER_REACH,
                        height=self.BRAZIER_HEIGHT)
                else:
                    b.edges = lighting.lit_wall_edges(self.level, b.x, b.y, 190)
                b.edges_rich = rich
            if rich:
                self._draw_edge_pieces(b.edges, ox, oy, b.ignite_t,
                                       height=self.BRAZIER_HEIGHT)
                continue
            for ax, ay, bx, by, s in b.edges:
                sx = ax - ox
                if sx < -260 or sx > self.view_w + 260:
                    continue
                lit = s * b.ignite_t
                drawLine(sx, ay - oy, bx - ox, by - oy,
                         fill=palette.wall_light(lit),
                         lineWidth=2, opacity=int(4 + 38 * lit))

    def _intro_cue(self, was, mark):
        """True on the one frame the arrival passes `mark`.

        `mark` is elapsed fraction and `boss_intro` counts down, so the edge
        is the time *remaining* at that point. Testing the interval the step
        crossed rather than the side it landed on is what makes each cue fire
        exactly once however long the frame was.
        """
        edge = self.BOSS_INTRO_TIME * (1.0 - mark)
        return was > edge >= self.boss_intro

    def _tick_boss_intro(self, dt):
        """A boss arrives instead of simply being there.

        Three beats, and they are all built out of light because that is the
        language the rest of the game speaks: the room answers first, then the
        thing itself gathers out of the dark, then it opens its eye and the
        fight starts. Nothing here takes control away from the player - you
        can walk about during it - but the boss does not act until it is done.
        """
        b = self.boss_ref
        if b is None:
            self.boss_intro = 0.0
            return
        was = self.boss_intro
        self.boss_intro = max(0.0, self.boss_intro - dt)
        t = 1.0 - self.boss_intro / max(self.BOSS_INTRO_TIME, 1e-6)
        col = art.rgb_tuple(b.eye_color)

        # 1. The room answers: one ring per toll, running outward from where
        #    it stands, each larger and louder than the last.
        for i, mark in enumerate(self.INTRO_TOLLS):
            if not self._intro_cue(was, mark):
                continue
            k = i / (len(self.INTRO_TOLLS) - 1)
            self.particles.ripple(b.x, b.y, b.eye_color,
                                  200 + 640 * k, 0.7, 58 + 34 * k)
            self.effects.add_light(b.x, b.y, 180 + 520 * k, 0.3, b.eye_color)
            self.effects.add_shake(2.2 + 6.8 * k)
            audio.play(f'{b.voice}_toll', 0.5 + 0.45 * k,
                       pitch=-1.0 + 2.0 * k)

        if self._intro_cue(was, self.INTRO_GATHER):
            # The riser goes through the room backwards, so its reflections
            # arrive ahead of it exactly as the motes do.
            audio.play(f'{b.voice}_gather', 0.9)

        # 2. It gathers: motes falling inward, and its light growing.
        if was > self.BOSS_INTRO_TIME * 0.16 and t >= self.INTRO_GATHER:
            # How far through the gathering we are, so the motes thicken at
            # the same rate the riser under them climbs. A flat 50 a second
            # against a sound that is visibly accelerating was the other half
            # of why this beat did not read as one event.
            g = clamp((t - self.INTRO_GATHER) / 0.39, 0.0, 1.0)
            self.effects.add_light(b.x, b.y, 90 + 420 * t, 0.14, b.eye_color)
            if self.fxrng.chance((24.0 + 96.0 * g) * dt):
                a = self.fxrng.angle()
                d = b.radius * self.fxrng.uniform(4.0, 11.0)
                self.particles.emit(
                    1, b.x + math.cos(a) * d, b.y + math.sin(a) * d,
                    -math.cos(a) * 260.0, -math.sin(a) * 260.0,
                    0.5, 4.2, b.eye_color, end_size=0.4, opacity=90)
            self.effects.add_shake(0.5 + 3.4 * g)
        # 3. It opens its eye.
        if was > 0.0 and self.boss_intro <= 0.0:
            self.effects.add_flash(0.95, b.eye_color, wash=True)
            self.effects.add_shake(13.0)
            self.effects.add_hitstop(0.12)
            self.effects.add_light(b.x, b.y, 760.0, 0.5, b.eye_color)
            self.particles.burst(b.x, b.y, 70, b.eye_color, self.fxrng,
                                 speed=(220, 660), life=(0.35, 0.9),
                                 size=(2.4, 6.0))
            self.particles.ripple(b.x, b.y, palette.LIGHT_CORE, 900, 0.8, 90)
            audio.play(f'{b.voice}_eye', 1.0)

    def begin_boss_death(self, enemy):
        """Hold the floor open while the thing comes apart.

        Everything it had in the air goes harmless immediately and unravels
        over the next half second. The camera is about to swing away to watch
        it die, and a bullet you cannot see is not a thing you can dodge -
        but deleting a screenful of shot outright reads as a bug, so they let
        go instead.
        """
        self.projectiles.unmake(projectile_mod.ENEMY)
        self.boss_death = self.BOSS_DEATH_TIME
        self.boss_corpse = [enemy.x, enemy.y, enemy.radius,
                            art.rgb_tuple(enemy.eye_color),
                            art.rgb_tuple(enemy.body_color)]
        self.effects.slowmo(2.2, 0.30)
        self.effects.add_shake(10.0)
        # Two and a bit seconds of slow motion to fill, so this is long and
        # it sags - a structure failing rather than an explosion. It is also
        # the sound this moment should always have had: `low` was never in
        # the bank, so `play` looked it up, found nothing and returned.
        audio.play(f'{enemy.voice}_break', 0.95)

    def _tick_boss_death(self, dt):
        """It does not simply stop existing.

        Two and a half seconds of coming apart: the body cracking open in
        bursts, the light it carried guttering and flaring, and then one last
        collapse that lights the whole chamber before the way down opens.
        """
        was = self.boss_death
        self.boss_death = max(0.0, self.boss_death - dt)
        if not self.boss_corpse:
            return
        x, y, r, eye, body = self.boss_corpse
        t = 1.0 - self.boss_death / max(self.BOSS_DEATH_TIME, 1e-6)

        # Cracking open, faster as it goes.
        if self.fxrng.chance((5.0 + 22.0 * t) * dt):
            a = self.fxrng.angle()
            d = r * self.fxrng.uniform(0.2, 1.5)
            px, py = x + math.cos(a) * d, y + math.sin(a) * d
            self.particles.burst(px, py, 16, eye, self.fxrng,
                                 speed=(140, 460), life=(0.25, 0.7),
                                 size=(2.0, 5.4))
            self.effects.add_light(px, py, 150.0 + 160.0 * t, 0.22, eye)
            self.effects.add_shake(2.0 + 4.0 * t)
            audio.play_at('boss_crack', px, py, 0.34 + 0.3 * t)
        if self.fxrng.chance(4.0 * dt):
            self.particles.ripple(x, y, eye, 200 + 500 * t, 0.5, 60)

        # The last collapse.
        if was > 0.0 and self.boss_death <= 0.0:
            self.effects.add_flash(1.0, palette.LIGHT_CORE, wash=True)
            self.effects.add_shake(18.0)
            self.effects.add_hitstop(0.16)
            self.effects.add_light(x, y, 1100.0, 0.9, palette.LIGHT_CORE)
            self.particles.burst(x, y, 120, palette.LIGHT_CORE, self.fxrng,
                                 speed=(260, 900), life=(0.5, 1.4),
                                 size=(2.6, 7.0))
            self.particles.ripple(x, y, palette.LIGHT_CORE, 1300, 1.0, 100)
            # The chamber goes white and the way down opens. The only effect
            # in the game that resolves upward, and it does the work `boom`
            # and `upgrade` were sharing.
            audio.play('boss_gone', 1.0)
            self.boss_corpse = None

    def _draw_boss_corpse(self, ox, oy):
        """What is left of it, shrinking and guttering as it comes apart."""
        if not self.boss_corpse:
            return
        x, y, r, eye, body = self.boss_corpse
        t = 1.0 - self.boss_death / max(self.BOSS_DEATH_TIME, 1e-6)
        sx, sy = x - ox, y - oy
        shrink = max(0.0, 1.0 - t) ** 0.6
        wobble = 1.0 + 0.18 * math.sin(self.run_time * 26.0)
        art.draw_glow(eye, sx, sy, (200 + 300 * t) * shrink,
                      int(clamp(70 * (1.0 - t) + 40 * t, 0, 100)), power=2.2)
        pts = []
        n = 9
        for i in range(n):
            a = self.run_time * 1.4 + i * math.tau / n
            d = r * shrink * wobble * (1.0 + 0.5 * t * self.fxrng.uniform(-1, 1))
            pts.append(sx + math.cos(a) * d)
            pts.append(sy + math.sin(a) * d)
        if shrink > 0.02:
            drawPolygon(*pts, fill=palette.VOID,
                        opacity=int(clamp(96 * shrink, 0, 100)))

    def _draw_rift_light(self, ox, oy):
        """The rift on the walls around it.

        It reaches the floor and nothing else, which is the one light in the
        game that opens across the chamber from wherever you are standing -
        so the walls beside it stayed dark and it read as a glow lying on the
        ground rather than as something in the room. Cast once, like its own
        pool: neither it nor the walls move.
        """
        rift = self.rift
        if rift is None or rift.open_t <= 0.01 or not gpu.active():
            return
        if rift.edges is None:
            rift.edges = lighting.lit_wall_segments(
                self.level, rift.x, rift.y, self.RIFT_LIGHT_RADIUS,
                height=self.RIFT_HEIGHT)
        if not rift.edges:
            return
        self._draw_edge_pieces(rift.edges, ox, oy,
                               ease_out_cubic(rift.open_t) * self.RIFT_ON_WALLS,
                               height=self.RIFT_HEIGHT,
                               tint=self.RIFT_LIGHT)

    # The ward across a sealed doorway. Cold, because it is the vault's
    # light and not the player's, and the two must never be mistaken for one
    # another: warm is yours and means safety, cold is the building's and
    # means a decision has been made for you.
    WARD_COLOR = palette.WARD
    WARD_REACH = 190.0
    WARD_HEIGHT = 26.0

    def _draw_doors(self, ox, oy):
        """Every doorway in the room: frame, leaves, and the seal on them.

        Drawn after the walls and before the bodies, so a leaf occludes the
        floor and the threshold but never the player standing in it.
        """
        if self.level is None or NO_WARDS:
            return
        for level, dx, dy in self._crossing_levels():
            for door in level.door_objects.values():
                sx = door.cx + dx - ox
                sy = door.cy + dy - oy
                if (sx < -110 or sy < -110 or sx > self.view_w + 110
                        or sy > self.view_h + 110):
                    continue
                door_mod.draw_frame(door, ox - dx, oy - dy)
                door_mod.draw_leaves(door, ox - dx, oy - dy, self.run_time,
                                     self._door_lit(door, dx, dy))

    def _door_lit(self, door, dx=0.0, dy=0.0):
        """Is the player's light reaching this doorway?

        Cheap, and deliberately generous: a door half in the light should
        draw as a lit door rather than flicker between two treatments as the
        player edges around it.
        """
        ddx = door.cx + dx - self.player.x
        ddy = door.cy + dy - self.player.y
        reach = self.light_radius + 60.0
        return (ddx * ddx + ddy * ddy) <= reach * reach

    def _draw_fixture(self, ox, oy):
        if self.fixture is not None:
            self.fixture.draw(ox, oy, self.run_time)
            self.fixture.draw_terms(ox, oy, self.player.x, self.player.y)

    def _draw_fixture_light(self, ox, oy):
        """What the room's offer throws on the stone around it.

        A cache and a hearth are worth finding from the doorway, so both are
        lights rather than shapes lying in the dark. The shrine is one too,
        and cold, so that a room with a bargain in it reads differently from
        a room with a gift in it before you have crossed the floor.
        """
        fixture = self.fixture
        if fixture is None or self.level is None:
            return
        k = 1.0 if not fixture.taken else clamp(
            1.0 - fixture.take_t / 1.4, 0.0, 1.0)
        if k <= 0.02:
            return
        reach = 300.0 if fixture.kind == fixture_mod.HEARTH else 210.0
        sx, sy = fixture.x - ox, fixture.y - oy
        if (sx < -reach or sy < -reach or sx > self.view_w + reach
                or sy > self.view_h + reach):
            return
        breathe = 0.84 + 0.16 * math.sin(self.run_time * 3.0)
        self._static_light(fixture, fixture.x, fixture.y, ox, oy, reach,
                           fixture.color, 58 * k * breathe, power=2.1,
                           spread=breathe, height=22.0)

    def _draw_ward_lights(self, ox, oy):
        """What the wards throw onto the stone around them."""
        if NO_WARDS or self.level is None:
            return
        for door in self.level.door_objects.values():
            k = (1.0 - door.open_t) + door.seam
            if k <= 0.02:
                continue
            sx, sy = door.cx - ox, door.cy - oy
            if (sx < -self.WARD_REACH or sy < -self.WARD_REACH
                    or sx > self.view_w + self.WARD_REACH
                    or sy > self.view_h + self.WARD_REACH):
                continue
            breathe = 0.82 + 0.18 * math.sin(self.run_time * 3.4 + door.phase)
            self._static_light(door, door.cx, door.cy, ox, oy,
                               self.WARD_REACH, self.WARD_COLOR,
                               24 * min(k, 1.4) * breathe, power=2.4,
                               spread=min(k, 1.0) * breathe,
                               height=self.WARD_HEIGHT)

    def _draw_braziers(self, ox, oy):
        for b in self.level.braziers:
            sx = b.x - ox
            sy = b.y - oy
            if sx < -60 or sy < -60 or sx > self.view_w + 60 or sy > self.view_h + 60:
                continue
            if b.lit:
                flick = 0.85 + 0.15 * math.sin(b.flicker * 13.0 + b.x * 0.05)
                art.draw_glow((255, 176, 92), sx, sy, 128 * b.ignite_t,
                              30 * flick * b.ignite_t, power=2.5)
                fire = 9.0 * flick
                drawPolygon(sx, sy - 15 * flick, sx + fire * 0.6, sy - 2,
                            sx, sy + 5, sx - fire * 0.6, sy - 2,
                            fill=palette.LIGHT_CORE, opacity=92)
            else:
                drawPolygon(sx - 7, sy - 3, sx + 7, sy - 3, sx + 5, sy + 7,
                            sx - 5, sy + 7, fill=palette.WALL_EDGE, opacity=70)
                if pulse(self.run_time, 1.6) > 0.5:
                    drawPolygon(sx - 2, sy - 6, sx + 2, sy - 6, sx + 2, sy - 2,
                                sx - 2, sy - 2, fill=palette.LIGHT_DEEP,
                                opacity=52)
            # Bowl.
            drawPolygon(sx - 9, sy + 6, sx + 9, sy + 6, sx + 6, sy + 13,
                        sx - 6, sy + 13, fill=palette.WALL_EDGE, opacity=86)

    def _draw_rift(self, ox, oy):
        rift = self.rift
        if rift is None:
            return
        sx = rift.x - ox
        sy = rift.y - oy
        t = ease_out_cubic(rift.open_t)
        art.draw_glow((150, 210, 255), sx, sy, 150 * t, 34 * t, power=2.2)
        spin = self.run_time * 1.4
        for ring in range(3):
            r = (16 + ring * 11) * t
            pts = []
            for i in range(7):
                a = spin * (1 + ring * 0.4) + i * math.tau / 7
                rr = r * (1.0 + 0.12 * math.sin(self.run_time * 3.0 + i + ring))
                pts.append(sx + math.cos(a) * rr)
                pts.append(sy + math.sin(a) * rr)
            drawPolygon(*pts, fill=palette.PLAYER_TRIM,
                        opacity=int((30 - ring * 7) * t))
        # Same reasoning as the lantern body: an emissive drawn into the
        # albedo gets lit and then bled onto, so it clips if it starts white.
        drawPolygon(sx, sy - 9 * t, sx + 9 * t, sy, sx, sy + 9 * t,
                    sx - 9 * t, sy, fill=palette.LIGHT_CORE, opacity=int(62 * t))

        if rift.hold > 0.01:
            frac = clamp(rift.hold / RIFT_HOLD, 0.0, 1.0)
            segs = max(1, int(20 * frac))
            r = 40.0
            for i in range(segs):
                a0 = -math.pi / 2 + i * math.tau / 20
                a1 = -math.pi / 2 + (i + 1) * math.tau / 20
                drawPolygon(sx + math.cos(a0) * r, sy + math.sin(a0) * r,
                            sx + math.cos(a1) * r, sy + math.sin(a1) * r,
                            sx + math.cos(a1) * (r + 4), sy + math.sin(a1) * (r + 4),
                            sx + math.cos(a0) * (r + 4), sy + math.sin(a0) * (r + 4),
                            fill=palette.LIGHT_CORE, opacity=88)

