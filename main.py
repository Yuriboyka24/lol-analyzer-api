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
