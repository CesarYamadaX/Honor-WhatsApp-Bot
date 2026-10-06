"""Que cada usuario vea solo sus cadenas (pedido el 6/10/2026).

Lo que importa probar es que el limite lo pone el codigo y no el modelo: una
consulta a otra cadena no llega al servidor, y una de varias cadenas vuelve
solo con las permitidas, la escriba como la escriba.
"""

import sqlite3

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import acceso
import resumen_local
from agent import (
    SQLAgent,
    consultar_sql,
    contexto_peticion,
    precio_en_internet,
    semanas_de_inventario,
)
from api import crear_app
from tests.test_api import AgenteFalso, _config
from usuarios import ErrorDeUsuarios, RegistroUsuarios

COPPEL = ["COPPEL"]
VENTAS = "[RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B]"


class TestQueTablasLee:
    def _objetos(self, query):
        return [r.objeto for r in acceso.referencias(query)]

    def test_las_de_from_y_join(self):
        q = (
            "SELECT * FROM [RAW_DATA_B2B].dbo.[vw_ETL_COPPEL] v "
            "LEFT JOIN [GTM BI Database].dbo.[UNICOS$] u ON u.[X] = v.[Y]"
        )
        assert self._objetos(q) == ["vw_ETL_COPPEL", "UNICOS$"]

    def test_las_separadas_por_coma(self):
        q = "SELECT * FROM vw_ETL_COPPEL a, vw_ETL_Sears b WHERE a.x = b.x"
        assert self._objetos(q) == ["vw_ETL_COPPEL", "vw_ETL_Sears"]

    def test_las_de_subconsultas_y_ctes(self):
        q = (
            "WITH x AS (SELECT * FROM vw_ETL_Elektra) "
            "SELECT * FROM x WHERE x.a IN (SELECT a FROM vw_ETL_Sears)"
        )
        # `x` es la CTE: lo que lee ya se mira dentro.
        assert self._objetos(q) == ["vw_ETL_Elektra", "vw_ETL_Sears"]

    def test_no_cuenta_lo_que_va_en_literales_ni_comentarios(self):
        q = (
            "SELECT 'FROM vw_ETL_Sears' AS a FROM vw_ETL_COPPEL -- JOIN vw_ETL_Sears\n"
            "/* FROM vw_ETL_TELCEL_B2B */"
        )
        assert self._objetos(q) == ["vw_ETL_COPPEL"]

    def test_columnas_con_alias_no_son_tablas(self):
        q = "SELECT v.[Retail], SUM(v.[QTY]) FROM vw_ETL_COPPEL v GROUP BY v.[Retail], v.Store"
        assert self._objetos(q) == ["vw_ETL_COPPEL"]


class TestAcotar:
    def test_sin_cadenas_no_toca_nada(self):
        q = "SELECT * FROM vw_ETL_TELCEL_B2B"
        assert acceso.acotar(q, None).query == q
        assert acceso.acotar(q, []).query == q

    def test_la_de_su_cadena_pasa_igual(self):
        q = "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_COPPEL]"
        acotada = acceso.acotar(q, COPPEL)
        assert not acotada.bloqueada and acotada.query == q

    def test_la_de_otra_cadena_no_se_ejecuta(self):
        acotada = acceso.acotar("SELECT * FROM [RAW_DATA_B2B].dbo.[EKT_B2B]", COPPEL)
        assert acotada.bloqueada and acotada.fuera == ["ELEKTRA"]
        assert "Coppel" in acceso.motivo(acotada, COPPEL)

    def test_la_consolidada_es_la_union_de_sus_vistas(self):
        acotada = acceso.acotar(
            f"SELECT SUM([QTY]) FROM {VENTAS} WHERE [Brand] = 'HONOR'", ["COPPEL", "SEARS"]
        )
        assert not acotada.bloqueada
        vistas = [r.objeto for r in acceso.referencias(acotada.query)]
        assert vistas == ["vw_ETL_COPPEL", "vw_ETL_COPPEL_IOT", "vw_ETL_Sears"]
        assert "ETL_Consolidado_B2B" in acotada.query  # como alias, para sus columnas

    def test_respeta_el_alias_de_la_consulta(self):
        acotada = acceso.acotar(
            "SELECT p.[Retail] FROM [GTM BI Database].dbo.[Prices] p WHERE p.[Carrier] = 'X'",
            COPPEL,
        )
        assert ") p WHERE p.[Carrier]" in acotada.query
        assert "LIKE 'COPPEL%'" in acotada.query

    def test_sin_alias_le_pone_el_nombre_de_la_tabla(self):
        acotada = acceso.acotar(
            "SELECT [Prices].[Retail] FROM [GTM BI Database].dbo.[Prices] WHERE 1 = 1", COPPEL
        )
        assert ") AS [Prices] WHERE 1 = 1" in acotada.query

    def test_la_promotoria_se_acota_por_la_cadena_de_la_tienda(self):
        acotada = acceso.acotar(
            "SELECT COUNT(*) FROM [GTM BI Database].dbo.[HCReport] h WHERE h.[Week] = 'W35'",
            COPPEL,
        )
        assert not acotada.bloqueada and acotada.acotados == ["HCReport"]
        assert "CustomerStores" in acotada.query and "_h.[NAME]" in acotada.query

    @pytest.mark.parametrize("query", [
        "SELECT * FROM sys.tables",
        "SELECT * FROM [OtraBase].dbo.[Ventas]",
        "SELECT * FROM TablaQueNadieConoce",
        "SELECT [GTM BI Database].dbo.fn_ventas(1)",
        "SELECT * FROM [GTM BI Database].dbo.[V_HCReport_Informes]",
    ])
    def test_lo_que_no_se_sabe_de_quien_es_no_se_ejecuta(self, query):
        acotada = acceso.acotar(query, COPPEL)
        assert acotada.bloqueada and acotada.desconocidos

    def test_las_neutras_pasan(self):
        q = "SELECT [HOMOLOGACION MODELO] FROM [GTM BI Database].dbo.[UNICOS$]"
        assert acceso.acotar(q, COPPEL).query == q

    def test_el_filtro_deja_solo_sus_valores(self):
        valores = [
            "Coppel", "TELCEL CAC", "TELCEL WEB", "AT&T Cacs", "AT&T Dealers",
            " Liverpool Online", "Unknown", "Mercado Libre", None,
        ]
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE t ([Retail] TEXT)")
        con.executemany("INSERT INTO t VALUES (?)", [(v,) for v in valores])
        cond = acceso.condicion("[Retail]", ["TELCEL", "AT&T", "LIVERPOOL"])
        quedan = sorted(r[0] for r in con.execute(f"SELECT [Retail] FROM t WHERE {cond}"))
        assert quedan == [" Liverpool Online", "AT&T Cacs", "AT&T Dealers", "TELCEL CAC", "TELCEL WEB"]

    def test_cadena_de(self):
        assert acceso.cadena_de("TELCEL DISTR. ASOC.") == "TELCEL"
        assert acceso.cadena_de("Movistar CACs") == "MOVISTAR"
        assert acceso.cadena_de("Unknown") is None


class TestResumenLocal:
    def test_la_consulta_acotada_se_sigue_contestando_en_el_resumen(self):
        q = f"SELECT [Retail], SUM([QTY]) FROM {VENTAS} GROUP BY [Retail]"
        local = acceso.acotar(q, COPPEL, local=True).query
        traducida = resumen_local.traducir(local)
        assert traducida is not None
        assert "vw_ETL_Sears" not in traducida and "[vw_ETL_COPPEL_IOT]" in traducida


class TestUsuarios:
    def test_por_defecto_ve_todas(self):
        registro = RegistroUsuarios()
        registro.agregar("5215512345678")
        assert registro.cadenas_de("5215512345678") is None

    def test_poner_y_guardar_cadenas(self, tmp_path):
        archivo = tmp_path / "usuarios.json"
        registro = RegistroUsuarios(archivo)
        registro.agregar("5215512345678")
        registro.poner_cadenas("5215512345678", ["elektra", "COPPEL", "COPPEL"])
        otra_vez = RegistroUsuarios(archivo)
        assert otra_vez.obtener("5215512345678").cadenas == ["COPPEL", "ELEKTRA"]
        assert otra_vez.cadenas_de("5215512345678") == {"COPPEL", "ELEKTRA"}

    def test_todas_marcadas_es_sin_limite(self):
        registro = RegistroUsuarios()
        registro.agregar("5215512345678")
        registro.poner_cadenas("5215512345678", acceso.CADENAS)
        assert registro.obtener("5215512345678").cadenas == []

    def test_una_cadena_que_no_existe_se_rechaza(self):
        registro = RegistroUsuarios()
        registro.agregar("5215512345678")
        with pytest.raises(ErrorDeUsuarios):
            registro.poner_cadenas("5215512345678", ["WALMART"])


class TestLaApi:
    def test_el_panel_las_pone_y_el_agente_las_recibe(self):
        recibidas = []

        class Agente(AgenteFalso):
            def responder(self, pregunta, cancelacion=None, cadenas=None):
                recibidas.append(cadenas)
                return "ok", []

        registro = RegistroUsuarios(semilla=["5215512345678"])
        app = crear_app(_config(), Agente, usuarios=registro)
        with TestClient(app) as cliente:
            r = cliente.post(
                "/panel/usuarios/cadenas",
                json={"numero": "5215512345678", "cadenas": ["COPPEL"]},
            )
            assert r.status_code == 200 and r.json()["usuario"]["cadenas"] == ["COPPEL"]
            lista = cliente.get("/panel/usuarios").json()
            assert {"clave": "AT&T", "nombre": "AT&T"} in lista["cadenas"]
            cliente.post("/ask", json={"chat_id": "5215512345678@s.whatsapp.net", "text": "hola"})
            assert cliente.post(
                "/panel/usuarios/cadenas", json={"numero": "5215512345678", "cadenas": ["NADA"]}
            ).status_code == 400
        assert recibidas == [{"COPPEL"}]


def _motor_de_precios():
    motor = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    pd.DataFrame({
        "Retail": ["Coppel", "TELCEL CAC", "Elektra"],
        "Precio": [100.0, 200.0, 300.0],
    }).to_sql("Prices", motor, index=False)
    return motor


class TestLasHerramientas:
    def test_otra_cadena_no_llega_a_la_base(self):
        motor = create_engine("sqlite://")  # sin tablas: si llegara, fallaria distinto
        with contexto_peticion(motor, cadenas=COPPEL):
            salida = consultar_sql.invoke({"query": "SELECT * FROM vw_ETL_TELCEL_B2B"})
        assert salida.startswith("No la ejecute") and "Telcel" in salida

    def test_una_tabla_de_varias_vuelve_solo_con_las_suyas(self):
        motor = _motor_de_precios()
        with contexto_peticion(motor, cadenas=COPPEL):
            salida = consultar_sql.invoke(
                {"query": "SELECT [Retail], SUM([Precio]) AS p FROM Prices GROUP BY [Retail]"}
            )
        assert "Coppel" in salida and "TELCEL" not in salida and "Elektra" not in salida
        assert "SOLO Coppel" in salida

    def test_sin_limite_vuelven_todas(self):
        motor = _motor_de_precios()
        with contexto_peticion(motor):
            salida = consultar_sql.invoke(
                {"query": "SELECT [Retail], SUM([Precio]) AS p FROM Prices GROUP BY [Retail]"}
            )
        assert "TELCEL" in salida and "Elektra" in salida

    def test_semanas_de_inventario_es_de_telcel(self):
        with contexto_peticion(create_engine("sqlite://"), cadenas=COPPEL):
            assert semanas_de_inventario.invoke({"modelo": "X6C"}).startswith("No la ejecute")

    def test_precio_en_internet_de_otra_cadena(self):
        with contexto_peticion(create_engine("sqlite://"), cadenas=COPPEL):
            salida = precio_en_internet.invoke({"modelo": "magic 8", "retailer": "Elektra"})
        assert salida.startswith("No la ejecute")


class TestElHistorial:
    def test_si_le_cambian_las_cadenas_se_olvida_lo_anterior(self):
        agente = SQLAgent.__new__(SQLAgent)
        agente.engine = create_engine("sqlite://")
        agente.historial = []
        agente.idioma = "es"
        vistos = []

        class _Ejecutor:
            def invoke(self, payload, config=None, **kwargs):
                vistos.append((len(payload["chat_history"]), payload["instruccion_idioma"]))
                return {"output": "listo"}

        agente.agent_executor = _Ejecutor()
        agente.responder("ventas de telcel")
        agente.responder("y de sears?")
        agente.responder("y de coppel?", cadenas=COPPEL)
        assert [n for n, _ in vistos] == [0, 2, 0]
        assert "ACCESO" not in vistos[0][1] and "SOLO puede ver datos de Coppel" in vistos[2][1]
