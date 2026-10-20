"""Los reportes grandes, armados aparte y entregados al acabar (19/10/2026)."""

import threading

from fastapi.testclient import TestClient

import idioma as idiomas
import segundo_plano as sp
from api import crear_app
from bitacora import Bitacora
from tests.test_api import AgenteFalso, _config
from usuarios import RegistroUsuarios

CHAT = "5215512345678@s.whatsapp.net"


class Reloj:
    def __init__(self):
        self.ahora = 0.0

    def __call__(self):
        return self.ahora


def test_lo_que_acaba_queda_para_entregar_una_vez_y_se_confirma():
    reloj = Reloj()
    t = sp.Trabajos(reloj)
    t.lanzar(CHAT, "turno1", "es", lambda c: ("Listo", ["a.xlsx"]), str, esperar=True)
    [entrega] = t.pendientes()
    assert (entrega.chat_id, entrega.turno, entrega.texto, entrega.archivos) == (CHAT, "turno1", "Listo", ["a.xlsx"])
    # El sidecar pregunta cada 5 s: no se la lleva dos veces...
    assert t.pendientes() == []
    # ...salvo que no la confirme (se cayo a media entrega).
    reloj.ahora += sp.REINTENTAR_ENTREGA_S + 1
    assert [e.id for e in t.pendientes()] == [entrega.id]
    assert t.confirmar(entrega.id) and t.pendientes() == []


def test_si_falla_se_le_dice_al_usuario():
    t = sp.Trabajos()

    def trabajo(cancelacion):
        raise RuntimeError("la base no contesta")

    t.lanzar(CHAT, "turno1", "es", trabajo, lambda e: f"No pude ({e})", esperar=True)
    [entrega] = t.pendientes()
    assert entrega.texto == "No pude (la base no contesta)" and entrega.archivos == []


def test_cancelado_no_se_entrega_nada():
    t = sp.Trabajos()
    empezo, seguir = threading.Event(), threading.Event()

    def trabajo(cancelacion):
        empezo.set()
        seguir.wait(5)
        raise RuntimeError("cancelada") if cancelacion.is_set() else None

    t.lanzar(CHAT, "turno1", "es", trabajo, str)
    empezo.wait(5)
    assert t.en_curso(CHAT) == 1
    assert t.cancelar(CHAT) and not t.cancelar("otro@s.whatsapp.net")
    seguir.set()
    for _ in range(50):
        if t.en_curso(CHAT) == 0:
            break
        threading.Event().wait(0.05)
    assert t.en_curso(CHAT) == 0 and t.pendientes() == []


def _app(tmp_path):
    usuarios = RegistroUsuarios(tmp_path / "u.json", semilla=["5215512345678"])
    return crear_app(_config(), lambda: AgenteFalso("ok"), usuarios, Bitacora(tmp_path / "b"))


def test_las_rutas_del_sidecar(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as cliente:
        assert cliente.get("/entregas").json() == {"entregas": []}
        app.state.trabajos.lanzar(CHAT, "t1", "es", lambda c: ("Listo", ["a.xlsx"]), str, esperar=True)
        [entrega] = cliente.get("/entregas").json()["entregas"]
        assert entrega["texto"] == "Listo" and entrega["archivos"] == ["a.xlsx"] and entrega["turno"] == "t1"
        assert cliente.post("/entregas/confirmar", json={"id": entrega["id"]}).json() == {"confirmada": True}


def test_cancela_detiene_un_reporte_que_se_arma_aparte(tmp_path):
    app = _app(tmp_path)
    seguir = threading.Event()
    with TestClient(app) as cliente:
        app.state.trabajos.lanzar(CHAT, "t1", "es", lambda c: (seguir.wait(5), ("x", []))[1], str)
        respuesta = cliente.post("/ask", json={"chat_id": CHAT, "text": "cancela"}).json()
        seguir.set()
        assert respuesta["reply"] == idiomas.texto("cancelado", "es")
        for _ in range(100):
            if app.state.trabajos.en_curso(CHAT) == 0:
                break
            threading.Event().wait(0.05)
        # Sin nada en curso, "cancela" dice que no hay nada que cancelar.
        respuesta = cliente.post("/ask", json={"chat_id": CHAT, "text": "cancela"}).json()
        assert respuesta["reply"] == idiomas.texto("nada_que_cancelar", "es")
