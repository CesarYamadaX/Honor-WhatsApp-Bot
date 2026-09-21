"""Pruebas de grafica.py.

La grafica vivia en una segunda hoja del Excel y se saco de ahi: en el
telefono nadie abre la segunda hoja de un adjunto, y una imagen se ve en la
conversacion misma. Aqui se verifica la eleccion de forma y las decisiones de
legibilidad; como se VE hay que mirarlo, y se miro al construirlo.
"""

import os

import pandas as pd
import pytest

import grafica
from grafica import exportar_grafica, grafica_del_resultado, pidio_grafica


@pytest.fixture(autouse=True)
def _exports(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))


def _plan(df):
    """El plan a secas. El motivo se revisa en TestLoQueSeLeDiceAlAgente."""
    return grafica._planear(df)[0]


class TestQueSeGrafica:
    def test_barras_para_comparar_magnitudes(self):
        df = pd.DataFrame({"Retail": ["a", "b", "c"], "QTY": [3, 1, 2]})
        plan = _plan(df)
        assert (plan.eje, list(plan.series), plan.es_temporal) == (
            "Retail",
            ["QTY"],
            False,
        )

    def test_linea_cuando_hay_una_columna_de_fecha(self):
        # Una fecha convierte la pregunta en "como evoluciona", no en "quien
        # es mayor".
        df = pd.DataFrame(
            {"Fecha": pd.date_range("2026-01-01", periods=3), "QTY": [1, 2, 3]}
        )
        assert _plan(df).es_temporal is True

    def test_todo_numerico_usa_la_primera_como_etiqueta(self):
        # Un GROUP BY por [Month] devuelve solo numeros; sin esto no habria
        # grafica para una de las preguntas mas comunes.
        df = pd.DataFrame({"Month": [1, 2, 3], "QTY": [10, 30, 20]})
        plan = _plan(df)
        assert (plan.eje, list(plan.series)) == ("Month", ["QTY"])

    @pytest.mark.parametrize(
        "df, motivo",
        [
            (pd.DataFrame({"a": ["x", "y"], "b": ["p", "q"]}), "sin columna numerica"),
            (pd.DataFrame({"Retail": ["a"], "QTY": [5]}), "una sola fila es un numero"),
        ],
    )
    def test_sin_grafica_cuando_no_ayudaria(self, df, motivo):
        assert exportar_grafica(df) is None, motivo


class TestResultadoAncho:
    """El caso que rompia: cuatro columnas -- [Año], [Mes], el modelo y la
    medida --, que es como vuelve un GROUP BY por mes y por modelo.

    Antes [Año] y [Mes] contaban como medidas: la grafica dibujaba una barra
    de valor 2026 y otra de valor 3 junto a una de 143 575, con el modelo
    repetido una vez por cada mes en el eje. Ninguna de las dos cosas que se
    habian pedido -- la evolucion y el modelo -- se veia.
    """

    def _seis_meses(self, modelos):
        filas = [
            (2026, mes, modelo, 100 + mes)
            for modelo in modelos
            for mes in range(1, 7)
        ]
        return pd.DataFrame(filas, columns=["Año", "Mes", "Modelo", "Total"])

    def test_el_anio_y_el_mes_son_el_eje_no_una_medida(self):
        plan = _plan(self._seis_meses(["A", "B"]))
        assert plan.es_temporal is True
        assert plan.etiquetas == [
            "2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06",
        ]
        # Una linea por modelo, y ni [Año] ni [Mes] entre ellas.
        assert list(plan.series) == ["A", "B"]

    def test_el_titulo_nombra_la_medida_no_las_lineas(self):
        # Con seis modelos, un titulo que los enumere no cabe; la leyenda ya
        # los nombra.
        plan = _plan(self._seis_meses(["A", "B"]))
        assert plan.titulo == "Total por periodo"

    def test_se_ordena_por_el_tiempo_no_por_el_order_by(self):
        # La consulta viene ordenada por cantidad ("los mas vendidos"):
        # dibujarla en ese orden da una linea que va y viene en el tiempo.
        df = self._seis_meses(["A"]).sort_values("Total", ascending=False)
        assert _plan(df).etiquetas == sorted(_plan(df).etiquetas)

    def test_un_solo_mes_vuelve_a_ser_una_comparacion(self):
        df = pd.DataFrame(
            {"Año": [2026, 2026], "Mes": [3, 3], "Modelo": ["A", "B"], "Total": [5, 9]}
        )
        plan = _plan(df)
        assert plan.es_temporal is False
        assert plan.eje == "Modelo"
        # Lo que NO puede pasar: que el año y el mes vuelvan a ser barras.
        assert list(plan.series) == ["Total"]

    def test_un_mes_suelto_sigue_siendo_una_categoria(self):
        # Sin año no hay periodo que ordenar: doce barras se leen igual de
        # bien, y es el comportamiento que ya habia.
        df = pd.DataFrame({"Mes": [1, 2, 3], "QTY": [10, 30, 20]})
        assert _plan(df).es_temporal is False


class TestElEjeDeTiempo:
    """Una fecha por dias no se lee en el telefono: "que el eje X sea de
    meses en lugar de por dias porque no se ve bien"."""

    def _dias(self, dias: int):
        return pd.DataFrame(
            {
                "Fecha": pd.date_range("2026-01-01", periods=dias, freq="D"),
                "QTY": range(dias),
            }
        )

    def test_medio_anio_de_dias_se_agrupa_por_mes(self):
        # 180 dias son 180 etiquetas de diez caracteres. Antes ni se
        # dibujaba: pasaban de MAX_CATEGORIAS y la grafica se descartaba
        # entera.
        plan = _plan(self._dias(180))
        assert plan is not None
        assert plan.eje == "periodo"
        assert plan.etiquetas == [f"2026-{m:02d}" for m in range(1, 7)]

    def test_el_mes_junta_lo_de_sus_dias(self):
        # Enero son los dias 0..30: la suma tiene que ser la del mes, no el
        # ultimo dia ni el primero.
        plan = _plan(self._dias(180))
        assert plan.series["QTY"][0] == sum(range(31))

    def test_dentro_de_un_mes_se_conservan_los_dias(self):
        # Ahi son pocos y se leen bien; agrupar dejaria un solo punto, que no
        # dibuja nada.
        plan = _plan(self._dias(10))
        assert plan.etiquetas[0] == "2026-01-01"
        assert len(plan.etiquetas) == 10

    def test_un_promedio_no_se_suma_al_juntar_los_dias(self):
        # Doce precios promedio sumados dan un numero que no existe.
        df = pd.DataFrame(
            {
                "Fecha": pd.date_range("2026-01-01", periods=59, freq="D"),
                "Precio promedio": [100.0] * 59,
            }
        )
        plan = _plan(df)
        # Enero y febrero: el promedio de cada mes sigue siendo 100, no 3 100.
        assert plan.series["Precio promedio"] == [100.0, 100.0]


class TestLoQueSeLeDiceAlAgente:
    """Cuando no hay grafica, el agente tiene que enterarse.

    Sin esto respondio "he preparado la grafica de lineas, te la envio" a un
    resultado que no habia producido ninguna imagen.
    """

    def test_demasiadas_lineas_dice_como_arreglarlo(self):
        filas = [
            (2026, mes, f"modelo {i}", 10 + mes)
            for i in range(grafica.MAX_SERIES + 1)
            for mes in range(1, 7)
        ]
        df = pd.DataFrame(filas, columns=["Año", "Mes", "Modelo", "Total"])
        ruta, motivo = grafica_del_resultado(df)
        assert ruta is None
        assert str(grafica.MAX_SERIES) in motivo and "[Modelo]" in motivo

    def test_demasiadas_filas_dice_como_arreglarlo(self):
        filas = grafica.MAX_CATEGORIAS + 1
        df = pd.DataFrame(
            {"R": [f"r{i}" for i in range(filas)], "QTY": list(range(filas))}
        )
        ruta, motivo = grafica_del_resultado(df)
        assert ruta is None
        assert "TOP" in motivo

    def test_lo_que_nunca_seria_grafica_no_da_motivo(self):
        # Un COUNT no se grafica y no hay nada que acotar: un aviso ahi solo
        # invitaria al agente a reintentar sin razon.
        assert grafica_del_resultado(pd.DataFrame({"total": [5]})) == (None, None)


class TestCuandoSePidioUnaGrafica:
    """La grafica se genera leyendo la PREGUNTA, no la bandera del modelo: el
    prompt le pedia marcar `entregar_archivo` y no lo marco."""

    @pytest.mark.parametrize(
        "pregunta",
        [
            "Muéstrame una gráfica de líneas con el top de ventas",
            "dame un grafico de barras de coppel",
            "show me a chart of sales by store",
            "plot the trend",
            "la tendencia de ventas de los ultimos 6 meses",
            "给我一张销售图表",
        ],
    )
    def test_se_reconoce_la_peticion(self, pregunta):
        assert pidio_grafica(pregunta) is True

    @pytest.mark.parametrize(
        "pregunta",
        ["ventas totales de elektra en 2026", "cuantos modelos hay", ""],
    )
    def test_una_pregunta_normal_no_pide_grafica(self, pregunta):
        assert pidio_grafica(pregunta) is False


class TestArchivoGenerado:
    def test_devuelve_un_png_absoluto_que_existe(self):
        # Absoluto porque la ruta viaja por HTTP al sidecar de Node, que la
        # resuelve contra SU propio cwd.
        ruta = exportar_grafica(pd.DataFrame({"R": ["a", "b"], "QTY": [1, 2]}))
        assert os.path.isabs(ruta)
        assert os.path.isfile(ruta)
        assert ruta.endswith(".png")
        with open(ruta, "rb") as fh:
            assert fh.read(8) == b"\x89PNG\r\n\x1a\n"

    def test_dos_graficas_no_se_pisan(self):
        # El timestamp resuelve al segundo: dos consultas simultaneas de dos
        # chats distintos caerian en el mismo nombre.
        df = pd.DataFrame({"R": ["a", "b"], "QTY": [1, 2]})
        assert exportar_grafica(df) != exportar_grafica(df)


class TestLegibilidad:
    def test_horizontal_cuando_las_etiquetas_son_largas(self):
        # Con "HONOR MAGIC 8 LITE 512GB 5G" y veinte categorias, las etiquetas
        # verticales rotadas se comen media imagen y aplastan las barras.
        largas = [f"HONOR MAGIC {i} LITE 512GB 5G" for i in range(10)]
        assert grafica._conviene_horizontal(largas) is True

    def test_horizontal_cuando_son_muchas(self):
        assert grafica._conviene_horizontal([str(i) for i in range(20)]) is True

    def test_vertical_cuando_son_pocas_y_cortas(self):
        assert grafica._conviene_horizontal(["Ene", "Feb", "Mar"]) is False

    def test_las_etiquetas_larguisimas_se_recortan(self):
        largo = "X" * 200
        recortado = grafica._recortar(largo)
        assert len(recortado) <= grafica.MAX_LARGO_ETIQUETA
        assert recortado.endswith("…")

    def test_los_miles_se_separan(self):
        # Sin esto el eje dice 600000 y hay que contar ceros para saber si son
        # seiscientos mil o seis millones.
        assert grafica._miles(600000) == "600 000"
        assert grafica._miles(1234567) == "1 234 567"

    def test_sin_grafica_cuando_hay_demasiadas_filas(self):
        # Antes dibujaba las primeras 25 de lo que hubiera. Con 17 879 filas
        # eso no es un resumen, es un pedazo arbitrario del principio de la
        # tabla: no informa y parece que informa. O cabe entera o no se hace.
        filas = grafica.MAX_CATEGORIAS + 1
        df = pd.DataFrame({"R": [f"r{i}" for i in range(filas)], "QTY": list(range(filas))})
        assert exportar_grafica(df) is None

    def test_justo_en_el_limite_si_se_grafica(self):
        filas = grafica.MAX_CATEGORIAS
        df = pd.DataFrame({"R": [f"r{i}" for i in range(filas)], "QTY": list(range(filas))})
        assert os.path.isfile(exportar_grafica(df))

    def test_la_grafica_muestra_todas_las_filas(self):
        # Si existe, no esconde nada: el titulo ya no puede decir
        # "primeras N de M" porque ese caso no ocurre.
        df = pd.DataFrame({"R": ["a", "b", "c"], "QTY": [3, 1, 2]})
        ruta = exportar_grafica(df)
        assert os.path.isfile(ruta)
