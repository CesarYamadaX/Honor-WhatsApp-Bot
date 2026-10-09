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
