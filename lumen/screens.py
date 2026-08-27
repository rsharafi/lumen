"""Front-end screens: title, help, upgrade draft, pause, and endings.

They share a look - a slow drift of embers behind a dark wash, one accent
colour, and type set in the same two faces as the HUD.
"""

import math

from .draw import drawImage, drawLabel, drawPolygon

from . import art, palette, upgrades
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
        self.menu = Menu(['DESCEND', 'HOW TO PLAY', 'DISPLAY', 'SOUND', 'QUIT'])
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

    def draw(self, sound_on, display_label=''):
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

        base = h * 0.56
        self.hit_rects = []
        for i, item in enumerate(self.menu.items):
            selected = i == self.menu.index
            y = base + i * 38
            self.hit_rects.append((w * 0.5 - 170, y - 14, 340, 32, i))
            label = item
            if item == 'SOUND':
                label = f'SOUND  {"ON" if sound_on else "OFF"}'
            elif item == 'DISPLAY':
                label = f'DISPLAY  {display_label}'
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
        self.index = 0
        self.t = 0.0
        self.hit_rects = []

    def open(self, choices, depth):
        self.choices = choices
        self.index = 0
        self.t = 0.0
        self.depth = depth

    def update(self, dt):
        self.t += dt

    def move(self, delta):
        if self.choices:
            self.index = (self.index + delta) % len(self.choices)

    def draw(self, world):
        w, h = self.w, self.h
        drawPolygon(0, 0, w, 0, w, h, 0, h, fill=palette.VOID, opacity=82)

        appear = ease_out_cubic(clamp(self.t / 0.45, 0.0, 1.0))
        drawLabel('THE VAULT OFFERS', w * 0.5, h * 0.17, size=30, bold=True,
                  fill=palette.UI_ACCENT, font=palette.FONT_DISPLAY,
                  opacity=int(100 * appear))
        drawLabel(f'floor {self.depth} cleared  -  choose one',
                  w * 0.5, h * 0.17 + 30, size=13, fill=palette.UI_DIM,
                  font=palette.FONT_UI, opacity=int(80 * appear))

        count = max(1, len(self.choices))
        cw, ch = 292, 300
        gap = 34
        total = count * cw + (count - 1) * gap
        x0 = (w - total) * 0.5
        y0 = h * 0.31

        self.hit_rects = []
        for i, up in enumerate(self.choices):
            selected = i == self.index
            delay = clamp((self.t - 0.08 * i) / 0.4, 0.0, 1.0)
            pop = ease_out_back(delay)
            lift = 16 if selected else 0
            cx = x0 + i * (cw + gap)
            cy = y0 - lift + (1.0 - pop) * 40
            self.hit_rects.append((cx, y0 - 20, cw, ch + 40, i))

            panel(cx, cy, cw, ch, 90 if selected else 66,
                  up.color if selected else palette.UI_LINE,
                  86 if selected else 42)
            if selected:
                corner_marks(cx, cy, cw, ch, up.color, 95, 16)
                size = 256
                drawImage(art.glow(_rgb(up.color), size, power=2.6),
                          cx + cw * 0.5 - size * 0.5, cy + 44 - size * 0.5,
                          opacity=20)

            _sigil(cx + cw * 0.5, cy + 62, up, self.t, selected)

            drawLabel(up.name, cx + cw * 0.5, cy + 132, size=19, bold=True,
                      fill=up.color if selected else palette.UI_TEXT,
                      font=palette.FONT_DISPLAY,
                      opacity=int(100 * delay))
            lines = wrap(up.blurb, 30)
            for j, line in enumerate(lines[:4]):
                drawLabel(line, cx + cw * 0.5, cy + 166 + j * 20, size=13,
                          fill=palette.UI_DIM, font=palette.FONT_UI,
                          opacity=int(88 * delay))
            drawLabel(f'[{i + 1}]', cx + cw * 0.5, cy + ch - 26, size=12,
                      fill=palette.UI_FAINT, font=palette.FONT_UI,
                      opacity=int(80 * delay))

        drawLabel('LEFT / RIGHT  CHOOSE      ENTER  TAKE IT      '
                  'OR CLICK A CARD', w * 0.5, y0 + ch + 44, size=13,
                  fill=palette.UI_DIM, font=palette.FONT_UI,
                  opacity=int(84 * appear))

        owned = world.stats.owned
        if owned:
            names = ', '.join(upgrades.BY_KEY[k].name for k in owned[-7:])
            drawLabel(f'CARRYING  {names}', w * 0.5, h - 40, size=11,
                      fill=palette.UI_FAINT, font=palette.FONT_UI, opacity=66)


def _sigil(cx, cy, up, t, selected):
    """A small procedural emblem so each card reads at a glance."""
    seed = sum(ord(c) for c in up.key)
    points = 5 + seed % 4
    spin = t * (0.5 if selected else 0.2) + seed
    r = 30 + (4 if selected else 0)

    # Outer rosette: 2*points vertices alternating between two radii, so the
    # star is actually symmetric (one vertex per point looks like a smudge).
    pts = []
    for i in range(points * 2):
        a = spin + i * math.pi / points
        rad = r * (1.0 if i % 2 == 0 else 0.52)
        pts.append(cx + math.cos(a) * rad)
        pts.append(cy + math.sin(a) * rad)
    drawPolygon(*pts, fill=up.color, opacity=90 if selected else 58)

    # Counter-rotating core, punched out in the panel colour.
    inner = []
    for i in range(points):
        a = -spin * 1.5 + i * math.tau / points
        inner.append(cx + math.cos(a) * r * 0.38)
        inner.append(cy + math.sin(a) * r * 0.38)
    drawPolygon(*inner, fill=palette.UI_PANEL, opacity=92)
    dot = r * 0.15
    drawPolygon(cx, cy - dot, cx + dot, cy, cx, cy + dot, cx - dot, cy,
                fill=up.color, opacity=95 if selected else 62)


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
