"""Trae la ficha de las tiendas, modelos y carriers que salen en un resultado.

El reporte llevaba solo la tabla de la consulta: si decia "TELCEL CAC" o
"SAMSUNG A07 64GB 4G", quien lo abria no tenia forma de saber en que region
esta esa tienda ni de que marca o tipo es ese modelo sin volver a preguntarle
al bot. Esto agrega esa ficha como hojas aparte, sin tocar la primera.

Se consultan solo tablas de catalogo, que son chicas (UNICOS$ 184k filas,
CustomerStores 7k, STATES_AND_REGIONS 8.6k, HonorCarriers 55) y responden en
decimas de segundo, a diferencia de las vistas de venta.
"""

import logging

import pandas as pd

logger = logging.getLogger("sql_agent.contexto")

GTM = "[GTM BI Database].dbo"

# Cada entrada: hoja, tabla, y las columnas por las que se intenta casar los
# valores que aparecen en el resultado.
REFERENCIAS = (
    (
        "Modelos",
        f"{GTM}.[UNICOS$]",
        ("HOMOLOGACION MODELO", "MATERIAL DESCRIPTION FINAL", "DESCRIPCION"),
    ),
    ("Tiendas", f"{GTM}.[CustomerStores]", ("ID", "HONOR NAME", "Coppel Name")),
    ("Regiones", f"{GTM}.[STATES_AND_REGIONS]", ("ID", "NAME")),
    ("Carriers", f"{GTM}.[HonorCarriers]", ("Carrier",)),
)

# Una columna con mas valores distintos que esto no es una dimension (es un
# identificador o texto libre) y buscarla en los catalogos no aporta nada.
MAX_VALORES_POR_COLUMNA = 200
# Tope de filas por hoja de contexto: la ficha acompaña al resultado, no lo
# sepulta.
MAX_FILAS_POR_HOJA = 2000


def _valores_de_texto(df: pd.DataFrame) -> list[str]:
    """Los valores distintos de las columnas no numericas del resultado."""
    valores: set[str] = set()
    for nombre in df.columns:
        columna = df[nombre]
        if pd.api.types.is_numeric_dtype(columna) or pd.api.types.is_datetime64_any_dtype(
            columna
        ):
            continue
        distintos = columna.dropna().astype(str).unique()
        if len(distintos) > MAX_VALORES_POR_COLUMNA:
            continue
        valores.update(v for v in distintos if v.strip())
    return sorted(valores)


def _buscar(conexion, tabla: str, columnas: tuple[str, ...], valores: list[str]):
    """Filas del catalogo cuyo valor en alguna de esas columnas aparece en el
    resultado. Los valores van como parametros, nunca pegados al SQL."""
    condiciones = " OR ".join(
        f"[{c}] IN ({', '.join('?' * len(valores))})" for c in columnas
    )
    sql = f"SELECT DISTINCT TOP {MAX_FILAS_POR_HOJA} * FROM {tabla} WHERE {condiciones}"
    cursor = conexion.exec_driver_sql(sql, tuple(valores) * len(columnas))
    filas = cursor.fetchall()
    return pd.DataFrame.from_records(filas, columns=list(cursor.keys()))


def contexto_del_resultado(df: pd.DataFrame, engine) -> dict[str, pd.DataFrame]:
    """Hojas de contexto para el resultado, vacio si no hay nada que agregar."""
    if df is None or df.empty:
        return {}
    valores = _valores_de_texto(df)
    if not valores:
        return {}

    hojas: dict[str, pd.DataFrame] = {}
    with engine.connect() as conexion:
        for hoja, tabla, columnas in REFERENCIAS:
            try:
                encontradas = _buscar(conexion, tabla, columnas, valores)
            except Exception:  # noqa: BLE001
                # Un catalogo que no responde no puede costar el reporte: la
                # primera hoja, que es lo que se pidio, ya esta lista.
                logger.exception("No se pudo traer el contexto de %s", tabla)
                continue
            if not encontradas.empty:
                hojas[hoja] = encontradas
    return hojas
