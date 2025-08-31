from fastapi import FastAPI
from pydantic import BaseModel
import openai
import requests
import os

app = FastAPI()

# Cargar claves de entorno
openai.api_key = os.getenv("OPENAI_API_KEY")
riot_api_key = os.getenv("RIOT_API_KEY")

# 🔹 Modello per il body
class MatchRequest(BaseModel):
    match_url: str

@app.post("/analizar")
async def analizar(req: MatchRequest):
    match_url = req.match_url

    if not match_url:
        return {"error": "Falta el enlace de la partida"}

    match_id = extraer_match_id(match_url)

    # Llamada a Riot API
    riot_response = requests.get(
        f"https://europe.api.riotgames.com/lol/match/v5/matches/{match_id}",
        headers={"X-Riot-Token": riot_api_key}
    )

    if riot_response.status_code != 200:
        return {"error": "No se pudo obtener la partida de Riot"}

    match_data = riot_response.json()

    prompt = generar_prompt(match_data)

    gpt_response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[{"role": "user", "content": prompt}]
    )

    analysis = gpt_response.choices[0].message.content
    return {"analisis": analysis}


def extraer_match_id(url):
    # Ejemplo: https://www.op.gg/multisearch/euw?summoners=nombre -> EUW1_1234567890
    partes = url.split("/")
    for parte in partes:
        if "EUW1_" in parte:
            return parte
    return "EUW1_1234567890"  # fallback de ejemplo


def generar_prompt(match_data):
    return f"""
    Analiza esta partida de League of Legends con base en los datos JSON:

    {match_data}

    Detecta errores comunes en posicionamiento, builds, farmeo, decisiones, etc.
    Sé claro y didáctico para ayudar al jugador a mejorar.
    """
