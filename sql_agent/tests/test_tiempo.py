"""Pruebas de tiempo.py: el orden cronologico de lo que se entrega.

Se pidio que "cuando se pide un archivo siempre venga ordenado, por ejemplo
de meses, primero los mas viejos a los mas recientes". El agente no lo
devuelve asi: escribe `ORDER BY SUM([QTY]) DESC` porque esta buscando los mas
vendidos, y el archivo salia con agosto arriba, enero en medio y marzo al
final.
"""

import pandas as pd

from tiempo import ES_ANIO, ES_MES, columna, enteros, ordenar_por_tiempo


class TestOrdenCronologico:
    def test_anio_y_mes_van_del_mas_viejo_al_mas_reciente(self):
        df = pd.DataFrame(
            {
                "Año": [2026, 2025, 2026, 2025],
                "Mes": [3, 12, 1, 6],
                "Total": [10, 40, 30, 20],
            }
        )
        ordenado = ordenar_por_tiempo(df)
        assert list(zip(ordenado["Año"], ordenado["Mes"])) == [
            (2025, 6),
            (2025, 12),
            (2026, 1),
            (2026, 3),
        ]

    def test_una_columna_de_fecha_tambien(self):
        df = pd.DataFrame(
            {
                "Fecha": pd.to_datetime(["2026-03-01", "2026-01-15", "2026-02-20"]),
                "QTY": [1, 2, 3],
            }
        )
        assert list(ordenar_por_tiempo(df)["QTY"]) == [2, 3, 1]

    def test_dentro_del_mes_se_conserva_el_orden_de_la_consulta(self):
        # El orden es estable a proposito: cronologico por fuera, y dentro de
        # cada mes el modelo mas vendido arriba, que es como venia la
        # consulta.
        df = pd.DataFrame(
            {
                "Año": [2026, 2026, 2026, 2026],
                "Mes": [2, 2, 1, 1],
                "Modelo": ["mas", "menos", "mas", "menos"],
                "Total": [90, 10, 80, 20],
            }
        )
        ordenado = ordenar_por_tiempo(df)
        assert list(ordenado["Total"]) == [80, 20, 90, 10]

    def test_un_resultado_sin_tiempo_se_queda_como_esta(self):
        # Un top 10 es un top 10: reordenarlo seria deshacer la respuesta.
        df = pd.DataFrame({"Retail": ["c", "a", "b"], "QTY": [30, 20, 10]})
        assert list(ordenar_por_tiempo(df)["Retail"]) == ["c", "a", "b"]

    def test_un_mes_escrito_con_letra_no_se_ordena_como_numero(self):
        # "enero" no se puede ordenar como numero, y ordenarlo alfabeticamente
        # pondria abril primero. Mejor dejarlo como venia.
        df = pd.DataFrame({"Mes": ["marzo", "enero"], "QTY": [1, 2]})
        assert list(ordenar_por_tiempo(df)["Mes"]) == ["marzo", "enero"]

    def test_un_anio_solo_tambien_se_ordena(self):
        df = pd.DataFrame({"Año": [2026, 2024, 2025], "QTY": [3, 1, 2]})
        assert list(ordenar_por_tiempo(df)["QTY"]) == [1, 2, 3]

    def test_vacio_o_none_no_revienta(self):
        assert ordenar_por_tiempo(None) is None
        vacio = pd.DataFrame()
        assert ordenar_por_tiempo(vacio).empty


class TestComoSeReconocenLasColumnas:
    def test_los_nombres_que_usa_el_agente(self):
        for nombre in ("Año", "ANIO", "year", "[Año]"):
            assert columna(pd.DataFrame(columns=[nombre]), ES_ANIO) == nombre
        for nombre in ("Mes", "month", "[Mes]"):
            assert columna(pd.DataFrame(columns=[nombre]), ES_MES) == nombre

    def test_no_confunde_una_columna_que_solo_menciona_el_mes(self):
        # "Mes_Anterior" o "Ventas del mes" no son la columna del periodo.
        for nombre in ("Mes_Anterior", "Ventas del mes", "Año_Pasado"):
            df = pd.DataFrame(columns=[nombre])
            assert columna(df, ES_MES) is None and columna(df, ES_ANIO) is None

    def test_enteros_distingue_un_mes_numerico_de_uno_escrito(self):
        assert list(enteros(pd.Series([1, 2, 12]))) == [1, 2, 12]
        assert enteros(pd.Series(["enero", "febrero"])) is None
