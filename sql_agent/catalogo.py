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
from functools import lru_cache
from pathlib import Path

from langchain.tools import tool

RUTA_CATALOGO = Path(__file__).with_name("catalogo.json")

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
    "La vista [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B] es la fuente OFICIAL de "
    "venta B2B, la que usa el negocio: unifica a todos los retailers con "
    "columnas [Retail], [Carrier], [Year], [Month], [Day] y [QTY]. Usala por "
    "defecto, y escribe su nombre COMPLETO tal cual: vive en RAW_DATA_B2B y "
    "NO en [GTM BI Database] -- escribirla ahi es el error mas comun y falla "
    "con 'Invalid object name'. Las tablas crudas son su insumo y no cuadran con ella (filtran "
    "distinto y no estan agregadas); ve a ellas solo si te piden el dato "
    "crudo, y entonces avisa que no es la cifra oficial.",
    "En ETL_Consolidado_B2B cada fila es una combinacion de tienda, SKU, "
    "carrier y dia ya sumada, no un movimiento suelto. Para unidades vendidas "
    "usa SUM([QTY]); COUNT(*) responde otra pregunta y casi nunca es la que "
    "te hicieron.",
    "Los nombres de retailer no son uniformes ni una sola etiqueta por cadena: "
    "Elektra aparece como 'ELEKTRA' en mayusculas, y Telcel esta repartido en "
    "'TELCEL CAC', 'TELCEL RETAIL', 'TELCEL DISTR. ASOC.', 'TELCEL WEB', "
    "'TELCEL CORPO.' y 'TELCEL VENTA EMP.'. Filtra con LIKE, o consulta antes "
    "los valores distintos de la columna.",
    "Si una consulta devuelve cero filas, NO concluyas que no hay datos: casi "
    "siempre el filtro esta mal. Comprueba los valores distintos de la columna "
    "que filtraste antes de responderle al usuario que no hay informacion.",
]

# Notas de un objeto concreto, que salen cuando el agente lo consulta.
NOTAS_POR_OBJETO = {
    "EKT_B2B": (
        "Solo ventas de Elektra; no tiene columna [Retail]. OJO con las fechas: "
        "[Month] es texto con la abreviatura inglesa del mes ('Mar', 'Nov'), "
        "no un numero, asi que `[Month] = 3` falla al convertir. Para acotar un "
        "mes usa el rango de [Day], que si es datetime."
    ),
    "ETL_Consolidado_B2B": (
        "Se escribe [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B]; no esta en "
        "[GTM BI Database]. "
        "LA fuente oficial de venta B2B: es la que usa el negocio, asi que sus "
        "numeros son los buenos. Union de las nueve vistas vw_ETL_*, una por "
        "retailer, ya normalizadas. [Year], [Month] y [Day] son enteros. "
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
    "TBL_DB_FOR_DOS": (
        "Su columna [Retail] solo contiene 'Coppel', 'Liverpool Offline' y "
        "'TELCEL CAC'. No incluye Elektra ni el resto de cadenas: para esas hay "
        "que ir a RAW_DATA_B2B."
    ),
}


@lru_cache(maxsize=1)
def cargar_catalogo() -> dict:
    return json.loads(RUTA_CATALOGO.read_text(encoding="utf-8"))


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
