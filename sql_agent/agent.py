import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterator

import pandas as pd
import sqlparse
from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_core.callbacks import BaseCallbackHandler
# El bucle del agente (pensar -> herramienta -> resultado -> pensar) es el
# AgentExecutor de siempre, que en LangChain 1.x vive en langchain-classic.
# Antes llegaba por create_sql_agent de langchain-community, que se
# descontinua y aqui solo aportaba un toolkit vacio (ver _create_agent).
from langchain_classic.agents import AgentExecutor, create_openai_tools_agent
from langchain_classic.agents.agent import RunnableMultiActionAgent
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.prompts import (
    ChatPromptTemplate,
    HumanMessagePromptTemplate,
    MessagesPlaceholder,
    SystemMessagePromptTemplate,
)
from langchain_openai import ChatOpenAI
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlparse import tokens as sql_tokens

from catalogo import (
    _corchetes,
    buscar,
    cargar_catalogo,
    avisos_de_media_cadena,
    avisos_de_promotoria,
    completar_media_cadena,
    consolidada_de_repuesto,
    consolidada_trae_basura,
    describir_objeto,
    filtros_que_no_casan,
    indice,
    motivo_para_evitar,
    valores_conocidos,
)
import acceso
from contexto import CRUDAS, contexto_del_resultado, ficha_del_archivo
import excel_pivot
import homologacion
from excel_exporter import MAX_FILAS_POR_HOJA_DE_EXCEL, ExcelExporter
from grafica import grafica_del_resultado, pidio_grafica
import bitacora
import inventario
import resumen_local
import revisor
import ventas_con_modelo
import idioma as idiomas
from pronostico import ERROR_TIPICO, pidio_pronostico, proyectar
from sugerencias import sugerir
import tiempo
from formatting import MAX_ROWS_AS_TEXT, formatear_resultado

logger = logging.getLogger("sql_agent.agent")

load_dotenv()


def _leer_entero_de_entorno(clave: str, por_defecto: int) -> int:
    crudo = os.getenv(clave)
    try:
        return int(crudo) if crudo and crudo.strip() else por_defecto
    except ValueError:
        return por_defecto

# Lista BLANCA de palabras clave que una consulta de lectura puede usar de
# verdad. Es la inversion deliberada de una lista negra: T-SQL no exige punto
# y coma entre sentencias (un lote se escribe pegado y sqlparse lo ve como
# UNA sola sentencia), asi que una lista negra de verbos peligrosos siempre
# va a faltarle una palabra -- BACKUP, RESTORE, SHUTDOWN, KILL, WAITFOR, DENY,
# USE, DBCC, RECONFIGURE, DISABLE TRIGGER, SET IDENTITY_INSERT, CHECKPOINT,
# ninguno de esos es INSERT/UPDATE/DELETE/DROP, y sin embargo "SELECT 1
# SHUTDOWN" es una sentencia completa y valida para SQL Server. Con una lista
# blanca, cualquier keyword que sqlparse reconozca y que nadie haya
# anticipado se rechaza por default en vez de colarse.
_KEYWORDS_PERMITIDAS = {
    "SELECT", "WITH", "FROM", "WHERE",
    "AND", "OR", "NOT", "IN", "IS", "NULL", "NOT NULL", "BETWEEN", "LIKE", "EXISTS",
    "AS", "ON",
    "JOIN", "INNER JOIN", "LEFT JOIN", "LEFT OUTER JOIN", "RIGHT JOIN",
    "RIGHT OUTER JOIN", "FULL JOIN", "FULL OUTER JOIN", "CROSS JOIN",
    "NATURAL JOIN", "INNER", "LEFT", "RIGHT", "FULL", "OUTER", "CROSS", "NATURAL",
    "APPLY", "CROSS APPLY", "OUTER APPLY",
    "GROUP BY", "GROUP", "HAVING", "ORDER BY", "ORDER", "BY", "ASC", "DESC",
    "DISTINCT", "ALL", "TOP", "PERCENT", "TIES",
    "UNION", "UNION ALL", "INTERSECT", "INTERSECT ALL", "EXCEPT", "EXCEPT ALL",
    "CASE", "WHEN", "THEN", "ELSE", "END",
    "OVER", "PARTITION", "WITHIN",
    "OFFSET", "ROWS", "FETCH", "NEXT", "ONLY",
    # --- Fechas: el argumento `datepart` de DATEADD/DATEDIFF/DATEPART/DATENAME
    # llega como palabra clave suelta ("day", "month", ...). Sin esto se
    # rechaza `SELECT DATEADD(day, -7, GETDATE())`, la consulta insignia del
    # bot ("ventas de los ultimos 7 dias"). Un datepart suelto no ejecuta ni
    # cambia nada: solo nombra una unidad de tiempo.
    "YEAR", "QUARTER", "MONTH", "WEEK", "DAY", "HOUR", "MINUTE", "SECOND",
    "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP",
    # --- Funciones reservadas de solo lectura (ver _RESERVADAS_TSQL: son
    # palabras reservadas de T-SQL, asi que sin nombrarlas aqui quedarian
    # bloqueadas y `SELECT COALESCE(a, b)` dejaria de funcionar).
    "COALESCE", "NULLIF", "CONVERT", "TRY_CONVERT",
    "SESSION_USER", "SYSTEM_USER",
    # --- Formato de salida: FOR XML / FOR JSON no escriben, solo cambian como
    # se serializa el resultado.
    "FOR", "XML", "JSON",
    # --- Comparacion e intercalacion.
    "COLLATE", "ESCAPE", "ANY", "SOME",
    # --- Transformaciones y muestreo de solo lectura.
    "PIVOT", "UNPIVOT", "TABLESAMPLE",
    # --- Agregacion avanzada.
    "GROUPING", "SETS", "GROUPING SETS", "CUBE", "ROLLUP",
    # --- Marcos de ventana: ROWS/RANGE BETWEEN UNBOUNDED PRECEDING AND
    # CURRENT ROW.
    "RANGE", "UNBOUNDED", "PRECEDING", "FOLLOWING", "CURRENT", "ROW",
    # --- Sugerencias al optimizador: OPTION (RECOMPILE) no altera datos.
    "OPTION",
}

# Preceder inmediatamente a un segundo SELECT/INSERT/... de nivel superior es
# lo unico que hace legitimo tener mas de una palabra clave DML en la misma
# sentencia (una consulta compuesta real). "ALL" cubre "UNION ALL"/"EXCEPT
# ALL" cuando sqlparse no los fusiona en un solo token.
_OPERACIONES_DE_CONJUNTO = {
    "UNION", "UNION ALL", "INTERSECT", "INTERSECT ALL", "EXCEPT", "EXCEPT ALL", "ALL",
}

# Palabras reservadas de Transact-SQL (lista oficial de Microsoft, finita y
# publica). Existe porque la lista blanca de arriba depende de COMO tipa
# sqlparse cada token, y sus diccionarios no son de T-SQL: WRITETEXT,
# UPDATETEXT, SETUSER, REVERT, READTEXT, RAISERROR, SAVE, TRAN, SHUTDOWN,
# KILL, WAITFOR, DBCC, BACKUP, RESTORE... se tokenizan como Token.Name, igual
# que el nombre de una columna, asi que el escaneo de keywords ni los mira.
#
# La regla que usa este conjunto (ver `_evaluar_sql`) recorre TODOS los tokens
# con valor, sin importar su ttype, y rechaza cualquiera cuyo texto sea una
# palabra reservada de T-SQL que no este explicitamente permitida. Eso cierra
# la clase completa en vez de ir agregando comandos de a uno cada vez que
# aparece un vector nuevo: si es reservada y no esta en la lista blanca, no
# pasa, sin importar como la haya clasificado sqlparse.
_RESERVADAS_TSQL = {
    "ADD", "ALL", "ALTER", "AND", "ANY", "AS", "ASC", "AUTHORIZATION",
    "BACKUP", "BEGIN", "BETWEEN", "BREAK", "BROWSE", "BULK", "BY",
    "CASCADE", "CASE", "CHECK", "CHECKPOINT", "CLOSE", "CLUSTERED",
    "COALESCE", "COLLATE", "COLUMN", "COMMIT", "COMPUTE", "CONSTRAINT",
    "CONTAINS", "CONTAINSTABLE", "CONTINUE", "CONVERT", "CREATE", "CROSS",
    "CURRENT", "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP",
    "CURRENT_USER", "CURSOR",
    "DATABASE", "DBCC", "DEALLOCATE", "DECLARE", "DEFAULT", "DELETE", "DENY",
    "DESC", "DISK", "DISTINCT", "DISTRIBUTED", "DOUBLE", "DROP", "DUMP",
    "ELSE", "END", "ERRLVL", "ESCAPE", "EXCEPT", "EXEC", "EXECUTE", "EXISTS",
    "EXIT", "EXTERNAL",
    "FETCH", "FILE", "FILLFACTOR", "FOR", "FOREIGN", "FREETEXT",
    "FREETEXTTABLE", "FROM", "FULL", "FUNCTION",
    "GOTO", "GRANT", "GROUP",
    "HAVING", "HOLDLOCK",
    "IDENTITY", "IDENTITY_INSERT", "IDENTITYCOL", "IF", "IN", "INDEX",
    "INNER", "INSERT", "INTERSECT", "INTO", "IS",
    "JOIN", "KEY", "KILL",
    "LEFT", "LIKE", "LINENO", "LOAD",
    "MERGE", "NATIONAL", "NOCHECK", "NONCLUSTERED", "NOT", "NULL", "NULLIF",
    "OF", "OFF", "OFFSETS", "ON", "OPEN", "OPENDATASOURCE", "OPENQUERY",
    "OPENROWSET", "OPENXML", "OPTION", "OR", "ORDER", "OUTER", "OVER",
    "PERCENT", "PIVOT", "PLAN", "PRECISION", "PRIMARY", "PRINT", "PROC",
    "PROCEDURE", "PUBLIC",
    "RAISERROR", "READ", "READTEXT", "RECONFIGURE", "REFERENCES",
    "REPLICATION", "RESTORE", "RESTRICT", "RETURN", "REVERT", "REVOKE",
    "RIGHT", "ROLLBACK", "ROWCOUNT", "ROWGUIDCOL", "RULE",
    "SAVE", "SCHEMA", "SECURITYAUDIT", "SELECT", "SEMANTICKEYPHRASETABLE",
    "SEMANTICSIMILARITYDETAILSTABLE", "SEMANTICSIMILARITYTABLE",
    "SESSION_USER", "SET", "SETUSER", "SHUTDOWN", "SOME", "STATISTICS",
    "SYSTEM_USER",
    "TABLE", "TABLESAMPLE", "TEXTSIZE", "THEN", "TO", "TOP", "TRAN",
    "TRANSACTION", "TRIGGER", "TRUNCATE", "TRY_CONVERT", "TSEQUAL",
    "UNION", "UNIQUE", "UNPIVOT", "UPDATE", "UPDATETEXT", "USE", "USER",
    "VALUES", "VARYING", "VIEW",
    "WAITFOR", "WHEN", "WHERE", "WHILE", "WITH", "WRITETEXT",
}

# Lo poco que NO es palabra reservada de T-SQL y aun asi es peligroso: las
# funciones de linked server, que ejecutan un literal de texto en otro motor
# (`SELECT * FROM OPENQUERY(srv, 'DELETE FROM t')`). El resto de lo que antes
# vivia aqui (SHUTDOWN, KILL, WAITFOR, DBCC, RECONFIGURE, BACKUP, RESTORE,
# DENY) salio: ya son reservadas y las cubre `_RESERVADAS_TSQL`, de forma
# estructural en vez de por enumeracion.
_NOMBRES_PROHIBIDOS = {
    "OPENQUERY", "OPENROWSET", "OPENDATASOURCE", "OPENXML",
}

# Caracteres con los que abre un identificador entrecomillado. Una columna que
# se llame `[backup]` o `"backup"` es legitima: el nombre esta delimitado, no
# es la palabra reservada.
_INICIOS_DE_IDENTIFICADOR_CITADO = ('[', '"', '`')


def _normalizado(tok) -> str:
    """Texto de un token en mayusculas y con espacios internos colapsados.

    sqlparse a veces preserva espacios irregulares dentro de una palabra
    clave compuesta (p.ej. "GROUP  BY" con doble espacio se normaliza igual,
    con el doble espacio adentro); esto evita que ese detalle de formato
    esquive la comparacion contra la lista blanca.
    """
    return " ".join(str(tok.normalized).split()).upper()


def _sentencia_tiene_contenido(sentencia: sqlparse.sql.Statement) -> bool:
    """True si la sentencia tiene algo mas que espacios, comentarios o un ';' suelto."""
    primero = sentencia.token_first(skip_cm=True, skip_ws=True)
    if primero is None:
        return False
    if primero.ttype is sql_tokens.Punctuation and str(primero) == ";":
        return False
    return True


def _primer_token_significativo(tokens_planos):
    """Primer token real, saltando espacios, comentarios y parentesis de apertura.

    Saltar los parentesis de apertura permite aceptar `(SELECT 1)` o
    `((SELECT 1))` sin dejar de exigir que lo primero "de verdad" sea SELECT o
    WITH.
    """
    for tok in tokens_planos:
        if tok.is_whitespace or tok.ttype in sql_tokens.Comment:
            continue
        if tok.ttype is sql_tokens.Punctuation and str(tok) == "(":
            continue
        return tok
    return None


def _evaluar_sql(sql: str) -> tuple[bool, str]:
    """Evalua si el SQL es una sola sentencia de lectura y por que, si no.

    Devuelve (es_valida, motivo); motivo es "" cuando es_valida es True, y
    describe la regla que se disparo cuando es False (esto es lo que le deja
    saber al modelo QUE cambiar en vez de reintentar a ciegas).

    El filtro original era `sql.strip().lower().startswith("select")`, y la
    primera reescritura de esta funcion uso una lista NEGRA de verbos
    prohibidos sobre tokens de sqlparse. Ambas fallan por el mismo motivo de
    fondo: T-SQL no exige punto y coma entre sentencias, asi que un lote
    pegado (`SELECT 1 SHUTDOWN`, `SELECT 1 BACKUP DATABASE ...`) sigue siendo
    UNA sola sentencia para sqlparse, y una lista negra siempre tiene un hueco
    -- no existe forma de enumerar todo lo que NO se debe poder ejecutar.

    Esta version es una lista BLANCA: en vez de prohibir lo peligroso, se
    exige que cada palabra clave real que aparezca en la sentencia (no solo
    la primera) pertenezca a un conjunto explicito de palabras clave de
    lectura. Cualquier palabra clave no anticipada se rechaza por default.
    Ademas:
    - Se exige una sola sentencia real (evita `select 1; drop table T`, y
      tambien detecta el caso donde sqlparse SI reconoce, por los literales,
      que hay dos sentencias reales aunque no haya un `;` visible al ojo
      humano por estar "escondido" en apariencia dentro de un literal
      anterior).
    - Se exige que, ignorando parentesis de apertura, la sentencia empiece
      con SELECT o WITH.
    - Se rechaza cualquier token cuyo texto sea una palabra RESERVADA de
      T-SQL (`_RESERVADAS_TSQL`) que no este en la lista blanca, sin mirar
      como lo haya tipado sqlparse. Esto es necesario porque los
      diccionarios de sqlparse no son de T-SQL: WRITETEXT, UPDATETEXT,
      SETUSER, REVERT, READTEXT, RAISERROR, SAVE/TRAN, SHUTDOWN, KILL,
      WAITFOR, DBCC, BACKUP... llegan como Token.Name, igual que el nombre
      de una columna, asi que la capa de keywords ni los ve. Se saltan los
      literales de cadena y los identificadores entrecomillados o entre
      corchetes: una columna llamada `[backup]` es legitima.
    - Se rechazan ademas las funciones de linked server OPENQUERY/
      OPENROWSET/OPENDATASOURCE/OPENXML, que ejecutan un literal de texto
      en otro motor y no son palabras reservadas.
    - FETCH solo se acepta como paginacion (despues de un OFFSET); sin
      OFFSET previo es la lectura de un cursor (`FETCH NEXT FROM cur`).
    - Se rechaza que aparezca mas de una palabra clave DML (tipicamente un
      segundo SELECT) al nivel superior de la sentencia (fuera de
      parentesis) a menos que la preceda inmediatamente UNION/INTERSECT/
      EXCEPT (con o sin ALL): esto es lo que cierra `SELECT 1 WAITFOR DELAY
      '...' SELECT 2`, donde ninguna palabra individual esta prohibida pero
      la combinacion sí ejecuta un segundo lote.

    Esto sigue siendo defensa en profundidad, no la garantia: la garantia es
    que DB_URI apunte a un login con permisos db_datareader.
    """
    # Guarda barata: un '/*' sin su '*/' no deberia ni llegar al parser.
    # Nota: cuenta a nivel de texto crudo, sin distinguir literales, asi que
    # un literal que contenga un '/*' sin '*/' en ningun lado de la consulta
    # se rechazaria de mas (falla segura: bloquea una consulta valida, nunca
    # deja pasar una peligrosa) -- por ejemplo
    # `SELECT comentario FROM t WHERE comentario LIKE '%/*%'`.
    if sql.count("/*") > sql.count("*/"):
        return False, "comentario de bloque sin cerrar"

    sentencias = [s for s in sqlparse.parse(sql) if _sentencia_tiene_contenido(s)]
    if len(sentencias) != 1:
        return False, "multiples sentencias"

    tokens_planos = list(sentencias[0].flatten())

    primero = _primer_token_significativo(tokens_planos)
    if primero is None:
        return False, "sentencia vacia"
    es_select_kw = (
        primero.ttype is sql_tokens.Keyword.DML and primero.normalized.upper() == "SELECT"
    )
    es_with_kw = (
        primero.ttype is sql_tokens.Keyword.CTE and primero.normalized.upper() == "WITH"
    )
    if not (es_select_kw or es_with_kw):
        return False, "no empieza con SELECT o WITH"

    # Capa 1 -- lista blanca sobre las palabras clave que sqlparse SI reconoce.
    # De paso se vigila FETCH, la unica palabra permitida con dos vidas:
    # paginacion (`ORDER BY a OFFSET 10 ROWS FETCH NEXT 5 ROWS ONLY`) y
    # lectura de cursor (`FETCH NEXT FROM cur`). La forma de paginacion solo
    # es valida despues de un OFFSET, asi que un FETCH sin OFFSET previo es
    # un cursor y se rechaza.
    hubo_offset = False
    for tok in tokens_planos:
        # "AT TIME ZONE '<zona>'" llega como UN solo token (Keyword.TZCast)
        # con el literal de la zona adentro, asi que nunca coincidiria con la
        # lista blanca. Es conversion de zona horaria: no ejecuta nada.
        if tok.ttype is sql_tokens.Keyword.TZCast:
            continue
        if tok.ttype in sql_tokens.Keyword:
            clave = _normalizado(tok)
            if clave not in _KEYWORDS_PERMITIDAS:
                return False, f"palabra clave no permitida: {clave}"
            if clave == "OFFSET":
                hubo_offset = True
            elif clave == "FETCH" and not hubo_offset:
                return False, "FETCH de cursor (solo se permite FETCH tras OFFSET)"

    # Capa 2 -- palabras reservadas de T-SQL, mire sqlparse donde mire. Se
    # recorren TODOS los tokens con valor, sin filtrar por ttype: es lo que
    # cierra WRITETEXT/UPDATETEXT/SETUSER/REVERT/READTEXT/RAISERROR/SAVE TRAN
    # y cualquier otro comando reservado que sqlparse tipe como Token.Name.
    # Se saltan literales, comentarios e identificadores entrecomillados, para
    # que `SELECT [backup] FROM t` o `SELECT 'drop table T'` sigan pasando.
    for tok in tokens_planos:
        if tok.is_whitespace or tok.ttype in sql_tokens.Comment:
            continue
        if tok.ttype in sql_tokens.Literal:
            continue
        texto = str(tok).strip()
        if not texto or texto.startswith(_INICIOS_DE_IDENTIFICADOR_CITADO):
            continue
        palabra = texto.upper()
        if palabra in _RESERVADAS_TSQL and palabra not in _KEYWORDS_PERMITIDAS:
            return False, f"palabra reservada de T-SQL no permitida: {palabra}"
        if palabra in _NOMBRES_PROHIBIDOS:
            return False, f"comando o funcion no permitido: {palabra}"

    # Mas de un SELECT/INSERT/... a nivel superior (fuera de parentesis) sin
    # una operacion de conjunto de por medio es un segundo lote disfrazado.
    profundidad = 0
    vistos_en_nivel_superior = 0
    anterior_significativo = None
    for tok in tokens_planos:
        if tok.is_whitespace or tok.ttype in sql_tokens.Comment:
            continue
        if tok.ttype is sql_tokens.Punctuation and str(tok) == "(":
            profundidad += 1
        elif tok.ttype is sql_tokens.Punctuation and str(tok) == ")":
            profundidad -= 1
        elif profundidad == 0 and tok.ttype is sql_tokens.Keyword.DML:
            vistos_en_nivel_superior += 1
            if vistos_en_nivel_superior > 1:
                previo = _normalizado(anterior_significativo) if anterior_significativo else ""
                if previo not in _OPERACIONES_DE_CONJUNTO:
                    return False, "multiples sentencias SELECT sin UNION/INTERSECT/EXCEPT"
        anterior_significativo = tok

    return True, ""


def es_select(sql: str) -> bool:
    """Indica si el SQL es una sola sentencia de lectura. Ver `_evaluar_sql`."""
    return _evaluar_sql(sql)[0]


# Un enlace de markdown cuyo destino no es http(s): el modelo los escribe
# solo, apuntando a rutas que no existen. Ver `_sin_enlaces_inventados`.
_ENLACE_SIN_DESTINO = re.compile(r"\[([^\]\n]+)\]\((?!https?://)[^)\n]*\)")
# El mismo enlace cuando ocupa una linea entera (con vineta, negritas o un
# emoji de adorno). Ahi no es parte de una oracion: es un boton de "Descargar
# el reporte" que no existe, y quitarle solo el destino dejaba esa linea
# suelta en el mensaje (visto el 30/09/2026 en dos reportes seguidos).
_LINEA_DE_ENLACE_INVENTADO = re.compile(
    r"^[ \t>*\-•\U0001F300-\U0001FAFF]*\*{0,2}"
    r"\[[^\]\n]+\]\((?!https?://)[^)\n]*\)\*{0,2}[ \t.:]*(?:\n|$)",
    re.MULTILINE,
)

# El modelo cierra ofreciendo mas trabajo ("¿Quieres que lo abra por
# cadena?"), y debajo va la ficha de sugerencias que arma el servicio. Son
# tres preguntas en un mensaje y la del modelo es la peor de las tres: no
# dice si lo que ofrece se puede o no. Ver `_sin_ofrecimiento_propio`.
_OFRECIMIENTO_FINAL = re.compile(
    r"(\bquieres\b|\bgustas\b|te\s+gustaria|\bgustaría\b|"
    r"puedo\s+(?:sacar|armar|hacer|prepararte|mandarte|darte)|"
    r"dime\s+si|avisame\s+si|avísame\s+si|"
    r"would\s+you\s+like|do\s+you\s+want|i\s+can\s+also|let\s+me\s+know|"
    r"需要|要不要|告诉我)",
    re.IGNORECASE,
)
# Hasta donde se considera "la despedida" y no contenido. Un parrafo largo
# que casualmente termine en pregunta es una respuesta, no un ofrecimiento.
MAX_LARGO_DEL_OFRECIMIENTO = 220


_MESES_ES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
             "agosto", "septiembre", "octubre", "noviembre", "diciembre")
_TODAS_LAS_CADENAS = re.compile(r"ETL_Consolidado_B2B|vw_ETL_Con_Modelo", re.IGNORECASE)
_FILTRA_CADENA = re.compile(r"\[?Retail\]?\s*(?:=|LIKE|IN\b|<>)", re.IGNORECASE)
_ANIO_IGUAL = re.compile(r"\[?Year\]?\s*=\s*(\d{4})", re.IGNORECASE)
_MES_IGUAL = re.compile(r"\[?Month\]?\s*=\s*(\d{1,2})\b", re.IGNORECASE)
_MES_EN = re.compile(r"\[?Month\]?\s+IN\s*\(([\d,\s]+)\)", re.IGNORECASE)
_MES_ENTRE = re.compile(r"\[?Month\]?\s+BETWEEN\s+(\d{1,2})\s+AND\s+(\d{1,2})", re.IGNORECASE)


def cadenas_sin_el_periodo(query: str) -> str:
    """El aviso de que un total de TODAS las cadenas deja fuera a alguna que
    aun no carga ese mes, o "".

    Visto el 9/10/2026: "cuantas unidades vendio Honor en septiembre" dio
    113 790 sin decir que Liverpool y Movistar todavia no tenian septiembre
    -- el total parecia completo y le faltaban dos cadenas. Solo para una
    consulta de todas las cadenas (la consolidada o la vista con modelo, sin
    filtrar [Retail]) y de un ano con meses concretos; lo demas no se
    adivina.
    """
    if not _TODAS_LAS_CADENAS.search(query or "") or _FILTRA_CADENA.search(query):
        return ""
    anios = set(_ANIO_IGUAL.findall(query))
    if len(anios) != 1:
        return ""
    meses: set[int] = {int(m) for m in _MES_IGUAL.findall(query)}
    for lista in _MES_EN.findall(query):
        meses.update(int(m) for m in re.findall(r"\d+", lista))
    for desde, hasta in _MES_ENTRE.findall(query):
        meses.update(range(int(desde), int(hasta) + 1))
    meses = {m for m in meses if 1 <= m <= 12}
    if not meses:
        return ""
    cargados = resumen_local.meses_por_cadena()
    if not cargados:
        return ""
    anio = int(anios.pop())
    faltan = {}
    for cadena, tiene in sorted(cargados.items()):
        sin = sorted(m for m in meses if (anio, m) not in tiene)
        if sin and tiene:
            ultimo = max(tiene)
            faltan[cadena] = (sin, ultimo)
    if not faltan:
        return ""
    detalle = "; ".join(
        f"{cadena} no tiene {', '.join(_MESES_ES[m - 1] for m in sin)} de {anio} "
        f"(lo mas reciente: {_MESES_ES[ultimo[1] - 1]} de {ultimo[0]})"
        for cadena, (sin, ultimo) in faltan.items()
    )
    return (
        f"OJO: este resultado NO incluye todas las cadenas: {detalle}. Al "
        "responder dilo en una frase ('sin Liverpool ni Movistar, que aun no "
        "cargan ese mes'), o el total parece completo y no lo es. Si es un "
        "periodo anterior a que esa cadena empezara a reportar, di que no hay "
        "datos de ella para ese periodo."
    )


# Las cadenas que cargan UN dato al mes, en una sola semana (medido el
# 9/10/2026: 2026 tiene las semanas 5, 9, 14, 18, 23, 27, 31, 36, 40).
_MENSUALES = {
    "vw_ETL_Sears": "Sears", "SEARS_B2B": "Sears",
    "vw_ETL_Movistar_CAC": "Movistar", "MVS_B2B": "Movistar",
    "vw_ETL_COPPEL_IOT": "Coppel IOT",
}
_PIDE_RETAIL_MENSUAL = re.compile(r"\[?Retail\]?\s*(?:=|LIKE)\s*N?'(SEARS|MOVISTAR)", re.IGNORECASE)
_SEMANA_IGUAL = re.compile(r"\[?WEEK\]?\s*=\s*(\d{1,2})\b", re.IGNORECASE)


def semana_de_cadena_mensual(query: str) -> str:
    """El aviso de que se pidio UNA semana de una cadena que reporta por mes.

    Visto el 9/10/2026: "Samsung en Sears en la semana 38" se contesto "no
    tengo dato de esa semana", y la nota del catalogo no alcanzo: Sears
    manda un dato al mes, y el de septiembre esta en la semana 40.
    """
    semanas = _SEMANA_IGUAL.findall(query or "")
    if len(semanas) != 1:
        return ""
    cadenas = {c for objeto, c in _MENSUALES.items() if objeto.lower() in query.lower()}
    retail = _PIDE_RETAIL_MENSUAL.search(query)
    if retail:
        cadenas.add(retail.group(1).title())
    if not cadenas:
        return ""
    semana = int(semanas[0])
    anios = _ANIO_IGUAL.findall(query)
    anio = int(anios[0]) if len(anios) == 1 else datetime.now().year
    try:
        mes = date.fromisocalendar(anio, semana, 4).month
    except ValueError:
        return ""
    quienes = " y ".join(sorted(cadenas))
    return (
        f"OJO: {quienes} NO reporta por semana: carga un solo dato al mes, en "
        "una sola semana. Por eso la semana pedida puede salir vacia o sin "
        f"sentido. La semana {semana} de {anio} cae en {_MESES_ES[mes - 1]}: "
        f"consulta [Month] = {mes} (y [Year] = {anio}) y al responder explica "
        f"que {quienes} reporta por mes y da la cifra de {_MESES_ES[mes - 1]}."
    )


class ConsultaCancelada(Exception):
    """El usuario mando otra pregunta (o pidio cancelar) y esta ya no importa."""


# Las cifras de un texto: "19,999", "4 210", "23157.5". Las de menos de 10
# no cuentan (meses, "2 carriers", dias): casan con cualquier tabla.
_CIFRA = re.compile(r"\d{1,3}(?:[,\u00a0\u202f ]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
MAX_FILAS_PARA_CIFRAS = 50_000


def cifras_del_texto(texto: str) -> frozenset[int]:
    cifras = set()
    for crudo in _CIFRA.findall(texto or ""):
        valor = float(re.sub(r"[,\u00a0\u202f ]", "", crudo))
        if valor >= 10:
            cifras.add(round(valor))
    return frozenset(cifras)


def cifras_del_resultado(df: pd.DataFrame) -> frozenset[int]:
    """Cada numero del resultado y el total de cada columna, redondeados."""
    numericas = df.select_dtypes("number").head(MAX_FILAS_PARA_CIFRAS)
    cifras = set()
    for columna in numericas.columns:
        valores = pd.to_numeric(numericas[columna], errors="coerce").dropna()
        valores = valores[valores.abs() < 1e15]
        if valores.empty:
            continue
        cifras.update(valores.round().astype("int64").tolist())
        cifras.add(round(float(valores.sum())))
    return frozenset(c for c in cifras if abs(c) >= 10)


# Que columna es "la cifra" de un resultado: unidades antes que un total
# (en un share, "Unidades totales" es el mercado entero), y nunca un precio,
# un share, un ano o un numero de tienda.
_MEDIDA_PRINCIPAL = (
    re.compile(r"unidades|qty|cantidad|ventas|units|piezas|equipos|stock", re.IGNORECASE),
    re.compile(r"total", re.IGNORECASE),
)
_NO_ES_CIFRA_PRINCIPAL = re.compile(
    r"^(a[nñ]o|anio|year|mes|month|semana|week|dia|day)$|precio|price|share|%|"
    r"participaci|promedio|capa|maximo|numero|n[uú]m\b|id\b|sku|codigo|c[oó]digo",
    re.IGNORECASE,
)


def cifra_principal(df: pd.DataFrame) -> tuple[str, float] | None:
    """(columna, total) de la medida principal del resultado, o None."""
    if df is None or df.empty:
        return None
    candidatas = [
        c for c in df.columns
        if pd.api.types.is_numeric_dtype(df[c]) and not _NO_ES_CIFRA_PRINCIPAL.search(str(c))
    ]
    for patron in _MEDIDA_PRINCIPAL:
        for columna in candidatas:
            if patron.search(str(columna)):
                total = pd.to_numeric(df[columna], errors="coerce").sum()
                return (str(columna), float(total)) if total else None
    return None


@dataclass
class Reporte:
    """Un reporte de la vuelta: sus archivos, sus cifras, y los pivots que
    se le arman solo si es el que se entrega (ver entregar_reporte)."""

    rutas: list[str]
    cifras: frozenset[int] = frozenset()
    # (archivo, hoja, config, nombre para la bitacora)
    pivotes: list[tuple[str, str, dict, str]] = field(default_factory=list)
    # (columna, total) de lo que trae: lo que se dice si el mensaje no da
    # ninguna cifra (ver responder).
    principal: tuple[str, float] | None = None

    @property
    def con_grafica(self) -> bool:
        return any(r.lower().endswith(".png") for r in self.rutas)


@dataclass
class ContextoPeticion:
    """Estado de una sola pregunta: de donde se lee y que reporte se genero."""

    engine: Engine
    # La pregunta tal cual la escribio el usuario. La miran las herramientas
    # que necesitan saber QUE se pidio y no solo que datos salieron: si dijo
    # "muestrame una grafica", la grafica se genera aunque el resultado quepa
    # en texto.
    pregunta: str = ""
    archivos: list[str] = field(default_factory=list)
    # Si ya se proyecto en esta vuelta. Evita que la misma pregunta lleve dos
    # pronosticos -- el de la herramienta y el del guardian de `consultar_sql`
    # -- calculados sobre series distintas.
    pronostico_hecho: bool = False
    # La ficha de en que se baso el pronostico, ya en el idioma del usuario.
    # La pega `responder` al final del mensaje: que aparezca no puede depender
    # de que el modelo se acuerde de contarlo.
    base_del_pronostico: str = ""
    # Se uso el scraping de internet: la respuesta lleva al pie de donde
    # salio (ver idioma "fuente_internet").
    de_internet: bool = False
    # Que mas se puede preguntar sobre lo que se acaba de entregar. Claves de
    # sugerencias.py; el texto lo arma `responder` en el idioma del usuario.
    sugerencias: list[str] = field(default_factory=list)
    # El idioma detectado de la pregunta, para los textos que escribe el
    # servicio y no el modelo.
    idioma: str = idiomas.POR_DEFECTO
    # Las consultas que ya fallaron en esta vuelta, normalizadas, con su
    # error. Evita que el modelo reintente LETRA POR LETRA lo mismo.
    consultas_fallidas: dict[str, str] = field(default_factory=dict)
    # Cuando empezo la peticion, para saber cuanto queda del presupuesto.
    inicio: float = field(default_factory=time.monotonic)
    # Lo pone la API cuando llega un mensaje nuevo del mismo chat. No hay
    # forma de matar el hilo que corre el agente, asi que la cancelacion es
    # cooperativa: se revisa antes de cada herramienta y se aborta ahi.
    cancelacion: threading.Event | None = None
    # Las cadenas que puede ver quien pregunta, o None si todas. Ver acceso.py.
    cadenas: frozenset[str] | None = None
    # Las cadenas que pidio y no tiene, y si alguna herramienta si devolvio
    # datos: con eso `responder` decide si el mensaje es solo el aviso de
    # permiso o la respuesta con el aviso al final.
    negadas: set[str] = field(default_factory=set)
    hubo_datos: bool = False
    # Lo que de verdad salio de la base en esta vuelta, como lo leyo el
    # modelo. Con esto el revisor comprueba que el mensaje diga la verdad.
    vistos: list[str] = field(default_factory=list)
    # Todos los reportes de la vuelta, para escoger al final (entregar_reporte)
    # el de la consulta con la que se contesto.
    reportes: list[Reporte] = field(default_factory=list)

    def registrar_reporte(
        self,
        *rutas: str | None,
        cifras: frozenset[int] = frozenset(),
        pivotes: list[tuple[str, str, dict, str]] | None = None,
        principal: tuple[str, float] | None = None,
    ) -> None:
        """Guarda los adjuntos de la pregunta. Se entrega UN reporte, el bueno.

        Una pregunta, un archivo. Antes se acumulaban y se enviaban todos: el
        usuario pidio agregar el modelo a un reporte de ventas y recibio dos
        Excel -- 11 501 filas de ventas sin el modelo y 4 509 nombres de modelo
        sin ventas-- cuando queria una sola tabla con ambas cosas.

        Cual es el bueno se decide asi: se conserva el PRIMERO, salvo que
        despues llegue uno CON GRAFICA y el guardado no la tenga.

        Las dos mitades de esa regla vienen de un fallo real:

        - El primero, porque lo que suele venir despues son comprobaciones
          (un DISTINCT para ver como se escriben los valores). Conservar el
          ultimo mandaba esas comprobaciones en lugar del reporte: en una
          prueba, la lista de 16 retailers en vez de las ventas pedidas.

        - Salvo que traiga grafica, porque el agente tambien explora al
          reves: pidio "una grafica del top de modelos", saco 635 filas, la
          herramienta le dijo que eran demasiadas, y recien entonces acoto a
          10. Se le entrego el volcado de 635 sin grafica y se tiraron el
          Excel chico Y su grafica, que era exactamente lo que se pidio.

        Y la grafica distingue bien los dos casos porque solo se dibuja
        cuando el resultado cabe entera en ella y tiene una columna
        numerica: un DISTINCT de comprobacion nunca la genera.

        Esto es solo la eleccion PROVISIONAL: al final, `entregar_reporte`
        la corrige con las cifras de la respuesta.
        """
        nuevos = [r for r in rutas if r]
        if not nuevos:
            return
        self.reportes.append(Reporte(nuevos, cifras, list(pivotes or []), principal))
        trae_grafica = any(r.lower().endswith(".png") for r in nuevos)
        tiene_grafica = any(r.lower().endswith(".png") for r in self.archivos)
        if not self.archivos or (trae_grafica and not tiene_grafica):
            self.archivos[:] = nuevos

    def entregar_reporte(self, respuesta: str) -> None:
        """Escoge el reporte que se entrega y le arma sus pivots.

        Visto el 8/10/2026: a "en cuanto esta el iPhone 17" el modelo hizo
        una consulta de precios minimos historicos, la corrigio con otra de
        precios recientes y contesto con la segunda, pero el Excel era el de
        la primera (la regla de quedarse con el primero): precios de hace un
        ano y modelos Pro Max que nadie pidio.

        El reporte bueno es el de la consulta con la que se contesto, y eso
        se ve en las cifras: gana el que mas cifras de la respuesta trae.
        Entre los que empatan, el que tiene grafica y luego el mas reciente
        -- el ultimo que leyo el modelo antes de contestar. Una comprobacion
        (un DISTINCT, una lista de cadenas) no trae las cifras de la
        respuesta, asi que no le puede quitar el lugar al reporte. Si la
        respuesta no cita ninguna cifra que este en algun reporte, queda la
        eleccion de registrar_reporte.
        """
        provisional = next((r for r in self.reportes if r.rutas == self.archivos), None)
        elegido = provisional
        citadas = cifras_del_texto(respuesta)
        if provisional is not None and len(self.reportes) > 1 and citadas:
            puntos = [len(citadas & r.cifras) for r in self.reportes]
            mejor = max(puntos)
            if mejor > 0:
                empatados = [r for r, p in zip(self.reportes, puntos) if p == mejor]
                con_grafica = [r for r in empatados if r.con_grafica]
                elegido = (con_grafica or empatados)[-1]
                if elegido is not provisional:
                    self.archivos[:] = elegido.rutas
                    bitacora.paso(
                        "el archivo que se entrega es el de la consulta con la que se contesto",
                        bitacora.NOTA,
                    )
        if elegido is None:
            return
        # El pivot va con Excel por COM sobre el archivo ya guardado, y solo
        # al que se entrega: cada uno de mas son ~5-7 s tirados.
        for archivo, hoja, config, nombre in elegido.pivotes:
            bitacora.paso(f"armando el pivot de {nombre}", bitacora.NOTA)
            if excel_pivot.agregar_pivot(archivo, hoja, **config):
                bitacora.paso("pivot listo", bitacora.NOTA)
            else:
                bitacora.paso("no se pudo armar el pivot; el reporte sale sin el", bitacora.NOTA)


_contexto: ContextVar[ContextoPeticion | None] = ContextVar("_contexto", default=None)


@contextmanager
def contexto_peticion(
    engine: Engine,
    cancelacion: threading.Event | None = None,
    pregunta: str = "",
    idioma: str = idiomas.POR_DEFECTO,
    cadenas=None,
) -> Iterator[ContextoPeticion]:
    """Activa un contexto por petición, en lugar del viejo global _db_instance."""
    ctx = ContextoPeticion(
        engine=engine, cancelacion=cancelacion, pregunta=pregunta, idioma=idioma,
        cadenas=acceso.normalizar(cadenas),
    )
    testigo = _contexto.set(ctx)
    try:
        yield ctx
    finally:
        _contexto.reset(testigo)


def _contexto_actual() -> ContextoPeticion:
    """El contexto de la peticion en curso, si no la han cancelado.

    Todas las herramientas que tocan la base pasan por aqui, asi que es el
    punto natural para rendirse: el agente alterna llamada al modelo y llamada
    a herramienta, y una llamada al modelo no se puede interrumpir, pero entre
    una y otra si. En el peor caso se aborta un paso despues de que el usuario
    lo pidio, no al terminar los 90 segundos.
    """
    ctx = _contexto.get()
    if ctx is None:
        raise RuntimeError(
            "No hay contexto de peticion activo. Las herramientas solo pueden "
            "usarse dentro de contexto_peticion()."
        )
    if ctx.cancelacion is not None and ctx.cancelacion.is_set():
        raise ConsultaCancelada()
    return ctx


# SQL Server dice que el nombre no existe, pero no dice donde SI existe.
_OBJETO_INVALIDO = re.compile(r"Invalid object name '([^']+)'")

# Lo que dice el driver cuando corta una consulta por tiempo. Reconocerlo por
# el mensaje y no solo por el reloj: el corte puede llegar antes de que se
# cumpla el presupuesto medido aqui (el driver cuenta desde que mandó la
# consulta, no desde que se armo).
_POR_TIEMPO = re.compile(r"timeout expired|HYT00|query timeout", re.IGNORECASE)


_COLUMNA_INVALIDA = re.compile(r"Invalid column name '([^']+)'")


def _columnas_que_si_existen(error: Exception, query: str) -> str:
    """Si la columna no existe, dice cuales tiene la tabla. "" si no aplica.

    SQL Server dice que [Día] no existe, pero no dice como se llama ahi la
    fecha. Y la fecha se llama distinto en cada tabla: [Día] en COPPEL_B2B,
    [Day] en EKT_B2B, [Fecha] en TELCEL_B2B, [Date]+[Year]/[Month]/[WEEK] en
    las vistas vw_ETL_*. En produccion el agente probaba [Día] contra la
    vista de Elektra, comia el error y recien entonces acertaba: un viaje
    perdido en cada pronostico. El catalogo tiene las columnas, asi que la
    correccion se puede dar hecha.
    """
    coincidencia = _COLUMNA_INVALIDA.search(str(error))
    if not coincidencia:
        return ""
    faltante = coincidencia.group(1)

    plano = " ".join(query.split())
    candidatos = [
        objeto
        for base in cargar_catalogo()["bases"]
        for objeto in base["objetos"]
        # Identificador completo, no subcadena: la vista "Coppel" vive dentro
        # del texto "COPPEL_B2B" y sin esto se reportaban las columnas de la
        # tabla equivocada.
        if re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(objeto['nombre'])}(?![A-Za-z0-9_])",
            plano,
            re.IGNORECASE,
        )
    ]
    # El nombre mas largo primero: si la consulta nombra varias tablas, la
    # mas especifica es la que suele traer la columna en cuestion.
    for objeto in sorted(candidatos, key=lambda o: -len(o["nombre"])):
        columnas = [c["nombre"] for c in objeto["columnas"]]
        if any(c.casefold() == faltante.casefold() for c in columnas):
            # Esta tabla si la tiene: el error viene de otra de la consulta.
            continue
        return (
            f" {objeto['nombre']} no tiene [{faltante}]. Sus columnas son: "
            + ", ".join(f"[{c}]" for c in columnas)
            + "."
        )
    return ""


def _es_por_tiempo(error: Exception) -> bool:
    return bool(_POR_TIEMPO.search(str(error)))


def _homologacion_mal_unida(sql: str) -> str:
    """Detecta el cruce de columnas que devuelve un top falso, o "" si no lo hay.

    UNICOS$ tiene dos columnas para unir y cada una va con su tabla:
    [DESCRIPCION] con la tabla CRUDA del retailer, y
    [MATERIAL DESCRIPTION FINAL] con las vistas vw_ETL_COPPEL*. Cruzarlas no
    da error ni cero filas: da MENOS filas. Medido contra vw_ETL_COPPEL, unir
    por [DESCRIPCION] casa apenas el 3.6% del volumen, y el top que sale
    parece de lo mas razonable.

    Por eso se bloquea aqui y no solo en el prompt: el agente acerto el join
    unas veces y lo cruzo otras con la misma instruccion delante, y el
    resultado equivocado es indistinguible del bueno a simple vista.
    """
    plano = " ".join(sql.split()).upper()
    if "UNICOS$" not in plano or "VW_ETL_" not in plano:
        return ""
    if "MATERIAL DESCRIPTION FINAL" in plano:
        return ""
    if "DESCRIPCION" not in plano:
        return ""
    return (
        "Ese JOIN esta mal y devolveria un top falso: estas uniendo una vista "
        "vw_ETL_* con [DESCRIPCION], y esa combinacion solo casa el 3.6% del "
        "volumen. Con una vista vw_ETL_COPPEL usa "
        "u.[MATERIAL DESCRIPTION FINAL] = v.[Material_Description]; para "
        "cualquier otro retailer consulta la TABLA CRUDA y une por "
        "u.[DESCRIPCION]."
    )


# `FROM [RAW_DATA_B2B].dbo.[vw_ETL_Elektra] v` -> vw_ETL_Elektra. Solo el
# ultimo tramo del nombre: base y esquema no le dicen nada al modelo.
_OBJETO_EN_FROM = re.compile(
    r"\b(?:FROM|JOIN)\s+((?:\[[^\]]+\]|[\w$]+)(?:\s*\.\s*(?:\[[^\]]+\]|[\w$]+))*)",
    re.IGNORECASE,
)


_UNION_CON_HOMOLOGACION = re.compile(
    r"JOIN\s+(?:\[?GTM BI Database\]?\s*\.\s*\[?dbo\]?\s*\.\s*)?\[?UNICOS\$\]?"
    r"(?P<resto>.*?)(?=\bWHERE\b|\bGROUP\b|\bORDER\b|\bJOIN\b|\bUNION\b|$)",
    re.IGNORECASE | re.DOTALL,
)


def _homologacion_por_like(sql: str) -> str:
    """Si se une con UNICOS$ por LIKE, por que no, o "" si no.

    Visto el 7/10/2026: "cuantos telefonos Honor se vendieron en agosto"
    unio la consolidada con UNICOS$ por `[Material_Description] LIKE '%' +
    u.[HOMOLOGACION MODELO] + '%'`. UNICOS$ tiene varias filas por modelo
    (una por cada forma de escribirlo en cada cadena), asi que cada venta
    casa varias veces: dio 358 079 cuando Honor vendio 160 909, y tardo 205
    segundos. El numero inflado no da error.
    """
    for union in _UNION_CON_HOMOLOGACION.finditer(sql or ""):
        on = re.split(r"\bON\b", union.group("resto"), maxsplit=1, flags=re.IGNORECASE)
        if len(on) == 2 and re.search(r"\bLIKE\b", on[1], re.IGNORECASE):
            return (
                "No la ejecute: unir con UNICOS$ por LIKE cuenta cada venta "
                "varias veces (UNICOS$ tiene varias filas por modelo). Asi, "
                "'telefonos Honor de agosto 2026' dio 358 079 cuando Honor "
                "vendio 160 909 en total. Une por IGUALDAD, como dice la nota "
                "de UNICOS$ (describir_objeto): [MATERIAL DESCRIPTION FINAL] "
                "con las vistas vw_ETL_COPPEL, [DESCRIPCION] con la tabla "
                "cruda de cada cadena. Si solo preguntan unidades de una "
                "marca, no hace falta UNICOS$: filtra [Brand]."
            )
    return ""


_POR_LUGAR = re.compile(
    r"tienda|sucursal|punto de venta|regi[oó]n|zona|estado|ciudad|almac[eé]n|cedis"
    r"|\bstores?\b|\bregions?\b|\bwarehouse|门店|地区|区域|仓库",
    re.IGNORECASE,
)
_INVENTARIO_TELCEL = re.compile(r"(?<!\w)(TELCEL_INV_B2B|INV_Telcel_B2B|vw_INV_Telcel_B2B)(?!\w)", re.IGNORECASE)


def _inventario_telcel_a_mano(query: str, pregunta: str) -> str:
    """Si se consulta el inventario de Telcel a mano, a la herramienta. "" si no.

    Visto el 7/10/2026: "What's the Honor inventory at Telcel?" se contesto
    con un SUM de TELCEL_INV_B2B -- el stock y nada mas -- en vez de con
    `semanas_de_inventario`, que da fecha, stock y para cuanto alcanza. En
    espanol si la uso, por suerte: su descripcion solo hablaba de semanas.
    Un desglose pedido ("por tienda", "por region") si va a mano: la
    herramienta lo da por modelo.
    """
    if not _INVENTARIO_TELCEL.search(query or "") or _POR_LUGAR.search(pregunta or ""):
        return ""
    return (
        "No la ejecute: el inventario de Telcel se contesta con "
        "`semanas_de_inventario` (llamala con el modelo, o vacio para toda la "
        "marca). Da la fecha del corte, el stock y para cuanto le alcanza, "
        "que es lo que se quiere saber aunque solo pregunten 'cuanto hay'."
    )


# Una tienda del catalogo comparada tal cual con la tienda de las ventas.
_NOMBRE_DE_TIENDA = r"(?:[\w$]+\s*\.\s*)?\[?(?:Coppel Name|HONOR NAME)\]?"
_TIENDA_DE_VENTA = r"(?:[\w$]+\s*\.\s*)?\[?(?:Store|STORE|Tienda|Store Name|Nombre Tienda)\]?"
_TIENDAS_UNIDAS_TAL_CUAL = re.compile(
    rf"(?<![\w(]){_NOMBRE_DE_TIENDA}\s*=\s*{_TIENDA_DE_VENTA}(?![\w)])"
    rf"|(?<![\w(]){_TIENDA_DE_VENTA}\s*=\s*{_NOMBRE_DE_TIENDA}(?![\w)])",
    re.IGNORECASE,
)


def _tiendas_sin_normalizar(sql: str) -> str:
    """Si se une el catalogo de tiendas con las ventas por nombre tal cual.

    Visto el 7/10/2026: en las ventas de Liverpool, Galerias Monterrey y
    Tampico vienen con DOBLE espacio ('L  MONTERREY', 'L  TAMPICO') y en
    CustomerStores con uno ('L MONTERREY'). `cs.[Coppel Name] = v.[Store]`
    las perdia sin dar error: el reporte de la region 4 salio sin dos de sus
    tiendas mas grandes (~29 mil unidades en 2024-2025).
    """
    if not _TIENDAS_UNIDAS_TAL_CUAL.search(sql or ""):
        return ""
    return (
        "No la ejecute: unir CustomerStores con las ventas por el nombre de "
        "la tienda TAL CUAL pierde tiendas sin dar error -- en las ventas "
        "algunas vienen con doble espacio ('L  MONTERREY', 'L  TAMPICO') y en "
        "el catalogo con uno ('L MONTERREY'); a Liverpool region 4 le faltaban "
        "Galerias Monterrey y Tampico. Para la region usa la columna [Region] "
        "de [RAW_DATA_B2B].dbo.[vw_ETL_Con_Modelo], que ya hace el cruce bien "
        "en todas las cadenas; si necesitas otra columna del catalogo, compara "
        "los dos lados sin espacios de mas."
    )


# La columna de unidades de cada tabla cruda de venta. Liverpool_IOT, la
# otra mitad de Liverpool, se llama distinto pero cuenta igual.
_UNIDADES_CRUDAS = {c.tabla.casefold(): c.unidades for c in CRUDAS.values()} | {
    "liverpool_iot": "Vtas# U Per 1",
}


def _crudo_con_devoluciones(sql: str) -> str:
    """Si suma unidades de una tabla cruda sin quitar las devoluciones. "" si no.

    La venta oficial -- la de las vistas vw_ETL_* y la de Power BI -- cuenta
    solo las filas con 1 unidad o mas; la tabla cruda trae ademas las
    devoluciones en negativo. Visto el 7/10/2026: la capa de Liverpool
    region 4 se saco de las crudas y Tampico Altama en diciembre de 2025
    dio 2 054 cuando la venta fue 2 090. La misma pregunta, contestada antes
    desde las vistas, habia dado 2 090: dos numeros para lo mismo.
    """
    if not re.search(r"\bSUM\s*\(", sql or "", re.IGNORECASE):
        return ""
    objetos = {r.objeto.casefold() for r in acceso.referencias(sql)}
    for tabla in sorted(objetos & set(_UNIDADES_CRUDAS)):
        columna = re.escape(_UNIDADES_CRUDAS[tabla])
        if not re.search(rf"\[?{columna}\]?", sql, re.IGNORECASE):
            continue
        if re.search(rf"\[?{columna}\]?\s*(>=\s*1(\.0+)?(?!\d)|>\s*0(\.0+)?(?!\d))", sql, re.IGNORECASE):
            continue
        return (
            f"No la ejecute: la venta oficial (la de las vistas vw_ETL_* y la "
            f"de Power BI) cuenta solo las filas con 1 unidad o mas, y la tabla "
            f"cruda trae ademas las devoluciones en negativo: sumada asi da de "
            f"menos (Liverpool Tampico Altama, diciembre de 2025: 2 054 contra "
            f"2 090). Agrega `[{_UNIDADES_CRUDAS[tabla]}] >= 1` al WHERE de "
            f"cada tabla cruda, o usa la vista vw_ETL_* de la cadena."
        )
    return ""


# Las tablas crudas de venta. Pedido el 7/10/2026: las respuestas salen
# SOLO de las vistas ETL, las mismas de Power BI; la cruda es el insumo y va
# al Excel como detalle (Raw_Data), nunca a la respuesta.
_CRUDAS_DE_VENTA = {c.tabla.casefold() for c in CRUDAS.values()} | {
    "liverpool_iot", "coppel_iot", "mvs_b2b",
}


def _tabla_cruda_para_responder(sql: str) -> str:
    """Si la consulta lee una tabla cruda de venta, por que no. "" si no."""
    crudas = sorted({
        r.objeto for r in acceso.referencias(sql or "") if r.objeto.casefold() in _CRUDAS_DE_VENTA
    })
    if not crudas:
        return ""
    return (
        f"No la ejecute: {', '.join(crudas)} es una tabla cruda, y las "
        "respuestas salen solo de las vistas ETL, que son la venta oficial "
        "(las mismas de Power BI); la cruda no suma igual. Usa la vista de la "
        "cadena (vw_ETL_*), la consolidada, o -- si piden el modelo, solo "
        "celulares o una region -- [RAW_DATA_B2B].dbo.[vw_ETL_Con_Modelo], que "
        "ya trae [Modelo], [Clasificacion] y [Region]. El detalle crudo le "
        "llega al usuario solo, en el Excel."
    )


_PIDE_CAPA = re.compile(r"(?<!\w)capa(?!\w)|capacidad de venta|store capacity", re.IGNORECASE)
_FILTRA_MARCA = re.compile(r"\[?Brand\]?\s*(=|IN\b|LIKE\b)|\[?Marca\]?\s*(=|IN\b|LIKE\b)", re.IGNORECASE)


def _capa_de_una_marca(query: str, pregunta: str) -> str:
    """Si se pide la CAPA y la consulta la saca de una sola marca. "" si no.

    La CAPA es la capacidad de venta de la TIENDA: lo maximo que vendio en un
    mes, de todas las marcas. Visto el 7/10/2026: "la capa mensual de las
    tiendas de Liverpool region 4" salio una vez con todas las marcas (L
    Monterrey La Fe, 2 149) y otra solo con Honor (L Saltillo, 245). Solo es
    de una marca si la pregunta la nombra.
    """
    if not _PIDE_CAPA.search(pregunta or "") or not _FILTRA_MARCA.search(query or ""):
        return ""
    marcas = valores_conocidos("ETL_Consolidado_B2B", "Brand") or []
    nombradas = [
        m for m in marcas
        if m and re.search(rf"(?<!\w){re.escape(str(m))}(?!\w)", pregunta, re.IGNORECASE)
    ]
    if nombradas:
        return ""
    return (
        "No la ejecute: la CAPA es la capacidad de venta de la TIENDA, lo maximo "
        "que vendio en un mes contando TODAS las marcas, y la pregunta no nombra "
        "ninguna. Quita el filtro de [Brand] (si quieres, agrega la parte de "
        "Honor como columna aparte, pero la capa es la de todas)."
    )


# Desde cuanto de la venta sin modelo vale la pena decirlo.
MINIMO_SIN_MODELO = 0.02


def _venta_sin_modelo(df: pd.DataFrame) -> str:
    """Si el resultado agrupa por modelo y una parte quedo sin modelo, el aviso.

    El catalogo de modelos (UNICOS$) no esta al dia: medido el 7/10/2026,
    el 35% de la venta de octubre de Telcel Retail son modelos que aun no
    tiene (Honor X6E y X7E, Redmi Note 17 Pro, Moto G37...). En un "por
    modelo" esa venta sale en una fila sin nombre; contarla como "otros" o
    ignorarla deja fuera justo los lanzamientos.
    """
    columna = next((c for c in df.columns if str(c).strip().lower() in ("modelo", "model")), None)
    if columna is None or df.empty:
        return ""
    medida = tiempo.medida(df)
    if medida is None:
        return ""
    valores = pd.to_numeric(df[medida], errors="coerce").fillna(0)
    total = valores.sum()
    sin = valores[df[columna].isna()].sum()
    if total <= 0 or sin / total < MINIMO_SIN_MODELO:
        return ""
    return (
        f"OJO: {sin:,.0f} unidades ({sin / total:.0%}) son modelos que el catalogo "
        "de modelos todavia no tiene (lanzamientos recientes): salen en la fila "
        "sin nombre. Dilo en una frase al contestar -- no las cuentes como "
        "'otros' ni las dejes fuera sin avisar."
    )


def _objetos_a_evitar(query: str) -> str:
    """Si la consulta lee algo marcado como NO USAR, por que y que usar. "" si no.

    Bloquea en vez de avisar, como los demas guardianes: un aviso pegado al
    resultado llega cuando el numero ya salio, y B2BHonorGeneralReport da
    una cuarta parte de la venta sin ningun error que lo delate.
    """
    motivos = []
    for ref in acceso.referencias(query):
        motivo = motivo_para_evitar(ref.objeto)
        if motivo and f"{ref.objeto}: {motivo}" not in motivos:
            motivos.append(f"{ref.objeto}: {motivo}")
    if not motivos:
        return ""
    return "No la ejecute. " + " ".join(motivos)


def _sin_permiso(ctx: "ContextoPeticion", acotada: "acceso.Acotada") -> str:
    """Anota las cadenas negadas y devuelve lo que se le dice al modelo."""
    ctx.negadas.update(acotada.fuera)
    return acceso.motivo(acotada, ctx.cadenas)


def _objetos_consultados(query: str) -> list[str]:
    """Las tablas y vistas que lee la consulta, sin repetir y en orden."""
    vistos: list[str] = []
    for completo in _OBJETO_EN_FROM.findall(query or ""):
        nombre = re.split(r"\s*\.\s*", completo)[-1].strip("[]")
        if nombre and nombre not in vistos and not nombre.startswith("("):
            vistos.append(nombre)
    return vistos


# Errores que no son de la consulta sino de la DEFINICION de una vista: 205
# es un UNION cuyas partes no traen las mismas columnas, 4413 una vista que
# no se puede resolver. Visto el 30 de septiembre de 2026: a seis de las
# nueve vistas vw_ETL_* les agregaron la columna [Key prices] y a tres no, y
# ETL_Consolidado_B2B -- que es el UNION de las nueve -- dejo de funcionar
# para cualquier consulta. El modelo leia el error como suyo y reintentaba
# la misma vista hasta agotar los pasos.
_VISTA_ROTA = re.compile(
    r"All queries combined using a UNION|Could not use view or function|binding errors",
    re.IGNORECASE,
)


def _vista_rota_en_el_servidor(error: Exception, query: str) -> str:
    """Si el error es de la vista consolidada y no de la consulta, lo dice.

    Solo cuando la consulta NO trae un UNION propio: si lo trae, el error de
    columnas puede ser del modelo, y ahi el mensaje correcto es el del driver.
    """
    if not _VISTA_ROTA.search(str(error)):
        return ""
    if "ETL_Consolidado_B2B".casefold() not in query.casefold():
        return ""
    if re.search(r"\bUNION\b", query, re.IGNORECASE):
        return ""
    return (
        " ESTE ERROR NO ES DE TU CONSULTA: la vista ETL_Consolidado_B2B esta "
        "rota en el servidor (las vistas vw_ETL_* que une ya no traen las "
        "mismas columnas). No la vuelvas a usar en esta pregunta, falle como "
        "falle. Consulta cadena por cadena en las vistas vw_ETL_* "
        "(vw_ETL_Elektra, vw_ETL_TELCEL_B2B, vw_ETL_Sears, vw_ETL_ATT_Postpaid, "
        "vw_ETL_Movistar_CAC), nombrando las columnas que necesitas y NUNCA "
        "con SELECT *: algunas traen una columna de mas. Coppel y Liverpool "
        "viven en dos vistas cada una y van juntas en la MISMA consulta con "
        "UNION ALL (vw_ETL_COPPEL con vw_ETL_COPPEL_IOT, vw_ETL_Liverpool con "
        "vw_ETL_Liverpool_IOT). Al contestar, dile al usuario que la vista "
        "consolidada esta fallando en el servidor y que el numero sale de "
        "sumar las cadenas una por una."
    )


def _donde_vive_de_verdad(error: Exception) -> str:
    """Si el objeto existe en otra base, nombra la buena. Si no, cadena vacia.

    El error crudo del driver no lleva a ningun lado: el modelo no sabe si se
    equivoco de base, de esquema o de nombre, asi que reintenta lo mismo hasta
    agotar el timeout. Paso en produccion con
    `[GTM BI Database].dbo.[ETL_Consolidado_B2B]` -- la vista existe, pero en
    RAW_DATA_B2B; dos intentos identicos y 90 segundos perdidos.

    El catalogo sabe en que base esta cada objeto, asi que la correccion se
    puede dar hecha en vez de esperar a que el modelo adivine.
    """
    coincidencia = _OBJETO_INVALIDO.search(str(error))
    if not coincidencia:
        return ""
    encontrados = buscar(coincidencia.group(1))
    if not encontrados:
        return ""
    correctos = ", ".join(
        f"[{base}].dbo.[{obj['nombre']}]" for base, obj in encontrados
    )
    return (
        f" Ese objeto existe, pero en otra base: usa {correctos}. "
        "Corrige la base y reintenta."
    )


# El catalogo de homologacion: 7 758 nombres de modelo, 184k filas. Se
# consulta en medio segundo, a diferencia de la vista de ventas (39M filas),
# donde un LIKE es un escaneo completo que agota el timeout.
CATALOGO_MODELOS = "[GTM BI Database].dbo.[UNICOS$]"
MAX_MODELOS = 30
MAX_TOKENS = 5


def _tokens_de_modelo(texto: str) -> list[str]:
    """Palabras y numeros sueltos del texto, sin comodines de LIKE.

    Se parte por todo lo que no sea alfanumerico (el guion bajo incluido, que
    en LIKE vale por "un caracter cualquiera"), asi que ningun token puede
    colarse como metacaracter.
    """
    return [t for t in re.findall(r"[A-Za-z0-9]+", texto)][:MAX_TOKENS]


@tool
def buscar_modelos(texto: str) -> str:
    """Traduce un nombre coloquial de celular a los nombres reales del catalogo.

    Usala ANTES de escribir el SELECT siempre que el usuario nombre un modelo
    de forma suelta ("magic", "magic pro", "el X9", "los plegables"): te dice
    que modelos existen de verdad, de que marca son, y como filtrarlos.
    """
    bitacora.paso(f"buscando el modelo {texto!r} en el catalogo", bitacora.NOTA)
    tokens = _tokens_de_modelo(texto)
    if not tokens:
        return "Dame al menos una palabra o numero del modelo."

    condiciones = " AND ".join(["[HOMOLOGACION MODELO] LIKE ?"] * len(tokens))
    sql = (
        f"SELECT TOP {MAX_MODELOS} [HOMOLOGACION MODELO], [BRAND], COUNT(*) AS n "
        f"FROM {CATALOGO_MODELOS} WHERE {condiciones} "
        "GROUP BY [HOMOLOGACION MODELO], [BRAND] ORDER BY COUNT(*) DESC"
    )

    ctx = _contexto_actual()
    try:
        with ctx.engine.connect() as conexion:
            with _presupuesto_de_tiempo(conexion, _plazo_de_herramienta(ctx)):
                filas = conexion.exec_driver_sql(
                    sql, tuple(f"%{t}%" for t in tokens)
                ).fetchall()
    except Exception as error:  # noqa: BLE001
        logger.exception("Fallo la busqueda de modelos")
        return f"No pude consultar el catalogo de modelos: {error}"

    if not filas:
        return (
            f"Ningun modelo del catalogo contiene {' + '.join(tokens)}. "
            "Prueba con menos palabras o revisa el nombre con el usuario."
        )

    por_marca: dict[str, list[str]] = {}
    for modelo, marca, _ in filas:
        por_marca.setdefault(marca or "(sin marca)", []).append(modelo)

    lineas = []
    for marca, modelos in por_marca.items():
        lineas.append(f"{marca}:")
        lineas += [f"  {m}" for m in modelos]

    if len(por_marca) > 1:
        lineas.append(
            "OJO: son MARCAS DISTINTAS. 'RED MAGIC' es de otro fabricante y un "
            "'MAGICBOOK' es una laptop, no un celular: no los mezcles."
        )

    filtro = " AND ".join(
        f"[Material_Description] LIKE '%{t}%'" for t in tokens
    )
    lineas.append(
        "COMO FILTRAR ESTO EN LAS VENTAS:\n"
        "- La tabla es [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B], escrita "
        "completa y tal cual. NO esta en [GTM BI Database].\n"
        "- La marca va por su COLUMNA: `[Brand] = 'HONOR'`. Meter la marca en "
        "el LIKE de la descripcion PIERDE ventas, porque no todas las "
        "descripciones repiten la marca.\n"
        "- El modelo va por LIKE, uno por cada palabra QUE DIJO EL USUARIO, "
        f"unidas con AND. Para lo que buscaste aqui es exactamente: {filtro}\n"
        "- No agregues palabras que el usuario no dijo: un '%lite%' de mas "
        "deja fuera los PRO y los plegables. Y no uses el nombre completo del "
        "catalogo, que se escribe distinto en cada fuente ('MAGIC 8 LITE' en "
        "el catalogo, 'MAGIC8 LITE' en la vista de ventas).\n"
        "- Si arriba salen cosas que no son celulares (un MAGICBOOK es una "
        "laptop) y la pregunta era de celulares, quitalas con NOT LIKE."
    )
    return "\n".join(lineas)


# Debe coincidir con AGENT_TIMEOUT_SECONDS: es el presupuesto que la API le
# da a la peticion completa. Aqui solo se usa para repartirlo, no para
# imponerlo.
# Sube a 600 porque se pidio que con muchos datos NO se rinda y entregue el
# Excel completo. Un reporte de un anio de Coppel son 164 507 filas crudas:
# la consulta, el Excel y el pivot juntos pasan de los 300 que habia, y el
# usuario recibia una respuesta a medias o ninguna. El sidecar espera mas que
# esto a proposito (ver TIMEOUT_MS_DEFAULT en agentClient.js): si fuera al
# reves, cortaria el la respuesta que el servicio si alcanzo a producir.
TIMEOUT_PETICION = _leer_entero_de_entorno("AGENT_TIMEOUT_SECONDS", 600)
# Lo que se reserva para armar el Excel y la grafica y devolver la respuesta.
MARGEN_DE_ENTREGA = 20
# Por debajo de esto no vale la pena intentar: contra la vista consolidada,
# hasta un SUM de un solo mes tarda mas de un minuto (medido).
MINIMO_UTIL = 30

MAX_VALORES = 50

# Lo maximo que puede esperar a la base una herramienta que no es la
# consulta principal (precios, modelos, pronostico, inventario). Antes no
# tenian tope: el 7/10/2026, en la prueba de 10 personas a la vez, la de
# precios espero 20 minutos a que el scraping soltara su tabla, y esa
# conversacion se quedo colgada hasta el corte de 10 minutos.
TIMEOUT_HERRAMIENTA = 90


def _plazo_de_herramienta(ctx: "ContextoPeticion") -> int:
    """El tope de una herramienta, sin pasarse de lo que le queda a la peticion."""
    queda = int(TIMEOUT_PETICION - (time.monotonic() - ctx.inicio) - MARGEN_DE_ENTREGA)
    return max(1, min(TIMEOUT_HERRAMIENTA, queda))

# Explorar no puede costar la respuesta: la peticion entera vive dentro de
# AGENT_TIMEOUT_SECONDS (300s por defecto), que ademas tiene que alcanzar para
# la consulta real y para armar el Excel.
TIMEOUT_VALORES = 20


@contextmanager
def _presupuesto_de_tiempo(conexion, segundos: int) -> Iterator[None]:
    """Limita cuanto puede tardar una consulta, y deja la conexion como estaba.

    El timeout de pyodbc se fija en la CONEXION, no en el cursor, y el pool de
    SQLAlchemy la recicla: si se dejara puesto, la siguiente consulta que
    reutilice esa conexion -- por ejemplo el SELECT de verdad, que puede tardar
    mucho mas-- moriria a los 20 segundos sin razon aparente. De ahi el
    restaurar en el finally.
    """
    # `.connection` devuelve el proxy del pool, no la conexion de pyodbc:
    # ponerle el timeout ahi no hace nada (se guarda en el envoltorio y la
    # consulta sigue corriendo sin limite). El atributo hay que fijarlo en la
    # conexion real, que en SQLAlchemy 2.x cuelga de `dbapi_connection`.
    bruta = getattr(conexion.connection, "dbapi_connection", conexion.connection)
    if not hasattr(bruta, "timeout"):  # SQLite en las pruebas
        yield
        return
    anterior = bruta.timeout
    bruta.timeout = segundos
    try:
        yield
    finally:
        try:
            bruta.timeout = anterior
        except Exception:  # noqa: BLE001
            # Tras un HYT00 la conexion ya esta cerrada y no hay nada que
            # restaurar; sin esto el "Attempt to use a closed connection"
            # tapaba el error de verdad (visto el 9/10/2026).
            pass


@tool
def valores_distintos(tabla: str, columna: str) -> str:
    """Lista como estan escritos los valores de una columna.

    Usala para comprobar con que texto buscar ANTES de escribir el SELECT
    final: los retailers, marcas y carriers no estan escritos de forma
    uniforme. Es solo para mirar -- no genera ningun reporte para el usuario.
    """
    bitacora.paso(f"mirando como se escriben los valores de {tabla}.{columna}", bitacora.NOTA)
    encontrados = buscar(tabla)
    if not encontrados:
        return f"No conozco ninguna tabla o vista {tabla!r}."
    base, objeto = encontrados[0]

    # El nombre de una columna no puede viajar como parametro, asi que se
    # resuelve contra el catalogo y se usa el del catalogo, no el del modelo:
    # lo que termina en el SQL nunca es texto libre.
    por_nombre = {c["nombre"].casefold(): c["nombre"] for c in objeto["columnas"]}
    real = por_nombre.get(columna.strip().strip("[]").casefold())
    if real is None:
        return (
            f"{objeto['nombre']} no tiene columna {columna!r}. Tiene: "
            + ", ".join(c["nombre"] for c in objeto["columnas"])
        )

    # Primero la foto. En las columnas que el agente consulta seguido
    # (retailers, marcas, carriers, anios) el DISTINCT contra la vista grande
    # tarda minutos: en produccion se comia los 20s de timeout sin devolver
    # nada, tres veces, y agotaba los 90 de la peticion antes de que la
    # consulta real alcanzara a correr.
    ctx = _contexto_actual()
    sql = (
        f"SELECT DISTINCT TOP {MAX_VALORES} {_corchetes(real)} "
        f"FROM {_corchetes(base)}.dbo.{_corchetes(objeto['nombre'])} "
        f"WHERE {_corchetes(real)} IS NOT NULL ORDER BY 1"
    )
    precalculados = valores_conocidos(objeto["nombre"], real)
    if objeto["nombre"] == ventas_con_modelo.NOMBRE and precalculados is None:
        # La vista virtual no se puede recorrer con un DISTINCT en 20 s: el
        # modelo y la clasificacion salen del catalogo de modelos.
        if real == "Modelo":
            return "Para ver como se escribe un modelo usa `buscar_modelos`: [Modelo] es [HOMOLOGACION MODELO] de UNICOS$."
    if objeto["nombre"] == ventas_con_modelo.NOMBRE:
        if precalculados is None and real == "Clasificacion":
            sql = (f"SELECT DISTINCT TOP {MAX_VALORES} [CLASSIFICATION] FROM "
                   f"{ventas_con_modelo.UNICOS} WHERE [CLASSIFICATION] IS NOT NULL ORDER BY 1")
        else:
            # Antes de los permisos: asi ven las vistas reales.
            sql = ventas_con_modelo.expandir(sql, ctx.cadenas)
    if ctx.cadenas:
        # Un usuario acotado ve los valores de sus cadenas, no los de todas:
        # la lista de tiendas de otra cadena ya es informacion de esa cadena.
        # Marcas, carriers y periodos no son de nadie y salen de la foto.
        acotada = acceso.acotar(sql, ctx.cadenas)
        if acotada.bloqueada:
            return _sin_permiso(ctx, acotada)
        columna_cadena = acceso.columna_de_cadena(objeto["nombre"])
        if precalculados is not None and columna_cadena and (
            columna_cadena.casefold() == real.casefold()
        ):
            precalculados = [v for v in precalculados if acceso.cadena_de(v) in ctx.cadenas]
        elif acotada.acotados and real.casefold() not in _DE_NADIE:
            precalculados = None
        sql = acotada.query
    if precalculados is not None:
        return _listar(real, objeto["nombre"], precalculados)

    try:
        with ctx.engine.connect() as conexion:
            with _presupuesto_de_tiempo(conexion, TIMEOUT_VALORES):
                filas = conexion.exec_driver_sql(sql).fetchall()
    except Exception as error:  # noqa: BLE001
        logger.exception("Fallo al listar valores distintos")
        # Un DISTINCT sobre ETL_Consolidado_B2B (39M filas, UNION ALL de nueve
        # vistas agregadas) tarda minutos. NO se responde con una muestra: las
        # primeras filas de la vista son todas de un mismo retailer, asi que
        # una muestra de 50k devuelve 3 de los 16 valores y el modelo
        # concluiria que los demas no existen. Mejor decir que no se pudo.
        # Sin el texto del driver: al agotarse el tiempo pyodbc cierra la
        # conexion y reporta "Attempt to use a closed connection", que suena a
        # un problema de red y manda a investigar donde no es.
        return (
            f"No pude listar los valores de {real} en {objeto['nombre']}: "
            f"es una columna demasiado grande para enumerarla en "
            f"{TIMEOUT_VALORES}s. Sigue con lo que ya sabes del prompt, o "
            "acota por fecha en la consulta final."
        )

    return _listar(real, objeto["nombre"], [fila[0] for fila in filas])


# Columnas cuyos valores no son de ninguna cadena: listarlas completas a un
# usuario acotado no le enseña nada de las demas.
_DE_NADIE = {"brand", "carrier", "year", "month", "week", "region"}


def _listar(columna: str, objeto: str, valores: list) -> str:
    if not valores:
        return f"{columna} no tiene valores en {objeto}."
    lineas = "\n".join(f"  {v}" for v in valores[:MAX_VALORES])
    if len(valores) > MAX_VALORES:
        # Viene de la foto, que trae la lista completa: se puede decir cuantos
        # quedaron fuera.
        aviso = f"\n(solo los primeros {MAX_VALORES} de {len(valores)})"
    elif len(valores) == MAX_VALORES:
        # Viene de la base con TOP 50: puede haber mas, no hay forma de saber
        # cuantos sin pagar otra consulta.
        aviso = f"\n(solo los primeros {MAX_VALORES})"
    else:
        aviso = ""
    return f"Valores de {columna} en {objeto}:\n{lineas}{aviso}"


def _ejecutar_interrumpible(conexion, query: str, cancelacion) -> pd.DataFrame:
    """Corre el SELECT de forma que se pueda abortar a media ejecucion.

    Revisar la cancelacion entre herramientas no alcanza: la parte lenta de
    una consulta dificil es el SQL mismo, y mientras el driver espera a SQL
    Server no se pasa por ningun punto de control. En una prueba real, pedir
    otra cosa a los 12 segundos no cancelaba nada y la consulta vieja seguia
    hasta agotar los 90 del timeout.

    La solucion es `cursor.cancel()` de pyodbc, que esta pensado justo para
    esto: se puede llamar desde OTRO hilo mientras el cursor esta ocupado, y
    manda un SQLCancel al servidor. Un hilo vigilante espera el evento y
    cancela; si la consulta termina antes, el vigilante se apaga solo.

    Se usa el cursor crudo y no `exec_driver_sql` por la misma razon de
    siempre: el SQL viaja tal cual al driver, sin que SQLAlchemy interprete
    ":algo" como parametro de vinculo.
    """
    bruta = getattr(conexion.connection, "dbapi_connection", conexion.connection)
    cursor = bruta.cursor()

    termino = threading.Event()

    def vigilar():
        while not termino.wait(0.5):
            if cancelacion.is_set():
                try:
                    cursor.cancel()
                except Exception:  # noqa: BLE001 - ya no hay nada que salvar
                    logger.debug("cursor.cancel() fallo; la consulta seguira sola")
                return

    vigilante = None
    if cancelacion is not None and hasattr(cursor, "cancel"):
        vigilante = threading.Thread(target=vigilar, daemon=True)
        vigilante.start()

    try:
        cursor.execute(query)
        columnas = [c[0] for c in cursor.description or []]
        return pd.DataFrame.from_records(cursor.fetchall(), columns=columnas)
    except Exception:
        # Si se cancelo, el error del driver es consecuencia de eso y no un
        # problema de la consulta: no tiene sentido devolverselo al modelo
        # para que "corrija" nada.
        if cancelacion is not None and cancelacion.is_set():
            raise ConsultaCancelada() from None
        raise
    finally:
        termino.set()
        if vigilante is not None:
            vigilante.join(timeout=1)
        try:
            cursor.close()
        except Exception:  # noqa: BLE001
            pass


MAX_OFERTAS = 12

# La tabla de scraping de las tiendas en linea. Se consulta cuando el precio
# no esta en [GTM BI Database].dbo.[Prices], que es el precio interno.
TABLA_SCRAP = "[Scrap].dbo.[Complete_Scrap]"


@tool
def precio_en_internet(modelo: str, retailer: str = "") -> str:
    """Precio publicado hoy en la tienda en linea de cada retailer.

    Usala cuando el precio NO aparezca en [GTM BI Database].dbo.[Prices], que
    es el precio interno. Esto es otra cosa: lo que la tienda publica en su
    sitio, recogido por scraping, con su liga y su carrier.

    `modelo` es el nombre del telefono ("magic 8 lite", "x9c"). `retailer`
    opcional para acotar a uno ("Coppel", "Elektra", "Liverpool").
    """
    bitacora.paso(f"buscando el precio de {modelo!r} en el scraping de internet", bitacora.NOTA)
    tokens = _tokens_de_modelo(modelo)
    if not tokens:
        return "Dame al menos una palabra o numero del modelo."

    condiciones = " AND ".join(["[approved_name] LIKE ?"] * len(tokens))
    parametros = [f"%{t}%" for t in tokens]
    filtro_retail = ""
    if retailer.strip():
        filtro_retail = " AND [retail] LIKE ?"
        parametros.append(f"%{retailer.strip()}%")
    ctx = _contexto_actual()
    if ctx.cadenas:
        pedida = acceso.cadena_de(retailer)
        if pedida is not None and pedida not in ctx.cadenas:
            return _sin_permiso(ctx, acceso.Acotada("", fuera=[pedida]))
        filtro_retail += " AND " + acceso.condicion("[retail]", ctx.cadenas)

    # NOLOCK: el scraping reescribe esta tabla y, mientras lo hace, una
    # lectura normal espera a que termine. Medido el 7/10/2026: la misma
    # consulta no termino en 5 minutos y con NOLOCK tardo 7 segundos. Para
    # precios publicados en internet, leer a media carga es aceptable; quedarse
    # sin respuesta, no.
    #
    # De la fecha mas reciente que exista para ese modelo, el anuncio mas
    # barato de cada tienda y carrier. Sin el ROW_NUMBER salen decenas de
    # anuncios del mismo telefono en la misma tienda, que en un chat no se
    # pueden leer.
    sql = f"""
        WITH reciente AS (
            SELECT [retail], [carrier], [approved_name], [price],
                   [consultation_date], [url],
                   ROW_NUMBER() OVER (
                       PARTITION BY [retail], [carrier] ORDER BY [price]
                   ) AS orden
            FROM {TABLA_SCRAP} WITH (NOLOCK)
            WHERE {condiciones}{filtro_retail}
              AND [consultation_date] = (
                  SELECT MAX([consultation_date]) FROM {TABLA_SCRAP} WITH (NOLOCK)
                  WHERE {condiciones}{filtro_retail}
              )
        )
        SELECT TOP {MAX_OFERTAS} [retail], [carrier], [approved_name], [price],
               [consultation_date], [url]
        FROM reciente WHERE orden = 1 ORDER BY [price]
    """

    try:
        with ctx.engine.connect() as conexion:
            with _presupuesto_de_tiempo(conexion, _plazo_de_herramienta(ctx)):
                cursor = conexion.exec_driver_sql(sql, tuple(parametros) * 2)
                filas = cursor.fetchall()
    except Exception as error:  # noqa: BLE001
        logger.exception("Fallo la consulta de precios de internet")
        if _es_por_tiempo(error):
            return (
                "La tabla de precios de internet no contesto a tiempo (puede "
                "estar actualizandose). No reintentes: dile al usuario que "
                "lo intente en unos minutos."
            )
        return f"No pude consultar los precios publicados: {error}"

    if not filas:
        return (
            f"No hay precios publicados para {' + '.join(tokens)}"
            + (f" en {retailer}" if retailer.strip() else "")
            + ". Prueba con menos palabras del modelo."
        )

    ctx.hubo_datos = True
    ctx.de_internet = True
    fecha = filas[0][4]
    lineas = [f"Precios publicados en las tiendas en linea el {fecha}:"]
    for retail, carrier, nombre, precio, _, url in filas:
        lineas.append(
            f"  {retail} | {carrier or 'sin carrier'} | {nombre} | "
            f"${float(precio):,.2f}".replace(",", " ") + f" | {url}"
        )
    modelos = {fila[2] for fila in filas}
    lineas.append(
        "AL RESPONDER: di que es el precio publicado en internet ese dia (no "
        "el precio interno) -- la nota de la fuente la agrega el sistema al "
        "final, no la copies --, y para cada renglon incluye SIEMPRE el NOMBRE "
        "COMPLETO del modelo tal como aparece arriba, el carrier y la liga. "
        "Sin el carrier no se sabe si el precio es con plan o libre, y sin la "
        "liga el usuario no puede verificarlo."
    )
    if len(modelos) > 1:
        # El caso real: 256GB a $7 199 y 512GB a $9 999 en la misma tienda.
        # Presentarlos como "el Magic 8 Lite" hace parecer que el mismo
        # telefono cuesta muy distinto segun el carrier, cuando lo que cambia
        # es la capacidad.
        lineas.append(
            f"OJO: aqui hay {len(modelos)} versiones distintas del modelo "
            f"({', '.join(sorted(modelos))}). NO las presentes como si fueran "
            "el mismo telefono: la diferencia de precio es de version, no de "
            "tienda. Nombra la version en cada renglon."
        )
    texto = "\n".join(lineas)
    ctx.vistos.append(f"Precios publicados en internet de {modelo!r}:\n{texto}")
    return texto


MAX_MESES_PRONOSTICO = 12


# Cuantos meses proyecta el guardian de `consultar_sql` cuando la pregunta
# pedia un estimado. Los mismos que la herramienta por defecto: se pregunta
# por el mes que viene, y los dos siguientes dan de contexto sin costar nada.
MESES_DEL_GUARDIAN = 3


def _serie_mensual(df: pd.DataFrame) -> list[tuple[int, int, float]] | None:
    """La serie (anio, mes, total) de un resultado, o None si no lo es.

    Es estricta a proposito -- anio entre 1900 y 2200, mes entre 1 y 12 --
    porque tambien la usa el guardian de `consultar_sql` para decidir si lo
    que acaba de salir de la base es una serie mensual o cualquier otra tabla
    de tres columnas.
    """
    if df is None or df.shape[1] < 3:
        return None
    serie: list[tuple[int, int, float]] = []
    for fila in df.itertuples(index=False):
        if fila[0] is None or fila[1] is None or fila[2] is None:
            continue
        try:
            anio, mes, total = int(fila[0]), int(fila[1]), float(fila[2])
        except (TypeError, ValueError):
            return None
        if not (1900 <= anio <= 2200 and 1 <= mes <= 12):
            return None
        serie.append((anio, mes, total))
    return serie or None


def _registrar_base(ctx: "ContextoPeticion", resultado) -> None:
    """Guarda, en el idioma del usuario, la ficha de en que se baso.

    Se pidio que el mensaje diga de donde sale el numero. No se le encarga al
    modelo: la ficha se arma con los datos de la proyeccion y `responder` la
    pega al final. Asi dice siempre los mismos meses y el mismo margen que se
    usaron de verdad.
    """
    ctx.pronostico_hecho = True
    ctx.base_del_pronostico = idiomas.base_del_pronostico(
        ctx.idioma,
        meses_usados=resultado.meses_usados,
        desde=resultado.desde,
        hasta=resultado.hasta,
        estacional=resultado.confiable,
        ventana=resultado.ventana,
        error=f"{ERROR_TIPICO:.0%}",
        descartado=resultado.descartado,
    )


def _texto_del_pronostico(resultado) -> str:
    """El pronostico como se lo lee el modelo, con sus advertencias."""
    lineas = [f"Pronostico ({resultado.metodo}):"]
    for anio, mes, valor in resultado.meses:
        lineas.append(f"  {anio}-{mes:02d}: {valor:,.0f}".replace(",", " "))

    if resultado.descartado:
        anio, mes = resultado.descartado
        lineas.append(
            f"Se dejo fuera {anio}-{mes:02d} porque el mes todavia no termina; "
            "incluirlo habria arrastrado el pronostico hacia abajo."
        )
    if resultado.confiable:
        lineas.append(
            f"En pruebas contra meses ya ocurridos este metodo se equivoca "
            f"alrededor de {ERROR_TIPICO:.0%}. Dile al usuario el rango, no "
            "un numero exacto."
        )
    else:
        lineas.append(
            "OJO: no hay un anio completo de historia, asi que esto es solo "
            "tendencia y NO toma en cuenta la estacionalidad, que en estos "
            "datos es fuerte (diciembre casi duplica a febrero). Avisale al "
            "usuario que es una estimacion gruesa."
        )

    # La ficha con los meses, el metodo y el margen se la pega `responder` al
    # final del mensaje, ya traducida. Si ademas el modelo la contara con sus
    # palabras, el usuario la leeria dos veces.
    lineas.append(
        "La ficha con los meses de historia, el metodo y el margen se agrega "
        "sola al final del mensaje: NO la repitas, solo da el numero y su "
        "lectura."
    )
    return "\n".join(lineas)


@tool
def pronosticar_ventas(sql_historico: str, meses: int = 3) -> str:
    """Proyecta las ventas de los proximos meses.

    `sql_historico` es un SELECT que devuelve la serie mensual con TRES
    columnas en este orden: anio, mes y total. Una fila por mes, por ejemplo:
      SELECT YEAR([Día]), MONTH([Día]), SUM([Unidades])
      FROM [RAW_DATA_B2B].dbo.[COPPEL_B2B]
      GROUP BY YEAR([Día]), MONTH([Día])
    Trae la historia COMPLETA, sin filtrar por anio: el pronostico necesita el
    mismo mes del anio anterior para tener en cuenta la estacionalidad.
    """
    bitacora.paso(f"proyectando los proximos {meses} meses", bitacora.NOTA)
    meses = max(1, min(int(meses), MAX_MESES_PRONOSTICO))

    es_valida, motivo = _evaluar_sql(sql_historico)
    if not es_valida:
        return f"Ese SQL no es una consulta de lectura valida ({motivo})."
    evitar = (
        _objetos_a_evitar(sql_historico)
        or _tabla_cruda_para_responder(sql_historico)
        or _crudo_con_devoluciones(sql_historico)
    )
    if evitar:
        return evitar

    ctx = _contexto_actual()
    sql_historico = ventas_con_modelo.expandir(sql_historico, ctx.cadenas)
    if ctx.cadenas:
        acotada = acceso.acotar(sql_historico, ctx.cadenas)
        if acotada.bloqueada:
            return _sin_permiso(ctx, acotada)
        sql_historico = acotada.query
    try:
        with ctx.engine.connect() as conexion:
            with _presupuesto_de_tiempo(conexion, _plazo_de_herramienta(ctx)):
                df = _ejecutar_interrumpible(conexion, sql_historico, ctx.cancelacion)
    except ConsultaCancelada:
        raise
    except Exception as error:  # noqa: BLE001
        logger.exception("Fallo la consulta historica del pronostico")
        return f"No pude traer la historia: {error}"

    if df.shape[1] < 3:
        return (
            "La consulta debe devolver tres columnas: anio, mes y total. "
            f"Devolvio {df.shape[1]}."
        )

    serie = _serie_mensual(df)
    if serie is None:
        return (
            "No pude leer la serie: las columnas deben ser anio (numero), "
            "mes (1-12) y total (numero), en ese orden."
        )

    resultado = proyectar(serie, meses)
    if resultado is None:
        return (
            f"La historia trae {len(serie)} meses utiles y no alcanza para "
            "proyectar. Trae la serie completa, sin filtrar por anio."
        )

    _registrar_base(ctx, resultado)
    ctx.hubo_datos = True
    texto = _texto_del_pronostico(resultado)
    ctx.vistos.append(f"Historia mensual ({len(serie)} meses) y pronostico:\n{texto}")
    return texto


# Un desglose pedido con todas sus letras. Solo entonces el resultado de la
# consulta va como hoja aparte en un reporte de cadena: lo demas ya lo cubren
# el PIVOT, Datos y Raw_Data.
_PIDE_DESGLOSE = re.compile(
    r"\bpor\s+(tienda|sucursal|punto de venta|modelo|sku|regi[oó]n|zona|semana|mes|"
    r"d[ií]a|marca|cadena|carrier|canal|estado|ciudad|categor[ií]a)"
    r"|desglos|agrupad|\bby\s+(store|model|sku|region|week|month|day|brand|carrier)\b"
    r"|breakdown|按",
    re.IGNORECASE,
)


def pidio_desglose(pregunta: str) -> bool:
    return bool(_PIDE_DESGLOSE.search(pregunta or ""))


# Las hojas extra de un reporte ("Ventas por tienda" y las fichas de
# tiendas, regiones y carriers) son de TIENDAS: solo si se pidieron tiendas o
# regiones. Visto el 7/10/2026: "por cadena y por mes" salio con "Ventas por
# tienda", que nadie pidio, porque bastaba cualquier desglose.
_PIDE_TIENDAS = re.compile(
    r"tienda|sucursal|punto de venta|regi[oó]n|zona|\bstores?\b|\bregions?\b|门店|地区",
    re.IGNORECASE,
)


def pidio_detalle_de_tiendas(pregunta: str) -> bool:
    return bool(_PIDE_TIENDAS.search(pregunta or ""))


@tool
def semanas_de_inventario(modelo: str = "", marca: str = "HONOR") -> str:
    """El inventario de Telcel: cuanto hay, a que fecha y para cuanto alcanza.

    Usala SIEMPRE que pregunten por el inventario o el stock de Telcel, en
    cualquier forma o idioma: "como esta el inventario", "cuanto hay",
    "what's the inventory", "库存", semanas de inventario, cobertura, "para
    cuanto le alcanza", "cuanto le dura el stock" o sell-through.
    No lo calcules con `consultar_sql`: la cuenta ya esta definida por el
    negocio (stock de la foto mas reciente / venta semanal promedio de las
    ultimas 4 semanas, de todos los canales de Telcel) y aqui sale siempre
    igual.

    `modelo` es lo que dijo el usuario ("X6C", "MAGIC 8 LITE"); vacio para
    toda la marca. `marca` es la marca en mayusculas ("HONOR", "SAMSUNG");
    si nombran un modelo de otra marca, pasala. Solo existe para Telcel: de
    las demas cadenas no hay inventario en la base.
    """
    bitacora.paso(
        f"calculando semanas de inventario en Telcel ({marca} {modelo})".strip(),
        bitacora.NOTA,
    )
    ctx = _contexto_actual()
    if ctx.cadenas and "TELCEL" not in ctx.cadenas:
        return _sin_permiso(ctx, acceso.Acotada("", fuera=["TELCEL"]))
    try:
        resultado = inventario.calcular(
            ctx.engine, modelo, (marca or "").strip().upper(),
            plazo=_plazo_de_herramienta(ctx),
        )
    except Exception as error:  # noqa: BLE001
        logger.exception("Fallo el calculo de semanas de inventario")
        return f"No pude calcular las semanas de inventario: {error}"
    if resultado is None:
        return (
            f"No hay stock ni venta en Telcel que coincida con {marca} {modelo!r}. "
            "Revisa el nombre con `buscar_modelos` o pregunta al usuario."
        )
    ctx.hubo_datos = True
    texto = inventario.como_texto(resultado)
    if len(resultado.tabla) > 15:
        try:
            ctx.archivos.append(ExcelExporter.export_to_excel(resultado.tabla))
        except Exception:  # noqa: BLE001 - el texto ya trae el total y los principales
            logger.exception("No se pudo armar el Excel de semanas de inventario")
    # Con lo que se pidio: visto el 9/10/2026, el revisor rechazo una
    # respuesta correcta del X6c porque los datos no decian de que modelo eran.
    pedido = " ".join(x for x in ((marca or "").strip(), (modelo or "").strip()) if x) or "todos los modelos"
    ctx.vistos.append(f"Semanas de inventario de Telcel, pedido: {pedido}:\n{texto}")
    return texto


@tool
def consultar_sql(query: str, entregar_archivo: bool = False) -> str:
    """Ejecuta una consulta SELECT y devuelve los resultados al usuario.

    Usa SIEMPRE esta herramienta para obtener los datos finales que responden
    la pregunta. Con muchas filas arma sola el Excel y la grafica.

    Pon `entregar_archivo=True` cuando el usuario PIDA el archivo ("mandame el
    excel", "pasamelo en archivo", "descargable"): sin eso, un resultado corto
    se responde solo como texto y el usuario se queda esperando un adjunto que
    nunca llega.
    """
    es_valida, motivo = _evaluar_sql(query)
    if not es_valida:
        # El motivo especifico (comentario sin cerrar, keyword no permitida,
        # multiples sentencias, etc.) le dice al modelo QUE cambiar; un
        # mensaje generico invita a reintentar la misma consulta en bucle.
        return (
            "Error: solo se permiten consultas de lectura (SELECT o WITH), "
            f"y una sola sentencia por consulta ({motivo})."
        )

    mal_unida = (
        _tabla_cruda_para_responder(query)
        or _homologacion_mal_unida(query)
        or _homologacion_por_like(query)
        or _tiendas_sin_normalizar(query)
        or _crudo_con_devoluciones(query)
    )
    if mal_unida:
        bitacora.paso("la consulta contaria de mas o de menos", bitacora.NOTA)
        return mal_unida

    evitar = _objetos_a_evitar(query)
    if evitar:
        bitacora.paso("la consulta lee una tabla marcada como NO USAR", bitacora.NOTA)
        return evitar

    ctx = _contexto_actual()
    a_la_herramienta = (
        _inventario_telcel_a_mano(query, ctx.pregunta)
        or _capa_de_una_marca(query, ctx.pregunta)
    )
    if a_la_herramienta:
        bitacora.paso("inventario de Telcel a mano: va a la herramienta", bitacora.NOTA)
        return a_la_herramienta
    # Lo que queda del presupuesto de la peticion, menos un margen para armar
    # el Excel y la grafica. Sin esto una consulta pesada se lleva los 90
    # segundos enteros y el usuario no recibe NADA: ni datos ni explicacion,
    # solo silencio y un 504. Asi, en el peor caso el agente se entera a
    # tiempo y puede decirle que acote la pregunta.
    presupuesto = int(TIMEOUT_PETICION - (time.monotonic() - ctx.inicio) - MARGEN_DE_ENTREGA)
    if presupuesto < MINIMO_UTIL:
        # Antes aqui habia un piso de 10s, y era peor que no correr: ninguna
        # consulta contra la vista consolidada baja de eso, asi que el agente
        # reintentaba una y otra vez con un presupuesto que no le alcanzaba a
        # nada. En el log se ven cuatro intentos seguidos cortados a los 10s
        # mientras el usuario esperaba sin recibir nada.
        return (
            "Ya no queda tiempo en esta peticion para otra consulta. No "
            "reintentes: dile al usuario que la consulta resulto demasiado "
            "pesada y sugierele preguntar por un retailer o un periodo "
            "concretos."
        )
    # Identica a una que ya fallo en esta misma pregunta: no tiene sentido
    # mandarla otra vez. Paso en produccion -- el modelo escribio dos veces
    # `[status_of_promoter] IN ('promotor fijo','promotor doble')` contra una
    # columna float, recibio el mismo error de conversion las dos veces y se
    # rindio sin probar nada distinto.
    huella = " ".join(query.split()).casefold()
    fallo_antes = ctx.consultas_fallidas.get(huella)
    if fallo_antes:
        bitacora.paso("repitio una consulta que ya habia fallado", bitacora.NOTA)
        return (
            "Esta consulta es identica a una que ya fallo en esta misma "
            f"pregunta, con este error: {fallo_antes} No la repitas. Cambia "
            "algo concreto -- el tipo del valor que comparas, la columna, la "
            "tabla -- y si no sabes que hay del otro lado, miralo primero "
            "con `describir_objeto` o `valores_distintos`."
        )

    # Un filtro cuyo valor no existe en esa columna no da error: da cero
    # filas, y si la consulta trae otras cadenas ni siquiera viene vacia.
    # `[Retail] IN ('LIVERPOOL')` se lleva Liverpool entero en silencio.
    # Media cadena: Coppel y Liverpool viven partidos en dos, y consultar uno
    # solo da de menos sin dar error. Esto BLOQUEA en vez de avisar, y no
    # empezo asi: primero se pegaba un "OJO:" al resultado, el modelo lo leyo
    # y lo ignoro -- ya habia gastado dos minutos y tenia el Excel armado, y
    # contesto igual con el 83% de Coppel. Un aviso es opcional; un numero
    # mal por el 17% no puede depender de que al modelo le parezca bien.
    # Las VISTAS de Coppel y Liverpool se completan solas con su otra mitad;
    # lo que quede sin completar (las tablas crudas) se bloquea abajo.
    # La consulta tal como la escribio el modelo: el resumen local completa
    # Coppel y Liverpool a su manera (ver resumen_local.traducir).
    pedida = query
    query, completadas = completar_media_cadena(query)
    if completadas:
        bitacora.paso(
            f"completo la cadena: {'; '.join(completadas)}", bitacora.NOTA
        )

    promotoria = avisos_de_promotoria(query, ctx.pregunta)
    if promotoria:
        bitacora.paso("la consulta de promotoria contaria de mas", bitacora.NOTA)
        return "No la ejecute. " + " ".join(promotoria)

    media_cadena = avisos_de_media_cadena(query)
    if media_cadena:
        bitacora.paso("la consulta es solo parte de su cadena", bitacora.NOTA)
        return "No la ejecute. " + " ".join(media_cadena)

    no_casan = filtros_que_no_casan(query)
    if no_casan:
        bitacora.paso("un filtro no casa con ningun valor de la columna", bitacora.NOTA)
        return (
            "No la ejecute: asi como esta, ese filtro no deja pasar ninguna "
            "fila y el resultado saldria incompleto sin avisar. "
            + " ".join(no_casan)
            + " Corrige el valor y vuelve a mandarla."
        )

    # La etiqueta del resultado se saca de la consulta tal como la pidio el
    # modelo: si hubo que reconstruir la consolidada, listar sus nueve vistas
    # solo confundiria.
    consulta_para_etiqueta = query
    con_repuesto = False

    # La vista virtual con el modelo (ver ventas_con_modelo.py): se cambia
    # por la union real aqui, despues de los guardianes (que leen lo que
    # escribio el modelo) y antes de los permisos.
    query = ventas_con_modelo.expandir(query, ctx.cadenas)

    # Las cadenas de quien pregunta: aqui y no antes, porque los guardianes
    # de arriba leen la consulta como la escribio el modelo. Ver acceso.py.
    acotada = None
    if ctx.cadenas:
        acotada = acceso.acotar(query, ctx.cadenas)
        if acotada.bloqueada:
            bitacora.paso(
                "la consulta lee cadenas que este usuario no tiene: "
                + ", ".join(acotada.fuera + acotada.desconocidos),
                bitacora.NOTA,
            )
            return _sin_permiso(ctx, acotada)
        query = acotada.query
        local = acceso.acotar(pedida, ctx.cadenas, local=True)
        pedida = None if local.bloqueada else local.query
        if acotada.acotados:
            bitacora.paso(
                f"limite {', '.join(acotada.acotados)} a {acceso.nombres(ctx.cadenas)}",
                bitacora.NOTA,
            )
    # Primero el resumen local: si la consulta solo necesita unidades por
    # cadena, marca, modelo, carrier y periodo, se contesta en un segundo en
    # vez de minutos. Si no aplica o falla, None, y va al servidor como
    # siempre. Ver resumen_local.py.
    comenzo = time.monotonic()
    desde_el_resumen = resumen_local.consultar(pedida) if pedida else None
    if desde_el_resumen is not None:
        df, sql_local = desde_el_resumen
        bitacora.paso(f"SQL (resumen local): {' '.join(sql_local.split())}", bitacora.SQL)
    else:
        if "ETL_Consolidado_B2B".casefold() in query.casefold():
            repuesto = consolidada_de_repuesto(query)
            if repuesto is not None and consolidada_trae_basura(ctx.engine):
                bitacora.paso(
                    "la vista consolidada trae cantidades que no son numeros; "
                    "la reconstruyo desde sus vistas",
                    bitacora.NOTA,
                )
                query, con_repuesto = repuesto, True

        comenzo = time.monotonic()
        bitacora.paso(f"SQL: {' '.join(query.split())}", bitacora.SQL)
        try:
            with ctx.engine.connect() as conexion:
                with _presupuesto_de_tiempo(conexion, presupuesto):
                    try:
                        df = _ejecutar_interrumpible(conexion, query, ctx.cancelacion)
                    except ConsultaCancelada:
                        raise
                    except Exception as primera:
                        # La consolidada rota en el servidor: se reintenta UNA vez
                        # con ella reconstruida desde sus vistas. Solo ante ese
                        # error -- en cuanto la arreglen, se vuelve a usar la real.
                        repuesto = (
                            consolidada_de_repuesto(query)
                            if _vista_rota_en_el_servidor(primera, query)
                            else None
                        )
                        if repuesto is None:
                            raise
                        bitacora.paso(
                            "la vista consolidada esta rota en el servidor; "
                            "reintento reconstruyendola desde sus vistas",
                            bitacora.NOTA,
                        )
                        query, con_repuesto = repuesto, True
                        df = _ejecutar_interrumpible(conexion, query, ctx.cancelacion)
        except ConsultaCancelada:
            raise
        except Exception as error:  # noqa: BLE001 - el mensaje (resumido) vuelve al modelo
            # El error completo -con nombres de tabla/columna y detalle del
            # driver ODBC- se registra aqui, en el log del servidor, que es
            # donde debe quedar (Importante de la revision final). Al modelo
            # solo le llega el texto de abajo, y el system_prefix de
            # _create_agent le prohibe citarlo tal cual al usuario.
            tardo = time.monotonic() - comenzo
            logger.exception(
                "Error al ejecutar SQL tras %.0fs (presupuesto %ss): %s",
                tardo,
                presupuesto,
                " ".join(query.split())[:400],
            )
            bitacora.paso(f"SQL fallo tras {tardo:.0f}s: {str(error)[:200]}", bitacora.ERROR)
            ctx.consultas_fallidas[huella] = str(error)[:300]
            if _es_por_tiempo(error) or tardo >= presupuesto:
                # Se agoto el presupuesto, no es un error de sintaxis: decirselo
                # tal cual para que acote en vez de reintentar lo mismo.
                return (
                    f"La consulta tardo mas de {presupuesto}s y la cancele para no "
                    "dejar al usuario sin respuesta. Acotarla por fecha NO va a "
                    "ayudar (medido: un SUM de un solo mes tarda lo mismo). Si "
                    "filtraste ETL_Consolidado_B2B por [Retail], ESE es el "
                    "problema: repite la consulta sobre la vista de ese retailer "
                    "(vw_ETL_Elektra, vw_ETL_TELCEL_B2B, vw_ETL_Sears...), que "
                    "trae las mismas columnas y no recorre las otras ocho. OJO con "
                    "Coppel y Liverpool: cada uno vive en DOS vistas y una sola da "
                    "de menos, asi que ahi va UNION ALL de las dos "
                    "(vw_ETL_COPPEL con vw_ETL_COPPEL_IOT, vw_ETL_Liverpool con "
                    "vw_ETL_Liverpool_IOT)."
                )
            return (
                f"Error al ejecutar la consulta: {error}"
                f"{_vista_rota_en_el_servidor(error, query)}"
                f"{_donde_vive_de_verdad(error)}"
                f"{_columnas_que_si_existen(error, query)}"
            )

    tardo = time.monotonic() - comenzo
    logger.info(
        "SQL ok en %.0fs, %d filas%s: %s",
        tardo,
        len(df),
        " (resumen local)" if desde_el_resumen is not None else "",
        " ".join(query.split())[:400],
    )
    bitacora.paso(f"-> {len(df)} filas en {tardo:.0f}s", bitacora.RESULTADO)
    ctx.hubo_datos = True
    # El modelo homologado junto a la descripcion, SIEMPRE (pedido el
    # 8/10/2026): lo ve el modelo, va en el Excel y el pivot lo puede usar.
    df = homologacion.con_modelo(df)
    resultado = formatear_resultado(df)

    # La grafica ya NO depende de que haya Excel. Antes iba dentro del `if`
    # de abajo, y por eso a un "muestrame una grafica de lineas del top de
    # modelos" no llegaba ninguna imagen: el agente acoto a 10 filas, esas 10
    # caben en texto, no hacia falta Excel... y con el Excel se cayo tambien
    # la grafica, que era lo unico que se habia pedido.
    #
    # Se dibuja cuando el reporte ya se va a mandar de todos modos, o cuando
    # la PREGUNTA pedia una grafica. Lo segundo se decide leyendo la pregunta
    # y no la bandera del modelo, que es justo la que no se marco. Un
    # resultado corto que nadie pidio ver graficado sigue sin adjunto: mandar
    # una imagen de tres filas a un chat es ruido.
    imagen = motivo_sin_grafica = None
    quiere_grafica = pidio_grafica(ctx.pregunta)
    if not df.empty and (
        resultado.necesita_excel or entregar_archivo or quiere_grafica
    ):
        try:
            imagen, motivo_sin_grafica = grafica_del_resultado(df)
        except Exception:  # noqa: BLE001
            # Que falle no cuesta el reporte: los datos son lo que se pidio.
            logger.exception("No se pudo generar la grafica; va sin ella")

    # `not df.empty`: sin filas no hay nada que exportar, y pedir el archivo
    # no puede convertir un "sin resultados" en un error de exportacion.
    excel = None
    contexto: dict = {}
    crudo_partido = False
    crudos_agrupados = False
    solo_crudos = False
    reporte_de_cadena = False
    con_datos = False
    zip_del_crudo = None
    pivotes: list[tuple[str, str, dict, str]] = []
    if not df.empty and (resultado.necesita_excel or entregar_archivo):
        # La ficha de las tiendas, modelos y carriers que salieron, para que
        # el Excel se entienda solo. Son catalogos chicos; si alguno falla, el
        # reporte sale igual con la hoja de datos.
        try:
            # Lo que de verdad queda, no un numero fijo: si el desglose
            # pidiera mas de lo que resta, la peticion entera se caeria por
            # timeout y el usuario no recibiria NADA -- ni el Excel ni el
            # texto. Asi, en el peor caso sale sin esa hoja.
            queda = int(TIMEOUT_PETICION - (time.monotonic() - ctx.inicio) - MARGEN_DE_ENTREGA)
            contexto = contexto_del_resultado(
                df, ctx.engine, query, queda, cadenas=ctx.cadenas,
                extras=pidio_detalle_de_tiendas(ctx.pregunta),
            )
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo armar el contexto; va solo la tabla")
            contexto = {}

        # El revisor de archivos: permisos, alcance y cuadre de cada hoja
        # antes de que se escriba nada (ver revisor.py).
        revision = revisor.revisar_hojas(df, contexto, cadenas=ctx.cadenas, query=query)
        contexto = revision.contexto
        for hallazgo in revision.hallazgos:
            logger.warning("Revisor de archivos: %s", hallazgo)
            bitacora.paso(f"revisor de archivos: {hallazgo}", bitacora.NOTA)

        try:
            # Reporte de una cadena (contexto trae su propia hoja "Datos", lo
            # agrupado, y "Raw_Data", lo completo): el resultado de la consulta
            # no va como hoja, salvo que el usuario haya pedido un desglose
            # ("por tienda", "por modelo"...), y entonces va al final como
            # "Consulta". En cualquier otro reporte, la hoja "Datos" sobra
            # cuando el resultado ya llego entero en el mensaje.
            crudo = contexto.get("Raw_Data")
            solo_crudos = crudo is not None and bool(crudo.attrs.get("solo_crudos"))
            reporte_de_cadena = "Datos" in contexto or solo_crudos
            if (
                reporte_de_cadena
                and len(df) > 1
                and pidio_desglose(ctx.pregunta)
            ):
                if solo_crudos:
                    # Con solo el crudo, el desglose que se pidio va PRIMERO:
                    # visto el 9/10/2026, "Honor por tienda en Elektra" llego
                    # sin la tabla por tienda, solo con el crudo -- y detras
                    # de cientos de miles de filas nadie la encontraria.
                    contexto = {"Consulta": df, **contexto}
                else:
                    contexto["Consulta"] = df
            # Si el resultado lleva pivot (ver config_generica), la hoja Datos
            # va siempre: es de donde sale.
            con_datos = not reporte_de_cadena and (
                not (contexto and len(df) <= MAX_ROWS_AS_TEXT)
                or excel_pivot.config_generica(df) is not None
            )
            excel = ExcelExporter.export_to_excel(
                revision.df,
                contexto=contexto,
                con_datos=con_datos,
                # Muchos datos: solo el crudo, y con el escritor rapido.
                rapido=solo_crudos,
            )
        except Exception as error:  # noqa: BLE001
            logger.exception("Fallo la exportacion a Excel")
            return f"Obtuve {len(df)} registros pero fallo el Excel: {error}"

        # El PIVOT de verdad: se arma con Excel por COM SOBRE el archivo ya
        # guardado, asi que va despues de escribirlo, nunca antes. Que Excel
        # no este disponible, tarde de mas, o falle no puede costar el
        # reporte: "Datos" y el resto de las hojas ya estan listas.
        #
        # Solo si este reporte va a ser el que se entrega. Medido en
        # produccion: un turno reintento la consulta 4 veces (un error de
        # SQL propio del modelo) y armo el pivot las 4, aunque
        # `registrar_reporte` solo se queda con el PRIMERO -- 3 pases de
        # Excel por COM tirados, ~20s perdidos. La condicion es la misma que
        # usa `registrar_reporte` para decidir si este reporte reemplaza al
        # que ya habia: el primero de la vuelta, o uno que trae grafica
        # cuando el que ya se tiene no la trae.
        # Ya no se sabe aqui cual se entrega (lo decide entregar_reporte con
        # la respuesta): los pivots se apuntan y se arman solo al elegido.
        sera_el_reporte_bueno = True
        # El pivot sale de Raw_Data (lo completo); si no cupo, de Datos (lo
        # agrupado, con las columnas Tienda/Marca/Mes/Unidades).
        hoja_del_pivot = "Raw_Data" if "Raw_Data" in contexto else "Datos"
        crudos = contexto.get(hoja_del_pivot) if "Datos" in contexto else None
        if solo_crudos:
            crudos = None  # muchos datos: sin pivot, solo el crudo
        crudos_agrupados = (
            "Raw_Data" not in contexto
            and "Datos" in contexto
            and bool(contexto["Datos"].attrs.get("agrupado"))
        )
        # No cupo en Excel: el detalle completo va aparte, en un ZIP de CSV.
        if crudos_agrupados:
            zip_del_crudo = contexto["Datos"].attrs.get("zip")
        # Un pivot solo puede mirar UNA hoja. Si el crudo no cupo en una, la
        # hoja "Raw_Data" es apenas el primer trozo y el pivot estaria
        # resumiendo una parte sin decirlo -- justo lo que se viene
        # corrigiendo. Antes que un resumen a medias, ninguno.
        crudo_partido = crudos is not None and len(crudos) > MAX_FILAS_POR_HOJA_DE_EXCEL
        if crudo_partido:
            bitacora.paso(
                "el crudo ocupa varias hojas; va sin pivot para no resumir solo una",
                bitacora.NOTA,
            )
        # Un reporte que no es de una cadena entera: si el resultado trae una
        # dimension (tienda, modelo, region, cadena...), un periodo y una
        # medida, su pivot sobre la hoja Datos -- como el de tiendas por mes
        # que armo a mano el usuario el 7/10/2026 (ver config_generica).
        if (
            sera_el_reporte_bueno
            and crudos is None
            and not solo_crudos
            and con_datos
            and len(df) <= MAX_FILAS_POR_HOJA_DE_EXCEL
        ):
            config = excel_pivot.config_generica(df)
            if config:
                pivotes.append((
                    excel, "Datos", config,
                    f"{' y '.join(map(str, config['filas']))} por {' y '.join(map(str, config['columnas']))}",
                ))
        if (
            sera_el_reporte_bueno
            and not crudo_partido
            and crudos is not None
            and not crudos.empty
        ):
            config = excel_pivot.config_ventas_por_tienda(crudos)
            if config:
                pivotes.append((excel, hoja_del_pivot, config, "tienda y mes"))

    # La grafica va primero: en el chat se ve de un vistazo, y el Excel queda
    # debajo para quien quiera el detalle.
    principal = cifra_principal(df) if excel else None
    ctx.registrar_reporte(
        imagen, excel, zip_del_crudo, cifras=cifras_del_resultado(df), pivotes=pivotes,
        principal=principal,
    )

    # Que mas se puede preguntar sobre esto. Se queda con las de la PRIMERA
    # consulta, que es la que responde la pregunta: las de despues salen de
    # comprobaciones y ofrecerian el detalle de una tabla que el usuario
    # nunca vio.
    if not ctx.sugerencias:
        ctx.sugerencias = sugerir(ctx.pregunta, df, query)

    partes = [resultado.texto]

    # Cada resultado dice de donde salio. Visto el 30 de septiembre de 2026:
    # el modelo mando siete consultas en paralelo, una por cadena, y cada una
    # le volvio como un numero pelado ("8383.0"). Dos de las siete fueron
    # bloqueadas, y al repartir los numeros por orden corrio todo un lugar:
    # a Coppel le puso el de Elektra, a Elektra el de AT&T... y lo contesto
    # como si nada. Con la etiqueta pegada al numero no hay orden que valga.
    if completadas:
        partes.append(
            "Nota: la consulta se completo con la otra mitad de la cadena "
            f"({'; '.join(completadas)}), asi que este numero ya es la cadena "
            "ENTERA. No hace falta consultar la otra vista."
        )

    # Reporte de una cadena: el Excel se arma con la tabla completa de esa
    # cadena, no con lo que devolvio la consulta del modelo. Antes el mensaje
    # citaba las filas de esa consulta (el mismo Excel de AT&T se anuncio con
    # 103 852 y con 51 869); despues las del archivo. Pedido el 7/10/2026:
    # ni unas ni otras -- el mensaje habla de ventas, no del archivo. Lo que
    # se le da al modelo es la cifra de negocio del periodo.
    ficha = ficha_del_archivo(contexto) if excel and reporte_de_cadena else None
    if ficha:
        partes[0] = partes[0].replace(
            f"La consulta devolvio {len(df):,} registros; van completos en el "
            "Excel que se le adjunta al usuario. ",
            f"Tu consulta devolvio {len(df):,} filas; ese numero es solo para "
            "ti, no lo menciones. ",
        )
        if ficha.get("unidades") is not None:
            partes.append(
                f"TOTAL DEL PERIODO en esta cadena, todas las marcas: "
                f"{ficha['unidades']:,.0f} unidades. Si das un total, es este."
            )

    if acotada is not None and acotada.acotados:
        partes.append(
            f"Nota: este resultado trae SOLO {acceso.nombres(ctx.cadenas)}, las "
            "cadenas a las que este usuario tiene acceso. Si hablas de un "
            "total o del mercado, aclara que es de esas cadenas."
        )

    if con_repuesto:
        partes.append(
            "Nota: la vista ETL_Consolidado_B2B esta rota en el servidor, asi "
            "que este resultado sale de sus nueve vistas unidas, que dan "
            "exactamente los mismos numeros. Puedes seguir usando "
            "ETL_Consolidado_B2B normalmente: cada consulta se reconstruye "
            "sola. Mencionale al usuario, en una linea, que la vista "
            "consolidada esta fallando en el servidor."
        )

    objetos = _objetos_consultados(consulta_para_etiqueta)
    if objetos:
        partes[0] = f"Resultado de la consulta a {', '.join(objetos)}:\n{partes[0]}"

    # El mes que va a medio cargar. Sin esto, "ventas por mes" contesta que
    # septiembre lleva 15 584 unidades contra 160 909 de agosto y nadie
    # aclara que el mes va por la mitad: el numero es correcto y la lectura
    # es un desplome que no ocurrio.
    # Como va armado el archivo (solo el detalle, un ZIP aparte, varias
    # hojas) ya no se le cuenta al usuario: pedido el 7/10/2026, el mensaje
    # habla de ventas y el adjunto se explica solo. La unica excepcion es
    # cuando el detalle NO va: eso si cambia lo que recibe.
    if crudos_agrupados and not zip_del_crudo:
        partes.append(
            "OJO: el detalle de cada venta de este periodo es demasiado grande "
            "para mandarlo por WhatsApp; va resumido por tienda, marca y mes. "
            "Si el usuario pidio el detalle, dile en una frase sencilla que "
            "para tenerlo completo acote el periodo (un mes, por ejemplo), sin "
            "hablar de hojas ni archivos."
        )

    sin_modelo = _venta_sin_modelo(df)
    if sin_modelo:
        partes.append(sin_modelo)

    sin_cargar = cadenas_sin_el_periodo(query)
    if sin_cargar:
        partes.append(sin_cargar)
    if principal:
        # Visto el 9/10/2026: el Excel de Honor por tienda en Elektra se
        # entrego con "Ya quedo: ventas de Honor por tienda...", sin una cifra.
        partes.append(
            f"La cifra principal de este resultado es {principal[1]:,.0f} "
            f"(total de [{principal[0]}]). Si este es el reporte que entregas, "
            "empieza el mensaje con ella y lo mas importante (la tienda, el "
            "modelo o el mes que mas pesa); un mensaje que solo dice que el "
            "reporte esta listo no sirve."
        )
    por_mes = semana_de_cadena_mensual(query)
    if por_mes:
        partes.append(por_mes)
    # Visto el 9/10/2026, en espanol y en ingles: "12 unidades sin cadena" y
    # un share de 20% para ellas. Son de AT&T; la nota del catalogo no basto.
    if any(
        df[c].astype(str).str.strip().str.lower().eq("unknown").any()
        for c in df.columns
        if re.search(r"retail|cadena", str(c), re.IGNORECASE)
        and not pd.api.types.is_numeric_dtype(df[c])
    ):
        partes.append(
            "OJO: 'Unknown' en [Retail] son ventas de AT&T sin canal. Sumalas a "
            "AT&T al responder y NO las presentes como otra cadena ni les "
            "calcules share aparte."
        )

    incompleto = tiempo.ultimo_periodo_incompleto(df)
    if incompleto:
        partes.append(
            f"OJO: {incompleto} todavia va incompleto -- el periodo no ha "
            "terminado de cargar, por eso sale mucho mas bajo. Dilo al "
            "responder y NO lo presentes como una caida."
        )

    # Solo cuando la pidieron: se le dice al agente POR QUE no hay grafica y
    # que tendria que cambiar para que la haya. Sin esto no se entera de que
    # no la hay y le promete al usuario una imagen que nunca se adjunto. Y
    # solo cuando la pidieron, porque a un reporte que nadie queria ver
    # graficado este aviso lo unico que le agrega es una consulta de mas.
    if quiere_grafica and motivo_sin_grafica:
        partes.append(f"No pude dibujar la grafica que te pidieron: {motivo_sin_grafica}.")

    # Se pidio un estimado y lo que acaba de salir de la base ES la serie
    # mensual: la proyeccion se calcula aqui, con los datos ya en la mano.
    #
    # En produccion el usuario pidio "un estimado de las ventas del siguiente
    # mes en Liverpool": el agente encontro la tabla, corrigio la columna,
    # trajo la serie de 32 meses... y nunca llamo a `pronosticar_ventas`.
    # Contesto "descarga el Excel y procede con la proyeccion". El usuario
    # pidio un pronostico y recibio deberes. Que se haga la cuenta no puede
    # depender de que el modelo elija la herramienta; los numeros ya estan
    # aqui y la cuenta no vuelve a tocar la base.
    if not ctx.pronostico_hecho and pidio_pronostico(ctx.pregunta):
        serie = _serie_mensual(df)
        proyeccion = proyectar(serie, MESES_DEL_GUARDIAN) if serie else None
        if proyeccion is not None:
            bitacora.paso(
                "la pregunta pedia un estimado: proyectando la serie que "
                "acaba de salir",
                bitacora.NOTA,
            )
            _registrar_base(ctx, proyeccion)
            partes.append(_texto_del_pronostico(proyeccion))

    salida = "\n\n".join(partes)
    ctx.vistos.append(f"Consulta: {' '.join(consulta_para_etiqueta.split())[:1500]}\n{salida}")
    return salida


class Observador(BaseCallbackHandler):
    """Escucha al agente y anota en la bitacora todo lo que va haciendo.

    Antes solo se veia lo que las herramientas reportaban por su cuenta: el
    SQL y poco mas. Entre una consulta y la siguiente el panel se quedaba en
    silencio durante minutos, que es justo cuando uno se pregunta si el bot
    sigue vivo. Con esto se ve la vuelta entera -- cuando le pregunta al
    modelo, que decidio hacer, con que argumentos y que le contestaron --,
    en vivo y guardado con el turno.

    No interrumpe ni cambia nada: solo mira. Cualquier error suyo se traga,
    porque una anotacion no puede tumbar una consulta que si esta
    funcionando.
    """

    def __init__(self):
        self._inicio_del_modelo: float | None = None

    # --- el modelo ------------------------------------------------------

    def on_chat_model_start(self, serialized, messages, **kwargs):
        self._inicio_del_modelo = time.monotonic()
        bitacora.paso("preguntandole al modelo que hacer", bitacora.PENSAMIENTO)

    on_llm_start = on_chat_model_start

    def on_llm_end(self, response, **kwargs):
        tardo = (
            f" ({time.monotonic() - self._inicio_del_modelo:.0f}s)"
            if self._inicio_del_modelo
            else ""
        )
        texto = ""
        try:
            texto = (response.generations[0][0].text or "").strip()
        except (AttributeError, IndexError):
            pass
        # Con las herramientas de OpenAI el modelo suele contestar solo con
        # la llamada, sin texto; cuando si escribe algo, eso es lo mas
        # parecido a su razonamiento y vale la pena verlo.
        if texto:
            bitacora.paso(f"el modelo dice{tardo}: {texto}", bitacora.PENSAMIENTO)
        else:
            bitacora.paso(f"el modelo decidio que hacer{tardo}", bitacora.PENSAMIENTO)

    def on_llm_error(self, error, **kwargs):
        bitacora.paso(f"el modelo fallo: {str(error)[:300]}", bitacora.ERROR)

    # --- las herramientas -----------------------------------------------

    def on_agent_action(self, action, **kwargs):
        bitacora.paso(
            f"usa {action.tool} con {_resumir(action.tool_input)}", bitacora.HERRAMIENTA
        )

    def on_tool_end(self, output, **kwargs):
        bitacora.paso(f"le devolvio: {_resumir(output)}", bitacora.RESULTADO)

    def on_tool_error(self, error, **kwargs):
        bitacora.paso(f"la herramienta fallo: {str(error)[:300]}", bitacora.ERROR)

    def on_agent_finish(self, finish, **kwargs):
        bitacora.paso("ya tiene la respuesta", bitacora.PENSAMIENTO)


def _resumir(valor) -> str:
    """Un dict o un texto largo, en una linea legible."""
    if isinstance(valor, dict):
        return ", ".join(f"{k}={_resumir(v)}" for k, v in valor.items())
    texto = " ".join(str(valor).split())
    return texto if len(texto) <= 600 else texto[:600] + " [...]"


class SQLAgent:
    """Un agente Text-to-SQL. Una instancia por conversacion de WhatsApp.

    El historial vive en la instancia y no en un almacen aparte porque
    `RegistroSesiones` ya crea un SQLAgent por chat_id y serializa sus
    consultas con un lock por conversacion: no hay dos hilos tocando el mismo
    historial, y cuando la sesion vence -- a medianoche -- el historial se va con ella.
    """

    # Cuantos mensajes del historial se le repiten al modelo. El prompt ya
    # carga el catalogo del servidor, y cada pregunta arrastra ademas sus
    # llamadas a herramientas, asi que el historial no puede crecer sin tope.
    # Diez mensajes son cinco preguntas con su respuesta: suficiente para
    # sostener un "y de Elektra?" sin encarecer cada consulta.
    MAX_MENSAJES_HISTORIAL = 10

    def __init__(self, engine: Engine | None = None):
        self.engine = engine if engine is not None else self._crear_engine()
        # El catalogo de modelos para la columna [Modelo] de los resultados
        # (ver homologacion.py); se lee en segundo plano.
        homologacion.configurar(self.engine)
        self.llm = self._init_llm()
        self.historial: list[BaseMessage] = []
        # El idioma se sostiene entre turnos: los mensajes de WhatsApp son
        # cortos y muchos ("top 10", "y de Elektra?") no tienen idioma propio.
        self.idioma = idiomas.POR_DEFECTO
        # Las cadenas con las que se contesto el turno anterior. Si se le
        # QUITA alguna (en el panel, a media conversacion) el historial se
        # olvida: traeria respuestas con datos que ya no puede ver. Si se le
        # dan mas, se conserva (ver `responder`).
        self.cadenas: frozenset[str] | None = None
        self.agent_executor = self._create_agent()

    @staticmethod
    def _crear_engine() -> Engine:
        db_uri = os.getenv("DB_URI")
        if not db_uri:
            raise ValueError("La variable de entorno DB_URI no esta configurada.")
        return create_engine(db_uri, pool_pre_ping=True)

    def _init_llm(self) -> ChatOpenAI:
        """Inicializa el modelo de lenguaje usando OpenRouter."""
        openrouter_api_key = os.getenv("OPENROUTER_API_KEY")
        if not openrouter_api_key:
            raise ValueError("La variable de entorno OPENROUTER_API_KEY no esta configurada.")

        model_name = os.getenv("OPENROUTER_MODEL", "openai/gpt-6-luna")
        return ChatOpenAI(
            temperature=0,
            model=model_name,
            api_key=openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            # Ver stream_runnable en _create_agent. Sin streaming, el cliente
            # de openai reintenta solo si la conexion se cae.
            disable_streaming=True,
            max_retries=3,
            default_headers={
                "HTTP-Referer": "http://localhost",
                "X-Title": "SQL Agent",
            },
        )

    def _create_agent(self) -> Any:
        catalogo = indice()

        system_prefix = f"""
        Eres un experto analista de datos que responde por WhatsApp. Tu trabajo es
        interactuar con un servidor Microsoft SQL Server.

        ESQUEMA DEL SERVIDOR:
        La conexion abre en `master`, que NO contiene datos de negocio. Todo lo
        consultable vive en las cinco bases de abajo, asi que SIEMPRE debes
        escribir nombres de tres partes: [Base de datos].dbo.[Objeto]. Por
        ejemplo: SELECT TOP 10 * FROM [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B].
        Lo que en el indice dice "NO USAR" da otro numero, uno viejo, o no
        termina: no lo consultes, ni para comprobar.
        {catalogo}

        REGLAS CRITICAS:
        1. SOLO DEBES EJECUTAR SENTENCIAS `SELECT`.
        2. BAJO NINGUNA CIRCUNSTANCIA debes ejecutar comandos DML o DDL (`INSERT`,
           `UPDATE`, `DELETE`, `DROP`, `CREATE`, `ALTER`, etc.).
        3. Si el usuario pide modificar o borrar informacion, niegate educadamente
           explicando que solo tienes acceso de lectura.
        4. Para obtener los datos finales que responden la pregunta usa SIEMPRE la
           herramienta `consultar_sql`. La grafica la arma sola siempre que el
           resultado se pueda graficar, y con muchas filas arma ademas el Excel.
           Para una grafica de evolucion, devuelve las columnas del periodo
           ([Año], [Mes] o la fecha) JUNTO CON la columna que separa las
           lineas (el modelo, el retailer) y una sola medida: se dibuja una
           linea por cada modelo a lo largo de los meses. No mas de 6 lineas.
           Si el usuario PIDE el archivo ("mandame el excel", "pasamelo en
           archivo", "descargable"), llamala con `entregar_archivo=True`: un
           resultado corto se responde solo como texto, y sin eso el usuario se
           queda esperando un adjunto que nunca llega.
           `consultar_sql` es SOLO para la respuesta final, nunca para explorar.
           Cualquier pregunta del inventario de Telcel (cuanto hay, como
           esta, semanas, cobertura, "para cuanto le alcanza"), en cualquier
           idioma: `semanas_de_inventario`, nunca un SELECT propio.
           Para explorar tienes tres herramientas que NO generan reporte:
           `describir_objeto` (que columnas hay), `buscar_modelos` (nombres de
           celular) y `valores_distintos` (como estan escritos los valores de una
           columna). El archivo que recibe el usuario es el de tu PRIMERA
           llamada a `consultar_sql`: haz ahi la consulta completa, con todas
           las columnas que te pidieron. Cualquier comprobacion va ANTES, con
           las herramientas de exploracion, nunca como un segundo
           `consultar_sql`.
           Si el usuario pide una columna mas ("agregale el modelo"), vuelve a
           correr UNA sola consulta con esa columna agregada a las que ya tenia.
           Nunca la traigas por separado: dos consultas dan dos tablas a medias
           -- las ventas sin el modelo y los modelos sin las ventas -- y ninguna
           responde lo que se pidio.
        5. Respondes en un telefono: se breve. No repitas la tabla de resultados en
           tu respuesta final, resumela en una o dos frases.
           Contesta con lo que se pidio, como lo diria una persona: las
           cifras del negocio (unidades, share, precios, promotores, semanas
           de inventario). NUNCA hables de como viene el dato: ni filas, ni
           registros, ni hojas, ni columnas, ni Excel, ZIP o CSV, ni "te
           adjunto el archivo". Si hay archivo o grafica, llega adjunto
           solo, debajo de tu mensaje.
           ESCRIBE COMO UN COLEGA que le pasa el dato a otro, no como un
           reporte: frases completas, con verbo y en voz natural ("se
           vendieron", "hay", "le alcanza", "tiene"), empezando por la
           respuesta. Di las fechas como se dicen ("en agosto", "al 4 de
           octubre"), no "2026-08" ni "W35". Nada de jerga interna: ni
           "foto", "vista", "consulta", "B2B", "sell-out", "registro", ni
           nombres de tablas o columnas. "Share" se dice "participacion".
           Por ejemplo:
             "En agosto se vendieron 160,909 equipos Honor."
             "Al 4 de octubre, Telcel tiene 136,392 equipos Honor en
              inventario; al ritmo de venta del ultimo mes (unos 16,900 a la
              semana) le alcanzan para unas 8 semanas."
             "En Liverpool, Honor se llevo el 5.25% de lo que se vendio en
              agosto: 9,062 de 172,648 equipos."
             "En la ultima semana de agosto, Telcel tenia 312 promotores en
              297 tiendas."
             "El Magic 8 Lite de 256GB esta en $7,499 en Coppel, segun su
              pagina al 6 de octubre."
           Resalta en negritas solo la cifra principal.
           No cierres ofreciendo mas datos ("¿quieres que lo abra por
           tienda?", "¿te lo comparo con el ano pasado?"): al final del
           mensaje se agregan solas, y en el idioma del usuario, dos cosas
           concretas que se pueden preguntar sobre lo que acabas de
           entregar. Si tu ademas ofreces, el usuario lee lo mismo dos
           veces.
        6. Si una herramienta devuelve un mensaje de error (por ejemplo, de la base
           de datos o del driver), NUNCA cites ese texto tal cual al usuario: puede
           contener nombres de tablas, columnas u otros detalles internos del
           esquema. Usalo solo para entender que salio mal y, si puedes, corregir tu
           consulta e intentarlo de nuevo. Si aun asi falla, explicale al usuario el
           problema en una frase de lenguaje llano (por ejemplo "no pude completar
           esa consulta, intenta reformularla"), sin tecnicismos ni fragmentos del
           error original.
        7. Antes de escribir un SELECT, llama a `describir_objeto` con el nombre de
           cada tabla o vista que vayas a usar, para confirmar los nombres exactos
           de sus columnas. No inventes columnas que no aparezcan ahi.
        8. Encierra SIEMPRE entre corchetes los nombres de base, tabla y columna.
           En este servidor los hay con espacios ([Store Name]), con sufijo de
           hoja de Excel ([UNICOS$]) y con palabras reservadas de T-SQL como
           nombre ([KEY], [DESC]): sin corchetes la consulta se rechaza.
        9. Varias tablas superan los 30 millones de filas. Agrega siempre con
           GROUP BY o acota con WHERE y TOP; nunca pidas todas las filas de una
           tabla grande.
        10. Agrupar o listar POR MODELO exige unir con la homologacion
           ([GTM BI Database].dbo.[UNICOS$]) y agrupar por
           [HOMOLOGACION MODELO]. Nunca agrupes por la descripcion de la tabla
           de ventas: cada cadena escribe el mismo telefono a su manera y el
           mismo modelo sale partido en varias filas. Para eso existe
           [RAW_DATA_B2B].dbo.[vw_ETL_Con_Modelo]: la venta oficial con
           [Modelo], [Clasificacion] y [Region] ya puestos. Las respuestas salen SOLO
           de las vistas ETL (la consolidada, las vw_ETL_* y esta); las
           tablas crudas de venta no se usan para responder.
           Despues del join comprueba cuanto quedo sin casar: si se perdio una
           parte grande del volumen, la llave esta mal y el resultado es falso
           aunque parezca razonable.
        11. Si el usuario nombra un celular de forma suelta ("magic", "magic pro",
           "el X9"), llama primero a `buscar_modelos`: los nombres comerciales no
           estan escritos igual en todas las tablas, y un LIKE con el nombre tal
           cual o no encuentra nada o mezcla marcas distintas.
        12. `pronosticar_ventas` es SOLO para periodos que TODAVIA NO HAN
           OCURRIDO: "cuanto va a vender", "proyecta", "pronostico del proximo
           mes", "un estimado del siguiente mes", "una prediccion". Pasale un SELECT con la serie mensual COMPLETA (anio, mes,
           total), sin filtrar por anio, porque necesita el mismo mes del anio
           anterior por la estacionalidad. Nunca inventes tu una proyeccion.
           TODO LO DEMAS SON DATOS REALES y va por `consultar_sql`: "ventas
           totales", "cuanto se vendio", "ventas de 2026", "cuantas unidades",
           "como va el mes". Son hechos que ya estan en la base; responderlos
           con una estimacion es darle al usuario un numero inventado en lugar
           del que pidio.
           Ante la duda, datos reales. Y no mezcles: si la respuesta son datos
           reales, no le pegues advertencias de pronostico, aunque la pregunta
           anterior de la conversacion si haya sido un pronostico.
        13. Es una conversacion: el usuario escribe desde el telefono y
           acorta. Si una pregunta se apoya en las anteriores ("y de Elektra?",
           "ahora por region", "el mismo dato pero de 2023"), resuelvela contra
           lo que ya se hablo en vez de pedirle que la repita completa.
        14. Justo antes de cada pregunta vas a recibir un mensaje que te dice
           en que idioma responder (espanol, ingles o chino). Obedecelo
           siempre: es el idioma del usuario, ya detectado, y manda sobre el
           idioma de los turnos anteriores y sobre el de estas instrucciones.
           La base de datos esta en espanol; los nombres de columna y los
           valores que menciones traducelos al idioma de la respuesta.
        """

        # Prompt propio en vez del que arma create_sql_agent: el suyo es
        # System / Human / AI / scratchpad, sin hueco para el historial, y
        # ademas cierra con un mensaje que instruye "look at the tables in the
        # database... query the schema of the most relevant tables", que es
        # justo el flujo que aqui no existe (esas dos herramientas se
        # excluyeron; el esquema sale del catalogo).
        #
        # El historial va entre el prompt de sistema y la pregunta actual, que
        # es donde el modelo espera encontrarlo: asi la pregunta nueva queda
        # como el ultimo turno y no sepultada entre mensajes viejos.
        #
        # La instruccion de idioma va DESPUES del historial y pegada a la
        # pregunta, no dentro del prompt de sistema: el historial trae los
        # turnos anteriores, que pueden estar en otro idioma, y lo ultimo que
        # el modelo lee antes de la pregunta es lo que mas pesa. Ademas cambia
        # turno a turno, asi que no puede ir en el texto fijo de arriba.
        prompt = ChatPromptTemplate.from_messages(
            [
                SystemMessage(content=system_prefix),
                MessagesPlaceholder(variable_name="chat_history"),
                SystemMessagePromptTemplate.from_template("{instruccion_idioma}"),
                HumanMessagePromptTemplate.from_template("{input}"),
                MessagesPlaceholder(variable_name="agent_scratchpad"),
            ]
        )

        # Las UNICAS herramientas del agente. Antes esto se armaba con
        # create_sql_agent de langchain-community, que ademas metia el
        # toolkit SQL estandar; sus cuatro herramientas se quitaban a mano
        # (sql_db_query corre cualquier SQL sin pasar por es_select, y las de
        # esquema reflejaban `master`, sin un dato de negocio). Quedaba vacio,
        # y para llenarlo de nada se creaba un SQLDatabase que inspeccionaba la
        # base cada vez que se abria una conversacion. Esto es lo mismo que
        # create_sql_agent armaba con agent_type="openai-tools", sin eso.
        herramientas = [
            consultar_sql,
            describir_objeto,
            buscar_modelos,
            valores_distintos,
            pronosticar_ventas,
            precio_en_internet,
            semanas_de_inventario,
        ]
        agente = RunnableMultiActionAgent(
            runnable=create_openai_tools_agent(self.llm, herramientas, prompt),
            input_keys_arg=["input"],
            return_keys_arg=["output"],
            # Sin streaming: la respuesta se manda entera por WhatsApp, y con
            # streaming un corte de red a media respuesta la pierde sin
            # reintento. Visto el 5/10/2026 al pasar a LangChain 1.x, que
            # pide la respuesta en streaming por defecto: un pronostico murio
            # con "peer closed connection without sending complete message
            # body" y al usuario le llego un error.
            stream_runnable=False,
        )
        return AgentExecutor(
            name="SQL Agent Executor",
            agent=agente,
            tools=herramientas,
            verbose=False,
            # Los mismos topes que traia create_sql_agent: 15 vueltas, y al
            # agotarlas se corta ("Agent stopped due to max iterations.", que
            # `responder` traduce) en vez de pedirle al modelo otra vuelta.
            max_iterations=15,
            early_stopping_method="force",
        )

    @staticmethod
    def _sin_ofrecimiento_propio(texto: str) -> str:
        """Quita la pregunta con la que el modelo cierra, si la hay.

        Solo se llama cuando el servicio YA tiene sugerencias que pegar: sin
        esto el mensaje termina con la pregunta del modelo ("¿Quieres que te
        lo abra por cadena?") y debajo la lista de "Tambien te puedo decir:".
        Tres preguntas seguidas, y la del modelo es la que no se puede
        confiar -- la escribe sin haber mirado si eso se puede consultar.

        Se recorta el ULTIMO parrafo, y solo si es corto y OFRECE algo. No
        basta con que termine en interrogacion: "¿Revisaste la hoja
        Raw_Data?" al final de un parrafo de contenido es contenido, y
        perderlo es peor que dejar una pregunta de mas.
        """
        partes = texto.rstrip().split("\n\n")
        ultimo = partes[-1].strip()
        if len(partes) < 2 or len(ultimo) > MAX_LARGO_DEL_OFRECIMIENTO:
            return texto
        if not _OFRECIMIENTO_FINAL.search(ultimo):
            return texto
        return "\n\n".join(partes[:-1]).rstrip()

    @staticmethod
    def _sin_enlaces_inventados(texto: str) -> str:
        """Quita los enlaces de descarga que el modelo se inventa.

        Respondio, tal cual: "[Descargar Excel con datos de ventas de
        Liverpool](sandbox:/path/to/excel/file)". Ese archivo no esta en
        ningun lado -- el adjunto viaja por WhatsApp, no por un enlace -- y en
        el telefono se ve el corchete y el parentesis crudos. Se queda el
        texto y se tira el destino.

        Un enlace http de verdad (el que trae `precio_en_internet`) se
        conserva: ese si se puede abrir.
        """
        texto = _LINEA_DE_ENLACE_INVENTADO.sub("", texto)
        return _ENLACE_SIN_DESTINO.sub(r"\1", texto)

    def responder(
        self,
        pregunta: str,
        cancelacion: threading.Event | None = None,
        cadenas=None,
        notas: str = "",
    ) -> tuple[str, list[str]]:
        """Responde una pregunta y devuelve (texto, rutas de archivos generados).

        Si se activa `cancelacion` mientras corre, aborta con
        `ConsultaCancelada` en la siguiente herramienta y no toca el
        historial: una pregunta cancelada no ocurrio.
        """
        self.idioma = idiomas.detectar(pregunta) or self.idioma
        cadenas = acceso.normalizar(cadenas)
        antes = getattr(self, "cadenas", None)
        ampliado = False
        if cadenas != antes:
            # Visto el 6/10/2026: le dieron todas las cadenas despues de que
            # se le nego Elektra, escribio "intentalo de nuevo" y el bot ya no
            # sabia de que hablaba: el historial se borraba con CUALQUIER
            # cambio. Solo hace falta cuando pierde acceso a algo.
            todas = frozenset(acceso.CADENAS)
            if (antes or todas) - (cadenas or todas):
                self.historial.clear()
            else:
                ampliado = bool(self.historial)
            self.cadenas = cadenas
        with contexto_peticion(
            self.engine, cancelacion, pregunta, self.idioma, cadenas
        ) as ctx:
            instruccion = idiomas.INSTRUCCION[self.idioma]
            if notas:
                # Los comentarios del panel sobre respuestas calificadas
                # (Calificaciones.notas_para_el_bot). Antes que lo de acceso:
                # el permiso es lo ultimo que lee el modelo y lo que manda.
                instruccion = f"{instruccion}\n\n{notas}"
            if ampliado:
                # Sin esto el modelo ve en el historial que se le nego y lo
                # vuelve a negar ("intentalo de nuevo" -> otra vez no).
                instruccion = f"{instruccion}\n\n{acceso.instruccion_ampliada(cadenas)}"
            if ctx.cadenas:
                # Junto a la del idioma: cambia por usuario, y lo ultimo que
                # lee el modelo antes de la pregunta es lo que mas pesa.
                instruccion = f"{instruccion}\n\n{acceso.instruccion(ctx.cadenas)}"
            respuesta = self.agent_executor.invoke(
                {
                    "input": pregunta,
                    "chat_history": list(self.historial),
                    "instruccion_idioma": instruccion,
                },
                # El observador solo mira: anota en la bitacora lo que el
                # agente va haciendo, para poder seguirlo desde el panel.
                config={"callbacks": [Observador()]},
            )
            texto = self._sin_enlaces_inventados(
                respuesta.get("output", "No se obtuvo respuesta.")
            )
            # Cuando el agente agota sus pasos, LangChain devuelve su propio
            # texto -- "Agent stopped due to max iterations." --, en ingles y
            # sin decir nada util, y eso llegaba tal cual al WhatsApp. Visto
            # el 30 de septiembre de 2026, con la vista consolidada rota.
            sin_conclusion = texto.strip().startswith("Agent stopped due to")
            if sin_conclusion:
                texto = idiomas.texto("sin_conclusion", self.idioma)
                # Ofrecer "tambien te puedo decir..." debajo de un "no pude"
                # seria prometer lo que acaba de fallar.
                ctx.sugerencias = []
            # El revisor: que cada cifra este en los datos y sea de lo que se
            # pidio (ver revisor.py). Va antes del aviso de permisos y de la
            # ficha del pronostico, que no son del modelo.
            if ctx.vistos and not sin_conclusion:
                revision = revisor.revisar_respuesta(pregunta, ctx.vistos, texto, ctx.cadenas)
                if revision.ok:
                    bitacora.paso("revisor: la respuesta cuadra con los datos", bitacora.NOTA)
                else:
                    logger.warning("Revisor: %s", "; ".join(revision.problemas))
                    bitacora.paso(
                        "revisor: " + ("; ".join(revision.problemas) or "encontro un problema")
                        + (" -- se corrigio el mensaje" if revision.texto else ""),
                        bitacora.NOTA,
                    )
                    if revision.texto:
                        texto = revision.texto
            # Pidio una cadena que no tiene: el aviso lo pone el servicio, en
            # su idioma, y dice que el permiso es SUYO. Si no quedo nada que
            # si pueda ver, el mensaje es solo el aviso.
            if ctx.cadenas:
                negadas = ctx.negadas | (acceso.cadenas_en(pregunta) - ctx.cadenas)
                if negadas:
                    aviso = acceso.aviso_sin_permiso(negadas, ctx.cadenas, self.idioma)
                    propio = acceso.sin_negativa_propia(
                        texto.replace(acceso.SIN_PERMISO, ""), negadas
                    ).strip()
                    if ctx.hubo_datos and propio:
                        texto = f"{propio}\n\n{aviso}"
                    else:
                        texto = aviso
                        ctx.sugerencias = []
            # El historial guarda lo que escribio el modelo; la ficha se pega
            # despues, para el usuario. Repetirsela al modelo en el siguiente
            # turno solo lo invitaria a copiarla.
            self._recordar(pregunta, texto)
            # Con la respuesta ya escrita se sabe con que consulta se
            # contesto: ese es el reporte que se entrega.
            ctx.entregar_reporte(texto)
            # Si aun asi el mensaje no da ninguna cifra, va la principal del
            # reporte que se entrega. La red de seguridad del aviso de arriba.
            entregado = next((r for r in ctx.reportes if r.rutas == ctx.archivos), None)
            if entregado and entregado.principal and not cifras_del_texto(texto):
                medida, total = entregado.principal
                texto = (
                    f"{texto}\n\n"
                    + idiomas.texto("cifra_principal", self.idioma).format(
                        total=f"{total:,.0f}", medida=medida
                    )
                )
            if ctx.base_del_pronostico:
                texto = f"{texto}\n\n{ctx.base_del_pronostico}"
            # Lo mismo con el scraping: que es de internet no puede depender
            # de que el modelo se acuerde de decirlo.
            if ctx.de_internet:
                texto = f"{texto}\n\n{idiomas.texto('fuente_internet', self.idioma)}"
            # Lo ultimo: primero la respuesta, luego de donde salio, y al
            # final lo que se puede preguntar despues.
            ofrecimiento = idiomas.sugerencias(self.idioma, ctx.sugerencias)
            if ofrecimiento:
                # El recorte va DESPUES de `_recordar`: en el historial se
                # guarda lo que el modelo escribio de verdad.
                texto = f"{self._sin_ofrecimiento_propio(texto)}\n\n{ofrecimiento}"
            return texto, list(ctx.archivos)

    def _recordar(self, pregunta: str, respuesta: str) -> None:
        """Agrega el turno al historial y descarta los mas viejos.

        Se guarda la respuesta final, no las llamadas a herramientas ni las
        filas que devolvieron: al modelo le sirve saber que ya se hablo de
        Coppel, no volver a cargar el SQL ni la tabla de resultados, que es de
        lejos lo mas caro del turno.
        """
        self.historial.append(HumanMessage(content=pregunta))
        self.historial.append(AIMessage(content=respuesta))
        del self.historial[: -self.MAX_MENSAJES_HISTORIAL]
