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
            ctx.effects.add_text(self.x, self.y - self.radius - 8,
                                 f'{int(amount)}', palette.CRIT, 21, True)
            ctx.effects.add_hitstop(0.045)
            audio.play_at('crit', self.x, self.y, 0.5)
        else:
            ctx.effects.add_text(self.x, self.y - self.radius - 6,
                                 f'{int(amount)}', palette.UI_TEXT, 15, False)
            audio.play_at('hit', self.x, self.y, 0.32)

        if self.hp <= 0.0:
            self.die(ctx, angle)
            return amount
        return amount

    def die(self, ctx, angle=None):
        self.alive = False
        ctx.on_kill(self, angle)

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
    shield_arc = 1.15   # radians either side of facing that the shield covers

    def behave(self, dt, ctx):
        self.chase(dt, ctx, accel=4.0)
        want = math.atan2(ctx.player.y - self.y, ctx.player.x - self.x)
        # The shield tracks you, but slowly enough that flanking works.
        self.facing += angle_diff(self.facing, want) * clamp(2.4 * dt, 0.0, 1.0)

    def face_velocity(self, dt, rate=10.0):
        pass

    def shield_blocks(self, angle_from):
        """True when an impact arriving from `angle_from` hits the shield."""
        return abs(angle_diff(self.facing, angle_from)) < self.shield_arc

    def damage_by(self, amount, ctx, angle=None, knockback=0.0, crit=False):
        if angle is not None and self.shield_blocks(angle + math.pi):
            ctx.particles.burst(
                self.x + math.cos(angle + math.pi) * self.radius,
                self.y + math.sin(angle + math.pi) * self.radius,
                7, palette.WARDEN_SHIELD, self.rng, speed=(120, 300),
                life=(0.14, 0.3), size=(1.8, 3.4),
                direction=angle + math.pi, spread=1.7)
            ctx.effects.add_text(self.x, self.y - self.radius - 6, 'BLOCKED',
                                 palette.WARDEN_SHIELD, 14, False)
            audio.play_at('hit', self.x, self.y, 0.2)
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

        # The shield: a thick arc drawn as a strip of quads.
        segs = 5
        inner = r * 1.25
        outer = r * 1.55
        for i in range(segs):
            a0 = self.facing - self.shield_arc + (2 * self.shield_arc) * i / segs
            a1 = self.facing - self.shield_arc + (2 * self.shield_arc) * (i + 1) / segs
            drawPolygon(sx + math.cos(a0) * inner, sy + math.sin(a0) * inner,
                        sx + math.cos(a1) * inner, sy + math.sin(a1) * inner,
                        sx + math.cos(a1) * outer, sy + math.sin(a1) * outer,
                        sx + math.cos(a0) * outer, sy + math.sin(a0) * outer,
                        fill=palette.WARDEN_SHIELD,
                        opacity=op(opacity * 0.62))


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


SPECIES = {
    CRAWLER: Crawler,
    HUSK: Husk,
    SPITTER: Spitter,
    WISP: Wisp,
    WARDEN: Warden,
}


def _rgb_of(color):
    """cmu-graphics rgb object -> a plain tuple the art cache can key on."""
    return art.rgb_tuple(color)


# --------------------------------------------------------------------------
# Wave composition
# --------------------------------------------------------------------------
def wave_for_depth(depth, rng):
    """Return a list of species keys for one floor."""
    budget = 7 + depth * 3.0
    table = [(CRAWLER, 5.0)]
    if depth >= 2:
        table.append((SPITTER, 2.6))
    if depth >= 3:
        table.append((HUSK, 2.0))
    if depth >= 4:
        table.append((WISP, 2.2))
    if depth >= 5:
        table.append((WARDEN, 1.6))

    cost = {CRAWLER: 1.0, SPITTER: 2.0, WISP: 1.8, HUSK: 3.0, WARDEN: 3.6}
    out = []
    guard = 0
    while budget > 0.5 and guard < 200:
        guard += 1
        pick = rng.weighted(table)
        if cost[pick] > budget + 0.6:
            continue
        out.append(pick)
        budget -= cost[pick]
    return out
