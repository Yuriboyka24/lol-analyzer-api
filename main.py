from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
from urllib.parse import urlparse
import requests, os, re, json

# -------------------- Config --------------------
RIOT_TOKEN = os.getenv("RIOT_API_KEY")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

if not RIOT_TOKEN:
    print("[WARN] RIOT_API_KEY non impostata: /resolve e /analizar falliranno sulle chiamate Riot.")

RIOT_HEADERS = {"X-Riot-Token": RIOT_TOKEN} if RIOT_TOKEN else {}

app = FastAPI(title="LoL Analyzer API", version="1.0.0")

# CORS per poter chiamare l'API dal frontend Shopify
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restringi al tuo dominio Shopify se vuoi
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------- Models --------------------
class MatchRequest(BaseModel):
    match_url: str  # può essere EUW1_123... oppure un link OP.GG

class RiotId(BaseModel):
    game_name: str   # es: "yuriboyka"
    tag_line: str    # es: "3436"
    count: int = 5

# -------------------- Helpers Riot --------------------
def riot_get_puuid(game_name: str, tag_line: str) -> Optional[str]:
    """Riot ID -> PUUID (routing EU per EUW)."""
    if not RIOT_TOKEN:
        return None
    url = f"https://europe.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
    r = requests.get(url, headers=RIOT_HEADERS, timeout=10)
    if r.status_code == 200:
        return r.json().get("puuid")
    print(f"[RIOT] account lookup fail {r.status_code}: {r.text}")
    return None

def riot_get_recent_match_ids(puuid: str, count: int = 10) -> Optional[List[str]]:
    """PUUID -> lista matchId (Match-V5)."""
    if not RIOT_TOKEN:
        return None
    url = f"https://europe.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count}"
    r = requests.get(url, headers=RIOT_HEADERS, timeout=10)
    if r.status_code == 200:
        return r.json()
    print(f"[RIOT] ids fail {r.status_code}: {r.text}")
    return None

def riot_get_match(match_id: str) -> Optional[dict]:
    """Dettaglio partita da matchId."""
    if not RIOT_TOKEN:
        return None
    url = f"https://europe.api.riotgames.com/lol/match/v5/matches/{match_id}"
    r = requests.get(url, headers=RIOT_HEADERS, timeout=15)
    if r.status_code == 200:
        return r.json()
    print(f"[RIOT] match fail {r.status_code}: {r.text}")
    return None

# -------------------- Parsing OP.GG / matchId --------------------
def extract_match_id(input_str: str) -> Optional[str]:
    """
    Accetta:
      - matchId diretto (EUW1_1234567890)
      - URL OP.GG tipo: /lol/summoners/euw/<riotId>/matches/<token>/<timestamp?>
    Ritorna un matchId o None.
    """
    # Caso 1: matchId diretto (EUW1_1234567890)
    if re.fullmatch(r"[A-Z]+1_\d+", input_str):
        return input_str

    # Caso 2: URL che contiene già EUW1_123...
    m = re.search(r"[A-Z]+1_\d+", input_str)
    if m:
        return m.group(0)

    # Caso 3: OP.GG pattern, risolviamo via Riot ID
    try:
        u = urlparse(input_str)
        if "op.gg" in u.netloc and "/lol/summoners/" in u.path and "/matches/" in u.path:
            segs = [s for s in u.path.split("/") if s]
            # ['lol','summoners','euw','yuriboyka-3436','matches','<token>','1756254759000?']
            region = segs[2].upper()
            riot_id = segs[3]
            parts = riot_id.split("-")
            game_name = "-".join(parts[:-1]) if len(parts) > 1 else riot_id
            tag_line = parts[-1] if len(parts) > 1 else "EUW"

            puuid = riot_get_puuid(game_name, tag_line)
            if not puuid:
                return None

            ids = riot_get_recent_match_ids(puuid, count=20)
            if ids:
                # Se non abbiamo timestamp/altro, torna il più recente
                return ids[0]
    except Exception as e:
        print(f"[PARSE] OP.GG parse error: {e}")

    return None

# -------------------- OpenAI helper (opzionale) --------------------
def analyze_with_openai(match_data: dict) -> str:
    """
    Usa OpenAI se OPENAI_API_KEY è presente. Supporta sia openai v1 che la legacy v0.
    Restituisce stringa di analisi; se non disponibile, restituisce fallback.
    """
    if not OPENAI_KEY:
        return "Analisi AI disattivata (manca OPENAI_API_KEY). Dati match inclusi."

    prompt = (
        "Sei un coach di League of Legends. Analizza sinteticamente questa partita:\n\n"
        f"{json.dumps(match_data)[:10000]}\n\n"
        "Evidenzia: (1) macro errori principali, (2) gestione ondate/farmeggio, "
        "(3) posizionamento/obiettivi, (4) build e rune, (5) 3 consigli pratici."
    )

    # Tentativo API client moderno (openai>=1.x)
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_KEY)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e1:
        print(f"[OPENAI v1] errore: {e1}")

    # Fallback legacy (openai<1.x)
    try:
        import openai
        openai.api_key = OPENAI_KEY
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e2:
        print(f"[OPENAI legacy] errore: {e2}")
        return "Non sono riuscito a generare l’analisi con OpenAI. Verifica la chiave/modello."

# -------------------- Endpoints --------------------
@app.get("/")
def root():
    return {"status": "ok", "message": "LoL Analyzer API is running!"}

@app.post("/resolve")
def resolve_match_ids(rid: RiotId):
    """Da Riot ID -> ultimi matchId."""
    if not RIOT_TOKEN:
        raise HTTPException(500, "RIOT_API_KEY non configurata.")
    puuid = riot_get_puuid(rid.game_name, rid.tag_line)
    if not puuid:
        raise HTTPException(404, "Non ho trovato il PUUID (controlla Riot ID o la chiave).")
    ids = riot_get_recent_match_ids(puuid, count=max(1, min(100, rid.count)))
    if not ids:
        raise HTTPException(404, "Non sono riuscito a ottenere i match IDs.")
    return {"match_ids": ids}

@app.post("/analizar")
def analizar(req: MatchRequest):
    """Accetta link OP.GG o matchId, risolve, prende i dettagli e (opzionale) analizza con OpenAI."""
    if not RIOT_TOKEN:
        raise HTTPException(500, "RIOT_API_KEY non configurata.")

    match_id = extract_match_id(req.match_url.strip())
    if not match_id:
        raise HTTPException(
            400,
            "Non riesco a estrarre il matchId. Incolla un matchId tipo EUW1_1234567890 o un link OP.GG valido."
        )

    match_data = riot_get_match(match_id)
    if not match_data:
        raise HTTPException(404, "Non sono riuscito a ottenere i dati della partita da Riot.")

    # Analisi AI (se disponibile)
    analysis = analyze_with_openai(match_data) if OPENAI_KEY else "OK: dati partita ottenuti."
    return {
        "match_id": match_id,
        "analisis": analysis,
        "info_snapshot": match_data.get("info", {}).get("gameMode", None)
    }
