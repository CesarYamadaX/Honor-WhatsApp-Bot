"""Que mas se puede preguntar, despues de contestar lo que se pregunto.

Una respuesta del bot termina en un numero y ahi se acaba la conversacion. El
dato que sigue --cuanto de eso fue HONOR, que tienda lo hizo, como venia el
ano pasado-- esta a una consulta de distancia, pero solo lo pide quien ya
sabe que se puede pedir. Esto lo dice.

Las sugerencias salen de las COLUMNAS del resultado y de la TABLA de donde
salio, no del modelo. De las columnas, para no ofrecer lo que ya estaba en la
respuesta: si la tabla entregada ya trae tienda, el detalle por tienda no es
novedad. De la tabla, para no ofrecer un sinsentido: el share de HONOR sobre
un reporte de promotoria seria el porcentaje de marca de unos promotores.

Cada clave de aqui corresponde a una consulta que las notas del catalogo ya
explican como escribir, asi que lo que se ofrece el bot lo sabe hacer.
"""

import re

import pandas as pd

import tiempo
from pronostico import pidio_pronostico

# Cuantas se mandan. Dos caben en una linea de telefono y se leen; cinco son
# un menu, y un menu al final de cada respuesta es ruido que se aprende a
# saltar.
MAX_SUGERENCIAS = 2

_ES_TIENDA = re.compile(r"tienda|store|sucursal|coppel name|honor name", re.IGNORECASE)
_ES_MARCA = re.compile(r"marca|brand", re.IGNORECASE)
_ES_MODELO = re.compile(
    r"modelo|model|homologacion|material.?description", re.IGNORECASE
)
_ES_RETAIL = re.compile(r"retail|cadena", re.IGNORECASE)
_ES_SHARE = re.compile(r"share|participacion|porcentaje", re.IGNORECASE)
_ES_CAPA = re.compile(r"\bcapa\b|capacidad", re.IGNORECASE)

# De donde salio la consulta. Lo que se puede preguntar despues depende de
# que se consulto: sobre venta se puede preguntar por marca y por modelo,
# sobre promotoria no.
_DE_VENTA = re.compile(
    r"RAW_DATA_B2B|B2BHonorGeneralReport|vw_ETL_|_B2B\b|_IOT\b", re.IGNORECASE
)
_DE_PROMOTORIA = re.compile(r"HCReport", re.IGNORECASE)

# Vistas y tablas de UN solo retailer: si la consulta salio de una de ellas,
# comparar contra las demas cadenas es algo que la tabla entregada no
# contesta. La consolidada ya las trae todas.
_UN_SOLO_RETAILER = re.compile(r"vw_ETL_|_B2B\b|_IOT\b", re.IGNORECASE)
_CONSOLIDADO = re.compile(r"ETL_Consolidado_B2B", re.IGNORECASE)

# La consulta ya se acoto a una marca ([Brand] = 'HONOR'). Ahi "que parte de
# eso fue HONOR" no significa nada -- es el 100% --, pero la pregunta que
# sigue SI es interesante: cuanto pesa ese numero dentro del mercado.
_FILTRA_MARCA = re.compile(r"\[?brand\]?\s*(=|in)\s*", re.IGNORECASE)


def _tiene(df: pd.DataFrame, patron: re.Pattern) -> bool:
    return any(patron.search(str(c)) for c in df.columns)


def sugerir(pregunta: str, df: pd.DataFrame, query: str) -> list[str]:
    """Las claves de lo que se puede preguntar despues, las mas utiles primero.

    Se devuelven claves y no texto porque el texto depende del idioma del
    usuario; lo arma `idioma.sugerencias`.
    """
    # Un SUM sobre cero filas devuelve UNA fila con NULL: no hubo datos, y
    # ofrecer "que share fue eso" despues de "no encontre ventas de ayer"
    # (visto el 9/10/2026) no tiene sentido.
    if df is None or df.empty or bool(df.isna().all().all()):
        return []

    pregunta, query = pregunta or "", query or ""
    de_venta = bool(_DE_VENTA.search(query))
    de_promotoria = bool(_DE_PROMOTORIA.search(query))
    claves: list[str] = []

    # Cuanto de eso fue HONOR. Es la pregunta de negocio que sigue a casi
    # cualquier cifra de venta, y la tabla entregada no la contesta ni cuando
    # filtro la marca (ahi falta el total) ni cuando no la filtro (ahi falta
    # separar la marca).
    if de_venta and not _tiene(df, _ES_MARCA) and not _ES_SHARE.search(pregunta):
        claves.append(
            "share_sobre_el_mercado" if _FILTRA_MARCA.search(query) else "share_honor"
        )

    # Contra que. Sin columna de tiempo no hay periodo con que comparar, y
    # sobre un pronostico no se ofrece: el metodo ya es el mismo mes del ano
    # anterior, asi que ofrecerlo seria ofrecer lo que acaba de usar.
    tiene_tiempo = (
        tiempo.columna_de_fecha(df) is not None
        or tiempo.columna(df, tiempo.ES_ANIO) is not None
    )
    if tiene_tiempo and not pidio_pronostico(pregunta):
        claves.append("mismo_periodo_anterior")

    # Quien lo hizo. Un total no dice que tienda lo movio.
    if not _tiene(df, _ES_TIENDA):
        claves.append("por_tienda")
    elif de_promotoria:
        # Hay tiendas y son de promotoria: la pregunta que sigue es si esas
        # tiendas venden distinto, que es cruzar las dos mitades del negocio.
        claves.append("ventas_de_esas_tiendas")
    elif de_venta and not _ES_CAPA.search(pregunta):
        # Hay tiendas y son de venta: lo que falta es con que comparar sus
        # numeros.
        claves.append("capa")

    if de_venta and not _tiene(df, _ES_MODELO):
        claves.append("por_modelo")

    # Salio de la vista de un solo retailer y no trae columna de cadena: no
    # hay forma de saber desde ahi como les fue a las demas.
    if (
        de_venta
        and not _tiene(df, _ES_RETAIL)
        and _UN_SOLO_RETAILER.search(query)
        and not _CONSOLIDADO.search(query)
    ):
        claves.append("otros_retailers")

    if de_promotoria and "ventas_de_esas_tiendas" not in claves:
        claves.append("ventas_de_esas_tiendas")

    return claves[:MAX_SUGERENCIAS]
