"""Un segundo par de ojos antes de que salga cada respuesta.

Pedido el 7/10/2026, despues de un Excel de "la capa mensual de Liverpool
region 4" que salio con dos tiendas de menos, con una hoja de tiendas de
todo el mercado que nadie pidio, y que se comparo contra un archivo de otro
anio sin que nadie lo notara. Ninguna de esas cosas daba error.

Dos revisiones, independientes del modelo que contesta:

- `revisar_hojas`, en codigo y sin modelo: antes de armar el Excel, cada
  hoja se mira contra los permisos del usuario (no puede llevar filas de una
  cadena que no tiene) y contra lo que se pidio (una hoja con cadenas que no
  estan en el resultado no va). Y si van el detalle y lo agrupado, tienen
  que sumar lo mismo.
- `revisar_respuesta`, con otro modelo: lee la pregunta, los datos que de
  verdad salieron de la base y el borrador, y comprueba que cada cifra este
  respaldada, que el periodo, la cadena y la marca sean los que se pidieron,
  y que no se hable de lo que el usuario no puede ver. Si algo no cuadra,
  devuelve el mensaje corregido.

Las dos fallan abiertas: si el revisor no contesta, la respuesta sale como
estaba (y queda en el log). Un revisor caido no puede dejar sin respuesta a
nadie.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

import httpx

import proveedor
import pandas as pd

import acceso

logger = logging.getLogger("sql_agent.revisor")

ACTIVO = os.getenv("REVISOR", "1").strip() not in ("0", "no", "false", "")
MODELO = proveedor.modelo("REVISOR_MODEL") or proveedor.modelo("MODEL", "openai/gpt-6-luna")
SEGUNDOS_DE_PLAZO = 45
# Lo que se le muestra al revisor de cada resultado y en total: los datos
# que vio el modelo que contesto, no mas (un Raw_Data entero no cabe ni hace
# falta: el mensaje se escribio con esto).
MAX_POR_RESULTADO = 6000
MAX_EN_TOTAL = 24000
# De un resultado mas largo va el principio y este final: despues de las
# filas van el total y las notas. Visto el 5/11/2026: de 456,133 filas de
# Coppel el revisor solo vio el principio y quito el total (545,787), que
# estaba bien.
FINAL_DE_UN_RESULTADO_LARGO = 2000

# Hojas que SON lo que se pidio: el resultado y el detalle de su cadena.
_HOJAS_DEL_RESULTADO = {"Datos", "Raw_Data", "Consulta"}
_ES_COLUMNA_DE_CADENA = re.compile(r"^\s*(retail|retailname|retailer|cadena|customer)\s*$", re.IGNORECASE)
# Diferencia tolerada entre el detalle y lo agrupado (redondeos de float).
TOLERANCIA_DE_CUADRE = 0.005


# --- Las hojas -------------------------------------------------------------

def _columna_de_cadena(df: pd.DataFrame) -> str | None:
    return next((c for c in df.columns if _ES_COLUMNA_DE_CADENA.match(str(c))), None)


def _cadenas_de(df: pd.DataFrame) -> set[str] | None:
    """Las cadenas que trae una hoja, o None si no tiene columna de cadena."""
    columna = _columna_de_cadena(df)
    if columna is None:
        return None
    return {c for c in (acceso.cadena_de(v) for v in df[columna].dropna().unique()) if c}


def cadenas_de_la_consulta(query: str) -> set[str]:
    """Las cadenas que lee la consulta por sus tablas de una sola cadena."""
    return {c for c in (acceso.de_quien(r.objeto) for r in acceso.referencias(query or "")) if c}


def _sin_cadenas_ajenas(df: pd.DataFrame, permitidas: frozenset[str]) -> tuple[pd.DataFrame, int]:
    columna = _columna_de_cadena(df)
    if columna is None:
        return df, 0
    cadena = df[columna].map(acceso.cadena_de)
    ajenas = cadena.notna() & ~cadena.isin(permitidas)
    if not ajenas.any():
        return df, 0
    limpio = df.loc[~ajenas].copy()
    limpio.attrs = dict(df.attrs)
    return limpio, int(ajenas.sum())


def _unidades(df: pd.DataFrame, columna: str | None) -> float | None:
    if columna and columna in df:
        return float(pd.to_numeric(df[columna], errors="coerce").sum())
    return None


@dataclass
class RevisionDeHojas:
    df: pd.DataFrame
    contexto: dict
    hallazgos: list[str] = field(default_factory=list)


def revisar_hojas(df: pd.DataFrame, contexto: dict, cadenas=None, query: str = "") -> RevisionDeHojas:
    """El resultado y sus hojas, sin lo que no debe ir, y que se encontro."""
    hallazgos: list[str] = []
    contexto = dict(contexto or {})
    permitidas = acceso.normalizar(cadenas)

    # Permisos: ninguna fila de una cadena que el usuario no tiene. La
    # consulta ya va acotada (acceso.py); esto es la red por si algo se
    # colara por otro camino, por ejemplo una hoja de contexto.
    if permitidas:
        df, quitadas = _sin_cadenas_ajenas(df, permitidas)
        if quitadas:
            hallazgos.append(f"se quitaron {quitadas} filas de cadenas sin permiso del resultado")
        for nombre in list(contexto):
            limpio, quitadas = _sin_cadenas_ajenas(contexto[nombre], permitidas)
            if quitadas:
                hallazgos.append(f"se quitaron {quitadas} filas de cadenas sin permiso de la hoja {nombre!r}")
                contexto[nombre] = limpio

    # Alcance: una hoja de apoyo no puede traer cadenas que no estan en lo
    # que se pidio. Visto el 7/10/2026: "Ventas por tienda" con las tiendas
    # de Telcel en un reporte de Liverpool region 4.
    del_resultado = _cadenas_de(df)
    if not del_resultado:
        del_resultado = cadenas_de_la_consulta(query) or None
    if del_resultado:
        for nombre in list(contexto):
            if nombre in _HOJAS_DEL_RESULTADO:
                continue
            de_la_hoja = _cadenas_de(contexto[nombre])
            if de_la_hoja and de_la_hoja - del_resultado:
                ajenas = ", ".join(sorted(acceso.NOMBRES.get(c, c) for c in de_la_hoja - del_resultado))
                hallazgos.append(f"se quito la hoja {nombre!r}: traia {ajenas}, que no estan en lo que se pidio")
                del contexto[nombre]

    # Cuadre: el detalle y lo agrupado son la misma venta.
    crudo, agrupado = contexto.get("Raw_Data"), contexto.get("Datos")
    if crudo is not None and agrupado is not None:
        columna = (crudo.attrs.get("pivot") or {}).get("unidades")
        detalle, resumen = _unidades(crudo, columna), _unidades(agrupado, "Unidades")
        if detalle and resumen is not None and abs(detalle - resumen) > TOLERANCIA_DE_CUADRE * detalle:
            hallazgos.append(
                f"el detalle suma {detalle:,.0f} unidades y lo agrupado {resumen:,.0f}: no cuadran"
            )
    return RevisionDeHojas(df=df, contexto=contexto, hallazgos=hallazgos)


# --- La respuesta ----------------------------------------------------------

INSTRUCCION = """Eres el revisor de un bot de ventas de celulares en Mexico que contesta por WhatsApp.
Antes de que el mensaje salga, compruebas que sea cierto. Recibes la PREGUNTA del usuario, los
DATOS que de verdad salieron de la base en este turno (lo unico que cuenta como verdad) y el
BORRADOR del mensaje.

Revisa:
1. Cada cifra del borrador (unidades, porcentajes, precios, semanas, promotores, fechas) tiene que
   estar en los DATOS o salir de una cuenta directa con ellos (una suma, una resta, un porcentaje).
   Una cifra que no se puede sacar de los DATOS es un error.
2. El periodo, la cadena, la region, la marca y el modelo de los DATOS tienen que ser los que pidio
   la PREGUNTA. Si pidio 2025 y los datos son de 2024, o pidio Liverpool y los datos son de otra
   cadena, es un error.
3. {acceso}
4. El mensaje contesta como una persona, con cifras de negocio: no habla de filas, registros,
   hojas, columnas, Excel, ZIP ni del archivo adjunto.
5. Venta e inventario no se confunden. La venta de los DATOS es SELL OUT: lo que la cadena le
   vendio al cliente final. El SELL IN (lo que Honor le vendio o embarco a la cadena) NO esta en
   la base: llamar sell in a la venta es un error, y decir que el sell in no esta disponible es
   correcto. Presentar inventario como venta, o venta como inventario, tambien es un error.

Un error es algo que los DATOS CONTRADICEN, no algo que no repiten: una herramienta o consulta
que se pidio para un modelo, cadena o periodo ya viene filtrada a eso aunque cada renglon no lo
diga. Si las cifras del borrador estan en los DATOS y nada en ellos dice que sean de otra cosa,
esta bien. Ante la duda, {{"ok": true}}: cambiar una respuesta correcta por "no puedo confirmar"
es peor que dejarla.

Contesta SOLO con un objeto JSON, sin nada mas:
- Si todo esta bien: {{"ok": true}}
- Si algo esta mal: {{"ok": false, "problemas": ["..."], "respuesta": "..."}}
  "problemas" dice en una frase corta cada error. "respuesta" es el mensaje corregido: mismo
  idioma, mismo tono y largo parecido, con las cifras de los DATOS. NUNCA inventes una cifra: si
  los DATOS no alcanzan para contestar lo que se pidio, dilo en una frase sencilla. Conserva las
  negritas (**) de la cifra principal."""


@dataclass
class RevisionDeRespuesta:
    ok: bool
    problemas: list[str] = field(default_factory=list)
    texto: str = ""


def _objeto_de_la_respuesta(contenido: str) -> dict:
    texto = (contenido or "").strip()
    inicio, fin = texto.find("{"), texto.rfind("}")
    if inicio < 0 or fin < inicio:
        raise ValueError(f"el revisor no contesto un JSON: {texto[:200]}")
    return json.loads(texto[inicio:fin + 1])


def _datos_para_el_revisor(vistos: list[str]) -> str:
    partes, largo = [], 0
    for visto in vistos:
        trozo = visto
        if len(visto) > MAX_POR_RESULTADO:
            trozo = (visto[:MAX_POR_RESULTADO - FINAL_DE_UN_RESULTADO_LARGO] + "\n[...]\n"
                     + visto[-FINAL_DE_UN_RESULTADO_LARGO:])
        if largo + len(trozo) > MAX_EN_TOTAL:
            break
        partes.append(trozo)
        largo += len(trozo)
    return "\n\n---\n\n".join(partes)


def revisar_respuesta(pregunta: str, vistos: list[str], borrador: str, cadenas=None) -> RevisionDeRespuesta:
    """Si el borrador dice la verdad sobre los datos, y si no, corregido."""
    if not ACTIVO or not vistos or not (borrador or "").strip():
        return RevisionDeRespuesta(ok=True)
    if not proveedor.clave():
        return RevisionDeRespuesta(ok=True)
    permitidas = acceso.normalizar(cadenas)
    regla_de_acceso = (
        f"El usuario SOLO puede ver datos de {acceso.nombres(permitidas)}. Si el borrador da "
        "cifras o nombres de tiendas de otra cadena, es un error grave."
        if permitidas else
        "El usuario puede ver todas las cadenas; no hay nada que revisar de permisos."
    )
    cuerpo = {
        "model": MODELO,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": INSTRUCCION.format(acceso=regla_de_acceso)},
            {"role": "user", "content": (
                f"PREGUNTA:\n{pregunta}\n\nDATOS:\n{_datos_para_el_revisor(vistos)}\n\n"
                f"BORRADOR:\n{borrador}"
            )},
        ],
    }
    try:
        respuesta = httpx.post(
            proveedor.url_chat(), json=cuerpo, timeout=SEGUNDOS_DE_PLAZO,
            headers=proveedor.encabezados(),
        )
        respuesta.raise_for_status()
        veredicto = _objeto_de_la_respuesta(respuesta.json()["choices"][0]["message"]["content"])
    except Exception:  # noqa: BLE001 - un revisor caido no puede tumbar la respuesta
        logger.warning("El revisor no contesto; la respuesta sale sin revisar", exc_info=True)
        return RevisionDeRespuesta(ok=True)

    if veredicto.get("ok") is True:
        return RevisionDeRespuesta(ok=True)
    problemas = [str(p) for p in veredicto.get("problemas") or []]
    corregida = str(veredicto.get("respuesta") or "").strip()
    # Un "corregido" vacio o desproporcionado no reemplaza al borrador: se
    # anota el problema y sale lo que habia.
    if not corregida or len(corregida) > 3 * len(borrador) + 400:
        corregida = ""
    return RevisionDeRespuesta(ok=False, problemas=problemas, texto=corregida)
