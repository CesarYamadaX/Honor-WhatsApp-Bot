"""Semanas de inventario de Telcel: para cuantas semanas le alcanza el stock.

La definicion la dio el negocio el 5 de octubre de 2026:

  semanas = stock de la foto mas reciente de TELCEL_INV_B2B
            / venta semanal promedio de las ultimas 4 semanas completas,
              de TODOS los canales de Telcel

Todos los canales porque el inventario no es solo de tiendas: medido en la
foto del 27/09/2026, de 2 534 765 unidades la mayor parte esta en almacenes
(ALM CENTRAL WM, ALMACEN CENTRAL, ASIG CACS, CAD DISTRIBUIDOR), que surten a
todos los canales. Contra la venta de solo el canal CAC, el X6c daba el
doble de semanas.

Se cruza por SKU: el [SKU] del inventario es el numero con el que empieza
[Material_Description] en vw_ETL_TELCEL_B2B. Medido en esas 4 semanas, de
53 184 unidades de HONOR vendidas, 53 168 tienen su SKU en el inventario. La
venta sale de la vista y no de la tabla cruda TELCEL_B2B porque la cruda
trae menos SKUs (X6c: 753 unidades contra 1 210).

Las 4 semanas son las 28 dias ANTES del dia de la foto (las fotos caen en
domingo, asi que son cuatro semanas domingo-sabado completas), salvo que la
venta todavia no llegue hasta ahi: entonces la ventana termina donde termina
la venta, y se dice.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd

INVENTARIO = "[RAW_INV_B2B].dbo.[TELCEL_INV_B2B]"
VENTA = "[RAW_DATA_B2B].dbo.[vw_ETL_TELCEL_B2B]"
HOMOLOGACION = "[GTM BI Database].dbo.[UNICOS$]"
SEMANAS_DE_PROMEDIO = 4

_SKU_DE_LA_VENTA = "LEFT([Material_Description], CHARINDEX(' ', [Material_Description] + ' ') - 1)"


@dataclass
class Resultado:
    foto: date
    desde: date
    hasta: date  # ultimo dia incluido
    tabla: pd.DataFrame  # Modelo, Stock, Venta 4 semanas, Venta semanal, Semanas
    stock: float
    venta_semanal: float
    sin_venta: list[str] = field(default_factory=list)
    recortada: bool = False

    @property
    def semanas(self) -> float | None:
        return self.stock / self.venta_semanal if self.venta_semanal > 0 else None


def _tokens(texto: str) -> list[str]:
    return [t for t in re.split(r"\s+", (texto or "").upper().strip()) if t]


def _filtro(columna_texto: str, columna_marca: str, texto: str, marca: str):
    condiciones, parametros = [], []
    for token in _tokens(texto):
        condiciones.append(f"{columna_texto} LIKE ?")
        parametros.append(f"%{token}%")
    if marca:
        condiciones.append(f"{columna_marca} = ?")
        parametros.append(marca)
    return (" AND " + " AND ".join(condiciones)) if condiciones else "", parametros


def _df(conexion, sql: str, parametros=()) -> pd.DataFrame:
    cursor = conexion.exec_driver_sql(sql, tuple(parametros))
    return pd.DataFrame.from_records(cursor.fetchall(), columns=list(cursor.keys()))


def _como_fecha(valor) -> date:
    return valor.date() if isinstance(valor, datetime) else valor


@contextmanager
def _con_plazo(conexion, segundos: int | None):
    """Lo maximo que puede esperar cada consulta; sin el, una tabla
    bloqueada dejaria la conversacion colgada. Se restaura al salir: la
    conexion vuelve al pool y la siguiente no debe heredar este tope."""
    bruta = getattr(conexion.connection, "dbapi_connection", conexion.connection)
    if not segundos or not hasattr(bruta, "timeout"):  # SQLite en las pruebas
        yield
        return
    anterior = bruta.timeout
    bruta.timeout = segundos
    try:
        yield
    finally:
        bruta.timeout = anterior


def calcular(engine, texto: str = "", marca: str = "HONOR",
             semanas: int = SEMANAS_DE_PROMEDIO, plazo: int | None = None) -> Resultado | None:
    """Las semanas de inventario por modelo. None si no hay stock que case."""
    with engine.connect() as conexion, _con_plazo(conexion, plazo):
        foto = _como_fecha(conexion.exec_driver_sql(f"SELECT MAX([date]) FROM {INVENTARIO}").scalar())
        hasta_exclusivo = foto
        # La venta puede venir atrasada respecto a la foto: entonces la
        # ventana termina en el ultimo dia con venta, no en dias vacios que
        # bajarian el promedio.
        ultima_venta = conexion.exec_driver_sql(
            f"SELECT MAX([Date]) FROM {VENTA} WHERE [Date] >= ?",
            (foto - timedelta(days=21),),
        ).scalar()
        recortada = False
        if ultima_venta is not None and _como_fecha(ultima_venta) < foto - timedelta(days=1):
            hasta_exclusivo = _como_fecha(ultima_venta) + timedelta(days=1)
            recortada = True
        desde = hasta_exclusivo - timedelta(days=7 * semanas)

        filtro_inv, par_inv = _filtro("[MODEL]", "[BRAND]", texto, marca)
        stock = _df(conexion, f"""
            SELECT CAST(CAST([SKU] AS BIGINT) AS VARCHAR(20)) AS sku,
                   MAX(RTRIM([MODEL])) AS descripcion, SUM([STOCK]) AS stock
            FROM {INVENTARIO}
            WHERE [date] = ?{filtro_inv}
            GROUP BY [SKU]""", [foto, *par_inv])
        if stock.empty:
            return None

        filtro_ven, par_ven = _filtro("[Material_Description]", "[Brand]", texto, marca)
        venta = _df(conexion, f"""
            SELECT {_SKU_DE_LA_VENTA} AS sku,
                   MAX(RTRIM([Material_Description])) AS descripcion_venta,
                   SUM([QTY]) AS venta
            FROM {VENTA}
            WHERE [Date] >= ? AND [Date] < ?{filtro_ven}
            GROUP BY {_SKU_DE_LA_VENTA}""", [desde, hasta_exclusivo, *par_ven])

        homologado = _df(conexion, f"""
            SELECT DISTINCT RTRIM([DESCRIPCION]) AS descripcion,
                   [HOMOLOGACION MODELO] AS modelo
            FROM {HOMOLOGACION} WHERE [Retail] IN ('TELCEL', 'TELCEL CAC')""")

    resultado = armar(stock, venta, homologado, foto, desde,
                      hasta_exclusivo - timedelta(days=1), semanas, recortada, texto)
    return resultado if resultado.stock > 0 or resultado.venta_semanal > 0 else None


def armar(stock: pd.DataFrame, venta: pd.DataFrame, homologado: pd.DataFrame,
          foto: date, desde: date, hasta: date, semanas: int = SEMANAS_DE_PROMEDIO,
          recortada: bool = False, texto: str = "") -> Resultado:
    """La cuenta, separada de la base para poder probarla."""
    venta = venta if not venta.empty else pd.DataFrame(columns=["sku", "descripcion_venta", "venta"])
    juntos = stock.merge(venta, on="sku", how="outer")
    juntos["stock"] = pd.to_numeric(juntos["stock"], errors="coerce").fillna(0.0)
    juntos["venta"] = pd.to_numeric(juntos["venta"], errors="coerce").fillna(0.0)
    # La descripcion de la venta trae el SKU adelante; la del inventario no.
    sin_sku = juntos["descripcion_venta"].fillna("").str.replace(r"^\S+\s+", "", regex=True)
    juntos["descripcion"] = juntos["descripcion"].fillna(sin_sku)

    # Agrupado por modelo homologado ('HONOR X6C 256GB 4G'); si el catalogo
    # todavia no lo tiene (X6E y X7E no estaban en octubre de 2026), por la
    # descripcion de Telcel.
    nombres = dict(zip(homologado["descripcion"], homologado["modelo"])) if not homologado.empty else {}
    juntos["Modelo"] = [nombre_de_modelo(d, nombres) for d in juntos["descripcion"]]
    # "X6C" tambien aparece en la descripcion de un combo del HONOR 400 que
    # regala un X6C: si se pidio un modelo, se queda lo que ES ese modelo.
    for token in _tokens(texto):
        juntos = juntos[juntos["Modelo"].str.upper().str.contains(token, regex=False)]

    tabla = juntos.groupby("Modelo", as_index=False).agg(
        Stock=("stock", "sum"), venta=("venta", "sum")
    )
    tabla["Venta semanal"] = tabla["venta"] / semanas
    tabla["Semanas de inventario"] = [
        round(s / v, 1) if v > 0 else None for s, v in zip(tabla["Stock"], tabla["Venta semanal"])
    ]
    tabla = tabla.rename(columns={"venta": f"Venta {semanas} semanas"})
    tabla = tabla.sort_values("Stock", ascending=False).reset_index(drop=True)

    sin_venta = tabla.loc[(tabla["Stock"] > 0) & (tabla[f"Venta {semanas} semanas"] <= 0), "Modelo"].tolist()
    return Resultado(
        foto=foto, desde=desde, hasta=hasta, tabla=tabla,
        stock=float(tabla["Stock"].sum()),
        venta_semanal=float(tabla["Venta semanal"].sum()),
        sin_venta=sin_venta, recortada=recortada,
    )


# 'HONOR 4G LBU-LX3 X6E 128GB NEGRO IU' -> 'HONOR X6E 128GB 4G', la forma
# del catalogo de homologacion, para los modelos que todavia no estan en el.
_DESCRIPCION_DE_TELCEL = re.compile(
    r"^(?P<marca>\S+)\s+(?P<red>[2345]G)\s+\S+\s+(?P<modelo>.+?)\s+(?P<memoria>\d+\s*(?:GB|TB))(?!\w)",
    re.IGNORECASE,
)


def nombre_de_modelo(descripcion: str, homologado: dict) -> str:
    if descripcion in homologado and homologado[descripcion]:
        return homologado[descripcion]
    m = _DESCRIPCION_DE_TELCEL.match(descripcion or "")
    if not m:
        return descripcion
    return f"{m['marca']} {m['modelo']} {m['memoria'].replace(' ', '')} {m['red']}".upper()


def _numero(x: float) -> str:
    return f"{x:,.0f}"


def como_texto(r: Resultado, maximo: int = 15) -> str:
    """Lo que lee el modelo: la cuenta, como se hizo y el detalle por modelo."""
    semanas = f"{r.semanas:.1f}" if r.semanas is not None else "sin venta, no se puede calcular"
    lineas = [
        f"Semanas de inventario en Telcel: {semanas}.",
        f"Stock: {_numero(r.stock)} unidades en la foto del {r.foto:%d/%m/%Y} "
        "(almacenes y tiendas de Telcel).",
        f"Venta semanal promedio: {_numero(r.venta_semanal)} unidades, de todos los "
        f"canales de Telcel, del {r.desde:%d/%m/%Y} al {r.hasta:%d/%m/%Y}.",
        "Calculo: stock / venta semanal promedio.",
    ]
    if r.recortada:
        lineas.append(
            "OJO: la venta todavia no llega a la fecha de la foto; la ventana "
            "termina en el ultimo dia con venta. Diselo al usuario."
        )
    filas = r.tabla.head(maximo)
    if len(r.tabla) > 1:
        lineas.append("Por modelo (de mas stock a menos):")
        for _, f in filas.iterrows():
            s = f["Semanas de inventario"]
            lineas.append(
                f"- {f['Modelo']}: stock {_numero(f['Stock'])}, venta semanal "
                f"{_numero(f['Venta semanal'])}, "
                + (f"{s:.1f} semanas" if s is not None and not pd.isna(s) else "sin venta en el periodo")
            )
        if len(r.tabla) > maximo:
            lineas.append(f"(y {len(r.tabla) - maximo} modelos mas, que van adjuntos)")
        lineas.append(
            "Al contestar, da el total y nombra los 3 modelos con mas stock con "
            "sus semanas. El resto va adjunto: no lo menciones."
        )
    lentos = r.tabla.loc[pd.to_numeric(r.tabla["Semanas de inventario"], errors="coerce").fillna(0) > 52, "Modelo"].tolist()
    if lentos:
        lineas.append(
            f"Con mas de un ano de inventario: {', '.join(lentos[:5])}. Casi "
            "siempre es un lanzamiento reciente (llego el stock y la venta "
            "apenas empieza) o inventario parado; dilo asi, no como un dato normal."
        )
    if r.sin_venta:
        lineas.append(
            f"{len(r.sin_venta)} modelo(s) tienen stock pero no vendieron nada en el "
            "periodo: ahi el inventario no se mueve."
        )
    return "\n".join(lineas)
