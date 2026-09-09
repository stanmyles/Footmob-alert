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

DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "2"))
MAX_MATCHES = int(os.getenv("MAX_MATCHES", "25"))
H2H_LIMIT = int(os.getenv("H2H_LIMIT", "10"))
H2H_YEARS_BACK = int(os.getenv("H2H_YEARS_BACK", "3"))

FD_HEADERS = {"X-Auth-Token": FD_TOKEN} if FD_TOKEN else {}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "github-actions-football-h2h/1.0"
})

# Plan gratuit football-data.org : garder un rythme prudent.
FD_MIN_INTERVAL = 6.5
_last_fd_call_ts = 0.0

# Corrige les différences de noms entre les endpoints de l'API.
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
    "rb bragantino": "bragantino"
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
                    f"[WARN] Limite API atteinte. "
                    f"Nouvel essai dans {delay} secondes."
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
                    f"Nouvel essai dans {delay} secondes."
                )
                time.sleep(delay)

    if last_error:
        raise last_error

    raise RuntimeError(f"Requête impossible : {url}")


def normalize_team_name(name):
    name = (name or "").lower().strip()

    for word in [
        " football club",
        " futebol clube",
        " calcio",
        " afc",
        " fc",
        " cf",
        " club"
    ]:
        name = name.replace(word, "")

    name = " ".join(name.split())
    return ALIASES.get(name, name)


def utc_to_paris(utc_str):
    dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    paris_time = dt.astimezone(tz.gettz("Europe/Paris"))

    return (
        paris_time.strftime("%d/%m/%Y"),
        paris_time.strftime("%H:%M")
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

    # DAYS_AHEAD=2 = aujourd'hui + les 2 prochains jours.
    date_to = (now + timedelta(days=DAYS_AHEAD + 1)).date().isoformat()

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
                f"[INFO] {code} : "
                f"{retained} match(s) programmé(s) trouvé(s)."
            )

        except Exception as error:
            print(f"[WARN] Compétition {code} ignorée : {error}")

    matches.sort(key=lambda item: item.get("utcDate", ""))

    # On répartit les matchs dans le temps avant de limiter.
    # Cela évite que le premier championnat interrogé prenne toutes les places.
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
        print(f"[WARN] H2H indisponible pour le match {match_id} : {error}")
        return []


def get_required_gap(total_h2h_matches):
    # Version volontairement plus souple pour obtenir davantage de résultats.
    if 2 <= total_h2h_matches <= 5:
        return 1

    if 6 <= total_h2h_matches <= 10:
        return 2

    if total_h2h_matches > 10:
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

    filtered_matches.sort(
        key=lambda match: match.get("utcDate", ""),
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
    required_gap = get_required_gap(total)

    dominant_team = None
    dominant_wins = 0
    dominant_losses = 0

    if required_gap is not None:
        if home_wins - away_wins >= required_gap:
            dominant_team = current_home
            dominant_wins = home_wins
            dominant_losses = away_wins

        elif away_wins - home_wins >= required_gap:
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
            h2h_summary["home_wins"]
            - h2h_summary["away_wins"]
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
                "p_away_model": round(probabilities["p_away"] * 100, 2)
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
            "⚠️ Aucun match avec tendance H2H nette trouvé "
            "sur la période analysée.\n"
            "Règle : 2 à 5 H2H = 1 victoire d'écart ; "
            "6 à 10 H2H = 2 victoires d'écart."
        )

    lines = [
        "📊 **Football : tendances H2H**",
        f"📅 Matchs retenus : {len(df)}",
        "ℹ️ Fenêtre : aujourd’hui + les 2 prochains jours."
    ]

    for row in df.head(15).itertuples():
        lines.append("")
        lines.append(f"⚽ **{row.homeTeam} vs {row.awayTeam}**")
        lines.append(f"🏆 Compétition : {row.competition}")
        lines.append(f"🗓️ {row.date_local} à {row.time_local}")
        lines.append(f"👑 Tendance H2H : {row.dominant_team}")
        lines.append(
            f"📚 Bilan : {row.dominant_wins}V | "
            f"{row.draws}N | {row.dominant_losses}D "
            f"sur {row.h2h_matches} confrontation(s)"
        )
        lines.append(
            f"📈 Modèle indicatif : "
            f"{row.homeTeam} {row.p_home_model}% | "
            f"Nul {row.p_draw_model}% | "
            f"{row.awayTeam} {row.p_away_model}%"
        )

    lines.append("")
    lines.append(
        "⚠️ Information statistique : le H2H seul ne garantit pas "
        "un résultat et ne constitue pas un conseil de pari."
    )

    return "\n".join(lines)


def send_discord(message):
    if not DISCORD_WEBHOOK_URL:
        print("[WARN] DISCORD_WEBHOOK_URL absent : aucun message envoyé.")
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
        f"[INFO] {len(competition_codes)} compétition(s) accessible(s)."
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
