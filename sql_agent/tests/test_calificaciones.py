"""Calificar respuestas con 👍/👎 desde WhatsApp y verlas en el panel.

Antes una respuesta equivocada solo se descubria si alguien la reportaba a
mano (asi aparecio lo de la promotoria de Sears).
"""

import pytest
from fastapi.testclient import TestClient

from api import crear_app
from bitacora import Bitacora
from calificaciones import BUENA, MALA, Calificaciones, valor_de
from tests.test_api import AUTORIZADO, NO_AUTORIZADO, AgenteFalso, _config


class TestQueCuentaComoCalificacion:
    def test_pulgares_con_cualquier_tono(self):
        assert valor_de("👍") == BUENA and valor_de("👎") == MALA
        assert valor_de("👍🏽") == BUENA and valor_de("👎🏿") == MALA

    def test_no_solo_los_pulgares(self):
        # Pedido el 6/10/2026. ❤️ y 🙏 vienen en la barra rapida de WhatsApp.
        for buena in ("❤️", "🙏🏻", "🔥", "👏🏽", "💯", "✅", "😍"):
            assert valor_de(buena) == BUENA, buena
        for mala in ("😡", "😢", "😭", "❌", "🤔", "☹️", "🤦🏽‍♀️"):
            assert valor_de(mala) == MALA, mala

    def test_las_ambiguas_no_califican(self):
        # 😂 lo mismo es "que buena" que "que mal salio esto".
        assert valor_de("😂") is None and valor_de("😮") is None and valor_de("") is None

    def test_ninguna_cuenta_dos_veces(self):
        from calificaciones import REACCIONES_BUENAS, REACCIONES_MALAS
        assert not set(REACCIONES_BUENAS) & set(REACCIONES_MALAS)


class TestElAlmacen:
    def test_una_reaccion_nueva_reemplaza_y_quitarla_la_borra(self, tmp_path):
        notas = Calificaciones(tmp_path / "c.sqlite")
        notas.registrar_enviados("chat", "t1", ["m1", "m2"])
        assert notas.turno_de("m2", "chat") == "t1"
        notas.calificar("t1", "chat", BUENA, "👍")
        notas.calificar("t1", "chat", MALA, "👎")
        assert notas.de_turnos(["t1"])["t1"]["valor"] == MALA
        assert notas.cuantas() == {"buenas": 0, "malas": 1}
        notas.quitar("t1", "chat")
        assert notas.de_turnos(["t1"]) == {}

    def test_un_mensaje_de_otro_chat_no_se_puede_calificar(self):
        notas = Calificaciones()
        notas.registrar_enviados("chat-a", "t1", ["m1"])
        assert notas.turno_de("m1", "chat-b") is None

    def test_sobrevive_al_reinicio(self, tmp_path):
        Calificaciones(tmp_path / "c.sqlite").registrar_enviados("chat", "t1", ["m1"])
        assert Calificaciones(tmp_path / "c.sqlite").turno_de("m1", "chat") == "t1"


@pytest.fixture
def todo(tmp_path):
    diario = Bitacora(tmp_path / "bitacora")
    notas = Calificaciones()
    app = crear_app(_config(), lambda: AgenteFalso("160 909 unidades"), bitacora=diario,
                    calificaciones=notas)
    with TestClient(app) as cliente:
        yield cliente, diario, notas


def _preguntar_y_mandar(cliente, mensajes=("wamid-1", "wamid-2")):
    turno = cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "cuanto vendio Honor"}).json()["turno"]
    r = cliente.post("/enviados", json={"chat_id": AUTORIZADO, "turno": turno, "mensajes": list(mensajes)})
    assert r.status_code == 200
    return turno


def _reaccionar(cliente, mensaje, emoji, chat=AUTORIZADO):
    return cliente.post("/reaccion", json={"chat_id": chat, "mensaje_id": mensaje, "emoji": emoji})


class TestDeWhatsAppAlPanel:
    def test_un_pulgar_abajo_llega_al_panel_con_su_turno(self, todo):
        cliente, _, _ = todo
        turno = _preguntar_y_mandar(cliente)
        # Reaccionar al Excel (el segundo mensaje) tambien califica.
        r = _reaccionar(cliente, "wamid-2", "👎").json()
        assert r == {"calificado": True, "turno": turno, "valor": MALA}

        panel = cliente.get("/panel/calificaciones?valor=-1").json()
        assert panel["malas"] == 1 and panel["buenas"] == 0
        fila = panel["calificaciones"][0]
        assert fila["turno"]["pregunta"] == "cuanto vendio Honor"
        assert fila["turno"]["respuesta"] == "160 909 unidades"

    def test_la_conversacion_muestra_la_calificacion(self, todo):
        cliente, _, _ = todo
        turno = _preguntar_y_mandar(cliente)
        _reaccionar(cliente, "wamid-1", "👍")
        turnos = cliente.get(f"/panel/conversaciones?chat_id={AUTORIZADO}").json()["turnos"]
        assert turnos[0]["id"] == turno and turnos[0]["calificacion"]["valor"] == BUENA

    def test_quitar_la_reaccion_quita_la_calificacion(self, todo):
        cliente, _, notas = todo
        turno = _preguntar_y_mandar(cliente)
        _reaccionar(cliente, "wamid-1", "👎")
        _reaccionar(cliente, "wamid-1", "")
        assert notas.de_turnos([turno]) == {}

    def test_las_reacciones_ambiguas_no_califican(self, todo):
        cliente, _, notas = todo
        _preguntar_y_mandar(cliente)
        assert _reaccionar(cliente, "wamid-1", "😂").json()["calificado"] is False
        assert notas.cuantas() == {"buenas": 0, "malas": 0}

    def test_un_mensaje_del_bot_sin_registro_se_califica_igual(self, todo):
        # Visto el 7/10/2026: un 👍 a un Excel del 1 de octubre (antes de que
        # se registraran los mensajes) se perdia. Se guarda ligado al
        # mensaje; el panel lo muestra como "anterior al registro".
        cliente, _, notas = todo
        r = _reaccionar(cliente, "3EB05BCD3E3BAD87FECE08", "👍").json()
        assert r["calificado"] is True and r["turno"] == "sin-registro:3EB05BCD3E3BAD87FECE08"
        assert notas.cuantas() == {"buenas": 1, "malas": 0}
        lista = cliente.get("/panel/calificaciones").json()["calificaciones"]
        assert lista[0]["turno_id"].startswith("sin-registro:") and lista[0]["turno"] is None
        # Y quitar la reaccion la quita.
        _reaccionar(cliente, "3EB05BCD3E3BAD87FECE08", "")
        assert notas.cuantas() == {"buenas": 0, "malas": 0}

    def test_una_respuesta_de_error_tambien_trae_su_turno(self, tmp_path):
        class Falla(AgenteFalso):
            def responder(self, pregunta, cancelacion=None, cadenas=None):
                raise RuntimeError("algo se rompio")

        app = crear_app(_config(), lambda: Falla("x"), bitacora=Bitacora(tmp_path / "b"),
                        calificaciones=Calificaciones())
        with TestClient(app) as cliente:
            r = cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "hola"}).json()
        assert r["turno"]

    def test_un_numero_ajeno_no_califica(self, todo):
        cliente, _, _ = todo
        _preguntar_y_mandar(cliente)
        assert _reaccionar(cliente, "wamid-1", "👎", chat=NO_AUTORIZADO).status_code == 403


def test_un_turno_se_encuentra_por_su_id(tmp_path):
    diario = Bitacora(tmp_path)
    turno = diario.comenzar("chat", "pregunta")
    diario.terminar(turno, respuesta="respuesta")
    assert diario.buscar(turno.id)["respuesta"] == "respuesta"
    assert diario.buscar("20990101-000000-ffffff") is None
