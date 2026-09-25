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
import re
from dataclasses import dataclass

import pandas as pd

import tiempo

logger = logging.getLogger("sql_agent.contexto")

GTM = "[GTM BI Database].dbo"
VENTAS = "[RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B]"
UNICOS = f"{GTM}.[UNICOS$]"

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
# Tope de filas por hoja de CATALOGO (Tiendas, Regiones, Carriers): la ficha
# acompaña al resultado, no lo sepulta. Un volcado de CustomerStores entero
# (7k filas) no es contexto, es ruido.
MAX_FILAS_POR_HOJA = 2000

# Tope de las hojas de VENTA, que es otra cosa: ahi cortar no es "menos
# ruido", es un total que no cuadra. Con el tope de 2 000, "ventas de Honor
# por mes en 2026" entregaba un mensaje que decia 1 634 670 unidades y una
# hoja que sumaba 1 381 369 -- el 84% --, sin decir en ningun lado que
# estaba cortada.
#
# Medido el 25 de septiembre de 2026, estas hojas no se pueden desbordar: el
# techo lo pone cuantas tiendas existen, no el periodo. Retail x Store en la
# consolidada da 6 626 filas para un mes y 9 760 para el ano entero con
# todas las marcas, que es el peor caso. La de tienda x modelo va acotada
# ademas a MAX_MODELOS_DESGLOSADOS, asi que su techo son ~25 000 (1 700
# tiendas de Coppel x 15 modelos).
#
# 80 000 deja margen de sobra sin acercarse al limite de adjunto de WhatsApp
# (16 MB): a ~7-8 bytes por celda, 80 000 filas x 4 columnas pesan bajo 3 MB.
MAX_FILAS_DE_VENTA = 80000

# Lo que se le da al desglose antes de rendirse. El reporte ya costo minutos;
# esta hoja es un extra y no puede duplicar la espera.
SEGUNDOS_PARA_EL_DESGLOSE = 90

# Hasta cuantas cadenas se separan en hojas propias. La vista consolidada
# tiene 16 valores de [Retail] -- Telcel sola aporta seis canales -- y ese es
# el tope real; mas que eso no es una columna de cadena.
MAX_HOJAS_POR_CADENA = 16

# Y cuantas filas tiene que tener en promedio cada cadena para que separarlas
# ayude. Un reporte de 48 filas y 16 cadenas son tres filas por pestaña: ahi
# separar no ayuda a comparar, estorba. La pestaña propia sirve cuando la
# cadena trae una tabla de verdad.
MIN_FILAS_POR_CADENA = 10

# Caracteres que Excel no acepta en el nombre de una hoja.
_PROHIBIDOS_EN_HOJA = ':\\/?*[]'

# Tope de filas para la hoja de datos crudos que alimenta el PIVOT. Se
# agrupa por tienda, marca y mes -- sin el modelo, que ya tiene su propia
# hoja mas fina ("Ventas por tienda") -- porque a nivel modelo la misma
# consulta de tres meses de Elektra da 127 602 filas, cuatro veces mas. Sin
# modelo, medido en agosto de 2026: Elektra 28 887, Coppel 58 066 (la tabla
# cruda mas grande, 12M filas). 80 000 deja margen sin acercarse al limite
# de adjunto de WhatsApp (16 MB por defecto): a ~7-8 bytes por celda, 80 000
# filas x 5 columnas pesan bajo 3 MB antes del pivot.
MAX_FILAS_RAW_DATA = 80000

# Cuantos modelos del resultado se desglosan. Un reporte con cien modelos no
# se lee de todos modos: se toman los mas vendidos del propio resultado.
MAX_MODELOS_DESGLOSADOS = 15

# Como se llama la columna de modelo en un resultado. El agente las nombra de
# varias formas segun como escriba el SELECT.
_ES_MODELO = re.compile(
    r".*(homologacion|modelo|model|material.?description)", re.IGNORECASE
)
_ES_RETAIL = re.compile(r".*(retail|cadena)", re.IGNORECASE)
_ES_MARCA = re.compile(r"^\[?(brand|marca)\]?$", re.IGNORECASE)

# `[Brand] = 'HONOR'` o `[Brand] IN ('HONOR','SAMSUNG')`, para cuando el
# resultado viene agregado y ya no trae la columna.
_MARCA_EN_SQL = re.compile(
    r"\[?\bbrand\b\]?\s*(?:=|in)\s*\(?\s*((?:'[^']*'\s*,?\s*)+)\)?",
    re.IGNORECASE,
)
_LITERAL = re.compile(r"'([^']*)'")

# Cuantos valores distintos puede tener un filtro antes de dejar de serlo. Si
# el resultado trae catorce marcas, no esta acotado a ninguna.
MAX_VALORES_ACOTADOS = 8


@dataclass(frozen=True)
class Cruda:
    """La tabla cruda de un retailer y las columnas que hay que mirar en ella.

    Cada cadena entrega su descarga con sus propios nombres: la tienda es
    [Tienda] en Coppel, [POS] en Elektra, [Venta] en Telcel y
    [Nombre de la Organización] en AT&T. Este mapa se verifico consultando
    las siete tablas, no leyendo el catalogo.

    `clave` y `etiqueta` son dos columnas distintas a proposito: se agrupa
    por la clave (el numero de tienda, que es estable) y se muestra la
    etiqueta (el nombre, que cambia). En Elektra la 6169 aparece como
    "EKT MEGA ZUMPANGO" y como "MEGA ZUMPANGO"; agrupar por el nombre la
    partiria en dos.
    """

    tabla: str
    retail_en_unicos: tuple[str, ...]
    # Con que texto de la venta se busca el modelo en UNICOS$, y contra cual
    # de sus dos columnas. No es lo mismo en todas las cadenas: Coppel y
    # Sears casan con [MATERIAL DESCRIPTION FINAL], que es una CONCATENACION
    # -- marca + descripcion en Coppel, la palabra "Sears" + descripcion en
    # Sears -- y las demas casan con [DESCRIPCION] a secas.
    llave: str
    columna_en_unicos: str
    clave: str
    etiqueta: str
    fecha: str
    unidades: str
    # Como se llama la cadena en CustomerStores.[RetailName], y como se
    # enlaza su tienda con la de la venta. Son dos formas distintas porque
    # las cadenas se identifican distinto: Coppel, Telcel, AT&T y Liverpool
    # por NOMBRE, y Elektra y Sears por NUMERO, que en el catalogo viene
    # entre parentesis dentro de [HONOR NAME] ("SEARS SATELITE (104)").
    retail_en_catalogo: str = ""
    enlace_con_catalogo: str = ""
    # La marca tal cual la escribe la tabla cruda. Existe en las seis, con
    # nombre distinto: [Marca] en Coppel/Elektra/Telcel, [Brand] en
    # Liverpool/Sears, [BRAND] en AT&T.
    marca: str = ""


# Medido en agosto de 2026: las seis consultan en 3 segundos o menos.
# Movistar no esta porque MVS_B2B no tiene columna de tienda -- su corte mas
# fino es el canal -- asi que para Movistar la hoja sale por periodo.
_POR_NOMBRE = "cs.[nombre] = v.[{etiqueta}]"
_POR_NUMERO = "cs.[numero] = TRY_CONVERT(int, v.[{clave}])"

_DESCRIPCION = "DESCRIPCION"
_FINAL = "MATERIAL DESCRIPTION FINAL"

# Medido en agosto de 2026, unidades que llegan a una homologacion:
#   Coppel    marca + modelo -> FINAL       100.0%   (contra 97.8% por DESCRIPCION)
#   Sears     'Sears ' + modelo -> FINAL    100.0%   (igual que por DESCRIPCION)
#   Elektra / Telcel / Liverpool -> DESCRIPCION  100.0%
#   AT&T      [Model] -> DESCRIPCION         89.0%   (la FINAL ahi casa 0%)
CRUDAS = {
    "COPPEL": Cruda("COPPEL_B2B", ("COPPEL",),
                    "CONCAT(v.[Marca], ' ', v.[Modelo])", _FINAL,
                    "Tienda", "Tienda", "Día", "Unidades", "COPPEL",
                    _POR_NOMBRE, marca="Marca"),
    "ELEKTRA": Cruda("EKT_B2B", ("ELEKTRA",), "v.[Desc Sku]", _DESCRIPCION,
                     "Tienda #", "POS", "Day", "QTY", "ELEKTRA", _POR_NUMERO,
                     marca="Marca"),
    "TELCEL": Cruda("TELCEL_B2B", ("TELCEL",), "v.[Descripción]", _DESCRIPCION,
                    "Venta", "Venta", "Fecha", "Cantidad", "TELCEL",
                    _POR_NOMBRE, marca="Marca"),
    "LIVERPOOL": Cruda("LIVERPOOL_B2B$", ("LIVERPOOL",),
                       "v.[Material Description]", _DESCRIPCION,
                       "Centro", "STORE", "Día/Periodo", "Vtas# U Per 1",
                       "LIVERPOOL", _POR_NOMBRE, marca="Brand"),
    # Los dos [Retail] de AT&T: con DEALERS solo se casa el 80.7% del mes, con
    # los dos el 88.9% (medido, agosto de 2026).
    "AT&T": Cruda("ATT_B2B", ("AT&T DEALERS", "AT&T CACS"), "v.[Model]",
                  _DESCRIPCION, "Codigo de Organización",
                  "Nombre de la Organización", "Fecha de Transacción",
                  "Cantidad", "AT&T", _POR_NOMBRE, marca="BRAND"),
    "SEARS": Cruda("SEARS_B2B", ("SEARS",), "CONCAT('Sears ', v.[Model])",
                   _FINAL, "#POS", "POS", "Fecha", "Total", "SEARS",
                   _POR_NUMERO, marca="Brand"),
}

# El numero de tienda que el catalogo esconde al final de [HONOR NAME], entre
# parentesis: "SEARS SATELITE (104)", "ELEKTRA MERIDA 2 (8649)". Es la unica
# forma de llegar a la region de esas dos cadenas: sus nombres no se parecen
# a los de la venta. El CASE evita el error de RIGHT() cuando no hay
# parentesis.
_NUMERO_DEL_CATALOGO = (
    "CASE WHEN CHARINDEX('(', REVERSE([HONOR NAME])) > 1 THEN "
    "TRY_CONVERT(int, REPLACE(RIGHT([HONOR NAME], "
    "CHARINDEX('(', REVERSE([HONOR NAME])) - 1), ')', '')) END"
)

# Quita el NBSP que a veces se cuela como espacio, colapsa las corridas de
# espacio y recorta las puntas. Se aplica a los DOS lados antes de comparar.
_NORMALIZAR = (
    "LTRIM(RTRIM(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE({expr}, "
    "NCHAR(160), ' '), '  ', ' '), '  ', ' '), '  ', ' '), '  ', ' ')))"
)


def _join_region(cruda: Cruda) -> tuple[str, str, tuple]:
    """El SQL para llegar a la region de una cruda: los JOIN, la expresion
    de la columna, y los parametros que le faltan.

    Las cadenas que se identifican por NOMBRE (Coppel, Telcel, AT&T,
    Liverpool) llevan ademas un SEGUNDO intento con los nombres
    normalizados -- sin el NBSP que a veces se cuela ni espacios de mas --,
    porque ahi es donde de verdad ayuda: medido en Liverpool, "L  TAMPICO"
    (dos espacios) no casaba con "L TAMPICO" del catalogo, y solo eso
    perdia 17 743 unidades del periodo, el 5.8% del total (92.5% -> 98.3%
    con el segundo intento). En Telcel y AT&T no ayuda en nada -- sus
    huecos son otra cosa: nombres de verdad distintos, o en AT&T etiquetas
    de canal ("J3A - Cadena COPPEL, S.A. DE C.V.") que no son tiendas -- y
    en Elektra y Sears, que se identifican por NUMERO, tampoco: sus pocas
    tiendas sin catalogar (13 de 1272 en Elektra) de verdad no estan dadas
    de alta, con cualquier normalizacion.
    """
    enlace = cruda.enlace_con_catalogo.format(
        etiqueta=cruda.etiqueta, clave=cruda.clave
    )
    if cruda.enlace_con_catalogo != _POR_NOMBRE:
        sql = f"""
            LEFT JOIN (SELECT [Region], [Coppel Name] AS [nombre],
                              {_NUMERO_DEL_CATALOGO} AS [numero]
                       FROM {GTM}.[CustomerStores]
                       WHERE [RetailName] = ?) cs
              ON {enlace}
        """
        return sql, "cs.[Region]", (cruda.retail_en_catalogo,)

    normalizado_venta = _NORMALIZAR.format(expr=f"v.[{cruda.etiqueta}]")
    normalizado_catalogo = _NORMALIZAR.format(expr="cs2.[nombre]")
    sql = f"""
        LEFT JOIN (SELECT [Region], [Coppel Name] AS [nombre]
                   FROM {GTM}.[CustomerStores] WHERE [RetailName] = ?) cs
          ON {enlace}
        LEFT JOIN (SELECT [Region], [Coppel Name] AS [nombre]
                   FROM {GTM}.[CustomerStores] WHERE [RetailName] = ?) cs2
          ON {normalizado_catalogo} = {normalizado_venta}
    """
    return (
        sql,
        "COALESCE(cs.[Region], cs2.[Region])",
        (cruda.retail_en_catalogo, cruda.retail_en_catalogo),
    )

# Como se nombra cada retailer en un resultado o en el SQL que lo produjo.
_RETAILER_POR_TEXTO = (
    ("COPPEL", re.compile(r"coppel", re.IGNORECASE)),
    ("ELEKTRA", re.compile(r"elektra|\bEKT\b|EKT_B2B", re.IGNORECASE)),
    ("TELCEL", re.compile(r"telcel", re.IGNORECASE)),
    ("LIVERPOOL", re.compile(r"liverpool", re.IGNORECASE)),
    ("AT&T", re.compile(r"at&t|ATT_B2B|ATT_Postpaid", re.IGNORECASE)),
    ("SEARS", re.compile(r"sears", re.IGNORECASE)),
)


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


# Vive en tiempo.py, junto con las demas reglas de periodo; aqui se deja el
# nombre con el que ya se le llama en este modulo.
_medida = tiempo.medida


def modelos_a_desglosar(df: pd.DataFrame) -> list[str]:
    """Los modelos del resultado, los mas vendidos primero.

    Se ordenan por la columna numerica del propio resultado: si el reporte
    trae cien modelos, el desglose es de los que de verdad pesan.
    """
    columna = tiempo.columna(df, _ES_MODELO)
    if columna is None:
        return []

    medida = _medida(df)
    if medida is not None:
        orden = df.groupby(columna, dropna=True)[medida].sum()
        nombres = orden.sort_values(ascending=False).index
    else:
        nombres = df[columna].dropna().unique()

    modelos = [str(v).strip() for v in nombres if str(v).strip()]
    return modelos[:MAX_MODELOS_DESGLOSADOS]


def _nombre_de_hoja(valor: str, usados: set[str]) -> str:
    """Un nombre que Excel acepte y que no pise a otro.

    Excel corta a 31 caracteres, rechaza : \\ / ? * [ ] y revienta con dos
    hojas del mismo nombre: 'TELCEL CORPO.' y 'TELCEL CAC' caben, pero dos
    cadenas largas podrian quedar iguales al recortarlas.
    """
    limpio = "".join(" " if c in _PROHIBIDOS_EN_HOJA else c for c in str(valor))
    limpio = " ".join(limpio.split())[:31].strip() or "Cadena"
    nombre, numero = limpio, 2
    while nombre.casefold() in usados:
        sufijo = f" {numero}"
        nombre = f"{limpio[:31 - len(sufijo)]}{sufijo}"
        numero += 1
    usados.add(nombre.casefold())
    return nombre


def hojas_por_cadena(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Una hoja con las filas de cada cadena, cuando el reporte trae varias.

    La hoja de datos sigue completa: esto se agrega al lado. Un reporte que
    compara Coppel contra Elektra se lee cadena por cadena --"que paso en
    Coppel"-- y en una sola tabla mezclada eso obliga a filtrar a mano cada
    vez.

    Las cadenas van ordenadas por volumen, para que las primeras pestañas
    sean las que pesan. Si el resultado no dice de que cadena es cada fila,
    o si son demasiadas, no se separa nada.
    """
    if df is None or df.empty:
        return {}
    columna = tiempo.columna(df, _ES_RETAIL)
    if columna is None:
        return {}

    cadenas = df[columna].dropna().astype(str)
    cuantas = cadenas.nunique()
    if not 2 <= cuantas <= MAX_HOJAS_POR_CADENA:
        return {}
    if len(df) < cuantas * MIN_FILAS_POR_CADENA:
        return {}

    medida = _medida(df)
    if medida is not None:
        orden = df.groupby(cadenas, dropna=True)[medida].sum()
        valores = list(orden.sort_values(ascending=False).index)
    else:
        valores = list(cadenas.value_counts().index)

    usados: set[str] = {"datos"}
    hojas: dict[str, pd.DataFrame] = {}
    for valor in valores:
        filas = df[cadenas == valor]
        if filas.empty:
            continue
        hojas[_nombre_de_hoja(valor, usados)] = filas
    return hojas


def _cual_retailer(texto: str) -> str | None:
    """El retailer que nombra ese texto, o None si no es exactamente uno.

    Exactamente uno: un reporte que compara Coppel contra Elektra no se puede
    desglosar por la tabla cruda de ninguno de los dos.
    """
    encontrados = {clave for clave, patron in _RETAILER_POR_TEXTO if patron.search(texto)}
    return encontrados.pop() if len(encontrados) == 1 else None


def retailer_del_resultado(df: pd.DataFrame, query: str) -> str | None:
    """De que cadena es el reporte, mirando primero el resultado y luego el SQL.

    El resultado manda: si trae una columna de retailer con un solo valor,
    eso es lo que el usuario esta viendo. Si no la trae, el SQL dice de que
    tabla salio.
    """
    columna = tiempo.columna(df, _ES_RETAIL)
    if columna is not None:
        valores = df[columna].dropna().astype(str).unique()
        if len(valores) == 1:
            return _cual_retailer(valores[0])
        return None
    return _cual_retailer(query or "")


def acotado_del_resultado(df: pd.DataFrame, query: str = "") -> dict[str, list[str]]:
    """A que marcas y cadenas esta acotado el resultado que se esta viendo.

    La hoja "Ventas por tienda" salia SIEMPRE con todas las marcas y todas
    las cadenas. Preguntabas "cuantas vendio Honor en agosto", el mensaje
    contestaba 160 909 unidades -- correcto -- y el Excel que lo acompaña
    traia 2 030 760 en esa hoja, la venta del mercado entero. Dos numeros que
    no cuadran en el mismo archivo, y el que manda es el del archivo: nadie
    revisa de donde salio cada hoja.

    Los valores salen del propio resultado cuando este trae la columna (son
    los que el servidor acaba de devolver, asi que existen tal cual) y, si
    viene agregado y ya no la trae, de los literales del SQL. Un resultado
    con muchos valores distintos no esta acotado a nada y no se filtra.
    """
    acotado: dict[str, list[str]] = {}
    for destino, patron in (("Brand", _ES_MARCA), ("Retail", _ES_RETAIL)):
        columna = tiempo.columna(df, patron) if df is not None else None
        if columna is None:
            continue
        valores = sorted({
            str(v).strip() for v in df[columna].dropna() if str(v).strip()
        })
        if 0 < len(valores) <= MAX_VALORES_ACOTADOS:
            acotado[destino] = valores

    if "Brand" not in acotado:
        encontrado = _MARCA_EN_SQL.search(query or "")
        if encontrado:
            marcas = sorted(set(_LITERAL.findall(encontrado.group(1))))
            if 0 < len(marcas) <= MAX_VALORES_ACOTADOS:
                acotado["Brand"] = marcas
    return acotado


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


def _ventas_por_tienda_y_modelo(conexion, cruda: Cruda, modelos: list[str],
                               desde: str, hasta: str):
    """Cuanto vendio cada tienda de esos modelos, por la tabla CRUDA.

    Esta es la ruta buena para llegar al modelo: el texto tal cual lo escribe
    la cadena contra UNICOS$, con el retailer pegado a la condicion. Medido,
    casa el 100% en Elektra, Telcel, Liverpool y Sears, el 97.8% en Coppel y
    el 89% en AT&T; hacerlo desde la vista consolidada casa el 8%.

Cada cadena tiene SU llave, y no es siempre la misma columna:
    Coppel y Sears casan con [MATERIAL DESCRIPTION FINAL], que es una
    concatenacion -- en Coppel marca + descripcion ("ACER ANV15-41-R4WJ"), en
    Sears la palabra Sears + descripcion ("Sears APPLE IPHONE 11 64GB") --, y
    las demas casan con [DESCRIPCION] a secas. Medido en agosto de 2026:
    armando la concatenacion, Coppel sube de 97.8% a 100.0%; Sears se queda
    en 100.0% por las dos rutas. En AT&T la final casa 0%, asi que ahi la
    llave es [Model] contra [DESCRIPCION].

    El GROUP BY dentro de UNICOS$ no es decoracion: en Liverpool hay 137
    descripciones que apuntan a mas de una homologacion (6 en Coppel). Sin
    colapsarlas, cada venta de esas se uniria dos veces y las unidades
    saldrian infladas. Por [MATERIAL DESCRIPTION FINAL] no hay ninguna
    ambigua ni en Coppel ni en Sears (medido: 0).

    La REGION sale de CustomerStores, y el enlace es distinto por cadena:
    Coppel, Telcel, AT&T y Liverpool casan por nombre (99.6%, 99.4%, 99.5% y
    94.5% de sus tiendas); Elektra y Sears solo por el numero que el catalogo
    guarda entre parentesis en [HONOR NAME] (99.1% y 98.9%). Va en LEFT JOIN:
    una tienda sin region no puede costar la fila.

    `>= 1` deja fuera devoluciones y ajustes, que es lo que hacen las vistas
    oficiales: asi la hoja suma como el reporte que acompaña.
    """
    huecos_retail = ", ".join("?" * len(cruda.retail_en_unicos))
    huecos_modelo = ", ".join("?" * len(modelos))
    join_region, region, parametros_region = _join_region(cruda)
    sql = f"""
        SELECT TOP {MAX_FILAS_DE_VENTA}
               MAX({region}) AS [Region],
               MAX(v.[{cruda.etiqueta}]) AS [Tienda],
               u.[modelo] AS [Modelo],
               SUM(v.[{cruda.unidades}]) AS [Unidades]
        FROM [RAW_DATA_B2B].dbo.[{cruda.tabla}] v
        JOIN (SELECT [{cruda.columna_en_unicos}] AS [texto],
                     MIN([HOMOLOGACION MODELO]) AS [modelo]
              FROM {UNICOS}
              WHERE [Retail] IN ({huecos_retail})
                AND [{cruda.columna_en_unicos}] IS NOT NULL
                AND LTRIM([{cruda.columna_en_unicos}]) <> ''
              GROUP BY [{cruda.columna_en_unicos}]) u
          ON u.[texto] = {cruda.llave}
        {join_region}
        WHERE v.[{cruda.fecha}] >= ? AND v.[{cruda.fecha}] < ?
          AND v.[{cruda.unidades}] >= 1
          AND u.[modelo] IN ({huecos_modelo})
        GROUP BY {region}, v.[{cruda.clave}], u.[modelo]
        ORDER BY SUM(v.[{cruda.unidades}]) DESC
    """
    parametros = (
        *cruda.retail_en_unicos,
        *parametros_region,
        desde,
        hasta,
        *modelos,
    )
    cursor = conexion.exec_driver_sql(sql, parametros)
    filas = cursor.fetchall()
    return pd.DataFrame.from_records(filas, columns=list(cursor.keys()))


def ventas_crudas(conexion, cruda: Cruda, desde: str, hasta: str):
    """Ventas de toda la cadena en el periodo, por tienda, marca y mes.

    Es la fuente del PIVOT: a diferencia de `_ventas_por_tienda_y_modelo`,
    que se acota a los modelos del resultado, esta trae TODA la cadena --
    "las ventas de Elektra de junio a agosto" no viene con una lista corta
    de modelos que desglosar, y limitarla a los top N habria escondido el
    resto.

    Se agrupa por marca, no por modelo: al nivel de modelo la misma consulta
    da cuatro veces mas filas (127 602 contra 28 887 en Elektra, medido), y
    el detalle por modelo ya tiene su propia hoja. Esta es la que alimenta
    un pivot de tienda x mes, y para eso la marca alcanza.
    """
    join_region, region, parametros_region = _join_region(cruda)
    sql = f"""
        SELECT TOP {MAX_FILAS_RAW_DATA}
               MAX({region}) AS [Region],
               MAX(v.[{cruda.etiqueta}]) AS [Tienda],
               v.[{cruda.marca}] AS [Marca],
               MONTH(v.[{cruda.fecha}]) AS [Mes],
               SUM(v.[{cruda.unidades}]) AS [Unidades]
        FROM [RAW_DATA_B2B].dbo.[{cruda.tabla}] v
        {join_region}
        WHERE v.[{cruda.fecha}] >= ? AND v.[{cruda.fecha}] < ?
          AND v.[{cruda.unidades}] >= 1
        GROUP BY {region}, v.[{cruda.clave}], v.[{cruda.marca}],
                 MONTH(v.[{cruda.fecha}])
        ORDER BY SUM(v.[{cruda.unidades}]) DESC
    """
    parametros = (*parametros_region, desde, hasta)
    cursor = conexion.exec_driver_sql(sql, parametros)
    filas = cursor.fetchall()
    return pd.DataFrame.from_records(filas, columns=list(cursor.keys()))


def _ventas_por_tienda(conexion, desde: str, hasta: str,
                       acotado: dict[str, list[str]] | None = None):
    """Cuanto vendio cada tienda en ese periodo, la que mas primero.

    No se acota por MODELO: desde la consolidada eso obliga a una union que
    pierde el 92% del volumen (ver el encabezado del modulo). Por marca y por
    cadena SI, y hay que hacerlo -- son columnas de la propia vista, y sin
    ese filtro la hoja contradice al mensaje que la acompaña.
    """
    condiciones, parametros = ["v.[Date] >= ?", "v.[Date] < ?"], [desde, hasta]
    for columna, valores in (acotado or {}).items():
        condiciones.append(f"v.[{columna}] IN ({', '.join('?' * len(valores))})")
        parametros.extend(valores)

    sql = f"""
        SELECT TOP {MAX_FILAS_DE_VENTA}
               v.[Retail], v.[Store], SUM(v.[QTY]) AS [Unidades]
        FROM {VENTAS} v
        WHERE {' AND '.join(condiciones)}
        GROUP BY v.[Retail], v.[Store]
        ORDER BY SUM(v.[QTY]) DESC
    """
    cursor = conexion.exec_driver_sql(sql, tuple(parametros))
    filas = cursor.fetchall()
    return pd.DataFrame.from_records(filas, columns=list(cursor.keys()))


def contexto_del_resultado(
    df: pd.DataFrame, engine, query: str = ""
) -> dict[str, pd.DataFrame]:
    """Hojas de contexto para el resultado, vacio si no hay nada que agregar."""
    if df is None or df.empty:
        return {}

    # Primero una hoja por cadena, si el reporte trae varias: van pegadas a
    # la hoja de datos, que es de donde salen.
    hojas: dict[str, pd.DataFrame] = hojas_por_cadena(df)

    # El desglose por tienda va despues: es lo que se pidio ver, y lo unico
    # que cuesta tiempo. Si falla o no aplica, el resto sigue.
    rango = periodo(df)
    if rango:
        # Con el modelo si se puede cuando el reporte es de UNA cadena: ahi
        # hay una tabla cruda donde la homologacion casa. Si son varias, o si
        # no hay modelos, la hoja sale por periodo, que siempre es correcta.
        cruda = CRUDAS.get(retailer_del_resultado(df, query) or "")
        modelos = modelos_a_desglosar(df)
        try:
            with engine.connect() as conexion:
                conexion.connection.dbapi_connection.timeout = SEGUNDOS_PARA_EL_DESGLOSE
                if cruda and modelos:
                    ventas = _ventas_por_tienda_y_modelo(
                        conexion, cruda, modelos, *rango
                    )
                else:
                    ventas = _ventas_por_tienda(
                        conexion, *rango, acotado_del_resultado(df, query)
                    )
            if not ventas.empty:
                hojas["Ventas por tienda"] = ventas
        except Exception:  # noqa: BLE001
            # El reporte ya esta listo; esta hoja es un extra. Que la vista de
            # ventas no conteste a tiempo no puede costar la respuesta.
            logger.exception("No se pudo desglosar por tienda")

        # Los datos crudos que alimentan el PIVOT, solo cuando hay UNA cadena
        # identificada: sin eso no hay tabla cruda de la que partir. Va en su
        # propio try: que esta consulta falle no puede tirar la de arriba, ni
        # al reves.
        if cruda:
            try:
                with engine.connect() as conexion:
                    conexion.connection.dbapi_connection.timeout = SEGUNDOS_PARA_EL_DESGLOSE
                    crudos = ventas_crudas(conexion, cruda, *rango)
                if not crudos.empty:
                    hojas["Raw_Data"] = crudos
            except Exception:  # noqa: BLE001
                logger.exception("No se pudo traer el detalle crudo para el pivot")

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
