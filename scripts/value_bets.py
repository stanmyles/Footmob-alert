import os
import math
import time
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

BASE_FD = "https://api.football-data.org/v4"
ODDS_BASE = "https://api.the-odds-api.com/v4"
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

FD_TOKEN = os.getenv("FOOTBALL_DATA_API_TOKEN", "")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

DAYS_AHEAD = int(float(os.getenv("DAYS_AHEAD", "7")))
BANKROLL = float(os.getenv("BANKROLL", "1000"))
MIN_EDGE_PCT = float(os.getenv("MIN_EDGE_PCT", "2"))
MIN_EV_PCT = float(os.getenv("MIN_EV_PCT", "1"))
H2H_YEARS = int(float(os.getenv("H2H_YEARS", "2")))
MAX_MATCHES = int(float(os.getenv("MAX_MATCHES", "150")))

FD_HEADERS = {"X-Auth-Token": FD_TOKEN} if FD_TOKEN else {}
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "github-actions-value-bets/1.2"})

COMPETITIONS = [
    "PL", "PD", "BL1", "SA", "FL1", "ELC", "PPL", "DED", "BSA", "CL", "WC"
]

ODDS_SPORTS_MAP = {
    "PL": "soccer_epl",
    "PD": "soccer_spain_la_liga",
    "BL1": "soccer_germany_bundesliga",
    "SA": "soccer_italy_serie_a",
    "FL1": "soccer_france_ligue_one",
    "ELC": "soccer_efl_champ",
    "PPL": "soccer_portugal_primeira_liga",
    "DED": "soccer_netherlands_eredivisie",
    "BSA": "soccer_brazil_campeonato",
    "CL": "soccer_uefa_champs_league",
    "WC": "soccer_fifa_world_cup"
}

ALIASES = {
    "paris saint-germain fc": "paris saint-germain",
    "psg": "paris saint-germain",
    "internazionale": "inter",
    "inter milan": "inter",
    "fc barcelona": "barcelona",
    "real madrid cf": "real madrid",
    "atletico de madrid": "atletico madrid",
    "manchester united fc": "manchester united",
    "manchester city fc": "manchester city",
    "tottenham hotspur fc": "tottenham",
    "newcastle united fc": "newcastle",
    "wolverhampton wanderers fc": "wolves",
    "olympique de marseille": "marseille",
    "olympique lyonnais": "lyon",
    "as monaco fc": "monaco",
    "sporting clube de portugal": "sporting cp",
}

def request_json(url, headers=None, params=None, timeout=30, retries=3, sleep_s=1.0):
    last_err = None
    for i in range(retries):
        try:
            r = SESSION.get(url, headers=headers, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(sleep_s * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            time.sleep(sleep_s * (i + 1))
    raise last_err

def poisson_pmf(k, lam):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)

def match_outcome_probs(home_xg, away_xg, max_goals=8):
    p_home = p_draw = p_away = 0.0
    for h in range(max_goals + 1):
        ph = poisson_pmf(h, home_xg)
        for a in range(max_goals + 1):
            pa = poisson_pmf(a, away_xg)
            p = ph * pa
            if h > a:
                p_home += p
            elif h == a:
                p_draw += p
            else:
                p_away += p
    total = p_home + p_draw + p_away
    return p_home / total, p_draw / total, p_away / total

def implied_prob(odds):
    if odds and odds > 1:
        return 1.0 / odds
    return None

def kelly_fraction(p, odds):
    b = odds - 1.0
    q = 1.0 - p
    if b <= 0:
        return 0.0
    f = ((b * p) - q) / b
    return max(0.0, f)

def normalize_team_name(s):
    s = (s or "").strip().lower()
    for token in [" fc", " cf", " club", " calcio", " futebol", " football club"]:
        s = s.replace(token, "")
    s = " ".join(s.split())
    return ALIASES.get(s, s)

def get_upcoming_matches():
    now = datetime.now(timezone.utc)
    date_from = now.date().isoformat()
    date_to = (now + timedelta(days=DAYS_AHEAD + 1)).date().isoformat()

    all_matches = []
    for comp in COMPETITIONS:
        try:
            data = request_json(
                f"{BASE_FD}/competitions/{comp}/matches",
                headers=FD_HEADERS,
                params={"dateFrom": date_from, "dateTo": date_to},
            )
            comp_matches = 0
            for m in data.get("matches", []):
                status = m.get("status")
                if status in {"SCHEDULED", "TIMED"}:
                    m["_competitionCode"] = comp
                    all_matches.append(m)
                    comp_matches += 1
            print(f"[INFO] {comp}: {comp_matches} match(s) retenu(s)")
        except Exception as e:
            print(f"[WARN] compétition {comp} ignorée: {e}")
            continue
    all_matches.sort(key=lambda x: x.get("utcDate", ""))
    return all_matches[:MAX_MATCHES]

def get_team_recent_matches(team_id, date_to, limit=8):
    try:
        data = request_json(
            f"{BASE_FD}/teams/{team_id}/matches",
            headers=FD_HEADERS,
            params={"dateTo": date_to, "status": "FINISHED", "limit": limit},
        )
        return data.get("matches", [])
    except Exception as e:
        print(f"[WARN] derniers matchs équipe {team_id}: {e}")
        return []

def summarize_recent_form(team_id, matches):
    pts = gf = ga = 0
    weighted_pts = weighted_gd = 0.0
    weights = [1.00, 0.92, 0.84, 0.76, 0.68, 0.60, 0.52, 0.44]
    count = 0
    for i, m in enumerate(matches[:8]):
        if m.get("score", {}).get("fullTime", {}).get("home") is None:
            continue
        home_id = m["homeTeam"]["id"]
        away_id = m["awayTeam"]["id"]
        hg = m["score"]["fullTime"]["home"]
        ag = m["score"]["fullTime"]["away"]
        if team_id == home_id:
            team_gf, team_ga = hg, ag
        else:
            team_gf, team_ga = ag, hg
        if team_gf > team_ga:
            p = 3
        elif team_gf == team_ga:
            p = 1
        else:
            p = 0
        w = weights[i] if i < len(weights) else 0.4
        pts += p
        gf += team_gf
        ga += team_ga
        weighted_pts += p * w
        weighted_gd += (team_gf - team_ga) * w
        count += 1
    if count == 0:
        return {
            "matches": 0, "ppg": 1.2, "gfpg": 1.2, "gapg": 1.2,
            "weighted_pts": 1.0, "weighted_gd": 0.0
        }
    return {
        "matches": count,
        "ppg": pts / count,
        "gfpg": gf / count,
        "gapg": ga / count,
        "weighted_pts": weighted_pts / count,
        "weighted_gd": weighted_gd / count,
    }

def get_h2h(match_id):
    date_from = (datetime.now(timezone.utc) - timedelta(days=365 * H2H_YEARS)).date().isoformat()
    try:
        data = request_json(
            f"{BASE_FD}/matches/{match_id}/head2head",
            headers=FD_HEADERS,
            params={"limit": 10, "dateFrom": date_from},
        )
        return data
    except Exception as e:
        print(f"[WARN] H2H match {match_id}: {e}")
        return {}

def extract_h2h_advantage(h2h_data, home_name, away_name):
    matches = h2h_data.get("matches", []) if isinstance(h2h_data, dict) else []
    home_wins = away_wins = draws = 0
    years = {}
    recent_scores = []
    for m in matches:
        score = m.get("score", {}).get("fullTime", {})
        if score.get("home") is None:
            continue
        hn = m.get("homeTeam", {}).get("name", "")
        an = m.get("awayTeam", {}).get("name", "")
        hg = score.get("home", 0)
        ag = score.get("away", 0)
        year = (m.get("utcDate", "")[:4] or "unknown")
        years.setdefault(year, {"home_ref_wins": 0, "away_ref_wins": 0, "draws": 0})
        recent_scores.append({
            "utcDate": m.get("utcDate"),
            "home": hn,
            "away": an,
            "score": f"{hg}-{ag}"
        })
        if hg == ag:
            draws += 1
            years[year]["draws"] += 1
        else:
            winner = hn if hg > ag else an
            if normalize_team_name(winner) == normalize_team_name(home_name):
                home_wins += 1
                years[year]["home_ref_wins"] += 1
            elif normalize_team_name(winner) == normalize_team_name(away_name):
                away_wins += 1
                years[year]["away_ref_wins"] += 1
    total = home_wins + away_wins + draws
    bias = 0.0 if total == 0 else (home_wins - away_wins) / total
    return {
        "h2h_matches": total,
        "h2h_home_wins": home_wins,
        "h2h_away_wins": away_wins,
        "h2h_draws": draws,
        "h2h_bias": bias,
        "h2h_by_year": years,
        "recent_h2h_scores": recent_scores[:5],
    }

def estimate_probs(match):
    utc_date = match["utcDate"]
    home = match["homeTeam"]
    away = match["awayTeam"]
    home_recent = get_team_recent_matches(home["id"], utc_date, limit=8)
    away_recent = get_team_recent_matches(away["id"], utc_date, limit=8)
    home_form = summarize_recent_form(home["id"], home_recent)
    away_form = summarize_recent_form(away["id"], away_recent)

    base_home_xg = 1.35
    base_away_xg = 1.10
    home_xg = base_home_xg \
        + 0.22 * (home_form["ppg"] - 1.3) \
        + 0.18 * (home_form["gfpg"] - away_form["gapg"]) \
        + 0.10 * (home_form["weighted_gd"] - away_form["weighted_gd"])
    away_xg = base_away_xg \
        + 0.18 * (away_form["ppg"] - 1.3) \
        + 0.16 * (away_form["gfpg"] - home_form["gapg"]) \
        + 0.08 * (away_form["weighted_gd"] - home_form["weighted_gd"])

    h2h = get_h2h(match["id"])
    h2h_summary = extract_h2h_advantage(h2h, home["name"], away["name"])
    home_xg += max(-0.20, min(0.20, h2h_summary["h2h_bias"] * 0.20))
    away_xg -= max(-0.15, min(0.15, h2h_summary["h2h_bias"] * 0.15))

    home_xg = max(0.2, min(3.2, home_xg))
    away_xg = max(0.2, min(3.0, away_xg))

    p_home, p_draw, p_away = match_outcome_probs(home_xg, away_xg)
    return {
        "home_xg": home_xg,
        "away_xg": away_xg,
        "p_home": p_home,
        "p_draw": p_draw,
        "p_away": p_away,
        **home_form,
        **{f"away_{k}": v for k, v in away_form.items()},
        **h2h_summary,
    }

def get_market_odds_for_match(match):
    if not ODDS_API_KEY:
        return {}
    sport_key = ODDS_SPORTS_MAP.get(match.get("_competitionCode"))
    if not sport_key:
        return {}
    try:
        events = request_json(
            f"{ODDS_BASE}/sports/{sport_key}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "eu",
                "markets": "h2h",
                "oddsFormat": "decimal"
            },
            timeout=40,
            retries=2,
        )
    except Exception as e:
        print(f"[WARN] odds indisponibles pour {sport_key}: {e}")
        return {}

    home_name = normalize_team_name(match["homeTeam"]["name"])
    away_name = normalize_team_name(match["awayTeam"]["name"])
    best = {"home": None, "draw": None, "away": None, "bookmaker": None}

    for ev in events:
        ev_home = normalize_team_name(ev.get("home_team", ""))
        ev_away = normalize_team_name(ev.get("away_team", ""))
        if {ev_home, ev_away} != {home_name, away_name}:
            continue

        reverse = (ev_home == away_name and ev_away == home_name)

        for bk in ev.get("bookmakers", []):
            for market in bk.get("markets", []):
                if market.get("key") != "h2h":
                    continue
                local = {"home": None, "draw": None, "away": None}
                for o in market.get("outcomes", []):
                    nm = normalize_team_name(o.get("name", ""))
                    price = o.get("price")
                    if not reverse:
                        if nm == home_name:
                            local["home"] = price
                        elif nm == away_name:
                            local["away"] = price
                    else:
                        if nm == away_name:
                            local["home"] = price
                        elif nm == home_name:
                            local["away"] = price
                    if nm in {"draw", "tie", "match nul", "nul"}:
                        local["draw"] = price
                for key in ["home", "draw", "away"]:
                    if local[key] and (best[key] is None or local[key] > best[key]):
                        best[key] = local[key]
                        best["bookmaker"] = bk.get("title", "")
        break
    return best

def normalize_probs_from_odds(home_odds, draw_odds, away_odds):
    probs = [implied_prob(home_odds), implied_prob(draw_odds), implied_prob(away_odds)]
    if any(p is None for p in probs):
        return (None, None, None, None)
    s = sum(probs)
    return probs[0], probs[1], probs[2], s

def fractional_kelly_stake(bankroll, p, odds, fraction=0.25):
    k = kelly_fraction(p, odds)
    return round(bankroll * k * fraction, 2)

def build_rows(matches):
    rows = []
    odds_found = 0
    for m in matches:
        try:
            est = estimate_probs(m)
            odds = get_market_odds_for_match(m)
            home_odds = odds.get("home")
            draw_odds = odds.get("draw")
            away_odds = odds.get("away")
            if home_odds and draw_odds and away_odds:
                odds_found += 1
            ip_home, ip_draw, ip_away, overround = normalize_probs_from_odds(home_odds, draw_odds, away_odds)
            fair_market_home = (ip_home / overround) if ip_home and overround else None
            fair_market_draw = (ip_draw / overround) if ip_draw and overround else None
            fair_market_away = (ip_away / overround) if ip_away and overround else None

            ev_home = (est["p_home"] * home_odds - 1) if home_odds else None
            ev_draw = (est["p_draw"] * draw_odds - 1) if draw_odds else None
            ev_away = (est["p_away"] * away_odds - 1) if away_odds else None

            edge_home = (est["p_home"] - fair_market_home) * 100 if fair_market_home is not None else None
            edge_draw = (est["p_draw"] - fair_market_draw) * 100 if fair_market_draw is not None else None
            edge_away = (est["p_away"] - fair_market_away) * 100 if fair_market_away is not None else None

            selections = []
            for side, p, od, ev, edge in [
                ("HOME", est["p_home"], home_odds, ev_home, edge_home),
                ("DRAW", est["p_draw"], draw_odds, ev_draw, edge_draw),
                ("AWAY", est["p_away"], away_odds, ev_away, edge_away),
            ]:
                if od and ev is not None and edge is not None:
                    if ev * 100 >= MIN_EV_PCT and edge >= MIN_EDGE_PCT:
                        selections.append((side, p, od, ev, edge))
            best_side = max(selections, key=lambda x: (x[3], x[4])) if selections else None

            row = {
                "utcDate": m["utcDate"],
                "competition": m.get("competition", {}).get("code", m.get("_competitionCode", "")),
                "status": m.get("status"),
                "homeTeam": m["homeTeam"]["name"],
                "awayTeam": m["awayTeam"]["name"],
                "home_xg": round(est["home_xg"], 3),
                "away_xg": round(est["away_xg"], 3),
                "p_home_model": round(est["p_home"] * 100, 2),
                "p_draw_model": round(est["p_draw"] * 100, 2),
                "p_away_model": round(est["p_away"] * 100, 2),
                "odds_home": home_odds,
                "odds_draw": draw_odds,
                "odds_away": away_odds,
                "market_prob_home_raw": round(ip_home * 100, 2) if ip_home else None,
                "market_prob_draw_raw": round(ip_draw * 100, 2) if ip_draw else None,
                "market_prob_away_raw": round(ip_away * 100, 2) if ip_away else None,
                "market_prob_home_fair": round(fair_market_home * 100, 2) if fair_market_home is not None else None,
                "market_prob_draw_fair": round(fair_market_draw * 100, 2) if fair_market_draw is not None else None,
                "market_prob_away_fair": round(fair_market_away * 100, 2) if fair_market_away is not None else None,
                "overround_pct": round((overround - 1) * 100, 2) if overround else None,
                "edge_home_pct": round(edge_home, 2) if edge_home is not None else None,
                "edge_draw_pct": round(edge_draw, 2) if edge_draw is not None else None,
                "edge_away_pct": round(edge_away, 2) if edge_away is not None else None,
                "ev_home_pct": round(ev_home * 100, 2) if ev_home is not None else None,
                "ev_draw_pct": round(ev_draw * 100, 2) if ev_draw is not None else None,
                "ev_away_pct": round(ev_away * 100, 2) if ev_away is not None else None,
                "home_ppg": round(est["ppg"], 3),
                "home_gfpg": round(est["gfpg"], 3),
                "home_gapg": round(est["gapg"], 3),
                "away_ppg": round(est["away_ppg"], 3),
                "away_gfpg": round(est["away_gfpg"], 3),
                "away_gapg": round(est["away_gapg"], 3),
                "h2h_matches": est["h2h_matches"],
                "h2h_home_wins": est["h2h_home_wins"],
                "h2h_away_wins": est["h2h_away_wins"],
                "h2h_draws": est["h2h_draws"],
                "best_bookmaker": odds.get("bookmaker"),
                "recommended_side": best_side[0] if best_side else None,
                "recommended_odds": best_side[2] if best_side else None,
                "recommended_prob_pct": round(best_side[1] * 100, 2) if best_side else None,
                "recommended_ev_pct": round(best_side[3] * 100, 2) if best_side else None,
                "recommended_edge_pct": round(best_side[4], 2) if best_side else None,
                "recommended_stake_qk": fractional_kelly_stake(BANKROLL, best_side[1], best_side[2], 0.25) if best_side else 0,
                "h2h_by_year": json.dumps(est["h2h_by_year"], ensure_ascii=False),
                "recent_h2h_scores": json.dumps(est["recent_h2h_scores"], ensure_ascii=False),
            }
            rows.append(row)
        except Exception as e:
            rows.append({
                "utcDate": m.get("utcDate"),
                "competition": m.get("competition", {}).get("code", m.get("_competitionCode", "")),
                "status": m.get("status"),
                "homeTeam": m.get("homeTeam", {}).get("name"),
                "awayTeam": m.get("awayTeam", {}).get("name"),
                "error": str(e),
            })
    print(f"[INFO] Matchs avec cotes trouvées: {odds_found}/{len(matches)}")
    return rows

def send_discord(text):
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL manquant")
        return
    if not text or not text.strip():
        print("Message Discord vide")
        return
    chunks = [text[i:i+1900] for i in range(0, len(text), 1900)]
    for idx, c in enumerate(chunks[:4], start=1):
        try:
            r = requests.post(DISCORD_WEBHOOK_URL, json={"content": c}, timeout=20)
            print(f"Discord chunk {idx}: HTTP {r.status_code}")
            if r.status_code >= 400:
                print(r.text)
        except Exception as e:
            print(f"Erreur Discord chunk {idx}: {e}")

def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not text.strip():
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [text[i:i+3900] for i in range(0, len(text), 3900)]
    for c in chunks[:4]:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": c}, timeout=20)

def render_message(df):
    if df.empty:
        return "Aucun match récupéré sur la fenêtre choisie."

    lines = ["📊 *Confrontations directes à venir*"]

    preview = df.head(10)
    for r in preview.itertuples():
        h2h_total = getattr(r, "h2h_matches", 0) or 0
        home_wins = getattr(r, "h2h_home_wins", 0) or 0
        away_wins = getattr(r, "h2h_away_wins", 0) or 0
        draws = getattr(r, "h2h_draws", 0) or 0

        if h2h_total == 0:
            trend = "⚪ Pas d'historique exploitable"
        elif abs(home_wins - away_wins) >= 4:
            trend = "🔥 Domination nette"
        elif abs(home_wins - away_wins) >= 2:
            trend = "📈 Léger avantage"
        else:
            trend = "⚖️ Confrontation équilibrée"

        lines.append("")
        lines.append(f"⚽ *{r.homeTeam} vs {r.awayTeam}*")
        lines.append(f"✅ {r.homeTeam} : {home_wins} victoire(s)")
        lines.append(f"🤝 Nuls : {draws}")
        lines.append(f"❌ {r.awayTeam} : {away_wins} victoire(s)")
        lines.append(f"📅 {h2h_total} confrontation(s) analysée(s)")
        lines.append(f"{trend}")

    with_odds = df[df['odds_home'].notna()].copy() if 'odds_home' in df.columns else pd.DataFrame()
    if not with_odds.empty:
        lines.append("")
        lines.append("💸 *Matchs avec cotes trouvées*")
        best = with_odds.head(5)
        for r in best.itertuples():
            lines.append(
                f"• {r.homeTeam} vs {r.awayTeam} | H/D/A = {r.odds_home}/{r.odds_draw}/{r.odds_away}"
            )
    else:
        lines.append("")
        lines.append("⚠️ Aucune cote trouvée pour les matchs listés.")

    return "\n".join(lines)

def main():
    if not FD_TOKEN:
        raise RuntimeError("FOOTBALL_DATA_API_TOKEN manquant")

    matches = get_upcoming_matches()
    print(f"[INFO] {len(matches)} matchs récupérés")
    for m in matches[:10]:
        print(
            f"[MATCH] {m.get('utcDate')} | {m.get('competition', {}).get('code', m.get('_competitionCode'))} | "
            f"{m.get('status')} | {m.get('homeTeam', {}).get('name')} vs {m.get('awayTeam', {}).get('name')}"
        )

    rows = build_rows(matches)
    df = pd.DataFrame(rows)

    csv_all = OUTPUT_DIR / "value_bets_all_matches.csv"
    csv_picks = OUTPUT_DIR / "value_bets_recommended.csv"
    json_all = OUTPUT_DIR / "value_bets_all_matches.json"
    txt_msg = OUTPUT_DIR / "notification_message.txt"

    df.to_csv(csv_all, index=False)
    df.to_json(json_all, orient="records", force_ascii=False, indent=2)

    picks = df[df["recommended_side"].notna()].copy() if "recommended_side" in df.columns else pd.DataFrame()
    if not picks.empty:
        picks = picks.sort_values(["recommended_ev_pct", "recommended_edge_pct"], ascending=False)
    picks.to_csv(csv_picks, index=False)

    message = render_message(df)
    txt_msg.write_text(message, encoding="utf-8")
    print(message)

    send_discord(message)
    send_telegram(message)

if __name__ == "__main__":
    main()
