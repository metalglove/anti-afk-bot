"""Anti-AFK bot: window version.

Start it with AntiAFK.bat (asks for administrator rights), or `python app.py`.
Same engine as anti_afk.py; this adds a window with the live state, buttons,
known screens and a log. Settings are remembered in settings.json.
"""
import json
import os
import queue
import sys
import threading
import tkinter as tk

import customtkinter as ctk

import anti_afk as bot

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, 'settings.json')
DEFAULTS = {'game': '', 'autojoin': False, 'anti_kick': False, 'delay': 5,
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

STATE_STYLE = {  # dot color, short explanation
    bot.PLAYING: ('#22c55e', "In a match: moving, looking and shooting"),
    bot.RESUMING: ('#86efac', "Menu closed, about to play"),
    bot.SCREEN: ('#3b82f6', "Known screen"),
    bot.MENU: ('#f59e0b', "Unknown menu: sending nothing"),
    bot.PAUSED: ('#a1a1aa', "Paused"),
    bot.NOT_FOCUSED: ('#a1a1aa', "Waiting for the game window"),
    bot.STARTING: ('#eab308', "Starting"),
    bot.STOPPED: ('#52525b', "Not running"),
}

FONT = 'Segoe UI'
MONO = 'Consolas'


class QueueWriter:
    """Sends print() output from every thread to the log box."""

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
            return {**DEFAULTS, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def card(parent, **kw):
    return ctk.CTkFrame(parent, fg_color=CARD, border_color=CARD_BORDER, border_width=1,
                        corner_radius=14, **kw)


def heading(parent, text):
    return ctk.CTkLabel(parent, text=text.upper(), text_color=MUTED,
                        font=ctk.CTkFont(FONT, 11, 'bold'))


class App:
    def __init__(self, root):
        self.root = root
        self.log_queue = queue.Queue()
        self.busy_reading = False
        self.stopping = False
        sys.stdout = sys.stderr = QueueWriter(self.log_queue)

        settings = load_settings()
        self.game = tk.StringVar(value=settings['game'])
        self.autojoin = tk.BooleanVar(value=settings['autojoin'])
        self.anti_kick = tk.BooleanVar(value=settings['anti_kick'])
        self.delay = tk.StringVar(value=str(settings['delay']))
        self.read_interval = tk.StringVar(value=f"{settings['read_interval']:g}")

        root.title("Anti-AFK")
        root.geometry('640x820')
        root.minsize(560, 700)
        root.configure(fg_color=BG)
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        root.bind('<FocusIn>', lambda e: self.refresh_screens() if e.widget is root else None)
        self.build()

        self.hotkeys = bot.Listener(on_press=bot.on_hotkey)
        self.hotkeys.start()
        self.refresh_screens()
        if not bot.is_admin():
            print("Not running as administrator: the game may ignore the bot. "
                  "Start the app with AntiAFK.bat.")
        self.poll()

    # -- layout ----------------------------------------------------------------
    def build(self):
        root = self.root
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(4, weight=1)

        # Status card.
        status = card(root)
        status.grid(row=0, column=0, sticky='we', padx=16, pady=(16, 8))
        status.grid_columnconfigure(1, weight=1)
        self.dot = ctk.CTkLabel(status, text='●', font=ctk.CTkFont(FONT, 34), width=40)
        self.dot.grid(row=0, column=0, rowspan=2, padx=(18, 6), pady=16)
        self.state_label = ctk.CTkLabel(status, text="STOPPED", text_color=TEXT, anchor='w',
                                        font=ctk.CTkFont(FONT, 26, 'bold'))
        self.state_label.grid(row=0, column=1, sticky='w', pady=(16, 0))
        self.detail_label = ctk.CTkLabel(status, text="", text_color=MUTED, anchor='w',
                                         font=ctk.CTkFont(FONT, 13))
        self.detail_label.grid(row=1, column=1, sticky='w', pady=(0, 16))
        self.timer_label = ctk.CTkLabel(status, text="", text_color=MUTED,
                                        font=ctk.CTkFont(MONO, 15))
        self.timer_label.grid(row=1, column=2, padx=18, sticky='e', pady=(0, 16))
        self.test_badge = ctk.CTkLabel(status, text=" TEST MODE ", fg_color='#3f3f46',
                                       corner_radius=8, text_color=TEXT,
                                       font=ctk.CTkFont(FONT, 11, 'bold'))

        # Controls.
        controls = ctk.CTkFrame(root, fg_color='transparent')
        controls.grid(row=1, column=0, sticky='we', padx=16, pady=4)
        for i in range(4):
            controls.grid_columnconfigure(i, weight=1, uniform='buttons')
        big = ctk.CTkFont(FONT, 14, 'bold')
        self.start_btn = ctk.CTkButton(controls, text="▶  Start", height=42, font=big,
                                       corner_radius=10, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                       command=self.start)
        self.test_btn = ctk.CTkButton(controls, text="Test run", height=42, font=big,
                                      corner_radius=10, fg_color='transparent', border_width=1,
                                      border_color=CARD_BORDER, hover_color=CARD,
                                      command=lambda: self.start(dry_run=True))
        self.pause_btn = ctk.CTkButton(controls, text="❚❚  Pause", height=42, font=big,
                                       corner_radius=10, fg_color='#27272a', hover_color='#3f3f46',
                                       command=self.toggle_pause)
        self.stop_btn = ctk.CTkButton(controls, text="■  Stop", height=42, font=big,
                                      corner_radius=10, fg_color=DANGER, hover_color=DANGER_HOVER,
                                      command=self.stop)
        for i, b in enumerate((self.start_btn, self.test_btn, self.pause_btn, self.stop_btn)):
            b.grid(row=0, column=i, sticky='we', padx=4)
        ctk.CTkLabel(root, text="F8 pause / resume   ·   F9 stop   ·   Test run sends no input",
                     text_color=MUTED, font=ctk.CTkFont(FONT, 11)).grid(row=2, column=0, pady=(0, 4))

        # Settings + known screens side by side.
        middle = ctk.CTkFrame(root, fg_color='transparent')
        middle.grid(row=3, column=0, sticky='we', padx=12, pady=4)
        middle.grid_columnconfigure((0, 1), weight=1, uniform='middle')

        settings = card(middle)
        settings.grid(row=0, column=0, sticky='nswe', padx=4)
        settings.grid_columnconfigure(0, weight=1)
        heading(settings, "Settings").grid(row=0, column=0, columnspan=2, sticky='w', padx=16, pady=(14, 6))
        game_row = ctk.CTkFrame(settings, fg_color='transparent')
        game_row.grid(row=1, column=0, columnspan=2, sticky='we', padx=16, pady=4)
        game_row.grid_columnconfigure(0, weight=1)
        self.game_entry = ctk.CTkEntry(game_row, textvariable=self.game, height=34,
                                       placeholder_text="game exe, e.g. cod.exe")
        self.game_entry.grid(row=0, column=0, sticky='we')
        self.detect_btn = ctk.CTkButton(game_row, text="Detect", width=70, height=34,
                                        fg_color='#27272a', hover_color='#3f3f46',
                                        command=self.detect_game)
        self.detect_btn.grid(row=0, column=1, padx=(6, 0))

        def number_row(row, label, var):
            ctk.CTkLabel(settings, text=label, text_color=TEXT, anchor='w').grid(
                row=row, column=0, sticky='w', padx=16, pady=3)
            entry = ctk.CTkEntry(settings, textvariable=var, width=60, height=30, justify='center')
            entry.grid(row=row, column=1, sticky='e', padx=16, pady=3)
            return entry
        number_row(2, "Start delay (s)", self.delay)
        number_row(3, "Menu read interval (s)", self.read_interval)
        ctk.CTkSwitch(settings, text="Autojoin (F5, also in menus)", variable=self.autojoin,
                      progress_color=ACCENT).grid(row=4, column=0, columnspan=2, sticky='w', padx=16, pady=(8, 3))
        ctk.CTkSwitch(settings, text="Anti-kick (L every 20s)", variable=self.anti_kick,
                      progress_color=ACCENT).grid(row=5, column=0, columnspan=2, sticky='w', padx=16, pady=(3, 14))

        screens = card(middle)
        screens.grid(row=0, column=1, sticky='nswe', padx=4)
        screens.grid_columnconfigure(0, weight=1)
        screens.grid_rowconfigure(1, weight=1)
        top = ctk.CTkFrame(screens, fg_color='transparent')
        top.grid(row=0, column=0, sticky='we', padx=16, pady=(14, 6))
        top.grid_columnconfigure(0, weight=1)
        heading(top, "Known screens").grid(row=0, column=0, sticky='w')
        self.read_btn = ctk.CTkButton(top, text="Read screen", width=96, height=28, corner_radius=8,
                                      fg_color='#27272a', hover_color='#3f3f46',
                                      command=self.read_screen)
        self.read_btn.grid(row=0, column=1, padx=(0, 6))
        self.edit_btn = ctk.CTkButton(top, text="Edit", width=54, height=28, corner_radius=8,
                                      fg_color=ACCENT, hover_color=ACCENT_HOVER, command=self.edit_rules)
        self.edit_btn.grid(row=0, column=2)
        self.screen_list = ctk.CTkScrollableFrame(screens, fg_color='transparent', height=190)
        self.screen_list.grid(row=1, column=0, sticky='nswe', padx=8, pady=(0, 10))
        self.screen_list.grid_columnconfigure(0, weight=1)

        # Log.
        log_card = card(root)
        log_card.grid(row=4, column=0, sticky='nswe', padx=16, pady=(8, 16))
        log_card.grid_columnconfigure(0, weight=1)
        log_card.grid_rowconfigure(1, weight=1)
        heading(log_card, "Activity").grid(row=0, column=0, sticky='w', padx=16, pady=(14, 4))
        self.log = ctk.CTkTextbox(log_card, fg_color=BG, text_color='#c9ccd1', corner_radius=10,
                                  font=ctk.CTkFont(MONO, 12), wrap='word')
        self.log.grid(row=1, column=0, sticky='nswe', padx=12, pady=(0, 12))
        self.log.configure(state='disabled')

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
        except OSError as e:
            print(f"Couldn't save settings: {e}")
        return data

    def start(self, dry_run=False):
        data = self.save_settings()
        bot.READ_INTERVAL = data['read_interval']
        if not data['game']:
            print("Tip: set the game (Detect) so the bot only acts while it is in front.")
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

        def later():
            exe, title = bot.foreground_window()
            value = exe or title
            if value and 'python' not in value.lower():
                self.root.after(0, self.game.set, value)
                print(f"Game set to {value}.")
            else:
                print("Couldn't tell which game that was; type its exe name instead.")
        self.root.after(3000, lambda: threading.Thread(target=later, daemon=True).start())

    def refresh_screens(self):
        bot.rules = bot.load_rules()
        for child in self.screen_list.winfo_children():
            child.destroy()
        if not bot.rules['screens']:
            ctk.CTkLabel(self.screen_list, text="No screens in rules.json.\nEvery menu stays paused.",
                         text_color=MUTED, justify='left').grid(row=0, column=0, sticky='w', padx=8, pady=8)
        for i, screen in enumerate(bot.rules['screens']):
            row = ctk.CTkFrame(self.screen_list, fg_color=BG, corner_radius=8)
            row.grid(row=i, column=0, sticky='we', pady=3)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(row, text=screen['name'], text_color=TEXT, anchor='w',
                         font=ctk.CTkFont(FONT, 13, 'bold')).grid(row=0, column=0, sticky='w', padx=10, pady=(6, 0))
            ctk.CTkLabel(row, text='"' + '" + "'.join(screen['if_seen']) + '"', text_color=MUTED, anchor='w',
                         font=ctk.CTkFont(FONT, 11)).grid(row=1, column=0, sticky='w', padx=10, pady=(0, 6))
            clicks = bool(screen['click'])
            ctk.CTkLabel(row, text=f" click {screen['click']} " if clicks else " wait ", corner_radius=6,
                         fg_color='#1e3a5f' if clicks else '#3f3f46', text_color=TEXT,
                         font=ctk.CTkFont(FONT, 11)).grid(row=0, column=1, rowspan=2, padx=8)
        never = bot.rules['never_click_if_seen']
        if never:
            ctk.CTkLabel(self.screen_list, text="Never clicks with " + ", ".join(never) + " on screen.",
                         text_color=MUTED, wraplength=230, justify='left', font=ctk.CTkFont(FONT, 11)).grid(
                row=len(bot.rules['screens']), column=0, sticky='w', padx=8, pady=(6, 2))

    def edit_rules(self):
        """Open rules.json in the default editor; the list reloads when the window is focused."""
        try:
            os.startfile(bot.RULES_FILE)  # Windows
        except AttributeError:
            print(f"Edit {bot.RULES_FILE} in a text editor.")
        except OSError as e:
            print(f"Couldn't open {bot.RULES_FILE}: {e}")

    def read_screen(self):
        if bot.is_started():
            print("Stop the bot first, or watch the state during a test run.")
            return
        self.busy_reading = True

        def run():
            try:
                bot.print_screen_text(delay=3)
            except Exception as e:
                print(f"Couldn't read the screen: {e}")
            finally:
                self.busy_reading = False
        threading.Thread(target=run, daemon=True).start()

    def on_close(self):
        self.save_settings()
        bot.stop()
        self.hotkeys.stop()
        self.root.destroy()

    # -- refresh -----------------------------------------------------------------
    def poll(self):
        chunks = []
        while not self.log_queue.empty():
            chunks.append(self.log_queue.get_nowait())
        if chunks:
            self.log.configure(state='normal')
            self.log.insert('end', ''.join(chunks))
            self.log.see('end')
            self.log.configure(state='disabled')

        # A crash or F9 sets stop_event; finish stopping cleanly.
        running = bot.is_started()
        if bot.stop_event.is_set() and running and not self.stopping:
            self.stopping = True
            self.stop()
        elif not running:
            self.stopping = False

        name, detail, lasted = bot.current_state()
        color, explanation = STATE_STYLE.get(name, ('#52525b', ''))
        self.dot.configure(text_color=color)
        self.state_label.configure(text=name.title(), text_color=color if name != bot.STOPPED else TEXT)
        if bot.DRY_RUN and running and name == bot.PLAYING:
            explanation = "In a match (test run: sending nothing)"
        self.detail_label.configure(text=detail or explanation)
        self.timer_label.configure(text=bot.format_duration(lasted) if name != bot.STOPPED else '')
        if bot.DRY_RUN and running:
            self.test_badge.grid(row=0, column=2, padx=18, sticky='e', pady=(16, 0))
        else:
            self.test_badge.grid_remove()
        self.root.title(f"Anti-AFK · {name.title()}")

        idle = not running and not self.busy_reading
        for widget in (self.start_btn, self.test_btn, self.read_btn, self.detect_btn):
            widget.configure(state='normal' if idle else 'disabled')
        self.stop_btn.configure(state='normal' if running else 'disabled')
        paused = not bot.running.is_set()
        self.pause_btn.configure(state='normal' if running and bot.started.is_set() else 'disabled',
                                 text="▶  Resume" if paused else "❚❚  Pause")
        self.root.after(200, self.poll)


def main():
    ctk.set_appearance_mode('dark')
    ctk.set_default_color_theme('blue')
    root = ctk.CTk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
