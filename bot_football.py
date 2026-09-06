import os
import datetime
import requests

API_KEY = os.getenv("RAPIDAPI_KEY", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

HEADERS = {
    "x-rapidapi-host": "api-football-v1.p.rapidapi.com",
    "x-rapidapi-key": API_KEY
}

BASE_URL = "https://api-football-v1.p.rapidapi.com/v3"

MIN_H2H_MATCHES = 5
MIN_H2H_GAP = 3
MIN_RANK_GAP = 8


def get_matches_today():
    today = datetime.date.today().isoformat()
    url = f"{BASE_URL}/fixtures"
    response = requests.get(url, headers=HEADERS, params={"date": today}, timeout=30)
    response.raise_for_status()
    data = response.json()
    return data.get("response", [])


def check_h2h_last_2_years(team1_id, team2_id):
    url = f"{BASE_URL}/fixtures/headtohead"
    response = requests.get(
        url,
        headers=HEADERS,
        params={"h2h": f"{team1_id}-{team2_id}"},
        timeout=30
    )
    response.raise_for_status()
    data = response.json()
    fixtures = data.get("response", [])

    two_years_ago = datetime.datetime.now() - datetime.timedelta(days=2 * 365)

    t1_wins = 0
    t2_wins = 0
    total_valid = 0

    for f in fixtures:
        fixture_date = f["fixture"]["date"]
        f_date = datetime.datetime.fromisoformat(fixture_date.replace("Z", "+00:00")).replace(tzinfo=None)

        if f_date < two_years_ago:
            continue

        home_id = f["teams"]["home"]["id"]
        away_id = f["teams"]["away"]["id"]
        home_goals = f["goals"]["home"]
        away_goals = f["goals"]["away"]

        if home_goals is None or away_goals is None:
            continue

        total_valid += 1

        if home_id == team1_id:
            if home_goals > away_goals:
                t1_wins += 1
            elif home_goals < away_goals:
                t2_wins += 1
        elif away_id == team1_id:
            if away_goals > home_goals:
                t1_wins += 1
            elif away_goals < home_goals:
                t2_wins += 1

    return t1_wins, t2_wins, total_valid


def get_team_standing(league_id, season, team_id):
    url = f"{BASE_URL}/standings"
    response = requests.get(
        url,
        headers=HEADERS,
        params={"league": league_id, "season": season},
        timeout=30
    )
    response.raise_for_status()
    data = response.json()

    try:
        standings_groups = data["response"][0]["league"]["standings"]
        for group in standings_groups:
            for row in group:
                if row["team"]["id"] == team_id:
                    return row["rank"]
    except (IndexError, KeyError, TypeError):
        return None

    return None


def send_discord(message):
    if not DISCORD_WEBHOOK_URL:
        raise ValueError("DISCORD_WEBHOOK_URL est vide.")

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={"content": message[:2000]},
        timeout=20
    )
    print("Discord status:", response.status_code)
    response.raise_for_status()


def main():
    if not API_KEY:
        raise ValueError("RAPIDAPI_KEY est vide.")

    matches = get_matches_today()
    alerts = []

    for match in matches:
        league_id = match["league"]["id"]
        season = match["league"]["season"]

        t1_id = match["teams"]["home"]["id"]
        t1_name = match["teams"]["home"]["name"]
        t2_id = match["teams"]["away"]["id"]
        t2_name = match["teams"]["away"]["name"]

        t1_wins, t2_wins, h2h_total = check_h2h_last_2_years(t1_id, t2_id)
        h2h_gap = abs(t1_wins - t2_wins)

        rank1 = get_team_standing(league_id, season, t1_id)
        rank2 = get_team_standing(league_id, season, t2_id)

        if rank1 is None or rank2 is None:
            continue

        rank_gap = abs(rank1 - rank2)

        if h2h_total >= MIN_H2H_MATCHES and h2h_gap >= MIN_H2H_GAP and rank_gap >= MIN_RANK_GAP:
            if t1_wins > t2_wins:
                dominant = t1_name
            elif t2_wins > t1_wins:
                dominant = t2_name
            else:
                dominant = "Aucune"

            alerts.append(
                f"⚽ {t1_name} vs {t2_name}\n"
                f"🏆 Dominance H2H : {dominant}\n"
                f"📊 H2H sur 2 ans : {t1_name} {t1_wins} - {t2_wins} {t2_name} "
                f"(écart {h2h_gap}, matchs {h2h_total})\n"
                f"📈 Classement : {t1_name} #{rank1} - #{rank2} {t2_name} "
                f"(écart {rank_gap})"
            )

    if alerts:
        message = "🚨 Matchs détectés aujourd'hui :\n\n" + "\n\n".join(alerts[:8])
    else:
        message = "ℹ️ Aucun match aujourd'hui ne respecte tes critères H2H + classement."

    send_discord(message)


if __name__ == "__main__":
    main()
