"""La bienvenida: que se le puede preguntar al bot, con ejemplos.

Pedido el 12/10/2026, con ~15 consultas por semana: mucha gente no sabe que
preguntarle. Dos momentos:

- A un saludo o un pedido de ayuda ("hola", "que puedes hacer?", "ayuda",
  "hi", "你好") se contesta al instante con ejemplos, sin pasar por el
  modelo: es siempre lo mismo y no tiene por que costar ni tardar.
- La PRIMERA vez que alguien escribe una pregunta de verdad, se contesta
  como siempre y al final van tres ejemplos, una sola vez.

Los ejemplos usan el mes pasado y las cadenas que esa persona puede ver: a
quien solo ve Coppel no se le sugiere preguntar por Telcel.
"""

from __future__ import annotations

import os
import re
from datetime import date, timedelta

import acceso
import idioma as idiomas

# BIENVENIDA=0 en .env la apaga (y las pruebas de lo demas la apagan).
ACTIVA = os.getenv("BIENVENIDA", "1").strip() not in ("0", "no", "false")

_SALUDO = (
    r"hola|holi|buen[oa]s?(\s+(d[ií]as|tardes|noches))?|qu[eé]\s+tal|hey|hi|hello|good\s+(morning|afternoon|evening)|"
    r"ayuda|help|men[uú]|instrucciones|"
    r"qu[eé]\s+(puedes|sabes|haces)(\s+hacer)?|c[oó]mo\s+(funciona|te\s+uso|se\s+usa)|"
    r"qu[eé]\s+te\s+puedo\s+preguntar|what\s+can\s+you\s+do|how\s+does\s+(this|it)\s+work|"
    r"你好|您好|帮助|你能做什么"
)
# Todo el mensaje es saludo o pedido de ayuda (uno o varios, con signos de
# por medio): "hola, que puedes hacer?" si; "hola, cuanto vendio Coppel" no.
_SOLO_SALUDO = re.compile(rf"^[\W_]*(?:(?:{_SALUDO})[\W_]*)+$", re.IGNORECASE)

_MESES = {
    idiomas.ESPANOL: ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
                      "septiembre", "octubre", "noviembre", "diciembre"),
    idiomas.INGLES: ("January", "February", "March", "April", "May", "June", "July", "August",
                     "September", "October", "November", "December"),
    idiomas.CHINO: tuple(f"{m}月" for m in range(1, 13)),
}


def es_saludo(texto: str) -> bool:
    return bool(_SOLO_SALUDO.match((texto or "").strip())) and len(texto.strip()) <= 60


def _contexto(cadenas, idioma: str, hoy: date | None):
    hoy = hoy or date.today()
    pasado = hoy.replace(day=1) - timedelta(days=1)
    mes = _MESES.get(idioma, _MESES[idiomas.ESPANOL])[pasado.month - 1]
    visibles = [c for c in acceso.CADENAS if not cadenas or c in cadenas]
    principal = acceso.NOMBRES[visibles[0]]
    return mes, pasado.year, principal, "TELCEL" in visibles


def _ejemplos(cadenas, idioma: str, hoy: date | None) -> list[str]:
    mes, anio, cadena, telcel = _contexto(cadenas, idioma, hoy)
    if idioma == idiomas.INGLES:
        ejemplos = [
            f"How many units did Honor sell in {mes} {anio}?",
            f"Honor's share in {cadena} in {mes}",
            f"Top 10 Honor models in {cadena} in {mes}",
            f"Send me the Excel of Honor sales by store in {cadena} for {mes}",
            f"How many {cadena} stores have a Honor promoter?",
            "How much is the Honor Magic 8 Lite?",
        ]
        if telcel:
            ejemplos.append("How many weeks of inventory does the Honor X6c have at Telcel?")
    elif idioma == idiomas.CHINO:
        ejemplos = [
            f"{anio}年{mes}荣耀卖了多少台？",
            f"{mes}荣耀在{cadena}的市场份额",
            f"{mes}{cadena}荣耀前10畅销型号",
            f"把{mes}{cadena}荣耀按门店的销售发给我Excel",
            f"{cadena}有多少门店有荣耀促销员？",
            "荣耀Magic 8 Lite多少钱？",
        ]
        if telcel:
            ejemplos.append("荣耀X6c在Telcel的库存够卖几周？")
    else:
        ejemplos = [
            f"¿Cuánto vendió Honor en {mes} de {anio}?",
            f"Participación de Honor en {cadena} en {mes}",
            f"Top 10 modelos de Honor en {cadena} en {mes}",
            f"Mándame el Excel de ventas de Honor por tienda en {cadena} de {mes}",
            f"¿Cuántas tiendas de {cadena} tienen promotor de Honor?",
            "¿En cuánto está el Honor Magic 8 Lite?",
        ]
        if telcel:
            ejemplos.append("¿Cuántas semanas de inventario tiene el Honor X6c en Telcel?")
    return ejemplos


_ENCABEZADO = {
    idiomas.ESPANOL: ("¡Hola! Te ayudo con ventas, participación, promotoría, precios e inventario "
                      "de Honor y su competencia. Pregúntame como se lo preguntarías a un colega, por "
                      "ejemplo:"),
    idiomas.INGLES: ("Hi! I can help with Honor and competitor sales, market share, promoters, prices "
                     "and inventory. Ask me like you'd ask a colleague, for example:"),
    idiomas.CHINO: "你好！我可以帮你查询荣耀及竞品的销量、份额、促销员、价格和库存。像问同事一样问我就行，例如：",
}
_PIE = {
    idiomas.ESPANOL: "Puedes escribir o mandar nota de voz, en español, inglés o chino.",
    idiomas.INGLES: "You can type or send a voice note, in English, Spanish or Chinese.",
    idiomas.CHINO: "可以打字或发语音，中文、英文、西班牙文都可以。",
}
_CONSEJO = {
    idiomas.ESPANOL: "Por cierto, también puedes preguntarme cosas como:",
    idiomas.INGLES: "By the way, you can also ask me things like:",
    idiomas.CHINO: "另外，你还可以问我：",
}


def texto(idioma: str, cadenas=None, hoy: date | None = None) -> str:
    """La bienvenida completa, para un saludo o un pedido de ayuda."""
    idioma = idioma if idioma in _ENCABEZADO else idiomas.ESPANOL
    lineas = [_ENCABEZADO[idioma], ""]
    lineas += [f"- {e}" for e in _ejemplos(cadenas, idioma, hoy)]
    lineas += ["", _PIE[idioma]]
    return "\n".join(lineas)


def consejo(idioma: str, cadenas=None, hoy: date | None = None) -> str:
    """Tres ejemplos, al pie de la primera respuesta de alguien nuevo."""
    idioma = idioma if idioma in _CONSEJO else idiomas.ESPANOL
    ejemplos = _ejemplos(cadenas, idioma, hoy)
    return "\n".join([_CONSEJO[idioma]] + [f"- {e}" for e in (ejemplos[1], ejemplos[3], ejemplos[4])])
