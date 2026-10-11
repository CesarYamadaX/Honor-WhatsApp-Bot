"""Con que proveedor de modelos habla el bot: OpenRouter o el que diga .env.

Hasta octubre de 2026 la direccion de OpenRouter estaba escrita en cuatro
lugares (el agente, el revisor, la traduccion y las notas de voz). El 10/10
hubo que migrar a la plataforma interna (Comagic), que habla el mismo
protocolo (/chat/completions de OpenAI) en otra direccion y con otra clave.

Todo se decide en .env, nunca en el codigo (la clave no puede ir a git):

  LLM_BASE_URL       la direccion de la API, p. ej. https://.../v1
  LLM_API_KEY        su clave
  LLM_MODEL          el modelo del agente
  LLM_REVISOR_MODEL  el del revisor (si no, el del agente)
  LLM_TRADUCCION_MODEL, LLM_AUDIO_MODEL  traduccion del panel y notas de voz

Sin LLM_BASE_URL todo sigue como antes, con OpenRouter y sus variables
OPENROUTER_*. Se cambia de proveedor de golpe, con la direccion: asi la
clave de uno nunca viaja a la direccion del otro.
"""

from __future__ import annotations

import os

OPENROUTER = "https://openrouter.ai/api/v1"


def usa_openrouter() -> bool:
    return not os.getenv("LLM_BASE_URL", "").strip()


def base_url() -> str:
    return (os.getenv("LLM_BASE_URL", "").strip() or OPENROUTER).rstrip("/")


def url_chat() -> str:
    return f"{base_url()}/chat/completions"


def nombre_de_la_clave() -> str:
    return "OPENROUTER_API_KEY" if usa_openrouter() else "LLM_API_KEY"


def clave() -> str | None:
    return os.getenv(nombre_de_la_clave()) or None


def modelo(variable: str, defecto: str | None = None) -> str | None:
    """El modelo de `variable` ('MODEL', 'REVISOR_MODEL'...) del proveedor en uso.

    Con la plataforma propia manda LLM_<variable>; con OpenRouter,
    OPENROUTER_<variable>. No se mezclan: un nombre de modelo de OpenRouter
    ('openai/gpt-6-luna') no existe en otra plataforma.
    """
    prefijo = "OPENROUTER_" if usa_openrouter() else "LLM_"
    return os.getenv(f"{prefijo}{variable}") or defecto


def encabezados() -> dict[str, str]:
    """Los de cada peticion. Los de atribucion solo los entiende OpenRouter."""
    extra = {"HTTP-Referer": "http://localhost", "X-Title": "SQL Agent"} if usa_openrouter() else {}
    return {"Authorization": f"Bearer {clave()}", **extra}
