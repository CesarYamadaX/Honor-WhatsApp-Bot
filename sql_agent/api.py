"""Servicio HTTP que expone el agente SQL al sidecar de WhatsApp.

Este endpoint no tiene autenticacion propia: su unica defensa es escuchar
solo en loopback. Nunca exponerlo en 0.0.0.0.
"""

import asyncio
import logging
import re
import time
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

import base64
import hashlib
import json
import binascii

import bienvenida
import idioma as idiomas
import registro_de_logs
import resumen_local
import traduccion
import transcripcion
from agent import ConsultaCancelada
from bitacora import Bitacora
from calificaciones import (
    MAX_COMENTARIO,
    REACCIONES_BUENAS,
    REACCIONES_MALAS,
    SIN_REGISTRO,
    Calificaciones,
    valor_de,
)
from fotos import LADO_DE_PERFIL, AlmacenDeFotos, ErrorDeFoto, cuadrada_en_jpeg
from config import Config
from session import RegistroSesiones
import acceso
from usuarios import ErrorDeUsuarios, RegistroUsuarios, Usuario
from config import normalizar_chat_id

logger = logging.getLogger("sql_agent.api")

# Cuanto se espera a que el sidecar cambie la foto del bot en WhatsApp antes de
# decirle al panel que no se pudo. Pregunta cada 5 s y subir la foto tarda
# unos segundos, asi que dos minutos solo se pasan si no esta corriendo.
PLAZO_DEL_CAMBIO_DE_FOTO = 120


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


def es_falla_de_red_del_modelo(error: BaseException | None) -> bool:
    """True si no se pudo hablar con el proveedor del modelo (red, timeout).

    Antes esto caia en "error_interno", que culpa a la base de datos: el
    5/10/2026 un corte con OpenRouter a media respuesta le llego al usuario
    como "tuve un problema al consultar la base de datos".
    """
    vistos: set[int] = set()
    while error is not None and id(error) not in vistos:
        vistos.add(id(error))
        nombre = type(error).__name__
        modulo = type(error).__module__.split(".")[0]
        if modulo in _MODULOS_DEL_MODELO and ("Connection" in nombre or "Timeout" in nombre):
            return True
        error = error.__cause__ or error.__context__
    return False


# SQLSTATE de "no hay conexion con el servidor": 08xxx es la familia de
# errores de conexion (08001: no se pudo establecer), y HYT00 es el login
# timeout con el que ODBC se rinde cuando el nombre ni siquiera resuelve.
# Visto el 28 de septiembre de 2026: DESKTOP-GOE584A dejo de resolverse y
# cada pregunta tardaba ~18s en devolver un 500.
#
# Como codigo suelto, no como subcadena: "08001" aparece tambien dentro de
# cualquier numero largo -- un timestamp como 1727508001000 en el
# X-RateLimit-Reset de un 429 de OpenRouter -- y eso haria pasar una cuota
# topada del modelo por una base caida.
_ESTADOS_SIN_CONEXION = re.compile(r"(?<![0-9A-Za-z])(?:08001|08S01|08004|HYT00)(?![0-9A-Za-z])")


def es_falla_de_conexion(error: BaseException | None) -> bool:
    """True si el fallo es que la base no contesta, no la consulta.

    Se mira el SQLSTATE y no el tipo: pyodbc usa OperationalError tanto para
    "no encuentro al servidor" como para cosas que si son de la consulta, y
    sqlalchemy lo reenvuelve en otro OperationalError. Por eso tambien se
    recorre la cadena de causas.
    """
    vistos: set[int] = set()
    while error is not None and id(error) not in vistos:
        vistos.add(id(error))
        if _ESTADOS_SIN_CONEXION.search(str(error)):
            return True
        error = error.__cause__ or error.__context__ or getattr(error, "orig", None)
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


def _solo_digitos(crudo: str) -> str:
    """El numero como lo guarda el registro, para buscarlo antes de cambiarlo."""
    return "".join(c for c in crudo if c.isdigit())


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


class NotaDeVoz(BaseModel):
    chat_id: str
    audio: str  # base64
    formato: str = "ogg"


# Una nota de voz de WhatsApp (Opus) pesa ~2 KB por segundo: 5 MB son mas de
# media hora. El tope de duracion de verdad lo pone el sidecar; esto es para
# que nadie mande un archivo enorme por aqui.
MAX_BYTES_DE_AUDIO = 5 * 1024 * 1024


class Respuesta(BaseModel):
    reply: str
    files: list[str] = []
    # Para que el sidecar escriba SUS mensajes (archivo ilegible, "no tengo
    # respuesta") en el mismo idioma en que acaba de contestar el bot.
    idioma: str = idiomas.POR_DEFECTO
    # El id del turno: el sidecar lo asocia a los mensajes que manda para
    # que una reaccion 👍/👎 a cualquiera de ellos califique esta respuesta.
    turno: str = ""


class Enviados(BaseModel):
    chat_id: str
    turno: str
    mensajes: list[str]
    # De los mensajes que son un archivo, cual (su nombre). Los demas son el
    # texto. Sin esto, un 👎 al Excel y uno al texto eran la misma
    # calificacion y el segundo borraba el primero.
    partes: dict[str, str] = {}


class ParaTraducir(BaseModel):
    textos: list[str]
    destino: str = "es"


# Un turno son dos textos (pregunta y respuesta), y la bitacora ya recorta
# cada uno a 4000 caracteres. Esto es solo para que nadie mande un libro.
MAX_TEXTOS_A_TRADUCIR = 10
MAX_CARACTERES_A_TRADUCIR = 20_000


class Reaccion(BaseModel):
    chat_id: str
    mensaje_id: str
    emoji: str = ""  # vacio = el usuario quito la reaccion


class Comentario(BaseModel):
    turno_id: str
    chat_id: str
    texto: str = ""  # vacio = borrar el comentario
    parte: str = ""  # "" = el texto de la respuesta; si no, el archivo


def crear_app(
    config: Config,
    fabrica_agente: Callable[[], Any],
    usuarios: RegistroUsuarios | None = None,
    bitacora: Bitacora | None = None,
    fotos: AlmacenDeFotos | None = None,
    calificaciones: Calificaciones | None = None,
    carpeta_logs: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="sql_agent")
    # Donde iniciar.py deja los logs de cada dia (ver registro_de_logs.py).
    # Se recibe para que las pruebas lean de una carpeta propia.
    logs = Path(carpeta_logs) if carpeta_logs else registro_de_logs.CARPETA_POR_DEFECTO
    registro = RegistroSesiones(
        fabrica=fabrica_agente,
        # La memoria dura el dia natural; SESSION_TTL_MINUTES es solo un tope
        # extra por inactividad, y en 0 (el valor por defecto) no aplica.
        ttl_segundos=config.ttl_minutos * 60,
    )
    # Sin registro en disco se trabaja con uno en memoria sembrado del .env:
    # el comportamiento es el de siempre y las pruebas no necesitan archivos.
    usuarios = usuarios if usuarios is not None else RegistroUsuarios(semilla=config.allowlist)
    diario = bitacora if bitacora is not None else Bitacora()
    album = fotos if fotos is not None else AlmacenDeFotos()
    notas = calificaciones if calificaciones is not None else Calificaciones()
    app.state.usuarios = usuarios
    app.state.bitacora = diario
    app.state.fotos = album
    app.state.calificaciones = notas

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
        numero = normalizar_chat_id(chat_id)
        cadenas_del_usuario = usuarios.cadenas_de(numero) if numero else None
        # Antes de anotar este turno: si no tiene ninguno, es su primera vez.
        primera_vez = diario.cuantos(chat_id) == 0
        turno = diario.comenzar(chat_id, pregunta)
        comenzo = time.monotonic()
        # Un saludo o un "que puedes hacer?": la bienvenida con ejemplos, al
        # instante y sin el modelo (ver bienvenida.py).
        if bienvenida.ACTIVA and bienvenida.es_saludo(pregunta):
            idioma_saludo = idiomas.detectar(pregunta) or idioma
            texto = bienvenida.texto(idioma_saludo, cadenas_del_usuario)
            diario.terminar(turno, respuesta=texto, idioma=idioma_saludo,
                            segundos=time.monotonic() - comenzo)
            return Respuesta(reply=texto, files=[], idioma=idioma_saludo, turno=turno.id)
        try:
            # Las cadenas se leen en cada pregunta, no al abrir la sesion:
            # un cambio en el panel vale desde el siguiente mensaje.
            texto, archivos = sesion.agente.responder(
                pregunta,
                cancelacion=sesion.cancelacion,
                cadenas=cadenas_del_usuario,
                # Se leen en cada pregunta: un comentario nuevo en el panel
                # vale desde el siguiente mensaje, como las cadenas.
                notas=notas.notas_para_el_bot(),
            )
            idioma_usado = getattr(sesion.agente, "idioma", idioma)
            # Su primera pregunta: la respuesta y, al pie, que mas se puede
            # preguntar. Una sola vez.
            if primera_vez and bienvenida.ACTIVA:
                texto = f"{texto}\n\n{bienvenida.consejo(idioma_usado, cadenas_del_usuario)}"
            diario.terminar(
                turno,
                respuesta=texto,
                archivos=list(archivos),
                idioma=idioma_usado,
                segundos=time.monotonic() - comenzo,
            )
            return Respuesta(
                reply=texto, files=list(archivos), idioma=idioma_usado, turno=turno.id
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
            if es_falla_de_conexion(error):
                logger.error("La base de datos no contesta (%s)", chat_id)
                return Respuesta(
                    reply=idiomas.texto("base_no_disponible", idioma),
                    files=[],
                    idioma=idioma,
                    turno=turno.id,
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
                    turno=turno.id,
                )
            if es_falla_de_red_del_modelo(error):
                logger.error("No se pudo hablar con el proveedor del modelo (%s)", chat_id)
                return Respuesta(
                    reply=idiomas.texto("modelo_sin_conexion", idioma),
                    files=[],
                    idioma=idioma,
                    turno=turno.id,
                )
            # Con su turno: tambien un "no pude" se puede calificar.
            return Respuesta(
                reply=idiomas.texto("error_interno", idioma), files=[], idioma=idioma,
                turno=turno.id,
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
        try:
            # En un hilo y no aqui: abrir una conversacion construye un
            # SQLAgent, que se conecta a la base -- ~18s con el servidor
            # caido. Hecho en el event loop, eso congelaba TODAS las
            # peticiones: el /authorized de otro chat pasaba sus 5s y el
            # sidecar descartaba ese mensaje como "no autorizado".
            sesion = await run_in_threadpool(registro.obtener, peticion.chat_id)
        except Exception as error:
            # Abrir una conversacion construye un SQLAgent, y ese se conecta
            # a la base. Si la base no contesta, esto reventaba como un 500
            # que el sidecar traducia a "no pude procesar tu pregunta".
            if not es_falla_de_conexion(error):
                raise
            logger.error("La base de datos no contesta (%s)", peticion.chat_id)
            idioma = idiomas.detectar(peticion.text) or idiomas.POR_DEFECTO
            return Respuesta(
                reply=idiomas.texto("base_no_disponible", idioma),
                files=[],
                idioma=idioma,
            )
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

    @app.post("/enviados")
    async def enviados(peticion: Enviados) -> dict:
        """Que mensajes de WhatsApp mando el sidecar para un turno."""
        if not autorizado(peticion.chat_id):
            raise HTTPException(status_code=403, detail="No autorizado")
        partes = {m: PureWindowsPath(a).name for m, a in peticion.partes.items() if a}
        await run_in_threadpool(
            notas.registrar_enviados, peticion.chat_id, peticion.turno, peticion.mensajes, partes
        )
        return {"ok": True}

    def parte_del_mensaje(mensaje_id: str, turno_id: str, chat_id: str, parte: str) -> str:
        """Que parte de la respuesta es un mensaje: "" el texto, o el archivo.

        Los mensajes registrados antes del 8/10/2026 no lo dicen. Para esos,
        el sidecar manda primero el texto y despues los archivos en orden,
        asi que los ultimos N mensajes del turno son sus N archivos.
        """
        if parte:
            return parte
        mensajes = notas.mensajes_del_turno(turno_id, chat_id)
        if any(p for _, p in mensajes):
            return ""  # registro nuevo: sin parte es el texto
        archivos = [PureWindowsPath(a).name for a in ((diario.buscar(turno_id) or {}).get("archivos") or [])]
        ids = [m for m, _ in mensajes]
        if not archivos or len(ids) < len(archivos):
            return ""
        cola = ids[-len(archivos):]
        return archivos[cola.index(mensaje_id)] if mensaje_id in cola else ""

    @app.post("/reaccion")
    async def reaccion(peticion: Reaccion) -> dict:
        """Una reaccion del usuario a un mensaje del bot.

        Cuentan las de calificaciones.REACCIONES_BUENAS y _MALAS; las demas
        (😂, 😮) no califican y se ignoran. Una reaccion a
        un mensaje que no es una respuesta del bot (un aviso de espera, un
        mensaje de antes de que esto existiera) tampoco.
        """
        if not autorizado(peticion.chat_id):
            raise HTTPException(status_code=403, detail="No autorizado")
        encontrado = await run_in_threadpool(
            notas.mensaje, peticion.mensaje_id, peticion.chat_id
        )
        turno_id, parte = encontrado or (None, "")
        if turno_id is not None:
            parte = await run_in_threadpool(
                parte_del_mensaje, peticion.mensaje_id, turno_id, peticion.chat_id, parte
            )
        else:
            # Un mensaje del bot que no quedo registrado: los anteriores al
            # 5/10/2026, cuando esto no existia. Visto el 7/10/2026: un 👍 a un
            # Excel del 1 de octubre se perdia como "no es una respuesta del
            # bot". Se guarda igual, ligado al mensaje y no a una pregunta:
            # en el panel sale como "respuesta anterior al registro". El
            # sidecar ya descarta las reacciones a mensajes del propio usuario.
            turno_id = f"{SIN_REGISTRO}{peticion.mensaje_id}"
        que = f"{turno_id} ({parte})" if parte else turno_id
        if not peticion.emoji:
            await run_in_threadpool(notas.quitar, turno_id, peticion.chat_id, parte)
            logger.info("%s quito su calificacion de %s", peticion.chat_id, que)
            return {"calificado": False, "turno": turno_id, "parte": parte, "motivo": "quitada"}
        valor = valor_de(peticion.emoji)
        if valor is None:
            return {"calificado": False, "turno": turno_id, "parte": parte, "motivo": "esa reaccion no califica"}
        await run_in_threadpool(
            notas.calificar, turno_id, peticion.chat_id, valor, peticion.emoji, parte
        )
        logger.info("%s califico %s con %s", peticion.chat_id, que, peticion.emoji)
        return {"calificado": True, "turno": turno_id, "parte": parte, "valor": valor}

    @app.post("/transcribir")
    async def transcribir(peticion: NotaDeVoz) -> dict:
        """El texto de una nota de voz, para que el sidecar la pregunte.

        Va aparte de /ask para que el sidecar pueda mostrarle al usuario que
        entendio ANTES de correr la consulta: si el audio se entendio mal,
        lo ve en seguida y no despues de esperar un reporte de otra cosa.
        """
        if not autorizado(peticion.chat_id):
            logger.warning("Nota de voz rechazada de %s", peticion.chat_id)
            raise HTTPException(status_code=403, detail="No autorizado")
        try:
            audio = base64.b64decode(peticion.audio, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=400, detail="Audio mal codificado")
        if not audio:
            raise HTTPException(status_code=400, detail="Audio vacio")
        if len(audio) > MAX_BYTES_DE_AUDIO:
            raise HTTPException(status_code=413, detail="Audio demasiado grande")
        try:
            texto = await run_in_threadpool(
                transcripcion.transcribir, audio, peticion.formato
            )
        except transcripcion.TranscripcionFallida:
            logger.exception("No se pudo transcribir la nota de %s", peticion.chat_id)
            raise HTTPException(status_code=502, detail="No se pudo transcribir")
        logger.info("Nota de voz de %s (%d KB): %s", peticion.chat_id, len(audio) // 1024, texto[:200])
        if not texto:
            raise HTTPException(status_code=422, detail="No se entendio el audio")
        return {"texto": texto, "idioma": idiomas.detectar(texto) or idiomas.POR_DEFECTO}

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
        return {
            "status": "ok",
            "sesiones": len(registro),
            "resumen_local": resumen_local.estado().get("construido"),
        }

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
        # Que el navegador la pida siempre: sin esto podia seguir usando la
        # pagina vieja horas despues de un git pull. Visto el 13/10/2026: las
        # fotos de WhatsApp ya estaban guardadas y el panel no las mostraba.
        return FileResponse(archivo, headers={"Cache-Control": "no-cache"})

    @app.get("/panel/icono/{nombre}")
    async def ver_icono(nombre: str) -> Any:
        """Sirve el icono de la aplicacion en el tamaño que pida el navegador.

        Son PNG dibujados aparte (panel/hacer_iconos.py) y no el SVG: a 16 o
        32 px, que es donde vive el icono, el rasterizado del navegador saca
        el borde con escalones. Estos estan reducidos con Lanczos desde ocho
        veces su tamaño.
        """
        _solo_local()
        if not re.fullmatch(r"icono-\d{1,4}\.png|icono\.ico", nombre):
            raise HTTPException(status_code=404, detail="No hay ese icono")
        archivo = Path(__file__).resolve().parent / "panel" / "iconos" / nombre
        if not archivo.exists():
            raise HTTPException(status_code=404, detail="No hay ese icono")
        tipo = "image/png" if nombre.endswith(".png") else "image/x-icon"
        return FileResponse(archivo, media_type=tipo)

    @app.get("/panel/usuarios")
    async def listar_usuarios() -> dict:
        _solo_local()
        return {
            "usuarios": [vars(u) for u in usuarios.listar()],
            "con_conversacion_viva": registro.chats(),
            "cadenas": [{"clave": c, "nombre": acceso.NOMBRES[c]} for c in acceso.CADENAS],
        }

    class AltaUsuario(BaseModel):
        numero: str
        nombre: str = ""
        nota: str = ""

    class CambioUsuario(BaseModel):
        numero: str
        nota: str = ""

    class NuevoNombre(BaseModel):
        numero: str
        nombre: str = ""

    class NuevasCadenas(BaseModel):
        numero: str
        # Vacio = todas.
        cadenas: list[str] = []

    class NuevaFoto(BaseModel):
        numero: str
        # La imagen en base64, tal cual la lee el navegador con FileReader.
        # Se acepta con o sin el prefijo "data:image/...;base64,".
        imagen: str

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

    @app.post("/panel/usuarios/renombrar")
    async def renombrar_usuario(cambio: NuevoNombre) -> dict:
        _solo_local()
        logger.info("Panel: %s ahora se llama %r", cambio.numero, cambio.nombre)
        return _respuesta_de_usuario(usuarios.renombrar, cambio.numero, cambio.nombre)

    @app.post("/panel/usuarios/cadenas")
    async def poner_cadenas(cambio: NuevasCadenas) -> dict:
        """Que cadenas puede ver. Vale desde su siguiente mensaje."""
        _solo_local()
        logger.info("Panel: %s ahora ve %s", cambio.numero, cambio.cadenas or "todas las cadenas")
        return _respuesta_de_usuario(usuarios.poner_cadenas, cambio.numero, cambio.cadenas)

    @app.post("/panel/usuarios/foto")
    async def poner_foto(cambio: NuevaFoto) -> dict:
        """Guarda la foto de un usuario.

        Llega en base64 y no como archivo adjunto para no sumar una
        dependencia (multipart) por un formulario de dos campos.
        """
        _solo_local()
        crudo = cambio.imagen.split(",", 1)[-1] if cambio.imagen.startswith("data:") else cambio.imagen
        try:
            datos = base64.b64decode(crudo, validate=True)
        except (binascii.Error, ValueError) as error:
            raise HTTPException(status_code=400, detail="La imagen llego corrupta.") from error

        usuario = usuarios.obtener(_solo_digitos(cambio.numero))
        try:
            archivo = album.guardar(_solo_digitos(cambio.numero), datos)
        except ErrorDeFoto as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

        anterior = usuario.foto if usuario else ""
        resultado = _respuesta_de_usuario(usuarios.poner_foto, cambio.numero, archivo)
        # La de antes se borra DESPUES de que la nueva quedo registrada: al
        # reves, un fallo al registrar dejaria al usuario sin ninguna.
        if anterior and anterior != archivo:
            album.borrar(anterior)
        logger.info("Panel: foto nueva para %s", cambio.numero)
        return resultado

    class FotoDeWhatsApp(BaseModel):
        numero: str
        # En base64; None = no tiene foto o no la deja ver (privacidad).
        imagen: str | None = None

    @app.get("/fotos/pendientes")
    async def fotos_pendientes() -> dict:
        """A quien le toca revisar la foto de perfil (lo pregunta el sidecar)."""
        _solo_local()
        return {"numeros": await run_in_threadpool(usuarios.pendientes_de_foto)}

    @app.post("/foto-whatsapp")
    async def foto_de_whatsapp(foto: FotoDeWhatsApp) -> dict:
        """La foto de perfil de WhatsApp de un usuario, que trae el sidecar.

        Se guarda aparte de la que se sube en el panel (`foto`), que manda.
        Sin imagen se borra la que habia: la quito de su perfil, o ya no la
        deja ver.
        """
        _solo_local()
        numero = _solo_digitos(foto.numero)
        usuario = usuarios.obtener(numero)
        if usuario is None:
            raise HTTPException(status_code=404, detail="No es un usuario del bot")
        archivo = ""
        if foto.imagen:
            try:
                archivo = album.guardar(numero, base64.b64decode(foto.imagen, validate=True))
            except (binascii.Error, ValueError, ErrorDeFoto) as error:
                raise HTTPException(status_code=400, detail=f"La foto no sirve: {error}") from error
        anterior = usuario.foto_whatsapp
        resultado = _respuesta_de_usuario(usuarios.poner_foto_whatsapp, numero, archivo)
        # Mismo contenido, mismo nombre: solo se borra si cambio, y nunca la
        # que se subio a mano.
        if anterior and anterior not in (archivo, usuario.foto):
            album.borrar(anterior)
        if archivo != anterior:
            logger.info("Foto de WhatsApp %s para %s", "nueva" if archivo else "quitada", numero)
        return resultado

    # La foto de perfil del propio bot, para el circulo de la marca del panel
    # (pedido el 13/10/2026). Se recuerda en un JSON junto a las fotos, para
    # que sobreviva a un reinicio antes de que el sidecar vuelva a traerla.
    foto_del_bot = {"archivo": "", "numero": ""}
    registro_del_bot = album.carpeta / "bot.json" if album.carpeta else None
    if registro_del_bot is not None and registro_del_bot.exists():
        try:
            foto_del_bot.update(json.loads(registro_del_bot.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    @app.post("/foto-bot")
    async def poner_foto_del_bot(foto: FotoDeWhatsApp) -> dict:
        """La foto de perfil del numero del bot, que trae el sidecar."""
        _solo_local()
        numero = _solo_digitos(foto.numero)
        archivo = ""
        if foto.imagen:
            try:
                archivo = album.guardar(numero, base64.b64decode(foto.imagen, validate=True))
            except (binascii.Error, ValueError, ErrorDeFoto) as error:
                raise HTTPException(status_code=400, detail=f"La foto no sirve: {error}") from error
        anterior = foto_del_bot["archivo"]
        foto_del_bot.update(archivo=archivo, numero=numero)
        if registro_del_bot is not None:
            registro_del_bot.parent.mkdir(parents=True, exist_ok=True)
            registro_del_bot.write_text(json.dumps(foto_del_bot), encoding="utf-8")
        if anterior and anterior != archivo:
            album.borrar(anterior)
        return dict(foto_del_bot)

    # Cambiar la foto de perfil del bot desde el panel (pedido el 13/10/2026).
    # El servicio no habla con WhatsApp: guarda el cambio, el sidecar lo
    # recoge (pregunta cada pocos segundos), lo aplica y avisa como salio.
    # Estados: "" nada, "pendiente" esperando al sidecar, "aplicando" el
    # sidecar ya se lo llevo, "listo" o "error". Vive en memoria: un cambio a
    # medias no tiene por que sobrevivir a un reinicio.
    cambio_de_foto: dict[str, Any] = {"id": "", "imagen": b"", "estado": "", "error": "", "desde": 0.0}

    def _estado_del_cambio() -> dict:
        en_curso = cambio_de_foto["estado"] in ("pendiente", "aplicando")
        if en_curso and time.monotonic() - cambio_de_foto["desde"] > PLAZO_DEL_CAMBIO_DE_FOTO:
            # Sin esto el panel esperaria para siempre si el sidecar no corre
            # o perdio la conexion con WhatsApp.
            cambio_de_foto.update(
                estado="error",
                imagen=b"",
                error="El bot no contesto: revisa que este conectado a WhatsApp e intenta de nuevo.",
            )
            logger.warning("Panel: la foto del bot no se cambio, el sidecar no contesto a tiempo")
        return {"id": cambio_de_foto["id"], "estado": cambio_de_foto["estado"], "error": cambio_de_foto["error"]}

    @app.get("/panel/bot")
    async def ver_bot() -> dict:
        """Que foto va en el circulo de la marca ("" = el icono de siempre)."""
        _solo_local()
        return {
            "foto": foto_del_bot["archivo"],
            "numero": foto_del_bot["numero"],
            "cambio": _estado_del_cambio(),
        }

    class FotoNuevaDelBot(BaseModel):
        # Como en NuevaFoto: base64, con o sin el prefijo "data:...;base64,".
        imagen: str

    @app.post("/panel/bot/foto")
    async def cambiar_foto_del_bot(nueva: FotoNuevaDelBot) -> dict:
        """Pide cambiar la foto de perfil del bot en WhatsApp."""
        _solo_local()
        crudo = nueva.imagen.split(",", 1)[-1] if nueva.imagen.startswith("data:") else nueva.imagen
        try:
            datos = base64.b64decode(crudo, validate=True)
        except (binascii.Error, ValueError) as error:
            raise HTTPException(status_code=400, detail="La imagen llego corrupta.") from error
        try:
            imagen = await run_in_threadpool(cuadrada_en_jpeg, datos, LADO_DE_PERFIL)
        except ErrorDeFoto as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        cambio_de_foto.update(
            id=hashlib.sha256(imagen + str(time.time()).encode()).hexdigest()[:12],
            imagen=imagen,
            estado="pendiente",
            error="",
            desde=time.monotonic(),
        )
        logger.info("Panel: foto de perfil nueva para el bot, esperando al sidecar")
        return _estado_del_cambio()

    @app.get("/foto-bot/cambio")
    async def cambio_pendiente_de_foto() -> dict:
        """La foto que hay que ponerle al bot, si hay (lo pregunta el sidecar).

        Se entrega una sola vez: el sidecar pregunta cada pocos segundos y
        subir la misma foto dos veces seria un cambio de perfil de mas.
        """
        _solo_local()
        _estado_del_cambio()
        if cambio_de_foto["estado"] != "pendiente":
            return {}
        cambio_de_foto["estado"] = "aplicando"
        return {"id": cambio_de_foto["id"], "imagen": base64.b64encode(cambio_de_foto["imagen"]).decode()}

    class ResultadoDelCambio(BaseModel):
        id: str
        numero: str = ""
        # None = WhatsApp la acepto; si no, lo que contesto.
        error: str | None = None

    @app.post("/foto-bot/cambio")
    async def resultado_del_cambio_de_foto(resultado: ResultadoDelCambio) -> dict:
        """Como salio el cambio de foto en WhatsApp (lo avisa el sidecar)."""
        _solo_local()
        if resultado.id != cambio_de_foto["id"] or cambio_de_foto["estado"] != "aplicando":
            # Uno viejo, o uno que ya se dio por perdido: no pisa al de ahora.
            return _estado_del_cambio()
        if resultado.error:
            cambio_de_foto.update(estado="error", error=f"WhatsApp no acepto la foto: {resultado.error}")
            logger.warning("Panel: WhatsApp no acepto la foto del bot: %s", resultado.error)
        else:
            # La miniatura del circulo sale de la misma imagen que se subio, y
            # no de volver a pedirsela a WhatsApp: asi se ve en cuanto acaba.
            numero = _solo_digitos(resultado.numero) or foto_del_bot["numero"]
            if numero:
                archivo = album.guardar(numero, cambio_de_foto["imagen"])
                anterior = foto_del_bot["archivo"]
                foto_del_bot.update(archivo=archivo, numero=numero)
                if registro_del_bot is not None:
                    registro_del_bot.parent.mkdir(parents=True, exist_ok=True)
                    registro_del_bot.write_text(json.dumps(foto_del_bot), encoding="utf-8")
                if anterior and anterior != archivo:
                    album.borrar(anterior)
            cambio_de_foto.update(estado="listo", error="")
            logger.info("Panel: foto de perfil del bot cambiada en WhatsApp")
        cambio_de_foto["imagen"] = b""
        return _estado_del_cambio()

    @app.post("/panel/usuarios/quitar-foto")
    async def quitar_foto(cambio: CambioUsuario) -> dict:
        _solo_local()
        # El nombre del archivo se lee ANTES de vaciarlo en el registro.
        anterior = (usuarios.obtener(_solo_digitos(cambio.numero)) or Usuario("")).foto
        resultado = _respuesta_de_usuario(usuarios.quitar_foto, cambio.numero)
        if anterior:
            album.borrar(anterior)
        return resultado

    @app.get("/panel/foto/{nombre}")
    async def ver_foto(nombre: str) -> Any:
        """Sirve una foto. El nombre viene del navegador, asi que se valida.

        `AlmacenDeFotos.ruta` devuelve None para cualquier cosa que no tenga
        la forma exacta de un archivo suyo -- un "../../.env" incluido.
        """
        _solo_local()
        archivo = album.ruta(nombre)
        if archivo is None:
            raise HTTPException(status_code=404, detail="No hay esa foto")
        # Un ano: el nombre lleva el resumen del contenido, asi que una foto
        # distinta es una URL distinta y la cache nunca queda vieja.
        return FileResponse(
            archivo, media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=31536000, immutable"},
        )

    # La version del panel: un resumen de su pagina al arrancar el servicio.
    # Si cambia (un git pull y un reinicio), la ventana del panel se recarga
    # sola: la de modo aplicacion se queda abierta entre reinicios y seguia
    # con la pagina vieja (visto el 13/10/2026 con las fotos de WhatsApp).
    pagina_del_panel = Path(__file__).resolve().parent / "panel" / "index.html"
    version_del_panel = (
        hashlib.sha1(pagina_del_panel.read_bytes()).hexdigest()[:12] if pagina_del_panel.exists() else ""
    )

    @app.get("/panel/actividad")
    async def actividad() -> dict:
        _solo_local()
        return {"en_curso": diario.en_curso(), "sesiones": len(registro), "version": version_del_panel}

    @app.get("/panel/ultimos")
    async def ultimos(limite: int = 6) -> dict:
        """Las ultimas consultas de cualquier chat, para la vista de actividad.

        Con el bot en reposo, "no hay nada corriendo" es una pantalla vacia.
        Lo que uno quiere ver en ese momento es que acaba de hacer.
        """
        _solo_local()
        return {"turnos": diario.turnos(limite=max(1, min(limite, 50)))}

    @app.get("/panel/conversaciones")
    async def conversaciones(
        chat_id: str | None = None, limite: int = 50, salto: int = 0
    ) -> dict:
        """La lista de conversaciones, o una pagina de turnos de una de ellas.

        Los turnos van por paginas y no todos de golpe: una conversacion de
        meses se pintaria entera cada vez que el panel se refresca. `salto`
        es lo que ya tiene el panel, y `hay_mas` le dice si vale la pena
        ofrecer el boton de ver los anteriores.
        """
        _solo_local()
        limite = max(1, min(limite, 500))
        if not chat_id:
            return {"chats": diario.chats()}

        salto = max(0, salto)
        # Se pide uno de mas para saber si quedan anteriores, sin contar todo.
        pagina = diario.turnos(limite=limite + 1, chat_id=chat_id, salto=salto)
        hay_mas = len(pagina) > limite
        pagina = pagina[:limite]
        calificadas = notas.de_turnos([turno.get("id", "") for turno in pagina])
        for turno in pagina:
            if turno.get("id") in calificadas:
                # Todas (texto y archivos); "calificacion" es la del texto.
                turno["calificaciones"] = calificadas[turno["id"]]
                texto = [c for c in calificadas[turno["id"]] if not c["parte"]]
                if texto:
                    turno["calificacion"] = texto[0]
        return {
            "turnos": pagina,
            "salto": salto,
            "hay_mas": hay_mas,
            "total": diario.cuantos(chat_id),
        }

    @app.get("/panel/buscar")
    async def buscar_en_conversaciones(texto: str = "", limite: int = 100) -> dict:
        """Que conversaciones dicen una palabra u oracion (pregunta o respuesta)."""
        _solo_local()
        if len(texto) > 200:
            raise HTTPException(status_code=413, detail="Busqueda demasiado larga")
        chats = await run_in_threadpool(
            diario.buscar_texto, texto, max(1, min(limite, 500))
        )
        return {"chats": chats}

    @app.post("/panel/traducir")
    async def traducir_para_el_panel(peticion: ParaTraducir) -> dict:
        """Traduce al idioma del panel una pregunta y su respuesta.

        Solo para quien revisa: lo que recibio el usuario no cambia.
        """
        _solo_local()
        if peticion.destino not in traduccion.DESTINOS:
            raise HTTPException(status_code=400, detail="Idioma de destino no soportado")
        if (
            len(peticion.textos) > MAX_TEXTOS_A_TRADUCIR
            or sum(len(x) for x in peticion.textos) > MAX_CARACTERES_A_TRADUCIR
        ):
            raise HTTPException(status_code=413, detail="Demasiado texto para traducir")
        try:
            traducidos = await run_in_threadpool(
                traduccion.traducir, peticion.textos, peticion.destino
            )
        except traduccion.TraduccionFallida as error:
            logger.warning("No se pudo traducir para el panel: %s", error)
            raise HTTPException(status_code=502, detail="No se pudo traducir")
        return {"traducciones": traducidos}

    @app.get("/panel/calificaciones")
    async def ver_calificaciones(valor: int | None = None, limite: int = 200) -> dict:
        """Las respuestas calificadas, la mas reciente primero, con su turno."""
        _solo_local()
        filas = notas.lista(valor=valor, limite=max(1, min(limite, 500)))
        # Que comentarios lee el bot ahora (los mas recientes): el panel lo
        # dice, para que no parezca que todo lo escrito sigue pesando.
        leidos = {(c["turno_id"], c["chat_id"], c["parte"]) for c in notas.comentarios()}
        for fila in filas:
            fila["turno"] = diario.buscar(fila["turno_id"])
            fila["lo_lee_el_bot"] = (fila["turno_id"], fila["chat_id"], fila["parte"]) in leidos
        return {
            "calificaciones": filas,
            **notas.cuantas(),
            # Para que el panel diga cuales cuentan sin tener su propia lista.
            "reacciones": {"buenas": REACCIONES_BUENAS, "malas": REACCIONES_MALAS},
        }

    @app.post("/panel/calificaciones/comentario")
    async def comentar_calificacion(comentario: Comentario) -> dict:
        """Por que una respuesta se califico asi. Lo lee el bot (ver
        Calificaciones.notas_para_el_bot) desde la siguiente pregunta."""
        _solo_local()
        if len(comentario.texto) > MAX_COMENTARIO:
            raise HTTPException(status_code=413, detail="Comentario demasiado largo")
        turno = await run_in_threadpool(diario.buscar, comentario.turno_id)
        actual = await run_in_threadpool(notas.de_turnos, [comentario.turno_id])
        valor = next(
            (c["valor"] for c in actual.get(comentario.turno_id, []) if c["parte"] == comentario.parte),
            None,
        )
        await run_in_threadpool(
            notas.comentar,
            comentario.turno_id,
            comentario.chat_id,
            comentario.texto,
            (turno or {}).get("pregunta", ""),
            valor,
            comentario.parte,
        )
        logger.info("Panel: comentario en %s (%d caracteres)", comentario.turno_id, len(comentario.texto.strip()))
        guardado = next(
            (c for c in await run_in_threadpool(notas.comentarios)
             if (c["turno_id"], c["chat_id"], c["parte"])
             == (comentario.turno_id, comentario.chat_id, comentario.parte)),
            None,
        )
        # Lo que quedo de verdad en la base, no el eco de lo que se mando:
        # el panel muestra "Guardado" con esto.
        return {
            "comentario": guardado["texto"] if guardado else "",
            "comentado": guardado["fecha"] if guardado else None,
            "lo_lee_el_bot": guardado is not None,
        }

    _FUENTES_DE_LOG = {"whatsapp", "api", "iniciar"}

    @app.get("/panel/logs/dias")
    async def dias_de_log() -> dict:
        """Los dias con log, con cuantos errores y avisos tuvo cada uno, y lo
        ultimo que se sabe de la conexion con WhatsApp."""
        _solo_local()
        return {
            "dias": await run_in_threadpool(registro_de_logs.dias, logs),
            "whatsapp": await run_in_threadpool(registro_de_logs.estado_whatsapp, logs),
        }

    @app.get("/panel/logs")
    async def leer_log(
        dia: str,
        fuente: str | None = None,
        solo_problemas: bool = False,
        limite: int = 500,
    ) -> dict:
        """Las ultimas lineas del log de un dia.

        Los logs los escribe iniciar.py: si el bot se arranco de otra forma
        (los dos procesos a mano), no hay archivo y la lista viene vacia.
        """
        _solo_local()
        if fuente and fuente not in _FUENTES_DE_LOG:
            raise HTTPException(status_code=400, detail=f"Fuente invalida: {fuente!r}")
        try:
            return await run_in_threadpool(
                registro_de_logs.leer,
                dia,
                logs,
                max(1, min(limite, 5000)),
                fuente or None,
                solo_problemas,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

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
    album = AlmacenDeFotos(datos / "fotos")
    calificadas = Calificaciones(datos / "calificaciones.sqlite")

    app = crear_app(
        config, lambda: SQLAgent(engine=motor), usuarios, diario, album, calificadas
    )

    # En hilos de fondo, para que el bot conteste desde el primer segundo:
    # el resumen local tarda ~7 minutos en construirse y mientras tanto todo
    # va al servidor como siempre. Ver resumen_local.py y limpieza.py.
    import limpieza

    resumen_local.mantener_al_dia(lambda: motor)
    limpieza.mantener_limpia()
    # La prueba semanal: preguntas reales contra cifras de los datos (ver
    # regresion.py). Su resumen sale en el log; REGRESION=0 la apaga.
    import regresion

    regresion.mantener_semanal(lambda: motor, lambda: SQLAgent(engine=motor))

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
