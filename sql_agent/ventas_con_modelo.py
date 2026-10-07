"""La venta oficial con su modelo y su region: las vistas ETL con sus catalogos.

Pedido el 7/10/2026: que las respuestas salgan SOLO de las vistas ETL (las
mismas de Power BI) y que las tablas crudas queden para el Raw_Data del
Excel. El problema era el modelo: el catalogo de homologacion (UNICOS$) solo
casaba directo con las vistas de Coppel, asi que para "por modelo" o "solo
celulares" el bot iba a las crudas, y las crudas no suman igual que las
vistas.

Las vistas si traen el modelo, solo que cada cadena lo escribe distinto
dentro de [Material_Description]. Medido en agosto de 2026 contra UNICOS$:

  Coppel, Sears      la descripcion entera es la FINAL del catalogo  100%
  Elektra, Liverpool,
  Movistar, Telcel   codigo + la DESCRIPCION del catalogo           97-100%
  Liverpool IOT      igual                                          94%
  AT&T               codigo + 'AT&T Cacs/Dealers' + su propio nombre; el
                     modelo sale del codigo, via la tabla cruda (89%)

`vw_ETL_Con_Modelo` no existe en el servidor (el login es de solo lectura,
no se puede crear): es un nombre que el bot puede usar en sus consultas y
que aqui se cambia por la union de las nueve vistas, cada una con su cruce,
antes de que la consulta salga. Trae las columnas de las vistas mas
[Modelo], [Clasificacion] ('SMARTPHONE', 'TABLET'...) y [Region] (R1 a R9,
RM). Lo que no casa queda en NULL, no se pierde: la suma total es la de las
vistas.
"""

from __future__ import annotations

import re
from typing import Iterable

NOMBRE = "vw_ETL_Con_Modelo"
UNICOS = "[GTM BI Database].dbo.[UNICOS$]"

_COLUMNAS = ("Retail", "Store", "Brand", "Material_Description", "Carrier",
             "Date", "Year", "Month", "WEEK", "QTY")

# La descripcion sin la primera palabra (el codigo de articulo).
_SIN_CODIGO = "LTRIM(SUBSTRING(RTRIM(v.[Material_Description]), CHARINDEX(' ', RTRIM(v.[Material_Description]) + ' ') + 1, 600))"

# vista -> (cadena, como se arma la llave del lado de la venta, contra que
# columna del catalogo, y que [Retail] del catalogo).
VISTAS = {
    "vw_ETL_COPPEL": ("COPPEL", "RTRIM(v.[Material_Description])", "MATERIAL DESCRIPTION FINAL", ("COPPEL",)),
    "vw_ETL_COPPEL_IOT": ("COPPEL", "RTRIM(v.[Material_Description])", "MATERIAL DESCRIPTION FINAL", ("COPPEL",)),
    "vw_ETL_Sears": ("SEARS", "RTRIM(v.[Material_Description])", "MATERIAL DESCRIPTION FINAL", ("SEARS",)),
    "vw_ETL_Elektra": ("ELEKTRA", _SIN_CODIGO, "DESCRIPCION", ("ELEKTRA",)),
    "vw_ETL_Liverpool": ("LIVERPOOL", _SIN_CODIGO, "DESCRIPCION", ("Liverpool",)),
    "vw_ETL_Liverpool_IOT": ("LIVERPOOL", _SIN_CODIGO, "DESCRIPCION", ("Liverpool",)),
    # En Movistar la FINAL del catalogo ya trae 'Movistar CACs ...': casa sin el codigo.
    "vw_ETL_Movistar_CAC": ("MOVISTAR", _SIN_CODIGO, "MATERIAL DESCRIPTION FINAL", ("Movistar CACS", "Movistar call center")),
    "vw_ETL_TELCEL_B2B": ("TELCEL", _SIN_CODIGO, "DESCRIPCION", ("TELCEL", "TELCEL CAC")),
    # AT&T va aparte (ver _rama_att): su descripcion no es la del catalogo.
    "vw_ETL_ATT_Postpaid": ("AT&T", "", "", ()),
}


# --- La region de la tienda -------------------------------------------------
#
# Tambien pedido el 7/10/2026, al dejar las crudas fuera de las respuestas:
# la region (R1 a R9, RM) se sacaba uniendo la TABLA CRUDA con CustomerStores,
# porque desde las vistas "no casaban" Telcel ni Sears. Si casan, normalizando
# espacios y, en Telcel, quitando el prefijo de la region de Telcel ('RG09
# LAGUNA DE MAYRAN' -> 'LAGUNA DE MAYRAN'). Medido en agosto de 2026, desde las
# vistas: AT&T, Movistar, Sears y Elektra 100%, Coppel 99.9%, Telcel 99.7%,
# Liverpool 98-99%.

def _normal(columna: str) -> str:
    """Mayusculas y sin espacios dobles: 'L  MONTERREY' es 'L MONTERREY'.

    Cualquier cantidad de espacios seguidos, no solo dos: Sears escribe
    '126      TLALNEPANTLA' (seis) y el catalogo '126 TLALNEPANTLA'; con un
    REPLACE de dos espacios no casaba ni una tienda (medido el 7/10/2026).
    El truco: cada espacio se marca con CHAR(7), se borran los pares
    marca-espacio y luego las marcas, y de una racha queda un solo espacio.
    Tambien el espacio duro (CHAR(160)) y el tabulador.
    """
    limpio = f"LTRIM(RTRIM(REPLACE(REPLACE({columna}, CHAR(160), ' '), CHAR(9), ' ')))"
    colapsado = (
        f"REPLACE(REPLACE(REPLACE({limpio}, ' ', ' ' + CHAR(7)), CHAR(7) + ' ', ''), CHAR(7), '')"
    )
    return f"UPPER({colapsado})"


def _sin_primera_palabra(columna: str) -> str:
    return f"LTRIM(SUBSTRING({columna}, CHARINDEX(' ', {columna} + ' ') + 1, 600))"


def _primera_palabra(columna: str) -> str:
    return f"LEFT({columna}, CHARINDEX(' ', {columna} + ' ') - 1)"


_CATALOGO = "[GTM BI Database].dbo.[CustomerStores]"

# cadena -> como se busca la region si no casa el nombre entero.
_SEGUNDA_LLAVE = {
    "TELCEL": "sin_prefijo",   # 'RG09 LAGUNA DE MAYRAN'
    "ELEKTRA": "numero",       # '100 EKT MEGA LA LUNA': el numero de tienda
}


def _joins_de_region(cadena: str) -> tuple[str, str]:
    """Los LEFT JOIN al catalogo de tiendas y la expresion de [Region]."""
    tienda = _normal("v.[Store]")
    por_nombre = (
        f"LEFT JOIN (SELECT {_normal('[Coppel Name]')} AS [Llave], MIN([Region]) AS [Region] "
        f"FROM {_CATALOGO} WHERE [RetailName] = '{cadena.replace(chr(39), chr(39) * 2)}' "
        f"GROUP BY {_normal('[Coppel Name]')}) r1 ON r1.[Llave] = {tienda}"
    )
    segunda = _SEGUNDA_LLAVE.get(cadena)
    if segunda is None:
        return por_nombre, "r1.[Region]"
    if segunda == "sin_prefijo":
        llave_catalogo, llave_venta = _normal("[Coppel Name]"), _sin_primera_palabra(tienda)
    else:
        llave_catalogo = _primera_palabra(_normal("[Coppel Name]"))
        llave_venta = _primera_palabra(tienda)
    # Por numero, solo si la primera palabra ES un numero: si no, 'EKT'
    # casaria con todas las tiendas que empiezan asi.
    solo_numero = (
        f" AND {llave_catalogo} <> '' AND {llave_catalogo} NOT LIKE '%[^0-9]%'"
        if segunda == "numero" else ""
    )
    otra = (
        f" LEFT JOIN (SELECT {llave_catalogo} AS [Llave], MIN([Region]) AS [Region] "
        f"FROM {_CATALOGO} WHERE [RetailName] = '{cadena}'{solo_numero} "
        f"GROUP BY {llave_catalogo}) r2 ON r2.[Llave] = {llave_venta}"
    )
    return por_nombre + otra, "COALESCE(r1.[Region], r2.[Region])"


# El codigo de articulo: la primera palabra de la descripcion.
_CODIGO = "LEFT(RTRIM(v.[Material_Description]), CHARINDEX(' ', RTRIM(v.[Material_Description]) + ' ') - 1)"


def _rama_att() -> str:
    """AT&T: el modelo sale del codigo de articulo.

    La vista escribe el telefono a su manera ('N.IP16128NE AT&T Dealers
    IPHONE 16 128GB 5G', 'HONOR MAGIC8 LITE'), que no es la del catalogo. La
    tabla cruda tiene el mismo codigo ([Codigo]) junto al nombre que SI casa
    con UNICOS$ ([Model] contra [DESCRIPCION], 89%). La cruda se usa solo
    como diccionario de codigo a modelo: las unidades son las de la vista.
    """
    columnas = ", ".join(f"v.[{c}]" for c in _COLUMNAS)
    joins_region, region = _joins_de_region("AT&T")
    return (
        f"SELECT {columnas}, u.[Modelo], u.[Clasificacion], {region} AS [Region] "
        f"FROM [RAW_DATA_B2B].dbo.[vw_ETL_ATT_Postpaid] v "
        f"LEFT JOIN (SELECT a.[Codigo] AS [Llave], MIN(h.[HOMOLOGACION MODELO]) AS [Modelo], "
        f"MIN(h.[CLASSIFICATION]) AS [Clasificacion] "
        f"FROM (SELECT DISTINCT UPPER(RTRIM([Codigo])) AS [Codigo], UPPER(RTRIM([Model])) AS [Model] "
        f"FROM [RAW_DATA_B2B].dbo.[ATT_B2B]) a "
        f"JOIN {UNICOS} h ON UPPER(RTRIM(h.[DESCRIPCION])) = a.[Model] "
        f"AND h.[Retail] IN ('AT&T CACS', 'AT&T DEALERS') "
        f"GROUP BY a.[Codigo]) u ON u.[Llave] = UPPER({_CODIGO}) "
        f"{joins_region}"
    )


def _rama(vista: str) -> str:
    if vista == "vw_ETL_ATT_Postpaid":
        return _rama_att()
    cadena, llave, columna, retails = VISTAS[vista]
    joins_region, region = _joins_de_region(cadena)
    retails_sql = ", ".join("'" + r.replace("'", "''") + "'" for r in retails)
    filtro = f"[Retail] IN ({retails_sql}) AND " if retails else ""
    columnas = ", ".join(f"v.[{c}]" for c in _COLUMNAS)
    # Una fila del catalogo por llave: si la misma descripcion aparece dos
    # veces, la venta se contaria dos veces.
    return (
        f"SELECT {columnas}, u.[Modelo], u.[Clasificacion], {region} AS [Region] "
        f"FROM [RAW_DATA_B2B].dbo.[{vista}] v "
        f"LEFT JOIN (SELECT UPPER(RTRIM([{columna}])) AS [Llave], "
        f"MIN([HOMOLOGACION MODELO]) AS [Modelo], MIN([CLASSIFICATION]) AS [Clasificacion] "
        f"FROM {UNICOS} WHERE {filtro}[{columna}] IS NOT NULL "
        f"GROUP BY UPPER(RTRIM([{columna}]))) u ON u.[Llave] = UPPER({llave}) "
        f"{joins_region}"
    )


def derivada(cadenas: Iterable[str] | None = None) -> str:
    """La union de las vistas con su modelo, solo de esas cadenas (None, todas)."""
    permitidas = set(cadenas) if cadenas else None
    ramas = [_rama(v) for v, (cadena, *_) in VISTAS.items() if permitidas is None or cadena in permitidas]
    return "(" + " UNION ALL ".join(ramas) + ")"


# Solo despues de FROM o JOIN: un `[vw_ETL_Con_Modelo].[QTY]` en el SELECT
# es una columna, y sigue valiendo porque la union toma ese nombre como alias.
_REFERENCIA = re.compile(
    r"(\b(?:FROM|JOIN)\s+)"
    rf"(?:\[?RAW_DATA_B2B\]?\s*\.\s*\[?dbo\]?\s*\.\s*)?\[?{re.escape(NOMBRE)}\]?(?![\w\]])",
    re.IGNORECASE,
)
_NO_SON_ALIAS = {
    "WHERE", "GROUP", "ORDER", "HAVING", "JOIN", "INNER", "LEFT", "RIGHT", "FULL",
    "CROSS", "OUTER", "ON", "UNION", "EXCEPT", "INTERSECT", "OPTION", "WITH",
}


# `[Retail] = 'Liverpool Offline'`, `v.[Retail] IN ('Coppel', ...)`,
# `[Retail] LIKE '%LIVERPOOL%'`: lo que dice de que cadena es la consulta.
_FILTRO_DE_RETAIL = re.compile(
    r"\[?Retail\]?\s*(NOT\s+)?(=|IN\b|LIKE\b)\s*\(?\s*((?:N?'(?:[^']|'')*'\s*,?\s*)+)\)?",
    re.IGNORECASE,
)
_RETAIL_NEGADO = re.compile(r"\[?Retail\]?\s*(<>|!=|NOT\b)", re.IGNORECASE)


def cadenas_del_filtro(query: str) -> set[str] | None:
    """Las cadenas a las que se acota la consulta por [Retail], o None.

    Visto el 7/10/2026: la capa de Liverpool region 4 de un anio tardo 231 s
    porque la union recorria las nueve cadenas para quedarse con una. Si la
    consulta solo filtra por cadena con = / IN / LIKE, basta con las ramas de
    esas cadenas. Ante cualquier duda (un OR, un NOT, un valor que no es de
    ninguna cadena) se devuelve None y van todas: mas lento, nunca de menos.
    """
    texto = query or ""
    if re.search(r"(?<!\w)OR(?!\w)", texto, re.IGNORECASE) or _RETAIL_NEGADO.search(texto):
        return None
    encontradas = _FILTRO_DE_RETAIL.findall(texto)
    if not encontradas:
        return None
    from acceso import cadena_de  # aqui: acceso importa catalogo, y catalogo es pesado
    cadenas: set[str] = set()
    for negado, _, literales in encontradas:
        if negado:
            return None
        for literal in re.findall(r"'((?:[^']|'')*)'", literales):
            cadena = cadena_de(literal.strip("%").strip())
            if cadena is None:
                return None
            cadenas.add(cadena)
    return cadenas or None


def expandir(query: str, cadenas: Iterable[str] | None = None) -> str:
    """Cambia cada `FROM vw_ETL_Con_Modelo` por la union real. Si no la usa, igual.

    `cadenas` son las que puede ver el usuario (None, todas). Si ademas la
    consulta se acota a unas cadenas por [Retail], solo van esas ramas.
    """
    if NOMBRE.casefold() not in (query or "").casefold():
        return query
    filtradas = cadenas_del_filtro(query)
    if filtradas is not None:
        cadenas = filtradas if not cadenas else (set(cadenas) & filtradas) or set(cadenas)
    union = derivada(cadenas)

    def cambio(m: re.Match) -> str:
        resto = query[m.end():]
        siguiente = re.match(r"\s+(?:AS\s+)?(\[?[A-Za-z_]\w*\]?)", resto, re.IGNORECASE)
        if siguiente and siguiente.group(1).strip("[]").upper() not in _NO_SON_ALIAS:
            return m.group(1) + union
        return f"{m.group(1)}{union} AS [{NOMBRE}]"

    return _REFERENCIA.sub(cambio, query)
