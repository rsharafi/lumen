"""The heads-up display.

Deliberately quiet: thin rules, one accent colour, and nothing that moves
unless it is telling you something changed.
"""

import math

from .draw import drawImage, drawLabel, drawPolygon

from . import art, palette
from .config import LANTERN_FLARE_COOLDOWN
from .mathx import clamp, ease_out_cubic, pulse

PAD = 22

# How far down the top row has to start to clear the display's notch, in
# design units. Set from `Game._adopt_size` alongside the drawing scale,
# because it depends on both the panel and how the window is filling it; zero
# in a window, and zero on any screen without one. Only the three things that
# hang off the top of the screen use it - the world underneath is supposed to
# run right to the edge, notch and all.
_safe_top = 0.0


def set_safe_top(units):
    global _safe_top
    _safe_top = max(0.0, float(units))

def draw_text(message, x, y, role, size, color, align='left', opacity=100):
    """Blit baked HUD text; shares the cache in `art`."""
    art.draw_label_sprite(message, x, y, role, size, color, align=align,
                          opacity=opacity)


def _bar(x, y, w, h, frac, color, back=palette.UI_PANEL, back_opacity=70,
         opacity=94):
    drawPolygon(x, y, x + w, y, x + w, y + h, x, y + h,
                fill=back, opacity=back_opacity)
    fw = max(0.0, w * clamp(frac, 0.0, 1.0))
    if fw > 0.5:
        drawPolygon(x, y, x + fw, y, x + fw, y + h, x, y + h,
                    fill=color, opacity=opacity)


def _frame(x, y, w, h, color=palette.UI_LINE, opacity=60, thickness=1.0):
    t = thickness
    drawPolygon(x, y, x + w, y, x + w, y + t, x, y + t, fill=color, opacity=opacity)
    drawPolygon(x, y + h - t, x + w, y + h - t, x + w, y + h, x, y + h,
                fill=color, opacity=opacity)
    drawPolygon(x, y, x + t, y, x + t, y + h, x, y + h, fill=color, opacity=opacity)
    drawPolygon(x + w - t, y, x + w, y, x + w, y + h, x + w - t, y + h,
                fill=color, opacity=opacity)


def draw(app, world, quiet=False):
    """Draw the HUD. `quiet` drops the transient callouts - banners, the
    chain counter, the rift prompt - so an overlay on top stays readable."""
    w, h = world.view_w, world.view_h
    player = world.player
    stats = world.stats

    _draw_vitals(player, stats, h)
    _draw_weapon(player, w, h)
    _draw_run_info(world, w)
    # The floor map replaced the chamber map. A room is close to a screenful
    # now, so a plan of the one you are standing in tells you what you can
    # already see; what you cannot see is the floor, and that is what the
    # corner is worth spending on.
    _draw_floor_map(world, w)
    if world.boss_ref is not None and world.boss_ref.alive:
        _draw_boss_bar(world, w)
    if quiet:
        return
    _draw_banner(world, w, h)
    if world.streak >= 3:
        _draw_streak(world, w, h)
    if world.cleared and world.rift is not None:
        _draw_guidance(world, w, h)


def _draw_vitals(player, stats, view_h):
    x = PAD
    y = view_h - PAD - 54

    frac = clamp(player.hp / max(stats.max_hp, 1e-6), 0.0, 1.0)
    color = palette.HP_RAMP[int(frac * (len(palette.HP_RAMP) - 1))]
    _bar(x, y, 262, 15, frac, color)
    _frame(x - 1, y - 1, 264, 17, palette.UI_LINE, 70)

    # Tick marks every 25 health so the bar stays readable as max HP grows.
    ticks = max(1, int(stats.max_hp // 25))
    for i in range(1, ticks):
        tx = x + 262 * (i / ticks)
        drawPolygon(tx, y, tx + 1, y, tx + 1, y + 15, tx, y + 15,
                    fill=palette.VOID, opacity=52)

    drawLabel(f'{int(player.hp)}', x + 268, y + 8, size=15, fill=palette.UI_TEXT,
              align='left', font=palette.FONT_UI, bold=True)

    for i in range(player.shield):
        sx = x + 262 + 44 + i * 15
        drawPolygon(sx, y + 1, sx + 9, y + 7, sx, y + 14, sx - 9, y + 7,
                    fill=palette.SHIELD, opacity=88)

    # Lantern fuel.
    fy = y + 24
    fuel = clamp(player.fuel / max(player.fuel_max, 1e-6), 0.0, 1.0)
    low = fuel < 0.22
    fuel_color = palette.LIGHT_WARM if not low else palette.UI_DANGER
    op = 94 if not low else int(60 + 40 * pulse(player.walk_phase * 0.2 + fuel * 30, 1.0))
    _bar(x, fy, 262, 9, fuel, fuel_color, opacity=op)
    _frame(x - 1, fy - 1, 264, 11, palette.UI_LINE, 55)
    draw_text('LANTERN', x, fy + 20, 'ui', 10, (126, 140, 168), align='left')

    # Flare readiness.
    ready = player.flare_cd <= 0.0 and player.fuel >= 26.0
    fx = x + 262 + 44
    frac_cd = 1.0 - clamp(player.flare_cd / max(LANTERN_FLARE_COOLDOWN, 1e-6), 0.0, 1.0)
    size = 30
    drawImage(art.glow((255, 244, 214), size, power=2.0),
              fx - size * 0.5 + 8, fy - size * 0.5 + 4,
              opacity=int(52 if ready else 12))
    drawPolygon(fx + 8, fy - 6, fx + 15, fy + 4, fx + 8, fy + 14, fx + 1, fy + 4,
                fill=palette.FLARE if ready else palette.UI_FAINT,
                opacity=95 if ready else 55)
    if not ready:
        _bar(fx - 2, fy + 17, 20, 3, frac_cd, palette.UI_DIM, back_opacity=40)


def _draw_weapon(player, view_w, view_h):
    weapon = player.weapon
    x = view_w - PAD
    y = view_h - PAD - 52

    draw_text(weapon.name, x, y, 'display', 17, (223, 231, 245), align='right')
    # Only advertise the keys that do something. Every weapon used to be in
    # hand from the first run, so "1-3 to switch" was always true; now they
    # are unsealed at the Vigil, and a prompt for keys that do nothing reads
    # as a broken control rather than as something not yet earned.
    carried = getattr(player.stats, 'weapons', None) or ['lance']
    if len(carried) > 1:
        hint = f'[{player.weapon_index + 1}]  1-{len(carried)} to switch'
    else:
        hint = '[1]  more at THE VIGIL'
    draw_text(hint, x, y + 20, 'ui', 11,
              (126, 140, 168), align='right')

    if weapon.charge_time > 0.0:
        frac = clamp(player.charge / weapon.charge_time, 0.0, 1.0)
        _bar(x - 150, y + 32, 150, 6, frac,
             palette.BEAM if frac < 0.999 else palette.LIGHT_CORE)
        _frame(x - 151, y + 31, 152, 8, palette.UI_LINE, 55)

    # Dash charges.
    for i in range(player.stats.dash_charges):
        dx = x - 14 - i * 20
        filled = i < player.dash_charges
        drawPolygon(dx, y + 46, dx + 11, y + 52, dx, y + 58, dx - 11, y + 52,
                    fill=palette.DASH_TRAIL if filled else palette.UI_FAINT,
                    opacity=92 if filled else 45)
    draw_text('DASH', x - 14 - player.stats.dash_charges * 20 - 6, y + 52,
              'ui', 10, (126, 140, 168), align='right')


def _draw_run_info(world, view_w):
    x = PAD
    y = PAD + _safe_top
    label = (world.boss_name if world.is_boss
             else f'FLOOR {world.depth:02d}')
    draw_text(label, x, y + 10, 'display', 19, (255, 178, 84), align='left')
    drawLabel(f'{world.score:,}', x, y + 26, size=15, fill=palette.UI_TEXT,
              align='left-top', font=palette.FONT_UI)
    drawLabel(f'EMBERS {world.embers}   KILLS {world.kills}', x, y + 46,
              size=11, fill=palette.UI_DIM, align='left-top',
              font=palette.FONT_UI)
    _draw_relics(world, x, y + 66)


def _draw_relics(world, x, y):
    """What the run is carrying, as marks rather than a list.

    A relic is an object, and the difference between it and an offering is
    that it keeps its identity - so it gets a permanent row on screen. Names
    would be a wall of text after five of them; a coloured mark each is
    readable at a glance and says how many and roughly what.
    """
    held = getattr(world.stats, 'relics', None)
    if not held:
        return
    from . import relics as relic_mod
    for i, key in enumerate(held[:10]):
        relic = relic_mod.BY_KEY.get(key)
        if relic is None:
            continue
        cx = x + 6 + i * 15
        cy = y + 6
        r = 4.6
        drawPolygon(cx, cy - r, cx + r * 0.8, cy, cx, cy + r, cx - r * 0.8, cy,
                    fill=relic.color, opacity=88)
        # A ring around the ones that are not merely common, so a legendary
        # in the row is visible as one without reading anything.
        if relic.tier != relic_mod.COMMON:
            rr = r + 2.4
            drawPolygon(cx, cy - rr, cx + rr * 0.8, cy, cx, cy + rr,
                        cx - rr * 0.8, cy, fill=None, border=relic.color,
                        borderWidth=1.2, opacity=64)


def _draw_streak(world, view_w, view_h):
    t = clamp(world.streak_timer / 3.0, 0.0, 1.0)
    x = view_w * 0.5
    y = 96
    scale = 1.0 + 0.12 * ease_out_cubic(1.0 - t)
    drawLabel(f'x{world.streak}', x, y, size=int(26 * scale), bold=True,
              fill=palette.CRIT, font=palette.FONT_DISPLAY,
              opacity=int(40 + 60 * t))
    drawLabel('CHAIN', x, y + 20, size=10, fill=palette.UI_DIM,
              font=palette.FONT_UI, opacity=int(30 + 45 * t))


def _draw_boss_bar(world, view_w):
    b = world.boss_ref
    frac = clamp(b.hp / max(b.max_hp, 1e-6), 0.0, 1.0)
    w = view_w * 0.52
    x = (view_w - w) * 0.5
    y = 30 + _safe_top
    _bar(x, y, w, 13, frac, palette.BOSS_EYE, back_opacity=78)
    _frame(x - 1, y - 1, w + 2, 15, palette.UI_LINE, 76)
    # Where the fight actually changes, from the boss's own thresholds -
    # they are not thirds any more, and they differ between the two.
    for i in getattr(b, 'TIERS', (0.33, 0.66)):
        tx = x + w * i
        drawPolygon(tx, y - 3, tx + 1.6, y - 3, tx + 1.6, y + 16, tx, y + 16,
                    fill=palette.UI_TEXT, opacity=55)
    drawLabel(world.boss_name, view_w * 0.5, y - 12, size=14,
              fill=palette.UI_TEXT, font=palette.FONT_DISPLAY, bold=True)
    drawLabel(f'PHASE {b.tier}', x + w + 12, y + 7, size=11,
              fill=palette.UI_DIM, align='left', font=palette.FONT_UI)


# --------------------------------------------------------------------------
# The floor map
# --------------------------------------------------------------------------
#: How big a room reads on the map, and how far apart two of them sit. The
#: gap between the two is the corridor a door is drawn along.
CELL = 21.0
PITCH = 30.0

#: Rooms whose kind is known before you walk in, because they are *loud*.
#: The rift hums, the Ferryman keeps a lantern, a hearth is a fire - all of
#: them announce themselves through a wall. Everything else in this vault is
#: silent and unlit, so a room you have only seen the door of stays a
#: question mark. This is the map obeying the same rule as the game: you know
#: what you have been shown, and the dark keeps the rest.
LOUD = ('descent', 'shop', 'hearth', 'boss')


def _seg(x0, y0, x1, y1, t, color, opacity):
    """A thick line segment, as a quad. The map is drawn from these."""
    dx, dy = x1 - x0, y1 - y0
    length = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / length * t, dx / length * t
    drawPolygon(x0 + nx, y0 + ny, x1 + nx, y1 + ny,
                x1 - nx, y1 - ny, x0 - nx, y0 - ny,
                fill=color, opacity=opacity)


def _room_glyph(kind, cx, cy, color, opacity, r=5.4):
    """A small mark saying what a room is for.

    Every one of these has to be told apart from every other at about eleven
    pixels, which rules out shading, and it rules out two of them being the
    same shape at different sizes. So each is a different *silhouette*, and
    where it can be, it is the silhouette of the thing itself - the hearth is
    a flame, the shrine is the standing stone, the Ferryman is his lantern.
    """
    op = int(opacity)
    if kind == 'descent':
        drawPolygon(cx - r, cy - r * 0.7, cx + r, cy - r * 0.7, cx, cy + r,
                    fill=color, opacity=op)
    elif kind == 'entrance':
        # An arch: two posts and a lintel. Deliberately not a triangle -
        # a shrine is a triangle, and the two were indistinguishable.
        _seg(cx - r * 0.75, cy - r * 0.2, cx - r * 0.75, cy + r, 1.3, color, op)
        _seg(cx + r * 0.75, cy - r * 0.2, cx + r * 0.75, cy + r, 1.3, color, op)
        _seg(cx - r * 0.75, cy - r * 0.5, cx + r * 0.75, cy - r * 0.5,
             1.3, color, op)
    elif kind == 'boss':
        # A hollow diamond, so it reads apart from the elite's solid one.
        for i in range(4):
            a0 = i * math.tau / 4 - math.pi / 2
            a1 = (i + 1) * math.tau / 4 - math.pi / 2
            _seg(cx + math.cos(a0) * r * 1.25, cy + math.sin(a0) * r * 1.25,
                 cx + math.cos(a1) * r * 1.25, cy + math.sin(a1) * r * 1.25,
                 1.3, color, op)
    elif kind == 'elite':
        drawPolygon(cx, cy - r, cx + r * 0.78, cy, cx, cy + r, cx - r * 0.78,
                    cy, fill=color, opacity=op)
    elif kind == 'cache':
        # A chest: wider at the base than the lid.
        drawPolygon(cx - r * 0.55, cy - r * 0.6, cx + r * 0.55, cy - r * 0.6,
                    cx + r * 0.85, cy + r * 0.6, cx - r * 0.85, cy + r * 0.6,
                    fill=color, opacity=op)
    elif kind == 'shop':
        # The Ferryman's lantern: a ring, and hollow, so it cannot be
        # mistaken for anything solid.
        for i in range(8):
            a0 = i * math.tau / 8
            a1 = (i + 1) * math.tau / 8
            _seg(cx + math.cos(a0) * r * 0.85, cy + math.sin(a0) * r * 0.85,
                 cx + math.cos(a1) * r * 0.85, cy + math.sin(a1) * r * 0.85,
                 1.2, color, op)
    elif kind == 'shrine':
        # The standing stone, as it is drawn in the room: narrow, upright,
        # shouldered.
        drawPolygon(cx - r * 0.42, cy + r, cx - r * 0.30, cy - r * 0.9,
                    cx + r * 0.30, cy - r * 0.9, cx + r * 0.42, cy + r,
                    fill=color, opacity=op)
    elif kind == 'hearth':
        # A flame: round at the base, drawn to a point.
        drawPolygon(cx, cy - r * 1.15, cx + r * 0.62, cy - r * 0.1,
                    cx + r * 0.42, cy + r * 0.85, cx - r * 0.42, cy + r * 0.85,
                    cx - r * 0.62, cy - r * 0.1, fill=color, opacity=op)
    elif kind == 'gauntlet':
        for dx in (-0.62, 0.0, 0.62):
            _seg(cx + dx * r, cy - r, cx + dx * r, cy + r, 1.1, color, op)
    else:
        # A fight, or something not yet known to be anything else.
        drawPolygon(cx - 2.0, cy - 2.0, cx + 2.0, cy - 2.0,
                    cx + 2.0, cy + 2.0, cx - 2.0, cy + 2.0,
                    fill=color, opacity=op)


def _draw_floor_map(world, view_w):
    """The floor as rooms and doors, drawn from what the player has seen."""
    plan = world.plan
    if plan is None:
        return
    c0, r0, cols, rows = plan.extent()
    width = (cols - 1) * PITCH + CELL
    height = (rows - 1) * PITCH + CELL

    pad = 10.0
    box_w = max(width + pad * 2, 92.0)
    box_h = max(height + pad * 2, 62.0)
    bx = view_w - PAD - box_w
    by = PAD + _safe_top
    drawPolygon(bx, by, bx + box_w, by, bx + box_w, by + box_h, bx, by + box_h,
                fill=palette.UI_PANEL, opacity=54)
    _frame(bx, by, box_w, box_h, palette.UI_LINE, 56)

    ox = bx + (box_w - width) * 0.5
    oy = by + (box_h - height) * 0.5

    def cell_xy(room):
        return (ox + (room.col - c0) * PITCH + CELL * 0.5,
                oy + (room.row - r0) * PITCH + CELL * 0.5)

    # Doors first, so the rooms sit on top of them.
    for room in plan.rooms.values():
        if not room.seen:
            continue
        ax, ay = cell_xy(room)
        for side, rid in room.doors.items():
            other = plan.rooms[rid]
            if not other.seen or other.id < room.id:
                continue
            bx2, by2 = cell_xy(other)
            both = room.visited and other.visited
            # Drawn across the short axis so a door reads as a link rather
            # than a hairline: the cells are what carry the information and
            # the corridors only have to say which of them touch.
            horizontal = abs(bx2 - ax) > abs(by2 - ay)
            t = 2.2
            if horizontal:
                drawPolygon(ax, ay - t, bx2, by2 - t, bx2, by2 + t, ax, ay + t,
                            fill=palette.UI_LINE if both else palette.UI_FAINT,
                            opacity=88 if both else 44)
            else:
                drawPolygon(ax - t, ay, bx2 - t, by2, bx2 + t, by2, ax + t, ay,
                            fill=palette.UI_LINE if both else palette.UI_FAINT,
                            opacity=88 if both else 44)

    here = world.room
    for room in plan.rooms.values():
        if not room.seen:
            continue
        cx, cy = cell_xy(room)
        half = CELL * 0.5
        current = here is not None and room.id == here.id
        known = room.visited or room.kind in LOUD

        if room.visited:
            fill = palette.UI_PANEL
            op = 92
        else:
            fill = palette.UI_PANEL
            op = 40
        drawPolygon(cx - half, cy - half, cx + half, cy - half,
                    cx + half, cy + half, cx - half, cy + half,
                    fill=fill, opacity=op)

        # An unfought room keeps its edge lit, so what is left to do on a
        # floor can be counted at a glance.
        pending = room.hostile and not room.cleared and room.visited
        edge = (palette.UI_ACCENT if pending
                else palette.UI_LINE if room.visited else palette.UI_FAINT)
        _frame(cx - half, cy - half, CELL, CELL, edge,
               70 if room.visited else 38)

        if known:
            colour = palette.UI_TEXT if room.visited else palette.UI_DIM
            r = 5.4
            if room.kind == 'descent':
                colour = palette.PLAYER_TRIM
            elif room.kind == 'boss':
                colour = palette.UI_DANGER
            elif pending:
                colour = palette.UI_ACCENT
            _room_glyph(room.kind, cx, cy, colour,
                        92 if room.visited else 58, r=r)
        else:
            drawLabel('?', cx, cy, size=12, fill=palette.UI_DIM,
                      opacity=62, font=palette.FONT_UI)

        if current:
            k = 1.6 + 1.4 * pulse(world.run_time, 1.3)
            _frame(cx - half - k, cy - half - k, CELL + k * 2, CELL + k * 2,
                   palette.PLAYER_BODY, 92)


def _draw_minimap(world, view_w):
    lv = world.level
    size = 148
    x = view_w - PAD - size
    y = PAD + _safe_top
    inset_x, inset_y, _mw, _mh, scale = lv.minimap_rect
    ox = x + inset_x
    oy = y + inset_y

    drawPolygon(x - 4, y - 4, x + size + 4, y - 4, x + size + 4, y + size + 4,
                x - 4, y + size + 4, fill=palette.UI_PANEL, opacity=58)
    _frame(x - 4, y - 4, size + 8, size + 8, palette.UI_LINE, 60)

    # Static chamber geometry, baked once per floor.
    drawImage(lv.minimap_image, int(ox), int(oy))

    for b in lv.braziers:
        bx = ox + b.x * scale
        by = oy + b.y * scale
        drawPolygon(bx - 2, by - 2, bx + 2, by - 2, bx + 2, by + 2, bx - 2, by + 2,
                    fill=palette.LIGHT_WARM if b.lit else palette.UI_FAINT,
                    opacity=90 if b.lit else 45)

    shown = 0
    for e in world.enemies:
        if not e.alive or not e.lit or shown > 40:
            continue
        shown += 1
        ex = ox + e.x * scale
        ey = oy + e.y * scale
        s = 2.6 if e.species != 'choir' else 5.0
        drawPolygon(ex - s, ey - s, ex + s, ey - s, ex + s, ey + s, ex - s, ey + s,
                    fill=e.eye_color, opacity=82)

    if world.rift is not None:
        rx = ox + world.rift.x * scale
        ry = oy + world.rift.y * scale
        s = 3.4 + 1.6 * pulse(world.run_time, 1.1)
        drawPolygon(rx, ry - s, rx + s, ry, rx, ry + s, rx - s, ry,
                    fill=palette.PLAYER_TRIM, opacity=95)

    px = ox + world.player.x * scale
    py = oy + world.player.y * scale
    drawPolygon(px, py - 4, px + 3.4, py + 3, px - 3.4, py + 3,
                fill=palette.PLAYER_BODY, opacity=98)


def _draw_banner(world, view_w, view_h):
    if world.banner_t <= 0.0:
        return
    t = world.banner_t
    fade = clamp(t / 0.7, 0.0, 1.0) if t < 0.7 else clamp((2.4 - t) / 0.35, 0.0, 1.0)
    y = view_h * 0.32
    drawLabel(world.banner, view_w * 0.5, y, size=42, bold=True,
              fill=palette.UI_TEXT, font=palette.FONT_DISPLAY,
              opacity=int(96 * fade))
    width = min(420, 60 + len(world.banner) * 15)
    drawPolygon(view_w * 0.5 - width * 0.5 * fade, y + 30,
                view_w * 0.5 + width * 0.5 * fade, y + 30,
                view_w * 0.5 + width * 0.5 * fade, y + 31.6,
                view_w * 0.5 - width * 0.5 * fade, y + 31.6,
                fill=palette.UI_ACCENT, opacity=int(80 * fade))


def _draw_guidance(world, view_w, view_h):
    p = pulse(world.run_time, 1.4)
    drawLabel('THE RIFT IS OPEN  -  FIND IT', view_w * 0.5, view_h - 118,
              size=15, fill=palette.PLAYER_TRIM, font=palette.FONT_UI,
              opacity=int(45 + 40 * p))

    # An arrow at the screen edge pointing the way, when it is off-screen.
    ox, oy = world.camera.ox, world.camera.oy
    sx = world.rift.x - ox
    sy = world.rift.y - oy
    if 0 <= sx <= view_w and 0 <= sy <= view_h:
        return
    cx, cy = view_w * 0.5, view_h * 0.5
    a = math.atan2(sy - cy, sx - cx)
    r = min(view_w, view_h) * 0.40
    ax = cx + math.cos(a) * r
    ay = cy + math.sin(a) * r
    ca, sa = math.cos(a), math.sin(a)
    s = 13.0
    drawPolygon(ax + ca * s, ay + sa * s,
                ax - ca * s * 0.6 - sa * s * 0.7, ay - sa * s * 0.6 + ca * s * 0.7,
                ax - ca * s * 0.6 + sa * s * 0.7, ay - sa * s * 0.6 - ca * s * 0.7,
                fill=palette.PLAYER_TRIM, opacity=int(50 + 45 * p))
