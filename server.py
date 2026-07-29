#!/usr/bin/env python3
"""
Snake highscore + multiplayer server.
Run:  python server.py
Then open http://localhost:8934/snake.html

Serves snake.html (and any other files in this folder) plus:
  - a small JSON API for the daily highscore board
  - a polling-based 1v1 multiplayer mode (host/join by 4-digit code or
    public lobby list)

Multiplayer is server-authoritative: a background thread advances every
active match on its own schedule using time.monotonic() as the single
clock, independent of when either client happens to poll. Collisions
(who hit the wall, who ran into whom, who was first) are always decided
by that server clock, never by request arrival order.

Match rules: both snakes start in the middle heading away from each
other. Any crash - wall, your own body, the opponent's body, or a
head-on hit - ends the round immediately. A wall/self crash costs the
crasher 20% of their score, running into the opponent's body costs 10%;
a head-on hit costs nothing extra. Whoever has more points once the
penalty (if any) is applied wins the round. Both players then have to
pick "replay" before a new round starts.
"""

import http.server
import json
import os
import random
import re
import secrets
import threading
import time
from datetime import date
from urllib.parse import parse_qs, urlsplit

PORT = int(os.environ.get("SNAKE_PORT", "8934"))
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


# ---------- multiplayer ----------
MP_GRID_W = 24
MP_GRID_H = 16
MP_TICK_S = 0.15
WALL_PENALTY = 0.20
OPP_PENALTY = 0.10
FOOD_SCORE = 10
PLAYER_TIMEOUT = 6      # seconds unseen -> player is dropped
ROOM_IDLE_TIMEOUT = 90  # seconds of total silence -> room is swept away

rooms = {}
rooms_lock = threading.RLock()


def make_code():
    with rooms_lock:
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


def new_room(public, host_name):
    now = time.monotonic()
    with rooms_lock:
        code = make_code()
        room = {
            "code": code,
            "public": public,
            "status": "waiting",
            "lock": threading.RLock(),
            "created": now,
            "lastTick": now,
            "players": {},
            "snakes": {1: spawn_snake(1)},
            "food": None,
            "winner": None,
            "endReason": None,
            "replayVotes": set(),
        }
        pid = secrets.token_hex(8)
        room["players"][pid] = {"num": 1, "name": host_name, "lastSeen": now}
        rooms[code] = room
    return room, pid


def join_room(code, name):
    with rooms_lock:
        room = rooms.get(code)
    if not room:
        return None, None, "not_found"
    with room["lock"]:
        if room["status"] != "waiting" or len(room["players"]) >= 2:
            return None, None, "unavailable"
        now = time.monotonic()
        pid = secrets.token_hex(8)
        room["players"][pid] = {"num": 2, "name": name, "lastSeen": now}
        room["snakes"][2] = spawn_snake(2)
        place_food(room)
        room["status"] = "playing"
        room["lastTick"] = now
        return room, pid, None


def touch_player(room, pid):
    p = room["players"].get(pid)
    if p:
        p["lastSeen"] = time.monotonic()


def list_public_rooms():
    with rooms_lock:
        room_list = list(rooms.values())
    out = []
    for r in room_list:
        with r["lock"]:
            if r["public"] and r["status"] == "waiting":
                host = next((p["name"] for p in r["players"].values() if p["num"] == 1), "HOST")
                out.append({"code": r["code"], "hostName": host})
    return out


def get_room_state(code, pid):
    with rooms_lock:
        room = rooms.get(code)
    if not room:
        return None, "not_found"
    with room["lock"]:
        me = room["players"].get(pid)
        if not me:
            return None, "not_found"
        touch_player(room, pid)
        my_num = me["num"]
        opp_num = 2 if my_num == 1 else 1
        opp = next((p for p in room["players"].values() if p["num"] == opp_num), None)
        my_snake = room["snakes"].get(my_num)
        opp_snake = room["snakes"].get(opp_num)

        result = {
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
            result["winner"] = "you" if w == my_num else ("opponent" if w == opp_num else w)
            result["endReason"] = room["endReason"]
            result["youReplayVoted"] = my_num in room["replayVotes"]
        return result, None


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


def game_loop_thread():
    while True:
        now = time.monotonic()
        with rooms_lock:
            room_list = list(rooms.values())

        for room in room_list:
            with room["lock"]:
                if room["status"] == "playing" and now - room["lastTick"] >= MP_TICK_S:
                    advance_room(room, now)
                if room["status"] in ("playing", "ended"):
                    for pid, p in list(room["players"].items()):
                        if now - p["lastSeen"] > PLAYER_TIMEOUT:
                            room["players"].pop(pid, None)
                            if room["status"] == "playing":
                                end_room(room, None, "opponent_left")
                            break

        with rooms_lock:
            stale = []
            for code, r in rooms.items():
                seen = [p["lastSeen"] for p in r["players"].values()]
                last_activity = max(seen) if seen else r["created"]
                if now - last_activity > ROOM_IDLE_TIMEOUT:
                    stale.append(code)
            for code in stale:
                del rooms[code]

        time.sleep(0.02)


class Handler(http.server.SimpleHTTPRequestHandler):
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
        parsed = urlsplit(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/highscores":
            data = load_board()
            return self.send_json(200, {"date": data["date"], "entries": public_entries(data["entries"])})

        if path == "/api/mp/public":
            return self.send_json(200, {"rooms": list_public_rooms()})

        if path == "/api/mp/state":
            code = (qs.get("code") or [""])[0]
            pid = (qs.get("playerId") or [""])[0]
            state, err = get_room_state(code, pid)
            if err:
                return self.send_json(404, {"error": err})
            return self.send_json(200, state)

        super().do_GET()

    def do_POST(self):
        if self.path == "/api/highscores":
            return self.handle_highscore_post()
        if self.path == "/api/mp/host":
            return self.handle_mp_host()
        if self.path == "/api/mp/join":
            return self.handle_mp_join()
        if self.path == "/api/mp/input":
            return self.handle_mp_input()
        if self.path == "/api/mp/replay":
            return self.handle_mp_replay()
        if self.path == "/api/mp/leave":
            return self.handle_mp_leave()
        self.send_error(404)

    def handle_highscore_post(self):
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

    def handle_mp_host(self):
        payload = self.read_json_body()
        name = sanitize_name(payload.get("name", "")) or "HOST"
        if contains_bad_word(name):
            return self.send_json(400, {"error": "profanity"})
        room, pid = new_room(bool(payload.get("public")), name)
        return self.send_json(200, {"code": room["code"], "playerId": pid, "num": 1})

    def handle_mp_join(self):
        payload = self.read_json_body()
        code = re.sub(r"\D", "", str(payload.get("code", "")))[:4]
        name = sanitize_name(payload.get("name", "")) or "GUEST"
        if len(code) != 4:
            return self.send_json(400, {"error": "bad_code"})
        if contains_bad_word(name):
            return self.send_json(400, {"error": "profanity"})
        room, pid, err = join_room(code, name)
        if err:
            return self.send_json(404, {"error": err})
        return self.send_json(200, {"code": room["code"], "playerId": pid, "num": 2})

    def handle_mp_input(self):
        payload = self.read_json_body()
        code = str(payload.get("code", ""))
        pid = str(payload.get("playerId", ""))
        d = payload.get("dir") or {}
        dx, dy = d.get("x"), d.get("y")
        if (dx, dy) not in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            return self.send_json(400, {"error": "bad_dir"})
        with rooms_lock:
            room = rooms.get(code)
        if not room:
            return self.send_json(404, {"error": "not_found"})
        with room["lock"]:
            me = room["players"].get(pid)
            if not me:
                return self.send_json(404, {"error": "not_found"})
            touch_player(room, pid)
            snake = room["snakes"].get(me["num"])
            if snake and room["status"] == "playing":
                snake["nextDir"] = [dx, dy]
        return self.send_json(200, {"ok": True})

    def handle_mp_replay(self):
        payload = self.read_json_body()
        code = str(payload.get("code", ""))
        pid = str(payload.get("playerId", ""))
        with rooms_lock:
            room = rooms.get(code)
        if not room:
            return self.send_json(404, {"error": "not_found"})
        with room["lock"]:
            me = room["players"].get(pid)
            if not me:
                return self.send_json(404, {"error": "not_found"})
            touch_player(room, pid)
            if room["status"] != "ended":
                return self.send_json(400, {"error": "not_ended"})
            room["replayVotes"].add(me["num"])
            if len(room["players"]) == 2 and {1, 2} <= room["replayVotes"]:
                room["snakes"][1] = spawn_snake(1)
                room["snakes"][2] = spawn_snake(2)
                place_food(room)
                room["status"] = "playing"
                room["winner"] = None
                room["endReason"] = None
                room["replayVotes"] = set()
                room["lastTick"] = time.monotonic()
        return self.send_json(200, {"ok": True})

    def handle_mp_leave(self):
        payload = self.read_json_body()
        code = str(payload.get("code", ""))
        pid = str(payload.get("playerId", ""))
        with rooms_lock:
            room = rooms.get(code)
        if room:
            with room["lock"]:
                room["players"].pop(pid, None)
                if len(room["players"]) == 0:
                    with rooms_lock:
                        rooms.pop(code, None)
                elif room["status"] == "playing":
                    end_room(room, None, "opponent_left")
        return self.send_json(200, {"ok": True})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    threading.Thread(target=game_loop_thread, daemon=True).start()
    with http.server.ThreadingHTTPServer(("", PORT), Handler) as httpd:
        print("Serving Snake on http://localhost:%d/snake.html" % PORT)
        httpd.serve_forever()
