"""Lo que encontro la revision del 9/10/2026 con la bateria de preguntas."""

from datetime import datetime, timedelta

import pandas as pd

import agent
import contexto
import resumen_local
from sugerencias import sugerir

CONSOLIDADA = "[RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B]"
MESES = {
    "Coppel": {(2026, m) for m in range(1, 10)},
    "Telcel": {(2026, m) for m in range(1, 10)},
    "Liverpool": {(2026, m) for m in range(1, 9)},
    "Movistar": {(2026, m) for m in range(1, 9)},
}


class TestCadenasSinElMes:
    def test_un_total_de_todas_las_cadenas_avisa_quien_falta(self, monkeypatch):
        monkeypatch.setattr(resumen_local, "meses_por_cadena", lambda: MESES)
        aviso = agent.cadenas_sin_el_periodo(
            f"SELECT SUM([QTY]) FROM {CONSOLIDADA} WHERE [Brand] = 'HONOR' AND [Year] = 2026 AND [Month] = 9")
        assert "Liverpool no tiene septiembre de 2026 (lo mas reciente: agosto de 2026)" in aviso
        assert "Movistar" in aviso and "Coppel" not in aviso

    def test_el_ejemplo_nombra_solo_las_que_faltan(self, monkeypatch):
        # Visto el 20/10/2026: el ejemplo era fijo ("sin Liverpool ni
        # Movistar") y el modelo lo copio cuando solo faltaba Movistar.
        meses = dict(MESES, Liverpool={(2026, m) for m in range(1, 10)})
        monkeypatch.setattr(resumen_local, "meses_por_cadena", lambda: meses)
        aviso = agent.cadenas_sin_el_periodo(
            f"SELECT SUM([QTY]) FROM {CONSOLIDADA} WHERE [Brand] = 'HONOR' AND [Year] = 2026 AND [Month] = 9")
        assert "'sin Movistar, que aun no carga ese mes'" in aviso and "Liverpool" not in aviso

    def test_filtrado_a_una_cadena_o_sin_mes_no_avisa(self, monkeypatch):
        monkeypatch.setattr(resumen_local, "meses_por_cadena", lambda: MESES)
        assert agent.cadenas_sin_el_periodo(
            f"SELECT SUM([QTY]) FROM {CONSOLIDADA} WHERE [Retail] LIKE 'TELCEL%' AND [Year] = 2026 AND [Month] = 9") == ""
        assert agent.cadenas_sin_el_periodo(f"SELECT SUM([QTY]) FROM {CONSOLIDADA} WHERE [Year] = 2026") == ""
        assert agent.cadenas_sin_el_periodo(
            f"SELECT SUM([QTY]) FROM {CONSOLIDADA} WHERE [Year] = 2026 AND [Month] IN (6, 7)") == ""

    def test_sin_resumen_no_avisa(self, monkeypatch):
        monkeypatch.setattr(resumen_local, "meses_por_cadena", lambda: None)
        assert agent.cadenas_sin_el_periodo(
            f"SELECT SUM([QTY]) FROM {CONSOLIDADA} WHERE [Year] = 2026 AND [Month] = 9") == ""


def test_un_resultado_vacio_no_ofrece_seguir_preguntando():
    # SUM sobre cero filas: una fila con NULL (visto con "las ventas de ayer").
    vacio = pd.DataFrame({"Unidades": [None]})
    assert sugerir("ventas de Honor de ayer", vacio, f"SELECT SUM([QTY]) AS [Unidades] FROM {CONSOLIDADA}") == []


def test_el_crudo_de_una_cadena_respeta_la_marca_de_la_consulta():
    elektra = contexto.CRUDAS["ELEKTRA"]
    honor = contexto.replace(elektra, marcas=("Honor",))
    assert contexto._filtro_de(elektra) == ""
    assert contexto._filtro_de(honor) == f" AND UPPER(LTRIM(RTRIM(v.[{elektra.marca}]))) IN ('HONOR')"
    coppel = contexto.replace(contexto.CRUDAS["COPPEL"], marcas=("HONOR", "O'X"))
    assert contexto._filtro_iot(coppel, "i.") == " AND UPPER(LTRIM(RTRIM(i.[Brand]))) IN ('HONOR', 'O''X')"


def test_un_resumen_viejo_no_se_usa(tmp_path, monkeypatch):
    archivo = tmp_path / "r.sqlite"
    archivo.write_bytes(b"")
    viejo = (datetime.now() - timedelta(hours=resumen_local.MAX_HORAS_DE_ATRASO + 1)).isoformat()
    monkeypatch.setattr(resumen_local, "estado", lambda a=None: {"listo": True, "construido": viejo})
    assert resumen_local.consultar(f"SELECT SUM([QTY]) FROM {CONSOLIDADA}", archivo) is None
    assert resumen_local.meses_por_cadena(archivo) is None


def test_una_semana_de_una_cadena_mensual_dice_el_mes():
    aviso = agent.semana_de_cadena_mensual(
        "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Sears] WHERE [Brand] = 'SAMSUNG' AND [Year] = 2026 AND [WEEK] = 38")
    assert "Sears NO reporta por semana" in aviso and "[Month] = 9" in aviso
    assert agent.semana_de_cadena_mensual(
        "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_TELCEL_B2B] WHERE [Year] = 2026 AND [WEEK] = 38") == ""


class TestLaCifraPrincipalDelReporte:
    """Pedido el 9/10/2026: el mensaje de un Excel siempre da la cifra."""

    def test_elige_las_unidades_y_nunca_un_numero_de_tienda_o_un_precio(self):
        assert agent.cifra_principal(pd.DataFrame(
            {"Numero de tienda": [9797, 6570], "Unidades Honor": [342.0, 191.0]})) == ("Unidades Honor", 533.0)
        assert agent.cifra_principal(pd.DataFrame({"Retail": ["Coppel"], "Precio Venta": [19999]})) is None
        assert agent.cifra_principal(pd.DataFrame(
            {"Participacion %": [5.0], "Unidades Honor": [10.0], "Unidades totales": [100.0]}))[0] == "Unidades Honor"

    def _responder(self, monkeypatch, respuesta):
        from tests.test_agent_sql import _EjecutorEspia, _crear_engine_con_datos

        class ConReporte(_EjecutorEspia):
            def invoke(self, payload, config=None, **kwargs):
                agent._contexto_actual().registrar_reporte(
                    None, "honor_elektra.xlsx", cifras=frozenset({533}),
                    principal=("Unidades Honor", 533.0))
                return super().invoke(payload, config, **kwargs)

        monkeypatch.setenv("OPENROUTER_API_KEY", "clave-de-prueba")
        agente = agent.SQLAgent(engine=_crear_engine_con_datos("cifra", filas=3))
        agente.agent_executor = ConReporte(respuesta)
        return agente.responder("mandame el excel de Honor por tienda en Elektra")

    def test_si_el_mensaje_no_da_cifras_se_agrega_el_total(self, monkeypatch):
        texto, archivos = self._responder(monkeypatch, "Ya quedó: ventas de Honor por tienda en Elektra.")
        assert archivos == ["honor_elektra.xlsx"]
        assert "Total: **533** (Unidades Honor)." in texto

    def test_si_ya_la_da_no_se_repite(self, monkeypatch):
        texto, _ = self._responder(monkeypatch, "Honor vendió **533 unidades** en Elektra.")
        assert "Total:" not in texto


def test_las_semanas_de_las_cadenas_mensuales_son_de_su_mes():
    # Sears 2026: un dato por mes en las semanas 5..40 (enero a septiembre).
    # La 40 es septiembre, no octubre (visto el 12/10/2026).
    semanas = (5, 9, 14, 18, 23, 27, 31, 36, 40)
    assert [agent.mes_de_la_semana_mensual(2026, s) for s in semanas] == list(range(1, 10))


def test_una_semana_de_cierre_de_todas_las_cadenas_avisa_que_va_inflada():
    aviso = agent.semana_de_cadena_mensual(
        "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B] WHERE [Brand] = 'HONOR' AND [Year] = 2026 AND [WEEK] = 40")
    assert "cierra septiembre" in aviso and "Sears y Movistar" in aviso
    assert agent.semana_de_cadena_mensual(
        "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B] WHERE [Year] = 2026 AND [WEEK] = 38") == ""


def test_el_reporte_de_la_cadena_solo_si_se_pidio_un_archivo(monkeypatch):
    assert agent.pidio_archivo("mandame el excel de ventas de Honor en Elektra")
    assert agent.pidio_archivo("reporte de ventas de Coppel de enero")
    assert not agent.pidio_archivo("que marca vende mas en Telcel en septiembre 2026")

    def no_debe_llamarse(*a, **k):
        raise AssertionError("armo el reporte de la cadena sin que se pidiera")

    monkeypatch.setattr(contexto, "_datos_y_raw", no_debe_llamarse)
    df = pd.DataFrame({"Brand": ["SAMSUNG", "HONOR"], "Year": [2026, 2026], "Month": [9, 9], "Unidades": [372436, 73628]})
    contexto.contexto_del_resultado(
        df, None, "SELECT [Brand], SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_TELCEL_B2B] WHERE [Year] = 2026 AND [Month] = 9 GROUP BY [Brand]",
        cadenas=None, extras=False, de_cadena=False)


def test_no_busca_el_modelo_por_texto_en_la_vista_con_modelo():
    lenta = ("SELECT [Modelo], SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Con_Modelo] "
             "WHERE [Retail] = 'Coppel' AND [Material_Description] LIKE '%X9D%' GROUP BY [Modelo]")
    assert "No la ejecute" in agent._like_en_la_vista_con_modelo(lenta)
    buena = ("SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Con_Modelo] "
             "WHERE [Retail] = 'Coppel' AND [Modelo] = 'HONOR X9D 6 78 4G'")
    assert agent._like_en_la_vista_con_modelo(buena) == ""
    # En otra vista no aplica (ahi el texto es la unica forma).
    assert agent._like_en_la_vista_con_modelo(
        "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_COPPEL] WHERE [Material_Description] LIKE '%X9D%'") == ""
