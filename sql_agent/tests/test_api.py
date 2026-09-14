import threading

import pytest
from fastapi.testclient import TestClient

from api import crear_app
from config import Config

AUTORIZADO = "5215512345678@s.whatsapp.net"
NO_AUTORIZADO = "5215599999999@s.whatsapp.net"
GRUPO = "120363001234567890@g.us"


def _config(timeout=90):
    return Config(
        allowlist=frozenset({"5215512345678"}),
        timeout_segundos=timeout,
        ttl_minutos=60,
        api_host="127.0.0.1",
        api_port=8000,
    )


class AgenteFalso:
    def __init__(self, respuesta="listo", archivos=None, antes=None):
        self.respuesta = respuesta
        self.archivos = archivos or []
        self.antes = antes
        self.preguntas = []

    def responder(self, pregunta):
        self.preguntas.append(pregunta)
        if self.antes is not None:
            self.antes()
        return self.respuesta, list(self.archivos)


@pytest.fixture
def agente():
    return AgenteFalso()


@pytest.fixture
def cliente(agente):
    app = crear_app(_config(), lambda: agente)
    with TestClient(app) as c:
        yield c


class TestAutorizacion:
    def test_numero_autorizado_recibe_respuesta(self, cliente, agente):
        r = cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "hola"})
        assert r.status_code == 200
        assert r.json() == {"reply": "listo", "files": []}
        assert agente.preguntas == ["hola"]

    def test_numero_no_autorizado_recibe_403(self, cliente, agente):
        r = cliente.post("/ask", json={"chat_id": NO_AUTORIZADO, "text": "hola"})
        assert r.status_code == 403
        assert agente.preguntas == []

    def test_grupo_recibe_403(self, cliente, agente):
        r = cliente.post("/ask", json={"chat_id": GRUPO, "text": "hola"})
        assert r.status_code == 403
        assert agente.preguntas == []


class TestRespuesta:
    def test_devuelve_los_archivos_generados(self):
        agente = AgenteFalso(respuesta="ahi va", archivos=["/tmp/reporte.xlsx"])
        app = crear_app(_config(), lambda: agente)
        with TestClient(app) as cliente:
            r = cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "dame todo"})
        assert r.json()["files"] == ["/tmp/reporte.xlsx"]

    def test_un_error_del_agente_se_explica_sin_stack_trace(self):
        class AgenteQueFalla:
            def responder(self, pregunta):
                raise RuntimeError("ODBC Driver 18: login failed for user 'sa'")

        app = crear_app(_config(), AgenteQueFalla)
        with TestClient(app) as cliente:
            r = cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "hola"})
        assert r.status_code == 200
        cuerpo = r.json()["reply"]
        assert "login failed" not in cuerpo
        assert "Traceback" not in cuerpo


class TestConcurrencia:
    # TestClient corre la app en un event loop de fondo, asi que la unica forma
    # de tener dos peticiones solapadas es lanzar la primera en otro hilo.
    def test_segunda_consulta_del_mismo_chat_recibe_429(self):
        soltar = threading.Event()
        agente = AgenteFalso(antes=soltar.wait)
        app = crear_app(_config(), lambda: agente)

        with TestClient(app) as cliente:
            resultados = []
            primera = threading.Thread(
                target=lambda: resultados.append(
                    cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "una"})
                )
            )
            primera.start()
            # Espera a que la primera consulta este dentro del agente.
            for _ in range(200):
                if agente.preguntas:
                    break
                threading.Event().wait(0.01)

            segunda = cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "dos"})
            assert segunda.status_code == 429

            soltar.set()
            primera.join(timeout=5)
            assert resultados[0].status_code == 200

    def test_timeout_devuelve_504(self):
        soltar = threading.Event()
        agente = AgenteFalso(antes=lambda: soltar.wait(timeout=5))
        app = crear_app(_config(timeout=0.2), lambda: agente)
        with TestClient(app) as cliente:
            r = cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "lenta"})
            assert r.status_code == 504

            soltar.set()

            # El hilo de fondo sigue corriendo tras el 504 (asyncio solo
            # cancela la espera, no el hilo). Si el lock no se soltara al
            # terminar, esta segunda consulta del mismo chat se quedaria en
            # 429 para siempre.
            segunda = None
            for _ in range(500):
                segunda = cliente.post(
                    "/ask", json={"chat_id": AUTORIZADO, "text": "otra"}
                )
                if segunda.status_code != 429:
                    break
                threading.Event().wait(0.01)
            assert segunda.status_code == 200


class TestHealth:
    def test_health_responde(self, cliente):
        r = cliente.get("/health")
        assert r.status_code == 200
        cuerpo = r.json()
        assert cuerpo["status"] == "ok"
        assert cuerpo["sesiones"] == 0
