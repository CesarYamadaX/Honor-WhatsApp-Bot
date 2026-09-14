"""Servicio HTTP que expone el agente SQL al sidecar de WhatsApp.

Este endpoint no tiene autenticacion propia: su unica defensa es escuchar
solo en loopback. Nunca exponerlo en 0.0.0.0.
"""

import asyncio
import logging
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from config import Config
from session import RegistroSesiones

logger = logging.getLogger("sql_agent.api")

MENSAJE_ERROR_INTERNO = (
    "Tuve un problema al consultar la base de datos. "
    "Intenta de nuevo en un momento, o reformula la pregunta."
)


class Peticion(BaseModel):
    chat_id: str
    text: str


class Respuesta(BaseModel):
    reply: str
    files: list[str] = []


def crear_app(config: Config, fabrica_agente: Callable[[], Any]) -> FastAPI:
    app = FastAPI(title="sql_agent")
    registro = RegistroSesiones(
        fabrica=fabrica_agente,
        ttl_segundos=config.ttl_minutos * 60,
    )

    def _ejecutar(sesion, pregunta: str, chat_id: str) -> Respuesta:
        # El lock se suelta aqui dentro, no en el llamador: si la peticion
        # expira, asyncio cancela la espera pero el hilo sigue corriendo, y
        # soltar el lock antes dejaria entrar una segunda consulta del mismo
        # chat mientras la primera todavia golpea la base.
        try:
            texto, archivos = sesion.agente.responder(pregunta)
            return Respuesta(reply=texto, files=list(archivos))
        except Exception:
            logger.exception("Fallo el agente para %s", chat_id)
            return Respuesta(reply=MENSAJE_ERROR_INTERNO, files=[])
        finally:
            sesion.lock.release()

    @app.post("/ask", response_model=Respuesta)
    async def ask(peticion: Peticion) -> Respuesta:
        if not config.autorizado(peticion.chat_id):
            # El sidecar traduce esto a silencio: responder confirmaria que
            # hay un bot vivo del otro lado.
            logger.warning("Mensaje rechazado de %s", peticion.chat_id)
            raise HTTPException(status_code=403, detail="No autorizado")

        registro.purgar()
        sesion = registro.obtener(peticion.chat_id)
        if not sesion.lock.acquire(blocking=False):
            raise HTTPException(status_code=429, detail="Consulta en curso")

        try:
            return await asyncio.wait_for(
                run_in_threadpool(_ejecutar, sesion, peticion.text, peticion.chat_id),
                timeout=config.timeout_segundos,
            )
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("Timeout para %s", peticion.chat_id)
            raise HTTPException(status_code=504, detail="La consulta tardo demasiado")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "sesiones": len(registro)}

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from agent import SQLAgent

    config = Config.desde_entorno()

    # Un solo engine para todas las conversaciones: su pool de conexiones es
    # seguro entre hilos, y crear uno por chat multiplicaria las conexiones a
    # SQL Server sin ganar nada.
    motor = SQLAgent._crear_engine()
    app = crear_app(config, lambda: SQLAgent(engine=motor))

    import uvicorn

    uvicorn.run(app, host=config.api_host, port=config.api_port)


if __name__ == "__main__":
    main()
