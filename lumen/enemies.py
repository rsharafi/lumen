"""The things in the dark.

Each species has a distinct silhouette, a distinct approach to the player, and
a distinct answer required from you: crawlers punish standing still, husks
punish panic, spitters punish open ground, wisps punish ignoring your fuel,
wardens punish shooting from the front.

Enemies outside the lantern's reach draw as nothing but eye-glints. That is
both the central visual idea of the game and, conveniently, a large saving in
draw calls.
"""

import math

from .draw import drawImage, drawLine, drawPolygon

from . import art, audio, palette
from .config import TILE
from .mathx import angle_diff, clamp, normalise, opacity as op

# Crawler silhouette: four body corners with a leg spike between each pair.
_CRAWLER_BODY = ((1.35, 0.0), (0.0, 0.95), (-0.9, 0.0), (0.0, -0.95))
_CRAWLER_LEGS = ((0.85, 1.75), (-1.25, 1.5), (-1.25, -1.5), (0.85, -1.75))

CRAWLER = 'crawler'
HUSK = 'husk'
SPITTER = 'spitter'
WISP = 'wisp'
WARDEN = 'warden'
CHOIR = 'choir'

# The second bestiary. The first five were about *how you move*; these are
# mostly about the lantern, because that is the thing this game has that no
# other one does and five species was not enough to interrogate it.
LURKER = 'lurker'
PALE = 'pale'
DOUSER = 'douser'
BOLTER = 'bolter'
KEENER = 'keener'
SPLITTER = 'splitter'
CINDER = 'cinder'
MIRROR = 'mirror'
DELVER = 'delver'
CARRION = 'carrion'


class Enemy:
    species = CRAWLER
    base_hp = 20.0
    base_speed = 170.0
    radius = 14.0
    touch_damage = 8.0
    ember_value = 1
    score = 10
    body_color = palette.CRAWLER
    eye_color = palette.CRAWLER_EYE
    flies = False
    mass = 1.0

    def __init__(self, x, y, depth, rng):
        self.x = x
        self.y = y
        self.vx = 0.0
        self.vy = 0.0
        self.rng = rng
        scale = 1.0 + 0.16 * (depth - 1)
        self.max_hp = self.base_hp * scale
        self.hp = self.max_hp
        self.speed = self.base_speed * (1.0 + 0.022 * (depth - 1))
        self.damage = self.touch_damage * (1.0 + 0.09 * (depth - 1))
        self.alive = True
        self.facing = rng.angle()
        self.hit_flash = 0.0
        self.stagger = 0.0
        self.phase = rng.uniform(0.0, 10.0)
        self.attack_cd = rng.uniform(0.3, 1.2)
        self.touch_cd = 0.0
        self.spawn_t = 0.55
        self.lit = False
        self.slow = 0.0
        # Elites: an affix name, or None for the ordinary case.
        self.elite = None
        self.elite_color = None
        self.ward = 0.0
        # Brief white flash on a warden's shield when it turns a shot.
        self.shield_flash = 0.0
        self.state = 'seek'
        self.state_t = 0.0
        self.wall_time = 0.0
        # Set when a floor drags on: stragglers stop playing keep-away.
        self.hunting = False
        # Which way this one prefers to slide around an obstruction.
        self.turn_bias = 1.0 if rng.chance(0.5) else -1.0

    # ------------------------------------------------------------ helpers --
    def separate(self, others, dt):
        """Cheap local repulsion so a pack does not collapse into one point."""
        px = py = 0.0
        r = self.radius * 2.1
        r2 = r * r
        for other in others:
            if other is self or not other.alive:
                continue
            dx = self.x - other.x
            dy = self.y - other.y
            d2 = dx * dx + dy * dy
            if 1e-6 < d2 < r2:
                inv = 1.0 / math.sqrt(d2)
                w = (1.0 - math.sqrt(d2) / r)
                px += dx * inv * w
                py += dy * inv * w
        if px or py:
            self.vx += px * 340.0 * dt
            self.vy += py * 340.0 * dt

    def steer_dir(self, dx, dy, dt, speed=None, accel=9.0):
        speed = self.speed if speed is None else speed
        speed *= (1.0 - self.slow)
        k = 1.0 - math.exp(-accel * dt)
        self.vx += (dx * speed - self.vx) * k
        self.vy += (dy * speed - self.vy) * k

    def steer_to(self, tx, ty, dt, speed=None, accel=9.0, level=None):
        dx, dy = normalise(tx - self.x, ty - self.y)
        if level is not None and not self.flies:
            dx, dy = self.avoid_walls(level, dx, dy)
        self.steer_dir(dx, dy, dt, speed, accel)

    def chase(self, dt, ctx, speed=None, accel=9.0):
        """Head for the player, around walls if need be.

        Fliers ignore geometry entirely; everything else follows the chamber's
        flow field when the player is out of sight, which is what stops a
        crawler pressing itself into a wall for the rest of the floor.
        """
        if self.flies:
            dx, dy = normalise(ctx.player.x - self.x, ctx.player.y - self.y)
        else:
            dx, dy = ctx.chase_dir(self.x, self.y)
        self.steer_dir(dx, dy, dt, speed, accel)

    def avoid_walls(self, level, dx, dy):
        """Deflect a heading that runs into masonry.

        Enemies steer straight at the player, which means a wall between the
        two leaves them pressed against it forever - and a floor that can only
        be cleared by the player walking around to them. Probing a short way
        ahead and sliding along the obstruction fixes it, and the per-enemy
        turn bias stops a pack from oscillating in the same doorway.
        """
        if not self._blocked_ahead(level, dx, dy):
            return dx, dy
        bias = self.turn_bias
        for magnitude in (0.8, 1.5, 2.3):
            for sign in (bias, -bias):
                angle = magnitude * sign
                c = math.cos(angle)
                s = math.sin(angle)
                nx = dx * c - dy * s
                ny = dx * s + dy * c
                if not self._blocked_ahead(level, nx, ny):
                    return nx, ny
        return dx, dy

    def _blocked_ahead(self, level, dx, dy):
        for reach in (self.radius + 20.0, self.radius + 52.0):
            px = self.x + dx * reach
            py = self.y + dy * reach
            if level.is_wall_tile(int(px // TILE), int(py // TILE)):
                return True
        return False

    def move(self, dt, level):
        nx = self.x + self.vx * dt
        ny = self.y + self.vy * dt
        if not self.flies:
            nx, ny = level.collide_circle(nx, ny, self.radius)
        nx = clamp(nx, self.radius, level.width - self.radius)
        ny = clamp(ny, self.radius, level.height - self.radius)
        self.x, self.y = nx, ny

    def face_velocity(self, dt, rate=10.0):
        if abs(self.vx) + abs(self.vy) > 8.0:
            want = math.atan2(self.vy, self.vx)
            self.facing += angle_diff(self.facing, want) * clamp(rate * dt, 0.0, 1.0)

    # ------------------------------------------------------------- update --
    def update(self, dt, ctx):
        self.spawn_t = max(0.0, self.spawn_t - dt)
        self.hit_flash = max(0.0, self.hit_flash - dt * 4.0)
        self.shield_flash = max(0.0, self.shield_flash - dt * 6.0)
        self.stagger = max(0.0, self.stagger - dt)
        self.touch_cd = max(0.0, self.touch_cd - dt)
        self.attack_cd = max(0.0, self.attack_cd - dt)
        self.state_t += dt
        self.phase += dt

        if self.spawn_t > 0.0:
            self.vx *= math.exp(-6.0 * dt)
            self.vy *= math.exp(-6.0 * dt)
            self.move(dt, ctx.level)
            return

        if self.stagger > 0.0:
            self.vx *= math.exp(-3.0 * dt)
            self.vy *= math.exp(-3.0 * dt)
            self.move(dt, ctx.level)
            return

        self.behave(dt, ctx)
        self.separate(ctx.enemies, dt)
        self.move(dt, ctx.level)
        self.face_velocity(dt)

    def behave(self, dt, ctx):
        self.chase(dt, ctx)

    # ------------------------------------------------------------- damage --
    def damage_by(self, amount, ctx, angle=None, knockback=0.0, crit=False):
        if not self.alive:
            return 0.0
        if self.ward > 0.0:
            # A ward soaks most of what lands on it until it breaks, which
            # asks for one large hit rather than a stream of small ones.
            soak = min(self.ward, amount * 0.72)
            self.ward -= soak
            amount -= soak
            if self.ward <= 0.0:
                self.ward = 0.0
                ctx.effects.add_text(self.x, self.y - self.radius - 12,
                                     'WARD BROKEN', palette.SHIELD, 15, True)
                ctx.effects.add_flash(0.34, palette.SHIELD)
                ctx.particles.burst(self.x, self.y, 30, palette.SHIELD,
                                    self.rng, speed=(160, 380),
                                    life=(0.2, 0.5), size=(2.0, 4.6))
        self.hp -= amount
        self.hit_flash = 1.0
        if knockback:
            a = angle if angle is not None else self.rng.angle()
            push = knockback / max(self.mass, 0.2)
            self.vx += math.cos(a) * push
            self.vy += math.sin(a) * push
            if push > 260.0:
                self.stagger = max(self.stagger, 0.12)

        ctx.particles.burst(
            self.x, self.y, 5 if not crit else 11,
            palette.CRIT if crit else palette.DAMAGE, self.rng,
            speed=(90, 300), life=(0.16, 0.36), size=(1.8, 3.8),
            direction=(angle if angle is not None else 0.0),
            spread=2.0 if angle is not None else math.tau)

        if crit:
            ctx.effects.add_damage(self.x, self.y - self.radius - 8,
                                   amount, palette.CRIT, 21, True)
            ctx.effects.add_hitstop(0.045)
            audio.play_at('crit', self.x, self.y, 0.5)
        else:
            ctx.effects.add_damage(self.x, self.y - self.radius - 6,
                                   amount, palette.UI_TEXT, 15, False)
            audio.play_at('hit', self.x, self.y, 0.32)

        if self.hp <= 0.0:
            self.die(ctx, angle)
            return amount
        return amount

    def die(self, ctx, angle=None):
        self.alive = False
        # Before the kill is booked, so anything a species leaves behind -
        # children, a pool of rot - exists by the time the world counts the
        # room's survivors. Booking first meant a splitter's last generation
        # could clear a warded room for a frame and unseal the doors.
        self.on_death(ctx)
        ctx.on_kill(self, angle)

    def on_death(self, ctx):
        """What this species leaves behind. Most leave nothing."""

    def on_touch(self, ctx):
        """Called when this one reaches the player, after the damage lands.

        Only the things that take something other than health implement it.
        """

    def immune(self):
        """True while nothing can touch it - a delver under the floor."""
        return False

    def death_burst(self, ctx, angle=None):
        ctx.particles.burst(self.x, self.y, 20, self.body_color, ctx.rng,
                            speed=(120, 420), life=(0.25, 0.65), size=(2.4, 5.6))
        ctx.particles.shards(self.x, self.y, 8, self.body_color, ctx.rng)
        ctx.particles.smoke(self.x, self.y, 4, palette.VOID, ctx.rng)
        ctx.particles.ripple(self.x, self.y, self.eye_color, self.radius * 3.4)
        ctx.effects.add_light(self.x, self.y, self.radius * 5.0, 0.2, self.eye_color)

    # --------------------------------------------------------------- draw --
    def draw(self, ox, oy, lit):
        sx = self.x - ox
        sy = self.y - oy
        if not lit:
            self.draw_glint(sx, sy)
            return
        self.draw_body(sx, sy)

    def draw_glint(self, sx, sy):
        """Unlit: two floating eyes and nothing else."""
        pulse = 0.6 + 0.4 * math.sin(self.phase * 3.2)
        size = 32
        drawImage(art.glow(_rgb_of(self.eye_color), size, power=2.4),
                  int(sx - size * 0.5), int(sy - size * 0.5),
                  opacity=op(22 + 20 * pulse))
        ca, sa = math.cos(self.facing), math.sin(self.facing)
        for side in (-1, 1):
            ex = sx + ca * 3.0 - sa * side * 4.0
            ey = sy + sa * 3.0 + ca * side * 4.0
            drawPolygon(ex - 1.7, ey - 1.7, ex + 1.7, ey - 1.7,
                        ex + 1.7, ey + 1.7, ex - 1.7, ey + 1.7,
                        fill=self.eye_color, opacity=op(58 + 34 * pulse))

    def body_opacity(self):
        if self.spawn_t > 0.0:
            return int(clamp(100 * (1.0 - self.spawn_t / 0.55), 10, 100))
        return 100

    def draw_body(self, sx, sy):
        color = palette.LIGHT_CORE if self.hit_flash > 0.45 else self.body_color
        opacity = self.body_opacity()
        r = self.radius
        ca, sa = math.cos(self.facing), math.sin(self.facing)
        drawPolygon(sx + ca * r * 1.4, sy + sa * r * 1.4,
                    sx - sa * r, sy + ca * r,
                    sx - ca * r, sy - sa * r,
                    sx + sa * r, sy - ca * r,
                    fill=color, opacity=opacity)
        self.draw_eyes(sx, sy, ca, sa, opacity)

    def draw_eyes(self, sx, sy, ca, sa, opacity, offset=0.45, size=2.1):
        for side in (-1, 1):
            ex = sx + ca * self.radius * offset - sa * side * self.radius * 0.36
            ey = sy + sa * self.radius * offset + ca * side * self.radius * 0.36
            drawPolygon(ex - size, ey - size, ex + size, ey - size,
                        ex + size, ey + size, ex - size, ey + size,
                        fill=self.eye_color, opacity=opacity)

    def draw_health(self, sx, sy):
        if self.hp >= self.max_hp - 0.01:
            return
        frac = clamp(self.hp / self.max_hp, 0.0, 1.0)
        w = self.radius * 2.2
        y = sy - self.radius - 11
        drawPolygon(sx - w * 0.5, y, sx + w * 0.5, y,
                    sx + w * 0.5, y + 3, sx - w * 0.5, y + 3,
                    fill=palette.UI_PANEL, opacity=62)
        drawPolygon(sx - w * 0.5, y, sx - w * 0.5 + w * frac, y,
                    sx - w * 0.5 + w * frac, y + 3, sx - w * 0.5, y + 3,
                    fill=palette.HP_RAMP[int(frac * (len(palette.HP_RAMP) - 1))],
                    opacity=86)


# --------------------------------------------------------------------------
class Crawler(Enemy):
    species = CRAWLER
    base_hp = 17.0
    base_speed = 196.0
    radius = 12.0
    touch_damage = 7.0
    ember_value = 1
    score = 10
    body_color = palette.CRAWLER
    eye_color = palette.CRAWLER_EYE
    mass = 0.7

    def behave(self, dt, ctx):
        # Crawlers lunge in bursts rather than gliding.
        cycle = (self.phase * 2.2) % 1.0
        boost = 1.55 if cycle < 0.35 else 0.62
        self.chase(dt, ctx, self.speed * boost, accel=7.0)

    def draw_body(self, sx, sy):
        color = palette.LIGHT_CORE if self.hit_flash > 0.45 else self.body_color
        opacity = self.body_opacity()
        r = self.radius
        ca, sa = math.cos(self.facing), math.sin(self.facing)
        scuttle = math.sin(self.phase * 13.0) * 0.35

        # Body and legs as one silhouette rather than a polygon plus four
        # `drawLine` calls. Lines are among the priciest shapes in the library
        # (each becomes a rotated quad), and a floor thick with crawlers was
        # spending about a millisecond a frame on legs alone. The spikes still
        # scuttle - the leg tips are driven by the same phase.
        pts = []
        for i in range(4):
            base = _CRAWLER_BODY[i]
            lx = base[0] * r
            ly = base[1] * r
            pts.append(sx + lx * ca - ly * sa)
            pts.append(sy + lx * sa + ly * ca)

            tip = _CRAWLER_LEGS[i]
            swing = scuttle * (1.0 if i % 2 == 0 else -1.0)
            lx = (tip[0] + swing * 0.32) * r
            ly = tip[1] * r * (1.0 + swing * 0.22)
            pts.append(sx + lx * ca - ly * sa)
            pts.append(sy + lx * sa + ly * ca)

        drawPolygon(*pts, fill=color, opacity=opacity)
        self.draw_eyes(sx, sy, ca, sa, opacity, offset=0.6, size=1.9)


class Husk(Enemy):
    species = HUSK
    base_hp = 78.0
    base_speed = 74.0
    radius = 21.0
    touch_damage = 19.0
    ember_value = 3
    score = 30
    body_color = palette.HUSK
    eye_color = palette.HUSK_EYE
    mass = 3.2

    def behave(self, dt, ctx):
        self.chase(dt, ctx, accel=3.2)

    def draw_body(self, sx, sy):
        color = palette.LIGHT_CORE if self.hit_flash > 0.45 else self.body_color
        opacity = self.body_opacity()
        r = self.radius
        ca, sa = math.cos(self.facing), math.sin(self.facing)
        heave = 1.0 + math.sin(self.phase * 3.4) * 0.05

        pts = []
        for i in range(6):
            a = self.facing + i * math.tau / 6 + 0.26
            pts.append(sx + math.cos(a) * r * heave)
            pts.append(sy + math.sin(a) * r * heave)
        drawPolygon(*pts, fill=color, opacity=opacity)

        # Heavy pauldrons.
        for side in (-1, 1):
            bx = sx - sa * side * r * 0.86
            by = sy + ca * side * r * 0.86
            drawPolygon(bx - 5, by - 5, bx + 5, by - 5, bx + 5, by + 5, bx - 5, by + 5,
                        fill=palette.WALL_EDGE, opacity=op(opacity * 0.8))

        ex = sx + ca * r * 0.42
        ey = sy + sa * r * 0.42
        drawPolygon(ex - 5.5, ey - 2.4, ex + 5.5, ey - 2.4,
                    ex + 5.5, ey + 2.4, ex - 5.5, ey + 2.4,
                    fill=self.eye_color, opacity=opacity)


class Spitter(Enemy):
    species = SPITTER
    base_hp = 26.0
    base_speed = 96.0
    radius = 14.0
    touch_damage = 6.0
    ember_value = 2
    score = 25
    body_color = palette.SPITTER
    eye_color = palette.SPITTER_EYE
    mass = 1.0
    ideal_range = 280.0

    def behave(self, dt, ctx):
        player = ctx.player
        dx = player.x - self.x
        dy = player.y - self.y
        d = math.hypot(dx, dy)

        if self.hunting:
            # No more kiting - close the distance and keep firing.
            self.chase(dt, ctx, self.speed * 1.25)
        elif d < self.ideal_range * 0.72:
            self.steer_to(self.x - dx, self.y - dy, dt, self.speed * 1.15,
                          level=ctx.level)
        elif d > self.ideal_range * 1.25:
            self.chase(dt, ctx)
        else:
            # Strafe to keep the shot lined up without closing.
            side = 1.0 if (int(self.phase * 0.4) % 2 == 0) else -1.0
            self.steer_to(self.x - dy * side, self.y + dx * side, dt,
                          self.speed * 0.8, level=ctx.level)

        self.facing += angle_diff(self.facing, math.atan2(dy, dx)) * clamp(7.0 * dt, 0, 1)

        if self.attack_cd <= 0.0 and d < 520 and not ctx.level.ray_blocked(
                self.x, self.y, player.x, player.y):
            self.fire(ctx, math.atan2(dy, dx))
            self.attack_cd = 2.1

    def fire(self, ctx, angle):
        self.state = 'shoot'
        self.state_t = 0.0
        for i in range(3):
            spread = (i - 1) * 0.13
            a = angle + spread
            ctx.projectiles.spawn(
                1, self.x + math.cos(a) * self.radius,
                self.y + math.sin(a) * self.radius,
                math.cos(a) * 330.0, math.sin(a) * 330.0,
                ctx.enemy_bullet_damage(9.0),
                radius=6.5, life=2.2, color=palette.ENEMY_BOLT_HOT,
                glow_color=(255, 120, 70), length=13.0, width=10.0,
                knockback=60.0)
        ctx.effects.add_light(self.x, self.y, 90, 0.16, (255, 130, 90))
        audio.play_at('enemy_shoot', self.x, self.y, 0.3)

    def face_velocity(self, dt, rate=10.0):
        pass  # Spitters always face the player, handled in behave.

    def draw_body(self, sx, sy):
        color = palette.LIGHT_CORE if self.hit_flash > 0.45 else self.body_color
        opacity = self.body_opacity()
        r = self.radius
        ca, sa = math.cos(self.facing), math.sin(self.facing)
        swell = 1.0 + max(0.0, 0.5 - self.attack_cd) * 0.5

        pts = []
        for i in range(7):
            a = self.facing + i * math.tau / 7
            rad = r * (1.0 + 0.16 * math.sin(self.phase * 2.6 + i)) * swell
            pts.append(sx + math.cos(a) * rad)
            pts.append(sy + math.sin(a) * rad)
        drawPolygon(*pts, fill=color, opacity=opacity)
        if self.hunting:
            drawPolygon(*pts, fill=None, border=palette.CRIT, borderWidth=2,
                        opacity=op(40 + 40 * abs(math.sin(self.phase * 4.0))))

        # Muzzle.
        mx = sx + ca * r * 1.15
        my = sy + sa * r * 1.15
        # The muzzle brightens as the next shot gets close.
        heat = clamp(1.0 - self.attack_cd / 2.1, 0.0, 1.0)
        drawPolygon(mx - sa * 4, my + ca * 4, mx + ca * 7, my + sa * 7,
                    mx + sa * 4, my - ca * 4,
                    fill=self.eye_color,
                    opacity=op(opacity * (0.55 + 0.45 * heat)))
        self.draw_eyes(sx, sy, ca, sa, opacity, offset=0.3, size=2.4)


class Wisp(Enemy):
    species = WISP
    base_hp = 22.0
    base_speed = 158.0
    radius = 11.0
    touch_damage = 5.0
    ember_value = 2
    score = 20
    body_color = palette.WISP
    eye_color = palette.WISP_EYE
    flies = True
    mass = 0.5
    fuel_drain = 13.0

    def behave(self, dt, ctx):
        player = ctx.player
        wobble = math.sin(self.phase * 2.7) * 1.15
        dx = player.x - self.x
        dy = player.y - self.y
        # Approach on a curve rather than a straight line.
        tx = player.x - dy * 0.34 * wobble
        ty = player.y + dx * 0.34 * wobble
        self.steer_to(tx, ty, dt, accel=4.5)
        if ctx.rng.chance(6.0 * dt):
            ctx.particles.emit(1, self.x, self.y,
                               ctx.rng.uniform(-18, 18), ctx.rng.uniform(-18, 18),
                               ctx.rng.uniform(0.4, 0.9), 2.4, self.eye_color,
                               end_size=0.4, opacity=60, drag=1.0)

    def draw_body(self, sx, sy):
        color = palette.LIGHT_CORE if self.hit_flash > 0.45 else self.eye_color
        opacity = self.body_opacity()
        r = self.radius * (1.0 + 0.14 * math.sin(self.phase * 4.4))
        size = 64
        drawImage(art.glow(_rgb_of(self.eye_color), size, power=2.3),
                  int(sx - size * 0.5), int(sy - size * 0.5),
                  opacity=op(opacity * 0.34))
        a = self.phase * 1.4
        ca, sa = math.cos(a), math.sin(a)
        drawPolygon(sx + ca * r, sy + sa * r, sx - sa * r * 0.62, sy + ca * r * 0.62,
                    sx - ca * r, sy - sa * r, sx + sa * r * 0.62, sy - ca * r * 0.62,
                    fill=palette.WISP, opacity=opacity)
        drawPolygon(sx + ca * r * 0.45, sy + sa * r * 0.45,
                    sx - sa * r * 0.3, sy + ca * r * 0.3,
                    sx - ca * r * 0.45, sy - sa * r * 0.45,
                    sx + sa * r * 0.3, sy - ca * r * 0.3,
                    fill=color, opacity=opacity)


class Warden(Enemy):
    species = WARDEN
    base_hp = 92.0
    base_speed = 104.0
    radius = 18.0
    touch_damage = 15.0
    ember_value = 4
    score = 45
    body_color = palette.WARDEN
    eye_color = palette.WARDEN_EYE
    mass = 2.6

    # The shield is a health pool, not a rule.
    #
    # It used to be a fixed arc that tracked you and returned zero damage from
    # the front, which meant the only answer was to walk around behind
    # something that was walking around to face you. That is not a fight, it
    # is an errand. Now it has a lot of health of its own, it narrows as it
    # takes damage, and it breaks. Shooting the front is a real - if slow -
    # option, and the arc shrinking as it goes is the progress bar: it needs
    # no numbers over its head, because you can see how much is left.
    #
    # Getting behind it still works, and still works *while the shield is up*.
    # It is a shortcut, not the only door.
    SHIELD_HP = 165.0
    SHIELD_ARC_FULL = 1.15      # radians either side of facing, intact
    SHIELD_ARC_SPENT = 0.26     # ...and just before it gives way

    def __init__(self, x, y, depth, rng):
        super().__init__(x, y, depth, rng)
        self.shield_max = self.SHIELD_HP * (1.0 + 0.18 * (depth - 1))
        self.shield_hp = self.shield_max

    @property
    def shield_arc(self):
        """How wide the shield still covers, from what is left of it."""
        if self.shield_hp <= 0.0:
            return 0.0
        frac = clamp(self.shield_hp / max(self.shield_max, 1e-6), 0.0, 1.0)
        return self.SHIELD_ARC_SPENT + (self.SHIELD_ARC_FULL
                                        - self.SHIELD_ARC_SPENT) * frac

    def behave(self, dt, ctx):
        self.chase(dt, ctx, accel=4.0)
        want = math.atan2(ctx.player.y - self.y, ctx.player.x - self.x)
        # The shield tracks you, but slowly enough that flanking works.
        self.facing += angle_diff(self.facing, want) * clamp(2.4 * dt, 0.0, 1.0)

    def face_velocity(self, dt, rate=10.0):
        pass

    def shield_blocks(self, angle_from):
        """True when an impact arriving from `angle_from` hits the shield."""
        if self.shield_hp <= 0.0:
            return False
        return abs(angle_diff(self.facing, angle_from)) < self.shield_arc

    def damage_by(self, amount, ctx, angle=None, knockback=0.0, crit=False):
        if angle is not None and self.shield_blocks(angle + math.pi):
            hit_a = angle + math.pi
            self.shield_hp -= amount
            ctx.particles.burst(
                self.x + math.cos(hit_a) * self.radius * 1.4,
                self.y + math.sin(hit_a) * self.radius * 1.4,
                7, palette.WARDEN_SHIELD, self.rng, speed=(120, 300),
                life=(0.14, 0.3), size=(1.8, 3.4),
                direction=hit_a, spread=1.7)
            if self.shield_hp <= 0.0:
                self.shield_hp = 0.0
                ctx.particles.burst(
                    self.x + math.cos(hit_a) * self.radius * 1.4,
                    self.y + math.sin(hit_a) * self.radius * 1.4,
                    26, palette.WARDEN_SHIELD, self.rng, speed=(180, 420),
                    life=(0.25, 0.6), size=(2.0, 4.4))
                ctx.effects.add_light(self.x, self.y, 130.0, 0.24,
                                      palette.WARDEN_SHIELD)
                audio.play_at('shield_break', self.x, self.y, 0.7)
            else:
                # No word over its head: the arc narrowing says it, and a
                # struck-metal ring says the shot did not land on flesh.
                self.shield_flash = 1.0
                audio.play_at('shield', self.x, self.y, 0.34)
            return 0.0
        return super().damage_by(amount, ctx, angle, knockback, crit)

    def draw_body(self, sx, sy):
        color = palette.LIGHT_CORE if self.hit_flash > 0.45 else self.body_color
        opacity = self.body_opacity()
        r = self.radius
        ca, sa = math.cos(self.facing), math.sin(self.facing)

        pts = []
        for i in range(5):
            a = self.facing + i * math.tau / 5 + math.pi / 5
            pts.append(sx + math.cos(a) * r)
            pts.append(sy + math.sin(a) * r)
        drawPolygon(*pts, fill=color, opacity=opacity)
        self.draw_eyes(sx, sy, ca, sa, opacity, offset=0.2, size=2.6)

        # The shield: a thick arc drawn as a strip of quads. Gone entirely
        # once it is spent, and thinner as well as narrower on the way there,
        # so its remaining strength is legible at a glance.
        if self.shield_hp <= 0.0:
            return
        frac = clamp(self.shield_hp / max(self.shield_max, 1e-6), 0.0, 1.0)
        segs = 5
        inner = r * 1.25
        outer = r * (1.28 + 0.27 * frac)
        for i in range(segs):
            a0 = self.facing - self.shield_arc + (2 * self.shield_arc) * i / segs
            a1 = self.facing - self.shield_arc + (2 * self.shield_arc) * (i + 1) / segs
            drawPolygon(sx + math.cos(a0) * inner, sy + math.sin(a0) * inner,
                        sx + math.cos(a1) * inner, sy + math.sin(a1) * inner,
                        sx + math.cos(a1) * outer, sy + math.sin(a1) * outer,
                        sx + math.cos(a0) * outer, sy + math.sin(a0) * outer,
                        fill=(palette.LIGHT_CORE if self.shield_flash > 0.4
                              else palette.WARDEN_SHIELD),
                        opacity=op(opacity * (0.34 + 0.34 * frac)))


# --------------------------------------------------------------------------
# Elites
# --------------------------------------------------------------------------
# A floor was a bag of the same five silhouettes at slowly rising health, and
# the only thing that changed between floor three and floor nine was how many
# of them there were. An elite is the cheapest way to make an encounter have a
# shape: one thing in the room is a different problem, and you can see which
# one it is before it reaches you, because an elite carries its own light.
#
# That last part matters more than the numbers. Everything else in this game
# is invisible until the lantern finds it; an elite announces itself from
# across a dark room, which turns "clear the floor" into "deal with that
# first, or last".

WARDED = 'warded'
SWIFT = 'swift'
GORGED = 'gorged'
EMBERFED = 'emberfed'

ELITE_AFFIXES = (WARDED, SWIFT, GORGED, EMBERFED)

ELITE_NAMES = {
    WARDED: 'WARDED',
    SWIFT: 'QUICKENED',
    GORGED: 'GORGED',
    EMBERFED: 'EMBER-FED',
}

ELITE_COLORS = {
    WARDED: palette.SHIELD,
    SWIFT: palette.DASH_TRAIL,
    GORGED: palette.DAMAGE,
    EMBERFED: palette.LIGHT_WARM,
}


def elite_chance(depth):
    """How likely any one spawn is to be an elite, by floor.

    Nothing on floor one - the first room should teach the ordinary case -
    then climbing to about one in five near the bottom.
    """
    if depth < 2:
        return 0.0
    return min(0.22, 0.045 * (depth - 1))


def make_elite(enemy, affix, depth):
    """Turn an ordinary enemy into an elite in place."""
    enemy.elite = affix
    enemy.elite_color = ELITE_COLORS[affix]
    enemy.radius *= 1.22
    enemy.score = int(enemy.score * 3)
    enemy.ember_value = enemy.ember_value * 3 + 2
    if affix == WARDED:
        # Takes far less damage until something breaks through: a health bar
        # that asks for burst rather than for chip.
        enemy.max_hp *= 2.4
        enemy.ward = enemy.max_hp * 0.45
    elif affix == SWIFT:
        enemy.speed *= 1.85
        enemy.max_hp *= 1.3
    elif affix == GORGED:
        enemy.max_hp *= 4.0
        enemy.speed *= 0.72
        enemy.damage *= 1.5
        enemy.radius *= 1.15
    elif affix == EMBERFED:
        enemy.max_hp *= 1.8
        enemy.ember_value = enemy.ember_value * 2 + 6
    enemy.hp = enemy.max_hp
    return enemy


# ==========================================================================
# The light-native three
#
# These are the species that could not exist in a game with room lighting.
# Everything above answers a question about movement; each of these answers
# one about the lantern, which is the only thing here worth building a
# bestiary around.
# ==========================================================================
class Lurker(Enemy):
    """It only moves while you are not looking at it.

    The whole game in one enemy. Your light is how you see and also how you
    stop it, and you cannot point it everywhere - so a room with three
    lurkers in it is a room where the lantern is a resource being spent on
    whichever one you fear most. Sweeping the light around carelessly is
    exactly the wrong instinct and the room teaches you that in about four
    seconds.

    Fast, because a frozen thing has to be frightening when it thaws. Not
    especially tough, because the counterplay has to be *look at it*, not
    *look at it for a long time*.
    """

    species = LURKER
    base_hp = 30.0
    base_speed = 246.0
    radius = 13.0
    touch_damage = 13.0
    ember_value = 3
    score = 35
    body_color = palette.LURKER
    eye_color = palette.LURKER_EYE
    mass = 1.0

    def __init__(self, x, y, depth, rng):
        super().__init__(x, y, depth, rng)
        #: 0 moving, 1 fully locked. Eased rather than switched, so being
        #: caught in the light reads as seizing up and not as a pause button.
        self.locked = 0.0

    def behave(self, dt, ctx):
        want = 1.0 if self.lit else 0.0
        # Locks fast and thaws slow: it should be the *release* of the light
        # that feels dangerous, not the catching.
        rate = 9.0 if want > self.locked else 2.6
        self.locked += (want - self.locked) * clamp(rate * dt, 0.0, 1.0)
        if self.locked > 0.92:
            self.vx *= math.exp(-14.0 * dt)
            self.vy *= math.exp(-14.0 * dt)
            return
        self.chase(dt, ctx, self.speed * (1.0 - self.locked))

    def face_velocity(self, dt, rate=10.0):
        # It always faces the player, lit or not. A statue turned away is a
        # statue you stop worrying about.
        pass

    def draw_body(self, sx, sy):
        color = palette.LIGHT_CORE if self.hit_flash > 0.45 else self.body_color
        opacity = self.body_opacity()
        r = self.radius
        # Tall and narrow, and it draws itself *up* as it locks - the shape
        # of something caught mid-step deciding to be a statue instead.
        stretch = 1.0 + 0.34 * self.locked
        lean = (1.0 - self.locked) * 0.22 * math.sin(self.phase * 7.0)
        h = r * 1.7 * stretch
        w = r * 0.78
        drawPolygon(sx - w + lean * h, sy - h,
                    sx + w + lean * h, sy - h,
                    sx + w * 1.15, sy + h * 0.75,
                    sx - w * 1.15, sy + h * 0.75,
                    fill=color, opacity=opacity)
        # The seam down it, which opens when it is free to move.
        if self.locked < 0.85:
            glow = op(opacity * (1.0 - self.locked) * 0.9)
            drawPolygon(sx - 2.0, sy - h * 0.8, sx + 2.0, sy - h * 0.8,
                        sx + 1.2, sy + h * 0.6, sx - 1.2, sy + h * 0.6,
                        fill=self.eye_color, opacity=glow)
        size = 2.3 + 0.7 * self.locked
        for side in (-1, 1):
            ex = sx + side * w * 0.46
            ey = sy - h * 0.52
            drawPolygon(ex - size, ey - size, ex + size, ey - size,
                        ex + size, ey + size, ex - size, ey + size,
                        fill=self.eye_color, opacity=opacity)


class Pale(Enemy):
    """It can only be hurt while your light is on it.

    The mirror of the lurker, and the reason both exist: one wants the
    lantern off it and the other wants it on, so a room holding both cannot
    be solved by pointing the light in one direction and leaving it there.

    Slow, because this is a positioning problem and not a reflex one. The
    flare is its natural answer - it lights everything at once - which is the
    first time in the game that button has had a job beyond crowd control.
    """

    species = PALE
    base_hp = 34.0
    base_speed = 82.0
    radius = 15.0
    touch_damage = 9.0
    ember_value = 3
    score = 34
    body_color = palette.PALE
    eye_color = palette.PALE_EYE
    mass = 1.6

    #: What survives of a hit landed while it is unlit. Not zero: a hard wall
    #: is memorable but a player with no light and no flare left would have
    #: no move at all, and a roguelite should never contain a locked door.
    DARK_SOAK = 0.08

    def damage_by(self, amount, ctx, angle=None, knockback=0.0, crit=False):
        if not self.lit:
            amount *= self.DARK_SOAK
            if ctx is not None and self.rng.chance(0.25):
                ctx.effects.add_text(self.x, self.y - self.radius - 10,
                                     'SHUT', palette.PALE_EYE, 11, False)
        return super().damage_by(amount, ctx, angle, knockback, crit)

    def behave(self, dt, ctx):
        # It closes faster in the dark, so leaving it unlit is not free.
        self.chase(dt, ctx, self.speed * (1.0 if self.lit else 1.45))

    def draw_body(self, sx, sy):
        color = palette.LIGHT_CORE if self.hit_flash > 0.45 else self.body_color
        opacity = self.body_opacity()
        r = self.radius
        ca, sa = math.cos(self.facing), math.sin(self.facing)
        # Plates, shut in the dark and open in the light. The state that
        # matters is the one the player must read at a glance, so it is the
        # silhouette that changes and not a tint.
        spread = 0.0 if not self.lit else 0.42
        for i in range(5):
            a = self.facing + i * math.tau / 5 + self.phase * 0.3
            out = r * (0.55 + spread)
            px = sx + math.cos(a) * out
            py = sy + math.sin(a) * out
            drawPolygon(px, py - r * 0.5, px + r * 0.5, py,
                        px, py + r * 0.5, px - r * 0.5, py,
                        fill=color, opacity=op(opacity * 0.92))
        core = self.eye_color if self.lit else palette.UI_FAINT
        cr = r * (0.5 if self.lit else 0.32)
        drawPolygon(sx, sy - cr, sx + cr, sy, sx, sy + cr, sx - cr, sy,
                    fill=core, opacity=op(opacity * (1.0 if self.lit else 0.5)))
        self.draw_eyes(sx, sy, ca, sa, opacity, offset=0.0, size=1.8)


class Douser(Enemy):
    """It puts your lantern out by touching you.

    Everything else in the vault takes health. This takes the thing health is
    *for*: it costs a slab of fuel and chokes the flame down for a few
    seconds, which in a dark room is far worse than the damage and is meant
    to be. The counterplay is simply never to let it arrive - it is fast and
    frail, and a player who respects it kills it at range every time.
    """

    species = DOUSER
    base_hp = 18.0
    base_speed = 232.0
    radius = 11.0
    touch_damage = 4.0
    ember_value = 3
    score = 32
    body_color = palette.DOUSER
    eye_color = palette.DOUSER_EYE
    mass = 0.7
    flies = True

    #: What one touch costs, and how long the flame stays choked for it.
    FUEL_BITE = 22.0
    CHOKE_TIME = 2.4

    def behave(self, dt, ctx):
        self.chase(dt, ctx)

    def on_touch(self, ctx):
        """Called by the world when it reaches the player."""
        player = ctx.player
        player.fuel = max(0.0, player.fuel - self.FUEL_BITE)
        player.choke = max(player.choke, self.CHOKE_TIME)
        ctx.effects.add_text(player.x, player.y - 30, 'SNUFFED',
                             palette.DOUSER_EYE, 15, True)
        ctx.effects.add_shake(3.0)
        audio.play_at('douse', self.x, self.y, 0.7)

    def draw_body(self, sx, sy):
        color = palette.LIGHT_CORE if self.hit_flash > 0.45 else self.body_color
        opacity = self.body_opacity()
        r = self.radius
        ca, sa = math.cos(self.facing), math.sin(self.facing)
        # A wet, trailing thing - a hood with nothing under it.
        pts = []
        for i in range(9):
            a = i * math.tau / 9
            # Longer behind than in front, so it reads as moving even when it
            # is not.
            drag = 1.0 + 0.5 * max(0.0, -math.cos(a - self.facing))
            rad = r * drag * (1.0 + 0.13 * math.sin(self.phase * 5.0 + i))
            pts.append(sx + math.cos(a) * rad)
            pts.append(sy + math.sin(a) * rad)
        drawPolygon(*pts, fill=color, opacity=op(opacity * 0.9))
        self.draw_eyes(sx, sy, ca, sa, opacity, offset=0.5, size=2.0)


# ==========================================================================
# The seven the roster needed anyway
#
# Not every enemy can be a thesis about light. A bestiary also needs the
# honest shapes that make fights legible: something that punishes standing in
# a lane, something that makes a target a priority, something that punishes
# clearing a room with area damage and walking away.
# ==========================================================================
class Bolter(Enemy):
    """It winds up, then crosses the room in a straight line.

    The one enemy in the vault you beat by *moving sideways*, which nothing
    else asks for. Its whole design is in the telegraph: it stops, it aims,
    the line it is about to take lights up, and then it is gone. If a player
    is ever hit by one without having been shown where it was going, this is
    tuned wrong.
    """

    species = BOLTER
    base_hp = 30.0
    base_speed = 118.0
    radius = 14.0
    touch_damage = 16.0
    ember_value = 3
    score = 34
    body_color = palette.BOLTER
    eye_color = palette.BOLTER_EYE
    mass = 1.5

    WIND_UP = 0.62
    CHARGE_TIME = 0.42
    CHARGE_SPEED = 940.0
    REACH = 520.0

    def __init__(self, x, y, depth, rng):
        super().__init__(x, y, depth, rng)
        self.wind = 0.0
        self.charging = 0.0
        self.aim = rng.angle()

    def behave(self, dt, ctx):
        player = ctx.player
        if self.charging > 0.0:
            self.charging -= dt
            self.steer_dir(math.cos(self.aim), math.sin(self.aim), dt,
                           self.CHARGE_SPEED, accel=22.0)
            return
        if self.wind > 0.0:
            self.wind -= dt
            # It keeps aiming right up to the last moment, but slower and
            # slower - so leading it works, and standing still does not.
            track = clamp(self.wind / self.WIND_UP, 0.0, 1.0) ** 2
            want = math.atan2(player.y - self.y, player.x - self.x)
            self.aim += angle_diff(self.aim, want) * clamp(6.0 * track * dt,
                                                           0.0, 1.0)
            self.vx *= math.exp(-8.0 * dt)
            self.vy *= math.exp(-8.0 * dt)
            if self.wind <= 0.0:
                self.charging = self.CHARGE_TIME
                audio.play_at('bolter_go', self.x, self.y, 0.6)
            return

        d = math.hypot(player.x - self.x, player.y - self.y)
        if (self.attack_cd <= 0.0 and d < self.REACH
                and not ctx.level.ray_blocked(self.x, self.y,
                                              player.x, player.y)):
            self.wind = self.WIND_UP
            self.aim = math.atan2(player.y - self.y, player.x - self.x)
            self.attack_cd = 2.6
            audio.play_at('bolter_tell', self.x, self.y, 0.5)
            return
        self.chase(dt, ctx, self.speed)

    def face_velocity(self, dt, rate=10.0):
        if self.wind > 0.0 or self.charging > 0.0:
            self.facing = self.aim
            return
        super().face_velocity(dt, rate)

    def draw_body(self, sx, sy):
        color = palette.LIGHT_CORE if self.hit_flash > 0.45 else self.body_color
        opacity = self.body_opacity()
        r = self.radius
        ca, sa = math.cos(self.facing), math.sin(self.facing)

        # The line it is about to take. This is the whole enemy: drawn to the
        # distance it will actually cover, brightening as it commits.
        if self.wind > 0.0:
            k = 1.0 - clamp(self.wind / self.WIND_UP, 0.0, 1.0)
            reach = self.CHARGE_SPEED * self.CHARGE_TIME
            ex = sx + ca * reach
            ey = sy + sa * reach
            wide = 3.0 + 5.0 * k
            drawPolygon(sx - sa * wide, sy + ca * wide,
                        ex - sa * wide, ey + ca * wide,
                        ex + sa * wide, ey - ca * wide,
                        sx + sa * wide, sy - ca * wide,
                        fill=self.eye_color, opacity=op(18 + 46 * k))

        # A wedge, blunt end forward.
        drawPolygon(sx + ca * r * 1.5, sy + sa * r * 1.5,
                    sx - sa * r * 0.95 - ca * r * 0.7,
                    sy + ca * r * 0.95 - sa * r * 0.7,
                    sx - ca * r * 1.25, sy - sa * r * 1.25,
                    sx + sa * r * 0.95 - ca * r * 0.7,
                    sy - ca * r * 0.95 - sa * r * 0.7,
                    fill=color, opacity=opacity)
        self.draw_eyes(sx, sy, ca, sa, opacity, offset=0.55, size=2.4)


class Keener(Enemy):
    """It keeps everything else alive, and never comes near you.

    The first enemy in the vault that makes a room a question of *order*.
    It hangs back, tethers itself to whatever is nearest the player, and
    mends it faster than a distracted player can take it down - so the fight
    is unwinnable until you go and deal with the thing that is not attacking
    you.

    The tether is drawn, always, lit or not. A priority target you cannot
    find is not a priority target, it is a mystery.
    """

    species = KEENER
    base_hp = 24.0
    base_speed = 128.0
    radius = 13.0
    touch_damage = 5.0
    ember_value = 4
    score = 40
    body_color = palette.KEENER
    eye_color = palette.KEENER_EYE
    mass = 1.0

    LINK_RANGE = 320.0
    MEND_RATE = 7.0
    KEEP_AWAY = 340.0

    def __init__(self, x, y, depth, rng):
        super().__init__(x, y, depth, rng)
        self.link = None

    def behave(self, dt, ctx):
        player = ctx.player
        dx = player.x - self.x
        dy = player.y - self.y
        d = math.hypot(dx, dy)

        # Find something worth mending: nearest to the player, not itself.
        best = None
        best_d = 1e18
        for other in ctx.enemies:
            if other is self or not other.alive or other.species == CHOIR:
                continue
            od = (other.x - self.x) ** 2 + (other.y - self.y) ** 2
            if od < best_d and od < self.LINK_RANGE ** 2:
                best_d = od
                best = other
        self.link = best

        if best is not None and best.hp < best.max_hp:
            best.hp = min(best.max_hp, best.hp + self.MEND_RATE * dt)

        if self.hunting or best is None:
            self.chase(dt, ctx)
        elif d < self.KEEP_AWAY:
            # Behind whatever it is holding up, and away from the player.
            self.steer_to(self.x - dx, self.y - dy, dt, self.speed,
                          level=ctx.level)
        else:
            self.steer_to(best.x, best.y, dt, self.speed * 0.7,
                          level=ctx.level)

    def draw_body(self, sx, sy):
        color = palette.LIGHT_CORE if self.hit_flash > 0.45 else self.body_color
        opacity = self.body_opacity()
        r = self.radius
        ca, sa = math.cos(self.facing), math.sin(self.facing)
        pts = []
        for i in range(3):
            a = self.facing + i * math.tau / 3 + self.phase * 0.9
            pts.append(sx + math.cos(a) * r * 1.25)
            pts.append(sy + math.sin(a) * r * 1.25)
        drawPolygon(*pts, fill=color, opacity=opacity)
        drawPolygon(sx, sy - r * 0.42, sx + r * 0.42, sy,
                    sx, sy + r * 0.42, sx - r * 0.42, sy,
                    fill=self.eye_color,
                    opacity=op(opacity * (0.6 + 0.4 * math.sin(self.phase * 5.0))))
        self.draw_eyes(sx, sy, ca, sa, opacity, offset=0.0, size=1.7)

    def draw_link(self, ox, oy):
        """The tether, drawn by the world so it sits under the bodies."""
        other = self.link
        if other is None or not other.alive or not self.alive:
            return
        drawLine(self.x - ox, self.y - oy, other.x - ox, other.y - oy,
                 fill=palette.KEENER_LINK,
                 opacity=op(30 + 26 * abs(math.sin(self.phase * 4.0))),
                 lineWidth=2.0)


class Splitter(Enemy):
    """Kill it and it becomes two smaller ones.

    A tax on area damage taken without follow-through. It is deliberately
    easy to kill: the cost is not in killing it, it is in what killing it
    does to the room a second later.
    """

    species = SPLITTER
    base_hp = 28.0
    base_speed = 150.0
    radius = 16.0
    touch_damage = 8.0
    ember_value = 2
    score = 26
    body_color = palette.SPLITTER
    eye_color = palette.SPLITTER_EYE
    mass = 1.2

    #: How many generations may split. Two is the whole design: one split is
    #: a gimmick nobody notices and three fills the room with confetti.
    GENERATIONS = 2

    def __init__(self, x, y, depth, rng, generation=0):
        super().__init__(x, y, depth, rng)
        self.generation = generation
        shrink = 0.62 ** generation
        self.radius = Splitter.radius * shrink
        self.max_hp *= shrink
        self.hp = self.max_hp
        self.speed *= 1.0 + 0.28 * generation
        self.damage *= shrink

    def behave(self, dt, ctx):
        self.chase(dt, ctx)

    def on_death(self, ctx):
        if self.generation >= self.GENERATIONS:
            return
        for i in (-1, 1):
            a = self.facing + i * 1.1
            child = Splitter(self.x + math.cos(a) * self.radius,
                             self.y + math.sin(a) * self.radius,
                             ctx.depth, ctx.rng,
                             generation=self.generation + 1)
            child.vx = math.cos(a) * 260.0
            child.vy = math.sin(a) * 260.0
            child.spawn_t = 0.22
            ctx.adopt_enemy(child)
        audio.play_at('split', self.x, self.y, 0.6)

    def draw_body(self, sx, sy):
        color = palette.LIGHT_CORE if self.hit_flash > 0.45 else self.body_color
        opacity = self.body_opacity()
        r = self.radius
        ca, sa = math.cos(self.facing), math.sin(self.facing)
        # Two lobes with a seam between them, so what it is about to do is
        # written on it before it does it.
        for side in (-1, 1):
            cx = sx - sa * r * 0.34 * side
            cy = sy + ca * r * 0.34 * side
            pts = []
            for i in range(7):
                a = i * math.tau / 7
                rad = r * 0.78 * (1.0 + 0.1 * math.sin(self.phase * 3.0 + i))
                pts.append(cx + math.cos(a) * rad)
                pts.append(cy + math.sin(a) * rad)
            drawPolygon(*pts, fill=color, opacity=opacity)
        self.draw_eyes(sx, sy, ca, sa, opacity, offset=0.3, size=1.9 * (r / 16.0))


class Cinder(Enemy):
    """Small, quick, and never alone.

    Everything else in the vault is fought one at a time. These arrive as six
    and are the reason a weapon with no reach and no spread feels different
    from one with both - which is a difference the game claims its six
    weapons are built around and, before these, never actually tested.
    """

    species = CINDER
    base_hp = 7.0
    base_speed = 268.0
    radius = 7.0
    touch_damage = 4.0
    ember_value = 1
    score = 8
    body_color = palette.CINDER
    eye_color = palette.CINDER_EYE
    mass = 0.4
    flies = True

    def behave(self, dt, ctx):
        # It weaves rather than driving straight in, so a swarm arrives as a
        # cloud and not as a queue.
        player = ctx.player
        dx, dy = normalise(player.x - self.x, player.y - self.y)
        wobble = math.sin(self.phase * 6.0) * 0.55
        c, sn = math.cos(wobble), math.sin(wobble)
        self.steer_dir(dx * c - dy * sn, dx * sn + dy * c, dt, self.speed,
                       accel=11.0)

    def draw_body(self, sx, sy):
        color = palette.LIGHT_CORE if self.hit_flash > 0.45 else self.eye_color
        opacity = self.body_opacity()
        r = self.radius * (1.0 + 0.16 * math.sin(self.phase * 9.0))
        drawPolygon(sx, sy - r, sx + r * 0.8, sy, sx, sy + r,
                    sx - r * 0.8, sy, fill=color, opacity=op(opacity * 0.95))


class Mirror(Enemy):
    """Shots come back off its face.

    It exists to make the flare and the dash worth having on a build that
    never needed them. Everything about it is slow and readable - it turns
    to face you at a fixed rate, so getting behind it is always possible and
    always a decision to spend a second on.
    """

    species = MIRROR
    base_hp = 40.0
    base_speed = 84.0
    radius = 15.0
    touch_damage = 8.0
    ember_value = 4
    score = 42
    body_color = palette.MIRROR
    eye_color = palette.MIRROR_EYE
    mass = 2.0

    #: Half-width of the reflecting arc, in radians.
    FACE_ARC = 1.05
    TURN_RATE = 2.2

    def behave(self, dt, ctx):
        player = ctx.player
        want = math.atan2(player.y - self.y, player.x - self.x)
        self.facing += angle_diff(self.facing, want) * clamp(
            self.TURN_RATE * dt, 0.0, 1.0)
        self.chase(dt, ctx, self.speed)

    def face_velocity(self, dt, rate=10.0):
        pass

    def reflects(self, angle_of_travel):
        """Would a shot travelling this way come back off the face?"""
        # The bolt is coming *at* it, so compare against the reversed heading.
        incoming = angle_of_travel + math.pi
        return abs(angle_diff(self.facing, incoming)) < self.FACE_ARC

    def draw_body(self, sx, sy):
        color = palette.LIGHT_CORE if self.hit_flash > 0.45 else self.body_color
        opacity = self.body_opacity()
        r = self.radius
        ca, sa = math.cos(self.facing), math.sin(self.facing)
        drawPolygon(sx - ca * r * 0.9 - sa * r, sy - sa * r * 0.9 + ca * r,
                    sx - ca * r * 0.9 + sa * r, sy - sa * r * 0.9 - ca * r,
                    sx + ca * r * 0.5 + sa * r * 0.6,
                    sy + sa * r * 0.5 - ca * r * 0.6,
                    sx + ca * r * 0.5 - sa * r * 0.6,
                    sy + sa * r * 0.5 + ca * r * 0.6,
                    fill=color, opacity=opacity)
        # The face, drawn as the thing it is: a bright plate across the front.
        fx = sx + ca * r * 0.72
        fy = sy + sa * r * 0.72
        shine = 0.55 + 0.45 * abs(math.sin(self.phase * 2.0))
        drawPolygon(fx - sa * r * 0.95, fy + ca * r * 0.95,
                    fx + sa * r * 0.95, fy - ca * r * 0.95,
                    fx + sa * r * 0.8 + ca * 3, fy - ca * r * 0.8 + sa * 3,
                    fx - sa * r * 0.8 + ca * 3, fy + ca * r * 0.8 + sa * 3,
                    fill=self.eye_color, opacity=op(opacity * shine))
        self.draw_eyes(sx, sy, ca, sa, opacity, offset=-0.35, size=1.9)


class Delver(Enemy):
    """It travels under the floor and comes up beneath you.

    Untouchable while it is down, which would be intolerable if it were not
    also loudly signposted: the ground breaks where it is about to surface,
    a full second before it does. The answer is to walk off the mark - which
    is to say, the answer is to look at the floor, in a game that spends most
    of its time asking you to look at the dark.
    """

    species = DELVER
    base_hp = 34.0
    base_speed = 210.0
    radius = 14.0
    touch_damage = 15.0
    ember_value = 3
    score = 36
    body_color = palette.DELVER
    eye_color = palette.DELVER_EYE
    mass = 1.4

    SURFACE_TELL = 0.95
    DIVE_AFTER = 3.4

    def __init__(self, x, y, depth, rng):
        super().__init__(x, y, depth, rng)
        self.buried = 0.0          # seconds left underground
        self.tell = 0.0            # seconds left of the surfacing mark
        self.above_t = rng.uniform(1.5, 3.0)

    @property
    def submerged(self):
        return self.buried > 0.0 and self.tell <= 0.0

    def immune(self):
        # Under the floor. Shots pass over it, and the lantern cannot burn
        # it either - the tell is the window, and it is a whole second long.
        return self.submerged

    def behave(self, dt, ctx):
        player = ctx.player
        if self.buried > 0.0:
            self.buried -= dt
            if self.tell > 0.0:
                self.tell -= dt
                self.vx = self.vy = 0.0
                if self.tell <= 0.0:
                    self.buried = 0.0
                    self.above_t = self.DIVE_AFTER
                    ctx.effects.add_shake(2.4)
                    ctx.particles.burst(self.x, self.y, 22, palette.DELVER,
                                        self.rng, speed=(120, 380),
                                        life=(0.2, 0.5), size=(2.0, 4.4))
                    audio.play_at('delve_up', self.x, self.y, 0.7)
                return
            # Under the floor: it tracks the player and cannot be touched.
            self.steer_to(player.x, player.y, dt, self.speed * 1.35,
                          level=None)
            if self.buried <= self.SURFACE_TELL:
                self.tell = self.SURFACE_TELL
                audio.play_at('delve_tell', self.x, self.y, 0.5)
            return

        self.above_t -= dt
        if self.above_t <= 0.0:
            self.buried = 2.0 + self.rng.uniform(0.0, 0.8)
            ctx.particles.burst(self.x, self.y, 16, palette.DELVER, self.rng,
                                speed=(80, 240), life=(0.2, 0.5),
                                size=(1.8, 3.8))
            audio.play_at('delve_down', self.x, self.y, 0.6)
            return
        self.chase(dt, ctx)

    def draw_body(self, sx, sy):
        opacity = self.body_opacity()
        r = self.radius
        if self.submerged:
            return                      # nothing to see; it is under you
        if self.tell > 0.0:
            # The mark. It closes in on the point it will come up at, so what
            # the circle covers when it shuts is exactly what gets hit.
            k = 1.0 - clamp(self.tell / self.SURFACE_TELL, 0.0, 1.0)
            rad = r * (2.6 - 1.4 * k)
            pts = []
            for i in range(10):
                a = i * math.tau / 10
                pts.append(sx + math.cos(a) * rad)
                pts.append(sy + math.sin(a) * rad)
            drawPolygon(*pts, fill=None, border=self.eye_color,
                        borderWidth=2.0, opacity=op(40 + 50 * k))
            return
        color = palette.LIGHT_CORE if self.hit_flash > 0.45 else self.body_color
        ca, sa = math.cos(self.facing), math.sin(self.facing)
        pts = []
        for i in range(6):
            a = self.facing + i * math.tau / 6
            rad = r * (1.0 + 0.2 * math.sin(self.phase * 4.0 + i * 2.0))
            pts.append(sx + math.cos(a) * rad)
            pts.append(sy + math.sin(a) * rad)
        drawPolygon(*pts, fill=color, opacity=opacity)
        self.draw_eyes(sx, sy, ca, sa, opacity, offset=0.4, size=2.2)


class Carrion(Enemy):
    """What it leaves is worse than what it is.

    A slow, weak thing that bursts into a pool of standing rot when it dies.
    Its point is spatial: kill one in a doorway and the doorway is closed for
    eight seconds, so *where* you fight it is a decision the rest of the
    bestiary never asks you to make.
    """

    species = CARRION
    base_hp = 26.0
    base_speed = 104.0
    radius = 15.0
    touch_damage = 7.0
    ember_value = 2
    score = 28
    body_color = palette.CARRION
    eye_color = palette.CARRION_EYE
    mass = 1.5

    POOL_RADIUS = 78.0
    POOL_TIME = 8.0
    POOL_DPS = 13.0

    def behave(self, dt, ctx):
        self.chase(dt, ctx)

    def on_death(self, ctx):
        ctx.add_pool(self.x, self.y, self.POOL_RADIUS, self.POOL_TIME,
                     self.POOL_DPS)
        audio.play_at('carrion_burst', self.x, self.y, 0.7)

    def draw_body(self, sx, sy):
        color = palette.LIGHT_CORE if self.hit_flash > 0.45 else self.body_color
        opacity = self.body_opacity()
        r = self.radius
        ca, sa = math.cos(self.facing), math.sin(self.facing)
        pts = []
        for i in range(8):
            a = i * math.tau / 8
            sag = 1.0 + 0.26 * math.sin(self.phase * 1.6 + i * 1.3)
            pts.append(sx + math.cos(a) * r * sag)
            pts.append(sy + math.sin(a) * r * sag * 0.86)
        drawPolygon(*pts, fill=color, opacity=opacity)
        # Blisters, which are the warning of what is inside it.
        for i in range(3):
            a = self.phase * 0.7 + i * math.tau / 3
            bx = sx + math.cos(a) * r * 0.5
            by = sy + math.sin(a) * r * 0.45
            bs = 3.0 + 1.2 * math.sin(self.phase * 3.0 + i)
            drawPolygon(bx, by - bs, bx + bs, by, bx, by + bs, bx - bs, by,
                        fill=palette.CARRION_POOL, opacity=op(opacity * 0.8))
        self.draw_eyes(sx, sy, ca, sa, opacity, offset=0.2, size=2.0)


SPECIES = {
    CRAWLER: Crawler,
    HUSK: Husk,
    SPITTER: Spitter,
    WISP: Wisp,
    WARDEN: Warden,
    LURKER: Lurker,
    PALE: Pale,
    DOUSER: Douser,
    BOLTER: Bolter,
    KEENER: Keener,
    SPLITTER: Splitter,
    CINDER: Cinder,
    MIRROR: Mirror,
    DELVER: Delver,
    CARRION: Carrion,
}


def _rgb_of(color):
    """cmu-graphics rgb object -> a plain tuple the art cache can key on."""
    return art.rgb_tuple(color)


# --------------------------------------------------------------------------
# Wave composition
# --------------------------------------------------------------------------
# What one of each costs out of a fight's budget, and the floor it is first
# allowed to appear on. The vault introduces roughly one new species a floor
# for sixteen floors, which is what stops act three looking like act one with
# bigger numbers - and it means a player is still being taught something new
# most of the way down.
#
# The order is a curriculum. Movement first (crawler, spitter, husk), then
# terrain (wisp), then the two that are about the *room* rather than the
# fight (splitter, carrion), and only then the ones that are about the
# lantern - by which point the player has had fifteen floors of relying on it
# and can be asked to spend it.
ROSTER = (
    # key        cost  from  weight
    (CRAWLER,    1.0,   1,   5.0),
    (SPITTER,    2.0,   2,   2.8),
    (CINDER,     0.5,   3,   3.4),
    (HUSK,       3.0,   3,   2.0),
    (WISP,       1.8,   4,   2.2),
    (SPLITTER,   2.2,   5,   2.0),
    (WARDEN,     3.6,   7,   1.7),
    (BOLTER,     2.4,   8,   2.0),
    (LURKER,     2.6,   9,   2.2),
    (CARRION,    2.0,  10,   1.8),
    (KEENER,     2.8,  11,   1.5),
    (DELVER,     3.0,  12,   1.6),
    (PALE,       3.2,  14,   1.7),
    (MIRROR,     3.4,  15,   1.5),
    (DOUSER,     2.4,  16,   1.8),
)

COST = {key: cost for key, cost, _from, _w in ROSTER}
FIRST_FLOOR = {key: floor for key, _c, floor, _w in ROSTER}


def wave_for_depth(depth, rng, weight=1.0):
    """Return a list of species keys for one fight.

    `weight` is the share of a whole floor's worth this fight is due. A floor
    used to be a single room and a single wave, so the budget below was the
    whole of it; a floor is four to seven rooms now and each takes a slice.

    A species' weight decays once it has been around a while, so the late
    floors are not still mostly crawlers. It never
    reaches zero: the vault should stay recognisably itself all the way down.
    """
    budget = (7 + depth * 3.0) * weight
    table = []
    for key, _cost, first, base in ROSTER:
        if depth < first:
            continue
        # Twenty floors of the same weighting leaves the starting species
        # dominant for the whole run simply because they have been legal for
        # longer. Ageing them off keeps each act looking like itself.
        age = depth - first
        table.append((key, max(base * 0.28, base * (0.94 ** age))))

    out = []
    guard = 0
    while budget > 0.5 and guard < 300:
        guard += 1
        pick = rng.weighted(table)
        if COST[pick] > budget + 0.6:
            continue
        out.append(pick)
        budget -= COST[pick]
    return out
