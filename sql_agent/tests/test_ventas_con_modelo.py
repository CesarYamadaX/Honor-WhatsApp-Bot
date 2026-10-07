"""Las respuestas salen solo de las vistas ETL (pedido el 7/10/2026).

Las tablas crudas quedan para el Raw_Data del Excel. Para no perder "por
modelo" ni "por region", vw_ETL_Con_Modelo es la venta oficial con esas
columnas ya puestas (medido en agosto de 2026: suma igual que las vistas en
las 16 etiquetas de [Retail], y casa el modelo y la region casi al 100%).
"""

from sqlalchemy import create_engine

import acceso
import ventas_con_modelo as m
from agent import _tabla_cruda_para_responder, consultar_sql, contexto_peticion


class TestLaVistaVirtual:
    def test_se_expande_a_las_nueve_vistas(self):
        q = m.expandir("SELECT [Modelo], SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Con_Modelo] GROUP BY [Modelo]")
        objetos = {r.objeto for r in acceso.referencias(q)}
        assert set(m.VISTAS) <= objetos and "vw_ETL_Con_Modelo" not in objetos
        assert q.count("UNION ALL") == len(m.VISTAS) - 1

    def test_respeta_el_alias_y_si_no_hay_le_pone_el_nombre(self):
        assert m.expandir("SELECT v.[Modelo] FROM vw_ETL_Con_Modelo v").endswith(") v")
        assert ") AS [vw_ETL_Con_Modelo] WHERE" in m.expandir("SELECT 1 FROM vw_ETL_Con_Modelo WHERE 1 = 1")

    def test_una_columna_con_su_nombre_no_se_toca(self):
        q = m.expandir("SELECT [vw_ETL_Con_Modelo].[QTY] FROM vw_ETL_Con_Modelo")
        assert q.startswith("SELECT [vw_ETL_Con_Modelo].[QTY] FROM (")

    def test_solo_las_cadenas_del_usuario(self):
        q = m.expandir("SELECT 1 FROM vw_ETL_Con_Modelo", ["COPPEL"])
        objetos = {r.objeto for r in acceso.referencias(q)}
        assert {"vw_ETL_COPPEL", "vw_ETL_COPPEL_IOT"} <= objetos
        assert not objetos & {"vw_ETL_TELCEL_B2B", "vw_ETL_ATT_Postpaid", "ATT_B2B"}
        assert not acceso.acotar(q, ["COPPEL"]).bloqueada

    def test_sin_la_vista_no_cambia_nada(self):
        q = "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Sears]"
        assert m.expandir(q) == q

    def test_espacios_de_mas_en_cualquier_cantidad(self):
        # Sears escribe '126      TLALNEPANTLA' (seis espacios). El truco de
        # _normal, paso por paso (la expresion es de SQL Server, no de SQLite).
        def normal(x):
            x = x.replace(chr(160), " ").replace(chr(9), " ").strip()
            return x.replace(" ", " " + chr(7)).replace(chr(7) + " ", "").replace(chr(7), "").upper()

        for crudo, esperado in [("126      TLALNEPANTLA", "126 TLALNEPANTLA"),
                                ("L  MONTERREY ", "L MONTERREY"), ("a" + chr(160) + "b", "A B")]:
            assert normal(crudo) == esperado
        expresion = m._normal("x")
        assert "CHAR(7)" in expresion and "CHAR(160)" in expresion


class TestLasCrudasNoSonParaResponder:
    def test_una_tabla_cruda_se_bloquea(self):
        for q in ("SELECT SUM([Unidades]) FROM [RAW_DATA_B2B].dbo.[COPPEL_B2B] WHERE [Unidades] >= 1",
                  "SELECT * FROM [RAW_DATA_B2B].dbo.[Liverpool_IOT]",
                  "SELECT TOP 5 * FROM [RAW_DATA_B2B].dbo.[MVS_B2B]"):
            assert "vw_ETL_Con_Modelo" in _tabla_cruda_para_responder(q), q

    def test_las_vistas_pasan(self):
        assert _tabla_cruda_para_responder("SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_COPPEL]") == ""
        assert _tabla_cruda_para_responder("SELECT 1 FROM vw_ETL_Con_Modelo") == ""

    def test_consultar_sql_no_la_ejecuta(self):
        with contexto_peticion(create_engine("sqlite://")):
            salida = consultar_sql.invoke({"query": "SELECT SUM(v.[Cantidad]) FROM [RAW_DATA_B2B].dbo.[TELCEL_B2B] v"})
        assert salida.startswith("No la ejecute") and "TELCEL_B2B" in salida

    def test_el_catalogo_la_muestra(self):
        import catalogo
        ficha = catalogo.describir_objeto.invoke({"nombre": "vw_ETL_Con_Modelo"})
        assert "[Modelo]" in ficha and "[Region]" in ficha and "NOTA:" in ficha
        assert "cruda: solo para el Excel" in catalogo.indice()


class TestLaCapaEsDeTodasLasMarcas:
    """"La capa mensual de las tiendas de Liverpool region 4" salio una vez
    con todas las marcas (2 149) y otra solo con Honor (245)."""

    Q = "SELECT MAX(u) FROM vw_ETL_Con_Modelo WHERE [Brand] = 'HONOR' AND [Region] = 'R4'"

    def test_sin_marca_en_la_pregunta_no_se_filtra_marca(self):
        from agent import _capa_de_una_marca as f
        assert f(self.Q, "Quiero la capa mensual de ventas de las tiendas de liverpool region 4 de 2025")

    def test_si_la_nombran_si(self):
        from agent import _capa_de_una_marca as f
        assert f(self.Q, "la capa de Honor en las tiendas de liverpool region 4") == ""

    def test_sin_filtro_de_marca_o_sin_capa_no_aplica(self):
        from agent import _capa_de_una_marca as f
        assert f("SELECT MAX(u) FROM vw_ETL_Con_Modelo WHERE [Region] = 'R4'", "la capa de liverpool") == ""
        assert f(self.Q, "ventas de Honor en Liverpool en agosto") == ""


class TestLaVistaVirtualRapida:
    """La capa de Liverpool region 4 tardo 231 s: filtro [Retail] = 'LIVERPOOL'
    (no existe, son 'Liverpool Offline' y 'Online') y luego LIKE sobre las
    nueve cadenas de un anio entero."""

    def test_solo_las_ramas_de_la_cadena_filtrada(self):
        q = m.expandir("SELECT 1 FROM vw_ETL_Con_Modelo WHERE [Retail] LIKE '%LIVERPOOL%' AND [Region] = 'R4'")
        vistas = {r.objeto for r in acceso.referencias(q) if r.objeto.startswith("vw_ETL")}
        assert vistas == {"vw_ETL_Liverpool", "vw_ETL_Liverpool_IOT"}

    def test_ante_la_duda_van_todas(self):
        for q in ("SELECT 1 FROM vw_ETL_Con_Modelo WHERE [Retail] = 'Coppel' OR [Brand] = 'X'",
                  "SELECT 1 FROM vw_ETL_Con_Modelo WHERE [Retail] <> 'Coppel'",
                  "SELECT 1 FROM vw_ETL_Con_Modelo WHERE [Retail] NOT IN ('Coppel')",
                  "SELECT 1 FROM vw_ETL_Con_Modelo WHERE [Retail] = 'Unknown'"):
            assert m.cadenas_del_filtro(q) is None, q

    def test_con_permisos_no_se_sale_de_los_suyos(self):
        q = m.expandir("SELECT 1 FROM vw_ETL_Con_Modelo WHERE [Retail] = 'TELCEL CAC'", ["COPPEL"])
        vistas = {r.objeto for r in acceso.referencias(q) if r.objeto.startswith("vw_ETL")}
        assert vistas == {"vw_ETL_COPPEL", "vw_ETL_COPPEL_IOT"}

    def test_un_retail_que_no_existe_se_avisa(self):
        import catalogo
        avisos = catalogo.filtros_que_no_casan(
            "SELECT 1 FROM [RAW_DATA_B2B].dbo.[vw_ETL_Con_Modelo] WHERE [Retail] = 'LIVERPOOL'")
        assert avisos and "Liverpool Offline" in avisos[0]

    def test_los_valores_de_region_y_retail_estan(self):
        import catalogo
        assert "R4" in catalogo.valores_conocidos("vw_ETL_Con_Modelo", "Region")
        assert "Liverpool Offline" in catalogo.valores_conocidos("vw_ETL_Con_Modelo", "Retail")


class TestElCatalogoNoEstaAlDia:
    """Medido el 7/10/2026: el 35% de la venta de octubre de Telcel Retail son
    modelos que UNICOS$ aun no tiene (Honor X6E y X7E entre ellos)."""

    def test_se_avisa_la_venta_sin_modelo(self):
        import pandas as pd
        from agent import _venta_sin_modelo
        df = pd.DataFrame({"Modelo": ["HONOR X6C", None, "HONOR X8D"], "Unidades": [100.0, 60.0, 40.0]})
        aviso = _venta_sin_modelo(df)
        assert "60 unidades (30%)" in aviso

    def test_poco_o_nada_sin_modelo_no_se_avisa(self):
        import pandas as pd
        from agent import _venta_sin_modelo
        assert _venta_sin_modelo(pd.DataFrame({"Modelo": ["A", "B"], "Unidades": [5.0, 5.0]})) == ""
        assert _venta_sin_modelo(pd.DataFrame({"Tienda": ["A"], "Unidades": [5.0]})) == ""

    def test_las_llaves_se_comparan_normalizadas(self):
        # ' HUAWEI BAND 10, NEGRO INTENSO' (con espacio delante) en el catalogo
        # de Liverpool no casaba.
        q = m.derivada(["LIVERPOOL"])
        assert "LTRIM(RTRIM(REPLACE(REPLACE([DESCRIPCION]" in q

    def test_coppel_tiene_segunda_llave_sin_la_marca(self):
        # 'POCO POCO C85 256 GB' en la venta, 'POCO C85 256 GB' en [DESCRIPCION].
        q = m.derivada(["COPPEL"])
        assert "COALESCE(u.[Modelo], u2.[Modelo])" in q
