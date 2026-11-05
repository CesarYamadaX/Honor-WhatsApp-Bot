"""La venta con modelo y region en el resumen local (22/10/2026): "sell out
y share de Telcel por region" tardo 176 s porque siempre iba al servidor."""

import pytest

import resumen_local as rl
from tests import test_resumen_local as base

# (Retail, Brand, Carrier, Modelo, Clasificacion, Region, Year, Month, WEEK, Descripcion, QTY)
_CON_MODELO = {
    "vw_ETL_TELCEL_B2B": [
        ("TELCEL CAC", "HONOR", "Telcel", "HONOR X6C 256GB 4G", "SMARTPHONE", "R7", 2026, 9, 37, None, 16.0),
        ("TELCEL CAC", "SAMSUNG", "Telcel", "SAMSUNG A17 128GB 4G", "SMARTPHONE", "R7", 2026, 9, 37, None, 184.0),
        ("TELCEL RETAIL", "HONOR", "Telcel", "HONOR X6C 256GB 4G", "SMARTPHONE", "R1", 2026, 9, 38, None, 4.0),
    ],
    "vw_ETL_Liverpool": [
        ("Liverpool Offline", "HONOR", "Open Market", "HONOR 400 512GB 5G", "SMARTPHONE", "R5", 2026, 9, 38, None, 10.0),
    ],
}


class _Cursor(base._Cursor):
    def execute(self, sql):
        if "UNICOS" in sql:
            for vista, filas in _CON_MODELO.items():
                if f"[{vista}]" in sql:
                    self.filas = list(filas)
                    return
            self.filas = []
            return
        super().execute(sql)


class _Crudo(base._Crudo):
    def cursor(self):
        return _Cursor([])


class _Motor:
    def raw_connection(self):
        return _Crudo()


@pytest.fixture
def archivo(tmp_path):
    destino = tmp_path / "resumen_con_modelo.sqlite"
    rl.construir_con_modelo(_Motor(), destino)
    return destino


def _consultar(q, archivo):
    return rl.consultar(q, archivo_con_modelo=archivo)


VISTA = "[RAW_DATA_B2B].dbo.[vw_ETL_Con_Modelo]"


def test_se_guarda_al_construir(archivo):
    assert rl.estado(archivo)[f"filas {rl.CON_MODELO}"] == "4"


def test_por_region_y_share_sale_del_resumen(archivo):
    q = (f"SELECT [Region], SUM(CASE WHEN [Brand] = 'HONOR' THEN [QTY] ELSE 0 END) AS [Honor], "
         f"SUM([QTY]) AS [Total] FROM {VISTA} WHERE [Retail] LIKE 'TELCEL%' AND [Year] = 2026 "
         f"AND [Month] = 9 GROUP BY [Region] ORDER BY [Region]")
    df, sql = _consultar(q, archivo)
    assert df.values.tolist() == [["R1", 4.0, 4.0], ["R7", 16.0, 200.0]]
    assert "RAW_DATA_B2B" not in sql


def test_por_modelo_y_semana_tambien(archivo):
    q = (f"SELECT [Modelo], SUM([QTY]) FROM {VISTA} WHERE [Brand] = 'HONOR' AND YEAR([Date]) = 2026 "
         f"AND [WEEK] = 38 GROUP BY [Modelo] ORDER BY [Modelo]")
    df, _ = _consultar(q, archivo)
    assert df.values.tolist() == [["HONOR 400 512GB 5G", 10.0], ["HONOR X6C 256GB 4G", 4.0]]


@pytest.mark.parametrize("columna", ["[Store]", "[Material_Description]", "[Date]"])
def test_lo_que_no_tiene_va_al_servidor(archivo, columna):
    q = f"SELECT {columna}, SUM([QTY]) FROM {VISTA} WHERE [Year] = 2026 GROUP BY {columna}"
    assert _consultar(q, archivo) is None


def test_no_se_mezcla_con_las_otras_vistas(archivo):
    q = (f"SELECT SUM(m.[QTY]) FROM {VISTA} m JOIN [RAW_DATA_B2B].dbo.[vw_ETL_Sears] s "
         "ON m.[Retail] = s.[Retail]")
    assert _consultar(q, archivo) is None


def test_va_aparte_y_sin_el_las_demas_vistas_siguen(tmp_path):
    # Armarlo cuesta ~23 min de servidor: va en su archivo, cada 12 horas.
    destino = tmp_path / "resumen.sqlite"
    rl.construir(base._Motor(), destino)
    sin = tmp_path / "no_existe.sqlite"
    assert rl.consultar(f"SELECT SUM([QTY]) FROM {VISTA}", destino, sin) is None
    assert rl.consultar("SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Sears]", destino, sin) is not None


def test_al_arrancar_se_borra_tambien(tmp_path, monkeypatch):
    base_ = tmp_path / "resumen.sqlite"
    con_modelo = tmp_path / rl.ARCHIVO_CON_MODELO.name
    rl.construir_con_modelo(_Motor(), con_modelo)
    detener = __import__("threading").Event()
    detener.set()  # que el hilo no construya nada
    rl.mantener_al_dia(lambda: None, base_, detener).join(2)
    assert not con_modelo.exists()


class TestElReporteDetallado:
    """Medido el 5/11/2026: todas las marcas por modelo y mes de las siete
    cadenas, 6.5 min de servidor; el top por cadena y mes, 12 min."""

    def test_sin_tienda_ni_dia_sale_de_aqui(self, archivo):
        import reporte_detallado as rd

        def servidor(sql, de_que):
            raise AssertionError(f"fue al servidor: {de_que}")

        r = rd.generar(servidor, ["TELCEL", "LIVERPOOL"], ["canal", "marca", "modelo"], (2026, 9), (2026, 9),
                       leer_del_resumen=lambda sql: rl.leer_con_modelo(sql, archivo))
        assert {p.cadena: p.unidades for p in r.partes} == {"TELCEL": 204.0, "LIVERPOOL": 10.0}
        assert r.partes[0].tabla.columns.tolist() == ["Canal", "Año", "Mes", "Marca", "Modelo", "Unidades"]

    def test_por_tienda_por_dia_o_con_el_ponderado_va_al_servidor(self, archivo):
        import reporte_detallado as rd

        for columnas, precio in ((["tienda", "modelo"], False), (["modelo", "dia"], False), (["modelo"], True)):
            servidor = []
            rd.generar(lambda sql, de_que: servidor.append(de_que), ["SEARS"], columnas, (2026, 9), (2026, 9),
                       precio=precio, leer_del_resumen=lambda sql: rl.leer_con_modelo(sql, archivo))
            assert "SEARS" in servidor, columnas

    def test_si_no_esta_al_dia_va_al_servidor(self, tmp_path):
        assert rl.leer_con_modelo("SELECT 1", tmp_path / "no_existe.sqlite") is None
