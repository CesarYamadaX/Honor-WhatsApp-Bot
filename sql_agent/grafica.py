"""Genera la grafica de un resultado como imagen PNG, para mandarla por chat.

Antes la grafica vivia en una segunda hoja del Excel. Se saco de ahi porque en
el telefono nadie abre la segunda hoja de un adjunto: una imagen se ve en la
conversacion misma, de un vistazo. El Excel queda de puros datos.
"""

import logging
import os
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime

import matplotlib

# Antes de importar pyplot: el proceso corre como servicio, sin escritorio.
# Con el backend por defecto, matplotlib intenta abrir una ventana y revienta.
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

import tiempo  # noqa: E402

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


@dataclass(frozen=True)
class Plan:
    """Que dibujar: unas categorias en un eje y una o mas series sobre ellas.

    Las series ya vienen resueltas -- nombre y valores alineados con las
    etiquetas -- porque no siempre son columnas del resultado: en una grafica
    de evolucion cada serie es un VALOR de una columna (un modelo, un
    retailer) y sale de pivotar la tabla.
    """

    eje: str
    etiquetas: list[str]
    series: dict[str, list]
    titulo: str
    es_temporal: bool


def _eje_de_tiempo(df: pd.DataFrame):
    """(etiquetas, nombre del eje, columnas que consume), o None sin tiempo.

    Las columnas que consume dejan de ser medidas: [Año] y [Mes] dicen
    CUANDO paso algo, no cuanto.
    """
    for nombre in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[nombre]):
            fechas = pd.to_datetime(df[nombre])
            meses = fechas.dt.strftime("%Y-%m")
            # Por MESES en cuanto la serie pasa de uno. Un eje por dias de
            # medio ano son 180 etiquetas de diez caracteres: no se lee
            # ninguna, y antes ni siquiera llegaba a dibujarse porque 180
            # puntos pasan de MAX_CATEGORIAS y la grafica se descartaba
            # entera. Agrupadas por mes son seis puntos y se lee de corrido.
            #
            # Dentro de un mismo mes se conservan los dias: ahi son pocos, y
            # un solo punto no dibuja nada.
            if meses.nunique() > 1:
                return meses, "periodo", {nombre}
            return fechas.dt.strftime("%Y-%m-%d"), str(nombre), {nombre}

    anio, mes = tiempo.columna(df, tiempo.ES_ANIO), tiempo.columna(df, tiempo.ES_MES)
    # Un [Mes] suelto, sin anio, se queda como categoria normal: son doce
    # barras y se leen igual de bien.
    if anio is None or mes is None:
        return None
    anios, meses = tiempo.enteros(df[anio]), tiempo.enteros(df[mes])
    if anios is None or meses is None:
        return None
    etiquetas = anios.astype(str) + "-" + meses.astype(str).str.zfill(2)
    return etiquetas, "periodo", {anio, mes}


# Una medida que se llame asi no se suma al juntar los dias de un mes: doce
# precios promedio sumados dan un numero que no existe. Se promedia.
_ES_PROMEDIO = re.compile(
    r"promedio|prom\b|media|average|\bavg\b|precio|price|ticket|porcentaje|"
    r"percent|\brate\b|margen|margin",
    re.IGNORECASE,
)


def _como_juntar(columna) -> str:
    return "mean" if _ES_PROMEDIO.search(str(columna)) else "sum"


def _por_periodo(etiquetas, columnas: pd.DataFrame) -> pd.DataFrame:
    """Una fila por punto del eje, en orden cronologico.

    El orden lo manda el eje, no el ORDER BY de la consulta: "los mas
    vendidos" viene ordenado por cantidad, y dibujarlo asi da una linea que va
    y viene en el tiempo.
    """
    tabla = columnas.copy()
    tabla.insert(0, "__eje", list(etiquetas))
    return tabla.groupby("__eje", sort=True).agg(
        {c: _como_juntar(c) for c in columnas.columns}
    )


def _plan_temporal(df, tiempo, medidas, categorias):
    """(Plan, motivo) para un resultado con tiempo.

    (None, None) significa que el tiempo no da una grafica y sigue la
    comparacion normal.
    """
    etiquetas, eje, _ = tiempo

    if categorias:
        # Una linea por cada valor de la columna de texto. "Ventas por mes y
        # modelo" son seis puntos de cada modelo, no una fila suelta por cada
        # combinacion: sin pivotar, el modelo salia repetido en el eje y la
        # evolucion no se veia por ningun lado.
        serie = categorias[0]
        tabla = pd.DataFrame(
            {
                "__eje": list(etiquetas),
                "__serie": [str(v) for v in df[serie]],
                "__valor": pd.to_numeric(df[medidas[0]], errors="coerce").values,
            }
        ).pivot_table(
            index="__eje",
            columns="__serie",
            values="__valor",
            aggfunc=_como_juntar(medidas[0]),
        ).sort_index()
        # pivot_table deja NaN donde no hubo fila. Se queda asi a proposito:
        # un modelo del que no hay dato ese mes no es un cero, y rellenarlo
        # dibujaria una caida a la base que nadie midio. La linea se corta.
        titulo = f"{medidas[0]} por {eje}"
    else:
        serie = None
        tabla = _por_periodo(etiquetas, df[medidas[:MAX_SERIES]])
        titulo = ", ".join(str(m) for m in tabla.columns) + f" por {eje}"

    if len(tabla) < 2:
        # Un solo punto no es una evolucion. No se rechaza: cae a la
        # comparacion normal, que con un solo mes es justo lo que toca.
        return None, None
    if len(tabla) > MAX_CATEGORIAS:
        return None, (
            f"son {len(tabla)} puntos en el eje y una grafica legible admite "
            f"{MAX_CATEGORIAS}"
        )
    if len(tabla.columns) > MAX_SERIES:
        return None, (
            f"son {len(tabla.columns)} lineas (una por cada valor de "
            f"[{serie}]) y una grafica legible admite {MAX_SERIES}: vuelve a "
            f"consultar acotando a {MAX_SERIES} para que salga la grafica"
        )

    series = {str(c): list(tabla[c]) for c in tabla.columns}
    return Plan(eje, [str(i) for i in tabla.index], series, titulo, True), None


# Lo que alguien escribe cuando quiere ver una grafica, sin acentos y en
# minusculas. El chino va aparte porque no se normaliza igual.
_PALABRAS_DE_GRAFICA = (
    "grafic", "graph", "chart", "plot", "curva", "tendencia", "evoluc",
)
_PALABRAS_DE_GRAFICA_CJK = ("图", "曲线", "趋势")


def pidio_grafica(pregunta: str) -> bool:
    """True si la pregunta pedia ver una grafica.

    Es una regla de codigo, no una instruccion del prompt, porque el prompt ya
    lo intento: el modelo tiene que marcar `entregar_archivo=True` y no lo
    marco -- a "muestrame una grafica de lineas" le respondio "te la envio" y
    no se genero ninguna imagen. Que salga la grafica no puede depender de que
    el modelo se acuerde de una bandera.
    """
    if not pregunta:
        return False
    texto = unicodedata.normalize("NFKD", pregunta.casefold())
    texto = texto.encode("ascii", "ignore").decode("ascii")
    return any(p in texto for p in _PALABRAS_DE_GRAFICA) or any(
        p in pregunta for p in _PALABRAS_DE_GRAFICA_CJK
    )


def _planear(df: pd.DataFrame):
    """Elige que graficar. Devuelve (Plan, motivo).

    El plan es None si ninguna grafica ayudaria. El motivo es el texto que se
    le devuelve al agente cuando SI puede hacer algo al respecto -- acotar la
    consulta --, y existe para que no prometa una grafica que no va adjunta.

    Las reglas salen de la guia de visualizacion: una sola fila es un numero,
    no una grafica; sin columna numerica no hay magnitud que mostrar; y una
    fecha convierte la pregunta en "como evoluciona" (linea) en vez de "quien
    es mayor" (barras).
    """
    if df is None or len(df) < 2:
        return None, None

    numericas = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    if not numericas:
        return None, None

    tiempo = _eje_de_tiempo(df)
    consumidas = tiempo[2] if tiempo else set()
    medidas = [c for c in numericas if c not in consumidas]
    if not medidas:
        return None, None
    categorias = [c for c in df.columns if c not in numericas and c not in consumidas]

    if tiempo is not None:
        plan, motivo = _plan_temporal(df, tiempo, medidas, categorias)
        if plan is not None or motivo is not None:
            return plan, motivo

    if len(df) > MAX_CATEGORIAS:
        # Demasiadas filas para mostrarlas todas: mejor sin grafica que con
        # una que solo ensena el principio de la tabla.
        return None, (
            f"son {len(df)} filas y una grafica legible admite "
            f"{MAX_CATEGORIAS}: vuelve a consultar con un TOP para que salga "
            "la grafica"
        )

    if categorias:
        eje = categorias[0]
    elif len(medidas) >= 2:
        # Todo numerico (p. ej. [Month] y [QTY] de un GROUP BY): la primera
        # hace de etiqueta y el resto son los valores.
        eje, medidas = medidas[0], medidas[1:]
    else:
        # Una sola columna numerica y nada contra que medirla.
        return None, None

    series = {str(m): list(df[m]) for m in medidas[:MAX_SERIES]}
    titulo = ", ".join(series) + f" por {eje}"
    etiquetas = [_recortar(str(v)) for v in df[eje]]
    return Plan(str(eje), etiquetas, series, titulo, False), None


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
    return grafica_del_resultado(df)[0]


def grafica_del_resultado(df: pd.DataFrame) -> tuple[str | None, str | None]:
    """(ruta del PNG, motivo por el que no se dibujo).

    El motivo existe para que el agente no prometa lo que no va adjunto: a un
    resultado que no produjo ninguna imagen le respondio al usuario "he
    preparado la grafica de lineas, te la envio".
    """
    plan, motivo = _planear(df)
    if plan is None:
        return None, motivo
    etiquetas, es_temporal = plan.etiquetas, plan.es_temporal
    valores = list(plan.series)

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
                    plan.series[nombre],
                    color=PALETA_SERIES[indice % len(PALETA_SERIES)],
                    linewidth=2,
                    label=str(nombre),
                )
        else:
            grosor = 0.8 / len(valores)
            posiciones = range(len(etiquetas))
            for indice, nombre in enumerate(valores):
                # Un solo color por serie pinta TODAS las barras igual. Es
                # deliberado: colorear cada barra segun su tamano duplicaria
                # en el color lo que la longitud ya dice, y gastaria el unico
                # canal libre que queda.
                desplazadas = [p + indice * grosor for p in posiciones]
                color = PALETA_SERIES[indice % len(PALETA_SERIES)]
                if horizontal:
                    ejes.barh(desplazadas, plan.series[nombre], height=grosor,
                              color=color, label=str(nombre))
                else:
                    ejes.bar(desplazadas, plan.series[nombre], width=grosor,
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

        ejes.set_title(plan.titulo, color=COLOR_TEXTO, fontsize=13, pad=14)

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
        return ruta, None
    finally:
        # Sin esto, cada consulta deja una figura viva y matplotlib se come la
        # memoria del proceso, que es un servicio de larga vida.
        plt.close(figura)
