import os
import requests

url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

print("Webhook trouvé :", bool(url))
print("Début URL :", url[:35])

if not url:
    raise ValueError("DISCORD_WEBHOOK_URL est vide.")

response = requests.post(
    url + "?wait=true",
    json={"content": "TEST DISCORD — message envoyé depuis GitHub Actions"},
    timeout=20
)

print("Code Discord :", response.status_code)
print("Réponse Discord :", response.text)

response.raise_for_status()
