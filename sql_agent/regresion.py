"""La prueba semanal del bot: preguntas reales, comparadas contra los datos.

Pedido el 9/10/2026, despues de una revision a mano con 24 preguntas que
encontro diez errores (un total sin avisar que faltaban dos cadenas, un
Excel con las marcas de todo el mercado, "Unknown" como si fuera una
cadena...). Esto la repite sola cada semana, para enterarse antes que los
usuarios.

Las cifras esperadas NO estan fijas: se calculan al correr, del resumen
local (las mismas nueve vistas ETL que usa el bot) y de la herramienta de
inventario. Fijas, la prueba caducaria con cada carga de datos.

Corre dentro del bot, en un hilo, una vez por semana (ver mantener_semanal)
y tambien a mano:

    python regresion.py

Le pregunta al agente directo, sin WhatsApp: no deja conversaciones en la
bitacora ni en el panel. Cada corrida queda en datos/regresion/ y su resumen
en el log (pestaña Logs del panel): "[regresion] 11 de 12 bien; fallaron: ...".
`REGRESION=0` la apaga.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import pandas as pd

import homologacion
import resumen_local

logger = logging.getLogger("sql_agent.regresion")

CARPETA = Path(__file__).resolve().parent / "datos" / "regresion"
DIAS_ENTRE_CORRIDAS = 7
# A partir de que hora corre: el resumen local se reconstruye al arrancar
# (~7-9 min) y sin el las cifras esperadas no se pueden calcular.
HORA_MINIMA = int(os.getenv("REGRESION_HORA", "10"))
# Cuanto se puede alejar una cifra del bot de la esperada: entre que se
# armo el resumen y la consulta del bot pueden haber llegado datos.
TOLERANCIA = 0.01

_MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "septiembre", "octubre", "noviembre", "diciembre")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")
_CADENAS = ("AT&T", "Coppel", "Elektra", "Liverpool", "Movistar", "Sears", "Telcel")
_CIFRA = re.compile(r"\d{1,3}(?:[,   ]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")


# --- Las cifras de verdad ----------------------------------------------------

def _resumen(sql: str, parametros=()) -> pd.DataFrame:
    with closing(sqlite3.connect(resumen_local.ARCHIVO)) as conexion:
        return pd.read_sql(sql, conexion, params=parametros)


def ventas(anio: int, mes: int, marca: str | None = "HONOR") -> dict[str, float]:
    """Unidades por cadena del mes, de las vistas ETL (via el resumen)."""
    por_cadena: dict[str, float] = {}
    for vista, cadena in resumen_local._CADENA_DE_LA_VISTA.items():
        filtro, parametros = "[Year] = ? AND [Month] = ?", [anio, mes]
        if marca:
            filtro += " AND UPPER([Brand]) = ?"
            parametros.append(marca.upper())
        total = _resumen(f"SELECT SUM([QTY]) AS q FROM [{vista}] WHERE {filtro}", parametros)["q"].iloc[0]
        if total:
            por_cadena[cadena] = por_cadena.get(cadena, 0.0) + float(total)
    return por_cadena


def top_modelo(anio: int, mes: int, cadena_vista: str = "vw_ETL_TELCEL_B2B") -> str | None:
    df = _resumen(
        f"SELECT [Material_Description] AS d, SUM([QTY]) AS q FROM [{cadena_vista}] "
        "WHERE [Year] = ? AND [Month] = ? AND UPPER([Brand]) = 'HONOR' GROUP BY [Material_Description]",
        (anio, mes),
    )
    cat = homologacion.catalogo()
    if df.empty or cat is None:
        return None
    df["modelo"] = df["d"].map(cat.buscar)
    tops = df.dropna(subset=["modelo"]).groupby("modelo")["q"].sum().sort_values(ascending=False)
    return tops.index[0] if len(tops) else None


# --- Como se revisa una respuesta ---------------------------------------------

def cifras(texto: str) -> list[float]:
    return [float(re.sub(r"[,   ]", "", c)) for c in _CIFRA.findall(texto or "")]


def menciona_cifra(texto: str, esperada: float, tolerancia: float = TOLERANCIA) -> bool:
    if not esperada:
        return False
    return any(abs(c - esperada) <= max(1.0, abs(esperada) * tolerancia) for c in cifras(texto))


@dataclass
class Resultado:
    clave: str
    pregunta: str
    respuesta: str = ""
    archivos: list[str] = field(default_factory=list)
    esperado: str = ""
    ok: bool = False
    problemas: list[str] = field(default_factory=list)
    segundos: float = 0.0


@dataclass
class Caso:
    clave: str
    pregunta: str
    # (respuesta, archivos) -> lista de problemas; vacia = bien
    revisar: Callable[[str, list[str]], list[str]]
    esperado: str = ""
    cadenas: list[str] | None = None
    # Una pregunta antes, en la misma conversacion (para probar la memoria).
    antes: str | None = None


def _mes_a_probar(hoy: date) -> tuple[int, int]:
    """El mes anterior: el ultimo que casi todas las cadenas ya cargaron."""
    primero = hoy.replace(day=1) - timedelta(days=1)
    return primero.year, primero.month


def casos(hoy: date | None = None, engine=None) -> list[Caso]:
    hoy = hoy or date.today()
    anio, mes = _mes_a_probar(hoy)
    nombre, name = f"{_MESES[mes - 1]} de {anio}", f"{_MONTHS[mes - 1]} {anio}"
    honor = ventas(anio, mes)
    total = sum(honor.values())
    faltan = [c for c in _CADENAS if c not in honor]
    mercado = sum(ventas(anio, mes, marca=None).values())
    lista: list[Caso] = []

    def total_del_mes(texto, _):
        problemas = []
        if not menciona_cifra(texto, total):
            problemas.append(f"no da el total esperado ({total:,.0f})")
        for cadena in faltan:
            if cadena.lower() not in texto.lower():
                problemas.append(f"no avisa que falta {cadena}")
        return problemas
    lista.append(Caso("total_del_mes", f"cuantas unidades vendio Honor en {nombre}",
                      total_del_mes, f"{total:,.0f}; faltan {faltan or 'ninguna'}"))

    def por_cadena(texto, _):
        problemas = [f"{c}: no da {v:,.0f}" for c, v in honor.items() if not menciona_cifra(texto, v)]
        if re.search(r"unknown|sin cadena|no identificad", texto, re.IGNORECASE):
            problemas.append("presenta 'Unknown' como otra cadena (es AT&T)")
        return problemas
    lista.append(Caso("por_cadena", f"ventas de Honor por cadena en {nombre}", por_cadena,
                      ", ".join(f"{c} {v:,.0f}" for c, v in honor.items())))

    modelo = top_modelo(anio, mes)
    if modelo:
        clave_modelo = modelo.split()[1] if len(modelo.split()) > 1 else modelo
        lista.append(Caso(
            "top_telcel", f"cual fue el modelo de Honor mas vendido en Telcel en {nombre}",
            lambda texto, _: [] if clave_modelo.lower() in texto.lower() else [f"no dice {modelo}"],
            modelo,
        ))

    if honor.get("Coppel"):
        antes = ventas(anio - 1, mes).get("Coppel", 0.0)

        def yoy(texto, _):
            problemas = []
            if not menciona_cifra(texto, honor["Coppel"]):
                problemas.append(f"no da {anio}: {honor['Coppel']:,.0f}")
            if antes and not menciona_cifra(texto, antes):
                problemas.append(f"no da {anio - 1}: {antes:,.0f}")
            return problemas
        lista.append(Caso("yoy_coppel",
                          f"compara las ventas de Honor en Coppel de {nombre} contra {_MESES[mes - 1]} de {anio - 1}",
                          yoy, f"{honor['Coppel']:,.0f} vs {antes:,.0f}"))

    if mercado:
        share = total / mercado * 100

        def share_total(texto, _):
            porcentajes = [float(p) for p in re.findall(r"(\d+(?:\.\d+)?)\s*%", texto)]
            return [] if any(abs(p - share) <= 0.15 for p in porcentajes) else [f"no da el share {share:.1f}%"]
        lista.append(Caso("share", f"cual fue el share de Honor en {nombre} sobre el total del mercado",
                          share_total, f"{share:.2f}%"))

    # Una semana a media mes de Sears, que reporta por mes.
    semana = date(anio, mes, 15).isocalendar()[1]
    samsung_sears = ventas(anio, mes, marca="SAMSUNG").get("Sears", 0.0)
    if samsung_sears:
        def sears_semana(texto, _):
            problemas = []
            if not re.search(r"mes|mensual", texto, re.IGNORECASE):
                problemas.append("no explica que Sears reporta por mes")
            if not menciona_cifra(texto, samsung_sears):
                problemas.append(f"no da la cifra del mes ({samsung_sears:,.0f})")
            return problemas
        lista.append(Caso("sears_semana",
                          f"cuantas unidades vendio Samsung en Sears en la semana {semana} de {anio}",
                          sears_semana, f"mensual; {samsung_sears:,.0f}"))

    def walmart(texto, _):
        nombradas = sum(c.lower() in texto.lower() for c in _CADENAS)
        problemas = [] if nombradas >= 4 else ["no dice que cadenas si hay"]
        if any(c >= 10 for c in cifras(texto)):
            problemas.append("da cifras de una cadena que no existe")
        return problemas
    lista.append(Caso("cadena_que_no_existe", f"ventas de Honor en Walmart en {nombre}", walmart,
                      "dice que Walmart no esta y cuales si"))

    if engine is not None:
        try:
            import inventario
            calculo = inventario.calcular(engine, "X6C", "HONOR", plazo=120)
        except Exception:  # noqa: BLE001
            logger.exception("Regresion: no se pudo calcular el inventario esperado")
            calculo = None
        if calculo is not None and calculo.semanas is not None:
            semanas = calculo.semanas
            lista.append(Caso(
                "inventario", "cuantas semanas de inventario tiene el Honor X6c en Telcel",
                lambda texto, _: [] if any(abs(c - semanas) <= 0.06 for c in cifras(texto))
                else [f"no da {semanas:.1f} semanas"],
                f"{semanas:.1f} semanas",
            ))

    if honor.get("Coppel"):
        lista.append(Caso(
            "ingles", f"How many units did Honor sell at Coppel in {name}?",
            lambda texto, _: ([] if menciona_cifra(texto, honor["Coppel"]) else [f"no da {honor['Coppel']:,.0f}"])
            + ([] if re.search(r"\bunits?\b|\bsold\b", texto, re.IGNORECASE) else ["no contesta en ingles"]),
            f"{honor['Coppel']:,.0f}",
        ))

    if honor.get("Elektra"):
        def excel_elektra(texto, archivos):
            problemas = []
            libros = [a for a in archivos if a.lower().endswith(".xlsx")]
            if not libros:
                return ["no mando el Excel"]
            try:
                for hoja, d in pd.read_excel(libros[0], sheet_name=None).items():
                    marca = next((c for c in d.columns if str(c).lower() in ("marca", "brand")), None)
                    if marca is not None:
                        otras = {str(m).upper() for m in d[marca].dropna().unique()} - {"HONOR"}
                        if otras:
                            problemas.append(f"la hoja {hoja} trae otras marcas: {sorted(otras)[:4]}")
            except Exception as error:  # noqa: BLE001
                problemas.append(f"no se pudo abrir el Excel: {error}")
            if not any(c >= 10 for c in cifras(texto)):
                problemas.append("el mensaje no da ninguna cifra")
            return problemas
        lista.append(Caso("excel_elektra",
                          f"mandame el excel de ventas de Honor por tienda en Elektra de {nombre}",
                          excel_elektra, "Excel solo de Honor y una cifra en el mensaje"))

    liverpool = honor.get("Liverpool") or ventas(anio, mes - 1 if mes > 1 else 12).get("Liverpool", 0.0)
    lista.append(Caso(
        "permiso", f"cuanto vendio Honor en Liverpool en {nombre}",
        lambda texto, _: ([] if re.search(r"permiso|acceso", texto, re.IGNORECASE) else ["no dice que no tiene permiso"])
        + (["dio la cifra de Liverpool a quien no la puede ver"] if liverpool and menciona_cifra(texto, liverpool) else []),
        "negativa por permiso", cadenas=["COPPEL"],
    ))

    if honor.get("Coppel") and honor.get("Telcel"):
        lista.append(Caso(
            "memoria", "y en Coppel?",
            lambda texto, _: [] if menciona_cifra(texto, honor["Coppel"]) else [f"no da Coppel {honor['Coppel']:,.0f}"],
            f"{honor['Coppel']:,.0f}", antes=f"cuanto vendio Honor en Telcel en {nombre}",
        ))
    return lista


# --- Correr ------------------------------------------------------------------

def correr(engine, fabrica_agente: Callable[[], object], casos_a_correr: list[Caso] | None = None) -> dict:
    """Corre los casos, guarda el informe y lo devuelve."""
    comenzo = datetime.now()
    lista = casos_a_correr if casos_a_correr is not None else casos(engine=engine)
    resultados: list[Resultado] = []
    for caso in lista:
        r = Resultado(caso.clave, caso.pregunta, esperado=caso.esperado)
        t = time.monotonic()
        try:
            agente = fabrica_agente()
            if caso.antes:
                agente.responder(caso.antes, cadenas=caso.cadenas)
            r.respuesta, r.archivos = agente.responder(caso.pregunta, cadenas=caso.cadenas)
            r.problemas = caso.revisar(r.respuesta, list(r.archivos))
        except Exception as error:  # noqa: BLE001 - un caso roto no tumba los demas
            logger.exception("Regresion: el caso %s fallo", caso.clave)
            r.problemas = [f"error: {error}"]
        r.segundos = round(time.monotonic() - t, 1)
        r.ok = not r.problemas
        resultados.append(r)
        logger.info("[regresion] %s: %s (%.0fs)", caso.clave, "bien" if r.ok else "; ".join(r.problemas), r.segundos)

    bien = sum(r.ok for r in resultados)
    # Si TODOS fallaron por error (sin conexion con el modelo, la base caida)
    # no se probo el bot: no cuenta como la corrida de la semana y se
    # reintenta en el siguiente ciclo. Visto el 12/10/2026: 0 de 11 por un
    # certificado, y la siguiente habria sido hasta dentro de 7 dias.
    sin_probar = bool(resultados) and all(
        r.problemas and r.problemas[0].startswith("error:") for r in resultados
    )
    informe = {
        "fecha": comenzo.isoformat(timespec="seconds"),
        "bien": bien,
        "total": len(resultados),
        "resultados": [asdict(r) for r in resultados],
    }
    CARPETA.mkdir(parents=True, exist_ok=True)
    nombre = f"{comenzo:%Y-%m-%d_%H%M}" + ("_sin_probar" if sin_probar else "")
    (CARPETA / f"{nombre}.json").write_text(
        json.dumps(informe, ensure_ascii=False, indent=1), encoding="utf-8")
    fallaron = [r.clave for r in resultados if not r.ok]
    if sin_probar:
        logger.error("[regresion] no se pudo probar: todos los casos dieron error (%s); se reintenta",
                     resultados[0].problemas[0])
        return informe
    nivel = logging.INFO if not fallaron else logging.WARNING
    logger.log(nivel, "[regresion] %d de %d bien%s", bien, len(resultados),
               f"; fallaron: {', '.join(fallaron)}" if fallaron else "")
    return informe


def ultima_corrida() -> datetime | None:
    if not CARPETA.exists():
        return None
    fechas = []
    for archivo in CARPETA.glob("*.json"):
        try:
            fecha = datetime.strptime(archivo.stem, "%Y-%m-%d_%H%M")
            resultados = json.loads(archivo.read_text(encoding="utf-8")).get("resultados", [])
        except (ValueError, OSError):
            continue
        # Una corrida donde todo dio error no probo nada (ver correr): las de
        # antes del 12/10/2026 se guardaron con el nombre normal.
        if resultados and all(
            (r.get("problemas") or [""])[0].startswith("error:") for r in resultados
        ):
            continue
        fechas.append(fecha)
    return max(fechas) if fechas else None


def toca_correr(ahora: datetime | None = None) -> bool:
    ahora = ahora or datetime.now()
    if ahora.hour < HORA_MINIMA:
        return False
    estado = resumen_local.estado()
    if not estado.get("construido"):
        return False
    if datetime.now() - datetime.fromisoformat(estado["construido"]) > timedelta(hours=resumen_local.MAX_HORAS_DE_ATRASO):
        return False
    ultima = ultima_corrida()
    return ultima is None or ahora - ultima >= timedelta(days=DIAS_ENTRE_CORRIDAS)


def mantener_semanal(crear_engine, fabrica_agente, detener: threading.Event | None = None):
    """Un hilo que la corre una vez por semana, cuando el resumen esta fresco."""
    if os.getenv("REGRESION", "1") == "0":
        return None
    detener = detener or threading.Event()

    def ciclo():
        while not detener.is_set():
            try:
                if toca_correr():
                    correr(crear_engine(), fabrica_agente)
            except Exception:  # noqa: BLE001 - la prueba nunca tumba al bot
                logger.exception("Fallo la prueba semanal")
            detener.wait(30 * 60)

    hilo = threading.Thread(target=ciclo, name="regresion", daemon=True)
    hilo.start()
    return hilo


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.stdout.reconfigure(encoding="utf-8")
    from agent import SQLAgent

    motor = SQLAgent._crear_engine()
    homologacion.configurar(motor, precargar=False)
    elegidos = set(sys.argv[1:])
    todos = casos(engine=motor)
    informe = correr(motor, lambda: SQLAgent(engine=motor),
                     [c for c in todos if not elegidos or c.clave in elegidos])
    for r in informe["resultados"]:
        print(f"\n[{'OK ' if r['ok'] else 'MAL'}] {r['clave']} ({r['segundos']:.0f}s) | {r['pregunta']}")
        print(f"   esperado: {r['esperado']}")
        if not r["ok"]:
            print(f"   problemas: {'; '.join(r['problemas'])}")
            print(f"   respuesta: {r['respuesta'][:400]}")
    print(f"\n{informe['bien']} de {informe['total']} bien")
