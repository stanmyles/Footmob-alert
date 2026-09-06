[23:13, 06/09/2026] L: import os
import requests
from datetime import datetime, timezone, timedelta

API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "7"))
COMPETITIONS = [c.strip() for c in os.getenv("COMPETITIONS", "PL,PD,SA,BL1,FL1,UCL").split(",") if c.strip()]

BASE_URL = "https://api.football-data.org/v4"
HEADERS = {
    "X-Auth-Token": API_KEY
}

REQUEST_TIMEOUT = 30
MAX_MATCHES_SENT = 500


def api_get(path, params=None):
    url = f"{BASE_URL}{path}"
    response = requests.get(url, headers=HEADERS, params=params or {}, timeout=REQUEST_TIMEOUT)

    print("URL appelée :", response.url)
    print("Code API :", response.status_code)
    print("Réponse API :", …
[23:16, 06/09/2026] L: je veux filtrer sur une semaine à partir d'un API tous les matchs de football opposant deux équipes avec l'une ultra dominante par rapport à l'autre au vu des confrontation gagnante pour l'une des deux équipes d'un point de vue historique et l'écart de classement dans le championnat auquel ils ppartienent
[23:17, 06/09/2026] L: je veux filtrer sur les deux prochains jours à partir de l'API tous les matchs de football opposant deux équipes avec l'une ultra dominante par rapport à l'autre au vu des confrontation gagnante pour l'une des deux équipes d'un point de vue historique et l'écart de classement dans le championnat auquel ils ppartienent
[23:19, 06/09/2026] L: name: Football Dominance Alert 2 Days

on:
  workflow_dispatch:
    inputs:
      competitions:
        description: "Codes competitions separes par des virgules (ex: PL,PD,SA,BL1,FL1,UCL)"
        required: true
        default: "PL,PD,SA,BL1,FL1,UCL"

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
          FOOTBALL_DATA_API_KEY: ${{ secrets.FOOTBALL_DATA_API_KEY }}
          DISCORD_WEBHO…
[23:21, 06/09/2026] L: import os
import requests
from datetime import datetime, timezone, timedelta

API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
COMPETITIONS = [c.strip() for c in os.getenv("COMPETITIONS", "PL,PD,SA,BL1,FL1,UCL").split(",") if c.strip()]

BASE_URL = "https://api.football-data.org/v4"
HEADERS = {
    "X-Auth-Token": API_KEY
}

REQUEST_TIMEOUT = 30
DAYS_AHEAD = 2
MIN_RANK_GAP = 8
MIN_H2H_MATCHES = 5
MIN_H2H_WIN_GAP = 4
MAX_MATCHES_SENT = 100


def api_get(path, params=None):
    url = f"{BASE_URL}{path}"
    response = requests.get(url, headers=HEADERS, params=params or {}, timeout=REQUEST_TIMEOUT)

    print("URL appelée :", response.url)
    print("Code API :", response.status_code)
    print("Réponse API :", response.text[:300])

    if response.status_code != 200:
        raise RuntimeError(f"Erreur API {response.status_code} sur {path} : {response.text[:300]}")

    return response.json()


def send_discord_message(content):
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
            raise RuntimeError(f"Erreur Discord {response.status_code} : {response.text[:300]}")


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


def format_match_time(utc_date_str):
    if not utc_date_str:
        return "Heure inconnue"

    try:
        dt = datetime.fromisoformat(utc_date_str.replace("Z", "+00:00"))
        dt_paris = dt.astimezone(timezone(timedelta(hours=2)))
        return dt_paris.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return utc_date_str


def get_matches_for_competition(code, start_date, end_date):
    data = api_get(
        f"/competitions/{code}/matches",
        params={"dateFrom": start_date, "dateTo": end_date}
    )
    return data.get("matches", [])


def get_standings_for_competition(code):
    data = api_get(f"/competitions/{code}/standings")
    standings = data.get("standings", [])

    rows = []
    for table in standings:
        if table.get("type") == "TOTAL" or not rows:
            rows = table.get("table", [])
            if rows:
                break

    result = {}
    for row in rows:
        team = row.get("team", {})
        team_id = team.get("id")
        result[team_id] = {
            "position": row.get("position"),
            "points": row.get("points"),
            "team_name": team.get("name", "Inconnu")
        }

    return result


def get_head2head(match_id):
    data = api_get(f"/matches/{match_id}/head2head")
    return data.get("aggregates", data.get("head2head", data))


def get_all_matches():
    start_dt = get_now_paris()
    end_dt = start_dt + timedelta(days=DAYS_AHEAD - 1)

    start_date = get_date_str(start_dt)
    end_date = get_date_str(end_dt)

    all_matches = []

    for code in COMPETITIONS:
        try:
            matches = get_matches_for_competition(code, start_date, end_date)
            print(f"Competition {code} -> {len(matches)} matchs")
            all_matches.extend(matches)
        except Exception as e:
            print(f"Erreur competition {code} :", str(e))
            continue

    return start_date, end_date, all_matches


def analyze_match(match, standings_cache):
    competition = match.get("competition", {})
    competition_code = competition.get("code")
    match_id = match.get("id")

    home = match.get("homeTeam", {})
    away = match.get("awayTeam", {})

    home_id = home.get("id")
    away_id = away.get("id")

    if not competition_code or not match_id or not home_id or not away_id:
        return None

    if competition_code not in standings_cache:
        standings_cache[competition_code] = get_standings_for_competition(competition_code)

    standings = standings_cache[competition_code]
    if home_id not in standings or away_id not in standings:
        return None

    home_rank = standings[home_id]["position"]
    away_rank = standings[away_id]["position"]
    rank_gap = abs(home_rank - away_rank)

    h2h = get_head2head(match_id)

    home_wins = h2h.get("homeTeamWins", 0)
    away_wins = h2h.get("awayTeamWins", 0)
    draws = h2h.get("draws", 0)
    total_h2h = home_wins + away_wins + draws
    h2h_gap = abs(home_wins - away_wins)

    if total_h2h < MIN_H2H_MATCHES:
        return None

    if rank_gap < MIN_RANK_GAP:
        return None

    if h2h_gap < MIN_H2H_WIN_GAP:
        return None

    if home_wins > away_wins:
        dominant_team = home.get("name", "Domicile")
    elif away_wins > home_wins:
        dominant_team = away.get("name", "Extérieur")
    else:
        return None

    return {
        "competition_name": competition.get("name", "Compétition inconnue"),
        "competition_code": competition_code,
        "home_name": home.get("name", "Domicile"),
        "away_name": away.get("name", "Extérieur"),
        "time": format_match_time(match.get("utcDate")),
        "status": match.get("status", "UNKNOWN"),
        "home_rank": home_rank,
        "away_rank": away_rank,
        "rank_gap": rank_gap,
        "home_points": standings[home_id]["points"],
        "away_points": standings[away_id]["points"],
        "home_wins": home_wins,
        "away_wins": away_wins,
        "draws": draws,
        "total_h2h": total_h2h,
        "h2h_gap": h2h_gap,
        "dominant_team": dominant_team,
    }


def build_message(start_date, end_date, matches):
    if not matches:
        return (
            f"📅 Matchs du {start_date} au {end_date}\n\n"
            f"Aucune affiche ne présente à la fois une domination historique forte et un gros écart de classement."
        )

    lines = [
        f"📅 Matchs du {start_date} au {end_date}",
        "",
        "⚠️ Affiches avec ultra domination historique + écart de classement",
        ""
    ]

    for i, m in enumerate(matches, start=1):
        lines.extend([
            f"{i}. {m['home_name']} vs {m['away_name']}",
            f"🏆 {m['competition_name']} ({m['competition_code']})",
            f"🕒 {m['time']}",
            f"📊 Classement : {m['home_name']} #{m['home_rank']} ({m['home_points']} pts) vs {m['away_name']} #{m['away_rank']} ({m['away_points']} pts)",
            f"📈 Écart classement : {m['rank_gap']}",
            f"📚 H2H : {m['home_name']} {m['home_wins']} victoires | {m['away_name']} {m['away_wins']} victoires | Nuls {m['draws']}",
            f"🔥 Équipe historiquement dominante : {m['dominant_team']}",
            ""
        ])

    lines.append(
        f"Filtres utilisés : écart classement >= {MIN_RANK_GAP}, au moins {MIN_H2H_MATCHES} confrontations H2H, et écart H2H >= {MIN_H2H_WIN_GAP} victoires."
    )
    return "\n".join(lines)


def main():
    if not API_KEY:
        raise RuntimeError("FOOTBALL_DATA_API_KEY manquant")
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL manquant")

    start_date, end_date, all_matches = get_all_matches()

    standings_cache = {}
    selected = []

    for match in all_matches:
        try:
            result = analyze_match(match, standings_cache)
            if result:
                selected.append(result)
        except Exception as e:
            print("Erreur analyse match :", str(e))
            continue

    selected.sort(key=lambda x: (-x["h2h_gap"], -x["rank_gap"], x["time"]))
    selected = selected[:MAX_MATCHES_SENT]

    message = build_message(start_date, end_date, selected)
    send_discord_message(message)

    print("Message envoyé sur Discord.")
    print(f"Nombre de matchs retenus : {len(selected)}")


if __name__ == "__main__":
    main()
