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

## Multiplayer

Host a game (public or private, gets a 4-digit code) or join one (from the
public lobby list or by entering a code). The server runs the match
simulation itself on its own clock and pushes state to both players over a
WebSocket the instant anything changes - collisions, timing and scoring are
always decided by the server, and there's no polling for the client to fall
behind on.
