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
python anti_afk.py --autojoin --anti-kick  # also press F5 every 2s / 'l' every 20s
```

| Hotkey | Action |
| --- | --- |
| F8 | Pause / resume (releases held keys) |
| F9 | Stop and release all keys |

Using automation in online games may violate their terms of service.
