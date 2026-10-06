"""Buscar en el panel por lo que se dijo en las conversaciones.

Antes la barra de la solapa de chats solo buscaba numeros (pedido el
6/10/2026: palabras u oraciones, ademas del nombre guardado y el numero; el
nombre y el numero los busca el panel, que ya tiene la lista de usuarios).
"""

from fastapi.testclient import TestClient

from api import crear_app
from bitacora import Bitacora
from tests.test_api import AgenteFalso, _config


def _diario(tmp_path):
    diario = Bitacora(tmp_path)
    for chat, pregunta, respuesta in [
        ("521@s.whatsapp.net", "¿Cuántos teléfonos vendió Honor?", "59,438 teléfonos"),
        ("522@s.whatsapp.net", "promotoria de Coppel", "757 promotores"),
        ("521@s.whatsapp.net", "y en Telcel?", "Telcel vendió 62,665"),
    ]:
        turno = diario.comenzar(chat, pregunta)
        diario.terminar(turno, respuesta=respuesta)
    return diario


class TestBuscarTexto:
    def test_sin_acentos_ni_mayusculas(self, tmp_path):
        filas = _diario(tmp_path).buscar_texto("TELEFONOS")
        assert [f["chat_id"] for f in filas] == ["521@s.whatsapp.net"]
        assert filas[0]["coincidencias"] == 2  # en la pregunta y en la respuesta
        assert "teléfonos" in filas[0]["fragmento"]

    def test_una_oracion(self, tmp_path):
        filas = _diario(tmp_path).buscar_texto("promotoria de coppel")
        assert [f["chat_id"] for f in filas] == ["522@s.whatsapp.net"]

    def test_menos_de_dos_letras_no_busca(self, tmp_path):
        assert _diario(tmp_path).buscar_texto("t") == []

    def test_el_fragmento_es_alrededor_de_la_coincidencia(self, tmp_path):
        diario = Bitacora(tmp_path)
        turno = diario.comenzar("523@s.whatsapp.net", "x" * 200 + " Liverpool " + "y" * 200)
        diario.terminar(turno, respuesta="")
        fragmento = diario.buscar_texto("liverpool")[0]["fragmento"]
        assert "Liverpool" in fragmento and len(fragmento) < 150
        assert fragmento.startswith("…") and fragmento.endswith("…")


def test_el_endpoint_del_panel(tmp_path):
    app = crear_app(_config(), AgenteFalso, bitacora=_diario(tmp_path))
    with TestClient(app) as cliente:
        r = cliente.get("/panel/buscar", params={"texto": "telcel"})
        assert r.status_code == 200
        assert [c["chat_id"] for c in r.json()["chats"]] == ["521@s.whatsapp.net"]
        assert cliente.get("/panel/buscar", params={"texto": "x" * 201}).status_code == 413
