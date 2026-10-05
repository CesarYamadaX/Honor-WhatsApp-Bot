"""Notas de voz: de audio a la pregunta en texto.

Antes el sidecar descartaba cualquier mensaje sin texto, en silencio. Quien
pregunta desde la calle o desde una tienda prefiere dictar.

Se transcribe con un modelo de OpenRouter que acepta audio, con la misma
clave que el agente. Medido el 5 de octubre de 2026 con una nota OGG/Opus
(el formato de WhatsApp) de 5 segundos: google/gemini-3.5-flash-lite la
transcribio bien en 2.2s por $0.00008; google/gemini-2.5-flash-lite cambio
"2026" por "2022", y un ano mal en una pregunta de ventas da otro numero sin
que nadie lo note.
"""

from __future__ import annotations

import base64
import logging
import os

import httpx

logger = logging.getLogger("sql_agent.transcripcion")

MODELO = os.getenv("OPENROUTER_AUDIO_MODEL", "google/gemini-3.5-flash-lite")
URL = "https://openrouter.ai/api/v1/chat/completions"
SEGUNDOS_DE_PLAZO = 60

# El vocabulario va en la instruccion: sin el, "Telcel" sale "tel cel",
# "X6c" sale "equis seis ce" y los anos se escriben con letra.
INSTRUCCION = (
    "Transcribe literalmente esta nota de voz. Es una pregunta para un bot "
    "de ventas de celulares en Mexico, en espanol o en ingles. Palabras que "
    "pueden aparecer: Honor, Samsung, Apple, Motorola, Oppo, Xiaomi; Telcel, "
    "Coppel, Liverpool, Elektra, Sears, AT&T, Movistar; share, sell out, "
    "promotoria, promotor, inventario, region; modelos como X6c, X5d, X7e, "
    "X8d, Magic 8 Lite, Honor 400, Honor 600. Escribe los numeros y los anos "
    "con cifras (2026, 35) y los modelos como se escriben (X6c, no 'equis "
    "seis ce'). Devuelve SOLO el texto, sin comillas ni comentarios. Si no "
    "se entiende ninguna palabra, devuelve vacio."
)


class TranscripcionFallida(Exception):
    """El servicio no pudo transcribir (red, cuota, formato)."""


def transcribir(audio: bytes, formato: str = "ogg") -> str:
    """El texto de la nota de voz. Cadena vacia si no se entendio nada."""

    clave = os.getenv("OPENROUTER_API_KEY")
    if not clave:
        raise TranscripcionFallida("OPENROUTER_API_KEY no esta configurada")
    cuerpo = {
        "model": MODELO,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": INSTRUCCION},
                {"type": "input_audio", "input_audio": {
                    "data": base64.b64encode(audio).decode("ascii"),
                    "format": formato,
                }},
            ],
        }],
    }
    try:
        respuesta = httpx.post(
            URL, json=cuerpo, timeout=SEGUNDOS_DE_PLAZO,
            headers={
                "Authorization": f"Bearer {clave}",
                "HTTP-Referer": "http://localhost",
                "X-Title": "SQL Agent",
            },
        )
    except httpx.HTTPError as error:
        raise TranscripcionFallida(f"sin conexion con OpenRouter: {error}") from error
    if respuesta.status_code != 200:
        raise TranscripcionFallida(f"OpenRouter contesto {respuesta.status_code}: {respuesta.text[:300]}")
    try:
        texto = respuesta.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, ValueError) as error:
        raise TranscripcionFallida(f"respuesta inesperada: {respuesta.text[:300]}") from error
    return " ".join(texto.strip().strip('"').split())
