"""El revisor que mira cada archivo y cada respuesta antes de salir.

Pedido el 7/10/2026 despues del reporte de Liverpool region 4: dos tiendas
de menos, una hoja con las tiendas de Telcel que nadie pidio, y una
comparacion contra otro anio. Nada de eso daba error.
"""

import json

import pandas as pd
import pytest

import revisor


def _hoja_con_cadenas(*retails):
    return pd.DataFrame({"Retail": list(retails), "Unidades": [1.0] * len(retails)})


class TestLasHojas:
    def test_una_hoja_con_cadenas_que_no_se_pidieron_se_quita(self):
        df = pd.DataFrame({"Tienda": ["L MTY CENTRO"], "Unidades": [5.0]})
        contexto = {"Ventas por tienda": _hoja_con_cadenas("TELCEL RETAIL", "Liverpool Offline")}
        r = revisor.revisar_hojas(df, contexto, query="SELECT * FROM [RAW_DATA_B2B].dbo.[vw_ETL_Liverpool]")
        assert "Ventas por tienda" not in r.contexto
        assert any("Telcel" in h for h in r.hallazgos)

    def test_las_de_la_misma_cadena_se_quedan(self):
        df = pd.DataFrame({"Retail": ["Liverpool Offline"], "Unidades": [5.0]})
        contexto = {"Liverpool Online": _hoja_con_cadenas("Liverpool Online")}
        r = revisor.revisar_hojas(df, contexto)
        assert list(r.contexto) == ["Liverpool Online"] and not r.hallazgos

    def test_sin_permiso_se_quitan_las_filas(self):
        df = _hoja_con_cadenas("Coppel", "TELCEL CAC", "Coppel")
        contexto = {"Raw_Data": _hoja_con_cadenas("Coppel", "ELEKTRA")}
        r = revisor.revisar_hojas(df, contexto, cadenas=["COPPEL"])
        assert set(r.df["Retail"]) == {"Coppel"}
        assert set(r.contexto["Raw_Data"]["Retail"]) == {"Coppel"}
        assert len(r.hallazgos) == 2

    def test_detalle_y_agrupado_que_no_cuadran(self):
        crudo = pd.DataFrame({"Cantidad": [10.0, 20.0]})
        crudo.attrs["pivot"] = {"unidades": "Cantidad"}
        agrupado = pd.DataFrame({"Unidades": [25.0]})
        r = revisor.revisar_hojas(pd.DataFrame({"x": [1]}), {"Raw_Data": crudo, "Datos": agrupado})
        assert any("no cuadran" in h for h in r.hallazgos)

    def test_sin_nada_raro_no_toca_nada(self):
        df = pd.DataFrame({"Tienda": ["a"], "Unidades": [1.0]})
        r = revisor.revisar_hojas(df, {})
        assert r.contexto == {} and r.hallazgos == [] and r.df is df


class _Respuesta:
    def __init__(self, contenido, estado=200):
        self.status_code = estado
        self._cuerpo = {"choices": [{"message": {"content": contenido}}]}

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError("fallo")

    def json(self):
        return self._cuerpo


@pytest.fixture
def modelo(monkeypatch):
    monkeypatch.setattr(revisor, "ACTIVO", True)
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    enviados = []

    def contestar(veredicto, estado=200):
        def post(url, json=None, **kwargs):
            enviados.append(json)
            if isinstance(veredicto, Exception):
                raise veredicto
            return _Respuesta(veredicto, estado)
        monkeypatch.setattr(revisor.httpx, "post", post)
        return enviados
    return contestar


class TestLaRespuesta:
    DATOS = ["Consulta: ...\nResultado: 1. Tienda: L CIUDAD VICTORIA | Año: 2025 | Mes: 1 | Unidades: 489"]

    def test_si_cuadra_sale_igual(self, modelo):
        modelo(json.dumps({"ok": True}))
        r = revisor.revisar_respuesta("ventas de enero 2025", self.DATOS, "Se vendieron **489** equipos.")
        assert r.ok

    def test_si_no_cuadra_sale_corregida(self, modelo):
        modelo("```json\n" + json.dumps({
            "ok": False, "problemas": ["la cifra 493 no esta en los datos (es de 2024)"],
            "respuesta": "En enero de 2025 se vendieron **489** equipos en Ciudad Victoria.",
        }) + "\n```")
        r = revisor.revisar_respuesta("ventas de enero 2025", self.DATOS, "Se vendieron **493** equipos.")
        assert not r.ok and "489" in r.texto and "2024" in r.problemas[0]

    def test_el_revisor_ve_la_pregunta_los_datos_y_el_borrador(self, modelo):
        enviados = modelo(json.dumps({"ok": True}))
        revisor.revisar_respuesta("ventas de enero 2025", self.DATOS, "borrador", cadenas=["COPPEL"])
        sistema, usuario = enviados[0]["messages"]
        assert "SOLO puede ver datos de Coppel" in sistema["content"]
        assert "ventas de enero 2025" in usuario["content"] and "CIUDAD VICTORIA" in usuario["content"]
        assert "borrador" in usuario["content"]

    def test_si_el_revisor_falla_sale_el_borrador(self, modelo):
        modelo(RuntimeError("sin red"))
        assert revisor.revisar_respuesta("p", self.DATOS, "borrador").ok

    def test_una_correccion_desproporcionada_no_reemplaza(self, modelo):
        modelo(json.dumps({"ok": False, "problemas": ["x"], "respuesta": "y" * 5000}))
        r = revisor.revisar_respuesta("p", self.DATOS, "corto")
        assert not r.ok and r.texto == ""

    def test_de_un_resultado_largo_ve_tambien_el_final(self, modelo):
        # Visto el 5/11/2026: 456,133 filas de Coppel; el total va despues de
        # las filas y el revisor, que solo veia el principio, lo quito.
        filas = "\n".join(f"{i}. Tienda: T{i} | Unidades: 1" for i in range(2000))
        visto = f"Consulta: ...\n{filas}\n\nLa cifra principal de este resultado es 545,787 (total de [Unidades])."
        enviados = modelo(json.dumps({"ok": True}))
        revisor.revisar_respuesta("raw data de Coppel", [visto], "Se vendieron **545,787** equipos.")
        usuario = enviados[0]["messages"][1]["content"]
        assert "545,787 (total de [Unidades])" in usuario and "1. Tienda: T1 |" in usuario
        assert len(usuario) < revisor.MAX_POR_RESULTADO + 1000

    def test_sin_datos_no_hay_nada_que_revisar(self, modelo):
        enviados = modelo(json.dumps({"ok": False}))
        assert revisor.revisar_respuesta("hola", [], "Hola, ¿en que te ayudo?").ok
        assert enviados == []


class TestLasDevolucionesDeLaTablaCruda:
    """La capa de Liverpool region 4 se saco una vez de las vistas (2 090 en
    Tampico Altama, diciembre de 2025) y otra de las crudas (2 054): las
    crudas traen las devoluciones en negativo."""

    def test_sumar_la_cruda_sin_filtro_se_bloquea(self):
        from agent import _crudo_con_devoluciones as f
        assert f("SELECT SUM(v.[Vtas# U Per 1]) FROM [RAW_DATA_B2B].dbo.[LIVERPOOL_B2B$] v")
        assert f("SELECT SUM([Cantidad]) FROM [RAW_DATA_B2B].dbo.[TELCEL_B2B]")

    def test_con_el_filtro_o_desde_la_vista_pasa(self):
        from agent import _crudo_con_devoluciones as f
        assert f("SELECT SUM(v.[Vtas# U Per 1]) FROM [RAW_DATA_B2B].dbo.[Liverpool_IOT] v "
                 "WHERE v.[Vtas# U Per 1] >= 1") == ""
        assert f("SELECT SUM([Unidades]) FROM [RAW_DATA_B2B].dbo.[COPPEL_B2B] WHERE [Unidades] > 0") == ""
        assert f("SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Liverpool]") == ""

    def test_sin_sumar_no_aplica(self):
        from agent import _crudo_con_devoluciones as f
        assert f("SELECT TOP 10 * FROM [RAW_DATA_B2B].dbo.[EKT_B2B]") == ""
