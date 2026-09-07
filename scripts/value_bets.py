import os
import math
import time
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

BASE_FD = "https://api.football-data.org/v4"

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

FD_TOKEN = os.getenv("FOOTBALL_DATA_API_TOKEN", "")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

DAYS_AHEAD = int(float(os.getenv("DAYS_AHEAD", "3")))
MAX_MATCHES = int(float(os.getenv("MAX_MATCHES", "25")))
BANKROLL = float(os.getenv("BANKROLL", "1000"))
MIN_EDGE_PCT = float(os.getenv("MIN_EDGE_PCT", "2"))
MIN_EV_PCT = float(os.getenv("MIN_EV_PCT", "1"))

FD_HEADERS = {"X-Auth-Token": FD_TOKEN} if FD_TOKEN else {}
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "github-actions-football-analysis/4.0"})

FD_MIN_INTERVAL = 6.5
_last_fd_call_ts = 0.0

COMPETITIONS = [
    "PL", "PD", "BL1", "SA", "FL1",
    "ELC", "PPL", "DED", "BSA", "CL"
]

ODDS_SPORTS_MAP = {
    "PL": "soccer_epl",
    "PD": "soccer_spain_la_liga",
    "BL1": "soccer_germany_bundesliga",
    "SA": "soccer_italy_serie_a",
    "FL1": "soccer_france_ligue_one",
    "ELC": "soccer_efl_champ",
    "PPL": "soccer_portugal_primeira_liga",
    "DED": "soccer_netherlands_eredivisie",
    "BSA": "soccer_brazil_campeonato",
    "CL": "soccer_uefa_champs_league"
}

ALIASES = {
    "paris saint-germain fc": "paris saint-germain",
    "psg": "paris saint-germain",
    "internazionale": "inter",
    "inter milan": "inter",
    "fc barcelona": "barcelona",
    "real madrid cf": "real madrid",
    "atletico de madrid": "atletico madrid",
    "manchester united fc": "manchester united",
    "manchester city fc": "manchester city",
    "tottenham hotspur fc": "tottenham",
    "newcastle united fc": "newcastle",
    "wolverhampton wanderers fc": "wolves",
    "olympique de marseille": "marseille",
    "olympique lyonnais": "lyon",
    "as monaco fc": "monaco",
    "sporting clube de portugal": "sporting cp"
}


def throttle_football_data():
    global _last_fd_call_ts
    now = time.time()
    wait = FD_MIN_INTERVAL - (now - _last_fd_call_ts)
    if wait > 0:
        time.sleep(wait)
    _last_fd_call_ts = time.time()


def request_json(url, headers=None, params=None, timeout=30, retries=3):
    last_error = None

    for attempt in range(retries):
        try:
            if "api.football-data.org" in url:
                throttle_football_data()

            response = SESSION.get(url, headers=headers, params=params, timeout=timeout)

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                delay = int(retry_after) if retry_after and retry_after.isdigit() else (attempt + 1) * 8
                print(f"[WARN] Limite API atteinte. Nouvelle tentative dans {delay}s.")
                time.sleep(delay)
                continue

            response.raise_for_status()
            return response.json()

        except Exception as error:
            last_error = error
            time.sleep(attempt + 1)

    if last_error is not None:
        raise last_error

    raise RuntimeError(f"Requête impossible : {url}")


def iso_to_api_date(value):
    if not value:
        return None
    return value[:10]


def normalize_team_name(name):
    name = (name or "").lower().strip()
    for word in [" football club", " futebol clube", " calcio", " fc", " cf", " club"]:
        name = name.replace(word, "")
    name = " ".join(name.split())
    return ALIASES.get(name, name)


def poisson_pmf(goals, expected_goals):
    return math.exp(-expected_goals) * (expected_goals ** goals) / math.factorial(goals)


def match_outcome_probabilities(home_xg, away_xg, max_goals=8):
    home_win = 0.0
    draw = 0.0
    away_win = 0.0

    for home_goals in range(max_goals + 1):
        p_home = poisson_pmf(home_goals, home_xg)
        for away_goals in range(max_goals + 1):
            p_away = poisson_pmf(away_goals, away_xg)
            p = p_home * p_away

            if home_goals > away_goals:
                home_win += p
            elif home_goals == away_goals:
                draw += p
            else:
                away_win += p

    total = home_win + draw + away_win
    return home_win / total, draw / total, away_win / total


def get_upcoming_matches():
    now = datetime.now(timezone.utc)
    date_from = now.date().isoformat()
    date_to = (now + timedelta(days=DAYS_AHEAD + 1)).date().isoformat()

    matches = []

    for competition in COMPETITIONS:
        try:
            data = request_json(
                f"{BASE_FD}/competitions/{competition}/matches",
                headers=FD_HEADERS,
                params={"dateFrom": date_from, "dateTo": date_to}
            )

            retained = 0
            for match in data.get("matches", []):
                if match.get("status") in {"SCHEDULED", "TIMED"}:
                    match["_competitionCode"] = competition
                    matches.append(match)
                    retained += 1

            print(f"[INFO] {competition}: {retained} match(s) retenu(s)")

        except Exception as error:
            print(f"[WARN] Compétition {competition} ignorée : {error}")

    matches.sort(key=lambda item: item.get("utcDate", ""))
    return matches[:MAX_MATCHES]


def get_team_recent_matches(team_id, fixture_date, limit=5):
    try:
        api_date = iso_to_api_date(fixture_date)
        data = request_json(
            f"{BASE_FD}/teams/{team_id}/matches",
            headers=FD_HEADERS,
            params={
                "dateTo": api_date,
                "status": "FINISHED",
                "limit": limit
            }
        )
        return data.get("matches", [])
    except Exception as error:
        print(f"[WARN] Forme récente équipe {team_id}: {error}")
        return []


def summarize_form(team_id, matches):
    points = 0
    goals_for = 0
    goals_against = 0
    games = 0

    for match in matches:
        full_time = match.get("score", {}).get("fullTime", {})
        home_goals = full_time.get("home")
        away_goals = full_time.get("away")

        if home_goals is None or away_goals is None:
            continue

        if match["homeTeam"]["id"] == team_id:
            scored = home_goals
            conceded = away_goals
        else:
            scored = away_goals
            conceded = home_goals

        if scored > conceded:
            points += 3
        elif scored == conceded:
            points += 1

        goals_for += scored
        goals_against += conceded
        games += 1

    if games == 0:
        return {"matches": 0, "ppg": 1.20, "gfpg": 1.20, "gapg": 1.20}

    return {
        "matches": games,
        "ppg": points / games,
        "gfpg": goals_for / games,
        "gapg": goals_against / games
    }


def get_h2h(match_id):
    try:
        data = request_json(
            f"{BASE_FD}/matches/{match_id}/head2head",
            headers=FD_HEADERS,
            params={"limit": 5}
        )
        return data.get("matches", [])
    except Exception as error:
        print(f"[WARN] H2H match {match_id}: {error}")
        return []


def summarize_h2h(h2h_matches, current_home, current_away):
    home_wins = 0
    away_wins = 0
    draws = 0
    latest_results = []

    home_normalized = normalize_team_name(current_home)
    away_normalized = normalize_team_name(current_away)

    for match in h2h_matches:
        full_time = match.get("score", {}).get("fullTime", {})
        home_goals = full_time.get("home")
        away_goals = full_time.get("away")

        if home_goals is None or away_goals is None:
            continue

        previous_home = match.get("homeTeam", {}).get("name", "")
        previous_away = match.get("awayTeam", {}).get("name", "")

        latest_results.append({
            "date": match.get("utcDate", "")[:10],
            "home": previous_home,
            "away": previous_away,
            "score": f"{home_goals}-{away_goals}"
        })

        if home_goals == away_goals:
            draws += 1
            continue

        winner = previous_home if home_goals > away_goals else previous_away
        winner_normalized = normalize_team_name(winner)

        if winner_normalized == home_normalized:
            home_wins += 1
        elif winner_normalized == away_normalized:
            away_wins += 1

    total = home_wins + away_wins + draws

    return {
        "h2h_matches": total,
        "h2h_home_wins": home_wins,
        "h2h_away_wins": away_wins,
        "h2h_draws": draws,
        "recent_h2h": latest_results[:5]
    }


def estimate_probabilities(match):
    home = match["homeTeam"]
    away = match["awayTeam"]
    fixture_date = match["utcDate"]

    home_form_matches = get_team_recent_matches(home["id"], fixture_date)
    away_form_matches = get_team_recent_matches(away["id"], fixture_date)

    home_form = summarize_form(home["id"], home_form_matches)
    away_form = summarize_form(away["id"], away_form_matches)
    h2h_summary = summarize_h2h(get_h2h(match["id"]), home["name"], away["name"])

    home_xg = 1.35 + 0.22 * (home_form["ppg"] - 1.30) + 0.18 * (home_form["gfpg"] - away_form["gapg"])
    away_xg = 1.10 + 0.18 * (away_form["ppg"] - 1.30) + 0.16 * (away_form["gfpg"] - home_form["gapg"])

    if h2h_summary["h2h_matches"] > 0:
        h2h_bias = (h2h_summary["h2h_home_wins"] - h2h_summary["h2h_away_wins"]) / h2h_summary["h2h_matches"]
        home_xg += max(-0.15, min(0.15, h2h_bias * 0.15))
        away_xg -= max(-0.12, min(0.12, h2h_bias * 0.12))

    home_xg = max(0.20, min(3.20, home_xg))
    away_xg = max(0.20, min(3.00, away_xg))

    p_home, p_draw, p_away = match_outcome_probabilities(home_xg, away_xg)

    return {
        "home_xg": home_xg,
        "away_xg": away_xg,
        "p_home": p_home,
        "p_draw": p_draw,
        "p_away": p_away,
        "home_form": home_form,
        "away_form": away_form,
        "h2h": h2h_summary
    }


def odds_request_variants(sport_key):
    return [
        {
            "url": f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds",
            "headers": {"x-api-key": ODDS_API_KEY},
            "params": {"regions": "eu", "markets": "h2h", "oddsFormat": "decimal"}
        },
        {
            "url": "https://api.theoddsapi.com/odds",
            "headers": {"x-api-key": ODDS_API_KEY},
            "params": {"sport_key": sport_key, "regions": "eu", "markets": "h2h", "oddsFormat": "decimal"}
        }
    ]


def get_odds(match):
    if not ODDS_API_KEY:
        return {}

    competition = match.get("_competitionCode")
    sport_key = ODDS_SPORTS_MAP.get(competition)

    if not sport_key:
        return {}

    events = None
    last_error = None

    for variant in odds_request_variants(sport_key):
        try:
            events = request_json(
                variant["url"],
                headers=variant["headers"],
                params=variant["params"]
            )
            if isinstance(events, list):
                break
        except Exception as error:
            last_error = error
            continue

    if not isinstance(events, list):
        print(f"[WARN] Cotes indisponibles pour {sport_key}: {last_error}")
        return {}

    wanted_home = normalize_team_name(match["homeTeam"]["name"])
    wanted_away = normalize_team_name(match["awayTeam"]["name"])

    best = {"home": None, "draw": None, "away": None, "bookmaker": None}

    for event in events:
        event_home = normalize_team_name(event.get("home_team", ""))
        event_away = normalize_team_name(event.get("away_team", ""))

        if {event_home, event_away} != {wanted_home, wanted_away}:
            continue

        for bookmaker in event.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market.get("key") != "h2h":
                    continue

                for outcome in market.get("outcomes", []):
                    outcome_name = normalize_team_name(outcome.get("name", ""))
                    price = outcome.get("price")

                    if outcome_name == wanted_home:
                        if best["home"] is None or price > best["home"]:
                            best["home"] = price
                            best["bookmaker"] = bookmaker.get("title")
                    elif outcome_name == wanted_away:
                        if best["away"] is None or price > best["away"]:
                            best["away"] = price
                            best["bookmaker"] = bookmaker.get("title")
                    elif outcome_name in {"draw", "tie", "nul", "match nul"}:
                        if best["draw"] is None or price > best["draw"]:
                            best["draw"] = price
                            best["bookmaker"] = bookmaker.get("title")

        break

    return best


def calculate_selection(probabilities, odds):
    selections = []

    mapping = [
        ("DOMICILE", probabilities["p_home"], odds.get("home")),
        ("NUL", probabilities["p_draw"], odds.get("draw")),
        ("EXTERIEUR", probabilities["p_away"], odds.get("away"))
    ]

    for side, probability, odd in mapping:
        if not odd or odd <= 1:
            continue

        market_probability = 1 / odd
        edge_pct = (probability - market_probability) * 100
        ev_pct = (probability * odd - 1) * 100

        if edge_pct >= MIN_EDGE_PCT and ev_pct >= MIN_EV_PCT:
            selections.append({
                "side": side,
                "probability_pct": round(probability * 100, 2),
                "odds": odd,
                "edge_pct": round(edge_pct, 2),
                "ev_pct": round(ev_pct, 2)
            })

    if not selections:
        return None

    return max(selections, key=lambda item: item["ev_pct"])


def build_rows(matches):
    rows = []
    odds_found = 0

    for index, match in enumerate(matches, start=1):
        home_name = match["homeTeam"]["name"]
        away_name = match["awayTeam"]["name"]

        print(f"[INFO] Analyse {index}/{len(matches)} : {home_name} vs {away_name}")

        try:
            probabilities = estimate_probabilities(match)
            odds = get_odds(match)

            if odds.get("home") and odds.get("draw") and odds.get("away"):
                odds_found += 1

            selection = calculate_selection(probabilities, odds)

            rows.append({
                "utcDate": match["utcDate"],
                "competition": match.get("_competitionCode", ""),
                "status": match.get("status", ""),
                "homeTeam": home_name,
                "awayTeam": away_name,
                "home_xg": round(probabilities["home_xg"], 2),
                "away_xg": round(probabilities["away_xg"], 2),
                "p_home_model": round(probabilities["p_home"] * 100, 2),
                "p_draw_model": round(probabilities["p_draw"] * 100, 2),
                "p_away_model": round(probabilities["p_away"] * 100, 2),
                "home_ppg": round(probabilities["home_form"]["ppg"], 2),
                "away_ppg": round(probabilities["away_form"]["ppg"], 2),
                "h2h_matches": probabilities["h2h"]["h2h_matches"],
                "h2h_home_wins": probabilities["h2h"]["h2h_home_wins"],
                "h2h_draws": probabilities["h2h"]["h2h_draws"],
                "h2h_away_wins": probabilities["h2h"]["h2h_away_wins"],
                "recent_h2h": json.dumps(probabilities["h2h"]["recent_h2h"], ensure_ascii=False),
                "odds_home": odds.get("home"),
                "odds_draw": odds.get("draw"),
                "odds_away": odds.get("away"),
                "bookmaker": odds.get("bookmaker"),
                "recommended_side": selection["side"] if selection else None,
                "recommended_probability_pct": selection["probability_pct"] if selection else None,
                "recommended_odds": selection["odds"] if selection else None,
                "recommended_edge_pct": selection["edge_pct"] if selection else None,
                "recommended_ev_pct": selection["ev_pct"] if selection else None
            })

        except Exception as error:
            print(f"[WARN] Analyse impossible {home_name} vs {away_name}: {error}")

    print(f"[INFO] Matchs avec cotes complètes : {odds_found}/{len(matches)}")
    return rows


def h2h_trend(home_wins, away_wins, total):
    if total == 0:
        return "⚪ Pas d'historique exploitable"
    difference = abs(home_wins - away_wins)
    if difference >= 3:
        return "🔥 Domination nette"
    if difference >= 1:
        return "📈 Léger avantage"
    return "⚖️ Confrontation équilibrée"


def render_discord_message(df):
    if df.empty:
        return "⚠️ Aucun match récupéré sur la fenêtre choisie."

    lines = [
        "📊 *Confrontations directes & Value Bets*",
        f"📅 Matchs analysés : {len(df)}"
    ]

    for row in df.head(10).itertuples():
        total = getattr(row, "h2h_matches", 0) or 0
        home_wins = getattr(row, "h2h_home_wins", 0) or 0
        draws = getattr(row, "h2h_draws", 0) or 0
        away_wins = getattr(row, "h2h_away_wins", 0) or 0

        lines.append("")
        lines.append(f"⚽ *{row.homeTeam} vs {row.awayTeam}*")
        lines.append(f"✅ {row.homeTeam} : {home_wins} victoire(s)")
        lines.append(f"🤝 Nuls : {draws}")
        lines.append(f"❌ {row.awayTeam} : {away_wins} victoire(s)")
        lines.append(f"📅 {total} confrontation(s) analysée(s)")
        lines.append(h2h_trend(home_wins, away_wins, total))

        if getattr(row, "recommended_side", None):
            lines.append(
                f"💸 *Value bet : {row.recommended_side}* | "
                f"cote {row.recommended_odds} | "
                f"EV {row.recommended_ev_pct}% | "
                f"edge {row.recommended_edge_pct}%"
            )

    picks = df[df["recommended_side"].notna()] if "recommended_side" in df.columns else pd.DataFrame()

    lines.append("")
    if picks.empty:
        lines.append("⚠️ Aucun value bet détecté avec les seuils actuels.")
    else:
        lines.append("🎯 *Top value bets*")
        for row in picks.sort_values("recommended_ev_pct", ascending=False).head(5).itertuples():
            lines.append(
                f"• {row.homeTeam} vs {row.awayTeam} → *{row.recommended_side}* "
                f"(cote {row.recommended_odds}, EV {row.recommended_ev_pct}%)"
            )

    return "\n".join(lines)


def send_discord(message):
    if not DISCORD_WEBHOOK_URL:
        print("[WARN] DISCORD_WEBHOOK_URL absent.")
        return

    chunks = [message[i:i + 1900] for i in range(0, len(message), 1900)]

    for idx, chunk in enumerate(chunks[:5], start=1):
        try:
            response = requests.post(
                DISCORD_WEBHOOK_URL,
                json={"content": chunk},
                timeout=20
            )
            print(f"[INFO] Discord message {idx} : HTTP {response.status_code}")
            if response.status_code >= 400:
                print(response.text)
        except Exception as error:
            print(f"[WARN] Discord message {idx} : {error}")


def main():
    if not FD_TOKEN:
        raise RuntimeError("FOOTBALL_DATA_API_TOKEN manquant.")

    matches = get_upcoming_matches()
    print(f"[INFO] {len(matches)} matchs récupérés")

    for match in matches[:10]:
        print(
            f"[MATCH] {match.get('utcDate')} | "
            f"{match.get('_competitionCode')} | "
            f"{match.get('status')} | "
            f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}"
        )

    rows = build_rows(matches)
    df = pd.DataFrame(rows)

    df.to_csv(OUTPUT_DIR / "football_matches.csv", index=False)
    df.to_json(OUTPUT_DIR / "football_matches.json", orient="records", force_ascii=False, indent=2)

    picks = df[df["recommended_side"].notna()] if not df.empty and "recommended_side" in df.columns else pd.DataFrame()
    picks.to_csv(OUTPUT_DIR / "value_bets.csv", index=False)

    message = render_discord_message(df)
    (OUTPUT_DIR / "notification_message.txt").write_text(message, encoding="utf-8")

    print("\n===== MESSAGE DISCORD =====")
    print(message)

    send_discord(message)


if __name__ == "__main__":
    main()
