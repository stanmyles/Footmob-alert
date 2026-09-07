import os
import json
import csv
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BASE_URL = "https://api.football-data.org/v4"
ODDS_API_URL = "https://api.the-odds-api.com/v4"
TOKEN = os.environ["FOOTBALL_DATA_API_TOKEN"]
ODDS_API_KEY = os.environ["ODDS_API_KEY"]
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
DAYS_AHEAD = int(os.environ.get("DAYS_AHEAD", 5))

HEADERS_FD = {"X-Auth-Token": TOKEN}
HEADERS_ODDS = {"x-api-key": ODDS_API_KEY}

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

# ─── RÉCUPÉRATION DES COMPÉTITIONS ────────────────────────────────────────────
def get_competitions():
    r = requests.get(f"{BASE_URL}/competitions", headers=HEADERS_FD, timeout=30)
    r.raise_for_status()
    return [c["code"] for c in r.json().get("competitions", [])]

# ─── RÉCUPÉRATION DES MATCHS PAR COMPÉTITION ──────────────────────────────────
def get_matches_by_competition(competition_code, date_from, date_to):
    url = f"{BASE_URL}/competitions/{competition_code}/matches"
    params = {"dateFrom": date_from, "dateTo": date_to}
    r = requests.get(url, headers=HEADERS_FD, params=params, timeout=30)
    if r.status_code != 200:
        return []
    return r.json().get("matches", [])

# ─── RÉCUPÉRATION DES MATCHS DES 5 PROCHAINS JOURS ───────────────────────────
def get_upcoming_matches():
    now = datetime.now(timezone.utc)
    date_from = now.strftime("%Y-%m-%d")
    date_to = (now + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%d")

    competitions = get_competitions()
    all_matches = []
    for code in competitions:
        matches = get_matches_by_competition(code, date_from, date_to)
        all_matches.extend(matches)
    return all_matches

# ─── RÉCUPÉRATION DES 10 DERNIERS H2H ─────────────────────────────────────────
def get_h2h(match_id):
    url = f"{BASE_URL}/matches/{match_id}/head2head"
    params = {"limit": 10}
    r = requests.get(url, headers=HEADERS_FD, params=params, timeout=30)
    if r.status_code != 200:
        return []
    return r.json().get("matches", [])

# ─── ANALYSE DE LA DOMINANCE H2H (CORRIGÉE) ───────────────────────────────────
def analyze_h2h(h2h_matches, home_team, away_team):
    stats = {
        home_team: {"wins": 0, "draws": 0, "losses": 0},
        away_team: {"wins": 0, "draws": 0, "losses": 0}
    }

    for m in h2h_matches:
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        score = m.get("score", {}).get("fullTime", {})
        home_goals = score.get("home")
        away_goals = score.get("away")

        if home_goals is None or away_goals is None:
            continue

        # ✅ Gagnant basé sur le score réel, indépendamment du positionnement
        if home_goals > away_goals:
            winner = home
            loser = away
        elif away_goals > home_goals:
            winner = away
            loser = home
        else:
            winner = None  # Match nul
            loser = None

        # ✅ Attribution correcte peu importe domicile/extérieur
        for team in [home_team, away_team]:
            if team == winner:
                stats[team]["wins"] += 1
            elif team == loser:
                stats[team]["losses"] += 1
            elif team in [home, away]:  # Match nul
                stats[team]["draws"] += 1

    # Déterminer l'équipe dominante
    dominant_team = None
    if stats[home_team]["wins"] > stats[away_team]["wins"]:
        dominant_team = home_team
    elif stats[away_team]["wins"] > stats[home_team]["wins"]:
        dominant_team = away_team

    return stats, dominant_team

# ─── RÉCUPÉRATION DES COTES BOOKMAKER ─────────────────────────────────────────
def get_bookmaker_odds(home_team, away_team):
    url = f"{ODDS_API_URL}/sports/soccer/odds"
    params = {
        "regions": "eu",
        "markets": "h2h",
        "oddsFormat": "decimal"
    }
    r = requests.get(url, headers=HEADERS_ODDS, params=params, timeout=30)
    if r.status_code != 200:
        return None, None

    for event in r.json():
        if home_team.lower() in event.get("home_team", "").lower() and \
           away_team.lower() in event.get("away_team", "").lower():
            for bookmaker in event.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    if market["key"] == "h2h":
                        outcomes = {o["name"]: o["price"] for o in market["outcomes"]}
                        return outcomes.get(home_team), outcomes.get(away_team)
    return None, None

# ─── CALCUL VALUE BET ─────────────────────────────────────────────────────────
def calculate_value_bet(model_prob, bookmaker_odd):
    if not bookmaker_odd or model_prob <= 0:
        return None
    value = (model_prob * bookmaker_odd) - 1
    return round(value, 4)

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    upcoming = get_upcoming_matches()
    rows = []

    for m in upcoming:
        home = m["homeTeam"]["name"]
        away = m["awayTeam"]["name"]
        match_id = m["id"]
        utc_date = m["utcDate"]
        competition = m["competition"]["code"]

        # Format date + horaire
        dt = datetime.fromisoformat(utc_date.replace("Z", "+00:00"))
        date_str = dt.strftime("%d/%m/%Y %H:%M UTC")

        # Analyse H2H
        h2h_matches = get_h2h(match_id)
        stats, dominant_team = analyze_h2h(h2h_matches, home, away)

        # Probabilité modèle basée sur H2H
        home_wins = stats[home]["wins"]
        away_wins = stats[away]["wins"]
        total = home_wins + away_wins + stats[home]["draws"]
        p_home = home_wins / total if total > 0 else 0.5

        # Cotes bookmaker
        odd_home, odd_away = get_bookmaker_odds(home, away)

        # Value bet
        value_home = calculate_value_bet(p_home, odd_home)

        rows.append({
            "date": date_str,
            "competition": competition,
            "homeTeam": home,
            "awayTeam": away,
            "dominant_team": dominant_team or "Aucune",
            "home_wins_h2h": stats[home]["wins"],
            "home_draws_h2h": stats[home]["draws"],
            "home_losses_h2h": stats[home]["losses"],
            "away_wins_h2h": stats[away]["wins"],
            "away_draws_h2h": stats[away]["draws"],
            "away_losses_h2h": stats[away]["losses"],
            "p_home_model": round(p_home, 4),
            "odd_home_bookmaker": odd_home,
            "value_bet_home": value_home
        })

    # ─── EXPORT CSV ───────────────────────────────────────────────────────────
    csv_path = OUTPUT_DIR / "value_bets.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        if rows:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)

    # ─── EXPORT JSON ──────────────────────────────────────────────────────────
    json_path = OUTPUT_DIR / "value_bets.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    # ─── ENVOI DISCORD ────────────────────────────────────────────────────────
    if DISCORD_WEBHOOK_URL and rows:
        # Filtrer uniquement les matchs avec une équipe dominante
        dominant_rows = [r for r in rows if r["dominant_team"] != "Aucune"]
        top = dominant_rows[:10]

        if top:
            content = "🏆 **Matchs avec équipe dominante (H2H) :**\n\n"
            for r in top:
                content += (
                    f"📅 {r['date']} | {r['competition']}\n"
                    f"⚽ {r['homeTeam']} vs {r['awayTeam']}\n"
                    f"👑 Dominant : **{r['dominant_team']}**\n"
                    f"🏠 {r['homeTeam']} : {r['home_wins_h2h']}V / {r['home_draws_h2h']}N / {r['home_losses_h2h']}D\n"
                    f"✈️ {r['awayTeam']} : {r['away_wins_h2h']}V / {r['away_draws_h2h']}N / {r['away_losses_h2h']}D\n"
                    f"📊 Cote bookmaker : {r['odd_home_bookmaker']} | Value bet : {r['value_bet_home']}\n"
                    f"─────────────────────\n"
                )
            requests.post(
                DISCORD_WEBHOOK_URL,
                json={"content": content[:1900]},
                timeout=20
            )

if __name__ == "__main__":
    main()
