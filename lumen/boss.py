"""The Hollow Choir - the vault's floor boss.

A telegraph-driven fight rather than a damage sponge: every attack has a
visible wind-up, and the pattern set widens as its health falls, so the reads
you learn in phase one still matter in phase three.
"""

import math

from .draw import drawLine, drawPolygon

from . import art, audio, palette
from .enemies import CHOIR, Enemy
from .mathx import angle_diff, clamp, ease_out_cubic, opacity as op


def _tell(boss):
    """The wind-up you hear, under the wind-up you see.

    Both bosses draw an expanding ring before every attack, and both shorten
    it as they enrage - which is the fight's most important read and, until
    now, one you could only get by looking at the thing. So the sound rises
    with `rage` across the seven takes baked for it: at full health it is the
    deepest and longest of them and lands just before the ring closes, and in
    the last stand it is the tightest and shortest and lands with it. Same
    information, and it still arrives with your back turned.
    """
    audio.play_at(f'{boss.voice}_tell', boss.x, boss.y, 0.55,
                  pitch=-1.0 + 2.0 * boss.rage)


class HollowChoir(Enemy):
    species = CHOIR
    base_hp = 3900.0
    base_speed = 78.0
    radius = 46.0
    touch_damage = 22.0
    ember_value = 30
    score = 900
    body_color = palette.BOSS
    eye_color = palette.BOSS_EYE
    mass = 40.0

    ATTACKS = ('ring', 'spiral', 'charge', 'summon', 'lash')

    # Which half of the boss bank this one speaks out of. The arrival beats
    # are fired by the world off the intro clock rather than from here, so
    # it has to be able to ask a boss what it sounds like.
    voice = 'choir'

    # How high its light hangs, for the per-light surface shading.
    LIGHT_HEIGHT = 34.0

    # THE RING. Every other attack it has is a stream of bolts you read and
    # walk around; this one is a *release*, and it is the same shape as the
    # lantern flare the player throws with shift - a circle of light leaving
    # a point, hitting everything it passes. Aimed the other way.
    #
    # It used to be the filler of its book: ten damage a bolt, slower than
    # the lash, and safest of all exactly where the boss was standing, which
    # is backwards for something that looks like a detonation. It is now the
    # hardest single thing in the game to be hit by, and the telegraph draws
    # the blast radius so that being inside one is a decision.
    RING_DAMAGE = 24.0          # per bolt, was 10
    RING_SPEED = 300.0          # was 250
    RING_WAVE = 268.0           # how far the close-range half reaches
    RING_WAVE_DAMAGE = 30.0     # at the centre, falling off to the rim
    RING_WAVE_KNOCKBACK = 640.0

    def __init__(self, x, y, depth, rng):
        super().__init__(x, y, depth, rng)
        self.max_hp = self.base_hp * (1.0 + 0.62 * max(0, depth // 6 - 1))
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
    # Health thresholds where it changes. Four now rather than three: the
    # last one is a short, very fast stand at the end rather than a long slow
    # grind through the final third.
    TIERS = (0.70, 0.42, 0.16)

    @property
    def tier(self):
        frac = self.hp / max(self.max_hp, 1e-6)
        for i, edge in enumerate(self.TIERS):
            if frac > edge:
                return i + 1
        return 4

    @property
    def rage(self):
        """0 at full health, 1 in the last stand, smooth in between.

        Three flat phases meant the fight got harder in two steps and was
        otherwise identical throughout each one - the last third of a phase
        played exactly like the first. Rage climbs *within* a phase as well as
        across it, so the thing is visibly winding up the whole way down: the
        gaps between attacks close, the warning before each one shortens, and
        what it throws gets faster and more numerous.
        """
        frac = clamp(self.hp / max(self.max_hp, 1e-6), 0.0, 1.0)
        return (1.0 - frac) ** 0.85

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
            # The arrival is `World._tick_boss_intro`'s, sound included: it
            # owns the clock the three beats hang off, and this runs on the
            # same frame its last beat does. Playing a roar here as well put
            # two of them on top of each other.
            self.intro_played = True
            ctx.effects.add_shake(9.0)

        if self.state == 'idle':
            self.steer_to(player.x, player.y, dt,
                          self.speed * (0.65 + 0.55 * self.rage), accel=2.0)
            # The pause between attacks, closing from a beat and a half down
            # to almost nothing.
            if self.state_t > 1.45 - 1.02 * self.rage:
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
            if self.state_t > 0.6 - 0.34 * self.rage:
                self.set_state('idle')

    def set_state(self, name):
        self.state = name
        self.state_t = 0.0

    def begin_attack(self, ctx):
        self.attack = ctx.rng.choice(self.available_attacks())
        # The warning before it lands. Losing nearly half of it by the end is
        # most of what makes the last stand frightening: the same attacks,
        # with far less time to read them.
        self.telegraph = {'ring': 0.7, 'spiral': 0.8, 'charge': 0.85,
                          'summon': 0.9, 'lash': 0.75}[self.attack]
        self.telegraph *= 1.0 - 0.46 * self.rage
        self.set_state('telegraph')
        ctx.effects.add_light(self.x, self.y, 190, self.telegraph, self.eye_color)
        _tell(self)

    def execute(self, ctx):
        self.set_state('attack')
        player = ctx.player
        if self.attack == 'ring':
            self.shots_left = (3 if self.tier >= 2 else 2) + (1 if self.tier >= 4 else 0)
            self.shot_timer = 0.0
        elif self.attack == 'spiral':
            self.shots_left = 26 + 10 * self.tier
            self.shot_timer = 0.0
        elif self.attack == 'charge':
            a = math.atan2(player.y - self.y, player.x - self.x)
            self.charge_dir = (math.cos(a), math.sin(a))
            charge_speed = 720.0 * (1.0 + 0.35 * self.rage)
            self.vx = self.charge_dir[0] * charge_speed
            self.vy = self.charge_dir[1] * charge_speed
            ctx.effects.add_shake(5.0)
            # The player's dash was standing in for this and is far too
            # light for forty tonnes crossing a room at seven hundred a
            # second.
            audio.play_at('choir_charge', self.x, self.y, 0.85)
        elif self.attack == 'summon':
            self.shots_left = 2 + self.tier + int(2 * self.rage)
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
            count = 14 + 4 * self.tier + int(8 * self.rage)
            base = ctx.rng.angle()
            for i in range(count):
                a = base + i * math.tau / count
                self._bullet(ctx, a, self.RING_SPEED, self.RING_DAMAGE,
                             scale=1.34)
            self._ring_wave(ctx)
            self.shot_timer = 0.42 - 0.16 * self.rage
            self.shots_left -= 1
            ctx.effects.add_light(self.x, self.y, 360, 0.3, self.eye_color)
            ctx.effects.add_shake(6.5)
            ctx.particles.ripple(self.x, self.y, self.eye_color,
                                 self.RING_WAVE * 2.0, 0.4, 92)
            ctx.particles.burst(self.x, self.y, 26, self.eye_color, ctx.fxrng,
                                speed=(220, 620), life=(0.2, 0.5), size=(2.4, 5.4))
            audio.play_at('choir_ring', self.x, self.y, 0.9)

        elif self.attack == 'spiral':
            arms = 2 + self.tier + int(2 * self.rage)
            base = self.state_t * 4.4
            for i in range(arms):
                a = base + i * math.tau / arms
                self._bullet(ctx, a, 285.0, 9.0)
            self.shot_timer = 0.055 - 0.018 * self.rage
            self.shots_left -= 1
            # Every fifth, not every bolt: eighteen a second is a dentist's
            # drill however soft the sound is.
            if self.shots_left % 5 == 0:
                audio.play_at('choir_spiral', self.x, self.y, 0.32)

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
            # At the spawn point rather than at the boss: that is where the
            # burst of particles is, and where the thing arrives.
            audio.play_at('choir_summon', sx, sy, 0.55)
            self.shot_timer = 0.3 - 0.12 * self.rage
            self.shots_left -= 1

        elif self.attack == 'lash':
            a = math.atan2(player.y - self.y, player.x - self.x)
            fan = (-2, -1, 0, 1, 2) if self.tier >= 3 else (-1, 0, 1)
            for k in fan:
                self._bullet(ctx, a + k * 0.14, 430.0, 12.0, life=2.4)
            self.shot_timer = 0.16 - 0.06 * self.rage
            self.shots_left -= 1
            audio.play_at('choir_lash', self.x, self.y, 0.55)

    def _ring_wave(self, ctx):
        """The close-range half of the ring, and the frightening half.

        A wall of bolts leaving a body is only dangerous once it has
        travelled, so the old ring left the safest square inch of the room
        directly under the thing throwing it. This is the player's own flare
        turned around: everything inside the radius is hit, hardest at the
        middle, and thrown out of it.

        The knockback is the mercy. Being shoved clear is what stops the
        second volley of the same attack landing on you while you are still
        picking yourself up - and it is the same shove the player's flare
        gives, which is the point of building it out of the same parts.
        """
        player = ctx.player
        dx = player.x - self.x
        dy = player.y - self.y
        d = math.hypot(dx, dy)
        if d > self.RING_WAVE:
            return
        falloff = 1.0 - d / self.RING_WAVE
        # `fxrng`, not `rng`: this is cosmetic, and drawing from the world's
        # stream on a condition as fiddly as "was the player standing here"
        # would make level generation depend on how someone dodged.
        angle = math.atan2(dy, dx) if d > 1e-6 else ctx.fxrng.angle()
        damage = self.RING_WAVE_DAMAGE * (1.0 + 0.30 * self.rage)
        if player.hurt(ctx.enemy_bullet_damage(damage) * (0.40 + 0.60 * falloff),
                       ctx.effects, ctx.particles, ctx.fxrng, angle):
            ctx.effects.add_shake(9.0)
            push = self.RING_WAVE_KNOCKBACK * (0.4 + 0.6 * falloff)
            player.vx += math.cos(angle) * push
            player.vy += math.sin(angle) * push

    def _bullet(self, ctx, angle, speed, damage, life=3.4, scale=1.0):
        speed *= 1.0 + 0.42 * self.rage
        damage *= 1.0 + 0.30 * self.rage
        ctx.projectiles.spawn(
            1, self.x + math.cos(angle) * self.radius * 0.8,
            self.y + math.sin(angle) * self.radius * 0.8,
            math.cos(angle) * speed, math.sin(angle) * speed,
            ctx.enemy_bullet_damage(damage),
            radius=7.5 * scale, life=life, color=palette.BOSS_BOLT,
            glow_color=palette.BOSS_BOLT_GLOW, length=15.0 * scale,
            width=12.0 * scale, knockback=90.0)

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
            # The voice cracking, and the crowd behind it bellowing.
            audio.play('choir_phase', 0.85)
            audio.play('roar', 0.5)
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

        # Its own light grows and quickens with rage, so how far into the
        # fight you are is legible from across the room without a health bar.
        # Everything else in this game is read by its light; the boss should
        # be no exception.
        rage = self.rage
        beat = math.sin(self.pulse() * (1.0 + 1.6 * rage))
        art.draw_glow(self.eye_color, sx, sy, 128 + 96 * rage,
                      16 + 10 * beat + 26 * rage, power=2.6,
                      height=self.LIGHT_HEIGHT)
        if rage > 0.34:
            # A second, hotter core once it is properly angry.
            art.draw_glow(palette.BOSS_FLASH, sx, sy,
                          58 + 46 * rage, 12 + 30 * rage * (0.7 + 0.3 * beat),
                          power=3.0, height=self.LIGHT_HEIGHT)

        if self.state == 'telegraph':
            frac = clamp(self.state_t / max(self.telegraph, 1e-6), 0.0, 1.0)
            # For the ring, the warning *is* the blast radius: what the
            # circle reaches when it closes is exactly what the wave will
            # hit. Every other attack keeps the generic reach.
            reach = self.RING_WAVE if self.attack == 'ring' else 208.0
            art.draw_ring(self.eye_color, sx, sy,
                          48 + (reach - 48) * ease_out_cubic(frac),
                          28 + 52 * frac, thickness=0.06, softness=1.6)

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


# ---------------------------------------------------------------------------
class Snuffer(Enemy):
    """The floor-six boss: something that fights the lantern, not the player.

    The Choir is a slow mass that fills the room with bullets, so the answer
    to it is footwork. This is the opposite in every direction that matters -
    fast, small, and it comes for the one resource the whole game is built on.
    Its aura eats fuel, its signature attack smothers the flame down to a
    crawl for a few seconds, and it fights hardest in the dark it has just
    made. Two boss fights that ask the same question are one boss fight.
    """

    species = CHOIR
    base_hp = 6200.0
    base_speed = 172.0        # was 148
    radius = 34.0
    # Was 18. Not higher than this: it is small, fast and it chases, and at
    # depth twelve the multiplier on touch damage is nearly 2x, so a value
    # that looks reasonable here lands at 42 in play. Above that a careless
    # brush costs more than the telegraphed move the whole fight is built
    # around, which is the wrong way round - the thing you can read should
    # always be the thing that hurts most.
    touch_damage = 21.0
    ember_value = 22
    score = 700
    body_color = palette.WISP
    eye_color = palette.WISP_EYE
    mass = 22.0

    ATTACKS = ('sweep', 'rush', 'motes', 'choke')

    voice = 'snuff'

    LIGHT_HEIGHT = 26.0
    DRAIN = 8.5             # fuel a second, inside the aura (was 5.5)
    AURA = 245.0            # was 210

    # How far the choke reaches. It used to reach everywhere: the attack
    # simply happened to you, wherever you were standing, which is why the
    # fight's signature move was also the one thing in it you could not
    # play against. Now it has a radius, the telegraph draws that radius,
    # and outrunning it is the reward for reading it.
    CHOKE_REACH = 430.0
    CHOKE_DAMAGE = 26.0     # its heaviest single hit, and the one you can read
    CHOKE_FUEL = 30.0       # was 14

    # What the dark it makes is worth to it. The class docstring has always
    # said this thing "fights hardest in the dark it has just made" and
    # nothing implemented it - `choke` shrank the lantern and the Snuffer
    # carried on at exactly the same pace. The choke is a *set-up*: it buys
    # three seconds in which everything it does is faster and lands closer
    # together, which is what makes putting your light out frightening
    # rather than merely inconvenient.
    SMOTHER = 1.55

    def __init__(self, x, y, depth, rng):
        super().__init__(x, y, depth, rng)
        self.max_hp = self.base_hp * (1.0 + 0.62 * max(0, depth // 6 - 1))
        self.hp = self.max_hp
        self.spawn_t = 1.6
        self.state = 'idle'
        self.state_t = 0.0
        self.attack = None
        self.telegraph = 0.0
        self.shots_left = 0
        self.shot_timer = 0.0
        self.spin = 0.0
        self.sweep_from = 0.0
        self.charge_dir = (1.0, 0.0)
        self.intro_played = False
        # A rush is no longer one dash: see `run_attack`.
        self.rush_left = 0
        self.wake_timer = 0.0

    # Earlier than the Choir's, not later. This thing has two and a half
    # times the Choir's health, so a threshold measured as a *fraction* of it
    # is a much longer wait in seconds: on the old edges a player spent the
    # first fifty seconds of the last fight in the vault watching its opening
    # two attacks, and the choke - the move the whole floor is built around -
    # did not unlock until a third of its health was gone.
    TIERS = (0.80, 0.52, 0.24)

    @property
    def tier(self):
        frac = self.hp / max(self.max_hp, 1e-6)
        for i, edge in enumerate(self.TIERS):
            if frac > edge:
                return i + 1
        return 4

    @property
    def rage(self):
        frac = clamp(self.hp / max(self.max_hp, 1e-6), 0.0, 1.0)
        return (1.0 - frac) ** 0.85

    def available_attacks(self):
        # It used to open with two attacks, one of which does no damage, and
        # not reach its full book until it was nearly dead. The Choir opens
        # with three. Meeting the last thing in the vault with less in its
        # hands than the first one is most of why it read as the easier of
        # the two.
        p = self.tier
        if p == 1:
            return ('sweep', 'rush', 'motes')
        return self.ATTACKS

    # ------------------------------------------------------------ update ---
    def smothering(self, ctx):
        """True while the dark it made is still on the player."""
        return ctx.player.choke > 0.0

    def behave(self, dt, ctx):
        self.spin += dt * (1.1 + 0.5 * self.tier)
        player = ctx.player
        press = self.SMOTHER if self.smothering(ctx) else 1.0

        if not self.intro_played:
            # As with the Choir: the arrival, sound included, belongs to
            # `World._tick_boss_intro`.
            self.intro_played = True
            ctx.effects.add_shake(8.0)

        # The aura: standing near it costs light, whatever it is doing.
        dx, dy = player.x - self.x, player.y - self.y
        if dx * dx + dy * dy < self.AURA * self.AURA:
            player.fuel = max(0.0, player.fuel
                              - self.DRAIN * (1.0 + 0.7 * self.rage) * dt)
            if ctx.rng.chance(6.0 * dt):
                ctx.particles.embers(player.x, player.y, 1, self.eye_color,
                                     ctx.rng)

        if self.state == 'idle':
            self.steer_to(player.x, player.y, dt,
                          self.speed * (0.85 + 0.5 * self.rage) * press,
                          accel=3.0)
            if self.state_t > (0.82 - 0.55 * self.rage) / press:
                self.begin_attack(ctx)
        elif self.state == 'telegraph':
            self.vx *= math.exp(-5.0 * dt)
            self.vy *= math.exp(-5.0 * dt)
            if self.attack == 'rush':
                want = math.atan2(player.y - self.y, player.x - self.x)
                self.facing += angle_diff(self.facing, want) * clamp(4.0 * dt, 0, 1)
            if self.state_t >= self.telegraph:
                self.execute(ctx)
        elif self.state == 'attack':
            self.run_attack(dt, ctx)
        elif self.state == 'recover':
            self.vx *= math.exp(-3.5 * dt)
            self.vy *= math.exp(-3.5 * dt)
            if self.state_t > (0.34 - 0.20 * self.rage) / press:
                self.set_state('idle')

    def set_state(self, name):
        self.state = name
        self.state_t = 0.0

    def begin_attack(self, ctx):
        self.attack = ctx.rng.choice(self.available_attacks())
        self.telegraph = {'sweep': 0.62, 'rush': 0.68, 'motes': 0.7,
                          'choke': 0.85}[self.attack]
        self.telegraph *= (1.0 - 0.50 * self.rage)
        if self.smothering(ctx):
            self.telegraph /= self.SMOTHER
        self.set_state('telegraph')
        ctx.effects.add_light(self.x, self.y, 170, self.telegraph,
                              self.eye_color)
        _tell(self)

    def execute(self, ctx):
        self.set_state('attack')
        player = ctx.player
        if self.attack == 'sweep':
            self.shots_left = 34 + 12 * self.tier
            self.shot_timer = 0.0
            self.sweep_from = math.atan2(player.y - self.y, player.x - self.x)
        elif self.attack == 'rush':
            # One dash was a thing you sidestepped once and then had a beat
            # to breathe. Two or three, re-aimed between and trailing bolts,
            # is a thing you have to keep moving away from.
            self.rush_left = 1 + (1 if self.tier >= 2 else 0) \
                + (1 if self.tier >= 4 else 0)
            self._launch_rush(ctx)
        elif self.attack == 'motes':
            self.shots_left = 4 + 2 * self.tier
            self.shot_timer = 0.0
        elif self.attack == 'choke':
            # The signature: it puts the light out for a moment, and the room
            # it leaves you in is the one it is best at fighting in.
            ctx.effects.add_shake(7.0)
            ctx.particles.ripple(self.x, self.y, self.eye_color,
                                 self.CHOKE_REACH, 0.5, 80)
            dx, dy = player.x - self.x, player.y - self.y
            if dx * dx + dy * dy <= self.CHOKE_REACH * self.CHOKE_REACH:
                player.choke = max(player.choke, 2.6 + 1.2 * self.rage)
                player.fuel = max(0.0, player.fuel - self.CHOKE_FUEL)
                ctx.effects.add_flash(0.8, palette.VOID, wash=True)
                player.hurt(ctx.enemy_bullet_damage(self.CHOKE_DAMAGE),
                            ctx.effects, ctx.particles, ctx.fxrng,
                            math.atan2(dy, dx) if dx or dy else 0.0)
            # Unpositioned and loud, because this one happens to *you* rather
            # than over there: your light goes out and the screen washes with
            # it. It is also the only effect in the game with no room on it,
            # which is the point - see `snuff_choke` in `audio`.
            audio.play('snuff_choke', 0.95)
            self.set_state('recover')

    def run_attack(self, dt, ctx):
        self.shot_timer -= dt
        player = ctx.player

        if self.attack == 'rush':
            self.vx *= math.exp(-1.9 * dt)
            self.vy *= math.exp(-1.9 * dt)
            if ctx.rng.chance(26.0 * dt):
                ctx.particles.embers(self.x, self.y, 2, self.eye_color, ctx.rng)
            # It leaves the room worse than it found it: a spreading V of
            # slow bolts behind it, so the line it took stays dangerous for
            # a couple of seconds after it has gone.
            self.wake_timer -= dt
            if self.wake_timer <= 0.0 and self.state_t < 0.5:
                self.wake_timer = 0.085
                back = math.atan2(-self.charge_dir[1], -self.charge_dir[0])
                for k in (-0.42, 0.42):
                    self._bolt(ctx, back + k, 200.0, 8.0, life=2.6)
            if self.state_t > 0.62:
                self.rush_left -= 1
                if self.rush_left > 0:
                    self._launch_rush(ctx)
                    self.state_t = 0.0
                else:
                    self.set_state('recover')
            return

        if self.shots_left <= 0:
            self.set_state('recover')
            return
        if self.shot_timer > 0.0:
            return

        if self.attack == 'sweep':
            # A turning arm of bullets: you go round it, or through the gap.
            arms = 3 if self.tier < 3 else 4
            turn = self.state_t * (5.6 + 3.0 * self.rage)
            for i in range(arms):
                a = self.sweep_from + turn + i * math.tau / arms
                self._bolt(ctx, a, 300.0, 12.0)
            self.shot_timer = 0.05
            self.shots_left -= 1
            if self.shots_left % 10 == 0:
                audio.play_at('snuff_sweep', self.x, self.y, 0.34)
        elif self.attack == 'motes':
            a = math.atan2(player.y - self.y, player.x - self.x)
            fan = (-2, -1, 0, 1, 2) if self.tier >= 3 else (-1, 0, 1)
            for k in fan:
                self._bolt(ctx, a + k * 0.22, 210.0, 15.0, life=4.5,
                           homing=1.15)
            self.shot_timer = 0.19 - 0.07 * self.rage
            self.shots_left -= 1
            audio.play_at('snuff_motes', self.x, self.y, 0.5)

    def _launch_rush(self, ctx):
        """Point it at the player and throw it."""
        player = ctx.player
        a = math.atan2(player.y - self.y, player.x - self.x)
        self.charge_dir = (math.cos(a), math.sin(a))
        speed = 900.0 * (1.0 + 0.3 * self.rage)
        self.vx = self.charge_dir[0] * speed
        self.vy = self.charge_dir[1] * speed
        self.wake_timer = 0.0
        ctx.effects.add_shake(4.5)
        audio.play_at('snuff_rush', self.x, self.y, 0.8)

    def _bolt(self, ctx, angle, speed, damage, life=3.2, homing=0.0):
        speed *= 1.0 + 0.4 * self.rage
        if self.smothering(ctx):
            speed *= 1.22
        damage *= 1.0 + 0.3 * self.rage
        ctx.projectiles.spawn(
            1, self.x + math.cos(angle) * self.radius * 0.8,
            self.y + math.sin(angle) * self.radius * 0.8,
            math.cos(angle) * speed, math.sin(angle) * speed,
            ctx.enemy_bullet_damage(damage),
            radius=6.5, life=life, color=palette.WISP_EYE,
            glow_color=palette.WISP, length=13.0, width=10.0,
            knockback=70.0, homing=homing)

    def damage_by(self, amount, ctx, angle=None, knockback=0.0, crit=False):
        """Mark the tier boundaries, which used to pass in silence.

        Its phases change what it throws and how hard it presses, and nothing
        said so - the Choir has announced its own since it was written. This
        is deliberately *not* the Choir's full treatment: no slow motion and
        no forced recovery, because those are pacing decisions belonging to a
        slow mass filling a room with bullets, and this thing's whole
        argument is that it never gives you that beat.
        """
        before = self.tier
        # Knockback passes straight through: the Choir scales it because it
        # is a forty-tonne mass, and changing this one's would be a change to
        # the fight rather than to what it sounds like.
        dealt = super().damage_by(amount, ctx, angle, knockback, crit)
        if self.alive and self.tier != before:
            ctx.effects.add_shake(6.0)
            ctx.effects.add_flash(0.4, self.eye_color, wash=True)
            ctx.effects.add_light(self.x, self.y, 420.0, 0.4, self.eye_color)
            ctx.particles.burst(self.x, self.y, 34, self.eye_color, ctx.rng,
                                speed=(160, 460), life=(0.3, 0.7), size=(2, 5))
            ctx.particles.ripple(self.x, self.y, self.eye_color, 300, 0.55, 80)
            audio.play('snuff_phase', 0.85)
        return dealt

    def die(self, ctx, angle=None):
        ctx.effects.add_flash(1.0, self.eye_color, wash=True)
        ctx.effects.add_shake(14.0)
        super().die(ctx, angle)

    # -------------------------------------------------------------- draw ---
    def draw(self, ox, oy, lit):
        self.draw_body(self.x - ox, self.y - oy)

    def draw_body(self, sx, sy):
        opacity = self.body_opacity()
        r = self.radius
        rage = self.rage
        flash = self.hit_flash > 0.82
        color = palette.BOSS_FLASH if flash else self.body_color

        # It carries a cold light, and unlike the Choir's it *shrinks* as the
        # thing rages: the fight gets darker the closer it is to dying.
        beat = math.sin(self.spin * 1.7)
        art.draw_glow(self.eye_color, sx, sy, (150 - 54 * rage) * (0.9 + 0.1 * beat),
                      22 + 8 * beat, power=2.5, height=self.LIGHT_HEIGHT)

        if self.state == 'telegraph':
            frac = clamp(self.state_t / max(self.telegraph, 1e-6), 0.0, 1.0)
            # For the choke, the warning is the reach: what the circle
            # covers when it closes is what will be smothered.
            reach = self.CHOKE_REACH if self.attack == 'choke' else 170.0
            art.draw_ring(self.eye_color, sx, sy,
                          40 + (reach - 40) * ease_out_cubic(frac),
                          30 + 46 * frac, thickness=0.05, softness=1.5)

        # A ragged crown of shards, turning against itself.
        for ring, (count, rad, spin) in enumerate(
                ((7, 1.0, 0.7), (5, 0.62, -1.3))):
            pts = []
            for i in range(count):
                a = self.spin * spin + i * math.tau / count
                d = r * rad * (1.0 + 0.1 * math.sin(self.spin * 2.0 + i))
                pts.append(sx + math.cos(a) * d)
                pts.append(sy + math.sin(a) * d)
            drawPolygon(*pts, fill=color if ring == 0 else palette.VOID,
                        opacity=opacity if ring == 0 else int(opacity * 0.85))

        # The eye, which is the only warm thing about it.
        art.draw_glow(palette.LIGHT_CORE, sx, sy, 16 + 6 * rage, 60,
                      power=3.0)

    def pulse(self):
        return self.spin


# Which thing is waiting on which floor. The Choir is the one you meet first -
# a slow mass filling the room with bullets, which is a fight you can learn by
# moving. The Snuffer waits at the bottom, because a boss that takes your
# light away is only frightening once you have spent eleven floors relying on
# it.
# One per act, and the acts moved when the run became twenty floors: the
# Choir still closes the first, the Snuffer the second. Floor twenty has no
# boss of its own yet and falls back to the Snuffer rather than to the Choir,
# because meeting the floor-six fight again at the bottom of the vault reads
# as a mistake, where meeting the previous act's does at least escalate.
class Keeper(Enemy):
    """The last thing in the vault, and the reason the vault is dark.

    It has every lantern but yours.

    For nineteen floors light has been the one safe thing in the game: it is
    how you see, it is what the things in the dark are afraid of, and it is
    the resource every other system is built around protecting. This fight
    takes that away by turning it round. The Keeper attacks *with* light - it
    brands the floor with it, it sweeps the room with it, and at the end it
    gathers every scrap in the chamber and throws the lot at you. The shadows
    it casts are the only cover there is.

    And where the Snuffer's own light shrinks as it rages - so its fight gets
    darker the closer it is to dying - the Keeper's grows. The last phase of
    the last fight in the game is played in a room that is almost entirely
    lit, with almost nowhere left to stand. That inversion is the whole
    reason it is at the bottom: the game spends twenty floors teaching you to
    want light, and then asks what you do when you finally get it.
    """

    species = CHOIR
    # Measured down from 9400 with `tools/boss_probe.py`: at that health, and
    # with the lantern shield as originally written, the fight did not finish
    # inside a five-minute cap on any seed.
    base_hp = 7200.0
    base_speed = 96.0
    radius = 42.0
    # Slow and enormous, so touching it is a mistake you had time to avoid.
    # Kept below the telegraphed moves for the same reason the Snuffer's is:
    # the thing you can read should always be the thing that hurts most.
    touch_damage = 24.0
    ember_value = 34
    score = 1400
    body_color = palette.KEEPER
    eye_color = palette.KEEPER_EYE
    mass = 40.0

    ATTACKS = ('brand', 'lanterns', 'glare', 'reap')

    voice = 'keep'

    LIGHT_HEIGHT = 30.0

    #: How far its own light reaches, at rest and at its worst. The Snuffer
    #: runs this the other way; see the class docstring.
    GLOW_MIN = 210.0
    GLOW_MAX = 690.0

    #: A brand: how long the mark sits before it lights, and how long it
    #: burns once it has.
    BRAND_ARM = 1.15
    BRAND_TIME = 3.4
    BRAND_RADIUS = 96.0
    BRAND_DPS = 46.0

    #: How long a held lantern burns before it gutters out on its own.
    LANTERN_LIFE = 9.0

    #: The reap: how long the room goes dark before the flash, and what the
    #: flash costs anyone still standing in the open when it lands.
    REAP_GATHER = 1.5
    REAP_DAMAGE = 68.0

    TIERS = (0.78, 0.50, 0.24)

    def __init__(self, x, y, depth, rng):
        super().__init__(x, y, depth, rng)
        self.max_hp = self.base_hp * (1.0 + 0.5 * max(0, depth // 7 - 1))
        self.hp = self.max_hp
        self.spawn_t = 1.8
        self.state = 'idle'
        self.state_t = 0.0
        self.attack = None
        self.telegraph = 0.0
        self.shots_left = 0
        self.shot_timer = 0.0
        self.spin = 0.0
        self.intro_played = False
        self.glare_from = 0.0
        self.reaping = 0.0
        #: The lanterns it carries: [angle, hp, seconds left]. Orbiting,
        #: shootable, and they burn out - without a lifetime they piled up
        #: across the fight and were back to shielding everything by the
        #: minute mark.
        self.lanterns = []

    @property
    def tier(self):
        frac = self.hp / max(self.max_hp, 1e-6)
        for i, edge in enumerate(self.TIERS):
            if frac > edge:
                return i + 1
        return 4

    @property
    def rage(self):
        frac = clamp(self.hp / max(self.max_hp, 1e-6), 0.0, 1.0)
        return (1.0 - frac) ** 0.85

    def glow_radius(self):
        """Its own light. Grows as it dies - see the class docstring."""
        if self.reaping > 0.0:
            # Gathering: it pulls every scrap in, and the room goes dark.
            g = clamp(self.reaping / self.REAP_GATHER, 0.0, 1.0)
            return self.GLOW_MIN + (40.0 - self.GLOW_MIN) * g
        return self.GLOW_MIN + (self.GLOW_MAX - self.GLOW_MIN) * self.rage

    def available_attacks(self):
        # It opens with three, like the Choir. A final boss that spends its
        # first minute showing you two moves reads as easier than the one
        # halfway up - a mistake this game has already made once, and the
        # measurements that caught it are in `Snuffer.available_attacks`.
        if self.tier == 1:
            return ('brand', 'lanterns', 'glare')
        return self.ATTACKS

    # ------------------------------------------------------------ update ---
    def behave(self, dt, ctx):
        self.spin += dt * (0.7 + 0.35 * self.tier)
        player = ctx.player
        self.reaping = max(0.0, self.reaping - dt)
        self._update_lanterns(dt, ctx)

        # It never chases hard. The pressure in this fight is the room, not
        # the body - a boss that both fills the floor with light and runs you
        # down is two fights at once.
        d = math.hypot(player.x - self.x, player.y - self.y)
        if d > 300.0:
            self.chase(dt, ctx, self.speed * (0.7 + 0.5 * self.rage))
        else:
            self.vx *= math.exp(-2.4 * dt)
            self.vy *= math.exp(-2.4 * dt)

        if self.state == 'idle':
            if self.attack_cd <= 0.0:
                self.begin_attack(ctx)
        elif self.state == 'telegraph':
            if self.state_t >= self.telegraph:
                self.execute(ctx)
        elif self.state == 'attack':
            self.run_attack(dt, ctx)
        elif self.state == 'recover':
            if self.state_t >= (0.70 - 0.34 * self.rage):
                self.set_state('idle')
                self.attack_cd = (0.80 - 0.38 * self.rage)

    def _update_lanterns(self, dt, ctx):
        """The lanterns it carries, orbiting it and lighting the room.

        They are light sources in their own right, which is what makes them
        worth shooting: every one broken is a piece of the chamber handed
        back to the dark.
        """
        if not self.lanterns:
            return
        for held in self.lanterns:
            held[0] += dt * 0.9
            held[2] -= dt
            if held[1] <= 0.0 or held[2] <= 0.0:
                continue
            hx, hy = self.lantern_pos(held)
            ctx.effects.add_light(hx, hy, 190.0, dt * 1.2,
                                  art.rgb_tuple(palette.KEEPER_EYE))
        self.lanterns = [h for h in self.lanterns
                         if h[1] > 0.0 and h[2] > 0.0]

    def lantern_pos(self, held):
        r = self.radius + 74.0
        return (self.x + math.cos(held[0]) * r,
                self.y + math.sin(held[0]) * r)

    def set_state(self, name):
        self.state = name
        self.state_t = 0.0

    def begin_attack(self, ctx):
        self.attack = ctx.rng.choice(self.available_attacks())
        self.telegraph = {'brand': 0.70, 'lanterns': 0.80,
                          'glare': 0.78, 'reap': 1.05}[self.attack]
        self.telegraph *= (1.0 - 0.45 * self.rage)
        self.set_state('telegraph')
        ctx.effects.add_light(self.x, self.y, 220, self.telegraph,
                              self.eye_color)
        _tell(self)

    def execute(self, ctx):
        self.set_state('attack')
        player = ctx.player
        if self.attack == 'brand':
            # Marks on the floor that light a beat later. The whole attack is
            # in that gap: you are shown exactly where and given exactly
            # enough time, and the room stays smaller for a while afterwards.
            count = 3 + self.tier
            ctx.add_pool(player.x, player.y, self.BRAND_RADIUS,
                         self.BRAND_TIME, self.BRAND_DPS,
                         color=palette.KEEPER_EYE, arm=self.BRAND_ARM)
            for _ in range(count - 1):
                a = ctx.rng.angle()
                d = ctx.rng.uniform(90.0, 340.0)
                ctx.add_pool(player.x + math.cos(a) * d,
                             player.y + math.sin(a) * d,
                             self.BRAND_RADIUS, self.BRAND_TIME,
                             self.BRAND_DPS, color=palette.KEEPER_EYE,
                             arm=self.BRAND_ARM)
            audio.play_at('keep_brand', self.x, self.y, 0.7)
            self.set_state('recover')
        elif self.attack == 'lanterns':
            want = 4 + self.tier
            base = ctx.rng.angle()
            for i in range(want):
                self.lanterns.append(
                    [base + i * math.tau / want, 90.0 + 30.0 * self.tier,
                     self.LANTERN_LIFE])
            self.shots_left = 5 + 2 * self.tier
            self.shot_timer = 0.35
            audio.play_at('keep_lantern', self.x, self.y, 0.75)
        elif self.attack == 'glare':
            self.shots_left = 30 + 10 * self.tier
            self.shot_timer = 0.0
            self.glare_from = math.atan2(player.y - self.y,
                                         player.x - self.x) - 1.1
            audio.play_at('keep_glare', self.x, self.y, 0.7)
        elif self.attack == 'reap':
            # It takes the room's light into itself, and for a second and a
            # half this is the darkest the game ever gets. Then it gives all
            # of it back at once.
            self.reaping = self.REAP_GATHER
            ctx.effects.add_shake(5.0)
            audio.play('keep_reap', 0.9)

    def run_attack(self, dt, ctx):
        self.shot_timer -= dt
        player = ctx.player

        if self.attack == 'reap':
            if self.reaping > 0.0:
                if ctx.fxrng.chance(40.0 * dt):
                    a = ctx.fxrng.angle()
                    d = ctx.fxrng.uniform(180.0, 520.0)
                    ctx.particles.emit(
                        1, self.x + math.cos(a) * d, self.y + math.sin(a) * d,
                        -math.cos(a) * 300.0, -math.sin(a) * 300.0,
                        0.7, 3.0, palette.KEEPER_EYE, end_size=0.2,
                        opacity=90, drag=0.2)
                return
            self._release(ctx)
            self.set_state('recover')
            return

        if self.attack == 'lanterns':
            if self.shots_left <= 0 or not self.lanterns:
                self.set_state('recover')
                return
            if self.shot_timer > 0.0:
                return
            for held in self.lanterns:
                hx, hy = self.lantern_pos(held)
                a = math.atan2(player.y - hy, player.x - hx)
                self._bolt(ctx, a, 340.0, 18.0, x=hx, y=hy)
            self.shot_timer = 0.52 - 0.18 * self.rage
            self.shots_left -= 1
            audio.play_at('keep_lantern', self.x, self.y, 0.3)
            return

        if self.shots_left <= 0:
            self.set_state('recover')
            return
        if self.shot_timer > 0.0:
            return

        if self.attack == 'glare':
            # A fan that sweeps one way across the room. Unlike the Snuffer's
            # turning arms there is always a side it is not covering, and the
            # answer is to be standing on that side.
            turn = self.state_t * (2.2 + 1.1 * self.rage)
            a = self.glare_from + turn
            for k in (-0.09, 0.0, 0.09):
                self._bolt(ctx, a + k, 380.0, 16.0)
            self.shot_timer = 0.055
            self.shots_left -= 1
            if self.shots_left % 12 == 0:
                audio.play_at('keep_glare', self.x, self.y, 0.28)

    def _release(self, ctx):
        """The reap's second half: everything it gathered, given back."""
        player = ctx.player
        ctx.effects.add_flash(1.0, palette.KEEPER_EYE, wash=True)
        ctx.effects.add_shake(11.0)
        ctx.particles.ripple(self.x, self.y, palette.KEEPER_EYE, 900.0,
                             0.8, 95)
        audio.play('keep_release', 1.0)
        # Cover is the whole answer. A wall between you and it turns this
        # aside entirely, which is why the sanctum has pillars in it - and
        # why this is the one attack in the game that checks line of sight
        # rather than distance.
        if not ctx.level.ray_blocked(self.x, self.y, player.x, player.y):
            player.hurt(ctx.enemy_bullet_damage(self.REAP_DAMAGE),
                        ctx.effects, ctx.particles, ctx.fxrng,
                        math.atan2(player.y - self.y, player.x - self.x))
        ctx.effects.add_light(self.x, self.y, self.GLOW_MAX, 0.9,
                              art.rgb_tuple(palette.KEEPER_EYE))

    def _bolt(self, ctx, angle, speed, damage, life=3.2, x=None, y=None):
        ox = self.x if x is None else x
        oy = self.y if y is None else y
        r = self.radius if x is None else 8.0
        ctx.projectiles.spawn(
            1, ox + math.cos(angle) * r, oy + math.sin(angle) * r,
            math.cos(angle) * speed, math.sin(angle) * speed,
            ctx.enemy_bullet_damage(damage), radius=7.0, life=life,
            color=palette.KEEPER_EYE, glow_color=(255, 236, 176),
            length=15.0, width=11.0, knockback=70.0)

    def damage_by(self, amount, ctx, angle=None, knockback=0.0, crit=False):
        # A shot that lands on a held lantern breaks that instead. They orbit
        # in front of the body, so a player who ignores them is shooting
        # through them - and each one broken is a piece of the room given
        # back to the dark, which is the only way this fight gets easier.
        if self.lanterns and angle is not None:
            for held in self.lanterns:
                if held[1] <= 0.0:
                    continue
                hx, hy = self.lantern_pos(held)
                facing = math.atan2(hy - self.y, hx - self.x)
                # A narrow arc each, on purpose. At the 0.42 rad this
                # started at, seven lanterns covered 94% of every angle the
                # player could shoot from - which is not a shield, it is
                # invulnerability, and the probe could not kill the thing
                # inside five minutes. At 0.15 they cover about a third:
                # enough that ignoring them costs you, not enough that they
                # decide the fight.
                if abs(angle_diff(facing, angle + math.pi)) < 0.15:
                    held[1] -= amount
                    if held[1] <= 0.0:
                        ctx.particles.burst(hx, hy, 26, palette.KEEPER_EYE,
                                            ctx.rng, speed=(140, 400),
                                            life=(0.2, 0.6), size=(2.0, 4.8))
                        audio.play_at('keep_lantern_break', hx, hy, 0.7)
                    return 0.0
        before = self.tier
        dealt = super().damage_by(amount, ctx, angle, knockback, crit)
        if self.alive and self.tier != before:
            audio.play('keep_phase', 0.85)
            ctx.effects.add_flash(0.6, palette.KEEPER_EYE)
            ctx.effects.add_shake(6.0)
        return dealt

    def die(self, ctx, angle=None):
        # `super().die` is what actually sets `alive` and books the kill -
        # and `on_kill` is what starts the coming-apart. Calling
        # `begin_boss_death` directly instead left the thing alive at
        # negative health forever, which the probe found as a fight that
        # could not be finished inside four minutes on any seed.
        ctx.effects.add_flash(1.0, self.eye_color, wash=True)
        ctx.effects.add_shake(15.0)
        super().die(ctx, angle)

    # -------------------------------------------------------------- draw ---
    def draw(self, ox, oy, lit):
        # It is its own light source, so it is always drawn. Being unlit is
        # not a state this one can be in.
        Enemy.draw(self, ox, oy, True)

    def draw_body(self, sx, sy):
        color = palette.LIGHT_CORE if self.hit_flash > 0.45 else self.body_color
        r = self.radius
        opacity = self.body_opacity()

        # The held lanterns, drawn first so the body sits in front of them.
        for held in self.lanterns:
            if held[1] <= 0.0:
                continue
            hx = sx + math.cos(held[0]) * (r + 74.0)
            hy = sy + math.sin(held[0]) * (r + 74.0)
            art.draw_glow(art.rgb_tuple(palette.KEEPER_EYE), hx, hy,
                          46.0, 60, power=2.0)
            g = 5.0
            drawPolygon(hx, hy - g, hx + g * 0.8, hy, hx, hy + g,
                        hx - g * 0.8, hy, fill=palette.LIGHT_CORE,
                        opacity=92)

        # A hooded mass, hunched over what it is holding.
        pts = []
        for i in range(11):
            a = self.spin * 0.2 + i * math.tau / 11
            rad = r * (1.0 + 0.1 * math.sin(self.spin * 1.3 + i * 1.9))
            pts.append(sx + math.cos(a) * rad)
            pts.append(sy + math.sin(a) * rad)
        drawPolygon(*pts, fill=color, opacity=opacity)

        # The hoard inside it: a ring of small lights, more of them the
        # nearer it is to dying, so the body itself reads as filling up.
        held_count = 6 + int(6 * self.rage)
        for i in range(held_count):
            a = -self.spin * 0.6 + i * math.tau / held_count
            rr = r * 0.58
            lx = sx + math.cos(a) * rr
            ly = sy + math.sin(a) * rr
            sz = 2.6 + 1.4 * math.sin(self.spin * 2.0 + i)
            drawPolygon(lx, ly - sz, lx + sz * 0.8, ly, lx, ly + sz,
                        lx - sz * 0.8, ly, fill=palette.KEEPER_EYE,
                        opacity=op(70 + 26 * self.rage))

        # The eye. Wider the angrier it is, and during a reap it is the only
        # thing in the room still lit.
        eye = r * (0.3 + 0.12 * self.rage)
        if self.reaping > 0.0:
            eye *= 1.0 + 0.6 * clamp(self.reaping / self.REAP_GATHER, 0.0, 1.0)
        drawPolygon(sx, sy - eye, sx + eye * 0.75, sy, sx, sy + eye,
                    sx - eye * 0.75, sy, fill=palette.LIGHT_CORE,
                    opacity=op(88))

    def pulse(self):
        return 0.5 + 0.5 * math.sin(self.spin * 2.2)


BOSSES = {6: HollowChoir, 13: Snuffer, 20: Keeper}


def for_depth(depth):
    """The boss class for a boss floor, defaulting to the Choir."""
    return BOSSES.get(depth, HollowChoir)


NAMES = {Snuffer: 'THE SNUFFER', HollowChoir: 'THE HOLLOW CHOIR',
         Keeper: 'THE KEEPER'}
