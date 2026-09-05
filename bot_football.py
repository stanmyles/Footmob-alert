import os
import datetime
import requests

# Configuration des API (récupérées depuis les secrets GitHub)
API_KEY = os.getenv("API_FOOTBALL_KEY")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK")

HEADERS = {
    'x-rapidapi-host': "://rapidapi.com",
    'x-rapidapi-key': API_KEY
}

def get_matches_today():
    today = datetime.date.today().isoformat()
    url = f"https://://rapidapi.com/v3/fixtures?date={today}"
    response = requests.get(url, headers=HEADERS).json()
    return response.get('response', [])

def check_h2h_last_2_years(team1_id, team2_id):
    url = f"https://://rapidapi.com/v3/fixtures/headtohead?h2h={team1_id}-{team2_id}"
    response = requests.get(url, headers=HEADERS).json()
    fixtures = response.get('response', [])
    
    two_years_ago = datetime.datetime.now() - datetime.timedelta(days=2*365)
    
    t1_wins = 0
    t2_wins = 0
    
    for f in fixtures:
        f_date = datetime.datetime.fromisoformat(f['fixture']['date'].replace('Z', '+00:00')).replace(tzinfo=None)
        if f_date >= two_years_ago:
            winner = f['teams']['home'] if f['fixtures']['winner']['home'] else f['teams']['away']
            if f['teams']['home']['id'] == team1_id and f['goals']['home'] > f['goals']['away']:
                t1_wins += 1
            elif f['teams']['home']['id'] == team1_id and f['goals']['home'] < f['goals']['away']:
                t2_wins += 1
            elif f['teams']['away']['id'] == team1_id and f['goals']['away'] > f['goals']['home']:
                t1_wins += 1
            elif f['teams']['away']['id'] == team1_id and f['goals']['away'] < f['goals']['home']:
                t2_wins += 1

    if t1_wins > t2_wins:
        return team1_id
    elif t2_wins > t1_wins:
        return team2_id
    return None

def get_team_standing(league_id, season, team_id):
    url = f"https://://rapidapi.com/v3/standings?league={league_id}&season={season}&team={team_id}"
    response = requests.get(url, headers=HEADERS).json()
    try:
        return response['response'][0]['league']['standings'][0][0]['rank']
    except (IndexError, KeyError):
        return None

def send_alert(message):
    if DISCORD_WEBHOOK_URL:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message})
    print(message)

def main():
    matches = get_matches_today()
    for match in matches:
        # Ignorer si le match n'a pas de ligue ou de classement disponible
        league_id = match['league']['id']
        season = match['league']['season']
        
        t1_id = match['teams']['home']['id']
        t1_name = match['teams']['home']['name']
        t2_id = match['teams']['away']['id']
        t2_name = match['teams']['away']['name']
        
        # 1. Vérification Dominance Historique (2 derniers ans)
        dominant_team_id = check_h2h_last_2_years(t1_id, t2_id)
        if not dominant_team_id:
            continue
            
        # 2. Vérification du classement
        rank1 = get_team_standing(league_id, season, t1_id)
        rank2 = get_team_standing(league_id, season, t2_id)
        
        if rank1 is None or rank2 is None:
            continue
            
        # Condition : L'équipe dominante à l'historique doit aussi être mieux classée (chiffre de rang plus petit)
        if (dominant_team_id == t1_id and rank1 < rank2) or (dominant_team_id == t2_id and rank2 < rank1):
            msg = f"⚽ Match Détecté : {t1_name} vs {t2_name}\n" \
                  f"🏆 Équipe dominante historique + mieux classée disponible."
            send_alert(msg)

if __name__ == "__main__":
    main()
