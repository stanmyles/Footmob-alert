[22:47, 06/09/2026] L: import os
import requests
from datetime import datetime, timezone, timedelta

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

BASE_URL = "https://sportapi7.p.rapidapi.com"
HEADERS = {
    "x-rapidapi-host": "sportapi7.p.rapidapi.com",
    "x-rapidapi-key": RAPIDAPI_KEY,
}

SPORT = "football"

MIN_RANK_GAP = 8
MIN_POINTS_GAP = 12
MIN_H2H_WIN_GAP_TOTAL = 3
MIN_H2H_MATCHES = 3
MAX_MATCHES_SENT = 40
REQUEST_TIMEOUT = 30
DAYS_AHEAD = 7


def api_get(path):
    url = f"{BASE_URL}{path}"
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)

    print("URL appelée :", url)
    print("Code API :", response.status_code)
    print("Réponse API :", response.text[:300])

    if…
[22:50, 06/09/2026] L: name: Bot Football Weekly Dominance Alert

on:
  workflow_dispatch:
  schedule:
    - cron: "0 6 * * 1"

jobs:
  run-bot:
    runs-on: ubuntu-latest

    steps:
      - name: Recuperer le code
        uses: actions/checkout@v4

      - name: Installer Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.10"

      - name: Installer les dependances
        run: |
          python -m pip install --upgrade pip
          pip install requests

      - name: Verifier les secrets
        env:
          RAPIDAPI_KEY: ${{ secrets.RAPIDAPI_KEY }}
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
        run: |
          if [ -z "$RAPIDAPI_KEY" ]; then
            echo "RAPIDAPI_KEY manquant"
            exit 1
       …
[22:54, 06/09/2026] L: name: Test All Football Matches

on:
  workflow_dispatch:
    inputs:
      days_ahead:
        description: "Nombre de jours à analyser"
        required: true
        default: "7"

jobs:
  run-bot:
    runs-on: ubuntu-latest

    steps:
      - name: Recuperer le code
        uses: actions/checkout@v4

      - name: Installer Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.10"

      - name: Installer les dependances
        run: |
          python -m pip install --upgrade pip
          pip install requests

      - name: Verifier les secrets
        env:
          RAPIDAPI_KEY: ${{ secrets.RAPIDAPI_KEY }}
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
        run: |
          if [ -z "$RAPIDAPI_KE…
[22:56, 06/09/2026] L: import os
import requests
from datetime import datetime, timezone, timedelta

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "7"))

BASE_URL = "https://sportapi7.p.rapidapi.com"
HEADERS = {
    "x-rapidapi-host": "sportapi7.p.rapidapi.com",
    "x-rapidapi-key": RAPIDAPI_KEY,
}

SPORT = "football"
REQUEST_TIMEOUT = 30
MAX_MATCHES_SENT = 500


def api_get(path):
    url = f"{BASE_URL}{path}"
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)

    print("URL appelée :", url)
    print("Code API :", response.status_code)
    print("Réponse API :", response.text[:300])

    if response.status_code != 200:
        raise RuntimeError(
            f"Erreur API {response.status_code} sur {path} : {response.text[:300]}"
        )

    return response.json()


def send_discord_message(content):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL manquant")

    chunks = split_message(content, 1900)

    for i, chunk in enumerate(chunks, start=1):
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": chunk},
            timeout=REQUEST_TIMEOUT
        )

        print(f"Discord status bloc {i} :", response.status_code)
        print("Discord response :", response.text[:300])

        if response.status_code not in (200, 204):
            raise RuntimeError(
                f"Erreur Discord {response.status_code} : {response.text[:300]}"
            )


def split_message(text, max_len=1900):
    if len(text) <= max_len:
        return [text]

    parts = []
    current = ""

    for line in text.splitlines():
        if len(current) + len(line) + 1 <= max_len:
            current += line + "\n"
        else:
            if current.strip():
                parts.append(current.strip())
            current = line + "\n"

    if current.strip():
        parts.append(current.strip())

    return parts


def get_now_paris():
    return datetime.now(timezone.utc) + timedelta(hours=2)


def get_date_str(dt):
    return dt.strftime("%Y-%m-%d")


def format_match_time(timestamp_value):
    if not timestamp_value:
        return "Heure inconnue"

    dt_utc = datetime.fromtimestamp(timestamp_value, tz=timezone.utc)
    dt_paris = dt_utc + timedelta(hours=2)
    return dt_paris.strftime("%d/%m/%Y %H:%M")


def get_matches_for_date(date_str):
    timezone_offset = 7200
    all_events = []

    try:
        data = api_get(f"/api/v1/sport/{SPORT}/scheduled-events/{date_str}")
        events = data.get("events", [])
        if events:
            return events
    except Exception:
        print(f"Endpoint global indisponible pour {date_str}, tentative par catégories")

    categories_data = api_get(f"/api/v1/sport/{SPORT}/{date_str}/{timezone_offset}/categories")
    categories = categories_data.get("categories", [])

    for category in categories:
        category_id = category.get("id")
        if not category_id:
            continue

        try:
            category_data = api_get(f"/api/v1/category/{category_id}/scheduled-events/{date_str}")
            events = category_data.get("events", [])
            all_events.extend(events)
        except Exception as e:
            print(f"Erreur catégorie {category_id} le {date_str} :", str(e))
            continue

    return all_events


def get_matches_period():
    start_dt = get_now_paris()
    all_events = []

    for i in range(DAYS_AHEAD):
        current_dt = start_dt + timedelta(days=i)
        date_str = get_date_str(current_dt)

        try:
            events = get_matches_for_date(date_str)
            print(f"{date_str} -> {len(events)} matchs")
            all_events.extend(events)
        except Exception as e:
            print(f"Erreur récupération {date_str} :", str(e))
            continue

    start_date = get_date_str(start_dt)
    end_date = get_date_str(start_dt + timedelta(days=DAYS_AHEAD - 1))
    return start_date, end_date, all_events


def parse_event(event):
    tournament = event.get("tournament", {})
    category = event.get("category", {})
    home = event.get("homeTeam", {})
    away = event.get("awayTeam", {})

    return {
        "home_name": home.get("name", "Domicile"),
        "away_name": away.get("name", "Extérieur"),
        "competition": tournament.get("name", "Compétition inconnue"),
        "country": category.get("name", "Pays inconnu"),
        "time": format_match_time(event.get("startTimestamp")),
    }


def build_message(start_date, end_date, events):
    if not events:
        return (
            f"📅 Test matchs football du {start_date} au {end_date}\n\n"
            f"Aucun match récupéré."
        )

    lines = [
        f"📅 Test matchs football du {start_date} au {end_date}",
        f"Nombre total de matchs récupérés : {len(events)}",
        "",
    ]

    parsed = [parse_event(event) for event in events[:MAX_MATCHES_SENT]]
    parsed.sort(key=lambda x: (x["time"], x["country"], x["competition"], x["home_name"]))

    for i, match in enumerate(parsed, start=1):
        lines.extend([
            f"{i}. {match['home_name']} vs {match['away_name']}",
            f"🏆 {match['competition']} ({match['country']})",
            f"🕒 {match['time']}",
            ""
        ])

    if len(events) > MAX_MATCHES_SENT:
        lines.append(
            f"Liste tronquée à {MAX_MATCHES_SENT} matchs pour éviter un volume trop important."
        )

    return "\n".join(lines)


def main():
    if not RAPIDAPI_KEY:
        raise RuntimeError("RAPIDAPI_KEY manquant")
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL manquant")

    start_date, end_date, events = get_matches_period()
    message = build_message(start_date, end_date, events)
    send_discord_message(message)

    print("Message de test envoyé sur Discord.")
    print(f"Nombre total de matchs récupérés : {len(events)}")


if __name__ == "__main__":
    main()
