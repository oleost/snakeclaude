#!/usr/bin/env python3
"""
Snake highscore + multiplayer server.
Run:  python server.py
Then open http://localhost:8934/snake.html

Two independent servers run in this one process:
  - a plain HTTP server (port SNAKE_PORT, default 8934) serving snake.html
    and the daily highscore JSON API - unchanged, simple request/response.
  - a WebSocket server (port SNAKE_WS_PORT, default 8935) that handles
    everything for 1v1 multiplayer (host/join by 4-digit code or public
    lobby list).

Multiplayer is server-authoritative and push-based: an asyncio task
advances every active match on its own schedule using time.monotonic()
as the single clock, and immediately pushes the new state to both
players' open WebSocket connections the moment anything changes -
there is no polling, so there is nothing for a client clock to drift
out of phase with.

Match rules: both snakes start in the middle heading away from each
other. Any crash - wall, your own body, the opponent's body, or a
head-on hit - ends the round immediately. A wall/self crash costs the
crasher 20% of their score, running into the opponent's body costs 10%;
a head-on hit costs nothing extra. Whoever has more points once the
penalty (if any) is applied wins the round. Both players then have to
pick "replay" before a new round starts.
"""

import asyncio
import http.server
import json
import os
import random
import re
import threading
import time
from datetime import date

import websockets

PORT = int(os.environ.get("SNAKE_PORT", "8934"))
WS_PORT = int(os.environ.get("SNAKE_WS_PORT", "8935"))
DATA_FILE = os.environ.get("SNAKE_DATA_FILE") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "highscores.json")
MAX_ENTRIES = 5
MAX_NAME_LEN = 8

# Simple, blunt substring filter - not exhaustive, just catches the obvious stuff.
# Edit this list freely to add/remove words.
BAD_WORDS = [
    "fuck", "shit", "bitch", "cunt", "dick", "penis", "vagina", "cock", "pussy",
    "asshole", "piss", "tits", "whore", "slut", "porn", "rape",
    "nazi", "hitler", "retard", "nigger", "nigga", "fag", "faggot", "gaylord",
    "cumshot", "anal", "dildo", "chink", "kike",
    # norwegian
    "faen", "fitte", "hore", "jaevla", "javla", "neger", "kuk",
]

LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})


def normalize(s):
    return re.sub(r"[^a-z]", "", s.lower().translate(LEET))


def contains_bad_word(name):
    n = normalize(name)
    return any(bad in n for bad in BAD_WORDS)


def sanitize_name(raw):
    name = "".join(ch for ch in str(raw).upper() if ch.isalnum() or ch == " ")
    return name.strip()[:MAX_NAME_LEN]


# ---------- daily highscore board ----------
HS_LOCK = threading.RLock()


def load_board():
    with HS_LOCK:
        today = str(date.today())
        data = None
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                data = None
        if not data or data.get("date") != today:
            data = {"date": today, "entries": []}
            save_board(data)
        return data


def save_board(data):
    with HS_LOCK:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)


def public_entries(entries):
    return [{"name": e["name"], "score": e["score"]} for e in entries]


def add_entry(client_id, name, score):
    with HS_LOCK:
        data = load_board()
        entries = data["entries"]

        existing = None
        if client_id:
            existing = next((e for e in entries if e.get("clientId") == client_id), None)

        if existing:
            if score <= existing["score"]:
                return data  # not an improvement, keep the old one
            entries.remove(existing)

        entries.append({"clientId": client_id, "name": name, "score": score, "ts": time.time()})
        entries.sort(key=lambda e: (-e["score"], e["ts"]))
        data["entries"] = entries[:MAX_ENTRIES]
        save_board(data)
        return data


# ---------- multiplayer (asyncio + websockets, single event loop) ----------
# Everything below runs on one asyncio event loop, so plain dicts/sets are
# safe without locks: coroutines only interleave at "await" points, and
# none of the state mutations below have an await in the middle of them.
MP_GRID_W = 24
MP_GRID_H = 16
MP_TICK_S = 0.15
WALL_PENALTY = 0.20
OPP_PENALTY = 0.10
FOOD_SCORE = 10
WAITING_ROOM_TIMEOUT_S = 300  # a hosted room nobody ever joined gets swept

rooms = {}  # code -> room dict


def make_code():
    while True:
        code = "%04d" % random.randint(0, 9999)
        if code not in rooms:
            return code


def spawn_snake(side):
    cx, cy = MP_GRID_W // 2, MP_GRID_H // 2
    if side == 1:
        body = [[cx - 2, cy], [cx - 1, cy], [cx, cy]]
        d = [-1, 0]
    else:
        body = [[cx + 3, cy], [cx + 2, cy], [cx + 1, cy]]
        d = [1, 0]
    return {"body": body, "dir": d, "nextDir": d, "score": 0}


def place_food(room):
    occupied = {tuple(p) for p in room["snakes"][1]["body"]} | {tuple(p) for p in room["snakes"][2]["body"]}
    while True:
        fx, fy = random.randrange(MP_GRID_W), random.randrange(MP_GRID_H)
        if (fx, fy) not in occupied:
            room["food"] = [fx, fy]
            return


def decide_winner(score1, score2):
    if score1 > score2:
        return 1
    if score2 > score1:
        return 2
    return "draw"


def end_room(room, winner, reason):
    room["status"] = "ended"
    room["winner"] = winner
    room["endReason"] = reason
    room["replayVotes"] = set()


def reset_for_replay(room):
    room["snakes"][1] = spawn_snake(1)
    room["snakes"][2] = spawn_snake(2)
    place_food(room)
    room["status"] = "playing"
    room["winner"] = None
    room["endReason"] = None
    room["replayVotes"] = set()
    room["lastTick"] = time.monotonic()


def new_room(public, name, ws):
    now = time.monotonic()
    code = make_code()
    room = {
        "code": code,
        "public": public,
        "status": "waiting",
        "created": now,
        "lastTick": now,
        "players": {1: {"ws": ws, "name": name}},
        "snakes": {1: spawn_snake(1)},
        "food": None,
        "winner": None,
        "endReason": None,
        "replayVotes": set(),
    }
    rooms[code] = room
    return room


def join_room(code, name, ws):
    room = rooms.get(code)
    if not room:
        return None, "not_found"
    if room["status"] != "waiting" or len(room["players"]) >= 2:
        return None, "unavailable"
    room["players"][2] = {"ws": ws, "name": name}
    room["snakes"][2] = spawn_snake(2)
    place_food(room)
    room["status"] = "playing"
    room["lastTick"] = time.monotonic()
    return room, None


def list_public_rooms():
    out = []
    for r in rooms.values():
        if r["public"] and r["status"] == "waiting":
            host = r["players"].get(1)
            if host:
                out.append({"code": r["code"], "hostName": host["name"]})
    return out


def build_state_payload(room, my_num):
    opp_num = 2 if my_num == 1 else 1
    opp = room["players"].get(opp_num)
    my_snake = room["snakes"].get(my_num)
    opp_snake = room["snakes"].get(opp_num)

    payload = {
        "type": "state",
        "status": room["status"],
        "food": room["food"],
        "you": {
            "body": my_snake["body"] if my_snake else [],
            "score": my_snake["score"] if my_snake else 0,
        },
        "opponent": ({
            "name": opp["name"],
            "body": opp_snake["body"] if opp_snake else [],
            "score": opp_snake["score"] if opp_snake else 0,
        } if opp else None),
    }
    if room["status"] == "ended":
        w = room["winner"]
        payload["winner"] = "you" if w == my_num else ("opponent" if w == opp_num else w)
        payload["endReason"] = room["endReason"]
        payload["youReplayVoted"] = my_num in room["replayVotes"]
    return payload


async def broadcast(room):
    for num, p in list(room["players"].items()):
        try:
            await p["ws"].send(json.dumps(build_state_payload(room, num)))
        except websockets.exceptions.ConnectionClosed:
            pass


def advance_room(room, now):
    s1, s2 = room["snakes"][1], room["snakes"][2]
    for s in (s1, s2):
        nd, cd = s["nextDir"], s["dir"]
        if not (nd[0] == -cd[0] and nd[1] == -cd[1]):
            s["dir"] = nd

    h1 = [s1["body"][0][0] + s1["dir"][0], s1["body"][0][1] + s1["dir"][1]]
    h2 = [s2["body"][0][0] + s2["dir"][0], s2["body"][0][1] + s2["dir"][1]]

    def oob(h):
        return h[0] < 0 or h[0] >= MP_GRID_W or h[1] < 0 or h[1] >= MP_GRID_H

    head_on = (h1 == h2) or (h1 == s2["body"][0] and h2 == s1["body"][0])
    crashed = {}

    if not head_on:
        if oob(h1) or h1 in s1["body"]:
            crashed[1] = WALL_PENALTY
        if oob(h2) or h2 in s2["body"]:
            crashed[2] = WALL_PENALTY
        if 1 not in crashed and h1 in s2["body"]:
            crashed[1] = OPP_PENALTY
        if 2 not in crashed and h2 in s1["body"]:
            crashed[2] = OPP_PENALTY

    if head_on or crashed:
        for num, frac in crashed.items():
            room["snakes"][num]["score"] = int(room["snakes"][num]["score"] * (1 - frac))
        winner = decide_winner(room["snakes"][1]["score"], room["snakes"][2]["score"])
        end_room(room, winner, "headon" if head_on else "crash")
        return

    for s, h in ((s1, h1), (s2, h2)):
        s["body"].insert(0, h)
        if room["food"] and h == room["food"]:
            s["score"] += FOOD_SCORE
            place_food(room)
        else:
            s["body"].pop()

    room["lastTick"] += MP_TICK_S


async def handle_disconnect(room, num):
    room["players"].pop(num, None)
    if room["status"] == "waiting" or not room["players"]:
        rooms.pop(room["code"], None)
        return
    if room["status"] == "playing":
        end_room(room, None, "opponent_left")
    await broadcast(room)


async def game_loop():
    while True:
        now = time.monotonic()
        for room in list(rooms.values()):
            if room["status"] == "playing" and now - room["lastTick"] >= MP_TICK_S:
                advance_room(room, now)
                await broadcast(room)

        stale = [c for c, r in rooms.items()
                 if r["status"] == "waiting" and now - r["created"] > WAITING_ROOM_TIMEOUT_S]
        for c in stale:
            del rooms[c]

        await asyncio.sleep(0.02)


async def ws_handler(websocket):
    room = None
    num = None
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            mtype = msg.get("type")

            if mtype == "host":
                name = sanitize_name(msg.get("name", "")) or "HOST"
                if contains_bad_word(name):
                    await websocket.send(json.dumps({"type": "error", "message": "profanity"}))
                    continue
                room = new_room(bool(msg.get("public")), name, websocket)
                num = 1
                await websocket.send(json.dumps({"type": "hosted", "code": room["code"], "public": room["public"]}))

            elif mtype == "join":
                code = re.sub(r"\D", "", str(msg.get("code", "")))[:4]
                name = sanitize_name(msg.get("name", "")) or "GUEST"
                if len(code) != 4:
                    await websocket.send(json.dumps({"type": "error", "message": "bad_code"}))
                    continue
                if contains_bad_word(name):
                    await websocket.send(json.dumps({"type": "error", "message": "profanity"}))
                    continue
                joined_room, err = join_room(code, name, websocket)
                if err:
                    await websocket.send(json.dumps({"type": "error", "message": err}))
                    continue
                room = joined_room
                num = 2
                await websocket.send(json.dumps({"type": "joined", "code": room["code"]}))
                await broadcast(room)

            elif mtype == "listPublic":
                await websocket.send(json.dumps({"type": "publicRooms", "rooms": list_public_rooms()}))

            elif mtype == "input":
                if room and num and room["status"] == "playing":
                    d = msg.get("dir") or {}
                    dx, dy = d.get("x"), d.get("y")
                    if (dx, dy) in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        snake = room["snakes"].get(num)
                        if snake:
                            snake["nextDir"] = [dx, dy]

            elif mtype == "replay":
                if room and num and room["status"] == "ended":
                    room["replayVotes"].add(num)
                    if len(room["players"]) == 2 and {1, 2} <= room["replayVotes"]:
                        reset_for_replay(room)
                    await broadcast(room)

            elif mtype == "leave":
                break
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if room is not None and num is not None:
            await handle_disconnect(room, num)


async def ws_main():
    async with websockets.serve(ws_handler, "0.0.0.0", WS_PORT, ping_interval=15, ping_timeout=15):
        print("Multiplayer WebSocket on ws://0.0.0.0:%d/" % WS_PORT)
        await game_loop()


# ---------- static file + highscore HTTP server (unchanged transport) ----------
class Handler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        # this project gets redeployed often - never let a browser (mobile
        # Safari/Chrome especially) hang on to a stale snake.html or API
        # response after an update
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if self.path == "/api/highscores":
            data = load_board()
            return self.send_json(200, {"date": data["date"], "entries": public_entries(data["entries"])})
        super().do_GET()

    def do_POST(self):
        if self.path != "/api/highscores":
            self.send_error(404)
            return
        payload = self.read_json_body()
        score = payload.get("score")
        if not isinstance(score, (int, float)) or score <= 0:
            return self.send_json(400, {"error": "invalid_score"})

        name = sanitize_name(payload.get("name", ""))
        if not name:
            return self.send_json(400, {"error": "invalid_name"})
        if contains_bad_word(name):
            return self.send_json(400, {"error": "profanity"})

        client_id = payload.get("clientId")
        if not isinstance(client_id, str) or not (0 < len(client_id) <= 64):
            client_id = None

        data = add_entry(client_id, name, int(score))
        return self.send_json(200, {"date": data["date"], "entries": public_entries(data["entries"])})

    def log_message(self, fmt, *args):
        pass


def run_http_server():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    with http.server.ThreadingHTTPServer(("", PORT), Handler) as httpd:
        print("Serving Snake on http://0.0.0.0:%d/snake.html" % PORT)
        httpd.serve_forever()


if __name__ == "__main__":
    threading.Thread(target=run_http_server, daemon=True).start()
    asyncio.run(ws_main())
