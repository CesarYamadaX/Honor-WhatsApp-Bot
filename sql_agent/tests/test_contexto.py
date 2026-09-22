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

    @property
    def connection(self):
        # El desglose por tienda le pone plazo a la conexion; el doble tiene
        # que aguantarlo sin enterarse de nada mas.
        return self

    @property
    def dbapi_connection(self):
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

    def test_un_resultado_solo_numerico_no_busca_en_los_catalogos(self):
        # Sin valores de texto no hay tiendas ni carriers que buscar. El
        # desglose por tienda si corre: solo necesita el periodo, y este
        # resultado lo tiene.
        motor = _MotorDeCatalogos()
        df = pd.DataFrame({"Year": [2026], "QTY": [5]})
        contexto_del_resultado(df, motor)

        for sql, _ in motor.consultas:
            assert "CustomerStores" not in sql
            assert "HonorCarriers" not in sql

    def test_un_resultado_sin_texto_ni_periodo_no_consulta_nada(self):
        motor = _MotorDeCatalogos()
        assert contexto_del_resultado(pd.DataFrame({"QTY": [5]}), motor) == {}
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
            {"[CustomerStores]": (["ID", "Region"], [("Coppel", "Norte")])}
        )
        hojas = contexto_del_resultado(_resultado(), motor)

        assert list(hojas) == ["Tiendas"]
        assert hojas["Tiendas"].iloc[0]["Region"] == "Norte"

    def test_ya_no_se_vuelca_el_catalogo_de_homologacion(self):
        # Era la hoja "Modelos": para un resultado de 635 filas metia 2001
        # filas de UNICOS$, que son las formas en que cada cadena escribe
        # esos modelos. Eso no es detalle de ventas, es ruido.
        motor = _MotorDeCatalogos(
            {"[UNICOS$]": (["HOMOLOGACION MODELO"], [("SAMSUNG A07",)])}
        )
        hojas = contexto_del_resultado(_resultado(), motor)

        assert "Modelos" not in hojas
        assert not any("UNICOS$" in sql for sql, _ in motor.consultas)

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


class TestDesglosePorTienda:
    """La hoja que se pidio: cuanto vendio cada tienda en el periodo.

    Va por PERIODO y no por modelo. La primera version filtraba los modelos
    del resultado uniendo la consolidada con UNICOS$ por [DESCRIPCION], y esa
    union esta mal por dos lados: en la consolidada [Material_Description] es
    SKU + retailer + descripcion, asi que casaba el 8% del volumen del mes
    (165 941 de 2 030 760 unidades en agosto de 2026), y de eso una parte se
    contaba de mas, porque UNICOS$ repite 1 249 descripciones y algunas
    apuntan a 25 homologaciones distintas. Las reglas del propio catalogo ya
    prohibian ese cruce.
    """

    def _resultado_por_modelo(self):
        return pd.DataFrame(
            {
                "Año": [2026, 2026, 2026],
                "Mes": [1, 3, 6],
                "HOMOLOGACION MODELO": ["OPPO A6X", "OPPO A5", "OPPO A6X"],
                "Total Vendido": [100, 500, 300],
            }
        )

    def _motor_con_ventas(self):
        return _MotorDeCatalogos(
            {
                "ETL_Consolidado_B2B": (
                    ["Retail", "Store", "Unidades"],
                    [("Coppel", "1114 CUAUHTEMOC", 2575)],
                )
            }
        )

    def test_la_hoja_trae_las_tiendas_del_periodo(self):
        hojas = contexto_del_resultado(
            self._resultado_por_modelo(), self._motor_con_ventas()
        )

        assert "Ventas por tienda" in hojas
        assert hojas["Ventas por tienda"].iloc[0]["Store"] == "1114 CUAUHTEMOC"

    def test_la_consulta_no_pasa_por_la_homologacion(self):
        # El cruce con UNICOS$ perdia el 92% del volumen y duplicaba parte
        # del resto. Que no vuelva a entrar sin que nadie lo note.
        motor = self._motor_con_ventas()
        contexto_del_resultado(self._resultado_por_modelo(), motor)

        ventas = [sql for sql, _ in motor.consultas if "ETL_Consolidado_B2B" in sql]
        assert ventas, "deberia consultar la vista de ventas"
        for sql in ventas:
            assert "UNICOS" not in sql.upper()
            assert "JOIN" not in sql.upper()

    def test_el_periodo_viaja_como_parametro(self):
        motor = self._motor_con_ventas()
        contexto_del_resultado(self._resultado_por_modelo(), motor)

        sql, parametros = next(
            (s, p) for s, p in motor.consultas if "ETL_Consolidado_B2B" in s
        )
        assert sql.count("?") == 2
        assert parametros == ("2026-01-01", "2026-07-01")

    def test_sin_periodo_no_se_consulta_la_vista_de_ventas(self):
        # Sin acotar la fecha la consulta recorre la vista entera, que es la
        # operacion cara que este modulo evita.
        sin_fecha = pd.DataFrame({"Tienda": ["1114 CUAUHTEMOC"], "Total": [10]})
        motor = _MotorDeCatalogos()
        contexto_del_resultado(sin_fecha, motor)

        assert not any("ETL_Consolidado_B2B" in sql for sql, _ in motor.consultas)

    def test_el_periodo_sale_del_año_y_el_mes(self):
        assert contexto.periodo(self._resultado_por_modelo()) == ("2026-01-01", "2026-07-01")

    def test_el_periodo_sale_tambien_de_una_columna_de_fecha(self):
        df = pd.DataFrame(
            {
                "Día": pd.to_datetime(["2026-03-05", "2026-03-20"]),
                "Modelo": ["OPPO A6X", "OPPO A6X"],
                "QTY": [1, 2],
            }
        )
        assert contexto.periodo(df) == ("2026-03-05", "2026-03-21")

    def test_un_desglose_que_no_contesta_no_cuesta_el_reporte(self):
        # La vista de ventas es la lenta; si no llega a tiempo, el reporte
        # sale sin esa hoja en vez de hacer esperar al usuario.
        motor = _MotorDeCatalogos(falla=("ETL_Consolidado_B2B",))
        hojas = contexto_del_resultado(self._resultado_por_modelo(), motor)

        assert "Ventas por tienda" not in hojas
