"""Cualquier resultado con dimension, periodo y medida lleva su pivot (7/10/2026).

Antes solo lo llevaban los reportes de una cadena y, desde ese mismo dia,
los de tiendas por mes. Las comparaciones por cadena, los "por modelo por
mes" y los "por region" salian sin pivot.
"""

import pandas as pd

from excel_pivot import config_generica


def test_cadena_por_mes():
    df = pd.DataFrame({"Retail": ["Coppel", "Coppel", "SEARS"], "Mes": [7, 8, 7], "Unidades": [1.0, 2, 3]})
    assert config_generica(df) == {"filas": ["Retail"], "columnas": ["Mes"], "medida": "Unidades",
                                   "nombre_medida": "Unidades", "con_share": False}


def test_la_cadena_va_primero_y_luego_la_dimension():
    df = pd.DataFrame({"Retail": ["Coppel"] * 4, "Tienda": ["a", "a", "b", "b"], "Mes": [1, 2, 1, 2],
                       "Unidades": [1.0, 2, 3, 4]})
    assert config_generica(df)["filas"] == ["Retail", "Tienda"]


def test_mes_de_varios_anios_lleva_anio_y_mes():
    df = pd.DataFrame({"Modelo": ["X6C"] * 3, "Año": [2025, 2026, 2026], "Mes": [12, 1, 2], "Unidades": [1.0, 2, 3]})
    assert config_generica(df)["columnas"] == ["Año", "Mes"]


def test_el_mismo_mes_de_dos_anios_va_por_anio():
    df = pd.DataFrame({"Region": ["R1", "R1"], "Año": [2025, 2026], "Mes": [7, 7], "Unidades": [1.0, 2]})
    assert config_generica(df)["columnas"] == ["Año"]


def test_sin_periodo_que_variar_no_hay_pivot():
    assert config_generica(pd.DataFrame({"Tienda": ["a", "b"], "Mes": [1, 1], "Unidades": [1.0, 2]})) is None
    assert config_generica(pd.DataFrame({"Retail": ["Coppel", "SEARS"], "Unidades": [1.0, 2]})) is None


def test_una_medida_que_no_se_suma_no_cuenta():
    df = pd.DataFrame({"Retail": ["Coppel", "Coppel"], "Mes": [1, 2], "Share %": [8.5, 9.0]})
    assert config_generica(df) is None


def test_prefiere_el_total():
    df = pd.DataFrame({"Tienda": ["a", "a"], "Mes": [1, 2], "Unidades Liverpool": [1.0, 2],
                       "Unidades totales": [3.0, 4], "Capa mensual": [9.0, 9]})
    assert config_generica(df)["medida"] == "Unidades totales"


def test_las_hojas_extra_solo_si_se_piden_tiendas():
    from agent import pidio_detalle_de_tiendas
    assert not pidio_detalle_de_tiendas("mandame el excel de las ventas de Honor por cadena y por mes de 2026")
    assert pidio_detalle_de_tiendas("ventas de Honor por tienda en Elektra")
    assert pidio_detalle_de_tiendas("comparando julio 2025 contra julio 2026 por region")
