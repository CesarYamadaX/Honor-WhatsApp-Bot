"""Traducir en el panel las conversaciones en ingles o chino.

El bot contesta en el idioma de quien pregunta y eso no cambia; esto es solo
para que quien revisa el panel pueda leer una conversacion en chino.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

import traduccion
from api import crear_app
from tests.test_api import AgenteFalso, _config


@pytest.fixture(autouse=True)
def _memoria_limpia():
    traduccion._recordadas.clear()
    yield
    traduccion._recordadas.clear()


def _openrouter_falso(monkeypatch, contenido, status=200):
    llamadas = []

    def post(url, json, timeout, headers):
        llamadas.append(json)
        return httpx.Response(status, json={"choices": [{"message": {"content": contenido}}]})

    monkeypatch.setenv("OPENROUTER_API_KEY", "clave")
    monkeypatch.setattr(traduccion.httpx, "post", post)
    return llamadas


class TestTraducir:
    def test_traduce_en_orden_y_conserva_las_negritas(self, monkeypatch):
        llamadas = _openrouter_falso(
            monkeypatch, '["¿Cuántos vendió Telcel?", "Telcel vendió **59,438**."]'
        )
        r = traduccion.traducir(["荣耀卖了多少台?", "售出 **59,438部**。"], "es")
        assert r == ["¿Cuántos vendió Telcel?", "Telcel vendió **59,438**."]
        assert "espanol" in llamadas[0]["messages"][0]["content"]

    def test_la_segunda_vez_no_vuelve_a_pagar(self, monkeypatch):
        llamadas = _openrouter_falso(monkeypatch, '["hola"]')
        traduccion.traducir(["hello"], "es")
        traduccion.traducir(["hello"], "es")
        assert len(llamadas) == 1

    def test_acepta_la_lista_entre_bloques_de_codigo(self, monkeypatch):
        _openrouter_falso(monkeypatch, '```json\n["hola"]\n```')
        assert traduccion.traducir(["hello"], "es") == ["hola"]

    def test_si_no_trae_un_texto_por_original_falla(self, monkeypatch):
        _openrouter_falso(monkeypatch, '["solo uno"]')
        with pytest.raises(traduccion.TraduccionFallida):
            traduccion.traducir(["uno", "dos"], "es")

    def test_un_idioma_que_no_es_del_panel(self):
        with pytest.raises(ValueError):
            traduccion.traducir(["x"], "zh")


class TestElEndpoint:
    def _cliente(self):
        return TestClient(crear_app(_config(), AgenteFalso))

    def test_devuelve_las_traducciones(self, monkeypatch):
        _openrouter_falso(monkeypatch, '["pregunta", "respuesta"]')
        with self._cliente() as c:
            r = c.post("/panel/traducir", json={"textos": ["question", "answer"], "destino": "es"})
        assert r.status_code == 200 and r.json() == {"traducciones": ["pregunta", "respuesta"]}

    def test_limites(self):
        with self._cliente() as c:
            assert c.post("/panel/traducir", json={"textos": ["x"], "destino": "fr"}).status_code == 400
            assert c.post("/panel/traducir", json={"textos": ["x" * 20_001]}).status_code == 413

    def test_si_openrouter_falla_es_502(self, monkeypatch):
        _openrouter_falso(monkeypatch, "", status=402)
        with self._cliente() as c:
            assert c.post("/panel/traducir", json={"textos": ["hello"]}).status_code == 502

    def test_solo_desde_la_misma_maquina(self):
        config = _config()
        config = type(config)(**{**config.__dict__, "api_host": "0.0.0.0"})
        with TestClient(crear_app(config, AgenteFalso)) as c:
            assert c.post("/panel/traducir", json={"textos": ["hello"]}).status_code == 403
