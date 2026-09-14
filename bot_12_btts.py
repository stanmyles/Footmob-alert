import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests


# ============================================================
# CONFIGURATION
# ============================================================

API_BASE_URL = "https://v3.football.api-sports.io"
PARIS_TZ = ZoneInfo("Europe/Paris")

API_KEY = os.getenv("API_FOOTBALL_KEY", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# Réglages modifiables depuis GitHub Actions.
LOOKBACK_FIXTURES = int(os.getenv("LOOKBACK_FIXTURES", "20"))
MIN_COMPARABLE_MATCHES = int(os.getenv("MIN_COMPARABLE_MATCHES", "5"))
MIN_SCORE = int(os.getenv("MIN_SCORE", "80"))
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "8"))
TARGET_DATE = os.getenv("TARGET_DATE", "").strip()

session = requests.Session()
session.headers.update({
    "x-apisports-key": API_KEY,
    "Accept": "application/json",
})


# ============================================================
# API-FOOTBALL
# ============================================================

def api_get(endpoint, params=None):
    url = f"{API_BASE_URL}{endpoint}"

    response = session.get(
        url,
        params=params or {},
        timeout=30
    )
    response.raise_for_status()

    data = response.json()

    if data.get("errors"):
        raise RuntimeError(f"Erreur API-Football : {data['errors']}")

    return data.get("response", [])


def get_target_date():
    if TARGET_DATE:
        return datetime.strptime(TARGET_DATE, "%Y-%m-%d").date()

    return datetime.now(PARIS_TZ).date()


def get_team_recent_fixtures(team_id):
    return api_get(
        "/fixtures",
        {
            "team": team_id,
            "last": LOOKBACK_FIXTURES,
        }
    )


def completed_fixture(fixture):
    status = fixture.get("fixture", {}).get("status", {}).get("short")
    goals = fixture.get("goals", {})

    return (
        status in {"FT", "AET", "PEN"}
        and goals.get("home") is not None
        and goals.get("away") is not None
    )


def fixture_is_usable(fixture):
    status = fixture.get("fixture", {}).get("status", {}).get("short")

    home = fixture.get("teams", {}).get("home", {})
    away = fixture.get("teams", {}).get("away", {})

    return (
        status in {"NS", "TBD"}
        and home.get("id") is not None
        and away.get("id") is not None
    )


# ============================================================
# STATISTIQUES DOMICILE / EXTERIEUR
# ============================================================

def team_venue_stats(fixtures, team_id, venue):
    """
    Calcule les statistiques d'une équipe :
    - venue='home' : uniquement les matchs à domicile
    - venue='away' : uniquement les matchs à l'extérieur
    """

    matches = []

    for fixture in fixtures:
        if not completed_fixture(fixture):
            continue

        home_id = fixture["teams"]["home"]["id"]
        is_home = home_id == team_id

        if venue == "home" and not is_home:
            continue

        if venue == "away" and is_home:
            continue

        home_goals = fixture["goals"]["home"]
        away_goals = fixture["goals"]["away"]

        goals_for = home_goals if is_home else away_goals
        goals_against = away_goals if is_home else home_goals

        matches.append({
            "btts": home_goals > 0 and away_goals > 0,
            "scored": goals_for > 0,
            "conceded": goals_against > 0,
            "draw": home_goals == away_goals,
            "goals_for": goals_for,
            "goals_against": goals_against,
        })

    total = len(matches)

    if total == 0:
        return None

    def pct(key):
        return round(
            100 * sum(match[key] for match in matches) / total,
            1
        )

    return {
        "matches": total,
        "btts_pct": pct("btts"),
        "scored_pct": pct("scored"),
        "conceded_pct": pct("conceded"),
        "draw_pct": pct("draw"),
        "goals_for_avg": round(
            sum(match["goals_for"] for match in matches) / total,
            2
        ),
        "goals_against_avg": round(
            sum(match["goals_against"] for match in matches) / total,
            2
        ),
    }


# ============================================================
# SCORE POUR LE MARCHE : 12 + BTTS OUI
# ============================================================

def calculate_score(home_stats, away_stats):
    """
    Marché analysé :
    - 12 = il ne doit pas y avoir match nul
    - BTTS Oui = les deux équipes doivent marquer

    Score maximum : 100.
    """

    score = 0
    reasons = []

    # BTTS dans les matchs récents à domicile : 20 points.
    if home_stats["btts_pct"] >= 60:
        score += 20
        reasons.append(
            f"BTTS domicile {home_stats['btts_pct']} %"
        )

    # BTTS dans les matchs récents à l'extérieur : 20 points.
    if away_stats["btts_pct"] >= 60:
        score += 20
        reasons.append(
            f"BTTS extérieur {away_stats['btts_pct']} %"
        )

    # L'équipe à domicile marque régulièrement : 15 points.
    if home_stats["scored_pct"] >= 80:
        score += 15
        reasons.append(
            f"domicile marque {home_stats['scored_pct']} %"
        )

    # L'équipe à l'extérieur marque régulièrement : 15 points.
    if away_stats["scored_pct"] >= 70:
        score += 15
        reasons.append(
            f"extérieur marque {away_stats['scored_pct']} %"
        )

    # L'équipe à domicile encaisse : 10 points.
    if home_stats["conceded_pct"] >= 60:
        score += 10
        reasons.append(
            f"domicile encaisse {home_stats['conceded_pct']} %"
        )

    # L'équipe à l'extérieur encaisse : 10 points.
    if away_stats["conceded_pct"] >= 60:
        score += 10
        reasons.append(
            f"extérieur encaisse {away_stats['conceded_pct']} %"
        )

    # Peu de nuls = important pour le pari 12 : 10 points.
    average_draw_pct = (
        home_stats["draw_pct"] + away_stats["draw_pct"]
    ) / 2

    if average_draw_pct <= 25:
        score += 10
        reasons.append(
            f"nuls moyens {average_draw_pct:.1f} %"
        )

    return score, reasons


# ============================================================
# FORMATAGE DISCORD
# ============================================================

def fixture_datetime_paris(fixture):
    raw_date = fixture["fixture"]["date"]

    return datetime.fromisoformat(
        raw_date.replace("Z", "+00:00")
    ).astimezone(PARIS_TZ)


def format_stats(label, stats):
    return (
        f"**{label}** ({stats['matches']} matchs)\n"
        f"BTTS : {stats['btts_pct']} % | "
        f"Marque : {stats['scored_pct']} % | "
        f"Encaisse : {stats['conceded_pct']} % | "
        f"Nuls : {stats['draw_pct']} %\n"
        f"Buts pour : {stats['goals_for_avg']} | "
        f"Buts contre : {stats['goals_against_avg']}"
    )


def build_discord_embed(candidates, target_date):
    if not candidates:
        return {
            "title": (
                "Alerte 12 + BTTS Oui — "
                f"{target_date.strftime('%d/%m/%Y')}"
            ),
            "description": (
                "Aucun match ne dépasse le seuil statistique aujourd'hui.\n\n"
                "Ne pas forcer une sélection est une bonne décision."
            ),
            "color": 9807270,
            "footer": {
                "text": (
                    "Filtre statistique uniquement. "
                    "Aucune garantie de résultat."
                )
            }
        }

    lines = [
        "Marché ciblé : **12 (pas de nul) + BTTS Oui**.",
        "Statistiques calculées séparément à domicile et à l'extérieur.",
        "",
    ]

    for index, candidate in enumerate(candidates, start=1):
        stats_home = candidate["home_stats"]
        stats_away = candidate["away_stats"]

        lines.extend([
            (
                f"**{index}. {candidate['home']} vs "
                f"{candidate['away']} — Score {candidate['score']}/100**"
            ),
            (
                f"{candidate['country']} · {candidate['league']} · "
                f"{candidate['kickoff'].strftime('%H:%M')} heure Paris"
            ),
            format_stats("Domicile", stats_home),
            format_stats("Extérieur", stats_away),
            f"Signaux : {' • '.join(candidate['reasons'])}",
            "",
        ])

    description = "\n".join(lines)

    # Discord limite une description d'embed à 4 096 caractères.
    if len(description) > 4000:
        description = description[:3950] + "\n\n... Liste tronquée."

    return {
        "title": (
            "Alerte 12 + BTTS Oui — "
            f"{target_date.strftime('%d/%m/%Y')}"
        ),
        "description": description,
        "color": 3066993,
        "footer": {
            "text": (
                "Vérifie absences, compositions et cotes avant toute décision."
            )
        }
    }


def send_to_discord(embed):
    payload = {
        "username": "Football 12 BTTS Bot",
        "embeds": [embed],
        "allowed_mentions": {
            "parse": []
        }
    }

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json=payload,
        timeout=30
    )

    response.raise_for_status()


# ============================================================
# ANALYSE D'UN MATCH
# ============================================================

def analyze_fixture(fixture, cache):
    home = fixture["teams"]["home"]
    away = fixture["teams"]["away"]
    league = fixture["league"]

    home_id = home["id"]
    away_id = away["id"]

    # Le cache empêche de demander plusieurs fois les mêmes données API.
    if home_id not in cache:
        cache[home_id] = get_team_recent_fixtures(home_id)
        time.sleep(0.15)

    if away_id not in cache:
        cache[away_id] = get_team_recent_fixtures(away_id)
        time.sleep(0.15)

    # Forme de l'équipe domicile : seulement ses matchs à domicile.
    home_stats = team_venue_stats(
        cache[home_id],
        home_id,
        "home"
    )

    # Forme de l'équipe extérieure : seulement ses matchs à l'extérieur.
    away_stats = team_venue_stats(
        cache[away_id],
        away_id,
        "away"
    )

    if not home_stats or not away_stats:
        return None

    # Évite les statistiques calculées sur trop peu de matchs.
    if (
        home_stats["matches"] < MIN_COMPARABLE_MATCHES
        or away_stats["matches"] < MIN_COMPARABLE_MATCHES
    ):
        return None

    score, reasons = calculate_score(home_stats, away_stats)

    if score < MIN_SCORE:
        return None

    return {
        "home": home["name"],
        "away": away["name"],
        "league": league["name"],
        "country": league.get("country", "Inconnu"),
        "kickoff": fixture_datetime_paris(fixture),
        "score": score,
        "reasons": reasons,
        "home_stats": home_stats,
        "away_stats": away_stats,
    }


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

def main():
    if not API_KEY:
        raise RuntimeError("Secret API_FOOTBALL_KEY manquant.")

    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Secret DISCORD_WEBHOOK_URL manquant.")

    target_date = get_target_date()

    fixtures = api_get(
        "/fixtures",
        {
            "date": target_date.isoformat(),
            "timezone": "Europe/Paris",
        }
    )

    fixtures_to_analyze = [
        fixture for fixture in fixtures
        if fixture_is_usable(fixture)
    ]

    print(
        f"Date : {target_date} | "
        f"matchs récupérés : {len(fixtures)} | "
        f"matchs analysables : {len(fixtures_to_analyze)}"
    )

    cache = {}
    candidates = []

    for index, fixture in enumerate(fixtures_to_analyze, start=1):
        home = fixture["teams"]["home"]["name"]
        away = fixture["teams"]["away"]["name"]

        print(
            f"[{index}/{len(fixtures_to_analyze)}] "
            f"{home} vs {away}"
        )

        try:
            candidate = analyze_fixture(fixture, cache)

            if candidate:
                candidates.append(candidate)
                print(
                    f"  -> Candidat retenu : "
                    f"{candidate['score']}/100"
                )

        except Exception as error:
            print(f"  -> Match ignoré : {error}")

    candidates.sort(
        key=lambda candidate: (
            -candidate["score"],
            candidate["kickoff"]
        )
    )

    candidates = candidates[:MAX_CANDIDATES]

    print(f"Candidats finaux : {len(candidates)}")

    embed = build_discord_embed(candidates, target_date)
    send_to_discord(embed)

    print("Alerte Discord envoyée.")


if __name__ == "__main__":
    main()
