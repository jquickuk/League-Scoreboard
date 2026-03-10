#!/usr/bin/env python3
"""Pool Scoreboard Backend."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, Thread
from typing import Any

from flask import Flask, abort, jsonify, render_template, request
from flask_socketio import SocketIO, join_room
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# =====================================================
# CONFIG
# =====================================================
BASE_DIR = Path(__file__).resolve().parent
PORT = int(os.environ.get("PORT", 10000))
LIVE_URL = "https://app.westonpoolleague.org/app/livescores/all"
SCOREBOARD_SECRET = os.environ.get("SCOREBOARD_SECRET", "frames-secret")
SCRAPE_INTERVAL = int(os.environ.get("SCRAPE_INTERVAL", 10))
MAX_CLIENTS = int(os.environ.get("MAX_CLIENTS", 20))
TEST_MODE = os.environ.get("TEST_MODE", "false").lower() == "true"

# =====================================================
# APP SETUP
# =====================================================
app = Flask(__name__)
app.logger.setLevel(logging.INFO)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent")
clients: set[str] = set()

# =====================================================
# LOAD TEAMS
# =====================================================
with (BASE_DIR / "teams.json").open(encoding="utf-8") as f:
    TEAMS_BY_DIVISION: dict[str, list[dict[str, Any]]] = json.load(f)

TEAM_BY_SLUG = {
    team["slug"]: team
    for teams in TEAMS_BY_DIVISION.values()
    for team in teams
}

# =====================================================
# LIVE TEAM TRACKING
# =====================================================
live_team_slugs: set[str] = set()
live_lock = Lock()

# =====================================================
# SCRAPER MANAGEMENT
# =====================================================
scrapers: dict[str, Thread] = {}
room_counts: dict[str, int] = {}
sid_rooms: dict[str, set[str]] = {}
scraper_lock = Lock()

# =====================================================
# ROUTES
# =====================================================
@app.route("/")
def index() -> str:
    return render_template("index.html", divisions=TEAMS_BY_DIVISION)


@app.route("/team/<slug>")
def team_scoreboard(slug: str) -> str:
    if slug not in TEAM_BY_SLUG:
        abort(404)
    return render_template("scoreboard.html")


@app.route("/api/live-teams")
def api_live_teams():
    with live_lock:
        return jsonify(sorted(live_team_slugs))


# =====================================================
# SOCKET SECURITY
# =====================================================
@socketio.on("connect")
def on_connect():
    key = request.args.get("key") or request.headers.get("X-Scoreboard-Key")
    if key != SCOREBOARD_SECRET or len(clients) >= MAX_CLIENTS:
        return False
    clients.add(request.sid)
    with scraper_lock:
        sid_rooms.setdefault(request.sid, set())
    return None


@socketio.on("disconnect")
def on_disconnect():
    clients.discard(request.sid)
    with scraper_lock:
        joined = sid_rooms.pop(request.sid, set())
        for room in joined:
            room_counts[room] = max(0, room_counts.get(room, 0) - 1)


# =====================================================
# SOCKET ROOM JOIN
# =====================================================
@socketio.on("join_team")
def join_team_handler(data):
    slug = (data or {}).get("slug")
    team = TEAM_BY_SLUG.get(slug)
    if not team:
        app.logger.warning("join_team rejected for unknown slug: %r", slug)
        return

    room = f"team:{slug}"
    join_room(room)

    with scraper_lock:
        sid_rooms.setdefault(request.sid, set()).add(room)
        room_counts[room] = room_counts.get(room, 0) + 1
        if room not in scrapers:
            thread = Thread(
                target=scrape_loop,
                args=(int(team["id"]), slug, room),
                daemon=True,
            )
            scrapers[room] = thread
            thread.start()
            app.logger.info("Started scraper for %s", room)


# =====================================================
# SCRAPER HELPERS
# =====================================================
def _extract_match_state_from_page(page: Any, team_id: int) -> dict[str, Any] | None:
    team = next((t for t in TEAM_BY_SLUG.values() if int(t["id"]) == int(team_id)), None)
    if not team:
        app.logger.warning("No team found for team_id=%s", team_id)
        return None

    target_name = team["name"].strip().lower()

    try:
        page_text = page.inner_text("body")
        app.logger.info("Page body sample for %s: %r", team_id, page_text[:1000])
    except Exception as exc:
        app.logger.warning("Could not read page body for %s: %s", team_id, exc)

    cards = page.query_selector_all("div.row.pb-3.mx-0")
    app.logger.info("Scanning %d match cards for team_id=%s", len(cards), team_id)

    for card in cards:
        try:
            text = (card.inner_text() or "").strip()
        except Exception:
            continue

        if not text:
            continue

        if target_name not in text.lower():
            continue

        app.logger.info("Candidate card for %s: %r", team_id, text[:500])

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        score_index = None
        score_match = None

        for i, line in enumerate(lines):
            m = re.fullmatch(r"(\d+)\s*\|\s*(\d+)", line)
            if m:
                score_index = i
                score_match = m
                break

        if score_index is None or score_match is None:
            continue

        before = None
        after = None

        ignored_lines = {
            "live",
            "live scores",
            "friendly",
            "hewlett cup",
            "league cup",
            "share tweet share",
            "facebook group",
            "information",
            "fixtures",
            "results",
            "league tables",
            "player stats",
            "competitions",
            "roll of honour",
            "about us",
            "terms and conditions",
            "privacy policy",
            "release notes",
            "status and maintenance",
        }

        for i in range(score_index - 1, -1, -1):
            line = lines[i]
            low = line.lower()
            if line.startswith("@"):
                continue
            if "|" in line:
                continue
            if len(line) > 40:
                continue
            if low in ignored_lines:
                continue
            before = line
            break

        for i in range(score_index + 1, len(lines)):
            line = lines[i]
            low = line.lower()
            if line.startswith("@"):
                continue
            if "|" in line:
                continue
            if len(line) > 40:
                continue
            if low in ignored_lines:
                continue
            after = line
            break

        if not before or not after:
            app.logger.info("Could not identify teams around score for %s", team_id)
            continue

        home_score, away_score = map(int, score_match.groups())
        state = {
            "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "home": {"name": before, "score": home_score},
            "away": {"name": after, "score": away_score},
        }
        app.logger.info("Live match found for %s: %r", team_id, state)
        return state

    app.logger.warning("No live match found for team_id=%s", team_id)
    return None


# =====================================================
# SCRAPER LOOP
# =====================================================
def scrape_loop(team_id: int, slug: str, room: str) -> None:
    last_state: dict[str, Any] | None = None
    socketio.emit("app_mode", {"test_mode": TEST_MODE}, room=room)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page()

        try:
            while True:
                with scraper_lock:
                    if room_counts.get(room, 0) <= 0:
                        scrapers.pop(room, None)
                        room_counts.pop(room, None)
                        with live_lock:
                            live_team_slugs.discard(slug)
                        app.logger.info("Stopping scraper for %s", room)
                        return

                try:
                    page.goto(LIVE_URL, timeout=60000, wait_until="domcontentloaded")
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    app.logger.warning("Timed out loading live scores page for %s", room)
                except Exception as exc:
                    app.logger.warning("Error loading live scores page for %s: %s", room, exc)

                current_state = _extract_match_state_from_page(page, team_id)
                found_live = current_state is not None

                with live_lock:
                    if found_live:
                        live_team_slugs.add(slug)
                    else:
                        live_team_slugs.discard(slug)

                socketio.emit(
                    "match_status",
                    {"status": "live" if found_live else "not_live"},
                    room=room,
                )

                if current_state and current_state != last_state:
                    last_state = current_state
                    socketio.emit("score_update", current_state, room=room)

                time.sleep(SCRAPE_INTERVAL)

        except Exception as exc:
            app.logger.exception("Fatal scrape loop error for %s: %s", room, exc)
        finally:
            browser.close()


# =====================================================
# ENTRY POINT
# =====================================================
if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=PORT)