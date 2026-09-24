"""Pruebas de excel_pivot.py.

openpyxl no arma PivotTables; la unica forma de tener uno de verdad --
arrastrable, refrescable, igual al que arma alguien desde el menu Insertar--
es dejar que Excel mismo lo construya, por COM. Las pruebas que necesitan
Excel se saltan solas si esta maquina no lo tiene (ver `pivot_disponible`);
las que no lo necesitan (la config, que no toca Excel) corren siempre.

Al correr estas pruebas puede aparecer en la salida un "Windows fatal
exception: code 0x800706ba/be" con su propio traceback. No es un cuelgue: es
CPython 3.13 imprimiendo una excepcion estructurada (SEH) que pywin32 ya
atrapa y convierte en un `pywintypes.com_error` normal por dentro de la
llamada COM. Se ve feo, pero las pruebas siguen y pasan; se confirmo
corriendolas varias veces seguidas con el mismo resultado.
"""

import openpyxl
import pandas as pd
import pytest

import excel_pivot


class TestConfigVentasPorTienda:
    """No necesita Excel: solo decide si el df sirve para armar el pivot."""

    def test_arma_la_config_con_las_columnas_esperadas(self):
        df = pd.DataFrame(
            {"Region": ["R9"], "Tienda": ["x"], "Marca": ["HONOR"],
             "Mes": [6], "Unidades": [10]}
        )
        config = excel_pivot.config_ventas_por_tienda(df)

        assert config["filas"] == ["Tienda", "Marca"]
        assert config["columnas"] == ["Mes"]
        assert config["medida"] == "Unidades"
        assert config["con_share"] is True

    def test_sin_las_columnas_no_hay_config(self):
        # Un dataframe a medio construir no debe intentar un pivot que va a
        # fallar seguro contra columnas que no existen.
        assert excel_pivot.config_ventas_por_tienda(pd.DataFrame({"a": [1]})) is None
        faltante = pd.DataFrame({"Tienda": ["x"], "Mes": [6]})
        assert excel_pivot.config_ventas_por_tienda(faltante) is None


REQUIERE_EXCEL = pytest.mark.skipif(
    not excel_pivot.pivot_disponible(),
    reason="Excel no esta disponible por COM en esta maquina",
)


@REQUIERE_EXCEL
class TestAgregarPivot:
    """Contra Excel de verdad: el pivot resultante tiene que ser uno real
    (con su pivotCache), no una tabla que solo se le parece, y los numeros
    tienen que cuadrar.
    """

    def _archivo_con_datos(self, tmp_path, filas):
        ruta = tmp_path / "reporte.xlsx"
        pd.DataFrame(
            filas, columns=["Tienda", "Marca", "Mes", "Unidades"]
        ).to_excel(ruta, index=False, sheet_name="Raw_Data")
        return str(ruta)

    def test_el_archivo_resultante_tiene_un_pivot_de_verdad(self, tmp_path):
        ruta = self._archivo_con_datos(
            tmp_path,
            [("Tienda A", "HONOR", 6, 10), ("Tienda A", "SAMSUNG", 6, 30)],
        )
        ok = excel_pivot.agregar_pivot(
            ruta, "Raw_Data", ["Tienda", "Marca"], ["Mes"], "Unidades"
        )

        assert ok is True
        import zipfile

        nombres = zipfile.ZipFile(ruta).namelist()
        assert any("pivotTables" in n for n in nombres)
        assert any("pivotCache" in n for n in nombres)

    def test_la_suma_y_el_share_son_correctos(self, tmp_path):
        # Honor 10 de 40 en la tienda A: share 25%.
        ruta = self._archivo_con_datos(
            tmp_path,
            [
                ("Tienda A", "HONOR", 6, 10),
                ("Tienda A", "SAMSUNG", 6, 30),
                ("Tienda B", "HONOR", 6, 5),
            ],
        )
        excel_pivot.agregar_pivot(
            ruta, "Raw_Data", ["Tienda", "Marca"], ["Mes"], "Unidades"
        )

        # Fila anidada Tienda > Marca: la tienda solo aparece en el renglon
        # que la abre, asi que hay que arrastrarla hacia las marcas de abajo
        # para no confundir el HONOR de la tienda A con el de la tienda B.
        hoja = openpyxl.load_workbook(ruta, data_only=True)["PIVOT"]
        valores = {}
        tienda_actual = None
        for fila in hoja.iter_rows(min_row=1, values_only=True):
            if fila[1] and fila[1] not in ("HONOR", "SAMSUNG"):
                tienda_actual = fila[1]
            elif fila[1] in ("HONOR", "SAMSUNG"):
                valores[(tienda_actual, fila[1])] = fila[2:4]

        assert valores[("Tienda A", "HONOR")][0] == 10
        assert round(valores[("Tienda A", "HONOR")][1], 2) == 0.25
        assert valores[("Tienda A", "SAMSUNG")][0] == 30
        assert round(valores[("Tienda A", "SAMSUNG")][1], 2) == 0.75
        assert valores[("Tienda B", "HONOR")][0] == 5

    def _pids_de_excel(self) -> set[str]:
        import subprocess

        salida = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq EXCEL.EXE", "/FO", "CSV", "/NH"],
            capture_output=True, text=True,
        )
        return {
            linea.split(",")[1].strip('"')
            for linea in salida.stdout.splitlines()
            if linea.startswith('"EXCEL.EXE"')
        }

    def test_no_deja_su_propio_proceso_de_excel_vivo(self, tmp_path):
        # El primer intento real dejo un EXCEL.EXE colgado porque un
        # workbook sin cerrar bloquea Quit() en silencio (Visible=False no
        # deja ver el dialogo que lo estaria reteniendo). No se puede exigir
        # CERO procesos EXCEL.EXE en la maquina -- el usuario puede tener el
        # suyo abierto -- asi que se compara antes/despues: los que existian
        # antes tienen que seguir, y no debe haber ninguno nuevo.
        import time

        antes = self._pids_de_excel()

        ruta = self._archivo_con_datos(tmp_path, [("A", "HONOR", 6, 1)])
        excel_pivot.agregar_pivot(
            ruta, "Raw_Data", ["Tienda", "Marca"], ["Mes"], "Unidades"
        )
        time.sleep(2)  # el kill de emergencia tiene su propio margen interno

        despues = self._pids_de_excel()
        assert despues - antes == set(), "quedo un EXCEL.EXE nuevo sin cerrar"

    def test_hoja_datos_que_no_existe_no_revienta(self, tmp_path):
        ruta = self._archivo_con_datos(tmp_path, [("A", "HONOR", 6, 1)])
        ok = excel_pivot.agregar_pivot(
            ruta, "Hoja_Que_No_Existe", ["Tienda"], ["Mes"], "Unidades"
        )
        assert ok is False


class TestDisponibilidad:
    def test_confia_en_el_cache_en_vez_de_volver_a_probar(self):
        # No tiene sentido abrir Excel en cada reporte solo para comprobar
        # que sigue instalado. Si `pivot_disponible()` no confiara en el
        # cache, esta maquina (que si tiene Excel) devolveria True aunque el
        # cache diga False.
        original = excel_pivot._disponible
        try:
            excel_pivot._disponible = False
            assert excel_pivot.pivot_disponible() is False
        finally:
            excel_pivot._disponible = original
