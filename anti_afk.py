"""Anti-AFK input bot.

Usage:
    python anti_afk.py [--mode fast|slow] [--autojoin] [--anti-kick]

Hotkeys (work while the game has focus):
    F8  pause / resume (releases all held keys while paused)
    F9  stop (releases all held keys and exits)
"""
import argparse
import random
import threading

import pydirectinput
from pynput.keyboard import Controller, Key, Listener

# The mouse-corner failsafe misfires in games that lock the cursor; F9 replaces it.
pydirectinput.FAILSAFE = False

PAUSE_KEY = Key.f8
STOP_KEY = Key.f9

stop_event = threading.Event()
running = threading.Event()  # set = active, cleared = paused
running.set()

kb = Controller()


# --------------------------------------------------------------------------- #
# Input helpers
# --------------------------------------------------------------------------- #
def is_active():
    return running.is_set() and not stop_event.is_set()


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
    """Block while paused. Returns False once stopped."""
    while not running.is_set() and not stop_event.is_set():
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
    parser.add_argument('--mode', choices=['fast', 'slow'], default='fast')
    parser.add_argument('--autojoin', action='store_true', help="press F5 every 2s")
    parser.add_argument('--anti-kick', action='store_true', help="press 'l' every 20s")
    args = parser.parse_args()

    tasks = list(FAST_MODE if args.mode == 'fast' else SLOW_MODE)
    if args.autojoin:
        tasks.append(autojoin)
    if args.anti_kick:
        tasks.append(anti_kick)

    listener = Listener(on_press=on_hotkey)
    listener.start()

    for task in tasks:
        threading.Thread(target=task, daemon=True).start()

    print(f"Running {args.mode} mode. F8 = pause/resume, F9 = stop.")
    try:
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
