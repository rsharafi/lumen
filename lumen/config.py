"""Tuning constants for LUMEN.

Everything the designer might want to twist lives here so the rest of the
codebase can stay free of magic numbers.
"""

# ---------------------------------------------------------------- display ---
# The window's initial pixel size. Everything in the game is authored against
# DESIGN_HEIGHT units tall; the renderer scales that up to whatever the window
# or display actually is, so a 4K panel gets 4K-sharp vector art and text from
# the same code. Width in design units follows the window's aspect ratio, so a
# wider window shows more of the chamber rather than stretching it.
WIDTH = 1280
HEIGHT = 720
DESIGN_HEIGHT = 720.0
MIN_WINDOW = (640, 400)
# Fallback only. At runtime the game reads the display's actual refresh rate
# and targets that, so a 120 Hz panel gets 120 fps.
FPS = 60
MAX_FPS = 240
TITLE = 'LUMEN - Descent into the Vault'

# The renderer profile that shaped this game (measured on cmu-graphics 2.0.1):
#   drawImage      ~21 us/call, independent of image size
#   drawRect/Poly  ~12 us/call
#   drawCircle     ~36 us/call   <- avoid in hot loops
#   drawLabel      ~35 us/call
# The frame budget below is what we aim to stay under for the whole redraw.
FRAME_BUDGET_MS = 12.0

# ------------------------------------------------------------------ world ---
TILE = 64
CHAMBER_MIN_W, CHAMBER_MAX_W = 22, 28   # in tiles
CHAMBER_MIN_H, CHAMBER_MAX_H = 14, 18

CAMERA_LERP = 0.14
CAMERA_LOOKAHEAD = 90.0

# ---------------------------------------------------------------- player ----
PLAYER_RADIUS = 13.0
PLAYER_SPEED = 268.0          # px / second
PLAYER_ACCEL = 14.0           # approach rate toward target velocity
PLAYER_MAX_HP = 100.0
PLAYER_IFRAMES = 0.55         # seconds of invulnerability after a hit

DASH_SPEED = 900.0
DASH_TIME = 0.16
DASH_COOLDOWN = 0.72
DASH_IFRAMES = 0.22
DASH_CHARGES = 1

# --------------------------------------------------------------- lantern ----
LANTERN_RADIUS = 300.0
LANTERN_RADIUS_MIN = 132.0
# The lantern's radius is snapped to this many pixels. Rescaling a ~600 px
# glow sprite costs ~1 ms every frame because CPCS mode rebuilds the shape and
# so never hits the renderer's scaled-image cache; snapping lets a handful of
# exact-size sprites be baked once and blitted straight. The falloff is soft
# enough at the rim that the steps are invisible, and the shadow geometry uses
# the same snapped value so the two always agree.
LANTERN_RADIUS_STEP = 24.0

# How a cast shadow is built. The bands are filled opaque (a translucent wash
# only removes its own share of the light, so half the lantern used to bleed
# through walls), and then the floor is blitted back over the whole chamber at
# this strength. The net effect is multiplicative: shadowed ground keeps the
# stone texture at full relative contrast instead of turning into a flat
# silhouette, while the light is still fully occluded.
# The floor is drawn *over* the light at this opacity, not under it. Drawing
# it under and then re-blitting to restore texture cost a second full-chamber
# blit every frame; this way one blit does both jobs, and shadowed ground comes
# out identical to unlit ground rather than merely close. The bake is
# brightened to compensate for being composited at partial opacity.
SHADOW_FLOOR_MIX = 46          # opacity of the floor pass, 0-100
LANTERN_FUEL_MAX = 100.0
LANTERN_DRAIN = 1.45           # fuel per second while lit
LANTERN_FLARE_COST = 26.0
# Recovery. Without these a run can dead-end: fuel gone, lantern collapsed,
# and nothing left to find in the dark.
BRAZIER_IGNITE_FUEL = 30.0     # one-off refill for lighting a brazier
BRAZIER_REFILL_RATE = 16.0     # fuel per second while standing in one
BRAZIER_REFILL_RANGE = 96.0
FLOOR_ENTRY_FUEL = 25.0
LANTERN_FLARE_RADIUS = 620.0
LANTERN_FLARE_TIME = 0.55
LANTERN_FLARE_COOLDOWN = 3.0
LANTERN_FLARE_DAMAGE = 26.0
LANTERN_FLARE_KNOCKBACK = 620.0

# Shadowcasting resolution. Rays are cast at wall corners (plus a hair either
# side); this caps how many corners we will consider in one frame.
LIGHT_MAX_CORNERS = 96
LIGHT_EPSILON = 0.0016         # radians nudged either side of each corner

# ------------------------------------------------------------------- fx -----
MAX_PARTICLES = 4000
# Particles keep simulating past this many, but only this many are drawn in a
# frame. The old cap of 170 was set by cmu-graphics charging ~35 us a shape,
# where a five-kill burst of 270 was most of a 120 Hz frame on its own. On the
# GPU a particle is one quad, so the cap is now high enough that it only
# exists as a backstop. When it is exceeded the most-faded ones are skipped,
# which is where the eye is least likely to miss them.
MAX_PARTICLES_DRAWN = 3000
SHAKE_DECAY = 7.0
HITSTOP_MAX = 0.09

# ------------------------------------------------------------ progression ---
# Twenty floors in three acts, each closing on a boss. Six, seven and seven:
# acts lengthen as they go, because a later act has more to show before its
# boss than the one that was still teaching you to walk.
FLOORS_PER_RUN = 20
BOSS_FLOORS = (6, 13, 20)
UPGRADE_CHOICES = 3
