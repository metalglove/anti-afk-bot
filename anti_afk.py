"""Anti-AFK input bot.

Usage:
    python anti_afk.py [--mode human|fast|slow] [--game cod.exe] [--delay N]
                       [--autojoin] [--anti-kick] [--show-focus]

Hotkeys (work while the game has focus):
    F8  pause / resume (releases all held keys while paused)
    F9  stop (releases all held keys and exits)
"""
import argparse
import math
import random
import threading
import time

import pydirectinput
from pynput.keyboard import Controller, Key, Listener

# The mouse-corner failsafe misfires in games that lock the cursor; F9 replaces it.
pydirectinput.FAILSAFE = False

# Human mode: never stand still longer than this many seconds. Keep it well
# below the game's AFK-kick timer.
MAX_STILL = 20

# Human mode: set to True if the camera doesn't turn in-game (sends raw
# relative mouse input instead of moving the cursor position).
MOUSE_RELATIVE = False

PAUSE_KEY = Key.f8
STOP_KEY = Key.f9

stop_event = threading.Event()
running = threading.Event()  # set = active, cleared = paused
running.set()
focused = threading.Event()  # set = game window is in front (always set if no game given)
focused.set()

kb = Controller()


# --------------------------------------------------------------------------- #
# Input helpers
# --------------------------------------------------------------------------- #
def is_active():
    return running.is_set() and focused.is_set() and not stop_event.is_set()


# --------------------------------------------------------------------------- #
# Game focus detection (Windows)
# --------------------------------------------------------------------------- #
def foreground_window():
    """Return (exe name, window title) of the focused window; exe is '' if unavailable."""
    from ctypes import byref, create_unicode_buffer, windll, wintypes

    user32, kernel32 = windll.user32, windll.kernel32
    user32.GetForegroundWindow.restype = wintypes.HWND
    kernel32.OpenProcess.restype = wintypes.HANDLE

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return '', ''

    title = create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, title, len(title))

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, byref(pid))
    exe = ''
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        try:
            path = create_unicode_buffer(1024)
            size = wintypes.DWORD(len(path))
            if kernel32.QueryFullProcessImageNameW(handle, 0, path, byref(size)):
                exe = path.value.rsplit('\\', 1)[-1]
        finally:
            kernel32.CloseHandle(handle)
    # Some games pad their title with zero-width spaces; strip them so it can be matched.
    clean_title = ''.join(c for c in title.value if c not in '​‌‍﻿')
    return exe, clean_title


def is_admin():
    """True if this process runs elevated (as administrator)."""
    try:
        from ctypes import windll
        return bool(windll.shell32.IsUserAnAdmin())
    except (ImportError, AttributeError, OSError):
        return True  # not Windows / can't tell: don't warn


def game_is_focused(game):
    """True if the focused window belongs to `game`.

    Matches the exe name. Only when Windows won't give the exe (some protected
    games) does it fall back to the window title, because a terminal's title can
    contain the command line, including the --game value.
    """
    exe, title = foreground_window()
    game = game.lower()
    if exe:
        return exe.lower() == game or exe.lower() == game + '.exe'
    return bool(title) and game in title.lower()


def watch_focus(game):
    """Pause input whenever the game window isn't in front."""
    while not stop_event.is_set():
        if game_is_focused(game):
            if not focused.is_set():
                focused.set()
                print("Game focused, resuming.")
        elif focused.is_set():
            focused.clear()
            inputs.release_all()
            print(f"{game} not focused, waiting...")
        stop_event.wait(0.25)


def show_focus():
    """Print the focused program and window title whenever it changes (Ctrl+C to quit)."""
    print("Click around; the focused program is printed below. Ctrl+C to quit.")
    last = None
    try:
        while True:
            current = foreground_window()
            if current != last:
                exe, title = current
                print(f"exe: {exe or '(unavailable)':<30} title: {title}")
                last = current
            stop_event.wait(1)
    except KeyboardInterrupt:
        pass


class InputState:
    """Reference-counted key and mouse-button holds.

    Several threads press the same keys (space, w, c, right mouse). Counting
    holds means one thread releasing a key doesn't cut off another thread that
    is still holding it, and release_all() can always clear everything.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._keys = {}
        self._buttons = {}

    def press(self, key):
        with self._lock:
            if not is_active():
                return
            if self._keys.get(key, 0) == 0:
                kb.press(key)
            self._keys[key] = self._keys.get(key, 0) + 1

    def release(self, key):
        with self._lock:
            count = self._keys.get(key, 0)
            if count == 0:
                return  # already released (e.g. by pause/stop)
            if count == 1:
                kb.release(key)
                del self._keys[key]
            else:
                self._keys[key] = count - 1

    def mouse_down(self, button):
        with self._lock:
            if not is_active():
                return
            if self._buttons.get(button, 0) == 0:
                pydirectinput.mouseDown(button=button)
            self._buttons[button] = self._buttons.get(button, 0) + 1

    def mouse_up(self, button):
        with self._lock:
            count = self._buttons.get(button, 0)
            if count == 0:
                return
            if count == 1:
                pydirectinput.mouseUp(button=button)
                del self._buttons[button]
            else:
                self._buttons[button] = count - 1

    def release_all(self):
        with self._lock:
            for key in self._keys:
                kb.release(key)
            for button in self._buttons:
                pydirectinput.mouseUp(button=button)
            self._keys.clear()
            self._buttons.clear()


inputs = InputState()


def wait(seconds):
    """Sleep, waking early on stop. Returns False once stopped."""
    return not stop_event.wait(seconds)


def wait_until_active():
    """Block while paused or the game isn't focused. Returns False once stopped."""
    while not (running.is_set() and focused.is_set()) and not stop_event.is_set():
        stop_event.wait(0.2)
    return not stop_event.is_set()


def hold(keys, duration):
    for key in keys:
        inputs.press(key)
    try:
        wait(duration)
    finally:
        for key in keys:
            inputs.release(key)


def tap(key):
    hold([key], 0.05)


def hold_mouse(button, duration):
    inputs.mouse_down(button)
    try:
        wait(duration)
    finally:
        inputs.mouse_up(button)


def sweep(dx, dy, steps, step_duration):
    """Move the mouse in small relative steps, stopping early if paused/stopped."""
    for _ in range(steps):
        if not is_active():
            return
        pydirectinput.moveRel(dx, dy, duration=step_duration)
        wait(0.05)


# --------------------------------------------------------------------------- #
# Shared actions
# --------------------------------------------------------------------------- #
def weapon_switching():
    while wait_until_active():
        if not wait(random.uniform(25, 60)):
            return
        key = random.choice(['1', '2'])
        tap(key)
        if is_active():
            print(f"Switched weapon using key {key}")


# --------------------------------------------------------------------------- #
# Fast mode
# --------------------------------------------------------------------------- #
DIRECTIONS = [
    ('w',), ('s',), ('a',), ('d',),
    ('w', 'a'), ('w', 'd'), ('s', 'a'), ('s', 'd'),
]


def random_movement():
    # Initial forward step.
    if wait_until_active():
        print("Performing initial action: 'w' key press.")
        hold(['w'], 1.2)

    last_direction = None
    while wait_until_active():
        direction = random.choice([d for d in DIRECTIONS if d != last_direction])
        duration = random.uniform(0.05, 1.5)

        hold([Key.space], 1)

        # Half the time, swap a forward move for a slightly longer backward one.
        if 'w' in direction and random.random() < 0.5:
            direction = ('s',)
            duration += random.uniform(0.2, 0.5)

        hold(direction, duration)

        # After a pure backward move, step forward a bit longer to stay in place.
        if direction == ('s',):
            hold(['w'], duration + 0.1)

        last_direction = direction
        wait(random.uniform(0.5, 1))


def random_actions():
    while wait_until_active():
        if random.random() < 0.2:  # shoot (aim, then fire)
            inputs.mouse_down('right')
            try:
                wait(random.uniform(0.05, 0.1))
                hold_mouse('left', random.uniform(0.05, 0.1))
            finally:
                inputs.mouse_up('right')

        if random.random() < 0.15:  # jump
            hold([Key.space], random.uniform(0.1, 0.3))

        if random.random() < 0.1:  # short crouch (long hold would go prone)
            hold(['c'], random.uniform(0.1, 0.3))

        wait(random.uniform(1, 1.5))


def look_left_right():
    while wait_until_active():
        sweep(-100, 0, steps=10, step_duration=0.1)
        wait(random.uniform(1, 2))
        sweep(100, 0, steps=10, step_duration=0.1)
        wait(random.uniform(1, 2))


def look_up_down():
    while wait_until_active():
        sweep(0, -100, steps=10, step_duration=0.1)
        wait(random.uniform(1, 2))
        sweep(0, 100, steps=10, step_duration=0.1)
        wait(random.uniform(1, 2))


FAST_MODE = [random_movement, random_actions, look_left_right, look_up_down, weapon_switching]


# --------------------------------------------------------------------------- #
# Slow mode
# --------------------------------------------------------------------------- #
OPPOSITE_MOVES = {
    'w': 's', 's': 'w', 'a': 'd', 'd': 'a',
    'wd': 'sa', 'wa': 'sd', 'sd': 'wa', 'sa': 'wd',
}


def slow_wasd():
    """Move in a direction, then back the opposite way for the same time."""
    recent_moves = []
    recent_durations = []

    while wait_until_active():
        move = random.choice([m for m in OPPOSITE_MOVES if m not in recent_moves])
        recent_moves = (recent_moves + [move])[-2:]

        # Pick a duration not within 0.1s of either of the last two.
        duration = random.uniform(1, 2.5)
        while any(abs(duration - d) < 0.1 for d in recent_durations):
            duration = random.uniform(1, 2.5)
        recent_durations = (recent_durations + [duration])[-2:]

        hold(move, duration)
        wait(random.uniform(0.5, 1.5))
        hold(OPPOSITE_MOVES[move], duration)

        wait(random.uniform(2, 6))


def slow_look_up_down():
    while wait_until_active():
        sweep(0, -1000, steps=20, step_duration=1)
        wait(random.uniform(1, 2))
        sweep(0, 1000, steps=20, step_duration=1)
        wait(random.uniform(1, 2))


def crouch_randomly():
    while wait_until_active():
        if random.random() < 0.2:
            hold(['c'], random.uniform(0.6, 1.3))
        wait(random.uniform(4, 6))


def slow_aim():
    while wait_until_active():
        cycle = random.uniform(5, 8)
        hold_mouse('right', 2)
        remaining = cycle - 2
        if is_active():
            print(f"Aimed. Next aim in {remaining:.2f} seconds")
        wait(remaining)


def inspect_weapon():
    while wait_until_active():
        delay = random.uniform(3, 7)
        if not wait(delay):
            return
        hold(['i'], 0.5)
        if is_active():
            print(f"Inspected weapon after {delay:.2f} seconds")


SLOW_MODE = [slow_wasd, slow_look_up_down, crouch_randomly, weapon_switching, slow_aim, inspect_weapon]


# --------------------------------------------------------------------------- #
# Human mode
# --------------------------------------------------------------------------- #
def human_time(median, spread=0.5, low=0.0, high=float('inf')):
    """Right-skewed (log-normal) duration: usually near the median, sometimes much longer."""
    return min(high, max(low, random.lognormvariate(math.log(median), spread)))


def mouse_move(dx, dy):
    try:
        pydirectinput.moveRel(dx, dy, relative=MOUSE_RELATIVE, _pause=False)
    except TypeError:  # older pydirectinput without these options
        pydirectinput.moveRel(dx, dy)


def smooth_look(dx, dy, duration):
    """Move the view along an eased, slightly curved, slightly shaky path."""
    distance = math.hypot(dx, dy)
    if distance < 1:
        return
    steps = max(8, int(duration / 0.012))
    bow = random.uniform(-0.12, 0.12) * distance       # sideways curve of the path
    perp_x, perp_y = -dy / distance, dx / distance
    sent_x = sent_y = 0

    for i in range(1, steps + 1):
        if not is_active():
            return
        t = i / steps
        ease = t * t * (3 - 2 * t)                     # speed up, then slow down
        curve = math.sin(math.pi * t) * bow
        shake = 0.7 if i < steps else 0                # land exactly on target
        target_x = dx * ease + perp_x * curve + random.gauss(0, shake)
        target_y = dy * ease + perp_y * curve + random.gauss(0, shake)

        step_x, step_y = round(target_x) - sent_x, round(target_y) - sent_y
        if step_x or step_y:
            mouse_move(step_x, step_y)
            sent_x += step_x
            sent_y += step_y
        wait(duration / steps)


def human_look(dx, dy):
    """Turn by (dx, dy); on bigger turns, sometimes overshoot and correct."""
    distance = math.hypot(dx, dy)
    duration = human_time(0.15 + distance / 1500, 0.3, 0.12, 1.5)
    if distance > 150 and random.random() < 0.35:
        over = random.uniform(1.05, 1.15)
        smooth_look(dx * over, dy * over, duration)
        wait(human_time(0.08, 0.3, 0.03, 0.3))
        smooth_look(dx * (1 - over), dy * (1 - over), human_time(0.12, 0.3, 0.06, 0.3))
    else:
        smooth_look(dx, dy, duration)


class HumanPlayer:
    """One loop that does one thing at a time, like a person idling in-game."""

    def __init__(self):
        self.pitch = 0.0          # vertical offset from the start view, in mouse units
        self.energy = 1.0         # drifts slowly: >1 busier, <1 lazier
        self.last_move = time.monotonic()

    # -- helpers ------------------------------------------------------------
    def still_budget(self):
        """Seconds we can still stand still before we must move."""
        return max(0.0, MAX_STILL - 1 - (time.monotonic() - self.last_move))

    def look(self, dx, dy):
        dy -= self.pitch * random.uniform(0.2, 0.5)   # drift back toward the horizon
        self.pitch += dy
        human_look(dx, dy)

    # -- behaviours ---------------------------------------------------------
    def idle(self):
        wait(min(human_time(1.5, 0.7, 0.25, 10), self.still_budget()))

    def zone_out(self):
        wait(min(human_time(7.5, 0.5, 3, MAX_STILL), self.still_budget()))

    def glance(self):
        dx = random.choice([-1, 1]) * human_time(250, 0.6, 40, 900)
        self.look(dx, random.gauss(0, 60))
        if random.random() < 0.6:                     # look back roughly where we were
            wait(human_time(0.6, 0.5, 0.2, 3))
            self.look(-dx * random.uniform(0.6, 1.0), random.gauss(0, 30))

    def look_around(self):
        for _ in range(random.randint(2, 4)):
            self.look(random.uniform(-500, 500), random.gauss(0, 50))
            wait(human_time(0.5, 0.5, 0.15, 2))

    def walk(self):
        held = random.choice(DIRECTIONS)
        for key in held:
            inputs.press(key)
        try:
            if random.random() < 0.4:                 # look while walking
                self.look(random.uniform(-300, 300), random.gauss(0, 30))
            wait(human_time(0.5, 0.5, 0.15, 2.0))

            if random.random() < 0.3:                 # change direction with a short overlap
                nxt = random.choice([d for d in DIRECTIONS if d != held])
                for key in nxt:
                    inputs.press(key)
                wait(random.uniform(0.03, 0.08))
                for key in held:
                    inputs.release(key)
                held = nxt
                wait(human_time(0.4, 0.5, 0.1, 1.5))
        finally:
            for key in held:
                inputs.release(key)
        self.last_move = time.monotonic()

    def strafe(self):
        side = random.choice(['a', 'd'])
        for _ in range(random.randint(2, 3)):
            hold([side], human_time(0.25, 0.4, 0.1, 0.6))
            wait(human_time(0.08, 0.4, 0.03, 0.3))
            side = 'd' if side == 'a' else 'a'
        self.last_move = time.monotonic()

    def fidget(self):
        action = random.choice(['switch', 'switch_back', 'inspect', 'aim', 'shoot', 'click',
                                'crouch', 'jump'])
        if action == 'shoot':                         # aim, fire one shot, keep aiming a moment
            inputs.mouse_down('right')
            try:
                wait(human_time(0.35, 0.4, 0.15, 1.0))
                hold_mouse('left', human_time(0.09, 0.3, 0.04, 0.2))
                wait(human_time(0.3, 0.5, 0.05, 1.0))
            finally:
                inputs.mouse_up('right')
        elif action == 'click':                       # single click (also gets past menu screens)
            hold_mouse('left', human_time(0.09, 0.3, 0.04, 0.2))
        elif action == 'switch':
            hold([random.choice(['1', '2'])], human_time(0.1, 0.25, 0.05, 0.25))
        elif action == 'switch_back':
            hold(['2'], human_time(0.1, 0.25, 0.05, 0.25))
            wait(human_time(1.2, 0.5, 0.4, 4))
            hold(['1'], human_time(0.1, 0.25, 0.05, 0.25))
        elif action == 'inspect':
            hold(['i'], human_time(0.4, 0.3, 0.2, 0.8))
        elif action == 'aim':
            hold_mouse('right', human_time(0.8, 0.5, 0.3, 3))
        elif action == 'crouch':
            hold(['c'], human_time(0.25, 0.3, 0.12, 0.5))
        elif action == 'jump':
            hold([Key.space], random.uniform(0.08, 0.15))

    # -- main loop ----------------------------------------------------------
    def run(self):
        rest = [(self.idle, 30), (self.zone_out, 6)]
        active = [(self.glance, 25), (self.look_around, 10), (self.walk, 18),
                  (self.strafe, 5), (self.fidget, 8)]

        while wait_until_active():
            self.energy = min(1.6, max(0.5, self.energy + random.gauss(0, 0.05)))

            # Leave room for a non-moving action (a look around can take a few seconds).
            if self.still_budget() < 8:
                action = self.walk                    # don't get kicked
            else:
                options = ([(f, w / self.energy) for f, w in rest] +
                           [(f, w * self.energy) for f, w in active])
                action = random.choices([f for f, _ in options], [w for _, w in options])[0]

            action()
            wait(min(human_time(0.4 / self.energy, 0.6, 0.05, 3), self.still_budget()))


def human_player():
    HumanPlayer().run()


HUMAN_MODE = [human_player]


# --------------------------------------------------------------------------- #
# Optional extras
# --------------------------------------------------------------------------- #
def autojoin():
    """Press F5 every 2 seconds."""
    while wait_until_active():
        tap(Key.f5)
        print("Pressing F5")
        wait(2)


def anti_kick():
    """Press 'l' every 20 seconds."""
    while wait_until_active():
        tap('l')
        wait(20)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def on_hotkey(key):
    if key == STOP_KEY:
        print("Stopping...")
        stop_event.set()
        return False  # stops the listener
    if key == PAUSE_KEY:
        if running.is_set():
            running.clear()
            inputs.release_all()
            print("Paused (F8 to resume).")
        else:
            running.set()
            print("Resumed.")


def main():
    parser = argparse.ArgumentParser(description="Anti-AFK input bot.")
    parser.add_argument('--mode', choices=['human', 'fast', 'slow'], default='human')
    parser.add_argument('--autojoin', action='store_true', help="press F5 every 2s")
    parser.add_argument('--anti-kick', action='store_true', help="press 'l' every 20s")
    parser.add_argument('--delay', type=int, default=5,
                        help="seconds to wait before sending input (default 5)")
    parser.add_argument('--game', default='',
                        help="game exe name (e.g. cod.exe) or part of its window title; "
                             "input only goes out while it is focused")
    parser.add_argument('--show-focus', action='store_true',
                        help="just print the focused program/window title, to find the --game value")
    args = parser.parse_args()

    modes = {'fast': FAST_MODE, 'slow': SLOW_MODE, 'human': HUMAN_MODE}
    tasks = list(modes[args.mode])
    if args.autojoin:
        tasks.append(autojoin)
    if args.anti_kick:
        tasks.append(anti_kick)

    if args.show_focus:
        show_focus()
        return

    if not is_admin():
        print("Warning: not running as administrator. If the game runs as admin, it will ignore\n"
              "         this input and F8/F9 won't work while it's focused. If nothing happens\n"
              "         in-game, rerun from an administrator terminal.")

    if args.game:
        focused.clear()  # wait for the game before sending anything
        threading.Thread(target=watch_focus, args=(args.game,), daemon=True).start()
    else:
        print("Tip: pass --game <exe or window title> so input pauses when the game isn't in front.")

    listener = Listener(on_press=on_hotkey)
    listener.start()

    try:
        for remaining in range(args.delay, 0, -1):
            print(f"Starting in {remaining}s - switch to the game (F9 cancels)")
            if not wait(1):
                break

        if not stop_event.is_set():
            for task in tasks:
                threading.Thread(target=task, daemon=True).start()
            print(f"Running {args.mode} mode. F8 = pause/resume, F9 = stop.")

        while not stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        listener.stop()
        inputs.release_all()
        print("Stopped; all keys released.")


if __name__ == '__main__':
    main()
