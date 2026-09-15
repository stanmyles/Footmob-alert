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

# DAYS_AHEAD = 1 : aujourd'hui + demain.
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "1"))

# Nombre de résultats comparables analysés :
# - équipe domicile : derniers matchs à domicile ;
# - équipe extérieure : derniers matchs à l'extérieur.
LOOKBACK_MATCHES = int(os.getenv("LOOKBACK_MATCHES", "10"))

# Minimum de matchs comparables réellement disponibles.
MIN_COMPARABLE_MATCHES = int(
    os.getenv("MIN_COMPARABLE_MATCHES", "5")
)

# ============================================================
# CRITERES PRINCIPAUX : FAIBLE RISQUE DE NUL
# ============================================================

# Maximum de nuls de l'équipe à domicile, dans ses matchs domicile.
MAX_HOME_DRAW_PCT = float(
    os.getenv("MAX_HOME_DRAW_PCT", "30")
)

# Maximum de nuls de l'équipe à l'extérieur, dans ses déplacements.
MAX_AWAY_DRAW_PCT = float(
    os.getenv("MAX_AWAY_DRAW_PCT", "30")
)

# Maximum de la moyenne des nuls domicile + extérieur.
MAX_AVERAGE_DRAW_PCT = float(
    os.getenv("MAX_AVERAGE_DRAW_PCT", "25")
)

# Au moins une équipe doit gagner régulièrement dans son contexte.
# 40 % permet d'obtenir plus de matchs qu'un filtre très strict.
MIN_WIN_PCT_ONE_TEAM = float(
    os.getenv("MIN_WIN_PCT_ONE_TEAM", "40")
)

# ============================================================
# TAG COMPLEMENTAIRE BTTS : NON OBLIGATOIRE
# ============================================================

# Le match est marqué "BTTS favorable" si ces 4 critères sont remplis.
# Ces paramètres n'éliminent pas un match faible nul.
BTTS_HOME_MIN_PCT = float(
    os.getenv("BTTS_HOME_MIN_PCT", "50")
)

BTTS_AWAY_MIN_PCT = float(
    os.getenv("BTTS_AWAY_MIN_PCT", "50")
)

BTTS_HOME_SCORED_MIN_PCT = float(
    os.getenv("BTTS_HOME_SCORED_MIN_PCT", "70")
)

BTTS_AWAY_SCORED_MIN_PCT = float(
    os.getenv("BTTS_AWAY_SCORED_MIN_PCT", "60")
)

# Nombre de matchs affichés dans Discord.
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "10"))
MAX_WATCHLIST = int(os.getenv("MAX_WATCHLIST", "5"))

# Facultatif : date manuelle, exemple 2026-09-17.
# Si vide, le bot analyse aujourd'hui + demain.
TARGET_DATE = os.getenv("TARGET_DATE", "").strip()

# Délai minimum entre deux appels football-data.org.
FD_MIN_INTERVAL = float(os.getenv("FD_MIN_INTERVAL", "6.5"))

last_api_call = 0.0

session = requests.Session()
session.headers.update({
    "User-Agent": "github-actions-football-low-draw/1.0",
    "Accept": "application/json",
})


# ============================================================
# OUTILS API FOOTBALL-DATA.ORG
# ============================================================

def throttle():
    """Respecte le délai minimal entre deux appels API."""
    global last_api_call

    now = time.time()
    wait = FD_MIN_INTERVAL - (now - last_api_call)

    if wait > 0:
        time.sleep(wait)

    last_api_call = time.time()


def api_get(endpoint, params=None, retries=3):
    """
    Fait une requête GET vers football-data.org :
    - avec token X-Auth-Token ;
    - avec temporisation ;
    - avec nouvelles tentatives sur erreur réseau ou HTTP 429.
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
# RECUPERATION DES MATCHS
# ============================================================

def get_target_dates():
    """
    Retourne la liste des dates à analyser.

    TARGET_DATE défini :
    - analyse uniquement cette date.

    TARGET_DATE vide :
    - analyse aujourd'hui + DAYS_AHEAD jours.
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
    Récupère les matchs programmés pendant la période demandée.
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
    Récupère les résultats terminés comparables d'une équipe :

    HOME : matchs à domicile seulement.
    AWAY : matchs à l'extérieur seulement.
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
        f"{len(matches)} match(s) récupéré(s)."
    )

    return matches


# ============================================================
# STATISTIQUES
# ============================================================

def get_full_time_goals(match):
    """Extrait les buts finaux ou renvoie (None, None)."""
    full_time = match.get("score", {}).get("fullTime", {})

    home_goals = full_time.get("home")
    away_goals = full_time.get("away")

    if home_goals is None or away_goals is None:
        return None, None

    return home_goals, away_goals


def team_stats(matches, team_id):
    """
    Calcule les statistiques domicile ou extérieur d'une équipe.
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
# FILTRE FAIBLE RISQUE DE NUL
# ============================================================

def calculate_low_draw_score(home_stats, away_stats):
    """
    Attribue un score de classement aux matchs.

    Les critères obligatoires sont vérifiés dans analyze_match().
    Ce score sert à classer les meilleurs matchs parmi ceux analysés.
    """
    score = 0
    signals = []

    average_draw_pct = (
        home_stats["draw_pct"] + away_stats["draw_pct"]
    ) / 2

    # Plus la moyenne des nuls est faible, plus le score est élevé.
    if average_draw_pct <= 10:
        score += 45
        signals.append(
            f"nuls moyens très très faibles {average_draw_pct:.1f} %"
        )

    elif average_draw_pct <= 15:
        score += 40
        signals.append(
            f"nuls moyens très faibles {average_draw_pct:.1f} %"
        )

    elif average_draw_pct <= 20:
        score += 30
        signals.append(
            f"nuls moyens faibles {average_draw_pct:.1f} %"
        )

    elif average_draw_pct <= 25:
        score += 20
        signals.append(
            f"nuls moyens limités {average_draw_pct:.1f} %"
        )

    # Bonus si chaque équipe fait vraiment peu de nuls.
    if home_stats["draw_pct"] <= 20:
        score += 10
        signals.append(
            f"nuls domicile {home_stats['draw_pct']} %"
        )

    if away_stats["draw_pct"] <= 20:
        score += 10
        signals.append(
            f"nuls extérieur {away_stats['draw_pct']} %"
        )

    # Équipe ayant le meilleur taux de victoires dans son contexte.
    strongest_win_pct = max(
        home_stats["win_pct"],
        away_stats["win_pct"]
    )

    if strongest_win_pct >= 70:
        score += 25
        signals.append(
            f"une équipe gagne {strongest_win_pct} %"
        )

    elif strongest_win_pct >= 60:
        score += 20
        signals.append(
            f"une équipe gagne {strongest_win_pct} %"
        )

    elif strongest_win_pct >= 50:
        score += 15
        signals.append(
            f"une équipe gagne {strongest_win_pct} %"
        )

    elif strongest_win_pct >= 40:
        score += 10
        signals.append(
            f"une équipe gagne {strongest_win_pct} %"
        )

    # Bonus si les profils de victoires sont très différents.
    win_gap = abs(
        home_stats["win_pct"] - away_stats["win_pct"]
    )

    if win_gap >= 40:
        score += 10
        signals.append(
            f"écart de victoires important {win_gap:.1f} %"
        )

    elif win_gap >= 30:
        score += 5
        signals.append(
            f"écart de victoires {win_gap:.1f} %"
        )

    return score, signals, round(average_draw_pct, 1)


def get_winner_hint(home_team, away_team, home_stats, away_stats):
    """
    Indique l'équipe avec la meilleure fréquence de victoire
    dans son contexte domicile / extérieur.
    """
    home_name = home_team.get("name", "Équipe domicile")
    away_name = away_team.get("name", "Équipe extérieure")

    if home_stats["win_pct"] > away_stats["win_pct"]:
        return (
            f"Avantage statistique domicile : {home_name} "
            f"({home_stats['win_pct']} % de victoires à domicile)"
        )

    if away_stats["win_pct"] > home_stats["win_pct"]:
        return (
            f"Avantage statistique extérieur : {away_name} "
            f"({away_stats['win_pct']} % de victoires à l'extérieur)"
        )

    return "Aucun avantage clair de victoire"


def is_btts_favorable(home_stats, away_stats):
    """
    Retourne True si les indicateurs BTTS sont favorables.

    BTTS est seulement un tag informatif :
    il ne bloque pas la sélection faible risque de nul.
    """
    return (
        home_stats["btts_pct"] >= BTTS_HOME_MIN_PCT
        and away_stats["btts_pct"] >= BTTS_AWAY_MIN_PCT
        and home_stats["scored_pct"] >= BTTS_HOME_SCORED_MIN_PCT
        and away_stats["scored_pct"] >= BTTS_AWAY_SCORED_MIN_PCT
    )


# ============================================================
# ANALYSE DES MATCHS
# ============================================================

def to_paris_time(utc_date):
    """Convertit l'heure UTC de l'API vers l'heure de Paris."""
    utc_datetime = datetime.fromisoformat(
        utc_date.replace("Z", "+00:00")
    )

    return utc_datetime.astimezone(PARIS_TZ)


def analyze_match(match, cache):
    """
    Analyse une rencontre :

    Critères obligatoires :
    - nuls domicile <= MAX_HOME_DRAW_PCT ;
    - nuls extérieur <= MAX_AWAY_DRAW_PCT ;
    - moyenne de nuls <= MAX_AVERAGE_DRAW_PCT ;
    - au moins une équipe gagne >= MIN_WIN_PCT_ONE_TEAM.

    Le BTTS est affiché comme tag complémentaire.
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
            "[INFO] Pas assez de résultats comparables : "
            f"{home_team.get('name', 'Domicile')} "
            f"({home_stats['matches']} match(s)) / "
            f"{away_team.get('name', 'Extérieur')} "
            f"({away_stats['matches']} match(s))."
        )
        return None

    average_draw_pct = (
        home_stats["draw_pct"] + away_stats["draw_pct"]
    ) / 2

    low_draw_profile = (
        home_stats["draw_pct"] <= MAX_HOME_DRAW_PCT
        and away_stats["draw_pct"] <= MAX_AWAY_DRAW_PCT
        and average_draw_pct <= MAX_AVERAGE_DRAW_PCT
    )

    has_decisive_team = (
        home_stats["win_pct"] >= MIN_WIN_PCT_ONE_TEAM
        or away_stats["win_pct"] >= MIN_WIN_PCT_ONE_TEAM
    )

    score, signals, average_draw_pct = calculate_low_draw_score(
        home_stats,
        away_stats
    )

    btts_favorable = is_btts_favorable(
        home_stats,
        away_stats
    )

    btts_label = (
        "🔥 BTTS favorable"
        if btts_favorable
        else "⚪ BTTS non confirmé"
    )

    qualified = low_draw_profile and has_decisive_team

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
        "winner_hint": get_winner_hint(
            home_team,
            away_team,
            home_stats,
            away_stats
        ),
        "btts_label": btts_label,
        "qualified": qualified,
    }


# ============================================================
# MESSAGE DISCORD
# ============================================================

def short_stats(label, stats):
    """Construit une ligne courte de statistiques Discord."""
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
    """Construit l'affichage d'un match pour Discord."""
    signals = " • ".join(candidate["signals"])

    return [
        (
            f"**{prefix}{index}. "
            f"{candidate['home']} vs {candidate['away']} — "
            f"Score {candidate['score']}/100**"
        ),
        "🎯 Marché : 12 — une équipe gagne, pas de nul",
        f"📌 {candidate['winner_hint']}",
        candidate["btts_label"],
        (
            f"🏆 {candidate['competition']} | "
            f"🕒 {candidate['kickoff'].strftime('%d/%m %H:%M')} "
            "heure Paris"
        ),
        (
            f"📊 Nuls moyens : "
            f"{candidate['average_draw_pct']} %"
        ),
        short_stats("Domicile", candidate["home_stats"]),
        short_stats("Extérieur", candidate["away_stats"]),
        f"📈 Signaux : {signals}",
        "",
    ]


def build_discord_message(candidates, watchlist, dates):
    """Construit le rapport Discord final."""
    first_date = min(dates).strftime("%d/%m/%Y")
    last_date = max(dates).strftime("%d/%m/%Y")

    date_text = (
        first_date
        if first_date == last_date
        else f"{first_date} au {last_date}"
    )

    lines = [
        "📊 **Alerte Football — 12 faible risque de nul**",
        f"📅 Période analysée : {date_text}",
        "🎯 Marché : une équipe gagne, donc aucun nul.",
        (
            f"✅ Conditions : domicile ≤ {MAX_HOME_DRAW_PCT:.0f} % nuls | "
            f"extérieur ≤ {MAX_AWAY_DRAW_PCT:.0f} % nuls | "
            f"moyenne ≤ {MAX_AVERAGE_DRAW_PCT:.0f} %"
        ),
        (
            f"✅ Au moins une équipe : "
            f"≥ {MIN_WIN_PCT_ONE_TEAM:.0f} % de victoires"
        ),
        "",
    ]

    if candidates:
        lines.extend([
            "✅ **Profils validés — faible risque de nul**",
            "",
        ])

        for index, candidate in enumerate(candidates, start=1):
            lines.extend(
                candidate_lines(index, candidate)
            )

    else:
        lines.extend([
            "⚠️ **Aucun match ne respecte tous les critères anti-nul.**",
            "Les meilleurs matchs non validés sont affichés ci-dessous.",
            "",
        ])

    if watchlist:
        lines.extend([
            "👀 **Matchs à surveiller — non validés**",
            (
                "Ils ne respectent pas toutes les conditions contre le nul."
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
        "ℹ️ Vérifie les cotes, absences, rotations, blessures et compositions.",
        "Le filtre statistique ne garantit aucun résultat."
    ])

    return "\n".join(lines)


# ============================================================
# ENVOI DISCORD
# ============================================================

def split_message(message, max_length=1900):
    """Découpe le message pour rester sous la limite Discord."""
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
    """Envoie le rapport au webhook Discord."""
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError(
            "Le secret DISCORD_WEBHOOK_URL est manquant."
        )

    chunks = split_message(message)

    for number, chunk in enumerate(chunks[:5], start=1):
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json={
                "username": "Football Faible Nul Bot",
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
    """Lance l'analyse et envoie le résultat vers Discord."""
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

                status = (
                    "VALIDÉ"
                    if result["qualified"]
                    else "À SURVEILLER"
                )

                print(
                    f"[INFO] {status} | "
                    f"Score {result['score']}/100 | "
                    f"Nuls moyens {result['average_draw_pct']} % | "
                    f"{result['home']} vs {result['away']}"
                )

        except Exception as error:
            print(
                f"[WARN] Match ignoré : "
                f"{home_name} vs {away_name} — {error}"
            )

    analyzed_matches.sort(
        key=lambda item: (
            not item["qualified"],
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
        f"[INFO] Profils validés : {len(candidates)}"
    )

    print(
        f"[INFO] Matchs à surveiller : {len(watchlist)}"
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
