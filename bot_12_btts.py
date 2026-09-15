import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from requests.exceptions import HTTPError, RequestException


# ============================================================
# CONFIGURATION
# ============================================================

BASE_FD = "https://api.football-data.org/v4"
PARIS_TZ = ZoneInfo("Europe/Paris")

# Secrets GitHub.
FD_TOKEN = os.getenv("FOOTBALL_DATA_API_TOKEN", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# DAYS_AHEAD = 1 : analyse aujourd'hui et demain.
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "1"))

# Nombre de résultats domicile / extérieur analysés par équipe.
LOOKBACK_MATCHES = int(os.getenv("LOOKBACK_MATCHES", "10"))

# Une équipe doit avoir au moins ce nombre de résultats comparables.
MIN_COMPARABLE_MATCHES = int(
    os.getenv("MIN_COMPARABLE_MATCHES", "5")
)

# Seuil des alertes principales.
# 65 : plus de profils, tout en restant filtré.
# 70 : plus sélectif.
# 75+ : très sélectif.
MIN_SCORE = int(os.getenv("MIN_SCORE", "65"))

# Nombre maximum de profils validés envoyés dans Discord.
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "5"))

# Nombre maximum de meilleurs matchs sous le seuil à afficher.
# Ils seront toujours affichés s'il existe des matchs analysés.
MAX_WATCHLIST = int(os.getenv("MAX_WATCHLIST", "3"))

# Facultatif : analyse une date précise en lancement manuel.
# Exemple : TARGET_DATE = 2026-09-15
TARGET_DATE = os.getenv("TARGET_DATE", "").strip()

# Délai minimal entre les appels football-data.org.
# 6,5 secondes est volontairement prudent.
FD_MIN_INTERVAL = float(os.getenv("FD_MIN_INTERVAL", "6.5"))

last_api_call = 0.0

session = requests.Session()
session.headers.update({
    "User-Agent": "github-actions-football-12-btts/3.0",
    "Accept": "application/json",
})


# ============================================================
# OUTILS API FOOTBALL-DATA.ORG
# ============================================================

def throttle():
    """Attend si nécessaire avant un nouvel appel API."""
    global last_api_call

    now = time.time()
    wait = FD_MIN_INTERVAL - (now - last_api_call)

    if wait > 0:
        time.sleep(wait)

    last_api_call = time.time()


def api_get(endpoint, params=None, retries=3):
    """
    Requête GET football-data.org avec :
    - authentification X-Auth-Token ;
    - attente entre les appels ;
    - reprise après erreur réseau ;
    - reprise après erreur HTTP 429.
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

            if status == 429 and attempt < retries - 1:
                retry_after = error.response.headers.get(
                    "Retry-After",
                    ""
                )

                if retry_after.isdigit():
                    delay = int(retry_after)
                else:
                    delay = (attempt + 1) * 20

                print(
                    f"[WARN] Limite API atteinte (HTTP 429). "
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
# DATES ET MATCHS A VENIR
# ============================================================

def get_target_dates():
    """
    Retourne les dates analysées.

    TARGET_DATE renseigné :
    - analyse uniquement cette date.

    TARGET_DATE vide :
    - analyse aujourd'hui et les DAYS_AHEAD jours suivants.
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
    """Récupère les compétitions accessibles avec ton token."""
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
    """
    Récupère les matchs SCHEDULED et TIMED pendant la période.
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
    """
    Récupère les derniers matchs terminés comparables :

    - HOME : résultats à domicile uniquement ;
    - AWAY : résultats à l'extérieur uniquement.
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
# CALCUL DES STATISTIQUES
# ============================================================

def get_full_time_goals(match):
    """Retourne les buts finaux domicile/extérieur ou (None, None)."""
    full_time = match.get("score", {}).get("fullTime", {})

    home_goals = full_time.get("home")
    away_goals = full_time.get("away")

    if home_goals is None or away_goals is None:
        return None, None

    return home_goals, away_goals


def team_stats(matches, team_id):
    """
    Calcule les statistiques d'une équipe à partir de ses matchs
    comparables domicile ou extérieur.

    Statistiques calculées :
    - BTTS ;
    - l'équipe marque ;
    - l'équipe encaisse ;
    - victoire / nul / défaite ;
    - buts marqués et encaissés par match.
    """
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
        "win_pct": pct("win"),
        "draw_pct": pct("draw"),
        "loss_pct": pct("loss"),
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
# SCORE 1 + BTTS OUI / 2 + BTTS OUI
# ============================================================

def calculate_side_score(winner_stats, opponent_stats, winner_side):
    """
    Calcule le score du scénario où une équipe précise gagne
    et les deux équipes marquent.

    winner_stats :
    statistiques de l'équipe supposée gagner.

    opponent_stats :
    statistiques de l'équipe adverse.

    winner_side :
    HOME ou AWAY.
    """
    score = 0
    signals = []

    side_label = "domicile" if winner_side == "HOME" else "extérieur"

    opponent_label = (
        "extérieur"
        if winner_side == "HOME"
        else "domicile"
    )

    # ========================================================
    # 1. L'équipe supposée gagner doit marquer.
    # ========================================================

    if winner_stats["scored_pct"] >= 80:
        score += 15
        signals.append(
            f"{side_label} marque {winner_stats['scored_pct']} %"
        )

    elif winner_stats["scored_pct"] >= 70:
        score += 8
        signals.append(
            f"{side_label} marque {winner_stats['scored_pct']} %"
        )

    if winner_stats["goals_for_avg"] >= 1.60:
        score += 8
        signals.append(
            f"{side_label} marque "
            f"{winner_stats['goals_for_avg']} but(s)/match"
        )

    elif winner_stats["goals_for_avg"] >= 1.30:
        score += 4
        signals.append(
            f"{side_label} marque "
            f"{winner_stats['goals_for_avg']} but(s)/match"
        )

    # ========================================================
    # 2. L'adversaire doit pouvoir marquer : BTTS Oui.
    # ========================================================

    if opponent_stats["scored_pct"] >= 75:
        score += 15
        signals.append(
            f"{opponent_label} marque "
            f"{opponent_stats['scored_pct']} %"
        )

    elif opponent_stats["scored_pct"] >= 65:
        score += 8
        signals.append(
            f"{opponent_label} marque "
            f"{opponent_stats['scored_pct']} %"
        )

    if opponent_stats["goals_for_avg"] >= 1.10:
        score += 6
        signals.append(
            f"{opponent_label} marque "
            f"{opponent_stats['goals_for_avg']} but(s)/match"
        )

    # ========================================================
    # 3. BTTS observé dans les matchs comparables.
    # ========================================================

    if winner_stats["btts_pct"] >= 60:
        score += 8
        signals.append(
            f"BTTS {side_label} {winner_stats['btts_pct']} %"
        )

    elif winner_stats["btts_pct"] >= 50:
        score += 4
        signals.append(
            f"BTTS {side_label} {winner_stats['btts_pct']} %"
        )

    if opponent_stats["btts_pct"] >= 55:
        score += 8
        signals.append(
            f"BTTS {opponent_label} "
            f"{opponent_stats['btts_pct']} %"
        )

    elif opponent_stats["btts_pct"] >= 50:
        score += 4
        signals.append(
            f"BTTS {opponent_label} "
            f"{opponent_stats['btts_pct']} %"
        )

    # ========================================================
    # 4. L'adversaire concède pour laisser une chance au vainqueur.
    # ========================================================

    if opponent_stats["conceded_pct"] >= 70:
        score += 8
        signals.append(
            f"{opponent_label} encaisse "
            f"{opponent_stats['conceded_pct']} %"
        )

    elif opponent_stats["conceded_pct"] >= 60:
        score += 4
        signals.append(
            f"{opponent_label} encaisse "
            f"{opponent_stats['conceded_pct']} %"
        )

    if opponent_stats["goals_against_avg"] >= 1.30:
        score += 5
        signals.append(
            f"{opponent_label} encaisse "
            f"{opponent_stats['goals_against_avg']} but(s)/match"
        )

    # ========================================================
    # 5. Signal de victoire.
    # ========================================================

    if winner_stats["win_pct"] >= 60:
        score += 15
        signals.append(
            f"{side_label} gagne {winner_stats['win_pct']} %"
        )

    elif winner_stats["win_pct"] >= 50:
        score += 8
        signals.append(
            f"{side_label} gagne {winner_stats['win_pct']} %"
        )

    if opponent_stats["loss_pct"] >= 50:
        score += 8
        signals.append(
            f"{opponent_label} perd {opponent_stats['loss_pct']} %"
        )

    elif opponent_stats["loss_pct"] >= 40:
        score += 4
        signals.append(
            f"{opponent_label} perd {opponent_stats['loss_pct']} %"
        )

    # ========================================================
    # 6. Protection contre le nul.
    # ========================================================

    average_draw_pct = (
        winner_stats["draw_pct"] + opponent_stats["draw_pct"]
    ) / 2

    if average_draw_pct <= 20:
        score += 10
        signals.append(
            f"nuls moyens faibles {average_draw_pct:.1f} %"
        )

    elif average_draw_pct <= 25:
        score += 5
        signals.append(
            f"nuls moyens modérés {average_draw_pct:.1f} %"
        )

    return score, signals, round(average_draw_pct, 1)


def calculate_score(home_stats, away_stats):
    """
    Compare les deux options :

    - 1 + BTTS Oui : domicile gagne et les deux équipes marquent.
    - 2 + BTTS Oui : extérieur gagne et les deux équipes marquent.

    La meilleure des deux options est retournée.
    """
    home_score, home_signals, home_draw_avg = calculate_side_score(
        winner_stats=home_stats,
        opponent_stats=away_stats,
        winner_side="HOME"
    )

    away_score, away_signals, away_draw_avg = calculate_side_score(
        winner_stats=away_stats,
        opponent_stats=home_stats,
        winner_side="AWAY"
    )

    if home_score >= away_score:
        return {
            "score": home_score,
            "pick_type": "HOME",
            "signals": home_signals,
            "average_draw_pct": home_draw_avg,
            "home_score": home_score,
            "away_score": away_score,
        }

    return {
        "score": away_score,
        "pick_type": "AWAY",
        "signals": away_signals,
        "average_draw_pct": away_draw_avg,
        "home_score": home_score,
        "away_score": away_score,
    }


# ============================================================
# ANALYSE D'UN MATCH
# ============================================================

def to_paris_time(utc_date):
    """Convertit la date UTC de l'API vers l'heure de Paris."""
    utc_datetime = datetime.fromisoformat(
        utc_date.replace("Z", "+00:00")
    )

    return utc_datetime.astimezone(PARIS_TZ)


def analyze_match(match, cache):
    """
    Analyse un match avec :
    - forme domicile de l'équipe qui reçoit ;
    - forme extérieure de l'équipe visiteuse ;
    - comparaison 1 + BTTS et 2 + BTTS.

    Un résultat est renvoyé même sous le seuil afin de pouvoir
    afficher les meilleurs matchs dans la section "à surveiller".
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
            "[INFO] Pas assez de données comparables : "
            f"{home_team.get('name', 'Domicile')} "
            f"({home_stats['matches']} match(s) domicile) / "
            f"{away_team.get('name', 'Extérieur')} "
            f"({away_stats['matches']} match(s) extérieur)."
        )
        return None

    score_data = calculate_score(home_stats, away_stats)

    if score_data["pick_type"] == "HOME":
        pick = (
            f"1 + BTTS Oui — "
            f"{home_team.get('name', 'Équipe domicile')} gagne "
            "et les deux équipes marquent"
        )
    else:
        pick = (
            f"2 + BTTS Oui — "
            f"{away_team.get('name', 'Équipe extérieure')} gagne "
            "et les deux équipes marquent"
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
        "score": score_data["score"],
        "pick_type": score_data["pick_type"],
        "pick": pick,
        "signals": score_data["signals"],
        "average_draw_pct": score_data["average_draw_pct"],
        "home_score": score_data["home_score"],
        "away_score": score_data["away_score"],
        "home_stats": home_stats,
        "away_stats": away_stats,
        "qualified": score_data["score"] >= MIN_SCORE,
    }


# ============================================================
# MESSAGE DISCORD
# ============================================================

def short_stats(label, stats):
    """Construit un résumé statistique court pour Discord."""
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
    """Construit les lignes Discord associées à une rencontre."""
    signals = " • ".join(candidate["signals"])

    return [
        (
            f"**{prefix}{index}. "
            f"{candidate['home']} vs {candidate['away']} — "
            f"Score {candidate['score']}/100**"
        ),
        f"🎯 {candidate['pick']}",
        (
            f"🏆 {candidate['competition']} | "
            f"🕒 {candidate['kickoff'].strftime('%d/%m %H:%M')} "
            "heure Paris"
        ),
        (
            f"📊 Comparaison : "
            f"1 + BTTS {candidate['home_score']}/100 | "
            f"2 + BTTS {candidate['away_score']}/100 | "
            f"nuls moyens {candidate['average_draw_pct']} %"
        ),
        short_stats("Domicile", candidate["home_stats"]),
        short_stats("Extérieur", candidate["away_stats"]),
        f"📌 Signaux : {signals}",
        "",
    ]


def build_discord_message(candidates, watchlist, dates):
    """
    Construit le message final envoyé à Discord.

    candidates :
    profils ayant un score >= MIN_SCORE.

    watchlist :
    meilleurs profils sous le seuil, affichés pour analyse,
    même lorsqu'ils ont un score inférieur à 60.
    """
    first_date = min(dates).strftime("%d/%m/%Y")
    last_date = max(dates).strftime("%d/%m/%Y")

    if first_date == last_date:
        date_text = first_date
    else:
        date_text = f"{first_date} au {last_date}"

    lines = [
        "📊 **Alerte Football — 1/2 + BTTS Oui**",
        f"📅 Période analysée : {date_text}",
        (
            "🎯 Marché analysé : une équipe précise gagne "
            "+ les deux équipes marquent."
        ),
        f"✅ Seuil d'alerte principale : {MIN_SCORE}/100",
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
            "Les meilleurs profils sous le seuil sont affichés ci-dessous.",
            "",
        ])

    if watchlist:
        lines.extend([
            "👀 **Meilleurs matchs à surveiller — non validés**",
            (
                "Ces matchs sont les meilleurs scores disponibles "
                "sous le seuil principal."
            ),
            "Ils demandent une analyse des cotes, absences et compositions.",
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

    elif not candidates:
        lines.extend([
            "Aucun match n'a pu être analysé avec suffisamment de données.",
            "",
        ])

    lines.extend([
        "ℹ️ Vérifie les cotes, les absences, les rotations, "
        "les blessures et les compositions avant le coup d'envoi.",
        "Le score est un filtre statistique : il ne garantit aucun résultat."
    ])

    return "\n".join(lines)


def split_message(message, max_length=1900):
    """
    Découpe le message pour respecter la limite Discord,
    en privilégiant les retours à la ligne.
    """
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
    """Envoie un ou plusieurs messages vers le webhook Discord."""
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Secret DISCORD_WEBHOOK_URL manquant.")

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
            f"[INFO] Message Discord {number}/{len(chunks)} envoyé."
        )


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

def main():
    """Exécute l'analyse, crée le message Discord et l'envoie."""
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
        home_team = match.get("homeTeam", {}).get("name", "")
        away_team = match.get("awayTeam", {}).get("name", "")

        print(
            f"[INFO] Analyse {index}/{len(upcoming_matches)} : "
            f"{home_team} vs {away_team}"
        )

        try:
            result = analyze_match(match, cache)

            if result:
                analyzed_matches.append(result)

                print(
                    f"[INFO] Score : {result['score']}/100 | "
                    f"1 + BTTS : {result['home_score']}/100 | "
                    f"2 + BTTS : {result['away_score']}/100 | "
                    f"Choix : {result['pick']}"
                )

        except Exception as error:
            print(
                f"[WARN] Match ignoré : "
                f"{home_team} vs {away_team} — {error}"
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

    # Important :
    # On affiche toujours les meilleurs scores sous le seuil.
    # Il n'y a plus de seuil minimum caché pour la watchlist.
    watchlist = [
        item
        for item in analyzed_matches
        if not item["qualified"]
    ][:MAX_WATCHLIST]

    print(
        f"[INFO] Matchs avec données suffisantes : "
        f"{len(analyzed_matches)}"
    )

    print(
        f"[INFO] Profils retenus (>= {MIN_SCORE}/100) : "
        f"{len(candidates)}"
    )

    print(
        f"[INFO] Meilleurs matchs sous le seuil : "
        f"{len(watchlist)}"
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
