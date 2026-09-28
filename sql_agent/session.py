"""Registro de sesiones: un agente con memoria propia por conversacion."""

import threading
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable


def dia_local(instante: float) -> date:
    """El dia calendario de ese instante, en la hora local de la maquina."""
    return date.fromtimestamp(instante)


@dataclass
class Sesion:
    agente: Any
    ultimo_uso: float
    lock: threading.Lock = field(default_factory=threading.Lock)
    # Se activa para pedirle a la consulta en curso que se rinda. No la mata:
    # corre en un hilo del threadpool y a un hilo no se le puede hacer nada
    # desde fuera. La consulta la mira entre paso y paso y se detiene sola.
    cancelacion: threading.Event = field(default_factory=threading.Event)


class RegistroSesiones:
    """Mapea chat_id a su sesion, con lock por chat y expiracion por dia.

    La memoria de una conversacion dura el DIA NATURAL: lo que se pregunto a
    las 9 de la manana se sigue recordando a las 6 de la tarde, y a
    medianoche se olvida todo y el dia siguiente empieza en blanco. Antes
    vencia tras 60 minutos sin mensajes, y quien volvia de comer tenia que
    repetir de que cadena y que periodo venia hablando.

    `ttl_segundos` queda como tope ADICIONAL por inactividad, apagado por
    defecto (None o 0). Si se pone, la sesion vence por lo que ocurra
    primero: la medianoche o ese rato sin mensajes.

    El lock es por conversacion, no global: dos personas distintas pueden
    consultar a la vez, pero una misma persona no dispara dos consultas en
    paralelo contra la base.
    """

    def __init__(
        self,
        fabrica: Callable[[], Any],
        ttl_segundos: float | None = None,
        # Reloj de PARED, no time.monotonic: para saber que cambio el dia hay
        # que saber que hora es, y el monotono solo sabe cuanto paso.
        reloj: Callable[[], float] = time.time,
        dia: Callable[[float], date] = dia_local,
    ):
        self._fabrica = fabrica
        self._ttl = ttl_segundos or None
        self._reloj = reloj
        self._dia = dia
        self._sesiones: dict[str, Sesion] = {}
        # Protege el diccionario, no la ejecucion del agente.
        self._candado = threading.Lock()

    def _cerrar_agente(self, agente: Any) -> None:
        """Cierra un agente perdedor en doble chequeo, defensivamente.

        En produccion, la fabrica construye SQLAgent que abre una conexion
        real a SQL Server al inicializarse. Si el agente pierde la carrera en
        doble chequeo, nadie va a usarlo y su engine quedaria con un pool vivo.
        Esta funcion cierra el engine si existe, envuelta en try/except para que
        un fallo al cerrar no tumbe la peticion que SI va a responder.
        """
        try:
            engine = getattr(agente, "engine", None)
            if engine is not None and hasattr(engine, "dispose"):
                engine.dispose()
        except Exception:
            # Si el cierre falla, ignorar. La peticion debe completar.
            pass

    def obtener(self, chat_id: str) -> Sesion:
        """Obtiene o crea una sesion para chat_id, con lock individual por chat.

        IMPORTANTE: El llamador debe tomar sesion.lock inmediatamente despues,
        sin trabajo intermedio. Si purgar() corre entre obtener() y lock.acquire(),
        la sesion podria ser eliminada y un segundo chat_id crearia otra, dejando
        dos consultas del mismo usuario en paralelo.

        purgar() no debe invocarse desde una tarea de fondo independiente sin
        revisar esta interaccion.
        """
        # Primer chequeo sin construir bajo candado (optimización).
        with self._candado:
            sesion = self._sesiones.get(chat_id)
            if sesion is not None:
                sesion.ultimo_uso = self._reloj()
                return sesion

        # Construir agente FUERA del candado para permitir paralelismo.
        agente = self._fabrica()
        ahora = self._reloj()

        # Segundo chequeo: doble chequeo para evitar duplicar agentes.
        with self._candado:
            # Si otro hilo insertó mientras nosotros construíamos, devuelve la suya.
            sesion = self._sesiones.get(chat_id)
            if sesion is not None:
                # Refrescar el timestamp de la existente.
                sesion.ultimo_uso = ahora
                # Cerrar el agente que construimos pero no vamos a usar.
                self._cerrar_agente(agente)
                return sesion
            # Inserta la nuestra por primera vez.
            sesion = Sesion(agente=agente, ultimo_uso=ahora)
            self._sesiones[chat_id] = sesion
            return sesion

    def existente(self, chat_id: str) -> Sesion | None:
        """Devuelve la sesion de un chat SOLO si ya existe, sin crearla.

        Crear una sesion construye un SQLAgent, que abre conexion a la base y
        arma el modelo. Quien solo necesita mirar de reojo lo que la sesion ya
        sabe -- por ejemplo, en que idioma venia hablando -- usa esto y no
        `obtener`, para no pagar esa construccion antes de tiempo.
        """
        with self._candado:
            return self._sesiones.get(chat_id)

    def chats(self) -> list[str]:
        """Los chats con conversacion viva, para mostrarlos en el panel."""
        with self._candado:
            return sorted(self._sesiones)

    def _vencida(self, sesion: Sesion, ahora: float) -> bool:
        """True si la sesion es de otro dia, o paso su tope de inactividad."""
        if self._dia(sesion.ultimo_uso) != self._dia(ahora):
            return True
        return self._ttl is not None and sesion.ultimo_uso <= ahora - self._ttl

    def purgar(self) -> int:
        """Elimina las sesiones vencidas. Devuelve cuantas se eliminaron.

        Se llama al principio de cada /ask, ANTES de `obtener`: el primer
        mensaje despues de medianoche encuentra la sesion de ayer todavia
        con su `ultimo_uso` de ayer, la tira, y `obtener` arma una nueva.
        """
        ahora = self._reloj()
        eliminadas = 0
        with self._candado:
            for chat_id, sesion in list(self._sesiones.items()):
                if not self._vencida(sesion, ahora):
                    continue
                # Una sesion con una consulta en curso no se toca.
                if not sesion.lock.acquire(blocking=False):
                    continue
                sesion.lock.release()
                del self._sesiones[chat_id]
                eliminadas += 1
        return eliminadas

    def __len__(self) -> int:
        with self._candado:
            return len(self._sesiones)
