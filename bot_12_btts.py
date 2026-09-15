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

# DAYS_AHEAD = 1 :
# analyse aujourd'hui et demain.
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "1"))

# Nombre de résultats comparables récupérés par équipe.
# Domicile pour l'équipe qui reçoit.
# Extérieur pour l'équipe qui se déplace.
LOOKBACK_MATCHES = int(os.getenv("LOOKBACK_MATCHES", "10"))

# Nombre minimum de matchs comparables requis.
MIN_COMPARABLE_MATCHES = int(
    os.getenv("MIN_COMPARABLE_MATCHES", "5")
)

# Score minimum pour être affiché dans les profils retenus.
# 60 est recommandé pour avoir davantage de matchs.
MIN_SCORE = int(os.getenv("MIN_SCORE", "60"))

# Nombre maximum de matchs retenus affichés dans Discord.
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "6"))

# Nombre maximum de meilleurs matchs sous le seuil principal.
MAX_WATCHLIST = int(os.getenv("MAX_WATCHLIST", "4"))

# Facultatif :
# analyse uniquement cette date, par exemple 2026-09-17.
# Vide = aujourd'hui + demain.
TARGET_DATE = os.getenv("TARGET_DATE", "").strip()

# Délai minimal entre les appels football-data.org.
FD_MIN_INTERVAL = float(os.getenv("FD_MIN_INTERVAL", "6.5"))

last_api_call = 0.0

session = requests.Session()
session.headers.update({
    "User-Agent": "github-actions-football-12-btts/4.0",
    "Accept": "application/json",
})


# ============================================================
# OUTILS API FOOTBALL-DATA.ORG
# ============================================================

def throttle():
    """Respecte un délai minimum entre deux appels API."""
    global last_api_call

    now = time.time()
    wait = FD_MIN_INTERVAL - (now - last_api_call)

    if wait > 0:
        time.sleep(wait)

    last_api_call = time.time()


def api_get(endpoint, params=None, retries=3):
    """
    Exécute une requête GET vers football-data.org.

    Le token est envoyé via X-Auth-Token.
    La fonction gère les erreurs réseau et les limites HTTP 429.
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
# RECUPERATION DES DATES ET DES MATCHS
# ============================================================

def get_target_dates():
    """
    Retourne les dates à analyser.

    Si TARGET_DATE est renseigné :
    - seule cette date est analysée.

    Si TARGET_DATE est vide :
    - aujourd'hui et les DAYS_AHEAD jours suivants sont analysés.
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
    """Récupère les compétitions disponibles avec ton token."""
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
    Récupère les matchs à venir pour les compétitions accessibles.
    Seuls les statuts SCHEDULED et TIMED sont analysés.
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
    Récupère les derniers matchs terminés comparables d'une équipe.

    venue='HOME' :
    matchs joués à domicile uniquement.

    venue='AWAY' :
    matchs joués à l'extérieur uniquement.
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
# STATISTIQUES D'EQUIPE
# ============================================================

def get_full_time_goals(match):
    """Retourne les buts finaux ou (None, None) si non disponibles."""
    full_time = match.get("score", {}).get("fullTime", {})

    home_goals = full_time.get("home")
    away_goals = full_time.get("away")

    if home_goals is None or away_goals is None:
        return None, None

    return home_goals, away_goals


def team_stats(matches, team_id):
    """
    Calcule les statistiques utiles pour le marché 12 + BTTS Oui.

    Les matchs ont déjà été filtrés HOME ou AWAY lors de l'appel API.
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
# SCORE SIMPLE : 12 + BTTS OUI
# ============================================================

def calculate_12_btts_score(home_stats, away_stats):
    """
    Calcule un score simple pour le marché 12 + BTTS Oui.

    Conditions recherchées :
    - BTTS domicile suffisamment fréquent ;
    - BTTS extérieur suffisamment fréquent ;
    - domicile marque régulièrement ;
    - extérieur marque régulièrement ;
    - risque de nul raisonnable.

    Le bot ne choisit pas l'équipe gagnante :
    il cherche seulement un match avec BTTS + pas de nul.
    """
    score = 0
    signals = []

    # BTTS dans les matchs domicile de l'équipe qui reçoit.
    if home_stats["btts_pct"] >= 60:
        score += 25
        signals.append(
            f"BTTS domicile {home_stats['btts_pct']} %"
        )

    # BTTS dans les matchs extérieur de l'équipe visiteuse.
    if away_stats["btts_pct"] >= 50:
        score += 20
        signals.append(
            f"BTTS extérieur {away_stats['btts_pct']} %"
        )

    # L'équipe à domicile doit marquer fréquemment chez elle.
    if home_stats["scored_pct"] >= 70:
        score += 20
        signals.append(
            f"domicile marque {home_stats['scored_pct']} %"
        )

    # L'équipe à l'extérieur doit marquer fréquemment dehors.
    if away_stats["scored_pct"] >= 70:
        score += 20
        signals.append(
            f"extérieur marque {away_stats['scored_pct']} %"
        )

    # Le nul fait perdre le marché 12 + BTTS.
    average_draw_pct = (
        home_stats["draw_pct"] + away_stats["draw_pct"]
    ) / 2

    if average_draw_pct <= 30:
        score += 15
        signals.append(
            f"nuls moyens {average_draw_pct:.1f} %"
        )

    return score, signals, round(average_draw_pct, 1)


# ============================================================
# ANALYSE DES MATCHS
# ============================================================

def to_paris_time(utc_date):
    """Convertit une date UTC de l'API vers l'heure de Paris."""
    utc_datetime = datetime.fromisoformat(
        utc_date.replace("Z", "+00:00")
    )

    return utc_datetime.astimezone(PARIS_TZ)


def analyze_match(match, cache):
    """
    Analyse une affiche à partir :
    - des résultats domicile de l'équipe qui reçoit ;
    - des résultats extérieur de l'équipe qui se déplace ;
    - du filtre 12 + BTTS Oui.

    Le résultat est retourné même s'il est sous MIN_SCORE :
    cela permet d'afficher les meilleurs matchs dans Discord.
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
        "pick": (
            "12 + BTTS Oui — "
            "une équipe gagne et les deux équipes marquent"
        ),
        "signals": signals,
        "average_draw_pct": average_draw_pct,
        "home_stats": home_stats,
        "away_stats": away_stats,
        "qualified": score >= MIN_SCORE,
    }


# ============================================================
# CONSTRUCTION DU MESSAGE DISCORD
# ============================================================

def short_stats(label, stats):
    """Retourne une ligne compacte de statistiques pour Discord."""
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
    """Construit toutes les lignes Discord pour un match."""
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
            f"📊 Risque de nul moyen : "
            f"{candidate['average_draw_pct']} %"
        ),
        short_stats("Domicile", candidate["home_stats"]),
        short_stats("Extérieur", candidate["away_stats"]),
        f"📌 Signaux : {signals}",
        "",
    ]


def build_discord_message(candidates, watchlist, dates):
    """
    Construit le message final Discord.

    candidates :
    matchs dont le score est >= MIN_SCORE.

    watchlist :
    meilleurs matchs sous le seuil principal.
    Ils sont affichés pour information uniquement.
    """
    first_date = min(dates).strftime("%d/%m/%Y")
    last_date = max(dates).strftime("%d/%m/%Y")

    if first_date == last_date:
        date_text = first_date
    else:
        date_text = f"{first_date} au {last_date}"

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
                "Ces profils sont les meilleurs scores disponibles "
                "sous le seuil principal."
            ),
            (
                "Ils demandent une vérification des cotes, absences "
                "et compositions."
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

    elif not candidates:
        lines.extend([
            "Aucun match n'a pu être analysé avec suffisamment de données.",
            "",
        ])

    lines.extend([
        "ℹ️ Vérifie les cotes, absences, rotations, blessures et "
        "compositions avant le coup d'envoi.",
        "Le score est un filtre statistique : il ne garantit aucun résultat."
    ])

    return "\n".join(lines)


# ============================================================
# ENVOI DISCORD
# ============================================================

def split_message(message, max_length=1900):
    """
    Découpe un message long pour rester sous la limite Discord,
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
    """
    Envoie le message au webhook Discord.

    Discord limite le champ content à 2 000 caractères.
    Les messages sont donc découpés par sécurité.
    """
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


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

def main():
    """Lance l'analyse complète puis envoie le rapport Discord."""
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
                    f"Nuls moyens : "
                    f"{result['average_draw_pct']} % | "
                    f"Match : {result['home']} vs {result['away']}"
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

    # Matchs dont le score atteint le seuil.
    candidates = [
        item
        for item in analyzed_matches
        if item["qualified"]
    ][:MAX_CANDIDATES]

    # Important :
    # les meilleurs matchs sous le seuil sont toujours affichés.
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
