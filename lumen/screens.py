"""Front-end screens: title, help, upgrade draft, pause, and endings.

They share a look - a slow drift of embers behind a dark wash, one accent
colour, and type set in the same two faces as the HUD.
"""

import math

from .draw import drawImage, drawLabel, drawPolygon

from . import art, gpu, palette, upgrades
from .mathx import clamp, ease_out_back, ease_out_cubic, pulse


def text(message, x, y, role, size, color, align='left', opacity=100):
    """Baked text. The help pages set a lot of static type, and `drawLabel`
    costs a shape plus several font-face selections each."""
    art.draw_label_sprite(message, x, y, role, size, art.rgb_tuple(color),
                          align=align, opacity=opacity)


def hit_test(rects, mx, my):
    """Index of the rect containing (mx, my), or None."""
    for x, y, w, h, index in rects:
        if x <= mx <= x + w and y <= my <= y + h:
            return index
    return None


def wrap(text_value, width):
    """Greedy word wrap to `width` characters."""
    words = text_value.split()
    lines = []
    current = ''
    for word in words:
        candidate = word if not current else current + ' ' + word
        if len(candidate) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def panel(x, y, w, h, opacity=76, border=palette.UI_LINE, border_opacity=68):
    drawPolygon(x, y, x + w, y, x + w, y + h, x, y + h,
                fill=palette.UI_PANEL, opacity=opacity)
    t = 1.2
    drawPolygon(x, y, x + w, y, x + w, y + t, x, y + t, fill=border,
                opacity=border_opacity)
    drawPolygon(x, y + h - t, x + w, y + h - t, x + w, y + h, x, y + h,
                fill=border, opacity=border_opacity)
    drawPolygon(x, y, x + t, y, x + t, y + h, x, y + h, fill=border,
                opacity=border_opacity)
    drawPolygon(x + w - t, y, x + w, y, x + w, y + h, x + w - t, y + h,
                fill=border, opacity=border_opacity)


def corner_marks(x, y, w, h, color=palette.UI_ACCENT, opacity=80, size=13):
    for cx, cy, sx, sy in ((x, y, 1, 1), (x + w, y, -1, 1),
                           (x, y + h, 1, -1), (x + w, y + h, -1, -1)):
        drawPolygon(cx, cy, cx + sx * size, cy, cx + sx * size, cy + sy * 2,
                    cx, cy + sy * 2, fill=color, opacity=opacity)
        drawPolygon(cx, cy, cx, cy + sy * size, cx + sx * 2, cy + sy * size,
                    cx + sx * 2, cy, fill=color, opacity=opacity)


class Backdrop:
    """Shared animated background: drifting embers over a dark gradient."""

    def __init__(self, view_w, view_h, rng, count=54):
        self.w = view_w
        self.h = view_h
        self.rng = rng
        self.motes = []
        for _ in range(count):
            self.motes.append([
                rng.uniform(0, view_w), rng.uniform(0, view_h),
                rng.uniform(-11, 11), rng.uniform(-34, -9),
                rng.uniform(1.0, 3.0), rng.uniform(0, math.tau),
            ])
        self.vignette = art.vignette(view_w, view_h, 0.95, 0.45)
        self.grain = art.grain(view_w, view_h, 0.045)
        self.t = 0.0

    def resize(self, view_w, view_h):
        self.w = view_w
        self.h = view_h
        for m in self.motes:
            m[0] = min(m[0], view_w)
            m[1] = min(m[1], view_h)
        self.vignette = art.vignette(view_w, view_h, 0.95, 0.45)
        self.grain = art.grain(view_w, view_h, 0.045)

    def update(self, dt):
        self.t += dt
        for m in self.motes:
            m[0] += m[2] * dt
            m[1] += m[3] * dt
            m[5] += dt * 1.6
            if m[1] < -20:
                m[1] = self.h + 20
                m[0] = self.rng.uniform(0, self.w)
            if m[0] < -20:
                m[0] = self.w + 20
            elif m[0] > self.w + 20:
                m[0] = -20

    def draw(self):
        drawPolygon(0, 0, self.w, 0, self.w, self.h, 0, self.h,
                    fill=palette.FLOOR_DEEP, opacity=100)
        big = 512
        drawImage(art.glow((60, 84, 140), big, power=2.4),
                  self.w * 0.5 - big * 0.5, self.h * 0.42 - big * 0.5,
                  opacity=18)
        for m in self.motes:
            s = m[4] * (0.75 + 0.25 * math.sin(m[5]))
            drawPolygon(m[0], m[1] - s, m[0] + s, m[1], m[0], m[1] + s,
                        m[0] - s, m[1], fill=palette.LIGHT_WARM,
                        opacity=int(18 + 26 * (0.5 + 0.5 * math.sin(m[5] * 0.7))))
        drawImage(self.vignette, 0, 0)
        drawImage(self.grain, 0, 0)


class Menu:
    def __init__(self, items):
        self.items = items
        self.index = 0

    def move(self, delta):
        self.index = (self.index + delta) % len(self.items)

    @property
    def current(self):
        return self.items[self.index]


# --------------------------------------------------------------------------
class TitleScreen:
    def resize(self, view_w, view_h):
        self.w = view_w
        self.h = view_h
        self.backdrop.resize(view_w, view_h)
        # The logo is a baked sprite, so it belongs to the render scale that
        # built it. A scale change empties the sprite cache and releases every
        # sprite's pixels, which would leave this one an empty husk that
        # crashes the next frame that draws it.
        self._bake_logo()

    def __init__(self, view_w, view_h, rng, save_data):
        self.w = view_w
        self.h = view_h
        self.backdrop = Backdrop(view_w, view_h, rng, 64)
        self.save = save_data
        self.menu = Menu(['DESCEND', 'HOW TO PLAY', 'DISPLAY', 'VISUALS',
                          'SHAFTS', 'SOUND', 'QUIT'])
        self.t = 0.0
        # Rectangles from the last frame's layout, used for mouse hit-testing.
        self.hit_rects = []
        self._bake_logo()

    def _bake_logo(self):
        self.logo = art.text_sprite('LUMEN', 'display', 104, (255, 236, 208),
                                    tracking=18, glow_color=(255, 168, 64),
                                    glow_radius=18)
        self.logo_w, self.logo_h = self.logo_size()

    def logo_size(self):
        w, h = art.text_size('LUMEN', 'display', 104, tracking=18)
        return w + 44, h + 44

    def update(self, dt):
        self.t += dt
        self.backdrop.update(dt)

    def draw(self, sound_on, display_label='', visuals_label='',
             volumetric_label='', renderer_label=''):
        self.backdrop.draw()
        w, h = self.w, self.h

        rise = ease_out_cubic(clamp(self.t / 1.1, 0.0, 1.0))
        ly = h * 0.24 - 30 + (1.0 - rise) * 26
        drawImage(self.logo, w * 0.5 - self.logo_w * 0.5, ly,
                  opacity=int(100 * rise))

        drawLabel('DESCENT INTO THE VAULT', w * 0.5, ly + self.logo_h + 4,
                  size=15, fill=palette.UI_ACCENT, font=palette.FONT_DISPLAY,
                  bold=True, opacity=int(88 * rise))
        drawLabel('you carry the only light', w * 0.5, ly + self.logo_h + 26,
                  size=12, fill=palette.UI_DIM, font=palette.FONT_UI,
                  opacity=int(70 * rise))

        # Sigil behind the menu.
        cx, cy = w * 0.5, h * 0.66
        for ring in range(3):
            r = 118 + ring * 26
            pts = []
            for i in range(9):
                a = self.t * (0.12 + ring * 0.05) * (1 if ring % 2 == 0 else -1) \
                    + i * math.tau / 9
                pts.append(cx + math.cos(a) * r)
                pts.append(cy + math.sin(a) * r * 0.42)
            drawPolygon(*pts, fill=None, border=palette.UI_LINE,
                        borderWidth=1, opacity=30 - ring * 6)

        # Seven rows now, so they start higher and sit closer together than
        # the five this was laid out for.
        base = h * 0.50
        self.hit_rects = []
        for i, item in enumerate(self.menu.items):
            selected = i == self.menu.index
            y = base + i * 34
            self.hit_rects.append((w * 0.5 - 170, y - 13, 340, 30, i))
            label = item
            if item == 'SOUND':
                label = f'SOUND  {"ON" if sound_on else "OFF"}'
            elif item == 'DISPLAY':
                label = f'DISPLAY  {display_label}'
            elif item == 'VISUALS':
                label = f'VISUALS  {visuals_label}'
            elif item == 'SHAFTS':
                label = f'SHAFTS  {volumetric_label}'
            if selected:
                glide = 8 + 3 * pulse(self.t, 1.6)
                drawPolygon(w * 0.5 - 150 - glide, y - 2,
                            w * 0.5 - 138 - glide, y + 8,
                            w * 0.5 - 150 - glide, y + 18,
                            fill=palette.UI_ACCENT, opacity=90)
                drawPolygon(w * 0.5 + 150 + glide, y - 2,
                            w * 0.5 + 138 + glide, y + 8,
                            w * 0.5 + 150 + glide, y + 18,
                            fill=palette.UI_ACCENT, opacity=90)
            drawLabel(label, w * 0.5, y + 8, size=20 if selected else 17,
                      bold=selected, font=palette.FONT_DISPLAY,
                      fill=palette.UI_TEXT if selected else palette.UI_DIM,
                      opacity=100 if selected else 68)

        best = self.save.get('best_score', 0)
        floor = self.save.get('best_floor', 0)
        runs = self.save.get('runs', 0)
        drawLabel(f'BEST {best:,}   DEEPEST FLOOR {floor}   RUNS {runs}',
                  w * 0.5, h - 54, size=12, fill=palette.UI_DIM,
                  font=palette.FONT_UI, opacity=72)
        drawLabel('ARROWS / W S  MOVE     ENTER  SELECT     OR USE THE MOUSE',
                  w * 0.5, h - 32, size=11, fill=palette.UI_FAINT,
                  font=palette.FONT_UI, opacity=64)
        if renderer_label:
            # Which of the four host/renderer pairs is live. Informational -
            # both are picked at launch, not from this menu.
            drawLabel(renderer_label, w - 14, h - 14, size=10,
                      fill=palette.UI_DIM, font=palette.FONT_UI,
                      align='right', opacity=74)


# --------------------------------------------------------------------------
class HelpScreen:
    def resize(self, view_w, view_h):
        self.w = view_w
        self.h = view_h
        self.backdrop.resize(view_w, view_h)

    """Controls, bestiary, and arsenal.

    The bestiary draws each species with its own `draw_body`, so what you
    study here is exactly the silhouette you will have to recognise in the
    dark - not an approximation of it.
    """

    PAGES = 3
    TITLES = ('HOW TO PLAY', 'WHAT LIVES DOWN THERE', 'THE ARSENAL')

    def __init__(self, view_w, view_h, rng):
        self.w = view_w
        self.h = view_h
        self.rng = rng
        self.backdrop = Backdrop(view_w, view_h, rng, 34)
        self.page = 0
        self.t = 0.0
        self._models = None

    def models(self):
        """One live instance of each species, built once, posed for display."""
        if self._models is None:
            from . import enemies as em
            from .boss import HollowChoir
            order = (em.Crawler, em.Spitter, em.Husk, em.Wisp, em.Warden,
                     HollowChoir)
            self._models = []
            for cls in order:
                model = cls(0.0, 0.0, 1, self.rng)
                model.spawn_t = 0.0
                model.facing = 0.0
                model.hit_flash = 0.0
                self._models.append(model)
        return self._models

    def update(self, dt):
        self.t += dt
        self.backdrop.update(dt)
        for model in (self._models or ()):
            model.phase += dt

    def turn(self, delta):
        self.page = (self.page + delta) % self.PAGES

    def draw(self):
        self.backdrop.draw()
        w, h = self.w, self.h
        panel(70, 56, w - 140, h - 130, 82)
        corner_marks(70, 56, w - 140, h - 130)
        drawLabel(self.TITLES[self.page], w * 0.5, 88, size=27, bold=True,
                  fill=palette.UI_ACCENT, font=palette.FONT_DISPLAY)

        if self.page == 0:
            self._controls(w, h)
        elif self.page == 1:
            self._bestiary(w, h)
        else:
            self._arsenal(w, h)

        drawLabel(f'{self.page + 1} / {self.PAGES}    LEFT / RIGHT  TURN PAGE'
                  '    ESC  BACK', w * 0.5, h - 50, size=12,
                  fill=palette.UI_DIM, font=palette.FONT_UI, opacity=76)

    def _controls(self, w, h):
        rows = [
            ('W A S D  /  ARROWS', 'Move through the chamber.'),
            ('MOUSE', 'Aim. Everything you fire goes where you point.'),
            ('LEFT CLICK  /  J', 'Fire. Hold to charge the Coilbeam.'),
            ('SPACE  /  K', 'Dash. Brief invulnerability - use it through attacks.'),
            ('SHIFT  /  L', 'Lantern flare: a burst of light that damages and shoves.'),
            ('1  2  3  /  TAB', 'Switch weapon.'),
            ('ESC  /  P', 'Pause.'),
        ]
        y = 132
        for key, body in rows:
            text(key, 132, y, 'ui', 14, palette.UI_TEXT)
            text(body, 400, y, 'ui', 14, palette.UI_DIM)
            y += 29

        y += 16
        text('THE LANTERN', 132, y, 'display', 16, palette.LIGHT_WARM)
        y += 25
        for line in [
            'Your lantern burns fuel constantly. As it empties its reach collapses and',
            'the dark closes in - it never goes out entirely, but you will be fighting',
            'blind. Oil refills it. Braziers you light stay lit, and standing in one',
            'refuels you, so they are worth holding ground for.',
        ]:
            text(line, 132, y, 'ui', 13, palette.UI_DIM)
            y += 20

        y += 18
        text('CLEARING A FLOOR', 132, y, 'display', 16, palette.PLAYER_TRIM)
        y += 25
        for line in [
            'Kill everything and a rift opens. Stand in it for a moment to descend,',
            'then take one of three offerings. Twelve floors down, and two of them',
            'belong to something that has been waiting.',
        ]:
            text(line, 132, y, 'ui', 13, palette.UI_DIM)
            y += 20

    def _bestiary(self, w, h):
        rows = [
            ('CRAWLER', 'Fast, fragile, lunges in bursts. Never stand still.'),
            ('SPITTER', 'Holds its distance and fires three-round bursts. Break line of sight.'),
            ('HUSK', 'Slow and very heavy. Kite it; do not let it corner you.'),
            ('WISP', 'Flies over walls and drains your lantern fuel on contact.'),
            ('WARDEN', 'Its shield blocks everything from the front. Flank it, or flare it.'),
            ('THE HOLLOW CHOIR', 'Floors 6 and 12. Every attack is telegraphed - learn the tells.'),
        ]
        models = self.models()
        y = 146
        for i, (name, body) in enumerate(rows):
            model = models[i]
            model.facing = math.sin(self.t * 0.7 + i) * 0.6
            scale_down = i == len(rows) - 1
            if scale_down:
                # The boss is drawn at a fraction of its real size so it fits.
                saved = model.radius
                model.radius = 22.0
                model.draw_body(178, y + 4)
                model.radius = saved
            else:
                model.draw_body(178, y + 4)
            text(name, 232, y - 6, 'display', 15, palette.UI_TEXT)
            text(body, 232, y + 13, 'ui', 13, palette.UI_DIM)
            y += 74

    def _arsenal(self, w, h):
        from .projectiles import WEAPONS
        y = 142
        for i, weapon in enumerate(WEAPONS):
            text(f'[{i + 1}]  {weapon.name}', 132, y, 'display', 17,
                 weapon.color)
            text(weapon.blurb, 132, y + 22, 'ui', 13, palette.UI_DIM)
            stats = (f'damage {weapon.damage:.1f}   '
                     f'rate {1.0 / weapon.cooldown:.1f}/s   '
                     f'speed {weapon.speed:.0f}')
            if weapon.pellets > 1:
                stats += f'   pellets {weapon.pellets}'
            if weapon.charge_time:
                stats += f'   charge x{weapon.charge_scale:.1f}'
            text(stats, 132, y + 42, 'ui', 12, palette.UI_FAINT)
            y += 84

        y += 6
        text('OFFERINGS', 132, y, 'display', 16, palette.UI_ACCENT)
        y += 25
        for line in [
            'Every cleared floor offers three upgrades and you take exactly one.',
            'They stack for the whole run and reset when you die - there is no',
            'meta-progression here, only what you build on the way down.',
        ]:
            text(line, 132, y, 'ui', 13, palette.UI_DIM)
            y += 20


# --------------------------------------------------------------------------
class UpgradeScreen:
    def resize(self, view_w, view_h):
        self.w = view_w
        self.h = view_h

    def __init__(self, view_w, view_h):
        self.w = view_w
        self.h = view_h
        self.choices = []
        self.rerolls = 0
        self.index = 0
        self.t = 0.0
        self.hit_rects = []

    def open(self, choices, depth, rerolls=0):
        self.choices = choices
        self.rerolls = rerolls
        self.index = 0
        self.t = 0.0
        self.depth = depth

    def update(self, dt):
        self.t += dt

    def move(self, delta):
        if self.choices:
            self.index = (self.index + delta) % len(self.choices)

    # An offering is a lit alcove in the dark rather than a card on a page:
    # the same thing the rest of the game does with light, applied to the one
    # screen where the player stops and reads.
    CARD_W = 286.0
    CARD_H = 360.0
    CARD_GAP = 40.0
    RARITY = {3: ('COMMON', 1), 2: ('UNCOMMON', 2), 1: ('RARE', 3)}

    def draw(self, world):
        w, h = self.w, self.h
        appear = ease_out_cubic(clamp(self.t / 0.45, 0.0, 1.0))

        # The chamber is still behind this, so it is dimmed rather than
        # replaced - you are standing in the room you just cleared.
        drawPolygon(0, 0, w, 0, w, h, 0, h, fill=palette.VOID,
                    opacity=int(88 * appear))

        self._draw_header(w, h, appear)

        count = max(1, len(self.choices))
        cw, ch, gap = self.CARD_W, self.CARD_H, self.CARD_GAP
        total = count * cw + (count - 1) * gap
        # The design view is only 720 units tall but its width follows the
        # window's aspect, so a tall window leaves less room across than the
        # cards want. Shrink them to fit rather than running off the edges.
        room = w * 0.92
        if total > room:
            k = room / total
            cw, gap, ch = cw * k, gap * k, ch * k
            total = room
        x0 = (w - total) * 0.5
        y0 = h * 0.245

        self.hit_rects = []
        for i, up in enumerate(self.choices):
            selected = i == self.index
            delay = clamp((self.t - 0.09 * i) / 0.42, 0.0, 1.0)
            pop = ease_out_back(delay)
            lift = 18.0 if selected else 0.0
            cx = x0 + i * (cw + gap)
            cy = y0 - lift + (1.0 - pop) * 46.0
            self.hit_rects.append((cx, y0 - 24, cw, ch + 48, i))
            self._draw_card(up, cx, cy, cw, ch, i, selected, delay,
                            cw / self.CARD_W)

        drawLabel('LEFT / RIGHT  CHOOSE      ENTER  TAKE IT      '
                  'OR CLICK AN OFFERING', w * 0.5, y0 + ch + 42, size=12,
                  fill=palette.UI_DIM, font=palette.FONT_UI,
                  opacity=int(76 * appear))
        self._draw_carrying(world, w, h, appear)

    def _draw_header(self, w, h, appear):
        cx, cy = w * 0.5, h * 0.115
        size = 300
        _glow(cx, cy + 4, size, palette.LIGHT_WARM, int(13 * appear), 2.7)
        drawLabel('THE VAULT OFFERS', cx, cy, size=30, bold=True,
                  fill=palette.UI_ACCENT, font=palette.FONT_DISPLAY,
                  opacity=int(100 * appear))
        # A rule that draws itself outward from the centre as the screen
        # settles, so the eye starts in the middle and is handed downward.
        half = 168.0 * appear
        _rule(cx - half, cx + half, cy + 22, palette.UI_ACCENT, 34)
        drawLabel(f'FLOOR {self.depth} CLEARED   -   CHOOSE ONE',
                  cx, cy + 38, size=11, fill=palette.UI_FAINT,
                  font=palette.FONT_UI, opacity=int(74 * appear))

    def _draw_card(self, up, x, y, cw, ch, index, selected, delay, k=1.0):
        """One offering. `k` shrinks the whole card, contents included.

        Everything inside is expressed against the card's full size and then
        multiplied, because scaling the frame alone leaves the type where it
        was and the bottom-anchored rows climb into the blurb.
        """
        colour = up.color
        alpha = int(100 * delay)
        if alpha <= 0:
            return
        mid = x + cw * 0.5

        # The pool the offering throws on the floor beneath it. Only the
        # chosen one is lit; the others are waiting in the dark.
        if selected:
            _glow(mid, y + ch * 0.60, int(cw * 2.0), colour,
                  int(22 * delay), 2.4)

        panel(x, y, cw, ch, (86 if selected else 58),
              colour if selected else palette.UI_LINE,
              (88 if selected else 34))
        if selected:
            corner_marks(x, y, cw, ch, colour, 92, 17 * k)

        # ---- the alcove -------------------------------------------------
        ay = y + 94.0 * k
        _glow(mid, ay, int(232 * k), colour,
              int((34 if selected else 12) * delay), 2.6)
        _sigil(mid, ay, up, self.t, selected,
               radius=(30.0 + (4.0 if selected else 0.0)) * k)

        # ---- name, rule, blurb ------------------------------------------
        drawLabel(up.name, mid, y + 190 * k, size=18 * k, bold=True,
                  fill=colour if selected else palette.UI_TEXT,
                  font=palette.FONT_DISPLAY, opacity=alpha)
        _rule(x + 52 * k, x + cw - 52 * k, y + 212 * k, colour,
              int((60 if selected else 26) * delay))

        for j, line in enumerate(wrap(up.blurb, 28)[:4]):
            drawLabel(line, mid, y + (238 + j * 20) * k, size=12 * k,
                      fill=palette.UI_TEXT if selected else palette.UI_DIM,
                      font=palette.FONT_UI,
                      opacity=int((88 if selected else 66) * delay))

        # ---- how often the vault offers this ----------------------------
        label, pips = self.RARITY.get(up.rarity, ('COMMON', 1))
        py = y + ch - 62 * k
        drawLabel(label, mid, py - 16 * k, size=9 * k,
                  fill=colour if selected else palette.UI_FAINT,
                  font=palette.FONT_UI,
                  opacity=int((72 if selected else 46) * delay))
        # Centred on however many there are, not on a fixed row of three: a
        # left-aligned pair under a centred label reads as a mistake.
        for i in range(pips):
            px = mid + (i - (pips - 1) * 0.5) * 13 * k
            r = 3.4 * k
            drawPolygon(px, py - r, px + r, py, px, py + r, px - r, py,
                        fill=colour, opacity=int(90 * delay))

        drawLabel(f'[{index + 1}]', mid, y + ch - 26 * k, size=11 * k,
                  fill=colour if selected else palette.UI_FAINT,
                  font=palette.FONT_UI, opacity=int(72 * delay))

    def _draw_carrying(self, world, w, h, appear):
        """What the run has already taken, as its own row of marks.

        A comma-separated list of names is unreadable at a glance and says
        nothing about what they were; the sigils are what the player saw when
        they chose each one.
        """
        owned = world.stats.owned
        if not owned:
            return
        recent = owned[-9:]
        y = h - 40
        drawLabel('CARRYING', w * 0.5, y - 24, size=9,
                  fill=palette.UI_FAINT, font=palette.FONT_UI,
                  opacity=int(56 * appear))
        for i, key in enumerate(recent):
            up = upgrades.BY_KEY.get(key)
            if up is None:
                continue
            cx = w * 0.5 - (len(recent) - 1) * 15 + i * 30
            _sigil(cx, y, up, self.t * 0.25, False, radius=9.0)


def _rule(x1, x2, y, color, opacity):
    """A hairline that fades out at both ends rather than stopping."""
    if x2 - x1 < 2 or opacity <= 0:
        return
    steps = 7
    span = (x2 - x1) / steps
    for i in range(steps):
        f = 1.0 - abs((i + 0.5) / steps - 0.5) * 2.0
        a = int(opacity * (0.25 + 0.75 * f))
        if a <= 0:
            continue
        sx = x1 + i * span
        drawPolygon(sx, y, sx + span, y, sx + span, y + 1.1, sx, y + 1.1,
                    fill=color, opacity=a)


def _glow(cx, cy, size, color, opacity, power=2.4):
    """A soft pool of an upgrade's own colour, added rather than laid over."""
    if opacity <= 0:
        return
    sprite = art.glow(_rgb(color), int(size), power=power)
    additive = gpu.active()
    if additive:
        gpu.set_mode(gpu.ADD)
    drawImage(sprite, cx - size * 0.5, cy - size * 0.5, opacity=opacity)
    if additive:
        gpu.set_mode(gpu.NORMAL)


def _sigil(cx, cy, up, t, selected, radius=None):
    """A small procedural emblem so each offering reads at a glance.

    Four different constructions rather than one, chosen by the upgrade's key.
    A single family with a varying point count made every sigil a star of some
    number of points, which at a glance is no distinction at all - three cards
    side by side looked like the same mark drawn three times.
    """
    seed = sum(ord(c) for c in up.key)
    family = seed % 4
    spin = t * (0.5 if selected else 0.2) + seed
    r = radius if radius is not None else (30.0 + (4.0 if selected else 0.0))
    bright = 92 if selected else 58
    faint = 62 if selected else 40

    if family == 0:
        _sigil_star(cx, cy, r, spin, seed, up.color, bright)
    elif family == 1:
        _sigil_rings(cx, cy, r, spin, seed, up.color, bright, faint)
    elif family == 2:
        _sigil_blades(cx, cy, r, spin, seed, up.color, bright, faint)
    else:
        _sigil_core(cx, cy, r, spin, seed, up.color, bright, faint)

    # Every family closes on the same lit centre, which is what makes them
    # read as one set rather than four unrelated marks.
    dot = r * 0.15
    drawPolygon(cx, cy - dot, cx + dot, cy, cx, cy + dot, cx - dot, cy,
                fill=up.color, opacity=95 if selected else 62)


def _sigil_star(cx, cy, r, spin, seed, color, bright):
    """A rosette: alternating radii, so the points are actually symmetric."""
    points = 5 + seed % 4
    pts = []
    for i in range(points * 2):
        a = spin + i * math.pi / points
        rad = r * (1.0 if i % 2 == 0 else 0.52)
        pts.append(cx + math.cos(a) * rad)
        pts.append(cy + math.sin(a) * rad)
    drawPolygon(*pts, fill=color, opacity=bright)
    inner = []
    for i in range(points):
        a = -spin * 1.5 + i * math.tau / points
        inner.append(cx + math.cos(a) * r * 0.38)
        inner.append(cy + math.sin(a) * r * 0.38)
    drawPolygon(*inner, fill=palette.UI_PANEL, opacity=92)


def _sigil_rings(cx, cy, r, spin, seed, color, bright, faint):
    """Broken concentric arcs, counter-rotating."""
    for ring in range(3):
        rr = r * (1.0 - ring * 0.27)
        arcs = 3 + (seed + ring) % 3
        direction = 1 if ring % 2 == 0 else -1
        for k in range(arcs):
            a0 = spin * direction * (1.0 + ring * 0.5) + k * math.tau / arcs
            a1 = a0 + math.tau / arcs * 0.56
            _arc(cx, cy, rr, rr - r * 0.13, a0, a1, color,
                 bright if ring == 0 else faint)


def _sigil_blades(cx, cy, r, spin, seed, color, bright, faint):
    """Crossed tapers, like something struck rather than drawn."""
    blades = 3 + seed % 3
    for k in range(blades):
        a = spin + k * math.tau / blades
        ca, sa = math.cos(a), math.sin(a)
        px, py = -sa, ca
        drawPolygon(cx + ca * r, cy + sa * r,
                    cx + px * r * 0.17, cy + py * r * 0.17,
                    cx - ca * r * 0.34, cy - sa * r * 0.34,
                    cx - px * r * 0.17, cy - py * r * 0.17,
                    fill=color, opacity=bright)
    _arc(cx, cy, r * 0.52, r * 0.44, spin * -1.4, spin * -1.4 + math.tau,
         color, faint, segments=18)


def _sigil_core(cx, cy, r, spin, seed, color, bright, faint):
    """A faceted core with spokes reaching out of it."""
    sides = 6
    pts = []
    for i in range(sides):
        a = spin * 0.6 + i * math.tau / sides
        pts.append(cx + math.cos(a) * r * 0.46)
        pts.append(cy + math.sin(a) * r * 0.46)
    drawPolygon(*pts, fill=color, opacity=bright)
    inner = []
    for i in range(sides):
        a = spin * 0.6 + i * math.tau / sides
        inner.append(cx + math.cos(a) * r * 0.26)
        inner.append(cy + math.sin(a) * r * 0.26)
    drawPolygon(*inner, fill=palette.UI_PANEL, opacity=90)
    spokes = 4 + seed % 3
    for k in range(spokes):
        a = -spin + k * math.tau / spokes
        ca, sa = math.cos(a), math.sin(a)
        px, py = -sa * r * 0.07, ca * r * 0.07
        drawPolygon(cx + ca * r * 0.62 + px, cy + sa * r * 0.62 + py,
                    cx + ca * r + px * 0.3, cy + sa * r + py * 0.3,
                    cx + ca * r - px * 0.3, cy + sa * r - py * 0.3,
                    cx + ca * r * 0.62 - px, cy + sa * r * 0.62 - py,
                    fill=color, opacity=faint + 18)


def _arc(cx, cy, outer, inner, a0, a1, color, opacity, segments=7):
    """A band of an annulus, as a strip of quads."""
    if opacity <= 0:
        return
    span = a1 - a0
    for i in range(segments):
        b0 = a0 + span * i / segments
        b1 = a0 + span * (i + 1) / segments
        c0, s0 = math.cos(b0), math.sin(b0)
        c1, s1 = math.cos(b1), math.sin(b1)
        drawPolygon(cx + c0 * outer, cy + s0 * outer,
                    cx + c1 * outer, cy + s1 * outer,
                    cx + c1 * inner, cy + s1 * inner,
                    cx + c0 * inner, cy + s0 * inner,
                    fill=color, opacity=opacity)


def _rgb(color):
    return art.rgb_tuple(color)


# --------------------------------------------------------------------------
class EndScreen:
    def resize(self, view_w, view_h):
        self.w = view_w
        self.h = view_h
        self.backdrop.resize(view_w, view_h)

    def __init__(self, view_w, view_h, rng):
        self.w = view_w
        self.h = view_h
        self.backdrop = Backdrop(view_w, view_h, rng, 44)
        self.t = 0.0
        self.won = False
        self.summary = {}
        self.record = False

    def open(self, world, won, save_data, record):
        self.t = 0.0
        self.won = won
        self.record = record
        self.summary = {
            'score': world.score,
            'floor': world.depth,
            'kills': world.kills,
            'embers': world.embers,
            'time': world.run_time,
            'streak': world.best_streak,
            'damage': world.player.damage_dealt,
            'upgrades': list(world.stats.owned),
            'best': save_data.get('best_score', 0),
        }

    def update(self, dt):
        self.t += dt
        self.backdrop.update(dt)

    def draw(self):
        self.backdrop.draw()
        w, h = self.w, self.h
        appear = ease_out_cubic(clamp(self.t / 0.8, 0.0, 1.0))

        title = 'THE VAULT IS EMPTY' if self.won else 'THE LIGHT GOES OUT'
        color = palette.UI_GOOD if self.won else palette.UI_DANGER
        drawLabel(title, w * 0.5, h * 0.17, size=40, bold=True, fill=color,
                  font=palette.FONT_DISPLAY, opacity=int(100 * appear))
        sub = ('you carried it all the way down'
               if self.won else 'the dark takes what it is owed')
        drawLabel(sub, w * 0.5, h * 0.17 + 34, size=13, fill=palette.UI_DIM,
                  font=palette.FONT_UI, opacity=int(78 * appear))

        s = self.summary
        minutes = int(s['time'] // 60)
        seconds = int(s['time'] % 60)
        rows = [
            ('SCORE', f"{s['score']:,}"),
            ('DEEPEST FLOOR', str(s['floor'])),
            ('SLAIN', str(s['kills'])),
            ('EMBERS', str(s['embers'])),
            ('LONGEST CHAIN', f"x{s['streak']}"),
            ('DAMAGE DEALT', f"{int(s['damage']):,}"),
            ('TIME', f'{minutes}:{seconds:02d}'),
        ]
        pw, ph = 430, 42 + len(rows) * 30
        px = w * 0.5 - pw * 0.5
        py = h * 0.30
        panel(px, py, pw, ph, 80)
        corner_marks(px, py, pw, ph, color, 78)

        for i, (label, value) in enumerate(rows):
            delay = clamp((self.t - 0.5 - i * 0.07) / 0.3, 0.0, 1.0)
            y = py + 30 + i * 30
            drawLabel(label, px + 26, y, size=13, fill=palette.UI_DIM,
                      align='left', font=palette.FONT_UI,
                      opacity=int(86 * delay))
            drawLabel(value, px + pw - 26, y, size=15, fill=palette.UI_TEXT,
                      align='right', font=palette.FONT_UI, bold=True,
                      opacity=int(100 * delay))

        if self.record:
            p = pulse(self.t, 0.9)
            drawLabel('NEW BEST', w * 0.5, py + ph + 26, size=18, bold=True,
                      fill=palette.CRIT, font=palette.FONT_DISPLAY,
                      opacity=int(60 + 40 * p))
        else:
            drawLabel(f"BEST  {s['best']:,}", w * 0.5, py + ph + 26, size=13,
                      fill=palette.UI_FAINT, font=palette.FONT_UI, opacity=76)

        if s['upgrades']:
            names = ', '.join(upgrades.BY_KEY[k].name for k in s['upgrades'])
            for i, line in enumerate(wrap(names, 74)[:3]):
                drawLabel(line, w * 0.5, py + ph + 56 + i * 19, size=11,
                          fill=palette.UI_FAINT, font=palette.FONT_UI,
                          opacity=64)

        late = clamp((self.t - 1.1) / 0.5, 0.0, 1.0)
        drawLabel('ENTER  DESCEND AGAIN        ESC  RETURN TO TITLE',
                  w * 0.5, h - 52, size=14, fill=palette.UI_TEXT,
                  font=palette.FONT_UI, opacity=int(90 * late))


# --------------------------------------------------------------------------
def draw_pause(view_w, view_h, t, sound_on):
    drawPolygon(0, 0, view_w, 0, view_w, view_h, 0, view_h,
                fill=palette.VOID, opacity=72)
    panel(view_w * 0.5 - 190, view_h * 0.5 - 128, 380, 256, 88)
    corner_marks(view_w * 0.5 - 190, view_h * 0.5 - 128, 380, 256)
    drawLabel('PAUSED', view_w * 0.5, view_h * 0.5 - 88, size=32, bold=True,
              fill=palette.UI_ACCENT, font=palette.FONT_DISPLAY)
    rows = [
        ('ESC  /  P', 'RESUME'),
        ('M', 'SOUND  ' + ('ON' if sound_on else 'OFF')),
        ('H', 'HOW TO PLAY'),
        ('Q', 'ABANDON RUN'),
    ]
    for i, (key, label) in enumerate(rows):
        y = view_h * 0.5 - 30 + i * 30
        drawLabel(key, view_w * 0.5 - 18, y, size=14, align='right',
                  fill=palette.UI_TEXT, font=palette.FONT_UI, bold=True)
        drawLabel(label, view_w * 0.5 + 18, y, size=14, align='left',
                  fill=palette.UI_DIM, font=palette.FONT_UI)
    drawLabel('the lantern still burns while you wait', view_w * 0.5,
              view_h * 0.5 + 96, size=11, fill=palette.UI_FAINT,
              font=palette.FONT_UI, opacity=int(50 + 30 * pulse(t, 2.4)))
