"""Un resumen de las ventas en un archivo local, para no esperar al servidor.

El servidor SQL es lento para lo que mas se pregunta: el share de Honor por
cadena en un mes recorre todas las marcas de todas las cadenas y tarda ~170s
(medido el 2 de octubre de 2026), y una pregunta que hace dos de esas pasa de
los cinco minutos. Con tres personas a la vez, cada una tarda 1.4-1.7 veces
mas.

Casi todas esas preguntas solo necesitan unidades por cadena, marca, modelo,
carrier, ano, mes y semana. Eso se baja de las nueve vistas vw_ETL_*
agrupado, a un SQLite junto al bot, y se refresca cada pocas horas. Una
consulta que solo usa esas columnas se contesta aqui en un segundo; la que
pide tienda, fecha exacta, precios u otra tabla sigue yendo al servidor,
igual que antes. Si algo de aqui falla, tambien: el resumen es un atajo, no
un requisito.

El SQL que escribe el modelo es de SQL Server. Lo que se traduce es poco y
mecanico (TOP -> LIMIT, ISNULL, LEN, N'...'); lo que podria dar OTRO numero
sin dar error no se traduce, se manda al servidor (ver `_NO_SE_TRADUCE`).

Desde el 12/10/2026 (pedido del usuario) tambien va, COPIADA FILA POR FILA
y no agrupada, la promotoria ([HCReport]) con la tabla con la que el bot
siempre la cruza, [CustomerStores] (region y cadena de la tienda): 23 de 24
consultas de promotoria en la bitacora las unen. Como son las filas de
verdad, en ellas si se puede contar, promediar y tomar minimos (ver
`_NO_SE_TRADUCE_EN_COPIAS`). Bajarlas tarda ~12 s.

Los precios ([Prices], con [UNICOS$]) se probaron aqui el mismo dia y se
quitaron: daban lo mismo que el servidor, pero el servidor ya los contesta
en 0.2-1.5 s y el precio actual por modelo tardaba aqui 4-8 s; ademas
pesaban ~460 MB y 77 s de construccion. Siguen yendo al servidor.

El resumen se BORRA cada vez que arranca el bot (ver mantener_al_dia): un
resumen de ayer contestaria con cifras de ayer mientras se construye el de
hoy; sin el, esos minutos todo va al servidor.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pandas as pd

from catalogo import _VISTAS_DE_LA_CONSOLIDADA, _VISTAS_PARTIDAS, _reemplazar_objeto

logger = logging.getLogger("sql_agent.resumen_local")

ARCHIVO = Path(__file__).resolve().parent / "datos" / "resumen_ventas.sqlite"

# Cada cuanto se vuelve a bajar. Construirlo tarda ~7 minutos y carga al
# servidor; el bot corre de dia, asi que con esto se refresca un par de veces
# por jornada ademas de al arrancar.
HORAS_ENTRE_REFRESCOS = float(os.getenv("RESUMEN_CADA_HORAS", "3"))
# Mas viejo que esto no se usa: la consulta va al servidor. Visto el 9/10/2026:
# al arrancar, mientras se reconstruye (~7 min), se contestaba con el resumen
# de hace dos dias -- el total de septiembre salio de ahi y el top de modelos,
# en vivo, con datos mas nuevos: dos respuestas que no cuadraban entre si. En
# produccion pasa cada manana, con el resumen de la tarde anterior.
MAX_HORAS_DE_ATRASO = HORAS_ENTRE_REFRESCOS * 2

VISTAS = _VISTAS_DE_LA_CONSOLIDADA
CONSOLIDADA = "ETL_Consolidado_B2B"

# Las tablas de [GTM BI Database] que se copian tal cual, fila por fila.
COPIAS = ("HCReport", "CustomerStores")
# Columnas que NO se copian: las llaves de texto de Prices (hasta 4000
# caracteres) pesan 600 MB y el bot no contesta con ellas. Si una consulta
# las nombra, va al servidor.
_SIN_COPIAR = {"Prices": ("KEY PRICES", "key2")}
_COLUMNA_QUE_NO_SE_COPIO = re.compile(r"\[\s*(KEY\s+PRICES|key2)\s*\]", re.IGNORECASE)
# Indices: por donde filtra el bot (la semana de promotoria, la fecha y la
# cadena de un precio, la descripcion contra el catalogo).
# Medido el 12/10/2026: sin el de CustomerStores([ID]), la promotoria de
# Sears de un ano tardaba 190 s (cada fila de HCReport recorria el catalogo
# entero); con el, 0.05 s. Sin ANALYZE: con estadisticas, SQLite elegia un
# plan peor para los precios (20 s contra 7.8 s).
_INDICES = {
    "HCReport": (("YEAR", "Week"), ("ID",)),
    "CustomerStores": (("ID",), ("RetailName",)),
    "Prices": (("Día/Periodo",), ("Retail",), ("Material Description",)),
    "UNICOS$": (("DESCRIPCION",), ("MATERIAL DESCRIPTION FINAL",)),
}
# Lo maximo que puede tardar aqui una consulta sobre las copias antes de
# cortarla y mandarla al servidor. El servidor contesta promotoria y precios
# en 0.2-1.5 s (las ventas no: 100-400 s), y el optimizador de SQLite es mas
# simple: el precio actual de un modelo por cadena tardaba aqui 7.8 s contra
# 1.2 s alla. Con el tope, lo peor es perder estos segundos, no minutos.
MAX_SEGUNDOS_EN_COPIAS = 5
_NOMBRA_UNA_COPIA = re.compile(
    "|".join(rf"\[{re.escape(c)}\]" for c in COPIAS), re.IGNORECASE
)
_TIPOS_DE_TEXTO = ("char", "varchar", "nchar", "nvarchar", "text", "ntext")
_TIPOS_ENTEROS = ("int", "bigint", "smallint", "tinyint", "bit")
_TIPOS_DE_FECHA = ("date", "datetime", "datetime2", "smalldatetime")
_TEXTO = ("Retail", "Brand", "Material_Description", "Carrier")
_NUMERO = ("Year", "Month", "WEEK")
COLUMNAS = _TEXTO + _NUMERO + ("QTY",)

# Columnas de las vistas que el resumen NO tiene: si la consulta las nombra,
# va al servidor.
_COLUMNA_QUE_NO_ESTA = re.compile(
    r"(?<![\w\[])\[?(Store|Date|Day|Key\s+prices)\]?(?![\w])", re.IGNORECASE
)

# YEAR([Date]) y MONTH([Date]) son [Year] y [Month], que el resumen si tiene.
# Medido el 12/10/2026 en las nueve vistas (30 millones de filas): [Year] y
# [Month] coinciden SIEMPRE con el ano y el mes de [Date], y no hay [Date]
# nula. Antes, siete consultas lentas de la bitacora iban al servidor solo
# por pedir el ano o el mes asi (hasta 52 s).
_PARTE_DE_LA_FECHA = re.compile(
    r"\b(?:(YEAR|MONTH)\s*\(\s*((?:\w+\.)?)\[?Date\]?\s*\)"
    r"|DATEPART\s*\(\s*(year|yyyy|yy|month|mm|m)\s*,\s*((?:\w+\.)?)\[?Date\]?\s*\))",
    re.IGNORECASE,
)


def _sin_partes_de_la_fecha(texto: str) -> str:
    def cambiar(m: re.Match) -> str:
        funcion = (m.group(1) or m.group(3)).lower()
        alias = m.group(2) or m.group(4) or ""
        return f"{alias}[{'Year' if funcion.startswith('y') else 'Month'}]"
    return _PARTE_DE_LA_FECHA.sub(cambiar, texto)


# Lo que no se traduce porque daria otro numero o porque SQLite no lo tiene.
# Una fila del resumen es un GRUPO, no una venta: contar filas, promediar o
# tomar el minimo de [QTY] ya no significa lo mismo. Y `+` entre textos en
# SQLite es una SUMA: `CAST([Year] AS VARCHAR) + '-'` daria un numero.
_NO_SE_TRADUCE = re.compile(
    r"COUNT\s*\(\s*(\*|1\s*\)|\[?QTY)"
    r"|\b(AVG|STDEV\w*|VAR\w*|PERCENTILE_\w+|CHECKSUM_AGG)\s*\("
    r"|\b(MIN|MAX)\s*\([^)]*QTY"
    r"|'\s*\+|\+\s*N?'"
    r"|\bAS\s+N?(VAR)?CHAR\b"
    r"|\b(DATEPART|DATENAME|DATEFROMPARTS|DATEADD|DATEDIFF|EOMONTH|GETDATE|SYSDATETIME"
    r"|FORMAT|CONVERT|TRY_CONVERT|TRY_CAST|TRY_PARSE|PARSE|STRING_AGG|CONCAT_WS"
    r"|CHARINDEX|PATINDEX|STUFF|LEFT|RIGHT)\s*\("
    r"|\b(OFFSET|FETCH|PERCENT|ROLLUP|CUBE|GROUPING\s+SETS|PIVOT|UNPIVOT|NOLOCK|APPLY|INTO|DECLARE)\b"
    r"|[@#]",
    re.IGNORECASE,
)

# En las copias las filas son las de verdad: COUNT, AVG, MIN y MAX dan lo
# mismo que en el servidor. Lo que sigue sin traducirse es lo que SQLite no
# tiene o hace distinto: fechas y conversiones de SQL Server, concatenar
# texto con `+` (en SQLite suma) y los CAST a texto, fecha o decimal.
# LEFT, RIGHT, CHARINDEX y el `+` de texto pegado a una comilla SI se
# traducen (ver `_traducir_funciones`): la promotoria real saca la cadena del
# nombre de la tienda con LEFT(h.[NAME], CHARINDEX(' ', h.[NAME] + ' ') - 1).
_NO_SE_TRADUCE_EN_COPIAS = re.compile(
    r"\b(STDEV\w*|VAR\w*|PERCENTILE_\w+|CHECKSUM_AGG)\s*\("
    r"|\bAS\s+(N?(VAR)?CHAR|DATE|DATETIME2?|SMALLDATETIME|TIME|DECIMAL|NUMERIC|MONEY)\b"
    r"|\b(DATEPART|DATENAME|DATEFROMPARTS|DATEADD|DATEDIFF|EOMONTH|GETDATE|SYSDATETIME"
    r"|FORMAT|CONVERT|TRY_CONVERT|TRY_CAST|TRY_PARSE|PARSE|STRING_AGG|CONCAT_WS"
    r"|PATINDEX|STUFF)\s*\("
    r"|\b(OFFSET|FETCH|PERCENT|ROLLUP|CUBE|GROUPING\s+SETS|PIVOT|UNPIVOT|APPLY|INTO|DECLARE)\b"
    r"|[@#]",
    re.IGNORECASE,
)
_SIN_NOLOCK = re.compile(r"\bWITH\s*\(\s*NOLOCK\s*\)|\(\s*NOLOCK\s*\)", re.IGNORECASE)
_PREFIJO_GTM = re.compile(
    r"\[GTM BI Database\]\s*\.\s*\[?dbo\]?\s*\.\s*", re.IGNORECASE
)
_FUNCION_A_TRADUCIR = re.compile(r"(?<![\w\[.])(LEFT|RIGHT|CHARINDEX)\s*\(", re.IGNORECASE)
_MAS_DE_TEXTO = re.compile(r"(?<=')\s*\+(?!\+)|\+(?=\s*N?')")


def _argumentos(texto: str, abre: int) -> tuple[list[str], int] | None:
    """Los argumentos de la llamada cuyo parentesis abre en `abre`, y donde
    cierra. Separa por las comas de primer nivel, respetando parentesis y
    comillas. None si no cierra."""
    nivel, comillas, inicio, partes = 0, False, abre + 1, []
    i = abre
    while i < len(texto):
        c = texto[i]
        if c == "'":
            comillas = not comillas
        elif not comillas:
            if c == "(":
                nivel += 1
            elif c == ")":
                nivel -= 1
                if nivel == 0:
                    partes.append(texto[inicio:i])
                    return partes, i
            elif c == "," and nivel == 1:
                partes.append(texto[inicio:i])
                inicio = i + 1
        i += 1
    return None


def _traducir_funciones(texto: str) -> str | None:
    """LEFT, RIGHT y CHARINDEX de SQL Server en su forma de SQLite, tambien
    anidadas. None si alguna no se puede traducir igual (CHARINDEX con
    posicion de inicio, o un numero de argumentos que no es el suyo)."""
    salida, posicion = [], 0
    while True:
        m = _FUNCION_A_TRADUCIR.search(texto, posicion)
        if not m:
            salida.append(texto[posicion:])
            return "".join(salida)
        llamada = _argumentos(texto, m.end() - 1)
        if llamada is None:
            return None
        partes, cierra = llamada
        if len(partes) != 2:
            return None
        a, b = (_traducir_funciones(p.strip()) for p in partes)
        if a is None or b is None:
            return None
        funcion = m.group(1).upper()
        if funcion == "LEFT":
            nuevo = f"SUBSTR({a}, 1, {b})"
        elif funcion == "RIGHT":
            # SUBSTR(x, -0) devuelve todo; RIGHT(x, 0) de SQL Server, nada.
            nuevo = f"(CASE WHEN ({b}) > 0 THEN SUBSTR({a}, -({b})) ELSE '' END)"
        else:
            nuevo = f"INSTR({b}, {a})"
        salida.append(texto[posicion:m.start()] + nuevo)
        posicion = cierra + 1


# Una fecha guardada como texto ('2026-10-04'): al devolverla vuelve a ser
# fecha, como la devuelve SQL Server.
_ES_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$")

_OBJETO_EN_FROM = re.compile(
    r"\b(?:FROM|JOIN)\s+((?:\[[^\]]+\]|\w+)(?:\s*\.\s*(?:\[[^\]]*\]|\w*))*)",
    re.IGNORECASE,
)
_NOMBRE_DE_CTE = re.compile(r"(\[[^\]]+\]|\w+)\s+AS\s*\(", re.IGNORECASE)
_TOP_INICIAL = re.compile(
    r"^\s*SELECT\s+(DISTINCT\s+)?TOP\s*\(?\s*(\d+)\s*\)?\s+", re.IGNORECASE
)
_PREFIJO_DEL_SERVIDOR = re.compile(
    r"\[?RAW_DATA_B2B\]?\s*\.\s*\[?dbo\]?\s*\.\s*", re.IGNORECASE
)


# --- Traducir -------------------------------------------------------------

def traducir(query: str) -> str | None:
    """La consulta en SQLite contra el resumen, o None si va al servidor."""
    texto = (query or "").strip().rstrip(";").strip()
    if not texto or not re.match(r"^(SELECT|WITH)\b", texto, re.IGNORECASE):
        return None

    permitidos = {v.casefold() for v in VISTAS} | {CONSOLIDADA.casefold()}
    copias = {c.casefold() for c in COPIAS}
    ctes = {n.strip("[]").casefold() for n in _NOMBRE_DE_CTE.findall(texto)}
    nombrados = _OBJETO_EN_FROM.findall(texto)
    if not nombrados:
        return None
    usa_ventas = usa_copias = False
    for completo in nombrados:
        if completo.startswith("("):
            continue
        partes = re.split(r"\s*\.\s*", completo)
        nombre = partes[-1].strip("[]").casefold()
        if nombre in ctes:
            continue
        # Una tabla de otra base con el mismo nombre no es la misma tabla.
        base = partes[0].strip("[]").casefold() if len(partes) > 1 else None
        if nombre in permitidos and base in (None, "raw_data_b2b"):
            usa_ventas = True
        elif nombre in copias and base in (None, "gtm bi database"):
            usa_copias = True
        else:
            return None
    # Las ventas estan agrupadas y las copias no: mezclarlas daria otra
    # cuenta. Y una consulta sin ninguna de las dos no es para aqui.
    if usa_ventas == usa_copias:
        return None

    if usa_ventas:
        texto = _sin_partes_de_la_fecha(texto)
    if usa_ventas and (_COLUMNA_QUE_NO_ESTA.search(texto) or _NO_SE_TRADUCE.search(texto)):
        return None
    if usa_copias:
        texto = _SIN_NOLOCK.sub("", texto)
        if _COLUMNA_QUE_NO_SE_COPIO.search(texto) or _NO_SE_TRADUCE_EN_COPIAS.search(texto):
            return None
        texto = _traducir_funciones(texto)
        if texto is None:
            return None
        # `+` junto a un texto entre comillas es concatenar (en SQLite, `||`).
        texto = _MAS_DE_TEXTO.sub("||", texto)

    limite = None
    inicial = _TOP_INICIAL.match(texto)
    if inicial:
        limite = int(inicial.group(2))
        texto = "SELECT " + (inicial.group(1) or "") + texto[inicial.end():]
    if re.search(r"\bTOP\b", texto, re.IGNORECASE):
        return None  # un TOP dentro de una subconsulta: no vale la pena
    if limite is not None and re.search(r"\bLIMIT\b", texto, re.IGNORECASE):
        return None

    # Coppel y Liverpool viven partidos en dos vistas: igual que en el
    # servidor (catalogo.completar_media_cadena), una mitad sola se completa.
    for vista, otra in _VISTAS_PARTIDAS:
        if re.search(rf"(?<!\w){re.escape(otra)}(?!\w)", texto, re.IGNORECASE):
            continue
        texto, _ = _reemplazar_objeto(
            texto, vista, f"(SELECT * FROM [{vista}] UNION ALL SELECT * FROM [{otra}])"
        )

    texto = _PREFIJO_DEL_SERVIDOR.sub("", texto)
    texto = _PREFIJO_GTM.sub("", texto)
    texto = re.sub(r"\bISNULL\s*\(", "IFNULL(", texto, flags=re.IGNORECASE)
    texto = re.sub(r"\bLEN\s*\(", "LENGTH(", texto, flags=re.IGNORECASE)
    texto = re.sub(r"\bN'", "'", texto)
    if limite is not None:
        texto += f" LIMIT {limite}"
    return texto


# --- Consultar ------------------------------------------------------------

def _parte_de_fecha(desde: int, hasta: int):
    def parte(valor):
        texto = str(valor or "")
        return int(texto[desde:hasta]) if len(texto) >= hasta and texto[desde:hasta].isdigit() else None
    return parte


def _abrir(archivo: Path, solo_lectura: bool = True) -> sqlite3.Connection:
    if solo_lectura:
        conexion = sqlite3.connect(f"file:{archivo.as_posix()}?mode=ro", uri=True, timeout=5)
    else:
        conexion = sqlite3.connect(archivo, timeout=30)
    # YEAR(), MONTH() y DAY() de SQL Server sobre las fechas de las copias
    # ('2026-10-04'): el bot las usa con los precios, y SQLite no las tiene.
    for nombre, desde, hasta in (("YEAR", 0, 4), ("MONTH", 5, 7), ("DAY", 8, 10)):
        conexion.create_function(nombre, 1, _parte_de_fecha(desde, hasta), deterministic=True)
    return conexion


def consultar(query: str, archivo: Path | None = None) -> tuple[pd.DataFrame, str] | None:
    """El resultado desde el resumen y el SQL que corrio, o None si no aplica.

    None tambien si el resumen no existe todavia o la consulta falla aqui:
    entonces va al servidor como siempre.
    """
    archivo = Path(archivo or ARCHIVO)
    if not archivo.exists():
        return None
    horas = _horas_desde_la_ultima(archivo)
    if horas is None or horas > MAX_HORAS_DE_ATRASO:
        return None
    local = traducir(query)
    if local is None:
        return None
    try:
        with closing(_abrir(archivo)) as conexion:
            if _NOMBRA_UNA_COPIA.search(local):
                limite = time.monotonic() + MAX_SEGUNDOS_EN_COPIAS
                # Devolver algo distinto de cero interrumpe la consulta.
                conexion.set_progress_handler(lambda: int(time.monotonic() > limite), 10_000)
            cursor = conexion.execute(local)
            columnas = [d[0] for d in cursor.description]
            filas = cursor.fetchall()
        df = pd.DataFrame.from_records(filas, columns=columnas)
        for columna in df.columns:
            valores = df[columna].dropna()
            if len(valores) and valores.map(lambda v: isinstance(v, str) and bool(_ES_FECHA.match(v))).all():
                df[columna] = pd.to_datetime(df[columna], errors="coerce")
        return df, local
    except sqlite3.OperationalError as error:
        if "interrupted" not in str(error):
            logger.warning("El resumen local no pudo con la consulta; va al servidor: %s",
                           " ".join(local.split())[:300], exc_info=True)
            return None
        # El tope de MAX_SEGUNDOS_EN_COPIAS: esperado, no es una falla.
        logger.info("Resumen local: la consulta paso de %ss; va al servidor", MAX_SEGUNDOS_EN_COPIAS)
        return None
    except Exception:  # noqa: BLE001 - el servidor la contesta igual
        logger.warning("El resumen local no pudo con la consulta; va al servidor: %s",
                       " ".join(local.split())[:300], exc_info=True)
        return None


# La cadena de cada vista, para decir quien no ha cargado un mes.
_CADENA_DE_LA_VISTA = {
    "vw_ETL_ATT_Postpaid": "AT&T", "vw_ETL_COPPEL": "Coppel", "vw_ETL_COPPEL_IOT": "Coppel",
    "vw_ETL_Elektra": "Elektra", "vw_ETL_Liverpool": "Liverpool", "vw_ETL_Liverpool_IOT": "Liverpool",
    "vw_ETL_Movistar_CAC": "Movistar", "vw_ETL_Sears": "Sears", "vw_ETL_TELCEL_B2B": "Telcel",
}


# Cuanto vale la copia de los meses por cadena cuando no hay resumen (al
# arrancar, mientras se construye): los meses solo crecen, asi que una de
# ayer a lo mas avisa de mas por un dia.
MAX_HORAS_DE_LOS_MESES = 48


def _archivo_de_meses(archivo: Path) -> Path:
    # Junto al resumen y con otro nombre: `borrar` no lo toca.
    return archivo.with_name("meses_por_cadena.json")


def _meses_guardados(archivo: Path) -> dict[str, set[tuple[int, int]]] | None:
    try:
        datos = json.loads(_archivo_de_meses(archivo).read_text(encoding="utf-8"))
        guardado = datetime.fromisoformat(datos["guardado"])
    except (OSError, ValueError, KeyError):
        return None
    if (datetime.now() - guardado).total_seconds() / 3600 > MAX_HORAS_DE_LOS_MESES:
        return None
    return {c: {tuple(m) for m in meses} for c, meses in datos["meses"].items()}


def meses_por_cadena(archivo: Path | None = None) -> dict[str, set[tuple[int, int]]] | None:
    """Que (ano, mes) tiene cargados cada cadena, o None si no se sabe.

    Es lo que permite avisar "Liverpool aun no carga septiembre". Del resumen
    si esta vigente; si no (al arrancar el bot se borra y tarda minutos en
    volver), de la copia que deja cada construccion. Visto el 12/10/2026: sin
    esa copia, en esos minutos el total de septiembre salio sin el aviso.
    """
    archivo = Path(archivo or ARCHIVO)
    horas = _horas_desde_la_ultima(archivo) if archivo.exists() else None
    if horas is None or horas > MAX_HORAS_DE_ATRASO:
        return _meses_guardados(archivo)
    meses: dict[str, set[tuple[int, int]]] = {}
    try:
        with closing(_abrir(archivo)) as conexion:
            for vista, cadena in _CADENA_DE_LA_VISTA.items():
                filas = conexion.execute(
                    f"SELECT DISTINCT [Year], [Month] FROM [{vista}] WHERE [QTY] > 0"
                ).fetchall()
                meses.setdefault(cadena, set()).update((int(a), int(m)) for a, m in filas if a and m)
    except Exception:  # noqa: BLE001 - sin esto solo no hay aviso
        logger.warning("No se pudieron leer los meses del resumen", exc_info=True)
        return None
    return meses


def estado(archivo: Path | None = None) -> dict:
    """Cuando se construyo y cuanto trae, para /health y el log."""
    archivo = Path(archivo or ARCHIVO)
    if not archivo.exists():
        return {"listo": False}
    try:
        with closing(_abrir(archivo)) as conexion:
            meta = dict(conexion.execute("SELECT clave, valor FROM resumen_meta").fetchall())
        return {"listo": True, **meta}
    except Exception:  # noqa: BLE001
        return {"listo": False}


# --- Construir ------------------------------------------------------------

def construir(engine, archivo: Path | None = None) -> dict:
    """Baja el resumen del servidor a un archivo nuevo y lo pone en su lugar.

    Se escribe en un temporal y se renombra al final: mientras se construye,
    las consultas siguen usando el anterior (o el servidor, si no habia).
    """
    archivo = Path(archivo or ARCHIVO)
    archivo.parent.mkdir(parents=True, exist_ok=True)
    temporal = archivo.with_suffix(".construyendo")
    temporal.unlink(missing_ok=True)

    comenzo = time.monotonic()
    columnas_sql = ", ".join(
        [f"RTRIM([{c}])" for c in _TEXTO] + [f"[{c}]" for c in _NUMERO] + ["SUM([QTY])"]
    )
    grupo = ", ".join(f"[{c}]" for c in _TEXTO + _NUMERO)
    definicion = ", ".join(
        [f"[{c}] TEXT COLLATE NOCASE" for c in _TEXTO]
        + [f"[{c}] INTEGER" for c in _NUMERO]
        + ["[QTY] REAL"]
    )
    meta: dict[str, str] = {}
    with closing(_abrir(temporal, solo_lectura=False)) as destino:
        for vista in VISTAS:
            destino.execute(f"CREATE TABLE [{vista}] ({definicion})")
            filas = unidades = 0
            crudo = engine.raw_connection()
            try:
                cursor = crudo.cursor()
                cursor.execute(
                    f"SELECT {columnas_sql} FROM [RAW_DATA_B2B].dbo.[{vista}] GROUP BY {grupo}"
                )
                while True:
                    lote = cursor.fetchmany(50_000)
                    if not lote:
                        break
                    destino.executemany(
                        f"INSERT INTO [{vista}] VALUES ({', '.join('?' * len(COLUMNAS))})",
                        [tuple(f) for f in lote],
                    )
                    filas += len(lote)
                    unidades += sum(f[-1] or 0 for f in lote)
            finally:
                crudo.close()
            destino.execute(f"CREATE INDEX [ix_{vista}] ON [{vista}] ([Year], [Month])")
            meta[f"filas {vista}"] = str(filas)
            meta[f"unidades {vista}"] = f"{unidades:.0f}"
            logger.info("Resumen local: %s, %d filas", vista, filas)

        for tabla in COPIAS:
            # Que falle una copia no cuesta el resumen: sus consultas van al
            # servidor como antes.
            try:
                filas = _copiar(engine, destino, tabla)
                meta[f"filas {tabla}"] = str(filas)
                logger.info("Resumen local: %s, %d filas (copia)", tabla, filas)
            except Exception:  # noqa: BLE001
                logger.exception("Resumen local: no se pudo copiar %s; va al servidor", tabla)
                destino.execute(f"DROP TABLE IF EXISTS [{tabla}]")

        union = " UNION ALL ".join(f"SELECT * FROM [{v}]" for v in VISTAS)
        destino.execute(f"CREATE VIEW [{CONSOLIDADA}] AS {union}")
        meta["construido"] = datetime.now().isoformat(timespec="seconds")
        meta["segundos"] = f"{time.monotonic() - comenzo:.0f}"
        destino.execute("CREATE TABLE resumen_meta (clave TEXT PRIMARY KEY, valor TEXT)")
        destino.executemany("INSERT INTO resumen_meta VALUES (?, ?)", list(meta.items()))
        destino.commit()
    _poner_en_su_lugar(temporal, archivo)
    # La copia de los meses por cadena que sobrevive al borrado del arranque.
    meses = meses_por_cadena(archivo)
    if meses:
        try:
            _archivo_de_meses(archivo).write_text(json.dumps({
                "guardado": datetime.now().isoformat(timespec="seconds"),
                "meses": {c: sorted(m) for c, m in meses.items()},
            }), encoding="utf-8")
        except OSError:
            logger.warning("No se pudo guardar la copia de los meses por cadena", exc_info=True)
    logger.info("Resumen local listo en %ss", meta["segundos"])
    return meta


def _como_texto_de_fecha(valor):
    if valor is None or not hasattr(valor, "isoformat"):
        return valor
    if hasattr(valor, "hour") and (valor.hour, valor.minute, valor.second) != (0, 0, 0):
        return valor.strftime("%Y-%m-%d %H:%M:%S")
    return valor.strftime("%Y-%m-%d")


def _copiar(engine, destino: sqlite3.Connection, tabla: str) -> int:
    """Copia una tabla de [GTM BI Database] fila por fila. Devuelve las filas.

    Los textos sin espacios al final (SQL Server los ignora al comparar y
    SQLite no) y sin distinguir mayusculas, como en el servidor. Las fechas
    como texto 'AAAA-MM-DD' (Prices: todas a medianoche, medido el
    12/10/2026), que compara igual que SQL Server contra '2026-10-04'.
    """
    crudo = engine.raw_connection()
    try:
        cursor = crudo.cursor()
        cursor.execute(
            "SELECT COLUMN_NAME, DATA_TYPE FROM [GTM BI Database].INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
            (tabla,),
        )
        fuera = {c.casefold() for c in _SIN_COPIAR.get(tabla, ())}
        columnas = [(n, t.lower()) for n, t in cursor.fetchall() if n.casefold() not in fuera]
        if not columnas:
            raise ValueError(f"{tabla} no tiene columnas visibles")
        definicion, seleccion = [], []
        for nombre, tipo in columnas:
            if tipo in _TIPOS_DE_TEXTO:
                definicion.append(f"[{nombre}] TEXT COLLATE NOCASE")
                seleccion.append(f"RTRIM([{nombre}])")
            elif tipo in _TIPOS_ENTEROS:
                definicion.append(f"[{nombre}] INTEGER")
                seleccion.append(f"[{nombre}]")
            elif tipo in _TIPOS_DE_FECHA:
                definicion.append(f"[{nombre}] TEXT")
                seleccion.append(f"[{nombre}]")
            else:
                definicion.append(f"[{nombre}] REAL")
                seleccion.append(f"[{nombre}]")
        destino.execute(f"CREATE TABLE [{tabla}] ({', '.join(definicion)})")
        cursor.execute(f"SELECT {', '.join(seleccion)} FROM [GTM BI Database].dbo.[{tabla}] WITH (NOLOCK)")
        insertar = f"INSERT INTO [{tabla}] VALUES ({', '.join('?' * len(columnas))})"
        filas = 0
        while True:
            lote = cursor.fetchmany(50_000)
            if not lote:
                break
            destino.executemany(insertar, [tuple(_como_texto_de_fecha(v) for v in f) for f in lote])
            filas += len(lote)
    finally:
        crudo.close()
    for i, columnas_del_indice in enumerate(_INDICES.get(tabla, ())):
        lista = ", ".join(f"[{c}]" for c in columnas_del_indice)
        destino.execute(f"CREATE INDEX [ix_{tabla}_{i}] ON [{tabla}] ({lista})")
    return filas


def _poner_en_su_lugar(temporal: Path, archivo: Path, intentos: int = 60) -> None:
    """Reemplaza el resumen, esperando a que nadie lo este leyendo.

    En Windows no se puede reemplazar un archivo abierto, y una consulta lo
    tiene abierto un segundo o dos. Se reintenta hasta un minuto.
    """
    for intento in range(intentos):
        try:
            os.replace(temporal, archivo)
            return
        except PermissionError:
            if intento == intentos - 1:
                raise
            time.sleep(1)


def _horas_desde_la_ultima(archivo: Path) -> float | None:
    construido = estado(archivo).get("construido")
    if not construido:
        return None
    return (datetime.now() - datetime.fromisoformat(construido)).total_seconds() / 3600


def borrar(archivo: Path | None = None) -> None:
    """Borra el resumen y lo que haya quedado de una construccion a medias."""
    archivo = Path(archivo or ARCHIVO)
    for ruta in (archivo, archivo.with_suffix(".construyendo"),
                 archivo.with_name(archivo.name + "-journal"),
                 archivo.with_suffix(".construyendo-journal")):
        try:
            ruta.unlink(missing_ok=True)
        except OSError:
            # Abierto por otro proceso (otro bot en la misma maquina): se
            # queda; la construccion lo reemplaza al terminar.
            logger.warning("No se pudo borrar %s; se reemplazara al construir", ruta)
    logger.info("Resumen local borrado al arrancar; mientras se construye, todo va al servidor")


def mantener_al_dia(crear_engine, archivo: Path | None = None,
                    detener: threading.Event | None = None) -> threading.Thread:
    """Un hilo que lo construye al arrancar y cada pocas horas.

    Al arrancar, primero borra el que habia (pedido el 12/10/2026): el bot se
    apaga de noche y a la manana el resumen es del dia anterior. Mientras se
    construye el nuevo, las consultas van al servidor.
    """
    archivo = Path(archivo or ARCHIVO)
    detener = detener or threading.Event()
    borrar(archivo)

    def ciclo():
        while not detener.is_set():
            horas = _horas_desde_la_ultima(archivo)
            if horas is None or horas >= HORAS_ENTRE_REFRESCOS:
                try:
                    construir(crear_engine(), archivo)
                except Exception:  # noqa: BLE001 - se reintenta en el siguiente ciclo
                    logger.exception("No se pudo construir el resumen local")
            detener.wait(15 * 60)

    hilo = threading.Thread(target=ciclo, name="resumen-local", daemon=True)
    hilo.start()
    return hilo
