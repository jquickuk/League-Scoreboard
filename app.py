#!/usr/bin/env python3
"""
Pool Scoreboard Backend
- Multi-team support
- Live scraping via Playwright
- Socket.IO realtime updates
- Auto-stop scrapers when no viewers
"""

import os
import time
import re
import json
from datetime import datetime
from threading import Thread, Lock

from flask import Flask, render_template, abort, request
from flask_socketio import SocketIO
from playwright.sync_api import sync_playwright

# =====================================================
# CONFIG
# =====================================================

PORT = int(os.environ.get("PORT", 10000))
LIVE_URL = "https://app.westonpoolleague.org/app/livescores/all"

SCOREBOARD_SECRET = os.environ.get("SCOREBOARD_SECRET", "frames-secret")
SCRAPE_INTERVAL = 10
MAX_CLIENTS = 20
TEST_MODE = False

# =====================================================
# APP SETUP
# =====================================================

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")

clients = set()

# =====================================================
# LOAD TEAMS
# =====================================================

with open("teams.json", encoding="utf-8") as f:
    TEAMS_BY_DIVISION = json.load(f)

TEAM_BY_SLUG = {
    team["slug"]: team
    for teams in TEAMS_BY_DIVISION.values()
    for team in teams
}

TEAM_SLUG_BY_ID = {
    team["id"]: slug
    for slug, team in TEAM_BY_SLUG.items()
}

# =====================================================
# LIVE TEAM TRACKING
# =====================================================

live_team_slugs = set()
live_lock = Lock()

# =====================================================
# SCRAPER MANAGEMENT
# =====================================================

scrapers = {}        # room -> Thread
room_counts = {}     # room -> int
scraper_lock = Lock()

# =====================================================
# ROUTES
# =====================================================

@app.route("/")
def index():
    return render_template("index.html", divisions=TEAMS_BY_DIVISION)


@app.route("/team/<slug>")
def team_scoreboard(slug):
    if slug not in TEAM_BY_SLUG:
        abort(404)
    return render_template("scoreboard.html")


@app.route("/api/live-teams")
def api_live_teams():
    with live_lock:
        return list(live_team_slugs)

# =====================================================
# SOCKET SECURITY
# =====================================================

@socketio.on("connect")
def on_connect():
    key = request.args.get("key") or request.headers.get("X-Scoreboard-Key")
    if key != SCOREBOARD_SECRET or len(clients) >= MAX_CLIENTS:
        return False
    clients.add(request.sid)


@socketio.on("disconnect")
def on_disconnect():
    clients.discard(request.sid)

    # Decrement room counts safely
    for room in list(room_counts):
        if request.sid in socketio.server.rooms(request.sid):
            room_counts[room] = max(0, room_counts.get(room, 1) - 1)

# =====================================================
# SOCKET ROOM JOIN
# =====================================================

@socketio.on("join_team")
def join_team(data):
    slug = data.get("slug")
    team = TEAM_BY_SLUG.get(slug)
    if not team:
        return

    room = f"team:{slug}"
    socketio.join_room(room)

    with scraper_lock:
        room_counts[room] = room_counts.get(room, 0) + 1
        if room not in scrapers:
            t = Thread(
                target=scrape_loop,
                args=(team["id"], slug, room),
                daemon=True
            )
            scrapers[room] = t
            t.start()

# =====================================================
# SCRAPER LOOP
# =====================================================

def scrape_loop(team_id, slug, room):
    last_state = None

    socketio.emit("app_mode", {"test_mode": TEST_MODE}, room=room)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        while True:
            # Stop scraper if nobody is watching
            with scraper_lock:
                if room_counts.get(room, 0) <= 0:
                    scrapers.pop(room, None)
                    room_counts.pop(room, None)
                    with live_lock:
                        live_team_slugs.discard(slug)
                    browser.close()
                    return

            try:
                page.goto(LIVE_URL, timeout=60000)
                page.wait_for_selector("div.row.pb-3.mx-0", timeout=15000)

                rows = page.query_selector_all("div.row.pb-3.mx-0")
                found_live = False
                current_state = None

                for row in rows:
                    links = row.query_selector_all("a[href*='/team/']")
                    if len(links) != 2:
                        continue

                    hrefs = [
                        links[0].get_attribute("href") or "",
                        links[1].get_attribute("href") or ""
                    ]

                    if f"/team/{team_id}" not in hrefs[0] and f"/team/{team_id}" not in hrefs[1]:
                        continue

                    found_live = True

                    score_text = row.query_selector("span.text-lighter").inner_text()
                    m = re.search(r"(\d+)\s*\|\s*(\d+)", score_text)
                    if not m:
                        continue

                    home_score, away_score = map(int, m.groups())

                    current_state = {
                        "updated": datetime.utcnow().isoformat() + "Z",
                        "home": {
                            "name": links[0].inner_text().strip(),
                            "score": home_score
                        },
                        "away": {
                            "name": links[1].inner_text().strip(),
                            "score": away_score
                        }
                    }
                    break

                # Track live teams globally
                with live_lock:
                    if found_live:
                        live_team_slugs.add(slug)
                    else:
                        live_team_slugs.discard(slug)

                socketio.emit(
                    "match_status",
                    {"status": "live" if found_live else "not_live"},
                    room=room
                )

                # Always emit timestamp, even if score unchanged
                if current_state:
                    if current_state != last_state:
                        last_state = current_state
                    socketio.emit("score_update", last_state, room=room)

            except Exception as e:
                app.logger.error("Scrape error (%s): %s", room, e)

            time.sleep(SCRAPE_INTERVAL)

# =====================================================
# MAIN
# =====================================================

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=PORT, debug=False)
