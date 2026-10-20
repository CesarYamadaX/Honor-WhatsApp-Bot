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

    def test_precios_por_mes_sin_cruzar_en_el_servidor(self):
        sql = rd.sql_de_precios((2026, 5), (2026, 10))
        assert "UNICOS" not in sql and "JOIN" not in sql
        assert "'2026-05-01'" in sql and "DATEADD(MONTH, 1, '2026-10-01')" in sql


class TestElCruce:
    def test_precio_por_retail_exacto_y_si_no_por_cadena(self):
        precios = pd.DataFrame(
            [("Liverpool Offline", "LIV X6C", 2026, 5, 3000.0),
             ("Liverpool Online", "LIV X6C", 2026, 5, 2800.0),
             ("Coppel", "COP X6C", 2026, 5, 2500.0)],
            columns=["Retail", "Producto", "Año", "Mes", "Precio"],
        )
        exacto, por_cadena = rd.precios_por_modelo(precios, lambda t: "HONOR X6C 256GB 4G")
        venta = _venta(("Liverpool Offline", 2026, 5, "T1", "HONOR X6C 256GB 4G", "", 4),
                       ("Liverpool IOT", 2026, 5, "T1", "HONOR X6C 256GB 4G", "", 1),
                       ("Coppel", 2026, 6, "T2", "HONOR X6C 256GB 4G", "", 2),
                       ("Coppel", 2026, 5, "T2", "", "COP RARO", 7))
        con = rd.con_precio(venta, exacto, por_cadena)
        assert con[rd.PRECIO].tolist()[:2] == [3000.0, 2900.0]
        # Junio sin precio, y lo que no tiene modelo tampoco: vacio.
        assert con[rd.PRECIO].isna().tolist()[2:] == [True, True]

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
                return pd.DataFrame([("Coppel", "COP X6C", 2026, 5, 2500.0)],
                                    columns=["Retail", "Producto", "Año", "Mes", "Precio"])
            cadena = "Coppel" if "vw_ETL_COPPEL" in sql else "SEARS"
            return _venta((cadena, 2026, 5, "T1", "HONOR X6C 256GB 4G", None, 10))

        r = rd.generar(ejecutar, ["COPPEL", "SEARS"], ["canal", "tienda"], (2026, 5), (2026, 5),
                       precio=True, buscar=lambda t: "HONOR X6C 256GB 4G")
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

        r = rd.generar(ejecutar, ["COPPEL", "SEARS"], ["tienda", "modelo"], (2026, 5), (2026, 5), precio=True)
        assert [p.cadena for p in r.partes] == ["SEARS"]
        assert r.partes[0].con_precio == 0.0
