"""Pruebas de pronostico.py.

El metodo se eligio midiendo contra meses que ya ocurrieron (ver el docstring
del modulo): mismo mes del anio anterior ajustado por crecimiento se equivoca
~9%, contra ~20% de una tendencia lineal. La diferencia es la estacionalidad,
que aqui es fuerte: diciembre casi duplica a febrero.
"""

import pronostico
from pronostico import proyectar

# Historia real de Coppel (unidades por mes), que es con la que se valido el
# metodo. El ultimo mes esta incompleto a proposito: asi llega de produccion.
COPPEL = [
    (2024, 1, 735903.0), (2024, 2, 616068.0), (2024, 3, 606363.0),
    (2024, 4, 476851.0), (2024, 5, 625946.0), (2024, 6, 617700.0),
    (2024, 7, 679942.0), (2024, 8, 643832.0), (2024, 9, 613279.0),
    (2024, 10, 564657.0), (2024, 11, 765776.0), (2024, 12, 914132.0),
    (2025, 1, 610945.0), (2025, 2, 490153.0), (2025, 3, 541829.0),
    (2025, 4, 532287.0), (2025, 5, 713206.0), (2025, 6, 649749.0),
    (2025, 7, 644055.0), (2025, 8, 606012.0), (2025, 9, 578301.0),
    (2025, 10, 543276.0), (2025, 11, 754958.0), (2025, 12, 830009.0),
    (2026, 1, 577191.0), (2026, 2, 444242.0), (2026, 3, 487044.0),
    (2026, 4, 481610.0), (2026, 5, 621308.0), (2026, 6, 531063.0),
    (2026, 7, 523179.0), (2026, 8, 483430.0), (2026, 9, 37788.0),
]


class TestMesIncompleto:
    def test_descarta_el_mes_en_curso(self):
        # 37 788 contra ~500 000 de los meses cerrados: no termino todavia.
        # Incluirlo arrastraria el pronostico hacia un valle inventado.
        resultado = proyectar(COPPEL, 3)
        assert resultado.descartado == (2026, 9)

    def test_proyecta_desde_el_mes_siguiente_al_ultimo_completo(self):
        resultado = proyectar(COPPEL, 3)
        assert [(a, m) for a, m, _ in resultado.meses] == [(2026, 9), (2026, 10), (2026, 11)]

    def test_no_descarta_un_mes_bajo_pero_real(self):
        # El mas bajo de la historia real (444 242 en feb 2026) es el 59% de
        # sus vecinos: legitimo. Un umbral mal puesto lo tiraria.
        hasta_febrero = [f for f in COPPEL if (f[0], f[1]) <= (2026, 2)]
        assert proyectar(hasta_febrero, 1).descartado is None


class TestMetodo:
    def test_con_un_anio_de_historia_usa_la_estacionalidad(self):
        resultado = proyectar(COPPEL, 3)
        assert resultado.confiable is True
        assert "anio anterior" in resultado.metodo

    def test_respeta_la_estacionalidad_de_diciembre(self):
        # Diciembre es el pico del anio. Una tendencia lineal lo aplanaria;
        # este metodo tiene que proyectarlo por encima de noviembre.
        resultado = proyectar(COPPEL, 4)  # sep, oct, nov, dic
        por_mes = {m: v for _, m, v in resultado.meses}
        assert por_mes[12] > por_mes[11] > por_mes[10]

    def test_sigue_la_tendencia_a_la_baja(self):
        # Las ventas vienen cayendo anio contra anio (ene: 736k -> 611k ->
        # 577k). La proyeccion tiene que quedar por debajo del mismo mes del
        # anio pasado, no repetirlo tal cual.
        resultado = proyectar(COPPEL, 3)
        del resultado
        anterior = {(a, m): v for a, m, v in COPPEL}
        proyectado = {(a, m): v for a, m, v in proyectar(COPPEL, 3).meses}
        for (anio, mes), valor in proyectado.items():
            assert valor < anterior[(anio - 1, mes)]

    def test_sin_un_anio_de_historia_avisa_que_es_gruesa(self):
        # Elektra solo tiene 11 meses: no hay mismo-mes-del-anio-pasado.
        corta = [(2025, 10 + i, 150000.0 + i * 1000) for i in range(3)]
        resultado = proyectar(corta, 2)
        assert resultado.confiable is False
        assert "tendencia" in resultado.metodo


class TestCasosLimite:
    def test_serie_vacia(self):
        assert proyectar([], 3) is None

    def test_un_solo_mes_no_da_para_proyectar(self):
        assert proyectar([(2026, 1, 100.0)], 3) is None

    def test_la_serie_desordenada_se_ordena_sola(self):
        revuelta = list(reversed(COPPEL))
        assert proyectar(revuelta, 2).meses == proyectar(COPPEL, 2).meses

    def test_una_tendencia_a_la_baja_no_proyecta_negativos(self):
        cayendo = [(2026, m, max(0.0, 1000.0 - m * 120)) for m in range(1, 9)]
        for _, _, valor in proyectar(cayendo, 6).meses:
            assert valor >= 0

    def test_el_horizonte_minimo_es_un_mes(self):
        assert proyectar(COPPEL, 0) is None


def test_el_error_tipico_viene_del_backtest():
    # No es un numero de adorno: se le dice al usuario para que lea el
    # pronostico como un rango. Si alguien cambia el metodo, este numero
    # tiene que volver a medirse.
    assert 0 < pronostico.ERROR_TIPICO < 0.25
