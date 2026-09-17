"""Camera, screen shake, hit-stop, floating numbers, and transient lights.

These are the systems that make a hit feel like a hit. None of them touch game
logic - if every one were switched off the game would play identically, it
would just feel dead.
"""

import math

from . import gpu
from .draw import drawImage, drawLine, drawPolygon

from . import art, palette
from .config import CAMERA_LERP, CAMERA_LOOKAHEAD, HITSTOP_MAX, SHAKE_DECAY
from .mathx import clamp, ease_out_cubic


class Camera:
    """Where the view is, and what it is allowed to see.

    ## The framing, and why it is not animated

    A chamber narrower than the window is centred in it, and a chamber wider
    than the window lets the camera roam inside its edges. A crossing frames
    both chambers at once, so the camera can follow the player out of one and
    into the next without either room's edge catching it.

    Switching between those framings moves the view even when the player is
    standing still, and the whole distance has to be covered somehow. The
    obvious answer - animate the frame itself, from one room's rectangle to
    two and back - is the wrong one, and it was wrong in a way that only
    shows up in the case that matters. While the frame is close to the
    window's own size the clamp has almost no slack in it, so the camera is
    not following the player at all: it is *pinned to the frame*, and it
    travels at whatever speed the frame is travelling. Coming out of a
    crossing that meant a room's width in a little over half a second -
    measured, 15.8 design units in a single frame at 120 Hz, against the 2.2
    a walking player covers - which is the shove you feel at the end of every
    door.

    So the frame is not animated. It changes the instant it is asked to, and
    what is eased instead is the **correction it implies**: the difference
    between where the camera was and where the new framing would put it is
    taken on as a debt, so nothing moves on the frame the framing changes,
    and the debt is then paid off over a window long enough that it is never
    paid faster than `PAN_PEAK`. The camera keeps following the player
    against the real frame the entire time, with the outstanding correction
    riding on top - so it can settle gently *and* never lose the player,
    which the animated frame could not do at once.
    """

    #: How fast the framing may pull the view about on its own, in design
    #: units a second, at the quickest point of the move. A walk is 268, and
    #: a correction that travels at about the speed the world already moves
    #: when you walk is one the eye reads as the room settling.
    PAN_PEAK = 330.0
    #: The window a correction is paid off over, whatever its size. The floor
    #: keeps a tiny nudge from being a slow crawl; the ceiling keeps a
    #: room-sized one from drifting for half the next fight.
    #:
    #: A crossing between two chambers narrower than the window owes about
    #: eight hundred units - a whole room, because the view has to travel
    #: from one centred room to the next - and that is more than `PAN_PEAK`
    #: will carry inside the ceiling. Measured over forty-eight crossings,
    #: the worst single frame at 120 Hz: 21.7 units with the frame animated,
    #: 6.3 at a 1.9 s ceiling, 5.6 at 2.6, 5.3 at 3.2. Past about two and a
    #: half seconds it is buying very little and the view is still settling
    #: while the next fight starts.
    PAY_MIN = 0.30
    PAY_MAX = 2.6

    def __init__(self, view_w, view_h):
        self.x = 0.0
        self.y = 0.0
        self.view_w = view_w
        self.view_h = view_h
        self.shake = 0.0
        self.shake_x = 0.0
        self.shake_y = 0.0
        self.bounds_w = view_w
        self.bounds_h = view_h
        # Where the framed region starts. Zero for a single room, and set
        # negative while the player is crossing into a room that sits above
        # or to the left of the one they are leaving - see `World.crossing`.
        self.bounds_x = 0.0
        self.bounds_y = 0.0
        # Where the camera wants to be before the framing has its say. Kept
        # separately so a framing that opens up knows where the player was
        # standing rather than where the old frame had pinned the view.
        self._raw_x = 0.0
        self._raw_y = 0.0
        # And where it would rather be with no lag at all - the player's own
        # position, this frame. See `set_bounds`.
        self._goal_x = 0.0
        self._goal_y = 0.0
        # The framing correction still owed, what it was when it was taken
        # on, and how far through paying it we are.
        self._off_x = 0.0
        self._off_y = 0.0
        self._owed_x = 0.0
        self._owed_y = 0.0
        self._pay_t = 1.0
        self._pay_dur = self.PAY_MIN
        self._t = 0.0

    def snap_to(self, x, y):
        self._raw_x = self._goal_x = x - self.view_w * 0.5
        self._raw_y = self._goal_y = y - self.view_h * 0.5
        self._off_x = self._off_y = 0.0
        self._owed_x = self._owed_y = 0.0
        self._pay_t = 1.0
        self.x, self.y = self._framed(self._raw_x, self._raw_y)

    def _framed(self, x, y):
        """Where the framing puts a camera that would rather be at (x, y)."""
        lo_x, lo_y = self.bounds_x, self.bounds_y
        fx = clamp(x, lo_x, lo_x + max(0.0, self.bounds_w - self.view_w))
        fy = clamp(y, lo_y, lo_y + max(0.0, self.bounds_h - self.view_h))
        # Centre the chamber when it is smaller than the window.
        if self.bounds_w < self.view_w:
            fx = lo_x + (self.bounds_w - self.view_w) * 0.5
        if self.bounds_h < self.view_h:
            fy = lo_y + (self.bounds_h - self.view_h) * 0.5
        return fx, fy

    def set_bounds(self, w, h, x=0.0, y=0.0, ease=False):
        """Frame this region.

        `ease` takes the move on as a correction to be paid off gradually
        rather than letting it land on the next frame - see the class note.
        Without it the framing simply takes effect, which is what arriving in
        a room wants: there is nothing on screen yet to be continuous with.
        """
        if (w, h, x, y) == (self.bounds_w, self.bounds_h,
                            self.bounds_x, self.bounds_y):
            return
        before = self._framed(self._raw_x, self._raw_y) if ease else None
        self.bounds_w, self.bounds_h = w, h
        self.bounds_x, self.bounds_y = x, y
        if before is None:
            self._off_x = self._off_y = 0.0
            self._owed_x = self._owed_y = 0.0
            self._pay_t = 1.0
            self.x, self.y = self._framed(self._raw_x, self._raw_y)
            return
        # Let the follow out of whatever the old frame was holding it back
        # from, here, where it becomes part of the debt - rather than on the
        # next frame, where it becomes a lurch. Walking at a door in a room
        # taller than the window pins the follow against that room's edge for
        # as long as it takes to get there; the frame then opens onto two
        # rooms and releases it, and the exponential catch-up that follows is
        # fastest on its very first frame. Measured, 15.9 units at 120 Hz -
        # the same shove as the collapse at the far end, from the other
        # direction.
        self._raw_x, self._raw_y = self._goal_x, self._goal_y
        after = self._framed(self._raw_x, self._raw_y)
        # Whatever was still owed rolls into the new debt, so two framing
        # changes in quick succession - which is exactly what a crossing is -
        # settle once instead of fighting each other.
        self._owe(self._off_x + before[0] - after[0],
                  self._off_y + before[1] - after[1])

    def _owe(self, dx, dy):
        """Carry `dx, dy` of correction, over a window that bounds its speed."""
        self._off_x, self._off_y = dx, dy
        self._owed_x, self._owed_y = dx, dy
        # Smoothstep is quickest in the middle, at 1.5x its own average, so
        # this is the shortest window in which the pan still tops out at
        # PAN_PEAK.
        self._pay_dur = clamp(1.5 * math.hypot(dx, dy) / self.PAN_PEAK,
                              self.PAY_MIN, self.PAY_MAX)
        self._pay_t = 0.0

    def _pay(self, dt):
        """Work the outstanding correction down toward nothing.

        Smoothstepped rather than lerped, because an exponential is fastest
        on its first frame - measured, 22 units of pan in one frame at 120 Hz
        and decaying from there, which reads as a shove followed by a drift.
        A pan should start and finish gently and be quickest in between.
        """
        if self._pay_t >= 1.0:
            return
        self._pay_t = min(1.0, self._pay_t + dt / self._pay_dur)
        t = self._pay_t
        k = 1.0 - t * t * (3.0 - 2.0 * t)
        self._off_x = self._owed_x * k
        self._off_y = self._owed_y * k

    def shift(self, dx, dy):
        """Move the camera and its frame by the same amount.

        Used when a crossing rebases the world under the player: the numbers
        all change and nothing moves on screen, which is the entire trick.
        The outstanding correction is a difference, so it survives untouched.
        """
        self.x += dx
        self.y += dy
        self._raw_x += dx
        self._raw_y += dy
        self.bounds_x += dx
        self.bounds_y += dy

    def follow(self, tx, ty, aim_x, aim_y, dt):
        goal_x = self._goal_x = tx + aim_x * CAMERA_LOOKAHEAD - self.view_w * 0.5
        goal_y = self._goal_y = ty + aim_y * CAMERA_LOOKAHEAD - self.view_h * 0.5
        k = 1.0 - math.exp(-(CAMERA_LERP * 60.0) * dt)
        self._raw_x += (goal_x - self._raw_x) * k
        self._raw_y += (goal_y - self._raw_y) * k
        # Held inside the frame on any axis the frame can actually constrain,
        # so the camera answers the instant the player turns round against a
        # wall rather than first walking an invisible position back inside
        # the room. On an axis where the chamber is narrower than the window
        # there is no inside to be held in - the view is centred there - and
        # this is only remembered so that a frame which opens up knows where
        # the player had got to.
        if self.bounds_w >= self.view_w:
            self._raw_x = clamp(self._raw_x, self.bounds_x,
                                self.bounds_x + self.bounds_w - self.view_w)
        if self.bounds_h >= self.view_h:
            self._raw_y = clamp(self._raw_y, self.bounds_y,
                                self.bounds_y + self.bounds_h - self.view_h)
        self._pay(dt)
        fx, fy = self._framed(self._raw_x, self._raw_y)
        self.x = fx + self._off_x
        self.y = fy + self._off_y

        self._t += dt
        if self.shake > 0.01:
            self.shake = max(0.0, self.shake - SHAKE_DECAY * self.shake * dt
                             - 0.55 * dt)
            amp = self.shake
            # Two out-of-phase sines read as a jolt rather than random jitter.
            self.shake_x = math.sin(self._t * 61.0) * amp + math.sin(self._t * 37.0) * amp * 0.6
            self.shake_y = math.cos(self._t * 53.0) * amp + math.cos(self._t * 29.0) * amp * 0.6
        else:
            self.shake = 0.0
            self.shake_x = self.shake_y = 0.0

    def add_shake(self, amount):
        self.shake = min(26.0, self.shake + amount)

    @property
    def ox(self):
        # Integer-aligned on the cmu-graphics renderer, on purpose: it blits an
        # image ~15x faster when the destination lands on whole pixels, and at
        # a fractional offset falls back to resampling - the two full-chamber
        # layers alone cost about 4 ms a frame that way.
        #
        # On the GPU it is exactly the wrong thing. Sampling between texels is
        # free there, while snapping quantises the whole world to a design
        # unit - about three physical pixels at native scale - so the ground
        # lurches in steps while the player, drawn at exact coordinates, moves
        # smoothly. At 120 fps that reads as the screen juddering as you walk.
        if gpu.active():
            return self.x + self.shake_x
        return float(int(self.x + self.shake_x))

    @property
    def oy(self):
        if gpu.active():
            return self.y + self.shake_y
        return float(int(self.y + self.shake_y))


class FloatingText:
    __slots__ = ('x', 'y', 'vx', 'vy', 'life', 'max_life', 'text', 'color',
                 'size', 'bold', 'tally')

    def __init__(self, x, y, text, color, size, vx, vy, life, bold):
        # Running total, for damage numbers that merge instead of stacking.
        self.tally = 0
        self.x, self.y = x, y
        self.vx, self.vy = vx, vy
        self.text = text
        self.color = color
        self.size = size
        self.life = self.max_life = life
        self.bold = bold


class Effects:
    """Everything transient that is not a particle."""

    def __init__(self, rng):
        self.rng = rng
        self.texts = []
        self.lights = []       # [x, y, radius, life, max_life, color]
        self.beams = []        # [x0, y0, x1, y1, life, max_life, color]
        self.hitstop = 0.0
        self.flash = 0.0
        self.flash_color = palette.LIGHT_CORE
        self.flash_wash = False
        self.chroma = 0.0
        self.time_scale = 1.0
        self.shake_request = 0.0
        self._slowmo = 0.0
        self._slowmo_target = 1.0

    def clear(self):
        self.texts.clear()
        self.lights.clear()
        self.beams.clear()
        self.hitstop = 0.0
        self.flash = 0.0
        self.chroma = 0.0
        self.shake_request = 0.0
        self._slowmo = 0.0
        self.time_scale = 1.0

    # ------------------------------------------------------------ inputs --
    def add_text(self, x, y, text, color, size=18, bold=False, rise=64.0):
        self.texts.append(FloatingText(
            x + self.rng.uniform(-6, 6), y, text, color, size,
            self.rng.uniform(-26, 26), -rise, 0.85, bold))
        if len(self.texts) > 40:
            del self.texts[0]

    # How close, and how recently, a damage number has to be for the next one
    # to be added into it rather than drawn on top of it.
    MERGE_DIST = 30.0
    MERGE_AGE = 0.30

    def add_damage(self, x, y, amount, color, size=15, bold=False):
        """A damage number that gathers rather than piles up.

        A weapon that fires nine times a second puts nine numbers on the same
        enemy in the same second, at the same place, and they draw on top of
        each other - which is illegible exactly when the most is happening.
        A new number close to a recent one of the same colour is added into
        it, so a burst reads as one rising total.
        """
        amount = int(amount)
        if amount <= 0:
            return
        for t in reversed(self.texts):
            if (t.tally and t.color is color
                    and (t.max_life - t.life) < self.MERGE_AGE
                    and abs(t.x - x) < self.MERGE_DIST
                    and abs(t.y - y) < self.MERGE_DIST):
                t.tally += amount
                t.text = str(t.tally)
                # Nudged back up and refreshed, so a sustained burst keeps one
                # number climbing rather than leaving a stalled one behind.
                t.life = min(t.max_life, t.life + 0.12)
                t.size = max(t.size, size)
                t.bold = t.bold or bold
                return
        self.add_text(x, y, str(amount), color, size, bold)
        self.texts[-1].tally = amount

    def add_beam(self, x0, y0, x1, y1, color=palette.BEAM, life=0.14):
        """A short-lived line between two points - a chained arc.

        Emissive, so it is drawn with the lighting rather than under it, and
        it throws a little light of its own at each end: an arc that lights
        nothing reads as a decal rather than as electricity.
        """
        self.beams.append([x0, y0, x1, y1, life, life, color])
        if len(self.beams) > 40:
            del self.beams[0]

    def add_light(self, x, y, radius, life, color=palette.LIGHT_WARM):
        self.lights.append([x, y, radius, life, life, color])
        if len(self.lights) > 24:
            del self.lights[0]

    def add_hitstop(self, seconds):
        self.hitstop = min(HITSTOP_MAX, max(self.hitstop, seconds))

    def add_flash(self, strength, color=palette.LIGHT_CORE, wash=False):
        """Screen feedback. `wash` covers the frame (light bursts); the
        default is a rim pulse, which reads as impact without hiding the
        thing that just hit you."""
        self.flash = min(1.0, max(self.flash, strength))
        self.flash_color = color
        self.flash_wash = wash

    def add_shake(self, amount):
        """Queue camera shake; the game applies it to the live camera."""
        self.shake_request = max(self.shake_request, amount)

    def take_shake(self):
        amount = self.shake_request
        self.shake_request = 0.0
        return amount

    def add_chroma(self, strength):
        self.chroma = min(1.0, max(self.chroma, strength))

    def slowmo(self, seconds, scale=0.28):
        self._slowmo = max(self._slowmo, seconds)
        self._slowmo_target = scale

    # ------------------------------------------------------------ update --
    def update(self, dt):
        """Advance effects on *unscaled* time and report the gameplay scale."""
        if self.hitstop > 0.0:
            self.hitstop -= dt
            scale = 0.0
        elif self._slowmo > 0.0:
            self._slowmo -= dt
            scale = self._slowmo_target
        else:
            scale = 1.0
        self.time_scale = scale

        self.flash = max(0.0, self.flash - dt * 5.4)
        self.chroma = max(0.0, self.chroma - dt * 3.0)

        for t in self.texts:
            t.life -= dt
            t.x += t.vx * dt
            t.y += t.vy * dt
            t.vy += 150.0 * dt
        if self.texts:
            self.texts = [t for t in self.texts if t.life > 0.0]

        for beam in self.beams:
            beam[4] -= dt
        self.beams = [b for b in self.beams if b[4] > 0.0]

        for light in self.lights:
            light[3] -= dt
        if self.lights:
            self.lights = [l for l in self.lights if l[3] > 0.0]

        return scale

    # -------------------------------------------------------------- draw --
    def draw_lights(self, ox, oy, view_w, view_h):
        for x, y, radius, life, max_life, color in self.lights:
            t = life / max_life
            r = radius * (0.55 + 0.45 * ease_out_cubic(1.0 - t))
            sx, sy = x - ox, y - oy
            if sx < -r or sy < -r or sx > view_w + r or sy > view_h + r:
                continue
            art.draw_glow(color, sx, sy, r, clamp(72 * t, 0, 100), power=2.4)

    def draw_beams(self, ox, oy):
        """Arcs, over the lighting: they are their own light source."""
        for x0, y0, x1, y1, life, max_life, color in self.beams:
            t = clamp(life / max_life, 0.0, 1.0)
            drawLine(x0 - ox, y0 - oy, x1 - ox, y1 - oy, fill=color,
                     lineWidth=1.4 + 2.6 * t,
                     opacity=int(clamp(96 * t, 0, 100)))

    def draw_texts(self, ox, oy):
        for t in self.texts:
            frac = t.life / t.max_life
            opacity = int(clamp(frac * 130, 0, 100))
            if opacity <= 2:
                continue
            art.draw_label_sprite(t.text, t.x - ox, t.y - oy, 'ui', t.size,
                                  art.rgb_tuple(t.color), opacity=opacity)

    def draw_flash(self, view_w, view_h):
        if self.flash <= 0.01:
            return
        if self.flash_wash:
            drawPolygon(0, 0, view_w, 0, view_w, view_h, 0, view_h,
                        fill=self.flash_color,
                        opacity=int(clamp(self.flash * 46, 0, 100)))
            return
        sprite = art.vignette(view_w, view_h, 1.0, 0.26,
                              art.rgb_tuple(self.flash_color))
        drawImage(sprite, 0, 0, opacity=int(clamp(self.flash * 78, 0, 100)))



