"""Las tablas y vistas que el bot no debe consultar (revisado el 6/10/2026).

Existen, parecen buenas por el nombre, y dan otro numero, uno viejo o no
terminan. Se bloquean antes de llegar a la base, no se le pide al modelo.
"""

from sqlalchemy import create_engine

import catalogo
from agent import consultar_sql, contexto_peticion, pronosticar_ventas


def test_el_indice_las_marca():
    indice = catalogo.indice.__wrapped__()
    assert "B2BHonorGeneralReport (" in indice
    linea = indice[indice.index("B2BHonorGeneralReport ("):]
    assert linea.split(")")[0].endswith("NO USAR")


def test_la_ficha_dice_por_que_y_que_usar():
    ficha = catalogo.describir_objeto.invoke({"nombre": "INV_Telcel_B2B"})
    assert "NO USES ESTE OBJETO" in ficha and "TELCEL_INV_B2B" in ficha


def test_consultar_sql_no_la_ejecuta():
    motor = create_engine("sqlite://")  # sin tablas: si llegara, fallaria distinto
    with contexto_peticion(motor):
        salida = consultar_sql.invoke({
            "query": "SELECT SUM([Qty]) FROM [GTM BI Database].dbo.[B2BHonorGeneralReport] b "
                     "JOIN [GTM BI Database].dbo.[UNICOS$] u ON u.[X] = b.[Model]"
        })
    assert salida.startswith("No la ejecute") and "ETL_Consolidado_B2B" in salida


def test_tampoco_para_pronosticar():
    with contexto_peticion(create_engine("sqlite://")):
        salida = pronosticar_ventas.invoke({
            "sql_historico": "SELECT [YEAR], [MONTH], SUM([QTY]) FROM TBL_DB_FOR_DOS GROUP BY [YEAR], [MONTH]"
        })
    assert salida.startswith("No la ejecute") and "24/03/2026" in salida


def test_un_literal_con_el_nombre_no_cuenta():
    assert catalogo.motivo_para_evitar("vw_ETL_COPPEL") is None
    with contexto_peticion(create_engine("sqlite://")):
        salida = consultar_sql.invoke({"query": "SELECT 'B2BHonorGeneralReport' AS nombre"})
    assert not salida.startswith("No la ejecute")
