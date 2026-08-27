"""Small, allocation-light maths helpers.

The game loop calls these thousands of times per second, so they take and
return plain floats and tuples rather than objects.
"""

import math

TAU = math.pi * 2.0


# ------------------------------------------------------------ interpolation --
def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def lerp(a, b, t):
    return a + (b - a) * t


def inv_lerp(a, b, v):
    if b == a:
        return 0.0
    return (v - a) / (b - a)


def approach(current, target, rate, dt):
    """Frame-rate independent exponential approach toward `target`."""
    return target + (current - target) * math.exp(-rate * dt)


def ease_out_cubic(t):
    t = clamp(t, 0.0, 1.0)
    return 1.0 - (1.0 - t) ** 3


def ease_in_cubic(t):
    t = clamp(t, 0.0, 1.0)
    return t * t * t


def ease_out_back(t):
    t = clamp(t, 0.0, 1.0)
    c1, c3 = 1.70158, 2.70158
    return 1.0 + c3 * (t - 1.0) ** 3 + c1 * (t - 1.0) ** 2


def ease_in_out_sine(t):
    return -(math.cos(math.pi * clamp(t, 0.0, 1.0)) - 1.0) / 2.0


def smoothstep(t):
    t = clamp(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def pulse(t, period):
    """A 0..1 triangle wave, useful for breathing UI elements."""
    phase = (t % period) / period
    return 1.0 - abs(phase * 2.0 - 1.0)


# ----------------------------------------------------------------- vectors --
def length(x, y):
    return math.hypot(x, y)


def length2(x, y):
    return x * x + y * y


def normalise(x, y):
    d = math.hypot(x, y)
    if d < 1e-9:
        return 0.0, 0.0
    return x / d, y / d


def rotate(x, y, radians):
    c, s = math.cos(radians), math.sin(radians)
    return x * c - y * s, x * s + y * c


def angle_of(x, y):
    return math.atan2(y, x)


def from_angle(a, r=1.0):
    return math.cos(a) * r, math.sin(a) * r


def angle_diff(a, b):
    """Shortest signed angular distance from a to b, in (-pi, pi]."""
    d = (b - a + math.pi) % TAU - math.pi
    return d


def turn_toward(current, target, max_step):
    d = angle_diff(current, target)
    if d > max_step:
        d = max_step
    elif d < -max_step:
        d = -max_step
    return current + d


# ---------------------------------------------------------------- geometry --
def dist(ax, ay, bx, by):
    return math.hypot(bx - ax, by - ay)


def dist2(ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    return dx * dx + dy * dy


def circles_overlap(ax, ay, ar, bx, by, br):
    r = ar + br
    dx, dy = bx - ax, by - ay
    return dx * dx + dy * dy <= r * r


def point_in_rect(px, py, x, y, w, h):
    return x <= px <= x + w and y <= py <= y + h


def closest_point_on_rect(px, py, x, y, w, h):
    return clamp(px, x, x + w), clamp(py, y, y + h)


def circle_hits_rect(cx, cy, r, x, y, w, h):
    qx, qy = clamp(cx, x, x + w), clamp(cy, y, y + h)
    dx, dy = cx - qx, cy - qy
    return dx * dx + dy * dy <= r * r


def segment_closest_t(px, py, ax, ay, bx, by):
    """Parameter t in [0,1] of the closest point on segment AB to P."""
    abx, aby = bx - ax, by - ay
    denom = abx * abx + aby * aby
    if denom < 1e-12:
        return 0.0
    t = ((px - ax) * abx + (py - ay) * aby) / denom
    return clamp(t, 0.0, 1.0)


def point_segment_dist2(px, py, ax, ay, bx, by):
    t = segment_closest_t(px, py, ax, ay, bx, by)
    qx, qy = ax + (bx - ax) * t, ay + (by - ay) * t
    dx, dy = px - qx, py - qy
    return dx * dx + dy * dy


def point_in_polygon(px, py, pts):
    """Even-odd test against a flat list of (x, y) tuples."""
    inside = False
    n = len(pts)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > py) != (yj > py):
            denom = yj - yi
            if denom != 0.0 and px < (xj - xi) * (py - yi) / denom + xi:
                inside = not inside
        j = i
    return inside


def opacity(value):
    """Clamp to the 0-100 integer range cmu-graphics requires.

    The library raises on an out-of-range opacity rather than clamping, so a
    computed value that momentarily reaches 101 is a hard crash mid-frame.
    Anything derived from gameplay state goes through here.
    """
    if value < 0:
        return 0
    if value > 100:
        return 100
    return int(value)
