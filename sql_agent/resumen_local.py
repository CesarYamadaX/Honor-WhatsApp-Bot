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
"""

from __future__ import annotations

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
_TEXTO = ("Retail", "Brand", "Material_Description", "Carrier")
_NUMERO = ("Year", "Month", "WEEK")
COLUMNAS = _TEXTO + _NUMERO + ("QTY",)

# Columnas de las vistas que el resumen NO tiene: si la consulta las nombra,
# va al servidor.
_COLUMNA_QUE_NO_ESTA = re.compile(
    r"(?<![\w\[])\[?(Store|Date|Day|Key\s+prices)\]?(?![\w])", re.IGNORECASE
)

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
    ctes = {n.strip("[]").casefold() for n in _NOMBRE_DE_CTE.findall(texto)}
    nombrados = _OBJETO_EN_FROM.findall(texto)
    if not nombrados:
        return None
    usa_ventas = False
    for completo in nombrados:
        if completo.startswith("("):
            continue
        partes = re.split(r"\s*\.\s*", completo)
        nombre = partes[-1].strip("[]").casefold()
        if nombre in ctes:
            continue
        if nombre not in permitidos:
            return None
        # Una tabla de otra base con el mismo nombre no es la vista.
        if len(partes) > 1 and partes[0].strip("[]").casefold() != "raw_data_b2b":
            return None
        usa_ventas = True
    if not usa_ventas:
        return None

    if _COLUMNA_QUE_NO_ESTA.search(texto) or _NO_SE_TRADUCE.search(texto):
        return None

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
    texto = re.sub(r"\bISNULL\s*\(", "IFNULL(", texto, flags=re.IGNORECASE)
    texto = re.sub(r"\bLEN\s*\(", "LENGTH(", texto, flags=re.IGNORECASE)
    texto = re.sub(r"\bN'", "'", texto)
    if limite is not None:
        texto += f" LIMIT {limite}"
    return texto


# --- Consultar ------------------------------------------------------------

def _abrir(archivo: Path, solo_lectura: bool = True) -> sqlite3.Connection:
    if solo_lectura:
        return sqlite3.connect(f"file:{archivo.as_posix()}?mode=ro", uri=True, timeout=5)
    return sqlite3.connect(archivo, timeout=30)


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
            cursor = conexion.execute(local)
            columnas = [d[0] for d in cursor.description]
            filas = cursor.fetchall()
        return pd.DataFrame.from_records(filas, columns=columnas), local
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


def meses_por_cadena(archivo: Path | None = None) -> dict[str, set[tuple[int, int]]] | None:
    """Que (ano, mes) tiene cargados cada cadena, o None si no hay resumen
    vigente. Es lo que permite avisar "Liverpool aun no carga septiembre"."""
    archivo = Path(archivo or ARCHIVO)
    if not archivo.exists():
        return None
    horas = _horas_desde_la_ultima(archivo)
    if horas is None or horas > MAX_HORAS_DE_ATRASO:
        return None
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

        union = " UNION ALL ".join(f"SELECT * FROM [{v}]" for v in VISTAS)
        destino.execute(f"CREATE VIEW [{CONSOLIDADA}] AS {union}")
        meta["construido"] = datetime.now().isoformat(timespec="seconds")
        meta["segundos"] = f"{time.monotonic() - comenzo:.0f}"
        destino.execute("CREATE TABLE resumen_meta (clave TEXT PRIMARY KEY, valor TEXT)")
        destino.executemany("INSERT INTO resumen_meta VALUES (?, ?)", list(meta.items()))
        destino.commit()
    _poner_en_su_lugar(temporal, archivo)
    logger.info("Resumen local listo en %ss", meta["segundos"])
    return meta


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


def mantener_al_dia(crear_engine, archivo: Path | None = None,
                    detener: threading.Event | None = None) -> threading.Thread:
    """Un hilo que lo construye al arrancar (si hace falta) y cada pocas horas."""
    archivo = Path(archivo or ARCHIVO)
    detener = detener or threading.Event()

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
