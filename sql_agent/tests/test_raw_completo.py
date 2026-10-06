"""Raw_Data con todas las columnas de la cruda, y la hoja Datos solo si sirve.

Pedido el 5/10/2026 viendo "las ventas de Elektra de enero": Raw_Data venia
agrupada en 5 columnas y la hoja Datos era un solo renglon con el numero que
ya decia el mensaje.
"""

import os

import pandas as pd
from openpyxl import load_workbook

import contexto
import excel_pivot
from excel_exporter import ExcelExporter

_COLUMNAS_EKT = ["Desc Sku", "Tienda #", "SKU", "QTY", "Day", "POS", "Marca", "Region"]


class _Cursor:
    def __init__(self, columnas=(), filas=(), escalar=None):
        self._columnas, self._filas, self._escalar = list(columnas), list(filas), escalar

    def keys(self):
        return self._columnas

    def fetchall(self):
        return self._filas

    def scalar(self):
        return self._escalar


class _Conexion:
    """Contesta como el servidor: columnas, conteo, catalogo, cruda e IOT."""

    def __init__(self, filas_en_periodo, columnas=_COLUMNAS_EKT, crudas=None, iot=None, catalogo=None):
        self.filas_en_periodo, self.columnas, self.consultas = filas_en_periodo, columnas, []
        self.crudas = crudas if crudas is not None else [
            ("TELCEL SAMSUNG A17", 6169, 31063113, 2.0, "2026-01-05", "6169 MEGA ZUMPANGO", "Samsung", "R9")
        ]
        self.iot = iot or []
        self.catalogo = catalogo or [("MEGA ZUMPANGO (6169)", "R9", 6169)]

    def exec_driver_sql(self, sql, parametros=()):
        self.consultas.append(sql)
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return _Cursor(filas=[(c,) for c in self.columnas])
        if "COUNT(*)" in sql:
            return _Cursor(escalar=self.filas_en_periodo)
        if "CustomerStores" in sql:
            return _Cursor(filas=self.catalogo)
        if "_IOT" in sql:
            return _Cursor(columnas=["Store", "Brand", "Material_Description", "Carrier", "Date", "QTY"], filas=self.iot)
        return _Cursor(columnas=list(self.columnas), filas=[f[: len(self.columnas)] for f in self.crudas])


class TestRawDataCompleto:
    def test_trae_todas_las_columnas_de_la_cruda_mas_region_y_mes(self):
        conexion = _Conexion(filas_en_periodo=163_034)
        df = contexto.ventas_crudas_completas(conexion, contexto.CRUDAS["ELEKTRA"], "2026-01-01", "2026-02-01")
        assert list(df.columns) == [*_COLUMNAS_EKT, "Region (catalogo)", "Mes"]
        # La region sale del catalogo por el numero de tienda, en memoria.
        assert df["Region (catalogo)"].tolist() == ["R9"] and df["Mes"].tolist() == [1]
        cruda = next(s for s in conexion.consultas if "EKT_B2B" in s and "COUNT" not in s)
        assert "[QTY] >= 1" in cruda  # las mismas filas que cuenta el bot
        assert "JOIN" not in cruda  # ya no se cruza en SQL: tardaba minutos
        assert df.attrs["pivot"] == {"tienda": "POS", "marca": "Marca", "mes": "Mes", "unidades": "QTY"}

    def test_si_no_cabe_ni_en_whatsapp_regresa_none_para_ir_agrupado(self):
        # Un trimestre de AT&T: ~1.3 millones de filas x 26 pasa de 20 M.
        conexion = _Conexion(filas_en_periodo=1_300_000, columnas=[f"c{i}" for i in range(24)])
        assert contexto.ventas_crudas_completas(conexion, contexto.CRUDAS["AT&T"], "a", "b") is None
        assert not any("CustomerStores" in s for s in conexion.consultas)

    def test_mucho_pero_cabe_va_solo_el_crudo(self):
        # Entre los dos topes (8 y 20 M celdas), como un mes de AT&T (11.6 M).
        conexion = _Conexion(filas_en_periodo=1_000_000)  # x 10 columnas = 10 M
        df = contexto.ventas_crudas_completas(conexion, contexto.CRUDAS["ELEKTRA"], "a", "b")
        assert df is not None and df.attrs["solo_crudos"] is True

    def test_poco_va_el_reporte_completo(self):
        df = contexto.ventas_crudas_completas(_Conexion(1000), contexto.CRUDAS["ELEKTRA"], "a", "b")
        assert df.attrs["solo_crudos"] is False

    def test_att_solo_cuenta_postpago_de_dealers_y_stores(self):
        conexion = _Conexion(1000, columnas=["Codigo de Organización", "Nombre de la Organización", "Fecha de Transacción", "Cantidad", "BRAND"],
                             crudas=[("O33", "O33 - TDA COACALCO", "2026-01-05", 1, "HONOR")])
        contexto.ventas_crudas_completas(conexion, contexto.CRUDAS["AT&T"], "a", "b")
        assert all("Postpago" in s for s in conexion.consultas if "ATT_B2B" in s and "INFORMATION" not in s)

    def test_coppel_lleva_su_mitad_iot_con_la_tienda_traducida(self):
        columnas = ["Tienda", "Clase", "Familia", "Marca", "Modelo", "Día", "Unidades"]
        conexion = _Conexion(
            filas_en_periodo=1000, columnas=columnas,
            crudas=[("100 • CENTRO-MAX", "24", "49", "OPPO", "A5", "2026-01-02", 1)],
            iot=[("100", "HONOR", "BAND 9", "Open Market", "2026-01-03", 4)],
            catalogo=[("100 • CENTRO-MAX", "R6", None)],
        )
        df = contexto.ventas_crudas_completas(conexion, contexto.CRUDAS["COPPEL"], "a", "b")
        iot = df[df["Fuente"] == "vw_ETL_COPPEL_IOT"].iloc[0]
        assert iot["Tienda"] == "100 • CENTRO-MAX" and iot["Region (catalogo)"] == "R6"
        assert iot["Modelo"] == "BAND 9" and pd.isna(iot["Clase"])
        assert df["Unidades"].sum() == 5

    def test_una_tienda_dos_veces_en_el_catalogo_no_duplica(self):
        # AT&T, enero de 2026: 303 filas de mas cuando se cruzaba en SQL.
        conexion = _Conexion(
            1000, columnas=["Codigo de Organización", "Nombre de la Organización", "Fecha de Transacción", "Cantidad", "BRAND"],
            crudas=[("O33", "O33 - TDA COACALCO", "2026-01-05", 1, "HONOR")],
            catalogo=[("O33 - TDA COACALCO", "R9", None), ("O33 -  TDA COACALCO", "R1", None)],
        )
        df = contexto.ventas_crudas_completas(conexion, contexto.CRUDAS["AT&T"], "a", "b")
        assert len(df) == 1


class TestElPivotUsaLosNombresDeCadaCruda:
    def test_con_las_columnas_de_elektra(self):
        df = pd.DataFrame(columns=["POS", "Marca", "Mes", "QTY"])
        df.attrs["pivot"] = {"tienda": "POS", "marca": "Marca", "mes": "Mes", "unidades": "QTY"}
        config = excel_pivot.config_ventas_por_tienda(df)
        assert config["filas"] == ["POS", "Marca"] and config["medida"] == "QTY"

    def test_la_agrupada_sigue_como_antes(self):
        df = pd.DataFrame(columns=["Region", "Tienda", "Marca", "Mes", "Unidades"])
        assert excel_pivot.config_ventas_por_tienda(df)["filas"] == ["Tienda", "Marca"]


class TestLaHojaDatos:
    def _hojas(self, tmp_path, monkeypatch, **kwargs):
        monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
        ruta = ExcelExporter.export_to_excel(pd.DataFrame({"Unidades": [181628]}), **kwargs)
        return load_workbook(ruta).sheetnames

    def test_sin_datos_cuando_hay_otras_hojas(self, tmp_path, monkeypatch):
        hojas = self._hojas(tmp_path, monkeypatch, contexto={"Raw_Data": pd.DataFrame({"x": [1]})},
                            con_datos=False)
        assert hojas == ["Raw_Data"]

    def test_sin_otras_hojas_va_igual(self, tmp_path, monkeypatch):
        assert self._hojas(tmp_path, monkeypatch, con_datos=False) == ["Datos"]

    def test_por_defecto_va(self, tmp_path, monkeypatch):
        hojas = self._hojas(tmp_path, monkeypatch, contexto={"Raw_Data": pd.DataFrame({"x": [1]})})
        assert hojas == ["Datos", "Raw_Data"]


class TestSiElCompletoFallaVaAgrupado:
    def test_un_error_o_un_plazo_vencido_no_deja_el_excel_sin_raw_data(self, monkeypatch):
        from tests.test_contexto import _MotorDeCatalogos

        def falla(*a):
            raise TimeoutError("se paso del plazo")

        agrupada = pd.DataFrame({"Region": ["R9"], "Tienda": ["T"], "Marca": ["HONOR"], "Mes": [1], "Unidades": [5]})
        monkeypatch.setattr(contexto, "ventas_crudas_completas", falla)
        monkeypatch.setattr(contexto, "ventas_crudas", lambda *a: agrupada.copy())
        hojas = contexto.contexto_del_resultado(
            pd.DataFrame({"Año": [2026], "Mes": [1], "Unidades": [181628]}),
            _MotorDeCatalogos(),
            "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Elektra] WHERE [Year] = 2026",
        )
        assert list(hojas) == ["Datos"]
        assert hojas["Datos"].attrs.get("agrupado") is True


class TestLaHojaDatosEsElCompletoAgrupado:
    def test_agrupa_por_clave_de_tienda_y_suma_igual(self):
        # La 6169 aparece con dos nombres: se agrupa por el numero.
        completo = pd.DataFrame({
            "POS": ["6169 EKT MEGA ZUMPANGO", "6169 MEGA ZUMPANGO", "9797 ELEKTRA.MX"],
            "Tienda #": [6169, 6169, 9797],
            "Marca": ["Samsung", "Samsung", "Apple"],
            "QTY": [2.0, 3.0, 7.0],
            "Region (catalogo)": ["R9", "R9", "R9"],
            "Mes": [1, 1, 1],
        })
        completo.attrs["pivot"] = {"tienda": "POS", "marca": "Marca", "mes": "Mes", "unidades": "QTY"}
        datos = contexto.agrupar_completo(completo, contexto.CRUDAS["ELEKTRA"])
        assert list(datos.columns) == ["Region", "Tienda", "Marca", "Mes", "Unidades"]
        assert len(datos) == 2 and datos["Unidades"].sum() == completo["QTY"].sum()
        assert datos.iloc[0]["Unidades"] == 7

    def test_las_filas_iot_sin_clave_se_agrupan_por_su_nombre(self):
        completo = pd.DataFrame({
            "Tienda": ["100 • CENTRO-MAX", "100 • CENTRO-MAX"],
            "Marca": ["HONOR", "HONOR"], "Unidades": [1, 4],
            "Region (catalogo)": ["R6", "R6"], "Mes": [1, 1],
        })
        datos = contexto.agrupar_completo(completo, contexto.CRUDAS["COPPEL"])
        assert datos["Unidades"].tolist() == [5]


class TestCuandoVaLaHojaConsulta:
    def test_solo_si_se_pide_un_desglose(self):
        import agent

        assert agent.pidio_desglose("reporte de ventas de Honor por tienda en Elektra")
        assert agent.pidio_desglose("sales by model at Coppel")
        assert not agent.pidio_desglose("ventas de Elektra de enero")
        assert not agent.pidio_desglose("mandame el excel de Coppel")


class TestElPeriodoSaleDeLaConsulta:
    """'Ventas de Honor por tienda en Elektra en enero 2026' devuelve tienda y
    unidades, sin fecha: el periodo solo esta en el WHERE."""

    def test_formas_de_filtrar(self):
        p = contexto.periodo_de_la_consulta
        assert p("WHERE [Year] = 2026 AND [Month] = 1") == ("2026-01-01", "2026-02-01")
        assert p("WHERE [Year]=2026 AND [Month] IN (6,7,8)") == ("2026-06-01", "2026-09-01")
        assert p("WHERE [Year] = 2026 AND [Month] BETWEEN 6 AND 8") == ("2026-06-01", "2026-09-01")
        assert p("WHERE v.[Day] >= '2026-06-01' AND v.[Day] < '2026-09-01'") == ("2026-06-01", "2026-09-01")
        assert p("WHERE [Year] = 2026") == ("2026-01-01", "2027-01-01")

    def test_si_no_esta_claro_no_adivina(self):
        assert contexto.periodo_de_la_consulta("WHERE [Year] IN (2025, 2026)") is None
        assert contexto.periodo_de_la_consulta("SELECT * FROM x") is None



class TestMuchosDatosSoloElCrudo:
    def test_con_solo_crudos_no_hay_datos_agrupados(self, monkeypatch):
        grande = pd.DataFrame({"POS": ["a"], "Marca": ["HONOR"], "Mes": [1], "QTY": [1]})
        grande.attrs["solo_crudos"] = True
        grande.attrs["pivot"] = {"tienda": "POS", "marca": "Marca", "mes": "Mes", "unidades": "QTY"}

        class _Motor:
            def connect(self):
                return self

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            connection = type("C", (), {"dbapi_connection": type("D", (), {"timeout": 0})()})()

        monkeypatch.setattr(contexto, "ventas_crudas_completas", lambda *a: grande)
        hojas = contexto._datos_y_raw(_Motor(), contexto.CRUDAS["ELEKTRA"], ("a", "b"), 10)
        assert list(hojas) == ["Raw_Data"]

    def test_el_escritor_rapido_reparte_en_hojas_y_da_formato(self, tmp_path, monkeypatch):
        import excel_exporter

        monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
        monkeypatch.setattr(excel_exporter, "MAX_FILAS_POR_HOJA_DE_EXCEL", 3)
        crudo = pd.DataFrame({
            "Fecha": pd.to_datetime(["2026-01-0%d" % d for d in range(1, 8)]),
            "Unidades": range(7),
        })
        ruta = ExcelExporter.export_to_excel(
            pd.DataFrame({"x": [1]}), contexto={"Raw_Data": crudo}, con_datos=False, rapido=True
        )
        libro = load_workbook(ruta)
        assert libro.sheetnames == ["Raw_Data", "Raw_Data (2)", "Raw_Data (3)"]
        assert sum(h.max_row - 1 for h in libro.worksheets) == 7
        assert libro["Raw_Data"]["A2"].number_format == "yyyy-mm-dd"


def test_att_dealers_y_cacs_son_una_sola_cadena():
    df = pd.DataFrame({"Retail": ["AT&T Dealers", "AT&T Cacs", "Unknown"], "QTY": [1, 2, 3]})
    assert contexto.retailer_del_resultado(df, "") == "AT&T"
    varias = pd.DataFrame({"Retail": ["Coppel", "ELEKTRA"], "QTY": [1, 2]})
    assert contexto.retailer_del_resultado(varias, "") is None
