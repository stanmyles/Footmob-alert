import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from requests.exceptions import HTTPError, RequestException


BASE_FD = "https://api.football-data.org/v4"
PARIS_TZ = ZoneInfo("Europe/Paris")

FD_TOKEN = os.getenv("FOOTBALL_DATA_API_TOKEN", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "1"))
LOOKBACK_MATCHES = int(os.getenv("LOOKBACK_MATCHES", "10"))
MIN_COMPARABLE_MATCHES = int(
    os.getenv("MIN_COMPARABLE_MATCHES", "5")
)

MIN_SCORE = int(os.getenv("MIN_SCORE", "60"))
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "6"))
MAX_WATCHLIST = int(os.getenv("MAX_WATCHLIST", "4"))

TARGET_DATE = os.getenv("TARGET_DATE", "").strip()
FD_MIN_INTERVAL = float(os.getenv("FD_MIN_INTERVAL", "6.5"))

last_api_call = 0.0

session = requests.Session()
session.headers.update({
    "User-Agent": "github-actions-football-12-btts/1.0",
    "Accept": "application/json",
})


def throttle():
    global last_api_call

    now = time.time()
    wait = FD_MIN_INTERVAL - (now - last_api_call)

    if wait > 0:
        time.sleep(wait)

    last_api_call = time.time()


def api_get(endpoint, params=None, retries=3):
    url = f"{BASE_FD}{endpoint}"
    last_error = None

    for attempt in range(retries):
        try:
            throttle()

            response = session.get(
                url,
                headers={
                    "X-Auth-Token": FD_TOKEN
                },
                params=params or {},
                timeout=30
            )

            response.raise_for_status()
            return response.json()

        except HTTPError as error:
            status = (
                error.response.status_code
                if error.response is not None
                else None
            )

            if status == 429 and attempt < retries - 1:
                retry_after = error.response.headers.get(
                    "Retry-After",
                    ""
                )

                delay = (
                    int(retry_after)
                    if retry_after.isdigit()
                    else (attempt + 1) * 20
                )

                print(
                    f"[WARN] Limite API atteinte. "
                    f"Nouvel essai dans {delay} seconde(s)."
                )

                time.sleep(delay)
                last_error = error
                continue

            raise

        except RequestException as error:
            last_error = error

            if attempt < retries - 1:
                delay = (attempt + 1) * 5

                print(
                    f"[WARN] Erreur réseau : {error}. "
                    f"Nouvel essai dans {delay} seconde(s)."
                )

                time.sleep(delay)

    if last_error:
        raise last_error

    raise RuntimeError(f"Requête impossible : {url}")


def get_target_dates():
    if TARGET_DATE:
        try:
            return [
                datetime.strptime(
                    TARGET_DATE,
                    "%Y-%m-%d"
                ).date()
            ]

        except ValueError as error:
            raise ValueError(
                "TARGET_DATE doit être au format YYYY-MM-DD."
            ) from error

    today = datetime.now(PARIS_TZ).date()

    return [
        today + timedelta(days=day)
        for day in range(DAYS_AHEAD + 1)
    ]


def get_available_competitions():
    data = api_get("/competitions")

    codes = sorted({
        competition.get("code")
        for competition in data.get("competitions", [])
        if competition.get("code")
    })

    print(
        "[INFO] Compétitions accessibles : "
        + (", ".join(codes) if codes else "aucune")
    )

    return codes


def get_upcoming_matches(competition_codes, dates):
    date_from = min(dates).isoformat()
    date_to = max(dates).isoformat()

    matches = []
    seen_match_ids = set()

    for competition_code in competition_codes:
        try:
            data = api_get(
                f"/competitions/{competition_code}/matches",
                params={
                    "dateFrom": date_from,
                    "dateTo": date_to,
                }
            )

            count = 0

            for match in data.get("matches", []):
                if match.get("status") not in {
                    "SCHEDULED",
                    "TIMED"
                }:
                    continue

                match_id = match.get("id")

                if not match_id or match_id in seen_match_ids:
                    continue

                match["_competition_code"] = competition_code

                matches.append(match)
                seen_match_ids.add(match_id)
                count += 1

            print(
                f"[INFO] {competition_code} : "
                f"{count} match(s) à venir."
            )

        except Exception as error:
            print(
                f"[WARN] Compétition {competition_code} ignorée : "
                f"{error}"
            )

    matches.sort(
        key=lambda match: match.get("utcDate", "")
    )

    return matches


def get_team_finished_matches(team_id, venue):
    data = api_get(
        f"/teams/{team_id}/matches",
        params={
            "status": "FINISHED",
            "venue": venue,
            "limit": LOOKBACK_MATCHES,
        }
    )

    matches = data.get("matches", [])

    print(
        f"[INFO] Équipe {team_id} — {venue} : "
        f"{len(matches)} match(s) récupéré(s)."
    )

    return matches


def get_full_time_goals(match):
    full_time = match.get("score", {}).get("fullTime", {})

    home_goals = full_time.get("home")
    away_goals = full_time.get("away")

    if home_goals is None or away_goals is None:
        return None, None

    return home_goals, away_goals


def team_stats(matches, team_id):
    rows = []

    for match in matches:
        home_goals, away_goals = get_full_time_goals(match)

        if home_goals is None or away_goals is None:
            continue

        home_team_id = match.get("homeTeam", {}).get("id")
        away_team_id = match.get("awayTeam", {}).get("id")

        if team_id not in {home_team_id, away_team_id}:
            continue

        is_home = home_team_id == team_id

        goals_for = home_goals if is_home else away_goals
        goals_against = away_goals if is_home else home_goals

        rows.append({
            "btts": home_goals > 0 and away_goals > 0,
            "scored": goals_for > 0,
            "conceded": goals_against > 0,
            "win": goals_for > goals_against,
            "draw": goals_for == goals_against,
            "loss": goals_for < goals_against,
        })

    total = len(rows)

    if total == 0:
        return None

    def pct(field):
        return round(
            100 * sum(row[field] for row in rows) / total,
            1
        )

    return {
        "matches": total,
        "btts_pct": pct("btts"),
        "scored_pct": pct("scored"),
        "conceded_pct": pct("conceded"),
        "win_pct": pct("win"),
        "draw_pct": pct("draw"),
        "loss_pct": pct("loss"),
    }


def calculate_12_btts_score(home_stats, away_stats):
    score = 0
    signals = []

    if home_stats["btts_pct"] >= 60:
        score += 25
        signals.append(
            f"BTTS domicile {home_stats['btts_pct']} %"
        )

    if away_stats["btts_pct"] >= 50:
        score += 20
        signals.append(
            f"BTTS extérieur {away_stats['btts_pct']} %"
        )

    if home_stats["scored_pct"] >= 70:
        score += 20
        signals.append(
            f"domicile marque {home_stats['scored_pct']} %"
        )

    if away_stats["scored_pct"] >= 70:
        score += 20
        signals.append(
            f"extérieur marque {away_stats['scored_pct']} %"
        )

    average_draw_pct = (
        home_stats["draw_pct"] + away_stats["draw_pct"]
    ) / 2

    if average_draw_pct <= 30:
        score += 15
        signals.append(
            f"nuls moyens {average_draw_pct:.1f} %"
        )

    return score, signals, round(average_draw_pct, 1)


def to_paris_time(utc_date):
    utc_datetime = datetime.fromisoformat(
        utc_date.replace("Z", "+00:00")
    )

    return utc_datetime.astimezone(PARIS_TZ)


def analyze_match(match, cache):
    home_team = match.get("homeTeam", {})
    away_team = match.get("awayTeam", {})

    home_id = home_team.get("id")
    away_id = away_team.get("id")

    if not home_id or not away_id:
        return None

    home_cache_key = f"{home_id}:HOME"
    away_cache_key = f"{away_id}:AWAY"

    if home_cache_key not in cache:
        cache[home_cache_key] = get_team_finished_matches(
            home_id,
            "HOME"
        )

    if away_cache_key not in cache:
        cache[away_cache_key] = get_team_finished_matches(
            away_id,
            "AWAY"
        )

    home_stats = team_stats(
        cache[home_cache_key],
        home_id
    )

    away_stats = team_stats(
        cache[away_cache_key],
        away_id
    )

    if not home_stats or not away_stats:
        return None

    if (
        home_stats["matches"] < MIN_COMPARABLE_MATCHES
        or away_stats["matches"] < MIN_COMPARABLE_MATCHES
    ):
        print(
            "[INFO] Pas assez de données comparables : "
            f"{home_team.get('name', 'Domicile')} / "
            f"{away_team.get('name', 'Extérieur')}."
        )
        return None

    score, signals, average_draw_pct = calculate_12_btts_score(
        home_stats,
        away_stats
    )

    return {
        "home": home_team.get("name", "Équipe domicile"),
        "away": away_team.get("name", "Équipe extérieure"),
        "competition": match.get(
            "competition",
            {}
        ).get(
            "name",
            match.get("_competition_code", "Compétition")
        ),
        "kickoff": to_paris_time(match["utcDate"]),
        "score": score,
        "signals": signals,
        "average_draw_pct": average_draw_pct,
        "home_stats": home_stats,
        "away_stats": away_stats,
        "qualified": score >= MIN_SCORE,
    }


def short_stats(label, stats):
    return (
        f"**{label}** ({stats['matches']} matchs) — "
        f"V {stats['win_pct']} % | "
        f"N {stats['draw_pct']} % | "
        f"D {stats['loss_pct']} % | "
        f"BTTS {stats['btts_pct']} % | "
        f"marque {stats['scored_pct']} % | "
        f"encaisse {stats['conceded_pct']} %"
    )


def candidate_lines(index, candidate, prefix=""):
    signals = " • ".join(candidate["signals"])

    return [
        (
            f"**{prefix}{index}. "
            f"{candidate['home']} vs {candidate['away']} — "
            f"Score {candidate['score']}/100**"
        ),
        "🎯 12 + BTTS Oui — une équipe gagne et les deux équipes marquent",
        (
            f"🏆 {candidate['competition']} | "
            f"🕒 {candidate['kickoff'].strftime('%d/%m %H:%M')} "
            "heure Paris"
        ),
        (
            f"📊 Risque de nul moyen : "
            f"{candidate['average_draw_pct']} %"
        ),
        short_stats("Domicile", candidate["home_stats"]),
        short_stats("Extérieur", candidate["away_stats"]),
        f"📌 Signaux : {signals}",
        "",
    ]


def build_discord_message(candidates, watchlist, dates):
    first_date = min(dates).strftime("%d/%m/%Y")
    last_date = max(dates).strftime("%d/%m/%Y")

    date_text = (
        first_date
        if first_date == last_date
        else f"{first_date} au {last_date}"
    )

    lines = [
        "📊 **Alerte Football — 12 + BTTS Oui**",
        f"📅 Période analysée : {date_text}",
        (
            "🎯 Marché : une équipe gagne "
            "(pas de nul) + les deux équipes marquent."
        ),
        f"✅ Seuil des profils retenus : {MIN_SCORE}/100",
        "",
    ]

    if candidates:
        lines.extend([
            "✅ **Profils retenus**",
            "",
        ])

        for index, candidate in enumerate(candidates, start=1):
            lines.extend(
                candidate_lines(index, candidate)
            )

    else:
        lines.extend([
            "⚠️ **Aucun match ne dépasse le seuil principal.**",
            "Les meilleurs matchs sous le seuil sont affichés ci-dessous.",
            "",
        ])

    if watchlist:
        lines.extend([
            "👀 **Meilleurs matchs à surveiller — non validés**",
            (
                "Ces profils sont sous le seuil et ne constituent "
                "pas une sélection validée."
            ),
            "",
        ])

        for index, candidate in enumerate(watchlist, start=1):
            lines.extend(
                candidate_lines(
                    index,
                    candidate,
                    prefix="👀 "
                )
            )

    lines.extend([
        "ℹ️ Vérifie les cotes, absences, rotations et compositions.",
        "Le score est un filtre statistique : il ne garantit aucun résultat."
    ])

    return "\n".join(lines)


def split_message(message, max_length=1900):
    chunks = []
    remaining = message.strip()

    while len(remaining) > max_length:
        split_at = remaining.rfind("\n", 0, max_length)

        if split_at < max_length // 2:
            split_at = remaining.rfind(" ", 0, max_length)

        if split_at <= 0:
            split_at = max_length

        chunks.append(
            remaining[:split_at].strip()
        )

        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks


def send_to_discord(message):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError(
            "Le secret DISCORD_WEBHOOK_URL est manquant."
        )

    chunks = split_message(message)

    for number, chunk in enumerate(chunks[:5], start=1):
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json={
                "username": "Football 12 BTTS Bot",
                "content": chunk,
                "allowed_mentions": {
                    "parse": []
                }
            },
            timeout=30
        )

        response.raise_for_status()

        print(
            f"[INFO] Message Discord "
            f"{number}/{len(chunks)} envoyé."
        )


def main():
    if not FD_TOKEN:
        raise RuntimeError(
            "Le secret FOOTBALL_DATA_API_TOKEN est manquant."
        )

    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError(
            "Le secret DISCORD_WEBHOOK_URL est manquant."
        )

    dates = get_target_dates()

    print(
        "[INFO] Dates analysées : "
        + ", ".join(date.isoformat() for date in dates)
    )

    competition_codes = get_available_competitions()

    if not competition_codes:
        raise RuntimeError(
            "Aucune compétition accessible avec ce token."
        )

    upcoming_matches = get_upcoming_matches(
        competition_codes,
        dates
    )

    print(
        f"[INFO] Matchs à analyser : {len(upcoming_matches)}"
    )

    cache = {}
    analyzed_matches = []

    for index, match in enumerate(upcoming_matches, start=1):
        home_name = match.get("homeTeam", {}).get("name", "")
        away_name = match.get("awayTeam", {}).get("name", "")

        print(
            f"[INFO] Analyse {index}/{len(upcoming_matches)} : "
            f"{home_name} vs {away_name}"
        )

        try:
            result = analyze_match(match, cache)

            if result:
                analyzed_matches.append(result)

                print(
                    f"[INFO] Score : {result['score']}/100 | "
                    f"Nuls moyens : {result['average_draw_pct']} %"
                )

        except Exception as error:
            print(
                f"[WARN] Match ignoré : "
                f"{home_name} vs {away_name} — {error}"
            )

    analyzed_matches.sort(
        key=lambda item: (
            -item["score"],
            item["kickoff"]
        )
    )

    candidates = [
        item
        for item in analyzed_matches
        if item["qualified"]
    ][:MAX_CANDIDATES]

    watchlist = [
        item
        for item in analyzed_matches
        if not item["qualified"]
    ][:MAX_WATCHLIST]

    print(
        f"[INFO] Profils retenus : {len(candidates)}"
    )

    print(
        f"[INFO] Matchs sous le seuil : {len(watchlist)}"
    )

    discord_message = build_discord_message(
        candidates,
        watchlist,
        dates
    )

    print("\n===== MESSAGE DISCORD =====")
    print(discord_message)

    send_to_discord(discord_message)


if __name__ == "__main__":
    main()
