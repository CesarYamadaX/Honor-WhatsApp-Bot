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
from excel_exporter import HOJA_DATOS, HOJA_GRAFICA, MAX_CATEGORIAS, ExcelExporter


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


def _grafica(libro):
    return libro[HOJA_GRAFICA]._charts[0]


def _color(serie) -> str:
    """Hex del relleno de una serie; al releer, openpyxl lo envuelve."""
    relleno = serie.graphicalProperties.solidFill
    return getattr(relleno, "srgbClr", relleno)


class TestGrafica:
    def test_barras_para_comparar_magnitudes(self, tmp_path, monkeypatch):
        libro = _exportar(
            pd.DataFrame({"Retail": ["Coppel", "Elektra", "Liverpool"], "QTY": [1, 2, 3]}),
            tmp_path,
            monkeypatch,
        )
        assert libro.sheetnames == [HOJA_DATOS, HOJA_GRAFICA]
        assert type(_grafica(libro)).__name__ == "BarChart"

    def test_linea_cuando_hay_una_columna_de_fecha(self, tmp_path, monkeypatch):
        # Una fecha convierte la pregunta en "como evoluciona", no en "quien
        # es mayor": eso es una linea, no barras.
        libro = _exportar(
            pd.DataFrame(
                {"Fecha": pd.date_range("2026-01-01", periods=5), "QTY": [1, 2, 3, 4, 5]}
            ),
            tmp_path,
            monkeypatch,
        )
        assert type(_grafica(libro)).__name__ == "LineChart"

    def test_una_serie_no_lleva_leyenda_y_varias_si(self, tmp_path, monkeypatch):
        # Con una sola serie el titulo ya la nombra y la leyenda es ruido. Con
        # dos o mas es obligatoria: el color no puede ser lo unico que las
        # distinga.
        una = _exportar(
            pd.DataFrame({"Retail": ["a", "b"], "QTY": [1, 2]}), tmp_path, monkeypatch
        )
        assert _grafica(una).legend is None

        dos = _exportar(
            pd.DataFrame({"Retail": ["a", "b"], "QTY": [1, 2], "INV": [3, 4]}),
            tmp_path,
            monkeypatch,
            nombre="dos.xlsx",
        )
        assert _grafica(dos).legend is not None

    def test_las_barras_de_una_serie_comparten_color(self, tmp_path, monkeypatch):
        # Colorear cada barra segun su tamano duplicaria en el color lo que la
        # longitud ya dice, y gasta el unico canal libre que queda.
        libro = _exportar(
            pd.DataFrame({"Retail": ["a", "b", "c"], "QTY": [3, 1, 2]}), tmp_path, monkeypatch
        )
        serie = _grafica(libro).series[0]
        assert _color(serie) == excel_exporter.PALETA_SERIES[0]
        assert serie.dPt == []  # ningun punto con color propio

    def test_los_colores_siguen_el_orden_fijo_de_la_paleta(self, tmp_path, monkeypatch):
        # El orden es el mecanismo que garantiza la separacion para
        # daltonismo; reordenarlo o generar un color nuevo la rompe.
        libro = _exportar(
            pd.DataFrame({"k": ["a", "b"], "x": [1, 2], "y": [3, 4], "z": [5, 6]}),
            tmp_path,
            monkeypatch,
        )
        colores = [_color(s) for s in _grafica(libro).series]
        assert colores == list(excel_exporter.PALETA_SERIES[:3])

    def test_se_recorta_a_max_categorias_sin_perder_la_tabla(self, tmp_path, monkeypatch):
        # La grafica se acota para que las etiquetas no se amontonen, pero la
        # hoja de datos conserva todas las filas.
        filas = MAX_CATEGORIAS + 10
        libro = _exportar(
            pd.DataFrame({"Modelo": [f"M{i}" for i in range(filas)], "QTY": list(range(filas))}),
            tmp_path,
            monkeypatch,
        )
        assert libro[HOJA_DATOS].max_row == filas + 1
        assert _grafica(libro).series[0].val.numRef.f.endswith(f"${MAX_CATEGORIAS + 1}")

    @pytest.mark.parametrize(
        "df, motivo",
        [
            (pd.DataFrame({"a": ["x", "y"], "b": ["p", "q"]}), "sin columna numerica"),
            (pd.DataFrame({"Retail": ["Coppel"], "QTY": [5]}), "una sola fila no es grafica"),
        ],
    )
    def test_sin_grafica_cuando_no_ayudaria(self, df, motivo, tmp_path, monkeypatch):
        libro = _exportar(df, tmp_path, monkeypatch)
        assert libro.sheetnames == [HOJA_DATOS], motivo

    def test_un_fallo_de_la_grafica_no_impide_enviar_los_datos(self, tmp_path, monkeypatch):
        # Los datos son lo que el usuario pidio; la grafica es un extra.
        def explotar(*_):
            raise RuntimeError("forma de datos no prevista")

        monkeypatch.setattr(excel_exporter, "_agregar_grafica", explotar)
        libro = _exportar(_df(), tmp_path, monkeypatch)
        assert libro.sheetnames == [HOJA_DATOS]
        assert libro[HOJA_DATOS].max_row == 3


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

    def test_el_eje_muestra_solo_el_dia_aunque_el_dato_traiga_hora(
        self, tmp_path, monkeypatch
    ):
        # La tabla conserva la hora porque es un dato; el eje no la muestra
        # porque ahi solo estorba (parte cada marca en dos lineas).
        df = pd.DataFrame(
            {
                "Momento": pd.to_datetime(["2026-03-01 08:30", "2026-03-02 14:45"]),
                "QTY": [5, 9],
            }
        )
        libro = _exportar(df, tmp_path, monkeypatch)

        eje = _grafica(libro).x_axis
        assert eje.numFmt.formatCode == excel_exporter.FORMATO_FECHA
        # sourceLinked dejaria el formato del eje a merced del de la celda.
        assert eje.numFmt.sourceLinked is False

    def test_la_linea_no_se_suaviza(self, tmp_path, monkeypatch):
        # Excel interpola con una curva que se sale del rango real: con datos
        # entre 3 y 9 dibuja 2.8 y 9.2, valores que nadie midio.
        libro = _exportar(
            pd.DataFrame(
                {"Fecha": pd.date_range("2026-03-01", periods=4), "QTY": [5, 9, 3, 7]}
            ),
            tmp_path,
            monkeypatch,
        )
        assert _grafica(libro).series[0].smooth is False

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
