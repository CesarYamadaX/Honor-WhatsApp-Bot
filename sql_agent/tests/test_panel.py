"""Pruebas del panel: administrar usuarios y mirar que hace el bot.

El panel manda sobre la allowlist, asi que dos cosas importan mas que el
resto: que un cambio hecho aqui se note de inmediato en quien puede usar el
bot, y que el panel no quede expuesto fuera de la maquina.
"""

import pytest
from fastapi.testclient import TestClient

from api import crear_app
from bitacora import Bitacora
from config import Config
from usuarios import RegistroUsuarios

AUTORIZADO = "5215512345678@s.whatsapp.net"
NUEVO = "5215599998888"


def _config(host="127.0.0.1"):
    return Config(
        allowlist=frozenset({"5215512345678"}),
        timeout_segundos=90,
        ttl_minutos=60,
        api_host=host,
        api_port=8000,
        espera_cancelacion=0.2,
    )


class AgenteFalso:
    idioma = "es"

    def __init__(self):
        self.preguntas = []

    def responder(self, pregunta, cancelacion=None):
        self.preguntas.append(pregunta)
        return "listo", []


@pytest.fixture
def usuarios():
    return RegistroUsuarios(semilla=["5215512345678"])


@pytest.fixture
def diario():
    return Bitacora()


@pytest.fixture
def cliente(usuarios, diario):
    app = crear_app(_config(), AgenteFalso, usuarios, diario)
    with TestClient(app) as c:
        yield c


class TestPantalla:
    def test_el_panel_se_sirve(self, cliente):
        respuesta = cliente.get("/panel")
        assert respuesta.status_code == 200
        assert "Panel del bot" in respuesta.text


class TestAdministracionDeUsuarios:
    def test_se_listan_los_usuarios(self, cliente):
        datos = cliente.get("/panel/usuarios").json()
        assert [u["numero"] for u in datos["usuarios"]] == ["5215512345678"]

    def test_dar_de_alta_habilita_a_alguien_de_inmediato(self, cliente, usuarios):
        # Sin reiniciar: es la razon de ser del panel. Antes habia que editar
        # el .env y bajar los dos procesos.
        chat = f"{NUEVO}@s.whatsapp.net"
        assert cliente.post("/ask", json={"chat_id": chat, "text": "hola"}).status_code == 403

        cliente.post("/panel/usuarios", json={"numero": NUEVO, "nombre": "Laura"})
        assert cliente.post("/ask", json={"chat_id": chat, "text": "hola"}).status_code == 200

    def test_bloquear_corta_el_acceso_de_inmediato(self, cliente):
        cliente.post("/panel/usuarios/bloquear", json={"numero": "5215512345678"})
        respuesta = cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "hola"})
        assert respuesta.status_code == 403

    def test_suspender_tambien(self, cliente):
        cliente.post("/panel/usuarios/suspender", json={"numero": "5215512345678"})
        assert cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "x"}).status_code == 403

    def test_reactivar_devuelve_el_acceso(self, cliente):
        cliente.post("/panel/usuarios/bloquear", json={"numero": "5215512345678"})
        cliente.post("/panel/usuarios/reactivar", json={"numero": "5215512345678"})
        assert cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "x"}).status_code == 200

    def test_eliminar_lo_saca_de_la_lista(self, cliente):
        cliente.post("/panel/usuarios/eliminar", json={"numero": "5215512345678"})
        assert cliente.get("/panel/usuarios").json()["usuarios"] == []
        assert cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "x"}).status_code == 403

    def test_un_numero_invalido_se_explica_en_vez_de_reventar(self, cliente):
        respuesta = cliente.post("/panel/usuarios", json={"numero": "no soy numero"})
        assert respuesta.status_code == 400
        assert "digito" in respuesta.json()["detail"]

    def test_dar_de_alta_dos_veces_avisa(self, cliente):
        respuesta = cliente.post("/panel/usuarios", json={"numero": "5215512345678"})
        assert respuesta.status_code == 400
        assert "ya esta" in respuesta.json()["detail"]

    def test_un_bloqueado_no_recibe_ni_el_indicador_de_escribiendo(self, cliente):
        # /authorized es lo que el sidecar consulta ANTES de mostrar señales
        # de vida. Si aqui dijera que si, un bloqueado veria al bot
        # "escribiendo" y sabria que sigue existiendo.
        cliente.post("/panel/usuarios/bloquear", json={"numero": "5215512345678"})
        assert cliente.post("/authorized", json={"chat_id": AUTORIZADO}).status_code == 403


class TestVerLoQueHaceElBot:
    def test_una_conversacion_queda_registrada(self, cliente, diario):
        cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "ventas de Elektra"})
        turno = diario.en_curso()
        assert turno == []  # ya termino

    def test_las_conversaciones_se_pueden_listar(self, cliente, tmp_path, usuarios):
        diario = Bitacora(tmp_path / "bitacora")
        app = crear_app(_config(), AgenteFalso, usuarios, diario)
        with TestClient(app) as c:
            c.post("/ask", json={"chat_id": AUTORIZADO, "text": "ventas de Elektra"})
            chats = c.get("/panel/conversaciones").json()["chats"]
            assert chats[0]["chat_id"] == AUTORIZADO
            assert chats[0]["ultima_pregunta"] == "ventas de Elektra"

            turnos = c.get(f"/panel/conversaciones?chat_id={AUTORIZADO}").json()["turnos"]
            assert turnos[0]["respuesta"] == "listo"

    def test_la_actividad_dice_cuantas_conversaciones_hay_vivas(self, cliente):
        cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "hola"})
        datos = cliente.get("/panel/actividad").json()
        assert datos["sesiones"] == 1
        assert datos["en_curso"] == []

    def test_un_error_del_agente_queda_registrado_como_error(self, tmp_path, usuarios):
        # Para eso sirve mirar la bitacora: saber que fallo sin leer el log
        # del proceso, donde el motivo esta entre stack traces.
        class AgenteQueFalla:
            idioma = "es"

            def responder(self, pregunta, cancelacion=None):
                raise RuntimeError("timeout expired")

        diario = Bitacora(tmp_path / "bitacora")
        app = crear_app(_config(), AgenteQueFalla, usuarios, diario)
        with TestClient(app) as c:
            c.post("/ask", json={"chat_id": AUTORIZADO, "text": "algo pesado"})
            turnos = c.get(f"/panel/conversaciones?chat_id={AUTORIZADO}").json()["turnos"]

        assert turnos[0]["estado"] == "error"
        assert "timeout" in turnos[0]["respuesta"]


class TestNoQuedaExpuesto:
    def test_fuera_de_loopback_el_panel_se_apaga(self, usuarios, diario):
        # El servicio no tiene autenticacion; su unica defensa es escuchar
        # solo en la maquina. El panel da de alta usuarios y deja leer
        # conversaciones, asi que atado a 0.0.0.0 no puede quedar servido.
        app = crear_app(_config(host="0.0.0.0"), AgenteFalso, usuarios, diario)
        with TestClient(app) as c:
            for ruta in ["/panel", "/panel/usuarios", "/panel/actividad", "/panel/conversaciones"]:
                assert c.get(ruta).status_code == 403, ruta
            assert c.post("/panel/usuarios", json={"numero": NUEVO}).status_code == 403

    def test_pero_el_bot_sigue_funcionando(self, usuarios, diario):
        # Apagar el panel no puede dejar sin servicio a WhatsApp.
        app = crear_app(_config(host="0.0.0.0"), AgenteFalso, usuarios, diario)
        with TestClient(app) as c:
            assert c.post("/ask", json={"chat_id": AUTORIZADO, "text": "x"}).status_code == 200
