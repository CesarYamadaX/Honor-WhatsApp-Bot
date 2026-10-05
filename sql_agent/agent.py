import logging
import os
import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
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
    valores_conocidos,
)
from contexto import contexto_del_resultado
import excel_pivot
from excel_exporter import MAX_FILAS_POR_HOJA_DE_EXCEL, ExcelExporter
from grafica import grafica_del_resultado, pidio_grafica
import bitacora
import inventario
import resumen_local
import idioma as idiomas
from pronostico import ERROR_TIPICO, pidio_pronostico, proyectar
from sugerencias import sugerir
import tiempo
from formatting import formatear_resultado

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


class ConsultaCancelada(Exception):
    """El usuario mando otra pregunta (o pidio cancelar) y esta ya no importa."""


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

    def registrar_reporte(self, *rutas: str | None) -> None:
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
        """
        nuevos = [r for r in rutas if r]
        if not nuevos:
            return
        trae_grafica = any(r.lower().endswith(".png") for r in nuevos)
        tiene_grafica = any(r.lower().endswith(".png") for r in self.archivos)
        if not self.archivos or (trae_grafica and not tiene_grafica):
            self.archivos[:] = nuevos


_contexto: ContextVar[ContextoPeticion | None] = ContextVar("_contexto", default=None)


@contextmanager
def contexto_peticion(
    engine: Engine,
    cancelacion: threading.Event | None = None,
    pregunta: str = "",
    idioma: str = idiomas.POR_DEFECTO,
) -> Iterator[ContextoPeticion]:
    """Activa un contexto por petición, en lugar del viejo global _db_instance."""
    ctx = ContextoPeticion(
        engine=engine, cancelacion=cancelacion, pregunta=pregunta, idioma=idioma
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
        bruta.timeout = anterior


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
    precalculados = valores_conocidos(objeto["nombre"], real)
    if precalculados is not None:
        return _listar(real, objeto["nombre"], precalculados)

    sql = (
        f"SELECT DISTINCT TOP {MAX_VALORES} {_corchetes(real)} "
        f"FROM {_corchetes(base)}.dbo.{_corchetes(objeto['nombre'])} "
        f"WHERE {_corchetes(real)} IS NOT NULL ORDER BY 1"
    )

    ctx = _contexto_actual()
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
            FROM {TABLA_SCRAP}
            WHERE {condiciones}{filtro_retail}
              AND [consultation_date] = (
                  SELECT MAX([consultation_date]) FROM {TABLA_SCRAP}
                  WHERE {condiciones}{filtro_retail}
              )
        )
        SELECT TOP {MAX_OFERTAS} [retail], [carrier], [approved_name], [price],
               [consultation_date], [url]
        FROM reciente WHERE orden = 1 ORDER BY [price]
    """

    ctx = _contexto_actual()
    try:
        with ctx.engine.connect() as conexion:
            cursor = conexion.exec_driver_sql(sql, tuple(parametros) * 2)
            filas = cursor.fetchall()
    except Exception as error:  # noqa: BLE001
        logger.exception("Fallo la consulta de precios de internet")
        return f"No pude consultar los precios publicados: {error}"

    if not filas:
        return (
            f"No hay precios publicados para {' + '.join(tokens)}"
            + (f" en {retailer}" if retailer.strip() else "")
            + ". Prueba con menos palabras del modelo."
        )

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
        "el precio interno), y para cada renglon incluye SIEMPRE el NOMBRE "
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
    return "\n".join(lineas)


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

    ctx = _contexto_actual()
    try:
        with ctx.engine.connect() as conexion:
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
    return _texto_del_pronostico(resultado)


@tool
def semanas_de_inventario(modelo: str = "", marca: str = "HONOR") -> str:
    """Para cuantas semanas le alcanza el inventario a Telcel.

    Usala SIEMPRE que pregunten por semanas de inventario, cobertura, "para
    cuanto le alcanza", "cuanto le dura el stock" o sell-through de Telcel.
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
    try:
        resultado = inventario.calcular(ctx.engine, modelo, (marca or "").strip().upper())
    except Exception as error:  # noqa: BLE001
        logger.exception("Fallo el calculo de semanas de inventario")
        return f"No pude calcular las semanas de inventario: {error}"
    if resultado is None:
        return (
            f"No hay stock ni venta en Telcel que coincida con {marca} {modelo!r}. "
            "Revisa el nombre con `buscar_modelos` o pregunta al usuario."
        )
    texto = inventario.como_texto(resultado)
    if len(resultado.tabla) > 15:
        try:
            ctx.archivos.append(ExcelExporter.export_to_excel(resultado.tabla))
        except Exception:  # noqa: BLE001 - el texto ya trae el total y los principales
            logger.exception("No se pudo armar el Excel de semanas de inventario")
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

    mal_unida = _homologacion_mal_unida(query)
    if mal_unida:
        return mal_unida

    ctx = _contexto_actual()
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
    # Primero el resumen local: si la consulta solo necesita unidades por
    # cadena, marca, modelo, carrier y periodo, se contesta en un segundo en
    # vez de minutos. Si no aplica o falla, None, y va al servidor como
    # siempre. Ver resumen_local.py.
    comenzo = time.monotonic()
    desde_el_resumen = resumen_local.consultar(pedida)
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
            contexto = contexto_del_resultado(df, ctx.engine, query, queda)
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo armar el contexto; va solo la tabla")
            contexto = {}

        try:
            excel = ExcelExporter.export_to_excel(df, contexto=contexto)
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
        sera_el_reporte_bueno = not ctx.archivos or (
            imagen is not None
            and not any(a.lower().endswith(".png") for a in ctx.archivos)
        )
        crudos = contexto.get("Raw_Data")
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
        if (
            sera_el_reporte_bueno
            and not crudo_partido
            and crudos is not None
            and not crudos.empty
        ):
            config = excel_pivot.config_ventas_por_tienda(crudos)
            if config:
                bitacora.paso("armando el pivot de tienda y mes", bitacora.NOTA)
                if excel_pivot.agregar_pivot(excel, "Raw_Data", **config):
                    bitacora.paso("pivot listo", bitacora.NOTA)
                else:
                    bitacora.paso(
                        "no se pudo armar el pivot; el reporte sale sin el",
                        bitacora.NOTA,
                    )

    # La grafica va primero: en el chat se ve de un vistazo, y el Excel queda
    # debajo para quien quiera el detalle.
    ctx.registrar_reporte(imagen, excel)

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
    if crudo_partido:
        partes.append(
            "OJO: los datos crudos no caben en una sola hoja de Excel, asi que "
            "van repartidos en varias ('Raw_Data', 'Raw_Data (2)'...) y el "
            "archivo va SIN tabla dinamica: un pivot solo puede resumir una "
            "hoja, y resumir solo la primera seria enganoso. Estan TODAS las "
            "filas. Dilo al responder."
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

    return "\n\n".join(partes)


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
        self.llm = self._init_llm()
        self.historial: list[BaseMessage] = []
        # El idioma se sostiene entre turnos: los mensajes de WhatsApp son
        # cortos y muchos ("top 10", "y de Elektra?") no tienen idioma propio.
        self.idioma = idiomas.POR_DEFECTO
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
        ejemplo: SELECT TOP 10 * FROM [GTM BI Database].dbo.[TBL_DB_FOR_DOS].
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
           Semanas de inventario, cobertura o "para cuanto le alcanza el
           stock" en Telcel: `semanas_de_inventario`, nunca un SELECT propio.
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
           mismo modelo sale partido en varias filas. La ruta que funciona
           para todos los retailers es la TABLA CRUDA unida por
           [DESCRIPCION] (ver la nota de UNICOS$ en el catalogo); ademas es
           mucho mas rapida que la vista.
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
        self, pregunta: str, cancelacion: threading.Event | None = None
    ) -> tuple[str, list[str]]:
        """Responde una pregunta y devuelve (texto, rutas de archivos generados).

        Si se activa `cancelacion` mientras corre, aborta con
        `ConsultaCancelada` en la siguiente herramienta y no toca el
        historial: una pregunta cancelada no ocurrio.
        """
        self.idioma = idiomas.detectar(pregunta) or self.idioma
        with contexto_peticion(
            self.engine, cancelacion, pregunta, self.idioma
        ) as ctx:
            respuesta = self.agent_executor.invoke(
                {
                    "input": pregunta,
                    "chat_history": list(self.historial),
                    "instruccion_idioma": idiomas.INSTRUCCION[self.idioma],
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
            # El historial guarda lo que escribio el modelo; la ficha se pega
            # despues, para el usuario. Repetirsela al modelo en el siguiente
            # turno solo lo invitaria a copiarla.
            self._recordar(pregunta, texto)
            if ctx.base_del_pronostico:
                texto = f"{texto}\n\n{ctx.base_del_pronostico}"
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
