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

Using automation in online games may break the game's terms of service and can get your
account banned.

More options: `python anti_afk.py --help`
