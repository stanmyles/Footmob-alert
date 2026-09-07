name: Value Bets Analysis

on:
  schedule:
    - cron: '0 6 * * *'  # Tous les jours à 6h UTC
  workflow_dispatch:      # Lancement manuel possible

jobs:
  run-value-bets:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout du code
        uses: actions/checkout@v4

      - name: Installation de Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Installation des dépendances
        run: pip install requests

      - name: Vérification des secrets
        run: |
          if [ -z "${FOOTBALL_DATA_API_TOKEN}" ]; then
            echo "FOOTBALL_DATA_API_TOKEN absent"
            exit 1
          fi
          if [ -z "${ODDS_API_KEY}" ]; then
            echo "ODDS_API_KEY absent"
            exit 1
          fi
        env:
          FOOTBALL_DATA_API_TOKEN: ${{ secrets.FOOTBALL_DATA_API_TOKEN }}
          ODDS_API_KEY: ${{ secrets.ODDS_API_KEY }}

      - name: Exécution du script
        env:
          FOOTBALL_DATA_API_TOKEN: ${{ secrets.FOOTBALL_DATA_API_TOKEN }}
          ODDS_API_KEY: ${{ secrets.ODDS_API_KEY }}
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
          DAYS_AHEAD: 5
        run: python scripts/value_bets.py

      - name: Sauvegarde des résultats
        uses: actions/upload-artifact@v4
        with:
          name: value-bets-output
          path: output/
