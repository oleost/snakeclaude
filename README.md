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

This builds the image and starts the server on a single port (8934) -
static files, the highscore API, and multiplayer all share it, so exposing
this one port (directly, or through a reverse proxy / Cloudflare Tunnel) is
enough for the whole thing to work. The daily highscore file is persisted
in a named volume (`snake-data`) so it survives container rebuilds/restarts.

Environment variables (both optional, set in `docker-compose.yml`):

- `SNAKE_PORT` - port to listen on (default `8934`)
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
- OPTIONS menu holds WALLS ON/OFF, "1/9 ONLY" (see below), VIBRATE ON/OFF,
  EXTRA HP ON/OFF (see below), and an entry to edit your stored highscore
  nickname.
- "1/9 ONLY": while it's on and you're actually playing (not paused/menu),
  any tap on the whole lower half of the phone - the keypad, the gray gap
  above it, and the side bezel around it, left half or right half - acts as
  a corner-turn on key 1 or key 9 respectively (nothing changes visually).
  This is decided from the actual tap position at click time rather than a
  fixed on-screen zone, so there's no dead spot anywhere below the nav
  cluster. The nav cluster (BACK/OK/up/down) itself is unaffected.
- "EXTRA HP": a shared 2-second grace pool (default on) shown as a small
  depleting bar top-center of the screen. Touching a wall, your own body,
  or - in multiplayer - the opponent's body no longer means instant death;
  instead the snake holds in place while the pool drains, and resumes
  normally if you steer away before it empties. The pool doesn't refill
  mid-life/round - once it's used up, the next touch is fatal exactly like
  this were off.

## Mobile

The page disables pinch/double-tap zoom and lays out full-height on a
phone screen (screen near the top, keypad near the bottom, with a
flexible gap between them) rather than sitting shrink-wrapped in the
middle of the viewport.

Every button press triggers a short `navigator.vibrate()` haptic tick
(default on, toggle in OPTIONS). Android/Chrome supports this; iOS Safari
has no vibration API at all, so it's silently a no-op there regardless of
the setting.

The page is installable as a home-screen app (manifest + icons), and shows
a one-time banner suggesting it if it isn't already installed:
- **Android/Chrome** gets a real "Installer" button, wired to the browser's
  own install prompt (`beforeinstallprompt`).
- **iOS Safari** has no install API at all, so the banner instead just
  explains the manual steps (Share icon → "Legg til på Hjemskjerm").

The banner is dismissible and stays dismissed (remembered in
`localStorage`), and never shows on desktop or once already running
standalone.

## Exposing it externally (e.g. via Cloudflare Tunnel)

Everything - static files, highscores, and multiplayer - is served on the
one port above, specifically so that a single reverse-proxy hostname is
enough; there's no second port to separately expose for multiplayer to
work. The client connects to its WebSocket using the same host and
protocol the page itself was loaded with (`wss://` when the page is
`https://`, `ws://` when it's plain `http://`), so anything that proxies
plain HTTP transparently proxies the multiplayer traffic too.

To add it to an existing Cloudflare Tunnel (Zero Trust dashboard →
Networks → Tunnels → your tunnel → Public Hostname → Add a public
hostname):

- Subdomain: `snake` (or whatever you want, e.g. gives `snake.yourdomain.tld`)
- Service type: HTTP
- URL: whatever address/port your other locally-tunneled apps use to reach
  this box, with port `8934` (e.g. the container's or host's LAN address -
  match however your other `*.yourdomain.tld` entries are configured)

Save, and the DNS record is created automatically. No separate hostname or
port is needed for multiplayer.

## Multiplayer

Host a game (public or private, gets a 4-digit code) or join one (from the
public lobby list or by entering a code). The server runs the match
simulation itself on its own clock and pushes state to both players over a
WebSocket the instant anything changes - collisions, timing and scoring are
always decided by the server, and there's no polling for the client to fall
behind on.

Walls on/off and Extra HP on/off are both the host's OPTIONS settings at
the moment they start hosting - with walls off, going out of bounds wraps
around to the opposite edge instead of crashing, same as solo mode. Small
`#`/`O` (walls) and `+`/`-` (Extra HP) markers show next to the code on
the host's own waiting screen, and next to each room in the public room
list so a joiner can see both before picking one.

Once both players are in (on join, or after both pick replay), there's a
server-driven 3-2-1 countdown before the round actually starts - snakes are
placed but frozen until it hits zero.

Both snakes start in the middle heading away from each other. A head-on
hit (both heads meet, or swap places) ends the round immediately for both,
no penalty either way. Touching a wall, your own body, or the opponent's
body instead draws down each snake's own 2-second Extra HP pool (if that
option is on) - the snake holds in place while touching, and resumes
normally if steered away before the pool empties; the pool never refills
mid-round. Once it's actually exhausted (or Extra HP is off), the same
touch ends the round: a wall/self crash costs the crasher 20% of their
score, running into the opponent's body costs 10%. Whoever has more points
once the penalty (if any) is applied wins the round. Both players then
have to pick "replay" before a new round starts.

Multiplayer has its own daily highscore board, separate from solo's -
"HIGH SCORE" in the MULTIPLAYER menu. Each player's own score is submitted
automatically the moment a round ends (win, lose, or draw), using their
already-stored name - no extra name-entry step like solo's. Both boards
live in the same highscore file and reset together at midnight, but are
otherwise completely independent (same top-5, same profanity filter, same
"only counts if it beats your own previous best" rule, applied separately
to each board).

Food spawns are biased toward whichever free cell is closest to equidistant
from both snakes' heads, measured by actual shortest path (BFS around both
bodies) rather than straight-line distance, so a snake's own tail or the
opponent's body forcing a detour is accounted for rather than ignored.
