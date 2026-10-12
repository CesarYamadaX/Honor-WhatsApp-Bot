"""Venden mas las tiendas con promotor? La comparacion, siempre igual.

Pedido el 12/10/2026. Antes el modelo armaba cada vez su propio cruce de la
promotoria con la venta por tienda: una vez salio en 53 s, otra se paso del
tiempo, y una dijo que la semana 40 era "octubre". Aqui se hace igual
siempre:

1. Que tiendas de la cadena tuvieron promotor en el periodo: [HCReport]
   (FIXED o DOUBLE PROMOTER en 1), ligado a la cadena por [CustomerStores].
   Del resumen local si esta; si no, del servidor.
2. Cuanto vendio de Honor cada tienda en el periodo: la vista vw_ETL_* de la
   cadena, agrupada por [Store].
3. Venta y promotoria se juntan por el nombre de la tienda con las MISMAS
   reglas que la region de vw_ETL_Con_Modelo (ventas_con_modelo): espacios
   colapsados, y en Telcel sin el prefijo de su region ('RG09 ...'), en
   Elektra por el numero de tienda.

Lo que no se puede: Movistar (su venta no viene por tienda y el catalogo
tiene una sola). Sears reporta por mes: se compara por mes. La mitad IOT de
Coppel y Liverpool (accesorios, tablets) no entra: es una comparacion de
telefonos.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

import resumen_local
from inventario import _con_plazo

VISTAS = {
    "COPPEL": "vw_ETL_COPPEL", "TELCEL": "vw_ETL_TELCEL_B2B", "LIVERPOOL": "vw_ETL_Liverpool",
    "ELEKTRA": "vw_ETL_Elektra", "AT&T": "vw_ETL_ATT_Postpaid", "SEARS": "vw_ETL_Sears",
}
MENSUALES = {"SEARS"}
NO_SE_PUEDE = {"MOVISTAR": "Movistar no reporta su venta por tienda, asi que no se puede comparar por tienda."}

_ESPACIOS = re.compile(r"\s+")


def normal(texto) -> str:
    """Igual que ventas_con_modelo._normal: mayusculas, un solo espacio."""
    return _ESPACIOS.sub(" ", str(texto or "").replace("\xa0", " ").replace("\t", " ")).strip().upper()


def llaves_de_venta(tienda: str, cadena: str) -> list[str]:
    """Como se busca una tienda de la venta en el catalogo, en orden."""
    nombre = normal(tienda)
    llaves = [nombre]
    if cadena == "TELCEL" and " " in nombre:
        llaves.append(nombre.split(" ", 1)[1])  # 'RG09 LAGUNA' -> 'LAGUNA'
    if cadena == "ELEKTRA":
        primera = nombre.split(" ", 1)[0]
        if primera.isdigit():
            llaves.append(f"#{int(primera)}")
    return llaves


def llaves_de_catalogo(nombre: str, cadena: str) -> list[str]:
    nombre = normal(nombre)
    llaves = [nombre]
    if cadena == "ELEKTRA":
        primera = nombre.split(" ", 1)[0]
        if primera.isdigit():
            llaves.append(f"#{int(primera)}")
    return llaves


@dataclass
class Grupo:
    tiendas: int = 0
    unidades: float = 0.0
    por_tienda: list[float] = field(default_factory=list)

    @property
    def promedio(self) -> float:
        return self.unidades / self.tiendas if self.tiendas else 0.0

    @property
    def mediana(self) -> float:
        return statistics.median(self.por_tienda) if self.por_tienda else 0.0


@dataclass
class Resultado:
    cadena: str
    periodo: str
    con: Grupo
    sin: Grupo
    unidades_venta: float          # toda la venta de Honor de la cadena en el periodo
    unidades_ligadas: float        # la que se pudo ligar a una tienda de la promotoria
    tabla: pd.DataFrame            # una fila por tienda

    @property
    def cobertura(self) -> float:
        return self.unidades_ligadas / self.unidades_venta * 100 if self.unidades_venta else 0.0


def _tabla(engine, sql: str, parametros=(), plazo: int | None = None) -> pd.DataFrame:
    with engine.connect() as conexion, _con_plazo(conexion, plazo):
        cursor = conexion.exec_driver_sql(sql, tuple(parametros))
        return pd.DataFrame.from_records(cursor.fetchall(), columns=list(cursor.keys()))


def _promotoria(engine, cadena: str, anio: int, semanas: list[str], plazo: int | None = None) -> pd.DataFrame:
    """ID, nombre en el catalogo y si tuvo promotor, de las tiendas de la cadena."""
    huecos = ", ".join("?" * len(semanas))
    sql = (
        "SELECT cs.[ID] AS id, MIN(cs.[Coppel Name]) AS nombre, "
        "MAX(CASE WHEN h.[FIXED PROMOTER] = 1 OR h.[DOUBLE PROMOTER] = 1 THEN 1 ELSE 0 END) AS promotor "
        "FROM {hc} h JOIN {cs} cs ON cs.[ID] = h.[ID] "
        f"WHERE cs.[RetailName] = ? AND h.[YEAR] = ? AND h.[Week] IN ({huecos}) GROUP BY cs.[ID]"
    )
    parametros = (cadena, anio, *semanas)
    archivo = resumen_local.ARCHIVO
    local = resumen_local.meses_por_cadena() is not None and archivo.exists()
    if local:
        try:
            with closing(sqlite3.connect(f"file:{archivo.as_posix()}?mode=ro", uri=True)) as c:
                return pd.read_sql(sql.format(hc="[HCReport]", cs="[CustomerStores]"), c, params=parametros)
        except Exception:  # noqa: BLE001 - el resumen no tiene las copias: al servidor
            pass
    return _tabla(engine, sql.format(hc="[GTM BI Database].dbo.[HCReport] WITH (NOLOCK)",
                                     cs="[GTM BI Database].dbo.[CustomerStores]"), parametros, plazo)


def semanas_del_mes(anio: int, mes: int) -> list[str]:
    """Las semanas ('W36') cuyo domingo cae en ese mes: las semanas de estos
    datos van de domingo a sabado (ver agent.mes_de_la_semana_mensual)."""
    semanas = []
    for numero in range(1, 54):
        try:
            domingo = date.fromordinal(date.fromisocalendar(anio, numero, 1).toordinal() - 1)
        except ValueError:
            continue
        if domingo.year == anio and domingo.month == mes:
            semanas.append(f"W{numero}")
    return semanas


def comparar(engine, cadena: str, anio: int, semana: int | None = None, mes: int | None = None,
             plazo: int | None = None) -> Resultado:
    cadena = cadena.upper()
    if cadena in NO_SE_PUEDE:
        raise ValueError(NO_SE_PUEDE[cadena])
    if cadena not in VISTAS:
        raise ValueError(f"No conozco la cadena {cadena!r}.")
    if cadena in MENSUALES and mes is None and semana is not None:
        mes = date.fromordinal(date.fromisocalendar(anio, semana, 1).toordinal() - 1).month
    if mes is not None:
        semanas = semanas_del_mes(anio, mes)
        filtro_venta, parametro_venta = "[Month] = ?", mes
        periodo = f"mes {mes}/{anio}"
    else:
        semanas = [f"W{semana}"]
        filtro_venta, parametro_venta = "[WEEK] = ?", semana
        periodo = f"semana {semana} de {anio}"

    tiendas = _promotoria(engine, cadena, anio, semanas, plazo)
    venta = _tabla(
        engine,
        f"SELECT [Store] AS tienda, SUM([QTY]) AS unidades FROM [RAW_DATA_B2B].dbo.[{VISTAS[cadena]}] WITH (NOLOCK) "
        f"WHERE UPPER([Brand]) = 'HONOR' AND [Year] = ? AND {filtro_venta} GROUP BY [Store]",
        (anio, parametro_venta),
        plazo,
    )

    # Del catalogo a la llave; una llave con varias tiendas del catalogo toma
    # la mas "promotora" (si una tuvo promotor, la tienda lo tuvo).
    por_llave: dict[str, int] = {}
    for _, fila in tiendas.iterrows():
        for llave in llaves_de_catalogo(fila["nombre"], cadena):
            por_llave.setdefault(llave, fila["id"])
    venta_por_id: dict = {}
    for _, fila in venta.iterrows():
        tienda_id = next((por_llave[l] for l in llaves_de_venta(fila["tienda"], cadena) if l in por_llave), None)
        if tienda_id is not None:
            venta_por_id[tienda_id] = venta_por_id.get(tienda_id, 0.0) + float(fila["unidades"] or 0)

    tiendas["unidades"] = tiendas["id"].map(venta_por_id).fillna(0.0)
    con, sin = Grupo(), Grupo()
    for _, fila in tiendas.iterrows():
        grupo = con if fila["promotor"] else sin
        grupo.tiendas += 1
        grupo.unidades += fila["unidades"]
        grupo.por_tienda.append(fila["unidades"])
    tabla = tiendas.rename(columns={"id": "ID", "nombre": "Tienda", "promotor": "Con promotor",
                                    "unidades": "Unidades Honor"})
    tabla["Con promotor"] = tabla["Con promotor"].map({1: "Si", 0: "No"})
    return Resultado(
        cadena=cadena, periodo=periodo, con=con, sin=sin,
        unidades_venta=float(pd.to_numeric(venta["unidades"]).sum()) if not venta.empty else 0.0,
        unidades_ligadas=float(sum(venta_por_id.values())),
        tabla=tabla.sort_values("Unidades Honor", ascending=False).reset_index(drop=True),
    )


def como_texto(r: Resultado) -> str:
    def linea(nombre: str, g: Grupo) -> str:
        return (f"- {nombre}: {g.tiendas:,} tiendas, {g.unidades:,.0f} unidades de Honor, "
                f"{g.promedio:,.1f} por tienda en promedio (mediana {g.mediana:,.0f}).")
    veces = r.con.promedio / r.sin.promedio if r.sin.promedio else None
    lineas = [
        f"Promotoria contra venta de Honor en {r.cadena.title() if r.cadena != 'AT&T' else r.cadena}, {r.periodo}:",
        linea("Con promotor", r.con),
        linea("Sin promotor", r.sin),
    ]
    if veces:
        lineas.append(f"Las tiendas con promotor venden {veces:,.1f} veces lo que las que no tienen, en promedio por tienda.")
    lineas.append(
        f"Se ligo a una tienda de la promotoria el {r.cobertura:.0f}% de la venta de Honor de la cadena en el "
        f"periodo ({r.unidades_ligadas:,.0f} de {r.unidades_venta:,.0f}); el resto es de tiendas que no estan "
        "en la promotoria (en linea, almacenes) o cuyo nombre no casa."
    )
    lineas.append(
        "Al responder: da las dos cifras por tienda y cuantas veces mas vende una que otra, y aclara que "
        "es una comparacion, no prueba que el promotor sea la causa (las tiendas con promotor suelen ser "
        "las mas grandes). Si la venta ligada es baja (menos de 70%), dilo."
    )
    return "\n".join(lineas)
