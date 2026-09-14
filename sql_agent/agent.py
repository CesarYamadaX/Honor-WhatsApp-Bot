import logging
import os
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
from langchain_openai import ChatOpenAI
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlparse import tokens as sql_tokens

from excel_exporter import ExcelExporter
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


@dataclass
class ContextoPeticion:
    """Estado de una sola pregunta: de donde se lee y que archivos se generaron."""

    engine: Engine
    archivos: list[str] = field(default_factory=list)


_contexto: ContextVar[ContextoPeticion | None] = ContextVar("_contexto", default=None)


@contextmanager
def contexto_peticion(engine: Engine) -> Iterator[ContextoPeticion]:
    """Activa un contexto por petición, en lugar del viejo global _db_instance."""
    ctx = ContextoPeticion(engine=engine)
    testigo = _contexto.set(ctx)
    try:
        yield ctx
    finally:
        _contexto.reset(testigo)


def _contexto_actual() -> ContextoPeticion:
    ctx = _contexto.get()
    if ctx is None:
        raise RuntimeError(
            "No hay contexto de peticion activo. Las herramientas solo pueden "
            "usarse dentro de contexto_peticion()."
        )
    return ctx


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
            # exec_driver_sql, no pd.read_sql(text(query), ...): text() hace
            # que SQLAlchemy interprete ":algo" dentro del SQL como parametro
            # de vinculo. Con exec_driver_sql el SQL del usuario viaja crudo
            # al driver, tal como lo escribio (o lo genero el modelo).
            resultado_bruto = conexion.exec_driver_sql(query)
            df = pd.DataFrame(resultado_bruto.fetchall(), columns=list(resultado_bruto.keys()))
    except Exception as error:  # noqa: BLE001 - el mensaje (resumido) vuelve al modelo
        # El error completo -con nombres de tabla/columna y detalle del
        # driver ODBC- se registra aqui, en el log del servidor, que es
        # donde debe quedar (Importante de la revision final). Al modelo
        # solo le llega el texto de abajo, y el system_prefix de
        # _create_agent le prohibe citarlo tal cual al usuario.
        logger.exception("Error al ejecutar SQL generado por el agente")
        return f"Error al ejecutar la consulta: {error}"

    resultado = formatear_resultado(df)
    if resultado.necesita_excel:
        try:
            ctx.archivos.append(ExcelExporter.export_to_excel(df))
        except Exception as error:  # noqa: BLE001
            logger.exception("Fallo la exportacion a Excel")
            return f"Obtuve {len(df)} registros pero fallo el Excel: {error}"
    return resultado.texto


class _ToolkitSoloLectura(SQLDatabaseToolkit):
    """El toolkit estandar de LangChain menos las herramientas que ejecutan SQL.

    `SQLDatabaseToolkit.get_tools()` trae `sql_db_query`, que corre cualquier
    SQL contra la base sin pasar por `es_select` (Importante 4 de la
    revision). Se excluye tambien `sql_db_query_checker`, que solo tiene
    sentido junto a esa herramienta. Quedan `sql_db_list_tables` y
    `sql_db_schema`: exploran el esquema, no ejecutan SQL arbitrario del
    modelo (las filas de muestra que traen son consultas fijas de LangChain,
    no texto libre).
    """

    _EXCLUIDAS = {"sql_db_query", "sql_db_query_checker"}

    def get_tools(self) -> list:
        return [t for t in super().get_tools() if t.name not in self._EXCLUIDAS]


class SQLAgent:
    """Un agente Text-to-SQL. Una instancia por conversacion de WhatsApp."""

    def __init__(self, engine: Engine | None = None):
        self.engine = engine if engine is not None else self._crear_engine()
        self.db = SQLDatabase(self.engine)
        self.llm = self._init_llm()
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
        system_prefix = """
        Eres un experto analista de datos que responde por WhatsApp. Tu trabajo es
        interactuar con una base de datos Microsoft SQL Server.

        REGLAS CRITICAS:
        1. SOLO DEBES EJECUTAR SENTENCIAS `SELECT`.
        2. BAJO NINGUNA CIRCUNSTANCIA debes ejecutar comandos DML o DDL (`INSERT`,
           `UPDATE`, `DELETE`, `DROP`, `CREATE`, `ALTER`, etc.).
        3. Si el usuario pide modificar o borrar informacion, niegate educadamente
           explicando que solo tienes acceso de lectura.
        4. Para obtener los datos finales que responden la pregunta usa SIEMPRE la
           herramienta `consultar_sql`. Ella decide sola si la respuesta va como
           texto o como archivo Excel; no le pidas que exporte.
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
        """

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
            prefix=system_prefix,
            extra_tools=[consultar_sql],
        )

    def responder(self, pregunta: str) -> tuple[str, list[str]]:
        """Responde una pregunta y devuelve (texto, rutas de archivos generados)."""
        with contexto_peticion(self.engine) as ctx:
            respuesta = self.agent_executor.invoke({"input": pregunta})
            texto = respuesta.get("output", "No se obtuvo respuesta.")
            return texto, list(ctx.archivos)
