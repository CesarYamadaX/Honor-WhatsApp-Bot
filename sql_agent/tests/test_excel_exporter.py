"""Pruebas de excel_exporter.py, especificas del Critico 1 de la revision final.

El bug: `export_to_excel` devolvia una ruta relativa ("exports/x.xlsx"),
resuelta contra el cwd de Python. Esa ruta viaja por HTTP hasta el sidecar de
Node, que la resuelve contra SU PROPIO cwd (distinto: Python arranca con
`cd sql_agent`, Node con `cd whatsapp`). El resultado es que Node nunca
encuentra el archivo y el usuario recibe "Genere el archivo pero no lo pude
leer para enviartelo" en TODA consulta de mas de 15 filas.
"""

import os

import pandas as pd

from excel_exporter import ExcelExporter


def _df():
    return pd.DataFrame({"tienda": ["a", "b"], "monto": [1, 2]})


def test_ruta_devuelta_es_absoluta(tmp_path, monkeypatch):
    """El contrato HTTP exige rutas absolutas (ver spec, "El contrato HTTP")."""
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    ruta = ExcelExporter.export_to_excel(_df(), filename="reporte.xlsx")
    assert os.path.isabs(ruta)
    assert os.path.isfile(ruta)


def test_archivo_se_encuentra_desde_un_cwd_distinto_al_de_generacion(tmp_path, monkeypatch):
    """Reproduce el escenario exacto que falla hoy en produccion.

    Python genera el archivo con un cwd (aqui, `dir_generacion`, análogo a
    `sql_agent/` cuando se arranca con `cd sql_agent`). Luego, sin volver a
    tocar la ruta devuelta, un lector la resuelve con OTRO cwd (aqui,
    `dir_lectura`, analogo a `whatsapp/`) -- exactamente lo que hace
    `fs.statSync(ruta)` en `whatsapp/index.js`.
    Deliberadamente NO se fija `EXPORT_DIR`: se ejercita el default real (el
    que usa produccion), que debe resolver contra el propio modulo y no
    contra el cwd.

    Si `export_to_excel` volviera a devolver una ruta relativa, `os.path.isfile`
    la resolveria contra `dir_lectura` (el cwd vigente en el momento de la
    aserción) en vez de contra `dir_generacion`, y esta prueba fallaria --se
    verifico manualmente revirtiendo el fix (ver reporte).
    """
    dir_generacion = tmp_path / "sql_agent_cwd"
    dir_lectura = tmp_path / "whatsapp_cwd"
    dir_generacion.mkdir()
    dir_lectura.mkdir()

    monkeypatch.delenv("EXPORT_DIR", raising=False)
    monkeypatch.chdir(dir_generacion)
    ruta = ExcelExporter.export_to_excel(_df(), filename="reporte_cwd_distinto.xlsx")

    try:
        monkeypatch.chdir(dir_lectura)
        # Sin resolver nada a mano: es justo lo que hace Node con
        # fs.statSync(ruta) usando la ruta tal como llego por HTTP.
        assert os.path.isfile(ruta), (
            f"no se encontro {ruta!r} desde un cwd distinto al de generacion "
            "-- probable regresion a una ruta relativa"
        )
    finally:
        # Limpieza: no dejar basura en sql_agent/exports/ (el default real,
        # ya que esta prueba no fija EXPORT_DIR).
        if os.path.isfile(ruta):
            os.remove(ruta)
