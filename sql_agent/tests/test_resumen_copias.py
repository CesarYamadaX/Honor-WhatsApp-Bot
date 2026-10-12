"""Promotoria y precios en el resumen local (pedido el 12/10/2026)."""

import sqlite3
from datetime import datetime

import pandas as pd
import pytest

import resumen_local as r

G = "[GTM BI Database].dbo"


@pytest.fixture
def archivo(tmp_path):
    ruta = tmp_path / "resumen.sqlite"
    with sqlite3.connect(ruta) as c:
        c.executescript("""
            CREATE TABLE [HCReport] ([ID] TEXT COLLATE NOCASE, [NAME] TEXT COLLATE NOCASE,
                [FIXED PROMOTER] REAL, [DOUBLE PROMOTER] REAL, [Week] TEXT COLLATE NOCASE, [YEAR] REAL);
            CREATE TABLE [CustomerStores] ([ID] TEXT COLLATE NOCASE, [Region] TEXT COLLATE NOCASE,
                [RetailName] TEXT COLLATE NOCASE);
            CREATE TABLE [Prices] ([Día/Periodo] TEXT, [Material Description] TEXT COLLATE NOCASE,
                [Precio Venta] REAL, [Retail] TEXT COLLATE NOCASE, [Carrier] TEXT COLLATE NOCASE);
            CREATE TABLE resumen_meta (clave TEXT PRIMARY KEY, valor TEXT);
            INSERT INTO [HCReport] VALUES ('A1', 'COPPEL CENTRO', 1, 0, 'W40', 2026),
                ('A2', 'TELCEL CAC NORTE', 0, 1, 'W40', 2026), ('A3', 'BODEGA SUR', 0, 0, 'W40', 2026);
            INSERT INTO [CustomerStores] VALUES ('A1', 'R4', 'COPPEL');
            INSERT INTO [Prices] VALUES ('2026-10-04', 'HONOR X6C', 4999, 'Coppel', 'Open Market'),
                ('2026-09-27', 'HONOR X6C', 5299, 'Coppel', 'Open Market');
        """)
        c.execute("INSERT INTO resumen_meta VALUES ('construido', ?)", (datetime.now().isoformat(),))
    return ruta


def test_la_cadena_sale_del_nombre_como_en_el_servidor(archivo):
    # La forma real de la promotoria: LEFT(... CHARINDEX(' ', h.[NAME] + ' ') - 1).
    q = (f"SELECT UPPER(COALESCE(cs.[RetailName], LEFT(h.[NAME], CHARINDEX(' ', h.[NAME] + ' ') - 1))) AS [Cadena], "
         f"COUNT(DISTINCT h.[ID]) AS [Tiendas] FROM {G}.[HCReport] h WITH (NOLOCK) "
         f"LEFT JOIN {G}.[CustomerStores] cs ON cs.[ID] = h.[ID] "
         "WHERE h.[YEAR] = 2026 AND (h.[FIXED PROMOTER] = 1 OR h.[DOUBLE PROMOTER] = 1) "
         "GROUP BY UPPER(COALESCE(cs.[RetailName], LEFT(h.[NAME], CHARINDEX(' ', h.[NAME] + ' ') - 1)))")
    df, _ = r.consultar(q, archivo)
    assert dict(zip(df["Cadena"], df["Tiendas"])) == {"COPPEL": 1, "TELCEL": 1}


def test_en_las_copias_si_se_cuenta_y_se_promedia(archivo):
    df, _ = r.consultar(f"SELECT COUNT(*) AS n, AVG([Precio Venta]) AS p FROM {G}.[Prices]", archivo)
    assert df.iloc[0].tolist() == [2, 5149.0]


def test_las_fechas_comparan_y_vuelven_como_fechas(archivo):
    df, _ = r.consultar(f"SELECT MAX([Día/Periodo]) AS f FROM {G}.[Prices] WHERE [Día/Periodo] >= '2026-10-01'", archivo)
    assert pd.api.types.is_datetime64_any_dtype(df["f"]) and df["f"].iloc[0] == pd.Timestamp("2026-10-04")
    df, _ = r.consultar(f"SELECT COUNT(*) AS n FROM {G}.[Prices] WHERE [Día/Periodo] = '2026-10-04'", archivo)
    assert df["n"].iloc[0] == 1
    df, _ = r.consultar(f"SELECT YEAR([Día/Periodo]) AS a, MONTH([Día/Periodo]) AS m FROM {G}.[Prices] "
                        "GROUP BY YEAR([Día/Periodo]), MONTH([Día/Periodo])", archivo)
    assert sorted(df["m"]) == [9, 10] and set(df["a"]) == {2026}


@pytest.mark.parametrize("consulta", [
    f"SELECT [KEY PRICES] FROM {G}.[Prices]",                              # no se copio
    f"SELECT CHARINDEX('a', [Retail], 3) FROM {G}.[Prices]",               # posicion de inicio
    f"SELECT DATEADD(day, 1, [Día/Periodo]) FROM {G}.[Prices]",            # fechas de SQL Server
    f"SELECT CAST([Día/Periodo] AS DATE) FROM {G}.[Prices]",               # en SQLite seria un numero
    f"SELECT * FROM {G}.[V_HCReport_Informes]",                            # vista no copiada
    f"SELECT * FROM {G}.[HCReport] h JOIN [RAW_DATA_B2B].dbo.[vw_ETL_COPPEL] v ON 1 = 1",  # mezcla
    "SELECT * FROM [OtraBase].dbo.[Prices]",                               # otra base
])
def test_lo_que_daria_otra_cosa_va_al_servidor(consulta):
    assert r.traducir(consulta) is None


def test_right_de_cero_es_vacio_como_en_sql_server(archivo):
    df, _ = r.consultar(f"SELECT RIGHT([Retail], 0) AS a, RIGHT([Retail], 3) AS b FROM {G}.[Prices]", archivo)
    assert df.iloc[0].tolist() == ["", "pel"]


def test_una_consulta_lenta_sobre_las_copias_se_corta_y_va_al_servidor(archivo, monkeypatch):
    # El servidor contesta precios en ~1 s; aqui una consulta con mal plan
    # tardo 7.8 s (12/10/2026). Con el tope, se corta y la contesta el servidor.
    monkeypatch.setattr(r, "MAX_SEGUNDOS_EN_COPIAS", -1)
    with sqlite3.connect(archivo) as c:
        c.executemany("INSERT INTO [Prices] VALUES ('2026-10-04', 'X', ?, 'Coppel', 'Open Market')",
                      [(i,) for i in range(300)])
    consulta = f"SELECT COUNT(*) AS n FROM {G}.[Prices] a JOIN {G}.[Prices] b ON a.[Retail] = b.[Retail]"
    assert r.traducir(consulta) is not None
    assert r.consultar(consulta, archivo) is None
