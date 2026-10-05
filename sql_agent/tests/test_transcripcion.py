"""Notas de voz: el endpoint /transcribir y la llamada a OpenRouter.

Antes el sidecar descartaba los audios en silencio. Ahora los manda aqui, y
con el texto de vuelta pregunta como si lo hubieran escrito.
"""

import base64

import httpx
import pytest
from fastapi.testclient import TestClient

import transcripcion
from api import MAX_BYTES_DE_AUDIO, crear_app
from tests.test_api import AUTORIZADO, NO_AUTORIZADO, AgenteFalso, _config

_AUDIO = base64.b64encode(b"OggS-de-mentira").decode()


@pytest.fixture
def cliente():
    with TestClient(crear_app(_config(), AgenteFalso)) as c:
        yield c


def _pedir(cliente, chat=AUTORIZADO, audio=_AUDIO):
    return cliente.post("/transcribir", json={"chat_id": chat, "audio": audio})


class TestElEndpoint:
    def test_devuelve_el_texto_y_su_idioma(self, cliente, monkeypatch):
        recibido = {}

        def falso(audio, formato):
            recibido.update(audio=audio, formato=formato)
            return "cuantas unidades vendio Honor en agosto de 2026"

        monkeypatch.setattr(transcripcion, "transcribir", falso)
        r = _pedir(cliente)
        assert r.status_code == 200
        assert r.json() == {"texto": "cuantas unidades vendio Honor en agosto de 2026", "idioma": "es"}
        assert recibido == {"audio": b"OggS-de-mentira", "formato": "ogg"}

    def test_un_numero_ajeno_no_gasta_una_transcripcion(self, cliente, monkeypatch):
        llamadas = []
        monkeypatch.setattr(transcripcion, "transcribir", lambda *a: llamadas.append(a) or "x")
        assert _pedir(cliente, chat=NO_AUTORIZADO).status_code == 403
        assert llamadas == []

    def test_audio_mal_codificado_o_vacio(self, cliente):
        assert _pedir(cliente, audio="esto no es base64!!").status_code == 400
        assert _pedir(cliente, audio="").status_code == 400

    def test_audio_demasiado_grande(self, cliente):
        enorme = base64.b64encode(b"0" * (MAX_BYTES_DE_AUDIO + 1)).decode()
        assert _pedir(cliente, audio=enorme).status_code == 413

    def test_si_no_se_entendio_nada_lo_dice(self, cliente, monkeypatch):
        monkeypatch.setattr(transcripcion, "transcribir", lambda *a: "")
        assert _pedir(cliente).status_code == 422

    def test_si_openrouter_falla(self, cliente, monkeypatch):
        def falla(*a):
            raise transcripcion.TranscripcionFallida("402 sin saldo")

        monkeypatch.setattr(transcripcion, "transcribir", falla)
        assert _pedir(cliente).status_code == 502


class TestLaLlamada:
    def _respuesta(self, monkeypatch, status=200, cuerpo=None):
        enviado = {}

        def post(url, json, timeout, headers):
            enviado.update(json=json, headers=headers)
            return httpx.Response(status, json=cuerpo if cuerpo is not None else {
                "choices": [{"message": {"content": '  "cuantas vendio\n Honor"  '}}]})

        monkeypatch.setenv("OPENROUTER_API_KEY", "clave")
        monkeypatch.setattr(transcripcion.httpx, "post", post)
        return enviado

    def test_manda_el_audio_y_limpia_el_texto(self, monkeypatch):
        enviado = self._respuesta(monkeypatch)
        assert transcripcion.transcribir(b"audio", "ogg") == "cuantas vendio Honor"
        partes = enviado["json"]["messages"][0]["content"]
        assert partes[1]["input_audio"] == {"data": base64.b64encode(b"audio").decode(), "format": "ogg"}
        assert "Telcel" in partes[0]["text"] and "X6c" in partes[0]["text"]
        assert enviado["headers"]["Authorization"] == "Bearer clave"

    def test_un_error_del_servicio_se_avisa(self, monkeypatch):
        self._respuesta(monkeypatch, status=402, cuerpo={"error": "sin saldo"})
        with pytest.raises(transcripcion.TranscripcionFallida):
            transcripcion.transcribir(b"audio")

    def test_sin_clave_no_llama(self, monkeypatch):
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        with pytest.raises(transcripcion.TranscripcionFallida):
            transcripcion.transcribir(b"audio")
