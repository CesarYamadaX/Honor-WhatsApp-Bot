"""Las hojas que acompañan al resultado: ventas por tienda, y la ficha de lo
que sale en el.

El reporte llevaba solo la tabla de la consulta: si decia "TELCEL CAC" o
"SAMSUNG A07 64GB 4G", quien lo abria no tenia forma de saber en que region
esta esa tienda ni de que marca es ese modelo sin volver a preguntarle al bot.

La hoja principal de contexto es el DETALLE POR TIENDA: para los modelos que
salen en el resultado, cuanto vendio cada tienda. Antes ahi iba un volcado de
UNICOS$ -- el catalogo de homologacion --, y no servia: para un resultado de
635 filas metia 2001 filas de catalogo, que son las formas en que cada cadena
escribe esos modelos. Eso no es detalle de ventas, es ruido.

Las otras hojas (tiendas, regiones, carriers) si son catalogo, y son chicas:
CustomerStores 7k filas, STATES_AND_REGIONS 8.6k, HonorCarriers 55. Responden
en decimas de segundo.

El detalle por tienda va por PERIODO, no por modelo, y eso no es una
simplificacion: es lo unico que se puede calcular bien desde la vista
consolidada. La primera version filtraba los modelos del resultado uniendo
la consolidada con UNICOS$ por [DESCRIPCION], y esa union esta mal por dos
lados a la vez. Medido en agosto de 2026: de 2 030 760 unidades del mes solo
casaban 165 941, el 8%, porque en la consolidada [Material_Description] es
SKU + retailer + descripcion ("N.I15128N AT&T Cacs APPLE IPHONE 15 128GB
4G") y nunca va a ser igual a una descripcion pelada. Y de lo poco que
casaba, una parte se contaba de mas: UNICOS$ repite 1 249 descripciones y
algunas apuntan a 25 homologaciones distintas, asi que sin [Retail] en el ON
una misma venta se multiplica y ademas puede quedar bajo el modelo
equivocado. Las propias reglas del catalogo ya lo prohibian ("PROHIBIDO: una
vista vw_ETL_* con u.[DESCRIPCION]"), y la consolidada es la union de esas
vistas.

Sin la homologacion de por medio la hoja es correcta, es mas rapida (16s un
mes, 39s medio ano, medido) y es lo que se habia pedido: el detalle de las
ventas por tienda, no el catalogo. Igual lleva su propio presupuesto de
tiempo: si no llega, el reporte sale sin esa hoja en vez de hacer esperar al
usuario.
"""

import logging

import pandas as pd

import tiempo

logger = logging.getLogger("sql_agent.contexto")

GTM = "[GTM BI Database].dbo"
VENTAS = "[RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B]"

# Cada entrada: hoja, tabla, y las columnas por las que se intenta casar los
# valores que aparecen en el resultado.
REFERENCIAS = (
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

# Lo que se le da al desglose antes de rendirse. El reporte ya costo minutos;
# esta hoja es un extra y no puede duplicar la espera.
SEGUNDOS_PARA_EL_DESGLOSE = 90


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


def periodo(df: pd.DataFrame) -> tuple[str, str] | None:
    """El rango de fechas del resultado, como (desde, hasta_exclusivo).

    Sin periodo no se desglosa: una consulta por modelo sin acotar la fecha
    recorre la vista entera, que es la operacion cara que este modulo
    justamente evita.
    """
    for nombre in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[nombre]):
            fechas = df[nombre].dropna()
            if fechas.empty:
                continue
            desde = fechas.min().normalize()
            hasta = fechas.max().normalize() + pd.Timedelta(days=1)
            return desde.strftime("%Y-%m-%d"), hasta.strftime("%Y-%m-%d")

    anio = tiempo.columna(df, tiempo.ES_ANIO)
    mes = tiempo.columna(df, tiempo.ES_MES)
    if anio is None:
        return None
    anios = pd.to_numeric(df[anio], errors="coerce").dropna()
    if anios.empty:
        return None

    if mes is None:
        return f"{int(anios.min())}-01-01", f"{int(anios.max()) + 1}-01-01"

    meses = pd.to_numeric(df[mes], errors="coerce").dropna()
    if meses.empty:
        return f"{int(anios.min())}-01-01", f"{int(anios.max()) + 1}-01-01"

    desde = pd.Timestamp(int(anios.min()), int(meses.min()), 1)
    fin = pd.Timestamp(int(anios.max()), int(meses.max()), 1)
    return desde.strftime("%Y-%m-%d"), (fin + pd.offsets.MonthBegin(1)).strftime("%Y-%m-%d")


def _ventas_por_tienda(conexion, desde: str, hasta: str):
    """Cuanto vendio cada tienda en ese periodo, la que mas primero.

    Son todas las marcas: desde la consolidada no se puede acotar por modelo
    sin pasar por una union que pierde el 92% del volumen (ver el encabezado
    del modulo). La hoja dice donde estuvo el volumen del periodo.
    """
    sql = f"""
        SELECT TOP {MAX_FILAS_POR_HOJA}
               v.[Retail], v.[Store], SUM(v.[QTY]) AS [Unidades]
        FROM {VENTAS} v
        WHERE v.[Date] >= ? AND v.[Date] < ?
        GROUP BY v.[Retail], v.[Store]
        ORDER BY SUM(v.[QTY]) DESC
    """
    cursor = conexion.exec_driver_sql(sql, (desde, hasta))
    filas = cursor.fetchall()
    return pd.DataFrame.from_records(filas, columns=list(cursor.keys()))


def contexto_del_resultado(df: pd.DataFrame, engine) -> dict[str, pd.DataFrame]:
    """Hojas de contexto para el resultado, vacio si no hay nada que agregar."""
    if df is None or df.empty:
        return {}

    hojas: dict[str, pd.DataFrame] = {}

    # El desglose por tienda va primero: es lo que se pidio ver, y lo unico
    # que cuesta tiempo. Si falla o no aplica, el resto sigue.
    rango = periodo(df)
    if rango:
        try:
            with engine.connect() as conexion:
                conexion.connection.dbapi_connection.timeout = SEGUNDOS_PARA_EL_DESGLOSE
                ventas = _ventas_por_tienda(conexion, *rango)
            if not ventas.empty:
                hojas["Ventas por tienda"] = ventas
        except Exception:  # noqa: BLE001
            # El reporte ya esta listo; esta hoja es un extra. Que la vista de
            # ventas no conteste a tiempo no puede costar la respuesta.
            logger.exception("No se pudo desglosar por tienda")

    valores = _valores_de_texto(df)
    if not valores:
        return hojas

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
