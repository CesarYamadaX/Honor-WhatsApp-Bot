"""Que cadenas puede ver cada usuario, y como se hace cumplir.

Pedido el 6/10/2026: que ciertas personas solo vean ciertas cadenas (el
ejecutivo de Coppel, solo Coppel), en todo -- venta, inventario,
promotoria y precios. El panel lo sigue viendo todo.

No se le encarga al modelo. "No le enseñes Telcel" en el prompt es una
sugerencia que se salta en cuanto le preguntan de lado ("y el total del
mercado?"). Aqui se mira cada tabla que nombra la consulta ANTES de que
llegue al servidor:

- la de una sola cadena (vw_ETL_COPPEL, EKT_B2B, Coppel_INV...) pasa si esa
  cadena esta permitida y si no, la consulta no se ejecuta;
- la de varias cadenas (la consolidada, Prices, INV_Retail, HCReport...) se
  cambia por una tabla derivada que solo trae las filas de las permitidas;
- la que no esta en ninguna lista no se ejecuta. Una tabla nueva en el
  servidor queda cerrada hasta que alguien diga de quien es, en vez de
  quedar abierta hasta que alguien se acuerde de cerrarla.

Un usuario sin cadenas puestas ve todas, como siempre: nada de esto corre.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from catalogo import CADENA_DE_PROMOTORIA, _VISTAS_DE_LA_CONSOLIDADA, _union_de

# Las cadenas, como se guardan y como se ven en el panel.
CADENAS = ("COPPEL", "ELEKTRA", "TELCEL", "LIVERPOOL", "AT&T", "SEARS", "MOVISTAR")
NOMBRES = {
    "COPPEL": "Coppel", "ELEKTRA": "Elektra", "TELCEL": "Telcel",
    "LIVERPOOL": "Liverpool", "AT&T": "AT&T", "SEARS": "Sears", "MOVISTAR": "Movistar",
}

# Como empieza el nombre de cada cadena en las columnas de retail, ya en
# mayusculas. Medido contra la base el 6/10/2026 en todas las tablas de
# abajo: Telcel llega en seis canales ('TELCEL CAC', 'TELCEL WEB'...),
# Liverpool en dos, AT&T como 'AT&T Cacs' y 'AT&T Dealers'. Lo que no empieza
# asi ('Unknown', 'Suburbia', 'Mercado Libre') no es de ninguna cadena y un
# usuario acotado no lo ve.
PREFIJOS = {
    "COPPEL": ("COPPEL",),
    "ELEKTRA": ("ELEKTRA", "EKT"),
    "TELCEL": ("TELCEL",),
    "LIVERPOOL": ("LIVERPOOL",),
    "AT&T": ("AT&T", "ATT"),
    "SEARS": ("SEARS",),
    "MOVISTAR": ("MOVISTAR", "MVS"),
}

# Las tablas y vistas de UNA cadena. La cadena esta en el nombre, no en una
# columna. Si la tabla SI tiene columna de cadena, aqui solo entra si se
# midio que trae una sola; si no, va abajo, con el filtro por columna: el
# nombre engaña -- Coppel_test traia las siete cadenas (medido el 6/10/2026;
# ya la borraron).
_DE_UNA_CADENA = {
    # Venta, RAW_DATA_B2B.
    "ATT_B2B": "AT&T", "vw_ETL_ATT_Postpaid": "AT&T",
    "COPPEL_B2B": "COPPEL", "Coppel_IOT": "COPPEL",
    "vw_ETL_COPPEL": "COPPEL", "vw_ETL_COPPEL_IOT": "COPPEL",
    "EKT_B2B": "ELEKTRA", "vw_ETL_Elektra": "ELEKTRA",
    "LIVERPOOL_B2B$": "LIVERPOOL", "Liverpool_IOT": "LIVERPOOL",
    "vw_ETL_Liverpool": "LIVERPOOL", "vw_ETL_Liverpool_IOT": "LIVERPOOL",
    "MVS_B2B": "MOVISTAR", "vw_ETL_Movistar_CAC": "MOVISTAR",
    "SEARS_B2B": "SEARS", "vw_ETL_Sears": "SEARS",
    "TELCEL_B2B": "TELCEL", "vw_ETL_TELCEL_B2B": "TELCEL",
    # Inventario, RAW_INV_B2B y GTM_BI_INVENTORIES.
    "Coppel_INV": "COPPEL", "ELEKTRA_INV_B2B": "ELEKTRA", "TELCEL_INV_B2B": "TELCEL",
    "INV_Telcel_B2B": "TELCEL", "vw_INV_Telcel_B2B": "TELCEL",
    "VW_ELEKTRA_MONTLY_SOandINV_LC": "ELEKTRA",
    "VW_TELCEL_B2B_SO_LC": "TELCEL",
    "VW_TELCELB2B_MONTH_SOandINV_LC": "TELCEL",
    # Reportes armados, GTM BI Database.
    "Coppel": "COPPEL", "Coppel_2024": "COPPEL", "Coppel_Monthly_Excel": "COPPEL",
    "vw_Coppel_Monthly_Excel": "COPPEL", "VW_Ventas_Coppel": "COPPEL",
    "vw_Elektra_Monthly_Excel": "ELEKTRA",
    "vw_Liverpool_Monthly_Base": "LIVERPOOL", "vw_Liverpool_Monthly_Excel": "LIVERPOOL",
    "CompetitorsAnalysis_VW_TELCEL": "TELCEL",
}

# Las de VARIAS cadenas, y la columna que dice de cual es cada fila. Tambien
# las que por el nombre serian de una, pero no se pudo medir (la consulta no
# termino en 4 minutos): con el filtro dan lo mismo si de verdad son de una.
_POR_COLUMNA = {
    "CompetitorsAnalysis_Telcel_VW": "Retail",
    "TELCEL_CompetitorsAnalysis_VW_LGC": "Retail",
    "VW_COPPEL_MONTLY_SOandINV_LC": "Retail",
    "VW_LIVERPOOL_MONTLY_SOandINV_LC": "Retail",
    "B2BHonorGeneralReport": "Retail",
    "CompetitorsAnalysis_VW": "Retail",
    "CompetitorsAnalysis2024_VW": "Retail",
    "CustomerStores": "RetailName",
    "CustomerStores_VW": "RetailName",
    "HonorCarriers": "Retail",
    "HonorCarriers_VW": "Retail",
    "Prices": "Retail",
    "Prices_Weekly_VW": "Retail",
    "Share_Retail_Price3000": "Retail",
    "STATES_AND_REGIONS": "Customer",
    "TBL_DB_FOR_DOS": "Retail",
    "V_GTM_HonorReport_LGC": "Retail",
    "VW_edicion": "Retail",
    "INV_Retail": "Retail",
    "Sheet1$": "Retail",
    "TAB_DB_FOR_DOS": "Retail",
    "VW_DB_FOR_DOS": "Retail",
    "VW_DB_FOR_DOSV1": "Retail",
    "VW_INVENTORIES": "Retail",
    "VW_MVS_ATT_SEARS_MONTLY_SOandINV_LC": "Retail",
    "VW_RETAILERS_MONTH_SOandINV": "Retail",
    "Complete_Scrap": "retail",
}

# La consolidada se cambia por la union de las vistas de las cadenas
# permitidas, no por un filtro: es la misma suma (es el UNION de esas nueve
# vistas, medido el 30/09/2026) y no recorre las cadenas que se iban a tirar.
CONSOLIDADA = "ETL_Consolidado_B2B"
_CADENA_DE_VISTA = {v: _DE_UNA_CADENA[v] for v in _VISTAS_DE_LA_CONSOLIDADA}

# Promotoria: HCReport no tiene columna de cadena; sale del catalogo de
# tiendas y, si la tienda no esta ahi, de la primera palabra del nombre.
PROMOTORIA = "HCReport"

# Sin datos de ninguna cadena: nombres de modelo y benchmarks.
_NEUTRALES = {"UNICOS$", "Benchmark$"}

_CLAVE = {n.casefold(): n for n in (
    list(_DE_UNA_CADENA) + list(_POR_COLUMNA) + [CONSOLIDADA, PROMOTORIA] + list(_NEUTRALES)
)}

# Las bases de verdad. Un nombre de tres partes de otra base no se sabe que
# trae.
_BASES = {b.casefold() for b in (
    "RAW_DATA_B2B", "RAW_INV_B2B", "GTM BI Database", "GTM_BI_INVENTORIES", "Scrap",
)}


def normalizar(cadenas: Iterable[str] | None) -> frozenset[str] | None:
    """Las cadenas validas, en su clave. None (o vacio) es "todas"."""
    if not cadenas:
        return None
    validas = frozenset(c.strip().upper() for c in cadenas if c and c.strip().upper() in CADENAS)
    return validas or None


def nombres(cadenas: Iterable[str]) -> str:
    """'Coppel y Elektra', para los textos."""
    lista = [NOMBRES[c] for c in CADENAS if c in set(cadenas)]
    if len(lista) <= 1:
        return "".join(lista)
    return ", ".join(lista[:-1]) + " y " + lista[-1]


def cadena_de(valor) -> str | None:
    """De que cadena es un valor de una columna de retail, o None."""
    texto = str(valor or "").strip().upper()
    for cadena, prefijos in PREFIJOS.items():
        if texto.startswith(prefijos):
            return cadena
    return None


def condicion(columna: str, cadenas: Iterable[str]) -> str:
    """El WHERE que deja solo las filas de esas cadenas (vale en SQLite)."""
    expresion = f"UPPER(LTRIM(RTRIM({columna})))"
    partes = [
        f"{expresion} LIKE '{prefijo}%'"
        for cadena in CADENAS if cadena in set(cadenas)
        for prefijo in PREFIJOS[cadena]
    ]
    return "(" + " OR ".join(partes) + ")"


def de_quien(objeto: str) -> str | None:
    """La cadena de una tabla de una sola cadena, o None."""
    clave = _CLAVE.get(objeto.casefold())
    return _DE_UNA_CADENA.get(clave) if clave else None


def columna_de_cadena(objeto: str) -> str | None:
    """La columna que dice la cadena en una tabla de varias, o None."""
    clave = _CLAVE.get(objeto.casefold())
    if clave == CONSOLIDADA:
        return "Retail"
    return _POR_COLUMNA.get(clave) if clave else None


# --- Leer la consulta -----------------------------------------------------

# Lo que no son nombres: literales y comentarios se tapan con espacios (el
# mismo largo, para que las posiciones sigan valiendo en el original). Los
# corchetes se reconocen primero para que un `[a'b]` no abra un literal.
_SALTAR = re.compile(
    r"(\[[^\]]*\])|(\"[^\"]*\")|(N?'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/)", re.DOTALL
)
_PARTE = r"\[[^\]]*\]|\"[^\"]*\"|[A-Za-z_#@][\w$#@]*"
_TOKEN = re.compile(
    rf"(?P<nombre>(?:{_PARTE})(?:\s*\.\s*(?:{_PARTE})?)*)"
    r"|(?P<numero>\d[\w.]*)"
    r"|(?P<signo>[(),;])"
    r"|(?P<otro>\S)"
)
_CTE = re.compile(
    rf"(?:\bWITH|,)\s*({_PARTE})\s*(?:\([^()]*\))?\s*AS\s*\(", re.IGNORECASE
)

_ABREN_LISTA = {"FROM", "JOIN", "APPLY"}
_CIERRAN_LISTA = {
    "WHERE", "GROUP", "ORDER", "HAVING", "UNION", "EXCEPT", "INTERSECT", "ON",
    "SELECT", "OPTION", "FOR", "WINDOW", "PIVOT", "UNPIVOT", "TABLESAMPLE",
}
# Lo que puede seguir al nombre de una tabla sin ser su alias.
_NO_SON_ALIAS = _CIERRAN_LISTA | {
    "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS", "OUTER", "WITH", "NATURAL",
}


def _enmascarar(query: str) -> str:
    def tapar(m):
        if m.group(3) is None:
            return m.group(0)
        return " " * len(m.group(0))
    return _SALTAR.sub(tapar, query)


def _partes(nombre: str) -> list[str]:
    partes, actual = [], ""
    for m in re.finditer(rf"({_PARTE})|\.", nombre):
        if m.group(1):
            actual = m.group(1)
        else:
            partes.append(actual)
            actual = ""
    partes.append(actual)
    return [p[1:-1] if p[:1] in "[\"" else p for p in partes]


@dataclass
class _Referencia:
    inicio: int
    fin: int
    partes: list[str]

    @property
    def objeto(self) -> str:
        return self.partes[-1]


def referencias(query: str) -> list[_Referencia]:
    """Cada tabla o vista que lee la consulta, con su lugar en el texto.

    Son tablas los nombres que van despues de FROM, JOIN o APPLY, los que
    siguen a una coma dentro de esa lista (`FROM a, b`), y cualquier nombre
    con base o esquema (`x.dbo.y`, `dbo.y`) donde sea que aparezca. Los
    nombres de las CTE no cuentan: lo que leen ya se mira dentro.
    """
    texto = _enmascarar(query or "")
    ctes = {_partes(m.group(1))[-1].casefold() for m in _CTE.finditer(texto)}
    tokens = list(_TOKEN.finditer(texto))
    encontradas: list[_Referencia] = []
    profundidad = 0
    en_lista: dict[int, bool] = {}
    espera = False
    for i, token in enumerate(tokens):
        signo = token.group("signo")
        if signo == "(":
            profundidad += 1
            espera = False
            continue
        if signo == ")":
            en_lista.pop(profundidad, None)
            profundidad -= 1
            espera = False
            continue
        if signo == ",":
            if en_lista.get(profundidad):
                espera = True
            continue
        nombre = token.group("nombre")
        if nombre is None:
            espera = False
            continue
        partes = _partes(nombre)
        if len(partes) == 1:
            palabra = partes[0].upper()
            if palabra in _ABREN_LISTA and not nombre.startswith(("[", '"')):
                espera = True
                en_lista[profundidad] = True
                continue
            if palabra in _CIERRAN_LISTA and not nombre.startswith(("[", '"')):
                en_lista[profundidad] = False
        # Un nombre con esquema seguido de "(" es una funcion de usuario
        # (`dbo.fn(x)`): tambien lee tablas, asi que cuenta igual.
        con_esquema = len(partes) >= 3 or (
            len(partes) == 2 and partes[0].casefold() in {"dbo", "sys", "information_schema"}
        )
        if (espera or con_esquema) and partes[-1]:
            if not (len(partes) == 1 and partes[0].casefold() in ctes):
                encontradas.append(_Referencia(token.start(), token.end(), partes))
        espera = False
    return encontradas


# --- Acotar ---------------------------------------------------------------

@dataclass
class Acotada:
    """La consulta ya acotada, o por que no se puede correr."""

    query: str
    fuera: list[str] = field(default_factory=list)        # cadenas no permitidas
    desconocidos: list[str] = field(default_factory=list)  # tablas sin dueño conocido
    acotados: list[str] = field(default_factory=list)      # tablas que se filtraron

    @property
    def bloqueada(self) -> bool:
        return bool(self.fuera or self.desconocidos)


def _alias_despues(texto: str, fin: int) -> bool:
    """Si al nombre de la tabla le sigue su alias."""
    siguiente = _TOKEN.search(texto, fin)
    if siguiente is None or siguiente.group("nombre") is None:
        return False
    partes = _partes(siguiente.group("nombre"))
    return len(partes) == 1 and (
        partes[0].upper() == "AS" or partes[0].upper() not in _NO_SON_ALIAS
    )


def _derivada(ref: _Referencia, original: str, cadenas: frozenset[str], local: bool) -> str:
    clave = _CLAVE[ref.objeto.casefold()]
    if clave == CONSOLIDADA:
        vistas = [v for v in _VISTAS_DE_LA_CONSOLIDADA if _CADENA_DE_VISTA[v] in cadenas]
        if local:
            # En el resumen local las vistas tienen las mismas columnas, y
            # nombrar [Store] o [Date] (que el resumen no trae) lo descartaria.
            return "(" + " UNION ALL ".join(
                f"SELECT * FROM [RAW_DATA_B2B].dbo.[{v}]" for v in vistas
            ) + ")"
        return _union_de(vistas)
    if clave == PROMOTORIA:
        cadena = re.sub(r"\bh\.", "_h.", re.sub(r"\bcs\.", "_cs.", CADENA_DE_PROMOTORIA))
        return (
            f"(SELECT _h.* FROM {original} AS _h LEFT JOIN (SELECT [ID], "
            "MIN([RetailName]) AS [RetailName] FROM [GTM BI Database].dbo."
            "[CustomerStores] GROUP BY [ID]) AS _cs ON _cs.[ID] = _h.[ID] "
            f"WHERE {condicion(cadena, cadenas)})"
        )
    columna = f"[{_POR_COLUMNA[clave]}]"
    return f"(SELECT * FROM {original} WHERE {condicion(columna, cadenas)})"


def acotar(query: str, cadenas: Iterable[str] | None, local: bool = False) -> Acotada:
    """La consulta limitada a esas cadenas. Sin cadenas, tal cual.

    `local` arma la consolidada para el resumen local (ver resumen_local.py)
    en vez de para el servidor.
    """
    permitidas = normalizar(cadenas)
    if permitidas is None:
        return Acotada(query)
    texto = _enmascarar(query)
    resultado = Acotada(query)
    cambios: list[tuple[int, int, str]] = []
    for ref in referencias(query):
        objeto = ref.objeto
        clave = _CLAVE.get(objeto.casefold())
        base = ref.partes[0] if len(ref.partes) >= 3 else None
        if clave is None or len(ref.partes) > 3 or (
            base is not None and base.casefold() not in _BASES
        ):
            if objeto not in resultado.desconocidos:
                resultado.desconocidos.append(objeto)
            continue
        if clave in _NEUTRALES:
            continue
        cadena = _DE_UNA_CADENA.get(clave)
        if cadena is not None:
            if cadena not in permitidas and cadena not in resultado.fuera:
                resultado.fuera.append(cadena)
            continue
        original = query[ref.inicio:ref.fin]
        derivada = _derivada(ref, original, permitidas, local)
        if not _alias_despues(texto, ref.fin):
            derivada += f" AS [{clave}]"
        cambios.append((ref.inicio, ref.fin, derivada))
        if clave not in resultado.acotados:
            resultado.acotados.append(clave)
    if resultado.bloqueada:
        return resultado
    nueva = query
    for inicio, fin, derivada in sorted(cambios, reverse=True):
        nueva = nueva[:inicio] + derivada + nueva[fin:]
    resultado.query = nueva
    return resultado


def motivo(acotada: Acotada, cadenas: Iterable[str]) -> str:
    """Lo que se le dice al modelo cuando la consulta no se corre."""
    permitidas = nombres(normalizar(cadenas) or ())
    if acotada.fuera:
        return (
            f"No la ejecute: este usuario solo tiene acceso a {permitidas}, y la "
            f"consulta lee datos de {nombres(acotada.fuera)}. No busques esos "
            "datos por otro camino: dile al usuario, en una frase, que no tiene "
            f"acceso a esa informacion y que puede consultar {permitidas}."
        )
    return (
        f"No la ejecute: este usuario solo tiene acceso a {permitidas}, y "
        f"{', '.join(acotada.desconocidos)} no se puede limitar por cadena. "
        "Usa las vistas o tablas de sus cadenas (las de cada cadena, la "
        "consolidada o las de inventario, precios y promotoria, que se "
        "limitan solas)."
    )


def instruccion(cadenas: Iterable[str]) -> str:
    """La nota que lee el modelo antes de cada pregunta de un usuario acotado."""
    permitidas = nombres(normalizar(cadenas) or ())
    return (
        f"ACCESO: este usuario SOLO puede ver datos de {permitidas} (venta, "
        "inventario, promotoria y precios). Cada consulta se limita sola a "
        "esas cadenas: la consolidada y las tablas de varias cadenas le "
        "devuelven solo las suyas, y las de otras cadenas no se ejecutan. "
        "Si pregunta por otra cadena, o pide compararse con una que no "
        "tiene, dile en una frase que no tiene acceso a esa informacion. Y "
        f"cuando des un total o un 'mercado', aclara que es de {permitidas}."
    )
