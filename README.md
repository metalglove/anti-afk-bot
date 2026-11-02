# anti-afk-bot

Sends randomized movement, look, aim, crouch and weapon-switch inputs to keep a game session from idling out.

## Setup

```
pip install -r requirements.txt
```

## Usage

```
python anti_afk.py                       # fast mode (default)
python anti_afk.py --mode slow           # slower, subtler inputs
python anti_afk.py --mode human          # one action at a time, natural mouse and timing
python anti_afk.py --autojoin --anti-kick  # also press F5 every 2s / 'l' every 20s
python anti_afk.py --mode human --game cod.exe  # only send input while the game is in front
python anti_afk.py --show-focus          # print the focused program, to find the --game value
```

Input starts after a 5-second countdown (`--delay N` to change). With `--game`, input
pauses whenever another window is in front (windowed, borderless and fullscreen all work)
and resumes when you switch back.

In human mode, `MAX_STILL` (top of `anti_afk.py`, default 20s) is the longest it will stand
still. Set it well below your game's AFK-kick time. If the camera doesn't turn, set
`MOUSE_RELATIVE = True`.

| Hotkey | Action |
| --- | --- |
| F8 | Pause / resume (releases held keys) |
| F9 | Stop and release all keys |

Using automation in online games may violate their terms of service.

## AutoHotkey alternative

`ahk/skilledlobby.ahk` is a simpler AutoHotkey v1 script for comparison: a fixed 27-second
loop (walk, jump, strafe, click). F3 starts it, F6 pauses it. Keys held at the moment
you pause stay held, and the only way to stop it is to exit AutoHotkey.
