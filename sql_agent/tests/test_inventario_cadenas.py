"""El inventario de las cadenas (9/11/2026): una fuente por cadena, su
propio ultimo corte, tienda + transito sin bodega, y el modelo de UNICOS$."""

from datetime import date

import pandas as pd
import pytest

import inventario_cadenas as ic
from reporte_detallado import DESCRIPCION

MODELOS = {"HONOR X7D": "HONOR X7D 256GB 4G", "TELCEL HONOR X7D, NEGRO": "HONOR X7D 256GB 4G",
           "HONOR MAGIC 8 LITE ROJO": "HONOR MAGIC 8 LITE 512GB 5G"}


def homologar(texto, cadena):
    return MODELOS.get(texto)


def _crudo(*filas, tienda=False):
    columnas = ["Fecha"] + (["Tienda"] if tienda else []) + ["Marca", "texto", "Unidades"]
    return pd.DataFrame(filas, columns=columnas)


class TestLaCuenta:
    def test_cada_cadena_con_su_corte_y_sus_anteriores(self):
        r = ic.armar({
            "ELEKTRA": _crudo((date(2026, 10, 11), "HONOR", "TELCEL HONOR X7D, NEGRO", 10),
                              (date(2026, 10, 4), "HONOR", "TELCEL HONOR X7D, NEGRO", 7)),
            "LIVERPOOL": _crudo((date(2026, 10, 26), "HONOR", "HONOR X7D", 5)),
        }, homologar, ["modelo"])
        assert r.totales["ELEKTRA"] == [(date(2026, 10, 11), 10.0), (date(2026, 10, 4), 7.0)]
        assert r.totales["LIVERPOOL"] == [(date(2026, 10, 26), 5.0)]
        assert r.tabla["Modelo"].unique().tolist() == ["HONOR X7D 256GB 4G"]

    def test_lo_que_no_esta_en_unicos_va_vacio_con_su_texto(self):
        r = ic.armar({"COPPEL": _crudo((date(2026, 9, 27), "HONOR", "HONOR X9Z RARO", 3))}, homologar, ["modelo"])
        assert r.tabla[["Modelo", DESCRIPCION]].values.tolist() == [["", "HONOR X9Z RARO"]]
        assert r.sin_homologar == ["HONOR X9Z RARO"]

    def test_un_modelo_por_su_nombre_homologado(self):
        r = ic.armar({"ELEKTRA": _crudo((date(2026, 10, 11), "HONOR", "HONOR MAGIC 8 LITE ROJO", 4),
                                        (date(2026, 10, 11), "HONOR", "TELCEL HONOR X7D, NEGRO", 9))},
                     homologar, ["modelo"], modelo="Magic 8 Lite")
        assert r.tabla["Unidades"].tolist() == [4.0]

    def test_el_modelo_se_busca_entero(self):
        # Medido el 9/11/2026: "Magic 8 Lite" palabra por palabra se llevaba
        # el Magic 5 Lite 128GB (el 8 de "128").
        r = ic.armar({"COPPEL": _crudo((date(2026, 9, 27), "HONOR", "HONOR MAGIC 8 LITE ROJO", 4),
                                       (date(2026, 9, 27), "HONOR", "HONOR MAGIC 5 LITE 128GB", 2))},
                     lambda t, c: {"HONOR MAGIC 5 LITE 128GB": "HONOR MAGIC 5 LITE 128GB 5G"}.get(t) or homologar(t, c),
                     ["modelo"], modelo="Magic 8 Lite")
        assert r.tabla["Modelo"].tolist() == ["HONOR MAGIC 8 LITE 512GB 5G"]

    def test_sin_coincidencias_la_cadena_se_dice(self):
        r = ic.armar({"COPPEL": _crudo((date(2026, 9, 27), "HONOR", "HONOR X7D", 3)),
                      "LIVERPOOL": _crudo()}, homologar, [], modelo="Magic")
        assert r.sin_datos == ["COPPEL", "LIVERPOOL"] and r.tabla.empty

    def test_por_tienda_y_varias_marcas(self):
        r = ic.armar({"COPPEL": _crudo((date(2026, 9, 27), "1 • UGARTE", "HONOR", "HONOR X7D", 3),
                                       (date(2026, 9, 27), "1 • UGARTE", "SAMSUNG", "SAMSUNG A07", 8),
                                       tienda=True)}, homologar, ["tienda"], varias_marcas=True)
        assert r.tabla.columns.tolist() == ["Cadena", "Fecha", "Marca", "Tienda", "Unidades"]
        assert r.tabla["Marca"].tolist() == ["SAMSUNG", "HONOR"]


class TestLoQueSePide:
    def test_cadenas(self):
        assert ic.cadenas_pedidas("todas") == list(ic.CADENAS)
        assert ic.cadenas_pedidas("Elektra y liverpool") == ["ELEKTRA", "LIVERPOOL"]
        with pytest.raises(ValueError, match="Sears"):
            ic.cadenas_pedidas("sears")

    def test_marcas_y_desglose(self):
        assert ic.marcas_pedidas("Honor vs Samsung") == ["HONOR", "SAMSUNG"]
        assert ic.marcas_pedidas("todas") == []
        assert ic.dimensiones_pedidas("tienda y modelo") == ["tienda", "modelo"]
        with pytest.raises(ValueError):
            ic.dimensiones_pedidas("color")


class _Cursor:
    def __init__(self, filas, columnas):
        self.filas, self.columnas = filas, columnas

    def fetchall(self):
        return self.filas

    def keys(self):
        return self.columnas


class _Conexion:
    def __init__(self, consultas):
        self.consultas = consultas
        self.connection = object()

    def exec_driver_sql(self, sql, parametros=()):
        self.consultas.append((sql, parametros))
        if "DISTINCT TOP" in sql:
            return _Cursor([(date(2026, 10, 11),), (date(2026, 10, 4),)], ["Fecha"])
        return _Cursor([], ["Fecha", "Marca", "texto", "Unidades"])

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Motor:
    def __init__(self):
        self.consultas = []

    def connect(self):
        return _Conexion(self.consultas)


class TestLaConsulta:
    def test_elektra_tienda_mas_transito_sin_el_cd(self):
        motor = _Motor()
        ic.leer(motor, "ELEKTRA", ["HONOR"], False, 2)
        cortes, venta = motor.consultas
        assert "DISTINCT TOP 2" in cortes[0] and "ELEKTRA_INV_B2B" in cortes[0]
        assert "[INV_TOTAL_RUCE]" in venta[0] and "[ENV_CD_TRANS]" in venta[0] and "[INV_CD]" not in venta[0]
        assert list(venta[1]) == [date(2026, 10, 11), date(2026, 10, 4), "HONOR"]

    def test_coppel_sin_cedis_y_liverpool_de_la_consolidada(self):
        motor = _Motor()
        ic.leer(motor, "COPPEL", [], True, 1)
        ic.leer(motor, "LIVERPOOL", [], False, 1)
        coppel, liverpool = motor.consultas[1][0], motor.consultas[3][0]
        assert "[ExisTda]" in coppel and "[TransitoATda]" in coppel and "ExisBod" not in coppel
        assert "AS Tienda" in coppel
        # Su ultimo corte es el de Liverpool, no el de toda la tabla.
        assert "INV_Retail" in motor.consultas[2][0] and "Liverpool Offline" in motor.consultas[2][0]
        assert "INV_Retail" in liverpool and "LIVERPOOL_INV_B2B" not in liverpool
        # Sus centros de distribucion (CD Tultitlan, Centro Softline) son bodega.
        assert "[Store Name] LIKE 'L %'" in liverpool


def test_con_varias_marcas_el_texto_da_cada_una():
    r = ic.armar({"ELEKTRA": _crudo((date(2026, 10, 11), "HONOR", "HONOR X7D", 5),
                                    (date(2026, 10, 11), "SAMSUNG", "SAMSUNG A07", 20))},
                 homologar, [], varias_marcas=True)
    texto = ic.como_texto(r, "HONOR, SAMSUNG", hoy=date(2026, 10, 12))
    assert "Por marca en Elektra (ultimo corte): SAMSUNG: 20; HONOR: 5" in texto


def test_el_texto_dice_cada_corte_y_si_esta_viejo():
    r = ic.armar({"COPPEL": _crudo((date(2026, 9, 27), "HONOR", "HONOR X7D", 3)),
                  "ELEKTRA": _crudo((date(2026, 10, 11), "HONOR", "HONOR X7D", 5),
                                    (date(2026, 10, 4), "HONOR", "HONOR X7D", 4))}, homologar, ["modelo"])
    texto = ic.como_texto(r, "HONOR", hoy=date(2026, 11, 9))
    assert "Coppel: 3 equipos al 27/09/2026" in texto and "sin el CEDIS" in texto
    assert "ultimo corte de Coppel" in texto
    assert "Cortes anteriores: 04/10/2026: 4" in texto
    assert "HONOR X7D 256GB 4G: 5" in texto


class TestLaHerramienta:
    """La del agente: permisos, el Excel y las fuentes que dan otro numero."""

    def _resultado(self):
        return ic.armar({"COPPEL": _crudo((date(2026, 9, 27), "HONOR", "HONOR X7D", 3),
                                          (date(2026, 9, 27), "HONOR", "HONOR MAGIC 8 LITE ROJO", 2))},
                        homologar, ["modelo"])

    def test_solo_las_cadenas_que_puede_ver(self, monkeypatch):
        from sqlalchemy import create_engine

        import agent

        pedidas = []
        monkeypatch.setattr(agent.inventario_cadenas, "calcular",
                            lambda engine, cadenas, *a, **k: pedidas.append(list(cadenas)) or self._resultado())
        monkeypatch.setattr(agent.ExcelExporter, "export_to_excel", lambda tabla: "inventario.xlsx")
        with agent.contexto_peticion(create_engine("sqlite://"), cadenas=frozenset({"COPPEL"})) as ctx:
            texto = agent.inventario_por_cadena.invoke({"cadenas": "coppel, telcel", "por": "modelo"})
            assert pedidas == [["COPPEL"]] and "Coppel: 5 equipos al 27/09/2026" in texto
            assert ctx.archivos == ["inventario.xlsx"] and "TELCEL" in ctx.negadas

    def test_las_fuentes_que_dan_otro_numero_van_a_la_herramienta(self):
        import agent

        for tabla in ("[GTM_BI_INVENTORIES].dbo.[VW_INVENTORIES]", "[RAW_INV_B2B].dbo.[LIVERPOOL_INV_B2B]"):
            assert "inventario_por_cadena" in agent._inventario_de_fuente_mala(f"SELECT SUM(x) FROM {tabla}")
        assert agent._inventario_de_fuente_mala("SELECT SUM([Inventario Total]) FROM [GTM_BI_INVENTORIES].dbo.[INV_Retail]") == ""


class TestTiendasSinInventario:
    """Visto el 9/11/2026: "que tiendas de Coppel no tienen inventario de
    Honor" -- la herramienta solo veia las que si tenian, y el bot invento
    cuantas faltaban."""

    def _coppel(self):
        crudo = _crudo((date(2026, 9, 27), "1 • UGARTE", "HONOR", "HONOR X7D", 3), tienda=True)
        crudo.attrs["tiendas"] = pd.DataFrame({"Fecha": [date(2026, 9, 27)] * 3,
                                               "Tienda": ["1 • UGARTE", "2 • CENTRO", "3 • NORTE"]})
        return crudo

    def test_por_tienda_van_tambien_las_que_tienen_cero(self):
        r = ic.armar({"COPPEL": self._coppel()}, homologar, ["tienda"])
        assert dict(zip(r.tabla["Tienda"], r.tabla["Unidades"])) == {"1 • UGARTE": 3.0, "2 • CENTRO": 0.0, "3 • NORTE": 0.0}
        texto = ic.como_texto(r, "HONOR", hoy=date(2026, 9, 28))
        assert "Tiendas sin inventario de lo pedido en Coppel: 2 de 3: 2 • CENTRO, 3 • NORTE" in texto

    def test_por_tienda_y_modelo_no_inventa_ceros(self):
        r = ic.armar({"COPPEL": self._coppel()}, homologar, ["tienda", "modelo"])
        assert r.tabla["Tienda"].tolist() == ["1 • UGARTE"]


def test_la_misma_pregunta_dos_veces_no_manda_dos_archivos(monkeypatch):
    # Visto el 9/11/2026: el Excel de Liverpool por tienda y modelo llego doble.
    from sqlalchemy import create_engine

    import agent

    r = ic.armar({"COPPEL": _crudo((date(2026, 9, 27), "HONOR", "HONOR X7D", 3),
                                   (date(2026, 9, 27), "HONOR", "HONOR MAGIC 8 LITE ROJO", 2))}, homologar, ["modelo"])
    monkeypatch.setattr(agent.inventario_cadenas, "calcular", lambda *a, **k: r)
    archivos = iter(["uno.xlsx", "dos.xlsx"])
    monkeypatch.setattr(agent.ExcelExporter, "export_to_excel", lambda tabla: next(archivos))
    with agent.contexto_peticion(create_engine("sqlite://")) as ctx:
        agent.inventario_por_cadena.invoke({"cadenas": "coppel", "por": "modelo"})
        segunda = agent.inventario_por_cadena.invoke({"cadenas": "coppel", "por": "modelo"})
        assert ctx.archivos == ["uno.xlsx"] and "ya va adjunto" in segunda
