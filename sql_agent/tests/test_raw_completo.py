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
    def __init__(self, filas_en_periodo, columnas=_COLUMNAS_EKT):
        self.filas_en_periodo, self.columnas, self.consultas = filas_en_periodo, columnas, []

    def exec_driver_sql(self, sql, parametros=()):
        self.consultas.append(sql)
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return _Cursor(filas=[(c,) for c in self.columnas])
        if "COUNT(*)" in sql:
            return _Cursor(escalar=self.filas_en_periodo)
        fila = ("TELCEL SAMSUNG A17", 6169, 31063113, 2.0, "2026-01-05", "6169 MEGA ZUMPANGO",
                "Samsung", "R9", "R9", 1)
        extras = [contexto.COLUMNA_REGION, contexto.COLUMNA_MES]
        fila = fila[: len(self.columnas)] + ("R9", 1)
        return _Cursor(columnas=[*self.columnas, *extras], filas=[fila])


class TestRawDataCompleto:
    def test_trae_todas_las_columnas_de_la_cruda_mas_region_y_mes(self):
        conexion = _Conexion(filas_en_periodo=163_034)
        df = contexto.ventas_crudas_completas(conexion, contexto.CRUDAS["ELEKTRA"], "2026-01-01", "2026-02-01")
        assert list(df.columns) == [*_COLUMNAS_EKT, "Region (catalogo)", "Mes"]
        sql = conexion.consultas[-1]
        assert "[QTY] >= 1" in sql  # las mismas filas que cuenta el bot
        assert df.attrs["pivot"] == {"tienda": "POS", "marca": "Marca", "mes": "Mes", "unidades": "QTY"}

    def test_si_no_cabe_regresa_none_para_ir_agrupado(self):
        # 445 mil filas x 24 columnas (un mes de AT&T) pasa del tope.
        conexion = _Conexion(filas_en_periodo=445_237, columnas=[f"c{i}" for i in range(24)])
        assert contexto.ventas_crudas_completas(conexion, contexto.CRUDAS["AT&T"], "a", "b") is None
        assert not any("UNION" in s or "SELECT v." in s for s in conexion.consultas)

    def test_coppel_lleva_su_mitad_iot_en_las_columnas_equivalentes(self):
        columnas = ["Tienda", "Clase", "Familia", "Marca", "Modelo", "Día", "Unidades"]
        conexion = _Conexion(filas_en_periodo=1000, columnas=columnas)
        contexto.ventas_crudas_completas(conexion, contexto.CRUDAS["COPPEL"], "a", "b")
        sql = conexion.consultas[-1]
        assert "UNION ALL" in sql and "vw_ETL_COPPEL_IOT" in sql
        assert "i.[Material_Description] AS [Modelo]" in sql
        assert "NULL AS [Clase]" in sql
        assert "[Fuente]" in sql


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
        assert hojas["Raw_Data"].attrs.get("agrupado") is True
