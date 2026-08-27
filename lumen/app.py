"""Top-level state machine.

`main.py` binds cmu-graphics' event handlers straight to the methods here, so
this module owns the whole lifecycle: title, help, a run, the draft between
floors, and the endings.

Two performance notes that shape the code:

* MOUSEMOTION events are blocked at the pygame level and the cursor is polled
  in `step` instead. cmu-graphics redraws once per event batch, so an
  unblocked mouse drags the frame rate down to however fast you can wiggle it.
* `dt` comes from a real clock rather than assuming 1/60, so the simulation
  stays honest if a frame runs long.
"""

import gc
import math
import os
import sys
import time

from .draw import drawLabel, drawPolygon

from . import (art, audio, draw, gpu, hud, palette, rng, runtime, save,
               screens, upgrades)
from .config import (DESIGN_HEIGHT, FPS, FLOORS_PER_RUN, HEIGHT, MAX_FPS,
                     UPGRADE_CHOICES, WIDTH)
from .mathx import clamp
from .world import World

TITLE = 'title'
HELP = 'help'
PLAYING = 'playing'
PAUSED = 'paused'
DRAFT = 'draft'
ENDED = 'ended'

# Sharpness-dial sentinels. AUTO picks a rung by measurement; PER_POINT means
# one framebuffer pixel per point, i.e. high-DPI off.
AUTO = 'auto'
PER_POINT = 'per-point'

MOVE_KEYS = {'w', 'a', 's', 'd', 'up', 'down', 'left', 'right'}
FIRE_KEYS = {'j'}
DASH_KEYS = {'space', 'k'}
FLARE_KEYS = {'shift', 'l'}


class Game:
    def __init__(self):
        self.state = TITLE
        self.width = WIDTH
        self.height = HEIGHT
        self.keys = set()
        self.mouse = (WIDTH * 0.5, HEIGHT * 0.5)
        self.mouse_down = False
        self.t = 0.0
        self._last = None

        self.save = save.load()
        self.sound_on = bool(self.save.get('sound', True))

        self.stats = None
        self.world = None
        self.title_screen = None
        self.help_screen = None
        self.draft_screen = None
        self.end_screen = None
        self.help_return = TITLE

        self.fade = 1.0
        self.fade_target = 0.0
        self.pending = None
        self.ending = False
        self._blit_checked = False
        self.scale = 1.0
        self.pixel_w = WIDTH
        self.pixel_h = HEIGHT
        self.fullscreen = False
        self.deferred = True
        self.volumetric = True
        self.windowed_size = (WIDTH, HEIGHT)
        self.display_index = 0
        self._app_ref = None
        self.frame_ms = 0.0
        self.show_debug = bool(os.environ.get('LUMEN_DEBUG'))
        # Fixed-step mode and a pinned seed make headless playtests
        # reproducible; both are off unless the environment asks for them.
        self.fixed_dt = 1.0 / FPS if os.environ.get('LUMEN_FIXED_DT') else None
        forced = os.environ.get('LUMEN_SEED')
        self.forced_seed = int(forced) if forced else None
        # Scripted harnesses set the cursor themselves; polling would stomp it.
        self.poll_pointer = not os.environ.get('LUMEN_NO_POINTER')
        # Test hook: begin a run partway down instead of at floor 1.
        self.start_floor = int(os.environ.get('LUMEN_START_FLOOR', '1'))
        # Test hook: quit automatically after N steps (used to smoke-test the
        # real entry point without a human at the keyboard).
        selftest = os.environ.get('LUMEN_SELFTEST')
        self.selftest_frames = int(selftest) if selftest else 0
        self.selftest_play = bool(os.environ.get('LUMEN_SELFTEST_PLAY'))
        self.frames = 0
        self.target_fps = FPS
        self._frame_marks = []
        # AUTO's evidence: recent frame periods, the rung it settled on, and
        # when it last moved.
        self._art_generation = art.GENERATION
        self._auto_history = []
        # Where AUTO settled last time. Starting from the remembered rung
        # saves a fresh run the first seconds of measuring its way back down.
        self.auto_index = 1
        self._auto_changed = 0.0

    # ----------------------------------------------------------- lifecycle --
    def start(self, app):
        app.background = palette.VOID
        # With a fixed timestep the simulation no longer depends on wall clock,
        # so headless harnesses can run the loop as fast as the machine allows.
        app.stepsPerSecond = 100000 if self.fixed_dt else FPS
        app.setMaxShapeCount(60000)
        app.inspectorEnabled = False
        try:
            app.title = 'LUMEN'
        except Exception:
            pass

        self._app_ref = app
        self._adopt_size(app)
        self.windowed_size = (self.pixel_w, self.pixel_h)
        stored = int(self.save.get('display', -1))
        self.display_index = (stored % len(self.QUALITY_MODES) if stored >= 0
                              else self.default_quality_index())
        # Test hook: LUMEN_QUALITY=NATIVE|HIGH|BALANCED|SMOOTH|FAST pins the
        # sharpness dial, so the trade can be measured without a human at the
        # menu.
        forced = (os.environ.get('LUMEN_QUALITY') or '').upper()
        if forced:
            names = [name for name, _ in self.QUALITY_MODES]
            if forced in names:
                self.display_index = names.index(forced)
        self.fullscreen = bool(self.save.get('fullscreen', False))
        remembered = int(self.save.get('auto', self.AUTO_RUNGS[0]))
        if remembered in self.AUTO_RUNGS:
            self.auto_index = remembered
        self.deferred = str(self.save.get('visuals', 'lit')).lower() != 'classic'
        self.volumetric = bool(self.save.get('volumetric', True))

        self._block_mouse_motion()

        bank = audio.bank()
        bank.set_enabled(self.sound_on and not os.environ.get('LUMEN_HEADLESS'))
        bank.build()

        rng.fx.reseed(self.forced_seed if self.forced_seed is not None
                      else int(time.time() * 1000) % 2 ** 31)
        self.title_screen = screens.TitleScreen(self.width, self.height, rng.fx,
                                                self.save)
        self.help_screen = screens.HelpScreen(self.width, self.height, rng.fx)
        self.draft_screen = screens.UpgradeScreen(self.width, self.height)
        self.end_screen = screens.EndScreen(self.width, self.height, rng.fx)

        self._warm_cache()

        # The long-lived caches (baked chambers, every sprite) are moved to a
        # permanent generation so the collector stops re-scanning them. Note
        # this is housekeeping, not a frame-time fix: measured against the
        # collector disabled entirely, and against thresholds from 700 to
        # 50000, frame times were indistinguishable. The periodic stalls that
        # looked like GC turned out to be sprites being rasterised mid-frame.
        gc.collect()
        gc.freeze()

        if self.selftest_play:
            self.new_run()

    def _hover(self, screen):
        """Let the pointer drive the selection on a menu-style screen."""
        rects = getattr(screen, 'hit_rects', None)
        if not rects:
            return
        index = screens.hit_test(rects, self.mouse[0], self.mouse[1])
        if index is None:
            return
        menu = getattr(screen, 'menu', None)
        current = menu.index if menu is not None else screen.index
        if index != current:
            if menu is not None:
                menu.index = index
            else:
                screen.index = index
            audio.play('ui_move', 0.4)

    def _adopt_size(self, app):
        """Recompute the design view from the window's pixel size.

        The scale is continuous, so the vertical framing is identical at every
        resolution and a wider window simply reveals more of the chamber.
        """
        pixel_w = max(1, int(app.width))
        pixel_h = max(1, int(app.height))
        self.pixel_w = pixel_w
        self.pixel_h = pixel_h
        design_h = float(os.environ.get('LUMEN_DESIGN_HEIGHT') or DESIGN_HEIGHT)
        self.scale = pixel_h / design_h
        # Design units stay integral: sprite sizes and layout maths downstream
        # all expect whole pixels.
        self.width = max(320, int(round(pixel_w / self.scale)))
        self.height = int(design_h)
        draw.set_scale(self.scale)
        art.set_scale(self.scale)

    def _warm_cache(self):
        """Bake every screen-sized sprite up front, so none is built mid-frame.

        The flash vignettes matter most: generating one on the frame you get
        hit is a ~20 ms hitch exactly when the game needs to feel responsive.
        Runs again after any resize, because a render-scale change empties the
        cache and every one of these is scale-dependent.
        """
        art.screen_overlay(self.width, self.height, 0.94, 0.6, 0.05, 0.1, 4)
        art.vignette(self.width, self.height, 0.95, 0.45)
        art.grain(self.width, self.height, 0.045)
        for color in (palette.UI_DANGER, palette.SHIELD, palette.FLARE,
                      palette.BOSS_EYE, palette.LIGHT_CORE, palette.UI_GOOD):
            art.vignette(self.width, self.height, 1.0, 0.26,
                         art.rgb_tuple(color))

    def resize(self, app):
        """Window changed size: re-derive the view and rebuild what depends on it.

        Anything holding a baked sprite has to rebuild it here. `art.set_scale`
        releases the pixels of every cached sprite when the scale changes, so a
        reference kept across a resize becomes an empty husk that raises on the
        next frame that draws it.
        """
        previous = self.scale
        previous_art = self._art_generation
        self._adopt_size(app)
        self._art_generation = art.GENERATION
        # A rebake is needed when the scale moved, and also when the sprite
        # cache was emptied under us - which is what happens when the renderer
        # is replaced, since a GPU sprite's texture belongs to one renderer.
        rescaled = (abs(previous - self.scale) > 1e-6
                    or previous_art != art.GENERATION)
        if rescaled and self.world is not None and self.world.level is not None:
            # Every sprite was discarded with the old scale; the chamber's
            # baked layers have to be re-rendered at the new one.
            from . import level as level_mod
            level_mod.rebake(self.world.level)
        for screen in (self.title_screen, self.help_screen, self.draft_screen,
                       self.end_screen):
            if screen is not None:
                screen.resize(self.width, self.height)
        if self.world is not None:
            self.world.resize(self.width, self.height)
        self._warm_cache()
        # Remember the window's own size in points, so leaving fullscreen goes
        # back to whatever the player last dragged it to.
        if not self.fullscreen and runtime.own_window() is not None:
            self.windowed_size = tuple(runtime.own_window().size)

    def _take_over_window(self, app, verbose=False):
        """Replace the framework's window with a high-DPI one.

        pygame's `set_mode` cannot ask SDL for a high-DPI framebuffer, so the
        window cmu-graphics opens is sized in points and gets stretched over
        the panel by the compositor. Swapping in an equivalent window with the
        flag set is what makes the game draw at the display's real pixels.
        """
        if os.environ.get('LUMEN_HEADLESS'):
            return          # dummy video driver: there is no real window
        if os.environ.get('LUMEN_FULLSCREEN'):
            self.fullscreen = True
        runtime.install_resize_hook(app)
        runtime.install_gpu_hooks(app)
        # Replacing the window resets SDL's event filters, so the block set up
        # in `start` is gone by now and has to go back on.
        self._block_mouse_motion()
        if not self.apply_video(app):
            sys.stderr.write('[lumen] high-DPI window unavailable; '
                             'running at window resolution\n')
            return
        self.resize(app)
        if verbose:
            w, h = runtime.render_size()
            sys.stderr.write(f'[lumen] rendering {w}x{h}\n')

    def apply_video(self, app):
        """Put the window into the current fullscreen/quality configuration."""
        if app is None:
            return False
        return runtime.set_video_mode(app, self.window_points(),
                                      self.fullscreen, self.quality())

    def window_points(self):
        """The window's size in points - the desktop's when fullscreen."""
        return runtime.desktop_size() if self.fullscreen else self.windowed_size

    def toggle_fullscreen(self, app):
        """Swap between the window and the whole display.

        Fullscreen keeps the high-DPI framebuffer, so it is the display's real
        pixels the game draws into, not the desktop's point size stretched to
        fit.
        """
        self.fullscreen = not self.fullscreen
        if not self.apply_video(app):
            self.fullscreen = not self.fullscreen
            return
        self.resize(app)
        self.save['fullscreen'] = self.fullscreen
        save.save(self.save)

    def _match_display_rate(self, app, verbose=False):
        """Target the display's own refresh rate rather than a fixed 60."""
        if self.fixed_dt is not None:
            return          # headless harness runs the loop flat out
        forced = os.environ.get('LUMEN_FPS')
        if forced:
            rate = max(30, min(MAX_FPS, int(forced)))
        else:
            rate = runtime.detect_refresh_rate(default=FPS, high=MAX_FPS)
        self.target_fps = rate
        try:
            app.stepsPerSecond = rate
        except Exception:
            return
        if verbose:
            sys.stderr.write(f'[lumen] targeting {rate} fps\n')

    @staticmethod
    def _block_mouse_motion():
        try:
            import pygame
            pygame.event.set_blocked(pygame.MOUSEMOTION)
        except Exception:
            pass

    def _poll_mouse(self):
        if not self.poll_pointer:
            return
        try:
            import pygame
            px, py = pygame.mouse.get_pos()
            self.mouse = self._to_design(px, py)
        except Exception:
            pass

    def _to_design(self, px, py):
        """Pointer position -> design units.

        SDL reports the cursor in points. On a high-DPI window a point is two
        framebuffer pixels, so the position has to cross into pixels before it
        can be divided by the render scale.
        """
        k = runtime.pointer_scale() / self.scale
        return (px * k, py * k)

    # --------------------------------------------------------------- runs --
    def new_run(self, seed=None):
        if seed is None:
            seed = (self.forced_seed if self.forced_seed is not None
                    else int(time.time() * 1000) % 2 ** 31)
        rng.world.reseed(seed)
        self.stats = upgrades.Stats()
        self.world = World(self.stats, rng.world, rng.fx, self.width, self.height)
        self._sync_visuals()
        self.world.enter_floor(max(1, min(FLOORS_PER_RUN, self.start_floor)))
        self.ending = False
        self.state = PLAYING

    def next_floor(self):
        world = self.world
        depth = world.depth + 1
        if depth > FLOORS_PER_RUN:
            self.finish_run(won=True)
            return
        world.enter_floor(depth)
        self.state = PLAYING
        audio.play('descend', 0.6)

    def finish_run(self, won):
        world = self.world
        record = world.score > self.save.get('best_score', 0)
        self.save = save.record_run(self.save, world.score, world.depth,
                                    world.kills, won)
        self.end_screen.open(world, won, self.save, record)
        self.state = ENDED
        audio.play('upgrade' if won else 'game_over', 0.8)

    def open_draft(self):
        choices = upgrades.offer(self.stats, rng.world, UPGRADE_CHOICES)
        if not choices:
            self.next_floor()
            return
        self.draft_screen.open(choices, self.world.depth)
        self.state = DRAFT
        audio.play('ui_select', 0.4)

    def take_upgrade(self, index):
        choices = self.draft_screen.choices
        if not choices:
            return
        index = clamp(index, 0, len(choices) - 1)
        upgrades.grant(self.stats, choices[index])
        self.world.player.refresh_from_stats()
        audio.play('upgrade', 0.6)
        self.transition(self.next_floor)

    def transition(self, action):
        """Fade out, run `action`, fade back in."""
        self.pending = action
        self.fade_target = 1.0

    # -------------------------------------------------------------- update --
    def step(self, app):
        now = time.perf_counter()
        if self.fixed_dt is not None:
            dt = self.fixed_dt
        elif self._last is None:
            dt = 1.0 / FPS
        else:
            dt = clamp(now - self._last, 0.0, 1.0 / 15.0)
        self._last = now
        self.t += dt
        self.frames += 1

        if not self._blit_checked:
            # The display surface only exists once the framework's loop has
            # started, so this cannot run from onAppStart.
            self._blit_checked = True
            verbose = bool(os.environ.get('LUMEN_DEBUG'))
            # Ask before the takeover: the framework's window is still up, and
            # that is the one query that needs a window of its own.
            self._match_display_rate(app, verbose)
            self._take_over_window(app, verbose)
            if not gpu.active():
                # Both of these tune cmu-graphics' own present path, which the
                # GPU backend replaces outright.
                runtime.enable(verbose=verbose)
                runtime.enable_adaptive_wait(verbose=verbose)
            try:
                import pygame
                pygame.mouse.set_visible(False)
            except Exception:
                pass
        if self.selftest_frames:
            self._frame_marks.append(now)
        if self.selftest_frames and self.frames >= self.selftest_frames:
            shot = os.environ.get('LUMEN_SELFTEST_SHOT')
            if shot:
                try:
                    app._app.getScreenshot(shot)
                except Exception as exc:
                    sys.stderr.write(f'[lumen] screenshot failed: {exc}\n')
            marks = self._frame_marks
            fps = ''
            if len(marks) > 40:
                gaps = sorted(marks[i + 1] - marks[i]
                              for i in range(20, len(marks) - 1))
                mid = gaps[len(gaps) // 2] * 1000.0
                p95 = gaps[int(len(gaps) * 0.95)] * 1000.0
                fps = (f', frame={mid:.2f}ms ({1000.0 / max(mid, 1e-6):.0f} fps)'
                       f', p95={p95:.2f}ms')
            world = self.world
            where = ''
            if world is not None:
                alive = len([e for e in world.enemies if e.alive])
                where = (f', floor={world.depth}'
                         f', player=({world.player.x:.1f},{world.player.y:.1f})'
                         f', enemies={alive}, hp={world.player.hp:.1f}')
            sys.stderr.write(
                f'[lumen] selftest ok: {self.frames} steps, state={self.state}'
                f'{where}, shapes={self.frame_ms:.2f}ms{fps}\n')
            app.quit()
            return

        self._poll_mouse()
        # dt is the real interval between steps, which is the frame period the
        # player actually sees - see the note in runtime about why timing
        # inside redrawAll measures only a third of it. Only frames spent in a
        # chamber count: the menus draw a fraction of the shapes, and letting
        # them into the history would talk AUTO into a setting that a boss
        # room cannot hold.
        if self.fixed_dt is None and self.state == PLAYING and self.fade <= 0.0:
            self._note_frame(dt * 1000.0)

        # Screen fade / scene handoff.
        if self.fade_target > self.fade:
            self.fade = min(1.0, self.fade + dt * 5.0)
            if self.fade >= 1.0 and self.pending is not None:
                action, self.pending = self.pending, None
                action()
                # The screen is fully black here, which is the one place a
                # re-bake of every sprite cannot be seen.
                self._auto_settle(app)
                self.fade_target = 0.0
        elif self.fade > 0.0:
            self.fade = max(0.0, self.fade - dt * 3.2)
        elif self.state in (TITLE, PAUSED, DRAFT):
            # Menus are calm enough to absorb the pause too, and a player who
            # never changes floor would otherwise never be re-measured.
            self._auto_settle(app)
        elif self.state == PLAYING:
            # A whole floor spent well under the refresh rate is worse than one
            # hitch that fixes it, so mid-play AUTO may still ease off - but
            # only downwards, only when the frame is clearly not sustainable,
            # and not twice in a hurry.
            self._auto_settle(app, urgent_only=True)

        if self.state == TITLE:
            self.title_screen.update(dt)
            self._hover(self.title_screen)
        elif self.state == HELP:
            self.help_screen.update(dt)
        elif self.state == DRAFT:
            self.draft_screen.update(dt)
            self._hover(self.draft_screen)
        elif self.state == ENDED:
            self.end_screen.update(dt)
        elif self.state == PLAYING:
            self._step_play(dt)

    def _step_play(self, dt):
        world = self.world
        cam = world.camera
        aim = (self.mouse[0] + cam.ox, self.mouse[1] + cam.oy)
        firing = self.mouse_down or bool(self.keys & FIRE_KEYS)
        world.update(dt, self.keys, aim, firing)

        if not world.player.alive:
            # Let the death play out under the fade, but only arm the handoff
            # once - `transition` is not idempotent across frames.
            if not self.ending:
                self.ending = True
                self.transition(lambda: self.finish_run(won=False))
            return

        if world.rift_ready():
            world.rift.entered = True
            if world.depth >= FLOORS_PER_RUN:
                self.transition(lambda: self.finish_run(won=True))
            else:
                self.transition(self.open_draft)

    # -------------------------------------------------------------- input --
    def key_press(self, app, key):
        key = key.lower() if len(key) == 1 else key
        self.keys.add(key)

        if key in ('f11', 'f'):
            self.toggle_fullscreen(app)
            return

        if self.state == TITLE:
            self._title_key(key)
        elif self.state == HELP:
            self._help_key(key)
        elif self.state == PLAYING:
            self._play_key(key)
        elif self.state == PAUSED:
            self._pause_key(key)
        elif self.state == DRAFT:
            self._draft_key(key)
        elif self.state == ENDED:
            self._end_key(key)

    def key_release(self, app, key):
        key = key.lower() if len(key) == 1 else key
        self.keys.discard(key)

    def mouse_press(self, app, x, y, button=0):
        self.mouse = self._to_design(x, y)
        if button == 0:
            self.mouse_down = True
        if button != 0:
            return
        if self.state == TITLE:
            if self._click(self.title_screen):
                self._title_key('enter')
        elif self.state == DRAFT:
            if self._click(self.draft_screen):
                self.take_upgrade(self.draft_screen.index)
        elif self.state == ENDED:
            self._end_key('enter')
        elif self.state == HELP:
            self.help_screen.turn(1)
            audio.play('ui_move', 0.4)

    def _click(self, screen):
        """True if the pointer is over an option (or the screen has none)."""
        rects = getattr(screen, 'hit_rects', None)
        if not rects:
            return True
        index = screens.hit_test(rects, self.mouse[0], self.mouse[1])
        if index is None:
            return False
        menu = getattr(screen, 'menu', None)
        if menu is not None:
            menu.index = index
        else:
            screen.index = index
        return True

    def mouse_release(self, app, x, y, button=0):
        if button == 0:
            self.mouse_down = False

    # -- per-state key handling --------------------------------------------
    def _title_key(self, key):
        menu = self.title_screen.menu
        if key in ('up', 'w'):
            menu.move(-1)
            audio.play('ui_move', 0.4)
        elif key in ('down', 's'):
            menu.move(1)
            audio.play('ui_move', 0.4)
        elif key in ('enter', 'space'):
            choice = menu.current
            if choice == 'DESCEND':
                audio.play('ui_select', 0.6)
                self.transition(self.new_run)
            elif choice == 'HOW TO PLAY':
                audio.play('ui_select', 0.5)
                self.help_return = TITLE
                self.state = HELP
            elif choice == 'DISPLAY':
                self.cycle_display(self._app_ref)
            elif choice == 'VISUALS':
                self.toggle_visuals()
            elif choice == 'SHAFTS':
                self.toggle_volumetric()
            elif choice == 'SOUND':
                self.toggle_sound()
            elif choice == 'QUIT':
                self.quit()

    def _help_key(self, key):
        if key in ('escape', 'backspace', 'h'):
            audio.play('ui_back', 0.4)
            self.state = self.help_return
        elif key in ('right', 'd', 'enter', 'space'):
            self.help_screen.turn(1)
            audio.play('ui_move', 0.4)
        elif key in ('left', 'a'):
            self.help_screen.turn(-1)
            audio.play('ui_move', 0.4)

    def _play_key(self, key):
        world = self.world
        player = world.player
        if key in ('escape', 'p'):
            self.state = PAUSED
            audio.play('ui_back', 0.4)
        elif key in DASH_KEYS:
            player.try_dash(self.keys, world.particles, rng.fx)
        elif key in FLARE_KEYS:
            world.flare()
        elif key in ('1', '2', '3'):
            index = int(key) - 1
            from .projectiles import WEAPONS
            if index < len(WEAPONS) and index != player.weapon_index:
                player.weapon_index = index
                player.charge = 0.0
                player.charging = False
                player.cooldown = max(player.cooldown, 0.12)
                world.set_banner(player.weapon.name, 1.0)
                audio.play('ui_move', 0.5)
        elif key == 'tab':
            from .projectiles import WEAPONS
            player.weapon_index = (player.weapon_index + 1) % len(WEAPONS)
            player.charge = 0.0
            world.set_banner(player.weapon.name, 1.0)
            audio.play('ui_move', 0.5)

    def _pause_key(self, key):
        if key in ('escape', 'p'):
            self.state = PLAYING
            self._last = None
            audio.play('ui_select', 0.4)
        elif key == 'm':
            self.toggle_sound()
        elif key == 'h':
            self.help_return = PAUSED
            self.state = HELP
        elif key == 'q':
            audio.play('ui_back', 0.5)
            self.transition(lambda: self.finish_run(won=False))

    def _draft_key(self, key):
        screen = self.draft_screen
        if key in ('left', 'a'):
            screen.move(-1)
            audio.play('ui_move', 0.4)
        elif key in ('right', 'd'):
            screen.move(1)
            audio.play('ui_move', 0.4)
        elif key in ('enter', 'space'):
            self.take_upgrade(screen.index)
        elif key in ('1', '2', '3'):
            self.take_upgrade(int(key) - 1)

    def _end_key(self, key):
        if key in ('enter', 'space'):
            self.transition(self.new_run)
        elif key in ('escape', 'backspace'):
            audio.play('ui_back', 0.4)
            self.title_screen.save = self.save
            self.transition(lambda: setattr(self, 'state', TITLE))

    # The sharpness dial, as a fraction of the display's native pixel density.
    #
    # This is a real trade, not a preference. The renderer is a CPU
    # rasteriser, and once the frame is bigger than cache it is bound by
    # memory bandwidth: measured on this machine, a busy chamber costs about
    # 3.9 ms of resolution-independent work plus ~2 ms per megapixel. NATIVE
    # on a 3600x2338 framebuffer is therefore ~24 ms a frame, and holding a
    # 120 Hz frame budget of 8.3 ms means drawing about 1.9 megapixels. No
    # setting is both; the dial is how you choose where to sit.
    #
    # FAST's None means "one framebuffer pixel per point" - the density the
    # game ran at before it asked SDL for a high-DPI window.
    QUALITY_MODES = (
        ('AUTO', AUTO),
        ('NATIVE', 1.00),
        ('HIGH', 0.84),
        ('BALANCED', 0.72),
        ('SMOOTH', 0.62),
        ('FAST', PER_POINT),
        ('FASTEST', 0.40),
    )
    # The rungs AUTO is allowed to walk, sharpest first.
    AUTO_RUNGS = (1, 2, 3, 4, 5, 6)

    def _rung_quality(self, value):
        if value is PER_POINT:
            factor = runtime.display_scale_factor()
            return 1.0 / factor if factor > 1.0 else 1.0
        return value

    def default_quality_index(self):
        """Where the dial starts for a player who has never touched it.

        On the GPU renderer every rung holds the display's refresh rate, so
        there is nothing to buy by rendering below native - it would only be
        softer. On cmu-graphics native costs 40 fps, and neither end of the
        dial is a good first impression, so it starts in the middle.
        """
        names = [name for name, _ in self.QUALITY_MODES]
        return names.index('NATIVE' if gpu.wanted() else 'BALANCED')

    def quality(self):
        """The current dial position as a fraction of native density."""
        value = self.QUALITY_MODES[self.display_index][1]
        if value is AUTO:
            value = self.QUALITY_MODES[self.auto_index][1]
        return self._rung_quality(value)

    def on_auto(self):
        return self.QUALITY_MODES[self.display_index][1] is AUTO

    def display_label(self):
        # The resolution says which rung AUTO settled on, so naming it as well
        # would only make the row long enough to crowd the menu.
        name = self.QUALITY_MODES[self.display_index][0]
        if runtime.own_window() is not None:
            w, h = runtime.render_size()
        else:
            w, h = self.pixel_w, self.pixel_h
        return f'{name}  {w} x {h}'

    # ----------------------------------------------------------------------
    # AUTO: hold the display's refresh rate at the sharpest setting that fits.
    #
    # Sharpness and frame rate are a straight trade here - the renderer is a
    # CPU rasteriser, so a frame costs a fixed amount of shape work plus about
    # two milliseconds per megapixel - and no single setting is right for both
    # a quiet corridor and a boss room. AUTO measures what frames actually
    # cost and moves the dial, but only while the screen is black between
    # floors or sitting in a menu: changing the render scale re-bakes every
    # sprite at the new size, which is a couple of hundred milliseconds, and
    # that has to happen where it cannot be felt.
    # ----------------------------------------------------------------------
    AUTO_WINDOW = 75          # frames of history a decision is made on
    AUTO_COOLDOWN = 3.0       # seconds between changes at a calm moment
    AUTO_URGENT_COOLDOWN = 8.0
    AUTO_URGENT_AT = 1.22     # mid-play, only step in if it is this far over
    AUTO_HEADROOM = 0.95      # of the budget, so a busy room still fits

    def _note_frame(self, period_ms):
        history = self._auto_history
        history.append(period_ms)
        if len(history) > self.AUTO_WINDOW:
            del history[:-self.AUTO_WINDOW]

    def _native_megapixels(self):
        """What the framebuffer would be at full density, in megapixels."""
        rw, rh = runtime.render_size()
        q = max(self.quality(), 0.01)
        return (rw * rh) / 1e6 / (q * q)

    def _auto_target(self, urgent_only=False):
        """The rung AUTO should be on, or None to stay put.

        Rather than stepping one rung at a time - which would cost a re-bake
        at every step on the way down - the measured frame is split into the
        part that does not depend on resolution (building shapes, which
        `frame_ms` already measures) and the part that does, and each rung's
        cost is predicted from that. One measurement, one move.
        """
        if not self.on_auto() or self.fixed_dt is not None:
            return None
        cooldown = self.AUTO_URGENT_COOLDOWN if urgent_only else self.AUTO_COOLDOWN
        if self.t - self._auto_changed < cooldown:
            return None
        history = self._auto_history
        if len(history) < self.AUTO_WINDOW:
            return None

        budget = 1000.0 / max(1, self.target_fps)
        median = sorted(history)[len(history) // 2]
        if urgent_only and median <= budget * self.AUTO_URGENT_AT:
            return None

        native_px = self._native_megapixels()
        here_px = native_px * self.quality() ** 2
        # `frame_ms` is shape construction only; the framework rasterises and
        # presents after the callback returns, which is the part that scales
        # with pixels.
        fixed = min(self.frame_ms, median * 0.8)
        per_px = max(0.0, median - fixed) / max(here_px, 0.01)

        best = self.AUTO_RUNGS[-1]
        for rung in self.AUTO_RUNGS:                    # sharpest first
            q = self._rung_quality(self.QUALITY_MODES[rung][1])
            if fixed + per_px * native_px * q * q <= budget * self.AUTO_HEADROOM:
                best = rung
                break
        # Rungs run sharpest-first, so easing off means a *higher* index.
        if urgent_only and best <= self.auto_index:
            return None
        return best if best != self.auto_index else None

    def _auto_settle(self, app, urgent_only=False):
        """Apply a pending AUTO change, where the re-bake hitch cannot be felt."""
        target = self._auto_target(urgent_only)
        if target is None:
            return
        previous = self.auto_index
        self.auto_index = target
        if self.apply_video(app):
            self.resize(app)
            self._auto_changed = self.t
            self._auto_history.clear()
            self.save['auto'] = self.auto_index
            save.save(self.save)
            if self.show_debug:
                name = self.QUALITY_MODES[target][0]
                sys.stderr.write(f'[lumen] auto -> {name} '
                                 f'{runtime.render_size()} at t={self.t:.1f}s '
                                 f'frame={self.frames}\n')
        else:
            self.auto_index = previous

    def cycle_display(self, app):
        """Step the sharpness dial, and remember where it was left."""
        if app is None:
            return
        self.display_index = (self.display_index + 1) % len(self.QUALITY_MODES)
        if self.apply_video(app):
            self.resize(app)
            self.save['display'] = self.display_index
            save.save(self.save)
        audio.play('ui_select', 0.5)

    def _sync_visuals(self):
        if self.world is not None:
            self.world.deferred = self.deferred
            self.world.volumetric = self.volumetric

    def visuals_label(self):
        return 'LIT' if self.deferred else 'CLASSIC'

    def toggle_visuals(self):
        """Swap between the light-buffer pipeline and the original look."""
        self.deferred = not self.deferred
        self._sync_visuals()
        self.save['visuals'] = 'lit' if self.deferred else 'classic'
        save.save(self.save)
        audio.play('ui_select', 0.5)

    def volumetric_label(self):
        if not self.deferred:
            return 'N/A'
        return 'ON' if self.volumetric else 'OFF'

    def toggle_volumetric(self):
        self.volumetric = not self.volumetric
        self._sync_visuals()
        self.save['volumetric'] = self.volumetric
        save.save(self.save)
        audio.play('ui_select', 0.5)

    def toggle_sound(self):
        self.sound_on = not self.sound_on
        self.save['sound'] = self.sound_on
        save.save(self.save)
        audio.bank().set_enabled(self.sound_on)
        if self.sound_on:
            audio.play('ui_select', 0.6)

    def quit(self):
        save.save(self.save)
        raise SystemExit(0)

    # --------------------------------------------------------------- draw --
    def draw(self, app):
        if gpu.wanted() and not gpu.active():
            # cmu-graphics redraws once at the end of onAppStart, which is
            # before the loop starts and so before the renderer exists - and
            # `App.run` rebuilds its own window after onAppStart anyway, so
            # the takeover cannot be brought forward. Skip that one frame
            # rather than draw it against the wrong backend.
            return
        started = time.perf_counter()

        if self.state == TITLE:
            self.title_screen.draw(self.sound_on, self.display_label(),
                                   self.visuals_label(),
                                   self.volumetric_label())
        elif self.state == HELP:
            self.help_screen.draw()
        elif self.state == ENDED:
            self.end_screen.draw()
        elif self.state in (PLAYING, PAUSED, DRAFT):
            self.world.draw(app)
            if self.state != DRAFT:
                # The draft takes over the screen entirely; pause keeps the
                # vitals but drops the callouts, which would otherwise bleed
                # through the panel.
                hud.draw(app, self.world, quiet=self.state == PAUSED)
            if self.state == PAUSED:
                screens.draw_pause(self.width, self.height, self.t,
                                   self.sound_on)
            elif self.state == DRAFT:
                self.draft_screen.draw(self.world)
            if self.state == PLAYING:
                self._draw_cursor()

        if self.state in (TITLE, HELP, ENDED, DRAFT, PAUSED):
            self._draw_cursor(menu=True)

        if self.fade > 0.001:
            drawPolygon(0, 0, self.width, 0, self.width, self.height,
                        0, self.height, fill=palette.VOID,
                        opacity=int(clamp(self.fade * 100, 0, 100)))

        self.frame_ms = (time.perf_counter() - started) * 1000.0
        if self.show_debug:
            self._draw_debug()

    def _draw_cursor(self, menu=False):
        mx, my = self.mouse
        charge = 0.0
        if not menu and self.world is not None:
            player = self.world.player
            if player.weapon.charge_time > 0.0:
                charge = clamp(player.charge / player.weapon.charge_time,
                               0.0, 1.0)
        color = palette.LIGHT_CORE if charge > 0.99 else palette.PLAYER_TRIM
        r = 11 - 3 * charge
        for a0 in (0.0, math.pi * 0.5, math.pi, math.pi * 1.5):
            ca, sa = math.cos(a0), math.sin(a0)
            drawPolygon(mx + ca * r, my + sa * r,
                        mx + ca * (r + 7) - sa * 1.2, my + sa * (r + 7) + ca * 1.2,
                        mx + ca * (r + 7) + sa * 1.2, my + sa * (r + 7) - ca * 1.2,
                        fill=color, opacity=78)
        drawPolygon(mx - 1.5, my - 1.5, mx + 1.5, my - 1.5, mx + 1.5, my + 1.5,
                    mx - 1.5, my + 1.5, fill=color, opacity=90)

    def _draw_debug(self):
        world = self.world
        lines = [f'draw {self.frame_ms:5.2f} ms']
        if world is not None:
            lines.append(f'particles {world.particles.live}')
            lines.append(f'enemies {len(world.enemies)}')
            lines.append(f'lightpts {len(world.light_poly)}')
        for i, line in enumerate(lines):
            drawLabel(line, self.width - 8, self.height - 74 + i * 15, size=11,
                      fill=palette.UI_GOOD, align='right',
                      font=palette.FONT_UI, opacity=85)
