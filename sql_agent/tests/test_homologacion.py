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
