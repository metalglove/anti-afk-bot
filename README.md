# anti-afk-bot

Keeps you from getting kicked for inactivity while you're away from the game.

## Setup

```
pip install -r requirements.txt
```

## Run

Double-click **AntiAFK.bat**. It asks for administrator rights (the game ignores input
from programs that don't have them) and opens the app.

1. Click **Detect** and then click on the game, or type its exe name (e.g. `cod.exe`).
2. Click **Start** and switch to the game.

The big bar at the top shows what the bot is doing right now. **Test (send nothing)** runs
everything except the input, so you can walk through the menus and check that each screen
is recognized before letting it play. Settings are remembered between runs.

| Key | Action |
| --- | --- |
| F8 | Pause / resume |
| F9 | Stop |

Using automation in online games may break the game's terms of service and can get your
account banned.

Prefer the terminal? `python anti_afk.py --game cod.exe` does the same (`--help` for options,
`--test` for test mode). Run it from an administrator terminal.

## What the bot is doing

The terminal prints every state change with the time, and the terminal's title shows the
current state:

```
[21:04:12] SCREEN: main_menu (clicking)   (after 3m10s MENU)
[21:04:40] MENU: unknown, mouse cursor is showing, nothing sent   (after 28s SCREEN)
[21:05:31] SCREEN: class_select (clicking)   (after 51s MENU)
[21:05:34] RESUMING: playing in 1.5s   (after 3s SCREEN)
[21:05:35] PLAYING   (after 1s RESUMING)
```

| State | Meaning |
| --- | --- |
| STARTING | Countdown before it begins |
| PAUSED | You pressed F8 |
| NOT FOCUSED | The game isn't the window in front |
| PLAYING | In a match: the only state where it moves, shoots and presses keys |
| RESUMING | A menu just closed; it waits a moment before playing |
| SCREEN | A screen you taught it (see below); it clicks the spot you picked |
| MENU | A menu it doesn't know, like the store: it sends nothing |

## Menus and the store

Input pauses whenever the mouse cursor is showing, which is how the game looks in menus,
the lobby and the store, so the bot can't click its way into a purchase. It resumes a
moment after the menu closes. Run `python anti_afk.py --show-menu` and open a few menus
to check that it says `MENU` there and `playing` in a match.

### Class selection and other screens you want clicked

The bot can recognize screens you teach it. On the class selection screen, click **Learn screen...** in the
app (or run `python anti_afk.py --learn class_select`) and follow the prompts in the log. Press F7 on the top-left and
then the bottom-right corner of something that is only on that screen and doesn't change,
like its title. Then press F7 on what it should click, such as your class. From then on it
clicks that spot whenever it sees that screen. Every other menu, including the store, stays
paused. `--show-menu` shows which screen it recognizes and how close the match is
(`--match-threshold` makes matching looser or stricter). Learned screens are saved in
`screens/`, so delete a file there to forget a screen. Positions are saved relative to the
game window, so moving or resizing the window, or changing the resolution, is fine as
long as the menu scales with it. Changing the aspect ratio (e.g. 16:9 to 21:9) usually
moves menu items, so learn the screen again after that. Screens can't be read in
exclusive fullscreen; use borderless or windowed mode.

### Games that draw their own menu cursor

If it still says `playing` in a menu (some games draw their own cursor), pick a spot of
the HUD that is only there while playing, such as part of the minimap frame. Take an
in-game screenshot and read its X,Y in Paint, then run
`python anti_afk.py --show-menu --ingame-pixel X,Y` in a match to read its color, and
start the bot with `--ingame-pixel X,Y,RRGGBB`. Input then only goes out while that pixel
matches. This needs borderless or windowed mode; in exclusive fullscreen the pixel can't
be read and the bot stays paused.

Using automation in online games may break the game's terms of service and can get your
account banned.

More options: `python anti_afk.py --help`
