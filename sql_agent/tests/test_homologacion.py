"""El modelo homologado SIEMPRE en los resultados (pedido el 8/10/2026)."""

import pandas as pd

import homologacion
from homologacion import Catalogo, con_modelo

CAT = Catalogo(
    por_final={"APPLE IPHONE 17, 256 GB, AZUL": "APPLE IPHONE 17 256GB 5G",
               "SEARS APPLE IPHONE 17 PRO MAX 512GB": "APPLE IPHONE 17 PRO MAX 512GB 5G"},
    por_descripcion={"APPLE 5G IPHONE 17 256 AZUL PV IU": "APPLE IPHONE 17 256GB 5G",
                     "HONOR X6C 256GB": "HONOR X6C 256GB 4G"},
    por_codigo_att={"N.HNX6C256": "HONOR X6C 256GB 4G"},
)


def test_precios_con_su_modelo_al_lado_de_la_descripcion():
    df = pd.DataFrame({
        "Retail": ["Liverpool Offline", "Telcel CAC", "Sears", "Coppel"],
        "Material Description": [" apple iphone 17,  256 GB, AZUL", "APPLE 5G IPHONE 17 256 AZUL PV IU",
                                 "SEARS Apple iPhone 17 Pro Max 512GB", "ALGO QUE NO ESTA"],
        "Precio Venta": [23157, 19999, 43156, 1],
    })
    salida = con_modelo(df, CAT)
    assert list(salida.columns) == ["Retail", "Material Description", "Modelo", "Precio Venta"]
    assert salida["Modelo"].tolist()[:3] == [
        "APPLE IPHONE 17 256GB 5G", "APPLE IPHONE 17 256GB 5G", "APPLE IPHONE 17 PRO MAX 512GB 5G"]
    assert pd.isna(salida["Modelo"].iloc[3])


def test_la_venta_etl_con_el_sku_delante_y_att_por_codigo():
    df = pd.DataFrame({"Material_Description": ["6943279 HONOR X6C 256GB", "N.HNX6C256 AT&T Dealers HONOR X6C"],
                       "QTY": [3, 4]})
    assert con_modelo(df, CAT)["Modelo"].tolist() == ["HONOR X6C 256GB 4G", "HONOR X6C 256GB 4G"]


def test_no_toca_lo_que_ya_trae_modelo_ni_lo_que_no_tiene_descripcion():
    con = pd.DataFrame({"Material_Description": ["HONOR X6C 256GB"], "Modelo": ["ya"]})
    assert con_modelo(con, CAT) is con
    sin = pd.DataFrame({"Retail": ["Coppel"], "QTY": [1]})
    assert con_modelo(sin, CAT) is sin
    nada_casa = pd.DataFrame({"Descripcion": ["LAPTOP X"], "QTY": [1]})
    assert con_modelo(nada_casa, CAT) is nada_casa


def test_conserva_los_attrs_de_la_hoja():
    df = pd.DataFrame({"Desc Sku": ["HONOR X6C 256GB"], "QTY": [1]})
    df.attrs["agrupado"] = True
    assert con_modelo(df, CAT).attrs == {"agrupado": True}


def test_el_excel_lleva_la_columna(tmp_path, monkeypatch):
    from excel_exporter import ExcelExporter
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    monkeypatch.setattr(homologacion, "catalogo", lambda: CAT)
    ruta = ExcelExporter.export_to_excel(
        pd.DataFrame({"Material Description": ["HONOR X6C 256GB"] * 30, "Precio Venta": range(30)}))
    assert "Modelo" in pd.read_excel(ruta).columns


def test_sin_base_sale_como_estaba():
    df = pd.DataFrame({"Material Description": ["HONOR X6C 256GB"]})
    assert con_modelo(df) is df


class TestComoLaVenta:
    """21/10/2026: UNICOS$ tiene el mismo iPad homologado dos veces; el
    precio tiene que salir con el modelo que tiene la venta de su cadena."""

    IPAD = Catalogo(
        {'1173978126 11" IPAD WIFI 128GB PLATA': 'APPLE IPAD WIFI 11" 128GB',
         "COP X6C": "HONOR X6C 256GB 4G"},
        {'11" IPAD WIFI 128GB PLATA': "APPLE IPAD 128GB",
         "SAMSUNG GALAXY A57, 256GB, GRIS": "SAMSUNG A57 256GB 5G"},
        {},
    )

    def test_liverpool_como_su_venta_por_descripcion_sin_codigo(self):
        texto = '1173978126 11" iPad WiFi 128GB Plata'
        assert self.IPAD.buscar(texto) == 'APPLE IPAD WIFI 11" 128GB'  # lo de antes
        assert self.IPAD.buscar_como_la_venta(texto, "LIVERPOOL") == "APPLE IPAD 128GB"

    def test_un_precio_sin_codigo_no_pierde_su_primera_palabra(self):
        assert self.IPAD.buscar_como_la_venta("SAMSUNG GALAXY A57, 256GB, GRIS", "LIVERPOOL") == "SAMSUNG A57 256GB 5G"

    def test_coppel_por_final_y_lo_demas_como_siempre(self):
        assert self.IPAD.buscar_como_la_venta("COP X6C", "COPPEL") == "HONOR X6C 256GB 4G"
        assert self.IPAD.buscar_como_la_venta("COP X6C", "AT&T") == "HONOR X6C 256GB 4G"
