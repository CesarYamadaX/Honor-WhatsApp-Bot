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
            if de_que == "scrap":
                return pd.DataFrame(columns=["Retail", "Producto", "Dia", "Precio"])
            assert "GROUPING SETS" in sql
            cadena = "Coppel" if "vw_ETL_COPPEL" in sql else "SEARS"
            return pd.DataFrame(
                [(cadena, 2026, 5, "T1", X6C, None, None, 1, 10), (cadena, 2026, 5, None, X6C, None, "2026-05-03", 0, 10)],
                columns=["Canal", "Año", "Mes", "Tienda", "Modelo", rd.DESCRIPCION, "Dia", "_detalle", "Unidades"],
            )

        r = rd.generar(ejecutar, ["COPPEL", "SEARS"], ["canal", "tienda"], (2026, 5), (2026, 5),
                       precio=True, buscar=lambda x: X6C)
        assert sorted(corridas) == ["COPPEL", "SEARS", "precios", "scrap"]
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
            if de_que in ("COPPEL", "precios", "scrap"):
                return None
            return _venta(("SEARS", 2026, 5, "T1", "HONOR X6C 256GB 4G", None, 10))

        r = rd.generar(ejecutar, ["COPPEL", "SEARS"], ["tienda", "modelo"], (2026, 5), (2026, 5), precio=False)
        assert [p.cadena for p in r.partes] == ["SEARS"]
        r = rd.generar(ejecutar, ["COPPEL", "SEARS"], ["tienda", "modelo"], (2026, 5), (2026, 5), precio=True)
        assert [p.cadena for p in r.partes] == ["SEARS"]


class TestQuePrecio:
    """Pedido el 19/10/2026: si el precio cambio en el mes, preguntar cual
    quiere, y poder ver todos los precios con lo vendido a cada uno."""

    def _coppel_septiembre(self):
        # $5,000 del 1 al 14 y $4,000 del 15 en adelante.
        return _precios(*[("Coppel", "COP X6C", f"2026-09-{d:02d}", 5000.0 if d < 15 else 4000.0)
                          for d in range(1, 31)])

    def test_se_entiende_lo_que_elige(self):
        assert rd.modo_de_precio("el último del mes") == "ultimo"
        assert rd.modo_de_precio("Todos los precios") == "desglosado"
        assert rd.modo_de_precio("el mas bajo") == "minimo"
        assert rd.modo_de_precio("") == ""
        with pytest.raises(ValueError):
            rd.modo_de_precio("el de mi primo")

    def test_cuanto_varia_por_cadena(self):
        precios = self._coppel_septiembre()
        fijo = _precios(*[("Sears", "SRS X6C", "2026-09-30", 6000.0)])
        varia = rd.cuanto_varia(pd.concat([precios, fijo]), ["COPPEL", "SEARS"], (2026, 9), (2026, 9))
        assert varia == {"COPPEL": 1.0}
        # Fuera del periodo pedido no cuenta.
        assert rd.cuanto_varia(precios, ["COPPEL"], (2026, 10), (2026, 10)) == {}

    def test_la_pregunta_trae_las_seis_opciones_y_donde_cambio(self):
        texto = rd.pregunta_de_precio({"COPPEL": 0.44, "TELCEL": 0.22})
        assert "TODAVIA NO ARMES" in texto and "Coppel (44% de sus modelos)" in texto
        assert all(f"{n}." in texto for n in range(1, 7)) and "desglosado" in texto

    def test_primero_ultimo_minimo_maximo(self):
        precios = self._coppel_septiembre()
        venta = _venta(("Coppel", 2026, 9, "T1", X6C, None, 40))
        for modo, esperado in (("primero", 5000.0), ("ultimo", 4000.0), ("minimo", 4000.0), ("maximo", 5000.0)):
            tabla = rd.armar_parte("COPPEL", venta, ["tienda", "modelo"], precios, modo).tabla
            assert tabla[rd.MODOS_DE_PRECIO[modo]].tolist() == [esperado], modo

    def test_desglosado_una_fila_por_precio_con_sus_unidades(self):
        precios = self._coppel_septiembre()
        # T1 vendio 10 antes del cambio y 30 despues; T2 solo despues.
        ventas = pd.DataFrame(
            [("Coppel", 2026, 9, "T1", X6C, None, "2026-09-03", 4),
             ("Coppel", 2026, 9, "T1", X6C, None, "2026-09-10", 6),
             ("Coppel", 2026, 9, "T1", X6C, None, "2026-09-20", 30),
             ("Coppel", 2026, 9, "T2", X6C, None, "2026-09-21", 2)],
            columns=["Canal", "Año", "Mes", "Tienda", "Modelo", rd.DESCRIPCION, "Dia", "Unidades"],
        )
        parte = rd.armar_parte("COPPEL", ventas, ["tienda", "modelo"], precios, "desglosado")
        filas = parte.tabla[["Tienda", "Unidades", "Precio"]].values.tolist()
        # Juntos y de menor a mayor precio.
        assert filas == [["T1", 30.0, 4000.0], ["T1", 10.0, 5000.0], ["T2", 2.0, 4000.0]]
        assert parte.unidades == 42 and parte.con_precio == 1.0

    def test_el_desglose_lee_cada_fila_por_dia_y_los_otros_no(self):
        consultas = {}

        def ejecutar(sql, de_que):
            consultas[de_que] = sql
            return None

        for modo in ("desglosado", "ultimo", "ponderado"):
            rd.generar(ejecutar, ["SEARS"], ["tienda", "modelo"], (2026, 9), (2026, 9), precio=True, modo=modo)
            sql = consultas["SEARS"]
            if modo == "desglosado":
                assert "[Date] AS [Dia]" in sql and "GROUPING SETS" not in sql
            elif modo == "ultimo":
                assert "AS [Dia]" not in sql
            else:
                assert "GROUPING SETS" in sql


class TestCuantoTarda:
    """Con lo que tardo de verdad el 20/10/2026 en el bot de prueba."""

    def test_un_mes_de_una_cadena_sin_tiendas_va_en_la_conversacion(self):
        # Elektra, septiembre, por modelo, ultimo precio: tardo 5 s.
        assert rd.estimar(["ELEKTRA"], "ultimo", meses=1, con_tienda=False) <= 60

    def test_telcel_tarda_aunque_sea_un_mes(self):
        # Todas las cadenas, septiembre, por modelo: Telcel solo tardo 167 s.
        segundos = rd.estimar(list(rd.acceso.CADENAS), False, meses=1, con_tienda=False)
        assert 150 <= segundos <= 220

    def test_seis_meses_por_tienda_con_desglose(self):
        # Coppel y Sears, mayo a octubre, por tienda, todos los precios: 3.5 min.
        assert 180 <= rd.estimar(["COPPEL", "SEARS"], "desglosado", meses=6) <= 300

    def test_cada_modo_de_precio_cuesta_distinto(self):
        cadenas = list(rd.acceso.CADENAS)
        assert rd.estimar(cadenas, "ultimo") < rd.estimar(cadenas, "ponderado")
        assert rd.estimar(cadenas, True) == rd.estimar(cadenas, "ponderado")

    def test_aprende_de_lo_que_tardo(self):
        historial = {}
        prediccion = rd.predecir("TELCEL", False, 1, False)
        rd.aprender(historial, "TELCEL", False, prediccion * 2, meses=1, con_tienda=False)
        assert historial == {"TELCEL|sin precio|total": 2.0}
        assert rd.estimar(["TELCEL"], False, historial, meses=1, con_tienda=False) == int(prediccion * 2 + 10)

    def test_meses_entre(self):
        assert rd.meses_entre((2026, 5), (2026, 10)) == 6 and rd.meses_entre((2025, 12), (2026, 1)) == 2


def test_el_mensaje_de_entrega_se_lee_bien():
    r = rd.Reporte(partes=[rd.Parte("COPPEL", pd.DataFrame(), 100.0, 0.0, 0.5)], desde=(2026, 9), hasta=(2026, 9),
                   columnas=["tienda", "modelo"], precio=True, modo="ultimo")
    texto = rd.para_el_usuario(r, 1, ["MOVISTAR"], [], "es")
    assert texto.startswith("Listo, aquí está el reporte que pediste: venta de septiembre de 2026, "
                            "por tienda y modelo, con el último precio de cada mes.")
    assert "- Coppel: 100 unidades (sin precio en el 50% de las unidades)" in texto
    assert "Todavía sin datos de ese periodo: Movistar." in texto
    assert "by store and model" in rd.para_el_usuario(r, 1, [], [], "en")


class TestPreciosQueUnicosNoTieneEnLaCadena:
    """21/10/2026: en AT&T solo 44% de las unidades tenia precio. Prices
    escribe 'AT&T CACS HONOR X6C 256GB 4G' y UNICOS$ no lo tiene dado de alta
    en AT&T, pero el modelo homologado si existe."""

    MODELOS = ["HONOR X6C 256GB 4G", "APPLE IPHONE 16 128GB 5G", "SAMSUNG A57 256GB 5G",
               "MOTOROLA G17 256GB 4G", "HONOR MAGIC 8 LITE 512GB 5G", "HONOR X8B 256GB 4.5G",
               "SAMSUNG A17 128GB 4G", "SAMSUNG A17 128GB 5G"]

    def _buscar(self, producto, retail="AT&T CACS"):
        return rd.modelo_del_precio(producto, lambda _t: None, retail, rd.IndiceDeModelos.de(self.MODELOS))

    def test_con_la_cadena_adelante_y_las_marcas_como_en_unicos(self):
        assert self._buscar("AT&T Dealers HONOR X6C 256GB 4G", "AT&T Dealers") == "HONOR X6C 256GB 4G"
        assert self._buscar("AT&T CACS IPHONE 16 128GB 5G") == "APPLE IPHONE 16 128GB 5G"
        assert self._buscar("AT&T CACS SAMSUNG GALAXY A57 256GB 5G") == "SAMSUNG A57 256GB 5G"
        assert self._buscar("AT&T CACS MOTOROLA MOTO G17 256GB 4G") == "MOTOROLA G17 256GB 4G"

    def test_sin_espacios_y_sin_la_red_si_no_hay_duda(self):
        assert self._buscar("AT&T CACS HONOR MAGIC8 LITE 512GB 5G") == "HONOR MAGIC 8 LITE 512GB 5G"
        assert self._buscar("AT&T CACS HONOR X8B 256GB 4G") == "HONOR X8B 256GB 4.5G"

    def test_si_hay_duda_no_se_adivina(self):
        # El A17 existe en 4G y en 5G: un precio "A17 4G" de otra red no se asigna...
        assert self._buscar("AT&T CACS SAMSUNG GALAXY A17 128GB 3G") is None
        # ...pero el que dice su red exacta si.
        assert self._buscar("AT&T CACS SAMSUNG GALAXY A17 128GB 5G") == "SAMSUNG A17 128GB 5G"
        assert self._buscar("AT&T CACS HONOR 8A 128GB 4G") is None

    def test_el_catalogo_va_primero(self):
        assert rd.modelo_del_precio("COP X6C", {"COP X6C": "DEL CATALOGO"}.get, "Coppel",
                                    rd.IndiceDeModelos.de(self.MODELOS)) == "DEL CATALOGO"


class TestRespaldoDeInternet:
    """Pedido el 21/10/2026: si Prices no tiene el precio, el de la pagina de
    la tienda (el scrap), marcado en 'Fuente del precio'."""

    def _scrap(self, *filas):
        crudos = pd.DataFrame(filas, columns=["Retail", "Producto", "Dia", "Precio"])
        return rd.scrap_por_modelo(crudos, [X6C, "APPLE IPAD 128GB", "HONOR MAGIC 8 LITE 512GB 5G"])

    def test_el_scrap_trae_su_modelo_homologado_y_su_cadena(self):
        s = self._scrap(("Liverpool", "APPLE IPAD 128GB", "2026-09-03", 7999.0),
                        ("Telcel CACS", "HONOR MAGIC8 LITE 512GB 5G", "2026-09-03", 9999.0),
                        ("Coppel", "NO EXISTE EN UNICOS", "2026-09-03", 1.0))
        assert sorted(zip(s["cadena"], s["Modelo"])) == [
            ("LIVERPOOL", "APPLE IPAD 128GB"), ("TELCEL", "HONOR MAGIC 8 LITE 512GB 5G")]

    def test_prices_primero_y_si_no_internet_dicho(self):
        # Dos dias de precio (con uno solo la serie pareceria mensual, como Sears).
        precios = _precios(("Coppel", "COP X6C", "2026-09-01", 5000.0), ("Coppel", "COP X6C", "2026-09-02", 5000.0))
        scrap = self._scrap(("Coppel", X6C, "2026-09-20", 4500.0),
                            ("Liverpool", "APPLE IPAD 128GB", "2026-09-10", 7999.0))
        dias = pd.DataFrame(
            [("Coppel", 2026, 9, X6C, "2026-09-03", 10), ("Coppel", 2026, 9, X6C, "2026-09-21", 30),
             ("Liverpool Offline", 2026, 9, "APPLE IPAD 128GB", "2026-09-12", 4)],
            columns=["Canal", "Año", "Mes", "Modelo", "Dia", "Unidades"])
        precio = rd.precio_por_fecha(dias, precios, LLAVES, scrap).set_index("Canal")
        # Coppel: el 3 de Prices (5000) y el 21 sin Prices -> internet (4500).
        assert precio.loc["Coppel", rd.PRECIO] == 4625.0 and precio.loc["Coppel", rd.FUENTE] == rd.DE_LOS_DOS
        assert precio.loc["Liverpool Offline", rd.PRECIO] == 7999.0
        assert precio.loc["Liverpool Offline", rd.FUENTE] == rd.DE_INTERNET

    def test_en_el_reporte_va_la_columna_y_cuanto_salio_de_internet(self):
        precios = _precios(("Coppel", "COP X6C", "2026-09-01", 5000.0))
        scrap = self._scrap(("Liverpool", "APPLE IPAD 128GB", "2026-09-01", 7999.0))
        ventas = pd.DataFrame(
            [("Liverpool Offline", 2026, 9, "APPLE IPAD 128GB", None, None, 1, 4),
             ("Liverpool Offline", 2026, 9, "APPLE IPAD 128GB", None, "2026-09-02", 0, 4)],
            columns=["Canal", "Año", "Mes", "Modelo", rd.DESCRIPCION, "Dia", "_detalle", "Unidades"])
        parte = rd.armar_parte("LIVERPOOL", ventas, ["modelo"], precios, "ponderado", scrap)
        assert parte.tabla[rd.FUENTE].tolist() == [rd.DE_INTERNET] and parte.de_internet == 1.0
        r = rd.Reporte(partes=[parte], desde=(2026, 9), hasta=(2026, 9), columnas=["modelo"], precio=True)
        assert "100% con el precio de internet" in rd.como_texto(r, [], 1)
        assert "página de la tienda" in rd.para_el_usuario(r, 1, [], [], "es")

    def test_sin_nada_de_internet_no_va_la_columna(self):
        precios = _precios(("Coppel", "COP X6C", "2026-09-01", 5000.0))
        ventas = _venta(("Coppel", 2026, 9, "T1", X6C, None, 4))
        parte = rd.armar_parte("COPPEL", ventas, ["tienda", "modelo"], precios, "ultimo", self._scrap())
        assert rd.FUENTE not in parte.tabla.columns and parte.de_internet == 0.0

    def test_el_ultimo_del_mes_tambien_usa_internet_si_falta(self):
        precios = _precios(("Coppel", "COP X6C", "2026-09-01", 5000.0))
        scrap = self._scrap(("Liverpool", "APPLE IPAD 128GB", "2026-09-01", 8999.0),
                            ("Liverpool", "APPLE IPAD 128GB", "2026-09-28", 7999.0))
        ventas = _venta(("Liverpool Offline", 2026, 9, "T1", "APPLE IPAD 128GB", None, 2))
        tabla = rd.armar_parte("LIVERPOOL", ventas, ["tienda", "modelo"], precios, "ultimo", scrap).tabla
        assert tabla[rd.MODOS_DE_PRECIO["ultimo"]].tolist() == [7999.0]
        assert tabla[rd.FUENTE].tolist() == [rd.DE_INTERNET]

    def test_la_consulta_del_scrap(self):
        sql = rd.sql_de_scrap((2026, 5), (2026, 10))
        assert "[Scrap].dbo.[Complete_Scrap]" in sql and "MIN([price])" in sql and "[price] > 0" in sql
        assert "'Mercado Libre', 'Suburbia'" in sql


class TestCanales:
    """22/10/2026: se pidio Liverpool Offline y el reporte trajo tambien Online."""

    def test_solo_los_canales_pedidos_y_de_su_cadena(self):
        assert rd.canales_pedidos("Liverpool Offline", ["LIVERPOOL"]) == ("Liverpool Offline",)
        assert rd.canales_pedidos("", ["LIVERPOOL"]) == ()
        with pytest.raises(ValueError, match="no es de"):
            rd.canales_pedidos("TELCEL CAC", ["LIVERPOOL"])

    def test_el_filtro_va_en_la_consulta_de_su_cadena(self):
        canales = ("Liverpool Offline", "TELCEL CAC")
        liverpool = rd.sql_de_ventas("LIVERPOOL", ["region"], (2026, 9), (2026, 9), canales=canales)
        assert "RTRIM([Retail]) IN ('Liverpool Offline')" in liverpool
        assert "IN ('TELCEL CAC')" in rd.sql_de_ventas("TELCEL", ["region"], (2026, 9), (2026, 9), canales=canales)
        assert "RTRIM([Retail]) IN" not in rd.sql_de_ventas("COPPEL", ["region"], (2026, 9), (2026, 9), canales=canales)
