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

from . import (art, audio, boss, enemies as enemy_mod, flow as flow_mod,
               level as level_mod, lighting, palette,
               particles as particle_mod, pickups as pickup_mod,
               projectiles as projectile_mod)
from .config import (BOSS_FLOORS, BRAZIER_IGNITE_FUEL, BRAZIER_REFILL_RANGE,
                     BRAZIER_REFILL_RATE, FLOOR_ENTRY_FUEL,
                     LANTERN_FLARE_DAMAGE, LANTERN_FLARE_KNOCKBACK,
                     LANTERN_FLARE_TIME, SHADOW_FLOOR_MIX, TILE)
from .fx import Camera, Effects
from .mathx import clamp, ease_out_cubic, from_angle, pulse
from .player import Player

# Seconds you must stand in an open rift before it takes you down.
RIFT_HOLD = 0.45
LANTERN_GLOW = (255, 198, 126)
SHADOW_OPACITY = int(os.environ.get('LUMEN_SHADOW_OPACITY', '100'))

# Radial bands the lit cone is filled in. Enough of them that no single step
# is a whole unit of opacity: nine put a ring every few percent of the radius,
# which against a dark floor is as visible as the 8-bit plateaus that had to
# be dithered out of the glow sprite itself.
_SHAFT_BAND_COUNT = 22
_SHAFT_BANDS = tuple(
    (i / _SHAFT_BAND_COUNT, (i + 1) / _SHAFT_BAND_COUNT,
     (1.0 - i / _SHAFT_BAND_COUNT) ** 2.4) for i in range(_SHAFT_BAND_COUNT))
# Test hook: render a frame with no lantern, to diff for light leaks.
NO_LANTERN = bool(os.environ.get('LUMEN_NO_LANTERN'))


class Rift:
    """The way down. Opens once the chamber is clear."""

    def __init__(self, x, y):
        self.x = x
        self.y = y
        self.open_t = 0.0
        self.hold = 0.0
        self.entered = False


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
        self.hunt_timer = 0.0
        self.banner = ''
        self.banner_t = 0.0

        self.overlay = art.screen_overlay(view_w, view_h, 0.94, 0.6, 0.05,
                                          0.1, 4)
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
    def enter_floor(self, depth):
        self.depth = depth
        self.is_boss = depth in BOSS_FLOORS
        art.clear_level_cache()
        self.level = level_mod.generate(depth, self.rng, boss=self.is_boss)

        self.player.x, self.player.y = self.level.player_start
        self.player.vx = self.player.vy = 0.0
        self.player.on_floor_start()
        self.player.refresh_from_stats()
        self.player.add_fuel(FLOOR_ENTRY_FUEL)

        self.enemies = []
        self.projectiles.clear()
        self.particles.clear()
        self.pickups.clear()
        self.effects.clear()
        self.rift = None
        self.cleared = False
        self.hunt_timer = 0.0
        self.boss_ref = None
        self.floor_time = 0.0

        self.flow = flow_mod.FlowField(self.level)
        self.flow.rebuild(self.player.x, self.player.y)

        # Upgrades change the lantern's reach, so re-bake the sizes this
        # floor can use while the screen is still behind the fade.
        self.prewarm_lantern_sizes()

        self.camera.set_bounds(self.level.width, self.level.height)
        self.camera.snap_to(self.player.x, self.player.y)

        self._populate()
        # The chamber's baked images are long-lived; keep them out of the
        # collector's way for the rest of the floor.
        gc.collect()
        gc.freeze()
        self.set_banner(f'FLOOR {depth}' if not self.is_boss else 'THE HOLLOW CHOIR')

    def _populate(self):
        spots = list(self.level.spawn_points)
        if self.is_boss:
            far = spots[0] if spots else (self.level.width * 0.5,
                                          self.level.height * 0.25)
            self.boss_ref = boss.HollowChoir(far[0], far[1], self.depth, self.rng)
            self.enemies.append(self.boss_ref)
            for key in enemy_mod.wave_for_depth(max(1, self.depth - 3), self.rng)[:5]:
                self._spawn_at_spot(enemy_mod.SPECIES[key], spots)
            return

        for key in enemy_mod.wave_for_depth(self.depth, self.rng):
            self._spawn_at_spot(enemy_mod.SPECIES[key], spots)

    def _spawn_at_spot(self, cls, spots):
        if not spots:
            spots = list(self.level.spawn_points) or [self.level.player_start]
        x, y = self.rng.choice(spots)
        x += self.rng.uniform(-TILE * 0.4, TILE * 0.4)
        y += self.rng.uniform(-TILE * 0.4, TILE * 0.4)
        x, y = self.level.collide_circle(x, y, cls.radius)
        self.enemies.append(cls(x, y, self.depth, self.rng))

    def set_banner(self, text, seconds=2.4):
        self.banner = text
        self.banner_t = seconds

    # ------------------------------------------- enemy-facing context API --
    def spawn_enemy(self, cls, x, y):
        if len(self.enemies) > 90:
            return
        x, y = self.level.collide_circle(x, y, cls.radius)
        self.enemies.append(cls(x, y, self.depth, self.rng))

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

        self.pickups.spawn(pickup_mod.EMBER, enemy.x, enemy.y, 1, self.rng,
                           count=enemy.ember_value)
        if self.rng.chance(0.16):
            self.pickups.spawn(pickup_mod.OIL, enemy.x, enemy.y, 20, self.rng)
        if self.rng.chance(0.055):
            self.pickups.spawn(pickup_mod.HEART, enemy.x, enemy.y, 14, self.rng)

        if enemy is self.boss_ref:
            self.effects.slowmo(1.4, 0.22)
            self.effects.add_flash(0.9, palette.BOSS_EYE, wash=True)
            self.pickups.spawn(pickup_mod.HEART, enemy.x, enemy.y, 18, self.rng,
                               count=3, speed=(90, 220))
            audio.play('boom', 1.0)
        else:
            audio.play('kill', 0.4)

    # ------------------------------------------------------------- update --
    def update(self, dt, keys, aim_world, firing):
        scale = self.effects.update(dt)
        sdt = dt * scale
        self.run_time += dt
        self.floor_time += dt
        self.banner_t = max(0.0, self.banner_t - dt)

        shake = self.effects.take_shake()
        if shake:
            self.camera.add_shake(shake)

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

        player.update(sdt, keys, dx, dy, self.level, self.particles, self.fxrng)

        # One BFS per tile the player crosses feeds every chaser and every
        # drifting ember for the rest of that tile.
        self.flow.maybe_rebuild(player.x, player.y)

        # Firing (charge weapons hold, others repeat).
        weapon = player.weapon
        if firing:
            if weapon.charge_time > 0.0:
                player.charging = True
                player.charge = min(weapon.charge_time, player.charge + sdt)
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
        self.camera.follow(player.x, player.y, aim_nx, aim_ny, dt)

        if not self.cleared and not any(e.alive for e in self.enemies):
            self._open_rift()

    def _collect(self, item):
        if item.kind == pickup_mod.EMBER:
            self.embers += item.value
            self.score += 5
        elif item.kind == pickup_mod.OIL:
            self.player.add_fuel(item.value)
        else:
            self.player.heal(item.value)

    def _update_lighting(self):
        player = self.player
        self.light_radius = player.lantern_radius
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
                if self.level.is_wall_tile(int(e.x // TILE), int(e.y // TILE)):
                    e.wall_time = getattr(e, 'wall_time', 0.0) + dt
                    if e.wall_time > 0.8:
                        e.wall_time = 0.0
                        ex, ey = self.level.collide_circle(e.x, e.y,
                                                           e.radius + 6.0)
                        self.particles.burst(e.x, e.y, 8, e.eye_color,
                                             self.fxrng, speed=(60, 200),
                                             life=(0.15, 0.35), size=(1.6, 3.2))
                        e.x, e.y = ex, ey
                else:
                    e.wall_time = 0.0

            e.slow = 0.0
            if slow:
                d2 = (e.x - player.x) ** 2 + (e.y - player.y) ** 2
                if d2 < 210.0 ** 2:
                    e.slow = slow
            e.update(dt, self)

            if light_dps and e.lit and e.alive:
                e.hp -= light_dps * dt
                if self.fxrng.chance(2.0 * dt):
                    self.particles.embers(e.x, e.y, 1, palette.LIGHT_CORE,
                                          self.fxrng)
                if e.hp <= 0.0:
                    e.die(self)

            if e.alive and player.alive and e.touch_cd <= 0.0:
                rr = e.radius + player.radius
                dx = player.x - e.x
                dy = player.y - e.y
                if dx * dx + dy * dy <= rr * rr:
                    angle = math.atan2(dy, dx)
                    if player.hurt(e.damage, self.effects, self.particles,
                                   self.fxrng, angle):
                        e.touch_cd = 0.65
                        self.camera.add_shake(4.5)
                        push = 240.0
                        player.vx += math.cos(angle) * push
                        player.vy += math.sin(angle) * push
                        e.vx -= math.cos(angle) * 160.0 / max(e.mass, 0.4)
                        e.vy -= math.sin(angle) * 160.0 / max(e.mass, 0.4)
                        if self.stats.thorns:
                            e.damage_by(self.stats.thorns, self, angle + math.pi,
                                        120.0)
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
                    dealt = e.damage_by(p.damage, self, angle, p.knockback,
                                        p.crit)
                    if dealt > 0.0:
                        self.player.damage_dealt += dealt
                        if self.stats.lifesteal:
                            player.heal(dealt * self.stats.lifesteal)
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
                audio.play('brazier', 0.55)

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
        self.cleared = True
        spots = self.level.spawn_points
        if spots:
            px, py = self.player.x, self.player.y
            best = min(spots, key=lambda s: abs(math.hypot(s[0] - px, s[1] - py) - 340))
        else:
            best = self.level.player_start
        self.rift = Rift(best[0], best[1])
        self.set_banner('THE WAY DOWN OPENS', 2.6)
        self.effects.add_light(best[0], best[1], 320, 1.2, palette.PLAYER_TRIM)
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

    def draw(self, app):
        if gpu.lighting_ready():
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

        # ---- what is there ------------------------------------------------
        gpu.begin_scene(palette.VOID_RGB)
        drawImage(lv.floor_image, -ox, -oy)
        self._draw_rift(ox, oy)
        self.pickups.draw(ox, oy, self.view_w, self.view_h)
        drawImage(lv.wall_image, -ox, -oy)
        self._draw_braziers(ox, oy)

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
        self.projectiles.draw(ox, oy, self.view_w, self.view_h)
        self.particles.draw(ox, oy, self.view_w, self.view_h)

        gpu.amplify_scene(self.ALBEDO_GAIN)

        # ---- what lights it -----------------------------------------------
        gpu.begin_light()
        self._draw_light(ox, oy)
        self._draw_point_lights(ox, oy, flicker)
        gpu.add_ambient(self.AMBIENT)

        # ---- and the two together -----------------------------------------
        gpu.composite(self.BLOOM, self.BLEED)

        # ---- light that lands on surfaces, not in the air ------------------
        # The masonry is baked very dark, so multiplying it by the light
        # buffer leaves it black however close the lantern gets. What a wall
        # actually shows is light *on* it, so that is added after the
        # composite where nothing can wash it out again.
        gpu.set_mode(gpu.ADD)
        self._draw_wall_light(ox, oy, flicker)
        gpu.set_mode(gpu.NORMAL)

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
        for b in self.level.braziers:
            if not b.lit:
                continue
            sx, sy = b.x - ox, b.y - oy
            if sx < -260 or sy < -260 or sx > self.view_w + 260 or sy > self.view_h + 260:
                continue
            r = 150.0 * (0.85 + 0.15 * math.sin(self.run_time * 7.0 + b.x))
            art.draw_glow(palette.LIGHT_DEEP, sx, sy, r,
                          clamp(64 * b.ignite_t * flicker, 0, 100), power=2.2)
        self.projectiles.draw_lights(ox, oy, self.view_w, self.view_h)
        self.effects.draw_lights(ox, oy, self.view_w, self.view_h)
        # Eyes throw just enough light to catch the ground under them. Any
        # more and a chamber full of enemies lights itself, which takes the
        # dark away from a game whose whole subject is the dark.
        for e in self.enemies:
            if not e.alive:
                continue
            sx, sy = e.x - ox, e.y - oy
            if sx < -40 or sy < -40 or sx > self.view_w + 40 or sy > self.view_h + 40:
                continue
            art.draw_glow(art.rgb_tuple(e.eye_color), sx, sy, 15.0, 13,
                          power=3.0)
        gpu.set_mode(gpu.NORMAL)

    def _draw_flat(self, app):
        """The original single-pass path, for the cmu-graphics renderer."""
        cam = self.camera
        ox, oy = cam.ox, cam.oy
        lv = self.level
        player = self.player

        # Light first, then the floor over it. See SHADOW_FLOOR_MIX.
        self._draw_light(ox, oy)
        drawImage(lv.floor_image, -ox, -oy, opacity=SHADOW_FLOOR_MIX)
        self._draw_rift(ox, oy)
        self.pickups.draw(ox, oy, self.view_w, self.view_h)

        drawImage(lv.wall_image, -ox, -oy)
        self._draw_wall_light(ox, oy, self.lantern_flicker())
        self._draw_braziers(ox, oy)

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

        self.projectiles.draw(ox, oy, self.view_w, self.view_h)
        self.particles.draw(ox, oy, self.view_w, self.view_h)
        self.effects.draw_lights(ox, oy, self.view_w, self.view_h)

        drawImage(self.overlay, 0, 0)

        self.effects.draw_texts(ox, oy)
        self.effects.draw_flash(self.view_w, self.view_h)

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
                             clamp(96 * flicker, 0, 100))
            # The lit region again, faintly, so the shafts the light throws
            # through a doorway read as air being lit rather than as a shape
            # cut out of the dark.
            self._draw_shafts(fan, ox, oy, flicker)

        gpu.set_mode(gpu.MOD)
        ribbons = fan.shadow_ribbons()
        self.last_wedges = len(ribbons)
        shade = palette.rgb(self.SHADOW_PASS, self.SHADOW_PASS,
                            self.SHADOW_PASS)
        for spread in (-self.SHADOW_SPREAD, 0.0, self.SHADOW_SPREAD):
            cos_s, sin_s = math.cos(spread), math.sin(spread)
            for inner, outer in ribbons:
                for i in range(len(inner) - 1):
                    ax, ay = inner[i]
                    bx, by = inner[i + 1]
                    cx, cy = self._swing(outer[i + 1], px, py, cos_s, sin_s)
                    dx, dy = self._swing(outer[i], px, py, cos_s, sin_s)
                    drawPolygon(ax - ox, ay - oy, bx - ox, by - oy,
                                cx - ox, cy - oy, dx - ox, dy - oy,
                                fill=shade, opacity=100)
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
    SHAFT_BANDS = _SHAFT_BANDS
    SHAFT_STRENGTH = 8.0

    def _draw_shafts(self, fan, ox, oy, flicker):
        """The lit cone, added faintly - light with some air in it.

        Each band is a ring of quads between two fractions along the same
        rays the visibility sweep already cast, so the fill stops exactly
        where the light does. Where the cone squeezes through a doorway the
        bands squeeze with it, which is the shaft.
        """
        pts = fan.points
        n = len(pts)
        if n < 3:
            return
        px, py = fan.ox, fan.oy
        tint = palette.LIGHT_WARM
        step = 2
        for lo, hi, weight in self.SHAFT_BANDS:
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

    # How far the light reaches back across the stone from a lit rim, in
    # design units, and how much of the rim's brightness survives that far.
    # This is what stops the lantern reading as a thin outline: the face of a
    # nearby block is lit, not just its corner.
    # (distance into the stone, share of the rim's brightness). The strokes
    # must overlap: a gap between two of them shows as a dark line running
    # along the wall, which is the same banding the piece count fixes in the
    # other direction.
    WALL_LIGHT_WASH_WIDTH = 3.2
    WALL_LIGHT_WASH = ((1.5, 0.54), (3.9, 0.30), (6.3, 0.15), (8.7, 0.06))

    # How deep the light lying on a wall reaches, in design units, and how
    # much of that is in front of the edge rather than behind it.
    EDGE_LIGHT_DEPTH = 15.0

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
            self.level, self.player.x, self.player.y, self.light_radius)
        self.last_edges = len(pieces)
        if not gpu.active():
            return self._draw_wall_light_flat(ox, oy)

        profile = art.edge_light()
        scale = draw.SCALE
        depth = self.EDGE_LIGHT_DEPTH
        # The gradient's bright line sits a fraction of the way down the
        # texture, so the quad is pushed forward to put that line on the edge.
        offset = depth * (art.EDGE_LIGHT_PEAK - 0.5)
        for ax, ay, bx, by, s, nx, ny in pieces:
            s = s * flicker
            if s > 1.0:
                s = 1.0
            ex, ey = bx - ax, by - ay
            length = math.hypot(ex, ey)
            if length < 1e-6:
                continue
            mx = (ax + bx) * 0.5 - ox + nx * offset
            my = (ay + by) * 0.5 - oy + ny * offset
            color = palette.wall_light(s)
            # The texture runs bright-edge-first down its own height, so the
            # quad is turned to put that axis along the outward normal.
            degrees = math.degrees(math.atan2(-ny, -nx)) - 90.0
            # Exactly the piece's length: the quads are additive, so any
            # overlap between two of them doubles up into a bright tick at
            # every boundary - a comb along the wall.
            gpu.blit_rot(profile, mx * scale, my * scale,
                         length * scale, depth * scale, degrees,
                         color=(color.red, color.green, color.blue),
                         opacity=int(6 + 74 * s))

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
            if b.edges is None:
                if rich:
                    b.edges = lighting.lit_wall_segments(
                        self.level, b.x, b.y, 190)
                else:
                    b.edges = lighting.lit_wall_edges(self.level, b.x, b.y, 190)
            for piece in b.edges:
                ax, ay, bx, by, s = piece[:5]
                sx = ax - ox
                if sx < -260 or sx > self.view_w + 260:
                    continue
                lit = s * b.ignite_t
                color = palette.wall_light(lit)
                # The cache was filled for whichever backend was live when the
                # brazier was lit; only the subdivided form carries a normal.
                if rich and len(piece) >= 7:
                    nx, ny = piece[5], piece[6]
                    drawLine(sx + nx * 1.4, ay - oy + ny * 1.4,
                             bx - ox + nx * 1.4, by - oy + ny * 1.4,
                             fill=color, lineWidth=6.0,
                             opacity=int(2 + 10 * lit * lit))
                    for dist, k in self.WALL_LIGHT_WASH:
                        drawLine(sx - nx * dist, ay - oy - ny * dist,
                                 bx - ox - nx * dist, by - oy - ny * dist,
                                 fill=color,
                                 lineWidth=self.WALL_LIGHT_WASH_WIDTH,
                                 opacity=int(38 * lit * k))
                drawLine(sx, ay - oy, bx - ox, by - oy, fill=color,
                         lineWidth=2, opacity=int(4 + 38 * lit))

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

