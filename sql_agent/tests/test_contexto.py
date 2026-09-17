"""Pruebas de contexto.py: la ficha que acompaña al resultado.

El reporte llevaba solo la tabla de la consulta. Si decia "TELCEL CAC" o
"SAMSUNG A07 64GB 4G", quien lo abria no podia saber en que region esta esa
tienda ni que tipo de producto es ese modelo sin volver a preguntarle al bot.
"""

import pandas as pd
import pytest

import contexto
from contexto import contexto_del_resultado


class _Cursor:
    def __init__(self, columnas, filas):
        self._columnas = columnas
        self._filas = filas

    def keys(self):
        return self._columnas

    def fetchall(self):
        return self._filas


class _MotorDeCatalogos:
    """Responde como el servidor, y guarda lo que se le pregunto."""

    def __init__(self, respuestas=None, falla=()):
        # respuestas: {fragmento de tabla: (columnas, filas)}
        self.respuestas = respuestas or {}
        self.falla = falla
        self.consultas = []

    def connect(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def exec_driver_sql(self, sql, parametros=None):
        self.consultas.append((sql, parametros))
        for fragmento in self.falla:
            if fragmento in sql:
                raise RuntimeError(f"{fragmento} no responde")
        for fragmento, (columnas, filas) in self.respuestas.items():
            if fragmento in sql:
                return _Cursor(columnas, filas)
        return _Cursor([], [])


def _resultado():
    return pd.DataFrame(
        {"Retail": ["Coppel", "ELEKTRA"], "Carrier": ["Telcel", "Telcel"], "QTY": [10, 20]}
    )


class TestQueSeConsulta:
    def test_sin_resultado_no_consulta_nada(self):
        motor = _MotorDeCatalogos()
        assert contexto_del_resultado(pd.DataFrame(), motor) == {}
        assert motor.consultas == []

    def test_un_resultado_solo_numerico_no_consulta_nada(self):
        # Sin valores de texto no hay tiendas ni modelos que buscar.
        motor = _MotorDeCatalogos()
        df = pd.DataFrame({"Year": [2026], "QTY": [5]})
        assert contexto_del_resultado(df, motor) == {}
        assert motor.consultas == []

    def test_los_valores_viajan_como_parametros(self):
        # Un nombre de tienda puede traer comillas; pegarlo al SQL seria una
        # inyeccion con datos que vienen de la propia base. Se usa un valor
        # que no puede confundirse con un nombre de columna.
        motor = _MotorDeCatalogos()
        peligroso = "O'Brien'); DROP TABLE x--"
        df = pd.DataFrame({"Tienda": [peligroso], "QTY": [1]})
        contexto_del_resultado(df, motor)

        assert motor.consultas, "deberia consultar los catalogos"
        for sql, parametros in motor.consultas:
            assert "?" in sql
            assert peligroso not in sql, "el valor no puede acabar pegado al SQL"
            assert peligroso in parametros

    def test_ignora_las_columnas_con_demasiados_valores(self):
        # Una columna con miles de valores distintos es un identificador o
        # texto libre, no una dimension que este en un catalogo.
        motor = _MotorDeCatalogos()
        muchos = [f"v{i}" for i in range(contexto.MAX_VALORES_POR_COLUMNA + 5)]
        df = pd.DataFrame({"Folio": muchos, "QTY": range(len(muchos))})
        contexto_del_resultado(df, motor)

        assert motor.consultas == []


class TestHojasDevueltas:
    def test_devuelve_la_hoja_del_catalogo_que_caso(self):
        motor = _MotorDeCatalogos(
            {"[UNICOS$]": (["HOMOLOGACION MODELO", "BRAND"], [("SAMSUNG A07", "SAMSUNG")])}
        )
        hojas = contexto_del_resultado(_resultado(), motor)

        assert list(hojas) == ["Modelos"]
        assert hojas["Modelos"].iloc[0]["BRAND"] == "SAMSUNG"

    def test_un_catalogo_sin_coincidencias_no_deja_hoja_vacia(self):
        motor = _MotorDeCatalogos()
        assert contexto_del_resultado(_resultado(), motor) == {}

    def test_un_catalogo_caido_no_cuesta_el_resto(self):
        # El reporte es lo que se pidio; la ficha es un extra. Que un catalogo
        # no responda no puede dejar al usuario sin nada.
        motor = _MotorDeCatalogos(
            respuestas={"[HonorCarriers]": (["Carrier"], [("Telcel",)])},
            falla=("[UNICOS$]",),
        )
        hojas = contexto_del_resultado(_resultado(), motor)

        assert list(hojas) == ["Carriers"]


class TestExcelConContexto:
    def test_el_resultado_va_en_la_primera_hoja(self, tmp_path, monkeypatch):
        import openpyxl

        from excel_exporter import HOJA_DATOS, ExcelExporter

        monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
        ruta = ExcelExporter.export_to_excel(
            _resultado(),
            filename="con_contexto.xlsx",
            contexto={"Modelos": pd.DataFrame({"HOMOLOGACION MODELO": ["SAMSUNG A07"]})},
        )
        libro = openpyxl.load_workbook(ruta)

        assert libro.sheetnames[0] == HOJA_DATOS, "lo que se pregunto va primero"
        assert libro.sheetnames == [HOJA_DATOS, "Modelos"]
        assert libro["Modelos"]["A2"].value == "SAMSUNG A07"

    @pytest.mark.parametrize("contexto_vacio", [None, {}, {"Modelos": pd.DataFrame()}])
    def test_sin_contexto_el_excel_queda_como_antes(
        self, contexto_vacio, tmp_path, monkeypatch
    ):
        import openpyxl

        from excel_exporter import HOJA_DATOS, ExcelExporter

        monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
        ruta = ExcelExporter.export_to_excel(
            _resultado(), filename="solo_datos.xlsx", contexto=contexto_vacio
        )
        assert openpyxl.load_workbook(ruta).sheetnames == [HOJA_DATOS]
