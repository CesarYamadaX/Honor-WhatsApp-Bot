"""La foto de perfil de WhatsApp en el panel (pedido el 13/10/2026)."""

import base64
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from api import crear_app
from bitacora import Bitacora
from fotos import AlmacenDeFotos
from tests.test_api import AgenteFalso, _config
from usuarios import RegistroUsuarios

NUMERO = "5215512345678"


def _existe(album, nombre) -> bool:
    ruta = album.ruta(nombre)
    return ruta is not None and ruta.exists()


def _png(color) -> str:
    salida = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(salida, "PNG")
    return base64.b64encode(salida.getvalue()).decode("ascii")


@pytest.fixture
def todo(tmp_path):
    usuarios = RegistroUsuarios(tmp_path / "u.json", semilla=[NUMERO])
    album = AlmacenDeFotos(tmp_path / "fotos")
    app = crear_app(_config(), lambda: AgenteFalso("ok"), usuarios, Bitacora(tmp_path / "b"), album)
    with TestClient(app) as cliente:
        yield cliente, usuarios, album


def test_pendientes_solo_los_activos_sin_revisar(todo):
    cliente, usuarios, _ = todo
    assert cliente.get("/fotos/pendientes").json()["numeros"] == [NUMERO]
    cliente.post("/foto-whatsapp", json={"numero": NUMERO, "imagen": _png("red")})
    assert cliente.get("/fotos/pendientes").json()["numeros"] == []


def test_se_guarda_se_cambia_y_se_quita(todo):
    cliente, usuarios, album = todo
    cliente.post("/foto-whatsapp", json={"numero": NUMERO, "imagen": _png("red")})
    primera = usuarios.obtener(NUMERO).foto_whatsapp
    assert primera and _existe(album, primera)
    cliente.post("/foto-whatsapp", json={"numero": NUMERO, "imagen": _png("blue")})
    segunda = usuarios.obtener(NUMERO).foto_whatsapp
    assert segunda != primera and not _existe(album, primera)
    # Sin imagen: la quito de su perfil o ya no la deja ver.
    cliente.post("/foto-whatsapp", json={"numero": NUMERO, "imagen": None})
    assert usuarios.obtener(NUMERO).foto_whatsapp == "" and not _existe(album, segunda)


def test_la_foto_subida_a_mano_no_se_toca(todo):
    cliente, usuarios, album = todo
    cliente.post("/panel/usuarios/foto", json={"numero": NUMERO, "imagen": _png("green")})
    propia = usuarios.obtener(NUMERO).foto
    cliente.post("/foto-whatsapp", json={"numero": NUMERO, "imagen": _png("red")})
    cliente.post("/foto-whatsapp", json={"numero": NUMERO, "imagen": None})
    assert usuarios.obtener(NUMERO).foto == propia and _existe(album, propia)


def test_un_numero_que_no_es_usuario_no_guarda_nada(todo):
    cliente, _, _ = todo
    assert cliente.post("/foto-whatsapp", json={"numero": "5219999999999", "imagen": _png("red")}).status_code == 404


def test_la_foto_del_bot_va_en_la_marca_y_sobrevive_al_reinicio(tmp_path):
    usuarios = RegistroUsuarios(tmp_path / "u.json", semilla=[NUMERO])
    album = AlmacenDeFotos(tmp_path / "fotos")
    app = crear_app(_config(), lambda: AgenteFalso("ok"), usuarios, Bitacora(tmp_path / "b"), album)
    with TestClient(app) as cliente:
        assert cliente.get("/panel/bot").json()["foto"] == ""
        cliente.post("/foto-bot", json={"numero": "5215500001111", "imagen": _png("red")})
        foto = cliente.get("/panel/bot").json()["foto"]
        assert foto and _existe(album, foto)
    # Otro arranque, antes de que el sidecar la vuelva a traer: la recuerda.
    app = crear_app(_config(), lambda: AgenteFalso("ok"), usuarios, Bitacora(tmp_path / "b"), album)
    with TestClient(app) as cliente:
        assert cliente.get("/panel/bot").json()["foto"] == foto
        cliente.post("/foto-bot", json={"numero": "5215500001111", "imagen": None})
        assert cliente.get("/panel/bot").json()["foto"] == "" and not _existe(album, foto)


def test_la_pagina_del_panel_no_se_queda_en_cache(todo):
    # Visto el 13/10/2026: despues del pull el navegador seguia con la
    # pagina vieja, que no sabia mostrar las fotos de WhatsApp.
    cliente, _, _ = todo
    assert cliente.get("/panel").headers["cache-control"] == "no-cache"
