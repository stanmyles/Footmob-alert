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

# Analyse aujourd'hui + les DAYS_AHEAD jours suivants.
# DAYS_AHEAD = 1 = aujourd'hui et demain.
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "1"))

# Nombre de résultats récents comparables par équipe.
# Domicile pour l'équipe qui reçoit, extérieur pour l'adversaire.
LOOKBACK_MATCHES = int(os.getenv("LOOKBACK_MATCHES", "10"))

# Minimum de matchs comparables réellement exploitables.
MIN_COMPARABLE_MATCHES = int(
    os.getenv("MIN_COMPARABLE_MATCHES", "5")
)

# Seuil principal conseillé avec ce nouveau score.
# 70 = alertes intéressantes.
# 75 = plus sélectif.
# 80 = très strict et donc peu d'alertes.
MIN_SCORE = int(os.getenv("MIN_SCORE", "70"))

# Nombre maximal de matchs retenus dans le message Discord.
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "6"))

# Nombre de matchs "à surveiller" affichés sous le seuil.
MAX_WATCHLIST = int(os.getenv("MAX_WATCHLIST", "3"))

# Score minimal pour la liste "à surveiller".
WATCHLIST_MIN_SCORE = int(
    os.getenv("WATCHLIST_MIN_SCORE", "60")
)

# Facultatif, utile pour le test manuel GitHub Actions.
# Exemple : 2026-09-15
TARGET_DATE = os.getenv("TARGET_DATE", "").strip()

# Intervalle minimum entre deux appels football-data.org.
FD_MIN_INTERVAL = float(os.getenv("FD_MIN_INTERVAL", "6.5"))
last_api_call = 0.0

session = requests.Session()
session.headers.update({
    "User-Agent": "github-actions-football-12-btts/2.0",
    "Accept": "application/json",
})


# ============================================================
# OUTILS API FOOTBALL-DATA.ORG
# ============================================================

def throttle():
    """Respecte l'intervalle minimal entre les appels API."""
    global last_api_call

    now = time.time()
    wait = FD_MIN_INTERVAL - (now - last_api_call)

    if wait > 0:
        time.sleep(wait)

    last_api_call = time.time()


def api_get(endpoint, params=None, retries=3):
    """
    Exécute une requête GET vers football-data.org avec :
    - token X-Auth-Token ;
    - limitation volontaire des appels ;
    - reprise sur erreur réseau ;
    - reprise spécifique après HTTP 429.
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

                delay = (
                    int(retry_after)
                    if retry_after.isdigit()
                    else (attempt + 1) * 20
                )

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
    Retourne les dates à analyser.

    TARGET_DATE :
    - renseigné : analyse seulement cette date ;
    - vide : analyse aujourd'hui et les jours suivants.
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
                "TARGET_DATE doit respecter le format YYYY-MM-DD."
            ) from error

    today = datetime.now(PARIS_TZ).date()

    return [
        today + timedelta(days=day)
        for day in range(DAYS_AHEAD + 1)
    ]


def get_available_competitions():
    """Récupère les compétitions accessibles avec le token API."""
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
    Récupère les matchs SCHEDULED ou TIMED pour la période choisie.
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

    matches.sort(key=lambda match: match.get("utcDate", ""))

    return matches


def get_team_finished_matches(team_id, venue):
    """
    Récupère les derniers matchs terminés comparables.

    venue='HOME' : seulement les matchs à domicile.
    venue='AWAY' : seulement les matchs à l'extérieur.
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
# STATISTIQUES EQUIPE
# ============================================================

def get_full_time_goals(match):
    """Extrait le score final d'un match ou renvoie (None, None)."""
    full_time = match.get("score", {}).get("fullTime", {})

    home_goals = full_time.get("home")
    away_goals = full_time.get("away")

    if home_goals is None or away_goals is None:
        return None, None

    return home_goals, away_goals


def team_stats(matches, team_id):
    """
    Calcule les statistiques d'une équipe sur ses matchs comparables :

    - BTTS ;
    - équipe qui marque ;
    - équipe qui encaisse ;
    - victoire, nul, défaite ;
    - moyenne de buts marqués et encaissés.

    Les matchs fournis sont déjà filtrés HOME ou AWAY.
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
# SCORE 1 + BTTS / 2 + BTTS
# ============================================================

def calculate_side_score(
    winner_stats,
    opponent_stats,
    winner_side
):
    """
    Calcule un score pour une équipe susceptible de gagner,
    avec BTTS Oui.

    winner_stats : statistiques de l'équipe censée gagner.
    opponent_stats : statistiques de l'adversaire.
    winner_side : 'HOME' ou 'AWAY'.
    """
    score = 0
    signals = []

    side_label = "domicile" if winner_side == "HOME" else "extérieur"
    opponent_label = (
        "extérieur" if winner_side == "HOME" else "domicile"
    )

    # --------------------------------------------------------
    # 1. L'équipe supposée gagner doit marquer régulièrement.
    # --------------------------------------------------------
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
            f"{side_label} {winner_stats['goals_for_avg']} but(s) marqué(s)"
        )

    elif winner_stats["goals_for_avg"] >= 1.30:
        score += 4
        signals.append(
            f"{side_label} {winner_stats['goals_for_avg']} but(s) marqué(s)"
        )

    # --------------------------------------------------------
    # 2. L'adversaire doit avoir une vraie capacité à marquer.
    # Cela protège la partie BTTS Oui.
    # --------------------------------------------------------
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
            f"{opponent_label} "
            f"{opponent_stats['goals_for_avg']} but(s) marqué(s)"
        )

    # --------------------------------------------------------
    # 3. BTTS observé dans les historiques comparables.
    # --------------------------------------------------------
    if winner_stats["btts_pct"] >= 60:
        score += 8
        signals.append(
            f"BTTS {side_label} {winner_stats['btts_pct']} %"
        )

    if opponent_stats["btts_pct"] >= 55:
        score += 8
        signals.append(
            f"BTTS {opponent_label} {opponent_stats['btts_pct']} %"
        )

    # --------------------------------------------------------
    # 4. L'adversaire doit concéder suffisamment.
    # --------------------------------------------------------
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
            f"{opponent_label} "
            f"{opponent_stats['goals_against_avg']} but(s) encaissé(s)"
        )

    # --------------------------------------------------------
    # 5. Signal victoire pour l'équipe choisie.
    # --------------------------------------------------------
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

    # L'adversaire perd régulièrement dans le même contexte.
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

    # --------------------------------------------------------
    # 6. Protection contre les nuls.
    # --------------------------------------------------------
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
    Compare les deux scénarios :

    - 1 + BTTS Oui : équipe domicile gagne + BTTS Oui.
    - 2 + BTTS Oui : équipe extérieure gagne + BTTS Oui.

    Le script conserve le sens ayant le meilleur score.
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
# ANALYSE DES MATCHS
# ============================================================

def to_paris_time(utc_date):
    """Convertit une date UTC football-data.org vers Paris."""
    utc_datetime = datetime.fromisoformat(
        utc_date.replace("Z", "+00:00")
    )

    return utc_datetime.astimezone(PARIS_TZ)


def analyze_match(match, cache):
    """
    Analyse une affiche et retourne son meilleur profil :

    - 1 + BTTS Oui ;
    - ou 2 + BTTS Oui.

    Retourne un résultat même sous MIN_SCORE afin de permettre
    l'affichage des matchs "à surveiller".
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
            f"({home_stats['matches']}) / "
            f"{away_team.get('name', 'Extérieur')} "
            f"({away_stats['matches']})."
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
    """Construit une ligne courte de statistiques pour Discord."""
    return (
        f"**{label}** ({stats['matches']} matchs) — "
        f"V {stats['win_pct']} % | "
        f"N {stats['draw_pct']} % | "
        f"D {stats['loss_pct']} % | "
        f"BTTS {stats['btts_pct']} % | "
        f"marque {stats['scored_pct']} % | "
        f"encaisse {stats['conceded_pct']} %"
    )


def candidate_lines(index, candidate, title_prefix=""):
    """Construit les lignes Discord d'un match analysé."""
    signals = " • ".join(candidate["signals"])

    return [
        (
            f"**{title_prefix}{index}. "
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
            f"📊 Comparaison : 1 + BTTS {candidate['home_score']}/100 | "
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
    Construit le message Discord.

    candidates :
    - score >= MIN_SCORE ;
    - vrais candidats statistiques.

    watchlist :
    - score inférieur à MIN_SCORE ;
    - mais score >= WATCHLIST_MIN_SCORE ;
    - affichés seulement pour suivi, pas comme sélection validée.
    """
    first_date = min(dates).strftime("%d/%m/%Y")
    last_date = max(dates).strftime("%d/%m/%Y")

    if first_date == last_date:
        date_text = first_date
    else:
        date_text = f"{first_date} au {last_date}"

    if not candidates and not watchlist:
        return (
            "⚠️ **Alerte Football — 1/2 + BTTS Oui**\n"
            f"📅 Période analysée : {date_text}\n\n"
            "Aucun match ne présente un profil statistique suffisant.\n"
            "Ne pas forcer une sélection est une bonne décision.\n\n"
            "ℹ️ Le score est un filtre statistique : "
            "il ne garantit aucun résultat."
        )

    lines = [
        "📊 **Alerte Football — 1/2 + BTTS Oui**",
        f"📅 Période analysée : {date_text}",
        (
            "🎯 Marché analysé : une équipe précise gagne "
            "+ les deux équipes marquent."
        ),
        f"✅ Seuil principal : {MIN_SCORE}/100",
        "",
    ]

    if candidates:
        lines.append("✅ **Profils retenus**")
        lines.append("")

        for index, candidate in enumerate(candidates, start=1):
            lines.extend(candidate_lines(index, candidate))

    else:
        lines.extend([
            "⚠️ **Aucun profil ne dépasse le seuil principal.**",
            "",
        ])

    if watchlist:
        lines.extend([
            "👀 **Matchs à surveiller — non validés**",
            (
                f"Scores entre {WATCHLIST_MIN_SCORE}/100 "
                f"et {MIN_SCORE - 1}/100."
            ),
            "Ils demandent une vérification renforcée avant toute décision.",
            "",
        ])

        for index, candidate in enumerate(watchlist, start=1):
            lines.extend(
                candidate_lines(index, candidate, title_prefix="👀 ")
            )

    lines.extend([
        "ℹ️ Vérifie impérativement les cotes, absences, rotations, "
        "blessures et compositions avant le coup d'envoi.",
        "Le filtre statistique ne garantit aucun résultat."
    ])

    return "\n".join(lines)


def split_message(message, max_length=1900):
    """
    Découpe proprement un message Discord sans couper un mot
    lorsque cela est possible.
    """
    chunks = []
    remaining = message.strip()

    while len(remaining) > max_length:
        split_at = remaining.rfind("\n", 0, max_length)

        if split_at < max_length // 2:
            split_at = remaining.rfind(" ", 0, max_length)

        if split_at <= 0:
            split_at = max_length

        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks


def send_to_discord(message):
    """Envoie le message Discord, éventuellement en plusieurs parties."""
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
                    f"[INFO] Résultat : {result['score']}/100 — "
                    f"{result['pick']}"
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
        item for item in analyzed_matches
        if item["qualified"]
    ][:MAX_CANDIDATES]

    watchlist = [
        item for item in analyzed_matches
        if (
            not item["qualified"]
            and item["score"] >= WATCHLIST_MIN_SCORE
        )
    ][:MAX_WATCHLIST]

    print(
        f"[INFO] Matchs analysés avec données suffisantes : "
        f"{len(analyzed_matches)}"
    )

    print(
        f"[INFO] Candidats retenus (>= {MIN_SCORE}) : "
        f"{len(candidates)}"
    )

    print(
        f"[INFO] Matchs à surveiller "
        f"({WATCHLIST_MIN_SCORE}-{MIN_SCORE - 1}) : "
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
