import os
import math
import time
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
from dateutil import tz
from requests.exceptions import HTTPError, RequestException


BASE_FD = "https://api.football-data.org/v4"

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

FD_TOKEN = os.getenv("FOOTBALL_DATA_API_TOKEN", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# Aujourd'hui + les 2 prochains jours.
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "2"))

# 25 matchs maximum : compromis entre diversité et limite API.
MAX_MATCHES = int(os.getenv("MAX_MATCHES", "25"))

# Nombre maximal de confrontations directes récupérées.
H2H_LIMIT = int(os.getenv("H2H_LIMIT", "10"))

# Seuls les H2H de cette période sont retenus.
H2H_YEARS_BACK = int(os.getenv("H2H_YEARS_BACK", "3"))

FD_HEADERS = {"X-Auth-Token": FD_TOKEN} if FD_TOKEN else {}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "github-actions-football-h2h/1.0"
})

# Rythme prudent pour respecter les limites de football-data.org.
FD_MIN_INTERVAL = 6.5
_last_fd_call_ts = 0.0


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
    "pae aek": "aek athens",
    "southampton fc": "southampton",
    "swansea city afc": "swansea",
    "mirassol fc": "mirassol",
    "ec vitória": "vitoria",
    "ec vitoria": "vitoria",
    "se palmeiras": "palmeiras",
    "são paulo fc": "sao paulo",
    "sao paulo fc": "sao paulo",
    "botafogo fr": "botafogo",
    "rb bragantino": "bragantino",
    "stade rennais fc 1901": "rennes",
    "stade rennais": "rennes",
    "olympique de marseille": "marseille",
    "west bromwich albion fc": "west bromwich albion",
    "birmingham city fc": "birmingham city",
    "norwich city fc": "norwich city",
    "queens park rangers fc": "queens park rangers",
    "charlton athletic fc": "charlton athletic",
    "sport lisboa e benfica": "benfica",
    "cf estrela da amadora": "estrela amadora",
    "sporting clube de braga": "braga",
    "venezia fc": "venezia",
    "acf fiorentina": "fiorentina"
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
            throttle_football_data()

            response = SESSION.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout
            )

            response.raise_for_status()
            return response.json()

        except HTTPError as error:
            status = error.response.status_code if error.response else None

            if status == 429:
                retry_after = (
                    error.response.headers.get("Retry-After")
                    if error.response
                    else None
                )

                delay = (
                    int(retry_after)
                    if retry_after and retry_after.isdigit()
                    else (attempt + 1) * 10
                )

                print(
                    f"[WARN] Limite football-data atteinte. "
                    f"Nouvel essai dans {delay} seconde(s)."
                )

                time.sleep(delay)
                last_error = error
                continue

            raise

        except RequestException as error:
            last_error = error

            if attempt < retries - 1:
                delay = attempt + 2

                print(
                    f"[WARN] Erreur réseau : {error}. "
                    f"Nouvel essai dans {delay} seconde(s)."
                )

                time.sleep(delay)

    if last_error:
        raise last_error

    raise RuntimeError(f"Requête impossible : {url}")


def normalize_team_name(name):
    normalized = (name or "").lower().strip()

    for word in [
        " football club",
        " futebol clube",
        " calcio",
        " afc",
        " fc",
        " cf",
        " club"
    ]:
        normalized = normalized.replace(word, "")

    normalized = " ".join(normalized.split())

    return ALIASES.get(normalized, normalized)


def utc_to_paris(utc_str):
    utc_datetime = datetime.fromisoformat(
        utc_str.replace("Z", "+00:00")
    )

    paris_datetime = utc_datetime.astimezone(
        tz.gettz("Europe/Paris")
    )

    return (
        paris_datetime.strftime("%d/%m/%Y"),
        paris_datetime.strftime("%H:%M")
    )


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
        probability_home_goals = poisson_pmf(home_goals, home_xg)

        for away_goals in range(max_goals + 1):
            probability_away_goals = poisson_pmf(
                away_goals,
                away_xg
            )

            probability = (
                probability_home_goals
                * probability_away_goals
            )

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

    competitions = [
        competition.get("code")
        for competition in data.get("competitions", [])
        if competition.get("code")
    ]

    competitions = sorted(set(competitions))

    print(
        "[INFO] Compétitions accessibles : "
        + ", ".join(competitions)
    )

    return competitions


def get_upcoming_matches(competition_codes):
    now = datetime.now(timezone.utc)

    date_from = now.date().isoformat()

    # Aujourd'hui + DAYS_AHEAD jours complets.
    date_to = (
        now + timedelta(days=DAYS_AHEAD + 1)
    ).date().isoformat()

    matches = []

    for code in competition_codes:
        try:
            data = request_json(
                f"{BASE_FD}/competitions/{code}/matches",
                headers=FD_HEADERS,
                params={
                    "dateFrom": date_from,
                    "dateTo": date_to
                }
            )

            retained = 0

            for match in data.get("matches", []):
                if match.get("status") not in {"SCHEDULED", "TIMED"}:
                    continue

                match["_competitionCode"] = code
                matches.append(match)
                retained += 1

            print(
                f"[INFO] {code} : {retained} match(s) "
                "programmé(s) trouvé(s)."
            )

        except Exception as error:
            print(
                f"[WARN] Compétition {code} ignorée : {error}"
            )

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
        print(
            f"[WARN] Historique H2H indisponible "
            f"pour le match {match_id} : {error}"
        )

        return []


def get_required_gap(total_h2h_matches):
    # Aucun match avec 0, 1 ou 2 confrontation(s).
    if 3 <= total_h2h_matches <= 5:
        return 2

    if 6 <= total_h2h_matches <= 10:
        return 2

    if total_h2h_matches > 10:
        return 3

    return None


def get_h2h_confidence(
    h2h_matches,
    dominant_wins,
    dominant_losses
):
    if h2h_matches < 3:
        return "FAIBLE"

    win_gap = dominant_wins - dominant_losses

    if h2h_matches >= 6 and win_gap >= 3:
        return "FORTE"

    if h2h_matches >= 4 and win_gap >= 2:
        return "MOYENNE"

    return "FAIBLE"


def summarize_h2h(h2h_matches, current_home, current_away):
    home_wins = 0
    away_wins = 0
    draws = 0
    latest_results = []

    home_normalized = normalize_team_name(current_home)
    away_normalized = normalize_team_name(current_away)

    now_utc = datetime.now(timezone.utc)

    cutoff_utc = now_utc - timedelta(
        days=365 * H2H_YEARS_BACK
    )

    filtered_matches = []

    for match in h2h_matches:
        utc_date = match.get("utcDate")

        if not utc_date:
            continue

        match_datetime = parse_utc_datetime(utc_date)

        if match_datetime <
