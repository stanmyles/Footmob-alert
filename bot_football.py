import os
import requests
from datetime import datetime, timezone, timedelta

API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "7"))
COMPETITIONS = [c.strip() for c in os.getenv("COMPETITIONS", "PL,PD,SA,BL1,FL1,UCL").split(",") if c.strip()]

BASE_URL = "https://api.football-data.org/v4"
HEADERS = {
    "X-Auth-Token": API_KEY
}

REQUEST_TIMEOUT = 30
MAX_MATCHES_SENT = 500


def api_get(path, params=None):
    url = f"{BASE_URL}{path}"
    response = requests.get(url, headers=HEADERS, params=params or {}, timeout=REQUEST_TIMEOUT)

    print("URL appelée :", response.url)
    print("Code API :", response.status_code)
    print("Réponse API :", response.text[:300])

    if response.status_code != 200:
        raise RuntimeError(f"Erreur API {response.status_code} sur {path} : {response.text[:300]}")

    return response.json()


def send_discord_message(content):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL manquant")

    chunks = split_message(content, 1900)

    for i, chunk in enumerate(chunks, start=1):
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": chunk},
            timeout=REQUEST_TIMEOUT
        )

        print(f"Discord status bloc {i} :", response.status_code)
        print("Discord response :", response.text[:300])

        if response.status_code not in (200, 204):
            raise RuntimeError(f"Erreur Discord {response.status_code} : {response.text[:300]}")


def split_message(text, max_len=1900):
    if len(text) <= max_len:
        return [text]

    parts = []
    current = ""

    for line in text.splitlines():
        if len(current) + len(line) + 1 <= max_len:
            current += line + "\n"
        else:
            if current.strip():
                parts.append(current.strip())
            current = line + "\n"

    if current.strip():
        parts.append(current.strip())

    return parts


def get_now_paris():
    return datetime.now(timezone.utc) + timedelta(hours=2)


def get_date_str(dt):
    return dt.strftime("%Y-%m-%d")


def format_match_time(utc_date_str):
    if not utc_date_str:
        return "Heure inconnue"

    try:
        dt = datetime.fromisoformat(utc_date_str.replace("Z", "+00:00"))
        dt_paris = dt.astimezone(timezone(timedelta(hours=2)))
        return dt_paris.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return utc_date_str


def get_matches_for_competition(code, start_date, end_date_exclusive):
    data = api_get(
        f"/competitions/{code}/matches",
        params={
            "dateFrom": start_date,
            "dateTo": end_date_exclusive
        }
    )
    return data.get("matches", [])


def get_all_matches():
    start_dt = get_now_paris()
    end_dt_exclusive = start_dt + timedelta(days=DAYS_AHEAD)

    start_date = get_date_str(start_dt)
    end_date_exclusive = get_date_str(end_dt_exclusive)
    display_end_date = get_date_str(end_dt_exclusive - timedelta(days=1))

    all_matches = []

    for code in COMPETITIONS:
        try:
            matches = get_matches_for_competition(code, start_date, end_date_exclusive)
            print(f"Competition {code} -> {len(matches)} matchs")
            all_matches.extend(matches)
        except Exception as e:
            print(f"Erreur competition {code} :", str(e))
            continue

    return start_date, display_end_date, all_matches


def parse_match(match):
    competition = match.get("competition", {})
    home = match.get("homeTeam", {})
    away = match.get("awayTeam", {})

    return {
        "competition_code": competition.get("code", "N/A"),
        "competition_name": competition.get("name", "Compétition inconnue"),
        "home_name": home.get("name", "Domicile"),
        "away_name": away.get("name", "Extérieur"),
        "utc_date": match.get("utcDate"),
        "status": match.get("status", "UNKNOWN"),
        "time": format_match_time(match.get("utcDate")),
    }


def build_message(start_date, end_date, matches):
    if not matches:
        return (
            f"📅 Matchs football du {start_date} au {end_date}\n\n"
            f"Aucun match récupéré sur les compétitions demandées."
        )

    parsed = [parse_match(m) for m in matches[:MAX_MATCHES_SENT]]
    parsed.sort(key=lambda x: (x["time"], x["competition_code"], x["home_name"]))

    lines = [
        f"📅 Matchs football du {start_date} au {end_date}",
        f"Compétitions : {', '.join(COMPETITIONS)}",
        f"Nombre total de matchs récupérés : {len(matches)}",
        ""
    ]

    for i, m in enumerate(parsed, start=1):
        lines.extend([
            f"{i}. {m['home_name']} vs {m['away_name']}",
            f"🏆 {m['competition_name']} ({m['competition_code']})",
            f"🕒 {m['time']}",
            f"📌 Statut : {m['status']}",
            ""
        ])

    if len(matches) > MAX_MATCHES_SENT:
        lines.append(f"Liste tronquée à {MAX_MATCHES_SENT} matchs.")

    return "\n".join(lines)


def main():
    if not API_KEY:
        raise RuntimeError("FOOTBALL_DATA_API_KEY manquant")
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL manquant")

    start_date, end_date, matches = get_all_matches()
    message = build_message(start_date, end_date, matches)
    send_discord_message(message)

    print("Message envoyé sur Discord.")
    print(f"Nombre total de matchs récupérés : {len(matches)}")


if __name__ == "__main__":
    main()
