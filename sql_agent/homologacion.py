"""El modelo homologado en todo resultado que trae descripciones de producto.

Pedido el 8/10/2026, con mayusculas: el Excel SIEMPRE trae la homologacion.
Ese dia un reporte de precios del iPhone 17 llego solo con el texto de cada
cadena ('APPLE 5G IPHONE 17PMAX 1TB AZUL PV IU', 'APPLE IPHONE 17, 256 GB,
AZUL') y sin [HOMOLOGACION MODELO]: el modelo no hizo el cruce con UNICOS$.
Dejarlo a que el modelo se acuerde es justo lo que fallo, asi que aqui se
hace siempre, sin consultar al modelo: si el resultado tiene una columna de
descripcion y no trae ya el modelo, se le agrega [Modelo] al lado.

La busqueda, medida el 8/10/2026 contra el cruce exacto por cadena de
ventas_con_modelo (venta de 2026) y contra Prices (desde julio de 2026):

- el texto tal cual contra [MATERIAL DESCRIPTION FINAL] y luego contra
  [DESCRIPCION]; si no, lo mismo sin la primera palabra (el SKU que las
  vistas ETL ponen delante). Casa lo mismo que el cruce exacto en todas las
  cadenas menos AT&T (Coppel, Liverpool, Telcel, Movistar 100%; Elektra
  99.5%; Sears 100%), y en Prices del 91% al 100% -- lo que no casa son
  modelos que no estan en el catalogo (iPhone 18, Honor X7E).
- AT&T escribe el telefono a su manera; su modelo sale del codigo de
  articulo (la primera palabra) por el diccionario de ATT_B2B, como en
  ventas_con_modelo._rama_att.

Todo en memoria: UNICOS$ son ~32 mil filas (1 s) y el diccionario de AT&T
~900 (4 s). Se cargan una vez por hora y en segundo plano al arrancar. Si la
base no contesta, el resultado sale como estaba: la homologacion acompaña,
no puede costar el reporte.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger("sql_agent.homologacion")

COLUMNA = "Modelo"
VIGENCIA_S = 3600

# Las columnas que ya SON el modelo homologado: con una de estas no se agrega
# nada. "Modelo Homologado" NO esta: en EKT_B2B es la homologacion interna de
# Elektra, que con la oficial casa el 2.5% (ver catalogo.py).
_YA_HOMOLOGADO = re.compile(
    r"^\s*(modelo|homologaci[oó]n[ _]modelo|approved_name)\s*$", re.IGNORECASE
)
# Las columnas con el texto del producto, en orden de preferencia.
_DESCRIPCION = re.compile(
    r"^\s*(material[ _]?description(?:[ _]final)?|descripci[oó]n|desc[ _]?sku|"
    r"description|producto|model)\s*$",
    re.IGNORECASE,
)


def _normal(texto) -> str:
    return re.sub(r"\s+", " ", str(texto or "").replace("\xa0", " ")).strip().upper()


@dataclass
class Catalogo:
    por_final: dict[str, str]
    por_descripcion: dict[str, str]
    por_codigo_att: dict[str, str]

    def modelos(self) -> set[str]:
        """Todos los modelos homologados que conoce."""
        return {m for d in (self.por_final, self.por_descripcion) for m in d.values() if m}

    def buscar(self, texto) -> str | None:
        llave = _normal(texto)
        if not llave:
            return None
        sin_codigo = llave.split(" ", 1)[1] if " " in llave else ""
        for candidata in (llave, sin_codigo):
            if not candidata:
                continue
            modelo = self.por_final.get(candidata) or self.por_descripcion.get(candidata)
            if modelo:
                return modelo
        return self.por_codigo_att.get(llave.split(" ", 1)[0])


_engine = None
_catalogo: Catalogo | None = None
_cargado_en = 0.0
_candado = threading.Lock()


def configurar(engine, precargar: bool = True) -> None:
    """Con que base se lee el catalogo. Lo llama el agente al crearse."""
    global _engine
    _engine = engine
    if precargar and _catalogo is None:
        threading.Thread(target=catalogo, name="homologacion", daemon=True).start()


def _leer(engine) -> Catalogo:
    conexion = engine.raw_connection()
    try:
        cursor = conexion.cursor()
        cursor.execute(
            "SELECT [DESCRIPCION], [MATERIAL DESCRIPTION FINAL], [HOMOLOGACION MODELO] "
            "FROM [GTM BI Database].dbo.[UNICOS$] WITH (NOLOCK) "
            "WHERE [HOMOLOGACION MODELO] IS NOT NULL"
        )
        unicos = pd.DataFrame.from_records(cursor.fetchall(), columns=["desc", "final", "modelo"])
        # Una sola homologacion por llave, la misma que elige el SQL de las
        # vistas (MIN): asi el Excel y la vista con modelo nunca discrepan.
        def indice(columna: str) -> dict[str, str]:
            filas = unicos.dropna(subset=[columna])
            llaves = filas[columna].map(_normal)
            return filas.assign(llave=llaves)[llaves != ""].groupby("llave")["modelo"].min().to_dict()

        cursor.execute(
            "SELECT DISTINCT UPPER(RTRIM([Codigo])), UPPER(RTRIM([Model])) "
            "FROM [RAW_DATA_B2B].dbo.[ATT_B2B] WITH (NOLOCK)"
        )
        por_descripcion = indice("desc")
        codigos: dict[str, str] = {}
        for codigo, modelo_att in cursor.fetchall():
            modelo = por_descripcion.get(_normal(modelo_att))
            if codigo and modelo:
                codigos[codigo] = min(modelo, codigos.get(codigo, modelo))
        return Catalogo(indice("final"), por_descripcion, codigos)
    finally:
        conexion.close()


def catalogo() -> Catalogo | None:
    """El catalogo vigente, leyendolo si hace falta; None si no se puede."""
    global _catalogo, _cargado_en
    if _catalogo is not None and time.monotonic() - _cargado_en < VIGENCIA_S:
        return _catalogo
    if _engine is None:
        return _catalogo
    with _candado:
        if _catalogo is not None and time.monotonic() - _cargado_en < VIGENCIA_S:
            return _catalogo
        try:
            comenzo = time.monotonic()
            _catalogo = _leer(_engine)
            _cargado_en = time.monotonic()
            logger.info(
                "Homologacion: %d descripciones finales, %d descripciones, %d codigos de AT&T (%.1fs)",
                len(_catalogo.por_final), len(_catalogo.por_descripcion),
                len(_catalogo.por_codigo_att), _cargado_en - comenzo,
            )
        except Exception:  # noqa: BLE001
            # Se sigue con el anterior si lo habia; sin el, sin homologacion.
            logger.exception("No se pudo leer el catalogo de homologacion")
            _cargado_en = time.monotonic() - VIGENCIA_S + 60  # reintento en un minuto
    return _catalogo


def columna_de_descripcion(df: pd.DataFrame):
    """La columna con el texto del producto, o None."""
    for columna in df.columns:
        if _DESCRIPCION.match(str(columna)) and not pd.api.types.is_numeric_dtype(df[columna]):
            return columna
    return None


def con_modelo(df: pd.DataFrame, cat: Catalogo | None = None) -> pd.DataFrame:
    """El mismo resultado con [Modelo] junto a la descripcion, si hace falta.

    No toca un resultado que ya trae el modelo, ni uno sin descripcion, ni
    uno donde ninguna descripcion casa (una columna vacia seria ruido).
    """
    if df is None or df.empty or any(_YA_HOMOLOGADO.match(str(c)) for c in df.columns):
        return df
    descripcion = columna_de_descripcion(df)
    if descripcion is None:
        return df
    cat = cat or catalogo()
    if cat is None:
        return df
    unicos = df[descripcion].dropna().unique()
    modelos = {texto: cat.buscar(texto) for texto in unicos}
    if not any(modelos.values()):
        return df
    nuevo = df.copy()
    nuevo.insert(
        list(df.columns).index(descripcion) + 1,
        COLUMNA,
        df[descripcion].map(modelos),
    )
    nuevo.attrs = dict(df.attrs)
    return nuevo
