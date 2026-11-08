"""Anti-AFK bot: window version.

Start it with AntiAFK.bat (asks for administrator rights), or `python app.py`.
Same engine as anti_afk.py; this adds a window with the live state, buttons,
learned screens and a log. Settings are remembered in settings.json.
"""
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

import anti_afk as bot

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, 'settings.json')
DEFAULTS = {'game': '', 'mode': 'human', 'autojoin': False, 'anti_kick': False, 'delay': 5,
            'match_threshold': bot.MATCH_THRESHOLD}

STATE_COLORS = {  # background, text
    bot.PLAYING: ('#1f7a3a', 'white'),
    bot.RESUMING: ('#4f8a5b', 'white'),
    bot.SCREEN: ('#1f5f9e', 'white'),
    bot.MENU: ('#b8661a', 'white'),
    bot.PAUSED: ('#6b6b6b', 'white'),
    bot.NOT_FOCUSED: ('#6b6b6b', 'white'),
    bot.STARTING: ('#8a7a1f', 'white'),
    bot.STOPPED: ('#3a3a3a', 'white'),
}


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


class App:
    def __init__(self, root):
        self.root = root
        self.log_queue = queue.Queue()
        self.busy_learning = False
        self.stopping = False
        sys.stdout = sys.stderr = QueueWriter(self.log_queue)

        settings = load_settings()
        self.game = tk.StringVar(value=settings['game'])
        self.mode = tk.StringVar(value=settings['mode'])
        self.autojoin = tk.BooleanVar(value=settings['autojoin'])
        self.anti_kick = tk.BooleanVar(value=settings['anti_kick'])
        self.delay = tk.IntVar(value=settings['delay'])
        self.threshold = tk.DoubleVar(value=settings['match_threshold'])

        root.title("Anti-AFK")
        root.minsize(560, 620)
        root.protocol('WM_DELETE_WINDOW', self.on_close)
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
        pad = {'padx': 8, 'pady': 4}

        self.state_label = tk.Label(self.root, text="STOPPED", font=('Segoe UI', 22, 'bold'),
                                    pady=10)
        self.state_label.pack(fill='x')
        self.detail_label = tk.Label(self.root, text="", font=('Segoe UI', 10), pady=4)
        self.detail_label.pack(fill='x')

        settings = ttk.LabelFrame(self.root, text="Settings")
        settings.pack(fill='x', **pad)
        ttk.Label(settings, text="Game").grid(row=0, column=0, sticky='w', **pad)
        ttk.Entry(settings, textvariable=self.game, width=28).grid(row=0, column=1, sticky='we', **pad)
        ttk.Button(settings, text="Detect", command=self.detect_game).grid(row=0, column=2, **pad)
        ttk.Label(settings, text="Mode").grid(row=1, column=0, sticky='w', **pad)
        ttk.Combobox(settings, textvariable=self.mode, values=list(bot.MODES), state='readonly',
                     width=10).grid(row=1, column=1, sticky='w', **pad)
        ttk.Label(settings, text="Start delay (s)").grid(row=2, column=0, sticky='w', **pad)
        ttk.Spinbox(settings, from_=0, to=60, textvariable=self.delay, width=6).grid(
            row=2, column=1, sticky='w', **pad)
        ttk.Label(settings, text="Screen match threshold").grid(row=3, column=0, sticky='w', **pad)
        ttk.Spinbox(settings, from_=5, to=80, increment=5, textvariable=self.threshold,
                    width=6).grid(row=3, column=1, sticky='w', **pad)
        ttk.Checkbutton(settings, text="Autojoin (press F5 every 2s, also in menus)",
                        variable=self.autojoin).grid(row=4, column=0, columnspan=3, sticky='w', **pad)
        ttk.Checkbutton(settings, text="Anti-kick (press L every 20s)",
                        variable=self.anti_kick).grid(row=5, column=0, columnspan=3, sticky='w', **pad)
        settings.columnconfigure(1, weight=1)

        buttons = ttk.Frame(self.root)
        buttons.pack(fill='x', **pad)
        self.start_btn = ttk.Button(buttons, text="Start", command=self.start)
        self.test_btn = ttk.Button(buttons, text="Test (send nothing)",
                                   command=lambda: self.start(dry_run=True))
        self.pause_btn = ttk.Button(buttons, text="Pause (F8)", command=self.toggle_pause)
        self.stop_btn = ttk.Button(buttons, text="Stop (F9)", command=self.stop)
        for b in (self.start_btn, self.test_btn, self.pause_btn, self.stop_btn):
            b.pack(side='left', expand=True, fill='x', padx=3)

        screens = ttk.LabelFrame(self.root, text="Learned screens")
        screens.pack(fill='x', **pad)
        self.screen_list = tk.Listbox(screens, height=5)
        self.screen_list.pack(side='left', fill='both', expand=True, padx=8, pady=6)
        side = ttk.Frame(screens)
        side.pack(side='left', fill='y', padx=4, pady=6)
        self.learn_btn = ttk.Button(side, text="Learn screen...", command=self.learn)
        self.learn_btn.pack(fill='x', pady=2)
        self.delete_btn = ttk.Button(side, text="Delete", command=self.delete_screen)
        self.delete_btn.pack(fill='x', pady=2)

        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.pack(fill='both', expand=True, **pad)
        self.log = ScrolledText(log_frame, height=10, state='disabled', font=('Consolas', 9))
        self.log.pack(fill='both', expand=True, padx=6, pady=6)

    # -- actions -----------------------------------------------------------------
    def save_settings(self):
        data = {'game': self.game.get().strip(), 'mode': self.mode.get(),
                'autojoin': self.autojoin.get(), 'anti_kick': self.anti_kick.get(),
                'delay': self.safe_int(self.delay, 5), 'match_threshold': self.safe_float(self.threshold, 25)}
        try:
            with open(SETTINGS_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except OSError as e:
            print(f"Couldn't save settings: {e}")
        return data

    @staticmethod
    def safe_int(var, default):
        try:
            return int(var.get())
        except (tk.TclError, ValueError):
            return default

    @staticmethod
    def safe_float(var, default):
        try:
            return float(var.get())
        except (tk.TclError, ValueError):
            return default

    def start(self, dry_run=False):
        data = self.save_settings()
        bot.MATCH_THRESHOLD = data['match_threshold']
        if not data['game']:
            print("Tip: set the game (Detect button) so the bot only acts while it is in front.")
        threading.Thread(target=bot.start, daemon=True, kwargs=dict(
            mode=data['mode'], game=data['game'], autojoin_on=data['autojoin'],
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
        bot.screens = bot.load_screens()
        self.screen_list.delete(0, 'end')
        for screen in bot.screens:
            action = "clicks" if screen['click'] else "waits"
            self.screen_list.insert('end', f"{screen['name']}  ({action})")

    def learn(self):
        if bot.is_started():
            messagebox.showinfo("Anti-AFK", "Stop the bot before learning a screen.")
            return
        name = simpledialog.askstring(
            "Learn screen", "Name for this screen (e.g. main_menu, class_select):", parent=self.root)
        if not name:
            return
        self.busy_learning = True

        def run():
            try:
                bot.learn_screen(name.strip())
            finally:
                self.busy_learning = False
                bot.stop_event.clear()  # F9 ends learning; don't leave the bot "stopped"
                self.root.after(0, self.refresh_screens)
        threading.Thread(target=run, daemon=True).start()

    def delete_screen(self):
        selected = self.screen_list.curselection()
        if not selected:
            return
        name = bot.screens[selected[0]]['name']
        if not messagebox.askyesno("Anti-AFK", f"Forget the screen '{name}'?"):
            return
        for ext in ('.json', '.png'):
            try:
                os.remove(os.path.join(bot.SCREENS_DIR, name + ext))
            except OSError:
                pass
        print(f"Forgot screen '{name}'.")
        self.refresh_screens()

    def on_close(self):
        self.save_settings()
        bot.stop()
        self.hotkeys.stop()
        self.root.destroy()

    # -- refresh -----------------------------------------------------------------
    def poll(self):
        # Log output from all threads.
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
        bg, fg = STATE_COLORS.get(name, ('#3a3a3a', 'white'))
        title = name + (" (test)" if bot.DRY_RUN and running else "")
        self.state_label.configure(text=title, bg=bg, fg=fg)
        self.detail_label.configure(text=f"{detail}   ·   {bot.format_duration(lasted)}" if detail
                                    else bot.format_duration(lasted))
        self.root.title(f"Anti-AFK: {name}")

        idle = not running and not self.busy_learning
        for widget in (self.start_btn, self.test_btn, self.learn_btn, self.delete_btn):
            widget.configure(state='normal' if idle else 'disabled')
        self.stop_btn.configure(state='normal' if running else 'disabled')
        self.pause_btn.configure(state='normal' if running and bot.started.is_set() else 'disabled',
                                 text="Resume (F8)" if not bot.running.is_set() else "Pause (F8)")
        self.root.after(200, self.poll)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
