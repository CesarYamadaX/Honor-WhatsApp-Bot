"""Pruebas de grafica.py.

La grafica vivia en una segunda hoja del Excel y se saco de ahi: en el
telefono nadie abre la segunda hoja de un adjunto, y una imagen se ve en la
conversacion misma. Aqui se verifica la eleccion de forma y las decisiones de
legibilidad; como se VE hay que mirarlo, y se miro al construirlo.
"""

import os

import pandas as pd
import pytest

import grafica
from grafica import exportar_grafica


@pytest.fixture(autouse=True)
def _exports(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))


def _plan(df):
    return grafica._planear(df)


class TestQueSeGrafica:
    def test_barras_para_comparar_magnitudes(self):
        df = pd.DataFrame({"Retail": ["a", "b", "c"], "QTY": [3, 1, 2]})
        categoria, valores, temporal = _plan(df)
        assert (categoria, valores, temporal) == ("Retail", ["QTY"], False)

    def test_linea_cuando_hay_una_columna_de_fecha(self):
        # Una fecha convierte la pregunta en "como evoluciona", no en "quien
        # es mayor".
        df = pd.DataFrame(
            {"Fecha": pd.date_range("2026-01-01", periods=3), "QTY": [1, 2, 3]}
        )
        assert _plan(df)[2] is True

    def test_todo_numerico_usa_la_primera_como_etiqueta(self):
        # Un GROUP BY por [Month] devuelve solo numeros; sin esto no habria
        # grafica para una de las preguntas mas comunes.
        df = pd.DataFrame({"Month": [1, 2, 3], "QTY": [10, 30, 20]})
        assert _plan(df)[:2] == ("Month", ["QTY"])

    @pytest.mark.parametrize(
        "df, motivo",
        [
            (pd.DataFrame({"a": ["x", "y"], "b": ["p", "q"]}), "sin columna numerica"),
            (pd.DataFrame({"Retail": ["a"], "QTY": [5]}), "una sola fila es un numero"),
        ],
    )
    def test_sin_grafica_cuando_no_ayudaria(self, df, motivo):
        assert exportar_grafica(df) is None, motivo


class TestArchivoGenerado:
    def test_devuelve_un_png_absoluto_que_existe(self):
        # Absoluto porque la ruta viaja por HTTP al sidecar de Node, que la
        # resuelve contra SU propio cwd.
        ruta = exportar_grafica(pd.DataFrame({"R": ["a", "b"], "QTY": [1, 2]}))
        assert os.path.isabs(ruta)
        assert os.path.isfile(ruta)
        assert ruta.endswith(".png")
        with open(ruta, "rb") as fh:
            assert fh.read(8) == b"\x89PNG\r\n\x1a\n"

    def test_dos_graficas_no_se_pisan(self):
        # El timestamp resuelve al segundo: dos consultas simultaneas de dos
        # chats distintos caerian en el mismo nombre.
        df = pd.DataFrame({"R": ["a", "b"], "QTY": [1, 2]})
        assert exportar_grafica(df) != exportar_grafica(df)


class TestLegibilidad:
    def test_horizontal_cuando_las_etiquetas_son_largas(self):
        # Con "HONOR MAGIC 8 LITE 512GB 5G" y veinte categorias, las etiquetas
        # verticales rotadas se comen media imagen y aplastan las barras.
        largas = [f"HONOR MAGIC {i} LITE 512GB 5G" for i in range(10)]
        assert grafica._conviene_horizontal(largas) is True

    def test_horizontal_cuando_son_muchas(self):
        assert grafica._conviene_horizontal([str(i) for i in range(20)]) is True

    def test_vertical_cuando_son_pocas_y_cortas(self):
        assert grafica._conviene_horizontal(["Ene", "Feb", "Mar"]) is False

    def test_las_etiquetas_larguisimas_se_recortan(self):
        largo = "X" * 200
        recortado = grafica._recortar(largo)
        assert len(recortado) <= grafica.MAX_LARGO_ETIQUETA
        assert recortado.endswith("…")

    def test_los_miles_se_separan(self):
        # Sin esto el eje dice 600000 y hay que contar ceros para saber si son
        # seiscientos mil o seis millones.
        assert grafica._miles(600000) == "600 000"
        assert grafica._miles(1234567) == "1 234 567"

    def test_se_recorta_a_max_categorias(self):
        filas = grafica.MAX_CATEGORIAS + 20
        df = pd.DataFrame({"R": [f"r{i}" for i in range(filas)], "QTY": list(range(filas))})
        # No revienta y produce imagen: el recorte vive dentro de
        # exportar_grafica, y la tabla completa va en el Excel.
        assert os.path.isfile(exportar_grafica(df))
