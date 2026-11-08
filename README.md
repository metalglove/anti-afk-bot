# anti-afk-bot

Keeps you from getting kicked for inactivity while you're away from the game.

## Setup

```
pip install -r requirements.txt
```

## Run

```
python anti_afk.py --game cod.exe
```

Then switch to the game. It only plays while the game window is in front.

| Key | Action |
| --- | --- |
| F8 | Pause / resume |
| F9 | Stop |

If nothing happens in-game, run it from an administrator terminal.

## Menus and the store

Input pauses whenever the mouse cursor is showing, which is how the game looks in menus,
the lobby and the store, so the bot can't click its way into a purchase. It resumes a
moment after the menu closes. Run `python anti_afk.py --show-menu` and open a few menus
to check that it says `MENU` there and `playing` in a match.

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
