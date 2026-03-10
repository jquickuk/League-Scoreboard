#!/usr/bin/env python3
"""Pool Scoreboard Backend.

Changes in this version:
- Fixes the team route to accept a slug parameter.
- Uses Flask-SocketIO's join_room helper correctly.
- Makes live-match detection more resilient to Weston Pool League markup changes.
- Adds safer extraction and logging around score parsing.
"""

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

from flask import Flask, abort, render_template, request
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
def api_live_teams() -> list[str]:
    with live_lock:
        return sorted(live_team_slugs)


# =====================================================
# SOCKET SECURITY
# =====================================================
@socketio.on("connect")
def on_connect() -> bool | None:
    key = request.args.get("key") or request.headers.get("X-Scoreboard-Key")
    if key != SCOREBOARD_SECRET or len(clients) >= MAX_CLIENTS:
        return False
    clients.add(request.sid)
    with scraper_lock:
        sid_rooms.setdefault(request.sid, set())
    return None


@socketio.on("disconnect")
def on_disconnect() -> None:
    clients.discard(request.sid)
    with scraper_lock:
        joined = sid_rooms.pop(request.sid, set())
        for room in joined:
            room_counts[room] = max(0, room_counts.get(room, 0) - 1)


# =====================================================
# SOCKET ROOM JOIN
# =====================================================
@socketio.on("join_team")
def join_team_handler(data: dict[str, Any]) -> None:
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
            thread = Thread(target=scrape_loop, args=(int(team["id"]), slug, room), daemon=True)
            scrapers[room] = thread
            thread.start()
            app.logger.info("Started scraper for %s", room)


# =====================================================
# SCRAPER HELPERS
# =====================================================
def _extract_match_state_from_page(page: Any, team_id: int) -> dict[str, Any] | None:
    """Return a match state if the given team is currently live.

    This avoids depending on one exact row class or one exact score span class.
    Instead, it starts from team links and walks up the DOM to find a nearby
    container that includes a score in the form "N | N".
    """
    candidate_links = page.query_selector_all("a[href*='/team/'], a[href*='/app/team/']")
    app.logger.info("Found %d candidate team links for team_id=%s", len(candidate_links), team_id)

    for link in candidate_links:
        href = (link.get_attribute("href") or "").strip()
        if f"/team/{team_id}" not in href and f"/app/team/{team_id}" not in href:
            continue

        container = link.evaluate_handle(
            """
            element => {
                let node = element;
                for (let i = 0; i < 8 && node; i += 1, node = node.parentElement) {
                    const text = (node.innerText || '').trim();
                    if (/\\d+\\s*\\|\\s*\\d+/.test(text)) {
                        return node;
                    }
                }
                return element.parentElement;
            }
            """
        ).as_element()

        if not container:
            continue

        text = (container.inner_text() or "").strip()
        score_match = re.search(r"(\\d+)\\s*\\|\\s*(\\d+)", text)
        if not score_match:
            app.logger.info("Skipping candidate without score pattern: %s", text[:250])
            continue

        team_links = container.query_selector_all("a[href*='/team/'], a[href*='/app/team/']")
        teams: list[tuple[str, str]] = []
        seen_hrefs: set[str] = set()

        for team_link in team_links:
            team_href = (team_link.get_attribute("href") or "").strip()
            team_name = (team_link.inner_text() or "").strip()
            if "/team/" not in team_href or not team_name or team_href in seen_hrefs:
                continue
            seen_hrefs.add(team_href)
            teams.append((team_name, team_href))

        if len(teams) < 2:
            app.logger.info("Skipping candidate with fewer than 2 team links: %s", text[:250])
            continue

        home_score, away_score = map(int, score_match.groups())
        return {
            "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "home": {"name": teams[0][0], "score": home_score},
            "away": {"name": teams[1][0], "score": away_score},
        }

    return None


# =====================================================
# SCRAPER LOOP
# =====================================================
def scrape_loop(team_id: int, slug: str, room: str) -> None:
    last_state: dict[str, Any] | None = None
    socketio.emit("app_mode", {"test_mode": TEST_MODE}, room=room)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
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