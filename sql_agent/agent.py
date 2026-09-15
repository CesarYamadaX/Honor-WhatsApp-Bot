import logging
import os
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

import pandas as pd
import sqlparse
from dotenv import load_dotenv
from langchain.tools import tool
from langchain_community.agent_toolkits import create_sql_agent
from langchain_community.agent_toolkits.sql.toolkit import SQLDatabaseToolkit
from langchain_community.utilities import SQLDatabase
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.prompts import (
    ChatPromptTemplate,
    HumanMessagePromptTemplate,
    MessagesPlaceholder,
)
from langchain_openai import ChatOpenAI
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlparse import tokens as sql_tokens

from catalogo import _corchetes, buscar, describir_objeto, indice
from excel_exporter import ExcelExporter
from grafica import exportar_grafica
from formatting import formatear_resultado

logger = logging.getLogger("sql_agent.agent")

load_dotenv()

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


class ConsultaCancelada(Exception):
    """El usuario mando otra pregunta (o pidio cancelar) y esta ya no importa."""


@dataclass
class ContextoPeticion:
    """Estado de una sola pregunta: de donde se lee y que reporte se genero."""

    engine: Engine
    archivos: list[str] = field(default_factory=list)
    # Lo pone la API cuando llega un mensaje nuevo del mismo chat. No hay
    # forma de matar el hilo que corre el agente, asi que la cancelacion es
    # cooperativa: se revisa antes de cada herramienta y se aborta ahi.
    cancelacion: threading.Event | None = None

    def registrar_reporte(self, *rutas: str | None) -> None:
        """Guarda los adjuntos de la pregunta. Solo cuenta la primera consulta.

        Una pregunta, un archivo. Antes se acumulaban y se enviaban todos: el
        usuario pidio agregar el modelo a un reporte de ventas y recibio dos
        Excel -- 11 501 filas de ventas sin el modelo y 4 509 nombres de modelo
        sin ventas-- cuando queria una sola tabla con ambas cosas.

        Se conserva el PRIMERO, no el ultimo, porque una consulta que falla no
        llega hasta aqui: solo las exitosas generan archivo. Asi que la primera
        es la respuesta que el agente construyo, y lo que venga despues en la
        misma vuelta son comprobaciones (un DISTINCT para ver como se escriben
        los valores, un listado de apoyo). Conservar el ultimo mandaba esas
        comprobaciones en lugar del reporte: en una prueba, la lista de 16
        retailers en vez de las ventas que se habian pedido.
        """
        if not self.archivos:
            self.archivos.extend(r for r in rutas if r)


_contexto: ContextVar[ContextoPeticion | None] = ContextVar("_contexto", default=None)


@contextmanager
def contexto_peticion(
    engine: Engine, cancelacion: threading.Event | None = None
) -> Iterator[ContextoPeticion]:
    """Activa un contexto por petición, en lugar del viejo global _db_instance."""
    ctx = ContextoPeticion(engine=engine, cancelacion=cancelacion)
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


MAX_VALORES = 50

# Explorar no puede costar la respuesta: la peticion entera vive dentro de
# AGENT_TIMEOUT_SECONDS (90s por defecto), que ademas tiene que alcanzar para
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

    if not filas:
        return f"{real} no tiene valores en {objeto['nombre']}."
    valores = "\n".join(f"  {fila[0]}" for fila in filas)
    aviso = f"\n(solo los primeros {MAX_VALORES})" if len(filas) == MAX_VALORES else ""
    return f"Valores de {real} en {objeto['nombre']}:\n{valores}{aviso}"


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


@tool
def consultar_sql(query: str) -> str:
    """Ejecuta una consulta SELECT y devuelve los resultados al usuario.

    Usa SIEMPRE esta herramienta para obtener los datos finales que responden
    la pregunta. Si el resultado tiene muchas filas, esta herramienta genera
    automaticamente un archivo Excel y lo entrega; no necesitas pedirlo.
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

    ctx = _contexto_actual()
    try:
        with ctx.engine.connect() as conexion:
            df = _ejecutar_interrumpible(conexion, query, ctx.cancelacion)
    except ConsultaCancelada:
        raise
    except Exception as error:  # noqa: BLE001 - el mensaje (resumido) vuelve al modelo
        # El error completo -con nombres de tabla/columna y detalle del
        # driver ODBC- se registra aqui, en el log del servidor, que es
        # donde debe quedar (Importante de la revision final). Al modelo
        # solo le llega el texto de abajo, y el system_prefix de
        # _create_agent le prohibe citarlo tal cual al usuario.
        logger.exception("Error al ejecutar SQL generado por el agente")
        return f"Error al ejecutar la consulta: {error}{_donde_vive_de_verdad(error)}"

    resultado = formatear_resultado(df)
    if resultado.necesita_excel:
        try:
            excel = ExcelExporter.export_to_excel(df)
        except Exception as error:  # noqa: BLE001
            logger.exception("Fallo la exportacion a Excel")
            return f"Obtuve {len(df)} registros pero fallo el Excel: {error}"

        # La grafica va primero: en el chat se ve de un vistazo, y el Excel
        # queda debajo para quien quiera el detalle. Que falle no cuesta el
        # reporte: los datos son lo que se pidio.
        try:
            imagen = exportar_grafica(df)
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo generar la grafica; se manda solo el Excel")
            imagen = None

        ctx.registrar_reporte(imagen, excel)
    return resultado.texto


class _ToolkitSoloLectura(SQLDatabaseToolkit):
    """El toolkit estandar de LangChain, sin ninguna de sus cuatro herramientas.

    `SQLDatabaseToolkit.get_tools()` trae `sql_db_query`, que corre cualquier
    SQL contra la base sin pasar por `es_select` (Importante 4 de la
    revision). Se excluye tambien `sql_db_query_checker`, que solo tiene
    sentido junto a esa herramienta.

    `sql_db_list_tables` y `sql_db_schema` se excluyen por un motivo distinto:
    reflejan la base del connection string, y DB_URI apunta a `master`, donde
    no hay ni un solo dato de negocio. Devolverle al modelo el catalogo de
    master no es util, es desinformacion -- invita a concluir que no hay nada
    que consultar. El esquema real, que vive repartido en otras cinco bases del
    servidor, se lo damos por el modulo `catalogo`: el indice va en el prompt y
    las columnas exactas salen de `describir_objeto`.

    El toolkit se sigue pasando a `create_sql_agent` porque de el sale
    `toolkit.dialect` para el prompt, aunque ya no aporte herramientas.
    """

    _EXCLUIDAS = {
        "sql_db_query",
        "sql_db_query_checker",
        "sql_db_list_tables",
        "sql_db_schema",
    }

    def get_tools(self) -> list:
        return [t for t in super().get_tools() if t.name not in self._EXCLUIDAS]


class SQLAgent:
    """Un agente Text-to-SQL. Una instancia por conversacion de WhatsApp.

    El historial vive en la instancia y no en un almacen aparte porque
    `RegistroSesiones` ya crea un SQLAgent por chat_id y serializa sus
    consultas con un lock por conversacion: no hay dos hilos tocando el mismo
    historial, y cuando la sesion expira por TTL el historial se va con ella.
    """

    # Cuantos mensajes del historial se le repiten al modelo. El prompt ya
    # carga el catalogo del servidor, y cada pregunta arrastra ademas sus
    # llamadas a herramientas, asi que el historial no puede crecer sin tope.
    # Diez mensajes son cinco preguntas con su respuesta: suficiente para
    # sostener un "y de Elektra?" sin encarecer cada consulta.
    MAX_MENSAJES_HISTORIAL = 10

    def __init__(self, engine: Engine | None = None):
        self.engine = engine if engine is not None else self._crear_engine()
        self.db = SQLDatabase(self.engine)
        self.llm = self._init_llm()
        self.historial: list[BaseMessage] = []
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

        model_name = os.getenv("OPENROUTER_MODEL", "openai/gpt-4-turbo-preview")
        return ChatOpenAI(
            temperature=0,
            model=model_name,
            openai_api_key=openrouter_api_key,
            openai_api_base="https://openrouter.ai/api/v1",
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
           herramienta `consultar_sql`. Ella decide sola si la respuesta va como
           texto o como archivo Excel; no le pidas que exporte.
           `consultar_sql` es SOLO para la respuesta final, nunca para explorar.
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
        10. Si el usuario nombra un celular de forma suelta ("magic", "magic pro",
           "el X9"), llama primero a `buscar_modelos`: los nombres comerciales no
           estan escritos igual en todas las tablas, y un LIKE con el nombre tal
           cual o no encuentra nada o mezcla marcas distintas.
        11. Es una conversacion: el usuario escribe desde el telefono y
           acorta. Si una pregunta se apoya en las anteriores ("y de Elektra?",
           "ahora por region", "el mismo dato pero de 2023"), resuelvela contra
           lo que ya se hablo en vez de pedirle que la repita completa.
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
        prompt = ChatPromptTemplate.from_messages(
            [
                SystemMessage(content=system_prefix),
                MessagesPlaceholder(variable_name="chat_history"),
                HumanMessagePromptTemplate.from_template("{input}"),
                MessagesPlaceholder(variable_name="agent_scratchpad"),
            ]
        )

        # No se usa `db=self.db`: eso arma el toolkit estandar completo, que
        # incluye `sql_db_query` (QuerySQLDataBaseTool), una herramienta que
        # ejecuta SQL arbitrario contra la base sin pasar por es_select. El
        # toolkit propio de abajo se queda solo con las herramientas de
        # exploracion de esquema; la unica forma de traer datos es
        # `consultar_sql`, que si filtra.
        toolkit = _ToolkitSoloLectura(db=self.db, llm=self.llm)

        return create_sql_agent(
            llm=self.llm,
            toolkit=toolkit,
            # Cadena literal y no `AgentType.OPENAI_TOOLS`: ese miembro del
            # enum no existe en langchain 0.1.16 (el pin de requirements.txt),
            # asi que el atributo revienta con AttributeError antes de
            # siquiera llamar a create_sql_agent. La firma de create_sql_agent
            # acepta este string directamente (Literal["openai-tools"]).
            agent_type="openai-tools",
            verbose=False,
            prompt=prompt,
            extra_tools=[
                consultar_sql,
                describir_objeto,
                buscar_modelos,
                valores_distintos,
            ],
        )

    def responder(
        self, pregunta: str, cancelacion: threading.Event | None = None
    ) -> tuple[str, list[str]]:
        """Responde una pregunta y devuelve (texto, rutas de archivos generados).

        Si se activa `cancelacion` mientras corre, aborta con
        `ConsultaCancelada` en la siguiente herramienta y no toca el
        historial: una pregunta cancelada no ocurrio.
        """
        with contexto_peticion(self.engine, cancelacion) as ctx:
            respuesta = self.agent_executor.invoke(
                {"input": pregunta, "chat_history": list(self.historial)}
            )
            texto = respuesta.get("output", "No se obtuvo respuesta.")
            self._recordar(pregunta, texto)
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
