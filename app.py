"""Anti-AFK bot: window version.

Start it with `python app.py`.
Same engine as anti_afk.py; this adds a window with the live state, a state
diagram, session stats, the known screens and settings. Settings are remembered
in settings.json.
"""
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

import customtkinter as ctk

import anti_afk as bot

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, 'settings.json')
DEFAULTS = {'game': 'cod.exe', 'autojoin': False, 'anti_kick': False, 'delay': 5,
            'read_interval': bot.READ_INTERVAL}

# Colors (dark theme).
BG = '#0f1115'
CARD = '#181b21'
CARD_BORDER = '#262a33'
TEXT = '#e8eaed'
MUTED = '#8b919c'
ACCENT = '#3b82f6'
ACCENT_HOVER = '#2563eb'
DANGER = '#ef4444'
DANGER_HOVER = '#dc2626'
WARN = '#f59e0b'
GOOD = '#22c55e'
NEUTRAL = '#27272a'
NEUTRAL_HOVER = '#3f3f46'

STATE_STYLE = {  # color, short explanation
    bot.PLAYING: (GOOD, "In a match: moving, looking and shooting"),
    bot.RESUMING: ('#16a34a', "Menu closed, about to play"),
    bot.SCREEN: (ACCENT, "Known screen"),
    bot.MENU: (WARN, "Unknown menu: sending nothing"),
    bot.PAUSED: ('#71717a', "Paused. Press F8 or Resume to continue"),
    bot.NOT_FOCUSED: ('#71717a', "Waiting for the game window to be in front"),
    bot.STARTING: ('#eab308', "Starting"),
    bot.STOPPED: ('#52525b', "Press Start, or Test run to try it without sending input"),
}

FONT = 'Segoe UI'
MONO = 'Consolas'


class QueueWriter:
    """Sends print() output from every thread to the activity log."""

    def __init__(self, q):
        self.q = q

    def write(self, text):
        if text:
            self.q.put(text)

    def flush(self):
        pass


def load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            settings = {**DEFAULTS, **json.load(f)}
        settings['game'] = settings['game'] or DEFAULTS['game']  # an empty game means the default
        return settings
    except (OSError, ValueError):
        return dict(DEFAULTS)


def card(parent, **kw):
    return ctk.CTkFrame(parent, fg_color=CARD, border_color=CARD_BORDER, border_width=1,
                        corner_radius=14, **kw)


def heading(parent, text):
    return ctk.CTkLabel(parent, text=text.upper(), text_color=MUTED,
                        font=ctk.CTkFont(FONT, 11, 'bold'))


def chip(parent, text, color, text_color=TEXT):
    return ctk.CTkLabel(parent, text=f"  {text}  ", fg_color=color, corner_radius=8,
                        text_color=text_color, font=ctk.CTkFont(FONT, 11, 'bold'), height=24)


class FlowDiagram:
    """The bot's state machine drawn as a diagram; the current state lights up."""

    # Logical layout; the drawing scales to fit the canvas. Columns 90/270/450/630,
    # rows: getting into a match, in the match, special screens.
    W, H = 720, 380
    NODES = {  # key: (x, y, title, action)
        'main_menu': (90, 50, "Main menu", "click QUICK PLAY"),
        'searching': (270, 50, "Searching", "wait"),
        'lobby': (450, 50, "Lobby", "wait"),
        'class_select': (630, 50, "Class select", "press SPACE"),
        'report': (270, 165, "Match report", "press ESC"),
        'play': (450, 165, "PLAYING", "move, look, shoot"),
        'resume': (630, 165, "Resuming", "wait 1.5s"),
        'kicked': (90, 280, "Kicked", "click EXIT"),
        'over': (270, 280, "Match over", "input paused"),
        'pause_menu': (450, 280, "In-game menu", "press ESC"),
        'menu': (630, 280, "Unknown / store", "send nothing"),
    }
    EDGES = [('main_menu', 'searching'), ('searching', 'lobby'), ('lobby', 'class_select'),
             ('class_select', 'resume'), ('resume', 'play'), ('play', 'over'),
             ('over', 'report'), ('report', 'searching'), ('play', 'kicked'),
             ('kicked', 'main_menu'), ('play', 'pause_menu'), ('pause_menu', 'resume'),
             ('menu', 'resume')]  # unknown menus can come from anywhere
    SIDE = [(bot.STARTING, "Starting"), (bot.PAUSED, "Paused (F8)"),
            (bot.NOT_FOCUSED, "Not focused"), (bot.STOPPED, "Stopped (F9)")]
    SCREEN_NODE = {'lobby_found': 'lobby', 'victory': 'over', 'defeat': 'over',
                   'best_play': 'over', 'post_match': 'over',
                   'report_victory': 'report', 'report_defeat': 'report'}
    NODE_W, NODE_H = 150, 56

    def __init__(self, parent):
        self.canvas = tk.Canvas(parent, bg=CARD, highlightthickness=0, height=200)
        self.canvas.bind('<Configure>', lambda e: self.draw())
        self.active, self.color, self.other = None, ACCENT, ''

    def show(self, state_name, detail, color):
        if state_name == bot.PLAYING:
            active = 'play'
        elif state_name == bot.RESUMING:
            active = 'resume'
        elif state_name == bot.MENU:
            active = 'menu'
        elif state_name == bot.SCREEN:
            screen = detail.split(' (')[0]
            active = self.SCREEN_NODE.get(screen, screen)
        else:
            active = state_name
        other = detail.split(' (')[0] if state_name == bot.SCREEN and active not in self.NODES else ''
        if (active, color, other) != (self.active, self.color, self.other):
            self.active, self.color, self.other = active, color, other
            self.draw()

    @staticmethod
    def rounded(c, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return c.create_polygon(pts, smooth=True, **kw)

    def draw(self):
        c = self.canvas
        c.delete('all')
        width, height = max(c.winfo_width(), 200), max(c.winfo_height(), 120)
        k = min(width / self.W, height / self.H)  # fit both ways
        ox, oy = (width - self.W * k) / 2, min(12, (height - self.H * k) / 2)  # top-aligned
        w, h = self.NODE_W * k / 2, self.NODE_H * k / 2
        # Negative sizes are pixels, so the text scales with the boxes whatever the
        # Windows display scaling is.
        title_font = tkfont.Font(family=FONT, size=-max(9, round(15 * k)), weight='bold')
        sub_font = tkfont.Font(family=FONT, size=-max(8, round(12 * k)))
        dim = '#3a4150'

        def center(key):
            x, y = self.NODES[key][:2]
            return ox + x * k, oy + y * k

        def border_point(key, toward, gap=4):
            # Where the line from this node's center toward `toward` leaves the box.
            (x, y), (tx, ty) = center(key), toward
            dx, dy = tx - x, ty - y
            if dx == 0 and dy == 0:
                return x, y
            scale = min((w + gap) / abs(dx) if dx else float('inf'),
                        (h + gap) / abs(dy) if dy else float('inf'))
            return x + dx * scale, y + dy * scale

        for a, b in self.EDGES:
            start, end = border_point(a, center(b)), border_point(b, center(a))
            lit = a == self.active
            c.create_line(*start, *end, arrow='last', arrowshape=(8 * k, 10 * k, 4 * k),
                          width=max(1.5, (2.5 if lit else 1.5) * k),
                          fill=self.color if lit else dim)

        for key, (x, y, title, action) in self.NODES.items():
            x, y = ox + x * k, oy + y * k
            lit = key == self.active
            self.rounded(c, x - w, y - h, x + w, y + h, 10 * k,
                         fill=self.color if lit else BG, outline=self.color if lit else CARD_BORDER,
                         width=2 if lit else 1)
            c.create_text(x, y - 9 * k, text=title, fill='white' if lit else TEXT, font=title_font)
            c.create_text(x, y + 11 * k, text=action, fill='white' if lit else MUTED, font=sub_font)

        # States that can happen at any time.
        y = oy + 352 * k
        x = ox + (self.NODES['kicked'][0] - self.NODE_W / 2) * k
        c.create_text(x, y, text="Any time", anchor='w', fill=MUTED, font=sub_font)
        x += sub_font.measure("Any time") + 12 * k
        side = list(self.SIDE)
        if self.other:
            side.append(('__other__', f"Screen: {self.other}"))
        for key, label in side:
            lit = key == self.active or key == '__other__'
            tw = sub_font.measure(label) + 22 * k
            self.rounded(c, x, y - 13 * k, x + tw, y + 13 * k, 10 * k,
                         fill=self.color if lit else BG, outline=self.color if lit else CARD_BORDER, width=1)
            c.create_text(x + tw / 2, y, text=label, fill='white' if lit else MUTED, font=sub_font)
            x += tw + 8 * k

class Pages:
    """Navigation pills in the header and the pages they switch between
    (same set / get / tab calls as a CTkTabview)."""

    def __init__(self, nav, container, names):
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(0, weight=1)
        self.frames, self.buttons, self.current = {}, {}, None
        for i, name in enumerate(names):
            frame = ctk.CTkFrame(container, fg_color='transparent')
            frame.grid(row=0, column=0, sticky='nswe')
            self.frames[name] = frame
            button = ctk.CTkButton(nav, text=name, width=104, height=34, corner_radius=9,
                                   font=ctk.CTkFont(FONT, 14, 'bold'), border_width=0,
                                   command=lambda n=name: self.set(n))
            button.grid(row=0, column=i, padx=(4 if i == 0 else 2, 4 if i == len(names) - 1 else 2),
                        pady=4)
            self.buttons[name] = button
        self.set(names[0])

    def set(self, name):
        self.current = name
        self.frames[name].tkraise()
        for n, button in self.buttons.items():
            if n == name:
                button.configure(fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color='white')
            else:
                button.configure(fg_color='transparent', hover_color=NEUTRAL, text_color=MUTED)

    def get(self):
        return self.current

    def tab(self, name):
        return self.frames[name]


class App:
    def __init__(self, root):
        self.root = root
        self.log_queue = queue.Queue()
        self.busy_reading = False
        self.stopping = False
        self.run_seconds = 0.0
        self.history_total = 0.0
        self.seen_last_match = None
        sys.stdout = sys.stderr = QueueWriter(self.log_queue)

        settings = load_settings()
        self.game = tk.StringVar(value=settings['game'])
        self.autojoin = tk.BooleanVar(value=settings['autojoin'])
        self.anti_kick = tk.BooleanVar(value=settings['anti_kick'])
        self.delay = tk.StringVar(value=str(settings['delay']))
        self.read_interval = tk.StringVar(value=f"{settings['read_interval']:g}")
        for var in (self.game, self.autojoin, self.anti_kick, self.delay, self.read_interval):
            var.trace_add('write', lambda *_: self.save_settings())  # settings save themselves

        root.title("Anti-AFK")
        root.geometry('1180x780')
        root.minsize(1000, 680)
        root.configure(fg_color=BG)
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.build()

        self.hotkeys = bot.Listener(on_press=bot.on_hotkey)
        self.hotkeys.start()
        self.refresh_screens()
        self.poll()

    # -- layout ----------------------------------------------------------------
    def build(self):
        root = self.root
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)

        # Header: name, navigation, and warnings that need attention.
        header = ctk.CTkFrame(root, fg_color='transparent')
        header.grid(row=0, column=0, sticky='we', padx=20, pady=(14, 10))
        header.grid_columnconfigure(2, weight=1)
        ctk.CTkLabel(header, text="Anti-AFK", text_color=TEXT,
                     font=ctk.CTkFont(FONT, 20, 'bold')).grid(row=0, column=0, sticky='w', padx=(0, 28))
        nav = ctk.CTkFrame(header, fg_color=CARD, corner_radius=12, border_width=1,
                           border_color=CARD_BORDER)
        nav.grid(row=0, column=1, sticky='w')
        # Plain tk frame: an empty CTkFrame falls back to 200x200 when no warning shows.
        self.warnings = tk.Frame(header, bg=BG)
        self.warnings.grid(row=0, column=2, sticky='e')
        self.admin_chip = chip(self.warnings, "⚠ Not admin: only a problem if the game runs as admin", '#422006', '#fcd34d')
        self.game_chip = chip(self.warnings, "⚠ No game set (Settings)", '#422006', '#fcd34d')
        self.game_chip.bind('<Button-1>', lambda e: self.tabs.set("Settings"))
        tk.Frame(root, bg=CARD_BORDER, height=1).grid(row=0, column=0, sticky='swe')

        pages = ctk.CTkFrame(root, fg_color='transparent')
        pages.grid(row=1, column=0, sticky='nswe', padx=12, pady=(10, 12))
        self.tabs = Pages(nav, pages, ("Dashboard", "History", "Screens", "Settings"))
        self.build_dashboard(self.tabs.tab("Dashboard"))
        self.build_history(self.tabs.tab("History"))
        self.build_screens(self.tabs.tab("Screens"))
        self.build_settings(self.tabs.tab("Settings"))

    def build_dashboard(self, tab):
        tab.grid_columnconfigure(0, weight=3, uniform='dash')
        tab.grid_columnconfigure(1, weight=2, uniform='dash')
        tab.grid_rowconfigure(2, weight=1)

        # Status.
        status = card(tab)
        status.grid(row=0, column=0, sticky='nswe', padx=(0, 6), pady=(0, 8))
        status.grid_columnconfigure(1, weight=1)
        self.dot = ctk.CTkLabel(status, text='●', font=ctk.CTkFont(FONT, 34), width=40)
        self.dot.grid(row=0, column=0, rowspan=2, padx=(18, 6), pady=(16, 0))
        self.state_label = ctk.CTkLabel(status, text="Stopped", text_color=TEXT, anchor='w',
                                        font=ctk.CTkFont(FONT, 26, 'bold'))
        self.state_label.grid(row=0, column=1, sticky='w', pady=(16, 0))
        self.timer_label = ctk.CTkLabel(status, text="", text_color=MUTED, font=ctk.CTkFont(MONO, 15))
        self.timer_label.grid(row=0, column=2, padx=18, sticky='e', pady=(16, 0))
        self.detail_label = ctk.CTkLabel(status, text="", text_color=MUTED, anchor='w', justify='left',
                                         font=ctk.CTkFont(FONT, 13), wraplength=520)
        self.detail_label.grid(row=1, column=1, columnspan=2, sticky='w', pady=(0, 6))
        self.test_badge = chip(status, "TEST RUN · nothing is sent", NEUTRAL_HOVER)

        controls = ctk.CTkFrame(status, fg_color='transparent')
        controls.grid(row=3, column=0, columnspan=3, sticky='we', padx=14, pady=(6, 16))
        controls.grid_columnconfigure(0, weight=2)
        controls.grid_columnconfigure((1, 2), weight=1)
        big = ctk.CTkFont(FONT, 15, 'bold')
        self.main_btn = ctk.CTkButton(controls, text="▶  Start", height=46, font=big, corner_radius=10,
                                      fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self.start_or_stop)
        self.main_btn.grid(row=0, column=0, sticky='we', padx=4)
        self.pause_btn = ctk.CTkButton(controls, text="❚❚  Pause", height=46, font=big, corner_radius=10,
                                       fg_color=NEUTRAL, hover_color=NEUTRAL_HOVER, command=self.toggle_pause)
        self.pause_btn.grid(row=0, column=1, sticky='we', padx=4)
        self.test_btn = ctk.CTkButton(controls, text="Test run", height=46, font=big, corner_radius=10,
                                      fg_color='transparent', border_width=1, border_color=CARD_BORDER,
                                      hover_color=NEUTRAL, command=lambda: self.start(dry_run=True))
        self.test_btn.grid(row=0, column=2, sticky='we', padx=4)
        ctk.CTkLabel(status, text="Hotkeys in game:  F8 pause / resume   ·   F9 stop", text_color=MUTED,
                     font=ctk.CTkFont(FONT, 11)).grid(row=4, column=0, columnspan=3, pady=(0, 12))

        # Session stats.
        stats = card(tab)
        stats.grid(row=0, column=1, sticky='nswe', padx=(6, 0), pady=(0, 8))
        stats.grid_columnconfigure((0, 1, 2), weight=1, uniform='stats')
        heading(stats, "Stats").grid(row=0, column=0, columnspan=3, sticky='w', padx=16, pady=(14, 4))
        self.stat_values = {}
        for i, (key, label) in enumerate([('match_time', "Match time, session"),
                                          ('total', "Match time, all time"),
                                          ('running', "Running for"),
                                          ('matches', "Matches, session"),
                                          ('last', "Last match"), ('kicks', "AFK kicks")]):
            box = ctk.CTkFrame(stats, fg_color=BG, corner_radius=10)
            box.grid(row=1 + i // 3, column=i % 3, sticky='we',
                     padx=(16 if i % 3 == 0 else 4, 16 if i % 3 == 2 else 4), pady=5)
            value = ctk.CTkLabel(box, text="–", text_color=TEXT, font=ctk.CTkFont(FONT, 18, 'bold'), anchor='w')
            value.pack(anchor='w', padx=12, pady=(8, 0))
            ctk.CTkLabel(box, text=label, text_color=MUTED, font=ctk.CTkFont(FONT, 11), anchor='w').pack(
                anchor='w', padx=12, pady=(0, 8))
            self.stat_values[key] = value

        # Diagram + activity.
        flow_card = card(tab)
        flow_card.grid(row=2, column=0, sticky='nswe', padx=(0, 6))
        flow_card.grid_columnconfigure(0, weight=1)
        heading(flow_card, "What the bot is doing").grid(row=0, column=0, sticky='w', padx=16, pady=(14, 0))
        self.flow = FlowDiagram(flow_card)
        self.flow.canvas.grid(row=1, column=0, sticky='nswe', padx=12, pady=(4, 12))
        flow_card.grid_rowconfigure(1, weight=1)

        log_card = card(tab)
        log_card.grid(row=2, column=1, sticky='nswe', padx=(6, 0))
        log_card.grid_columnconfigure(0, weight=1)
        log_card.grid_rowconfigure(1, weight=1)
        top = ctk.CTkFrame(log_card, fg_color='transparent')
        top.grid(row=0, column=0, sticky='we', padx=16, pady=(10, 2))
        top.grid_columnconfigure(0, weight=1)
        heading(top, "Activity").grid(row=0, column=0, sticky='w')
        ctk.CTkButton(top, text="Clear", width=56, height=24, corner_radius=6, fg_color=NEUTRAL,
                      hover_color=NEUTRAL_HOVER, command=self.clear_log).grid(row=0, column=1)
        self.log = ctk.CTkTextbox(log_card, fg_color=BG, text_color='#c9ccd1', corner_radius=10,
                                  font=ctk.CTkFont(MONO, 12), wrap='word')
        self.log.grid(row=1, column=0, sticky='nswe', padx=12, pady=(0, 12))
        for tag, color in (('state', TEXT), ('click', '#93c5fd'), ('warn', '#fcd34d'),
                           ('playing', '#86efac'), ('muted', MUTED)):
            self.log.tag_config(tag, foreground=color)
        self.log.configure(state='disabled')

    def build_history(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)
        summary = card(tab)
        summary.grid(row=0, column=0, sticky='we', pady=(0, 8))
        summary.grid_columnconfigure((0, 1, 2), weight=1, uniform='hist')
        self.hist_values = {}
        for i, (key, label) in enumerate([('total', "Total match time"), ('count', "Matches recorded"),
                                          ('average', "Average match")]):
            box = ctk.CTkFrame(summary, fg_color=BG, corner_radius=10)
            box.grid(row=0, column=i, sticky='we', padx=(16 if i == 0 else 4, 16 if i == 2 else 4), pady=14)
            value = ctk.CTkLabel(box, text="–", text_color=TEXT, font=ctk.CTkFont(FONT, 24, 'bold'))
            value.pack(anchor='w', padx=14, pady=(10, 0))
            ctk.CTkLabel(box, text=label, text_color=MUTED, font=ctk.CTkFont(FONT, 12)).pack(
                anchor='w', padx=14, pady=(0, 10))
            self.hist_values[key] = value

        table = card(tab)
        table.grid(row=1, column=0, sticky='nswe')
        table.grid_columnconfigure(0, weight=1)
        table.grid_rowconfigure(2, weight=1)
        top = ctk.CTkFrame(table, fg_color='transparent')
        top.grid(row=0, column=0, sticky='we', padx=16, pady=(14, 4))
        top.grid_columnconfigure(0, weight=1)
        heading(top, "Matches").grid(row=0, column=0, sticky='w')
        ctk.CTkButton(top, text="Open matches.csv", width=130, height=28, corner_radius=8, fg_color=NEUTRAL,
                      hover_color=NEUTRAL_HOVER, command=self.open_history).grid(row=0, column=1)
        cols = ctk.CTkFrame(table, fg_color='transparent')
        cols.grid(row=1, column=0, sticky='we', padx=24)
        for i, (text, weight) in enumerate((("Started", 3), ("Duration", 2), ("Result", 2))):
            cols.grid_columnconfigure(i, weight=weight, uniform='cols')
            ctk.CTkLabel(cols, text=text, text_color=MUTED, anchor='w',
                         font=ctk.CTkFont(FONT, 12, 'bold')).grid(row=0, column=i, sticky='w')
        self.history_list = ctk.CTkScrollableFrame(table, fg_color='transparent')
        self.history_list.grid(row=2, column=0, sticky='nswe', padx=8, pady=(0, 12))
        self.history_list.grid_columnconfigure(0, weight=1)
        self.refresh_history()

    def refresh_history(self):
        rows = bot.load_history()
        self.history_total = sum(r['duration'] for r in rows)
        self.hist_values['total'].configure(text=bot.format_duration(self.history_total))
        self.hist_values['count'].configure(text=str(len(rows)))
        self.hist_values['average'].configure(
            text=bot.format_duration(self.history_total / len(rows)) if rows else '–')
        for child in self.history_list.winfo_children():
            child.destroy()
        if not rows:
            ctk.CTkLabel(self.history_list, text="No matches recorded yet. Finished matches show up here "
                                                 "(test runs aren't recorded).",
                         text_color=MUTED).grid(row=0, column=0, sticky='w', padx=16, pady=8)
        colors = {'victory': GOOD, 'defeat': DANGER, 'kicked': WARN}
        for i, r in enumerate(reversed(rows[-200:])):
            row = ctk.CTkFrame(self.history_list, fg_color=BG if i % 2 == 0 else 'transparent', corner_radius=8)
            row.grid(row=i, column=0, sticky='we', padx=4, pady=1)
            for c, weight in enumerate((3, 2, 2)):
                row.grid_columnconfigure(c, weight=weight, uniform='cols')
            ctk.CTkLabel(row, text=r['date'], text_color=TEXT, anchor='w').grid(row=0, column=0, sticky='w', padx=12, pady=4)
            ctk.CTkLabel(row, text=bot.format_duration(r['duration']), text_color=TEXT, anchor='w',
                         font=ctk.CTkFont(MONO, 13)).grid(row=0, column=1, sticky='w', padx=12)
            ctk.CTkLabel(row, text=r['result'], text_color=colors.get(r['result'], MUTED), anchor='w',
                         font=ctk.CTkFont(FONT, 13, 'bold')).grid(row=0, column=2, sticky='w', padx=12)

    def open_history(self):
        try:
            os.startfile(bot.HISTORY_FILE)
        except AttributeError:
            print(f"Match history is in {bot.HISTORY_FILE}.")
        except OSError:
            print("No matches recorded yet.")

    def build_screens(self, tab):
        tab.grid_columnconfigure(0, weight=3, uniform='screens')
        tab.grid_columnconfigure(1, weight=2, uniform='screens')
        tab.grid_rowconfigure(0, weight=1)

        rules_card = card(tab)
        rules_card.grid(row=0, column=0, sticky='nswe', padx=(0, 6))
        rules_card.grid_columnconfigure(0, weight=1)
        rules_card.grid_rowconfigure(2, weight=1)
        top = ctk.CTkFrame(rules_card, fg_color='transparent')
        top.grid(row=0, column=0, sticky='we', padx=16, pady=(14, 0))
        top.grid_columnconfigure(0, weight=1)
        heading(top, "Known screens").grid(row=0, column=0, sticky='w')
        ctk.CTkButton(top, text="Reload", width=70, height=28, corner_radius=8, fg_color=NEUTRAL,
                      hover_color=NEUTRAL_HOVER, command=self.refresh_screens).grid(row=0, column=1, padx=(0, 6))
        ctk.CTkButton(top, text="Edit rules.json", width=120, height=28, corner_radius=8, fg_color=ACCENT,
                      hover_color=ACCENT_HOVER, command=self.edit_rules).grid(row=0, column=2)
        ctk.CTkLabel(rules_card, text="When a menu is open, the bot reads the text on screen. A screen "
                                      "matches when all of its words are visible; the bot then clicks "
                                      "its button, or waits.", text_color=MUTED, justify='left',
                     anchor='w', wraplength=600, font=ctk.CTkFont(FONT, 12)).grid(
            row=1, column=0, sticky='we', padx=16, pady=(4, 8))
        self.screen_list = ctk.CTkScrollableFrame(rules_card, fg_color='transparent')
        self.screen_list.grid(row=2, column=0, sticky='nswe', padx=8, pady=(0, 8))
        self.screen_list.grid_columnconfigure(0, weight=1)
        self.never_label = ctk.CTkLabel(rules_card, text="", text_color='#fca5a5', justify='left', anchor='w',
                                        wraplength=600, font=ctk.CTkFont(FONT, 12))
        self.never_label.grid(row=3, column=0, sticky='we', padx=16, pady=(0, 14))

        read_card = card(tab)
        read_card.grid(row=0, column=1, sticky='nswe', padx=(6, 0))
        read_card.grid_columnconfigure(0, weight=1)
        read_card.grid_rowconfigure(3, weight=1)
        heading(read_card, "Check a screen").grid(row=0, column=0, sticky='w', padx=16, pady=(14, 0))
        ctk.CTkLabel(read_card, text="Open a screen in the game and press Read. After 3 seconds the bot "
                                     "reads the game window and shows what it sees and which rule matches.",
                     text_color=MUTED, justify='left', anchor='w', wraplength=380,
                     font=ctk.CTkFont(FONT, 12)).grid(row=1, column=0, sticky='we', padx=16, pady=(4, 8))
        self.read_btn = ctk.CTkButton(read_card, text="Read screen in 3s", height=36, corner_radius=8,
                                      fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self.read_screen)
        self.read_btn.grid(row=2, column=0, sticky='w', padx=16, pady=(0, 8))
        self.read_result = ctk.CTkTextbox(read_card, fg_color=BG, text_color='#c9ccd1', corner_radius=10,
                                          font=ctk.CTkFont(MONO, 12), wrap='word')
        self.read_result.grid(row=3, column=0, sticky='nswe', padx=12, pady=(0, 12))
        self.set_read_result("Nothing read yet.")

    def build_settings(self, tab):
        tab.grid_columnconfigure(0, weight=1)
        box = card(tab)
        box.grid(row=0, column=0, sticky='nwe')
        box.grid_columnconfigure(0, weight=1)
        heading(box, "Settings").grid(row=0, column=0, columnspan=2, sticky='w', padx=20, pady=(16, 4))
        ctk.CTkLabel(box, text="Changes are saved right away and used the next time you press Start.",
                     text_color=MUTED, font=ctk.CTkFont(FONT, 12)).grid(
            row=1, column=0, columnspan=2, sticky='w', padx=20, pady=(0, 8))

        row = 2

        def setting(label, help_text, widget_factory):
            nonlocal row
            ctk.CTkLabel(box, text=label, text_color=TEXT, anchor='w',
                         font=ctk.CTkFont(FONT, 14, 'bold')).grid(row=row, column=0, sticky='w', padx=20, pady=(12, 0))
            ctk.CTkLabel(box, text=help_text, text_color=MUTED, anchor='w', justify='left', wraplength=640,
                         font=ctk.CTkFont(FONT, 12)).grid(row=row + 1, column=0, sticky='w', padx=20, pady=(0, 12))
            widget = widget_factory(box)
            widget.grid(row=row, column=1, rowspan=2, sticky='e', padx=20)
            ctk.CTkFrame(box, fg_color=CARD_BORDER, height=1).grid(row=row + 2, column=0, columnspan=2,
                                                                    sticky='we', padx=20)
            row += 3
            return widget

        def game_widget(parent):
            frame = ctk.CTkFrame(parent, fg_color='transparent')
            ctk.CTkEntry(frame, textvariable=self.game, width=220, height=34,
                         placeholder_text="e.g. cod.exe").pack(side='left')
            self.detect_btn = ctk.CTkButton(frame, text="Detect", width=80, height=34, fg_color=NEUTRAL,
                                            hover_color=NEUTRAL_HOVER, command=self.detect_game)
            self.detect_btn.pack(side='left', padx=(8, 0))
            return frame

        def number(var):
            return lambda parent: ctk.CTkEntry(parent, textvariable=var, width=80, height=34, justify='center')

        def switch(var):
            return lambda parent: ctk.CTkSwitch(parent, text="", variable=var, progress_color=ACCENT)

        setting("Game", "The game's program name. Input only goes out while this window is in front. "
                        "Press Detect, then click on the game within 3 seconds.", game_widget)
        setting("Start delay (seconds)", "Time to switch to the game after pressing Start.", number(self.delay))
        setting("Menu read interval (seconds)", "How often the screen is read while a menu stays open. "
                                                "It also reads right when a menu opens and after each click.",
                number(self.read_interval))
        setting("Autojoin", "Press F5 every 2 seconds, also in menus.", switch(self.autojoin))
        setting("Anti-kick key", "Press L every 20 seconds while playing.", switch(self.anti_kick))

    # -- actions -----------------------------------------------------------------
    @staticmethod
    def number(var, default, kind=float):
        try:
            return kind(var.get())
        except (tk.TclError, ValueError):
            return default

    def save_settings(self):
        data = {'game': self.game.get().strip(),
                'autojoin': self.autojoin.get(), 'anti_kick': self.anti_kick.get(),
                'delay': max(0, self.number(self.delay, 5, int)),
                'read_interval': max(0.5, self.number(self.read_interval, 3.0))}
        try:
            with open(SETTINGS_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except OSError:
            pass
        return data

    def start_or_stop(self):
        if bot.is_started():
            self.stop()
        else:
            self.start()

    def start(self, dry_run=False):
        data = self.save_settings()
        bot.READ_INTERVAL = data['read_interval']
        self.tabs.set("Dashboard")
        threading.Thread(target=bot.start, daemon=True, kwargs=dict(
            game=data['game'], autojoin_on=data['autojoin'],
            anti_kick_on=data['anti_kick'], delay=data['delay'], dry_run=dry_run)).start()

    def stop(self):
        threading.Thread(target=bot.stop, daemon=True).start()

    def toggle_pause(self):
        if bot.started.is_set():
            bot.toggle_pause()

    def detect_game(self):
        print("Click on the game window within 3 seconds...")
        self.detect_btn.configure(text="Click game…", state='disabled')

        def later():
            exe, title = bot.foreground_window()
            value = exe or title
            if value and 'python' not in value.lower():
                self.root.after(0, self.game.set, value)
                print(f"Game set to {value}.")
            else:
                print("Couldn't tell which game that was; type its exe name instead.")
            self.root.after(0, lambda: self.detect_btn.configure(text="Detect", state='normal'))
        self.root.after(3000, lambda: threading.Thread(target=later, daemon=True).start())

    def refresh_screens(self):
        bot.rules = bot.load_rules()
        for child in self.screen_list.winfo_children():
            child.destroy()
        if not bot.rules['screens']:
            ctk.CTkLabel(self.screen_list, text="No screens in rules.json. Every menu stays paused.",
                         text_color=MUTED).grid(row=0, column=0, sticky='w', padx=8, pady=8)
        for i, screen in enumerate(bot.rules['screens']):
            row = ctk.CTkFrame(self.screen_list, fg_color=BG, corner_radius=10)
            row.grid(row=i, column=0, sticky='we', pady=4, padx=4)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(row, text=screen['name'], text_color=TEXT, anchor='w',
                         font=ctk.CTkFont(FONT, 14, 'bold')).grid(row=0, column=0, sticky='w', padx=14, pady=(10, 0))
            when = "during a match, " if screen['in_match'] else ""
            ctk.CTkLabel(row, text=f"{when}when it sees: " + "  +  ".join(screen['if_seen'])
                         + (f"  (not: {', '.join(screen['unless_seen'])})" if screen['unless_seen'] else ''), text_color=MUTED,
                         anchor='w', font=ctk.CTkFont(FONT, 12)).grid(row=1, column=0, sticky='w', padx=14, pady=(0, 10))
            if bot.screen_action(screen):
                c = chip(row, bot.screen_action(screen), '#1e3a5f')
            elif screen['in_match']:
                c = chip(row, "pause input", '#3b2f0b')
            else:
                c = chip(row, "wait", NEUTRAL_HOVER)
            c.grid(row=0, column=1, rowspan=2, padx=14)
        never = bot.rules['never_click_if_seen']
        self.never_label.configure(text=("⛔ Never clicks while any of these is on screen: " + ", ".join(never))
                                   if never else "")

    def edit_rules(self):
        try:
            os.startfile(bot.RULES_FILE)  # Windows: opens in the default editor
            print("Opened rules.json. Press Reload on the Screens tab after saving.")
        except AttributeError:
            print(f"Edit {bot.RULES_FILE} in a text editor, then press Reload.")
        except OSError as e:
            print(f"Couldn't open {bot.RULES_FILE}: {e}")

    def set_read_result(self, text):
        self.read_result.configure(state='normal')
        self.read_result.delete('1.0', 'end')
        self.read_result.insert('end', text)
        self.read_result.configure(state='disabled')

    def read_screen(self):
        if bot.is_started():
            self.set_read_result("Stop the bot first. During a test run the state shows what it sees.")
            return
        self.busy_reading = True
        self.read_btn.configure(text="Switch to the game…")
        self.set_read_result("Reading in 3 seconds. Switch to the game now.")

        def run():
            try:
                time.sleep(3)
                bot.rules = bot.load_rules()
                read = bot.read_screen()
                if read is None:
                    result = "Couldn't capture the screen. Use borderless or windowed mode."
                else:
                    screen, danger = bot.classify(read)
                    if danger:
                        verdict = f"⛔ '{danger}' is on screen: the bot never clicks here."
                    elif screen and screen['click']:
                        verdict = f"✔ Screen '{screen['name']}': clicks '{screen['click']}' at " \
                                  f"{bot.find_text(read, screen['click'])}."
                    elif screen and screen['press']:
                        verdict = f"✔ Screen '{screen['name']}': presses {screen['press'].upper()}."
                    elif screen:
                        verdict = f"✔ Screen '{screen['name']}': waits."
                    else:
                        verdict = "No rule matches. The bot sends nothing on this screen.\n" \
                                  "To add one, pick a few words below that are only on this screen."
                    words = "\n".join(f"  {text}" for text, _, _ in read['items'])
                    result = f"{verdict}\n\nText on screen:\n{words}"
            except Exception as e:
                result = f"Couldn't read the screen: {e}"
            self.root.after(0, self.set_read_result, result)
            self.root.after(0, lambda: self.read_btn.configure(text="Read screen in 3s"))
            self.busy_reading = False
        threading.Thread(target=run, daemon=True).start()

    def clear_log(self):
        self.log.configure(state='normal')
        self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')

    def on_close(self):
        self.save_settings()
        bot.stop()
        self.hotkeys.stop()
        self.root.destroy()

    # -- refresh -----------------------------------------------------------------
    @staticmethod
    def log_tag(line):
        if 'PLAYING' in line and line.startswith('['):
            return 'playing'
        if line.startswith('['):
            return 'state'
        if 'click' in line.lower():
            return 'click'
        if any(w in line for w in ('Warning', "Can't", "Couldn't", 'Not running', 'crashed', 'Tip')):
            return 'warn'
        return 'muted'

    def flush_log(self):
        chunks = []
        while not self.log_queue.empty():
            chunks.append(self.log_queue.get_nowait())
        if not chunks:
            return
        self.log.configure(state='normal')
        for line in ''.join(chunks).splitlines(keepends=True):
            self.log.insert('end', line, self.log_tag(line))
        self.log.see('end')
        self.log.configure(state='disabled')

    def update_stats(self, running):
        s = bot.stats
        if s['last_match'] is not self.seen_last_match:  # a match just ended: reload history
            self.seen_last_match = s['last_match']
            self.refresh_history()
        current = time.time() - bot.match['start'] if bot.match['start'] else 0.0
        if running and s['started'] is not None:
            self.run_seconds = time.monotonic() - s['started']
        last = s['last_match']
        values = {'running': bot.format_duration(self.run_seconds) if s['started'] else '–',
                  'match_time': bot.format_duration(s['match_time'] + current),
                  'total': bot.format_duration(self.history_total + current),
                  'matches': str(s['matches']),
                  'last': f"{bot.format_duration(last[1])} · {last[0]}" if last else '–',
                  'kicks': str(s['kicks'])}
        for key, value in values.items():
            self.stat_values[key].configure(text=value)

    def poll(self):
        self.flush_log()

        # A crash or F9 sets stop_event; finish stopping cleanly.
        running = bot.is_started()
        if bot.stop_event.is_set() and running and not self.stopping:
            self.stopping = True
            self.stop()
        elif not running:
            self.stopping = False

        name, detail, lasted = bot.current_state()
        color, explanation = STATE_STYLE.get(name, ('#52525b', ''))
        if bot.DRY_RUN and running and name == bot.PLAYING:
            explanation = "In a match (test run: sending nothing)"
        self.dot.configure(text_color=color)
        self.state_label.configure(text=name.title(), text_color=color if name != bot.STOPPED else TEXT)
        self.detail_label.configure(text=detail or explanation)
        self.timer_label.configure(text=bot.format_duration(lasted) if name != bot.STOPPED else '')
        if bot.DRY_RUN and running:
            self.test_badge.grid(row=2, column=1, columnspan=2, sticky='w', pady=(2, 0))
        else:
            self.test_badge.grid_remove()
        self.root.title(f"Anti-AFK · {name.title()}")
        self.flow.show(name, detail, color)
        self.update_stats(running)

        # Warnings in the header.
        if bot.is_admin():
            self.admin_chip.pack_forget()
        else:
            self.admin_chip.pack(side='left', padx=4)
        if self.game.get().strip():
            self.game_chip.pack_forget()
        else:
            self.game_chip.pack(side='left', padx=4)

        # Buttons.
        if running:
            self.main_btn.configure(text="■  Stop", fg_color=DANGER, hover_color=DANGER_HOVER,
                                    state='normal' if not self.stopping else 'disabled')
        else:
            self.main_btn.configure(text="▶  Start", fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                    state='disabled' if self.busy_reading else 'normal')
        self.test_btn.configure(state='normal' if not running and not self.busy_reading else 'disabled')
        paused = not bot.running.is_set()
        self.pause_btn.configure(state='normal' if running and bot.started.is_set() else 'disabled',
                                 text="▶  Resume" if paused else "❚❚  Pause")
        self.read_btn.configure(state='disabled' if running or self.busy_reading else 'normal')
        self.root.after(200, self.poll)


def main():
    ctk.set_appearance_mode('dark')
    ctk.set_default_color_theme('blue')
    root = ctk.CTk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
