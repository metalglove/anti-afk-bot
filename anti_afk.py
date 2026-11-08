"""Anti-AFK input bot.

Usage:
    python anti_afk.py [--game cod.exe] [--delay N]
                       [--autojoin] [--anti-kick] [--show-focus]
                       [--ingame-pixel X,Y,RRGGBB] [--no-menu-check] [--show-menu]
                       [--read-screen]

Hotkeys (work while the game has focus):
    F8  pause / resume (releases all held keys while paused)
    F9  stop (releases all held keys and exits)

Menu safety: input pauses whenever the game looks like it is in a menu (the
mouse cursor is showing, or the --ingame-pixel check fails), so the bot can't
click around the store. --show-menu prints what it detects.

Known screens: while a menu is open, the bot reads the text on screen and
looks it up in rules.json, e.g. "KICKED FOR INACTIVITY" -> click "EXIT".
Unknown menus, and any screen showing STORE, PURCHASE etc., stay paused.
--read-screen prints the text it reads, to help add rules.
"""
import argparse
import csv
import difflib
import json
import math
import os
import random
import re
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

# never stand still longer than this many seconds (--max-still).
# Keep it well below the game's AFK-kick timer.
MAX_STILL = 10

# always fire at least once within this many seconds.
MAX_NO_SHOT = 20

# multiplier for how far the view turns (--look-scale). Raise it if
# turning looks sluggish at your in-game sensitivity, lower it if it spins.
LOOK_SCALE = 3.0

# set to True if the camera doesn't turn in-game (sends raw
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
# Reading the screen (which menu is open?)
# --------------------------------------------------------------------------- #
# While a menu is open, a background thread screenshots the game window and reads
# the text on it (OCR): right when the menu opens, right after each click, and
# every READ_INTERVAL seconds in between. Nothing is read during a match. rules.json then says what each
# screen is and what to click, by the words on it, e.g.
#   {"name": "kicked", "if_seen": ["KICKED FOR INACTIVITY"], "click": "EXIT"}
# Nothing is clicked while any "never_click_if_seen" word (STORE, PURCHASE...)
# is on screen. Rules with "in_match": true are also checked during a match
# (every MATCH_READ_INTERVAL s, no cursor needed) and pause input while they match,
# e.g. the VICTORY / DEFEAT banner at the end of a match. No per-resolution setup: the words are found wherever they are.
RULES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rules.json')
READ_INTERVAL = 3.0               # seconds between screen reads while a menu stays open
READ_AFTER_CLICK = 0.8            # seconds after a click before reading the new screen
MATCH_READ_INTERVAL = 5.0         # seconds between reads during a match, only if a rule has
                                  # "in_match": true (e.g. VICTORY / DEFEAT banners)
SCREEN_CLICK_DELAY = (1.0, 2.5)   # seconds to "read" a screen before clicking it
SCREEN_CLICK_COOLDOWN = 4.0       # seconds before clicking the same screen again
OCR_MIN_CONFIDENCE = 0.5

rules = {'screens': [], 'never_click_if_seen': []}


def grab(box):
    """Screenshot of a screen rectangle (left, top, right, bottom), or None."""
    try:
        from PIL import ImageGrab
        return ImageGrab.grab(bbox=tuple(box)).convert('RGB')
    except Exception:  # Pillow missing, or the screen can't be read (exclusive fullscreen)
        return None


def load_rules():
    """Load rules.json; returns the rules, or empty rules if it can't be read."""
    try:
        with open(RULES_FILE, encoding='utf-8') as f:
            data = json.load(f)
        data.setdefault('screens', [])
        data.setdefault('never_click_if_seen', [])
        for screen in data['screens']:
            screen.setdefault('click', None)
            screen.setdefault('press', None)  # a key to press instead, e.g. "ESC"
            if screen['press']:
                screen['press'] = str(screen['press']).lower()
            screen['in_match'] = bool(screen.get('in_match', False))
            if screen['in_match']:
                screen['click'] = screen['press'] = None  # in-match rules only pause input
            screen['if_seen'] = list(screen.get('if_seen', []))
        return data
    except (OSError, ValueError) as e:
        print(f"Warning: can't read {os.path.basename(RULES_FILE)} ({e}); no screens will be clicked.")
        return {'screens': [], 'never_click_if_seen': []}


def phrase_pattern(phrase):
    """Regex for a phrase that also matches when OCR drops the spaces ('QUICKPLAY')."""
    words = [re.escape(w) for w in phrase.split()]
    return re.compile(r'(?<![A-Z0-9])' + r'\s*'.join(words) + r'(?![A-Z0-9])', re.IGNORECASE)


def _letters(text):
    return re.sub(r'[^A-Z0-9]', '', text.upper())


def seen(read, phrase):
    """True if `phrase` is on screen. Also accepts an item that is a near miss of the
    whole phrase, since OCR sometimes drops or swaps a letter ('VICTORY' -> '/CTORY')."""
    if phrase_pattern(phrase).search(read['text']):
        return True
    want = _letters(phrase)
    if len(want) < 5:
        return False
    return any(difflib.SequenceMatcher(None, want, _letters(text)).ratio() >= 0.82
               for text, _, _ in read['items'])


_ocr_engine = None
_ocr_lock = threading.Lock()


def ocr(image):
    """Text on an image: [(text, (x, y) center, confidence)]."""
    global _ocr_engine
    import numpy
    with _ocr_lock:
        if _ocr_engine is None:
            print("Loading text recognition (first time takes a few seconds)...")
            from rapidocr_onnxruntime import RapidOCR
            _ocr_engine = RapidOCR()
        result, _ = _ocr_engine(numpy.asarray(image))
    items = []
    for box, text, confidence in result or []:
        if float(confidence) < OCR_MIN_CONFIDENCE:
            continue
        x = sum(p[0] for p in box) / 4
        y = sum(p[1] for p in box) / 4
        items.append((text.strip(), (x, y), float(confidence)))
    return items


def read_screen():
    """Screenshot the game window and read it. Returns a 'read' dict or None."""
    rect = window_client_rect()
    left, top, width, height = rect
    image = grab((left, top, left + width, top + height))
    if image is None:
        return None
    items = [(text, (round(left + x), round(top + y)), c) for text, (x, y), c in ocr(image)]
    return {'time': time.monotonic(), 'items': items,
            'text': ' | '.join(text for text, _, _ in items)}


def find_text(read, phrase):
    """Screen position of the OCR item that best matches `phrase`, or None.
    Prefers an item that is just that phrase (a button) over one that contains it."""
    pattern = phrase_pattern(phrase)
    best = None
    for text, pos, _ in read['items']:
        m = pattern.search(text)
        if not m:
            continue
        extra = len(text) - (m.end() - m.start())  # how much else is in this item
        if best is None or extra < best[0]:
            best = (extra, pos)
    return best[1] if best else None


def screen_action(screen):
    """What the bot does on a screen: "click X", "press ESC", or None (wait)."""
    if screen is None:
        return None
    if screen['click']:
        return f"click {screen['click']}"
    if screen['press']:
        return f"press {screen['press'].upper()}"
    return None


def classify(read, in_match=False):
    """Which rule matches a read: (screen rule or None, danger word or None).
    in_match: only consider rules marked "in_match" (no cursor is showing)."""
    if read is None:
        return None, None
    danger = next((w for w in rules['never_click_if_seen'] if seen(read, w)), None)
    for screen in rules['screens']:
        if in_match and not screen['in_match']:
            continue
        if screen['if_seen'] and all(seen(read, p) for p in screen['if_seen']):
            return screen, danger
    return None, danger


class ScreenReader:
    """Reads the screen in the background while a menu is open."""

    def __init__(self):
        self.latest = None
        self.lock = threading.Lock()
        self.wanted = threading.Event()  # set while the state machine wants reads
        self.soon = threading.Event()    # set to read now instead of after the interval
        self.interval = READ_INTERVAL    # set by the state machine (menu or match pace)

    def read_soon(self):
        self.soon.set()

    def max_age(self):
        """A read older than this is not trusted."""
        return self.interval + 2.0

    def run(self):
        warned = False
        while not stop_event.is_set():
            if not self.wanted.wait(0.2):  # a menu just opened: the next pass reads at once
                with self.lock:
                    self.latest = None
                continue
            try:
                read = read_screen()
            except Exception as e:  # OCR missing or failed: stay safe, just don't click
                if not warned:
                    print(f"Can't read the screen ({e}). Menus stay paused; nothing is clicked.")
                    warned = True
                read = None
            if read is None and not warned:
                print("Can't capture the game window. Use borderless or windowed mode.")
                warned = True
            with self.lock:
                self.latest = read
            # Wait for the interval, or less if someone asks for a read (e.g. after a click).
            deadline = time.monotonic() + self.interval
            while not stop_event.is_set() and self.wanted.is_set() and time.monotonic() < deadline:
                if self.soon.wait(0.1):
                    self.soon.clear()
                    break

    def fresh(self, newer_than=0.0):
        with self.lock:
            read = self.latest
        if read and read['time'] > newer_than and time.monotonic() - read['time'] <= self.max_age():
            return read
        return None


reader = ScreenReader()


def click_screen(screen, seen_at):
    """Click a screen's button (or press its key) after a short pause, if the screen
    is still there."""
    if not wait(random.uniform(*SCREEN_CLICK_DELAY)):
        return
    if not (running.is_set() and focused.is_set()):
        return
    reader.read_soon()  # confirm with a new read that the screen is still there
    read = None
    deadline = time.monotonic() + reader.max_age()
    while read is None and time.monotonic() < deadline:  # wait for a read taken after the pause
        read = reader.fresh(newer_than=seen_at)
        if read is None and not wait(0.1):
            return
    if read is None:
        return
    current, danger = classify(read)
    if current is not screen or danger or not menu_reason():
        return  # the screen changed while we waited
    if screen['press']:
        key = screen['press'].upper()
        stats['clicks'] += 1
        if DRY_RUN:
            print(f"Test: would press {key} on screen '{screen['name']}'.")
            return
        inputs.press_key(screen['press'])
        print(f"Pressed {key} on screen '{screen['name']}'.")
        if wait(READ_AFTER_CLICK):
            reader.read_soon()
        return
    pos = find_text(read, screen['click'])
    if pos is None:
        print(f"Screen '{screen['name']}': couldn't find '{screen['click']}' to click.")
        return
    x, y = pos
    if DRY_RUN:
        stats['clicks'] += 1
        print(f"Test: would click '{screen['click']}' at {x},{y} on screen '{screen['name']}'.")
        return
    inputs.click_at(x, y)
    stats['clicks'] += 1
    print(f"Clicked '{screen['click']}' at {x},{y} on screen '{screen['name']}'.")
    if wait(READ_AFTER_CLICK):
        reader.read_soon()  # see what the click led to without waiting the full interval


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
#   SCREEN name   a screen from rules.json (e.g. class_select): clicks its button or presses its key
#   MENU          a menu it doesn't know, or a store/purchase screen: nothing is sent
STARTING, PAUSED, NOT_FOCUSED, PLAYING, RESUMING, SCREEN, MENU, STOPPED = (
    'STARTING', 'PAUSED', 'NOT FOCUSED', 'PLAYING', 'RESUMING', 'SCREEN', 'MENU', 'STOPPED')
DRY_RUN = False  # test mode: detect states and report clicks, but send nothing
UNKNOWN_MENU_REMINDER = 300  # seconds in an unknown menu before suggesting a rule

stats = {'started': None, 'clicks': 0, 'matches': 0, 'kicks': 0, 'playing': 0.0,
         'match_time': 0.0, 'last_match': None}

# Match timing: a match starts at the first PLAYING after the previous one ended
# and ends at VICTORY / DEFEAT, a kick, or the main menu. Finished matches are
# appended to matches.csv (not in test runs), so the total time adds up across runs.
HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'matches.csv')
MATCH_END_SCREENS = {'victory': 'victory', 'defeat': 'defeat', 'kicked': 'kicked',
                     'report_victory': 'victory', 'report_defeat': 'defeat', 'report': 'over',
                     'main_menu': 'left', 'searching': 'left'}
match = {'start': None,    # wall-clock start of the current match
         'over_at': None}  # monotonic time the last match ended (VICTORY / DEFEAT)
# After VICTORY / DEFEAT the game shows a few screens without a cursor (best play,
# scoreboard). Those aren't a new match: input stays paused until a pre-match screen
# shows up (or POST_MATCH_TIMEOUT passes, in case those screens were missed).
PRE_MATCH_SCREENS = {'searching', 'lobby', 'lobby_found', 'class_select', 'main_menu', 'kicked'}
POST_MATCH_TIMEOUT = 300.0


def load_history():
    """Finished matches from matches.csv: [{'date', 'duration', 'result'}], oldest first."""
    rows = []
    try:
        with open(HISTORY_FILE, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                try:
                    rows.append({'date': row['date'], 'duration': float(row['duration_s']),
                                 'result': row['result']})
                except (KeyError, ValueError):
                    pass
    except OSError:
        pass
    return rows


def _track_match(name, detail):
    """Start or end the current match on state changes (called from enter_state)."""
    if name == SCREEN and detail.split(' (')[0] in PRE_MATCH_SCREENS:
        match['over_at'] = None  # the next match is on its way
    if name == PLAYING and match['start'] is None:
        match['start'] = time.time()
        print(f"   Match started at {time.strftime('%H:%M')}.")
        return
    if name != SCREEN or match['start'] is None:
        return
    result = MATCH_END_SCREENS.get(detail.split(' (')[0])
    if result is not None:
        end_match(result)


def end_match(result):
    """Record the current match as finished with `result`."""
    if match['start'] is None:
        return
    duration = time.time() - match['start']
    started_at = match['start']
    match['start'] = None
    if result in ('victory', 'defeat', 'over'):  # counted once, even if the banner and report both show
        stats['matches'] += 1
        match['over_at'] = time.monotonic()
    stats['match_time'] += duration
    stats['last_match'] = (result, duration)
    print(f"   Match over ({result}) after {format_duration(duration)}. "
          f"Match time this session: {format_duration(stats['match_time'])}.")
    if DRY_RUN:
        return
    try:
        new_file = not os.path.exists(HISTORY_FILE)
        with open(HISTORY_FILE, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if new_file:
                writer.writerow(['date', 'duration_s', 'result'])
            writer.writerow([time.strftime('%Y-%m-%d %H:%M', time.localtime(started_at)),
                             round(duration), result])
    except OSError as e:
        print(f"   Couldn't save the match to {os.path.basename(HISTORY_FILE)}: {e}")

state_lock = threading.Lock()
state = {'name': STOPPED, 'detail': '', 'since': time.monotonic()}
started = threading.Event()  # set once the countdown is over


def detect(read=None):
    """Raw state: (state, detail, screen rule or None). `read` is the latest screen
    read (or None if there isn't a fresh one). No delays; the state machine adds those."""
    if not started.is_set():
        return STARTING, '', None
    if stop_event.is_set():
        return STOPPED, '', None
    if not running.is_set():
        return PAUSED, 'F8 to resume', None
    if not focused.is_set():
        return NOT_FOCUSED, 'waiting for the game window', None
    reason = menu_reason()
    if not reason:
        screen, _ = classify(read, in_match=True)
        if screen:  # e.g. the VICTORY / DEFEAT banner: the match is over
            return SCREEN, f"{screen['name']} (match over, input paused)", screen
        if match['over_at'] is not None:
            if time.monotonic() - match['over_at'] < POST_MATCH_TIMEOUT:
                return SCREEN, "post_match (match over, waiting for the next one)", None
            match['over_at'] = None
        return PLAYING, '', None
    if read is None:
        return MENU, 'reading the screen...', None
    screen, danger = classify(read)
    if danger:
        return MENU, f"'{danger}' on screen: never clicking here", None
    if screen:
        action = screen_action(screen) or 'waiting'
        action = action.replace('click ', 'clicking ', 1).replace('press ', 'pressing ', 1)
        return SCREEN, f"{screen['name']} ({action})", screen
    return MENU, "unknown, nothing sent", None


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
            if previous == PLAYING:
                stats['playing'] += lasted
        if name == SCREEN and detail.split(' (')[0] == 'kicked':
            stats['kicks'] += 1
    if name == PLAYING:
        no_menu.set()
    elif no_menu.is_set():
        no_menu.clear()
        inputs.release_all()
    label = f"{name}: {detail}" if detail else name
    was = '' if same_name else f"   (after {format_duration(lasted)} {previous})"
    print(f"[{time.strftime('%H:%M:%S')}] {label}{was}")
    set_console_title(f"anti-afk: {label}")
    _track_match(name, detail)


def run_state_machine():
    """Track the state and act on it: release input outside PLAYING, wait a moment
    before resuming after a menu, and click known screens."""
    resume_since = None
    last_clicked = {}            # screen name -> time
    last_reminder = time.monotonic()
    while not stop_event.is_set():
        now = time.monotonic()
        active = started.is_set() and running.is_set() and focused.is_set()
        in_menu = active and bool(menu_reason())
        watch_match = active and not in_menu and any(r['in_match'] for r in rules['screens'])
        reader.interval = READ_INTERVAL if in_menu else MATCH_READ_INTERVAL
        if in_menu or watch_match:
            reader.wanted.set()
        else:
            reader.wanted.clear()
        read = reader.fresh() if (in_menu or watch_match) else None
        name, detail, screen = detect(read)
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
        if name != STARTING:  # the countdown reports STARTING itself
            enter_state(name, detail)

        if name == SCREEN and screen_action(screen) and \
                now - last_clicked.get(screen['name'], 0) >= SCREEN_CLICK_COOLDOWN:
            click_screen(screen, read['time'])
            last_clicked[screen['name']] = time.monotonic()
        elif name == MENU and read and lasted > UNKNOWN_MENU_REMINDER and \
                now - last_reminder > UNKNOWN_MENU_REMINDER:
            last_reminder = now
            print(f"   Still in an unknown menu after {format_duration(lasted)}. Text on screen:\n"
                  f"   {read['text'][:300]}\n"
                  f"   If this screen needs a click, add it to {os.path.basename(RULES_FILE)}.")
        stop_event.wait(0.1)


def print_screen_text(delay=3):
    """Read the game window once and print its text, to help write rules."""
    global rules
    rules = load_rules()
    print(f"Reading the screen in {delay}s; switch to the game...")
    time.sleep(delay)
    read = read_screen()
    if read is None:
        print("Couldn't capture the screen. Use borderless or windowed mode.")
        return
    print("Text on screen:\n  " + read['text'])
    screen, danger = classify(read)
    if danger:
        print(f"=> '{danger}' is on screen: the bot never clicks here.")
    elif screen:
        target = f", clicks '{screen['click']}' at {find_text(read, screen['click'])}" if screen['click']             else f", presses {screen['press'].upper()}" if screen['press'] else ''
        print(f"=> screen '{screen['name']}'{target}")
    else:
        print("=> no rule matches this screen (the bot sends nothing here).")


def show_menu():
    """Print what menu detection sees whenever it changes (Ctrl+C to quit)."""
    global rules
    rules = load_rules()
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
            read = read_screen() if menu_reason() else None
            name, detail, _ = detect(read)
            parts.append(f"=> {name}" + (f": {detail}" if detail else ''))
            line = '   '.join(parts)
            if line != last:
                print(line)
                last = line
            stop_event.wait(1.0)
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
        """Move to a screen spot and click it. Only for known screens, which are menus,
        so it skips the menu check but still needs the bot running and the game in front."""
        with self._lock:
            if not (running.is_set() and focused.is_set()) or stop_event.is_set():
                return
            pydirectinput.moveTo(x, y)
            time.sleep(random.uniform(0.08, 0.2))
            pydirectinput.mouseDown(button='left')
            time.sleep(random.uniform(0.05, 0.1))
            pydirectinput.mouseUp(button='left')

    def press_key(self, key):
        """Tap a key on a known screen (e.g. ESC to dismiss a report). Like click_at,
        it skips the menu check but needs the bot running and the game in front."""
        with self._lock:
            if not (running.is_set() and focused.is_set()) or stop_event.is_set():
                return
            pydirectinput.keyDown(key)
            time.sleep(random.uniform(0.05, 0.12))
            pydirectinput.keyUp(key)

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


# Directions the human player walks in.
DIRECTIONS = [
    ('w',), ('s',), ('a',), ('d',),
    ('w', 'a'), ('w', 'd'), ('s', 'a'), ('s', 'd'),
]


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


def toggle_pause():
    if running.is_set():
        running.clear()
        inputs.release_all()
    else:
        running.set()


def on_hotkey(key):
    if key == STOP_KEY:
        stop_event.set()
    elif key == PAUSE_KEY and started.is_set():
        toggle_pause()


# --------------------------------------------------------------------------- #
# Starting and stopping (used by the command line and by app.py)
# --------------------------------------------------------------------------- #
_threads = []


def _spawn(target, *args):
    t = threading.Thread(target=target, args=args, daemon=True)
    t.start()
    _threads.append(t)


def describe_rules():
    global rules
    rules = load_rules()
    for screen in rules['screens']:
        action = screen_action(screen) or "wait"
        print(f"Screen '{screen['name']}' (shows {' + '.join(screen['if_seen'])}): {action}.")
    if rules['never_click_if_seen']:
        print("Never clicks while any of these is on screen: "
              + ', '.join(rules['never_click_if_seen']))


def is_started():
    """True while a session's threads are still running."""
    return any(t.is_alive() for t in _threads)


def start(game='', autojoin_on=False, anti_kick_on=False, delay=5, dry_run=False):
    """Start a session in background threads and return right away.
    dry_run: only detect and report states and clicks; send no input at all."""
    global DRY_RUN, rules, game_hwnd
    stop()
    stop_event.clear()
    running.set()
    started.clear()
    no_menu.clear()  # nothing goes out until the state machine says PLAYING
    focused.set()
    game_hwnd = None
    DRY_RUN = dry_run

    rules = load_rules()
    print(f"{len(rules['screens'])} known screens loaded from {os.path.basename(RULES_FILE)}.")
    stats.update(started=time.monotonic(), clicks=0, matches=0, kicks=0, playing=0.0,
                 match_time=0.0, last_match=None)
    match['start'] = None
    match['over_at'] = None
    tasks = [] if dry_run else list(HUMAN_MODE)
    if autojoin_on and not dry_run:
        tasks.append(autojoin)
    if anti_kick_on and not dry_run:
        tasks.append(anti_kick)

    if not is_admin():
        print("Warning: not running as administrator. If the game runs as admin, it will ignore\n"
              "         this input and F8/F9 won't work while it's focused.")
    if game:
        focused.clear()  # wait for the game before sending anything
        _spawn(watch_focus, game)
    else:
        print("Tip: set the game so input pauses when the game isn't in front.")
    if not (MENU_CHECK or INGAME_PIXEL):
        print("Warning: menu check is off; the bot may click things in menus and the store.")
    if dry_run:
        print("Test mode: nothing is sent. Go through the menus and watch the state.")

    enter_state(STARTING)
    _spawn(reader.run)
    _spawn(run_state_machine)
    _spawn(_countdown, delay, tasks)


def _countdown(delay, tasks):
    for remaining in range(delay, 0, -1):
        enter_state(STARTING, f"{remaining}s, switch to the game")
        if not wait(1):
            return
    started.set()
    for task in tasks:
        _spawn(run_task, task)
    if tasks:
        print("Running. F8 = pause/resume, F9 = stop.")


def stop():
    """Stop the session, wait for its threads and release everything."""
    stop_event.set()
    deadline = time.monotonic() + 3
    for t in list(_threads):
        if t is not threading.current_thread():
            t.join(max(0.0, deadline - time.monotonic()))
    _threads.clear()
    inputs.release_all()
    end_match('stopped')  # count the time of a match that was still going
    if current_state()[0] != STOPPED:
        enter_state(STOPPED)
        print("Stopped; all keys released.")


def main():
    global LOOK_SCALE, MAX_STILL, MENU_CHECK, INGAME_PIXEL, PIXEL_TOLERANCE, READ_INTERVAL
    parser = argparse.ArgumentParser(description="Anti-AFK input bot. Run app.py for the window version.")
    parser.add_argument('--autojoin', action='store_true', help="press F5 every 2s")
    parser.add_argument('--anti-kick', action='store_true', help="press 'l' every 20s")
    parser.add_argument('--delay', type=int, default=5,
                        help="seconds to wait before sending input (default 5)")
    parser.add_argument('--game', default='cod.exe',
                        help="game exe name or part of its window title (default: cod.exe); "
                             "input only goes out while it is focused")
    parser.add_argument('--look-scale', type=float, default=LOOK_SCALE,
                        help=f"how far turns go (default {LOOK_SCALE})")
    parser.add_argument('--max-still', type=float, default=MAX_STILL,
                        help=f"max seconds without moving (default {MAX_STILL})")
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
    parser.add_argument('--test', action='store_true',
                        help="run the state machine without sending any input")
    parser.add_argument('--read-interval', type=float, default=READ_INTERVAL,
                        help=f"seconds between screen reads while a menu stays open "
                             f"(default {READ_INTERVAL:g}; it also reads when a menu opens "
                             f"and after each click)")
    parser.add_argument('--read-screen', action='store_true',
                        help="after 3s, read the game window once and print its text and "
                             "which rule matches, to help write rules.json")
    args = parser.parse_args()

    LOOK_SCALE, MAX_STILL = args.look_scale, args.max_still
    MENU_CHECK, PIXEL_TOLERANCE = not args.no_menu_check, args.pixel_tolerance
    READ_INTERVAL = max(0.5, args.read_interval)
    if args.ingame_pixel:
        try:
            x, y, color = parse_pixel(args.ingame_pixel, need_color=not args.show_menu)
        except (argparse.ArgumentTypeError, ValueError, IndexError) as e:
            parser.error(f"--ingame-pixel: {e}")
        INGAME_PIXEL = (x, y, color)  # color is None only with --show-menu: just print it

    if args.show_focus:
        show_focus()
        return
    if args.read_screen:
        print_screen_text()
        return
    if args.show_menu:
        show_menu()
        return

    describe_rules()
    listener = Listener(on_press=on_hotkey)
    listener.start()
    try:
        start(args.game, args.autojoin, args.anti_kick, args.delay, dry_run=args.test)
        while not stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()
        stop()


if __name__ == '__main__':
    main()
