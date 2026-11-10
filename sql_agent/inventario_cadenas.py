"""El inventario de las cadenas, siempre de la misma fuente y con la misma cuenta.

Pedido el 9/11/2026, despues de probar diez preguntas de inventario: el bot
leia a veces la tabla cruda de la cadena (RAW_INV_B2B) y a veces la
consolidada (GTM_BI_INVENTORIES), y el mismo dato cambiaba. Medido ese dia:

  Cadena     Fuente                          Por que
  Coppel     RAW_INV_B2B.Coppel_INV          igual a la consolidada, y semanal
  Elektra    RAW_INV_B2B.ELEKTRA_INV_B2B     semanal; en la consolidada el corte
                                             del 31/10 quedo a medias (35 mil
                                             equipos de todas las marcas contra
                                             ~425 mil)
  Liverpool  GTM_BI_INVENTORIES.INV_Retail   la cruda trae renglones de subtotal
             (Liverpool Offline)             por tienda y suma el doble, y se
                                             quedo el 31/08
  Telcel     RAW_INV_B2B.TELCEL_INV_B2B      la misma de `semanas_de_inventario`

El inventario es lo que esta en la tienda mas lo que va en transito a la
tienda; lo que esta en la bodega (CEDIS de Coppel, CD de Elektra) no cuenta
(definido por el usuario el 9/11/2026). Liverpool no reporta transito. En
Telcel se deja la cuenta de `semanas_de_inventario` (todo el stock de la foto,
almacenes incluidos, definida el 5/10/2026) para que las dos herramientas den
el mismo numero.

Cada cadena va con SU ultimo corte: el de una no sirve para otra (visto el
9/11/2026, "inventario de Liverpool" dio vacio porque se busco el 31/10 de
Elektra, y Liverpool llega al 26/10).

El modelo va por su nombre de UNICOS$, con la misma regla que la venta de la
cadena; lo que no esta, con el modelo vacio y su texto de la cadena al lado.
Medido el 9/11/2026: se homologa el 100% del inventario de Honor de las
cuatro cadenas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable, Iterable

import pandas as pd

import acceso
from inventario import _con_plazo, _df
from reporte_detallado import DESCRIPCION


@dataclass(frozen=True)
class Fuente:
    tabla: str
    fecha: str
    tienda: str
    texto: str  # lo que se homologa
    marca: str
    unidades: str
    filtro: str = ""
    nota: str = ""


FUENTES = {
    "COPPEL": Fuente(
        "[RAW_INV_B2B].dbo.[Coppel_INV]", "[Fecha]",
        "CONCAT(CAST([Tienda] AS varchar(10)), ' • ', RTRIM([NombreTienda]))",
        "CONCAT(RTRIM([Marca]), ' ', RTRIM([Modelo]))", "[Marca]",
        "ISNULL([ExisTda], 0) + ISNULL([TransitoATda], 0)",
        nota="en tienda + en transito a tienda; sin el CEDIS",
    ),
    "ELEKTRA": Fuente(
        "[RAW_INV_B2B].dbo.[ELEKTRA_INV_B2B]", "[Fecha]", "RTRIM([TIENDA])", "RTRIM([DESCRIPCION])", "[Marca]",
        "ISNULL([INV_TOTAL_RUCE], 0) + ISNULL([ENV_CD_TRANS], 0)",
        nota="en tienda (exhibicion y trastienda) + en transito del CD a la tienda; sin el CD",
    ),
    "LIVERPOOL": Fuente(
        "[GTM_BI_INVENTORIES].dbo.[INV_Retail]", "[Fecha]", "RTRIM([Store Name])", "RTRIM([Modelo Concat])",
        "[Marca]", "ISNULL([Inventario Total], 0)",
        # Sin sus centros de distribucion, que son bodega: medido el
        # 26/10/2026, CENTRO SOFTLINE PLAN NAVE 5 tenia 5,232 Honor, y hay CD
        # Tultitlan, CD Izcalli y CD Juarez. Las tiendas empiezan con "L ".
        filtro="[Retail] = 'Liverpool Offline' AND [Store Name] LIKE 'L %'",
        nota="en tienda, sin sus centros de distribucion; Liverpool no reporta transito",
    ),
    "TELCEL": Fuente(
        "[RAW_INV_B2B].dbo.[TELCEL_INV_B2B]", "[date]", "RTRIM([STORE])", "RTRIM([MODEL])", "[BRAND]",
        "ISNULL([STOCK], 0)",
        nota="todo el stock de la foto, almacenes de Telcel incluidos (como `semanas_de_inventario`)",
    ),
}
CADENAS = tuple(FUENTES)
DIMENSIONES = ("marca", "tienda", "modelo")
MAX_CORTES = 12


@dataclass
class Resultado:
    tabla: pd.DataFrame  # Cadena, Fecha[, Marca][, Tienda][, Modelo, Descripcion], Unidades
    # (cadena, fecha) -> unidades: el total de cada corte, de la mas nueva a la mas vieja.
    totales: dict[str, list[tuple[date, float]]] = field(default_factory=dict)
    sin_datos: list[str] = field(default_factory=list)
    sin_homologar: list[str] = field(default_factory=list)


def cadenas_pedidas(texto: str) -> list[str]:
    """'coppel, elektra' -> ['COPPEL', 'ELEKTRA']; vacio o 'todas', las cuatro."""
    partes = [p.strip().upper() for p in re.split(r"[,;/]|\by\b|\band\b", texto or "", flags=re.I)]
    partes = [p for p in partes if p and p not in ("TODAS", "TODOS", "ALL")]
    if not partes:
        return list(CADENAS)
    pedidas = []
    for parte in partes:
        cadena = next((c for c in CADENAS if parte.startswith(c) or c.startswith(parte)), None)
        if cadena is None:
            raise ValueError(
                f"No hay inventario de {parte.title()} en la base: solo de "
                f"{', '.join(acceso.NOMBRES.get(c, c) for c in CADENAS)}."
            )
        if cadena not in pedidas:
            pedidas.append(cadena)
    return pedidas


def dimensiones_pedidas(texto: str) -> list[str]:
    """'tienda y modelo' -> ['tienda', 'modelo']."""
    alias = {"store": "tienda", "tiendas": "tienda", "model": "modelo", "modelos": "modelo",
             "brand": "marca", "marcas": "marca", "": ""}
    nombres = []
    for parte in re.split(r"[,;/]|\by\b|\band\b", (texto or "").casefold()):
        parte = alias.get(parte.strip(), parte.strip())
        if not parte:
            continue
        if parte not in DIMENSIONES:
            raise ValueError(f"No puedo desglosar el inventario por {parte!r}: solo por {', '.join(DIMENSIONES)}.")
        if parte not in nombres:
            nombres.append(parte)
    return nombres


def marcas_pedidas(texto: str) -> list[str]:
    """'honor, samsung' -> ['HONOR', 'SAMSUNG']; vacio o 'todas', todas ([])."""
    partes = [p.strip().upper() for p in re.split(r"[,;/]|\by\b|\band\b|\bvs\.?\b", texto or "", flags=re.I)]
    return [p for p in dict.fromkeys(partes) if p and p not in ("TODAS", "TODOS", "ALL")]


def _como_fecha(valor) -> date:
    valor = pd.Timestamp(valor)
    return valor.date()


def leer(engine, cadena: str, marcas: list[str], por_tienda: bool, cortes: int,
         plazo: int | None = None) -> pd.DataFrame:
    """Los ultimos `cortes` de UNA cadena, por texto de la cadena (y tienda)."""
    f = FUENTES[cadena]
    donde = [f.filtro] if f.filtro else []
    with engine.connect() as conexion, _con_plazo(conexion, plazo):
        fechas = _df(conexion, f"SELECT DISTINCT TOP {int(cortes)} {f.fecha} AS Fecha FROM {f.tabla}"
                               + (f" WHERE {' AND '.join(donde)}" if donde else "")
                               + f" ORDER BY {f.fecha} DESC")
        if fechas.empty:
            return pd.DataFrame(columns=["Fecha", "Marca", "texto", "Unidades"])
        parametros = list(fechas["Fecha"])
        donde.append(f"{f.fecha} IN ({', '.join('?' * len(parametros))})")
        if marcas:
            donde.append(f"UPPER(RTRIM({f.marca})) IN ({', '.join('?' * len(marcas))})")
            parametros += marcas
        tienda = [f"{f.tienda} AS Tienda"] if por_tienda else []
        grupo = [f.fecha, f"UPPER(RTRIM({f.marca}))", f.texto] + ([f.tienda] if por_tienda else [])
        df = _df(conexion, f"""
            SELECT {f.fecha} AS Fecha, {', '.join(tienda + [f'UPPER(RTRIM({f.marca})) AS Marca'])},
                   {f.texto} AS texto, SUM({f.unidades}) AS Unidades
            FROM {f.tabla}
            WHERE {' AND '.join(donde)}
            GROUP BY {', '.join(grupo)}""", parametros)
        if por_tienda and marcas:
            # Todas las tiendas del corte, para decir cuales no tienen nada de
            # lo pedido (ver armar).
            df.attrs["tiendas"] = _df(conexion, f"""
                SELECT DISTINCT {f.fecha} AS Fecha, {f.tienda} AS Tienda FROM {f.tabla}
                WHERE {' AND '.join(donde[:-1])}""", parametros[:len(fechas)])
        return df


def armar(crudos: dict[str, pd.DataFrame], homologar: Callable[[str, str], str | None],
          por: list[str], modelo: str = "", varias_marcas: bool = False) -> Resultado:
    """La cuenta, separada de la base para poder probarla. `crudos` es lo
    leido de cada cadena (ver leer); `homologar(texto, cadena)` el modelo."""
    partes, sin_datos, sin_homologar = [], [], []
    # Solo por tienda: tambien las que no tienen nada de lo pedido, en cero.
    solo_tienda = "tienda" in por and "modelo" not in por and "marca" not in por and not varias_marcas
    for cadena, df in crudos.items():
        if df is None or df.empty:
            sin_datos.append(cadena)
            continue
        tiendas = df.attrs.get("tiendas")
        df = df.copy()
        df["_clave"] = cadena
        df["Cadena"] = acceso.NOMBRES.get(cadena, cadena.title())
        df["Fecha"] = [_como_fecha(x) for x in df["Fecha"]]
        df["Unidades"] = pd.to_numeric(df["Unidades"], errors="coerce").fillna(0.0)
        cache = {t: homologar(t, cadena) for t in df["texto"].dropna().unique()}
        df["Modelo"] = [cache.get(t) or "" for t in df["texto"]]
        df[DESCRIPCION] = ["" if m else str(t or "").strip() for m, t in zip(df["Modelo"], df["texto"])]
        # Si se pidio un modelo: lo que ES ese modelo, por su nombre de UNICOS$
        # (o por su texto si no esta homologado), el nombre entero y sin
        # espacios: palabra por palabra, el 8 de "Magic 8 Lite" casaba con el
        # "128GB" del Magic 5 Lite.
        buscado = re.sub(r"\s+", "", modelo.upper())
        if buscado:
            nombre = df["Modelo"].where(df["Modelo"] != "", df[DESCRIPCION]).str.upper()
            df = df[nombre.str.replace(r"\s+", "", regex=True).str.contains(buscado, regex=False)]
        if df.empty:
            sin_datos.append(cadena)
            continue
        sin_homologar += df.loc[df["Modelo"] == "", DESCRIPCION].drop_duplicates().tolist()
        if solo_tienda and tiendas is not None and not tiendas.empty:
            todas = tiendas.assign(Fecha=[_como_fecha(x) for x in tiendas["Fecha"]])
            con_algo = df[["Fecha", "Tienda"]].drop_duplicates()
            faltan = todas.merge(con_algo, how="left", indicator=True)
            faltan = faltan[faltan["_merge"] == "left_only"].drop(columns="_merge")
            df = pd.concat([df, faltan.assign(Unidades=0.0, Cadena=df["Cadena"].iloc[0], _clave=cadena,
                                              Marca="", texto="", Modelo="", **{DESCRIPCION: ""})],
                           ignore_index=True)
        partes.append(df)
    columnas = (["Cadena", "Fecha"] + (["Marca"] if "marca" in por or varias_marcas else [])
                + (["Tienda"] if "tienda" in por else []) + (["Modelo", DESCRIPCION] if "modelo" in por else []))
    if not partes:
        return Resultado(pd.DataFrame(columns=columnas + ["Unidades"]), sin_datos=sin_datos)
    juntos = pd.concat(partes, ignore_index=True)
    totales = {
        cadena: sorted(((f, float(u)) for f, u in g.groupby("Fecha")["Unidades"].sum().items()), reverse=True)
        for cadena, g in juntos.groupby("_clave", sort=False)
    }
    tabla = juntos.groupby(columnas, as_index=False, sort=False)["Unidades"].sum()
    if not solo_tienda:
        tabla = tabla[tabla["Unidades"] != 0]
    if DESCRIPCION in tabla.columns and not (tabla[DESCRIPCION] != "").any():
        tabla = tabla.drop(columns=[DESCRIPCION])
    tabla = tabla.sort_values(["Cadena", "Fecha", "Unidades"], ascending=[True, False, False]).reset_index(drop=True)
    return Resultado(tabla, totales, sin_datos, sin_homologar)


def calcular(engine, cadenas: Iterable[str], homologar: Callable[[str, str], str | None],
             marcas: list[str] | None = None, por: list[str] | None = None, modelo: str = "",
             cortes: int = 1, plazo: int | None = None) -> Resultado:
    por = por or []
    marcas = marcas if marcas is not None else ["HONOR"]
    cortes = max(1, min(int(cortes or 1), MAX_CORTES))
    crudos = {c: leer(engine, c, marcas, "tienda" in por, cortes, plazo) for c in cadenas}
    return armar(crudos, homologar, por, modelo, varias_marcas=len(marcas) != 1)


def _numero(x: float) -> str:
    return f"{x:,.0f}"


def como_texto(r: Resultado, pedido: str, hoy: date | None = None, maximo: int = 10) -> str:
    """Lo que lee el modelo."""
    hoy = hoy or datetime.now().date()
    lineas = [f"Inventario, pedido: {pedido}."]
    for clave, cortes in r.totales.items():
        fecha, unidades = cortes[0]
        cadena = acceso.NOMBRES.get(clave, clave.title())
        lineas.append(f"- {cadena}: {_numero(unidades)} equipos al {fecha:%d/%m/%Y} ({FUENTES[clave].nota}).")
        if (hoy - fecha).days > 21:
            lineas.append(f"  OJO: es el ultimo corte de {cadena} que hay en la base; no se ha cargado "
                          "uno mas reciente. Dilo asi.")
        if len(cortes) > 1:
            lineas.append("  Cortes anteriores: " + "; ".join(f"{f:%d/%m/%Y}: {_numero(u)}" for f, u in cortes[1:]))
    if r.sin_datos:
        lineas.append(f"Sin inventario que coincida en: {', '.join(acceso.NOMBRES.get(c, c) for c in r.sin_datos)}.")
    if "Marca" in r.tabla.columns and len(r.tabla):
        ultimo = r.tabla[r.tabla["Fecha"] == r.tabla.groupby("Cadena")["Fecha"].transform("max")]
        for cadena, g in ultimo.groupby("Cadena", sort=False):
            marcas = g.groupby("Marca")["Unidades"].sum().sort_values(ascending=False)
            lineas.append(f"Por marca en {cadena} (ultimo corte): "
                          + "; ".join(f"{m}: {_numero(u)}" for m, u in marcas.head(maximo).items()))
    if "Modelo" in r.tabla.columns and len(r.tabla):
        ultimo = r.tabla[r.tabla["Fecha"] == r.tabla.groupby("Cadena")["Fecha"].transform("max")]
        nombre = ultimo["Modelo"].where(ultimo["Modelo"] != "", ultimo.get(DESCRIPCION, ""))
        top = ultimo.assign(Nombre=nombre).groupby(["Cadena", "Nombre"], as_index=False)["Unidades"].sum()
        for cadena, g in top.groupby("Cadena", sort=False):
            g = g.sort_values("Unidades", ascending=False)
            lineas.append(f"Por modelo en {cadena} (ultimo corte): "
                          + "; ".join(f"{n}: {_numero(u)}" for n, u in g[["Nombre", "Unidades"]].head(maximo).values)
                          + (f" (y {len(g) - maximo} mas, adjuntos)" if len(g) > maximo else ""))
    if "Tienda" in r.tabla.columns and len(r.tabla):
        ultimo = r.tabla[r.tabla["Fecha"] == r.tabla.groupby("Cadena")["Fecha"].transform("max")]
        for cadena, g in ultimo.groupby("Cadena", sort=False):
            tiendas = g.groupby("Tienda")["Unidades"].sum().sort_values(ascending=False)
            con = tiendas[tiendas > 0]
            lineas.append(f"Tiendas con inventario en {cadena}: {len(con)}; las de mas: "
                          + "; ".join(f"{t}: {_numero(u)}" for t, u in con.head(5).items()))
            sin = sorted(tiendas[tiendas <= 0].index)
            if sin:
                lineas.append(f"Tiendas sin inventario de lo pedido en {cadena}: {len(sin)} de {len(tiendas)}: "
                              + ", ".join(sin[:15]) + (f" (y {len(sin) - 15} mas, adjuntas)" if len(sin) > 15 else ""))
    if r.sin_homologar:
        lineas.append(
            "OJO: estos productos NO estan en UNICOS$; van con el modelo vacio y su texto de la cadena: "
            f"{', '.join(r.sin_homologar[:10])}. No les inventes un nombre de modelo."
        )
    lineas.append(
        "Contesta lo que se pregunto. Cada cifra va con la fecha del corte de SU cadena (cada una "
        "tiene el suyo). El inventario es lo que esta en tienda mas lo que va en transito; no cuenta "
        "la bodega. Si hay cortes anteriores, di como cambio. El detalle va adjunto."
    )
    return "\n".join(lineas)
