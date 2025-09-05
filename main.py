from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from urllib.parse import urlparse
import requests, os, re, json, time

# ===================== Config =====================
RIOT_TOKEN = os.getenv("RIOT_API_KEY")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

if not RIOT_TOKEN:
    print("[WARN] RIOT_API_KEY non impostata: /resolve e /analizar falliranno sulle chiamate Riot.")

RIOT_HEADERS = {"X-Riot-Token": RIOT_TOKEN} if RIOT_TOKEN else {}

# platform route -> regional route per Account/Match v5
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
app = FastAPI(title="LoL Analyzer API", version="3.0.0")

# Apri CORS per frontend (restringi a dominio Shopify in produzione)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # es: ["https://il-tuo-shop.myshopify.com"]
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===================== Models =====================
class RiotId(BaseModel):
    game_name: str
    tag_line: str
    count: int = 5
    platform: str = "euw1"

class PlayerContext(BaseModel):
    game_name: Optional[str] = None
    tag_line: Optional[str] = None
    summoner_name: Optional[str] = None
    lane: Optional[str] = None
    goals: Optional[str] = None
    target_rank: Optional[str] = None

class MatchRequest(BaseModel):
    match_url: str                      # matchId (EUW1_...) o link OP.GG
    platform: str = "euw1"
    use_ai: bool = True                 # abilita/disabilita AI
    include_timeline: bool = True       # scarica e usa la timeline
    lang: str = "it"                    # lingua output
    player: Optional[PlayerContext] = None

# ===================== Helpers Riot =====================
def _retry_get(url: str, headers: dict, timeout: int = 10, retries: int = 2, backoff: float = 0.6):
    """GET con piccolo backoff per 429."""
    for i in range(retries + 1):
        r = requests.get(url, headers=headers, timeout=timeout)
        if r.status_code != 429:
            return r
        sleep_for = backoff * (2 ** i)
        print(f"[RIOT] 429 rate limited, retry in {sleep_for:.1f}s → {url}")
        time.sleep(sleep_for)
    return r  # ultimo response

def riot_get_puuid(game_name: str, tag_line: str, platform: str = "euw1") -> Optional[str]:
    """Prova Riot ID → PUUID (case-sensitive). Se 404, fallback Summoner-V4 by-name (case-insensitive)."""
    if not RIOT_TOKEN:
        return None
    # account-v1 (regional)
    url1 = f"https://{platform_to_region(platform)}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
    r1 = _retry_get(url1, RIOT_HEADERS)
    if r1.status_code == 200:
        return r1.json().get("puuid")
    # summoner-v4 (platform)
    url2 = f"https://{platform.lower()}.api.riotgames.com/lol/summoner/v4/summoners/by-name/{game_name}"
    r2 = _retry_get(url2, RIOT_HEADERS)
    if r2.status_code == 200:
        return r2.json().get("puuid")
    print(f"[RIOT] PUUID not found. account={r1.status_code} summoner={r2.status_code}")
    return None

def riot_get_recent_match_ids(puuid: str, count: int = 10, platform: str = "euw1") -> Optional[List[str]]:
    if not RIOT_TOKEN:
        return None
    region = platform_to_region(platform)
    url = f"https://{region}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count={count}"
    r = _retry_get(url, RIOT_HEADERS)
    if r.status_code == 200:
        return r.json()
    print(f"[RIOT] ids fail {r.status_code}: {r.text[:200]}")
    return None

def riot_get_match(match_id: str, platform: str = "euw1") -> Optional[dict]:
    if not RIOT_TOKEN:
        return None
    region = platform_to_region(platform)
    url = f"https://{region}.api.riotgames.com/lol/match/v5/matches/{match_id}"
    r = _retry_get(url, RIOT_HEADERS, timeout=15)
    if r.status_code == 200:
        return r.json()
    print(f"[RIOT] match fail {r.status_code}: {r.text[:200]}")
    return None

def riot_get_timeline(match_id: str, platform: str = "euw1") -> Optional[dict]:
    """Scarica la timeline (frames + eventi)."""
    if not RIOT_TOKEN:
        return None
    region = platform_to_region(platform)
    url = f"https://{region}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
    r = _retry_get(url, RIOT_HEADERS, timeout=20)
    if r.status_code == 200:
        return r.json()
    print(f"[RIOT] timeline fail {r.status_code}: {r.text[:200]}")
    return None

# ===================== Parsing matchId / OP.GG =====================
def extract_match_id(input_str: str, platform: str = "euw1") -> Optional[str]:
    """
    Accetta:
      - matchId diretto (EUW1_1234567890)
      - URL con EUW1_... (regex)
      - URL OP.GG /lol/summoners/euw/<riotId>/matches/<token>/<timestamp?>
        -> risolve via Riot ID e prende l'ID più recente (o più vicino al timestamp)
    """
    s = input_str.strip()

    # matchId diretto
    if re.fullmatch(r"[A-Z]+1_\d+", s):
        return s

    # qualsiasi URL con EUW1_...
    m = re.search(r"[A-Z]+1_\d+", s)
    if m:
        return m.group(0)

    # OP.GG
    try:
        u = urlparse(s)
        if "op.gg" in u.netloc and "/lol/summoners/" in u.path and "/matches/" in u.path:
            segs = [x for x in u.path.split("/") if x]
            # ['lol','summoners','euw','name-3436','matches','<token>','1756...']
            riot_id = segs[3] if len(segs) >= 4 else ""
            parts = riot_id.split("-")
            if len(parts) >= 2:
                game_name = "-".join(parts[:-1])
                tag_line = parts[-1]
            else:
                game_name, tag_line = riot_id, "EUW"

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
                return ids[0]

            # se c'è timestamp, scegli match più vicino
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

# ===================== Player targeting & metrics =====================
def find_participant_index(match_data: dict,
                           puuid: Optional[str] = None,
                           summoner_name: Optional[str] = None) -> Optional[int]:
    info = match_data.get("info", {})
    parts = info.get("participants", [])
    if puuid:
        for i, p in enumerate(parts):
            if p.get("puuid") == puuid:
                return i
    if summoner_name:
        for i, p in enumerate(parts):
            if p.get("summonerName", "").lower() == summoner_name.lower():
                return i
    return None

def compute_player_metrics(match_data: dict, idx: int) -> dict:
    info = match_data.get("info", {})
    parts = info.get("participants", [])
    me = parts[idx]
    dur = info.get("gameDuration") or me.get("timePlayed") or 0
    dur_min = (dur / 60.0) if dur else 0.0

    cs = (me.get("totalMinionsKilled", 0) + me.get("neutralMinionsKilled", 0))
    csmin = round(cs / max(1e-9, dur_min), 2) if dur_min else None

    team_id = me.get("teamId")
    team = [p for p in parts if p.get("teamId") == team_id]
    enemy = [p for p in parts if p.get("teamId") != team_id]

    team_kills = sum(p.get("kills", 0) for p in team) or 0
    kp = round(100.0 * (me.get("kills", 0) + me.get("assists", 0)) / team_kills, 1) if team_kills else None

    team_dmg = sum(p.get("totalDamageDealtToChampions", 0) for p in team) or 0
    dmg_share = round(100.0 * me.get("totalDamageDealtToChampions", 0) / team_dmg, 1) if team_dmg else None

    chal = me.get("challenges", {}) or {}
    vision = me.get("visionScore")
    vspm = chal.get("visionScorePerMinute")
    gpm = chal.get("goldPerMinute")
    kda = chal.get("kda") or round((me.get("kills",0)+me.get("assists",0)) / max(1, me.get("deaths",0)), 2)

    lane = me.get("teamPosition") or me.get("lane")
    # avversario diretto
    opp = None
    for p in enemy:
        if (p.get("teamPosition") or p.get("lane")) == lane:
            opp = p
            break

    # trova index avversario per differenze gold
    opp_idx = None
    if opp:
        for i, p in enumerate(parts):
            if p.get("puuid") == opp.get("puuid"):
                opp_idx = i
                break

    return {
        "champion": me.get("championName"),
        "lane": lane,
        "kills": me.get("kills",0),
        "deaths": me.get("deaths",0),
        "assists": me.get("assists",0),
        "kda": kda,
        "cs": cs,
        "cs_per_min": csmin,
        "gold_per_min": gpm,
        "vision": vision,
        "vision_per_min": vspm,
        "kill_participation_pct": kp,
        "team_damage_share_pct": dmg_share,
        "opponent": {
            "summonerName": opp.get("summonerName") if opp else None,
            "champion": opp.get("championName") if opp else None
        },
        "me_idx": idx,
        "opp_idx": opp_idx,
        "raw": me
    }

# ===================== Timeline helpers =====================
def _minutes(ts_ms: int) -> float:
    return round(ts_ms / 60000.0, 1)

def _find_pid_from_puuid_timeline(timeline: dict, puuid: str) -> Optional[int]:
    """Nella timeline l'array metadata.participants è in ordine 1..10."""
    try:
        puuids = timeline.get("metadata", {}).get("participants", [])
        if puuid in puuids:
            return puuids.index(puuid) + 1  # pid 1..10
    except Exception:
        pass
    return None

def summarize_timeline(match_data: dict, timeline: dict, me_idx: int, opp_idx: Optional[int]) -> Dict[str, Any]:
    """Estrae info utili: cs/min 0-10 e 10-20, gold diff 10/20, kill/death times, obiettivi, torri, plates."""
    if not timeline:
        return {}

    info = match_data.get("info", {})
    parts = info.get("participants", [])
    me = parts[me_idx]
    me_puuid = me.get("puuid")

    # participantId nella timeline
    pid = _find_pid_from_puuid_timeline(timeline, me_puuid)
    if not pid:
        return {}

    pid_str = str(pid)
    opp_pid_str = None
    if opp_idx is not None:
        opp_puuid = parts[opp_idx].get("puuid")
        opp_pid = _find_pid_from_puuid_timeline(timeline, opp_puuid) if opp_puuid else None
        opp_pid_str = str(opp_pid) if opp_pid else None

    frames = timeline.get("info", {}).get("frames", [])

    def cs_at_min(min_mark: int, pid_s: str) -> Optional[int]:
        target_ms = min_mark * 60000
        closest = None
        best_dt = None
        for fr in frames:
            ts = fr.get("timestamp", 0)
            pf = fr.get("participantFrames", {})
            if pid_s not in pf: 
                continue
            dt = abs(ts - target_ms)
            if best_dt is None or dt < best_dt:
                best_dt = dt
                closest = pf[pid_s]
        if not closest:
            return None
        return (closest.get("minionsKilled", 0) + closest.get("jungleMinionsKilled", 0))

    def gold_at_min(min_mark: int, pid_s: str) -> Optional[int]:
        target_ms = min_mark * 60000
        closest = None
        best_dt = None
        for fr in frames:
            ts = fr.get("timestamp", 0)
            pf = fr.get("participantFrames", {})
            if pid_s not in pf: 
                continue
            dt = abs(ts - target_ms)
            if best_dt is None or dt < best_dt:
                best_dt = dt
                closest = pf[pid_s]
        if not closest:
            return None
        return closest.get("totalGold", 0)

    cs10 = cs_at_min(10, pid_str)
    cs20 = cs_at_min(20, pid_str)
    cs10_per_min = round((cs10 or 0) / 10.0, 2) if cs10 is not None else None
    cs20_window = None
    if cs20 is not None and cs10 is not None:
        cs20_window = round((cs20 - cs10) / 10.0, 2)

    gold10 = gold_at_min(10, pid_str)
    gold20 = gold_at_min(20, pid_str)
    golddiff10 = None
    golddiff20 = None
    if opp_pid_str:
        og10 = gold_at_min(10, opp_pid_str)
        og20 = gold_at_min(20, opp_pid_str)
        golddiff10 = (gold10 - og10) if (gold10 is not None and og10 is not None) else None
        golddiff20 = (gold20 - og20) if (gold20 is not None and og20 is not None) else None

    # Eventi: kill/death timings, obiettivi, torri, plates
    kills_at = []
    deaths_at = []
    assists = 0
    dragons = []
    heralds = []
    barons = []
    plates = []
    towers = []

    for fr in frames:
        for ev in fr.get("events", []):
            et = ev.get("type")
            ts = _minutes(ev.get("timestamp", 0))

            if et == "CHAMPION_KILL":
                if ev.get("killerId") == pid:
                    kills_at.append(ts)
                elif ev.get("victimId") == pid:
                    deaths_at.append(ts)
                else:
                    aids = ev.get("assistingParticipantIds", []) or []
                    if pid in aids:
                        assists += 1

            elif et == "ELITE_MONSTER_KILL":
                name = ev.get("monsterType")  # DRAGON / RIFTHERALD / BARON_NASHOR
                killer_team = ev.get("killerTeamId")
                rec = {"min": ts, "team": killer_team, "monster": name}
                if name == "DRAGON":
                    dragons.append(rec)
                elif name == "RIFTHERALD":
                    heralds.append(rec)
                elif name == "BARON_NASHOR":
                    barons.append(rec)

            elif et == "TURRET_PLATE_DESTROYED":
                # non sempre c'è killerId del giocatore, ma salviamo il timing
                plates.append({"min": ts, "lane": ev.get("laneType")})

            elif et == "BUILDING_KILL":
                if ev.get("buildingType") == "TOWER_BUILDING":
                    towers.append({"min": ts, "lane": ev.get("laneType"), "team": ev.get("teamId")})

    return {
        "cs10_per_min": cs10_per_min,
        "cs10_total": cs10,
        "cs10_20_per_min": cs20_window,
        "golddiff10": golddiff10,
        "golddiff20": golddiff20,
        "kills_at": kills_at,
        "deaths_at": deaths_at,
        "assists_count": assists,
        "dragons": dragons,
        "heralds": heralds,
        "barons": barons,
        "plates": plates,
        "towers": towers,
    }

# ===================== Prompt builder =====================
def build_player_prompt(lang: str, match_data: dict, metrics: dict, ctx: Optional[PlayerContext],
                        timeline_summary: Optional[dict]) -> str:
    info = match_data.get("info", {})
    mode = info.get("gameMode")
    me = metrics["raw"]

    goals = (ctx.goals if ctx and ctx.goals else "")
    target = (ctx.target_rank if ctx and ctx.target_rank else "")
    declared_lane = (ctx.lane if ctx and ctx.lane else "")

    table = (
        f"- Campione: {metrics['champion']} | Lane: {metrics['lane'] or declared_lane}\n"
        f"- K/D/A: {metrics['kills']}/{metrics['deaths']}/{metrics['assists']} | KDA: {metrics['kda']}\n"
        f"- CS: {metrics['cs']} | CS/min (match): {metrics['cs_per_min']}\n"
        f"- KP: {metrics['kill_participation_pct']}% | Team DMG: {metrics['team_damage_share_pct']}%\n"
        f"- Vision: {metrics['vision']} | Vision/min: {metrics['vision_per_min']} | GPM: {metrics['gold_per_min']}\n"
        f"- GameMode: {mode}\n"
        f"- Avversario diretto: {metrics['opponent']['champion']} ({metrics['opponent']['summonerName']})\n"
    )

    tl = ""
    if timeline_summary:
        tl += "\n== ESTRATTO TIMELINE ==\n"
        if timeline_summary.get("cs10_per_min") is not None:
            tl += f"- CS/min 0-10: {timeline_summary['cs10_per_min']} (CS @10: {timeline_summary['cs10_total']})\n"
        if timeline_summary.get("cs10_20_per_min") is not None:
            tl += f"- CS/min 10-20: {timeline_summary['cs10_20_per_min']}\n"
        if timeline_summary.get("golddiff10") is not None:
            tl += f"- Gold diff @10: {timeline_summary['golddiff10']}\n"
        if timeline_summary.get("golddiff20") is not None:
            tl += f"- Gold diff @20: {timeline_summary['golddiff20']}\n"
        if timeline_summary.get("kills_at"):
            tl += f"- Kill ai minuti: {timeline_summary['kills_at']}\n"
        if timeline_summary.get("deaths_at"):
            tl += f"- Morti ai minuti: {timeline_summary['deaths_at']}\n"
        if timeline_summary.get("dragons"):
            tl += f"- Draghi: {timeline_summary['dragons']}\n"
        if timeline_summary.get("heralds"):
            tl += f"- Herald: {timeline_summary['heralds']}\n"
        if timeline_summary.get("barons"):
            tl += f"- Baron: {timeline_summary['barons']}\n"
        if timeline_summary.get("plates"):
            tl += f"- Plate: {timeline_summary['plates']}\n"
        if timeline_summary.get("towers"):
            tl += f"- Torri: {timeline_summary['towers']}\n"

    if (lang or "").lower().startswith("it"):
        instructions = (
            "Agisci come coach di League of Legends. Fornisci un'analisi PERSONALIZZATA per il giocatore. "
            "Usa i numeri e la timeline (kill/morti, gold diff, CS/min per fase, obiettivi) per spiegare cosa è andato bene/male. "
            "Dai consigli pratici e immediati.\n"
            "Struttura la risposta in sezioni:\n"
            "1) Punti chiave\n"
            "2) Errori principali (con riferimento a minuti/eventi)\n"
            "3) Piano d'azione 0-10 / 10-20 / 20+\n"
            "4) Build/Rune alternative\n"
            "5) Drills (esercizi) e 3 azioni concrete da fare nella prossima partita."
        )
    else:
        instructions = (
            "Act as a League of Legends coach. Provide a PLAYER-FOCUSED review. "
            "Use numbers and timeline (kills/deaths, gold diff, CS/min per phase, objectives) to justify feedback. "
            "Give concrete, actionable advice.\n"
            "Structure:\n"
            "1) Key takeaways\n"
            "2) Main mistakes (with minute references)\n"
            "3) Action plan 0-10 / 10-20 / 20+\n"
            "4) Build/Runes alternatives\n"
            "5) Drills and 3 next actions."
        )

    goals_line = f"\nObiettivi dichiarati: {goals} | Rank target: {target}\n" if (goals or target) else "\n"
    snippet = json.dumps(me, ensure_ascii=False)[:8000]

    return (
        f"{instructions}\n\n"
        f"== RIEPILOGO GIOCATORE ==\n{table}{goals_line}"
        f"{tl}\n"
        f"== DATI GIOCATORE (estratto JSON) ==\n{snippet}\n"
        f"== CONTESTO MATCH ==\n{json.dumps({'gameMode': info.get('gameMode'), 'gameDuration': info.get('gameDuration')}, ensure_ascii=False)}\n"
        f"Fornisci l'analisi in lingua: {('Italiano' if (lang or '').lower().startswith('it') else 'English')}."
    )

# ===================== OpenAI =====================
def analyze_with_openai_text(prompt: str) -> str:
    if not OPENAI_KEY:
        raise RuntimeError("OPENAI_API_KEY mancante")
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_KEY)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[OPENAI] error: {type(e).__name__}: {e}")
        raise

# ===================== Endpoints =====================
@app.get("/")
def root():
    return {"status": "ok", "message": "LoL Analyzer API is running!"}

@app.get("/ai_health")
def ai_health():
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_KEY)
        r = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"user","content":"rispondi SOLO con: pong"}],
            temperature=0
        )
        return {"ok": True, "text": r.choices[0].message.content}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "has_key": bool(OPENAI_KEY), "key_len": len(OPENAI_KEY or "")}

@app.post("/resolve")
def resolve_match_ids(rid: RiotId):
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
    if not RIOT_TOKEN:
        raise HTTPException(500, "RIOT_API_KEY non configurata.")

    # 1) matchId
    match_id = extract_match_id(req.match_url, platform=req.platform)
    if not match_id:
        raise HTTPException(400, "Non riesco a estrarre il matchId. Incolla un matchId EUW1_... o un link OP.GG valido.")

    # 2) dati match
    match_data = riot_get_match(match_id, platform=req.platform)
    if not match_data:
        raise HTTPException(404, "Dati della partita non disponibili da Riot.")

    # 3) identifica il giocatore (se fornito)
    puuid = None
    summ_name = None
    if req.player and req.player.game_name and req.player.tag_line:
        puuid = riot_get_puuid(req.player.game_name, req.player.tag_line, req.platform)
    if not puuid and req.player and req.player.summoner_name:
        summ_name = req.player.summoner_name

    idx = find_participant_index(match_data, puuid=puuid, summoner_name=summ_name)
    if idx is None:
        parts = match_data.get("info", {}).get("participants", [])
        winners = [i for i,p in enumerate(parts) if p.get("win")]
        idx = winners[0] if winners else 0

    # 4) metriche + (opzionale) timeline
    metrics = compute_player_metrics(match_data, idx)
    timeline_summary = None
    if req.include_timeline:
        timeline = riot_get_timeline(match_id, platform=req.platform)
        timeline_summary = summarize_timeline(match_data, timeline, metrics["me_idx"], metrics["opp_idx"])

    # 5) Prompt e AI
    analysis = None
    ai_error = None
    if req.use_ai and OPENAI_KEY:
        try:
            prompt = build_player_prompt(req.lang, match_data, metrics, req.player, timeline_summary)
            analysis = analyze_with_openai_text(prompt)
        except Exception as e:
            ai_error = f"{type(e).__name__}: {e}"

    if not analysis:
        # fallback senza AI: riassunto numerico utile
        base = (
            f"[Senza AI] {metrics['champion']} {metrics['lane']}: "
            f"KDA {metrics['kda']} | CS/min {metrics['cs_per_min']} | "
            f"KP {metrics['kill_participation_pct']}% | DMG {metrics['team_damage_share_pct']}% | "
            f"Vision {metrics['vision']} (V/min {metrics['vision_per_min']})."
        )
        if timeline_summary and (timeline_summary.get("cs10_per_min") is not None):
            base += f" | 0-10 CS/min {timeline_summary['cs10_per_min']}"
        if timeline_summary and (timeline_summary.get("cs10_20_per_min") is not None):
            base += f" | 10-20 CS/min {timeline_summary['cs10_20_per_min']}"
        analysis = base

    return {
        "match_id": match_id,
        "player": {
            "champion": metrics["champion"],
            "lane": metrics["lane"],
            "opponent": metrics["opponent"],
        },
        "metrics": {
            "kda": metrics["kda"],
            "cs_per_min": metrics["cs_per_min"],
            "kill_participation_pct": metrics["kill_participation_pct"],
            "team_damage_share_pct": metrics["team_damage_share_pct"],
            "vision": metrics["vision"],
            "vision_per_min": metrics["vision_per_min"],
            "gold_per_min": metrics["gold_per_min"],
        },
        "timeline_summary": timeline_summary,
        "analisis": analysis,
        "ai_error": ai_error
    }



