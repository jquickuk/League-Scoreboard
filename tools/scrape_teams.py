import json
import re
import requests
from bs4 import BeautifulSoup

# -------------------------------------------------
# CONFIG
# -------------------------------------------------

LEAGUE_DOMAIN = "app.westonpoolleague.org"

DIVISION_INDEX_URL = (
    "https://www.rackemapp.com/leagues/"
    f"{LEAGUE_DOMAIN}/tables/all"
)

DIVISION_TABLE_URL = (
    f"https://{LEAGUE_DOMAIN}/app/tables/{{}}"
)

OUTPUT_FILE = "teams.json"

# Division ID -> Friendly name (INT keys)
DIVISION_NAMES = {
    2342: "Division 1",
    2343: "Division 2",
    2344: "Division 3",
    2345: "Division 4",
    2346: "Division 5",
    2347: "Division 6",
}

DIVISION_ID_REGEX = re.compile(r"/tables/(\d+)")
TEAM_ID_REGEX = re.compile(r"/app/team/(\d+)")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}

# -------------------------------------------------
# HELPERS
# -------------------------------------------------

def slugify(name: str) -> str:
    name = name.lower()
    name = name.replace("&", " and ")
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"\s+", "-", name)
    return name.strip("-")


def fetch(url: str) -> str:
    print("GET", url)
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.text


# -------------------------------------------------
# SCRAPE
# -------------------------------------------------

# 1. Discover division IDs
html = fetch(DIVISION_INDEX_URL)
soup = BeautifulSoup(html, "html.parser")

division_ids = set()
for a in soup.find_all("a", href=True):
    match = DIVISION_ID_REGEX.search(a["href"])
    if match:
        division_ids.add(int(match.group(1)))

print(f"Found {len(division_ids)} divisions")

teams_by_division = {}

# 2. Scrape each division page
for division_id in sorted(division_ids):
    division_name = DIVISION_NAMES.get(
        division_id, f"Division {division_id}"
    )

    html = fetch(DIVISION_TABLE_URL.format(division_id))
    soup = BeautifulSoup(html, "html.parser")

    teams = []

    for a in soup.find_all("a", href=True):
        match = TEAM_ID_REGEX.search(a["href"])
        if not match:
            continue

        team_name = a.get_text(strip=True)
        if not team_name:
            continue

        teams.append({
            "id": int(match.group(1)),
            "name": team_name,
            "slug": slugify(team_name)
        })

    if teams:
        teams_by_division[division_name] = teams

# 3. Ensure unique slugs across all divisions
seen_slugs = set()
for teams in teams_by_division.values():
    for team in teams:
        if team["slug"] in seen_slugs:
            team["slug"] = f"{team['slug']}-{team['id']}"
        seen_slugs.add(team["slug"])

# 4. Write output
with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
    json.dump(teams_by_division, f, indent=2)

total_teams = sum(len(t) for t in teams_by_division.values())
print(
    f"Saved {total_teams} teams across "
    f"{len(teams_by_division)} divisions to {OUTPUT_FILE}"
)
