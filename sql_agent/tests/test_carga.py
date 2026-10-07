"""Lo que salio de la prueba de 10 personas a la vez (7/10/2026).

Ocho de diez contestaron bien; las dos que no, no fueron por la carga:
- la de precios espero 20 minutos a la tabla del scraping, que estaba
  bloqueada, porque esa herramienta no tenia tope de tiempo;
- la de "telefonos Honor" unio con UNICOS$ por LIKE y conto de mas.
"""

import inspect

from sqlalchemy import create_engine

import agent
import inventario
from agent import _homologacion_por_like, consultar_sql, contexto_peticion


class TestLaUnionPorLike:
    MALA = (
        "SELECT SUM(v.[QTY]) FROM [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B] v "
        "INNER JOIN [GTM BI Database].dbo.[UNICOS$] u ON u.[BRAND] = 'HONOR' "
        "AND v.[Material_Description] LIKE '%' + u.[HOMOLOGACION MODELO] + '%' "
        "WHERE v.[Brand] = 'HONOR' AND v.[Year] = 2026 AND v.[Month] = 8"
    )

    def test_se_bloquea_antes_de_llegar_a_la_base(self):
        with contexto_peticion(create_engine("sqlite://")):  # sin tablas
            salida = consultar_sql.invoke({"query": self.MALA})
        assert salida.startswith("No la ejecute") and "IGUALDAD" in salida

    def test_por_igualdad_y_like_en_el_filtro_si_pasa(self):
        buena = (
            "SELECT u.[HOMOLOGACION MODELO], SUM(v.[QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_COPPEL] v "
            "JOIN [GTM BI Database].dbo.[UNICOS$] u ON u.[MATERIAL DESCRIPTION FINAL] = v.[Material_Description] "
            "WHERE v.[Material_Description] LIKE '%X6C%' GROUP BY u.[HOMOLOGACION MODELO]"
        )
        assert _homologacion_por_like(buena) == ""

    def test_sin_unicos_no_aplica(self):
        assert _homologacion_por_like("SELECT * FROM t WHERE a LIKE '%x%'") == ""


class TestNingunaHerramientaSinTope:
    def test_precios_lee_sin_esperar_al_scraping(self):
        fuente = inspect.getsource(agent.precio_en_internet.func)
        assert fuente.count("WITH (NOLOCK)") == 2
        assert "_presupuesto_de_tiempo" in fuente

    def test_las_herramientas_tienen_tope(self):
        for herramienta in (agent.buscar_modelos, agent.pronosticar_ventas):
            assert "_presupuesto_de_tiempo" in inspect.getsource(herramienta.func)
        assert "plazo=" in inspect.getsource(agent.semanas_de_inventario.func)

    def test_el_tope_no_pasa_de_lo_que_le_queda_a_la_peticion(self):
        with contexto_peticion(create_engine("sqlite://")) as ctx:
            assert agent._plazo_de_herramienta(ctx) == agent.TIMEOUT_HERRAMIENTA
            ctx.inicio -= agent.TIMEOUT_PETICION - agent.MARGEN_DE_ENTREGA - 10
            assert 1 <= agent._plazo_de_herramienta(ctx) <= 10

    def test_inventario_acepta_plazo_y_lo_restaura(self):
        class Bruta:
            timeout = 0

        class Conexion:
            def __init__(self):
                self.connection = type("P", (), {"dbapi_connection": Bruta()})()

        conexion = Conexion()
        with inventario._con_plazo(conexion, 30):
            assert conexion.connection.dbapi_connection.timeout == 30
        assert conexion.connection.dbapi_connection.timeout == 0


class TestElMensajeCitaElArchivo:
    """Visto el 7/10/2026: el mismo Excel de AT&T se anuncio con 103 852
    registros una vez y con 51 869 otra (las filas de la consulta del
    modelo); el archivo traia 118 758 las dos veces."""

    def _contexto(self):
        import pandas as pd
        crudo = pd.DataFrame({"Tienda": ["a", "b", "c"], "Cantidad": [2.0, 3.0, 5.0]})
        crudo.attrs["pivot"] = {"unidades": "Cantidad"}
        datos = pd.DataFrame({"Tienda": ["a", "b"], "Unidades": [5.0, 5.0]})
        return {"Datos": datos, "Raw_Data": crudo}

    def test_ficha_del_raw_data(self):
        from contexto import ficha_del_archivo
        assert ficha_del_archivo(self._contexto()) == {"filas": 3, "unidades": 10.0, "donde": "Raw_Data"}

    def test_ficha_del_zip(self):
        import pandas as pd
        from contexto import ficha_del_archivo
        datos = pd.DataFrame({"Unidades": [4.0, 6.0]})
        datos.attrs.update({"zip": "x.zip", "filas_zip": 1_500_000, "agrupado": True})
        assert ficha_del_archivo({"Datos": datos}) == {"filas": 1_500_000, "unidades": 10.0, "donde": "ZIP"}

    def test_sin_reporte_de_cadena_no_hay_ficha(self):
        from contexto import ficha_del_archivo
        assert ficha_del_archivo({}) is None

    def test_el_texto_que_se_reemplaza_sigue_existiendo(self):
        # agent.consultar_sql cambia este encabezado de formatting.py por el
        # del archivo: si alguien lo reescribe alla, el reemplazo dejaria de
        # aplicar sin dar error.
        import pandas as pd
        from formatting import MAX_ROWS_AS_TEXT, formatear_resultado
        df = pd.DataFrame({"a": range(MAX_ROWS_AS_TEXT + 5)})
        assert f"La consulta devolvio {len(df):,} registros; van completos en el Excel que se le adjunta al usuario. " in formatear_resultado(df).texto


class TestElInventarioDeTelcelVaALaHerramienta:
    """Visto el 7/10/2026: en ingles se contesto el inventario con un SUM a
    mano (solo el stock, sin para cuanto alcanza)."""

    Q = "SELECT SUM([STOCK]) FROM [RAW_INV_B2B].dbo.[TELCEL_INV_B2B] WHERE [BRAND] = 'HONOR'"

    def test_en_cualquier_idioma(self):
        from agent import _inventario_telcel_a_mano as f
        for pregunta in ("What's the Honor inventory at Telcel?", "Telcel的荣耀库存情况怎么样？",
                         "cuanto inventario hay en Telcel por modelo?"):
            assert "semanas_de_inventario" in f(self.Q, pregunta), pregunta

    def test_por_tienda_o_region_si_va_a_mano(self):
        from agent import _inventario_telcel_a_mano as f
        assert f(self.Q, "inventario de Honor en Telcel por tienda") == ""
        assert f(self.Q, "Telcel inventory by region") == ""

    def test_otras_tablas_no(self):
        from agent import _inventario_telcel_a_mano as f
        assert f("SELECT 1 FROM [RAW_DATA_B2B].dbo.[vw_ETL_TELCEL_B2B]", "ventas de Telcel") == ""


class TestElReporteDeLiverpoolRegion4:
    """Visto el 7/10/2026: "la capa mensual de las tiendas de Liverpool
    region 4 de 2025" en Excel. Le faltaban dos tiendas, traia hojas que no
    se pidieron, y el usuario le agrego a mano un pivot de tiendas por mes."""

    def test_unir_tiendas_tal_cual_se_bloquea(self):
        from agent import _tiendas_sin_normalizar as f
        assert f("SELECT 1 FROM v JOIN CustomerStores cs ON cs.[Coppel Name] = v.[Store]")
        assert f("SELECT 1 FROM v JOIN CustomerStores cs ON v.STORE = cs.[Coppel Name]")
        normalizado = (
            "SELECT 1 FROM v JOIN CustomerStores cs ON "
            "REPLACE(REPLACE(LTRIM(RTRIM(cs.[Coppel Name])), '  ', ' '), '  ', ' ') = "
            "REPLACE(REPLACE(LTRIM(RTRIM(v.[Store])), '  ', ' '), '  ', ' ')"
        )
        assert f(normalizado) == ""

    def test_coppel_name_no_es_coppel(self):
        import pandas as pd
        from contexto import retailer_del_resultado
        q = "SELECT * FROM vw_ETL_Liverpool v JOIN CustomerStores cs ON cs.[Coppel Name] = v.[Store]"
        assert retailer_del_resultado(pd.DataFrame({"x": [1]}), q) == "LIVERPOOL"

    def test_acotado_a_tiendas_no_lleva_hojas_extra(self):
        import pandas as pd
        from contexto import contexto_del_resultado
        df = pd.DataFrame({"Tienda": ["L MTY CENTRO"], "Año": [2025], "Mes": [1], "Unidades": [5.0]})
        q = "SELECT ... FROM CustomerStores cs WHERE cs.[RetailName] = 'LIVERPOOL' AND cs.[Region] = 'R4'"
        # Sin base: si intentara el reporte de cadena o las hojas extra, fallaria.
        assert contexto_del_resultado(df, None, q) == {}

    def test_pivot_de_tiendas_por_mes(self):
        import pandas as pd
        import excel_pivot
        df = pd.DataFrame({
            "Tienda": ["a"], "Año": [2025], "Mes": [1], "Unidades Liverpool": [1.0],
            "Unidades IOT": [0.0], "Unidades totales": [1.0], "CAPA mensual 2025": [5.0],
        })
        config = excel_pivot.config_por_tienda_y_mes(df)
        assert config["filas"] == ["Tienda"] and config["columnas"] == ["Mes"]
        assert config["medida"] == "Unidades totales"  # ni la capa ni una parcial

    def test_sin_tienda_o_mes_no_hay_pivot(self):
        import pandas as pd
        import excel_pivot
        assert excel_pivot.config_por_tienda_y_mes(pd.DataFrame({"Mes": [1], "Unidades": [2.0]})) is None
        assert excel_pivot.config_por_tienda_y_mes(pd.DataFrame({"Tienda": ["a"], "Unidades": [2.0]})) is None
