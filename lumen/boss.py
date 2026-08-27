"""The Hollow Choir - the vault's floor boss.

A telegraph-driven fight rather than a damage sponge: every attack has a
visible wind-up, and the pattern set widens as its health falls, so the reads
you learn in phase one still matter in phase three.
"""

import math

from .draw import drawLine, drawPolygon

from . import art, audio, palette
from .enemies import CHOIR, Enemy
from .mathx import angle_diff, clamp, ease_out_cubic


class HollowChoir(Enemy):
    species = CHOIR
    base_hp = 620.0
    base_speed = 78.0
    radius = 46.0
    touch_damage = 22.0
    ember_value = 30
    score = 900
    body_color = palette.BOSS
    eye_color = palette.BOSS_EYE
    mass = 40.0

    ATTACKS = ('ring', 'spiral', 'charge', 'summon', 'lash')

    def __init__(self, x, y, depth, rng):
        super().__init__(x, y, depth, rng)
        self.max_hp = self.base_hp * (1.0 + 0.55 * max(0, depth // 6 - 1))
        self.hp = self.max_hp
        self.spawn_t = 1.6
        self.state = 'idle'
        self.state_t = 0.0
        self.attack = None
        self.telegraph = 0.0
        self.shots_left = 0
        self.shot_timer = 0.0
        self.spin = 0.0
        self.satellites = 5
        self.charge_dir = (1.0, 0.0)
        self.enrage = False
        self.intro_played = False

    # ------------------------------------------------------------- phases --
    @property
    def tier(self):
        frac = self.hp / max(self.max_hp, 1e-6)
        if frac > 0.66:
            return 1
        if frac > 0.33:
            return 2
        return 3

    def available_attacks(self):
        p = self.tier
        if p == 1:
            return ('ring', 'summon', 'charge')
        if p == 2:
            return ('ring', 'spiral', 'charge', 'summon')
        return self.ATTACKS

    # ------------------------------------------------------------ update ---
    def behave(self, dt, ctx):
        self.spin += dt * (0.6 + 0.28 * self.tier)
        player = ctx.player

        if not self.intro_played:
            self.intro_played = True
            audio.play('roar', 0.9)
            ctx.effects.add_shake(9.0)

        if self.state == 'idle':
            self.steer_to(player.x, player.y, dt, self.speed * 0.65, accel=2.0)
            if self.state_t > (1.35 if self.tier == 1 else 0.95 if self.tier == 2 else 0.7):
                self.begin_attack(ctx)

        elif self.state == 'telegraph':
            self.vx *= math.exp(-4.0 * dt)
            self.vy *= math.exp(-4.0 * dt)
            if self.attack == 'charge':
                want = math.atan2(player.y - self.y, player.x - self.x)
                self.facing += angle_diff(self.facing, want) * clamp(3.0 * dt, 0, 1)
            if self.state_t >= self.telegraph:
                self.execute(ctx)

        elif self.state == 'attack':
            self.run_attack(dt, ctx)

        elif self.state == 'recover':
            self.vx *= math.exp(-3.0 * dt)
            self.vy *= math.exp(-3.0 * dt)
            if self.state_t > 0.55:
                self.set_state('idle')

    def set_state(self, name):
        self.state = name
        self.state_t = 0.0

    def begin_attack(self, ctx):
        self.attack = ctx.rng.choice(self.available_attacks())
        self.telegraph = {'ring': 0.7, 'spiral': 0.8, 'charge': 0.85,
                          'summon': 0.9, 'lash': 0.75}[self.attack]
        self.set_state('telegraph')
        ctx.effects.add_light(self.x, self.y, 190, self.telegraph, self.eye_color)

    def execute(self, ctx):
        self.set_state('attack')
        player = ctx.player
        if self.attack == 'ring':
            self.shots_left = 3 if self.tier >= 2 else 2
            self.shot_timer = 0.0
        elif self.attack == 'spiral':
            self.shots_left = 26 + 10 * self.tier
            self.shot_timer = 0.0
        elif self.attack == 'charge':
            a = math.atan2(player.y - self.y, player.x - self.x)
            self.charge_dir = (math.cos(a), math.sin(a))
            self.vx = self.charge_dir[0] * 720.0
            self.vy = self.charge_dir[1] * 720.0
            ctx.effects.add_shake(5.0)
            audio.play('dash', 0.8)
        elif self.attack == 'summon':
            self.shots_left = 2 + self.tier
            self.shot_timer = 0.0
        elif self.attack == 'lash':
            self.shots_left = 5
            self.shot_timer = 0.0

    def run_attack(self, dt, ctx):
        self.shot_timer -= dt
        player = ctx.player

        if self.attack == 'charge':
            self.vx *= math.exp(-1.6 * dt)
            self.vy *= math.exp(-1.6 * dt)
            if ctx.rng.chance(24.0 * dt):
                ctx.particles.embers(self.x, self.y, 2, self.eye_color, ctx.rng)
            if self.state_t > 0.9:
                self.set_state('recover')
            return

        if self.shots_left <= 0:
            self.set_state('recover')
            return

        if self.shot_timer > 0.0:
            return

        if self.attack == 'ring':
            count = 14 + 4 * self.tier
            base = ctx.rng.angle()
            for i in range(count):
                a = base + i * math.tau / count
                self._bullet(ctx, a, 250.0, 10.0)
            self.shot_timer = 0.42
            self.shots_left -= 1
            ctx.effects.add_light(self.x, self.y, 240, 0.22, self.eye_color)
            audio.play('enemy_shoot', 0.5)

        elif self.attack == 'spiral':
            arms = 2 + self.tier
            base = self.state_t * 4.4
            for i in range(arms):
                a = base + i * math.tau / arms
                self._bullet(ctx, a, 285.0, 9.0)
            self.shot_timer = 0.055
            self.shots_left -= 1
            if self.shots_left % 8 == 0:
                audio.play('enemy_shoot', 0.24)

        elif self.attack == 'summon':
            from .enemies import Crawler, Wisp
            kind = Crawler if ctx.rng.chance(0.6) else Wisp
            a = ctx.rng.angle()
            d = self.radius + 46
            sx = clamp(self.x + math.cos(a) * d, 40, ctx.level.width - 40)
            sy = clamp(self.y + math.sin(a) * d, 40, ctx.level.height - 40)
            ctx.spawn_enemy(kind, sx, sy)
            ctx.particles.burst(sx, sy, 12, self.eye_color, ctx.rng,
                                speed=(80, 220), life=(0.2, 0.5), size=(2, 4))
            self.shot_timer = 0.3
            self.shots_left -= 1

        elif self.attack == 'lash':
            a = math.atan2(player.y - self.y, player.x - self.x)
            for k in (-1, 0, 1):
                self._bullet(ctx, a + k * 0.14, 430.0, 12.0, life=2.4)
            self.shot_timer = 0.16
            self.shots_left -= 1
            audio.play('enemy_shoot', 0.4)

    def _bullet(self, ctx, angle, speed, damage, life=3.4):
        ctx.projectiles.spawn(
            1, self.x + math.cos(angle) * self.radius * 0.8,
            self.y + math.sin(angle) * self.radius * 0.8,
            math.cos(angle) * speed, math.sin(angle) * speed,
            ctx.enemy_bullet_damage(damage),
            radius=7.5, life=life, color=palette.BOSS_BOLT,
            glow_color=palette.BOSS_BOLT_GLOW, length=15.0, width=12.0,
            knockback=90.0)

    def damage_by(self, amount, ctx, angle=None, knockback=0.0, crit=False):
        before = self.tier
        dealt = super().damage_by(amount, ctx, angle, knockback * 0.12, crit)
        if self.alive and self.tier != before:
            ctx.effects.add_shake(7.0)
            ctx.effects.add_flash(0.5, palette.BOSS_EYE, wash=True)
            ctx.effects.slowmo(0.5, 0.35)
            ctx.particles.burst(self.x, self.y, 40, palette.BOSS_EYE, ctx.rng,
                                speed=(180, 520), life=(0.3, 0.8), size=(3, 6))
            ctx.particles.ripple(self.x, self.y, palette.BOSS_EYE, 320, 0.6, 80)
            audio.play('roar', 0.7)
            self.set_state('recover')
        return dealt

    def die(self, ctx, angle=None):
        super().die(ctx, angle)

    # -------------------------------------------------------------- draw ---
    def draw(self, ox, oy, lit):
        # The boss is always visible: it is its own light source.
        self.draw_body(self.x - ox, self.y - oy)

    def draw_body(self, sx, sy):
        opacity = self.body_opacity()
        r = self.radius
        # A brief, less blinding flash: under sustained fire the old threshold
        # left the boss white for most of the fight.
        flash = self.hit_flash > 0.82
        color = palette.BOSS_FLASH if flash else self.body_color

        art.draw_glow(self.eye_color, sx, sy, 128,
                      16 + 10 * math.sin(self.pulse()), power=2.6)

        if self.state == 'telegraph':
            frac = clamp(self.state_t / max(self.telegraph, 1e-6), 0.0, 1.0)
            art.draw_ring(self.eye_color, sx, sy,
                          48 + 160 * ease_out_cubic(frac), 28 + 52 * frac,
                          thickness=0.06, softness=1.6)

        # Outer shell.
        pts = []
        for i in range(9):
            a = self.spin * 0.4 + i * math.tau / 9
            rad = r * (1.0 + 0.07 * math.sin(self.pulse() + i * 0.7))
            pts.append(sx + math.cos(a) * rad)
            pts.append(sy + math.sin(a) * rad)
        drawPolygon(*pts, fill=color, opacity=opacity)

        # Inner counter-rotating ring.
        pts = []
        for i in range(6):
            a = -self.spin * 0.9 + i * math.tau / 6
            rad = r * 0.62
            pts.append(sx + math.cos(a) * rad)
            pts.append(sy + math.sin(a) * rad)
        drawPolygon(*pts, fill=palette.HUSK, opacity=int(opacity * 0.9))

        # Satellite voices.
        count = self.satellites + self.tier
        for i in range(count):
            a = self.spin * 1.6 + i * math.tau / count
            d = r * 1.42
            vx = sx + math.cos(a) * d
            vy = sy + math.sin(a) * d
            s = 5.0 + 1.6 * math.sin(self.pulse() * 1.7 + i)
            drawPolygon(vx, vy - s, vx + s, vy, vx, vy + s, vx - s, vy,
                        fill=self.eye_color, opacity=int(opacity * 0.85))

        # The eye.
        eye = r * 0.3 * (1.0 + 0.14 * math.sin(self.pulse() * 2.2))
        ca, sa = math.cos(self.facing), math.sin(self.facing)
        drawPolygon(sx + ca * eye * 2.0, sy + sa * eye * 2.0,
                    sx - sa * eye, sy + ca * eye,
                    sx - ca * eye * 2.0, sy - sa * eye * 2.0,
                    sx + sa * eye, sy - ca * eye,
                    fill=palette.BOSS_EYE, opacity=opacity)

        if self.state == 'attack' and self.attack == 'charge':
            tx = sx - self.charge_dir[0] * 90
            ty = sy - self.charge_dir[1] * 90
            drawLine(sx, sy, tx, ty, fill=palette.BOSS_EYE, lineWidth=10,
                     opacity=30)

    def pulse(self):
        """Animation clock, inherited from Enemy and advanced every frame."""
        return self.phase
