"""Traduce, solo para el panel, las preguntas y respuestas en otro idioma.

El bot contesta en el idioma en que le preguntan (espanol, ingles o chino), y
eso esta bien para el usuario. Pero quien revisa el panel no siempre lee
chino: antes una conversacion en chino era una caja de texto que no se podia
revisar. Esto la traduce al idioma del panel cuando se pide, sin tocar lo que
recibio el usuario por WhatsApp.

Se usa un modelo barato de OpenRouter, con la misma clave del agente. Las
traducciones se recuerdan mientras el servicio esta encendido: abrir otra vez
la misma conversacion no vuelve a pagar.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict

import httpx

import proveedor

logger = logging.getLogger("sql_agent.traduccion")

MODELO = proveedor.modelo("TRADUCCION_MODEL", "google/gemini-3.1-flash-lite")
SEGUNDOS_DE_PLAZO = 60
DESTINOS = {"es": "espanol de Mexico", "en": "ingles"}
# Cuantas traducciones se recuerdan. Un turno son dos textos; esto cubre
# cientos de conversaciones revisadas sin crecer sin limite.
MAX_EN_MEMORIA = 2000

INSTRUCCION = (
    "Traduce al {destino} cada texto de la lista JSON que te paso. Son "
    "preguntas y respuestas de un bot de ventas de celulares en Mexico. "
    "Reglas: no traduzcas nombres de marcas, cadenas ni modelos (Honor, "
    "Telcel, Coppel, X6C, Magic 8 Lite); deja los numeros, fechas y "
    "porcentajes exactamente igual; conserva los ** de las negritas, los "
    "saltos de linea y las viñetas; si un texto ya esta en {destino}, "
    "devuelvelo igual. Contesta SOLO con una lista JSON de cadenas, en el "
    "mismo orden y con la misma cantidad de elementos, sin nada mas."
)


class TraduccionFallida(Exception):
    """El servicio no pudo traducir (red, cuota, respuesta ilegible)."""


_recordadas: "OrderedDict[tuple[str, str], str]" = OrderedDict()
_candado = threading.Lock()


def _de_memoria(texto: str, destino: str) -> str | None:
    with _candado:
        clave = (destino, texto)
        if clave in _recordadas:
            _recordadas.move_to_end(clave)
            return _recordadas[clave]
    return None


def _recordar(texto: str, destino: str, traduccion: str) -> None:
    with _candado:
        _recordadas[(destino, texto)] = traduccion
        while len(_recordadas) > MAX_EN_MEMORIA:
            _recordadas.popitem(last=False)


def _lista_de_la_respuesta(contenido: str) -> list:
    """La lista JSON de la respuesta del modelo, aunque venga entre ```."""
    texto = contenido.strip()
    if texto.startswith("```"):
        texto = texto.strip("`")
        texto = texto[texto.find("["):]
    inicio, fin = texto.find("["), texto.rfind("]")
    if inicio < 0 or fin < inicio:
        raise TraduccionFallida(f"la respuesta no es una lista: {contenido[:200]}")
    return json.loads(texto[inicio:fin + 1])


def traducir(textos: list[str], destino: str = "es") -> list[str]:
    """Los textos traducidos al `destino`, en el mismo orden."""
    if destino not in DESTINOS:
        raise ValueError(f"destino no soportado: {destino}")
    resultado: list[str | None] = [_de_memoria(t, destino) if t else t for t in textos]
    pendientes = [i for i, r in enumerate(resultado) if r is None]
    if not pendientes:
        return resultado  # type: ignore[return-value]

    if not proveedor.clave():
        raise TraduccionFallida(f"{proveedor.nombre_de_la_clave()} no esta configurada")
    cuerpo = {
        "model": MODELO,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": INSTRUCCION.format(destino=DESTINOS[destino])},
            {"role": "user", "content": json.dumps([textos[i] for i in pendientes], ensure_ascii=False)},
        ],
    }
    try:
        respuesta = httpx.post(
            proveedor.url_chat(), json=cuerpo, timeout=SEGUNDOS_DE_PLAZO,
            headers=proveedor.encabezados(),
        )
    except httpx.HTTPError as error:
        raise TraduccionFallida(f"sin conexion con OpenRouter: {error}") from error
    if respuesta.status_code != 200:
        raise TraduccionFallida(f"OpenRouter contesto {respuesta.status_code}: {respuesta.text[:300]}")
    try:
        contenido = respuesta.json()["choices"][0]["message"]["content"] or ""
        traducidas = _lista_de_la_respuesta(contenido)
    except (KeyError, IndexError, ValueError) as error:
        raise TraduccionFallida(f"respuesta inesperada: {respuesta.text[:300]}") from error
    if len(traducidas) != len(pendientes) or not all(isinstance(t, str) for t in traducidas):
        raise TraduccionFallida("la traduccion no trae un texto por cada original")

    for i, traduccion in zip(pendientes, traducidas):
        resultado[i] = traduccion
        _recordar(textos[i], destino, traduccion)
    return resultado  # type: ignore[return-value]
