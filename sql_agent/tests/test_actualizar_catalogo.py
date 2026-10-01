"""Pruebas del script que relee el esquema y de como el bot ve lo que trae.

Antes el catalogo se armaba a mano desde un HTML y el script nunca se guardo:
cuando la base cambio el 30 de septiembre de 2026 (las vistas perdieron [Day]
y ganaron [WEEK]; cuatro vistas quedaron rotas) no habia como ponerse al dia.
"""

import actualizar_catalogo as ac
import catalogo


class _Conexion:
    """Responde a las consultas del script como lo haria el servidor."""

    def __init__(self, objetos, columnas, rotas=()):
        self.objetos, self.columnas, self.rotas = objetos, columnas, set(rotas)

    def exec_driver_sql(self, sql, parametros=()):
        if "INFORMATION_SCHEMA.TABLES" in sql:
            return _Resultado(self.objetos)
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return _Resultado(self.columnas.get(parametros[0], []))
        if "sys.partitions" in sql:
            return _Resultado([(1234,)])
        if "SELECT TOP 0" in sql:
            for rota in self.rotas:
                if rota in sql:
                    raise RuntimeError("Invalid object name 'dbo.Retail_StoreMap'.")
            return _Resultado([])
        raise AssertionError(sql)


class _Resultado:
    def __init__(self, filas):
        self.filas = filas

    def fetchall(self):
        return self.filas

    def scalar(self):
        return self.filas[0][0] if self.filas else None


class TestLeerElEsquema:
    def test_tipos_como_los_escribe_el_catalogo(self):
        assert ac._tipo(("nvarchar", 255, None, None)) == "nvarchar(255)"
        assert ac._tipo(("varchar", -1, None, None)) == "varchar(max)"
        assert ac._tipo(("decimal", None, 18, 2)) == "decimal(18,2)"
        assert ac._tipo(("int", None, 10, 0)) == "int"

    def test_los_respaldos_no_entran_al_catalogo(self):
        # bkp_LIVERPOOL_B2B_20260930 aparecio el mismo dia que cambiaron las
        # vistas de Liverpool: dejarlo es invitar al modelo a sumar una copia.
        conexion = _Conexion(
            [("LIVERPOOL_B2B$", "BASE TABLE"), ("bkp_LIVERPOOL_B2B_20260930", "BASE TABLE")],
            {"LIVERPOOL_B2B$": [("STORE", "nvarchar", 255, None, None)]},
        )
        assert [o["nombre"] for o in ac.leer_base(conexion, "RAW_DATA_B2B")] == ["LIVERPOOL_B2B$"]

    def test_una_vista_que_no_resuelve_queda_marcada(self):
        conexion = _Conexion(
            [("vw_Coppel_Monthly_Excel", "VIEW"), ("vw_bien", "VIEW")],
            {"vw_Coppel_Monthly_Excel": [("a", "int", None, 10, 0)], "vw_bien": [("a", "int", None, 10, 0)]},
            rotas=["vw_Coppel_Monthly_Excel"],
        )
        objetos = {o["nombre"]: o for o in ac.leer_base(conexion, "GTM BI Database")}
        assert "Retail_StoreMap" in objetos["vw_Coppel_Monthly_Excel"]["rota"]
        assert "rota" not in objetos["vw_bien"]

    def test_las_tablas_traen_filas_y_las_vistas_no(self):
        conexion = _Conexion(
            [("UNICOS$", "BASE TABLE"), ("vw", "VIEW")],
            {"UNICOS$": [("a", "int", None, 10, 0)], "vw": [("a", "int", None, 10, 0)]},
        )
        objetos = {o["nombre"]: o for o in ac.leer_base(conexion, "GTM BI Database")}
        assert objetos["UNICOS$"]["filas"] == 1234
        assert objetos["vw"]["filas"] is None


class TestComparar:
    def _catalogo(self, columnas, rota=None):
        objeto = {"nombre": "ETL_Consolidado_B2B", "tipo": "vista", "filas": None,
                  "columnas": [{"nombre": n, "tipo": t} for n, t in columnas]}
        if rota:
            objeto["rota"] = rota
        return {"bases": [{"nombre": "RAW_DATA_B2B", "objetos": [objeto]}]}

    def test_dice_que_columnas_aparecieron_se_fueron_o_cambiaron(self):
        # Lo que paso el 30 de septiembre de 2026 con las vistas de venta.
        antes = self._catalogo([("Day", "int"), ("Store", "float")])
        ahora = self._catalogo([("WEEK", "int"), ("Store", "nvarchar(511)")])
        cambios = " | ".join(ac.comparar(antes, ahora))
        assert "columna nueva [WEEK]" in cambios
        assert "ya no tiene [Day]" in cambios
        assert "[Store] cambio de float a nvarchar(511)" in cambios

    def test_objetos_nuevos_idos_y_rotos(self):
        antes = {"bases": [{"nombre": "Scrap", "objetos": [
            {"nombre": "Complete_Scrap_COPPEL", "tipo": "vista", "filas": None, "columnas": []}]}]}
        ahora = {"bases": [{"nombre": "Scrap", "objetos": [
            {"nombre": "Complete_Scrap", "tipo": "tabla", "filas": 1, "columnas": []}]}]}
        cambios = " | ".join(ac.comparar(antes, ahora))
        assert "NUEVO tabla Complete_Scrap" in cambios
        assert "YA NO EXISTE Complete_Scrap_COPPEL" in cambios

    def test_sin_cambios(self):
        igual = self._catalogo([("WEEK", "int")])
        assert ac.comparar(igual, igual) == []


class TestValores:
    def test_un_float_entero_va_sin_decimales(self):
        # HCReport.[YEAR] es float: '2025.0' invitaria a filtrar con ese texto.
        assert ac._como_texto(2025.0) == "2025"
        assert ac._como_texto(2.5) == "2.5"
        assert ac._como_texto(" Coppel ") == "Coppel"


class TestComoLoVeElBot:
    _ROTA = {"nombre": "vw_Coppel_Monthly_Excel", "tipo": "vista", "filas": None,
             "columnas": [{"nombre": "Mes", "tipo": "int"}],
             "rota": "Invalid object name 'dbo.Retail_StoreMap'."}

    def test_una_vista_rota_se_marca_en_el_indice(self):
        assert "ROTA, no usar" in catalogo._resumen(self._ROTA)

    def test_y_su_ficha_dice_que_no_la_use(self):
        ficha = catalogo._ficha("GTM BI Database", self._ROTA)
        assert "NO FUNCIONA EN EL SERVIDOR" in ficha
        assert "Retail_StoreMap" in ficha

    def test_las_vistas_de_venta_ya_no_tienen_day(self):
        # Desde el 30/09/2026 tienen [WEEK]; escribir [Day] falla.
        nota = " ".join(catalogo.NOTAS_GENERALES)
        assert "YA NO TIENEN [Day]" in nota
        assert "DATEPART(week, [Date])" in nota
        assert "[Year], [Month], [WEEK] y [QTY]" in nota

    def test_key_prices_no_sirve_para_cruzar(self):
        nota = " ".join(catalogo.NOTAS_GENERALES)
        assert "[Key prices]" in nota and "casan el 0%" in nota

    def test_fichas_de_los_objetos_nuevos(self):
        precios = catalogo.NOTAS_POR_OBJETO["Prices_Weekly_VW"]
        assert "[MATERIAL DESCRIPTION FINAL]" in precios and "[Precio Venta] > 0" in precios
        inventario = catalogo.NOTAS_POR_OBJETO["TELCEL_INV_B2B"]
        assert "NUNCA sumes [STOCK] de varias fechas" in inventario
        competidores = catalogo.NOTAS_POR_OBJETO["CompetitorsAnalysis_VW_TELCEL"]
        assert "NO la sumes" in competidores

    def test_la_ficha_de_scrap_ya_no_ofrece_vistas_que_no_existen(self):
        nota = catalogo.NOTAS_POR_OBJETO["Complete_Scrap"]
        assert "se quitaron del servidor" in nota
