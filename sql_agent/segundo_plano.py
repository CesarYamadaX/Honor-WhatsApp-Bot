"""Los reportes grandes, armados aparte y entregados al terminar.

Pedido el 19/10/2026: que los archivos grandes ya no tengan limite de tiempo,
y que al usuario se le diga que va a tardar y cuanto, mas o menos. Antes todo
vivia dentro de la peticion de WhatsApp (10 minutos): el reporte de precios
de ese dia se corto a los 507 s, y su respuesta salio 11 s despues de que el
servicio ya habia dejado de esperarla.

Ahora la herramienta contesta al momento ("tardara unos N minutos, te lo
mando aqui") y el trabajo sigue en su propio hilo. Al acabar queda una
ENTREGA pendiente, que el sidecar recoge (pregunta cada pocos segundos, como
con la foto del bot) y manda al chat. El servicio no puede escribirle al
sidecar; el sidecar si al servicio.

Vive en memoria: un reinicio a medio reporte lo pierde, y el usuario tendria
que volver a pedirlo.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger("sql_agent.segundo_plano")

# Una entrega que el sidecar se llevo y no confirmo en este tiempo (se cayo
# la conexion a media entrega) se le vuelve a dar.
REINTENTAR_ENTREGA_S = 120


@dataclass
class Entrega:
    id: str
    chat_id: str
    turno: str
    idioma: str
    texto: str
    archivos: list[str] = field(default_factory=list)
    tomada_en: float | None = None


class Trabajos:
    """Los reportes en curso y lo que falta entregar."""

    def __init__(self, reloj: Callable[[], float] = time.monotonic):
        self._lock = threading.Lock()
        self._reloj = reloj
        self._entregas: dict[str, Entrega] = {}
        self._corriendo: dict[str, list[threading.Event]] = {}

    def lanzar(self, chat_id: str, turno: str, idioma: str,
               trabajo: Callable[[threading.Event], tuple[str, list[str]]],
               si_falla: Callable[[Exception], str],
               esperar: bool = False) -> None:
        """Corre `trabajo(cancelacion)` en su hilo y deja su entrega al acabar.

        `trabajo` devuelve (texto para el usuario, archivos). Si lanza, se
        entrega `si_falla(error)`: el usuario espera algo, aunque sea que no
        se pudo. Si se cancelo, no se entrega nada. `esperar` es para las
        pruebas: corre aqui mismo.
        """
        cancelacion = threading.Event()
        with self._lock:
            self._corriendo.setdefault(chat_id, []).append(cancelacion)

        def correr() -> None:
            comenzo = self._reloj()
            try:
                texto, archivos = trabajo(cancelacion)
            except Exception as error:  # noqa: BLE001 - se le dice al usuario
                if cancelacion.is_set():
                    logger.info("Reporte en segundo plano cancelado para %s", chat_id)
                    return
                logger.exception("Fallo un reporte en segundo plano para %s", chat_id)
                texto, archivos = si_falla(error), []
            finally:
                with self._lock:
                    activos = self._corriendo.get(chat_id, [])
                    if cancelacion in activos:
                        activos.remove(cancelacion)
                    if not activos:
                        self._corriendo.pop(chat_id, None)
            if cancelacion.is_set():
                logger.info("Reporte en segundo plano cancelado para %s", chat_id)
                return
            entrega = Entrega(uuid.uuid4().hex[:12], chat_id, turno, idioma, texto, list(archivos))
            with self._lock:
                self._entregas[entrega.id] = entrega
            logger.info("Reporte en segundo plano listo para %s en %.0fs (%d archivos)",
                        chat_id, self._reloj() - comenzo, len(archivos))

        if esperar:
            correr()
        else:
            threading.Thread(target=correr, name=f"reporte-{chat_id}", daemon=True).start()

    def en_curso(self, chat_id: str) -> int:
        with self._lock:
            return len(self._corriendo.get(chat_id, []))

    def cancelar(self, chat_id: str) -> bool:
        """Pide a los reportes de ese chat que se rindan. True si habia alguno."""
        with self._lock:
            activos = list(self._corriendo.get(chat_id, []))
        for evento in activos:
            evento.set()
        return bool(activos)

    def pendientes(self) -> list[Entrega]:
        """Lo que hay que mandar: lo nuevo, y lo que se llevo y no confirmo."""
        ahora = self._reloj()
        with self._lock:
            listas = [
                e for e in self._entregas.values()
                if e.tomada_en is None or ahora - e.tomada_en > REINTENTAR_ENTREGA_S
            ]
            for entrega in listas:
                entrega.tomada_en = ahora
            return listas

    def confirmar(self, entrega_id: str) -> bool:
        with self._lock:
            return self._entregas.pop(entrega_id, None) is not None
