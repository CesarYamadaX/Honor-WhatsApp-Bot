"""Dibuja el icono de la aplicacion en PNG, a los tamaños en que se usa.

    python sql_agent/panel/hacer_iconos.py

El icono es un SVG y un SVG no se pixelea... salvo cuando alguien lo
rasteriza. En la barra de titulo y en la de tareas quien lo rasteriza es el
navegador, a 16 o 32 px, y ahi el borde del circulo sale con escalones y
los puntos se emborronan.

Asi que aqui se dibuja a mano, ocho veces mas grande, y se reduce con
Lanczos: el borde queda suavizado con los pixeles intermedios que hacen
falta, que es justo lo que el rasterizado rapido no hace. La diferencia se
ve sobre todo a 16 px.

Las coordenadas son las mismas del SVG del panel (lienzo de 200), asi que
los dos dibujan la misma marca.
"""

import pathlib

from PIL import Image, ImageDraw

MARCA = (0, 177, 255, 255)
BLANCO = (255, 255, 255, 255)
LIENZO = 200

# Cuanto mas grande se dibuja antes de reducir. Con 8 hay 64 muestras por
# pixel final: mas que suficiente, y sigue siendo instantaneo.
SUPERMUESTREO = 8

TAMANOS = (16, 20, 24, 32, 48, 64, 128, 256)

CARPETA = pathlib.Path(__file__).resolve().parent / "iconos"


def dibujar(lado: int) -> Image.Image:
    """La marca a `lado` pixeles, con el borde suavizado."""
    grande = lado * SUPERMUESTREO
    escala = grande / LIENZO
    lienzo = Image.new("RGBA", (grande, grande), (0, 0, 0, 0))
    pincel = ImageDraw.Draw(lienzo)

    def punto(x, y):
        return (x * escala, y * escala)

    # La colita va primero: el circulo le pasa por encima y el empalme no se
    # nota.
    pincel.polygon([punto(57, 151), punto(52, 194), punto(104, 160)], fill=MARCA)
    pincel.ellipse(
        [punto(100 - 81, 87 - 81), punto(100 + 81, 87 + 81)], fill=MARCA
    )
    for x in (64, 100, 136):
        pincel.ellipse(
            [punto(x - 12.5, 87 - 12.5), punto(x + 12.5, 87 + 12.5)], fill=BLANCO
        )

    return lienzo.resize((lado, lado), Image.LANCZOS)


def main() -> None:
    CARPETA.mkdir(parents=True, exist_ok=True)
    for lado in TAMANOS:
        archivo = CARPETA / f"icono-{lado}.png"
        dibujar(lado).save(archivo, optimize=True)
        print(f"  {archivo.name:<16}{archivo.stat().st_size:>6} bytes")

    # Windows prefiere un .ico con todos los tamaños dentro y elige el que
    # le toca; asi la barra de tareas no reescala nada.
    ico = CARPETA / "icono.ico"
    dibujar(256).save(ico, sizes=[(n, n) for n in TAMANOS])
    print(f"  {ico.name:<16}{ico.stat().st_size:>6} bytes")


if __name__ == "__main__":
    main()
