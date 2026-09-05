import os
import requests
from datetime import datetime

BASE_URL = "https://www.fotmob.com/api"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, /",
    "Referer": "https://www.fotmob.com/",
}

BIG_FORM_THRESHOLD = 4
RANK_GAP_THRESHOLD = 8


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

    response = requests.post(url, json=payload, timeout=20)
    response.raise_for_status()


def …
[15:28, 05/09/2026] L: import os
import requests
from datetime import datetime

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

    response = requests.post(url, json=payload, timeout=20)
    response.raise_for_status()

if _name_ == "_main_":
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    tg_send(f"✅ Test GitHub Actions + Telegram OK\nHeure: {now}")
