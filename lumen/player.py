"""The lightbearer: movement, dash, aiming, the lantern, and damage."""

import math

from .draw import drawImage, drawPolygon

from . import art, audio, palette
from .config import (DASH_COOLDOWN, DASH_IFRAMES, DASH_SPEED, DASH_TIME,
                     LANTERN_DRAIN, LANTERN_FLARE_COOLDOWN, LANTERN_FLARE_COST,
                     LANTERN_FUEL_MAX, LANTERN_RADIUS, LANTERN_RADIUS_MIN,
                     LANTERN_RADIUS_STEP, PLAYER_ACCEL, PLAYER_IFRAMES,
                     PLAYER_RADIUS, PLAYER_SPEED)
from .mathx import clamp, from_angle, lerp, normalise


class Player:
    def __init__(self, stats):
        self.stats = stats
        self.x = 0.0
        self.y = 0.0
        self.vx = 0.0
        self.vy = 0.0
        self.radius = PLAYER_RADIUS
        self.facing = 0.0
        self.aim = 0.0

        self.hp = stats.max_hp
        self.shield = stats.shield_charges
        self.alive = True

        self.iframes = 0.0
        self.hurt_flash = 0.0

        self.dash_time = 0.0
        self.dash_cd = 0.0
        self.dash_charges = stats.dash_charges
        self.dash_dir = (1.0, 0.0)

        self.fuel = LANTERN_FUEL_MAX * stats.fuel_max_mult
        self.fuel_max = LANTERN_FUEL_MAX * stats.fuel_max_mult
        self.lantern_on = True
        self.flare_cd = 0.0
        self.flare_time = 0.0

        self.weapon_index = 0
        self.cooldown = 0.0
        self.charge = 0.0
        self.charging = False
        self.recoil = 0.0
        self.walk_phase = 0.0
        self.muzzle_flash = 0.0

        self.kills = 0
        self.damage_dealt = 0.0
        self.shots_fired = 0

    # -------------------------------------------------------------- state --
    @property
    def weapon(self):
        from .projectiles import WEAPONS
        return WEAPONS[self.weapon_index]

    def refresh_from_stats(self):
        s = self.stats
        pending = getattr(s, 'pending_heal', 0.0)
        if pending:
            self.hp = min(s.max_hp, self.hp + pending)
            s.pending_heal = 0.0
        self.fuel_max = LANTERN_FUEL_MAX * s.fuel_max_mult
        self.fuel = min(self.fuel_max, self.fuel)
        self.dash_charges = max(self.dash_charges, 0)

    def on_floor_start(self):
        self.shield = self.stats.shield_charges
        self.dash_charges = self.stats.dash_charges
        self.dash_cd = 0.0
        self.cooldown = 0.0
        self.charge = 0.0
        self.charging = False

    @property
    def lantern_radius(self):
        s = self.stats
        base = LANTERN_RADIUS * s.lantern_mult
        low = LANTERN_RADIUS_MIN * s.lantern_mult
        frac = clamp(self.fuel / max(self.fuel_max, 1e-6), 0.0, 1.0)
        # Fuel does not scale the light linearly; it holds up, then collapses.
        shaped = frac ** 0.45
        radius = lerp(low, base, shaped)
        if self.flare_time > 0.0:
            from .config import LANTERN_FLARE_RADIUS, LANTERN_FLARE_TIME
            t = self.flare_time / LANTERN_FLARE_TIME
            radius = lerp(radius, LANTERN_FLARE_RADIUS * s.lantern_mult, t ** 0.6)
        # Snap so the glow sprite can be a pre-baked exact size.
        return max(LANTERN_RADIUS_STEP,
                   int(radius / LANTERN_RADIUS_STEP) * LANTERN_RADIUS_STEP)

    # ------------------------------------------------------------- update --
    def update(self, dt, keys, aim_x, aim_y, level, particles, rng):
        s = self.stats

        self.aim = math.atan2(aim_y, aim_x)
        if self.dash_time <= 0.0:
            self.facing = self.aim

        # --- movement ---
        mx = my = 0.0
        if 'a' in keys or 'left' in keys:
            mx -= 1.0
        if 'd' in keys or 'right' in keys:
            mx += 1.0
        if 'w' in keys or 'up' in keys:
            my -= 1.0
        if 's' in keys or 'down' in keys:
            my += 1.0
        mx, my = normalise(mx, my)

        if self.dash_time > 0.0:
            self.dash_time -= dt
            self.vx = self.dash_dir[0] * DASH_SPEED
            self.vy = self.dash_dir[1] * DASH_SPEED
            if rng.chance(0.9):
                particles.emit(
                    3, self.x, self.y,
                    -self.dash_dir[0] * rng.uniform(20, 90),
                    -self.dash_dir[1] * rng.uniform(20, 90),
                    rng.uniform(0.18, 0.34), rng.uniform(3.5, 7.0),
                    palette.DASH_TRAIL, end_size=0.5, opacity=64, drag=3.0)
        else:
            speed = PLAYER_SPEED * s.speed_mult
            target_vx = mx * speed
            target_vy = my * speed
            k = 1.0 - math.exp(-PLAYER_ACCEL * dt)
            self.vx += (target_vx - self.vx) * k
            self.vy += (target_vy - self.vy) * k

        if self.recoil > 0.0:
            push = self.recoil
            self.vx -= math.cos(self.aim) * push
            self.vy -= math.sin(self.aim) * push
            self.recoil = 0.0

        nx = self.x + self.vx * dt
        ny = self.y + self.vy * dt
        nx, ny = level.collide_circle(nx, ny, self.radius)
        nx = clamp(nx, self.radius, level.width - self.radius)
        ny = clamp(ny, self.radius, level.height - self.radius)
        self.x, self.y = nx, ny

        moving = (mx or my) and self.dash_time <= 0.0
        if moving:
            self.walk_phase += dt * 11.0
        else:
            self.walk_phase *= math.exp(-6.0 * dt)

        # --- timers ---
        if self.dash_cd > 0.0:
            self.dash_cd -= dt
            if self.dash_cd <= 0.0 and self.dash_charges < s.dash_charges:
                self.dash_charges += 1
                if self.dash_charges < s.dash_charges:
                    self.dash_cd = DASH_COOLDOWN * s.dash_cooldown_mult
        self.iframes = max(0.0, self.iframes - dt)
        self.hurt_flash = max(0.0, self.hurt_flash - dt * 3.0)
        self.cooldown = max(0.0, self.cooldown - dt)
        self.flare_cd = max(0.0, self.flare_cd - dt)
        self.flare_time = max(0.0, self.flare_time - dt)
        self.muzzle_flash = max(0.0, self.muzzle_flash - dt * 8.0)

        # --- lantern fuel ---
        if self.lantern_on:
            self.fuel = max(0.0, self.fuel - LANTERN_DRAIN * s.lantern_efficiency * dt)

    # ------------------------------------------------------------ actions --
    def try_dash(self, keys, particles, rng):
        if self.dash_time > 0.0 or self.dash_charges <= 0:
            return False
        dx = dy = 0.0
        if 'a' in keys or 'left' in keys:
            dx -= 1.0
        if 'd' in keys or 'right' in keys:
            dx += 1.0
        if 'w' in keys or 'up' in keys:
            dy -= 1.0
        if 's' in keys or 'down' in keys:
            dy += 1.0
        if dx == 0.0 and dy == 0.0:
            dx, dy = from_angle(self.aim)
        self.dash_dir = normalise(dx, dy)
        self.dash_time = DASH_TIME
        self.iframes = max(self.iframes, DASH_IFRAMES)
        self.dash_charges -= 1
        if self.dash_cd <= 0.0:
            self.dash_cd = DASH_COOLDOWN * self.stats.dash_cooldown_mult
        particles.burst(self.x, self.y, 12, palette.DASH_TRAIL, rng,
                        speed=(60, 240), life=(0.16, 0.34), size=(2.0, 4.0),
                        direction=math.atan2(-self.dash_dir[1], -self.dash_dir[0]),
                        spread=1.6)
        audio.play('dash', 0.5)
        return True

    def try_flare(self):
        if self.flare_cd > 0.0 or self.fuel < LANTERN_FLARE_COST:
            return False
        from .config import LANTERN_FLARE_TIME
        self.fuel -= LANTERN_FLARE_COST
        self.flare_cd = LANTERN_FLARE_COOLDOWN * self.stats.flare_cooldown_mult
        self.flare_time = LANTERN_FLARE_TIME
        return True

    def can_fire(self):
        return self.cooldown <= 0.0

    def fire(self, pool, particles, rng, effects):
        """Emit one shot from the current weapon. Returns True if it fired."""
        weapon = self.weapon
        if self.cooldown > 0.0:
            return False
        s = self.stats

        charge_mult = 1.0
        if weapon.charge_time > 0.0:
            frac = clamp(self.charge / weapon.charge_time, 0.0, 1.0)
            charge_mult = 1.0 + (weapon.charge_scale - 1.0) * frac
            self.charge = 0.0
            self.charging = False

        self.cooldown = weapon.cooldown / s.fire_rate_mult
        muzzle = self.radius + 12.0
        ox = self.x + math.cos(self.aim) * muzzle
        oy = self.y + math.sin(self.aim) * muzzle

        speed = weapon.speed * s.projectile_speed_mult
        for _ in range(weapon.pellets):
            a = rng.spread(self.aim, weapon.spread)
            crit = rng.chance(s.crit_chance)
            damage = weapon.damage * s.damage_mult * charge_mult
            if crit:
                damage *= s.crit_mult
            pool.spawn(
                0, ox, oy, math.cos(a) * speed, math.sin(a) * speed, damage,
                radius=weapon.radius * (1.0 + 0.35 * (charge_mult - 1.0)),
                life=weapon.life * s.projectile_speed_mult,
                pierce=weapon.pierce + s.pierce_bonus,
                color=weapon.color, glow_color=weapon.glow,
                length=weapon.length * (1.0 + 0.5 * (charge_mult - 1.0)),
                width=weapon.width * (1.0 + 0.4 * (charge_mult - 1.0)),
                knockback=weapon.knockback, homing=s.homing,
                explode=s.explode_radius, bounces=s.bounces, crit=crit)

        self.shots_fired += weapon.pellets
        self.recoil = weapon.recoil * (1.0 + 0.4 * (charge_mult - 1.0))
        self.muzzle_flash = 1.0
        effects.add_light(ox, oy, weapon.light * charge_mult, 0.14,
                          weapon.glow)
        effects.add_shake(weapon.shake * charge_mult)
        particles.burst(ox, oy, 4 + weapon.pellets, weapon.color, rng,
                        speed=(120, 300), life=(0.1, 0.22), size=(1.6, 3.4),
                        direction=self.aim, spread=weapon.spread * 3.0 + 0.4)
        audio.play(weapon.sound, 0.55 * charge_mult)
        return True

    def hurt(self, amount, effects, particles, rng, source_angle=None):
        if self.iframes > 0.0 or not self.alive:
            return False
        if self.shield > 0:
            self.shield -= 1
            self.iframes = PLAYER_IFRAMES
            effects.add_flash(0.62, palette.SHIELD)
            effects.add_text(self.x, self.y - 26, 'WARD', palette.SHIELD, 17, True)
            particles.burst(self.x, self.y, 22, palette.SHIELD, rng,
                            speed=(140, 340), life=(0.2, 0.5), size=(2, 4.5))
            audio.play('crit', 0.6)
            return False

        amount *= self.stats.taken_mult
        self.hp -= amount
        self.iframes = PLAYER_IFRAMES
        self.hurt_flash = 1.0
        effects.add_flash(0.72, palette.UI_DANGER)
        effects.add_chroma(0.8)
        effects.add_hitstop(0.055)
        direction = source_angle if source_angle is not None else rng.angle()
        particles.burst(self.x, self.y, 18, palette.DAMAGE, rng,
                        speed=(120, 380), life=(0.24, 0.55), size=(2.2, 5.0),
                        direction=direction, spread=2.2)
        audio.play('hurt', 0.8)
        if self.hp <= 0.0:
            self.hp = 0.0
            self.alive = False
        return True

    def heal(self, amount):
        self.hp = min(self.stats.max_hp, self.hp + amount)

    def add_fuel(self, amount):
        self.fuel = min(self.fuel_max, self.fuel + amount)

    # --------------------------------------------------------------- draw --
    def draw(self, ox, oy):
        sx = self.x - ox
        sy = self.y - oy
        a = self.facing
        ca, sa = math.cos(a), math.sin(a)

        bob = math.sin(self.walk_phase) * 1.4
        sy += bob

        # Soft pool directly under the bearer.
        glow_size = 96
        drawImage(art.glow((120, 160, 220), glow_size, power=2.6),
                  int(sx - glow_size * 0.5), int(sy - glow_size * 0.5),
                  opacity=18)

        # The lantern's halo goes down *before* the body, or it washes the
        # bearer out completely - the figure has to stay the most readable
        # thing on screen.
        lx = sx + ca * 6.0 - sa * 15.0
        ly = sy + sa * 6.0 + ca * 15.0
        halo = 48
        strength = 30 + 18 * (self.fuel / max(self.fuel_max, 1e-6))
        if self.flare_time > 0.0:
            strength = 62
        drawImage(art.glow((255, 200, 120), halo, power=2.1),
                  int(lx - halo * 0.5), int(ly - halo * 0.5),
                  opacity=int(clamp(strength, 0, 100)))

        if self.dash_time > 0.0:
            for i in range(3):
                t = (i + 1) * 9.0
                gx = sx - self.dash_dir[0] * t
                gy = sy - self.dash_dir[1] * t
                _cloak(gx, gy, ca, sa, self.radius * (1.0 - i * 0.14),
                       palette.DASH_TRAIL, 28 - i * 8)

        hurt = self.hurt_flash
        body_color = palette.PLAYER_BODY if hurt < 0.35 else palette.DAMAGE
        cloak_color = palette.PLAYER_CLOAK if hurt < 0.35 else palette.DAMAGE

        flicker = 1.0 if self.iframes <= 0.0 else (0.35 + 0.65 * ((self.iframes * 18) % 1.0))
        opacity = int(clamp(100 * flicker, 25, 100))

        # A near-black silhouette one size up keeps the figure legible
        # against the bright floor right under the lantern.
        _cloak(sx, sy, ca, sa, self.radius + 6.0, palette.VOID,
               int(opacity * 0.9))
        _cloak(sx, sy, ca, sa, self.radius + 2.5, cloak_color, opacity)

        # Torso: a broad wedge pointing along the aim, so facing is readable
        # at a glance even at this size.
        r = self.radius
        drawPolygon(sx + ca * r * 1.18, sy + sa * r * 1.18,
                    sx + ca * r * 0.05 - sa * r * 0.74,
                    sy + sa * r * 0.05 + ca * r * 0.74,
                    sx - ca * r * 0.62, sy - sa * r * 0.62,
                    sx + ca * r * 0.05 + sa * r * 0.74,
                    sy + sa * r * 0.05 - ca * r * 0.74,
                    fill=body_color, opacity=opacity)

        # Hood: a darker cap over the rear half.
        drawPolygon(sx + ca * r * 0.18 - sa * r * 0.52,
                    sy + sa * r * 0.18 + ca * r * 0.52,
                    sx - ca * r * 0.66, sy - sa * r * 0.66,
                    sx + ca * r * 0.18 + sa * r * 0.52,
                    sy + sa * r * 0.18 - ca * r * 0.52,
                    fill=cloak_color, opacity=opacity)

        # Trim along the leading edge catches the lantern.
        drawPolygon(sx + ca * r * 1.18, sy + sa * r * 1.18,
                    sx + ca * r * 0.42 - sa * r * 0.34,
                    sy + sa * r * 0.42 + ca * r * 0.34,
                    sx + ca * r * 0.42 + sa * r * 0.34,
                    sy + sa * r * 0.42 - ca * r * 0.34,
                    fill=palette.PLAYER_TRIM, opacity=opacity)

        # The lantern body itself, on top of everything.
        drawPolygon(lx - 4.0, ly - 5.0, lx + 4.0, ly - 5.0,
                    lx + 4.0, ly + 5.0, lx - 4.0, ly + 5.0,
                    fill=palette.VOID, opacity=90)
        # Not full strength: this sits at the exact centre of the light, so
        # anything near-white here is multiplied by a bright light buffer and
        # then has the light added on top again, and clips to a flat white
        # slab. It reads as a flame because of what surrounds it.
        drawPolygon(lx - 2.6, ly - 3.6, lx + 2.6, ly - 3.6,
                    lx + 2.6, ly + 3.6, lx - 2.6, ly + 3.6,
                    fill=palette.LIGHT_CORE, opacity=60)

        if self.muzzle_flash > 0.05:
            mx = sx + ca * (self.radius + 14.0)
            my = sy + sa * (self.radius + 14.0)
            size = 64
            drawImage(art.glow(self.weapon.glow, size, power=1.8),
                      int(mx - size * 0.5), int(my - size * 0.5),
                      opacity=int(70 * self.muzzle_flash))

        if self.charging and self.charge > 0.04:
            from .mathx import clamp as _clamp
            frac = _clamp(self.charge / max(self.weapon.charge_time, 1e-6), 0.0, 1.0)
            art.draw_ring(self.weapon.glow, sx, sy, 16 + 32 * frac,
                          30 + 60 * frac, thickness=0.2)

        if self.shield > 0:
            art.draw_ring((127, 212, 255), sx, sy, 40, 44, thickness=0.1,
                          softness=2.0)


def _cloak(sx, sy, ca, sa, r, color, opacity):
    """A six-point cape shape, longer behind than in front."""
    pts = []
    for angle, scale in ((0.0, 1.05), (0.9, 1.0), (2.1, 1.5),
                         (math.pi, 1.65), (-2.1, 1.5), (-0.9, 1.0)):
        c = math.cos(angle)
        s = math.sin(angle)
        # Rotate the local offset into world space.
        lx = c * r * scale
        ly = s * r * scale
        pts.append(sx + lx * ca - ly * sa)
        pts.append(sy + lx * sa + ly * ca)
    drawPolygon(*pts, fill=color, opacity=opacity)
