"""El resumen local: que contesta, que manda al servidor y que no cambia.

Lo que importa es que NUNCA de otro numero que el servidor. Por eso cada
cosa que podria cambiar el resultado sin dar error (contar filas, promediar,
`+` entre textos, una columna que el resumen no tiene) se manda al servidor.
"""

import sqlite3
from contextlib import closing

import pytest

import resumen_local as rl

# (Retail, Brand, Material_Description, Carrier, Year, Month, WEEK, QTY)
_FILAS = {
    "vw_ETL_Sears": [
        ("SEARS", "HONOR", "HONOR X6C 256GB 4G", "Telcel", 2026, 8, 33, 30.0),
        ("SEARS", "SAMSUNG", "SAMSUNG A17", "Telcel", 2026, 8, 33, 70.0),
    ],
    "vw_ETL_COPPEL": [
        ("Coppel", "HONOR", "HONOR X6C 256GB 4G", "Telcel", 2026, 8, 34, 100.0),
        ("Coppel", "APPLE", "IPHONE 15", "Telcel", 2026, 8, 34, 300.0),
    ],
    "vw_ETL_COPPEL_IOT": [
        ("Coppel", "HONOR", "HONOR BAND 9", "Open Market", 2026, 8, 34, 20.0),
    ],
}


class _Cursor:
    def __init__(self, filas):
        self.filas = list(filas)

    def execute(self, sql):
        for vista, filas in _FILAS.items():
            if f"[{vista}]" in sql:
                self.filas = list(filas)
                return
        self.filas = []

    def fetchmany(self, n):
        lote, self.filas = self.filas[:n], self.filas[n:]
        return lote


class _Crudo:
    def cursor(self):
        return _Cursor([])

    def close(self):
        pass


class _Motor:
    def raw_connection(self):
        return _Crudo()


@pytest.fixture
def archivo(tmp_path):
    destino = tmp_path / "resumen.sqlite"
    rl.construir(_Motor(), destino)
    return destino


def _valor(archivo, query):
    df, _ = rl.consultar(query, archivo)
    return df.iloc[0, 0]


class TestQueContesta:
    def test_el_total_de_honor_sale_de_las_nueve_vistas(self, archivo):
        q = ("SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B] "
             "WHERE [Brand] = 'HONOR' AND [Year] = 2026 AND [Month] = 8;")
        assert _valor(archivo, q) == 150.0

    def test_el_share_por_cadena(self, archivo):
        q = ("SELECT [Retail], SUM(CASE WHEN [Brand] = 'HONOR' THEN [QTY] ELSE 0 END) * 100.0 "
             "/ NULLIF(SUM([QTY]), 0) AS [Share] FROM [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B] "
             "WHERE [Year] = 2026 AND [Month] = 8 GROUP BY [Retail] ORDER BY [Retail]")
        df, _ = rl.consultar(q, archivo)
        # Coppel con su IOT: 120 de HONOR de 420.
        assert dict(zip(df["Retail"], df["Share"])) == pytest.approx({"Coppel": 120 / 420 * 100, "SEARS": 30.0})

    def test_como_en_el_servidor_las_mayusculas_no_importan(self, archivo):
        # La intercalacion del servidor no distingue mayusculas: 'Honor' casa
        # con 'HONOR'. Si aqui no, el resumen daria cero en silencio.
        q = "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Sears] WHERE [Brand] = N'Honor'"
        assert _valor(archivo, q) == 30.0

    def test_coppel_solo_se_completa_con_su_otra_mitad(self, archivo):
        q = "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_COPPEL] WHERE [Brand] = 'HONOR'"
        assert _valor(archivo, q) == 120.0

    def test_si_el_modelo_ya_unio_las_dos_no_se_duplica(self, archivo):
        q = ("SELECT SUM([QTY]) FROM (SELECT [Brand], [QTY] FROM [RAW_DATA_B2B].dbo.[vw_ETL_COPPEL] "
             "UNION ALL SELECT [Brand], [QTY] FROM [RAW_DATA_B2B].dbo.[vw_ETL_COPPEL_IOT]) t "
             "WHERE [Brand] = 'HONOR'")
        assert _valor(archivo, q) == 120.0

    def test_top_se_vuelve_limit(self, archivo):
        q = ("SELECT TOP 1 [Brand], SUM([QTY]) AS [U] FROM [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B] "
             "GROUP BY [Brand] ORDER BY SUM([QTY]) DESC")
        df, sql = rl.consultar(q, archivo)
        assert list(df["Brand"]) == ["APPLE"] and sql.endswith("LIMIT 1")

    def test_las_semanas_tambien(self, archivo):
        q = "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B] WHERE [WEEK] = 34"
        assert _valor(archivo, q) == 420.0


class TestQueMandaAlServidor:
    @pytest.mark.parametrize("query", [
        # Columnas que el resumen no tiene.
        "SELECT [Store], SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Sears] GROUP BY [Store]",
        "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Sears] WHERE [Date] >= '2026-08-15'",
        "SELECT [Key prices] FROM [RAW_DATA_B2B].dbo.[vw_ETL_Sears]",
        # Una fila del resumen es un grupo, no una venta.
        "SELECT COUNT(*) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Sears]",
        "SELECT AVG([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Sears]",
        "SELECT MAX([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Sears]",
        # `+` entre textos en SQLite es una suma.
        "SELECT CAST([Year] AS VARCHAR(4)) + '-' + CAST([Month] AS VARCHAR(2)) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Sears]",
        # Otra tabla u otra base.
        "SELECT SUM(v.[QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Sears] v JOIN [GTM BI Database].dbo.[UNICOS$] u ON u.[DESCRIPCION] = v.[Material_Description]",
        "SELECT * FROM [RAW_DATA_B2B].dbo.[SEARS_B2B]",
        # Funciones de SQL Server que SQLite no tiene igual.
        "SELECT DATEPART(week, [Year]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Sears]",
        "SELECT * FROM (SELECT TOP 5 [Brand] FROM [RAW_DATA_B2B].dbo.[vw_ETL_Sears]) t",
        # No es una consulta de lectura.
        "EXEC algo",
    ])
    def test_no_se_traduce(self, query):
        assert rl.traducir(query) is None

    def test_sin_archivo_todo_va_al_servidor(self, tmp_path):
        q = "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Sears]"
        assert rl.consultar(q, tmp_path / "no_existe.sqlite") is None

    def test_si_sqlite_no_puede_va_al_servidor(self, archivo):
        # Algo que pasa el filtro pero SQLite no entiende: no truena, cede.
        q = "SELECT SQUARE([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Sears]"
        assert rl.consultar(q, archivo) is None


class TestConstruir:
    def test_guarda_cuando_y_cuanto(self, archivo):
        estado = rl.estado(archivo)
        assert estado["listo"] and estado["construido"]
        assert estado["filas vw_ETL_COPPEL"] == "2"
        assert estado["unidades vw_ETL_COPPEL"] == "400"

    def test_un_resumen_nuevo_reemplaza_al_anterior_entero(self, archivo):
        _FILAS["vw_ETL_Sears"].append(
            ("SEARS", "HONOR", "HONOR 400", "Telcel", 2026, 9, 37, 5.0)
        )
        try:
            rl.construir(_Motor(), archivo)
            q = "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Sears] WHERE [Brand] = 'HONOR'"
            assert _valor(archivo, q) == 35.0
            assert not archivo.with_suffix(".construyendo").exists()
        finally:
            _FILAS["vw_ETL_Sears"].pop()

    def test_el_archivo_se_abre_solo_para_leer(self, archivo):
        with pytest.raises(sqlite3.OperationalError):
            with closing(rl._abrir(archivo)) as conexion:
                conexion.execute("DELETE FROM [vw_ETL_Sears]")
