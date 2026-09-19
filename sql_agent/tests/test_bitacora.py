"""Pruebas de la bitacora: lo que el bot hizo, y lo que esta haciendo."""

import json

import pytest
import bitacora as modulo
from bitacora import Bitacora, paso, turno_actual


@pytest.fixture(autouse=True)
def sin_turno_colgado():
    """El turno vive en un ContextVar y las pruebas comparten contexto.

    En produccion no hace falta: cada peticion corre en su propia copia del
    contexto (run_in_threadpool la copia), asi que un turno nunca se cuela de
    una peticion a la siguiente. Aqui si, porque todo corre en el mismo hilo.
    """
    modulo._turno_actual.set(None)
    yield
    modulo._turno_actual.set(None)


@pytest.fixture
def diario(tmp_path):
    return Bitacora(tmp_path / "bitacora")


class TestActividadEnCurso:
    def test_una_pregunta_en_vuelo_se_ve(self, diario):
        diario.comenzar("chat@s.whatsapp.net", "ventas de Elektra")
        en_curso = diario.en_curso()
        assert len(en_curso) == 1
        assert en_curso[0]["pregunta"] == "ventas de Elektra"
        assert en_curso[0]["estado"] == "en curso"

    def test_al_terminar_desaparece_de_lo_que_corre(self, diario):
        turno = diario.comenzar("chat@s.whatsapp.net", "ventas")
        diario.terminar(turno, respuesta="1,268,566 unidades", segundos=4.2)
        assert diario.en_curso() == []

    def test_los_pasos_se_ven_mientras_corre(self, diario):
        # Es el punto de todo esto: una consulta de tres minutos se ve igual
        # que un bot colgado si no se puede mirar en que paso va.
        diario.comenzar("chat@s.whatsapp.net", "ventas por tienda")
        paso("SQL: SELECT SUM([QTY]) FROM vw_ETL_Elektra", modulo.SQL)
        anotado = diario.en_curso()[0]["pasos"][0]
        assert "SELECT SUM" in anotado["texto"]
        assert anotado["tipo"] == "sql"
        assert anotado["hora"]

    def test_una_pregunta_cancelada_no_borra_a_la_que_la_reemplazo(self, diario):
        # La vieja termina DESPUES de que la nueva empezo (la cancelacion es
        # cooperativa). Borrando por chat_id a secas, la nueva desapareceria
        # de la vista justo cuando es la unica que esta corriendo.
        vieja = diario.comenzar("chat@s.whatsapp.net", "la lenta")
        nueva = diario.comenzar("chat@s.whatsapp.net", "la nueva")
        diario.terminar(vieja, estado="cancelado")

        en_curso = diario.en_curso()
        assert len(en_curso) == 1
        assert en_curso[0]["pregunta"] == "la nueva"
        assert nueva.estado == "en curso"

    def test_cada_chat_se_ve_por_separado(self, diario):
        diario.comenzar("uno@s.whatsapp.net", "ventas")
        diario.comenzar("dos@s.whatsapp.net", "precios")
        assert len(diario.en_curso()) == 2


class TestPasosFueraDeUnTurno:
    def test_al_terminar_deja_de_haber_turno_actual(self, diario):
        turno = diario.comenzar("chat@s.whatsapp.net", "algo")
        assert turno_actual() is turno
        diario.terminar(turno, respuesta="listo")
        assert turno_actual() is None

    def test_anotar_un_paso_sin_turno_no_revienta(self):
        # Las herramientas del agente llaman a paso() siempre, y tambien
        # corren desde la CLI y desde las pruebas, donde no hay turno.
        assert turno_actual() is None
        paso("esto no va a ninguna parte")

    def test_no_crece_sin_limite(self, diario):
        # Un agente atorado en un bucle de herramientas llenaria la memoria
        # y el panel con miles de renglones.
        diario.comenzar("chat@s.whatsapp.net", "algo")
        for i in range(200):
            paso(f"paso {i}")
        assert len(diario.en_curso()[0]["pasos"]) <= modulo.MAX_PASOS


class TestHistorial:
    def test_los_turnos_terminados_quedan_en_el_archivo(self, diario, tmp_path):
        turno = diario.comenzar("chat@s.whatsapp.net", "ventas totales")
        paso("SQL: SELECT 1", modulo.SQL)
        diario.terminar(turno, respuesta="son 10", idioma="es", segundos=2.0)

        archivos = list((tmp_path / "bitacora").glob("*.jsonl"))
        assert len(archivos) == 1
        guardado = json.loads(archivos[0].read_text(encoding="utf-8").strip())
        assert guardado["pregunta"] == "ventas totales"
        assert guardado["respuesta"] == "son 10"
        assert guardado["pasos"][0]["texto"] == "SQL: SELECT 1"

    def test_se_leen_del_mas_nuevo_al_mas_viejo(self, diario):
        for i in range(3):
            diario.terminar(diario.comenzar("chat@s.whatsapp.net", f"p{i}"), respuesta="r")
        assert [t["pregunta"] for t in diario.turnos()] == ["p2", "p1", "p0"]

    def test_se_puede_ver_un_solo_chat(self, diario):
        diario.terminar(diario.comenzar("uno@s.whatsapp.net", "de uno"), respuesta="r")
        diario.terminar(diario.comenzar("dos@s.whatsapp.net", "de dos"), respuesta="r")
        turnos = diario.turnos(chat_id="uno@s.whatsapp.net")
        assert [t["pregunta"] for t in turnos] == ["de uno"]

    def test_el_resumen_por_chat_cuenta_los_turnos(self, diario):
        for i in range(3):
            diario.terminar(diario.comenzar("uno@s.whatsapp.net", f"p{i}"), respuesta="r")
        diario.terminar(diario.comenzar("dos@s.whatsapp.net", "otra"), respuesta="r")

        chats = {c["chat_id"]: c for c in diario.chats()}
        assert chats["uno@s.whatsapp.net"]["turnos"] == 3
        assert chats["dos@s.whatsapp.net"]["turnos"] == 1

    def test_una_respuesta_enorme_se_recorta(self, diario):
        # El panel muestra turnos completos; un volcado de 200 KB lo vuelve
        # inservible y engorda el archivo sin aportar nada.
        turno = diario.comenzar("chat@s.whatsapp.net", "dame todo")
        diario.terminar(turno, respuesta="x" * 50_000)
        assert len(diario.turnos()[0]["respuesta"]) < 5_000

    def test_sin_carpeta_no_se_guarda_nada_y_no_falla(self):
        # Asi corren las pruebas de la API: actividad en vivo si, archivos no.
        diario = Bitacora()
        diario.terminar(diario.comenzar("chat@s.whatsapp.net", "hola"), respuesta="hey")
        assert diario.turnos() == []
        assert diario.en_curso() == []


class TestHistorialCompleto:
    def _llenar(self, diario, chat, cuantos, desde=0):
        for i in range(desde, desde + cuantos):
            diario.terminar(diario.comenzar(chat, f"pregunta {i}"), respuesta=f"r{i}")

    def test_se_pueden_pedir_los_turnos_anteriores(self, diario):
        # El panel trae 50 y ofrece "ver anteriores"; sin `salto` no habria
        # forma de llegar al turno 51 de una conversacion.
        chat = "uno@s.whatsapp.net"
        self._llenar(diario, chat, 120)

        primera = diario.turnos(limite=50, chat_id=chat)
        segunda = diario.turnos(limite=50, chat_id=chat, salto=50)

        assert [t["pregunta"] for t in primera][:2] == ["pregunta 119", "pregunta 118"]
        assert [t["pregunta"] for t in segunda][:2] == ["pregunta 69", "pregunta 68"]
        # Sin repetir ni saltarse ninguno entre una pagina y la siguiente.
        assert not set(t["inicio"] + t["pregunta"] for t in primera) & set(
            t["inicio"] + t["pregunta"] for t in segunda
        )

    def test_el_salto_cuenta_solo_los_del_chat_pedido(self, diario):
        # Si contara turnos de otras conversaciones, la segunda pagina se
        # saltaria turnos que el usuario nunca llego a ver.
        self._llenar(diario, "uno@s.whatsapp.net", 3)
        self._llenar(diario, "dos@s.whatsapp.net", 10)
        self._llenar(diario, "uno@s.whatsapp.net", 3, desde=100)

        segunda = diario.turnos(limite=2, chat_id="uno@s.whatsapp.net", salto=2)
        assert [t["pregunta"] for t in segunda] == ["pregunta 100", "pregunta 2"]

    def test_la_lista_ve_conversaciones_viejas(self, diario, tmp_path):
        # Antes la lista se armaba con los ultimos 2000 turnos y una
        # conversacion sin actividad reciente desaparecia, aunque sus datos
        # siguieran en disco.
        import json

        carpeta = tmp_path / "bitacora"
        carpeta.mkdir(parents=True, exist_ok=True)
        (carpeta / "2020-01-01.jsonl").write_text(
            json.dumps({
                "chat_id": "viejo@s.whatsapp.net", "pregunta": "de hace años",
                "inicio": "2020-01-01T10:00:00", "pasos": [], "respuesta": "r",
                "archivos": [], "idioma": "es", "segundos": 1, "estado": "listo",
            }) + "\n",
            encoding="utf-8",
        )
        self._llenar(diario, "nuevo@s.whatsapp.net", 60)

        chats = {c["chat_id"]: c for c in diario.chats()}
        assert "viejo@s.whatsapp.net" in chats
        assert chats["viejo@s.whatsapp.net"]["ultima_pregunta"] == "de hace años"
        assert chats["nuevo@s.whatsapp.net"]["turnos"] == 60

    def test_cuantos_cuenta_todos_los_turnos_del_chat(self, diario):
        self._llenar(diario, "uno@s.whatsapp.net", 7)
        self._llenar(diario, "dos@s.whatsapp.net", 2)
        assert diario.cuantos("uno@s.whatsapp.net") == 7
        assert diario.cuantos("nadie@s.whatsapp.net") == 0

    def test_el_resumen_se_recalcula_cuando_el_archivo_crece(self, diario):
        # El resumen por archivo se recuerda para no releer la historia en
        # cada refresco. Si no se invalidara al crecer el archivo de hoy, el
        # panel se quedaria con la cuenta vieja toda la jornada.
        chat = "uno@s.whatsapp.net"
        self._llenar(diario, chat, 2)
        assert diario.chats()[0]["turnos"] == 2

        self._llenar(diario, chat, 1, desde=50)
        assert diario.chats()[0]["turnos"] == 3
        assert diario.chats()[0]["ultima_pregunta"] == "pregunta 50"


class TestObservadorDelAgente:
    """El observador anota lo que el agente va haciendo, para verlo en vivo."""

    def _observador(self):
        from agent import Observador

        return Observador()

    def test_anota_cuando_le_pregunta_al_modelo(self, diario):
        diario.comenzar("chat@s.whatsapp.net", "ventas")
        self._observador().on_chat_model_start({}, [])

        anotado = diario.en_curso()[0]["pasos"]
        assert anotado[0]["tipo"] == "pensamiento"
        assert "modelo" in anotado[0]["texto"]

    def test_anota_lo_que_el_modelo_escribio(self, diario):
        # Con las herramientas de OpenAI el modelo suele contestar solo con
        # la llamada; cuando ademas escribe algo, eso es lo mas parecido a
        # su razonamiento y es lo que se queria ver.
        from langchain_core.outputs import Generation, LLMResult

        diario.comenzar("chat@s.whatsapp.net", "ventas")
        observador = self._observador()
        observador.on_chat_model_start({}, [])
        observador.on_llm_end(LLMResult(generations=[[Generation(text="Voy por Elektra")]]))

        assert "Voy por Elektra" in diario.en_curso()[0]["pasos"][-1]["texto"]

    def test_anota_que_herramienta_uso_y_con_que(self, diario):
        from langchain_core.agents import AgentAction

        diario.comenzar("chat@s.whatsapp.net", "ventas")
        self._observador().on_agent_action(
            AgentAction(tool="consultar_sql", tool_input={"query": "SELECT 1"}, log="")
        )

        anotado = diario.en_curso()[0]["pasos"][-1]
        assert anotado["tipo"] == "herramienta"
        assert "consultar_sql" in anotado["texto"] and "SELECT 1" in anotado["texto"]

    def test_anota_lo_que_le_devolvieron(self, diario):
        diario.comenzar("chat@s.whatsapp.net", "ventas")
        self._observador().on_tool_end("1,268,566 unidades")

        anotado = diario.en_curso()[0]["pasos"][-1]
        assert anotado["tipo"] == "resultado"
        assert "1,268,566" in anotado["texto"]

    def test_un_resultado_enorme_se_resume(self, diario):
        # Una consulta puede devolver miles de filas; el panel muestra el
        # razonamiento, no un volcado.
        diario.comenzar("chat@s.whatsapp.net", "ventas")
        self._observador().on_tool_end("fila\n" * 5000)

        assert len(diario.en_curso()[0]["pasos"][-1]["texto"]) < 1000

    def test_fuera_de_una_peticion_no_hace_nada(self):
        # La CLI y las pruebas usan el mismo agente sin turno abierto.
        self._observador().on_tool_end("lo que sea")
