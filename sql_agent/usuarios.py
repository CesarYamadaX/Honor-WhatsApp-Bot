"""Quien puede usar el bot, y en que estado esta cada quien.

Antes la lista vivia en WHATSAPP_ALLOWED_NUMBERS y cambiarla era editar el
.env y reiniciar los dos procesos. Aqui vive en un archivo que se puede
cambiar en caliente desde el panel, sin tocar la conversacion de nadie.

Los cuatro estados y que significan:

- activo       puede usar el bot.
- suspendido   no puede, temporalmente. Si se le puso fecha, al pasar esa
               fecha vuelve solo a activo sin que nadie haga nada.
- bloqueado    no puede, y no se reactiva solo: alguien tiene que quitarlo.
- (eliminado)  no es un estado sino la baja: desaparece del registro. Si
               vuelve a escribir es un desconocido mas.

Para el bot los tres primeros casos que no son `activo` son lo mismo:
silencio total, igual que un numero que nunca estuvo. Esa es la unica
propiedad de seguridad que da la lista -- cualquier respuesta, incluido un
"estas suspendido", le confirma a quien escribe que hay un bot del otro lado.
"""

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

ACTIVO = "activo"
SUSPENDIDO = "suspendido"
BLOQUEADO = "bloqueado"
ESTADOS = (ACTIVO, SUSPENDIDO, BLOQUEADO)


class ErrorDeUsuarios(ValueError):
    """La operacion pedida sobre un usuario no tiene sentido."""


def _ahora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def normalizar_numero(crudo: str) -> str:
    """Deja el numero como lo escribe WhatsApp: solo digitos.

    Se teclea de muchas formas ("+52 1 55 1234 5678", "52-155...") y sin esto
    el mismo telefono entra dos veces al registro y ninguna de las dos casa
    con lo que manda el sidecar.
    """
    numero = "".join(c for c in crudo if c.isdigit())
    if not numero:
        raise ErrorDeUsuarios(f"{crudo!r} no tiene ningun digito; no es un numero.")
    if len(numero) < 8:
        raise ErrorDeUsuarios(
            f"{crudo!r} es muy corto para ser un numero internacional "
            "(se espera algo como 5215512345678)."
        )
    return numero


@dataclass
class Usuario:
    numero: str
    nombre: str = ""
    estado: str = ACTIVO
    # Solo para suspendidos, en formato AAAA-MM-DD. None = sin fecha de
    # regreso: la suspension dura hasta que alguien la levante.
    suspendido_hasta: str | None = None
    nota: str = ""
    creado: str = field(default_factory=_ahora)
    actualizado: str = field(default_factory=_ahora)

    def vencio_la_suspension(self, hoy: date | None = None) -> bool:
        if self.estado != SUSPENDIDO or not self.suspendido_hasta:
            return False
        try:
            hasta = date.fromisoformat(self.suspendido_hasta)
        except ValueError:
            return False
        return (hoy or date.today()) > hasta


class RegistroUsuarios:
    """Los usuarios del bot, guardados en un JSON.

    Un archivo y no una tabla en SQL Server: el login de la base es de solo
    lectura a proposito (es la garantia real de que el bot no escribe nada),
    asi que el registro no puede vivir ahi.
    """

    def __init__(self, archivo: Path | None = None, semilla: Iterable[str] = ()):
        self._archivo = Path(archivo) if archivo else None
        self._candado = threading.RLock()
        self._usuarios: dict[str, Usuario] = {}
        self._cargar()
        self._sembrar(semilla)

    # --- persistencia -----------------------------------------------------

    def _cargar(self) -> None:
        if self._archivo is None or not self._archivo.exists():
            return
        try:
            datos = json.loads(self._archivo.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Un archivo corrupto no puede dejar al bot sin allowlist y
            # atendiendo a cualquiera: se arranca vacio, que niega a todos.
            return
        for crudo in datos.get("usuarios", []):
            try:
                usuario = Usuario(**crudo)
            except TypeError:
                continue
            self._usuarios[usuario.numero] = usuario

    def _guardar(self) -> None:
        if self._archivo is None:
            return
        self._archivo.parent.mkdir(parents=True, exist_ok=True)
        contenido = json.dumps(
            {"usuarios": [asdict(u) for u in self._usuarios.values()]},
            indent=2,
            ensure_ascii=False,
        )
        # Escritura atomica: si el proceso muere a media escritura, el archivo
        # bueno sigue entero. Perder el registro es quedarse sin allowlist.
        temporal = self._archivo.with_suffix(self._archivo.suffix + ".tmp")
        temporal.write_text(contenido, encoding="utf-8")
        temporal.replace(self._archivo)

    def _sembrar(self, semilla: Iterable[str]) -> None:
        """Da de alta los numeros del .env que todavia no esten en el registro.

        Sirve para la primera vez, para que al estrenar esto nadie se quede
        fuera, y como respaldo: si el archivo se borra, los del .env vuelven.
        Nunca revive a un bloqueado ni cambia a nadie que ya exista.
        """
        nuevos = False
        with self._candado:
            for crudo in semilla:
                try:
                    numero = normalizar_numero(crudo)
                except ErrorDeUsuarios:
                    continue
                if numero not in self._usuarios:
                    self._usuarios[numero] = Usuario(
                        numero=numero, nota="Dado de alta desde WHATSAPP_ALLOWED_NUMBERS."
                    )
                    nuevos = True
            if nuevos:
                self._guardar()

    # --- consulta ---------------------------------------------------------

    def puede_usar(self, numero: str) -> bool:
        """La unica pregunta que le hace el bot a este registro."""
        with self._candado:
            usuario = self._usuarios.get(numero)
            if usuario is None:
                return False
            if usuario.vencio_la_suspension():
                # Se levanta sola y queda por escrito, para que el panel no
                # muestre "suspendido hasta" una fecha que ya paso.
                usuario.estado = ACTIVO
                usuario.suspendido_hasta = None
                usuario.actualizado = _ahora()
                self._guardar()
            return usuario.estado == ACTIVO

    def listar(self) -> list[Usuario]:
        with self._candado:
            return sorted(self._usuarios.values(), key=lambda u: (u.estado, u.numero))

    def obtener(self, numero: str) -> Usuario | None:
        with self._candado:
            return self._usuarios.get(numero)

    # --- altas y bajas ----------------------------------------------------

    def agregar(self, crudo: str, nombre: str = "", nota: str = "") -> Usuario:
        numero = normalizar_numero(crudo)
        with self._candado:
            if numero in self._usuarios:
                raise ErrorDeUsuarios(f"{numero} ya esta en la lista.")
            usuario = Usuario(numero=numero, nombre=nombre.strip(), nota=nota.strip())
            self._usuarios[numero] = usuario
            self._guardar()
            return usuario

    def _cambiar(self, crudo: str, **campos) -> Usuario:
        numero = normalizar_numero(crudo)
        with self._candado:
            usuario = self._usuarios.get(numero)
            if usuario is None:
                raise ErrorDeUsuarios(f"{numero} no esta en la lista.")
            for clave, valor in campos.items():
                setattr(usuario, clave, valor)
            usuario.actualizado = _ahora()
            self._guardar()
            return usuario

    def bloquear(self, crudo: str, nota: str = "") -> Usuario:
        return self._cambiar(
            crudo, estado=BLOQUEADO, suspendido_hasta=None, nota=nota.strip()
        )

    def suspender(self, crudo: str, hasta: str | None = None, nota: str = "") -> Usuario:
        """Suspende, opcionalmente hasta una fecha (AAAA-MM-DD)."""
        if hasta:
            try:
                limite = date.fromisoformat(hasta)
            except ValueError as error:
                raise ErrorDeUsuarios(
                    f"{hasta!r} no es una fecha valida; se espera AAAA-MM-DD."
                ) from error
            if limite < date.today():
                raise ErrorDeUsuarios(
                    f"La fecha {hasta} ya paso: la suspension nacería vencida."
                )
        return self._cambiar(
            crudo, estado=SUSPENDIDO, suspendido_hasta=hasta or None, nota=nota.strip()
        )

    def reactivar(self, crudo: str) -> Usuario:
        return self._cambiar(crudo, estado=ACTIVO, suspendido_hasta=None)

    def eliminar(self, crudo: str) -> None:
        numero = normalizar_numero(crudo)
        with self._candado:
            if numero not in self._usuarios:
                raise ErrorDeUsuarios(f"{numero} no esta en la lista.")
            del self._usuarios[numero]
            self._guardar()
