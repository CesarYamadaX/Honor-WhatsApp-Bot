"""Que columnas de un resultado dicen CUANDO paso algo, y como ordenarlo.

Vive aparte porque tres modulos necesitan lo mismo y por caminos distintos:
`grafica` para no dibujar [Año] y [Mes] como si fueran ventas, `contexto`
para saber que periodo cubre el reporte, y `excel_exporter` para entregar el
archivo en orden. Tres copias del mismo par de expresiones regulares se
separan a la primera correccion que solo se aplica a una.

El agente parte la fecha en columnas porque asi la agrupa el GROUP BY, y
quedan como numeros indistinguibles de una medida: de ahi salen los dos
fallos que este modulo evita, una barra de valor 2026 junto a una de 143 575,
y un archivo de meses que empieza en agosto porque la consulta venia ordenada
por cantidad.
"""

import re

import pandas as pd

ES_ANIO = re.compile(r"^\s*\[?\s*(a[nñ]o|anio|year)\s*\]?\s*$", re.IGNORECASE)
ES_MES = re.compile(r"^\s*\[?\s*(mes|month)\s*\]?\s*$", re.IGNORECASE)


def columna(df: pd.DataFrame, patron: re.Pattern):
    """La primera columna cuyo nombre case con el patron, o None."""
    for nombre in df.columns:
        if patron.match(str(nombre)):
            return nombre
    return None


def enteros(serie: pd.Series):
    """La columna como enteros, o None si alguna celda no lo es.

    Sirve para distinguir un [Mes] de 1 a 12 de uno escrito "enero": el
    segundo no se puede ordenar como numero y tampoco se debe.
    """
    numeros = pd.to_numeric(serie, errors="coerce")
    if numeros.isna().any():
        return None
    return numeros.astype(int)


def columna_de_fecha(df: pd.DataFrame):
    """La primera columna de tipo fecha, o None."""
    for nombre in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[nombre]):
            return nombre
    return None


def ordenar_por_tiempo(df: pd.DataFrame) -> pd.DataFrame:
    """El resultado en orden cronologico, del mes mas viejo al mas reciente.

    Un reporte de meses se pidio ordenado asi. El agente casi nunca lo
    devuelve asi: escribe `ORDER BY SUM([QTY]) DESC` porque esta buscando los
    mas vendidos, y el archivo sale con agosto arriba, enero en medio y marzo
    al final. Se reordena aqui, al entregarlo, y no pidiendoselo al modelo en
    el prompt.

    El orden es ESTABLE: dentro de cada mes se conserva el orden que traia la
    consulta, asi que un reporte por mes y modelo queda cronologico por fuera
    y con el modelo mas vendido arriba dentro de cada mes. Y si el resultado
    no tiene tiempo, se devuelve tal cual: ahi el orden de la consulta es la
    respuesta (un top 10 es un top 10).
    """
    if df is None or df.empty:
        return df

    fecha = columna_de_fecha(df)
    if fecha is not None:
        return df.sort_values(fecha, kind="stable").reset_index(drop=True)

    anio, mes = columna(df, ES_ANIO), columna(df, ES_MES)
    claves = [c for c in (anio, mes) if c is not None and enteros(df[c]) is not None]
    if not claves:
        return df
    return df.sort_values(claves, kind="stable").reset_index(drop=True)
