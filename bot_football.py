import os
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

    if response.status_code != 200:
        raise RuntimeError(
            f"Erreur API {response.status_code} sur {path} : {response.text[:300]}"
        )

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

        print("Discord status :", response.status_code)
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


def get_current_year_paris():
    return get_now_paris().year


def get_matches_for_date(date_str):
    timezone_offset = 7200
    all_events = []

    try:
        data = api_get(f"/api/v1/sport/{SPORT}/scheduled-events/{date_str}")
        events = data.get("events", [])
        if events:
            return events
    except Exception:
        pass

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
        except Exception:
            continue

    return all_events


def get_matches_week():
    start_dt = get_now_paris()
    all_events = []

    for i in range(DAYS_AHEAD):
        date_dt = start_dt + timedelta(days=i)
        date_str = get_date_str(date_dt)

        try:
            events = get_matches_for_date(date_str)
            all_events.extend(events)
        except Exception as e:
            print(f"Erreur récupération date {date_str} :", str(e))
            continue

    start_date = get_date_str(start_dt)
    end_date = get_date_str(start_dt + timedelta(days=DAYS_AHEAD - 1))
    return start_date, end_date, all_events


def get_standings(unique_tournament_id, season_id):
    data = api_get(
        f"/api/v1/unique-tournament/{unique_tournament_id}/season/{season_id}/standings/total"
    )

    standings_blocks = data.get("standings", [])
    rows = []

    for block in standings_blocks:
        for row in block.get("rows", []):
            team = row.get("team", {})
            rows.append({
                "team_id": team.get("id"),
                "position": row.get("position"),
                "points": row.get("points"),
                "team_name": team.get("name", "Inconnu")
            })

    return rows


def get_h2h_events(home_id, away_id):
    paths_to_try = [
        f"/api/v1/{SPORT}/h2h/{home_id}/{away_id}",
        f"/api/v1/h2h/{home_id}/{away_id}",
    ]

    for path in paths_to_try:
        try:
            data = api_get(path)
            events = data.get("events", [])
            if events:
                return events
        except Exception:
            continue

    return []


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


def get_event_year(event):
    ts = event.get("startTimestamp")
    if not ts:
        return None
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=2)
    return dt.year


def get_event_winner_name(event):
    winner_code = event.get("winnerCode")
    home = event.get("homeTeam", {})
    away = event.get("awayTeam", {})

    if winner_code == 1:
        return home.get("name")
    if winner_code == 2:
        return away.get("name")
    return None


def analyze_h2h(home_id, away_id, home_name, away_name):
    h2h_events = get_h2h_events(home_id, away_id)
    current_year = get_current_year_paris()
    years_to_keep = {current_year, current_year - 1}

    yearly = {
        current_year - 1: {"home_wins": 0, "away_wins": 0, "draws": 0},
        current_year: {"home_wins": 0, "away_wins": 0, "draws": 0},
    }

    counted_matches = 0

    for event in h2h_events:
        year = get_event_year(event)
        if year not in years_to_keep:
            continue

        winner_name = get_event_winner_name(event)
        counted_matches += 1

        if winner_name == home_name:
            yearly[year]["home_wins"] += 1
        elif winner_name == away_name:
            yearly[year]["away_wins"] += 1
        else:
            yearly[year]["draws"] += 1

    total_home = sum(v["home_wins"] for v in yearly.values())
    total_away = sum(v["away_wins"] for v in yearly.values())
    total_draws = sum(v["draws"] for v in yearly.values())
    total_gap = abs(total_home - total_away)

    if total_home > total_away:
        dominant = home_name
    elif total_away > total_home:
        dominant = away_name
    else:
        dominant = "Équilibre"

    return {
        "yearly": yearly,
        "total_home_wins": total_home,
        "total_away_wins": total_away,
        "total_draws": total_draws,
        "total_gap": total_gap,
        "dominant": dominant,
        "counted_matches": counted_matches,
    }


def analyze_matches():
    start_date, end_date, events = get_matches_week()

    if not events:
        return start_date, end_date, []

    standings_cache = {}
    h2h_cache = {}
    selected = []

    for event in events:
        info = parse_event(event)

        utid = info["unique_tournament_id"]
        season_id = info["season_id"]
        home_id = info["home_id"]
        away_id = info["away_id"]

        if not utid or not season_id or not home_id or not away_id:
            continue

        standings_key = f"{utid}_{season_id}"
        if standings_key not in standings_cache:
            try:
                standings_cache[standings_key] = get_standings(utid, season_id)
            except Exception:
                standings_cache[standings_key] = []

        standings_rows = standings_cache[standings_key]
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

        pair_key = tuple(sorted([home_id, away_id]))
        if pair_key not in h2h_cache:
            h2h_cache[pair_key] = analyze_h2h(
                home_id, away_id, info["home_name"], info["away_name"]
            )

        h2h = h2h_cache[pair_key]

        standings_ok = rank_gap >= MIN_RANK_GAP or (
            points_gap is not None and points_gap >= MIN_POINTS_GAP
        )

        h2h_ok = (
            h2h["counted_matches"] >= MIN_H2H_MATCHES
            and h2h["total_gap"] >= MIN_H2H_WIN_GAP_TOTAL
            and h2h["dominant"] != "Équilibre"
        )

        if standings_ok and h2h_ok:
            favorite_rank = info["home_name"] if home_rank < away_rank else info["away_name"]

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
                "favorite_rank": favorite_rank,
                "h2h": h2h,
            })

    selected.sort(
        key=lambda x: (
            x["time"],
            -x["h2h"]["total_gap"],
            -x["rank_gap"],
            -(x["points_gap"] or 0),
        )
    )

    return start_date, end_date, selected[:MAX_MATCHES_SENT]


def build_h2h_lines(match):
    h2h = match["h2h"]
    yearly = h2h["yearly"]
    years = sorted(yearly.keys())

    lines = []
    for year in years:
        y = yearly[year]
        lines.append(
            f"📚 H2H {year} : {match['home_name']} {y['home_wins']} victoires | "
            f"{match['away_name']} {y['away_wins']} victoires | Nuls {y['draws']}"
        )

    lines.append(
        f"🔥 Dominant historique : {h2h['dominant']} "
        f"({h2h['total_home_wins']} vs {h2h['total_away_wins']}, nuls {h2h['total_draws']})"
    )
    return lines


def build_message(start_date, end_date, matches):
    if not matches:
        return (
            f"📅 Matchs de football du {start_date} au {end_date}\n\n"
            f"Aucune affiche ne remplit les critères de domination historique et d’écart de classement."
        )

    lines = [
        f"📅 Matchs de football du {start_date} au {end_date}",
        "",
        "⚠️ Affiches avec domination historique + gros écart de niveau",
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
            f"⭐ Mieux classé : {m['favorite_rank']}",
        ])

        lines.extend(build_h2h_lines(m))
        lines.append("")

    lines.append(
        "Filtres : au moins 3 H2H sur 2 ans, au moins 3 victoires d’écart au total, et gros écart de classement ou de points."
    )
    return "\n".join(lines)


def main():
    if not RAPIDAPI_KEY:
        raise RuntimeError("RAPIDAPI_KEY manquant")
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL manquant")

    start_date, end_date, matches = analyze_matches()
    message = build_message(start_date, end_date, matches)
    send_discord_message(message)

    print("Message hebdomadaire envoyé sur Discord.")
    print(f"Nombre de matchs retenus : {len(matches)}")


if __name__ == "__main__":
    main()
