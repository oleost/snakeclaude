# Snake

A retro monochrome-phone-styled Snake game (single HTML file + a small Python
server) with a daily server-side highscore board and a 1v1 multiplayer mode.

## Running locally

Requires Python 3 and the `websockets` package (used for multiplayer):

```
pip install -r requirements.txt
python server.py
```

Then open `http://localhost:8934/snake.html`.

Opening `snake.html` directly as a file also works for solo play, but the
highscore board and multiplayer need the server running.

## Running with Docker

```
docker compose up -d --build
```

This builds the image and starts the server on ports 8934 (game + highscore
API) and 8935 (multiplayer WebSocket), with the daily highscore file
persisted in a named volume (`snake-data`) so it survives container
rebuilds/restarts.

Environment variables (all optional, set in `docker-compose.yml`):

- `SNAKE_PORT` - HTTP port to listen on (default `8934`)
- `SNAKE_WS_PORT` - multiplayer WebSocket port (default `8935`)
- `SNAKE_DATA_FILE` - path to the highscore JSON file (default `/data/highscores.json` in the container)

The server sends `Cache-Control: no-cache` on every response, so a normal
page refresh always picks up whatever was last deployed - no stale cached
copy of `snake.html` lingering on a phone after an update.

## Controls

- Classic numeric keypad: 2/4/6/8 to move, 1/9 for a diagonal corner-turn,
  5 to start/pause/select, `*`/`#` for back/confirm in menus.
- A Nokia 3210-style nav cluster sits fixed right under the screen: an
  up/OK/down rocker with a BACK key to its left (standing in for the
  classic "C" key) - mirrors the keypad's move/select/back everywhere,
  including menus, gameplay and multiplayer.
- OPTIONS menu holds two toggles: WALLS ON/OFF, and "BIG CORNER" - while
  the latter is on and you're actually playing (not paused/menu), the tap
  targets for keys 1 and 9 expand to cover most of the keypad (nothing
  changes visually), making one-handed corner-turn steering much easier on
  a touchscreen.

## Mobile

The page disables pinch/double-tap zoom and lays out full-height on a
phone screen (screen near the top, keypad near the bottom, with a
flexible gap between them) rather than sitting shrink-wrapped in the
middle of the viewport.

## Multiplayer

Host a game (public or private, gets a 4-digit code) or join one (from the
public lobby list or by entering a code). The server runs the match
simulation itself on its own clock and pushes state to both players over a
WebSocket the instant anything changes - collisions, timing and scoring are
always decided by the server, and there's no polling for the client to fall
behind on.

Both snakes start in the middle heading away from each other. Any crash -
wall, your own body, the opponent's body, or a head-on hit - ends the round
immediately. A wall/self crash costs the crasher 20% of their score, running
into the opponent's body costs 10%; a head-on hit costs nothing extra.
Whoever has more points once the penalty (if any) is applied wins the round.
Both players then have to pick "replay" before a new round starts.
