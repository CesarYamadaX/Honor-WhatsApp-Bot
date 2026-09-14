"""Registro de sesiones: un agente con memoria propia por conversacion."""

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Sesion:
    agente: Any
    ultimo_uso: float
    lock: threading.Lock = field(default_factory=threading.Lock)


class RegistroSesiones:
    """Mapea chat_id a su sesion, con lock por chat y expiracion por TTL.

    El lock es por conversacion, no global: dos personas distintas pueden
    consultar a la vez, pero una misma persona no dispara dos consultas en
    paralelo contra la base.
    """

    def __init__(
        self,
        fabrica: Callable[[], Any],
        ttl_segundos: float,
        reloj: Callable[[], float] = time.monotonic,
    ):
        self._fabrica = fabrica
        self._ttl = ttl_segundos
        self._reloj = reloj
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

    def purgar(self) -> int:
        """Elimina las sesiones inactivas. Devuelve cuantas se eliminaron."""
        limite = self._reloj() - self._ttl
        eliminadas = 0
        with self._candado:
            for chat_id, sesion in list(self._sesiones.items()):
                if sesion.ultimo_uso > limite:
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
