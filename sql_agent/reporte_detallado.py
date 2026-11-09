"""El reporte detallado de venta: por cadena, mes, tienda, modelo... y precio.

Pedido el 19/10/2026, despues de dos fallas el mismo dia:

- "un reporte de mayo a la fecha con canal, mes, tienda, modelo y precio":
  el modelo escribio UNA consulta que cruzaba Prices (2.4 millones de filas)
  con UNICOS$ en el servidor, y se corto a los 507 s sin entregar nada.
- "un archivo por cada cadena": 9 min 50 s, y llegaron Coppel y Sears; las
  otras cinco cadenas las cruzo con UNICOS$ de una forma que solo casa el
  3.6% y el guardian se las rechazo 13 veces.

Medido ese dia: la venta por cadena, tienda, modelo y mes sale en ~1 minuto
por cadena desde vw_ETL_Con_Modelo (que ya hace el cruce bien, ver
ventas_con_modelo.py), y los precios agrupados por cadena, producto y mes en
3.6 s (52 mil filas). Lo lento era cruzarlos en el servidor. Aqui se piden
por separado y se cruzan en pandas, con el mismo catalogo de homologacion
que la columna [Modelo] del Excel.

El modelo solo elige cadenas, periodo y columnas; la consulta es siempre la
misma. Lo que no esta en UNICOS$ va con el [Modelo] vacio (pedido el
19/10/2026) y su descripcion de la cadena al lado, para saber que es.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable

import pandas as pd

import acceso
import ventas_con_modelo

PRECIOS = "[GTM BI Database].dbo.[Prices]"
SCRAP = "[Scrap].dbo.[Complete_Scrap]"

# De donde salio el precio de cada fila. Pedido el 21/10/2026: si Prices no
# lo tiene, el de la pagina de la tienda (el scrap), dicho. No es el mismo
# precio -- Prices es el de venta interno; el scrap, el publicado en linea --
# y por eso nunca se mezclan sin que se note.
FUENTE = "Fuente del precio"
DE_PRICES = "Prices"
DE_INTERNET = "Internet (pagina de la tienda)"
DE_LOS_DOS = "Prices e internet"

# Lo que se puede pedir, ademas del año y el mes que van siempre:
# nombre que usa el modelo -> (expresion en vw_ETL_Con_Modelo, columna del Excel).
COLUMNAS = {
    "canal": ("[Retail]", "Canal"),
    "tienda": ("[Store]", "Tienda"),
    "region": ("[Region]", "Region"),
    "operador": ("[Carrier]", "Operador"),
    "marca": ("[Brand]", "Marca"),
    "modelo": ("[Modelo]", "Modelo"),
    "clasificacion": ("[Clasificacion]", "Clasificacion"),
    # La semana de la venta (domingo a sabado): para ver el precio semana a
    # semana. No va por defecto: por tienda, Coppel pasaria de un millon de
    # filas a varios.
    "semana": ("[WEEK]", "Semana"),
    # El dia de la venta. Visto el 5/11/2026: "por tienda, modelo y dia" se
    # entrego por mes porque no estaba. Con precio, cada fila lleva el de su
    # dia (PRECIO_DEL_DIA): no hay que escoger cual.
    "dia": ("CAST([Date] AS date)", "Día"),
}
# Lo que no esta en UNICOS$: su texto de la cadena, solo en esas filas.
DESCRIPCION = "Descripcion de la cadena"
_EXPRESION_DESCRIPCION = "CASE WHEN [Modelo] IS NULL THEN RTRIM([Material_Description]) END"
PRECIO = "Precio promedio"
PRECIO_DEL_DIA = "Precio del dia"

# Que precio va cuando cambio dentro del mes. Pedido el 19/10/2026: que se le
# pregunte al usuario cual prefiere, y que se puedan ver todos los precios con
# lo que se vendio a cada uno.
MODOS_DE_PRECIO = {
    "ponderado": "Precio promedio",           # por lo vendido cada dia
    "ultimo": "Precio al cierre del mes",
    "primero": "Precio al inicio del mes",
    "minimo": "Precio mas bajo del mes",
    "maximo": "Precio mas alto del mes",
    "desglosado": "Precio",                   # una fila por precio
}
_ALIAS_DE_MODO = {
    "promedio": "ponderado", "ponderado": "ponderado", "último": "ultimo", "ultimo": "ultimo",
    "cierre": "ultimo", "final": "ultimo", "primero": "primero", "inicio": "primero",
    "minimo": "minimo", "mínimo": "minimo", "mas bajo": "minimo", "más bajo": "minimo",
    "maximo": "maximo", "máximo": "maximo", "mas alto": "maximo", "más alto": "maximo",
    "desglosado": "desglosado", "todos": "desglosado", "todos los precios": "desglosado",
}


def modo_de_precio(texto: str) -> str:
    """'el ultimo' -> 'ultimo'; vacio -> ''. Uno que no se entiende, ValueError."""
    limpio = (texto or "").strip().casefold()
    if not limpio:
        return ""
    for alias, modo in _ALIAS_DE_MODO.items():
        if alias in limpio:
            return modo
    raise ValueError(f"No entiendo el precio {texto!r}. Opciones: {', '.join(MODOS_DE_PRECIO)}.")

_ALIAS_DE_CADENA = {
    "ATT": "AT&T", "AT T": "AT&T", "AT&T CACS": "AT&T", "AT&T DEALERS": "AT&T",
    "TELCEL CAC": "TELCEL", "MOVISTAR CACS": "MOVISTAR", "MOVISTAR CAC": "MOVISTAR",
    "LIVERPOOL OFFLINE": "LIVERPOOL", "LIVERPOOL ONLINE": "LIVERPOOL", "SEAS": "SEARS",
}


def cadena_de(retail) -> str | None:
    """La cadena de un valor de [Retail] ('AT&T Cacs' -> 'AT&T'), de la venta o de Prices."""
    texto = str(retail or "").strip().upper()
    if texto.startswith("UNKNOWN"):  # en la venta, Unknown es AT&T
        return "AT&T"
    if re.match(r"AT\s*&?\s*T\b", texto):  # "att cacs", como lo escribe la gente
        return "AT&T"
    for cadena in acceso.CADENAS:
        if texto.startswith(cadena):
            return cadena
    return None


def cadenas_pedidas(texto: str) -> list[str]:
    """'coppel, telcel cac, att' -> ['COPPEL', 'TELCEL', 'AT&T']; vacio o 'todas', todas."""
    partes = [p.strip().upper() for p in re.split(r"[,;/]|\by\b|\band\b", texto or "", flags=re.I)]
    partes = [p for p in partes if p]
    if not partes or any(p in ("TODAS", "ALL", "TODOS") for p in partes):
        return list(acceso.CADENAS)
    vistas: list[str] = []
    for parte in partes:
        # "att cacs y dealers": el canal suelto es de la cadena de antes.
        if parte in ("DEALERS", "CACS", "CAC", "CALL CENTER", "ONLINE", "OFFLINE") and vistas:
            continue
        cadena = _ALIAS_DE_CADENA.get(parte) or cadena_de(parte)
        if cadena is None:
            raise ValueError(
                f"No conozco la cadena {parte!r}. Las que hay: {', '.join(acceso.CADENAS)}."
            )
        if cadena not in vistas:
            vistas.append(cadena)
    return vistas


def canales_pedidos(texto: str, cadenas: Iterable[str]) -> tuple[str, ...]:
    """'Liverpool Offline' -> ('Liverpool Offline',). Cada canal tiene que ser
    de una de las cadenas pedidas; vacio, todos."""
    canales = tuple(re.sub(r"\s+", " ", c).strip() for c in (texto or "").split(",") if c.strip())
    validas = set(cadenas)
    for canal in canales:
        if cadena_de(canal) not in validas:
            raise ValueError(
                f"El canal {canal!r} no es de {', '.join(sorted(validas))}: pide tambien su cadena "
                "o escribelo como en [Retail] ('Liverpool Offline', 'TELCEL CAC')."
            )
    return canales


def mes_de(texto: str, defecto: date) -> tuple[int, int]:
    """'2026-05' o '05/2026' -> (2026, 5)."""
    if not (texto or "").strip():
        return defecto.year, defecto.month
    m = re.fullmatch(r"\s*(\d{4})[-/](\d{1,2})\s*", texto) or re.fullmatch(r"\s*(\d{1,2})[-/](\d{4})\s*", texto)
    if not m:
        raise ValueError(f"No entiendo el mes {texto!r}: escribelo como AAAA-MM (2026-05).")
    a, b = int(m.group(1)), int(m.group(2))
    anio, mes = (a, b) if a > 12 else (b, a)
    if not 1 <= mes <= 12:
        raise ValueError(f"No entiendo el mes {texto!r}: escribelo como AAAA-MM (2026-05).")
    return anio, mes


def _filtro_de_periodo(desde: tuple[int, int], hasta: tuple[int, int]) -> str:
    # Por [Year]/[Month] y no por [Date]: en las vistas va mucho mas rapido.
    (a1, m1), (a2, m2) = desde, hasta
    return f"([Year] * 100 + [Month]) BETWEEN {a1 * 100 + m1} AND {a2 * 100 + m2}"


def columnas_pedidas(texto: str) -> list[str]:
    """'canal, mes, tienda, modelo' -> ['canal', 'tienda', 'modelo'] (año y mes van siempre)."""
    nombres = []
    for parte in re.split(r"[,;/]|\by\b|\band\b", (texto or "").casefold()):
        parte = parte.strip()
        parte = {"cadena": "canal", "retail": "canal", "store": "tienda", "carrier": "operador",
                 "brand": "marca", "model": "modelo", "región": "region", "clasificación": "clasificacion",
                 "week": "semana", "semanas": "semana", "día": "dia", "días": "dia", "dias": "dia",
                 "diario": "dia", "day": "dia", "mes": "", "año": "", "anio": "", "fecha": "", "precio": "", "ventas": "",
                 "unidades": ""}.get(parte, parte)
        if not parte:
            continue
        if parte not in COLUMNAS:
            raise ValueError(
                f"No puedo dar la columna {parte!r}. Las que hay: {', '.join(COLUMNAS)}."
            )
        if parte not in nombres:
            nombres.append(parte)
    return nombres or ["canal", "modelo"]


def va_del_resumen(columnas: list[str], precio: bool, modo: str) -> bool:
    """Si la venta puede salir del resumen local (resumen_local.leer_con_modelo):
    no tiene la tienda ni el dia, ni la venta por dia que necesitan el
    ponderado y el desglose. Medido el 5/11/2026: todas las marcas por modelo
    y mes de las siete cadenas tardo 6.5 min en el servidor."""
    return not {"tienda", "dia"} & set(columnas) and not (precio and modo in ("ponderado", "desglosado"))


def sql_de_ventas(cadena: str, columnas: list[str], desde, hasta, marca: str = "",
                  diario: bool | str = False, canales: Iterable[str] = (),
                  del_resumen: bool = False) -> str:
    """La venta de UNA cadena, agrupada. Siempre por [Retail] (para saber la
    cadena de cada fila y cruzar el precio) y por año y mes.

    Con `diario`, la misma lectura de la vista trae ademas la venta de cada
    modelo POR DIA ([_detalle] = 0), para darle a cada dia el precio de ese
    dia (ver precio_por_fecha). Va en la misma consulta, con GROUPING SETS:
    recorrer la vista dos veces costaria el doble (Telcel: ~4 min cada vez).

    Con `diario="completo"` (el desglose de precios), cada fila del reporte
    por dia: para saber cuanto vendio esa tienda a cada precio.

    Con `del_resumen`, la consulta es para la tabla del resumen local (ver
    va_del_resumen), que ya trae el texto de la cadena y la cadena de cada fila.
    """
    descripcion = "[Descripcion]" if del_resumen else _EXPRESION_DESCRIPCION
    expresiones = ["[Retail] AS [Canal]", "[Year] AS [Año]", "[Month] AS [Mes]"]
    agrupar = ["[Retail]", "[Year]", "[Month]"]
    por_dia = ["[Retail]", "[Year]", "[Month]", "[Modelo]", "[Date]"]
    for nombre in columnas:
        expresion, alias = COLUMNAS[nombre]
        if nombre == "canal":
            continue
        expresiones.append(f"{expresion} AS [{alias}]")
        agrupar.append(expresion)
        if nombre == "semana":
            por_dia.append(expresion)
        if nombre == "modelo":
            expresiones.append(f"{descripcion} AS [{DESCRIPCION}]")
            agrupar.append(descripcion)
    filtros = [_filtro_de_periodo(desde, hasta)]
    if del_resumen:
        filtros.append(f"[Cadena] = '{cadena.replace(chr(39), chr(39) * 2)}'")
    if marca:
        filtros.append(f"[Brand] = '{marca.replace(chr(39), chr(39) * 2)}'")
    de_la_cadena = [c for c in canales if cadena_de(c) == cadena]
    if de_la_cadena:
        lista = ", ".join("'" + c.replace("'", "''") + "'" for c in de_la_cadena)
        filtros.append(f"RTRIM([Retail]) IN ({lista})")
    if diario == "completo":
        if "[Modelo]" not in agrupar:
            raise ValueError("el precio va por modelo: pide la columna modelo")
        expresiones.append("[Date] AS [Dia]")
        grupos = ", ".join(agrupar + ["[Date]"])
    elif diario:
        if "[Modelo]" not in agrupar:
            raise ValueError("el precio va por modelo: pide la columna modelo")
        expresiones += ["[Date] AS [Dia]", "GROUPING([Date]) AS [_detalle]"]
        grupos = f"GROUPING SETS (({', '.join(agrupar)}), ({', '.join(por_dia)}))"
    else:
        grupos = ", ".join(agrupar)
    return (
        f"SELECT {', '.join(expresiones)}, SUM([QTY]) AS [Unidades] "
        f"FROM {f'[{ventas_con_modelo.NOMBRE}]' if del_resumen else ventas_con_modelo.derivada([cadena])} AS [v] "
        f"WHERE {' AND '.join(filtros)} "
        f"GROUP BY {grupos}"
    )


def sql_de_precios(desde, hasta) -> str:
    """El precio de venta por cadena, producto y DIA.

    Pedido el 19/10/2026: que el precio concuerde con la fecha, porque cambia
    dentro del mes -- en septiembre de 2026 cambio de precio el 44% de los
    productos de Coppel, el 22% de Telcel y el 16-19% de Liverpool. Medido ese
    dia: Coppel, Liverpool, Telcel y AT&T traen un precio por dia; Elektra
    uno por semana (el domingo); Sears y Movistar uno al cierre del mes.
    """
    (a1, m1), (a2, m2) = desde, hasta
    return (
        "SELECT [Retail], [Material Description] AS [Producto], "
        "CAST([Día/Periodo] AS date) AS [Dia], AVG([Precio Venta]) AS [Precio] "
        f"FROM {PRECIOS} "
        # Un mes antes: la venta del dia 1 toma el ultimo precio anterior.
        f"WHERE [Día/Periodo] >= DATEADD(MONTH, -1, '{a1:04d}-{m1:02d}-01') "
        f"AND [Día/Periodo] < DATEADD(MONTH, 1, '{a2:04d}-{m2:02d}-01') "
        "AND [Precio Venta] > 0 "
        "GROUP BY [Retail], [Material Description], CAST([Día/Periodo] AS date)"
    )


def sql_de_scrap(desde, hasta) -> str:
    """El precio publicado en la pagina de cada tienda, por modelo y dia.

    El scrap ya trae el modelo homologado ([approved_name]). Un mismo modelo
    sale varias veces el mismo dia (por operador, color o vendedor: 26% de
    las veces, con 29% de diferencia tipica, medido el 21/10/2026); va el mas
    bajo publicado, como cuando el bot responde un precio de internet.
    Mercado Libre y Suburbia no son cadenas del bot. 14 s, medido.
    """
    (a1, m1), (a2, m2) = desde, hasta
    return (
        "SELECT [retail] AS [Retail], [approved_name] AS [Producto], "
        "CAST([consultation_date] AS date) AS [Dia], MIN([price]) AS [Precio] "
        f"FROM {SCRAP} WITH (NOLOCK) "
        f"WHERE [consultation_date] >= DATEADD(MONTH, -1, '{a1:04d}-{m1:02d}-01') "
        f"AND [consultation_date] < DATEADD(MONTH, 1, '{a2:04d}-{m2:02d}-01') "
        "AND [price] > 0 AND [approved_name] IS NOT NULL "
        "AND [retail] NOT IN ('Mercado Libre', 'Suburbia') "
        "GROUP BY [retail], [approved_name], CAST([consultation_date] AS date)"
    )


# AT&T en Prices: 'AT&T CACS APPLE IPHONE 15 256GB 4G'; en UNICOS$:
# 'AT&T CACS APPLE IPHONE 15 256GB', sin la red. Medido el 19/10/2026: tal
# cual casaba el 0% de AT&T; sin la red, dos de cada tres productos. El resto
# esta escrito distinto en las dos tablas y queda sin precio: un precio mal
# asignado es peor que uno vacio.
_RED_AL_FINAL = re.compile(r"\s+\d(?:\.\d)?G$")


# Y lo que UNICOS$ no tiene dado de alta en la cadena, pero si existe como
# modelo homologado: Prices lo escribe con el nombre de la cadena adelante y
# casi igual que el modelo ('AT&T CACS HONOR X6C 256GB 4G' es 'HONOR X6C
# 256GB 4G'). Medido el 21/10/2026, AT&T de mayo a octubre: las unidades con
# precio pasaron de 44% a 95%. Solo se aceptan coincidencias sin duda: el
# mismo texto sin espacios ('MAGIC8' = 'MAGIC 8'), con las marcas escritas
# como en UNICOS$ (GALAXY, MOTO e IPHONE sin APPLE), o sin la red si ese
# modelo existe en una sola.
_MARCAS_DE_PRICES = (
    (re.compile(r"\bSAMSUNG GALAXY\b"), "SAMSUNG"),
    (re.compile(r"\bMOTOROLA MOTO\b"), "MOTOROLA"),
    (re.compile(r"^IPHONE\b"), "APPLE IPHONE"),
)


def _compacto(texto) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(texto).upper())


@dataclass
class IndiceDeModelos:
    """Los modelos homologados por su texto sin espacios, con y sin la red;
    una llave que da dos modelos no sirve (es una duda)."""
    por_nombre: dict[str, set[str]] = field(default_factory=dict)
    por_base: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def de(cls, modelos: Iterable[str]) -> "IndiceDeModelos":
        indice = cls()
        for modelo in {m for m in modelos if m}:
            indice.por_nombre.setdefault(_compacto(modelo), set()).add(modelo)
            indice.por_base.setdefault(_compacto(_RED_AL_FINAL.sub("", modelo)), set()).add(modelo)
        return indice

    def buscar(self, texto: str, retail: str = "") -> str | None:
        t = re.sub(r"\s+", " ", texto.upper()).strip()
        prefijo = re.sub(r"\s+", " ", str(retail or "").upper()).strip()
        if prefijo and t.startswith(prefijo + " "):
            t = t[len(prefijo) + 1:]
        for patron, cambio in _MARCAS_DE_PRICES:
            t = patron.sub(cambio, t)
        for indice, llave in ((self.por_nombre, _compacto(t)),
                              (self.por_base, _compacto(_RED_AL_FINAL.sub("", t)))):
            encontrados = indice.get(llave, set())
            if len(encontrados) == 1:
                return next(iter(encontrados))
        return None


def modelo_del_precio(texto: str, buscar: Callable[[str], str | None], retail: str = "",
                      indice: IndiceDeModelos | None = None) -> str | None:
    if not isinstance(texto, str):
        return None
    encontrado = buscar(texto) or buscar(_RED_AL_FINAL.sub("", texto.strip()))
    if encontrado or indice is None:
        return encontrado
    return indice.buscar(texto, retail)


# Un precio vale hasta una semana despues de su fecha: alcanza para el de
# Elektra (cada domingo) y para un dia sin precio en las diarias, y no deja
# que un precio de hace un mes pase por el de hoy.
VIGENCIA_DE_UN_PRECIO = pd.Timedelta(days=6)


def _como_dia(columna: pd.Series) -> pd.Series:
    """Fechas sin hora y con la misma precision de los dos lados: el driver
    las da en segundos o en microsegundos, y merge_asof no cruza las dos."""
    return pd.to_datetime(columna).dt.normalize().astype("datetime64[ns]")


def precios_por_modelo(precios: pd.DataFrame, buscar: Callable[[str], str | None],
                       modelos: Iterable[str] | None = None,
                       buscar_en_cadena: Callable[[str, str], str | None] | None = None) -> pd.DataFrame:
    """Los precios por dia con su modelo homologado y sus dos llaves: el
    [Retail] exacto (Liverpool Offline no cuesta lo que Online) y la cadena,
    para los canales que no tienen precio propio (Telcel Retail usa el de
    Telcel CAC). `mensual` marca las series de un precio al mes (Sears,
    Movistar): esas se cruzan por mes y no por dia."""
    columnas = ["retail", "cadena", "Modelo", "Dia", "Año", "Mes", "Precio", "mensual"]
    if precios is None or precios.empty:
        return pd.DataFrame(columns=columnas)
    p = precios.copy()
    indice = IndiceDeModelos.de(modelos) if modelos is not None else None
    pares = p[["Producto", "Retail"]].dropna().drop_duplicates()
    # Primero con la regla de la venta de su cadena (ver
    # homologacion.buscar_como_la_venta): asi el precio y la venta del mismo
    # producto salen con el mismo modelo.
    def del_precio(x, r):
        if buscar_en_cadena is not None:
            cadena = cadena_de(r)
            encontrado = buscar_en_cadena(x, cadena) if cadena else None
            if encontrado:
                return encontrado
        return modelo_del_precio(x, buscar, r, indice)

    modelo = {(x, r): del_precio(x, r) for x, r in pares.itertuples(index=False)}
    p["Modelo"] = [modelo.get((x, r)) for x, r in zip(p["Producto"], p["Retail"])]
    p = p.dropna(subset=["Modelo"])
    p["Dia"] = _como_dia(p["Dia"])
    p["Año"], p["Mes"] = p["Dia"].dt.year, p["Dia"].dt.month
    p["retail"] = p["Retail"].astype(str).str.strip().str.casefold()
    p["cadena"] = p["Retail"].map(cadena_de)
    fechas = p.groupby("retail")["Dia"].nunique()
    meses = p.groupby("retail")[["Año", "Mes"]].apply(lambda g: len(g.drop_duplicates()))
    mensuales = set(fechas[fechas <= meses].index)
    p["mensual"] = p["retail"].isin(mensuales)
    return p[columnas]


def scrap_por_modelo(scrap: pd.DataFrame | None, modelos: Iterable[str] | None) -> pd.DataFrame:
    """El scrap en la forma de precios_por_modelo, solo por cadena (sus
    tiendas no se parten en canales: 'Liverpool', no Offline/Online). El
    modelo es el suyo si existe en UNICOS$, o el que case sin duda."""
    columnas = ["retail", "cadena", "Modelo", "Dia", "Año", "Mes", "Precio", "mensual"]
    if scrap is None or scrap.empty:
        return pd.DataFrame(columns=columnas)
    s = scrap.copy()
    conocidos = set(modelos or ())
    indice = IndiceDeModelos.de(conocidos) if conocidos else None
    nombres = {}
    for nombre in s["Producto"].dropna().unique():
        limpio = re.sub(r"\s+", " ", str(nombre)).strip()
        if not conocidos or limpio in conocidos:
            nombres[nombre] = limpio
        else:
            nombres[nombre] = indice.buscar(limpio) if indice else None
    s["Modelo"] = s["Producto"].map(nombres)
    s["cadena"] = s["Retail"].map(cadena_de)
    s = s.dropna(subset=["Modelo", "cadena"])
    s["Dia"] = _como_dia(s["Dia"])
    s["Año"], s["Mes"] = s["Dia"].dt.year, s["Dia"].dt.month
    s["retail"] = ""
    s["mensual"] = False
    s["Precio"] = pd.to_numeric(s["Precio"], errors="coerce")
    return s[columnas]


def _precio_y_fuente(d: pd.DataFrame, precios: pd.DataFrame,
                     scrap: pd.DataFrame | None) -> tuple[pd.Series, pd.Series]:
    """El precio de cada dia y de donde salio: Prices (del canal exacto, si
    no de su cadena) y, si no tiene, el scrap de esa cadena."""
    precio = _precio_en(d, precios, "retail").fillna(_precio_en(d, precios, "cadena"))
    fuente = pd.Series(None, index=d.index, dtype=object).where(precio.isna(), DE_PRICES)
    if scrap is not None and not scrap.empty:
        de_internet = _precio_en(d, scrap, "cadena")
        hueco = precio.isna() & de_internet.notna()
        precio = precio.where(~hueco, de_internet)
        fuente = fuente.where(~hueco, DE_INTERNET)
    return precio, fuente


def _fuente_de(fuentes: pd.Series):
    distintas = set(fuentes.dropna())
    if not distintas:
        return None
    return distintas.pop() if len(distintas) == 1 else DE_LOS_DOS


def _precio_en(dias: pd.DataFrame, precios: pd.DataFrame, llave: str) -> pd.Series:
    """El precio de cada fila de `dias` por `llave` (retail o cadena): el de
    su dia (o el ultimo de la semana anterior), o el de su mes si la serie es
    mensual. NaN si no hay."""
    resultado = pd.Series(float("nan"), index=dias.index)
    if precios.empty or dias.empty:
        return resultado
    serie = precios.dropna(subset=[llave]).groupby([llave, "Modelo", "Dia", "Año", "Mes", "mensual"],
                                                    as_index=False)["Precio"].mean()
    mensual = set(serie.loc[serie["mensual"], llave])
    es_mensual = dias[llave].isin(mensual)
    # Por mes: Sears y Movistar ponen un precio al cierre.
    por_mes = serie[serie["mensual"]].groupby([llave, "Modelo", "Año", "Mes"], as_index=False)["Precio"].mean()
    if not por_mes.empty and es_mensual.any():
        unidos = dias.loc[es_mensual, [llave, "Modelo", "Año", "Mes"]].reset_index().merge(
            por_mes, on=[llave, "Modelo", "Año", "Mes"], how="left").set_index("index")
        resultado.loc[unidos.index] = unidos["Precio"]
    # Por dia: el precio vigente ese dia.
    diarios = serie[~serie["mensual"]].sort_values("Dia")
    if not diarios.empty and (~es_mensual).any():
        izquierda = dias.loc[~es_mensual, [llave, "Modelo", "Dia"]].reset_index()
        izquierda["Dia"] = _como_dia(izquierda["Dia"])
        izquierda = izquierda.sort_values("Dia")
        diarios = diarios.assign(Dia=_como_dia(diarios["Dia"])).sort_values("Dia")
        unidos = pd.merge_asof(
            izquierda, diarios[[llave, "Modelo", "Dia", "Precio"]], on="Dia", by=[llave, "Modelo"],
            direction="backward", tolerance=VIGENCIA_DE_UN_PRECIO,
        ).set_index("index")
        resultado.loc[unidos.index] = unidos["Precio"]
    return resultado


def precio_por_fecha(dias: pd.DataFrame, precios: pd.DataFrame, llaves: list[str],
                     scrap: pd.DataFrame | None = None) -> pd.DataFrame:
    """El precio de cada fila del reporte, de acuerdo con CUANDO se vendio.

    `dias` es la venta por modelo y dia. Cada dia toma el precio de ese dia
    (del canal exacto, si no de su cadena) y el precio de la fila es el
    promedio ponderado por las unidades de cada dia: si en septiembre se
    vendieron 10 a $5,000 y 30 a $4,000, el precio es $4,250. Devuelve
    `llaves` + [Precio promedio] + [Fuente del precio]."""
    if dias.empty:
        return pd.DataFrame(columns=llaves + [PRECIO, FUENTE])
    d = dias.copy()
    d["Dia"] = _como_dia(d["Dia"])
    d["retail"] = d["Canal"].astype(str).str.strip().str.casefold()
    d["cadena"] = d["Canal"].map(cadena_de)
    d["Precio"], d["fuente"] = _precio_y_fuente(d, precios, scrap)
    d["Unidades"] = pd.to_numeric(d["Unidades"], errors="coerce").fillna(0.0)
    con = d[d["Precio"].notna() & (d["Unidades"] > 0)].copy()
    if con.empty:
        return pd.DataFrame(columns=llaves + [PRECIO, FUENTE])
    con["importe"] = con["Precio"] * con["Unidades"]
    grupos = con.groupby(llaves)
    suma = grupos[["importe", "Unidades"]].sum().reset_index()
    suma[PRECIO] = (suma["importe"] / suma["Unidades"]).round(2)
    suma[FUENTE] = grupos["fuente"].agg(_fuente_de).values
    return suma[llaves + [PRECIO, FUENTE]]


def precio_del_mes(precios: pd.DataFrame, modo: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """El primero, el ultimo, el mas bajo o el mas alto de cada mes, por
    [Retail] exacto y por cadena (el respaldo, como en el ponderado). Con el
    scrap da lo mismo, por cadena (su [retail] va vacio)."""
    funcion = {"primero": "first", "ultimo": "last", "minimo": "min", "maximo": "max"}[modo]
    ordenados = precios.sort_values("Dia")
    exacto = ordenados.groupby(["retail", "Modelo", "Año", "Mes"], as_index=False)["Precio"].agg(funcion)
    por_cadena = (ordenados.dropna(subset=["cadena"])
                  .groupby(["cadena", "Modelo", "Año", "Mes", "Dia"], as_index=False)["Precio"].mean()
                  .groupby(["cadena", "Modelo", "Año", "Mes"], as_index=False)["Precio"].agg(funcion))
    return exacto, por_cadena


def cuanto_varia(precios: pd.DataFrame, cadenas: Iterable[str], desde, hasta) -> dict[str, float]:
    """Por cadena, que parte de sus modelos cambio de precio dentro de algun
    mes del periodo (0.44 = 44%). Solo las que tienen alguno."""
    if precios is None or precios.empty:
        return {}
    (a1, m1), (a2, m2) = desde, hasta
    p = precios[(precios["Año"] * 100 + precios["Mes"]).between(a1 * 100 + m1, a2 * 100 + m2)]
    p = p[p["cadena"].isin(set(cadenas))]
    if p.empty:
        return {}
    distintos = p.assign(Precio=p["Precio"].round(0)).groupby(
        ["cadena", "retail", "Modelo", "Año", "Mes"])["Precio"].nunique()
    por_modelo = (distintos > 1).groupby(level=["cadena", "Modelo"]).any()
    fraccion = por_modelo.groupby(level="cadena").mean()
    return {c: float(f) for c, f in fraccion.items() if f > 0}


@dataclass
class Parte:
    """Lo de una cadena: su tabla y como salio."""
    cadena: str
    tabla: pd.DataFrame
    unidades: float = 0.0
    sin_modelo: float = 0.0  # fraccion de las unidades sin modelo homologado
    con_precio: float | None = None  # fraccion de las unidades con precio
    de_internet: float = 0.0  # fraccion de las unidades con precio del scrap


@dataclass
class Reporte:
    partes: list[Parte] = field(default_factory=list)
    desde: tuple[int, int] = (0, 0)
    hasta: tuple[int, int] = (0, 0)
    columnas: list[str] = field(default_factory=list)
    precio: bool = False
    modo: str = "ponderado"
    # Los filtros, para decirlos. Visto el 6/11/2026: sin ellos, el revisor
    # leyo "LIVERPOOL: 12,843" y corrigio el mensaje de "solo Liverpool
    # Online" a que no se sabia cuanto era de Liverpool Online.
    marca: str = ""
    canales: tuple[str, ...] = ()

    @property
    def filtros(self) -> list[str]:
        return [f for f in (self.marca, *self.canales) if f]

    @property
    def unidades(self) -> float:
        return sum(p.unidades for p in self.partes)


def armar_parte(cadena: str, ventas: pd.DataFrame, columnas: list[str],
                precios: pd.DataFrame | None, modo: str = "ponderado",
                scrap: pd.DataFrame | None = None) -> Parte:
    """Ordena la tabla de una cadena, le pone el precio y la mide.

    Con `precios`, `ventas` trae tambien la venta por dia ([_detalle] = 0,
    ver sql_de_ventas): con ella se pone el precio de acuerdo con la fecha."""
    t = ventas.copy()
    # En la venta, Unknown es AT&T.
    t["Canal"] = t["Canal"].where(~t["Canal"].astype(str).str.upper().str.startswith("UNKNOWN"), "AT&T")
    dias = None
    if "_detalle" in t.columns:
        dias = t[t["_detalle"] == 0]
        t = t[t["_detalle"] == 1].drop(columns=["_detalle", "Dia"])
    t["Unidades"] = pd.to_numeric(t["Unidades"], errors="coerce").fillna(0.0)
    por_dia = "Día" in t.columns
    nombre_del_precio = PRECIO_DEL_DIA if por_dia else MODOS_DE_PRECIO.get(modo, PRECIO)
    if "Modelo" in t.columns:
        t["Modelo"] = t["Modelo"].fillna("")
        t[DESCRIPCION] = t[DESCRIPCION].fillna("")
    total = float(t["Unidades"].sum())
    sin_modelo = (
        float(t.loc[t["Modelo"] == "", "Unidades"].sum()) / total
        if "Modelo" in t.columns and total else 0.0
    )
    cubierto = None
    internet = 0.0
    if precios is not None and "Modelo" in t.columns:
        if por_dia:
            con_fecha = t.assign(Dia=_como_dia(t["Día"]), retail=t["Canal"].astype(str).str.strip().str.casefold(),
                                 cadena=t["Canal"].map(cadena_de))
            precio, fuente = _precio_y_fuente(con_fecha, precios, scrap)
            t[nombre_del_precio], t[FUENTE] = precio.round(2), fuente
        elif modo == "desglosado" and "Dia" in t.columns:
            # Cada dia con su precio, y se suma lo vendido a cada precio: una
            # fila por precio, con sus unidades.
            t["Dia"] = _como_dia(t["Dia"])
            t["retail"] = t["Canal"].astype(str).str.strip().str.casefold()
            t["cadena"] = t["Canal"].map(cadena_de)
            precio, fuente = _precio_y_fuente(t, precios, scrap)
            t[nombre_del_precio], t[FUENTE] = precio.round(2), fuente
            fila = [c for c in t.columns if c not in ("Dia", "retail", "cadena", "Unidades")]
            t = t.groupby(fila, as_index=False, dropna=False, sort=False)["Unidades"].sum()
        elif modo == "ponderado" and dias is not None:
            llaves = ["Canal", "Año", "Mes"] + (["Semana"] if "Semana" in t.columns else []) + ["Modelo"]
            precio = precio_por_fecha(dias.dropna(subset=["Modelo"]), precios, llaves, scrap)
            t = t.merge(precio, on=llaves, how="left")
        elif modo in ("primero", "ultimo", "minimo", "maximo"):
            exacto, por_cadena = precio_del_mes(precios, modo)
            t["retail"] = t["Canal"].astype(str).str.strip().str.casefold()
            t["cadena"] = t["Canal"].map(cadena_de)
            t = t.merge(exacto.rename(columns={"Precio": "_exacto"}), on=["retail", "Modelo", "Año", "Mes"], how="left")
            t = t.merge(por_cadena.rename(columns={"Precio": "_cadena"}), on=["cadena", "Modelo", "Año", "Mes"], how="left")
            precio = t["_exacto"].fillna(t["_cadena"])
            t[FUENTE] = pd.Series(None, index=t.index, dtype=object).where(precio.isna(), DE_PRICES)
            if scrap is not None and not scrap.empty:
                _, del_scrap = precio_del_mes(scrap, modo)
                t = t.merge(del_scrap.rename(columns={"Precio": "_scrap"}),
                            on=["cadena", "Modelo", "Año", "Mes"], how="left")
                hueco = precio.isna().values & t["_scrap"].notna().values
                precio = precio.where(~hueco, t["_scrap"])
                t[FUENTE] = t[FUENTE].where(~hueco, DE_INTERNET)
                t = t.drop(columns=["_scrap"])
            t[nombre_del_precio] = precio.round(2)
            t = t.drop(columns=["retail", "cadena", "_exacto", "_cadena"])
        if nombre_del_precio in t.columns:
            cubierto = float(t.loc[t[nombre_del_precio].notna(), "Unidades"].sum()) / total if total else 0.0
        if FUENTE in t.columns:
            de_internet = t[FUENTE].isin([DE_INTERNET, DE_LOS_DOS])
            internet = float(t.loc[de_internet, "Unidades"].sum()) / total if total else 0.0
            if not de_internet.any():
                # Todo de Prices: la columna no dice nada.
                t = t.drop(columns=[FUENTE])
    else:
        internet = 0.0
    # Columnas en un orden que se lee: canal, periodo, lo pedido, unidades, precio.
    orden = ["Canal", "Año", "Mes"] + [COLUMNAS[c][1] for c in columnas if c != "canal"]
    if "modelo" in columnas and (t[DESCRIPCION] != "").any():
        orden.insert(orden.index("Modelo") + 1, DESCRIPCION)
    orden += ["Unidades"] + ([nombre_del_precio] if nombre_del_precio in t.columns else [])
    orden += [FUENTE] if FUENTE in t.columns else []
    t = t[[c for c in orden if c in t.columns]]
    orden_de_filas = ["Año", "Mes"] + [c for c in ("Semana", "Día") if c in t.columns] + ["Canal"]
    if modo == "desglosado" and nombre_del_precio in t.columns:
        # Los precios de una misma fila, juntos y de menor a mayor.
        orden_de_filas = [c for c in t.columns if c not in ("Unidades", FUENTE)]
    t = t[t["Unidades"] != 0].sort_values(orden_de_filas, kind="stable").reset_index(drop=True)
    return Parte(cadena, t, total, sin_modelo, cubierto, internet)


def generar(ejecutar: Callable[[str, str], pd.DataFrame | None], cadenas: Iterable[str], columnas: list[str],
            desde, hasta, marca: str = "", precio: bool = False,
            buscar: Callable[[str], str | None] | None = None,
            modo: str = "ponderado",
            modelos: Iterable[str] | None = None,
            buscar_en_cadena: Callable[[str, str], str | None] | None = None,
            canales: Iterable[str] = (),
            en_paralelo: Callable[[list[Callable[[], object]]], list] | None = None,
            al_terminar: Callable[[Parte], None] | None = None,
            leer_del_resumen: Callable[[str], pd.DataFrame | None] | None = None) -> Reporte:
    """Corre las consultas y arma el reporte.

    `ejecutar(sql, de_que)` devuelve la tabla, o None si esa consulta fallo
    (`de_que` es la cadena, o "precios"): una cadena que se cae no tumba el
    reporte de las demas. `en_paralelo` recibe una lista de funciones y
    devuelve sus resultados en orden; sin el, una tras otra.

    `al_terminar(parte)` se llama con cada cadena en cuanto esta lista, desde
    su hilo: asi su Excel se escribe mientras la mas lenta (Telcel, ~4 min)
    sigue en el servidor.

    `leer_del_resumen(sql)` lee la venta del resumen local cuando se puede
    (ver va_del_resumen); si devuelve None, va al servidor.
    """
    cadenas = list(cadenas)
    if precio and "modelo" not in columnas:
        columnas = columnas + ["modelo"]
    # Los precios (8 s) y su respaldo de internet (14-18 s) se piden a la vez
    # que la venta, y cada cadena los espera al terminar su consulta: antes
    # iban primero y retrasaban todo ~25 s.
    if precio:
        modelos = set(modelos) if modelos is not None else None
        vacio = pd.DataFrame(columns=["Retail", "Producto", "Dia", "Precio"])

        def traer_precios():
            crudos = ejecutar(sql_de_precios(desde, hasta), "precios")
            return precios_por_modelo(vacio if crudos is None else crudos, buscar or (lambda _t: None), modelos,
                                      buscar_en_cadena)

        def traer_scrap():
            return scrap_por_modelo(ejecutar(sql_de_scrap(desde, hasta), "scrap"), modelos)

        apartado = ThreadPoolExecutor(max_workers=2)
        futuros = (apartado.submit(traer_precios), apartado.submit(traer_scrap))
    else:
        apartado, futuros = None, None

    def una(cadena: str) -> Parte | None:
        # Solo el ponderado y el desglose necesitan la venta por dia; el
        # primero, ultimo, minimo y maximo salen de los precios del mes.
        diario = {"ponderado": True, "desglosado": "completo"}.get(modo, False) if precio else False
        if "dia" in columnas:
            diario = False
        ventas = None
        if leer_del_resumen is not None and va_del_resumen(columnas, precio, modo):
            ventas = leer_del_resumen(sql_de_ventas(cadena, columnas, desde, hasta, marca, canales=canales,
                                                    del_resumen=True))
        if ventas is None:
            ventas = ejecutar(sql_de_ventas(cadena, columnas, desde, hasta, marca, diario=diario, canales=canales),
                              cadena)
        if ventas is None or ventas.empty:
            return None
        precios, scrap = (futuros[0].result(), futuros[1].result()) if futuros else (None, None)
        parte = armar_parte(cadena, ventas, columnas, precios, modo, scrap)
        if al_terminar is not None:
            al_terminar(parte)
        return parte

    trabajos: list[Callable[[], object]] = [(lambda c=c: una(c)) for c in cadenas]
    try:
        resultados = en_paralelo(trabajos) if en_paralelo else [t() for t in trabajos]
    finally:
        if apartado is not None:
            apartado.shutdown(wait=True)
    reporte = Reporte(desde=desde, hasta=hasta, columnas=columnas, precio=precio, modo=modo,
                      marca=marca, canales=tuple(canales))
    reporte.partes = [p for p in resultados if p is not None]
    return reporte


_MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre")


def periodo(desde, hasta) -> str:
    (a1, m1), (a2, m2) = desde, hasta
    if (a1, m1) == (a2, m2):
        return f"{_MESES[m1 - 1]} de {a1}"
    return f"{_MESES[m1 - 1]} de {a1} a {_MESES[m2 - 1]} de {a2}"


_COMO_ES_EL_PRECIO = {
    "ponderado": ("El precio es el de la fecha de cada venta (tabla Prices): cada dia vendido toma "
                  "el precio de ese dia, y el de la fila es el promedio ponderado por lo que se "
                  "vendio cada dia. Vacio si no hay precio para esas fechas. Dilo asi."),
    "ultimo": "El precio es el ultimo del mes (tabla Prices). Vacio si ese mes no hay precio. Dilo asi.",
    "primero": "El precio es el primero del mes (tabla Prices). Vacio si ese mes no hay precio. Dilo asi.",
    "minimo": "El precio es el mas bajo del mes (tabla Prices). Vacio si ese mes no hay precio. Dilo asi.",
    "maximo": "El precio es el mas alto del mes (tabla Prices). Vacio si ese mes no hay precio. Dilo asi.",
    "dia": ("El precio de cada fila es el de ese dia (tabla Prices). Vacio si ese dia no hay "
            "precio. Dilo asi."),
    "desglosado": ("Cada precio va en su propia fila, con las unidades que se vendieron a ese precio "
                   "(el de la fecha de cada venta, tabla Prices): un modelo que cambio de precio en el "
                   "mes sale en varias filas. Dilo asi."),
}


def _nombre_del_precio(r: "Reporte") -> str:
    return PRECIO_DEL_DIA if "dia" in r.columnas else MODOS_DE_PRECIO.get(r.modo, PRECIO)


def pregunta_de_precio(varia: dict[str, float]) -> str:
    """Lo que se le dice al modelo cuando hay que preguntar que precio quiere."""
    donde = ", ".join(f"{acceso.NOMBRES.get(c, c)} ({f:.0%} de sus modelos)" for c, f in
                      sorted(varia.items(), key=lambda x: -x[1]))
    return (
        "TODAVIA NO ARMES EL REPORTE. En ese periodo hay modelos que cambiaron de precio dentro del "
        f"mes: {donde}. Preguntale al usuario, en un mensaje corto, que precio quiere: primero dile "
        "en que cadenas cambio y que tanto (con esos porcentajes), y luego las opciones numeradas:\n"
        "1. El promedio ponderado por lo vendido cada dia (recomendado: por las unidades da el valor real vendido)\n"
        "2. El ultimo precio del mes\n"
        "3. El primero del mes\n"
        "4. El mas bajo del mes\n"
        "5. El mas alto del mes\n"
        "6. Todos los precios: una fila por precio, con las unidades que se vendieron a cada uno\n"
        "Cuando conteste, vuelve a llamar `reporte_detallado` con lo mismo que pidio y "
        "`tipo_de_precio` = ponderado, ultimo, primero, minimo, maximo o desglosado."
    )


def como_texto(r: Reporte, sin_datos: list[str], archivos: int) -> str:
    """Lo que lee el modelo."""
    lineas = [
        f"Reporte detallado de venta, {periodo(r.desde, r.hasta)}, con: "
        f"año, mes, {', '.join(r.columnas)}, unidades"
        + (f", {_nombre_del_precio(r).lower()}" if r.precio else "") + ".",
        *([f"Solo: {', '.join(r.filtros)} (todo el reporte y sus totales son solo de eso)."] if r.filtros else []),
        f"Total: {r.unidades:,.0f} unidades.",
        "Por cadena:",
    ]
    for p in r.partes:
        extra = []
        if "modelo" in r.columnas and p.sin_modelo:
            cuanto = f"{p.sin_modelo:.0%}" if p.sin_modelo >= 0.005 else "menos del 1%"
            extra.append(f"{cuanto} de las unidades sin modelo en UNICOS$ (van con el modelo vacio)")
        if p.con_precio is not None:
            extra.append(f"{p.con_precio:.0%} de las unidades con precio")
        if p.de_internet:
            extra.append(f"{p.de_internet:.0%} con el precio de internet")
        lineas.append(f"- {p.cadena}: {p.unidades:,.0f} unidades" + (f" ({'; '.join(extra)})" if extra else ""))
    if sin_datos:
        lineas.append(f"Sin datos de ese periodo (todavia no lo cargan): {', '.join(sin_datos)}. "
                      "Dilo asi, no como que vendieron cero.")
    lineas.append(
        f"Va adjunto en {archivos} archivo{'s' if archivos != 1 else ''}"
        + (", uno por cadena" if archivos > 1 else "") + ". "
        "NO hagas otra consulta para lo mismo: el reporte ya esta. Contesta en "
        "una o dos frases con el total y lo que se pidio."
    )
    if r.precio:
        lineas.append(_COMO_ES_EL_PRECIO.get("dia" if "dia" in r.columnas else r.modo,
                                             _COMO_ES_EL_PRECIO["ponderado"]))
    if any(p.de_internet for p in r.partes):
        lineas.append(
            "Donde Prices no tenia precio va el de la pagina de la tienda (scrap de internet, el "
            "mas bajo publicado ese dia), marcado en la columna 'Fuente del precio'. Dilo: que parte "
            "salio de internet y que no es el precio de venta interno."
        )
    return "\n".join(lineas)


# --- Cuanto va a tardar -----------------------------------------------------
#
# Por cadena: (segundos fijos, segundos por mes, Excel por mes si va por
# tienda). Medido el 19 y 20/10/2026: en Telcel casi todo es fijo (un mes por
# modelo 167 s, seis por tienda 240 s); en Coppel, Liverpool y Elektra crece
# con los meses (Coppel: un mes 18 s, seis ~100 s, y su Excel por tienda de un
# millon de filas 37 s).
TIEMPOS = {
    "TELCEL": (155, 13, 2), "COPPEL": (2, 16, 7), "AT&T": (24, 4, 2), "LIVERPOOL": (0, 10, 2),
    "ELEKTRA": (0, 6, 2), "SEARS": (2, 1, 0), "MOVISTAR": (2, 1, 0),
}
# Cuanto mas tarda cada forma de poner el precio, en la parte que crece con
# los meses: el ponderado lee tambien la venta por dia y el desglose cada fila
# por dia (Coppel, seis meses: 186 s contra 117). La parte fija no cambia:
# Telcel con precio, dos meses, tardo lo mismo que sin el (20/10/2026). El
# primero, ultimo, minimo y maximo solo miran los precios del mes.
FACTOR_DE_PRECIO = {"ponderado": 1.6, "desglosado": 1.6}


def _modo(precio: bool | str) -> str:
    return "" if not precio else ("ponderado" if precio is True else str(precio))


def _llave(cadena: str, precio: bool | str, con_tienda: bool) -> str:
    return f"{cadena}|{_modo(precio) or 'sin precio'}|{'tienda' if con_tienda else 'total'}"


def predecir(cadena: str, precio: bool | str, meses: int, con_tienda: bool) -> float:
    fijo, por_mes, excel = TIEMPOS.get(cadena, (10, 10, 2))
    factor = FACTOR_DE_PRECIO.get(_modo(precio), 1)
    return fijo + (por_mes * factor + (excel if con_tienda else 0)) * max(1, meses)


def estimar(cadenas: Iterable[str], precio: bool | str, historial: dict | None = None,
            a_la_vez: int = 4, meses: int = 6, con_tienda: bool = True) -> int:
    """Los segundos que tardara, repartiendo las cadenas entre `a_la_vez`
    consultas: la mas larga primero, cada una al que se libere antes. Lo
    aprendido de reportes anteriores corrige cada cadena (ver aprender)."""
    historial = historial or {}
    tiempos = [
        predecir(c, precio, meses, con_tienda) * float(historial.get(_llave(c, precio, con_tienda), 1.0))
        for c in cadenas
    ]
    carriles = [0.0] * max(1, a_la_vez)
    for segundos in sorted(tiempos, reverse=True):
        carriles[carriles.index(min(carriles))] += segundos
    return int(max(carriles) + (5 if precio else 0) + 10)


def aprender(historial: dict, cadena: str, precio: bool | str, segundos: float,
             meses: int = 6, con_tienda: bool = True) -> None:
    """Guarda cuanto se equivoco la prediccion de esa cadena (mitad y mitad
    con lo que ya sabia), para corregir la siguiente."""
    prediccion = predecir(cadena, precio, meses, con_tienda)
    if prediccion <= 0:
        return
    llave = _llave(cadena, precio, con_tienda)
    razon = segundos / prediccion
    anterior = historial.get(llave)
    historial[llave] = round(razon if anterior is None else (float(anterior) + razon) / 2, 3)


def meses_entre(desde, hasta) -> int:
    (a1, m1), (a2, m2) = desde, hasta
    return (a2 - a1) * 12 + (m2 - m1) + 1


def en_minutos(segundos: int) -> int:
    return max(1, -(-segundos // 60))


# --- Lo que se le dice al usuario al entregar -------------------------------

_TEXTOS = {
    "es": {
        "listo": "Listo, aquí está el reporte que pediste: venta de {periodo}, por {columnas}{precio}.",
        "precio_ponderado": ", con el precio promedio ponderado por lo vendido cada día",
        "precio_ultimo": ", con el último precio de cada mes",
        "precio_primero": ", con el primer precio de cada mes",
        "precio_minimo": ", con el precio más bajo de cada mes",
        "precio_maximo": ", con el precio más alto de cada mes",
        "precio_desglosado": ", con cada precio en su fila y lo que se vendió a ese precio",
        "precio_dia": ", con el precio de cada día",
        "solo": "Solo {que}.",
        "archivos": "Va en {n} archivos, uno por cadena.",
        "archivo": "Va en un archivo.",
        "total": "Total: {total} unidades.",
        "sin_precio": "sin precio en el {pct} de las unidades",
        "de_internet": "precio de internet en el {pct}",
        "nota_internet": ("Donde la tabla de precios no tenía el precio va el de la página de la tienda "
                          "(internet, el más bajo publicado ese día), marcado en la columna "
                          "\"Fuente del precio\"."),
        "sin_venta": "Todavía sin datos de ese periodo: {cadenas}.",
        "fallas": "No pude sacar: {cadenas}. Pídemelo de nuevo si lo necesitas.",
        "falla": "No pude terminar el reporte que pediste ({error}). Pídemelo de nuevo, por favor.",
        "unidades": "unidades",
        "y": " y ",
        "columnas": {"canal": "canal", "tienda": "tienda", "region": "región", "operador": "operador",
                     "marca": "marca", "modelo": "modelo", "clasificacion": "clasificación",
                     "semana": "semana", "dia": "día"},
    },
    "en": {
        "listo": "Done, here is the report you asked for: sales for {periodo}, by {columnas}{precio}.",
        "precio_ponderado": ", with the average price weighted by daily sales",
        "precio_ultimo": ", with the last price of each month",
        "precio_primero": ", with the first price of each month",
        "precio_minimo": ", with the lowest price of each month",
        "precio_maximo": ", with the highest price of each month",
        "precio_desglosado": ", with each price on its own row and the units sold at it",
        "precio_dia": ", with each day's price",
        "solo": "Only {que}.",
        "archivos": "It comes in {n} files, one per retailer.",
        "archivo": "It comes in one file.",
        "total": "Total: {total} units.",
        "sin_precio": "no price for {pct} of the units",
        "de_internet": "online price for {pct}",
        "nota_internet": ("Where the price table had no price, the store's website price is used "
                          "(online, the lowest listed that day), marked in the \"Fuente del precio\" column."),
        "sin_venta": "No data loaded yet for that period: {cadenas}.",
        "fallas": "I couldn't get: {cadenas}. Ask me again if you need it.",
        "falla": "I couldn't finish the report you asked for ({error}). Please ask me again.",
        "unidades": "units",
        "y": " and ",
        "columnas": {"canal": "channel", "tienda": "store", "region": "region", "operador": "carrier",
                     "marca": "brand", "modelo": "model", "clasificacion": "category", "semana": "week",
                     "dia": "day"},
    },
    "zh": {
        "listo": "好了，这是你要的报表：{periodo}的销量，按{columnas}{precio}。",
        "precio_ponderado": "，价格为按每日销量加权的平均价",
        "precio_ultimo": "，价格为每月最后的价格",
        "precio_primero": "，价格为每月第一个价格",
        "precio_minimo": "，价格为每月最低价",
        "precio_maximo": "，价格为每月最高价",
        "precio_desglosado": "，每个价格单独一行，附该价格的销量",
        "precio_dia": "，价格为每天的价格",
        "solo": "仅限 {que}。",
        "archivos": "共 {n} 个文件，每个渠道一个。",
        "archivo": "共一个文件。",
        "total": "合计：{total} 台。",
        "sin_precio": "{pct} 的销量没有价格",
        "de_internet": "{pct} 使用网上价格",
        "nota_internet": "价格表中没有的价格，使用门店官网价格（当日最低标价），在“Fuente del precio”列中标明。",
        "sin_venta": "该期间暂无数据：{cadenas}。",
        "fallas": "未能获取：{cadenas}。需要的话请再问我一次。",
        "falla": "我没能完成你要的报表（{error}）。请再问我一次。",
        "unidades": "台",
        "y": "和",
        "columnas": {"canal": "渠道", "tienda": "门店", "region": "区域", "operador": "运营商",
                     "marca": "品牌", "modelo": "型号", "clasificacion": "类别", "semana": "周", "dia": "日"},
    },
}


def _lista(cosas: list[str], y: str) -> str:
    """'tienda, operador y modelo'."""
    if len(cosas) <= 1:
        return "".join(cosas)
    return ", ".join(cosas[:-1]) + y + cosas[-1]


def _texto(idioma: str) -> dict:
    return _TEXTOS.get(idioma, _TEXTOS["es"])


def para_el_usuario(r: Reporte, archivos: int, sin_venta: list[str], fallas: list[str],
                    idioma: str = "es") -> str:
    """El mensaje que acompaña los archivos cuando el reporte se armo aparte:
    aqui no hay modelo que lo redacte, asi que va escrito."""
    t = _texto(idioma)
    lineas = [
        t["listo"].format(periodo=periodo(r.desde, r.hasta), columnas=_lista(
                              [t["columnas"].get(c, c) for c in r.columnas], t["y"]),
                          precio=t["precio_" + ("dia" if "dia" in r.columnas else r.modo)] if r.precio else ""),
        *([t["solo"].format(que=", ".join(r.filtros))] if r.filtros else []),
        t["archivos"].format(n=archivos) if archivos > 1 else t["archivo"],
        "",
        t["total"].format(total=f"{r.unidades:,.0f}"),
    ]
    for p in r.partes:
        notas = []
        if p.con_precio is not None and p.con_precio < 0.95:
            notas.append(t["sin_precio"].format(pct=f"{1 - p.con_precio:.0%}"))
        if p.de_internet >= 0.005:
            notas.append(t["de_internet"].format(pct=f"{p.de_internet:.0%}"))
        nota = f" ({'; '.join(notas)})" if notas else ""
        lineas.append(f"- {acceso.NOMBRES.get(p.cadena, p.cadena)}: {p.unidades:,.0f} {t['unidades']}{nota}")
    if any(p.de_internet for p in r.partes):
        lineas.append(t["nota_internet"])
    if sin_venta:
        lineas.append(t["sin_venta"].format(cadenas=", ".join(acceso.NOMBRES.get(c, c) for c in sin_venta)))
    if fallas:
        lineas.append(t["fallas"].format(cadenas=", ".join(acceso.NOMBRES.get(c, c) for c in fallas)))
    return "\n".join(lineas)


def no_se_pudo(error: Exception, idioma: str = "es") -> str:
    return _texto(idioma)["falla"].format(error=str(error)[:120])
