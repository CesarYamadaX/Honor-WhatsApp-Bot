import threading

import pytest
from fastapi.testclient import TestClient

from api import crear_app
from config import Config

AUTORIZADO = "5215512345678@s.whatsapp.net"
NO_AUTORIZADO = "5215599999999@s.whatsapp.net"
GRUPO = "120363001234567890@g.us"


def _config(timeout=90, espera_cancelacion=0.2):
    # `espera_cancelacion` corto por defecto: con los 25s de produccion, cada
    # prueba que topa con una sesion ocupada tardaria 25 segundos.
    return Config(
        allowlist=frozenset({"5215512345678"}),
        timeout_segundos=timeout,
        ttl_minutos=60,
        api_host="127.0.0.1",
        api_port=8000,
        espera_cancelacion=espera_cancelacion,
    )


class AgenteFalso:
    def __init__(self, respuesta="listo", archivos=None, antes=None):
        self.respuesta = respuesta
        self.archivos = archivos or []
        self.antes = antes
        self.preguntas = []

    def responder(self, pregunta, cancelacion=None):
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
            def responder(self, pregunta, cancelacion=None):
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


class TestAutorizado:
    """El sidecar pregunta esto ANTES de correr nada.

    Sin este endpoint no podia mostrar "escribiendo" ni el aviso de espera:
    la unica forma de saber si un chat estaba autorizado era mandar la
    pregunta entera y aguardar, y para entonces la consulta ya habia
    terminado. Mostrarle cualquier señal de vida a un numero ajeno confirma
    que hay un bot del otro lado.
    """

    def test_autorizado_responde_ok(self, cliente):
        r = cliente.post("/authorized", json={"chat_id": AUTORIZADO})
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_no_autorizado_responde_403(self, cliente, agente):
        r = cliente.post("/authorized", json={"chat_id": NO_AUTORIZADO})
        assert r.status_code == 403
        assert agente.preguntas == []

    def test_grupo_responde_403(self, cliente):
        assert cliente.post("/authorized", json={"chat_id": GRUPO}).status_code == 403


class TestCancelacion:
    """Un mensaje nuevo cancela el anterior en vez de chocar con un 429.

    En los logs de produccion se veia el patron: una consulta larga, el
    usuario reenvia porque no le llega nada, y el reenvio rebota con 429
    mientras la primera sigue corriendo hasta agotar los 90s.
    """

    def _app_ocupada(self, respuesta_larga):
        """Un agente que se queda esperando hasta que se le cancele."""
        agente = AgenteFalso(respuesta=respuesta_larga)
        return crear_app(_config(), lambda: agente), agente

    def test_una_orden_de_cancelar_sin_nada_en_curso(self, cliente, agente):
        r = cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "cancela"})
        assert r.status_code == 200
        assert "No tenias" in r.json()["reply"]
        assert agente.preguntas == [], "no debe llegar al agente como pregunta"

    @pytest.mark.parametrize(
        "texto", ["cancela", "Cancelar", "  CANCELA. ", "cancélalo", "ya no"]
    )
    def test_formas_de_pedir_cancelacion(self, texto):
        from api import es_cancelacion

        assert es_cancelacion(texto) is True

    @pytest.mark.parametrize(
        "texto",
        [
            "cuantas ventas se cancelaron en marzo",
            "dame las cancelaciones de Coppel",
            "ventas de honor",
        ],
    )
    def test_una_pregunta_que_menciona_cancelar_no_es_una_cancelacion(self, texto):
        # Se compara el mensaje entero, no si contiene la palabra: si no,
        # preguntar por cancelaciones cancelaria la consulta.
        from api import es_cancelacion

        assert es_cancelacion(texto) is False

    def test_el_agente_recibe_el_evento_de_cancelacion(self, cliente, agente):
        # Es lo que le permite rendirse: sin el evento, la consulta corre
        # hasta el final pase lo que pase.
        vistos = []

        def responder(pregunta, cancelacion=None):
            vistos.append(cancelacion)
            return "listo", []

        agente.responder = responder
        cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "hola"})

        assert len(vistos) == 1
        assert isinstance(vistos[0], threading.Event)

    def test_la_consulta_cancelada_no_le_contesta_al_usuario(self, agente):
        # Devuelve 409 y el sidecar calla: a esa pregunta ya la reemplazo otra,
        # que es la que va a responder. Mandar ademas un aviso por la vieja
        # seria ruido.
        from agent import ConsultaCancelada

        def responder(pregunta, cancelacion=None):
            raise ConsultaCancelada()

        agente.responder = responder
        app = crear_app(_config(), lambda: agente)
        with TestClient(app) as cliente:
            r = cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "hola"})

        assert r.status_code == 409

    def test_si_la_anterior_no_suelta_a_tiempo_sigue_el_429(self, agente):
        # La cancelacion es cooperativa: si la consulta vieja esta atorada en
        # una llamada al modelo, no hay forma de interrumpirla. Pasado el
        # plazo se responde como antes en vez de dejar esperando al usuario.
        atorado = threading.Event()

        def responder(pregunta, cancelacion=None):
            atorado.wait(timeout=5)
            return "listo", []

        agente.responder = responder
        app = crear_app(_config(espera_cancelacion=0.2), lambda: agente)
        with TestClient(app) as cliente:
            primera = threading.Thread(
                target=lambda: cliente.post(
                    "/ask", json={"chat_id": AUTORIZADO, "text": "larga"}
                )
            )
            primera.start()
            threading.Event().wait(0.3)  # deja que tome el lock
            r = cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "otra"})
            atorado.set()
            primera.join(timeout=10)

        assert r.status_code == 429
