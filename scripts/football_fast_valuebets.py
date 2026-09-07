import os
import math
import time
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from requests.exceptions import HTTPError, RequestException
from dateutil import tz


BASE_FD = "https://api.football-data.org/v4"
ODDS_BASE = "https://api.theoddsapi.com"

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

FD_TOKEN = os.getenv("FOOTBALL_DATA_API_TOKEN", "").strip()
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

DAYS_AHEAD = int(float(os.getenv("DAYS_AHEAD", "5")))
MAX_MATCHES = int(float(os.getenv("MAX_MATCHES", "15")))
H2H_LIMIT = int(float(os.getenv("H2H_LIMIT", "10")))
H2H_YEARS_BACK = int(float(os.getenv("H2H_YEARS_BACK", "3")))
MIN_EV_PCT = float(os.getenv("MIN_EV_PCT", "2"))

FD_HEADERS = {"X-Auth-Token": FD_TOKEN} if FD_TOKEN else {}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "github-actions-football-fast-valuebets/1.0"
})

# football-data gratuit : environ 10 requêtes/minute.
FD_MIN_INTERVAL = 6.5
_last_fd_call_ts = 0.0

# The Odds API : protection contre les erreurs 429.
ODDS_ENABLED = True
ODDS_MIN_INTERVAL = 2.0
_last_odds_call_ts = 0.0
ODDS_429_STREAK = 0
ODDS_429_MAX_STREAK = 2

# Correspondance entre les codes football-data et The Odds API.
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

# Permet de faire correspondre les noms d'équipes entre les deux API.
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
    "sporting clube de portugal": "sporting cp",
    "club brugge kv": "club brugge",
    "pae aek": "aek athens"
}


def throttle_football_data():
    global _last_fd_call_ts

    now = time.time()
    wait = FD_MIN_INTERVAL - (now - _last_fd_call_ts)

    if wait > 0:
        time.sleep(wait)

    _last_fd_call_ts = time.time()


def throttle_odds_api():
    global _last_odds_call_ts

    now = time.time()
    wait = ODDS_MIN_INTERVAL - (now - _last_odds_call_ts)

    if wait > 0:
        time.sleep(wait)

    _last_odds_call_ts = time.time()


def request_json(url, headers=None, params=None, timeout=30, retries=3):
    global ODDS_ENABLED, ODDS_429_STREAK

    last_error = None
    is_fd = "api.football-data.org" in url
    is_odds = "api.theoddsapi.com" in url

    for attempt in range(retries):
        try:
            if is_fd:
                throttle_football_data()

            elif is_odds:
                if not ODDS_ENABLED:
                    raise RuntimeError("The Odds API est désactivée pour ce run.")

                throttle_odds_api()

            response = SESSION.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout
            )

            response.raise_for_status()

            if is_odds:
                ODDS_429_STREAK = 0

            return response.json()

        except HTTPError as error:
            status = error.response.status_code if error.response is not None else None

            if status == 429:
                if is_fd:
                    retry_after = (
                        error.response.headers.get("Retry-After")
                        if error.response is not None
                        else None
                    )

                    delay = (
                        int(retry_after)
                        if retry_after and retry_after.isdigit()
                        else (attempt + 1) * 8
                    )

                    print(
                        f"[WARN] Limite football-data atteinte. "
                        f"Nouvelle tentative dans {delay}s."
                    )

                    time.sleep(delay)
                    last_error = error
                    continue

                if is_odds:
                    ODDS_429_STREAK += 1
                    delay = 3 + attempt * 2

                    print(
                        f"[WARN] The Odds API limitée "
                        f"({ODDS_429_STREAK}/{ODDS_429_MAX_STREAK}). "
                        f"Attente : {delay}s."
                    )

                    time.sleep(delay)

                    if ODDS_429_STREAK >= ODDS_429_MAX_STREAK:
                        ODDS_ENABLED = False
                        print(
                            "[INFO] The Odds API désactivée "
                            "pour le reste de ce run."
                        )
                        raise RuntimeError(
                            "The Odds API désactivée après plusieurs erreurs 429."
                        )

                    last_error = error
                    continue

            raise

        except RequestException as error:
            last_error = error
            time.sleep(attempt + 1)

    if last_error is not None:
        raise last_error

    raise RuntimeError(f"Requête impossible : {url}")


def normalize_team_name(name):
    name = (name or "").lower().strip()

    for word in [
        " football club",
        " futebol clube",
        " calcio",
        " fc",
        " cf",
        " club"
    ]:
        name = name.replace(word, "")

    name = " ".join(name.split())

    return ALIASES.get(name, name)


def utc_to_paris(utc_str):
    dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    paris = dt.astimezone(tz.gettz("Europe/Paris"))

    return paris.strftime("%Y-%m-%d"), paris.strftime("%H:%M")


def parse_utc_datetime(utc_str):
    return datetime.fromisoformat(utc_str.replace("Z", "+00:00"))


def poisson_pmf(goals, expected_goals):
    return (
        math.exp(-expected_goals)
        * (expected_goals ** goals)
        / math.factorial(goals)
    )


def match_outcome_probabilities(home_xg, away_xg, max_goals=8):
    home_win = 0.0
    draw = 0.0
    away_win = 0.0

    for home_goals in range(max_goals + 1):
        p_home = poisson_pmf(home_goals, home_xg)

        for away_goals in range(max_goals + 1):
            p_away = poisson_pmf(away_goals, away_xg)
            probability = p_home * p_away

            if home_goals > away_goals:
                home_win += probability

            elif home_goals == away_goals:
                draw += probability

            else:
                away_win += probability

    total = home_win + draw + away_win

    return (
        home_win / total,
        draw / total,
        away_win / total
    )


def get_available_competitions():
    data = request_json(
        f"{BASE_FD}/competitions",
        headers=FD_HEADERS
    )

    preferred = []

    for competition in data.get("competitions", []):
        code = competition.get("code")

        if code in ODDS_SPORTS_MAP:
            preferred.append(code)

    return preferred


def get_upcoming_matches(competition_codes):
    now = datetime.now(timezone.utc)

    date_from = now.date().isoformat()
    date_to = (now + timedelta(days=DAYS_AHEAD)).date().isoformat()

    matches = []

    for code in competition_codes:
        if len(matches) >= MAX_MATCHES:
            break

        try:
            data = request_json(
                f"{BASE_FD}/competitions/{code}/matches",
                headers=FD_HEADERS,
                params={
                    "dateFrom": date_from,
                    "dateTo": date_to,
                    "status": "SCHEDULED"
                }
            )

            retained = 0

            for match in data.get("matches", []):
                if match.get("status") in {"SCHEDULED", "TIMED"}:
                    match["_competitionCode"] = code
                    matches.append(match)
                    retained += 1

                    if len(matches) >= MAX_MATCHES:
                        break

            print(f"[INFO] {code}: {retained} match(s) retenu(s).")

        except Exception as error:
            print(f"[WARN] Compétition {code} ignorée : {error}")

    matches.sort(key=lambda item: item.get("utcDate", ""))

    return matches[:MAX_MATCHES]


def get_h2h(match_id):
    try:
        data = request_json(
            f"{BASE_FD}/matches/{match_id}/head2head",
            headers=FD_HEADERS,
            params={"limit": H2H_LIMIT}
        )

        return data.get("matches", [])

    except Exception as error:
        print(f"[WARN] H2H ignoré pour match {match_id} : {error}")
        return []


def get_required_gap(total_h2h_matches):
    if 3 <= total_h2h_matches <= 5:
        return 2
    if 6 <= total_h2h_matches <= 10:
        return 3
    return None


def summarize_h2h(h2h_matches, current_home, current_away):
    home_wins = 0
    away_wins = 0
    draws = 0
    latest_results = []

    home_normalized = normalize_team_name(current_home)
    away_normalized = normalize_team_name(current_away)

    now_utc = datetime.now(timezone.utc)
    cutoff_utc = now_utc - timedelta(days=365 * H2H_YEARS_BACK)

    filtered_matches = []

    for match in h2h_matches:
        utc_date = match.get("utcDate")
        if not utc_date:
            continue

        match_dt = parse_utc_datetime(utc_date)
        if match_dt < cutoff_utc:
            continue

        full_time = match.get("score", {}).get("fullTime", {})
        home_goals = full_time.get("home")
        away_goals = full_time.get("away")

        if home_goals is None or away_goals is None:
            continue

        filtered_matches.append(match)

    filtered_matches.sort(key=lambda m: m.get("utcDate", ""), reverse=True)
    filtered_matches = filtered_matches[:H2H_LIMIT]

    for match in filtered_matches:
        full_time = match.get("score", {}).get("fullTime", {})
        home_goals = full_time.get("home")
        away_goals = full_time.get("away")

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
    required_gap = get_required_gap(total)

    dominant_team = None
    dominant_wins = 0
    dominant_losses = 0

    if required_gap is not None:
        if (home_wins - away_wins) >= required_gap:
            dominant_team = current_home
            dominant_wins = home_wins
            dominant_losses = away_wins

        elif (away_wins - home_wins) >= required_gap:
            dominant_team = current_away
            dominant_wins = away_wins
            dominant_losses = home_wins

    return {
        "h2h_matches": total,
        "home_wins": home_wins,
        "away_wins": away_wins,
        "draws": draws,
        "dominant_team": dominant_team,
        "dominant_wins": dominant_wins,
        "dominant_losses": dominant_losses,
        "required_gap": required_gap,
        "recent_h2h": latest_results
    }


def estimate_probabilities_fast(h2h_summary):
    home_xg = 1.35
    away_xg = 1.10

    if h2h_summary["h2h_matches"] > 0:
        h2h_bias = (
            h2h_summary["home_wins"] - h2h_summary["away_wins"]
        ) / h2h_summary["h2h_matches"]

        home_xg += max(-0.25, min(0.25, h2h_bias * 0.22))
        away_xg -= max(-0.20, min(0.20, h2h_bias * 0.18))

    home_xg = max(0.20, min(3.00, home_xg))
    away_xg = max(0.20, min(2.80, away_xg))

    p_home, p_draw, p_away = match_outcome_probabilities(
        home_xg,
        away_xg
    )

    return {
        "home_xg": home_xg,
        "away_xg": away_xg,
        "p_home": p_home,
        "p_draw": p_draw,
        "p_away": p_away
    }


def get_odds(match):
    if not ODDS_API_KEY or not ODDS_ENABLED:
        return {}

    competition_code = match.get("_competitionCode")
    sport_key = ODDS_SPORTS_MAP.get(competition_code)

    if not sport_key:
        return {}

    try:
        events = request_json(
            f"{ODDS_BASE}/odds/",
            headers={"x-api-key": ODDS_API_KEY},
            params={
                "sport_key": sport_key,
                "regions": "eu",
                "markets": "h2h",
                "oddsFormat": "decimal"
            },
            retries=2
        )

    except Exception as error:
        print(
            f"[WARN] Cotes ignorées pour "
            f"{match['homeTeam']['name']} vs {match['awayTeam']['name']} : {error}"
        )
        return {}

    if not isinstance(events, list):
        return {}

    wanted_home = normalize_team_name(match["homeTeam"]["name"])
    wanted_away = normalize_team_name(match["awayTeam"]["name"])

    best = {
        "home": None,
        "draw": None,
        "away": None,
        "bookmaker": None
    }

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
                    name = normalize_team_name(outcome.get("name", ""))
                    price = outcome.get("price")

                    if not price:
                        continue

                    if name == wanted_home:
                        if best["home"] is None or price > best["home"]:
                            best["home"] = price
                            best["bookmaker"] = bookmaker.get("title")

                    elif name == wanted_away:
                        if best["away"] is None or price > best["away"]:
                            best["away"] = price
                            best["bookmaker"] = bookmaker.get("title")

                    elif name in {"draw", "tie", "nul", "match nul"}:
                        if best["draw"] is None or price > best["draw"]:
                            best["draw"] = price
                            best["bookmaker"] = bookmaker.get("title")

        break

    return best


def calculate_value_bet(probabilities, odds):
    selections = [
        ("DOMICILE", probabilities["p_home"], odds.get("home")),
        ("NUL", probabilities["p_draw"], odds.get("draw")),
        ("EXTERIEUR", probabilities["p_away"], odds.get("away"))
    ]

    candidates = []

    for side, probability, odd in selections:
        if not odd or odd <= 1:
            continue

        market_probability = 1 / odd
        edge_pct = (probability - market_probability) * 100
        ev_pct = (probability * odd - 1) * 100

        if ev_pct >= MIN_EV_PCT:
            candidates.append({
                "side": side,
                "probability_pct": round(probability * 100, 2),
                "odds": round(odd, 2),
                "edge_pct": round(edge_pct, 2),
                "ev_pct": round(ev_pct, 2)
            })

    if not candidates:
        return None

    return max(candidates, key=lambda item: item["ev_pct"])


def build_rows(matches):
    rows = []

    for index, match in enumerate(matches, start=1):
        home_team = match["homeTeam"]["name"]
        away_team = match["awayTeam"]["name"]

        print(
            f"[INFO] Analyse {index}/{len(matches)} : "
            f"{home_team} vs {away_team}"
        )

        try:
            date_local, time_local = utc_to_paris(match["utcDate"])

            h2h = summarize_h2h(
                get_h2h(match["id"]),
                home_team,
                away_team
            )

            if not h2h["dominant_team"]:
                continue

            probabilities = estimate_probabilities_fast(h2h)
            odds = get_odds(match)

            value_bet = (
                calculate_value_bet(probabilities, odds)
                if odds
                else None
            )

            rows.append({
                "competition": match.get("_competitionCode", ""),
                "utcDate": match["utcDate"],
                "date_local": date_local,
                "time_local": time_local,
                "homeTeam": home_team,
                "awayTeam": away_team,
                "dominant_team": h2h["dominant_team"],
                "h2h_matches": h2h["h2h_matches"],
                "dominant_wins": h2h["dominant_wins"],
                "draws": h2h["draws"],
                "dominant_losses": h2h["dominant_losses"],
                "required_gap": h2h["required_gap"],
                "recent_h2h": json.dumps(
                    h2h["recent_h2h"],
                    ensure_ascii=False
                ),
                "p_home_model": round(probabilities["p_home"] * 100, 2),
                "p_draw_model": round(probabilities["p_draw"] * 100, 2),
                "p_away_model": round(probabilities["p_away"] * 100, 2),
                "odds_home": odds.get("home"),
                "odds_draw": odds.get("draw"),
                "odds_away": odds.get("away"),
                "bookmaker": odds.get("bookmaker"),
                "value_bet_side": (
                    value_bet["side"] if value_bet else None
                ),
                "value_bet_probability_pct": (
                    value_bet["probability_pct"] if value_bet else None
                ),
                "value_bet_odds": (
                    value_bet["odds"] if value_bet else None
                ),
                "value_bet_edge_pct": (
                    value_bet["edge_pct"] if value_bet else None
                ),
                "value_bet_ev_pct": (
                    value_bet["ev_pct"] if value_bet else None
                )
            })

        except Exception as error:
            print(
                f"[WARN] Analyse impossible "
                f"{home_team} vs {away_team} : {error}"
            )

    return rows


def render_discord_message(df):
    if df.empty:
        return (
            "⚠️ Aucun match avec domination H2H forte trouvé sur la période.\n"
            "Règle actuelle : H2H des 3 dernières années uniquement, "
            "3 à 5 matchs = 2 victoires d'écart minimum, "
            "6 à 10 matchs = 3 victoires d'écart minimum."
        )

    lines = [
        "📊 *Football rapide : H2H dominants + value bets*",
        f"📅 Matchs retenus : {len(df)}"
    ]

    for row in df.head(10).itertuples():
        lines.append("")
        lines.append(f"⚽ *{row.homeTeam} vs {row.awayTeam}*")
        lines.append(f"🗓️ {row.date_local} à {row.time_local}")
        lines.append(f"👑 Équipe dominante : {row.dominant_team}")
        lines.append(
            f"📚 H2H : {row.dominant_wins}V | "
            f"{row.draws}N | {row.dominant_losses}D "
            f"(écart requis : {row.required_gap})"
        )
        lines.append(
            f"📈 Modèle : {row.homeTeam} {row.p_home_model}% | "
            f"Nul {row.p_draw_model}% | "
            f"{row.awayTeam} {row.p_away_model}%"
        )

        if pd.notna(row.value_bet_side):
            lines.append(
                f"💸 Value bet : {row.value_bet_side} | "
                f"Cote {row.value_bet_odds} | "
                f"EV {row.value_bet_ev_pct}% | "
                f"Bookmaker : {row.bookmaker}"
            )
        else:
            lines.append("ℹ️ Pas de value bet détectée ou cote indisponible.")

    return "\n".join(lines)


def send_discord(message):
    if not DISCORD_WEBHOOK_URL:
        print("[WARN] DISCORD_WEBHOOK_URL absent.")
        return

    chunks = [
        message[index:index + 1900]
        for index in range(0, len(message), 1900)
    ]

    for index, chunk in enumerate(chunks[:5], start=1):
        try:
            response = requests.post(
                DISCORD_WEBHOOK_URL,
                json={"content": chunk},
                timeout=20
            )

            print(
                f"[INFO] Message Discord {index} : "
                f"HTTP {response.status_code}"
            )

            if response.status_code >= 400:
                print(response.text[:500])

        except Exception as error:
            print(f"[WARN] Discord message {index} : {error}")


def main():
    if not FD_TOKEN:
        raise RuntimeError("FOOTBALL_DATA_API_TOKEN manquant.")

    competition_codes = get_available_competitions()

    print(
        f"[INFO] {len(competition_codes)} compétitions "
        "exploitables détectées."
    )

    matches = get_upcoming_matches(competition_codes)

    print(f"[INFO] {len(matches)} matchs présélectionnés.")

    rows = build_rows(matches)
    df = pd.DataFrame(rows)

    if not df.empty:
        df = df.sort_values(
            ["date_local", "time_local", "competition", "homeTeam"]
        )

    df.to_csv(
        OUTPUT_DIR / "football_fast_valuebets.csv",
        index=False
    )

    df.to_json(
        OUTPUT_DIR / "football_fast_valuebets.json",
        orient="records",
        force_ascii=False,
        indent=2
    )

    message = render_discord_message(df)

    (
        OUTPUT_DIR / "notification_message.txt"
    ).write_text(
        message,
        encoding="utf-8"
    )

    print("\n===== MESSAGE DISCORD =====")
    print(message)

    send_discord(message)


if __name__ == "__main__":
    main()
