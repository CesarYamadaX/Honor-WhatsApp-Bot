import threading
import time

import pytest

from session import RegistroSesiones


class RelojFalso:
    def __init__(self):
        self.ahora = 1000.0

    def __call__(self):
        return self.ahora

    def avanzar(self, segundos):
        self.ahora += segundos


class AgenteFalso:
    contador = 0

    def __init__(self):
        AgenteFalso.contador += 1
        self.id = AgenteFalso.contador


@pytest.fixture(autouse=True)
def reiniciar_contador():
    AgenteFalso.contador = 0


class TestRegistroSesiones:
    def test_el_mismo_chat_reusa_su_agente(self):
        registro = RegistroSesiones(AgenteFalso, ttl_segundos=60)
        primera = registro.obtener("chat-a")
        segunda = registro.obtener("chat-a")
        assert primera is segunda
        assert AgenteFalso.contador == 1

    def test_chats_distintos_tienen_agentes_distintos(self):
        registro = RegistroSesiones(AgenteFalso, ttl_segundos=60)
        assert registro.obtener("chat-a").agente is not registro.obtener("chat-b").agente
        assert AgenteFalso.contador == 2

    def test_obtener_refresca_el_ultimo_uso(self):
        reloj = RelojFalso()
        registro = RegistroSesiones(AgenteFalso, ttl_segundos=60, reloj=reloj)
        registro.obtener("chat-a")
        reloj.avanzar(50)
        registro.obtener("chat-a")
        reloj.avanzar(50)
        assert registro.purgar() == 0
        assert len(registro) == 1

    def test_purga_las_sesiones_vencidas(self):
        reloj = RelojFalso()
        registro = RegistroSesiones(AgenteFalso, ttl_segundos=60, reloj=reloj)
        registro.obtener("chat-a")
        reloj.avanzar(61)
        assert registro.purgar() == 1
        assert len(registro) == 0

    def test_no_purga_una_sesion_ocupada(self):
        reloj = RelojFalso()
        registro = RegistroSesiones(AgenteFalso, ttl_segundos=60, reloj=reloj)
        sesion = registro.obtener("chat-a")
        sesion.lock.acquire()
        try:
            reloj.avanzar(61)
            assert registro.purgar() == 0
        finally:
            sesion.lock.release()

    def test_el_lock_es_por_chat(self):
        registro = RegistroSesiones(AgenteFalso, ttl_segundos=60)
        a = registro.obtener("chat-a")
        b = registro.obtener("chat-b")
        assert a.lock.acquire(blocking=False) is True
        try:
            # Otro chat no queda bloqueado por el primero.
            assert b.lock.acquire(blocking=False) is True
            b.lock.release()
            # Pero el mismo chat si.
            assert a.lock.acquire(blocking=False) is False
        finally:
            a.lock.release()

    def test_crear_sesiones_en_paralelo_no_duplica_agentes(self):
        registro = RegistroSesiones(AgenteFalso, ttl_segundos=60)
        listos = []
        arranque = threading.Barrier(8)

        def pedir():
            arranque.wait()
            listos.append(registro.obtener("chat-a"))

        hilos = [threading.Thread(target=pedir) for _ in range(8)]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join()

        assert len({id(s) for s in listos}) == 1
        assert AgenteFalso.contador == 1

    def test_chats_distintos_no_se_bloquean_por_fabrica_lenta(self):
        """Chats distintos deben crearse en paralelo, no secuencial.

        Con fábrica lenta (0.3s), dos chats distintos pidiendo en paralelo deben
        tardar ~0.3s total (no 0.6s). Comprueba que no hay un candado global
        bloqueando toda creación de sesión.
        """
        def fabrica_lenta():
            time.sleep(0.3)
            return AgenteFalso()

        registro = RegistroSesiones(fabrica_lenta, ttl_segundos=60)
        resultados = []
        arranque = threading.Barrier(2)
        inicio = time.time()

        def pedir(chat_id):
            arranque.wait()
            resultados.append((chat_id, registro.obtener(chat_id)))

        hilos = [
            threading.Thread(target=pedir, args=("chat-x",)),
            threading.Thread(target=pedir, args=("chat-y",)),
        ]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join()

        duracion = time.time() - inicio

        # Dos chats distintos, fábrica 0.3s cada uno:
        # - Si corre en paralelo: ~0.3s
        # - Si está bloqueado: ~0.6s
        # Tolerancia: 0.5s (0.3 + margen para overhead)
        assert duracion < 0.5, f"Tardó {duracion:.2f}s, esperaba <0.5s (señal de bloqueo)"
        assert len(resultados) == 2
        assert AgenteFalso.contador == 2

    def test_cierra_el_agente_descartado_en_doble_chequeo(self):
        """El agente que pierde la carrera en doble chequeo es cerrado defensivamente.

        Cuando dos hilos piden el MISMO chat_id nuevo simultáneamente, ambos
        construyen un agente. Uno pierde la carrera y debe ser cerrado para
        liberar su engine y conexiones a SQL Server. Solo el ganador queda
        registrado (sin cerrar).
        """
        class MotorFalso:
            def __init__(self):
                self.dispose_llamadas = []

            def dispose(self):
                self.dispose_llamadas.append(True)

        class AgenteFalsoConMotor:
            def __init__(self, motor):
                self.motor = motor
                self.engine = motor

        motores = []

        def fabrica():
            motor = MotorFalso()
            motores.append(motor)
            # Pequeño sleep para forzar que ambos hilos construyan en paralelo
            # si el primer chequeo es superado por ambos.
            time.sleep(0.05)
            return AgenteFalsoConMotor(motor)

        registro = RegistroSesiones(fabrica, ttl_segundos=60)
        sesiones = []
        arranque = threading.Barrier(2)

        def pedir():
            arranque.wait()
            sesiones.append(registro.obtener("chat-mismo"))

        hilos = [threading.Thread(target=pedir) for _ in range(2)]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join()

        # Los dos hilos deben haber construido un agente cada uno.
        assert len(motores) == 2, f"Esperaba 2 motores, obtuvo: {len(motores)}"
        # Las dos sesiones devueltas deben ser la MISMA (mismo agente).
        assert sesiones[0] is sesiones[1]

        # Exactamente uno de los motores debe tener dispose() llamado:
        # el del agente descartado.
        dispose_conteos = [len(m.dispose_llamadas) for m in motores]
        assert dispose_conteos.count(1) == 1, f"Esperaba un motor cerrado, obtuvo: {dispose_conteos}"
        assert dispose_conteos.count(0) == 1, f"Esperaba un motor abierto, obtuvo: {dispose_conteos}"

        # El agente que quedó registrado NO debe estar cerrado.
        agente_registrado = sesiones[0].agente
        assert agente_registrado.engine.dispose_llamadas == []
