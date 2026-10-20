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
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable

import pandas as pd

import acceso
import ventas_con_modelo

PRECIOS = "[GTM BI Database].dbo.[Prices]"

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
}
# Lo que no esta en UNICOS$: su texto de la cadena, solo en esas filas.
DESCRIPCION = "Descripcion de la cadena"
_EXPRESION_DESCRIPCION = "CASE WHEN [Modelo] IS NULL THEN RTRIM([Material_Description]) END"
PRECIO = "Precio promedio"

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
                 "mes": "", "año": "", "anio": "", "fecha": "", "precio": "", "ventas": "",
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


def sql_de_ventas(cadena: str, columnas: list[str], desde, hasta, marca: str = "") -> str:
    """La venta de UNA cadena, agrupada. Siempre por [Retail] (para saber la
    cadena de cada fila y cruzar el precio) y por año y mes."""
    expresiones = ["[Retail] AS [Canal]", "[Year] AS [Año]", "[Month] AS [Mes]"]
    agrupar = ["[Retail]", "[Year]", "[Month]"]
    for nombre in columnas:
        expresion, alias = COLUMNAS[nombre]
        if nombre == "canal":
            continue
        expresiones.append(f"{expresion} AS [{alias}]")
        agrupar.append(expresion)
        if nombre == "modelo":
            expresiones.append(f"{_EXPRESION_DESCRIPCION} AS [{DESCRIPCION}]")
            agrupar.append(_EXPRESION_DESCRIPCION)
    filtros = [_filtro_de_periodo(desde, hasta)]
    if marca:
        filtros.append(f"[Brand] = '{marca.replace(chr(39), chr(39) * 2)}'")
    return (
        f"SELECT {', '.join(expresiones)}, SUM([QTY]) AS [Unidades] "
        f"FROM {ventas_con_modelo.derivada([cadena])} AS [v] "
        f"WHERE {' AND '.join(filtros)} "
        f"GROUP BY {', '.join(agrupar)}"
    )


def sql_de_precios(desde, hasta) -> str:
    """El precio de venta promedio por cadena, producto y mes (3.6 s, medido)."""
    (a1, m1), (a2, m2) = desde, hasta
    return (
        "SELECT [Retail], [Material Description] AS [Producto], "
        "YEAR([Día/Periodo]) AS [Año], MONTH([Día/Periodo]) AS [Mes], "
        "AVG([Precio Venta]) AS [Precio] "
        f"FROM {PRECIOS} "
        f"WHERE [Día/Periodo] >= '{a1:04d}-{m1:02d}-01' "
        f"AND [Día/Periodo] < DATEADD(MONTH, 1, '{a2:04d}-{m2:02d}-01') "
        "AND [Precio Venta] > 0 "
        "GROUP BY [Retail], [Material Description], YEAR([Día/Periodo]), MONTH([Día/Periodo])"
    )


# AT&T en Prices: 'AT&T CACS APPLE IPHONE 15 256GB 4G'; en UNICOS$:
# 'AT&T CACS APPLE IPHONE 15 256GB', sin la red. Medido el 19/10/2026: tal
# cual casaba el 0% de AT&T; sin la red, dos de cada tres productos. El resto
# esta escrito distinto en las dos tablas y queda sin precio: un precio mal
# asignado es peor que uno vacio.
_RED_AL_FINAL = re.compile(r"\s+\d(?:\.\d)?G$")


def modelo_del_precio(texto: str, buscar: Callable[[str], str | None]) -> str | None:
    if not isinstance(texto, str):
        return None
    return buscar(texto) or buscar(_RED_AL_FINAL.sub("", texto.strip()))


def precios_por_modelo(precios: pd.DataFrame,
                       buscar: Callable[[str], str | None]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Los precios con su modelo homologado, promediados en dos niveles: por
    [Retail] exacto (Liverpool Offline no cuesta lo que Online) y por cadena,
    para lo que no tenga el primero."""
    if precios.empty:
        return (pd.DataFrame(columns=["retail", "Modelo", "Año", "Mes", "Precio"]),
                pd.DataFrame(columns=["cadena", "Modelo", "Año", "Mes", "Precio"]))
    p = precios.copy()
    p["Modelo"] = p["Producto"].map({t: modelo_del_precio(t, buscar) for t in p["Producto"].dropna().unique()})
    p = p.dropna(subset=["Modelo"])
    p["retail"] = p["Retail"].astype(str).str.strip().str.casefold()
    p["cadena"] = p["Retail"].map(cadena_de)
    exacto = p.groupby(["retail", "Modelo", "Año", "Mes"], as_index=False)["Precio"].mean()
    por_cadena = p.dropna(subset=["cadena"]).groupby(["cadena", "Modelo", "Año", "Mes"], as_index=False)["Precio"].mean()
    return exacto, por_cadena


def con_precio(ventas: pd.DataFrame, exacto: pd.DataFrame, por_cadena: pd.DataFrame) -> pd.DataFrame:
    """La venta con su precio promedio del mes; vacio si ese mes no hay precio."""
    v = ventas.copy()
    v["retail"] = v["Canal"].astype(str).str.strip().str.casefold()
    v["cadena"] = v["Canal"].map(cadena_de)
    v = v.merge(exacto.rename(columns={"Precio": "_exacto"}), on=["retail", "Modelo", "Año", "Mes"], how="left")
    v = v.merge(por_cadena.rename(columns={"Precio": "_cadena"}), on=["cadena", "Modelo", "Año", "Mes"], how="left")
    v[PRECIO] = v["_exacto"].fillna(v["_cadena"]).round(2)
    return v.drop(columns=["retail", "cadena", "_exacto", "_cadena"])


@dataclass
class Parte:
    """Lo de una cadena: su tabla y como salio."""
    cadena: str
    tabla: pd.DataFrame
    unidades: float = 0.0
    sin_modelo: float = 0.0  # fraccion de las unidades sin modelo homologado
    con_precio: float | None = None  # fraccion de las unidades con precio


@dataclass
class Reporte:
    partes: list[Parte] = field(default_factory=list)
    desde: tuple[int, int] = (0, 0)
    hasta: tuple[int, int] = (0, 0)
    columnas: list[str] = field(default_factory=list)
    precio: bool = False

    @property
    def unidades(self) -> float:
        return sum(p.unidades for p in self.partes)


def armar_parte(cadena: str, ventas: pd.DataFrame, columnas: list[str],
                precios: tuple[pd.DataFrame, pd.DataFrame] | None) -> Parte:
    """Ordena la tabla de una cadena, le pone el precio y la mide."""
    t = ventas.copy()
    t["Unidades"] = pd.to_numeric(t["Unidades"], errors="coerce").fillna(0.0)
    t["Canal"] = t["Canal"].where(~t["Canal"].astype(str).str.upper().str.startswith("UNKNOWN"), "AT&T")
    if "Modelo" in t.columns:
        t["Modelo"] = t["Modelo"].fillna("")
        t[DESCRIPCION] = t[DESCRIPCION].fillna("")
    total = float(t["Unidades"].sum())
    sin_modelo = (
        float(t.loc[t["Modelo"] == "", "Unidades"].sum()) / total
        if "Modelo" in t.columns and total else 0.0
    )
    cubierto = None
    if precios is not None and "Modelo" in t.columns:
        t = con_precio(t, *precios)
        cubierto = float(t.loc[t[PRECIO].notna(), "Unidades"].sum()) / total if total else 0.0
    # Columnas en un orden que se lee: canal, periodo, lo pedido, unidades, precio.
    orden = ["Canal", "Año", "Mes"] + [COLUMNAS[c][1] for c in columnas if c != "canal"]
    if "modelo" in columnas and (t[DESCRIPCION] != "").any():
        orden.insert(orden.index("Modelo") + 1, DESCRIPCION)
    orden += ["Unidades"] + ([PRECIO] if PRECIO in t.columns else [])
    t = t[[c for c in orden if c in t.columns]]
    t = t[t["Unidades"] != 0].sort_values(["Año", "Mes", "Canal"], kind="stable").reset_index(drop=True)
    return Parte(cadena, t, total, sin_modelo, cubierto)


def generar(ejecutar: Callable[[str, str], pd.DataFrame | None], cadenas: Iterable[str], columnas: list[str],
            desde, hasta, marca: str = "", precio: bool = False,
            buscar: Callable[[str], str | None] | None = None,
            en_paralelo: Callable[[list[Callable[[], object]]], list] | None = None,
            al_terminar: Callable[[Parte], None] | None = None) -> Reporte:
    """Corre las consultas y arma el reporte.

    `ejecutar(sql, de_que)` devuelve la tabla, o None si esa consulta fallo
    (`de_que` es la cadena, o "precios"): una cadena que se cae no tumba el
    reporte de las demas. `en_paralelo` recibe una lista de funciones y
    devuelve sus resultados en orden; sin el, una tras otra.

    `al_terminar(parte)` se llama con cada cadena en cuanto esta lista, desde
    su hilo: asi su Excel se escribe mientras la mas lenta (Telcel, ~4 min)
    sigue en el servidor.
    """
    cadenas = list(cadenas)
    if precio and "modelo" not in columnas:
        columnas = columnas + ["modelo"]
    # Los precios primero (3 s): cada cadena los necesita al terminar.
    precios = None
    if precio:
        crudos = ejecutar(sql_de_precios(desde, hasta), "precios")
        if crudos is None:
            crudos = pd.DataFrame(columns=["Retail", "Producto", "Año", "Mes", "Precio"])
        precios = precios_por_modelo(crudos, buscar or (lambda _t: None))

    def una(cadena: str) -> Parte | None:
        ventas = ejecutar(sql_de_ventas(cadena, columnas, desde, hasta, marca), cadena)
        if ventas is None or ventas.empty:
            return None
        parte = armar_parte(cadena, ventas, columnas, precios)
        if al_terminar is not None:
            al_terminar(parte)
        return parte

    trabajos: list[Callable[[], object]] = [(lambda c=c: una(c)) for c in cadenas]
    resultados = en_paralelo(trabajos) if en_paralelo else [t() for t in trabajos]
    reporte = Reporte(desde=desde, hasta=hasta, columnas=columnas, precio=precio)
    reporte.partes = [p for p in resultados if p is not None]
    return reporte


_MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre")


def periodo(desde, hasta) -> str:
    (a1, m1), (a2, m2) = desde, hasta
    if (a1, m1) == (a2, m2):
        return f"{_MESES[m1 - 1]} de {a1}"
    return f"{_MESES[m1 - 1]} de {a1} a {_MESES[m2 - 1]} de {a2}"


def como_texto(r: Reporte, sin_datos: list[str], archivos: int) -> str:
    """Lo que lee el modelo."""
    lineas = [
        f"Reporte detallado de venta, {periodo(r.desde, r.hasta)}, con: "
        f"año, mes, {', '.join(r.columnas)}, unidades" + (", precio promedio del mes" if r.precio else "") + ".",
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
        lineas.append(f"- {p.cadena}: {p.unidades:,.0f} unidades" + (f" ({'; '.join(extra)})" if extra else ""))
    if sin_datos:
        lineas.append(f"Sin venta en ese periodo: {', '.join(sin_datos)}. Dilo.")
    lineas.append(
        f"Va adjunto en {archivos} archivo{'s' if archivos != 1 else ''}"
        + (", uno por cadena" if archivos > 1 else "") + ". "
        "NO hagas otra consulta para lo mismo: el reporte ya esta. Contesta en "
        "una o dos frases con el total y lo que se pidio."
    )
    if r.precio:
        lineas.append(
            "El precio es el promedio del precio de venta de ese modelo en esa "
            "cadena ese mes (tabla Prices); vacio si ese mes no hay precio. Dilo asi."
        )
    return "\n".join(lineas)
