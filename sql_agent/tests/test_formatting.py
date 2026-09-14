import pandas as pd

from formatting import MAX_ROWS_AS_TEXT, formatear_resultado


def _df(filas):
    return pd.DataFrame(filas)


class TestFormatearResultado:
    def test_sin_filas(self):
        resultado = formatear_resultado(_df([]))
        assert resultado.necesita_excel is False
        assert "sin resultados" in resultado.texto.lower()

    def test_un_solo_valor_se_devuelve_pelado(self):
        resultado = formatear_resultado(_df([{"total": 1203}]))
        assert resultado.texto == "1203"
        assert resultado.necesita_excel is False

    def test_pocas_filas_se_rinden_como_texto(self):
        resultado = formatear_resultado(
            _df([{"tienda": "Liverpool", "ventas": 100}, {"tienda": "Sears", "ventas": 90}])
        )
        assert resultado.necesita_excel is False
        assert "Liverpool" in resultado.texto
        assert "Sears" in resultado.texto
        assert "tienda" in resultado.texto

    def test_en_el_umbral_todavia_es_texto(self):
        resultado = formatear_resultado(_df([{"n": i} for i in range(MAX_ROWS_AS_TEXT)]))
        assert resultado.necesita_excel is False

    def test_pasando_el_umbral_pide_excel(self):
        resultado = formatear_resultado(_df([{"n": i} for i in range(MAX_ROWS_AS_TEXT + 1)]))
        assert resultado.necesita_excel is True
        assert str(MAX_ROWS_AS_TEXT + 1) in resultado.texto
        assert "excel" in resultado.texto.lower()

    def test_el_resumen_no_filtra_los_datos(self):
        # Cuando va a Excel, el texto anuncia el tamano pero no vuelca las filas.
        df = _df([{"secreto": f"valor-{i}"} for i in range(MAX_ROWS_AS_TEXT + 1)])
        resultado = formatear_resultado(df)
        assert "valor-0" not in resultado.texto

    def test_nulo_none_se_muestra_vacio(self):
        # None en columna object (dtype por defecto)
        resultado = formatear_resultado(_df([{"tienda": "Sears", "ventas": None}]))
        assert "(vacio)" in resultado.texto

    def test_nulo_float_nan_se_muestra_vacio(self):
        # float('nan') en columna float64
        df = pd.DataFrame([{"valor": float("nan")}])
        resultado = formatear_resultado(df)
        assert "(vacio)" in resultado.texto

    def test_nulo_nat_se_muestra_vacio(self):
        # pd.NaT en columna datetime64
        df = pd.DataFrame([{"fecha": pd.NaT}])
        resultado = formatear_resultado(df)
        assert "(vacio)" in resultado.texto

    def test_nulo_pd_na_se_muestra_vacio(self):
        # pd.NA en columna Int64 (dtype nullable)
        df = pd.DataFrame({"total": pd.array([pd.NA], dtype="Int64")})
        resultado = formatear_resultado(df)
        assert "(vacio)" in resultado.texto
        # Verificar que NO aparece el string "<NA>" que indica que el bug existe
        assert "<NA>" not in resultado.texto
