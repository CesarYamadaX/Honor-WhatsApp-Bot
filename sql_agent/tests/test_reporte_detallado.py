"""El reporte detallado: venta y precio por separado, cruzados aqui (19/10/2026)."""

from datetime import date

import pandas as pd
import pytest

import reporte_detallado as rd


def _venta(*filas, columnas=("Canal", "Año", "Mes", "Tienda", "Modelo", rd.DESCRIPCION, "Unidades")):
    return pd.DataFrame(filas, columns=list(columnas))


class TestLoQuePide:
    def test_cadenas_con_alias_y_sin_repetir(self):
        # Tal cual se pidio el 19/10/2026.
        assert rd.cadenas_pedidas(
            "coppel, telcel cac, movistar cacs y dealers, Liverpool, att cacs y dealers, seas, Elektra"
        ) == ["COPPEL", "TELCEL", "MOVISTAR", "LIVERPOOL", "AT&T", "SEARS", "ELEKTRA"]
        assert rd.cadenas_pedidas("Coppel, Telcel CAC, AT&T, Sears, Elektra, Liverpool") == [
            "COPPEL", "TELCEL", "AT&T", "SEARS", "ELEKTRA", "LIVERPOOL",
        ]
        assert rd.cadenas_pedidas("") == list(rd.acceso.CADENAS)
        assert rd.cadenas_pedidas("todas") == list(rd.acceso.CADENAS)

    def test_una_cadena_que_no_existe_se_dice(self):
        with pytest.raises(ValueError, match="(?i)walmart"):
            rd.cadenas_pedidas("Walmart")

    def test_meses(self):
        hoy = date(2026, 10, 19)
        assert rd.mes_de("2026-05", hoy) == (2026, 5)
        assert rd.mes_de("05/2026", hoy) == (2026, 5)
        assert rd.mes_de("", hoy) == (2026, 10)
        with pytest.raises(ValueError):
            rd.mes_de("mayo", hoy)

    def test_columnas(self):
        assert rd.columnas_pedidas("canal, mes, tienda, modelo y precio") == ["canal", "tienda", "modelo"]
        assert rd.columnas_pedidas("") == ["canal", "modelo"]
        with pytest.raises(ValueError, match="color"):
            rd.columnas_pedidas("color")


class TestLaConsulta:
    def test_lee_la_vista_con_modelo_de_una_cadena_y_por_ano_y_mes(self):
        sql = rd.sql_de_ventas("COPPEL", ["canal", "tienda", "modelo"], (2026, 5), (2026, 10))
        assert "vw_ETL_COPPEL" in sql and "vw_ETL_Sears" not in sql
        assert "([Year] * 100 + [Month]) BETWEEN 202605 AND 202610" in sql
        assert "[Store] AS [Tienda]" in sql and "[Modelo] AS [Modelo]" in sql
        # Lo que no esta en UNICOS$ conserva su texto, para no juntarse.
        assert rd.DESCRIPCION in sql
        assert "Prices" not in sql

    def test_la_marca_va_escapada(self):
        assert "[Brand] = 'O''BRIEN'" in rd.sql_de_ventas("SEARS", ["modelo"], (2026, 5), (2026, 5), "O'BRIEN")

    def test_precios_por_dia_sin_cruzar_en_el_servidor(self):
        sql = rd.sql_de_precios((2026, 5), (2026, 10))
        assert "UNICOS" not in sql and "JOIN" not in sql
        assert "CAST([Día/Periodo] AS date) AS [Dia]" in sql
        # Desde un mes antes: la venta del dia 1 toma el ultimo precio anterior.
        assert "DATEADD(MONTH, -1, '2026-05-01')" in sql and "DATEADD(MONTH, 1, '2026-10-01')" in sql

    def test_con_precio_la_misma_lectura_trae_la_venta_por_dia(self):
        sql = rd.sql_de_ventas("COPPEL", ["tienda", "modelo", "semana"], (2026, 5), (2026, 10), diario=True)
        # Una sola lectura de las vistas, igual que sin precio.
        sin_precio = rd.sql_de_ventas("COPPEL", ["tienda", "modelo", "semana"], (2026, 5), (2026, 10))
        assert sql.count("vw_ETL_") == sin_precio.count("vw_ETL_")
        assert "GROUPING SETS" in sql and "GROUPING([Date]) AS [_detalle]" in sql
        assert "([Retail], [Year], [Month], [Modelo], [Date], [WEEK])" in sql


def _precios(*filas):
    crudos = pd.DataFrame(filas, columns=["Retail", "Producto", "Dia", "Precio"])
    return rd.precios_por_modelo(crudos, lambda x: "HONOR X6C 256GB 4G" if "X6C" in x else None)


def _dias(*filas):
    return pd.DataFrame(filas, columns=["Canal", "Año", "Mes", "Modelo", "Dia", "Unidades"])


X6C = "HONOR X6C 256GB 4G"
LLAVES = ["Canal", "Año", "Mes", "Modelo"]


class TestPrecioPorFecha:
    """Pedido el 19/10/2026: el precio de acuerdo con la fecha de la venta,
    porque cambia dentro del mes."""

    def test_cada_dia_su_precio_y_la_fila_ponderada_por_lo_vendido(self):
        precios = _precios(("Coppel", "COP X6C", "2026-09-01", 5000.0),
                           ("Coppel", "COP X6C", "2026-09-15", 4000.0))
        # Cada dia hay precio en las diarias; aqui solo cambia el 15.
        for dia in pd.date_range("2026-09-02", "2026-09-30"):
            precios.loc[len(precios)] = ["coppel", "COPPEL", X6C, dia, 2026, 9,
                                         5000.0 if dia.day < 15 else 4000.0, False]
        dias = _dias(("Coppel", 2026, 9, X6C, "2026-09-03", 10), ("Coppel", 2026, 9, X6C, "2026-09-20", 30))
        precio = rd.precio_por_fecha(dias, precios, LLAVES)
        assert precio[rd.PRECIO].tolist() == [4250.0]  # (10 x 5000 + 30 x 4000) / 40

    def test_elektra_toma_el_precio_de_su_semana(self):
        # Elektra pone un precio cada domingo; la semana de venta va de domingo a sabado.
        precios = _precios(("Elektra", "EKT X6C", "2026-09-06", 3000.0),
                           ("Elektra", "EKT X6C", "2026-09-13", 2800.0))
        dias = _dias(("ELEKTRA", 2026, 9, X6C, "2026-09-09", 1), ("ELEKTRA", 2026, 9, X6C, "2026-09-14", 1))
        assert rd._precio_en(
            dias.assign(Dia=pd.to_datetime(dias["Dia"]), retail="elektra"), precios, "retail"
        ).tolist() == [3000.0, 2800.0]

    def test_sears_y_movistar_van_por_mes(self):
        # Un solo precio al cierre: una venta del 10 de septiembre toma el de
        # septiembre (30/09), no el de agosto (31/08), que es el mas cercano.
        precios = _precios(("Sears", "SRS X6C", "2026-08-31", 6000.0), ("Sears", "SRS X6C", "2026-09-30", 5500.0))
        dias = _dias(("SEARS", 2026, 9, X6C, "2026-09-10", 2))
        assert rd.precio_por_fecha(dias, precios, LLAVES)[rd.PRECIO].tolist() == [5500.0]

    def test_el_canal_exacto_y_si_no_el_de_su_cadena(self):
        precios = _precios(("Liverpool Offline", "LIV X6C", "2026-09-01", 3000.0),
                           ("Liverpool Online", "LIV X6C", "2026-09-01", 2800.0),
                           ("Telcel CAC", "TEL X6C", "2026-09-01", 2500.0))
        dias = _dias(("Liverpool Online", 2026, 9, X6C, "2026-09-02", 1),
                     ("TELCEL RETAIL", 2026, 9, X6C, "2026-09-02", 1))
        precio = rd.precio_por_fecha(dias, precios, LLAVES)
        assert dict(zip(precio["Canal"], precio[rd.PRECIO])) == {"Liverpool Online": 2800.0, "TELCEL RETAIL": 2500.0}

    def test_un_precio_de_hace_mas_de_una_semana_no_se_usa(self):
        precios = _precios(("Coppel", "COP X6C", "2026-08-01", 5000.0), ("Coppel", "COP X6C", "2026-08-02", 5000.0))
        dias = _dias(("Coppel", 2026, 9, X6C, "2026-09-20", 3))
        assert rd.precio_por_fecha(dias, precios, LLAVES).empty

    def test_la_parte_separa_el_detalle_y_le_pone_el_precio(self):
        precios = _precios(("Coppel", "COP X6C", "2026-09-01", 5000.0), ("Coppel", "COP X6C", "2026-09-02", 5000.0))
        ventas = pd.DataFrame(
            [("Coppel", 2026, 9, "T1", X6C, None, None, 1, 4),
             ("Coppel", 2026, 9, "T2", None, "COP RARO", None, 1, 7),
             ("Coppel", 2026, 9, None, X6C, None, "2026-09-02", 0, 4)],
            columns=["Canal", "Año", "Mes", "Tienda", "Modelo", rd.DESCRIPCION, "Dia", "_detalle", "Unidades"],
        )
        parte = rd.armar_parte("COPPEL", ventas, ["tienda", "modelo"], precios)
        assert parte.tabla["Tienda"].tolist() == ["T1", "T2"]
        assert parte.tabla[rd.PRECIO].tolist()[0] == 5000.0 and pd.isna(parte.tabla[rd.PRECIO].tolist()[1])
        assert parte.unidades == 11 and parte.con_precio == pytest.approx(4 / 11)


class TestElCruce:
    def test_la_parte_va_ordenada_con_modelo_vacio_y_su_descripcion(self):
        venta = _venta(("Unknown", 2026, 6, "T1", None, "ATT RARO", 3),
                       ("AT&T Cacs", 2026, 5, "T1", "HONOR X6C 256GB 4G", None, 5),
                       ("AT&T Cacs", 2026, 5, "T2", "HONOR X6C 256GB 4G", None, 0))
        parte = rd.armar_parte("AT&T", venta, ["canal", "tienda", "modelo"], None)
        assert parte.tabla.columns.tolist() == ["Canal", "Año", "Mes", "Tienda", "Modelo", rd.DESCRIPCION, "Unidades"]
        assert parte.tabla.values.tolist() == [
            ["AT&T Cacs", 2026, 5, "T1", "HONOR X6C 256GB 4G", "", 5.0],
            ["AT&T", 2026, 6, "T1", "", "ATT RARO", 3.0],
        ]
        assert parte.unidades == 8 and parte.sin_modelo == pytest.approx(3 / 8)

    def test_sin_nada_sin_homologar_no_va_la_columna_de_descripcion(self):
        venta = _venta(("Coppel", 2026, 5, "T1", "HONOR X6C 256GB 4G", None, 5))
        assert rd.DESCRIPCION not in rd.armar_parte("COPPEL", venta, ["tienda", "modelo"], None).tabla.columns


class TestPreciosDeATT:
    def test_sin_la_red_al_final_casa_con_unicos(self):
        # Medido el 19/10/2026: tal cual, 0% de AT&T; sin la red, 2 de cada 3.
        catalogo = {"AT&T CACS APPLE IPHONE 15 256GB": "APPLE IPHONE 15 256GB 5G"}
        assert rd.modelo_del_precio("AT&T CACS APPLE IPHONE 15 256GB 4G", catalogo.get) == "APPLE IPHONE 15 256GB 5G"
        assert rd.modelo_del_precio("AT&T CACS SAMSUNG GALAXY A35 128GB 5G", catalogo.get) is None


class TestGenerar:
    def test_cada_cadena_se_entrega_en_cuanto_termina(self):
        listas = []
        rd.generar(lambda sql, de_que: _venta((de_que, 2026, 5, "T1", "HONOR X6C 256GB 4G", None, 1)),
                   ["COPPEL", "SEARS"], ["tienda", "modelo"], (2026, 5), (2026, 5),
                   al_terminar=lambda parte: listas.append(parte.cadena))
        assert listas == ["COPPEL", "SEARS"]

    def test_una_consulta_por_cadena_y_una_de_precios(self):
        corridas = []

        def ejecutar(sql, de_que):
            corridas.append(de_que)
            if "Prices" in sql:
                return pd.DataFrame([("Coppel", "COP X6C", "2026-05-02", 2500.0)],
                                    columns=["Retail", "Producto", "Dia", "Precio"])
            assert "GROUPING SETS" in sql
            cadena = "Coppel" if "vw_ETL_COPPEL" in sql else "SEARS"
            return pd.DataFrame(
                [(cadena, 2026, 5, "T1", X6C, None, None, 1, 10), (cadena, 2026, 5, None, X6C, None, "2026-05-03", 0, 10)],
                columns=["Canal", "Año", "Mes", "Tienda", "Modelo", rd.DESCRIPCION, "Dia", "_detalle", "Unidades"],
            )

        r = rd.generar(ejecutar, ["COPPEL", "SEARS"], ["canal", "tienda"], (2026, 5), (2026, 5),
                       precio=True, buscar=lambda x: X6C)
        assert corridas == ["precios", "COPPEL", "SEARS"]
        assert [p.cadena for p in r.partes] == ["COPPEL", "SEARS"]
        # Con precio, el modelo va aunque no se pidiera: el precio es por modelo.
        assert "modelo" in r.columnas
        assert r.partes[0].con_precio == 1.0 and r.partes[1].con_precio == 0.0
        assert r.unidades == 20
        r.partes[1].sin_modelo = 0.001
        texto = rd.como_texto(r, [], 2)
        assert "SEARS: 10 unidades (menos del 1% de las unidades sin modelo" in texto
        assert "COPPEL: 10 unidades (100% de las unidades con precio)" in texto
        assert "20 unidades" in texto and "uno por cadena" in texto and "NO hagas otra consulta" in texto

    def test_una_cadena_sin_venta_no_lleva_parte(self):
        r = rd.generar(lambda sql, de_que: _venta(), ["COPPEL"], ["modelo"], (2026, 5), (2026, 5))
        assert r.partes == []

    def test_si_falla_una_cadena_o_los_precios_las_demas_salen(self):
        def ejecutar(sql, de_que):
            if de_que in ("COPPEL", "precios"):
                return None
            return _venta(("SEARS", 2026, 5, "T1", "HONOR X6C 256GB 4G", None, 10))

        r = rd.generar(ejecutar, ["COPPEL", "SEARS"], ["tienda", "modelo"], (2026, 5), (2026, 5), precio=False)
        assert [p.cadena for p in r.partes] == ["SEARS"]
        r = rd.generar(ejecutar, ["COPPEL", "SEARS"], ["tienda", "modelo"], (2026, 5), (2026, 5), precio=True)
        assert [p.cadena for p in r.partes] == ["SEARS"]
