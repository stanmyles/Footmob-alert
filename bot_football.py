import os
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
MIN_H2H_MATCHES = 2
MIN_H2H_WIN_GAP = 2
H2H_LIMIT = 50
MAX_MATCHES_SENT = 100


def api_get(path, params=None):
    url = f"{BASE_URL}{path}"
    response = requests.get(url, headers=HEADERS, params=params or {}, timeout=REQUEST_TIMEOUT)

    print("URL appelée :", response.url)
    print("Code API :", response.status_code)
    print("Réponse API :", response.text[:500])

    if response.status_code != 200:
        raise RuntimeError(f"Erreur API {response.status_code} sur {path} : {response.text[:500]}")

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


def get_matches_for_competition(code, start_date, end_date_exclusive):
    data = api_get(
        f"/competitions/{code}/matches",
        params={"dateFrom": start_date, "dateTo": end_date_exclusive}
    )
    return data.get("matches", [])


def get_head2head(match_id):
    today = get_now_paris().date()
    date_to = today.strftime("%Y-%m-%d")
    date_from = (today - timedelta(days=730)).strftime("%Y-%m-%d")

    return api_get(
        f"/matches/{match_id}/head2head",
        params={
            "limit": H2H_LIMIT,
            "dateFrom": date_from,
            "dateTo": date_to
        }
    )


def get_all_matches():
    start_dt = get_now_paris()
    end_dt_exclusive = start_dt + timedelta(days=DAYS_AHEAD)

    start_date = get_date_str(start_dt)
    end_date_exclusive = get_date_str(end_dt_exclusive)
    display_end_date = get_date_str(end_dt_exclusive - timedelta(days=1))

    all_matches = []

    for code in COMPETITIONS:
        try:
            matches = get_matches_for_competition(code, start_date, end_date_exclusive)
            print(f"Competition {code} -> {len(matches)} matchs")
            all_matches.extend(matches)
        except Exception as e:
            print(f"Erreur competition {code} :", str(e))
            continue

    return start_date, display_end_date, all_matches


def parse_h2h_from_matches(h2h_data, current_home_id, current_away_id, current_home_name, current_away_name):
    matches_list = h2h_data.get("matches", [])
    if not matches_list:
        matches_list = h2h_data.get("fixtures", [])

    home_wins = 0
    away_wins = 0
    draws = 0

    for m in matches_list:
        home_team = m.get("homeTeam", {})
        away_team = m.get("awayTeam", {})

        hist_home_id = home_team.get("id")
        hist_away_id = away_team.get("id")

        if not hist_home_id or not hist_away_id:
            continue

        if {hist_home_id, hist_away_id} != {current_home_id, current_away_id}:
            continue

        score = m.get("score", {})
        winner = score.get("winner")

        if winner == "DRAW":
            draws += 1
            continue

        if winner == "HOME_TEAM":
            winning_team_id = hist_home_id
        elif winner == "AWAY_TEAM":
            winning_team_id = hist_away_id
        else:
            full_time = score.get("fullTime", {})
            home_goals = full_time.get("home")
            if home_goals is None:
                home_goals = full_time.get("homeTeam")
            away_goals = full_time.get("away")
            if away_goals is None:
                away_goals = full_time.get("awayTeam")

            if home_goals is None or away_goals is None:
                continue

            if home_goals > away_goals:
                winning_team_id = hist_home_id
            elif away_goals > home_goals:
                winning_team_id = hist_away_id
            else:
                draws += 1
                continue

        if winning_team_id == current_home_id:
            home_wins += 1
        elif winning_team_id == current_away_id:
            away_wins += 1

    total_h2h = home_wins + away_wins + draws
    h2h_gap = abs(home_wins - away_wins)

    if home_wins > away_wins:
        dominant_team = current_home_name
    elif away_wins > home_wins:
        dominant_team = current_away_name
    else:
        dominant_team = None

    return home_wins, away_wins, draws, total_h2h, h2h_gap, dominant_team


def analyze_match(match):
    competition = match.get("competition", {})
    home = match.get("homeTeam", {})
    away = match.get("awayTeam", {})

    match_id = match.get("id")
    home_id = home.get("id")
    away_id = away.get("id")
    home_name = home.get("name", "Domicile")
    away_name = away.get("name", "Extérieur")

    if not match_id or not home_id or not away_id:
        return None

    h2h_data = get_head2head(match_id)

    home_wins, away_wins, draws, total_h2h, h2h_gap, dominant_team = parse_h2h_from_matches(
        h2h_data, home_id, away_id, home_name, away_name
    )

    print(
        f"H2H recalculé - {home_name} vs {away_name} : "
        f"{home_wins}-{away_wins}, draws={draws}, total={total_h2h}"
    )

    if total_h2h < MIN_H2H_MATCHES:
        return None

    if h2h_gap < MIN_H2H_WIN_GAP:
        return None

    if not dominant_team:
        return None

    return {
        "competition_name": competition.get("name", "Compétition inconnue"),
        "competition_code": competition.get("code", "N/A"),
        "home_name": home_name,
        "away_name": away_name,
        "time": format_match_time(match.get("utcDate")),
        "status": match.get("status", "UNKNOWN"),
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
            f"Aucune affiche ne présente une domination historique assez forte."
        )

    lines = [
        f"📅 Matchs du {start_date} au {end_date}",
        "",
        "⚠️ Affiches avec domination historique nette",
        ""
    ]

    for i, m in enumerate(matches, start=1):
        lines.extend([
            f"{i}. {m['home_name']} vs {m['away_name']}",
            f"🏆 {m['competition_name']} ({m['competition_code']})",
            f"🕒 {m['time']}",
            f"📚 H2H : {m['home_name']} {m['home_wins']} victoires | {m['away_name']} {m['away_wins']} victoires | Nuls {m['draws']}",
            f"🔥 Équipe dominante : {m['dominant_team']}",
            f"📈 Écart H2H : {m['h2h_gap']} victoires sur {m['total_h2h']} confrontations",
            ""
        ])

    lines.append(
        f"Filtres utilisés : au moins {MIN_H2H_MATCHES} confrontations H2H, au moins {MIN_H2H_WIN_GAP} victoires d’écart, H2H borné aux 2 dernières années."
    )
    return "\n".join(lines)


def main():
    if not API_KEY:
        raise RuntimeError("FOOTBALL_DATA_API_KEY manquant")
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL manquant")

    start_date, end_date, all_matches = get_all_matches()

    selected = []
    for match in all_matches:
        try:
            result = analyze_match(match)
            if result:
                selected.append(result)
        except Exception as e:
            print("Erreur analyse match :", str(e))
            continue

    selected.sort(key=lambda x: (-x["h2h_gap"], -x["total_h2h"], x["time"]))
    selected = selected[:MAX_MATCHES_SENT]

    message = build_message(start_date, end_date, selected)
    send_discord_message(message)

    print("Message envoyé sur Discord.")
    print(f"Nombre de matchs retenus : {len(selected)}")


if __name__ == "__main__":
    main()
