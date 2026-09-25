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
import statistics

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


def medida(df: pd.DataFrame):
    """La columna numerica que MIDE algo, o None.

    No basta con "la primera numerica": en un resultado por mes, la primera
    numerica es [Año], y ordenar o comparar por ella es hacerlo por 2026 --
    o sea, por numero de filas. Las columnas de periodo dicen CUANDO paso
    algo, no cuanto.
    """
    for nombre in df.columns:
        if not pd.api.types.is_numeric_dtype(df[nombre]):
            continue
        texto = str(nombre)
        if ES_ANIO.match(texto) or ES_MES.match(texto):
            continue
        return nombre
    return None


# Un periodo que apenas es una fraccion de los anteriores no termino de
# cargar. El umbral es el mismo que pronostico.py ya tenia validado contra
# la historia completa de Coppel y Elektra: no marca ningun mes legitimo, y
# el mas bajo de verdad quedo en 59%.
FRACCION_DE_PERIODO_INCOMPLETO = 0.40

# Cuantos periodos anteriores se miran para saber que es "normal". Con menos
# de esto no hay con que comparar, y no se marca nada.
PERIODOS_PARA_COMPARAR = 3


def serie_por_periodo(df: pd.DataFrame):
    """(etiquetas, totales) del resultado agrupado por periodo, en orden.

    None si el resultado no tiene una linea de tiempo que agrupar.
    """
    col_medida = medida(df)
    if col_medida is None:
        return None

    fecha = columna_de_fecha(df)
    if fecha is not None:
        etiquetas = pd.to_datetime(df[fecha]).dt.strftime("%Y-%m")
    else:
        anio, mes = columna(df, ES_ANIO), columna(df, ES_MES)
        if anio is None or mes is None:
            return None
        anios, meses = enteros(df[anio]), enteros(df[mes])
        if anios is None or meses is None:
            return None
        etiquetas = anios.astype(str) + "-" + meses.astype(str).str.zfill(2)

    serie = (
        pd.DataFrame(
            {"periodo": list(etiquetas), "valor": list(df[col_medida])}
        )
        .groupby("periodo", sort=True)["valor"]
        .sum()
    )
    return list(serie.index), list(serie.values)


def ultimo_periodo_incompleto(df: pd.DataFrame) -> str | None:
    """La etiqueta del ultimo periodo si viene a medio cargar, o None.

    Pasa de verdad y engana feo: el 24 de septiembre de 2026, "ventas de
    Honor por mes" devolvia 15 584 unidades para septiembre contra ~160 000
    de agosto -- no porque las ventas se hubieran desplomado, sino porque el
    mes iba a la mitad y ademas la carga venia atrasada (datos hasta el dia
    20, el 15% de lo que llevaria un mes normal a esa altura). En una
    grafica eso es un precipicio que no existe, y en el texto es un numero
    que invita a una junta de emergencia.

    Se usa el mismo criterio con el que el pronostico ya descartaba el mes
    en curso, y por el mismo motivo.
    """
    serie = serie_por_periodo(df)
    if serie is None:
        return None
    etiquetas, valores = serie
    if len(etiquetas) < PERIODOS_PARA_COMPARAR + 1:
        return None

    try:
        anteriores = [float(v) for v in valores[-(PERIODOS_PARA_COMPARAR + 1):-1]]
        ultimo = float(valores[-1])
    except (TypeError, ValueError):
        return None

    referencia = statistics.median(anteriores)
    if referencia <= 0:
        return None
    if ultimo / referencia < FRACCION_DE_PERIODO_INCOMPLETO:
        return str(etiquetas[-1])
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
