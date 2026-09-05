import os
import requests
from datetime import datetime

BASE_URL = "https://www.fotmob.com/api"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, /",
    "Referer": "https://www.fotmob.com/",
}

BIG_FORM_THRESHOLD = 4      # 4 victoires ou 4 défaites récentes
RANK_GAP_THRESHOLD = 8      # écart de classement minimum


def tg_send(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise ValueError("TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }

    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()


def api_get(path: str, params=None):
    url = f"{BASE_URL}{path}"
    r = requests.get(url, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json()


def get_matches_today():
    today = datetime.now().strftime("%Y%m%d")
    data = api_get("/matches", {"date": today})
    matches = []

    for league in data.get("leagues", []):
        league_id = league.get("id")
        league_name = league.get("name")

        for match in league.get("matches", []):
            home = (match.get("home") or {}).get("name")
            away = (match.get("away") or {}).get("name")
            home_id = (match.get("home") or {}).get("id")
            away_id = (match.get("away") or {}).get("id")

            if not home or not away:
                continue

            matches.append({
                "match_id": match.get("id"),
                "league_id": league_id,
                "league_name": league_name,
                "home_team": home,
                "away_team": away,
                "home_id": home_id,
                "away_id": away_id,
            })

    return matches


def get_league_table(league_id):
    data = api_get("/leagues", {"id": league_id})

    if isinstance(data.get("table"), list):
        return data["table"]

    tables = data.get("tables", [])
    if tables and isinstance(tables[0], dict):
        if isinstance(tables[0].get("table"), list):
            return tables[0]["table"]
        if isinstance(tables[0].get("entries"), list):
            return tables[0]["entries"]

    return []


def find_rank(table, team_name):
    for row in table:
        row_name = (row.get("team") or {}).get("name") or row.get("name")
        if row_name and row_name.strip().lower() == team_name.strip().lower():
            return row.get("idx") or row.get("rank") or row.get("position")
    return None


def get_match_details(match_id):
    return api_get("/matchDetails", {"matchId": match_id})


def extract_recent_form(match_details, team_name):
    possible_paths = [
        ["content", "form", team_name],
        ["form"],
        ["content", "teamForm"],
    ]

    form_list = []

    for path in possible_paths:
        cur = match_details
        ok = True
        for p in path:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
            else:
                ok = False
                break
        if ok and isinstance(cur, list):
            form_list = cur
            break

    # Si la structure précise n'est pas trouvée, on renvoie vide
    wins = sum(1 for x in form_list if str(x).upper().startswith("W"))
    losses = sum(1 for x in form_list if str(x).upper().startswith("L"))

    return {
        "wins": wins,
        "losses": losses,
        "raw": form_list
    }


def build_alerts():
    alerts = []
    matches = get_matches_today()

    for m in matches:
        try:
            table = get_league_table(m["league_id"]) if m["league_id"] else []
            home_rank = find_rank(table, m["home_team"])
            away_rank = find_rank(table, m["away_team"])

            rank_gap = None
            indicator_2 = False
            if home_rank and away_rank:
                rank_gap = abs(int(home_rank) - int(away_rank))
                indicator_2 = rank_gap >= RANK_GAP_THRESHOLD

            details = get_match_details(m["match_id"])
            home_form = extract_recent_form(details, m["home_team"])
            away_form = extract_recent_form(details, m["away_team"])

            home_big = (
                home_form["wins"] >= BIG_FORM_THRESHOLD
                or home_form["losses"] >= BIG_FORM_THRESHOLD
            )
            away_big = (
                away_form["wins"] >= BIG_FORM_THRESHOLD
                or away_form["losses"] >= BIG_FORM_THRESHOLD
            )

            indicator_1 = home_big or away_big

            if indicator_1 or indicator_2:
                lines = [
                    f"⚽ <b>{m['home_team']} vs {m['away_team']}</b>",
                    f"Compétition : {m['league_name']}",
                ]

                if indicator_1:
                    lines.append("Indicateur 1 : forme marquée détectée")
                    lines.append(
                        f"{m['home_team']} forme: {home_form['raw']} | "
                        f"{m['away_team']} forme: {away_form['raw']}"
                    )

                if indicator_2:
                    lines.append(
                        f"Indicateur 2 : écart classement = {rank_gap} "
                        f"(rang {m['home_team']}={home_rank}, {m['away_team']}={away_rank})"
                    )

                alerts.append("\n".join(lines))

        except Exception as e:
            continue

    return alerts


if _name_ == "_main_":
    alerts = build_alerts()

    if not alerts:
        message = "Aucune alerte match détectée aujourd'hui."
    else:
        header = f"📊 Alertes FootMob du jour ({len(alerts)} match(s))\n\n"
        message = header + "\n\n--------------------\n\n".join(alerts)

    tg_send(message[:4000])
