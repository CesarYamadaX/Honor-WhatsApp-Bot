"""Servicio HTTP que expone el agente SQL al sidecar de WhatsApp.

Este endpoint no tiene autenticacion propia: su unica defensa es escuchar
solo en loopback. Nunca exponerlo en 0.0.0.0.
"""

import asyncio
import logging
import unicodedata
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from agent import ConsultaCancelada
from config import Config
from session import RegistroSesiones

logger = logging.getLogger("sql_agent.api")

MENSAJE_ERROR_INTERNO = (
    "Tuve un problema al consultar la base de datos. "
    "Intenta de nuevo en un momento, o reformula la pregunta."
)
MENSAJE_SERVICIO_CAIDO = (
    "Ahorita no puedo responder preguntas: el servicio de inteligencia "
    "artificial que uso esta fuera de linea. No se arregla reformulando la "
    "pregunta ni esperando un rato. Comunicate con lauragabrielacastellanos "
    "o con alguien del equipo de GTM Mexico para que lo revisen."
)

# Errores del proveedor del modelo: presupuesto agotado (403), sin saldo
# (402), clave vencida (401) o cuota diaria topada (429). Ninguno se arregla
# del lado del usuario -- la pregunta ni siquiera llega a la base de datos --,
# y culpar al SQL lo manda a buscar el problema donde no esta.
_CODIGOS_DE_CUENTA = frozenset({401, 402, 403, 429})
_MODULOS_DEL_MODELO = frozenset({"openai", "langchain_openai"})


def es_falla_del_servicio(error: BaseException | None) -> bool:
    """True si el fallo viene del proveedor del modelo y no de la base.

    Se recorre la cadena de causas porque langchain reenvuelve el error de
    openai en uno suyo, y el codigo HTTP puede venir en cualquiera de los dos.
    """
    vistos: set[int] = set()
    while error is not None and id(error) not in vistos:
        vistos.add(id(error))
        modulo = type(error).__module__.split(".")[0]
        if modulo in _MODULOS_DEL_MODELO:
            if getattr(error, "status_code", None) in _CODIGOS_DE_CUENTA:
                return True
        error = error.__cause__ or error.__context__
    return False
MENSAJE_CANCELADO = "Listo, cancele la consulta anterior."
MENSAJE_NADA_QUE_CANCELAR = "No tenias ninguna consulta en curso."

_PALABRAS_DE_CANCELACION = {
    "cancela",
    "cancelar",
    "cancelalo",
    "cancelala",
    "detente",
    "olvidalo",
    "ya no",
}


def _sin_acentos(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto) if not unicodedata.combining(c)
    )


def es_cancelacion(texto: str) -> bool:
    """True si el mensaje es una orden de cancelar y no una pregunta.

    Se compara el mensaje entero, no si contiene la palabra: "cancela" es una
    orden, pero "cuantas ventas se cancelaron en marzo" es una pregunta
    legitima que no se debe tratar como cancelacion.
    """
    limpio = _sin_acentos(texto).strip().strip(".!¡").casefold()
    return limpio in _PALABRAS_DE_CANCELACION


class Peticion(BaseModel):
    chat_id: str
    text: str


class Autorizacion(BaseModel):
    chat_id: str


class Respuesta(BaseModel):
    reply: str
    files: list[str] = []


def crear_app(config: Config, fabrica_agente: Callable[[], Any]) -> FastAPI:
    app = FastAPI(title="sql_agent")
    registro = RegistroSesiones(
        fabrica=fabrica_agente,
        ttl_segundos=config.ttl_minutos * 60,
    )

    def _ejecutar(sesion, pregunta: str, chat_id: str) -> Respuesta | None:
        # El lock se suelta aqui dentro, no en el llamador: si la peticion
        # expira, asyncio cancela la espera pero el hilo sigue corriendo, y
        # soltar el lock antes dejaria entrar una segunda consulta del mismo
        # chat mientras la primera todavia golpea la base.
        try:
            texto, archivos = sesion.agente.responder(
                pregunta, cancelacion=sesion.cancelacion
            )
            return Respuesta(reply=texto, files=list(archivos))
        except ConsultaCancelada:
            # None, no un mensaje: al usuario ya le esta contestando la
            # pregunta nueva, la que provoco la cancelacion. Mandarle ademas
            # un "cancelado" por la vieja seria ruido.
            logger.info("Consulta cancelada para %s", chat_id)
            return None
        except Exception as error:
            logger.exception("Fallo el agente para %s", chat_id)
            if es_falla_del_servicio(error):
                logger.error(
                    "El proveedor del modelo rechazo la peticion de %s: "
                    "revisar la cuenta (presupuesto, saldo o clave)",
                    chat_id,
                )
                return Respuesta(reply=MENSAJE_SERVICIO_CAIDO, files=[])
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
        quiere_cancelar = es_cancelacion(peticion.text)

        if sesion.lock.acquire(blocking=False):
            # Nada en curso. Un "cancela" aqui no tiene a que aplicarse.
            if quiere_cancelar:
                sesion.lock.release()
                return Respuesta(reply=MENSAJE_NADA_QUE_CANCELAR, files=[])
        else:
            # Hay una consulta viva. El mensaje nuevo manda: se le pide que se
            # rinda y se espera a que suelte el turno.
            logger.info("Cancelando la consulta en curso de %s", peticion.chat_id)
            sesion.cancelacion.set()
            liberado = await run_in_threadpool(
                sesion.lock.acquire, True, config.espera_cancelacion
            )
            sesion.cancelacion.clear()
            if not liberado:
                # No solto a tiempo: probablemente atorada en una llamada al
                # modelo, que no se puede interrumpir. Se responde como antes.
                raise HTTPException(status_code=429, detail="Consulta en curso")
            if quiere_cancelar:
                sesion.lock.release()
                return Respuesta(reply=MENSAJE_CANCELADO, files=[])

        try:
            respuesta = await asyncio.wait_for(
                run_in_threadpool(_ejecutar, sesion, peticion.text, peticion.chat_id),
                timeout=config.timeout_segundos,
            )
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("Timeout para %s", peticion.chat_id)
            raise HTTPException(status_code=504, detail="La consulta tardo demasiado")

        if respuesta is None:
            # A esta pregunta la cancelo una posterior, que es la que va a
            # contestar. 409 le dice al sidecar que no mande nada.
            raise HTTPException(status_code=409, detail="Consulta cancelada")
        return respuesta

    @app.post("/authorized")
    async def authorized(peticion: Autorizacion) -> dict:
        """Dice si un chat esta en la allowlist, sin correr nada.

        Existe para que el sidecar pueda mostrar "escribiendo" y el aviso de
        espera SOLO a quien esta autorizado. Antes no podia: la unica forma de
        saberlo era mandar la pregunta entera y esperar, y para entonces la
        consulta ya habia terminado. Mostrarle cualquier señal de vida a un
        numero ajeno confirma que hay un bot del otro lado, que es justo lo
        que la allowlist evita.
        """
        if not config.autorizado(peticion.chat_id):
            raise HTTPException(status_code=403, detail="No autorizado")
        return {"ok": True}

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
