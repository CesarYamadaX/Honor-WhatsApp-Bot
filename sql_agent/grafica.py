"""Genera la grafica de un resultado como imagen PNG, para mandarla por chat.

Antes la grafica vivia en una segunda hoja del Excel. Se saco de ahi porque en
el telefono nadie abre la segunda hoja de un adjunto: una imagen se ve en la
conversacion misma, de un vistazo. El Excel queda de puros datos.
"""

import logging
import os
import uuid
from datetime import datetime

import matplotlib

# Antes de importar pyplot: el proceso corre como servicio, sin escritorio.
# Con el backend por defecto, matplotlib intenta abrir una ventana y revienta.
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

logger = logging.getLogger("sql_agent.grafica")

# Paleta categorica en orden fijo, validada con el validador de la guia de
# visualizacion contra fondo blanco: banda de luminosidad, piso de croma,
# separacion para daltonismo (peor par adyacente dE 9.1) y piso de vision
# normal (19.6) pasan. El orden es el mecanismo que garantiza esa separacion:
# nunca reordenar ni generar un color nuevo para una septima serie -- por eso
# MAX_SERIES corta antes.
PALETA_SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300")
COLOR_REJILLA = "#e1e0d9"
COLOR_EJE = "#c3c2b7"
COLOR_TEXTO = "#0b0b0b"
COLOR_TEXTO_SUAVE = "#52514e"

MAX_SERIES = 6

# Cuantas categorias puede mostrar una grafica legible. Pasado esto NO se
# recorta: no se hace grafica.
#
# Antes se dibujaban las primeras 25 filas de lo que hubiera. Con un resultado
# de 17 879 filas eso no es un resumen, es un pedazo arbitrario del principio
# de la tabla: no informa, y encima parece que informa. Una grafica que no
# muestra todo lo que se pregunto miente por omision, asi que o cabe entera o
# se manda solo el Excel.
MAX_CATEGORIAS = 40
# Ancho comodo para la pantalla de un telefono sin que las etiquetas se
# apelmacen; 150 dpi deja el texto nitido al hacer zoom.
ANCHO_PULGADAS = 10
ALTO_PULGADAS = 5.5
DPI = 150

# A partir de aqui las barras se dibujan horizontales. Con nombres como
# "HONOR MAGIC 8 LITE 512GB 5G" y veinte categorias, las etiquetas verticales
# se rotan 45 grados y se comen casi la mitad de la imagen, dejando las
# barras aplastadas arriba. En horizontal cada nombre tiene su propio renglon
# y se lee de corrido.
CATEGORIAS_PARA_HORIZONTAL = 8
LARGO_DE_ETIQUETA_PARA_HORIZONTAL = 14
MAX_LARGO_ETIQUETA = 42


def _planear(df: pd.DataFrame):
    """Elige que graficar, o None si ninguna grafica ayudaria.

    Devuelve (columna_categoria, columnas_de_valor, es_serie_temporal).

    Las reglas salen de la guia de visualizacion: una sola fila es un numero,
    no una grafica; sin columna numerica no hay magnitud que mostrar; y una
    columna de fecha convierte la pregunta en "como evoluciona" (linea) en vez
    de "quien es mayor" (barras).
    """
    if df is None or len(df) < 2:
        return None
    if len(df) > MAX_CATEGORIAS:
        # Demasiadas filas para mostrarlas todas: mejor sin grafica que con
        # una que solo ensena el principio de la tabla.
        return None

    numericas = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    if not numericas:
        return None

    fechas = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    if fechas:
        return fechas[0], numericas[:MAX_SERIES], True

    etiquetas = [c for c in df.columns if c not in numericas]
    if etiquetas:
        return etiquetas[0], numericas[:MAX_SERIES], False

    # Todo numerico (p. ej. [Month] y [QTY] de un GROUP BY): la primera hace
    # de etiqueta y el resto son los valores. Con una sola columna numerica no
    # queda nada que medir contra que.
    if len(numericas) >= 2:
        return numericas[0], numericas[1 : MAX_SERIES + 1], False
    return None


def _etiquetas(serie: pd.Series, es_temporal: bool) -> list[str]:
    if es_temporal:
        return [d.strftime("%Y-%m-%d") for d in pd.to_datetime(serie)]
    return [_recortar(str(v)) for v in serie]


def _recortar(texto: str) -> str:
    """Una etiqueta mas larga que esto empuja el area del grafico a nada."""
    if len(texto) <= MAX_LARGO_ETIQUETA:
        return texto
    return texto[: MAX_LARGO_ETIQUETA - 1] + "…"


def _conviene_horizontal(etiquetas: list[str]) -> bool:
    """True si las barras se leen mejor acostadas.

    Con nombres como "HONOR MAGIC 8 LITE 512GB 5G" y veinte categorias, las
    etiquetas verticales se rotan 45 grados y se comen casi la mitad de la
    imagen, dejando las barras aplastadas contra el borde. En horizontal cada
    nombre tiene su propio renglon y se lee de corrido.
    """
    return (
        len(etiquetas) > CATEGORIAS_PARA_HORIZONTAL
        or max((len(e) for e in etiquetas), default=0)
        > LARGO_DE_ETIQUETA_PARA_HORIZONTAL
    )


def _miles(valor, _pos=None) -> str:
    """12345678 -> "12 345 678". Sin esto el eje dice 600000 y hay que contar
    los ceros para saber si son seiscientos mil o seis millones."""
    if valor == int(valor):
        return f"{int(valor):,}".replace(",", " ")
    return f"{valor:,.1f}".replace(",", " ")


def _ruta_de_salida() -> str:
    directorio = os.environ.get("EXPORT_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "exports"
    )
    os.makedirs(directorio, exist_ok=True)
    nombre = (
        f"grafica_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.png"
    )
    # Absoluta: la ruta viaja por HTTP al sidecar de Node, que la resuelve
    # contra SU propio cwd (ver "El contrato HTTP" en el spec).
    return os.path.abspath(os.path.join(directorio, nombre))


def exportar_grafica(df: pd.DataFrame) -> str | None:
    """Dibuja el resultado y devuelve la ruta del PNG, o None si no aplica."""
    plan = _planear(df)
    if plan is None:
        return None
    categoria, valores, es_temporal = plan

    etiquetas = _etiquetas(df[categoria], es_temporal)

    horizontal = not es_temporal and _conviene_horizontal(etiquetas)
    alto = (
        max(ALTO_PULGADAS, 0.34 * len(etiquetas) * len(valores) + 1.6)
        if horizontal
        else ALTO_PULGADAS
    )

    figura, ejes = plt.subplots(figsize=(ANCHO_PULGADAS, alto), dpi=DPI)
    try:
        if es_temporal:
            for indice, nombre in enumerate(valores):
                ejes.plot(
                    etiquetas,
                    df[nombre],
                    color=PALETA_SERIES[indice % len(PALETA_SERIES)],
                    linewidth=2,
                    label=str(nombre),
                )
        else:
            grosor = 0.8 / len(valores)
            posiciones = range(len(df))
            for indice, nombre in enumerate(valores):
                # Un solo color por serie pinta TODAS las barras igual. Es
                # deliberado: colorear cada barra segun su tamano duplicaria
                # en el color lo que la longitud ya dice, y gastaria el unico
                # canal libre que queda.
                desplazadas = [p + indice * grosor for p in posiciones]
                color = PALETA_SERIES[indice % len(PALETA_SERIES)]
                if horizontal:
                    ejes.barh(desplazadas, df[nombre], height=grosor,
                              color=color, label=str(nombre))
                else:
                    ejes.bar(desplazadas, df[nombre], width=grosor,
                             color=color, label=str(nombre))

            centro = [p + (len(valores) - 1) * grosor / 2 for p in posiciones]
            if horizontal:
                ejes.set_yticks(centro)
                ejes.set_yticklabels(etiquetas)
                # La primera fila del resultado arriba: viene ordenada por la
                # consulta, y al usuario le importa la cabeza de la lista.
                ejes.invert_yaxis()
            else:
                ejes.set_xticks(centro)
                ejes.set_xticklabels(etiquetas)

        titulo = ", ".join(str(v) for v in valores) + f" por {categoria}"
        ejes.set_title(titulo, color=COLOR_TEXTO, fontsize=13, pad=14)

        # Una sola serie se nombra sola en el titulo; una leyenda de un
        # elemento es ruido. Con dos o mas es obligatoria: el color no puede
        # ser lo unico que las distinga.
        if len(valores) > 1:
            ejes.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.18),
                        ncol=min(len(valores), 4), fontsize=9, labelcolor=COLOR_TEXTO_SUAVE)

        # Rejilla y ejes de hairline, por detras de los datos: acompañan la
        # lectura sin competir con ella. Va en el eje de los VALORES, que en
        # barras horizontales es el de abajo.
        eje_de_valores = "x" if horizontal else "y"
        ejes.grid(axis=eje_de_valores, color=COLOR_REJILLA, linewidth=0.8)
        ejes.set_axisbelow(True)
        if eje_de_valores == "x":
            ejes.xaxis.set_major_formatter(_miles)
        else:
            ejes.yaxis.set_major_formatter(_miles)
        for lado in ("top", "right"):
            ejes.spines[lado].set_visible(False)
        for lado in ("left", "bottom"):
            ejes.spines[lado].set_color(COLOR_EJE)
            ejes.spines[lado].set_linewidth(0.8)
        ejes.tick_params(colors=COLOR_TEXTO_SUAVE, labelsize=9, length=0)

        # Solo en vertical: rotar las etiquetas de abajo cuando se pisarian.
        # En horizontal cada una ya tiene su renglon.
        if not horizontal and max((len(e) for e in etiquetas), default=0) > 8:
            plt.setp(ejes.get_xticklabels(), rotation=45, ha="right")

        figura.tight_layout()
        ruta = _ruta_de_salida()
        figura.savefig(ruta, facecolor="white", bbox_inches="tight")
        return ruta
    finally:
        # Sin esto, cada consulta deja una figura viva y matplotlib se come la
        # memoria del proceso, que es un servicio de larga vida.
        plt.close(figura)
