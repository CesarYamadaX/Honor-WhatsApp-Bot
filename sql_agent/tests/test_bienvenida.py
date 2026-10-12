"""La bienvenida con ejemplos (pedido el 12/10/2026)."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

import bienvenida
from api import crear_app
from bitacora import Bitacora
from tests.test_api import AUTORIZADO, AgenteFalso, _config


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    monkeypatch.setattr(bienvenida, "ACTIVA", True)
    llamadas = []

    class Contador(AgenteFalso):
        def responder(self, pregunta, cancelacion=None, cadenas=None, notas=""):
            llamadas.append(pregunta)
            return super().responder(pregunta, cancelacion, cadenas)

    app = crear_app(_config(), lambda: Contador("Fueron 500 unidades."), bitacora=Bitacora(tmp_path / "b"))
    with TestClient(app) as c:
        yield c, llamadas


def _preguntar(cliente, texto):
    return cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": texto}).json()


@pytest.mark.parametrize("texto", ["hola", "Hola, ¿qué puedes hacer?", "buenos días", "ayuda", "hi", "你好"])
def test_un_saludo_es_un_saludo(texto):
    assert bienvenida.es_saludo(texto)


@pytest.mark.parametrize("texto", ["hola, cuanto vendio Coppel?", "ventas de Honor", "que puedes hacer con las ventas de Telcel por tienda en Coppel"])
def test_una_pregunta_con_saludo_no_es_solo_saludo(texto):
    assert not bienvenida.es_saludo(texto)


def test_el_saludo_se_contesta_sin_el_modelo(cliente):
    c, llamadas = cliente
    r = _preguntar(c, "hola")
    assert "por ejemplo" in r["reply"] and "Mándame el Excel" in r["reply"]
    assert llamadas == []


def test_la_primera_pregunta_lleva_ejemplos_y_la_segunda_no(cliente):
    c, llamadas = cliente
    primera = _preguntar(c, "cuanto vendio Honor en septiembre")
    assert primera["reply"].startswith("Fueron 500 unidades.") and "también puedes preguntarme" in primera["reply"]
    segunda = _preguntar(c, "y en agosto?")
    assert "también puedes preguntarme" not in segunda["reply"]


def test_los_ejemplos_son_de_las_cadenas_que_puede_ver():
    solo_coppel = bienvenida.texto("es", frozenset({"COPPEL"}), hoy=date(2026, 10, 12))
    assert "Coppel" in solo_coppel and "Telcel" not in solo_coppel
    assert "septiembre de 2026" in solo_coppel
    assert "Telcel" in bienvenida.texto("es", None, hoy=date(2026, 10, 12))


def test_en_su_idioma():
    assert "for example" in bienvenida.texto("en")
    assert "例如" in bienvenida.texto("zh")
