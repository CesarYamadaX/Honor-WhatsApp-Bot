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
    lineas = []
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
    return f"{cabecera}\n{columnas}"


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
