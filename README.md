# LoL Analyzer API

Questa API riceve l'URL di una partita di League of Legends e restituisce
un'analisi con IA (GPT-4) per aiutare il giocatore a migliorare.

## Endpoint

**POST** `/analizar`  
Body:
```json
{
  "match_url": "https://link-alla-partita"
}
