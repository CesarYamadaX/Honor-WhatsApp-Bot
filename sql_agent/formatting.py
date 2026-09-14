"""Decide como entregar un resultado de SQL por WhatsApp."""

from dataclasses import dataclass

import pandas as pd

# Arriba de este numero de filas, el resultado se manda como Excel en vez de
# como texto: una tabla mas larga es ilegible en la pantalla de un telefono.
MAX_ROWS_AS_TEXT = 15


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

    if len(df) > MAX_ROWS_AS_TEXT:
        return ResultadoFormateado(
            f"La consulta devolvio {len(df):,} registros. "
            "Son demasiados para leerlos aqui, te los mando en Excel.",
            True,
        )

    # Un unico valor (por ejemplo un COUNT) se devuelve pelado.
    if len(df) == 1 and len(df.columns) == 1:
        return ResultadoFormateado(_celda(df.iloc[0, 0]), False)

    lineas = []
    for numero, (_, fila) in enumerate(df.iterrows(), start=1):
        campos = " | ".join(f"{col}: {_celda(fila[col])}" for col in df.columns)
        lineas.append(f"{numero}. {campos}")
    return ResultadoFormateado("\n".join(lineas), False)
