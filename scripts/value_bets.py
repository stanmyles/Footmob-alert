import os
import json
import csv
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_URL = "https://api.football-data.org/v4"
TOKEN = os.environ["FOOTBALL_DATA_API_TOKEN"]
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", 2))
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

HEADERS = {"X-Auth-Token": TOKEN}
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

def get_matches():
    r = requests.get(f"{BASE_URL}/matches", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json().get("matches", [])

def filter_upcoming(matches, days_ahead):
    now = datetime.now(timezone.utc)
    limit = now + timedelta(days=days_ahead)
    selected = []
    for m in matches:
        utc_date = datetime.fromisoformat(m["utcDate"].replace("Z", "+00:00"))
        if now <= utc_date <= limit:
            selected.append(m)
    return selected

def estimate_home_win_probability(match):
    return 0.50

def fair_odds(prob):
    if prob <= 0:
        return None
    return 1 / prob

def main():
    matches = get_matches()
    upcoming = filter_upcoming(matches, DAYS_AHEAD)

    rows = []
    for m in upcoming:
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        p_home = estimate_home_win_probability(m)
        odds_fair = fair_odds(p_home)

        rows.append({
            "utcDate": m["utcDate"],
            "competition": m["competition"]["code"],
            "homeTeam": home,
            "awayTeam": away,
            "p_home_model": round(p_home, 4),
            "fair_home_odds": round(odds_fair, 3) if odds_fair else None
        })

    # Écriture CSV
    csv_path = OUTPUT_DIR / "value_bets.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    # Écriture JSON
    json_path = OUTPUT_DIR / "value_bets.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    # Envoi Discord
    if DISCORD_WEBHOOK_URL and rows:
        top = rows[:10]
        content = "Top matchs analysés:\n" + "\n".join(
            f"- {r['homeTeam']} vs {r['awayTeam']} | p_home={r['p_home_model']} | fair_odds={r['fair_home_odds']}"
            for r in top
        )
        requests.post(DISCORD_WEBHOOK_URL, json={"content": content[:1900]}, timeout=20)

if __name__ == "__main__":
    main()
