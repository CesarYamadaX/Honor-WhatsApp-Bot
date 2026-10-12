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
import os
import re
from datetime import timedelta
from contextlib import contextmanager
from dataclasses import dataclass, replace

import pandas as pd

import tiempo
from catalogo import (
    _VISTAS_DE_LA_CONSOLIDADA,
    _union_de,
    consolidada_trae_basura,
    valores_conocidos,
)

logger = logging.getLogger("sql_agent.contexto")

GTM = "[GTM BI Database].dbo"
VENTAS = "[RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B]"
_DE_PROMOTORIA = re.compile(r"(?<!\w)(V_)?HCReport", re.IGNORECASE)
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

# Las hojas de VENTA NO se topan. Se pidio explicitamente: "que sea todas
# las filas relacionadas a la pregunta, no importa si las colocas en mas
# hojas". Cortarlas nunca fue "menos ruido" -- era un total que no cuadra con
# el mensaje, y quien abre el Excel no tiene como saberlo.
#
# No se pueden desbordar, y eso esta medido (25 de septiembre de 2026): lo
# que cada una entrega lo acota la propia realidad, no un TOP.
#
#   hoja                que la acota                     peor caso medido
#   Ventas por tienda   cuantas tiendas existen          9 760 (ano, todas
#                                                        las marcas)
#   " (por modelo)      tiendas x MAX_MODELOS_DESGLOSADOS  ~25 000 (Coppel)
#   Raw_Data            tiendas x marcas x meses         301 319 (Coppel,
#                                                        toda la historia)
#
# Si alguna vez una superara el millon de filas de Excel, se parte en varias
# hojas en vez de perderse: ver `excel_exporter.partir_en_hojas`.

# Lo que se le da al desglose antes de rendirse. Es generoso a proposito: la
# alternativa no es "esperar menos", es entregar un Excel sin la hoja que se
# pidio. El techo real de la peticion lo pone AGENT_TIMEOUT_SECONDS, y quien
# llama recorta este presupuesto a lo que quede (ver `contexto_del_resultado`).
SEGUNDOS_PARA_EL_DESGLOSE = 240

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

# La hoja de datos crudos que alimenta el PIVOT se agrupa por tienda, marca
# y mes -- sin el modelo, que ya tiene su propia hoja mas fina ("Ventas por
# tienda") -- porque a nivel modelo la misma consulta de tres meses de
# Elektra da 127 602 filas, cuatro veces mas.
#
# Lo que entrega, YA AGRUPADA, medido el 25 de septiembre de 2026 (las
# tablas crudas detras suman 37M de filas: Telcel 14.8M, Coppel 12.2M, AT&T
# 5.0M, Liverpool 3.4M, Elektra 1.7M, Sears 90k):
#
#   cadena      1 mes    3 meses    1 anio   todo (2024->hoy)
#   COPPEL     19 355     58 066   164 507        301 319
#   AT&T        9 785     29 068    86 620        115 976
#   ELEKTRA     9 540     28 887    76 253        106 317
#   TELCEL      8 865     26 520    69 576        119 821
#   LIVERPOOL   2 116      6 341    16 878         35 806
#   SEARS         717      2 159     5 561          7 964
#
# Van TODAS. Lo que antes las topaba era el limite de adjunto, y ese limite
# estaba mal leido: los 16 MB son el tope de MEDIA de WhatsApp, y un .xlsx
# viaja como DOCUMENTO, que admite mucho mas (ver MAX_FILE_MB en el sidecar).

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

# `[Brand] = 'HONOR'` o `[Retail] IN ('Liverpool Offline','Liverpool
# Online')`, para cuando el resultado viene agregado y ya no trae la columna.
# "Ventas de Coppel por mes" es justo eso: nueve filas de Year/Month/Total
# donde la cadena solo vive en el WHERE.
_EN_SQL = {
    columna: re.compile(
        rf"\[?\b{columna}\b\]?\s*(?:=|in)\s*\(?\s*((?:'[^']*'\s*,?\s*)+)\)?",
        re.IGNORECASE,
    )
    for columna in ("brand", "retail")
}
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
    # La vista con el feed _IOT de esa cadena, si lo tiene. Coppel y
    # Liverpool son las dos unicas partidas en dos, y la tabla cruda solo
    # trae una mitad: sin esto, "las ventas de Coppel" en la hoja Raw_Data
    # se quedan 880 689 unidades cortas (medido, 2026).
    vista_iot: str = ""
    # La columna de la cruda donde va la descripcion del producto. Solo la
    # usan Coppel y Liverpool, para acomodar su mitad IOT en Raw_Data.
    descripcion: str = ""
    # Condicion extra (con alias v) para contar lo MISMO que la vista del
    # bot. Solo AT&T la necesita: ATT_B2B trae ademas prepago y la venta en
    # linea (EBS-B2C), y vw_ETL_ATT_Postpaid no. Medido, enero de 2026: la
    # cruda entera 445 237 unidades; con este filtro 137 538, igual que la
    # vista (y en agosto, 118 758 = 118 758). Las otras cinco cadenas ya
    # cuadran exactas con unidades >= 1.
    filtro: str = ""
    # Las marcas a las que se acota el reporte (las de la consulta). Visto el
    # 9/10/2026: "el excel de ventas de Honor por tienda en Elektra" salio con
    # 363 294 filas de TODAS las marcas en Raw_Data. Vacio = todas.
    marcas: tuple[str, ...] = ()


def _en_marcas(columna: str, marcas: tuple[str, ...]) -> str:
    valores = ", ".join("'" + m.upper().replace("'", "''") + "'" for m in marcas)
    return f" AND UPPER(LTRIM(RTRIM({columna}))) IN ({valores})"


def _filtro_de(cruda: "Cruda") -> str:
    filtro = f" AND ({cruda.filtro})" if cruda.filtro else ""
    if cruda.marcas:
        filtro += _en_marcas(f"v.[{cruda.marca}]", cruda.marcas)
    return filtro


def _filtro_iot(cruda: "Cruda", alias: str = "") -> str:
    """Lo mismo para la mitad IOT, que nombra la marca [Brand]."""
    return _en_marcas(f"{alias}[Brand]", cruda.marcas) if cruda.marcas else ""


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
                    _POR_NOMBRE, marca="Marca", vista_iot="vw_ETL_COPPEL_IOT",
                    descripcion="Modelo"),
    "ELEKTRA": Cruda("EKT_B2B", ("ELEKTRA",), "v.[Desc Sku]", _DESCRIPCION,
                     "Tienda #", "POS", "Day", "QTY", "ELEKTRA", _POR_NUMERO,
                     marca="Marca"),
    "TELCEL": Cruda("TELCEL_B2B", ("TELCEL",), "v.[Descripción]", _DESCRIPCION,
                    "Venta", "Venta", "Fecha", "Cantidad", "TELCEL",
                    _POR_NOMBRE, marca="Marca"),
    "LIVERPOOL": Cruda("LIVERPOOL_B2B$", ("LIVERPOOL",),
                       "v.[Material Description]", _DESCRIPCION,
                       "Centro", "STORE", "Día/Periodo", "Vtas# U Per 1",
                       "LIVERPOOL", _POR_NOMBRE, marca="Brand",
                       vista_iot="vw_ETL_Liverpool_IOT",
                       descripcion="Material Description"),
    # Los dos [Retail] de AT&T: con DEALERS solo se casa el 80.7% del mes, con
    # los dos el 88.9% (medido, agosto de 2026).
    "AT&T": Cruda("ATT_B2B", ("AT&T DEALERS", "AT&T CACS"), "v.[Model]",
                  _DESCRIPCION, "Codigo de Organización",
                  "Nombre de la Organización", "Fecha de Transacción",
                  "Cantidad", "AT&T", _POR_NOMBRE, marca="BRAND",
                  filtro="v.[Type] = 'Postpago' AND v.[Channel 2] IN ('Dealers', 'Stores')"),
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

    El catalogo se agrupa a UNA fila por tienda antes de unir: hay tiendas de
    AT&T dadas de alta dos veces, y unir contra el catalogo tal cual repetia
    sus ventas (visto el 5/10/2026: 303 filas de mas en enero de 2026).

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
            LEFT JOIN (SELECT MIN([Region]) AS [Region], MIN([Coppel Name]) AS [nombre],
                              {_NUMERO_DEL_CATALOGO} AS [numero]
                       FROM {GTM}.[CustomerStores]
                       WHERE [RetailName] = ?
                       GROUP BY {_NUMERO_DEL_CATALOGO}) cs
              ON {enlace}
        """
        return sql, "cs.[Region]", (cruda.retail_en_catalogo,)

    normalizado_venta = _NORMALIZAR.format(expr=f"v.[{cruda.etiqueta}]")
    nombre_normalizado = _NORMALIZAR.format(expr="[Coppel Name]")
    sql = f"""
        LEFT JOIN (SELECT MIN([Region]) AS [Region], [Coppel Name] AS [nombre] FROM {GTM}.[CustomerStores] WHERE [RetailName] = ? GROUP BY [Coppel Name]) cs
          ON {enlace}
        LEFT JOIN (SELECT MIN([Region]) AS [Region], {nombre_normalizado} AS [nombre]
                   FROM {GTM}.[CustomerStores] WHERE [RetailName] = ?
                   GROUP BY {nombre_normalizado}) cs2
          ON cs2.[nombre] = {normalizado_venta}
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


# Columnas del catalogo de tiendas cuyo NOMBRE trae una cadena. "[Coppel
# Name]" es el nombre de cualquier tienda en CustomerStores, no Coppel.
# Visto el 7/10/2026: un reporte de Liverpool region 4 que unia por [Coppel
# Name] se tomo como "Coppel y Liverpool" y salio con las tiendas de todo
# el mercado pegadas.
_COLUMNAS_CON_NOMBRE_DE_CADENA = re.compile(r"\[?Coppel[ _]Name\]?", re.IGNORECASE)

# La consulta se queda con unas tiendas o una region, no con la cadena
# entera: el reporte de cadena (todas sus tiendas del periodo) le pegaria
# lo que no se pidio.
_ACOTA_TIENDAS = re.compile(
    r"\[?Region\]?\s*(=|IN\b|LIKE\b)|CustomerStores|STATES_AND_REGIONS"
    r"|\[?(Store|STORE|Tienda|Store Name|Nombre Tienda)\]?\s*(=|IN\b|LIKE\b)",
    re.IGNORECASE,
)


def acotado_a_tiendas(query: str) -> bool:
    return bool(_ACOTA_TIENDAS.search(query or ""))


def retailer_del_resultado(df: pd.DataFrame, query: str) -> str | None:
    """De que cadena es el reporte, mirando primero el resultado y luego el SQL.

    El resultado manda: si trae una columna de retailer con un solo valor,
    eso es lo que el usuario esta viendo. Si no la trae, el SQL dice de que
    tabla salio.
    """
    columna = tiempo.columna(df, _ES_RETAIL)
    if columna is not None:
        valores = df[columna].dropna().astype(str).unique()
        # Varios valores pueden ser la MISMA cadena: AT&T llega como "AT&T
        # Dealers" y "AT&T Cacs" (y alguna fila "Unknown", que no es cadena).
        # Visto el 5/10/2026: "las ventas de AT&T de enero" se tomo como un
        # reporte de varias cadenas y salio sin PIVOT ni Raw_Data.
        cadenas = {_cual_retailer(v) for v in valores} - {None}
        return cadenas.pop() if len(cadenas) == 1 else None
    return _cual_retailer(_COLUMNAS_CON_NOMBRE_DE_CADENA.sub("", query or ""))


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

    for destino, patron in (("Brand", _EN_SQL["brand"]), ("Retail", _EN_SQL["retail"])):
        if destino in acotado:
            continue
        encontrado = patron.search(query or "")
        if not encontrado:
            continue
        valores = sorted(set(_LITERAL.findall(encontrado.group(1))))
        if 0 < len(valores) <= MAX_VALORES_ACOTADOS:
            acotado[destino] = valores

    # Y la tercera forma de decir de que cadena es: el nombre de la tabla.
    # "Ventas de Coppel por mes" sale de COPPEL_B2B, que NO tiene columna
    # [Retail] -- la cadena esta en el nombre y en ningun otro lado.
    if "Retail" not in acotado:
        valores = _retail_de_la_cadena(retailer_del_resultado(df, query))
        if valores:
            acotado["Retail"] = valores
    return acotado


def _retail_de_la_cadena(cadena: str | None) -> list[str]:
    """Como se llama esa cadena en [Retail] de la consolidada.

    No se escriben a mano: se filtran los valores REALES de la columna por
    la cadena que nombran. Telcel son seis canales y Liverpool dos, y
    escribir 'TELCEL' o 'LIVERPOOL' a mano no casaria con ninguno -- que es
    justo el error que `catalogo.filtros_que_no_casan` existe para atrapar.
    """
    if not cadena:
        return []
    conocidos = valores_conocidos("ETL_Consolidado_B2B", "Retail") or []
    return [v for v in conocidos if _cual_retailer(v) == cadena]


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
            # El dia se suma con datetime.timedelta y no con pd.Timedelta: con
            # pandas 2.2 y numpy 2.x, Timestamp + pd.Timedelta ya avisa que
            # sera error ("generic unit for NumPy timedelta").
            desde = pd.Timestamp(fechas.min()).normalize()
            hasta = pd.Timestamp(fechas.max()).normalize() + timedelta(days=1)
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


_ANIO_EN_SQL = re.compile(r"\[?Year\]?\s*=\s*'?(\d{4})", re.IGNORECASE)
_MES_EN_SQL = re.compile(
    r"\[?Month\]?\s*(?:=\s*'?(\d{1,2})|IN\s*\(([\d\s,]+)\)|BETWEEN\s+(\d{1,2})\s+AND\s+(\d{1,2}))",
    re.IGNORECASE,
)
_FECHA_EN_SQL = re.compile(
    r"\[?(?:Date|D[ií]a|Day|Fecha)\]?\s*>=\s*'(\d{4}-\d{2}-\d{2})'.*?<\s*'(\d{4}-\d{2}-\d{2})'",
    re.IGNORECASE | re.DOTALL,
)


def periodo_de_la_consulta(query: str) -> tuple[str, str] | None:
    """El periodo que filtro la consulta, cuando el resultado no lo trae.

    "Ventas de Honor por tienda en Elektra en enero 2026" devuelve tienda y
    unidades, sin columna de fecha: el periodo solo esta en el WHERE
    ([Year] = 2026 AND [Month] = 1). Sin esto ese reporte salia sin PIVOT,
    Datos ni Raw_Data (visto el 5/10/2026).
    """
    texto = query or ""
    fechas = _FECHA_EN_SQL.search(texto)
    if fechas:
        return fechas.group(1), fechas.group(2)
    anios = sorted({int(a) for a in _ANIO_EN_SQL.findall(texto)})
    if len(anios) != 1:
        return None  # sin anio, o varios: no se adivina
    anio = anios[0]
    meses: set[int] = set()
    for igual, lista, desde, hasta in _MES_EN_SQL.findall(texto):
        if igual:
            meses.add(int(igual))
        elif lista:
            meses |= {int(m) for m in lista.replace(" ", "").split(",") if m}
        elif desde and hasta:
            meses |= set(range(int(desde), int(hasta) + 1))
    meses = {m for m in meses if 1 <= m <= 12}
    if not meses:
        return f"{anio}-01-01", f"{anio + 1}-01-01"
    inicio = pd.Timestamp(anio, min(meses), 1)
    fin = pd.Timestamp(anio, max(meses), 1) + pd.offsets.MonthBegin(1)
    return inicio.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")


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
        SELECT
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
          AND v.[{cruda.unidades}] >= 1{_filtro_de(cruda)}
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


# La otra mitad de Coppel y Liverpool. La vista _IOT tiene la forma de la
# consolidada ([Retail][Store][Brand][Date][QTY]...), asi que sus columnas no
# son las de la tabla cruda y hay que nombrarlas aparte.
#
# Su [Store] NO se escribe igual en las dos cadenas, y por eso se intentan
# los dos caminos en vez de asumir uno (medido el 25/09/2026):
#
#   Liverpool  es un NOMBRE ('L POLANCO'), como el resto -> casa directo
#              contra CustomerStores, 92.8%, y sube con la normalizacion de
#              espacios que ya se usa para la mitad cruda.
#   Coppel     es un NUMERO pelado ('249') y el resto usa nombres
#              ('1159 - TETLAN') -> contra el catalogo casa 0%. Ahi el
#              puente es la vista principal, que da nombre y region para ese
#              numero: 880 661 de 880 689 unidades, el 100.0%.
#
# El COALESCE los prueba en ese orden, asi que cada cadena toma el que le
# sirve sin que haya que saber de antemano cual es.
_MITAD_IOT = """
        SELECT COALESCE(csi.[Region], csi2.[Region], p.[Region]) AS [Region],
               COALESCE(p.[nombre], i.[Store]) AS [Tienda],
               COALESCE(p.[nombre], i.[Store]) AS [Clave],
               i.[Brand] AS [Marca],
               MONTH(i.[Date]) AS [Mes],
               i.[QTY] AS [Unidades]
        FROM [RAW_DATA_B2B].dbo.[{iot}] i
        LEFT JOIN (SELECT MIN([Region]) AS [Region], [Coppel Name] AS [nombre] FROM [GTM BI Database].dbo.[CustomerStores] WHERE [RetailName] = ? GROUP BY [Coppel Name]) csi
          ON csi.[nombre] = i.[Store]
        LEFT JOIN (SELECT MIN([Region]) AS [Region], {normalizado_catalogo} AS [nombre]
                   FROM [GTM BI Database].dbo.[CustomerStores] WHERE [RetailName] = ?
                   GROUP BY {normalizado_catalogo}) csi2
          ON csi2.[nombre] = {normalizado_iot}
        LEFT JOIN (
            SELECT TRY_CONVERT(int, LEFT(w.[Store], CHARINDEX(' ', w.[Store] + ' ') - 1)) AS [numero],
                   MIN(cs.[Region]) AS [Region],
                   MIN(w.[Store]) AS [nombre]
            FROM [RAW_DATA_B2B].dbo.[{principal}] w
            JOIN [GTM BI Database].dbo.[CustomerStores] cs
              ON cs.[Coppel Name] = w.[Store] AND cs.[RetailName] = ?
            GROUP BY TRY_CONVERT(int, LEFT(w.[Store], CHARINDEX(' ', w.[Store] + ' ') - 1))
        ) p ON p.[numero] = TRY_CONVERT(int, i.[Store])
        WHERE i.[Date] >= ? AND i.[Date] < ? AND i.[QTY] >= 1{filtro_iot}
"""


# Raw_Data con TODAS las columnas de la tabla cruda, fila por fila. Pedido el
# 5 de octubre de 2026: la hoja venia agrupada por tienda, marca y mes (5
# columnas), y faltaban el modelo, el SKU, el dia, el precio y el resto de lo
# que trae la descarga de cada cadena.
#
# Pesa: un mes de Elektra son 163 034 filas x 22 columnas, 19 MB y ~40 s de
# escritura (medido). Por encima de este tope de celdas el archivo pasaria de
# ~40 MB y tardaria minutos, asi que se vuelve a la hoja agrupada y se avisa.
# Un mes de AT&T (445 mil filas x 24) ya no cabe; uno de Elektra o Liverpool si.
LIMITE_DE_CELDAS_CRUDAS = 8_000_000

# Por encima del limite de arriba, el reporte es SOLO Raw_Data, repartido en
# varias hojas si pasa del millon de filas, sin pivot ni Datos (pedido el
# 5/10/2026: "cuando se piden muchos datos, solo los datos"). Lo escribe
# xlsxwriter, que tarda la mitad: un mes de AT&T, 11.6 millones de celdas,
# 54 s y 42 MB (openpyxl: 97 s y 49 MB). Este tope es lo que cabe holgado en
# los 100 MB que WhatsApp acepta para un documento: ~75 MB, ~2 minutos.
# Por encima, ni asi se puede mandar, y va agrupado (PIVOT + Datos).
LIMITE_DE_CELDAS_SOLO_CRUDOS = 20_000_000

# Columnas que se agregan al final, para el pivot y para ubicar la tienda. No
# chocan con las de ninguna cruda (Elektra y Sears ya traen [Region] y
# [Month], por eso estas llevan otro nombre).
COLUMNA_REGION = "Region (catalogo)"
COLUMNA_MES = "Mes"
COLUMNA_FUENTE = "Fuente"


def _columnas_de(conexion, tabla: str) -> list[str]:
    filas = conexion.exec_driver_sql(
        "SELECT COLUMN_NAME FROM [RAW_DATA_B2B].INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
        (tabla,),
    ).fetchall()
    return [f[0] for f in filas]


def _normalizado(texto) -> str | None:
    """Lo mismo que _NORMALIZAR en SQL: sin NBSP, sin espacios de mas."""
    if texto is None or (isinstance(texto, float) and pd.isna(texto)):
        return None
    return " ".join(str(texto).replace("\u00a0", " ").split())


_NUMERO_AL_INICIO = re.compile(r"^\s*(\d+)\b")


def _numero_al_inicio(texto) -> int | None:
    m = _NUMERO_AL_INICIO.match(str(texto)) if texto is not None else None
    return int(m.group(1)) if m else None


class _Catalogo:
    """CustomerStores de una cadena, para buscar en memoria.

    Antes la region salia de dos LEFT JOIN en el SQL de la cruda (por nombre
    y por nombre normalizado) y la tienda IOT de un puente que recorria la
    vista principal entera: en Coppel, un mes tardaba 100-200 s, y de eso la
    cruda sola eran 6 s (medido el 5/10/2026). El catalogo son ~2 000
    tiendas: se trae una vez y se busca aqui, con las mismas reglas. Una
    tienda dada de alta dos veces se queda con la region menor (como el MIN
    del SQL), asi que ninguna venta se duplica.
    """

    def __init__(self, conexion, cruda: Cruda):
        filas = conexion.exec_driver_sql(
            f"SELECT [Coppel Name], [Region], {_NUMERO_DEL_CATALOGO} "
            f"FROM {GTM}.[CustomerStores] WHERE [RetailName] = ?",
            (cruda.retail_en_catalogo,),
        ).fetchall()
        self.por_nombre: dict[str, str] = {}
        self.por_normalizado: dict[str, str] = {}
        self.por_numero: dict[int, str] = {}
        # El numero con que empieza el nombre ("100 • CENTRO-MAX"): asi se
        # traduce la tienda de la mitad IOT, que llega solo como "100".
        self.nombre_por_prefijo: dict[int, str] = {}
        for nombre, region, numero in sorted(filas, key=lambda f: (str(f[1]), str(f[0]))):
            if nombre is None:
                continue
            self.por_nombre.setdefault(str(nombre), region)
            self.por_normalizado.setdefault(_normalizado(nombre), region)
            if numero is not None:
                self.por_numero.setdefault(int(numero), region)
            prefijo = _numero_al_inicio(nombre)
            if prefijo is not None:
                self.nombre_por_prefijo.setdefault(prefijo, str(nombre))

    def region_por_nombre(self, nombres: pd.Series) -> pd.Series:
        exacta = nombres.map(lambda n: self.por_nombre.get(str(n)) if pd.notna(n) else None)
        normalizada = nombres.map(lambda n: self.por_normalizado.get(_normalizado(n)))
        return exacta.where(exacta.notna(), normalizada)

    def region_por_numero(self, numeros: pd.Series) -> pd.Series:
        return pd.to_numeric(numeros, errors="coerce").map(
            lambda n: self.por_numero.get(int(n)) if pd.notna(n) else None
        )


def _df_de(conexion, sql: str, parametros=()) -> pd.DataFrame:
    cursor = conexion.exec_driver_sql(sql, tuple(parametros))
    return pd.DataFrame.from_records(cursor.fetchall(), columns=list(cursor.keys()))


def _tamano_del_crudo(conexion, cruda: Cruda, desde: str, hasta: str) -> tuple[list[str], int]:
    """Las columnas de la cruda y cuantas filas trae el periodo (con IOT)."""
    columnas = _columnas_de(conexion, cruda.tabla)
    if not columnas:
        return [], 0
    filas = conexion.exec_driver_sql(
        f"SELECT COUNT(*) FROM [RAW_DATA_B2B].dbo.[{cruda.tabla}] v "
        f"WHERE v.[{cruda.fecha}] >= ? AND v.[{cruda.fecha}] < ? "
        f"AND v.[{cruda.unidades}] >= 1{_filtro_de(cruda)}",
        (desde, hasta),
    ).scalar() or 0
    if cruda.vista_iot:
        filas += conexion.exec_driver_sql(
            f"SELECT COUNT(*) FROM [RAW_DATA_B2B].dbo.[{cruda.vista_iot}] "
            "WHERE [Date] >= ? AND [Date] < ? AND [QTY] >= 1" + _filtro_iot(cruda),
            (desde, hasta),
        ).scalar() or 0
    return columnas, filas


def _celdas(cruda: Cruda, columnas: list[str], filas: int) -> int:
    return filas * (len(columnas) + (3 if cruda.vista_iot else 2))


TROZO_DE_FILAS = 100_000


def _trozos_del_crudo(conexion, cruda: Cruda, desde: str, hasta: str,
                      columnas: list[str], tamano: int = TROZO_DE_FILAS):
    """Las filas del crudo en trozos, ya con region, mes y fuente.

    En trozos para que un reporte de millones de filas no tenga que caber
    entero en memoria (el ZIP, ver crudo_en_zip). La region y la tienda IOT
    se ponen aqui con el catalogo en memoria (ver _Catalogo).
    """
    catalogo = _Catalogo(conexion, cruda)
    lista = ", ".join(f"v.[{c}]" for c in columnas)

    def con_mes(df: pd.DataFrame) -> pd.DataFrame:
        df.insert(
            len(columnas) + 1, COLUMNA_MES,
            pd.to_datetime(df[cruda.fecha], errors="coerce").dt.month,
        )
        return df

    nombre_por_prefijo = dict(catalogo.nombre_por_prefijo)
    cursor = conexion.exec_driver_sql(f"""
        SELECT {lista} FROM [RAW_DATA_B2B].dbo.[{cruda.tabla}] v
        WHERE v.[{cruda.fecha}] >= ? AND v.[{cruda.fecha}] < ?
          AND v.[{cruda.unidades}] >= 1{_filtro_de(cruda)}""", (desde, hasta))
    nombres = list(cursor.keys())
    while True:
        lote = cursor.fetchmany(tamano)
        if not lote:
            break
        trozo = pd.DataFrame.from_records(lote, columns=nombres)
        if cruda.enlace_con_catalogo == _POR_NOMBRE:
            trozo[COLUMNA_REGION] = catalogo.region_por_nombre(trozo[cruda.etiqueta])
        else:
            trozo[COLUMNA_REGION] = catalogo.region_por_numero(trozo[cruda.clave])
        if cruda.vista_iot:
            # De paso, el numero con que empieza cada tienda: con eso se
            # traduce la tienda IOT si el catalogo no la tiene.
            for tienda in trozo[cruda.etiqueta].dropna().unique():
                prefijo = _numero_al_inicio(tienda)
                if prefijo is not None:
                    nombre_por_prefijo.setdefault(prefijo, str(tienda))
            trozo[COLUMNA_FUENTE] = cruda.tabla
        yield con_mes(trozo)

    if not cruda.vista_iot:
        return
    cursor = conexion.exec_driver_sql(f"""
        SELECT [Store], [Brand], [Material_Description], [Carrier], [Date], [QTY]
        FROM [RAW_DATA_B2B].dbo.[{cruda.vista_iot}]
        WHERE [Date] >= ? AND [Date] < ? AND [QTY] >= 1{_filtro_iot(cruda)}""", (desde, hasta))
    nombres = list(cursor.keys())
    while True:
        lote = cursor.fetchmany(tamano)
        if not lote:
            break
        iot = pd.DataFrame.from_records(lote, columns=nombres)
        # La tienda IOT llega como numero ("100"): se traduce al nombre del
        # catalogo, o al de la cruda si el catalogo no la tiene.
        tienda_iot = iot["Store"].map(
            lambda s: nombre_por_prefijo.get(_numero_al_inicio(s), s)
            if str(s).strip().isdigit() else s
        )
        equivalentes = {
            cruda.etiqueta: tienda_iot,
            cruda.marca: iot["Brand"],
            cruda.fecha: iot["Date"],
            cruda.unidades: iot["QTY"],
            cruda.descripcion: iot["Material_Description"],
            "Carrier": iot["Carrier"],
        }
        mitad = pd.DataFrame({c: equivalentes.get(c) for c in columnas}, index=iot.index)
        region = catalogo.region_por_nombre(tienda_iot)
        mitad[COLUMNA_REGION] = region.where(
            region.notna(), catalogo.region_por_numero(iot["Store"])
        )
        mitad[COLUMNA_FUENTE] = cruda.vista_iot
        yield con_mes(mitad)


def _con_atributos(df: pd.DataFrame, cruda: Cruda, solo_crudos: bool) -> pd.DataFrame:
    # Con que columnas se arma el pivot (excel_pivot.config_ventas_por_tienda):
    # cada cruda nombra distinto la tienda, la marca y las unidades.
    df.attrs["pivot"] = {
        "tienda": cruda.etiqueta,
        "marca": cruda.marca,
        "mes": COLUMNA_MES,
        "unidades": cruda.unidades,
    }
    # Grande: el reporte va solo con esta hoja (ver LIMITE_DE_CELDAS_SOLO_CRUDOS).
    df.attrs["solo_crudos"] = solo_crudos
    return df


def ventas_crudas_completas(conexion, cruda: Cruda, desde: str, hasta: str):
    """Las filas de la tabla cruda del periodo, con todas sus columnas.

    Las mismas filas que cuenta el bot: unidades >= 1 (las vistas descartan
    devoluciones y ajustes, y sin eso el total del Excel no seria el del
    mensaje: en Elektra, enero de 2026, 181 362 contra 181 628) y, en AT&T,
    solo postpago de Dealers y Stores (ver Cruda.filtro). Coppel y Liverpool
    llevan ademas su mitad IOT, acomodada en las columnas equivalentes y
    marcada en [Fuente].

    None si no cabe en un Excel que WhatsApp acepte (ver
    LIMITE_DE_CELDAS_SOLO_CRUDOS): quien llama decide entonces si va en ZIP
    (crudo_en_zip) o agrupado. Entre los dos topes va marcado
    attrs["solo_crudos"].
    """
    columnas, filas = _tamano_del_crudo(conexion, cruda, desde, hasta)
    if not columnas:
        return None
    celdas = _celdas(cruda, columnas, filas)
    if celdas > LIMITE_DE_CELDAS_SOLO_CRUDOS:
        logger.info(
            "Raw_Data completo de %s no cabe en Excel (%d filas); va en ZIP o agrupado",
            cruda.tabla, filas,
        )
        return None
    trozos = list(_trozos_del_crudo(conexion, cruda, desde, hasta, columnas))
    if not trozos:
        return _con_atributos(pd.DataFrame(columns=columnas), cruda, False)
    df = pd.concat(trozos, ignore_index=True) if len(trozos) > 1 else trozos[0]
    return _con_atributos(df, cruda, celdas > LIMITE_DE_CELDAS_CRUDAS)


# Lo que no cabe en un Excel que WhatsApp acepte va en un ZIP con CSV: el
# CSV comprime seis veces mejor que el xlsx (que ya es un ZIP por dentro).
# Medido con Coppel enero-marzo de 2026, 1 473 521 filas: xlsx 52.8 MB, ese
# xlsx zipeado 40.8 MB, el CSV zipeado 8.4 MB en 4 s. Este tope son ~57 MB de
# ZIP, mas o menos un ano de Coppel; por encima, solo agrupado.
LIMITE_DE_CELDAS_ZIP = 100_000_000
# Un CSV por cada tanto, porque es lo que Excel abre de un CSV.
FILAS_POR_CSV = 1_048_575


def crudo_en_zip(conexion, cruda: Cruda, desde: str, hasta: str,
                 carpeta: str | None = None) -> tuple[str, pd.DataFrame] | None:
    """Escribe el crudo completo en un ZIP de CSV y devuelve (ruta, agrupado).

    Va por trozos: nunca tiene en memoria mas de TROZO_DE_FILAS filas del
    crudo. De paso arma lo agrupado (la hoja Datos), sumando trozo a trozo.
    None si no cabe ni asi (LIMITE_DE_CELDAS_ZIP).
    """
    import uuid
    import zipfile
    from datetime import datetime as _dt

    columnas, filas = _tamano_del_crudo(conexion, cruda, desde, hasta)
    if not columnas or _celdas(cruda, columnas, filas) > LIMITE_DE_CELDAS_ZIP:
        return None
    carpeta = carpeta or os.environ.get("EXPORT_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "exports"
    )
    os.makedirs(carpeta, exist_ok=True)
    nombre = f"detalle_{cruda.tabla.strip('$').lower()}_{desde}_{hasta}_{_dt.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    ruta = os.path.abspath(os.path.join(carpeta, f"{nombre}.zip"))

    parciales = []
    escritas = 0
    numero_csv, en_este_csv, archivo, escritor = 0, 0, None, None
    with zipfile.ZipFile(ruta, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zip_:
        try:
            for trozo in _trozos_del_crudo(conexion, cruda, desde, hasta, columnas):
                parciales.append(_agrupado_parcial(_con_atributos(trozo, cruda, True), cruda))
                escritas += len(trozo)
                inicio = 0
                while inicio < len(trozo):
                    if escritor is None or en_este_csv >= FILAS_POR_CSV:
                        if escritor is not None:
                            escritor.close()
                        numero_csv += 1
                        en_este_csv = 0
                        escritor = zip_.open(f"Raw_Data_{numero_csv}.csv", "w")
                        # BOM: sin el, Excel abre el CSV como ANSI y los
                        # acentos ("Día", "Organización") salen rotos.
                        escritor.write("\ufeff".encode("utf-8"))
                        escritor.write((",".join(_csv_campo(c) for c in trozo.columns) + "\n").encode("utf-8"))
                    cabe = min(FILAS_POR_CSV - en_este_csv, len(trozo) - inicio)
                    pedazo = trozo.iloc[inicio:inicio + cabe]
                    escritor.write(pedazo.to_csv(index=False, header=False).encode("utf-8"))
                    en_este_csv += cabe
                    inicio += cabe
        finally:
            if escritor is not None:
                escritor.close()

    agrupado = _juntar_agrupados(parciales)
    agrupado.attrs["agrupado"] = True
    agrupado.attrs["zip"] = ruta
    # Las filas que de verdad van en el ZIP, para que el mensaje las cite.
    agrupado.attrs["filas_zip"] = escritas
    logger.info("Crudo de %s en ZIP: %d filas, %.1f MB", cruda.tabla, filas, os.path.getsize(ruta) / 1e6)
    return ruta, agrupado


def _csv_campo(valor) -> str:
    texto = str(valor)
    return f'"{texto}"' if any(c in texto for c in ',"\n') else texto


def _agrupado_parcial(trozo: pd.DataFrame, cruda: Cruda) -> pd.DataFrame:
    """Lo agrupado de un trozo, con la clave para seguir sumando."""
    unidades = cruda.unidades
    clave = trozo[cruda.clave] if cruda.clave in trozo else trozo[cruda.etiqueta]
    trabajo = pd.DataFrame({
        "Region": trozo[COLUMNA_REGION],
        "Tienda": trozo[cruda.etiqueta].astype(str),
        "Clave": clave.where(clave.notna(), trozo[cruda.etiqueta]).astype(str),
        "Marca": trozo[cruda.marca],
        "Mes": trozo[COLUMNA_MES],
        "Unidades": pd.to_numeric(trozo[unidades], errors="coerce"),
    })
    return (
        trabajo.groupby(["Region", "Clave", "Marca", "Mes"], dropna=False, sort=False)
        .agg(Tienda=("Tienda", "max"), Unidades=("Unidades", "sum"))
        .reset_index()
    )


def _juntar_agrupados(parciales: list[pd.DataFrame]) -> pd.DataFrame:
    if not parciales:
        return pd.DataFrame(columns=["Region", "Tienda", "Marca", "Mes", "Unidades"])
    todo = pd.concat(parciales, ignore_index=True)
    return (
        todo.groupby(["Region", "Clave", "Marca", "Mes"], dropna=False, sort=False)
        .agg(Tienda=("Tienda", "max"), Unidades=("Unidades", "sum"))
        .reset_index()[["Region", "Tienda", "Marca", "Mes", "Unidades"]]
        .sort_values("Unidades", ascending=False, kind="stable")
        .reset_index(drop=True)
    )


def agrupar_completo(completo: pd.DataFrame, cruda: Cruda) -> pd.DataFrame:
    """La hoja Datos: el Raw_Data completo agrupado por region, tienda, marca
    y mes, igual que ventas_crudas pero sin volver a consultar la base.

    Se agrupa por la CLAVE de la tienda (el numero, estable) y se muestra su
    nombre, como en ventas_crudas: en Elektra la 6169 aparece como "EKT MEGA
    ZUMPANGO" y como "MEGA ZUMPANGO". Las filas IOT no traen clave; ahi la
    clave es el nombre. Es la misma cuenta que arma el ZIP por trozos.
    """
    return _juntar_agrupados([_agrupado_parcial(completo, cruda)])


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
    mitad_cruda = f"""
        SELECT {region} AS [Region],
               CAST(v.[{cruda.etiqueta}] AS NVARCHAR(200)) AS [Tienda],
               CAST(v.[{cruda.clave}] AS NVARCHAR(200)) AS [Clave],
               v.[{cruda.marca}] AS [Marca],
               MONTH(v.[{cruda.fecha}]) AS [Mes],
               v.[{cruda.unidades}] AS [Unidades]
        FROM [RAW_DATA_B2B].dbo.[{cruda.tabla}] v
        {join_region}
        WHERE v.[{cruda.fecha}] >= ? AND v.[{cruda.fecha}] < ?
          AND v.[{cruda.unidades}] >= 1{_filtro_de(cruda)}
    """
    partes, parametros = [mitad_cruda], [*parametros_region, desde, hasta]

    if cruda.vista_iot:
        partes.append(_MITAD_IOT.format(
            iot=cruda.vista_iot,
            principal=cruda.vista_iot[: -len("_IOT")],
            normalizado_catalogo=_NORMALIZAR.format(expr="[Coppel Name]"),
            normalizado_iot=_NORMALIZAR.format(expr="i.[Store]"),
            filtro_iot=_filtro_iot(cruda, "i."),
        ))
        parametros.extend(
            [cruda.retail_en_catalogo] * 3 + [desde, hasta]
        )

    sql = f"""
        SELECT MAX([Region]) AS [Region],
               MAX([Tienda]) AS [Tienda],
               [Marca], [Mes],
               SUM([Unidades]) AS [Unidades]
        FROM ({" UNION ALL ".join(partes)}) todo
        GROUP BY [Region], [Clave], [Marca], [Mes]
        ORDER BY SUM([Unidades]) DESC
    """
    parametros = tuple(parametros)
    cursor = conexion.exec_driver_sql(sql, parametros)
    filas = cursor.fetchall()
    return pd.DataFrame.from_records(filas, columns=list(cursor.keys()))


def _ventas_por_tienda(conexion, desde: str, hasta: str,
                       acotado: dict[str, list[str]] | None = None,
                       fuente: str = VENTAS):
    """Cuanto vendio cada tienda en ese periodo, la que mas primero.

    No se acota por MODELO: desde la consolidada eso obliga a una union que
    pierde el 92% del volumen (ver el encabezado del modulo). Por marca y por
    cadena SI, y hay que hacerlo -- son columnas de la propia vista, y sin
    ese filtro la hoja contradice al mensaje que la acompaña.

    `fuente` es la consolidada, o sus nueve vistas unidas cuando la del
    servidor trae basura en [QTY] (desde el 1 de octubre de 2026: el SUM
    fallaba con "nvarchar is invalid for sum operator" y la hoja no salia).
    """
    condiciones, parametros = ["v.[Date] >= ?", "v.[Date] < ?"], [desde, hasta]
    for columna, valores in (acotado or {}).items():
        condiciones.append(f"v.[{columna}] IN ({', '.join('?' * len(valores))})")
        parametros.extend(valores)

    sql = f"""
        SELECT
               v.[Retail], v.[Store], SUM(v.[QTY]) AS [Unidades]
        FROM {fuente} v
        WHERE {' AND '.join(condiciones)}
        GROUP BY v.[Retail], v.[Store]
        ORDER BY SUM(v.[QTY]) DESC
    """
    cursor = conexion.exec_driver_sql(sql, tuple(parametros))
    filas = cursor.fetchall()
    return pd.DataFrame.from_records(filas, columns=list(cursor.keys()))


@contextmanager
def _con_plazo(conexion, segundos: int):
    """Le pone plazo a la conexion mientras dura el bloque, y lo deja como estaba.

    El timeout de pyodbc vive en la conexion, y el pool de SQLAlchemy la
    recicla: sin restaurarlo, la siguiente consulta que la reutilice -- el
    pronostico, buscar_modelos, los precios, que no fijan plazo propio --
    heredaria este (que puede ser de 1s) y moriria con HYT00 sin razon.
    """
    bruta = conexion.connection.dbapi_connection
    anterior = getattr(bruta, "timeout", 0)
    bruta.timeout = segundos
    try:
        yield
    finally:
        bruta.timeout = anterior


def _datos_y_raw(engine, cruda: Cruda, rango, plazo) -> dict[str, pd.DataFrame]:
    """{"Datos": agrupado, "Raw_Data": completo}, o {} si la cruda no contesta.

    Si el completo no cabe (ver LIMITE_DE_CELDAS_CRUDAS) o falla, va solo
    Datos, agrupado desde la base, marcado con attrs["agrupado"] para que el
    bot avise que el detalle no cupo.
    """
    completo = None
    try:
        with engine.connect() as conexion, _con_plazo(conexion, plazo):
            completo = ventas_crudas_completas(conexion, cruda, *rango)
    except Exception:  # noqa: BLE001
        # Con todas las columnas tarda mas (Coppel, un mes: ~100 s por los
        # cruces con el catalogo). Si se pasa del plazo o falla, mejor lo
        # agrupado que nada.
        logger.exception("No se pudo traer Raw_Data completo; va agrupado")
    if completo is not None and not completo.empty:
        if completo.attrs.get("solo_crudos"):
            return {"Raw_Data": completo}
        return {"Datos": agrupar_completo(completo, cruda), "Raw_Data": completo}
    if completo is None:
        # No cabe en un Excel que WhatsApp acepte: el detalle va en un ZIP de
        # CSV (pedido el 5/10/2026) y el Excel lleva PIVOT y Datos. Sin plazo
        # de consulta: se lee en trozos y el tiempo se va en bajar filas.
        try:
            with engine.connect() as conexion:
                en_zip = crudo_en_zip(conexion, cruda, *rango)
            if en_zip:
                return {"Datos": en_zip[1]}
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo armar el ZIP del crudo; va agrupado")
    try:
        with engine.connect() as conexion, _con_plazo(conexion, plazo):
            agrupado = ventas_crudas(conexion, cruda, *rango)
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo traer el detalle crudo para el pivot")
        return {}
    if agrupado.empty:
        return {}
    agrupado.attrs["agrupado"] = True
    return {"Datos": agrupado}


def ficha_del_archivo(contexto: dict) -> dict | None:
    """Que trae el detalle del reporte de una cadena: filas y unidades.

    Es lo que el mensaje debe citar. Visto el 7/10/2026: la misma pregunta
    ("el excel de las ventas de AT&T de agosto") dijo 103 852 registros una
    vez y 51 869 otra, porque citaba las filas de la consulta del modelo
    (por dia una vez, por tienda la otra). Los dos Excel eran identicos:
    118 758 filas en Raw_Data. None si no es un reporte de cadena.
    """
    crudo = contexto.get("Raw_Data")
    datos = contexto.get("Datos")
    if crudo is not None and not crudo.empty:
        columna = (crudo.attrs.get("pivot") or {}).get("unidades")
        unidades = (
            pd.to_numeric(crudo[columna], errors="coerce").sum()
            if columna in crudo else None
        )
        return {"filas": len(crudo), "unidades": unidades, "donde": "Raw_Data"}
    if datos is not None and not datos.empty and "Unidades" in datos:
        unidades = pd.to_numeric(datos["Unidades"], errors="coerce").sum()
        if datos.attrs.get("zip"):
            return {"filas": datos.attrs.get("filas_zip"), "unidades": unidades, "donde": "ZIP"}
        return {"filas": len(datos), "unidades": unidades, "donde": "Datos"}
    return None


def contexto_del_resultado(
    df: pd.DataFrame, engine, query: str = "", segundos: int | None = None,
    cadenas=None, extras: bool = True, de_cadena: bool = True,
) -> dict[str, pd.DataFrame]:
    """Hojas de contexto para el resultado, vacio si no hay nada que agregar.

    `segundos` es lo que queda de la peticion. Quien llama lo sabe y este
    modulo no, asi que se recibe en vez de adivinarlo: un presupuesto fijo
    mas largo que el que queda no hace esperar mas, hace que la peticion
    entera se caiga por timeout y el usuario no reciba NADA.

    `cadenas` son las que puede ver quien pregunta (None, todas). Acotado,
    solo se agrega el detalle de UNA cadena suya: "Ventas por tienda" y las
    fichas de catalogo salen de tablas de todas las cadenas.

    `extras` en False deja fuera "Ventas por tienda" y las fichas de
    catalogo (Tiendas, Regiones, Carriers): pedido el 7/10/2026, un reporte
    trae lo que se pidio y esas hojas solo si se pidio un desglose.

    `de_cadena` en False no arma el reporte de la cadena entera (Datos y
    Raw_Data): solo cuando se pidio un archivo (ver agent.pidio_archivo).
    """
    if df is None or df.empty:
        return {}

    # `segundos or ...` estaria mal: un presupuesto de 0 es "ya no queda
    # tiempo", no "usa el tuyo". Y el piso es 1 porque en pyodbc timeout=0
    # significa SIN limite, justo lo contrario de lo que se quiere decir.
    pedido = SEGUNDOS_PARA_EL_DESGLOSE if segundos is None else segundos
    plazo = max(1, min(pedido, SEGUNDOS_PARA_EL_DESGLOSE))

    # Primero una hoja por cadena, si el reporte trae varias: van pegadas a
    # la hoja de datos, que es de donde salen.
    hojas: dict[str, pd.DataFrame] = hojas_por_cadena(df)

    # Un reporte de promotoria no lleva ventas pegadas. Visto el 1 de octubre
    # de 2026: "como se veia el ano pasado" (promotores de Sears por semana)
    # salio con el detalle de ventas de Sears de todo 2025, que nadie pidio,
    # y tardo 11 segundos mas en armar el pivot.
    if _DE_PROMOTORIA.search(query or ""):
        return hojas

    # El desglose por tienda va despues: es lo que se pidio ver, y lo unico
    # que cuesta tiempo. Si falla o no aplica, el resto sigue.
    # Unas tiendas o una region: el resultado ya es lo que se pidio. Ni el
    # reporte de la cadena entera ni las hojas extra.
    if acotado_a_tiendas(query):
        return hojas

    rango = periodo(df) or periodo_de_la_consulta(query)
    if rango:
        cadena = retailer_del_resultado(df, query)
        if cadenas and cadena not in cadenas:
            return hojas
        cruda = CRUDAS.get(cadena or "")
        marcas = acotado_del_resultado(df, query).get("Brand")
        if cruda and marcas:
            cruda = replace(cruda, marcas=tuple(marcas))

        # Reporte de UNA cadena: tres hojas y nada mas (pedido el 5/10/2026,
        # viendo el de "las ventas de Elektra de enero"): el PIVOT (lo arma
        # agent.py), Datos con lo agrupado por region, tienda, marca y mes,
        # y Raw_Data con las filas de la cruda y todas sus columnas. "Ventas
        # por tienda", las fichas de catalogo y la hoja del resultado se
        # quedan fuera -- el pivot ya cubre la tienda -- salvo que el usuario
        # pida un desglose (eso lo decide agent.py, que tiene la pregunta).
        if cruda and de_cadena:
            reporte = _datos_y_raw(engine, cruda, rango, plazo)
            if reporte:
                return reporte

        if cadenas or not extras:
            return hojas

        # Varias cadenas, o la cruda no contesto: el reporte de siempre.
        # Con el modelo si se puede cuando el reporte es de UNA cadena: ahi
        # hay una tabla cruda donde la homologacion casa. Si son varias, o si
        # no hay modelos, la hoja sale por periodo, que siempre es correcta.
        modelos = modelos_a_desglosar(df)
        try:
            with engine.connect() as conexion, _con_plazo(conexion, plazo):
                if cruda and modelos:
                    ventas = _ventas_por_tienda_y_modelo(
                        conexion, cruda, modelos, *rango
                    )
                else:
                    fuente = VENTAS
                    if consolidada_trae_basura(engine):
                        fuente = _union_de(_VISTAS_DE_LA_CONSOLIDADA)
                    ventas = _ventas_por_tienda(
                        conexion, *rango, acotado_del_resultado(df, query), fuente
                    )
            if not ventas.empty:
                hojas["Ventas por tienda"] = ventas
        except Exception:  # noqa: BLE001
            # El reporte ya esta listo; esta hoja es un extra. Que la vista de
            # ventas no conteste a tiempo no puede costar la respuesta.
            logger.exception("No se pudo desglosar por tienda")

    valores = _valores_de_texto(df)
    if not valores or cadenas or not extras:
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
