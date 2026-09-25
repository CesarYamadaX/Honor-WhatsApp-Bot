"""Pruebas de sugerencias.py.

Lo que se ofrece al final de una respuesta tiene que cumplir dos cosas: no
repetir lo que ya venia en la tabla, y no ser un sinsentido. El share de
HONOR sobre un reporte de promotoria seria el porcentaje de marca de unos
promotores, y ofrecerlo enseña que el bot no entiende lo que acaba de
entregar.
"""

import pandas as pd

import idioma as idiomas
from sugerencias import MAX_SUGERENCIAS, sugerir

VENTA = "SELECT ... FROM [RAW_DATA_B2B].dbo.[vw_ETL_Elektra] WHERE [Year]=2026"
CONSOLIDADO = "SELECT ... FROM [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B]"
PROMOTORIA = "SELECT ... FROM [GTM BI Database].dbo.[V_HCReport_Informes]"


class TestQueSeOfrece:
    def test_un_total_pelado_abre_las_dos_preguntas_de_siempre(self):
        df = pd.DataFrame({"Total": [1268566]})
        assert sugerir("ventas totales de Elektra", df, VENTA) == [
            "share_honor",
            "por_tienda",
        ]

    def test_si_la_consulta_ya_filtro_la_marca_el_share_es_otro(self):
        # "Que parte de eso fue HONOR" sobre una consulta que ya filtro
        # HONOR es el 100%: no significa nada. Lo que si abre camino es
        # cuanto pesa ese numero dentro del mercado.
        df = pd.DataFrame({"Total": [160909]})
        de_honor = "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B] WHERE [Brand] = 'HONOR'"
        claves = sugerir("cuantas vendio Honor en agosto", df, de_honor)

        assert "share_sobre_el_mercado" in claves
        assert "share_honor" not in claves

    def test_sin_marca_filtrada_se_ofrece_el_share_de_honor(self):
        df = pd.DataFrame({"Total": [1268566]})
        assert "share_honor" in sugerir("ventas totales de Elektra", df, VENTA)

    def test_una_serie_mensual_ofrece_comparar_contra_el_ano_pasado(self):
        df = pd.DataFrame({"Año": [2026] * 3, "Mes": [1, 2, 3], "Total": [1, 2, 3]})
        assert "mismo_periodo_anterior" in sugerir("ventas por mes", df, VENTA)

    def test_sin_columna_de_tiempo_no_se_ofrece_comparar(self):
        # No hay periodo en la tabla: la comparacion no se apoya en nada.
        df = pd.DataFrame({"Store": ["a", "b"], "QTY": [2, 1]})
        assert "mismo_periodo_anterior" not in sugerir("top de tiendas", df, VENTA)

    def test_nunca_se_ofrece_lo_que_ya_estaba_en_la_tabla(self):
        df = pd.DataFrame({"Store": ["a"], "Brand": ["HONOR"], "Modelo": ["x"], "QTY": [1]})
        claves = sugerir("ventas por tienda, marca y modelo", df, VENTA)
        assert "por_tienda" not in claves
        assert "share_honor" not in claves
        assert "por_modelo" not in claves

    def test_con_tiendas_de_venta_se_ofrece_su_capa(self):
        df = pd.DataFrame({"Store": ["a", "b"], "QTY": [2, 1]})
        assert "capa" in sugerir("top 10 de tiendas de Coppel", df, VENTA)

    def test_a_quien_ya_pidio_capa_no_se_le_ofrece_capa(self):
        df = pd.DataFrame({"Store": ["a", "b"], "CAPA": [2, 1]})
        assert "capa" not in sugerir("dame el capa de las tiendas", df, VENTA)

    def test_desde_un_solo_retailer_se_ofrece_comparar_con_los_demas(self):
        # Con tienda, marca y modelo ya en la tabla, lo unico que queda por
        # fuera de la respuesta son las otras cadenas.
        df = pd.DataFrame(
            {"Store": ["a"], "Brand": ["HONOR"], "Modelo": ["x"], "QTY": [1]}
        )
        assert "otros_retailers" in sugerir("capa por tienda de Elektra", df, VENTA)

    def test_desde_la_consolidada_no(self):
        # Ahi ya estan todas las cadenas: no hay nada que comparar afuera.
        df = pd.DataFrame({"Brand": ["HONOR"], "QTY": [1]})
        assert "otros_retailers" not in sugerir("ventas 2026", df, CONSOLIDADO)

    def test_se_mandan_dos_como_mucho(self):
        df = pd.DataFrame({"Año": [2026, 2026], "Total": [1, 2]})
        assert len(sugerir("ventas", df, VENTA)) <= MAX_SUGERENCIAS


class TestPromotoria:
    """Sobre promotoria no se pregunta lo mismo que sobre venta."""

    def test_no_se_ofrece_el_share_de_unos_promotores(self):
        df = pd.DataFrame({"Region": ["R9"], "promotores": [412]})
        claves = sugerir("promotoria de agosto por region", df, PROMOTORIA)
        assert "share_honor" not in claves
        assert "por_modelo" not in claves

    def test_se_ofrece_cruzarla_con_la_venta(self):
        # La pregunta que junta las dos mitades del negocio: las tiendas con
        # promotor, venden mas?
        df = pd.DataFrame({"Coppel Name": ["x"], "status_of_promoter": [1]})
        assert "ventas_de_esas_tiendas" in sugerir("promotoria", df, PROMOTORIA)


class TestCasosQueNoOfrecenNada:
    def test_sin_resultados_no_se_ofrece_nada(self):
        assert sugerir("lo que sea", pd.DataFrame(), VENTA) == []
        assert sugerir("lo que sea", None, VENTA) == []

    def test_una_consulta_a_un_catalogo_no_abre_preguntas_de_venta(self):
        # Un listado de tiendas no es un dato de negocio del que colgar un
        # share ni un CAPA.
        df = pd.DataFrame({"ID": ["APSMX1"], "Region": ["R9"]})
        catalogo = "SELECT [ID], [Region] FROM [GTM BI Database].dbo.[CustomerStores]"
        claves = sugerir("dame las tiendas", df, catalogo)
        assert "share_honor" not in claves and "capa" not in claves


class TestElTextoQueSeManda:
    def test_va_en_el_idioma_del_usuario(self):
        claves = ["share_honor", "por_tienda"]
        assert "HONOR" in idiomas.sugerencias("es", claves)
        assert idiomas.sugerencias("en", claves).startswith("I can also tell you")
        assert "份额" in idiomas.sugerencias("zh", claves)

    def test_sin_claves_no_hay_texto(self):
        # Para que quien llama no tenga que acordarse de comprobarlo.
        assert idiomas.sugerencias("es", []) == ""

    def test_una_clave_desconocida_no_revienta_el_mensaje(self):
        assert idiomas.sugerencias("es", ["inventada"]) == ""

    def test_cada_clave_que_produce_sugerir_tiene_texto(self):
        # Una clave sin traduccion se cae en silencio y la sugerencia nunca
        # llega; aqui se ve.
        import sugerencias as modulo

        claves = set()
        for query in (VENTA, CONSOLIDADO, PROMOTORIA):
            for df in (
                pd.DataFrame({"Total": [1]}),
                pd.DataFrame({"Año": [2026], "Total": [1]}),
                pd.DataFrame({"Store": ["a"], "QTY": [1]}),
                pd.DataFrame({"Coppel Name": ["a"], "status_of_promoter": [1]}),
                pd.DataFrame({"Store": ["a"], "Brand": ["HONOR"], "QTY": [1]}),
            ):
                claves.update(modulo.sugerir("pregunta", df, query))
        assert claves, "no se genero ninguna clave"
        for clave in claves:
            assert idiomas.sugerencias("es", [clave]), f"{clave} no tiene texto"


class TestSobreUnPronostico:
    def test_no_se_ofrece_comparar_con_el_ano_pasado(self):
        # El metodo del pronostico YA es el mismo mes del ano anterior:
        # ofrecerlo seria ofrecer lo que acaba de usar para contestar.
        df = pd.DataFrame({"Año": [2026] * 3, "Mes": [1, 2, 3], "Total": [1, 2, 3]})
        claves = sugerir("dame un estimado del proximo mes", df, VENTA)
        assert "mismo_periodo_anterior" not in claves
        # Y si queda lo que de verdad abre camino.
        assert claves
