import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from requests.exceptions import HTTPError, RequestException


# ============================================================
# CONFIGURATION
# ============================================================

# Même API que ton bot H2H actuel.
BASE_FD = "https://api.football-data.org/v4"
PARIS_TZ = ZoneInfo("Europe/Paris")

# Même Secrets GitHub que ton bot H2H.
FD_TOKEN = os.getenv("FOOTBALL_DATA_API_TOKEN", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# Analyse aujourd'hui + demain par défaut.
# DAYS_AHEAD = 1 signifie : aujourd'hui et demain.
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "1"))

# Nombre de résultats récents récupérés par équipe.
LOOKBACK_MATCHES = int(os.getenv("LOOKBACK_MATCHES", "10"))

# Minimum de matchs comparables :
# - équipe domicile : résultats à domicile
# - équipe extérieure : résultats à l'extérieur
MIN_COMPARABLE_MATCHES = int(
    os.getenv("MIN_COMPARABLE_MATCHES", "5")
)

# Score minimum sur 100 pour envoyer une alerte Discord.
MIN_SCORE = int(os.getenv("MIN_SCORE", "80"))

# Nombre maximal de candidats affichés dans Discord.
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "6"))

# Facultatif : pour tester une date manuellement.
# Exemple : TARGET_DATE = 2026-09-15
TARGET_DATE = os.getenv("TARGET_DATE", "").strip()

# football-data.org impose des limites d'appels selon ton forfait.
# Ton bot H2H utilise déjà environ 6,5 secondes entre les appels.
FD_MIN_INTERVAL = 6.5
last_api_call = 0.0

session = requests.Session()
session.headers.update({
    "User-Agent": "github-actions-football-12-btts/1.0",
})


# ============================================================
# OUTILS API FOOTBALL-DATA.ORG
# ============================================================

def throttle():
    """Respecte un intervalle minimal entre les appels API."""
    global last_api_call

    now = time.time()
    wait = FD_MIN_INTERVAL - (now - last_api_call)

    if wait > 0:
        time.sleep(wait)

    last_api_call = time.time()


def api_get(endpoint, params=None, retries=3):
    """
    Fait une requête GET vers football-data.org.

    Le token est transmis dans le header X-Auth-Token,
    comme dans ton bot H2H.
    """
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

            # Si la limite de l'API est atteinte, attend puis réessaie.
            if status == 429 and attempt < retries - 1:
                retry_after = error.response.headers.get(
                    "Retry-After",
                    ""
                )

                if retry_after.isdigit():
                    delay = int(retry_after)
                else:
                    delay = (attempt + 1) * 15

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


# ============================================================
# RECUPERATION DES MATCHS
# ============================================================

def get_target_dates():
    """
    Retourne les dates à analyser.

    - Si TARGET_DATE est renseigné manuellement :
      analyse uniquement cette date.
    - Sinon :
      analyse aujourd'hui et les DAYS_AHEAD jours suivants.
    """
    if TARGET_DATE:
        try:
            selected_date = datetime.strptime(
                TARGET_DATE,
                "%Y-%m-%d"
            ).date()

            return [selected_date]

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
    """
    Récupère les compétitions auxquelles ton token donne accès.
    Même principe que ton bot H2H.
    """
    data = api_get("/competitions")

    codes = sorted({
        competition.get("code")
        for competition in data.get("competitions", [])
        if competition.get("code")
    })

    print(
        "[INFO] Compétitions accessibles : "
        + ", ".join(codes)
    )

    return codes


def get_upcoming_matches(competition_codes, dates):
    """
    Récupère les matchs à venir dans les compétitions accessibles.
    """
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

                if not match_id:
                    continue

                if match_id in seen_match_ids:
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
                f"[WARN] Compétition {competition_code} "
                f"ignorée : {error}"
            )

    matches.sort(
        key=lambda match: match.get("utcDate", "")
    )

    return matches


def get_team_finished_matches(team_id, venue):
    """
    Récupère les résultats terminés d'une équipe selon son lieu :

    - venue='HOME' : seulement les matchs à domicile.
    - venue='AWAY' : seulement les matchs à l'extérieur.
    """
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
        f"{len(matches)} match(s) terminé(s) récupéré(s)."
    )

    return matches


# ============================================================
# STATISTIQUES BTTS / BUTS / NULS
# ============================================================

def get_full_time_goals(match):
    """Extrait les scores finaux, ou renvoie None si indisponibles."""
    full_time = match.get("score", {}).get("fullTime", {})

    home_goals = full_time.get("home")
    away_goals = full_time.get("away")

    if home_goals is None or away_goals is None:
        return None, None

    return home_goals, away_goals


def team_stats(matches, team_id):
    """
    Calcule les statistiques nécessaires pour le marché :
    12 + BTTS Oui.

    Les matchs ont déjà été demandés à l'API avec un filtre
    domicile ou extérieur.
    """
    rows = []

    for match in matches:
        home_goals, away_goals = get_full_time_goals(match)

        if home_goals is None or away_goals is None:
            continue

        home_team_id = match.get("homeTeam", {}).get("id")
        is_home = home_team_id == team_id

        goals_for = home_goals if is_home else away_goals
        goals_against = away_goals if is_home else home_goals

        rows.append({
            "btts": home_goals > 0 and away_goals > 0,
            "scored": goals_for > 0,
            "conceded": goals_against > 0,
            "draw": home_goals == away_goals,
            "goals_for": goals_for,
            "goals_against": goals_against,
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
        "draw_pct": pct("draw"),
        "goals_for_avg": round(
            sum(row["goals_for"] for row in rows) / total,
            2
        ),
        "goals_against_avg": round(
            sum(row["goals_against"] for row in rows) / total,
            2
        ),
    }


# ============================================================
# SCORE POUR 12 + BTTS OUI
# ============================================================

def calculate_score(home_stats, away_stats):
    """
    Calcule un score sur 100 pour le marché :

    - 12 : le match ne doit pas se terminer nul.
    - BTTS Oui : les deux équipes doivent marquer.

    Le score est un filtre statistique, pas une prédiction certaine.
    """
    score = 0
    signals = []

    # BTTS équipe qui reçoit : 20 points.
    if home_stats["btts_pct"] >= 60:
        score += 20
        signals.append(
            f"BTTS domicile {home_stats['btts_pct']} %"
        )

    # BTTS équipe qui se déplace : 20 points.
    if away_stats["btts_pct"] >= 60:
        score += 20
        signals.append(
            f"BTTS extérieur {away_stats['btts_pct']} %"
        )

    # L'équipe à domicile marque : 15 points.
    if home_stats["scored_pct"] >= 80:
        score += 15
        signals.append(
            f"domicile marque {home_stats['scored_pct']} %"
        )

    # L'équipe à l'extérieur marque : 15 points.
    if away_stats["scored_pct"] >= 70:
        score += 15
        signals.append(
            f"extérieur marque {away_stats['scored_pct']} %"
        )

    # L'équipe à domicile encaisse : 10 points.
    if home_stats["conceded_pct"] >= 60:
        score += 10
        signals.append(
            f"domicile encaisse {home_stats['conceded_pct']} %"
        )

    # L'équipe à l'extérieur encaisse : 10 points.
    if away_stats["conceded_pct"] >= 60:
        score += 10
        signals.append(
            f"extérieur encaisse {away_stats['conceded_pct']} %"
        )

    # Un faible taux de nuls est nécessaire pour le marché 12.
    average_draw_pct = (
        home_stats["draw_pct"] + away_stats["draw_pct"]
    ) / 2

    if average_draw_pct <= 25:
        score += 10
        signals.append(
            f"nuls moyens {average_draw_pct:.1f} %"
        )

    return score, signals


# ============================================================
# ANALYSE DES MATCHS
# ============================================================

def to_paris_time(utc_date):
    """Convertit l'heure UTC de football-data.org vers Paris."""
    utc_datetime = datetime.fromisoformat(
        utc_date.replace("Z", "+00:00")
    )

    return utc_datetime.astimezone(PARIS_TZ)


def analyze_match(match, cache):
    """
    Analyse une affiche :
    - résultats domicile de l'équipe qui reçoit ;
    - résultats extérieur de l'équipe qui se déplace ;
    - attribution d'un score pour 12 + BTTS Oui.
    """
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
            "[INFO] Pas assez de résultats comparables "
            "domicile / extérieur."
        )
        return None

    score, signals = calculate_score(
        home_stats,
        away_stats
    )

    if score < MIN_SCORE:
        return None

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
        "home_stats": home_stats,
        "away_stats": away_stats,
    }


# ============================================================
# MESSAGE DISCORD
# ============================================================

def short_stats(label, stats):
    """Produit une ligne courte de statistiques pour Discord."""
    return (
        f"**{label}** ({stats['matches']} matchs) — "
        f"BTTS {stats['btts_pct']} % | "
        f"marque {stats['scored_pct']} % | "
        f"encaisse {stats['conceded_pct']} % | "
        f"nuls {stats['draw_pct']} %"
    )


def build_discord_message(candidates, dates):
    """
    Construit le message Discord.
    Discord limite le contenu à environ 2 000 caractères par message.
    """
    first_date = min(dates).strftime("%d/%m/%Y")
    last_date = max(dates).strftime("%d/%m/%Y")

    if first_date == last_date:
        date_text = first_date
    else:
        date_text = f"{first_date} au {last_date}"

    if not candidates:
        return (
            "⚠️ **Alerte Football — 12 + BTTS Oui**\n"
            f"📅 Période analysée : {date_text}\n\n"
            "Aucun match ne dépasse le seuil statistique défini.\n"
            "Ne pas forcer une sélection est une bonne décision.\n\n"
            "ℹ️ Filtre statistique uniquement : "
            "il ne garantit pas un résultat."
        )

    lines = [
        "📊 **Alerte Football — 12 + BTTS Oui**",
        f"📅 Période analysée : {date_text}",
        (
            "🎯 Marché : une équipe gagne "
            "(pas de nul) + les deux équipes marquent."
        ),
        "",
    ]

    for index, candidate in enumerate(candidates, start=1):
        signals = " • ".join(candidate["signals"])

        lines.extend([
            (
                f"**{index}. {candidate['home']} vs "
                f"{candidate['away']} — Score {candidate['score']}/100**"
            ),
            (
                f"🏆 {candidate['competition']} | "
                f"🕒 {candidate['kickoff'].strftime('%d/%m %H:%M')} "
                "heure Paris"
            ),
            short_stats("Domicile", candidate["home_stats"]),
            short_stats("Extérieur", candidate["away_stats"]),
            f"📌 Signaux : {signals}",
            "",
        ])

    lines.extend([
        "ℹ️ Vérifie les cotes, absences et compositions avant toute décision.",
        "Le filtre ne garantit aucun résultat."
    ])

    return "\n".join(lines)


def send_to_discord(message):
    """
    Envoie le message au webhook Discord.
    Découpe automatiquement le message pour respecter la limite Discord.
    """
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Secret DISCORD_WEBHOOK_URL manquant.")

    chunks = [
        message[index:index + 1900]
        for index in range(0, len(message), 1900)
    ]

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
            f"[INFO] Message Discord {number} envoyé."
        )


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

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
    candidates = []

    for index, match in enumerate(upcoming_matches, start=1):
        home_team = match.get("homeTeam", {}).get("name", "")
        away_team = match.get("awayTeam", {}).get("name", "")

        print(
            f"[INFO] Analyse {index}/{len(upcoming_matches)} : "
            f"{home_team} vs {away_team}"
        )

        try:
            candidate = analyze_match(match, cache)

            if candidate:
                candidates.append(candidate)

                print(
                    f"[INFO] Candidat retenu : "
                    f"{candidate['score']}/100"
                )

        except Exception as error:
            print(
                f"[WARN] Match ignoré : "
                f"{home_team} vs {away_team} — {error}"
            )

    candidates.sort(
        key=lambda item: (
            -item["score"],
            item["kickoff"]
        )
    )

    candidates = candidates[:MAX_CANDIDATES]

    print(
        f"[INFO] Candidats finaux : {len(candidates)}"
    )

    discord_message = build_discord_message(
        candidates,
        dates
    )

    print("\n===== MESSAGE DISCORD =====")
    print(discord_message)

    send_to_discord(discord_message)


if __name__ == "__main__":
    main()
