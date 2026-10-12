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
    df, _ = r.consultar(f"SELECT COUNT(*) AS n, AVG([FIXED PROMOTER]) AS p FROM {G}.[HCReport]", archivo)
    assert df.iloc[0].tolist() == [3, 1 / 3]


def test_los_precios_ya_no_se_contestan_aqui():
    # Se probaron el 12/10/2026 y se quitaron: el servidor ya es rapido con
    # ellos y aqui el precio actual por modelo tardaba 4-8 s.
    assert r.traducir(f"SELECT MAX([Día/Periodo]) FROM {G}.[Prices]") is None
    assert r.traducir(f"SELECT COUNT(*) FROM {G}.[UNICOS$]") is None


def test_sin_resumen_los_meses_salen_de_la_copia(tmp_path):
    # Al arrancar el resumen se borra; el aviso de "esa cadena aun no carga
    # el mes" sigue funcionando con la copia que dejo la construccion.
    import json
    resumen = tmp_path / "resumen.sqlite"
    (tmp_path / "meses_por_cadena.json").write_text(json.dumps({
        "guardado": datetime.now().isoformat(), "meses": {"Liverpool": [[2026, 8]], "Coppel": [[2026, 9]]}}))
    r.borrar(resumen)
    assert (tmp_path / "meses_por_cadena.json").exists()
    assert r.meses_por_cadena(resumen) == {"Liverpool": {(2026, 8)}, "Coppel": {(2026, 9)}}
    (tmp_path / "meses_por_cadena.json").write_text(json.dumps({
        "guardado": "2026-01-01T00:00:00", "meses": {"Coppel": [[2026, 1]]}}))
    assert r.meses_por_cadena(resumen) is None  # demasiado vieja


def test_al_arrancar_se_borra_el_resumen(tmp_path):
    viejo = tmp_path / "resumen.sqlite"
    viejo.write_bytes(b"de ayer")
    (tmp_path / "resumen.construyendo").write_bytes(b"a medias")
    r.borrar(viejo)
    assert not viejo.exists() and not (tmp_path / "resumen.construyendo").exists()
    r.borrar(viejo)  # sin nada que borrar, tampoco truena


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
    df, _ = r.consultar(f"SELECT RIGHT([RetailName], 0) AS a, RIGHT([RetailName], 3) AS b FROM {G}.[CustomerStores]", archivo)
    assert df.iloc[0].tolist() == ["", "PEL"]


def test_una_consulta_lenta_sobre_las_copias_se_corta_y_va_al_servidor(archivo, monkeypatch):
    # El servidor contesta precios en ~1 s; aqui una consulta con mal plan
    # tardo 7.8 s (12/10/2026). Con el tope, se corta y la contesta el servidor.
    monkeypatch.setattr(r, "MAX_SEGUNDOS_EN_COPIAS", -1)
    with sqlite3.connect(archivo) as c:
        c.executemany("INSERT INTO [HCReport] VALUES (?, 'COPPEL X', 1, 0, 'W40', 2026)",
                      [(f"B{i}",) for i in range(300)])
    consulta = f"SELECT COUNT(*) AS n FROM {G}.[HCReport] a JOIN {G}.[HCReport] b ON a.[Week] = b.[Week]"
    assert r.traducir(consulta) is not None
    assert r.consultar(consulta, archivo) is None


def test_el_anio_y_el_mes_de_la_fecha_se_contestan_con_year_y_month():
    # Medido: en las nueve vistas [Year]/[Month] = YEAR/MONTH([Date]) siempre.
    q = ("SELECT YEAR([Date]) AS a, MONTH(v.[Date]) AS m, SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_COPPEL] v "
         "WHERE DATEPART(year, v.[Date]) = 2026 GROUP BY YEAR([Date]), MONTH(v.[Date])")
    local = r.traducir(q)
    assert local is not None and "Date" not in local
    assert "v.[Month]" in local and "v.[Year] = 2026" in local
    # La fecha usada para otra cosa (un dia) sigue yendo al servidor.
    assert r.traducir("SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_COPPEL] WHERE [Date] >= '2026-09-01'") is None
