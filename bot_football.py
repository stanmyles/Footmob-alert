import os
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

# Maximum de matchs qui recevront une analyse H2H.
MAX_MATCHES = int(os.getenv("MAX_MATCHES", "25"))

# Nombre maximum de confrontations H2H téléchargées par match.
H2H_LIMIT = int(os.getenv("H2H_LIMIT", "10"))

# Seules les confrontations de cette période sont utilisées.
H2H_YEARS_BACK = int(os.getenv("H2H_YEARS_BACK", "3"))

FD_HEADERS = {"X-Auth-Token": FD_TOKEN} if FD_TOKEN else {}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "github-actions-football-h2h/2.0"
})

# Pause prudente entre les appels à football-data.org.
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
                    if error.response else None
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

    # DAYS_AHEAD=2 : aujourd'hui + les deux prochains jours.
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
            f"[WARN] H2H indisponible pour le match "
            f"{match_id} : {error}"
        )

        return []


def get_h2h_signal(total_matches, dominant_wins, dominant_losses):
    """
    Un seul indicateur clair :

    🟢 Avantage H2H net :
    - au moins 6 confrontations
    - au moins 3 victoires d'écart

    🟡 Avantage H2H à surveiller :
    - 3 à 5 confrontations et au moins 2 victoires d'écart
    - ou au moins 6 confrontations et au moins 2 victoires d'écart
    """

    win_gap = dominant_wins - dominant_losses

    if total_matches >= 6 and win_gap >= 3:
        return "🟢 Avantage H2H net"

    if total_matches >= 6 and win_gap >= 2:
        return "🟡 Avantage H2H à surveiller"

    if 3 <= total_matches <= 5 and win_gap >= 2:
        return "🟡 Avantage H2H à surveiller"

    return None


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

        if match_datetime < cutoff_utc:
            continue

        full_time = match.get("score", {}).get("fullTime", {})
        home_goals = full_time.get("home")
        away_goals = full_time.get("away")

        if home_goals is None or away_goals is None:
            continue

        filtered_matches.append(match)

    filtered_matches.sort(
        key=lambda item: item.get("utcDate", ""),
        reverse=True
    )

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

    dominant_team = None
    dominant_wins = 0
    dominant_losses = 0

    if home_wins > away_wins:
        dominant_team = current_home
        dominant_wins = home_wins
        dominant_losses = away_wins

    elif away_wins > home_wins:
        dominant_team = current_away
        dominant_wins = away_wins
        dominant_losses = home_wins

    signal = None

    if dominant_team:
        signal = get_h2h_signal(
            total,
            dominant_wins,
            dominant_losses
        )

    return {
        "h2h_matches": total,
        "home_wins": home_wins,
        "away_wins": away_wins,
        "draws": draws,
        "dominant_team": dominant_team,
        "dominant_wins": dominant_wins,
        "dominant_losses": dominant_losses,
        "signal": signal,
        "recent_h2h": latest_results
    }


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

            if not h2h["signal"]:
                continue

            rows.append({
                "competition": match.get(
                    "_competitionCode",
                    ""
                ),
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
                "h2h_signal": h2h["signal"],
                "recent_h2h": json.dumps(
                    h2h["recent_h2h"],
                    ensure_ascii=False
                )
            })

        except Exception as error:
            print(
                f"[WARN] Analyse impossible pour "
                f"{home_team} vs {away_team} : {error}"
            )

    return rows


def render_discord_message(df):
    if df.empty:
        return (
            "⚠️ Aucun match avec un avantage H2H suffisamment net "
            "sur la période analysée.\n"
            "Critères : 3 à 5 H2H avec 2 victoires d'écart, "
            "ou au moins 6 H2H avec 2 victoires d'écart."
        )

    lines = [
        "📊 **Football — confrontations directes récentes**",
        f"📅 Matchs sélectionnés : {len(df)}",
        "📆 Période : aujourd’hui + les 2 prochains jours."
    ]

    for row in df.head(15).itertuples():
        lines.append("")
        lines.append(
            f"⚽ **{row.homeTeam} vs {row.awayTeam}**"
        )
        lines.append(f"🏆 Compétition : {row.competition}")
        lines.append(f"🗓️ Coup d’envoi : {row.date_local} à {row.time_local}")
        lines.append(
            f"📌 Avantage historique : {row.dominant_team}"
        )
        lines.append(
            f"📊 Historique : {row.dominant_team} "
            f"{row.dominant_wins} victoire(s) | "
            f"{row.draws} nul(s) | "
            f"{row.dominant_losses} défaite(s)"
        )
        lines.append(
            f"📚 Échantillon : {row.h2h_matches} confrontation(s) "
            f"sur les {H2H_YEARS_BACK} dernières années"
        )
        lines.append(row.h2h_signal)

    lines.append("")
    lines.append(
        "ℹ️ Signal fondé uniquement sur les confrontations directes "
        f"des {H2H_YEARS_BACK} dernières années. "
        "Il ne prédit pas un résultat et ne garantit pas une victoire."
    )

    return "\n".join(lines)


def send_discord(message):
    if not DISCORD_WEBHOOK_URL:
        print(
            "[WARN] DISCORD_WEBHOOK_URL absent : "
            "aucun message Discord envoyé."
        )
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

            response.raise_for_status()

            print(
                f"[INFO] Message Discord {index} envoyé "
                f"(HTTP {response.status_code})."
            )

        except RequestException as error:
            print(
                f"[WARN] Envoi Discord {index} impossible : {error}"
            )


def main():
    if not FD_TOKEN:
        raise RuntimeError(
            "Le secret FOOTBALL_DATA_API_TOKEN est manquant."
        )

    competition_codes = get_available_competitions()

    if not competition_codes:
        raise RuntimeError(
            "Aucune compétition disponible avec ce token."
        )

    print(
        f"[INFO] {len(competition_codes)} compétition(s) "
        "accessible(s)."
    )

    matches = get_upcoming_matches(competition_codes)

    print(
        f"[INFO] {len(matches)} match(s) présélectionné(s) "
        f"(maximum : {MAX_MATCHES})."
    )

    rows = build_rows(matches)
    df = pd.DataFrame(rows)

    if not df.empty:
        df = df.sort_values(
            ["utcDate", "competition", "homeTeam"]
        )

    df.to_csv(
        OUTPUT_DIR / "football_h2h_alerts.csv",
        index=False
    )

    df.to_json(
        OUTPUT_DIR / "football_h2h_alerts.json",
        orient="records",
        force_ascii=False,
        indent=2
    )

    message = render_discord_message(df)

    (OUTPUT_DIR / "notification_message.txt").write_text(
        message,
        encoding="utf-8"
    )

    print("\n===== MESSAGE DISCORD =====")
    print(message)

    send_discord(message)


if __name__ == "__main__":
    main()
