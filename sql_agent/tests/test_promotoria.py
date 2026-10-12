"""Tiendas con promotor contra sin promotor (pedido el 12/10/2026)."""

import sqlite3

import pandas as pd
import pytest

import promotoria


def test_las_tiendas_se_ligan_con_las_reglas_de_cada_cadena():
    # Telcel: el prefijo de su region; Elektra: el numero de tienda.
    assert "LAGUNA DE MAYRAN" in promotoria.llaves_de_venta("RG09  Laguna de Mayran", "TELCEL")
    assert "#100" in promotoria.llaves_de_venta("100 EKT MEGA LA LUNA", "ELEKTRA")
    assert "#100" in promotoria.llaves_de_catalogo("0100 EKT LA LUNA", "ELEKTRA")
    assert promotoria.llaves_de_venta("L  MONTERREY", "LIVERPOOL") == ["L MONTERREY"]


def test_las_semanas_de_un_mes_van_por_su_domingo():
    # Septiembre de 2026: domingos 6, 13, 20 y 27 -> semanas 37 a 40.
    assert promotoria.semanas_del_mes(2026, 9) == ["W37", "W38", "W39", "W40"]


def test_movistar_no_se_puede():
    with pytest.raises(ValueError, match="por tienda"):
        promotoria.comparar(None, "Movistar", 2026, mes=9)


def test_la_comparacion(monkeypatch):
    tiendas = pd.DataFrame({"id": ["A", "B", "C"], "nombre": ["CENTRO", "NORTE", "SUR"], "promotor": [1, 0, 0]})
    venta = pd.DataFrame({"tienda": ["Centro", "norte", "EN LINEA"], "unidades": [30.0, 6.0, 4.0]})
    monkeypatch.setattr(promotoria, "_promotoria", lambda *a, **k: tiendas.copy())
    monkeypatch.setattr(promotoria, "_tabla", lambda *a, **k: venta.copy())
    r = promotoria.comparar(None, "COPPEL", 2026, semana=40)
    assert (r.con.tiendas, r.con.unidades) == (1, 30.0)
    assert (r.sin.tiendas, r.sin.unidades, r.sin.promedio) == (2, 6.0, 3.0)
    assert r.unidades_venta == 40.0 and round(r.cobertura) == 90
    texto = promotoria.como_texto(r)
    assert "10.0 veces" in texto and "no prueba que el promotor sea la causa" in texto
