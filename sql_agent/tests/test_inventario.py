"""Semanas de inventario de Telcel: la cuenta que definio el negocio.

stock de la foto / venta semanal promedio de 4 semanas, todos los canales.
Medido el 5/10/2026 con el X6c: 1 184 de stock, 1 210 vendidos en 4 semanas
-> 3.9 semanas.
"""

from datetime import date

import pandas as pd
import pytest

import catalogo
import inventario as inv

FOTO, DESDE, HASTA = date(2026, 9, 27), date(2026, 8, 30), date(2026, 9, 26)


def _stock(*filas):
    return pd.DataFrame(filas, columns=["sku", "descripcion", "stock"])


def _venta(*filas):
    return pd.DataFrame(filas, columns=["sku", "descripcion_venta", "venta"])


_HOMOLOGADO = pd.DataFrame(
    [("HONOR 4G NIC-LX3 X6C 256GB CIAN IU", "HONOR X6C 256GB 4G"),
     ("HONOR 4G NIC-LX3 X6C 256GB BLANCO IU", "HONOR X6C 256GB 4G"),
     ("HONOR 5G DNY-NX9 400 DOR HB COMBO X6C IU", "HONOR 400 512GB 5G")],
    columns=["descripcion", "modelo"],
)


def _x6c(texto="X6C"):
    return inv.armar(
        _stock(("71002231", "HONOR 4G NIC-LX3 X6C 256GB CIAN IU", 579.0),
               ("71002232", "HONOR 4G NIC-LX3 X6C 256GB BLANCO IU", 594.0),
               ("71002296", "HONOR 5G DNY-NX9 400 DOR HB COMBO X6C IU", 12.0)),
        _venta(("71002231", "71002231 HONOR 4G NIC-LX3 X6C 256GB CIAN IU", 508.0),
               ("71002232", "71002232 HONOR 4G NIC-LX3 X6C 256GB BLANCO IU", 702.0)),
        _HOMOLOGADO, FOTO, DESDE, HASTA, texto=texto,
    )


class TestLaCuenta:
    def test_stock_entre_venta_semanal_de_cuatro_semanas(self):
        r = _x6c()
        assert r.stock == 1173.0
        assert r.venta_semanal == pytest.approx(1210 / 4)
        assert r.semanas == pytest.approx(1173 / (1210 / 4))

    def test_los_colores_se_juntan_en_su_modelo(self):
        r = _x6c()
        assert r.tabla["Modelo"].tolist() == ["HONOR X6C 256GB 4G"]

    def test_un_combo_de_otro_modelo_que_menciona_x6c_no_cuenta(self):
        # "HONOR 400 ... COMBO X6C" es un HONOR 400 que regala un X6C.
        assert "HONOR 400 512GB 5G" not in _x6c().tabla["Modelo"].tolist()

    def test_sin_modelo_pedido_va_todo(self):
        assert "HONOR 400 512GB 5G" in _x6c(texto="").tabla["Modelo"].tolist()

    def test_stock_sin_venta_no_divide_entre_cero(self):
        r = inv.armar(_stock(("1", "HONOR 4G LBU-LX3 X6E 128GB NEGRO IU", 8955.0)),
                      _venta(), _HOMOLOGADO, FOTO, DESDE, HASTA)
        assert r.semanas is None
        assert r.sin_venta == ["HONOR X6E 128GB 4G (sin homologar)"]
        assert "sin venta" in inv.como_texto(r)

    def test_venta_sin_stock_es_cero_semanas(self):
        r = inv.armar(_stock(), _venta(("9", "9 HONOR 4G ANY-LX3 X9 128GB NEGRO", 8.0)),
                      _HOMOLOGADO, FOTO, DESDE, HASTA)
        assert r.semanas == 0


class TestNombres:
    def test_un_modelo_sin_homologar_se_dice(self):
        # X6E y X7E no estaban en UNICOS$ en octubre de 2026. Pedido el
        # 9/10/2026: siempre el nombre homologado; si no lo hay, se dice, no
        # se arma uno que parezca del catalogo.
        assert inv.nombre_de_modelo("HONOR 4G LBU-LX3 X6E 128GB NEGRO IU", {}) == "HONOR X6E 128GB 4G (sin homologar)"
        assert inv.nombre_de_modelo("HONOR 4G LAB-LX3 X7E 256GB NARANJA IU", {}) == "HONOR X7E 256GB 4G (sin homologar)"

    def test_lo_que_no_se_entiende_va_con_su_texto_y_marcado(self):
        assert inv.nombre_de_modelo("ACCESORIO RARO", {}) == "ACCESORIO RARO (sin homologar)"

    def test_casa_con_el_catalogo_normalizado_o_por_la_venta(self):
        from homologacion import Catalogo
        cat = Catalogo({}, {"HONOR 4G NIC-LX3 X6C 256GB VERDE IU": "HONOR X6C 256GB 4G"}, {})
        # Espacios de mas en el inventario.
        assert inv.nombre_de_modelo(" HONOR 4G NIC-LX3  X6C 256GB VERDE IU", {}, cat) == "HONOR X6C 256GB 4G"
        # El inventario lo escribe distinto; la venta del mismo SKU casa.
        assert inv.nombre_de_modelo("X6C VERDE", {}, cat, "7100 HONOR 4G NIC-LX3 X6C 256GB VERDE IU") == "HONOR X6C 256GB 4G"

    def test_la_respuesta_avisa_los_que_no_estan_en_unicos(self):
        r = inv.armar(_stock(("1", "HONOR 4G LBU-LX3 X6E 128GB NEGRO IU", 10.0)),
                      _venta(("1", "1 HONOR 4G LBU-LX3 X6E 128GB NEGRO IU", 8.0)),
                      _HOMOLOGADO, FOTO, DESDE, HASTA)
        assert r.sin_homologar == ["HONOR X6E 128GB 4G (sin homologar)"]
        assert "NO estan en UNICOS$" in inv.como_texto(r)


class TestComoLoLeeElModelo:
    def test_dice_la_foto_la_ventana_y_la_formula(self):
        texto = inv.como_texto(_x6c())
        assert "27/09/2026" in texto and "30/08/2026" in texto and "26/09/2026" in texto
        assert "todos los" in texto and "stock / venta semanal promedio" in texto

    def test_avisa_cuando_pasa_de_un_ano(self):
        r = inv.armar(_stock(("1", "HONOR 4G LAB-LX3 X7E 256GB NARANJA IU", 6243.0)),
                      _venta(("1", "1 HONOR 4G LAB-LX3 X7E 256GB NARANJA IU", 52.0)),
                      _HOMOLOGADO, FOTO, DESDE, HASTA)
        assert "lanzamiento reciente" in inv.como_texto(r)

    def test_avisa_si_la_venta_no_llega_a_la_foto(self):
        r = _x6c()
        r.recortada = True
        assert "todavia no llega" in inv.como_texto(r)


def test_la_ficha_del_inventario_manda_a_la_herramienta():
    nota = catalogo.NOTAS_POR_OBJETO["TELCEL_INV_B2B"]
    assert "semanas_de_inventario" in nota and "almacenes" in nota
