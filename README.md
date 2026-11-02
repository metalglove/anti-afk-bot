# anti-afk-bot

Sends randomized movement, look, aim, crouch and weapon-switch inputs to keep a game session from idling out.

## Setup

```
pip install -r requirements.txt
```

## Usage

```
python anti_afk.py                       # human mode (default): one action at a time, natural timing
python anti_afk.py --game cod.exe        # only send input while the game is in front
python anti_afk.py --mode fast           # old constant random input
python anti_afk.py --mode slow           # old slower, subtler input
python anti_afk.py --autojoin --anti-kick  # also press F5 every 2s / 'l' every 20s
python anti_afk.py --show-focus          # print the focused program, to find the --game value
```

Input starts after a 5-second countdown (`--delay N` to change). With `--game`, input
pauses whenever another window is in front (windowed, borderless and fullscreen all work)
and resumes when you switch back.

Human mode walks, strafes, jumps and shoots regularly. It never stands still longer than
`--max-still` seconds (default 10) and always fires at least once every 20 seconds
(`MAX_NO_SHOT` at the top of `anti_afk.py`).

If turning looks sluggish at your in-game sensitivity, raise `--look-scale` (default 3);
lower it if it spins too far. If the camera doesn't turn at all, set `MOUSE_RELATIVE = True`.

| Hotkey | Action |
| --- | --- |
| F8 | Pause / resume (releases held keys) |
| F9 | Stop and release all keys |

If the game runs as administrator, run the script from an administrator terminal too,
otherwise the game ignores its input and F8/F9 don't work while the game is focused.
The script warns at startup when it isn't elevated.

Using automation in online games may violate their terms of service.

## AutoHotkey alternative

`ahk/skilledlobby.ahk` is a simpler AutoHotkey v1 script for comparison: a fixed 27-second
loop (walk, jump, strafe, click). F3 starts it, F6 pauses it. Keys held at the moment
you pause stay held, and the only way to stop it is to exit AutoHotkey.
