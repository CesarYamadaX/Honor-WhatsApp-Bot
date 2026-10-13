"""Las fotos de los usuarios del bot.

Lo que llega del panel es lo que el usuario tenga a mano -- una captura de
pantalla de 4000 px, una foto del telefono, un PNG con transparencia -- y lo
que el panel necesita es un circulo de 44 px. Guardar el original seria
cargar megabytes en cada refresco para pintar una miniatura, asi que aqui se
normaliza: se recorta al cuadrado del centro, se reduce y se guarda en JPEG.

Abrir la imagen con Pillow ademas la valida: lo que no sea una imagen de
verdad no pasa de aqui, aunque venga con nombre de .jpg.
"""

import hashlib
import io
import re
from pathlib import Path

from PIL import Image, UnidentifiedImageError

LADO = 256
# La foto de perfil de WhatsApp: 640 px es la que guarda WhatsApp, asi que
# mandarle mas no se veria mejor y mandarle la miniatura del panel se veria
# borrosa en el telefono.
LADO_DE_PERFIL = 640
CALIDAD = 85
# Tope de lo que se acepta recibir. Una foto de telefono ronda los 3 MB; mas
# que esto es casi seguro un archivo que no tiene nada que hacer aqui.
MAX_BYTES = 6 * 1024 * 1024

# Como se llama un archivo de foto. Lo comprueba quien SIRVE la foto, porque
# ahi el nombre viene de fuera: sin esto, un "../../.env" saldria por el
# mismo endpoint.
_NOMBRE = re.compile(r"^[0-9]{6,20}-[0-9a-f]{8}\.jpg$")


class ErrorDeFoto(ValueError):
    """La imagen no sirve y hay que decirle al usuario por que."""


def nombre_valido(nombre: str) -> bool:
    return bool(_NOMBRE.fullmatch(nombre or ""))


class AlmacenDeFotos:
    """Guarda, sirve y borra las fotos, en una carpeta propia."""

    def __init__(self, carpeta: Path | None = None):
        self.carpeta = Path(carpeta) if carpeta else None

    def guardar(self, numero: str, datos: bytes) -> str:
        """Normaliza la imagen y la guarda. Devuelve el nombre del archivo.

        El nombre lleva un resumen del contenido: al cambiar la foto cambia
        el nombre, y el navegador no se queda mostrando la anterior de su
        cache. Es el mismo truco que un `?v=` pero sin ensuciar la ruta.
        """
        if self.carpeta is None:
            raise ErrorDeFoto("No hay carpeta donde guardar las fotos.")
        contenido = cuadrada_en_jpeg(datos, LADO)
        nombre = f"{numero}-{hashlib.sha256(contenido).hexdigest()[:8]}.jpg"
        self.carpeta.mkdir(parents=True, exist_ok=True)
        (self.carpeta / nombre).write_bytes(contenido)
        return nombre

    def ruta(self, nombre: str) -> Path | None:
        """La ruta de una foto, o None si el nombre no es de aqui.

        El nombre llega desde el navegador, asi que se comprueba contra la
        forma esperada en vez de confiar: `ruta("../../.env")` devuelve None.
        """
        if self.carpeta is None or not nombre_valido(nombre):
            return None
        archivo = self.carpeta / nombre
        return archivo if archivo.exists() else None

    def borrar(self, nombre: str) -> None:
        archivo = self.ruta(nombre)
        if archivo is not None:
            archivo.unlink(missing_ok=True)


def cuadrada_en_jpeg(datos: bytes, lado: int) -> bytes:
    """Valida la imagen, la recorta al cuadrado del centro y la da en JPEG.

    Lo usan las miniaturas del panel (`LADO`) y la foto de perfil que se le
    pone al bot en WhatsApp (`LADO_DE_PERFIL`).
    """
    if not datos:
        raise ErrorDeFoto("No llego ninguna imagen.")
    if len(datos) > MAX_BYTES:
        raise ErrorDeFoto(
            f"La imagen pesa {len(datos) / 1024 / 1024:.1f} MB y el tope son "
            f"{MAX_BYTES // 1024 // 1024} MB."
        )

    try:
        imagen = Image.open(io.BytesIO(datos))
        imagen.load()
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise ErrorDeFoto("Ese archivo no es una imagen que pueda leer.") from error

    cuadrada = _recortar_al_centro(imagen).resize((lado, lado), Image.LANCZOS)
    # JPEG no tiene transparencia: lo transparente se compone sobre
    # blanco en vez de salir negro, que es lo que hace la conversion
    # directa a RGB.
    if cuadrada.mode in ("RGBA", "LA", "P"):
        cuadrada = cuadrada.convert("RGBA")
        fondo = Image.new("RGB", cuadrada.size, (255, 255, 255))
        fondo.paste(cuadrada, mask=cuadrada.split()[-1])
        cuadrada = fondo
    else:
        cuadrada = cuadrada.convert("RGB")

    buffer = io.BytesIO()
    cuadrada.save(buffer, format="JPEG", quality=CALIDAD, optimize=True)
    return buffer.getvalue()


def _recortar_al_centro(imagen: Image.Image) -> Image.Image:
    """El cuadrado mas grande que cabe, centrado.

    El avatar es un circulo: si la imagen se deformara para encajar, todas
    las caras saldrian estiradas. Recortar conserva las proporciones y deja
    fuera los bordes, que es donde menos pasa.
    """
    ancho, alto = imagen.size
    lado = min(ancho, alto)
    izquierda = (ancho - lado) // 2
    arriba = (alto - lado) // 2
    return imagen.crop((izquierda, arriba, izquierda + lado, arriba + lado))
