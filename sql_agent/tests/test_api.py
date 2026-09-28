import threading

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

import api
import idioma as idiomas
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
        assert r.json() == {"reply": "listo", "files": [], "idioma": "es"}
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

    def test_si_el_proveedor_del_modelo_falla_no_se_culpa_a_la_base(self):
        # Paso en produccion: se acabo el presupuesto de la cuenta y el bot
        # le contestaba a todos "problema al consultar la base de datos".
        # La pregunta ni siquiera llega al SQL, y ese mensaje manda a buscar
        # el problema donde no esta -- ademas de invitar a reintentar algo
        # que va a fallar igual hasta que alguien toque la cuenta.
        class AgenteSinCuota:
            def responder(self, pregunta, cancelacion=None):
                raise openai.PermissionDeniedError(
                    message="Budget limit exceeded (monthly limit).",
                    response=httpx.Response(
                        403, request=httpx.Request("POST", "http://x")
                    ),
                    body=None,
                )

        app = crear_app(_config(), AgenteSinCuota)
        with TestClient(app) as cliente:
            r = cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "hola"})

        cuerpo = r.json()["reply"]
        assert cuerpo == idiomas.texto("servicio_caido", idiomas.ESPANOL)
        assert "base de datos" not in cuerpo
        assert "lauragabrielacastellanos" in cuerpo
        assert "Budget limit" not in cuerpo

    def test_el_error_que_langchain_reenvuelve_tambien_se_reconoce(self):
        # langchain_openai no propaga el error de openai tal cual: lo vuelve
        # a levantar como una clase suya con `raise ... from e`. Si solo se
        # mirara la excepcion de hasta arriba, el caso real no se detecta.
        from langchain_openai.chat_models.base import OpenAIPermissionDeniedError

        respuesta = httpx.Response(403, request=httpx.Request("POST", "http://x"))
        original = openai.PermissionDeniedError(
            message="Budget limit exceeded", response=respuesta, body=None
        )
        try:
            try:
                raise original
            except openai.PermissionDeniedError as e:
                raise OpenAIPermissionDeniedError(
                    message=e.message, response=respuesta, body=None
                ) from e
        except Exception as envuelto:
            assert api.es_falla_del_servicio(envuelto)

    def test_una_falla_de_la_base_sigue_dando_el_mensaje_de_siempre(self):
        # El mensaje del servicio caido es para el proveedor del modelo. Un
        # timeout de SQL si se puede arreglar reformulando o reintentando,
        # y no hay nada que pedirle al equipo de GTM.
        class AgenteQueFalla:
            def responder(self, pregunta, cancelacion=None):
                raise RuntimeError("timeout expired")

        app = crear_app(_config(), AgenteQueFalla)
        with TestClient(app) as cliente:
            r = cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "hola"})

        assert r.json()["reply"] == idiomas.texto("error_interno", idiomas.ESPANOL)

    def test_un_400_del_modelo_no_es_falla_de_cuenta(self):
        # Un prompt demasiado largo llega como 400 desde el mismo modulo.
        # Eso es culpa de la consulta, no de la cuenta: mandar al usuario a
        # hablar con el equipo de GTM seria un falso positivo.
        from langchain_openai.chat_models.base import OpenAIContextOverflowError

        error = OpenAIContextOverflowError(
            message="too many tokens",
            response=httpx.Response(400, request=httpx.Request("POST", "http://x")),
            body=None,
        )
        assert not api.es_falla_del_servicio(error)


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


class TestIdiomaDelChat:
    def test_sin_sesion_previa_contesta_el_idioma_por_defecto(self, cliente):
        r = cliente.post("/idioma", json={"chat_id": AUTORIZADO})
        assert r.status_code == 200
        assert r.json() == {"idioma": idiomas.POR_DEFECTO}

    def test_devuelve_el_idioma_que_dedujo_la_sesion(self, cliente, agente):
        # Para esto existe el endpoint: el aviso de espera del sidecar sale a
        # los 12s, cuando todavia no hay respuesta de donde sacar el idioma,
        # pero la sesion ya lo dedujo de la pregunta que esta corriendo.
        agente.idioma = "en"
        cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "sales by store"})

        r = cliente.post("/idioma", json={"chat_id": AUTORIZADO})
        assert r.json() == {"idioma": "en"}

    def test_no_consulta_el_idioma_de_un_numero_ajeno(self, cliente):
        r = cliente.post("/idioma", json={"chat_id": NO_AUTORIZADO})
        assert r.status_code == 403

    def test_no_crea_la_sesion_solo_por_preguntar(self, cliente):
        # Crear una sesion construye un SQLAgent, que abre conexion a la base
        # y arma el modelo. Un detalle de presentacion no puede pagar eso.
        llamadas = []
        app = crear_app(_config(), lambda: llamadas.append(1) or AgenteFalso())
        with TestClient(app) as c:
            c.post("/idioma", json={"chat_id": AUTORIZADO})
        assert llamadas == []


class TestMensajesEnElIdiomaDelUsuario:
    def test_el_error_sale_en_ingles_si_la_pregunta_venia_en_ingles(self):
        class AgenteQueFalla:
            idioma = "en"

            def responder(self, pregunta, cancelacion=None):
                raise RuntimeError("timeout expired")

        app = crear_app(_config(), AgenteQueFalla)
        with TestClient(app) as cliente:
            r = cliente.post(
                "/ask",
                json={"chat_id": AUTORIZADO, "text": "how many units did Coppel sell"},
            )

        assert r.json()["reply"] == idiomas.texto("error_interno", "en")
        assert r.json()["idioma"] == "en"

    def test_el_aviso_de_cancelacion_sale_en_el_idioma_de_la_orden(self, cliente):
        r = cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "cancel"})
        assert r.json()["reply"] == idiomas.texto(
            "nada_que_cancelar", idiomas.POR_DEFECTO
        )

        r = cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "取消"})
        assert r.json()["reply"] == idiomas.texto("nada_que_cancelar", "zh")

    def test_cancelar_se_entiende_en_los_tres_idiomas(self):
        # Si "cancel" o "取消" no se reconocieran como orden, se irian al
        # agente como si fueran preguntas de negocio.
        for orden in ["cancela", "cancel", "stop", "取消", "算了"]:
            assert api.es_cancelacion(orden), orden
        # Y lo de siempre: una pregunta que habla de cancelaciones no lo es.
        assert not api.es_cancelacion("cuantas ventas se cancelaron en marzo")

    def test_la_respuesta_dice_en_que_idioma_contesto(self, cliente, agente):
        # El sidecar lo necesita para sus propios avisos posteriores (archivo
        # ilegible, "no tengo respuesta").
        agente.idioma = "zh"
        r = cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "总销量"})
        assert r.json()["idioma"] == "zh"


# El mensaje real que devolvio el driver el 28 de septiembre de 2026, cuando
# DESKTOP-GOE584A dejo de resolverse desde la maquina del bot.
_SIN_CONEXION = (
    "('08001', '[08001] [Microsoft][ODBC Driver 18 for SQL Server]Named Pipes "
    "Provider: Could not open a connection to SQL Server [53]. (53) "
    "(SQLDriverConnect); [08001] [Microsoft][ODBC Driver 18 for SQL Server]"
    "Login timeout expired (0)')"
)


class ErrorDeODBC(Exception):
    """Hace las veces de pyodbc.OperationalError: lo que importa es el texto."""


class TestBaseDeDatosCaida:
    """Con la base caida, cada pregunta tardaba ~18s y devolvia un 500 que el
    sidecar traducia a "no pude procesar tu pregunta", o un "reformula la
    pregunta". Las dos mandan a buscar el problema en la pregunta, y ahi no
    esta: reformular no sirve de nada.
    """

    def _cliente(self, fabrica):
        return TestClient(crear_app(_config(), fabrica))

    def test_si_no_se_puede_abrir_la_conversacion_lo_dice(self):
        # Abrir la conversacion construye un SQLAgent, que se conecta a la
        # base. Ahi era donde salia el 500.
        def fabrica():
            raise ErrorDeODBC(_SIN_CONEXION)

        with self._cliente(fabrica) as c:
            r = c.post("/ask", json={"chat_id": AUTORIZADO, "text": "ventas de agosto"})

        assert r.status_code == 200
        assert r.json()["reply"] == idiomas.texto("base_no_disponible", "es")

    def test_contesta_en_el_idioma_de_la_pregunta(self):
        # Todavia no hay sesion de donde sacar el idioma: sale de la pregunta.
        def fabrica():
            raise ErrorDeODBC(_SIN_CONEXION)

        with self._cliente(fabrica) as c:
            r = c.post(
                "/ask",
                json={"chat_id": AUTORIZADO, "text": "how many units did Honor sell"},
            )

        assert r.json()["idioma"] == "en"
        assert r.json()["reply"] == idiomas.texto("base_no_disponible", "en")

    def test_si_se_cae_a_media_conversacion_tambien(self):
        agente = AgenteFalso(antes=lambda: (_ for _ in ()).throw(ErrorDeODBC(_SIN_CONEXION)))
        with self._cliente(lambda: agente) as c:
            r = c.post("/ask", json={"chat_id": AUTORIZADO, "text": "ventas"})

        assert r.json()["reply"] == idiomas.texto("base_no_disponible", "es")

    def test_un_error_de_la_consulta_no_se_confunde_con_la_base_caida(self):
        agente = AgenteFalso(
            antes=lambda: (_ for _ in ()).throw(
                ErrorDeODBC("('42S22', \"Invalid column name 'QTY'.\")")
            )
        )
        with self._cliente(lambda: agente) as c:
            r = c.post("/ask", json={"chat_id": AUTORIZADO, "text": "ventas"})

        assert r.json()["reply"] == idiomas.texto("error_interno", "es")

    def test_un_fallo_raro_al_abrir_sigue_siendo_un_error_de_verdad(self):
        # No se disfraza cualquier cosa de "la base esta caida": solo la
        # falta de conexion. Lo demas tiene que seguir viendose como fallo.
        def fabrica():
            raise RuntimeError("otra cosa")

        with TestClient(crear_app(_config(), fabrica), raise_server_exceptions=False) as c:
            r = c.post("/ask", json={"chat_id": AUTORIZADO, "text": "ventas"})

        assert r.status_code == 500

    def test_se_reconoce_aunque_venga_envuelto(self):
        # sqlalchemy reenvuelve el error de pyodbc en uno suyo.
        try:
            try:
                raise ErrorDeODBC(_SIN_CONEXION)
            except ErrorDeODBC as interno:
                raise RuntimeError("(sqlalchemy) algo") from interno
        except RuntimeError as envuelto:
            assert api.es_falla_de_conexion(envuelto)

        assert not api.es_falla_de_conexion(RuntimeError("42S22 columna"))
        assert not api.es_falla_de_conexion(None)
