"""Quien puede usar el bot, y en que estado esta cada quien.

Antes la lista vivia en WHATSAPP_ALLOWED_NUMBERS y cambiarla era editar el
.env y reiniciar los dos procesos. Aqui vive en un archivo que se puede
cambiar en caliente desde el panel, sin tocar la conversacion de nadie.

Los estados y que significan:

- activo       puede usar el bot.
- suspendido   no puede, hasta que alguien lo reactive.
- (eliminado)  no es un estado sino la baja: desaparece del registro. Si
               vuelve a escribir es un desconocido mas.

Hubo un tiempo un tercer estado, `bloqueado`, y una suspension con fecha de
regreso. Se quitaron los dos: en el panel eran tres botones que hacian casi
lo mismo, y no se distinguian de un vistazo. Los registros viejos que digan
`bloqueado` se leen como suspendidos.

Para el bot, no estar `activo` es lo mismo que no estar: silencio total,
igual que un numero que nunca estuvo. Esa es la unica propiedad de seguridad
que da la lista -- cualquier respuesta, incluido un "estas suspendido", le
confirma a quien escribe que hay un bot del otro lado.
"""

import json
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Iterable

import acceso

ACTIVO = "activo"
SUSPENDIDO = "suspendido"
ESTADOS = (ACTIVO, SUSPENDIDO)

# Como se llamaba antes lo que hoy es `suspendido`.
_ESTADO_VIEJO = "bloqueado"


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
    # Nombre del archivo en la carpeta de fotos, no la imagen: el registro
    # se lee entero en cada refresco del panel y se edita a mano cuando hace
    # falta, y un base64 de 30 KB por usuario arruina las dos cosas.
    foto: str = ""
    nota: str = ""
    # Las cadenas que puede ver (claves de acceso.CADENAS). Vacio = todas,
    # que es como estaba todo el mundo antes de que esto existiera.
    cadenas: list[str] = field(default_factory=list)
    creado: str = field(default_factory=_ahora)
    actualizado: str = field(default_factory=_ahora)


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
            # Los archivos de antes traen `bloqueado` y una fecha de regreso.
            # Se leen igual: el estado se traduce y el campo sobrante se
            # descarta, en vez de dejar al registro sin cargar.
            crudo = {k: v for k, v in crudo.items() if k != "suspendido_hasta"}
            if crudo.get("estado") == _ESTADO_VIEJO:
                crudo["estado"] = SUSPENDIDO
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
        Nunca revive a un suspendido ni cambia a nadie que ya exista.
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
            return usuario is not None and usuario.estado == ACTIVO

    def cadenas_de(self, numero: str) -> frozenset[str] | None:
        """Las cadenas que puede ver, o None si todas."""
        with self._candado:
            usuario = self._usuarios.get(numero)
            return acceso.normalizar(usuario.cadenas) if usuario is not None else None

    def listar(self) -> list[Usuario]:
        with self._candado:
            return sorted(self._usuarios.values(), key=lambda u: (u.estado, u.numero))

    def obtener(self, numero: str) -> Usuario | None:
        """Una COPIA del usuario, no el que vive en el registro.

        Devolver el vivo es una trampa: quien lo guarda para comparar
        despues de un cambio se encuentra con que ya cambio bajo sus pies.
        Paso de verdad -- al quitar una foto, el nombre del archivo a borrar
        se leia despues de vaciarlo, asi que la foto se quedaba en el disco
        para siempre.
        """
        with self._candado:
            usuario = self._usuarios.get(numero)
            return replace(usuario) if usuario is not None else None

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

    def renombrar(self, crudo: str, nombre: str) -> Usuario:
        """Le cambia el nombre. Vacio lo deja sin nombre, que es valido.

        El numero no se puede cambiar: es la identidad con la que llegan los
        mensajes, asi que cambiarlo no es editar a alguien sino darlo de alta
        otra vez.
        """
        return self._cambiar(crudo, nombre=nombre.strip())

    def poner_cadenas(self, crudo: str, cadenas: Iterable[str]) -> Usuario:
        """Las cadenas que puede ver. Vacio le devuelve todas."""
        pedidas = [c.strip().upper() for c in cadenas if c and c.strip()]
        desconocidas = [c for c in pedidas if c not in acceso.CADENAS]
        if desconocidas:
            raise ErrorDeUsuarios(
                f"No conozco la cadena {', '.join(desconocidas)}. Las que hay: "
                + ", ".join(acceso.CADENAS) + "."
            )
        # En el orden de siempre y sin repetir, para que el archivo se lea igual.
        ordenadas = [c for c in acceso.CADENAS if c in set(pedidas)]
        # Marcar todas es lo mismo que no limitar.
        if len(ordenadas) == len(acceso.CADENAS):
            ordenadas = []
        return self._cambiar(crudo, cadenas=ordenadas)

    def poner_foto(self, crudo: str, archivo: str) -> Usuario:
        return self._cambiar(crudo, foto=archivo)

    def quitar_foto(self, crudo: str) -> Usuario:
        return self._cambiar(crudo, foto="")

    def suspender(self, crudo: str, nota: str = "") -> Usuario:
        """Le quita el acceso hasta que alguien lo reactive."""
        return self._cambiar(crudo, estado=SUSPENDIDO, nota=nota.strip())

    def reactivar(self, crudo: str) -> Usuario:
        return self._cambiar(crudo, estado=ACTIVO)

    def eliminar(self, crudo: str) -> None:
        numero = normalizar_numero(crudo)
        with self._candado:
            if numero not in self._usuarios:
                raise ErrorDeUsuarios(f"{numero} no esta en la lista.")
            del self._usuarios[numero]
            self._guardar()
