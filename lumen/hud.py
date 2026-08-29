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
    _draw_minimap(world, w)
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
    y = PAD
    label = (world.boss_name if world.is_boss
             else f'FLOOR {world.depth:02d}')
    draw_text(label, x, y + 10, 'display', 19, (255, 178, 84), align='left')
    drawLabel(f'{world.score:,}', x, y + 26, size=15, fill=palette.UI_TEXT,
              align='left-top', font=palette.FONT_UI)
    drawLabel(f'EMBERS {world.embers}   KILLS {world.kills}', x, y + 46,
              size=11, fill=palette.UI_DIM, align='left-top',
              font=palette.FONT_UI)


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
    y = 30
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


def _draw_minimap(world, view_w):
    lv = world.level
    size = 148
    x = view_w - PAD - size
    y = PAD
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
