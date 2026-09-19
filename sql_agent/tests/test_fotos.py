"""Pruebas de fotos.py.

Dos cosas importan aqui: que lo que llega se normalice (si no, el panel
carga megabytes para pintar un circulo de 44 px) y que el nombre con el que
se pide una foto no pueda apuntar fuera de la carpeta de fotos.
"""

import io

import pytest
from fotos import LADO, MAX_BYTES, AlmacenDeFotos, ErrorDeFoto, nombre_valido
from PIL import Image

NUMERO = "5215512345678"


def _imagen(ancho, alto, modo="RGB", formato="PNG", color=(200, 40, 40)):
    lienzo = Image.new(modo, (ancho, alto), color if modo != "RGBA" else color + (255,))
    buffer = io.BytesIO()
    lienzo.save(buffer, format=formato)
    return buffer.getvalue()


@pytest.fixture
def album(tmp_path):
    return AlmacenDeFotos(tmp_path / "fotos")


class TestNormalizar:
    def test_una_foto_enorme_se_reduce(self, album):
        # Lo que manda la gente es lo que tiene: una captura de 4000 px.
        grande = _imagen(4000, 3000)
        nombre = album.guardar(NUMERO, grande)
        guardada = Image.open(album.ruta(nombre))

        assert guardada.size == (LADO, LADO)
        assert album.ruta(nombre).stat().st_size < len(grande) / 10

    def test_se_recorta_al_centro_sin_deformar(self, album):
        # El avatar es un circulo: estirar para encajar saldria con todas
        # las caras alargadas. Se recorta, que descarta los bordes.
        nombre = album.guardar(NUMERO, _imagen(1000, 400))
        assert Image.open(album.ruta(nombre)).size == (LADO, LADO)

    def test_lo_transparente_queda_sobre_blanco(self, album):
        # JPEG no tiene transparencia; convertir a RGB de golpe la pinta de
        # negro, que sobre un avatar se ve como un borron.
        transparente = Image.new("RGBA", (300, 300), (255, 0, 0, 0))
        buffer = io.BytesIO()
        transparente.save(buffer, format="PNG")

        nombre = album.guardar(NUMERO, buffer.getvalue())
        assert Image.open(album.ruta(nombre)).getpixel((10, 10)) == (255, 255, 255)

    def test_el_nombre_cambia_cuando_cambia_la_foto(self, album):
        # Lleva el resumen del contenido: sin eso, el navegador seguiria
        # mostrando la foto anterior desde su cache.
        uno = album.guardar(NUMERO, _imagen(300, 300, color=(10, 10, 200)))
        otro = album.guardar(NUMERO, _imagen(300, 300, color=(10, 200, 10)))
        assert uno != otro
        assert uno.startswith(NUMERO) and otro.endswith(".jpg")


class TestLoQueNoEsUnaFoto:
    def test_un_archivo_cualquiera_se_rechaza(self, album):
        with pytest.raises(ErrorDeFoto):
            album.guardar(NUMERO, b"esto es un .txt con nombre de .jpg")

    def test_vacio_se_rechaza(self, album):
        with pytest.raises(ErrorDeFoto):
            album.guardar(NUMERO, b"")

    def test_algo_gigantesco_se_rechaza_antes_de_abrirlo(self, album):
        # Se mira el tamaño primero: abrir con Pillow 60 MB de basura para
        # descubrir que no es una imagen es trabajo tirado.
        with pytest.raises(ErrorDeFoto, match="MB"):
            album.guardar(NUMERO, b"x" * (MAX_BYTES + 1))


class TestNombresQueApuntanFuera:
    def test_un_nombre_con_ruta_no_resuelve_a_nada(self, album):
        # El nombre viene del navegador cuando se PIDE una foto. Sin esta
        # comprobacion, /panel/foto/../../.env serviria el .env.
        for intento in ["../../.env", "..\\..\\.env", "/etc/passwd", "algo.jpg"]:
            assert album.ruta(intento) is None, intento

    def test_solo_pasa_la_forma_que_genera_el_almacen(self):
        assert nombre_valido("5215512345678-a1b2c3d4.jpg")
        assert not nombre_valido("5215512345678-a1b2c3d4.png")
        assert not nombre_valido("5215512345678.jpg")
        assert not nombre_valido("../5215512345678-a1b2c3d4.jpg")

    def test_una_foto_que_ya_no_esta_devuelve_None(self, album):
        nombre = album.guardar(NUMERO, _imagen(100, 100))
        album.borrar(nombre)
        assert album.ruta(nombre) is None

    def test_borrar_algo_que_no_existe_no_revienta(self, album):
        album.borrar("5215512345678-deadbeef.jpg")
        album.borrar("../../.env")


class TestSinCarpeta:
    def test_sin_carpeta_lo_dice_en_vez_de_fallar_raro(self):
        # Asi lo construyen las pruebas de la API, que no guardan nada.
        with pytest.raises(ErrorDeFoto):
            AlmacenDeFotos().guardar(NUMERO, _imagen(100, 100))
