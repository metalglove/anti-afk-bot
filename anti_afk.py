"""Anti-AFK input bot.

Usage:
    python anti_afk.py [--mode human|fast|slow] [--game cod.exe] [--delay N]
                       [--autojoin] [--anti-kick] [--show-focus]
                       [--ingame-pixel X,Y,RRGGBB] [--no-menu-check] [--show-menu]
                       [--learn NAME]

Hotkeys (work while the game has focus):
    F8  pause / resume (releases all held keys while paused)
    F9  stop (releases all held keys and exits)

Menu safety: input pauses whenever the game looks like it is in a menu (the
mouse cursor is showing, or the --ingame-pixel check fails), so the bot can't
click around the store. --show-menu prints what it detects.

Known screens: `--learn class_select` saves a piece of a menu screen (and
optionally a spot to click). While running, a menu that matches a learned
screen gets its spot clicked, e.g. to pick a class; every other menu stays
paused. Screens live in the screens/ folder next to this script.
"""
import argparse
import json
import math
import os
import random
import threading
import time
import traceback

import pydirectinput
from pynput.keyboard import Controller, Key, Listener

# The mouse-corner failsafe misfires in games that lock the cursor; F9 replaces it.
pydirectinput.FAILSAFE = False

# Use real pixel coordinates, so window rectangles and cursor positions agree on
# scaled displays (e.g. 125% / 150% in Windows display settings).
try:
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
except (ImportError, AttributeError, OSError):
    pass

# Human mode: never stand still longer than this many seconds (--max-still).
# Keep it well below the game's AFK-kick timer.
MAX_STILL = 10

# Human mode: always fire at least once within this many seconds.
MAX_NO_SHOT = 20

# Human mode: multiplier for how far the view turns (--look-scale). Raise it if
# turning looks sluggish at your in-game sensitivity, lower it if it spins.
LOOK_SCALE = 3.0

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
game_hwnd = None  # the game's window handle while it is focused (only with --game)
no_menu = threading.Event()  # set = the state machine says PLAYING (normal input allowed)
no_menu.set()

# Menu detection (see "Menu detection" below).
MENU_CHECK = True        # pause while the mouse cursor is showing (--no-menu-check turns off)
INGAME_PIXEL = None      # (x, y, (r, g, b)): pause unless this screen pixel has this color
PIXEL_TOLERANCE = 30     # max difference per color channel for the pixel to count as matching
MENU_RESUME_AFTER = 1.5  # seconds the menu must be gone before input resumes

kb = Controller()


# --------------------------------------------------------------------------- #
# Input helpers
# --------------------------------------------------------------------------- #
def is_active(menu_ok=False):
    """True if input may go out now. menu_ok=True also allows it while a menu is open."""
    return (running.is_set() and focused.is_set() and (menu_ok or no_menu.is_set())
            and not stop_event.is_set())


# --------------------------------------------------------------------------- #
# Game focus detection (Windows)
# --------------------------------------------------------------------------- #
def foreground_window():
    """Return (exe name, window title) of the focused window; exe is '' if unavailable."""
    from ctypes import WinDLL, byref, create_unicode_buffer, wintypes

    # Private handles: don't change the shared windll objects pydirectinput uses.
    user32, kernel32 = WinDLL('user32'), WinDLL('kernel32')
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
    global game_hwnd
    while not stop_event.is_set():
        if game_is_focused(game):
            game_hwnd = _user32().GetForegroundWindow()
            if not focused.is_set():
                focused.set()
        elif focused.is_set():
            focused.clear()
            game_hwnd = None
            inputs.release_all()
        stop_event.wait(0.25)


# --------------------------------------------------------------------------- #
# Keeping the mouse inside the game window (Windows)
# --------------------------------------------------------------------------- #
EDGE_MARGIN = 8  # pixels kept clear of the window edges

_user32_cache = None


def _user32():
    """A private user32 handle. Setting argtypes on the shared ctypes.windll.user32
    would break pydirectinput, which calls the same functions with its own types."""
    global _user32_cache
    if _user32_cache is None:
        from ctypes import POINTER, WinDLL, wintypes
        u = WinDLL('user32')
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetClientRect.argtypes = [wintypes.HWND, POINTER(wintypes.RECT)]
        u.ClientToScreen.argtypes = [wintypes.HWND, POINTER(wintypes.POINT)]
        u.GetCursorPos.argtypes = [POINTER(wintypes.POINT)]
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.WindowFromPoint.restype = wintypes.HWND
        u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetAncestor.restype = wintypes.HWND
        u.GetCursorInfo.argtypes = [POINTER(CURSORINFO)]
        u.GetDC.argtypes = [wintypes.HWND]
        u.GetDC.restype = wintypes.HDC
        u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        _user32_cache = u
    return _user32_cache


def window_client_rect(hwnd=None):
    """(left, top, width, height) on screen of a window's client area (default: the
    game window, else the focused window), or of the primary screen if unknown."""
    from ctypes import byref, wintypes
    u = _user32()
    hwnd = hwnd or game_hwnd or u.GetForegroundWindow()
    rect = wintypes.RECT()
    origin = wintypes.POINT(0, 0)
    if hwnd and u.GetClientRect(hwnd, byref(rect)) and u.ClientToScreen(hwnd, byref(origin)) \
            and rect.right > 0 and rect.bottom > 0:
        return origin.x, origin.y, rect.right, rect.bottom
    return 0, 0, u.GetSystemMetrics(0), u.GetSystemMetrics(1)  # SM_CXSCREEN, SM_CYSCREEN


def game_area():
    """Screen rectangle (left, top, right, bottom) of the game's client area, inset
    by EDGE_MARGIN, or None if unknown."""
    from ctypes import byref, wintypes
    hwnd = game_hwnd
    if not hwnd:
        return None
    u = _user32()
    rect = wintypes.RECT()
    origin = wintypes.POINT(0, 0)
    if not u.GetClientRect(hwnd, byref(rect)) or not u.ClientToScreen(hwnd, byref(origin)):
        return None
    left, top = origin.x + EDGE_MARGIN, origin.y + EDGE_MARGIN
    right = origin.x + rect.right - 1 - EDGE_MARGIN
    bottom = origin.y + rect.bottom - 1 - EDGE_MARGIN
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


_gdi32_cache = None


def _gdi32():
    global _gdi32_cache
    if _gdi32_cache is None:
        from ctypes import WinDLL, c_int, wintypes
        g = WinDLL('gdi32')
        g.GetPixel.argtypes = [wintypes.HDC, c_int, c_int]
        g.GetPixel.restype = wintypes.DWORD
        _gdi32_cache = g
    return _gdi32_cache


try:
    from ctypes import Structure, wintypes as _wt

    class CURSORINFO(Structure):
        _fields_ = [('cbSize', _wt.DWORD), ('flags', _wt.DWORD),
                    ('hCursor', _wt.HANDLE), ('ptScreenPos', _wt.POINT)]
except (ImportError, ValueError):  # wintypes is Windows-only on some Pythons
    CURSORINFO = None


def cursor_pos():
    from ctypes import byref, wintypes
    point = wintypes.POINT()
    _user32().GetCursorPos(byref(point))
    return point


def cursor_in_game():
    """True if a click now would land on the game window (always True without --game)."""
    if not game_hwnd:
        return True
    area = game_area()
    if area is None:
        return False
    point = cursor_pos()
    left, top, right, bottom = area
    if not (left <= point.x <= right and top <= point.y <= bottom):
        return False
    # Also make sure no other window (overlay, always-on-top app) covers that spot.
    u = _user32()
    under = u.WindowFromPoint(point)
    return bool(under) and u.GetAncestor(under, 2) == game_hwnd  # 2 = GA_ROOT


def clamp_to_game(dx, dy):
    """Shrink a cursor move so it ends inside the game window."""
    if MOUSE_RELATIVE or not game_hwnd:
        return dx, dy  # raw relative input doesn't move the cursor
    area = game_area()
    if area is None:
        return dx, dy
    left, top, right, bottom = area
    point = cursor_pos()
    x = min(max(point.x + dx, left), right)
    y = min(max(point.y + dy, top), bottom)
    return x - point.x, y - point.y


# --------------------------------------------------------------------------- #
# Menu detection (Windows)
# --------------------------------------------------------------------------- #
# In first-person games the mouse cursor is hidden while you play and shown in
# menus, the lobby and the store. So "cursor showing" is a good sign we must not
# click or press anything. Games that draw their own menu cursor (and keep the
# Windows one hidden) won't be caught by this; for those, --ingame-pixel checks a
# spot of the HUD that is only there while playing.
def cursor_showing():
    """True if Windows is showing the mouse cursor."""
    from ctypes import byref, sizeof
    info = CURSORINFO()
    info.cbSize = sizeof(CURSORINFO)
    if not _user32().GetCursorInfo(byref(info)):
        return False
    return bool(info.flags & 1) and bool(info.hCursor)  # 1 = CURSOR_SHOWING


def screen_pixel(x, y):
    """(r, g, b) of a screen pixel, or None if Windows can't read it."""
    u = _user32()
    hdc = u.GetDC(None)
    if not hdc:
        return None
    try:
        color = _gdi32().GetPixel(hdc, x, y)
    finally:
        u.ReleaseDC(None, hdc)
    if color == 0xFFFFFFFF:  # CLR_INVALID (off screen, or exclusive fullscreen)
        return None
    return color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF


def pixel_matches():
    """True if the --ingame-pixel spot has the expected color (True if not set)."""
    if INGAME_PIXEL is None or INGAME_PIXEL[2] is None:
        return True
    x, y, expected = INGAME_PIXEL
    actual = screen_pixel(x, y)
    return actual is not None and all(abs(a - e) <= PIXEL_TOLERANCE
                                      for a, e in zip(actual, expected))


def menu_reason():
    """Why the game looks like it's in a menu, or '' if it looks like gameplay."""
    if MENU_CHECK and cursor_showing():
        return "mouse cursor is showing"
    if not pixel_matches():
        return "--ingame-pixel doesn't match"
    return ''


# --------------------------------------------------------------------------- #
# Known screens (class selection etc.)
# --------------------------------------------------------------------------- #
# A learned screen is a crop of the game window (screens/NAME.png) plus where it
# was taken (screens/NAME.json). Positions are stored as fractions of the game
# window's client area ({"box": [l, t, r, b], "click": [x, y] or null, all 0-1}),
# so the window can be moved or resized: the box is found at the same relative
# spot and the crop is scaled to its current size before comparing.
SCREENS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'screens')
MATCH_THRESHOLD = 25     # max average color difference (0-255) for a screen to match
SCREEN_CLICK_DELAY = (1.0, 2.5)   # seconds to "read" a screen before clicking it
SCREEN_CLICK_COOLDOWN = 4.0       # seconds before clicking the same screen again

screens = []  # [{'name', 'box', 'click', 'image'}], box/click as window fractions


def to_screen(fx, fy, rect=None):
    """Window fraction -> screen pixel."""
    left, top, width, height = rect or window_client_rect()
    return round(left + fx * width), round(top + fy * height)


def to_fraction(x, y, rect):
    left, top, width, height = rect
    return (x - left) / width, (y - top) / height


def grab(box):
    """Screenshot of a screen rectangle (left, top, right, bottom), or None."""
    try:
        from PIL import ImageGrab
        return ImageGrab.grab(bbox=tuple(box)).convert('RGB')
    except Exception:  # Pillow missing, or the screen can't be read (exclusive fullscreen)
        return None


def screen_difference(screen):
    """Average color difference (0-255) between the screen now and a learned screen."""
    from PIL import ImageChops, ImageStat
    from PIL import Image
    rect = window_client_rect()
    l, t, r, b = screen['box']
    left, top = to_screen(l, t, rect)
    right, bottom = to_screen(r, b, rect)
    if right - left < 4 or bottom - top < 4:
        return 255.0
    now = grab((left, top, right, bottom))
    if now is None:
        return 255.0
    saved = screen['image']
    if saved.size != now.size:
        saved = saved.resize(now.size, Image.BILINEAR)
    diff = ImageStat.Stat(ImageChops.difference(now, saved)).mean
    return sum(diff) / len(diff)


def load_screens():
    """Load every learned screen from SCREENS_DIR."""
    found = []
    if not os.path.isdir(SCREENS_DIR):
        return found
    try:
        from PIL import Image
    except ImportError:
        print("Warning: learned screens need Pillow (pip install -r requirements.txt); ignoring them.")
        return found
    for file in sorted(os.listdir(SCREENS_DIR)):
        if not file.endswith('.json'):
            continue
        name = file[:-5]
        try:
            with open(os.path.join(SCREENS_DIR, file)) as f:
                info = json.load(f)
            image = Image.open(os.path.join(SCREENS_DIR, name + '.png')).convert('RGB')
            found.append({'name': name, 'box': info['box'], 'click': info.get('click'),
                          'image': image})
        except (OSError, ValueError, KeyError) as e:
            print(f"Warning: can't load screen '{name}': {e}")
    return found


def matching_screen():
    """(learned screen, difference) that best matches the screen now, or (None, best diff)."""
    best, best_diff = None, 255.0
    for screen in screens:
        diff = screen_difference(screen)
        if diff < best_diff:
            best, best_diff = screen, diff
    if best is not None and best_diff <= MATCH_THRESHOLD:
        return best, best_diff
    return None, best_diff


def learn_screen(name):
    """Save part of the current screen as a known screen, using F7 at the cursor."""
    from pynput.keyboard import Listener as KeyListener
    if not name.replace('_', '').replace('-', '').isalnum():
        print("Use a simple name, e.g. class_select.")
        return
    os.makedirs(SCREENS_DIR, exist_ok=True)
    json_path = os.path.join(SCREENS_DIR, name + '.json')
    points = []
    state = {}  # 'rect': the game window's client area when the box was taken
    done = threading.Event()

    print(f"Learning screen '{name}'. Open that screen in the game, then:\n"
          "  1. Point the mouse at the top-left corner of something that is only on this\n"
          "     screen and doesn't change (a title like 'CHOOSE CLASS'), press F7.\n"
          "  2. Point at its bottom-right corner, press F7.\n"
          "  3. Optional: point at what the bot should click here, press F7.\n"
          "Press F9 when done. Keep the mouse out of the box from steps 1 and 2.")

    def on_press(key):
        if key == STOP_KEY:
            done.set()
            return False
        if key != Key.f7:
            return None
        point = cursor_pos()
        points.append((point.x, point.y))
        if len(points) == 1:
            state['rect'] = window_client_rect(_user32().GetForegroundWindow())
            print(f"Top-left: {point.x},{point.y}")
        elif len(points) == 2:
            (x1, y1), (x2, y2) = points
            box = [min(x1, x2), min(y1, y2), max(x1, x2) + 1, max(y1, y2) + 1]
            if box[2] - box[0] < 8 or box[3] - box[1] < 8:
                print("That box is too small; start again with the top-left corner.")
                points.clear()
                return None
            image = grab(box)
            if image is None:
                print("Couldn't read the screen. Use borderless or windowed mode.")
                points.clear()
                return None
            rect = state['rect']
            rel_box = [*to_fraction(box[0], box[1], rect), *to_fraction(box[2], box[3], rect)]
            if not all(0 <= v <= 1 for v in rel_box):
                print("That box isn't inside the game window; start again.")
                points.clear()
                return None
            image.save(os.path.join(SCREENS_DIR, name + '.png'))
            with open(json_path, 'w') as f:
                json.dump({'box': rel_box, 'click': None, 'window': list(rect[2:])}, f)
            print(f"Saved box {box} (game window {rect[2]}x{rect[3]}). "
                  "Press F7 on a spot to click, or F9 to finish.")
        elif len(points) == 3:
            with open(json_path) as f:
                info = json.load(f)
            info['click'] = list(to_fraction(point.x, point.y, state['rect']))
            with open(json_path, 'w') as f:
                json.dump(info, f)
            print(f"The bot will click {point.x},{point.y} on this screen. Done.")
            done.set()
            return False
        return None

    with KeyListener(on_press=on_press):
        try:
            done.wait()
        except KeyboardInterrupt:
            pass
    if len(points) >= 2:
        print(f"Saved to {SCREENS_DIR}. Check it with --show-menu.")
    else:
        print("Nothing saved.")


def click_screen(screen):
    """Click a learned screen's click spot, like a person would."""
    if not wait(random.uniform(*SCREEN_CLICK_DELAY)):
        return
    if not (running.is_set() and focused.is_set()):
        return
    current, _ = matching_screen()
    if current is not screen:
        return  # the screen changed while we waited
    x, y = to_screen(*screen['click'])
    inputs.click_at(x, y)
    print(f"Clicked {x},{y} on screen '{screen['name']}'.")


# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #
# One thread decides which state the bot is in, ten times a second, and only the
# PLAYING state lets normal input out. Every change is printed with the time and
# how long the previous state lasted, and the terminal title shows the state.
#
#   STARTING      countdown before the bot begins
#   PAUSED        you pressed F8
#   NOT FOCUSED   the game isn't the window in front (only with --game)
#   PLAYING       in a match: all actions run
#   RESUMING      a menu just closed; waits MENU_RESUME_AFTER before PLAYING
#   SCREEN name   a learned screen (e.g. class_select): clicks its spot, if it has one
#   MENU          a menu it doesn't know (store, settings...): nothing is sent
STARTING, PAUSED, NOT_FOCUSED, PLAYING, RESUMING, SCREEN, MENU = (
    'STARTING', 'PAUSED', 'NOT FOCUSED', 'PLAYING', 'RESUMING', 'SCREEN', 'MENU')
UNKNOWN_MENU_REMINDER = 300  # seconds in an unknown menu before suggesting --learn

state_lock = threading.Lock()
state = {'name': STARTING, 'detail': '', 'since': time.monotonic()}
started = threading.Event()  # set once the countdown is over


def detect(last_screen=None):
    """Raw state from what's on screen now: (state, detail, learned screen or None).
    No delays applied; StateMachine adds those."""
    if not started.is_set():
        return STARTING, '', None
    if not running.is_set():
        return PAUSED, 'F8 to resume', None
    if not focused.is_set():
        return NOT_FOCUSED, 'waiting for the game window', None
    reason = menu_reason()
    if not reason:
        return PLAYING, '', None
    screen = last_screen if last_screen is not None else (matching_screen()[0] if screens else None)
    if screen:
        action = 'clicking' if screen['click'] else 'waiting'
        return SCREEN, f"{screen['name']} ({action})", screen
    return MENU, f"unknown, {reason}, nothing sent", None


def set_console_title(text):
    try:
        from ctypes import WinDLL
        WinDLL('kernel32').SetConsoleTitleW(text)
    except (ImportError, AttributeError, OSError):
        pass


def format_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"


def current_state():
    with state_lock:
        return state['name'], state['detail'], time.monotonic() - state['since']


def enter_state(name, detail=''):
    """Switch state, gate input, and print the change."""
    with state_lock:
        if state['name'] == name and state['detail'] == detail:
            return
        previous, lasted = state['name'], time.monotonic() - state['since']
        same_name = previous == name
        state.update(name=name, detail=detail)
        if not same_name:
            state['since'] = time.monotonic()
    if name == PLAYING:
        no_menu.set()
    elif no_menu.is_set():
        no_menu.clear()
        inputs.release_all()
    label = f"{name}: {detail}" if detail else name
    was = '' if same_name else f"   (after {format_duration(lasted)} {previous})"
    print(f"[{time.strftime('%H:%M:%S')}] {label}{was}")
    set_console_title(f"anti-afk: {label}")


def run_state_machine():
    """Track the state and act on it: release input outside PLAYING, wait a moment
    before resuming after a menu, and click learned screens."""
    resume_since = None
    last_clicked = {}            # screen name -> time
    screen, next_screen_check = None, 0.0
    last_reminder = time.monotonic()
    while not stop_event.is_set():
        now = time.monotonic()
        if not (screens and focused.is_set() and menu_reason()):
            screen, next_screen_check = None, 0.0     # check right away when a menu opens
        elif now >= next_screen_check:  # screenshots are slower; refresh 3x a second
            next_screen_check = now + 0.3
            screen = matching_screen()[0]
        name, detail, screen_now = detect(screen or False)
        current, _, lasted = current_state()

        if name == PLAYING and current not in (PLAYING, STARTING) and MENU_RESUME_AFTER > 0:
            # Back from a menu: give it a moment in case the menu flickers back.
            resume_since = resume_since or now
            if now - resume_since < MENU_RESUME_AFTER:
                name, detail = RESUMING, f"playing in {MENU_RESUME_AFTER:g}s"
            else:
                resume_since = None
        else:
            resume_since = None
        enter_state(name, detail)

        if name == SCREEN and screen_now['click'] and \
                now - last_clicked.get(screen_now['name'], 0) >= SCREEN_CLICK_COOLDOWN:
            click_screen(screen_now)
            last_clicked[screen_now['name']] = time.monotonic()
        elif name == MENU and lasted > UNKNOWN_MENU_REMINDER and \
                now - last_reminder > UNKNOWN_MENU_REMINDER:
            last_reminder = now
            print(f"   Still in an unknown menu after {format_duration(lasted)}. If this screen "
                  "needs a click, teach it with --learn.")
        stop_event.wait(0.1)


def show_menu():
    """Print what menu detection sees whenever it changes (Ctrl+C to quit)."""
    print("Open and close menus in the game; what the bot sees is printed below. Ctrl+C to quit.")
    started.set()
    last = None
    try:
        while True:
            parts = [f"cursor showing: {'yes' if cursor_showing() else 'no '}"]
            if INGAME_PIXEL is not None:
                x, y, _ = INGAME_PIXEL
                rgb = screen_pixel(x, y)
                color = '%02X%02X%02X' % rgb if rgb else 'unreadable'
                parts.append(f"pixel {x},{y}: {color}")
            screen = None
            if screens:
                screen, diff = matching_screen()
                parts.append(f"screen: {screen['name'] if screen else 'unknown'} "
                             f"(diff {diff:.0f}, match <= {MATCH_THRESHOLD})")
            name, detail, _ = detect(screen or False)
            parts.append(f"=> {name}" + (f": {detail}" if detail else ''))
            line = '   '.join(parts)
            if line != last:
                print(line)
                last = line
            stop_event.wait(0.5)
    except KeyboardInterrupt:
        pass


def parse_pixel(text, need_color):
    """Parse 'X,Y' or 'X,Y,RRGGBB' into (x, y, (r, g, b) or None)."""
    parts = [p.strip() for p in text.split(',')]
    if len(parts) not in (2, 3) or (need_color and len(parts) != 3):
        raise argparse.ArgumentTypeError("use X,Y,RRGGBB, e.g. 120,64,FFFFFF "
                                         "(run --show-menu --ingame-pixel X,Y to read the color)")
    x, y = int(parts[0]), int(parts[1])
    color = None
    if len(parts) == 3:
        h = parts[2].lstrip('#')
        color = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    return x, y, color


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

    def press(self, key, menu_ok=False):
        with self._lock:
            if not is_active(menu_ok):
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
            if not is_active() or not cursor_in_game() or menu_reason():
                return  # never click outside the game window or in a menu
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

    def click_at(self, x, y):
        """Move to a screen spot and click it. Only for learned screens, which are menus,
        so it skips the menu check but still needs the bot running and the game in front."""
        with self._lock:
            if not (running.is_set() and focused.is_set()) or stop_event.is_set():
                return
            pydirectinput.moveTo(x, y)
            time.sleep(random.uniform(0.08, 0.2))
            pydirectinput.mouseDown(button='left')
            time.sleep(random.uniform(0.05, 0.1))
            pydirectinput.mouseUp(button='left')

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


def wait_until_active(menu_ok=False):
    """Block while paused, the game isn't focused, or (unless menu_ok) a menu is open.
    Returns False once stopped."""
    while not is_active(menu_ok) and not stop_event.is_set():
        stop_event.wait(0.2)
    return not stop_event.is_set()


def hold(keys, duration, menu_ok=False):
    for key in keys:
        inputs.press(key, menu_ok)
    try:
        wait(duration)
    finally:
        for key in keys:
            inputs.release(key)


def tap(key, menu_ok=False):
    hold([key], 0.05, menu_ok)


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
        mouse_move(dx, dy)
        wait(0.15)  # pydirectinput ignores step_duration; 0.15s matches the old pacing


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
    """Move the mouse by (dx, dy), kept inside the game window when --game is set."""
    if not is_active():
        return  # don't drag the cursor across menu buttons
    dx, dy = clamp_to_game(dx, dy)
    if not (dx or dy):
        return
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
    """Turn by (dx, dy) * LOOK_SCALE; on bigger turns, sometimes overshoot and correct.

    The duration is based on the unscaled distance, so a higher LOOK_SCALE turns
    further in the same time instead of turning slower.
    """
    distance = math.hypot(dx, dy)
    duration = human_time(0.15 + distance / 1500, 0.3, 0.12, 1.5)
    dx, dy = dx * LOOK_SCALE, dy * LOOK_SCALE
    if distance > 150 and random.random() < 0.35:
        over = random.uniform(1.05, 1.15)
        smooth_look(dx * over, dy * over, duration)
        wait(human_time(0.08, 0.3, 0.03, 0.3))
        smooth_look(dx * (1 - over), dy * (1 - over), human_time(0.12, 0.3, 0.06, 0.3))
    else:
        smooth_look(dx, dy, duration)


def fire_once():
    hold_mouse('left', human_time(0.09, 0.3, 0.04, 0.2))


class HumanPlayer:
    """One loop that does one thing at a time, like a person idling in-game."""

    def __init__(self):
        self.pitch = 0.0          # vertical offset from the start view (unscaled units)
        self.energy = 1.0         # drifts slowly: >1 busier, <1 lazier
        self.last_move = time.monotonic()
        self.last_shot = time.monotonic()

    # -- helpers ------------------------------------------------------------
    def still_budget(self):
        """Seconds we can still stand still before we must move."""
        return max(0.0, MAX_STILL - 1 - (time.monotonic() - self.last_move))

    def reserve(self):
        """Budget at which we stop starting non-moving actions and walk instead."""
        return min(7.0, MAX_STILL * 0.7)

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
            wait(min(human_time(0.6, 0.5, 0.2, 3), self.still_budget()))
            self.look(-dx * random.uniform(0.6, 1.0), random.gauss(0, 30))

    def look_around(self):
        for _ in range(random.randint(2, 4)):
            if self.still_budget() < 2:
                return
            self.look(random.uniform(-500, 500), random.gauss(0, 50))
            wait(min(human_time(0.5, 0.5, 0.15, 2), self.still_budget()))

    def walk(self):
        held = random.choice(DIRECTIONS)
        for key in held:
            inputs.press(key)
        try:
            if random.random() < 0.4:                 # look while walking
                self.look(random.uniform(-300, 300), random.gauss(0, 30))
            shot_due = time.monotonic() - self.last_shot > MAX_NO_SHOT - 12
            if shot_due or random.random() < 0.25:    # hip-fire while walking
                wait(human_time(0.3, 0.5, 0.1, 1.0))
                fire_once()
                self.last_shot = time.monotonic()
            wait(human_time(1.0, 0.5, 0.3, 3.0))

            if random.random() < 0.3:                 # jump while walking
                hold([Key.space], random.uniform(0.08, 0.18))
                wait(human_time(0.5, 0.4, 0.2, 1.2))

            if random.random() < 0.35:               # change direction with a short overlap
                nxt = random.choice([d for d in DIRECTIONS if d != held])
                for key in nxt:
                    inputs.press(key)
                wait(random.uniform(0.03, 0.08))
                for key in held:
                    inputs.release(key)
                held = nxt
                wait(human_time(0.7, 0.5, 0.2, 2.0))
        finally:
            for key in held:
                inputs.release(key)
        self.last_move = time.monotonic()

    def strafe(self):
        side = random.choice(['a', 'd'])
        for _ in range(random.randint(2, 4)):
            hold([side], human_time(0.35, 0.4, 0.12, 0.9))
            wait(human_time(0.08, 0.4, 0.03, 0.3))
            side = 'd' if side == 'a' else 'a'
        self.last_move = time.monotonic()

    def jump(self):
        """One jump, or a quick double hop, sometimes moving forward with it."""
        forward = random.random() < 0.5
        if forward:
            inputs.press('w')
        try:
            for i in range(random.choice([1, 1, 2])):
                if i:
                    wait(human_time(0.6, 0.3, 0.4, 1.0))  # land before jumping again
                hold([Key.space], random.uniform(0.08, 0.18))
            wait(human_time(0.4, 0.4, 0.2, 0.9))
        finally:
            if forward:
                inputs.release('w')
        if forward:
            self.last_move = time.monotonic()

    def shoot(self):
        """Fire 1-3 shots, usually aimed, sometimes nudging the aim between them."""
        aimed = random.random() < 0.7
        if aimed:
            inputs.mouse_down('right')
        try:
            if aimed:
                wait(human_time(0.3, 0.4, 0.12, 0.8))
            for i in range(random.choice([1, 1, 2, 3])):
                if i and self.still_budget() < 2:
                    break
                if i:
                    if random.random() < 0.5:
                        human_look(random.gauss(0, 25), random.gauss(0, 10))
                    wait(human_time(0.25, 0.4, 0.1, 0.6))
                fire_once()
                self.last_shot = time.monotonic()
            wait(human_time(0.3, 0.5, 0.05, 1.0))
        finally:
            if aimed:
                inputs.mouse_up('right')
        self.last_shot = time.monotonic()

    def fidget(self):
        action = random.choice(['switch', 'switch_back', 'inspect', 'aim', 'click',
                                'crouch', 'jump'])
        if action == 'click':                         # single click (never in menus)
            fire_once()
        elif action == 'switch':
            hold([random.choice(['1', '2'])], human_time(0.1, 0.25, 0.05, 0.25))
        elif action == 'switch_back':
            hold(['2'], human_time(0.1, 0.25, 0.05, 0.25))
            wait(min(human_time(1.2, 0.5, 0.4, 4), self.still_budget()))
            hold(['1'], human_time(0.1, 0.25, 0.05, 0.25))
        elif action == 'inspect':
            hold(['i'], human_time(0.4, 0.3, 0.2, 0.8))
        elif action == 'aim':
            hold_mouse('right', min(human_time(0.8, 0.5, 0.3, 3), self.still_budget()))
        elif action == 'crouch':
            hold(['c'], human_time(0.25, 0.3, 0.12, 0.5))
        elif action == 'jump':
            hold([Key.space], random.uniform(0.08, 0.15))

    # -- main loop ----------------------------------------------------------
    def run(self):
        rest = [(self.idle, 15), (self.zone_out, 2)]
        active = [(self.walk, 30), (self.glance, 18), (self.shoot, 12), (self.look_around, 8),
                  (self.strafe, 8), (self.jump, 8), (self.fidget, 7)]

        while wait_until_active():
            self.energy = min(1.6, max(0.5, self.energy + random.gauss(0, 0.05)))

            # Hard limits first so the game never sees us idle for too long.
            if self.still_budget() < self.reserve():
                action = self.walk
            elif time.monotonic() - self.last_shot > MAX_NO_SHOT - 10:
                action = self.shoot
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
    """Press F5 every 2 seconds. Keeps going in menus, since joining happens there."""
    while wait_until_active(menu_ok=True):
        tap(Key.f5, menu_ok=True)
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
def run_task(task):
    """Run one action loop; if it crashes, stop everything instead of idling silently."""
    try:
        task()
    except Exception:
        traceback.print_exc()
        print(f"{task.__name__} crashed - stopping so you notice (you'd get kicked otherwise).")
        stop_event.set()


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
    global LOOK_SCALE, MAX_STILL, MENU_CHECK, INGAME_PIXEL, PIXEL_TOLERANCE, MATCH_THRESHOLD, screens
    parser = argparse.ArgumentParser(description="Anti-AFK input bot.")
    parser.add_argument('--mode', choices=['human', 'fast', 'slow'], default='human')
    parser.add_argument('--autojoin', action='store_true', help="press F5 every 2s")
    parser.add_argument('--anti-kick', action='store_true', help="press 'l' every 20s")
    parser.add_argument('--delay', type=int, default=5,
                        help="seconds to wait before sending input (default 5)")
    parser.add_argument('--game', default='',
                        help="game exe name (e.g. cod.exe) or part of its window title; "
                             "input only goes out while it is focused")
    parser.add_argument('--look-scale', type=float, default=LOOK_SCALE,
                        help=f"human mode: how far turns go (default {LOOK_SCALE})")
    parser.add_argument('--max-still', type=float, default=MAX_STILL,
                        help=f"human mode: max seconds without moving (default {MAX_STILL})")
    parser.add_argument('--show-focus', action='store_true',
                        help="just print the focused program/window title, to find the --game value")
    parser.add_argument('--no-menu-check', action='store_true',
                        help="don't pause while the mouse cursor is showing (menus, store)")
    parser.add_argument('--ingame-pixel', metavar='X,Y,RRGGBB',
                        help="only send input while this screen pixel has this color "
                             "(pick a HUD spot that is only there while playing)")
    parser.add_argument('--pixel-tolerance', type=int, default=PIXEL_TOLERANCE,
                        help=f"max color difference per channel for --ingame-pixel "
                             f"(default {PIXEL_TOLERANCE})")
    parser.add_argument('--show-menu', action='store_true',
                        help="just print what menu detection sees (cursor, pixel, known screen)")
    parser.add_argument('--learn', metavar='NAME',
                        help="save the current game screen as a known screen (e.g. class_select), "
                             "optionally with a spot to click there")
    parser.add_argument('--match-threshold', type=float, default=MATCH_THRESHOLD,
                        help=f"how different a learned screen may look and still match "
                             f"(default {MATCH_THRESHOLD})")
    args = parser.parse_args()

    LOOK_SCALE, MAX_STILL = args.look_scale, args.max_still
    MENU_CHECK, PIXEL_TOLERANCE = not args.no_menu_check, args.pixel_tolerance
    MATCH_THRESHOLD = args.match_threshold
    if args.ingame_pixel:
        try:
            x, y, color = parse_pixel(args.ingame_pixel, need_color=not args.show_menu)
        except (argparse.ArgumentTypeError, ValueError, IndexError) as e:
            parser.error(f"--ingame-pixel: {e}")
        INGAME_PIXEL = (x, y, color)  # color is None only with --show-menu: just print it

    modes = {'fast': FAST_MODE, 'slow': SLOW_MODE, 'human': HUMAN_MODE}
    tasks = list(modes[args.mode])
    if args.autojoin:
        tasks.append(autojoin)
    if args.anti_kick:
        tasks.append(anti_kick)

    if args.show_focus:
        show_focus()
        return
    if args.learn:
        learn_screen(args.learn)
        return
    screens = load_screens()
    for screen in screens:
        if screen['click']:
            cx, cy = screen['click']
            action = f"click at {cx:.0%} across, {cy:.0%} down the game window"
        else:
            action = "stay paused"
        print(f"Known screen '{screen['name']}': {action}.")
    if args.show_menu:
        show_menu()
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

    if not (MENU_CHECK or INGAME_PIXEL):
        print("Warning: menu check is off; the bot may click things in menus and the store.")
    no_menu.clear()  # nothing goes out until the state machine says PLAYING
    threading.Thread(target=run_state_machine, daemon=True).start()

    listener = Listener(on_press=on_hotkey)
    listener.start()

    try:
        for remaining in range(args.delay, 0, -1):
            print(f"Starting in {remaining}s - switch to the game (F9 cancels)")
            if not wait(1):
                break

        if not stop_event.is_set():
            started.set()
            for task in tasks:
                threading.Thread(target=run_task, args=(task,), daemon=True).start()
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
