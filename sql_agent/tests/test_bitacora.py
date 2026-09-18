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
        paso("SQL: SELECT SUM([QTY]) FROM vw_ETL_Elektra")
        assert "SELECT SUM" in diario.en_curso()[0]["pasos"][0]

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
        assert len(diario.en_curso()[0]["pasos"]) <= 40


class TestHistorial:
    def test_los_turnos_terminados_quedan_en_el_archivo(self, diario, tmp_path):
        turno = diario.comenzar("chat@s.whatsapp.net", "ventas totales")
        paso("SQL: SELECT 1")
        diario.terminar(turno, respuesta="son 10", idioma="es", segundos=2.0)

        archivos = list((tmp_path / "bitacora").glob("*.jsonl"))
        assert len(archivos) == 1
        guardado = json.loads(archivos[0].read_text(encoding="utf-8").strip())
        assert guardado["pregunta"] == "ventas totales"
        assert guardado["respuesta"] == "son 10"
        assert guardado["pasos"][0].endswith("SQL: SELECT 1")

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
