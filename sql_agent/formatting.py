"""Decide como entregar un resultado de SQL por WhatsApp."""

from dataclasses import dataclass

import pandas as pd

# Arriba de este numero de filas, el resultado se manda como Excel en vez de
# como texto: una tabla mas larga es ilegible en la pantalla de un telefono.
MAX_ROWS_AS_TEXT = 15

# Cuanto de un resultado largo LEE EL MODELO. Es otra cosa que lo de arriba:
# el texto que arma este modulo no le llega al usuario, se lo devuelve la
# herramienta al modelo, y el modelo escribe el mensaje con eso. Antes, con
# mas de 15 filas, el modelo solo recibia "son demasiados, te los mando en
# Excel" -- ni un numero -- y contestaba igual. Visto el 30 de septiembre de
# 2026: comparando julio 2026 contra julio 2025, la consulta de 2026 dio 16
# filas y le llego en blanco; el modelo presento los numeros de 2025 como si
# fueran de 2026, invento cifras redondas para 2025 (45 000, 7 500, 1 200...)
# y concluyo que Honor "crecio en todas las cadenas".
#
# Con estos topes ve todo lo que cabe en un resumen, y cuando no cabe se le
# dice cuantas filas NO vio para que no hable de ellas.
MAX_FILAS_PARA_EL_MODELO = 60
MAX_CARACTERES_PARA_EL_MODELO = 8000


@dataclass
class ResultadoFormateado:
    texto: str
    necesita_excel: bool


def _celda(valor) -> str:
    if valor is None:
        return "(vacio)"
    try:
        if pd.isna(valor):
            return "(vacio)"
    except (TypeError, ValueError):
        # pd.isna sobre una celda que contiene una lista o un array
        # devuelve un array, no un booleano: esa celda no es un nulo.
        pass
    return str(valor)


def formatear_resultado(df: pd.DataFrame) -> ResultadoFormateado:
    """Convierte un DataFrame en el texto que vera el usuario.

    Un resultado corto se rinde como texto. Uno largo solo se anuncia: las
    filas viajan en el Excel, y el texto no las repite.
    """
    if df is None or df.empty:
        return ResultadoFormateado("Sin resultados.", False)

    # Un unico valor (por ejemplo un COUNT) se devuelve pelado.
    if len(df) == 1 and len(df.columns) == 1:
        return ResultadoFormateado(_celda(df.iloc[0, 0]), False)

    if len(df) <= MAX_ROWS_AS_TEXT:
        return ResultadoFormateado("\n".join(_lineas(df)), False)

    # Largo: va al usuario en Excel, pero el modelo tiene que VER los datos
    # para escribir el mensaje. Ver MAX_FILAS_PARA_EL_MODELO.
    vistas: list[str] = []
    largo = 0
    for linea in _lineas(df.head(MAX_FILAS_PARA_EL_MODELO)):
        if largo + len(linea) > MAX_CARACTERES_PARA_EL_MODELO:
            break
        vistas.append(linea)
        largo += len(linea) + 1
    faltan = len(df) - len(vistas)

    encabezado = (
        f"La consulta devolvio {len(df):,} registros; van completos en el "
        "Excel que se le adjunta al usuario. "
        + (
            "Aqui estan TODOS, para que contestes con ellos:"
            if not faltan
            else f"Aqui estan los primeros {len(vistas)}:"
        )
    )
    cierre = []
    if faltan:
        cierre.append(
            f"(Hay {faltan:,} registros mas que NO ves aqui. No digas nada de "
            "ellos, no los estimes y no sumes de cabeza: si hace falta un "
            "total o un resumen de todo, pidelo con otra consulta en SQL.)"
        )
    cierre.append(
        "No copies la tabla entera en el mensaje: resume lo importante con "
        "estos numeros, tal cual; el detalle va en el Excel."
    )
    return ResultadoFormateado(
        "\n".join([encabezado, *vistas, *cierre]), True
    )


def _lineas(df: pd.DataFrame) -> list[str]:
    """Una linea numerada por fila: 'columna: valor | columna: valor'."""
    lineas = []
    for numero, (_, fila) in enumerate(df.iterrows(), start=1):
        campos = " | ".join(f"{col}: {_celda(fila[col])}" for col in df.columns)
        lineas.append(f"{numero}. {campos}")
    return lineas
