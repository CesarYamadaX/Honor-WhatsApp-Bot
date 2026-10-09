"""Catalogo de esquema del servidor GTM BI, leido de `catalogo.json`.

Existe porque DB_URI apunta a `master`: las herramientas de esquema de
LangChain (`sql_db_list_tables`, `sql_db_schema`) reflejan la base del
connection string, asi que ahi solo ven los objetos de sistema de master y el
agente queda ciego ante los datos reales, que viven en otras cinco bases del
mismo servidor. En vez de que el modelo adivine nombres, el catalogo le da el
indice completo en el prompt y las columnas exactas bajo demanda.

`catalogo.json` es una foto del esquema, no una lectura en vivo: si alguien
agrega o renombra columnas en el servidor, el bot sigue creyendo lo que dice el
archivo. Se regenera desde INFORMATION_SCHEMA y sys.partitions.
"""

import json
import logging
import re
import threading
import time
import weakref
from functools import lru_cache
from pathlib import Path

from langchain_core.tools import tool

logger = logging.getLogger("sql_agent.catalogo")

RUTA_CATALOGO = Path(__file__).with_name("catalogo.json")
RUTA_VALORES = Path(__file__).with_name("valores.json")


@lru_cache(maxsize=1)
def cargar_valores() -> dict:
    """Valores conocidos de las columnas que el agente consulta seguido.

    Son una foto, calculada una vez contra el servidor. Existen porque un
    SELECT DISTINCT sobre ETL_Consolidado_B2B (39M filas, UNION ALL de nueve
    vistas agregadas) tarda minutos: en produccion cada consulta de esas se
    comia los 20s de timeout de `valores_distintos` sin devolver nada, y tres
    llamadas agotaban los 90 segundos de la peticion entera antes de que la
    consulta de verdad alcanzara a correr.

    Son columnas de baja cardinalidad y poco movimiento (retailers, marcas,
    carriers, anios): si aparece un retailer nuevo, se regenera el archivo.
    """
    if not RUTA_VALORES.exists():
        return {}
    return json.loads(RUTA_VALORES.read_text(encoding="utf-8"))


# vw_ETL_Con_Modelo no existe en el servidor: sus valores son los de la
# consolidada (de la que es la misma venta) y, la region, los del catalogo
# de tiendas.
_VALORES_PRESTADOS = {
    "vw_etl_con_modelo": {"region": "CustomerStores", "*": "ETL_Consolidado_B2B"},
}


def valores_conocidos(objeto: str, columna: str) -> list[str] | None:
    """Los valores precalculados de esa columna, o None si no hay foto."""
    prestados = _VALORES_PRESTADOS.get(objeto.casefold())
    if prestados:
        objeto = prestados.get(columna.casefold(), prestados["*"])
    por_objeto = cargar_valores().get(objeto, {})
    for nombre, valores in por_objeto.items():
        if nombre.casefold() == columna.casefold():
            return valores
    return None

# `[Retail] = 'LIVERPOOL'` o `[Retail] IN ('LIVERPOOL', 'Coppel')`, con el
# alias de la tabla delante si lo lleva. Solo el igual y el IN: un LIKE lleva
# comodines y un <> con un valor que no existe no esconde nada.
_FILTRO_DE_TEXTO = re.compile(
    r"(?:\b\w+\s*\.\s*)?\[?(\w+)\]?\s*(?<![!<>])(?:=|\bIN\b)\s*"
    r"\(?\s*((?:'(?:[^']|'')*'\s*,\s*)*'(?:[^']|'')*')\s*\)?",
    re.IGNORECASE,
)
_LITERAL = re.compile(r"'((?:[^']|'')*)'")

# Cuantos valores reales se le muestran al modelo cuando su filtro no casa.
# [Brand] tiene 222: volcarlos todos es gastar el contexto de la vuelta en la
# que tiene que corregir. Los parecidos a lo que pidio van primero, que es lo
# que de verdad resuelve el caso -- quien pide 'HUAWEI HONOR' necesita ver
# 'HONOR', no la lista alfabetica desde ABS.
MAX_VALORES_SUGERIDOS = 25

# Coppel y Liverpool viven PARTIDOS en dos vistas cada uno, y usar una sola
# no da error: da de menos, en silencio. Medido el 25 de septiembre de 2026
# sobre toda la historia, y cuadra al dedo con la consolidada:
#
#   vista                    2024        2025        2026         TODO
#   vw_ETL_COPPEL       7 860 449   7 494 780   4 186 855   19 542 084
#   vw_ETL_COPPEL_IOT           0           0     880 689      880 689
#                                           suma =  20 422 773 = 'Coppel'
#   vw_ETL_Liverpool    1 597 169   1 601 575     875 494    4 074 238
#   vw_ETL_Liverpool_IOT        0      12 163     721 254      733 417
#          suma = 4 807 655 = 'Liverpool Offline' + 'Liverpool Online'
#
# El feed _IOT es cosa de 2026: cero en 2024 y 2025 en las dos cadenas. Por
# eso una medicion de UN mes puede no verlo -- en agosto de 2026 el de
# Coppel viene vacio, y de ahi salio la conclusion equivocada de que esa
# vista traia el Coppel entero. No lo trae: en 2026 le falta el 17%.
#
# Los dos casos se vieron en produccion. "Cuanto vendio Liverpool en agosto
# de 2026" contestaba 99 040 de 172 648 (el 57%), y "ventas de Coppel por
# mes en 2026" contestaba 4 186 855 de 5 067 544 (faltaban 880 689).
#
# Las demas cadenas si tienen una sola vista: Elektra, Sears, Movistar y
# Telcel casan al dedo con la consolidada.
# La tabla CRUDA tiene el mismo problema que su vista, y ahi el UNION ni
# siquiera es una opcion: Coppel_IOT es [Tienda][Familia][Marca][Modelo]
# [Attribute][Value] -- ni fecha ni cantidad --, y quien las pone en la misma
# forma es justamente la vista vw_ETL_*. Por eso el remedio que se ofrece
# para las crudas es solo la consolidada.
_AVISO = (
    "{objeto} es SOLO una parte de {cadena}: la otra esta en {otra}. {medido} "
    "NO contestes con ese numero como si fuera {cadena} entero. Salvo que te "
    "hayan pedido justamente esa parte, vuelve a consultar la cadena completa "
    "con ETL_Consolidado_B2B y [Retail] IN ({literales}){union}."
)
_UNION = (
    ", o haz UNION ALL de las dos vistas vw_ETL_* en la MISMA consulta, "
    "nombrando columnas (nunca SELECT *), por ejemplo: SELECT SUM([QTY]) "
    "FROM (SELECT [QTY] FROM [RAW_DATA_B2B].dbo.[{objeto}] WHERE ... "
    "UNION ALL SELECT [QTY] FROM [RAW_DATA_B2B].dbo.[{otra}] WHERE ...) t"
)


def _media(objeto, otra, cadena, literales, medido, union):
    return (objeto, otra, _AVISO.format(
        objeto=objeto, otra=otra, cadena=cadena, literales=literales,
        medido=medido,
        union=_UNION.format(objeto=objeto, otra=otra) if union else ""))


_LP = ("Medido en agosto de 2026, esto solo da 99 040 unidades de las "
       "172 648 que vendio Liverpool -- el 57%.")
_CP = ("Medido en 2026, esto solo da 4 186 855 unidades de las 5 067 544 "
       "que vendio Coppel -- le falta el 17%.")
_LIT_LP = "'Liverpool Offline', 'Liverpool Online'"

_MEDIA_CADENA = (
    _media("vw_ETL_Liverpool", "vw_ETL_Liverpool_IOT", "Liverpool", _LIT_LP, _LP, True),
    _media("vw_ETL_COPPEL", "vw_ETL_COPPEL_IOT", "Coppel", "'Coppel'", _CP, True),
    _media("LIVERPOOL_B2B$", "Liverpool_IOT", "Liverpool", _LIT_LP, _LP, False),
    _media("COPPEL_B2B", "Coppel_IOT", "Coppel", "'Coppel'", _CP, False),
)


# Las columnas que tienen TODAS las vistas vw_ETL_*. Medido el 30 de
# septiembre de 2026: a seis les agregaron [Key prices] y a tres no, asi
# que un SELECT * entre dos de ellas ya no es seguro. Estas diez si.
_COLUMNAS_COMUNES = (
    "Retail", "Store", "Brand", "Material_Description", "Carrier",
    "Date", "Year", "Month", "WEEK", "QTY",
)
_VISTAS_PARTIDAS = (
    ("vw_ETL_COPPEL", "vw_ETL_COPPEL_IOT"),
    ("vw_ETL_Liverpool", "vw_ETL_Liverpool_IOT"),
)
# Lo que puede venir despues del nombre de una tabla y NO es su alias.
_NO_SON_ALIAS = {
    "WHERE", "GROUP", "ORDER", "HAVING", "JOIN", "INNER", "LEFT", "RIGHT",
    "FULL", "CROSS", "OUTER", "ON", "UNION", "EXCEPT", "INTERSECT", "OPTION",
    "WITH", "PIVOT", "UNPIVOT",
}


def completar_media_cadena(query: str) -> tuple[str, list[str]]:
    """Reescribe `FROM vw_ETL_COPPEL` como la union de sus dos mitades.

    Antes esto solo se bloqueaba con un aviso, y el aviso no alcanzo: tres
    veces seguidas (30 de septiembre de 2026) el modelo lo leyo, contesto
    "no se puede determinar el total de Coppel" y siguio con las demas
    cadenas. Coppel es la cadena mas grande; quedarse sin ella no es un
    detalle. Asi que la consulta se completa sola.

    Solo las VISTAS: tienen la misma forma, y se unen por las columnas que
    comparten. Las tablas crudas no -- Coppel_IOT ni siquiera tiene fecha ni
    cantidad -- y siguen bloqueadas por `avisos_de_media_cadena`. Y solo lo
    que va despues de FROM o JOIN: un `[vw_ETL_COPPEL].[QTY]` en el SELECT se
    deja como esta, y sigue funcionando porque la union toma ese nombre como
    alias cuando la consulta no traia otro.

    Devuelve la consulta, y las vistas que se completaron para decirselo.
    """
    completadas: list[str] = []
    for vista, otra in _VISTAS_PARTIDAS:
        if re.search(rf"(?<!\w){re.escape(otra)}(?!\w)", query, re.IGNORECASE):
            continue
        query, cuantas = _reemplazar_objeto(query, vista, _union_de((vista, otra)))
        if cuantas:
            completadas.append(f"{vista} con {otra}")
    return query, completadas


def _union_de(vistas) -> str:
    """Las vistas unidas por sus columnas comunes, como tabla derivada."""
    columnas = ", ".join(f"[{c}]" for c in _COLUMNAS_COMUNES)
    return "(" + " UNION ALL ".join(
        f"SELECT {columnas} FROM [RAW_DATA_B2B].dbo.[{v}]" for v in vistas
    ) + ")"


def _reemplazar_objeto(query: str, nombre: str, derivada: str) -> tuple[str, int]:
    """Cambia `FROM <nombre>` por `FROM <derivada> AS <alias>`.

    Solo despues de FROM o JOIN: un `[nombre].[col]` en el SELECT se deja
    como esta, y sigue funcionando porque la tabla derivada toma ese nombre
    como alias cuando la consulta no traia otro. Si traia uno, lo respeta.
    """
    patron = re.compile(
        rf"(\b(?:FROM|JOIN)\s+)"
        rf"(?:\[?RAW_DATA_B2B\]?\s*\.\s*\[?dbo\]?\s*\.\s*)?"
        rf"\[?{re.escape(nombre)}\]?(?![\w\]])"
        rf"(\s+(?:AS\s+)?(\[?[A-Za-z_]\w*\]?))?",
        re.IGNORECASE,
    )

    def reemplazo(m):
        antes, resto, alias = m.group(1), m.group(2) or "", m.group(3)
        if alias and alias.strip("[]").upper() not in _NO_SON_ALIAS:
            return f"{antes}{derivada} AS {alias}"
        return f"{antes}{derivada} AS [{nombre}]{resto}"

    return patron.subn(reemplazo, query)


# Las nueve vistas que une ETL_Consolidado_B2B. No se puede leer la
# definicion desde aqui (el login es de solo lectura y no la ve), asi que la
# lista es la medida: el 30 de septiembre de 2026 su union por las columnas
# comunes dio 160 909 unidades de Honor en agosto y 2 056 940 del mercado,
# los mismos numeros que la consolidada cuando funcionaba.
_VISTAS_DE_LA_CONSOLIDADA = (
    "vw_ETL_ATT_Postpaid", "vw_ETL_COPPEL", "vw_ETL_COPPEL_IOT",
    "vw_ETL_Elektra", "vw_ETL_Liverpool", "vw_ETL_Liverpool_IOT",
    "vw_ETL_Movistar_CAC", "vw_ETL_Sears", "vw_ETL_TELCEL_B2B",
)


def consolidada_de_repuesto(query: str) -> str | None:
    """La misma consulta, con la consolidada reconstruida desde sus vistas.

    Para cuando la vista del servidor esta rota: el 30 de septiembre de 2026
    a seis de sus nueve vistas les agregaron [Key prices] y a tres no, y su
    UNION dejo de funcionar para cualquier consulta. Sin repuesto el modelo
    consultaba cadena por cadena y sumaba de cabeza, y se equivoco: dijo
    116 105 unidades cuando sus propios numeros sumaban 160 909.

    None si la consulta no usa la consolidada.
    """
    nueva, cuantas = _reemplazar_objeto(
        query, "ETL_Consolidado_B2B", _union_de(_VISTAS_DE_LA_CONSOLIDADA)
    )
    return nueva if cuantas else None


# La otra forma de estar rota, peor porque no da error: el 1 de octubre de
# 2026 la consolidada volvio a responder, pero su [QTY] trae el texto de
# [Key prices] ('2026 8 36 N.HO600N AT&T Cacs HONOR 600 ...') en el 100% de
# las filas, aunque INFORMATION_SCHEMA siga diciendo float. SUM falla por
# nvarchar, el modelo prueba TRY_CONVERT, sale NULL y contesta "no pude
# calcular" tras dos minutos. Sus filas si coinciden una por una con las
# nueve vistas (por cadena, agosto de 2026), asi que el repuesto sirve igual.
# Se mira una muestra antes de usarla y se recuerda un rato: cuando la
# arreglen, se vuelve sola a la real.
_SONDA_DE_LA_CONSOLIDADA = (
    "SELECT COUNT(*) FROM (SELECT TOP 200 [QTY] AS q FROM "
    "[RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B]) AS muestra "
    "WHERE muestra.q IS NOT NULL AND TRY_CONVERT(float, muestra.q) IS NULL"
)
_CADA_CUANTO_SE_MIRA_LA_CONSOLIDADA = 600  # segundos
SEGUNDOS_PARA_LA_SONDA = 30
_consolidada_mirada: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_candado_consolidada = threading.Lock()


def consolidada_trae_basura(engine) -> bool:
    """True si el [QTY] de la consolidada no son numeros.

    Si la sonda misma falla, False: ese caso ya lo cubre el reintento por
    error, y una sonda que no corre (otro motor, red caida) no debe desviar
    consultas que quiza funcionan.
    """
    ahora = time.monotonic()
    with _candado_consolidada:
        visto = _consolidada_mirada.get(engine)
        if visto and ahora - visto[0] < _CADA_CUANTO_SE_MIRA_LA_CONSOLIDADA:
            return visto[1]
    try:
        with engine.connect() as conexion:
            # Con plazo: es la unica consulta al servidor de ese camino sin
            # el suyo, y el 2 de octubre de 2026 una respuesta se quedo
            # colgada despues de su consulta hasta el timeout de 10 minutos.
            bruta = getattr(getattr(conexion, "connection", None), "dbapi_connection", None)
            anterior = getattr(bruta, "timeout", 0)
            if bruta is not None:
                bruta.timeout = SEGUNDOS_PARA_LA_SONDA
            try:
                basura = bool(conexion.exec_driver_sql(_SONDA_DE_LA_CONSOLIDADA).scalar())
            finally:
                if bruta is not None:
                    bruta.timeout = anterior
    except Exception:  # noqa: BLE001 - ver docstring
        # Sin recordarlo: que la proxima consulta vuelva a mirar en vez de
        # confiar diez minutos en una sonda que no contesto.
        logger.warning("No se pudo mirar la vista consolidada", exc_info=True)
        return False
    if basura:
        logger.warning("ETL_Consolidado_B2B trae [QTY] que no son numeros; se reconstruye")
    with _candado_consolidada:
        _consolidada_mirada[engine] = (ahora, basura)
    return basura



# Cadenas que se pueden nombrar en una pregunta, y como se escriben en
# CustomerStores.[RetailName].
_CADENAS_EN_PREGUNTA = (
    ("COPPEL", r"coppel"),
    ("ELEKTRA", r"elektra"),
    ("TELCEL", r"telcel"),
    ("LIVERPOOL", r"liverpool"),
    ("AT&T", r"at\s*&\s*t|\batt\b"),
    ("SEARS", r"sears"),
    ("MOVISTAR", r"movistar"),
)

# La cadena de una tienda de HCReport: la del catalogo por [ID] y, si no
# esta ahi, la primera palabra de su nombre. Movistar solo sale por la
# segunda: ninguna de sus tiendas esta en CustomerStores.
CADENA_DE_PROMOTORIA = (
    "UPPER(COALESCE(cs.[RetailName], "
    "LEFT(h.[NAME], CHARINDEX(' ', h.[NAME] + ' ') - 1)))"
)
_UNION_DE_PROMOTORIA = (
    "LEFT JOIN (SELECT DISTINCT [ID], [RetailName], [Region] FROM "
    "[GTM BI Database].dbo.[CustomerStores]) cs ON cs.[ID] = h.[ID]"
)

# La semana que representa cada mes en promotoria (la ultima completa), el
# mismo mapa que trae V_HCReport_Informes. 2025 y 2026 difieren en enero,
# febrero y octubre.
SEMANA_DEL_MES = {
    2025: {1: "W04", 2: "W08", 3: "W13", 4: "W17", 5: "W22", 6: "W26",
           7: "W30", 8: "W35", 9: "W39", 10: "W43", 11: "W48", 12: "W52"},
    2026: {1: "W05", 2: "W09", 3: "W13", 4: "W17", 5: "W22", 6: "W26",
           7: "W30", 8: "W35", 9: "W39", 10: "W44", 11: "W48", 12: "W52"},
}
_MESES_EN_PREGUNTA = (
    (1, r"enero|january"), (2, r"febrero|february"), (3, r"marzo|march"),
    (4, r"abril|april"), (5, r"mayo|may"), (6, r"junio|june"),
    (7, r"julio|july"), (8, r"agosto|august"), (9, r"septiembre|setiembre|september"),
    (10, r"octubre|october"), (11, r"noviembre|november"), (12, r"diciembre|december"),
)


def _semanas_que_faltan(query: str, pregunta: str) -> list[tuple[str, int, str]]:
    """Los meses que nombra la pregunta cuya semana no esta en la consulta.

    Visto el 5/10/2026: "cuantos promotores hay en Coppel en agosto 2026" se
    filtro con W30 (julio) y salio 754 en vez de 757. La nota del catalogo
    tiene el mapa; el modelo lo leyo mal.
    """
    semanas = {s.upper() for s in re.findall(r"'(W\d{2})'", query, re.IGNORECASE)}
    if not semanas:
        return []  # sin semana fija (una tendencia por semana): no aplica
    anios = {int(a) for a in re.findall(r"\bYEAR\]?\s*=\s*(\d{4})", query, re.IGNORECASE)}
    anios |= {int(a) for a in re.findall(r"\b(2025|2026)\b", pregunta or "")}
    faltan = []
    for mes, patron in _MESES_EN_PREGUNTA:
        if not re.search(rf"\b(?:{patron})\b", pregunta or "", re.IGNORECASE):
            continue
        for anio in sorted(anios) or [2026]:
            esperada = SEMANA_DEL_MES.get(anio, {}).get(mes)
            if esperada and esperada not in semanas:
                faltan.append((esperada, anio, patron.split("|")[0]))
    return faltan


def _filtra_o_agrupa(query: str, columna: str) -> bool:
    filtra = re.search(
        rf"\[?{columna}\]?\s*(=|IN\b|LIKE\b|BETWEEN\b|[<>])", query, re.IGNORECASE
    )
    agrupa = re.search(rf"GROUP\s+BY[\s\S]*\b{columna}\b", query, re.IGNORECASE)
    return bool(filtra or agrupa)


def avisos_de_promotoria(query: str, pregunta: str = "") -> list[str]:
    """Consultas de promotoria que van a contar de mas o de menos.

    La regla, dada por el negocio el 2 de octubre de 2026: la promotoria se
    saca de HCReport, y una tienda tiene promotoria si [FIXED PROMOTER] o
    [DOUBLE PROMOTER] vale 1. Antes el bot iba a V_HCReport_Informes, que
    es un INNER JOIN con CustomerStores: en agosto de 2026 contaba 1 289
    tiendas con promotor contra 1 323 de HCReport, y Movistar no salia.

    Tambien quedan los errores medidos el 30 de septiembre de 2026 con
    "cuantas tiendas de Coppel tienen promotor": sin periodo (junta todas
    las semanas) y sin cadena (contaba todas), contesto 1 721 y eran 676.
    """
    if re.search(r"(?<!\w)(V_HCReport_Informes|HCReport_VW)(?!\w)", query, re.IGNORECASE):
        return [
            "Para promotoria usa [GTM BI Database].dbo.[HCReport], no esa "
            "vista: V_HCReport_Informes deja fuera las tiendas que no estan "
            "en CustomerStores (Movistar entero; en agosto de 2026 daba 1 289 "
            "tiendas con promotor y son 1 323) y HCReport_VW no tiene cadena "
            "ni semana. Una tienda tiene promotoria si `h.[FIXED PROMOTER] = 1 "
            "OR h.[DOUBLE PROMOTER] = 1`; un mes es su ultima semana completa "
            "(agosto de 2026 = W35)."
        ]
    if not re.search(r"(?<!\w)HCReport(?!\w)", query, re.IGNORECASE):
        return []

    avisos = []
    if not (_filtra_o_agrupa(query, "Week") and _filtra_o_agrupa(query, "YEAR")):
        avisos.append(
            "Falta filtrar la semana: cada semana trae todas las tiendas otra "
            "vez, asi que sin [YEAR] y [Week] el conteo las junta. Filtra "
            "una (`h.[YEAR] = 2026 AND h.[Week] = 'W35'`); un mes es su "
            "ultima semana completa, y si no te dijeron periodo usa la mas "
            "reciente y dilo."
        )

    fijo = re.search(r"FIXED\s+PROMOTER", query, re.IGNORECASE)
    doble = re.search(r"DOUBLE\s+PROMOTER", query, re.IGNORECASE)
    pidio_uno = re.search(r"fij|fixed|dobl|double", pregunta or "", re.IGNORECASE)
    if bool(fijo) != bool(doble) and not pidio_uno:
        avisos.append(
            "La promotoria se cuenta con las DOS columnas: una tienda tiene "
            "promotoria si `h.[FIXED PROMOTER] = 1 OR h.[DOUBLE PROMOTER] = 1`. "
            "Con una sola te saltas tiendas."
        )

    # Una tienda con fijo y doble en 1 tiene DOS promotores: si preguntan
    # por personas, contar tiendas da de menos (W35 de 2026: 1 323 tiendas,
    # 1 439 promotores).
    pide_personas = re.search(
        r"cu[aá]nt[oa]s\s+promotor|n[uú]mero\s+de\s+promotor|total\s+de\s+promotor"
        r"|how\s+many\s+promoters|number\s+of\s+promoters",
        pregunta or "", re.IGNORECASE,
    )
    suma_las_dos = re.search(
        r"(FIXED|DOUBLE)\s+PROMOTER\]?\s*\)?\s*\+\s*(SUM\s*\(\s*)?[\w.\[]*(DOUBLE|FIXED)\s+PROMOTER",
        query, re.IGNORECASE,
    )
    if pide_personas and not suma_las_dos:
        avisos.append(
            "Preguntan cuantos PROMOTORES (personas), no cuantas tiendas: si "
            "las dos columnas valen 1, esa tienda tiene DOS promotores. Cuenta "
            "con `SUM(h.[FIXED PROMOTER] + h.[DOUBLE PROMOTER])`."
        )

    for esperada, anio, mes in _semanas_que_faltan(query, pregunta):
        avisos.append(
            f"La pregunta es de {mes} de {anio}, y en promotoria ese mes es la "
            f"semana {esperada} (su ultima semana completa); la consulta filtra "
            "otra. Usa `h.[Week] = '" + esperada + "'`."
        )

    usa_catalogo = re.search(r"CustomerStores|RetailName", query, re.IGNORECASE)
    if usa_catalogo and not re.search(r"\bLEFT\s+(OUTER\s+)?JOIN\b", query, re.IGNORECASE):
        avisos.append(
            "El cruce con CustomerStores tiene que ser LEFT JOIN: 3 801 de las "
            "8 330 tiendas de HCReport no estan en el catalogo, y un JOIN "
            f"normal las tira (Movistar entero). Usa `{_UNION_DE_PROMOTORIA}`."
        )
    elif usa_catalogo and not re.search(r"\[NAME\]|\.NAME\b", query, re.IGNORECASE):
        avisos.append(
            "La cadena no puede salir solo de cs.[RetailName]: las tiendas que "
            "no estan en el catalogo quedan sin cadena (Movistar entero). Usa "
            f"`{CADENA_DE_PROMOTORIA}`."
        )

    nombradas = [
        cadena for cadena, patron in _CADENAS_EN_PREGUNTA
        if re.search(patron, pregunta or "", re.IGNORECASE)
    ]
    sin_filtrar = [
        cadena for cadena in nombradas
        if not re.search(rf"'{re.escape(cadena)}'", query, re.IGNORECASE)
    ]
    if sin_filtrar:
        avisos.append(
            f"La pregunta es de {', '.join(sin_filtrar)}, pero HCReport no "
            "tiene columna de cadena y la consulta no filtra esa: contaria "
            f"las tiendas de todas. Agrega `{_UNION_DE_PROMOTORIA}` y filtra "
            f"`{CADENA_DE_PROMOTORIA} = '{sin_filtrar[0]}'`."
        )
    return avisos


def avisos_de_media_cadena(query: str) -> list[str]:
    """Avisos por consultar una vista que es solo un pedazo de su cadena."""
    # `(?<!\w)...(?!\w)` y no `\b...\b`: la tabla de Liverpool se llama
    # LIVERPOOL_B2B$ y `\b` detras del `$` nunca casa. De paso, el lookahead
    # es lo que impide que vw_ETL_COPPEL se reconozca dentro de
    # vw_ETL_COPPEL_IOT, que seria el aviso justo al reves.
    def nombra(objeto):
        return re.search(rf"(?<!\w){re.escape(objeto)}(?!\w)", query, re.IGNORECASE)

    return [
        aviso
        for objeto, la_otra, aviso in _MEDIA_CADENA
        if nombra(objeto) and not nombra(la_otra)
    ]


def _objetos_nombrados(query: str) -> list[str]:
    """Los objetos con foto de valores que esta consulta menciona."""
    candidatos = list(cargar_valores()) + ["vw_ETL_Con_Modelo"]  # usa los valores prestados
    return [o for o in candidatos if re.search(rf"\b{re.escape(o)}\b", query, re.I)]


def _parecidos_primero(conocidos: list[str], pedidos: list[str]) -> str:
    """Los valores reales a mostrar: los que se parecen a lo pedido, primero."""
    def se_parece(valor: str) -> bool:
        v = valor.casefold()
        return any(v in p.casefold() or p.casefold() in v for p in pedidos)

    ordenados = sorted(conocidos, key=lambda v: not se_parece(v))
    listado = ", ".join(ordenados[:MAX_VALORES_SUGERIDOS])
    sobran = len(ordenados) - MAX_VALORES_SUGERIDOS
    return f"{listado} (y {sobran} mas)" if sobran > 0 else listado


def filtros_que_no_casan(query: str) -> list[str]:
    """Filtros de la consulta cuyo valor no existe en esa columna.

    El caso: `[Retail] IN ('LIVERPOOL')` sobre la consolidada. Ahi la cadena
    se llama 'Liverpool Offline' y 'Liverpool Online', asi que ese filtro se
    lleva las ventas de Liverpool EN SILENCIO -- no es un error, son cero
    filas, y si la consulta trae otras cadenas ni siquiera viene vacia. El
    reporte sale sin Liverpool y nadie se entera.

    Solo mira las columnas de las que hay foto (retailers, marcas, carriers,
    regiones, periodos): son de baja cardinalidad y poco movimiento. La
    comparacion ignora mayusculas, como el servidor.
    """
    objetos = _objetos_nombrados(query)
    if not objetos:
        return []

    avisos = []
    for columna, literales in _FILTRO_DE_TEXTO.findall(query):
        conocidos: list[str] = []
        for objeto in objetos:
            conocidos.extend(valores_conocidos(objeto, columna) or [])
        if not conocidos:
            continue

        existentes = {v.casefold() for v in conocidos}
        pedidos = [v.replace("''", "'") for v in _LITERAL.findall(literales)]
        faltan = [v for v in pedidos if v.casefold() not in existentes]
        if faltan:
            avisos.append(
                f"En [{columna}] no existe {', '.join(repr(v) for v in faltan)}. "
                f"Los valores que hay son: {_parecidos_primero(conocidos, faltan)}."
            )
    return avisos


# Lo que el esquema NO dice y hace que el agente se equivoque con seguridad.
#
# Viven aqui y no en catalogo.json a proposito: el JSON es un volcado de
# INFORMATION_SCHEMA que se regenera, y una regeneracion borraria cualquier
# nota escrita a mano. Esto es conocimiento de negocio curado, y se revisa
# como codigo.
#
# El caso que las motivo: a "cuantas ventas hubo en Elektra en marzo 2026" el
# agente respondio "no se encontraron ventas" porque genero
# `WHERE [Carrier] = 'Elektra'` sobre EKT_B2B. Ahi Carrier son operadores
# telefonicos, no retailers, asi que el filtro daba cero filas -- y un cero
# se reporto como "no hay ventas" en vez de como "mi filtro esta mal".
NOTAS_GENERALES = [
    "Las tablas de RAW_DATA_B2B son la descarga cruda de UN solo retailer cada "
    "una, identificado por el nombre de la tabla y no por una columna: "
    "EKT_B2B=Elektra, COPPEL_B2B y Coppel_IOT=Coppel, TELCEL_B2B=Telcel, "
    "ATT_B2B=AT&T, LIVERPOOL_B2B$ y Liverpool_IOT=Liverpool, MVS_B2B=Movistar, "
    "SEARS_B2B=Sears. Salvo SEARS_B2B, ninguna tiene columna [Retail]: filtrar "
    "por el nombre del retailer dentro de ellas siempre da cero filas.",
    "[Carrier] es el operador telefonico (TELCEL, AT&T, MOVISTAR, ABIERTA), "
    "NUNCA el retailer. Para filtrar por retailer se usa [Retail].",
    # Medido el 30 de septiembre de 2026 contra las nueve vistas vw_ETL_*. Un
    # comparativo de julio 2026 contra julio 2025 contesto "Elektra julio
    # 2025: 0 unidades", como si hubiera dejado de vender.
    "DESDE CUANDO HAY DATOS, por cadena. Coppel, Liverpool y Telcel tienen "
    "desde enero de 2024. Elektra (2025-10-01), AT&T (2025-10-06), Sears y "
    "Movistar (2025-10-31) empiezan en OCTUBRE DE 2025: antes de esa fecha no "
    "vendieron cero, simplemente no hay datos cargados. En un comparativo "
    "contra el ano anterior, o en cualquier periodo previo a octubre de 2025, "
    "NUNCA pongas '0 unidades' para esas cuatro: di 'sin datos (la carga "
    "empieza en octubre de 2025)' y no calcules crecimientos contra un "
    "periodo sin datos.",
    # Medido el 1 de octubre de 2026, con agosto de 2026.
    "[Key prices] (en las nueve vistas vw_ETL_*, NO en la consolidada) es "
    "una llave de texto: ano, mes, semana, la descripcion del retailer, la "
    "cadena y el carrier ('2026 8 32 SAMSUNG A07 Coppel Out of Scope'). NO la "
    "uses para cruzar con [Prices_Weekly_VW].[Key]: casan el 0% en las siete "
    "cadenas, son formatos distintos. Para precio por modelo ve a "
    "Prices_Weekly_VW por su propia ruta (ver su ficha).",
    "La vista [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B] es la fuente OFICIAL de "
    "venta B2B, la que usa el negocio: unifica a todos los retailers con "
    "columnas [Retail], [Store], [Brand], [Material_Description], [Carrier], "
    "[Date], [Year], [Month], [WEEK] y [QTY] (NO tiene [Day] ni [Key prices]). "
    "Usala por "
    "defecto, y escribe su nombre COMPLETO tal cual: vive en RAW_DATA_B2B y "
    "NO en [GTM BI Database] -- escribirla ahi es el error mas comun y falla "
    "con 'Invalid object name'. Las tablas crudas (COPPEL_B2B, EKT_B2B, "
    "TELCEL_B2B, LIVERPOOL_B2B$, ATT_B2B, SEARS_B2B, MVS_B2B y sus _IOT) son "
    "su insumo y NO se usan para responder: no cuadran con la venta oficial "
    "y una consulta que las lea no se ejecuta. El detalle crudo le llega al "
    "usuario solo, en el Excel.",
    "Este es el BI de Honor Mexico, pero las tablas de venta traen TODAS las "
    "marcas (Samsung, Motorola, Oppo, Xiaomi, Apple...): son datos de "
    "competencia. Por eso 'las ventas de Honor' NO son el total de la tabla: "
    "exigen `[Brand] = 'HONOR'` explicito. Si el usuario nombra una marca y no "
    "filtras por ella, le estaras reportando el mercado entero como si fuera "
    "de esa marca, y el numero saldra varias veces mas grande de lo real.",
    "NUNCA filtres ETL_Consolidado_B2B por [Retail]. Si la pregunta es de UN "
    "retailer, consulta directamente la vista de ESE retailer (o, si piden "
    "el MODELO o la clasificacion, vw_ETL_Con_Modelo filtrada por [Retail]). "
    "Las vistas por retailer son para totales, tendencias y cortes por tienda o "
    "carrier: "
    "[RAW_DATA_B2B].dbo.[vw_ETL_COPPEL] para Coppel, [vw_ETL_Elektra] para "
    "Elektra, [vw_ETL_Liverpool] para Liverpool, [vw_ETL_TELCEL_B2B] para "
    "Telcel (cubre sus seis canales), [vw_ETL_ATT_Postpaid] para AT&T, "
    "[vw_ETL_Movistar_CAC] para Movistar y [vw_ETL_Sears] para Sears. Traen "
    "las mismas columnas que la consolidada --es la union de estas nueve-- "
    "asi que el dato es igual de oficial. "
    "El motivo es de rendimiento, medido: filtrar la consolidada por [Retail] "
    "tarda entre 60 y 210 SEGUNDOS, porque [Retail] es una constante fija en "
    "cada rama de la union y el motor no puede descartar ninguna: las calcula "
    "todas. Acotar por fecha NO ayuda (un solo SUM de un mes tarda 63s igual). "
    "Deja la consolidada para comparar entre retailers o filtrar por [Brand], "
    "que si es rapido (~9s).",
    "POR MODELO, POR CLASIFICACION O POR REGION ('solo celulares', 'que "
    "modelos se vendieron', 'top de modelos', 'la region 4'): usa "
    "[RAW_DATA_B2B].dbo.[vw_ETL_Con_Modelo]. "
    "Es la venta oficial de las nueve vistas ETL con tres columnas mas, "
    "[Region] (R1 a R9, RM) y: "
    "[Modelo] (el modelo homologado, 'HONOR MAGIC 8 LITE 512GB 5G') y "
    "[Clasificacion] ('SMARTPHONE', 'TABLET', 'LAPTOP', 'SMARTWATCH', "
    "'AUDIO', 'ACCESORIO'). Trae las mismas columnas que la consolidada "
    "([Retail], [Store], [Brand], [Material_Description], [Carrier], [Date], "
    "[Year], [Month], [WEEK], [QTY]), asi que se filtra igual, y suma "
    "exactamente lo mismo que las vistas (medido en agosto de 2026, cadena por "
    "cadena). NO la unas tu con UNICOS$: el modelo ya viene puesto, con el "
    "cruce correcto de cada cadena. Ejemplo:\n"
    "  SELECT [Modelo], SUM([QTY]) AS [Unidades]\n"
    "  FROM [RAW_DATA_B2B].dbo.[vw_ETL_Con_Modelo]\n"
    "  WHERE [Brand] = 'HONOR' AND [Clasificacion] = 'SMARTPHONE' AND [Year] = 2026 AND [Month] = 8\n"
    "  GROUP BY [Modelo] ORDER BY [Unidades] DESC\n"
    "Lo que el catalogo aun no tiene sale con [Modelo] NULL: no se pierde de "
    "los totales. Agrupar por la descripcion del retailer parte un mismo "
    "modelo en varias filas: agrupa siempre por [Modelo].",
    "UNICOS$ tiene ademas [CLASSIFICATION], que separa SMARTPHONE de TABLET, "
    "LAPTOP, COMPUTER, SMARTWATCH, AUDIO y ACCESORIO. Es la forma limpia de "
    "responder 'solo celulares' o 'solo accesorios' sin adivinar por el "
    "nombre del modelo.",
    "LA COLUMNA DE FECHA SE LLAMA DISTINTO EN CADA TABLA. En las vistas "
    "vw_ETL_* y en ETL_Consolidado_B2B siempre es [Date] (datetime) mas "
    "[Year], [Month] y [WEEK] como ENTEROS: ahi se filtra con "
    "`[Year]=2026 AND [Month]=3`. Desde el 30 de septiembre de 2026 esas "
    "vistas YA NO TIENEN [Day]: para un dia, filtra [Date]. [WEEK] es la "
    "semana del ano de SQL Server con semanas de domingo a sabado "
    "(DATEPART(week, [Date]); NO es la semana ISO): la semana 1 es la que "
    "contiene el 1 de enero, asi que en el cambio de ano hay semanas partidas "
    "(28-31 de diciembre de 2025 es la 53; 1-3 de enero de 2026 ya es la 1). "
    "Para 'la semana 35' filtra `[Year]=2026 AND [WEEK]=35`. "
    "En las tablas crudas cambia por retailer: "
    "COPPEL_B2B y Coppel_IOT [Día], LIVERPOOL_B2B$ y Liverpool_IOT "
    "[Día/Periodo], EKT_B2B [Day], TELCEL_B2B, MVS_B2B y SEARS_B2B [Fecha], "
    "ATT_B2B [Fecha de Transacción]; todas datetime, se filtran por rango "
    "(`>= '2026-01-01' AND < '2026-02-01'`).\n"
    "TRAMPA: [Day] solo existe ya en la tabla cruda EKT_B2B, y ahi es la "
    "fecha completa (datetime), no el dia del mes. Y [Month] es entero en las vistas pero texto en EKT_B2B "
    "('Mar', 'Nov'), MVS_B2B y SEARS_B2B. Comprueba con `describir_objeto` "
    "antes de filtrar por fecha en una tabla que no hayas usado.",
    "PRECIOS: hay DOS fuentes y no son lo mismo. "
    "[GTM BI Database].dbo.[Prices] es el precio interno, el que maneja el "
    "negocio, y es la fuente por defecto; su [Material Description] es el texto "
    "de cada cadena, que casa con UNICOS$ por [MATERIAL DESCRIPTION FINAL] y si "
    "no por [DESCRIPCION] (91-100% por cadena, medido el 8/10/2026). "
    "A 'en cuanto esta X' o 'precio de X' (visto el 8/10/2026, mal contestado): "
    "(1) el precio ACTUAL es el de la fecha mas reciente de cada [Retail] y "
    "[Carrier] -- nunca el MIN de toda la historia, que trae precios de hace "
    "un ano -- y se dice de que fecha es; (2) contesta con TODAS las cadenas y "
    "carriers que lo tienen, no con dos ni tres: una linea por cadena "
    "('- AT&T (pospago): $19,999, 27 sep 2026'), juntando en una las que "
    "tienen el mismo precio, y si el ultimo dato de una cadena tiene mas de "
    "dos meses, dilo ('ultimo dato: sep 2025'); (3) solo la variante que "
    "pidieron: 'iPhone 17' no incluye 17 "
    "Pro, Pro Max, Air ni 17e, y un LIKE sobre el texto se los cuela -- "
    "filtra por el modelo homologado de UNICOS$. "
    "Si ahi no aparece el modelo, usa la herramienta `precio_en_internet`, "
    "que consulta el scraping de las tiendas en linea ([Scrap]): trae el "
    "precio publicado, la fecha, el carrier y la liga del producto. "
    "Al responder con esa fuente hay que decir SIEMPRE que es el precio "
    "publicado en internet ese dia --no el interno-- y dar la liga y el "
    "carrier de cada uno: sin el carrier no se sabe si es con plan o libre, y "
    "sin la liga el usuario no puede verificarlo.",
    "Las vistas y tablas con IOT en el nombre (vw_ETL_COPPEL_IOT, "
    "vw_ETL_Liverpool_IOT, Coppel_IOT, Liverpool_IOT) son la linea que el "
    "negocio llama accesorios: NO son telefonos. En los datos son tablets, "
    "laptops, equipos gamer y computo (iPad, Galaxy Tab, MacBook, Alienware, "
    "Acer Aspire). Si preguntan por celulares no las incluyas; si preguntan "
    "por accesorios o por tablets y laptops, son ESAS y no las normales.",
    "En las tablas de venta no hay columna [Model] ni [Modelo]. El modelo del "
    "telefono va dentro de [Material_Description] en ETL_Consolidado_B2B (y en "
    "[Modelo Homologado] o [Desc Sku] en las crudas). [Brand] es el "
    "fabricante, no el modelo.",
    "En ETL_Consolidado_B2B cada fila es una combinacion de tienda, SKU, "
    "carrier y dia ya sumada, no un movimiento suelto. Para unidades vendidas "
    "usa SUM([QTY]); COUNT(*) responde otra pregunta y casi nunca es la que "
    "te hicieron.",
    "Los nombres de retailer no son uniformes ni una sola etiqueta por cadena: "
    "Elektra aparece como 'ELEKTRA' en mayusculas, y Telcel esta repartido en "
    "'TELCEL CAC', 'TELCEL RETAIL', 'TELCEL DISTR. ASOC.', 'TELCEL WEB', "
    "'TELCEL CORPO.' y 'TELCEL VENTA EMP.'. Filtra con LIKE, o mira antes con "
    "`valores_distintos` como estan escritos.",
    # Medido el 9/10/2026 con el resumen local (2026).
    "[Retail] = 'Unknown' son ventas de AT&T: vienen de vw_ETL_ATT_Postpaid "
    "sin canal (CACs o Dealers). Sumalas a AT&T y NUNCA las reportes como una "
    "cadena aparte: son unas decenas de unidades al mes, y su share sale "
    "absurdo (en septiembre de 2026, 12 de 60 = '20%').",
    "AYER, HOY, ESTA SEMANA: la venta llega por semana y con retraso, no al "
    "dia. A 'las ventas de ayer' o 'de hoy' no contestes solo 'no hay datos': "
    "busca la semana mas reciente cargada de cada cadena con [Year] y [WEEK] "
    "(MAX([Year] * 100 + [WEEK]) por [Retail]) -- NO con MAX([Date]), que "
    "recorre todo en el servidor y tardo 160 s (9/10/2026); con [Year] y "
    "[WEEK] se contesta en un segundo -- y di hasta donde llega cada cadena "
    "('lo mas reciente es la semana 40'), y ofrece esa semana.",
    "CADENAS CON DATOS: AT&T, Coppel, Elektra, Liverpool, Movistar, Sears y "
    "Telcel; no hay otras. Si preguntan por una que no esta (Walmart, "
    "Bodega Aurrera, Amazon, Mercado Libre en ventas...), no digas 'no "
    "encuentro datos' como si faltara cargar algo: di que esa cadena no esta "
    "en la informacion del bot y cuales si estan. (Suburbia y Mercado Libre "
    "solo existen en los precios de internet.)",
    "REPORTE MENSUAL: Sears, Movistar y la mitad IOT de Coppel "
    "(vw_ETL_COPPEL_IOT) NO reportan por semana: cargan un solo dato al mes, "
    "en una sola semana (2026: 5, 9, 14, 18, 23, 27, 31, 36, 40). Las demas "
    "cadenas son semanales. Si preguntan por una semana de esas cadenas, no "
    "digas solo 'no hay datos': explica que esa cadena reporta por mes y da "
    "el mes que contiene esa semana. Y en un total POR SEMANA de varias "
    "cadenas, esas tres se amontonan en la semana de cierre de mes: dilo, o "
    "esa semana parece un pico de ventas que no existe.",
    "REGION: la region de una tienda (R1 a R9, y RM) ya viene en la columna "
    "[Region] de [RAW_DATA_B2B].dbo.[vw_ETL_Con_Modelo], sobre la venta "
    "oficial: filtra `[Region] = 'R4'` o agrupa por ella. NO la unas tu con "
    "CustomerStores: los nombres de tienda vienen escritos distinto en cada "
    "cadena (espacios de mas, prefijos de region de Telcel, numeros en "
    "Elektra y Sears) y la vista ya hace el cruce correcto de cada una "
    "(medido en agosto de 2026: AT&T, Coppel, Elektra, Movistar y Sears "
    "100%, Telcel 99.7%, Liverpool 98%). Lo que no casa sale con [Region] "
    "NULL y sigue contando en los totales.",
    "EL MODELO SIEMPRE SE AGRUPA POR SU NOMBRE HOMOLOGADO, nunca por el "
    "texto que escribe el retailer, y cada base tiene SU columna homologada:\n"
    "  - venta      -> [GTM BI Database].dbo.[UNICOS$].[HOMOLOGACION MODELO]\n"
    "  - precios    -> [Scrap].dbo.[Complete_Scrap].[approved_name]\n"
    "Las dos usan el mismo vocabulario ('HONOR MAGIC 6 PRO 512GB'), asi que "
    "es por ahi por donde se cruzan precio y venta. Medido: de los 183 "
    "modelos que Elektra vendio en agosto de 2026, 156 tienen precio en "
    "Scrap.\n"
    "CUIDADO con las columnas que dicen 'homologado' y no lo son: "
    "EKT_B2B.[Modelo Homologado] es la homologacion INTERNA de Elektra "
    "('Oppo A6', 'Samsung Galaxy A37') y con la oficial casa el 2.5% del "
    "volumen (4 250 de 167 845 unidades en agosto de 2026). No la uses para "
    "agrupar ni para unir: usa [Desc Sku] contra UNICOS$.[DESCRIPCION].",
    "PRECIOS: lo que cuesta un telefono en la tienda online no esta en las "
    "tablas de venta, esta en [Scrap].dbo.[Complete_Scrap] -- el "
    "web-scraping de los e-commerce, con una fila por SKU y fecha de "
    "consulta. Trae OCHO retailers, y dos de ellos no existen en las tablas "
    "de venta: Suburbia y Mercado Libre. Pide `describir_objeto` de "
    "Complete_Scrap antes de consultarla: [price] trae ceros y el modelo "
    "solo se agrupa bien por [approved_name].",
    "CAPA es la capacidad de venta de una tienda: el MAXIMO que esa tienda "
    "llego a vender en un periodo, contando TODAS las marcas (es de la tienda, "
    "no de Honor; solo es de una marca si la pregunta la nombra). No es una columna de ninguna tabla, se "
    "calcula: unidades por tienda y por mes, y de esos meses el MAX. Di "
    "siempre de que ventana salio ('el maximo mensual desde 2025'), porque el "
    "CAPA depende de cuanto historial mires. "
    "OJO con la tienda: la misma tienda puede estar escrita de varias formas. "
    "En Elektra el numero 9797 aparece como 'ELEKTRA.MX', 'ELEKTRA.COM' y "
    "'ELETRA.MX', y medido en 2026 el 73% del volumen (927 913 de 1 268 566 "
    "unidades) esta en numeros de tienda con mas de un nombre. Agrupar por "
    "[Store] parte una tienda en varias y su CAPA sale MAS BAJO de lo que es "
    "-- la tienda 6169 da 670 como 'EKT MEGA ZUMPANGO' y 908 como 'MEGA "
    "ZUMPANGO', y su CAPA real es 908. Ahi agrupa por el numero: "
    "LEFT([Store], CHARINDEX(' ', [Store]+' ')-1). "
    "En Coppel, AT&T, Movistar y Sears el nombre es estable (medido: 0.2% o "
    "menos del volumen en nombres repetidos) y [Store] sirve tal cual.",
    "SHARE es el porcentaje que una marca representa dentro de una tienda (o "
    "de un retailer, o de una region): unidades de la marca entre unidades "
    "TOTALES de esa tienda, en el mismo periodo. El denominador son TODAS las "
    "marcas, asi que sale de UNA sola consulta con un CASE, nunca de dos:\n"
    "  SUM(CASE WHEN [Brand]='HONOR' THEN [QTY] ELSE 0 END) * 100.0\n"
    "    / NULLIF(SUM([QTY]), 0) AS [Share %]\n"
    "NUNCA pongas `[Brand]='HONOR'` en el WHERE si vas a calcular share: te "
    "quedas sin denominador y todo da 100%. Medido en Elektra, agosto 2026: "
    "la tienda mas grande vendio 1 012 unidades, 56 de HONOR, share 5.5%.",
    "PROMOTORIA: NO esta en las tablas de venta. Se saca SOLO de "
    "[GTM BI Database].dbo.[HCReport] (una fila por tienda y SEMANA), y la "
    "regla del negocio es: una tienda TIENE PROMOTORIA si [FIXED PROMOTER] = 1 "
    "O [DOUBLE PROMOTER] = 1 (las dos, nunca solo una). Tiendas con "
    "promotoria = `COUNT(DISTINCT CASE WHEN h.[FIXED PROMOTER] = 1 OR "
    "h.[DOUBLE PROMOTER] = 1 THEN h.[ID] END)`. Si las DOS valen 1, esa "
    "tienda tiene DOS promotores: 'cuantos promotores' son personas, no "
    "tiendas, y van con `SUM(h.[FIXED PROMOTER] + h.[DOUBLE PROMOTER])` "
    "(W35 de 2026: 1 323 tiendas con promotoria, 1 439 promotores; "
    "COPPEL 676 tiendas, 757 promotores). AL CONTESTAR: los promotores "
    "estan en las tiendas CON promotoria, no en todas. COUNT(DISTINCT "
    "h.[ID]) sin el CASE son TODAS las tiendas de la cadena: si lo das, di "
    "'59 promotores en 59 tiendas, de las 92 tiendas de Sears', nunca "
    "'59 promotores distribuidos en 92 tiendas' (paso el 1/10/2026). "
    "'Como estaba el ano pasado' es la MISMA semana del ano anterior: "
    "Sears W38 de 2026 tiene 59 promotores y W38 de 2025 tenia 53, asi que "
    "la respuesta empieza con '59 contra 53, +6'; la serie del ano entero "
    "va despues, si sirve. Supervisor, "
    "merchandiser, trainer y demas son visitas, NO promotoria. NO uses "
    "V_HCReport_Informes ni HCReport_VW para esto: la primera deja fuera "
    "las tiendas que no estan en CustomerStores (Movistar entero) y la "
    "segunda no tiene cadena ni semana. "
    "SIEMPRE filtra UNA semana ([YEAR] y [Week]): cada semana trae todas "
    "las tiendas otra vez. Un MES se reporta con su ULTIMA SEMANA COMPLETA "
    "(2026: W05 enero, W09 febrero, W13 marzo, W17 abril, W22 mayo, W26 "
    "junio, W30 julio, W35 agosto, W39 septiembre, W44 octubre, W48 "
    "noviembre, W52 diciembre; 2025 cambia en W04 enero, W08 febrero y W43 "
    "octubre, el resto igual); si esa semana aun no esta cargada, o no "
    "dicen periodo, usa la semana mas reciente que haya y dilo. "
    "HCReport NO tiene columna de cadena: sale de CustomerStores por [ID] y, "
    "para las tiendas que no estan ahi (Movistar, algunas de AT&T y "
    "Telcel), de la primera palabra de [NAME]. Por cadena, asi:\n"
    "  SELECT UPPER(COALESCE(cs.[RetailName], LEFT(h.[NAME], CHARINDEX(' ', "
    "h.[NAME] + ' ') - 1))) AS [Cadena],\n"
    "    COUNT(DISTINCT CASE WHEN h.[FIXED PROMOTER] = 1 OR h.[DOUBLE "
    "PROMOTER] = 1 THEN h.[ID] END) AS [Tiendas con promotoria]\n"
    "  FROM [GTM BI Database].dbo.[HCReport] h\n"
    "  LEFT JOIN (SELECT DISTINCT [ID], [RetailName], [Region] FROM "
    "[GTM BI Database].dbo.[CustomerStores]) cs ON cs.[ID] = h.[ID]\n"
    "  WHERE h.[YEAR] = 2026 AND h.[Week] = 'W35'\n"
    "  GROUP BY UPPER(COALESCE(cs.[RetailName], LEFT(h.[NAME], CHARINDEX(' ', "
    "h.[NAME] + ' ') - 1)))\n"
    "Para una sola cadena, esa misma expresion de [Cadena] va en el WHERE "
    "(`= 'COPPEL'`, 'TELCEL', 'LIVERPOOL', 'AT&T', 'ELEKTRA', 'SEARS', "
    "'MOVISTAR'). La region sale de cs.[Region]. Medido en W35 de 2026: "
    "1 323 tiendas con promotoria -- COPPEL 676, TELCEL 297, LIVERPOOL 107, "
    "AT&T 83, ELEKTRA 68, SEARS 60, MOVISTAR 32. "
    "Para cruzar promotoria con VENTA ('las tiendas con promotor venden mas "
    "Honor?'), lleva el [ID] de HCReport a CustomerStores.[Coppel Name] y "
    "une ese nombre con el [Store] de la vista de venta del retailer. Eso casa en AT&T (1732 de 1732 tiendas de "
    "2026), Coppel (1792/1800) y Liverpool (193/204); en Elektra casa la "
    "mitad (1253/2307) y en Telcel y Sears NO casa nada (0 de 675 y 0 de "
    "91), asi que ahi no hay cruce por nombre y no hay que forzarlo.",
    "Si una consulta devuelve cero filas, NO concluyas que no hay datos: casi "
    "siempre el filtro esta mal. Revisa con `valores_distintos` la columna que "
    "filtraste antes de responderle al usuario que no hay informacion. Esa "
    "comprobacion va SIEMPRE por `valores_distintos`, nunca por un SELECT "
    "DISTINCT en `consultar_sql`: eso generaria un reporte que pisa el bueno.",
]

# Notas de un objeto concreto, que salen cuando el agente lo consulta.
NOTAS_POR_OBJETO = {
    "vw_ETL_Con_Modelo": (
        "La venta OFICIAL (las nueve vistas vw_ETL_*, las de Power BI) con el "
        "modelo ya puesto: [Modelo] es [HOMOLOGACION MODELO] de UNICOS$ y "
        "[Clasificacion] su [CLASSIFICATION]. Usala para TODO lo que sea por "
        "modelo o por tipo de producto ('top de modelos', 'solo celulares', "
        "'cuantos Magic 8 Lite'). Filtra la cadena por [Retail] como en la "
        "consolidada. Trae ademas [Region] (R1 a R9, RM) de la tienda, del "
        "catalogo CustomerStores. Suma exactamente lo que las vistas; lo que "
        "no casa sale en NULL y sigue contando. No la unas con UNICOS$ ni con "
        "CustomerStores: ya esta unida, con el cruce correcto de cada cadena."
    ),
    "UNICOS$": (
        "PARA RESPONDER POR MODELO NO LA UNAS TU: usa vw_ETL_Con_Modelo, que "
        "ya trae [Modelo] y [Clasificacion] sobre la venta oficial. Lo que "
        "sigue es como esta hecha esta tabla.\n"
        "LA tabla de homologacion: convierte la descripcion que usa cada "
        "retailer en un nombre de modelo unico, y toda agrupacion por modelo "
        "pasa por aqui. Se agrupa por [HOMOLOGACION MODELO].\n"
        "Tiene DOS columnas para unir, y cual usar depende del otro lado:\n"
        "- [DESCRIPCION] es la descripcion tal cual la escribe la cadena, y "
        "casa con la TABLA CRUDA del retailer (COPPEL_B2B [Modelo], EKT_B2B "
        "[Desc Sku], TELCEL_B2B [Descripción], LIVERPOOL_B2B$ [Material "
        "Description], ATT_B2B/MVS_B2B/SEARS_B2B [Model]). Es la ruta que "
        "funciona para todos: medido, pierde 0% en Telcel, 0.7% en Elektra, "
        "2.5% en Coppel.\n"
        "- [MATERIAL DESCRIPTION FINAL] es marca + descripcion, y casa al "
        "100% con [Material_Description] de vw_ETL_COPPEL y "
        "vw_ETL_COPPEL_IOT, que son las unicas vistas que lo arman como "
        "CONCAT(Marca, Modelo). Contra las otras vw_ETL_* no casa nada.\n"
        "[MATERIAL DESCRIPTION FINAL] es PREFIJO + [DESCRIPCION], y el "
        "prefijo cambia por cadena. Por eso no es una columna que se pueda "
        "usar igual en todas: en unas hay que armar la concatenacion del "
        "lado de la venta, y en otras no aporta nada porque es identica a "
        "[DESCRIPCION]. Cuantas filas del catalogo tienen las dos columnas "
        "iguales:\n"
        "  TELCEL     8 947 de 8 947 (sin prefijo, son la misma columna)\n"
        "  ELEKTRA    3 364 de 3 393\n"
        "  LIVERPOOL  6 227 de 12 489 (la otra mitad lleva delante el "
        "codigo de articulo: '1200272087 ABS Nebula Station...')\n"
        "  COPPEL        11 de 3 076 (el prefijo es la MARCA)\n"
        "  SEARS          0 de 455 (el prefijo es la palabra 'Sears')\n"
        "  AT&T CACS      6 de 243 y AT&T DEALERS 5 de 211 (el prefijo es "
        "el nombre del retail: 'AT&T CACS APPLE IPHONE 17 PRO MAX 256GB')\n"
        "Regla practica: donde el prefijo existe y se puede reconstruir "
        "(Coppel, Sears) la FINAL es la llave; donde las dos columnas son "
        "casi la misma (Telcel, Elektra, Liverpool) [DESCRIPCION] es la "
        "llave, y ademas es la que cubre la mitad del catalogo de Liverpool "
        "que lleva codigo. En AT&T el prefijo es el retail, y como la tabla "
        "cruda mezcla CACS y DEALERS habria que adivinar cual poner: con "
        "'AT&T CACS ' casa 89.3% y con 'AT&T DEALERS ' 86.2%, contra 89.0% "
        "por [DESCRIPCION], asi que no compensa.\n"
        "  - COPPEL: es marca + descripcion ('ACER ANV15-41-R4WJ'). Contra "
        "COPPEL_B2B la llave se arma CONCAT([Marca], ' ', [Modelo]), y asi "
        "casa el 100.0% del mes; con [Modelo] contra [DESCRIPCION] casa el "
        "97.8%.\n"
        "  - SEARS: es la palabra 'Sears' + descripcion ('Sears APPLE "
        "IPHONE 11 64GB'). Contra SEARS_B2B, CONCAT('Sears ', [Model]). "
        "Casa el 100.0%, igual que por [DESCRIPCION].\n"
        "  - AT&T: ahi la final casa 0%. Usa [Model] contra [DESCRIPCION] "
        "(89.0%).\n"
        "  - ELEKTRA, TELCEL y LIVERPOOL: las dos columnas casan al 100%; "
        "[DESCRIPCION] es la ruta por defecto.\n"
        "(Todo medido sobre las tablas crudas de agosto de 2026, en "
        "unidades.)\n"
        "[Retail] va EN MAYUSCULAS y no como en las ventas: COPPEL, ELEKTRA, "
        "TELCEL, LIVERPOOL, SEARS, 'AT&T DEALERS', 'AT&T CACS', "
        "'MOVISTAR CACS', 'MOVISTAR CALL CENTER'.\n"
        "[CLASSIFICATION] separa SMARTPHONE de TABLET, LAPTOP, COMPUTER, "
        "SMARTWATCH, AUDIO y ACCESORIO: es la forma limpia de responder "
        "'solo celulares' sin adivinar por el nombre."
    ),
    "EKT_B2B": (
        "Solo ventas de Elektra; no tiene columna [Retail]. OJO con las fechas: "
        "[Month] es texto con la abreviatura inglesa del mes ('Mar', 'Nov'), "
        "no un numero, asi que `[Month] = 3` falla al convertir. Para acotar un "
        "mes usa el rango de [Day], que si es datetime.\n"
        "Y OJO con [Modelo Homologado]: es la homologacion INTERNA de "
        "Elektra ('Oppo A6', 'Samsung Galaxy A37'), NO la del negocio. "
        "Contra UNICOS$.[HOMOLOGACION MODELO] casa el 2.5% del volumen "
        "(4 250 de 167 845 unidades en agosto de 2026). Para agrupar por "
        "modelo, une [Desc Sku] con UNICOS$.[DESCRIPCION] filtrando "
        "u.[Retail] = 'ELEKTRA'."
    ),
    "ETL_Consolidado_B2B": (
        "Se escribe [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B]; no esta en "
        "[GTM BI Database]. "
        "NO tiene columna [Model] ni [Modelo]: el modelo del telefono vive "
        "dentro de [Material_Description] (que es el SKU mas la descripcion, "
        "p. ej. 'N.HMG8LTNBDLCH AT&T Dealers HONOR MAGIC8 LITE 512GB 5G'). "
        "Si te piden agrupar o mostrar 'el modelo', esa es la columna. "
        "[Brand] es el fabricante (HONOR, SAMSUNG), no el modelo: no la uses "
        "como reemplazo. "
        "LA fuente oficial de venta B2B: es la que usa el negocio, asi que sus "
        "numeros son los buenos. Union de las nueve vistas vw_ETL_*, una por "
        "retailer, ya normalizadas. [Year], [Month] y [WEEK] son enteros (no "
        "hay [Day]: para un dia se filtra [Date]); [Store] es texto. "
        "Dos cosas que la separan de las tablas crudas: "
        "(1) cada vw_ETL_* filtra la cantidad con `>= 1`, asi que quedan fuera "
        "las devoluciones y ajustes (QTY negativo) y las filas en cero; "
        "ademas vw_ETL_ATT_Postpaid se limita a ciertos canales y "
        "vw_ETL_Movistar_CAC solo trae 'Propio MX'. "
        "(2) viene AGREGADA con GROUP BY tienda/marca/SKU/carrier/dia y "
        "SUM(cantidad), o sea una fila por combinacion y no una por "
        "movimiento. Por eso COUNT(*) aqui NO es el numero de ventas: para "
        "unidades siempre SUM([QTY]). "
        "Reconciliacion verificada en Elektra marzo 2026: 134 049 filas "
        "crudas en EKT_B2B, menos 246 que no pasan el `>= 1` (233 negativas y "
        "13 en cero), menos 263 que el GROUP BY fusiona, dan las 133 540 de la "
        "vista; en unidades, 144 190 crudas + 250 de las negativas = 144 440."
    ),
    "HCReport": (
        "LA fuente de PROMOTORIA: una fila por tienda y semana. Una tienda "
        "TIENE PROMOTORIA si [FIXED PROMOTER] = 1 O [DOUBLE PROMOTER] = 1 -- "
        "regla del negocio, siempre las dos columnas juntas. Valen 0 o 1 "
        "(nunca NULL); el doble va casi siempre con el fijo, pero hay tiendas "
        "con solo el doble (47 filas en 2026), asi que filtrar solo "
        "[FIXED PROMOTER] = 1 se las salta. Si las dos valen 1 son DOS "
        "promotores en esa tienda: promotores (personas) = "
        "SUM([FIXED PROMOTER] + [DOUBLE PROMOTER]). [Supervisor's visit], "
        "[City Manager visit], [Merchandiser], [Trainer], [FIELD REP] y "
        "[SUPERVISOR MIX] son visitas y apoyos, NO promotoria.\n"
        "Las fechas NO son fechas: [Week] es texto 'W01'..'W52' (con el cero, "
        "asi que `[Week] = 'W5'` no encuentra nada) y [YEAR] es un numero "
        "decimal (2026.0). Filtra SIEMPRE [YEAR] y [Week]: cada semana trae "
        "las ~8 330 tiendas otra vez, y sin semana un conteo junta todas. "
        "Para ordenar semanas usa [YEAR] y [Week], que siendo 'Wnn' ordena "
        "bien como texto.\n"
        "[ID] es la tienda (APSMX222602) y casa con [ID] de CustomerStores, "
        "que es de donde salen la cadena ([RetailName]), el nombre y la "
        "region. Se une por ID, NUNCA por nombre. 3 801 de las 8 330 tiendas "
        "no estan en CustomerStores (Bodega, Walmart, Movistar, Suburbia...): "
        "para ellas la cadena es la primera palabra de [NAME] ('MOVISTAR "
        "PLAZA ARAGON'). Por eso la cadena es `UPPER(COALESCE(cs.[RetailName], "
        "LEFT(h.[NAME], CHARINDEX(' ', h.[NAME] + ' ') - 1)))` con un LEFT "
        "JOIN; con INNER JOIN se pierde Movistar entero (32 tiendas con "
        "promotoria en W35 de 2026)."
    ),
    "HCReport_VW": (
        "NO la uses para promotoria: usa [HCReport], que tiene la misma "
        "informacion con la cadena, la semana y la tienda separadas. Esta "
        "vista solo trae [FKEY_HR] (CONCAT(ano, semana, id) -- "
        "'2026W10APSMX222602'), [PromtsQTY] (fijo + doble) y [AsigdenPromts] "
        "('No Promotors', 'One Promotor', 'Two Promotors')."
    ),
    "V_HCReport_Informes": (
        "NO la uses para promotoria: usa [HCReport]. Esta vista es un INNER "
        "JOIN con CustomerStores y deja fuera las tiendas que no estan ahi: "
        "en agosto de 2026 contaba 1 289 tiendas con promotor cuando HCReport "
        "tiene 1 323, y Movistar no aparece en absoluto. Ademas su mapa de "
        "semana->mes esta escrito a mano y solo cubre 2025 y 2026.\n"
        "Por si alguien la nombra: [Period] es texto en ingles "
        "('2026-August'), [status_of_promoter] es un numero (0, 1 o 2: fijo "
        "mas doble) y la tienda es [Coppel Name]; no tiene cadena ni ID."
    ),
    "CustomerStores": (
        "El catalogo de tiendas: [ID] (APSMX222602), [Coppel Name] (el nombre "
        "con el que aparecen en casi todo), [HONOR NAME], [Region] (R1 a R9 "
        "y RM), "
        "[StoreLevel] y [RetailName]. Es el puente entre la promotoria (que "
        "va por [ID]) y las ventas (que van por nombre de tienda).\n"
        "Cruzar ventas con este catalogo por nombre funciona o no segun de "
        "DONDE venga la venta. Desde la vista consolidada, medido con las "
        "tiendas de 2026 contra [Coppel Name]: AT&T 1732/1732, Coppel "
        "1792/1800, Liverpool 193/204, Elektra 1253/2307, Telcel 0/675 y "
        "Sears 0/91.\n"
        "Desde la TABLA CRUDA de cada cadena casa mucho mejor, y ahi si se "
        "puede cortar por region. Tiendas de agosto de 2026:\n"
        "  COPPEL_B2B.[Tienda]                 -> [Coppel Name]  1728/1735\n"
        "  TELCEL_B2B.[Venta]                  -> [Coppel Name]   649/653\n"
        "  ATT_B2B.[Nombre de la Organización] -> [Coppel Name]  1674/1682\n"
        "  LIVERPOOL_B2B$.[STORE]              -> [Coppel Name]   189/200\n"
        "  EKT_B2B.[Tienda #]                  -> numero en [HONOR NAME]  1258/1269\n"
        "  SEARS_B2B.[#POS]                    -> numero en [HONOR NAME]    90/91\n"
        "El numero de [HONOR NAME] es el que va entre parentesis al final "
        "('ELEKTRA MERIDA 2 (8649)'). Se saca asi, y el CASE evita el error "
        "de RIGHT() cuando no hay parentesis:\n"
        "  CASE WHEN CHARINDEX('(', REVERSE([HONOR NAME])) > 1 THEN\n"
        "    TRY_CONVERT(int, REPLACE(RIGHT([HONOR NAME],\n"
        "      CHARINDEX('(', REVERSE([HONOR NAME])) - 1), ')', '')) END"
    ),
    "Complete_Scrap": (
        "Precios y fichas de e-commerce: lo que cada tienda online publicaba "
        "de cada SKU el dia de la consulta ([consultation_date]). Va de 2025 "
        "a la fecha de hoy y trae ocho retailers -- Coppel, Telcel CACS, "
        "Sears, Liverpool, Elektra, Movistar CACS y dos que NO estan en las "
        "tablas de venta, Suburbia y Mercado Libre.\n"
        "EL MODELO SE AGRUPA POR [approved_name], que es la homologacion de "
        "esta base. [model] es el codigo del sitio del retailer ('BVL-N49', "
        "'CLK-NX3'): agrupar por el parte un mismo telefono en varias filas "
        "y ademas repite codigos entre productos. De 2 286 749 filas, solo 3 "
        "no traen [approved_name]; un mismo [approved_name] junta hasta 4 "
        "codigos distintos y hasta 7 retailers. "
        "[approved_name_without_capacity] es lo mismo sin los GB, para "
        "comparar familias.\n"
        "[approved_name] usa el mismo vocabulario que "
        "UNICOS$.[HOMOLOGACION MODELO], asi que es la llave para cruzar "
        "precio con venta.\n"
        "DOS TRAMPAS medidas:\n"
        "  1. [price] es la unica columna de precio que es numero "
        "([lauch_price], [original_price] y [discount_price] son texto), y "
        "trae CEROS. Filtra `[price] > 0` o el promedio sale hundido.\n"
        "  2. Hay varias filas del mismo modelo, tienda y fecha. Para 'el "
        "precio de hoy' toma el MAX([consultation_date]) y agrega; contar "
        "filas no cuenta productos.\n"
        "[store] no siempre es una tienda fisica: en los CACs es el nombre "
        "del retailer. Es la UNICA tabla de la base Scrap: las vistas por "
        "retailer (Complete_Scrap_COPPEL, Complete_Scrap_LIVERPOOL) se "
        "quitaron del servidor; para un retailer filtra [retail] aqui."
    ),
    "Prices_Weekly_VW": (
        "El PRECIO DE VENTA SEMANAL por producto y cadena: [Fecha], [Year], "
        "[Month], [Week], [Material Description], [Precio Venta], [Retail], "
        "[Carrier], [Key]. Medido el 1 de octubre de 2026: Telcel CAC, "
        "Liverpool y Coppel desde enero de 2024; Elektra, AT&T, Sears y "
        "Movistar desde octubre-noviembre de 2025.\n"
        "[Material Description] es el TEXTO DE CADA CADENA, no el modelo "
        "homologado ('Oppo reno 13 f amoled 6.6 pulgadas', 'AT&T Dealers Oppo "
        "Reno 13', 'SEARS Apple Iphone 17 Pro Max 512GB'). Para agrupar por "
        "modelo une con UNICOS$ por [MATERIAL DESCRIPTION FINAL], que casa el "
        "99-100% en todas las cadenas (por [DESCRIPCION] casa entre 0% y 47% "
        "salvo Telcel y Elektra). Colapsa UNICOS$ antes de unir para no "
        "duplicar: `JOIN (SELECT [MATERIAL DESCRIPTION FINAL] AS d, "
        "MIN([HOMOLOGACION MODELO]) AS modelo FROM [GTM BI Database].dbo.[UNICOS$] "
        "GROUP BY [MATERIAL DESCRIPTION FINAL]) u ON u.d = "
        "LTRIM(RTRIM(p.[Material Description]))`.\n"
        "DOS TRAMPAS: (1) 42 723 filas traen [Precio Venta] en cero o negativo "
        "y 7 269 por debajo de $500: filtra `[Precio Venta] > 0` siempre, y "
        "sospecha de un precio de celular por debajo de $500. (2) [Key] NO es "
        "unica: el mismo producto y semana sale una vez por cadena y carrier "
        "(Liverpool Online y Offline, por ejemplo), con precios distintos. "
        "Agrupa siempre por [Retail] y di que precio das (minimo, maximo o "
        "promedio). [Key] tampoco casa con el [Key prices] de las ventas."
    ),
    "TELCEL_INV_B2B": (
        "INVENTARIO de Telcel por tienda y SKU, en [RAW_INV_B2B]: [SKU], "
        "[MODEL] (el texto de Telcel), [MODE] ('Inventario Universal', 'Equipo "
        "prepago', 'Equipo post-pago'), [BRAND], [AREA] (la region de Telcel, "
        "'RG03'), [STORE], [STOCK], [date] y [retail]. [retail] dice 'TELCEL "
        "CAC', pero NO son solo tiendas: la mayor parte del stock esta en "
        "almacenes (ALM CENTRAL WM, ALMACEN CENTRAL, ASIG CACS, CAD "
        "DISTRIBUIDOR) que surten a todos los canales.\n"
        "Para SEMANAS DE INVENTARIO o cobertura usa la herramienta "
        "`semanas_de_inventario`, no un SELECT: la cuenta esta definida por "
        "el negocio y cruza por SKU con la venta.\n"
        "Son FOTOS SEMANALES (39 fechas de enero a septiembre de 2026, casi "
        "todas en domingo). NUNCA sumes [STOCK] de varias fechas: eso cuenta "
        "el mismo telefono una vez por semana. 'El inventario' es el de UNA "
        "fecha, la mas reciente si no dicen otra: "
        "`WHERE [date] = (SELECT MAX([date]) FROM [RAW_INV_B2B].dbo.[TELCEL_INV_B2B])`. "
        "Medido: HONOR tenia 151 987 unidades en la foto del 27 de septiembre "
        "de 2026. OJO: el 30 y el 31 de agosto de 2026 son la MISMA foto "
        "repetida (mismo total, 2 578 805); para fin de agosto usa una sola."
    ),
    "CompetitorsAnalysis_VW_TELCEL": (
        "La MISMA venta de Telcel que vw_ETL_TELCEL_B2B, con otra forma: "
        "[Brand], [Store], [Model], [RETAIL], [Qty], [Date]. Medido: agosto "
        "de 2026 da 1 074 478 unidades en las dos. NO la sumes con "
        "vw_ETL_TELCEL_B2B ni con la consolidada: contarias Telcel dos veces. "
        "Para venta de Telcel usa la consolidada o vw_ETL_TELCEL_B2B."
    ),
    "TBL_DB_FOR_DOS": (
        "Su columna [Retail] solo contiene 'Coppel', 'Liverpool Offline' y "
        "'TELCEL CAC'. No incluye Elektra ni el resto de cadenas: para esas hay "
        "que ir a RAW_DATA_B2B."
    ),
}


# Lo que el bot NO debe consultar: tablas y vistas que existen, que por el
# nombre parecen buenas, y que dan otro numero, uno viejo, o no terminan.
# Revisado el 6/10/2026 contra la base, despues de que el negocio borro
# Coppel_test (traia las siete cadenas con nombre de una). Cada motivo dice
# que usar en su lugar, porque es lo que se le devuelve al modelo cuando la
# consulta se bloquea (ver agent.consultar_sql) y lo que lee en el indice.
_VENTA = "Para venta usa [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B] o la vista vw_ETL_* de la cadena."
_LENTA = (
    "No termina: un MAX de una columna paso de 5 minutos (medido el "
    "6/10/2026), asi que se comeria la pregunta entera. "
)
EVITAR = {
    # Dan otro numero.
    "B2BHonorGeneralReport": (
        "Da OTRO numero que la venta oficial: Honor en agosto de 2026 sale con "
        "Elektra 1 616 (son 8 383) y AT&T Cacs 1 912 (son 6 612), y no trae "
        "Coppel, Telcel, Sears ni Movistar. " + _VENTA
    ),
    "VW_DB_FOR_DOS": (
        "Mezcla venta e inventario en la misma columna [QTY] (las separa "
        "[DataType]: 'SO' e 'INV'): sumarla da Coppel agosto 2026 = 120 874 "
        "cuando la venta fue ~49 000. Y solo trae Coppel, Liverpool Offline y "
        "Telcel CAC. " + _VENTA + " Para inventario, INV_Retail o el "
        "inventario de la cadena."
    ),
    # Se quedaron viejas.
    "TBL_DB_FOR_DOS": "Se quedo el 24/03/2026 y solo trae tres cadenas. " + _VENTA,
    "TAB_DB_FOR_DOS": "Solo trae octubre a diciembre de 2025. " + _VENTA,
    "VW_DB_FOR_DOSV1": "Se quedo el 31/12/2025. " + _VENTA,
    "TEMP_SALES": "Tabla temporal, solo trae 2025. " + _VENTA,
    "INV_Telcel_B2B": (
        "Se quedo el 31/05/2026. El inventario de Telcel al dia es "
        "[RAW_INV_B2B].dbo.[TELCEL_INV_B2B] (y las semanas de inventario, la "
        "herramienta `semanas_de_inventario`)."
    ),
    "vw_INV_Telcel_B2B": (
        "Se quedo el 31/05/2026. El inventario de Telcel al dia es "
        "[RAW_INV_B2B].dbo.[TELCEL_INV_B2B]."
    ),
    # Sin anio: no se sabe de cuando es cada numero.
    "Coppel": "No tiene anio, solo [MONTH]: no se sabe de que anio es cada numero. " + _VENTA,
    "Coppel_2024": "No tiene anio, solo [MONTH]: no se sabe de que anio es cada numero. " + _VENTA,
    "Coppel_Monthly_Excel": "No tiene anio, solo [MesNum]. " + _VENTA,
    # Rotas en el servidor (binding errors: les falta una tabla de origen).
    "vw_Coppel_Monthly_Excel": "Esta rota en el servidor (le falta una tabla de origen). " + _VENTA,
    "vw_Elektra_Monthly_Excel": "Esta rota en el servidor (le falta una tabla de origen). " + _VENTA,
    "vw_Liverpool_Monthly_Base": "Esta rota en el servidor (le falta una tabla de origen). " + _VENTA,
    "vw_Liverpool_Monthly_Excel": "Esta rota en el servidor (le falta dbo.Retail_StoreMap). " + _VENTA,
    # No terminan a tiempo.
    "VW_COPPEL_MONTLY_SOandINV_LC": _LENTA + _VENTA,
    "VW_ELEKTRA_MONTLY_SOandINV_LC": _LENTA + _VENTA,
    "VW_LIVERPOOL_MONTLY_SOandINV_LC": _LENTA + _VENTA,
    "VW_RETAILERS_MONTH_SOandINV": _LENTA + "Ademas falla con 'Error converting data type varchar to float'. " + _VENTA,
    "VW_MVS_ATT_SEARS_MONTLY_SOandINV_LC": (
        "Tarda mas de 3 minutos y en las pruebas fallo 4 de 6 consultas. " + _VENTA
    ),
    "V_GTM_HonorReport_LGC": _LENTA + _VENTA,
    "VW_edicion": _LENTA + _VENTA,
    "Share_Retail_Price3000": _LENTA + _VENTA + " Para precios, Prices_Weekly_VW.",
    "CompetitorsAnalysis2024_VW": _LENTA + _VENTA,
    "CompetitorsAnalysis_Telcel_VW": _LENTA + "Para Telcel usa vw_ETL_TELCEL_B2B.",
    "TELCEL_CompetitorsAnalysis_VW_LGC": _LENTA + "Para Telcel usa vw_ETL_TELCEL_B2B.",
}


def motivo_para_evitar(nombre: str) -> str | None:
    """Por que no usar ese objeto, o None si se puede."""
    for clave, motivo in EVITAR.items():
        if clave.casefold() == nombre.casefold():
            return motivo
    return None


# La vista con el modelo no existe en el servidor (ver ventas_con_modelo.py):
# se agrega al catalogo para que el bot la vea en el indice y la pueda
# describir como a cualquier otra.
_VISTA_CON_MODELO = {
    "nombre": "vw_ETL_Con_Modelo",
    "tipo": "vista",
    "filas": None,
    "columnas": [
        {"nombre": n, "tipo": t} for n, t in (
            ("Retail", "varchar(20)"), ("Store", "nvarchar(511)"), ("Brand", "nvarchar(255)"),
            ("Material_Description", "nvarchar(532)"), ("Carrier", "varchar(13)"),
            ("Date", "datetime"), ("Year", "int"), ("Month", "int"), ("WEEK", "int"),
            ("QTY", "float"), ("Modelo", "nvarchar(255)"), ("Clasificacion", "varchar(50)"),
            ("Region", "nvarchar(255)"),
        )
    ],
}

# Las tablas crudas de venta: el detalle del Excel, no para responder.
_CRUDAS_DE_VENTA = {
    "COPPEL_B2B", "Coppel_IOT", "EKT_B2B", "TELCEL_B2B", "LIVERPOOL_B2B$",
    "Liverpool_IOT", "ATT_B2B", "SEARS_B2B", "MVS_B2B",
}


@lru_cache(maxsize=1)
def cargar_catalogo() -> dict:
    catalogo = json.loads(RUTA_CATALOGO.read_text(encoding="utf-8"))
    for base in catalogo["bases"]:
        if base["nombre"] == "RAW_DATA_B2B" and not any(
            o["nombre"] == _VISTA_CON_MODELO["nombre"] for o in base["objetos"]
        ):
            base["objetos"].append(_VISTA_CON_MODELO)
    return catalogo


def _corchetes(nombre: str) -> str:
    """Nombre delimitado, que es como siempre hay que escribirlo en el SQL.

    Los objetos reales de este servidor llevan espacios (`GTM BI Database`,
    `Store Name`), sufijos `$` heredados de las hojas de Excel importadas
    (`UNICOS$`) y palabras reservadas de T-SQL como nombre de columna (`KEY`,
    `DESC`): sin corchetes ninguno de esos parsea, y `KEY` ademas lo rechaza
    la lista blanca de `agent._evaluar_sql` por reservada.
    """
    return "[" + nombre.replace("]", "]]") + "]"


def _filas_humanas(filas: int | None) -> str:
    if filas is None:
        return ""
    if filas >= 1_000_000:
        return f"{filas / 1_000_000:.1f}M filas"
    if filas >= 1_000:
        return f"{filas // 1_000}k filas"
    return f"{filas} filas"


def _resumen(obj: dict) -> str:
    partes = [p for p in (_filas_humanas(obj["filas"]), f"{len(obj['columnas'])} col") if p]
    # Una vista que el servidor no puede resolver se marca aqui, en el indice
    # que el modelo lee siempre: si solo se enterara al consultarla, gastaria
    # un paso en descubrirlo (ver actualizar_catalogo.py).
    if obj.get("rota"):
        partes.append("ROTA, no usar")
    elif obj["nombre"] in EVITAR:
        partes.append("NO USAR")
    elif obj["nombre"] in _CRUDAS_DE_VENTA:
        partes.append("cruda: solo para el Excel, no para responder")
    elif obj["nombre"] == _VISTA_CON_MODELO["nombre"]:
        partes.append("venta oficial con [Modelo], [Clasificacion] y [Region]")
    return f"{obj['nombre']} ({', '.join(partes)})"


@lru_cache(maxsize=1)
def indice() -> str:
    """Indice compacto de todo el servidor, para inyectar en el system prompt.

    Van los nombres y el volumen, no las columnas: las 893 columnas del
    catalogo completo son demasiado para repetirlas en cada mensaje, y para
    eso esta la herramienta `describir_objeto`.
    """
    lineas = ["COMO LEER ESTE SERVIDOR:"]
    lineas += [f"- {nota}" for nota in NOTAS_GENERALES]

    for base in cargar_catalogo()["bases"]:
        tablas = [o for o in base["objetos"] if o["tipo"] == "tabla"]
        vistas = [o for o in base["objetos"] if o["tipo"] == "vista"]
        lineas.append(
            f"\n### {_corchetes(base['nombre'])} "
            f"- {len(tablas)} tablas, {len(vistas)} vistas"
        )
        lineas.append(base["descripcion"])
        if tablas:
            lineas.append("  Tablas: " + " | ".join(_resumen(o) for o in tablas))
        if vistas:
            lineas.append("  Vistas: " + " | ".join(_resumen(o) for o in vistas))
    return "\n".join(lineas)


def buscar(nombre: str) -> list[tuple[str, dict]]:
    """Objetos cuyo nombre coincide, como (base, objeto).

    Acepta tanto `TBL_DB_FOR_DOS` como `GTM BI Database.dbo.TBL_DB_FOR_DOS`:
    se queda con el ultimo segmento y limpia corchetes, porque el modelo tiende
    a pasar el nombre ya calificado que leyo en el indice. La coincidencia es
    exacta salvo mayusculas; buscar por subcadena haria que `Coppel` arrastre
    las nueve tablas que lo contienen.
    """
    limpio = nombre.strip().split(".")[-1].strip().strip("[]").casefold()
    return [
        (base["nombre"], obj)
        for base in cargar_catalogo()["bases"]
        for obj in base["objetos"]
        if obj["nombre"].casefold() == limpio
    ]


def _ficha(base: str, obj: dict) -> str:
    cabecera = (
        f"{obj['tipo'].upper()} {_corchetes(base)}.dbo.{_corchetes(obj['nombre'])}"
    )
    if obj["filas"] is not None:
        cabecera += f" -- {obj['filas']:,} filas".replace(",", " ")
    columnas = "\n".join(
        f"  {_corchetes(c['nombre'])} {c['tipo']}" for c in obj["columnas"]
    )
    ficha = f"{cabecera}\n{columnas}"
    if obj.get("rota"):
        ficha += (
            "\nOJO: ESTA VISTA NO FUNCIONA EN EL SERVIDOR (se comprobo al "
            "actualizar el catalogo). Cualquier consulta a ella falla con: "
            f"{obj['rota']} -- No la uses; busca el dato en otra tabla o vista."
        )
    evitar = EVITAR.get(obj["nombre"])
    if evitar:
        ficha += f"\nOJO: NO USES ESTE OBJETO. {evitar}"
    nota = NOTAS_POR_OBJETO.get(obj["nombre"])
    if nota:
        # Despues de las columnas, no antes: es la ultima linea que el modelo
        # lee antes de escribir el SELECT.
        ficha += f"\nNOTA: {nota}"
    return ficha


@tool
def describir_objeto(nombre: str) -> str:
    """Devuelve las columnas y tipos de una tabla o vista del servidor.

    Usa esta herramienta ANTES de escribir un SELECT, para confirmar los
    nombres exactos de las columnas. Recibe el nombre de la tabla o vista tal
    como aparece en el catalogo, por ejemplo "TBL_DB_FOR_DOS".
    """
    encontrados = buscar(nombre)
    if not encontrados:
        return (
            f"No hay ninguna tabla o vista llamada {nombre!r} en el catalogo. "
            "Revisa el indice de bases de datos del prompt para ver los nombres "
            "disponibles."
        )
    return "\n\n".join(_ficha(base, obj) for base, obj in encontrados)
