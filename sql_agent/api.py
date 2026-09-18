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

import idioma as idiomas
from agent import ConsultaCancelada
from config import Config
from session import RegistroSesiones

logger = logging.getLogger("sql_agent.api")


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
# Los textos fijos viven en idioma.py, traducidos. Aqui solo se elige cual.
_PALABRAS_DE_CANCELACION = {
    "cancela",
    "cancelar",
    "cancelalo",
    "cancelala",
    "detente",
    "olvidalo",
    "ya no",
    "cancel",
    "cancel it",
    "cancel that",
    "stop",
    "never mind",
    "nevermind",
    "forget it",
    "取消",
    "停",
    "停止",
    "算了",
    "不用了",
}


def es_cancelacion(texto: str) -> bool:
    """True si el mensaje es una orden de cancelar y no una pregunta.

    Se compara el mensaje entero, no si contiene la palabra: "cancela" es una
    orden, pero "cuantas ventas se cancelaron en marzo" es una pregunta
    legitima que no se debe tratar como cancelacion.
    """
    limpio = idiomas.sin_acentos(texto).strip().strip(".!¡。！").casefold()
    return limpio in _PALABRAS_DE_CANCELACION


def _idioma_de(sesion: Any, texto: str) -> str:
    """Idioma de este mensaje, o el que ya traia la conversacion.

    Un mensaje corto ("top 10", "2026?") no tiene idioma. Ahi se conserva el
    de la sesion en vez de caer al espanol por defecto: quien llevaba toda la
    charla en ingles no deberia recibir el aviso de error en espanol solo
    porque su ultima pregunta fue de dos palabras.
    """
    detectado = idiomas.detectar(texto)
    if detectado is not None:
        return detectado
    return getattr(sesion.agente, "idioma", idiomas.POR_DEFECTO)


class Peticion(BaseModel):
    chat_id: str
    text: str


class Autorizacion(BaseModel):
    chat_id: str


class Respuesta(BaseModel):
    reply: str
    files: list[str] = []
    # Para que el sidecar escriba SUS mensajes (archivo ilegible, "no tengo
    # respuesta") en el mismo idioma en que acaba de contestar el bot.
    idioma: str = idiomas.POR_DEFECTO


def crear_app(config: Config, fabrica_agente: Callable[[], Any]) -> FastAPI:
    app = FastAPI(title="sql_agent")
    registro = RegistroSesiones(
        fabrica=fabrica_agente,
        ttl_segundos=config.ttl_minutos * 60,
    )

    def _ejecutar(sesion, pregunta: str, chat_id: str, idioma: str) -> Respuesta | None:
        # El lock se suelta aqui dentro, no en el llamador: si la peticion
        # expira, asyncio cancela la espera pero el hilo sigue corriendo, y
        # soltar el lock antes dejaria entrar una segunda consulta del mismo
        # chat mientras la primera todavia golpea la base.
        try:
            texto, archivos = sesion.agente.responder(
                pregunta, cancelacion=sesion.cancelacion
            )
            return Respuesta(
                reply=texto,
                files=list(archivos),
                idioma=getattr(sesion.agente, "idioma", idioma),
            )
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
                return Respuesta(
                    reply=idiomas.texto("servicio_caido", idioma),
                    files=[],
                    idioma=idioma,
                )
            return Respuesta(
                reply=idiomas.texto("error_interno", idioma), files=[], idioma=idioma
            )
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
        idioma = _idioma_de(sesion, peticion.text)
        quiere_cancelar = es_cancelacion(peticion.text)

        if sesion.lock.acquire(blocking=False):
            # Nada en curso. Un "cancela" aqui no tiene a que aplicarse.
            if quiere_cancelar:
                sesion.lock.release()
                return Respuesta(
                    reply=idiomas.texto("nada_que_cancelar", idioma),
                    files=[],
                    idioma=idioma,
                )
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
                return Respuesta(
                    reply=idiomas.texto("cancelado", idioma), files=[], idioma=idioma
                )

        try:
            respuesta = await asyncio.wait_for(
                run_in_threadpool(
                    _ejecutar, sesion, peticion.text, peticion.chat_id, idioma
                ),
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

    @app.post("/idioma")
    async def idioma_del_chat(peticion: Autorizacion) -> dict:
        """En que idioma le esta hablando este chat al bot.

        El sidecar tambien escribe por su cuenta -- el aviso de espera, los
        errores de red -- y esos mensajes deben salir en el idioma de la
        conversacion. La deteccion no se duplica en JavaScript: dos copias de
        la misma heuristica se separan a la primera correccion que se aplica
        solo a una. El sidecar pregunta y aqui se contesta con lo que la
        sesion ya dedujo.

        Deliberadamente NO recibe el texto del mensaje. /authorized se
        contesta ANTES de saber si el numero esta en la allowlist, y el
        mensaje de un numero ajeno no tiene por que cruzar hasta aca; cuando
        el sidecar llama a esto, la pregunta ya viajo por /ask.
        """
        if not config.autorizado(peticion.chat_id):
            raise HTTPException(status_code=403, detail="No autorizado")
        sesion = registro.existente(peticion.chat_id)
        if sesion is None:
            return {"idioma": idiomas.POR_DEFECTO}
        return {"idioma": getattr(sesion.agente, "idioma", idiomas.POR_DEFECTO)}

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
