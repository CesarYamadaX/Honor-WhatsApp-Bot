"""Servicio HTTP que expone el agente SQL al sidecar de WhatsApp.

Este endpoint no tiene autenticacion propia: su unica defensa es escuchar
solo en loopback. Nunca exponerlo en 0.0.0.0.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import idioma as idiomas
from agent import ConsultaCancelada
from bitacora import Bitacora
from config import Config
from session import RegistroSesiones
from usuarios import ErrorDeUsuarios, RegistroUsuarios
from config import normalizar_chat_id

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


def crear_app(
    config: Config,
    fabrica_agente: Callable[[], Any],
    usuarios: RegistroUsuarios | None = None,
    bitacora: Bitacora | None = None,
) -> FastAPI:
    app = FastAPI(title="sql_agent")
    registro = RegistroSesiones(
        fabrica=fabrica_agente,
        ttl_segundos=config.ttl_minutos * 60,
    )
    # Sin registro en disco se trabaja con uno en memoria sembrado del .env:
    # el comportamiento es el de siempre y las pruebas no necesitan archivos.
    usuarios = usuarios if usuarios is not None else RegistroUsuarios(semilla=config.allowlist)
    diario = bitacora if bitacora is not None else Bitacora()
    app.state.usuarios = usuarios
    app.state.bitacora = diario

    def autorizado(chat_id: str) -> bool:
        """Unico punto donde se decide si un chat existe para el bot.

        Los grupos y las difusiones no pasan `normalizar_chat_id`, y un
        numero que no este activo es indistinguible de uno que nunca estuvo:
        cualquier respuesta distinta le confirmaria que hay un bot aqui.
        """
        numero = normalizar_chat_id(chat_id)
        return numero is not None and usuarios.puede_usar(numero)

    def _ejecutar(sesion, pregunta: str, chat_id: str, idioma: str) -> Respuesta | None:
        # El lock se suelta aqui dentro, no en el llamador: si la peticion
        # expira, asyncio cancela la espera pero el hilo sigue corriendo, y
        # soltar el lock antes dejaria entrar una segunda consulta del mismo
        # chat mientras la primera todavia golpea la base.
        turno = diario.comenzar(chat_id, pregunta)
        comenzo = time.monotonic()
        try:
            texto, archivos = sesion.agente.responder(
                pregunta, cancelacion=sesion.cancelacion
            )
            idioma_usado = getattr(sesion.agente, "idioma", idioma)
            diario.terminar(
                turno,
                respuesta=texto,
                archivos=list(archivos),
                idioma=idioma_usado,
                segundos=time.monotonic() - comenzo,
            )
            return Respuesta(
                reply=texto, files=list(archivos), idioma=idioma_usado
            )
        except ConsultaCancelada:
            # None, no un mensaje: al usuario ya le esta contestando la
            # pregunta nueva, la que provoco la cancelacion. Mandarle ademas
            # un "cancelado" por la vieja seria ruido.
            logger.info("Consulta cancelada para %s", chat_id)
            diario.terminar(
                turno,
                estado="cancelado",
                idioma=idioma,
                segundos=time.monotonic() - comenzo,
            )
            return None
        except Exception as error:
            logger.exception("Fallo el agente para %s", chat_id)
            diario.terminar(
                turno,
                respuesta=str(error)[:500],
                estado="error",
                idioma=idioma,
                segundos=time.monotonic() - comenzo,
            )
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
        if not autorizado(peticion.chat_id):
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
        if not autorizado(peticion.chat_id):
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
        if not autorizado(peticion.chat_id):
            raise HTTPException(status_code=403, detail="No autorizado")
        sesion = registro.existente(peticion.chat_id)
        if sesion is None:
            return {"idioma": idiomas.POR_DEFECTO}
        return {"idioma": getattr(sesion.agente, "idioma", idiomas.POR_DEFECTO)}

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "sesiones": len(registro)}

    # ------------------------------------------------------------------
    # Panel de administracion. Vive dentro del mismo servicio para que se
    # encienda con el bot y no haya un tercer proceso que recordar.
    # ------------------------------------------------------------------

    def _solo_local() -> None:
        """Corta el panel si el servicio no esta atado a loopback.

        El servicio no tiene autenticacion: su unica defensa es escuchar solo
        en la maquina. Con /ask eso ya era delicado; el panel ademas da de
        alta usuarios y deja leer conversaciones, asi que si alguien cambia
        API_HOST a 0.0.0.0 el panel se apaga en vez de quedar abierto a la
        red.
        """
        if config.api_host not in ("127.0.0.1", "localhost", "::1"):
            raise HTTPException(
                status_code=403,
                detail=(
                    "El panel solo funciona con el servicio atado a loopback. "
                    f"API_HOST es {config.api_host!r}: no tiene autenticacion y "
                    "quedaria abierto a la red."
                ),
            )

    @app.get("/panel", response_class=HTMLResponse)
    async def panel() -> Any:
        _solo_local()
        archivo = Path(__file__).resolve().parent / "panel" / "index.html"
        if not archivo.exists():
            raise HTTPException(status_code=404, detail="Falta panel/index.html")
        return FileResponse(archivo)

    @app.get("/panel/usuarios")
    async def listar_usuarios() -> dict:
        _solo_local()
        return {
            "usuarios": [vars(u) for u in usuarios.listar()],
            "con_conversacion_viva": registro.chats(),
        }

    class AltaUsuario(BaseModel):
        numero: str
        nombre: str = ""
        nota: str = ""

    class CambioUsuario(BaseModel):
        numero: str
        nota: str = ""

    def _respuesta_de_usuario(accion, *args, **kwargs) -> dict:
        """Ejecuta una operacion del registro y traduce su error a HTTP 400."""
        try:
            resultado = accion(*args, **kwargs)
        except ErrorDeUsuarios as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"usuario": vars(resultado) if resultado is not None else None}

    @app.post("/panel/usuarios")
    async def agregar_usuario(alta: AltaUsuario) -> dict:
        _solo_local()
        logger.info("Panel: alta de %s", alta.numero)
        return _respuesta_de_usuario(usuarios.agregar, alta.numero, alta.nombre, alta.nota)

    @app.post("/panel/usuarios/suspender")
    async def suspender_usuario(cambio: CambioUsuario) -> dict:
        _solo_local()
        logger.info("Panel: suspension de %s", cambio.numero)
        return _respuesta_de_usuario(usuarios.suspender, cambio.numero, cambio.nota)

    @app.post("/panel/usuarios/reactivar")
    async def reactivar_usuario(cambio: CambioUsuario) -> dict:
        _solo_local()
        logger.info("Panel: reactivacion de %s", cambio.numero)
        return _respuesta_de_usuario(usuarios.reactivar, cambio.numero)

    @app.post("/panel/usuarios/eliminar")
    async def eliminar_usuario(cambio: CambioUsuario) -> dict:
        _solo_local()
        logger.info("Panel: baja de %s", cambio.numero)
        return _respuesta_de_usuario(usuarios.eliminar, cambio.numero)

    @app.get("/panel/actividad")
    async def actividad() -> dict:
        _solo_local()
        return {"en_curso": diario.en_curso(), "sesiones": len(registro)}

    @app.get("/panel/ultimos")
    async def ultimos(limite: int = 6) -> dict:
        """Las ultimas consultas de cualquier chat, para la vista de actividad.

        Con el bot en reposo, "no hay nada corriendo" es una pantalla vacia.
        Lo que uno quiere ver en ese momento es que acaba de hacer.
        """
        _solo_local()
        return {"turnos": diario.turnos(limite=max(1, min(limite, 50)))}

    @app.get("/panel/conversaciones")
    async def conversaciones(chat_id: str | None = None, limite: int = 50) -> dict:
        _solo_local()
        limite = max(1, min(limite, 500))
        if chat_id:
            return {"turnos": diario.turnos(limite=limite, chat_id=chat_id)}
        return {"chats": diario.chats(limite=limite)}

    return app



def main() -> None:
    logging.basicConfig(level=logging.INFO)
    from agent import SQLAgent

    config = Config.desde_entorno()

    # Un solo engine para todas las conversaciones: su pool de conexiones es
    # seguro entre hilos, y crear uno por chat multiplicaria las conexiones a
    # SQL Server sin ganar nada.
    motor = SQLAgent._crear_engine()

    # El registro de usuarios se siembra con WHATSAPP_ALLOWED_NUMBERS la
    # primera vez, asi que estrenar el panel no deja a nadie fuera. De ahi en
    # adelante manda el archivo, que es lo que el panel edita en caliente.
    datos = Path(__file__).resolve().parent / "datos"
    usuarios = RegistroUsuarios(datos / "usuarios.json", semilla=config.allowlist)
    diario = Bitacora(datos / "bitacora")

    app = crear_app(config, lambda: SQLAgent(engine=motor), usuarios, diario)

    if config.api_host in ("127.0.0.1", "localhost", "::1"):
        logger.info(
            "Panel de administracion: http://%s:%s/panel",
            config.api_host,
            config.api_port,
        )
    else:
        logger.warning(
            "API_HOST es %r y no loopback: el panel queda apagado. El servicio "
            "no tiene autenticacion, y el panel da de alta usuarios y deja leer "
            "conversaciones.",
            config.api_host,
        )

    import uvicorn

    uvicorn.run(app, host=config.api_host, port=config.api_port)


if __name__ == "__main__":
    main()
