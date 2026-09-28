"""Lectura y validacion de la configuracion del bot de WhatsApp."""

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """La configuracion es invalida y el servicio no debe arrancar."""


SUFIJO_INDIVIDUAL = "@s.whatsapp.net"


def normalizar_chat_id(chat_id: str) -> str | None:
    """Devuelve el numero de un chat individual, o None si no lo es.

    Los chats de grupo (@g.us) y las difusiones no son atendidos, asi que
    devuelven None y el llamador los descarta igual que a un numero no
    autorizado.
    """
    if not chat_id.endswith(SUFIJO_INDIVIDUAL):
        return None
    # Baileys puede incluir el id de dispositivo: "521...:12@s.whatsapp.net"
    numero = chat_id[: -len(SUFIJO_INDIVIDUAL)].split(":", 1)[0]
    return numero if numero.isdigit() else None


def _leer_allowlist() -> frozenset[str]:
    crudo = os.getenv("WHATSAPP_ALLOWED_NUMBERS")
    if crudo is None:
        raise ConfigError(
            "WHATSAPP_ALLOWED_NUMBERS no esta definida. Sin allowlist, cualquiera "
            "que conozca el numero puede consultar la base de datos."
        )

    numeros = {parte.strip().lstrip("+") for parte in crudo.split(",")}
    numeros.discard("")
    if not numeros:
        raise ConfigError("WHATSAPP_ALLOWED_NUMBERS esta vacia.")

    for numero in sorted(numeros):
        if not numero.isdigit():
            raise ConfigError(
                f"Numero invalido en WHATSAPP_ALLOWED_NUMBERS: {numero!r}. "
                "Se espera formato internacional sin signos, por ejemplo 5215512345678."
            )
    return frozenset(numeros)


def _leer_entero(clave: str, por_defecto: int) -> int:
    crudo = os.getenv(clave)
    if crudo is None or not crudo.strip():
        return por_defecto
    try:
        return int(crudo)
    except ValueError as error:
        raise ConfigError(f"{clave} debe ser un numero entero, no {crudo!r}.") from error


@dataclass(frozen=True)
class Config:
    allowlist: frozenset[str]
    timeout_segundos: float
    ttl_minutos: int
    api_host: str
    api_port: int
    # Cuanto se espera a que la consulta en curso se de por cancelada cuando
    # llega un mensaje nuevo del mismo chat. En la practica tarda segundos
    # (el SELECT se aborta con cursor.cancel()); este plazo cubre el caso
    # feo, que es estar dentro de una llamada al modelo, que no se puede
    # interrumpir. Pasado el plazo se responde 429, como antes.
    espera_cancelacion: float = 25.0

    @classmethod
    def desde_entorno(cls) -> "Config":
        return cls(
            allowlist=_leer_allowlist(),
            # El mismo default que agent.TIMEOUT_PETICION: si difieren, el
            # agente se reparte un presupuesto que la API no le va a dar.
            timeout_segundos=_leer_entero("AGENT_TIMEOUT_SECONDS", 600),
            # 0 = sin tope por inactividad: la memoria vence a medianoche.
            ttl_minutos=_leer_entero("SESSION_TTL_MINUTES", 0),
            api_host=os.getenv("API_HOST", "127.0.0.1"),
            api_port=_leer_entero("API_PORT", 8000),
            espera_cancelacion=_leer_entero("CANCEL_WAIT_SECONDS", 25),
        )

    def autorizado(self, chat_id: str) -> bool:
        numero = normalizar_chat_id(chat_id)
        return numero is not None and numero in self.allowlist
