from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
import time
import re

TEAM_ID = "47720"   # Frames Fugitives or any team
LIVE_URL = "https://app.westonpoolleague.org/app/livescores/all"

options = Options()
options.add_argument("--headless=new")
options.add_argument("--disable-gpu")

driver = webdriver.Chrome(options=options)

try:
    driver.get(LIVE_URL)
    time.sleep(5)  # allow JS to render

    matches = []

    rows = driver.find_elements(By.CSS_SELECTOR, "div.row.pb-3.mx-0")

    for row in rows:
        team_links = row.find_elements(By.CSS_SELECTOR, "a[href*='/team/']")
        if len(team_links) != 2:
            continue

        home_link = team_links[0].get_attribute("href")
        away_link = team_links[1].get_attribute("href")

        # ✅ THIS is the correct filter
        if f"/team/{TEAM_ID}" not in home_link and f"/team/{TEAM_ID}" not in away_link:
            continue

        home_team = team_links[0].text.strip()
        away_team = team_links[1].text.strip()

        score_el = row.find_element(By.CSS_SELECTOR, "span.text-lighter")
        score_text = score_el.text.strip()

        m = re.search(r"(\d+)\s*\|\s*(\d+)", score_text)
        if not m:
            continue

        home_score, away_score = m.groups()

        matches.append({
            "home_team": home_team,
            "home_score": int(home_score),
            "away_team": away_team,
            "away_score": int(away_score)
        })

    if not matches:
        print("No live matches found for this team.")
    else:
        for m in matches:
            print(
                f"{m['home_team']} {m['home_score']} - "
                f"{m['away_score']} {m['away_team']}"
            )

finally:
    driver.quit()
