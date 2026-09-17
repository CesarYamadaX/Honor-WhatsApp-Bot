"""Proyeccion de ventas a partir de una serie mensual.

El metodo no se eligio por gusto: se midieron tres contra los meses que ya
ocurrieron, reconstruyendo el pronostico en cada punto de corte de la historia
de Coppel (32 meses completos, horizonte de 3 meses, 51 comparaciones):

    promedio de 3 meses     20.5% de error medio
    tendencia a 12 meses    19.7%
    mismo mes + crecimiento  8.8%   <- el que se usa

La diferencia viene de la estacionalidad, que en estos datos es fuerte:
diciembre casi duplica a febrero. Una tendencia lineal la ignora y falla justo
en los meses que mas importan.
"""

import statistics
from dataclasses import dataclass

# Un mes que apenas es una fraccion de los anteriores no termino todavia. Sin
# esto el pronostico arranca desde un valle inventado: en produccion, el mes
# en curso de Coppel marcaba 37 788 contra ~500 000 de los meses cerrados.
# El umbral se valido contra la historia completa de Coppel y Elektra: no
# marca ningun mes legitimo, y el mas bajo de verdad quedo en 59%.
FRACCION_DE_MES_INCOMPLETO = 0.40

# Con menos de esto no hay un mismo mes del anio anterior con que comparar.
MESES_PARA_ESTACIONALIDAD = 13

# Error medio medido del metodo estacional en el backtest. Se le dice al
# usuario: un pronostico sin margen invita a leerlo como un dato cerrado.
ERROR_TIPICO = 0.09


@dataclass
class Pronostico:
    meses: list[tuple[int, int, float]]  # (anio, mes, proyeccion)
    metodo: str
    descartado: tuple[int, int] | None  # el mes incompleto que se dejo fuera
    confiable: bool


def _ultimo_mes_incompleto(serie: list[tuple[int, int, float]]) -> bool:
    if len(serie) < 4:
        return False
    referencia = statistics.median(total for _, _, total in serie[-4:-1])
    return referencia > 0 and serie[-1][2] / referencia < FRACCION_DE_MES_INCOMPLETO


def _siguientes(anio: int, mes: int, pasos: int):
    for paso in range(1, pasos + 1):
        yield anio + (mes - 1 + paso) // 12, (mes - 1 + paso) % 12 + 1


def _estacional(serie, pasos):
    """Mismo mes del anio pasado, ajustado por el crecimiento reciente."""
    por_mes = {(anio, mes): total for anio, mes, total in serie}

    # El crecimiento sale de comparar los ultimos seis meses contra los mismos
    # seis del anio anterior: recoge la tendencia sin que un mes raro la
    # domine.
    recientes, previos = [], []
    for anio, mes, total in serie[-6:]:
        anterior = por_mes.get((anio - 1, mes))
        if anterior:
            recientes.append(total)
            previos.append(anterior)
    if not previos:
        return None
    factor = sum(recientes) / sum(previos)

    ultimo_anio, ultimo_mes = serie[-1][0], serie[-1][1]
    proyeccion = []
    for anio, mes in _siguientes(ultimo_anio, ultimo_mes, pasos):
        base = por_mes.get((anio - 1, mes))
        if base is None:
            return None
        proyeccion.append((anio, mes, base * factor))
    return proyeccion


def _tendencia(serie, pasos):
    """Recta sobre los ultimos doce meses. Peor, pero no necesita un anio."""
    ultimos = serie[-12:]
    n = len(ultimos)
    xs = list(range(n))
    ys = [total for _, _, total in ultimos]
    media_x, media_y = statistics.mean(xs), statistics.mean(ys)
    denominador = sum((x - media_x) ** 2 for x in xs)
    pendiente = (
        sum((x - media_x) * (y - media_y) for x, y in zip(xs, ys)) / denominador
        if denominador
        else 0.0
    )
    interseccion = media_y - pendiente * media_x

    ultimo_anio, ultimo_mes = serie[-1][0], serie[-1][1]
    proyeccion = []
    for paso, (anio, mes) in enumerate(_siguientes(ultimo_anio, ultimo_mes, pasos), 1):
        # max(0): una recta a la baja puede cruzar el cero y nadie vende
        # cantidades negativas.
        proyeccion.append((anio, mes, max(0.0, interseccion + pendiente * (n - 1 + paso))))
    return proyeccion


def proyectar(serie: list[tuple[int, int, float]], meses: int = 3) -> Pronostico | None:
    """Proyecta `meses` hacia adelante. None si la serie no da para nada.

    `serie` son tuplas (anio, mes, total) ordenadas. El ultimo mes se descarta
    si esta incompleto.
    """
    if not serie or meses < 1:
        return None
    serie = sorted(serie)

    descartado = None
    if _ultimo_mes_incompleto(serie):
        descartado = (serie[-1][0], serie[-1][1])
        serie = serie[:-1]

    if len(serie) < 2:
        return None

    if len(serie) >= MESES_PARA_ESTACIONALIDAD:
        estacional = _estacional(serie, meses)
        if estacional:
            return Pronostico(estacional, "mismo mes del anio anterior ajustado por el crecimiento reciente", descartado, True)

    return Pronostico(
        _tendencia(serie, meses),
        f"tendencia de los ultimos {min(len(serie), 12)} meses",
        descartado,
        # Sin un anio completo no hay estacionalidad, y en estos datos la
        # estacionalidad es lo que mas pesa: diciembre casi duplica a febrero.
        False,
    )
