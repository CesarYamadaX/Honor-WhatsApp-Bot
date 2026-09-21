"""Pruebas de excel_exporter.py, especificas del Critico 1 de la revision final.

El bug: `export_to_excel` devolvia una ruta relativa ("exports/x.xlsx"),
resuelta contra el cwd de Python. Esa ruta viaja por HTTP hasta el sidecar de
Node, que la resuelve contra SU PROPIO cwd (distinto: Python arranca con
`cd sql_agent`, Node con `cd whatsapp`). El resultado es que Node nunca
encuentra el archivo y el usuario recibe "Genere el archivo pero no lo pude
leer para enviartelo" en TODA consulta de mas de 15 filas.
"""

import os

import openpyxl
import pandas as pd
import pytest

import excel_exporter
from excel_exporter import HOJA_DATOS, ExcelExporter


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


def _exportar(df, tmp_path, monkeypatch, nombre="reporte.xlsx"):
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    return openpyxl.load_workbook(ExcelExporter.export_to_excel(df, filename=nombre))


def test_el_excel_es_solo_datos(tmp_path, monkeypatch):
    # La grafica salio del Excel: ahora se manda como imagen aparte, porque en
    # el telefono nadie abre la segunda hoja de un adjunto. Una hoja de mas
    # aqui significa que la grafica volvio a colarse.
    libro = _exportar(
        pd.DataFrame({"Retail": ["a", "b", "c"], "QTY": [3, 1, 2]}), tmp_path, monkeypatch
    )
    assert libro.sheetnames == [HOJA_DATOS]
    assert libro[HOJA_DATOS]._charts == []


class TestElArchivoSaleEnOrden:
    """"Que cuando se pide un archivo siempre venga ordenado, por ejemplo de
    meses, primero los mas viejos a los mas recientes".

    El agente escribe `ORDER BY SUM([QTY]) DESC` porque esta buscando los mas
    vendidos, asi que el reporte llegaba con agosto arriba, enero en medio y
    marzo al final. Se reordena al entregarlo.
    """

    def _leer(self, ruta, hoja=HOJA_DATOS):
        libro = openpyxl.load_workbook(ruta)
        return list(libro[hoja].iter_rows(min_row=2, values_only=True))

    def test_los_meses_salen_del_mas_viejo_al_mas_reciente(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
        # Como viene de la consulta: ordenado por cantidad.
        df = pd.DataFrame(
            {
                "Año": [2026, 2025, 2026],
                "Mes": [3, 12, 1],
                "Total": [300, 200, 100],
            }
        )
        ruta = ExcelExporter.export_to_excel(df, filename="orden.xlsx")
        assert [(f[0], f[1]) for f in self._leer(ruta)] == [
            (2025, 12),
            (2026, 1),
            (2026, 3),
        ]

    def test_las_hojas_de_contexto_tambien(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
        ficha = pd.DataFrame({"Mes": [9, 2], "Tienda": ["b", "a"], "Unidades": [5, 9]})
        ruta = ExcelExporter.export_to_excel(
            _df(), filename="contexto.xlsx", contexto={"Ventas por tienda": ficha}
        )
        assert [f[0] for f in self._leer(ruta, "Ventas por tienda")] == [2, 9]

    def test_un_top_no_se_reordena(self, tmp_path, monkeypatch):
        # Sin columna de tiempo, el orden de la consulta ES la respuesta.
        monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
        df = pd.DataFrame({"Retail": ["c", "a", "b"], "QTY": [30, 20, 10]})
        ruta = ExcelExporter.export_to_excel(df, filename="top.xlsx")
        assert [f[0] for f in self._leer(ruta)] == ["c", "a", "b"]


class TestAnchoDeColumnas:
    def _ancho(self, libro, letra):
        return libro[HOJA_DATOS].column_dimensions[letra].width

    def test_la_columna_se_ancha_al_texto_mas_largo(self, tmp_path, monkeypatch):
        libro = _exportar(
            pd.DataFrame({"Modelo": ["X5", "HONOR Magic7 Pro 12/512"], "QTY": [1, 2]}),
            tmp_path,
            monkeypatch,
        )
        esperado = len("HONOR Magic7 Pro 12/512") + excel_exporter.MARGEN_COLUMNA
        assert self._ancho(libro, "A") == esperado

    def test_el_encabezado_cuenta_cuando_es_mas_largo_que_el_dato(
        self, tmp_path, monkeypatch
    ):
        libro = _exportar(
            pd.DataFrame({"Homologacion_modelo_final": ["X5"], "QTY": [1]}),
            tmp_path,
            monkeypatch,
        )
        esperado = len("Homologacion_modelo_final") + excel_exporter.MARGEN_COLUMNA
        assert self._ancho(libro, "A") == esperado

    def test_una_fecha_se_mide_por_lo_que_muestra_no_por_lo_que_guarda(
        self, tmp_path, monkeypatch
    ):
        # La celda guarda "2026-03-01 08:30:00" (19) pero muestra "2026-03-01"
        # (10). Medir el valor guardado daria una columna casi al doble de
        # ancha que su contenido visible.
        libro = _exportar(
            pd.DataFrame(
                {"Fe": pd.to_datetime(["2026-03-01 08:30", "2026-03-02 14:45"]), "QTY": [1, 2]}
            ),
            tmp_path,
            monkeypatch,
        )
        esperado = len(excel_exporter.FORMATO_FECHA) + excel_exporter.MARGEN_COLUMNA
        assert self._ancho(libro, "A") == esperado

    def test_una_columna_diminuta_recibe_el_ancho_minimo(self, tmp_path, monkeypatch):
        libro = _exportar(pd.DataFrame({"ID": ["A", "B"], "N": [1, 2]}), tmp_path, monkeypatch)
        assert self._ancho(libro, "A") == excel_exporter.ANCHO_MINIMO

    def test_un_texto_larguisimo_se_topa_y_se_ajusta_en_varias_lineas(
        self, tmp_path, monkeypatch
    ):
        # Ensanchar la columna hasta el texto empujaria el resto de la tabla
        # fuera de la pantalla; recortarla escondería datos. Se ajusta.
        largo = "HONOR X9c 8/256 Verde Esmeralda " * 5
        libro = _exportar(
            pd.DataFrame({"Descripcion": [largo, "X5"], "QTY": [1, 2]}), tmp_path, monkeypatch
        )
        assert self._ancho(libro, "A") == excel_exporter.ANCHO_MAXIMO
        assert libro[HOJA_DATOS]["A2"].alignment.wrap_text is True
        # Y el texto sigue completo en la celda: el ajuste no recorta.
        assert libro[HOJA_DATOS]["A2"].value == largo

    def test_una_columna_normal_no_se_ajusta(self, tmp_path, monkeypatch):
        # wrap_text en todo engorda las filas sin motivo.
        libro = _exportar(
            pd.DataFrame({"Modelo": ["HONOR 200", "X5"], "QTY": [1, 2]}), tmp_path, monkeypatch
        )
        assert not libro[HOJA_DATOS]["A2"].alignment.wrap_text


class TestFormatoDeFechas:
    def test_una_fecha_sin_hora_pierde_el_00_00_00(self, tmp_path, monkeypatch):
        # Si no, cada marca del eje se parte en dos lineas con un 00:00:00
        # que no dice nada.
        libro = _exportar(
            pd.DataFrame(
                {"Fecha": pd.date_range("2026-01-01", periods=3), "QTY": [1, 2, 3]}
            ),
            tmp_path,
            monkeypatch,
        )
        assert libro[HOJA_DATOS]["A2"].number_format == "yyyy-mm-dd"

    def test_la_tabla_tampoco_muestra_la_hora(self, tmp_path, monkeypatch):
        libro = _exportar(
            pd.DataFrame(
                {
                    "Momento": pd.to_datetime(["2026-01-01 08:30", "2026-01-02 14:45"]),
                    "QTY": [1, 2],
                }
            ),
            tmp_path,
            monkeypatch,
        )
        assert libro[HOJA_DATOS]["A2"].number_format == excel_exporter.FORMATO_FECHA

    def test_ocultar_la_hora_no_la_borra(self, tmp_path, monkeypatch):
        # El formato es presentacion: el valor guardado sigue siendo el
        # datetime completo, asi que la hora se recupera con solo cambiarle
        # el formato a la columna.
        libro = _exportar(
            pd.DataFrame(
                {"Momento": pd.to_datetime(["2026-01-01 08:30", "2026-01-02 14:45"]), "QTY": [1, 2]}
            ),
            tmp_path,
            monkeypatch,
        )
        assert libro[HOJA_DATOS]["A2"].value == pd.Timestamp("2026-01-01 08:30").to_pydatetime()
