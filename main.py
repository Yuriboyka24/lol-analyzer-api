from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
from urllib.parse import urlparse
import requests, os, re, json, time

# ===================== Config =====================
RIOT_TOKEN = os.getenv("RIOT_API_KEY")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

if not RIOT_TOKEN:
    print("[WARN] RIOT_API_KEY non impostata: /resolve e /analizar falliranno sulle chiamate Riot.")

RIOT_HEADERS = {"X-Riot-Token": RIOT_TOKEN} if RIOT_TOKEN else {}

# Piattaforma (platform route) -> Regione (regional route) per Match/Account v5
PLATFORM_TO_REGION = {
    # Europe
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe",
    # Americas
    "na1": "americas", "br1": "americas", "la1": "americas",
    "la2": "americas", "oc1": "americas",
    # Asia
    "kr": "asia", "jp1": "asia",
}
def platform_to_region(platform: str) -> str:
    return PLATFORM_TO_REGION.get(platform.lower(), "europe")

# ===================== App & CORS =====================
app = FastAPI(title="LoL Analyzer API", version="1.1.0")

# Apri CORS (puoi restringerlo al tuo dominio Shopify)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # es: ["https://tua-shop.myshopify.com"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===================== Models =====================
class RiotId(BaseModel):
    game_name: str            # es: "YuriBoyka" (rispetta maiuscole se possibile)
    tag_line: str             # es: "3436"
    count: int = 5
    platform: str = "euw1"    # es: euw1, na1, kr...

class MatchRequest(BaseModel):
    match_url: str            # accetta matchId (EUW1_...) o link OP.GG
    platform: str = "euw1"

# ===================== Helpers Riot =====================
def _retry_get(url: str, headers: dict, timeout: int = 10, retries: int = 2, backoff: float = 0.6):
    """GET con piccolo backoff per gestire 429 temporanei."""
    for i in range(retries + 1):
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code != 429:
            return r
        sleep_for = backoff * (2 ** i)
        print(f"[RIOT] 429 rate limited, retry in {sleep_for:.1f}s")
        time.sleep(sleep_for)
    return r  # ultimo response

def riot_get_puuid(game_name: str, tag_line: str, platform: str = "euw1") -> Optional[str]:
    """Tenta prima Riot ID → PUUID (case-sensitive). Se 404, fallback Summoner-V4 by-name (case-insensitive)."""
    if not RIOT_TOKEN:
        return None

    # 1) Riot ID → PUUID (regional route)
    url1 = f"https://{platform_to_region(platform)}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
    r1 = _retry_get(url1, RIOT_HEADERS)
    if r1.status_code == 200:
        return r1.json().get("puuid")

    # 2) Fallback: Summoner-V4 by-name (platform route)
    url2 = f"https://{platform.lower()}.api.riotgames.com/lol/summoner/v4/summoners/by-name/{game_name}"
    r2 = _retry_get(url2, RIOT_HEADERS)
    if r2.status_code == 200:
        return r2.json().get("puuid")

    print(f"[RIOT] PUUID not found. account={r1.status_code} summoner={r2.status_code}")
    return None

def riot_get_recent_match_ids(puuid: str, count: int = 10, platform: str = "euw1") -> Optional[List[str]]:
    """PUUID → lista matchId (regional route)."""
    if not RIOT_TOKEN:
        return None
    region = platform_to_region(platform)
    url = f"https://{region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count}"
    r = _retry_get(url, RIOT_HEADERS)
    if r.status_code == 200:
        return r.json()
    print(f"[RIOT] ids fail {r.status_code}: {r.text}")
    return None

def riot_get_match(match_id: str, platform: str = "euw1") -> Optional[dict]:
    """Dettaglio partita (regional route)."""
    if not RIOT_TOKEN:
        return None
    region = platform_to_region(platform)
    url = f"https://{region}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    r = _retry_get(url, RIOT_HEADERS, timeout=15)
    if r.status_code == 200:
        return r.json()
    print(f"[RIOT] match fail {r.status_code}: {r.text}")
    return None

# ===================== Parsing matchId / OP.GG =====================
def extract_match_id(input_str: str, platform: str = "euw1") -> Optional[str]:
    """
    Accetta:
      - matchId diretto (EUW1_1234567890)
      - URL contenente già EUW1_123... (regex)
      - URL OP.GG tipo: /lol/summoners/euw/<riotId>/matches/<token>/<timestamp?>
        -> risolve via Riot ID e ritorna l'ID più recente (o il più vicino se c'è timestamp)
    """
    s = input_str.strip()

    # Caso 1: matchId diretto
    if re.fullmatch(r"[A-Z]+1_\d+", s):
        return s

    # Caso 2: in qualunque URL, prova ad estrarre EUW1_123...
    m = re.search(r"[A-Z]+1_\d+", s)
    if m:
        return m.group(0)

    # Caso 3: OP.GG
    try:
        u = urlparse(s)
        if "op.gg" in u.netloc and "/lol/summoners/" in u.path and "/matches/" in u.path:
            segs = [x for x in u.path.split("/") if x]
            # ['lol','summoners','euw','yuriboyka-3436','matches','<token>','1756254759000?']
            riot_id = segs[3] if len(segs) >= 4 else ""
            parts = riot_id.split("-")
            if len(parts) >= 2:
                game_name = "-".join(parts[:-1])
                tag_line = parts[-1]
            else:
                # se il formato non è esatto, prova a prendere tutto come game_name e tag_line di default
                game_name, tag_line = riot_id, "EUW"

            # timestamp opzionale in ms (se presente sceglieremo il match piu vicino)
            ts_ms = None
            if len(segs) >= 7:
                try:
                    ts_ms = int(re.sub(r"\D", "", segs[6]))
                except Exception:
                    ts_ms = None

            puuid = riot_get_puuid(game_name, tag_line, platform)
            if not puuid:
                return None

            ids = riot_get_recent_match_ids(puuid, count=20, platform=platform)
            if not ids:
                return None

            if ts_ms is None:
                return ids[0]  # più recente

            # Se abbiamo timestamp, cerchiamo la più vicina
            best_id, best_diff = None, None
            for mid in ids:
                md = riot_get_match(mid, platform=platform)
                start = md.get("info", {}).get("gameStartTimestamp") if md else None
                if start is None:
                    continue
                diff = abs(start - ts_ms)
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best_id = mid
            return best_id
    except Exception as e:
        print(f"[PARSE] OP.GG parse error: {e}")

    return None

# ===================== OpenAI (opzionale) =====================
def analyze_with_openai(match_data: dict) -> str:
    """
    Usa OpenAI se OPENAI_API_KEY è presente.
    Supporta client moderno (openai>=1.x) e fallback legacy.
    """
    if not OPENAI_KEY:
        return "Analisi AI disattivata (manca OPENAI_API_KEY). Dati match ottenuti correttamente."

    prompt = (
        "Sei un coach di League of Legends. Analizza in modo conciso questa partita (dal JSON):\n\n"
        f"{json.dumps(match_data)[:10000]}\n\n"
        "Evidenzia: (1) principali errori macro, (2) gestione ondate/farmeggio, "
        "(3) posizionamento/obiettivi, (4) build/rune, (5) 3 consigli pratici e immediati."
    )

    # Client moderno
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

    # Fallback legacy
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
        return "Non sono riuscito a generare l’analisi con OpenAI. Verifica chiave/modello."

# ===================== Endpoints =====================
@app.get("/")
def root():
    return {"status": "ok", "message": "LoL Analyzer API is running!"}

@app.post("/resolve")
def resolve_match_ids(rid: RiotId):
    """Da Riot ID -> ultimi matchId (con fallback case-insensitive)."""
    if not RIOT_TOKEN:
        raise HTTPException(500, "RIOT_API_KEY non configurata.")
    puuid = riot_get_puuid(rid.game_name, rid.tag_line, rid.platform)
    if not puuid:
        raise HTTPException(404, "Non ho trovato il PUUID (controlla Riot ID/case, tag o la key).")
    ids = riot_get_recent_match_ids(puuid, count=max(1, min(100, rid.count)), platform=rid.platform)
    if not ids:
        raise HTTPException(404, "Non sono riuscito a ottenere i match IDs.")
    return {"match_ids": ids}

@app.post("/analizar")
def analizar(req: MatchRequest):
    """
    Accetta link OP.GG o matchId, risolve, scarica dettagli dal Match-V5 e (opzionale) genera un feedback AI.
    """
    if not RIOT_TOKEN:
        raise HTTPException(500, "RIOT_API_KEY non configurata.")

    match_id = extract_match_id(req.match_url, platform=req.platform)
    if not match_id:
        raise HTTPException(
            400,
            "Non riesco a estrarre il matchId. Incolla un matchId tipo EUW1_1234567890 o un link OP.GG valido."
        )

    match_data = riot_get_match(match_id, platform=req.platform)
    if not match_data:
        raise HTTPException(404, "Non sono riuscito a ottenere i dati della partita da Riot.")

    analysis = analyze_with_openai(match_data) if OPENAI_KEY else "OK: dati partita ottenuti."
    return {
        "match_id": match_id,
        "analisis": analysis,
        "gameMode": match_data.get("info", {}).get("gameMode", None)
    }

