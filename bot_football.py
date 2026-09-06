import os
import math
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
MAX_MATCHES_SENT = 25
REQUEST_TIMEOUT = 30


def api_get(path):
    url = f"{BASE_URL}{path}"
    response = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    if response.status_code != 200:
        raise RuntimeError(f"Erreur API {response.status_code} sur {path} : {response.text[:300]}")
    return response.json()


def send_discord_message(content):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL manquant")

    chunks = split_message(content, 1900)
    for chunk in chunks:
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": chunk},
            timeout=REQUEST_TIMEOUT
        )
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


def get_today_date_paris():
    paris_offset = timedelta(hours=2)
    return (datetime.now(timezone.utc) + paris_offset).strftime("%Y-%m-%d")


def get_matches_today():
    date_str = get_today_date_paris()
    data = api_get(f"/api/v1/sport/{SPORT}/scheduled-events/{date_str}")

    events = data.get("events", [])
    return date_str, events


def get_standings(unique_tournament_id, season_id):
    data = api_get(
        f"/api/v1/unique-tournament/{unique_tournament_id}/season/{season_id}/standings/total"
    )

    standings_blocks = data.get("standings", [])
    rows = []

    for block in standings_blocks:
        for row in block.get("rows", []):
            team = row.get("team", {})
            team_id = team.get("id")
            position = row.get("position")
            points = row.get("points")

            rows.append({
                "team_id": team_id,
                "position": position,
                "points": points,
                "team_name": team.get("name", "Inconnu")
            })

    return rows


def extract_team_ids(event):
    home = event.get("homeTeam", {})
    away = event.get("awayTeam", {})
    return home, away


def get_rank_info(standings_rows, home_id, away_id):
    home_rank = None
    away_rank = None
    home_points = None
    away_points = None

    for row in standings_rows:
        if row["team_id"] == home_id:
            home_rank = row["position"]
            home_points = row["points"]
        elif row["team_id"] == away_id:
            away_rank = row["position"]
            away_points = row["points"]

    return home_rank, away_rank, home_points, away_points


def parse_event(event):
    tournament = event.get("tournament", {})
    unique_tournament = tournament.get("uniqueTournament", {})
    season = event.get("season", {})
    home, away = extract_team_ids(event)

    return {
        "event_id": event.get("id"),
        "home_name": home.get("name", "Domicile"),
        "away_name": away.get("name", "Extérieur"),
        "home_id": home.get("id"),
        "away_id": away.get("id"),
        "tournament_name": tournament.get("name", "Compétition inconnue"),
        "country_name": event.get("category", {}).get("name", "Pays inconnu"),
        "start": event.get("startTimestamp"),
        "unique_tournament_id": unique_tournament.get("id"),
        "season_id": season.get("id"),
    }


def format_match_time(timestamp_value):
    if not timestamp_value:
        return "Heure inconnue"

    dt_utc = datetime.fromtimestamp(timestamp_value, tz=timezone.utc)
    dt_paris = dt_utc + timedelta(hours=2)
    return dt_paris.strftime("%d/%m/%Y %H:%M")


def analyze_matches():
    date_str, events = get_matches_today()

    if not events:
        return date_str, []

    standings_cache = {}
    selected = []

    for event in events:
        info = parse_event(event)

        utid = info["unique_tournament_id"]
        season_id = info["season_id"]
        home_id = info["home_id"]
        away_id = info["away_id"]

        if not utid or not season_id or not home_id or not away_id:
            continue

        cache_key = f"{utid}_{season_id}"
        if cache_key not in standings_cache:
            try:
                standings_cache[cache_key] = get_standings(utid, season_id)
            except Exception:
                standings_cache[cache_key] = []

        standings_rows = standings_cache[cache_key]
        if not standings_rows:
            continue

        home_rank, away_rank, home_points, away_points = get_rank_info(
            standings_rows, home_id, away_id
        )

        if home_rank is None or away_rank is None:
            continue

        rank_gap = abs(home_rank - away_rank)
        points_gap = None
        if home_points is not None and away_points is not None:
            points_gap = abs(home_points - away_points)

        if rank_gap >= MIN_RANK_GAP or (
            points_gap is not None and points_gap >= MIN_POINTS_GAP
        ):
            if home_rank < away_rank:
                favorite = info["home_name"]
                outsider = info["away_name"]
            else:
                favorite = info["away_name"]
                outsider = info["home_name"]

            selected.append({
                "time": format_match_time(info["start"]),
                "competition": info["tournament_name"],
                "country": info["country_name"],
                "home_name": info["home_name"],
                "away_name": info["away_name"],
                "home_rank": home_rank,
                "away_rank": away_rank,
                "home_points": home_points,
                "away_points": away_points,
                "rank_gap": rank_gap,
                "points_gap": points_gap,
                "favorite": favorite,
                "outsider": outsider,
            })

    selected.sort(key=lambda x: (-x["rank_gap"], -(x["points_gap"] or 0), x["time"]))
    return date_str, selected[:MAX_MATCHES_SENT]


def build_message(date_str, matches):
    if not matches:
        return (
            f"📅 Matchs du {date_str}\n\n"
            f"Aucune affiche avec gros écart de classement détectée aujourd’hui."
        )

    lines = [
        f"📅 Matchs du {date_str}",
        "",
        "⚠️ Affiches avec gros écart de niveau estimé",
        ""
    ]

    for i, m in enumerate(matches, start=1):
        points_text = (
            f"{m['home_points']} pts vs {m['away_points']} pts"
            if m["home_points"] is not None and m["away_points"] is not None
            else "points indisponibles"
        )

        lines.extend([
            f"{i}. {m['home_name']} vs {m['away_name']}",
            f"🏆 Compétition : {m['competition']} ({m['country']})",
            f"🕒 Heure : {m['time']}",
            f"📊 Classement : {m['home_name']} #{m['home_rank']} vs {m['away_name']} #{m['away_rank']}",
            f"📈 Écart classement : {m['rank_gap']}",
            f"📉 Points : {points_text}",
            f"⭐ Favori théorique : {m['favorite']}",
            ""
        ])

    lines.append("Critères : gros écart de classement ou de points dans leur championnat.")
    return "\n".join(lines)


def main():
    if not RAPIDAPI_KEY:
        raise RuntimeError("RAPIDAPI_KEY manquant")
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL manquant")

    date_str, matches = analyze_matches()
    message = build_message(date_str, matches)
    send_discord_message(message)

    print("Message envoyé sur Discord.")
    print(f"Nombre de matchs retenus : {len(matches)}")


if __name__ == "__main__":
    main()
