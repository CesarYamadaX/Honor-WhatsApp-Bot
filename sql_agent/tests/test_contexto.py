"""Pruebas de contexto.py: la ficha que acompaña al resultado.

El reporte llevaba solo la tabla de la consulta. Si decia "TELCEL CAC" o
"SAMSUNG A07 64GB 4G", quien lo abria no podia saber en que region esta esa
tienda ni que tipo de producto es ese modelo sin volver a preguntarle al bot.
"""

import pandas as pd
import pytest

import contexto
from contexto import contexto_del_resultado


class _Cursor:
    def __init__(self, columnas, filas):
        self._columnas = columnas
        self._filas = filas

    def keys(self):
        return self._columnas

    def fetchall(self):
        return self._filas


class _MotorDeCatalogos:
    """Responde como el servidor, y guarda lo que se le pregunto."""

    def __init__(self, respuestas=None, falla=()):
        # respuestas: {fragmento de tabla: (columnas, filas)}
        self.respuestas = respuestas or {}
        self.falla = falla
        self.consultas = []

    def connect(self):
        return self

    @property
    def connection(self):
        # El desglose por tienda le pone plazo a la conexion; el doble tiene
        # que aguantarlo sin enterarse de nada mas.
        return self

    @property
    def dbapi_connection(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def exec_driver_sql(self, sql, parametros=None):
        self.consultas.append((sql, parametros))
        for fragmento in self.falla:
            if fragmento in sql:
                raise RuntimeError(f"{fragmento} no responde")
        for fragmento, (columnas, filas) in self.respuestas.items():
            if fragmento in sql:
                return _Cursor(columnas, filas)
        return _Cursor([], [])


def _resultado():
    return pd.DataFrame(
        {"Retail": ["Coppel", "ELEKTRA"], "Carrier": ["Telcel", "Telcel"], "QTY": [10, 20]}
    )


class TestQueSeConsulta:
    def test_sin_resultado_no_consulta_nada(self):
        motor = _MotorDeCatalogos()
        assert contexto_del_resultado(pd.DataFrame(), motor) == {}
        assert motor.consultas == []

    def test_un_resultado_solo_numerico_no_busca_en_los_catalogos(self):
        # Sin valores de texto no hay tiendas ni carriers que buscar. El
        # desglose por tienda si corre: solo necesita el periodo, y este
        # resultado lo tiene.
        motor = _MotorDeCatalogos()
        df = pd.DataFrame({"Year": [2026], "QTY": [5]})
        contexto_del_resultado(df, motor)

        for sql, _ in motor.consultas:
            assert "CustomerStores" not in sql
            assert "HonorCarriers" not in sql

    def test_un_resultado_sin_texto_ni_periodo_no_consulta_nada(self):
        motor = _MotorDeCatalogos()
        assert contexto_del_resultado(pd.DataFrame({"QTY": [5]}), motor) == {}
        assert motor.consultas == []

    def test_los_valores_viajan_como_parametros(self):
        # Un nombre de tienda puede traer comillas; pegarlo al SQL seria una
        # inyeccion con datos que vienen de la propia base. Se usa un valor
        # que no puede confundirse con un nombre de columna.
        motor = _MotorDeCatalogos()
        peligroso = "O'Brien'); DROP TABLE x--"
        df = pd.DataFrame({"Tienda": [peligroso], "QTY": [1]})
        contexto_del_resultado(df, motor)

        assert motor.consultas, "deberia consultar los catalogos"
        for sql, parametros in motor.consultas:
            assert "?" in sql
            assert peligroso not in sql, "el valor no puede acabar pegado al SQL"
            assert peligroso in parametros

    def test_ignora_las_columnas_con_demasiados_valores(self):
        # Una columna con miles de valores distintos es un identificador o
        # texto libre, no una dimension que este en un catalogo.
        motor = _MotorDeCatalogos()
        muchos = [f"v{i}" for i in range(contexto.MAX_VALORES_POR_COLUMNA + 5)]
        df = pd.DataFrame({"Folio": muchos, "QTY": range(len(muchos))})
        contexto_del_resultado(df, motor)

        assert motor.consultas == []


class TestHojasDevueltas:
    def test_devuelve_la_hoja_del_catalogo_que_caso(self):
        motor = _MotorDeCatalogos(
            {"[CustomerStores]": (["ID", "Region"], [("Coppel", "Norte")])}
        )
        hojas = contexto_del_resultado(_resultado(), motor)

        assert list(hojas) == ["Tiendas"]
        assert hojas["Tiendas"].iloc[0]["Region"] == "Norte"

    def test_ya_no_se_vuelca_el_catalogo_de_homologacion(self):
        # Era la hoja "Modelos": para un resultado de 635 filas metia 2001
        # filas de UNICOS$, que son las formas en que cada cadena escribe
        # esos modelos. Eso no es detalle de ventas, es ruido.
        motor = _MotorDeCatalogos(
            {"[UNICOS$]": (["HOMOLOGACION MODELO"], [("SAMSUNG A07",)])}
        )
        hojas = contexto_del_resultado(_resultado(), motor)

        assert "Modelos" not in hojas
        assert not any("UNICOS$" in sql for sql, _ in motor.consultas)

    def test_un_catalogo_sin_coincidencias_no_deja_hoja_vacia(self):
        motor = _MotorDeCatalogos()
        assert contexto_del_resultado(_resultado(), motor) == {}

    def test_un_catalogo_caido_no_cuesta_el_resto(self):
        # El reporte es lo que se pidio; la ficha es un extra. Que un catalogo
        # no responda no puede dejar al usuario sin nada.
        motor = _MotorDeCatalogos(
            respuestas={"[HonorCarriers]": (["Carrier"], [("Telcel",)])},
            falla=("[UNICOS$]",),
        )
        hojas = contexto_del_resultado(_resultado(), motor)

        assert list(hojas) == ["Carriers"]


class TestUnaHojaPorCadena:
    """Cuando el reporte trae varias cadenas, cada una tiene su pestaña.

    La hoja de datos se queda completa: esto se agrega al lado. Un reporte
    que compara Coppel contra Elektra se lee cadena por cadena, y en una sola
    tabla mezclada eso obliga a filtrar a mano cada vez.
    """

    def _reporte(self, cadenas, filas_por_cadena, unidades=None):
        datos = []
        for indice, cadena in enumerate(cadenas):
            for fila in range(filas_por_cadena):
                datos.append(
                    {
                        "Retail": cadena,
                        "Año": 2026,
                        "Mes": (fila % 12) + 1,
                        "Unidades": (unidades or {}).get(cadena, 10),
                    }
                )
        return pd.DataFrame(datos)

    def test_cada_cadena_tiene_su_hoja(self):
        hojas = contexto.hojas_por_cadena(self._reporte(["Coppel", "ELEKTRA"], 12))
        assert set(hojas) == {"Coppel", "ELEKTRA"}
        assert (hojas["Coppel"]["Retail"] == "Coppel").all()
        assert len(hojas["Coppel"]) == 12

    def test_las_pestañas_van_por_volumen_no_por_el_año(self):
        # La primera columna numerica de un reporte por mes es [Año]:
        # ordenar por ella es ordenar por 2026, o sea por numero de filas.
        df = self._reporte(
            ["chica", "grande"], 12, unidades={"chica": 1, "grande": 1000}
        )
        assert list(contexto.hojas_por_cadena(df)) == ["grande", "chica"]

    def test_con_una_sola_cadena_no_se_separa_nada(self):
        assert contexto.hojas_por_cadena(self._reporte(["Coppel"], 30)) == {}

    def test_sin_columna_de_cadena_tampoco(self):
        df = pd.DataFrame({"Modelo": ["a"] * 20, "Unidades": range(20)})
        assert contexto.hojas_por_cadena(df) == {}

    def test_no_se_parte_un_reporte_flaco(self):
        # 48 filas y 16 cadenas son tres filas por pestaña: separar ahi no
        # ayuda a comparar, estorba.
        df = self._reporte([f"C{i}" for i in range(16)], 3)
        assert contexto.hojas_por_cadena(df) == {}

    def test_ni_uno_con_demasiadas_cadenas(self):
        df = self._reporte([f"C{i}" for i in range(20)], 30)
        assert contexto.hojas_por_cadena(df) == {}

    def test_el_nombre_de_la_hoja_lo_acepta_excel(self):
        # Excel rechaza : \\ / ? * [ ] y corta a 31 caracteres.
        largo = "AT&T Cacs / Retail [norte] " + "x" * 40
        nombre = contexto._nombre_de_hoja(largo, set())
        assert len(nombre) <= 31
        assert not any(c in nombre for c in ":\\/?*[]")

    def test_dos_cadenas_no_se_pisan_al_recortarse(self):
        usados = set()
        largo = "CADENA MUY LARGA QUE SE RECORTA AQUI Y ALLA"
        primero = contexto._nombre_de_hoja(largo, usados)
        segundo = contexto._nombre_de_hoja(largo, usados)
        assert primero != segundo
        assert len(segundo) <= 31

    def test_ninguna_hoja_se_llama_como_la_de_datos(self):
        # Dos hojas con el mismo nombre revientan el archivo.
        df = self._reporte(["Datos", "Coppel"], 12)
        assert "Datos" not in contexto.hojas_por_cadena(df)

    def test_la_medida_ignora_el_periodo(self):
        df = pd.DataFrame({"Año": [2026], "Mes": [8], "Unidades": [5]})
        assert contexto._medida(df) == "Unidades"
        assert contexto._medida(pd.DataFrame({"Año": [2026]})) is None


class TestExcelConContexto:
    def test_el_resultado_va_en_la_primera_hoja(self, tmp_path, monkeypatch):
        import openpyxl

        from excel_exporter import HOJA_DATOS, ExcelExporter

        monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
        ruta = ExcelExporter.export_to_excel(
            _resultado(),
            filename="con_contexto.xlsx",
            contexto={"Modelos": pd.DataFrame({"HOMOLOGACION MODELO": ["SAMSUNG A07"]})},
        )
        libro = openpyxl.load_workbook(ruta)

        assert libro.sheetnames[0] == HOJA_DATOS, "lo que se pregunto va primero"
        assert libro.sheetnames == [HOJA_DATOS, "Modelos"]
        assert libro["Modelos"]["A2"].value == "SAMSUNG A07"

    @pytest.mark.parametrize("contexto_vacio", [None, {}, {"Modelos": pd.DataFrame()}])
    def test_sin_contexto_el_excel_queda_como_antes(
        self, contexto_vacio, tmp_path, monkeypatch
    ):
        import openpyxl

        from excel_exporter import HOJA_DATOS, ExcelExporter

        monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
        ruta = ExcelExporter.export_to_excel(
            _resultado(), filename="solo_datos.xlsx", contexto=contexto_vacio
        )
        assert openpyxl.load_workbook(ruta).sheetnames == [HOJA_DATOS]


class TestLaHojaNoContradiceAlMensaje:
    """Medido el 25 de septiembre de 2026 contra la base: "cuantas vendio
    Honor en agosto" contesta 160 909 unidades, y la hoja "Ventas por
    tienda" que acompaña al Excel traia 2 030 760 -- el mercado entero. Dos
    numeros que no cuadran en el mismo archivo, y el que se queda es el del
    archivo. Con el filtro, esa hoja suma 139 381 de 2 000 tiendas (el tope
    de la hoja), que si es HONOR.
    """

    def _sql_de_la_hoja(self, motor):
        return next(
            (sql, par) for sql, par in motor.consultas
            if "GROUP BY v.[Retail], v.[Store]" in sql
        )

    def _por_mes(self, extra=None):
        datos = {"Año": [2026], "Mes": [8], "Total Unidades": [160909]}
        datos.update(extra or {})
        return pd.DataFrame(datos)

    def test_la_marca_del_sql_acota_la_hoja(self):
        # El resultado viene agregado: ya no trae la columna [Brand], asi que
        # la marca solo se puede saber por el SQL que lo produjo.
        motor = _MotorDeCatalogos()
        contexto_del_resultado(
            self._por_mes(), motor, "SELECT SUM([QTY]) FROM v WHERE [Brand] = 'HONOR'"
        )
        sql, parametros = self._sql_de_la_hoja(motor)

        assert "v.[Brand] IN (?)" in sql
        assert "HONOR" in parametros

    def test_la_marca_de_la_columna_manda_sobre_la_del_sql(self):
        motor = _MotorDeCatalogos()
        contexto_del_resultado(
            self._por_mes({"Brand": ["SAMSUNG"]}),
            motor,
            "... WHERE [Brand] = 'HONOR'",
        )
        _, parametros = self._sql_de_la_hoja(motor)

        assert "SAMSUNG" in parametros and "HONOR" not in parametros

    def test_varias_marcas_en_un_IN_se_respetan_todas(self):
        motor = _MotorDeCatalogos()
        contexto_del_resultado(
            self._por_mes(), motor, "... WHERE [Brand] IN ('HONOR', 'SAMSUNG')"
        )
        sql, parametros = self._sql_de_la_hoja(motor)

        assert "v.[Brand] IN (?, ?)" in sql
        assert {"HONOR", "SAMSUNG"} <= set(parametros)

    def test_la_cadena_del_sql_acota_aunque_el_resultado_no_la_traiga(self):
        # "Ventas de Coppel por mes en 2026" son nueve filas de Year/Month/
        # Total: la cadena solo vive en el WHERE. Sin leerla de ahi, la hoja
        # salia con 18 034 947 unidades -- el mercado entero -- contra los
        # 4 186 855 del mensaje. Medido contra el bot el 25/09/2026.
        motor = _MotorDeCatalogos()
        contexto_del_resultado(
            self._por_mes(),
            motor,
            "SELECT [Year],[Month],SUM([QTY]) FROM ETL_Consolidado_B2B "
            "WHERE [Retail] = 'Coppel' GROUP BY [Year],[Month]",
        )
        sql, parametros = self._sql_de_la_hoja(motor)

        assert "v.[Retail] IN (?)" in sql
        assert "Coppel" in parametros

    def test_la_cadena_sale_del_nombre_de_la_tabla_cuando_no_hay_otra_forma(self):
        # Tercera forma de decir de que cadena es el reporte: la tabla cruda.
        # COPPEL_B2B NO tiene columna [Retail] -- la cadena esta en el nombre
        # y en ningun otro lado. Medido contra el bot el 25/09/2026: el
        # modelo eligio justo esa ruta y la hoja volvia a traer el mercado.
        motor = _MotorDeCatalogos()
        contexto_del_resultado(
            self._por_mes(),
            motor,
            "SELECT YEAR([Dia]),MONTH([Dia]),SUM([Unidades]) "
            "FROM [RAW_DATA_B2B].dbo.[COPPEL_B2B]",
        )
        sql, parametros = self._sql_de_la_hoja(motor)

        assert "v.[Retail] IN (?)" in sql
        assert "Coppel" in parametros

    def test_los_literales_de_la_cadena_no_se_escriben_a_mano(self):
        # Telcel son SEIS canales y Liverpool DOS. Escribir 'TELCEL' a mano
        # no casaria con ninguno: los valores salen de la columna real.
        telcel = contexto._retail_de_la_cadena("TELCEL")
        assert len(telcel) == 6 and "TELCEL RETAIL" in telcel
        assert contexto._retail_de_la_cadena("LIVERPOOL") == [
            "Liverpool Offline", "Liverpool Online",
        ]
        assert contexto._retail_de_la_cadena(None) == []

    def test_la_cadena_tambien_acota(self):
        # Los valores salen del resultado, no de un literal inventado: en la
        # consolidada la cadena es 'Liverpool Offline', no 'LIVERPOOL'.
        motor = _MotorDeCatalogos()
        contexto_del_resultado(
            self._por_mes({"Retail": ["Liverpool Offline"]}), motor, ""
        )
        sql, parametros = self._sql_de_la_hoja(motor)

        assert "v.[Retail] IN (?)" in sql
        assert "Liverpool Offline" in parametros

    def test_una_pregunta_sin_marca_trae_la_hoja_entera(self):
        # "Cuanto se vendio en agosto" es del mercado: acotarla seria mentir
        # al reves.
        motor = _MotorDeCatalogos()
        contexto_del_resultado(self._por_mes(), motor, "SELECT SUM([QTY]) FROM v")
        sql, parametros = self._sql_de_la_hoja(motor)

        assert "v.[Brand]" not in sql
        assert parametros == ("2026-08-01", "2026-09-01")

    def test_un_resultado_con_muchas_marcas_no_esta_acotado_a_ninguna(self):
        df = pd.DataFrame({
            "Año": [2026] * 9,
            "Brand": list("abcdefghi"),
            "Total": list(range(9)),
        })
        assert "Brand" not in contexto.acotado_del_resultado(df, "")

    def test_el_desglose_por_modelo_no_necesita_este_filtro(self):
        # Esa otra hoja ya se acota con los modelos del propio resultado.
        assert contexto.acotado_del_resultado(
            pd.DataFrame({"HOMOLOGACION MODELO": ["HONOR X5B"], "QTY": [5]}), ""
        ) == {}


class TestElTopeNoParteUnTotal:
    """Cortar una hoja de VENTA no es "menos ruido": es un total que no
    cuadra. Con el tope viejo de 2 000, "ventas de Honor por mes en 2026"
    entregaba un mensaje que decia 1 634 670 unidades y una hoja que sumaba
    1 381 369 -- el 84% -- sin decirlo en ningun lado.

    Medido el 25 de septiembre de 2026, estas hojas no se pueden desbordar:
    el techo lo pone cuantas tiendas existen. Retail x Store en la
    consolidada da 6 626 filas para un mes y 9 760 para el ano entero con
    todas las marcas.
    """

    def _sql_de(self, motor, fragmento):
        return next(sql for sql, _ in motor.consultas if fragmento in sql)

    def test_la_hoja_por_tienda_cabe_entera(self):
        motor = _MotorDeCatalogos()
        contexto_del_resultado(
            pd.DataFrame({"Año": [2026], "Mes": [8], "Total": [160909]}), motor, ""
        )
        sql = self._sql_de(motor, "GROUP BY v.[Retail], v.[Store]")

        assert f"TOP {contexto.MAX_FILAS_DE_VENTA}" in sql
        assert contexto.MAX_FILAS_DE_VENTA > 9760, "el peor caso medido"

    def test_la_hoja_por_modelo_tambien(self):
        motor = _MotorDeCatalogos()
        contexto_del_resultado(
            pd.DataFrame({
                "Año": [2026], "Mes": [8],
                "HOMOLOGACION MODELO": ["HONOR X5C"], "Total": [5],
            }),
            motor,
            "FROM EKT_B2B",
        )
        assert f"TOP {contexto.MAX_FILAS_DE_VENTA}" in self._sql_de(motor, "u.[modelo]")

    def test_las_hojas_de_catalogo_si_siguen_topadas(self):
        # Ahi cortar SI es lo correcto: un volcado de CustomerStores entero
        # (7k filas) no es contexto del resultado, es ruido.
        motor = _MotorDeCatalogos()
        contexto_del_resultado(_resultado(), motor)
        sql = self._sql_de(motor, "CustomerStores")

        assert f"TOP {contexto.MAX_FILAS_POR_HOJA}" in sql
        assert contexto.MAX_FILAS_POR_HOJA == 2000


class TestCuandoUnaHojaSaleTopada:
    """Un tope que corta en silencio es el mismo bug que el de 2 000 filas:
    dos numeros que no cuadran en el mismo archivo. El tope sigue
    existiendo -- lo impone el limite de adjunto de WhatsApp, no la base --
    pero cuando muerde hay que decirlo.
    """

    def _hoja(self, nombre, filas):
        return {nombre: pd.DataFrame({"Unidades": range(filas)})}

    def test_una_hoja_en_su_tope_se_reporta(self):
        assert contexto.hojas_cortadas(
            self._hoja("Raw_Data", contexto.MAX_FILAS_RAW_DATA)
        ) == ["Raw_Data"]
        assert contexto.hojas_cortadas(
            self._hoja("Ventas por tienda", contexto.MAX_FILAS_DE_VENTA)
        ) == ["Ventas por tienda"]

    def test_una_hoja_que_cabe_entera_no_se_reporta(self):
        # 58 066 son los tres meses de Coppel, el reporte tipico.
        assert contexto.hojas_cortadas(self._hoja("Raw_Data", 58066)) == []

    def test_las_hojas_de_catalogo_no_entran_aqui(self):
        # Esas si se topan a proposito y no pretenden sumar ningun total.
        assert contexto.hojas_cortadas(
            self._hoja("Tiendas", contexto.MAX_FILAS_POR_HOJA)
        ) == []

    def test_sin_hojas_no_revienta(self):
        assert contexto.hojas_cortadas({}) == []
        assert contexto.hojas_cortadas(None) == []

    def test_el_tope_del_crudo_cubre_un_anio_de_la_cadena_mas_grande(self):
        # Medido el 25 de septiembre de 2026: un anio de Coppel son 164 507
        # filas ya agrupadas, y toda la historia 301 319. Con el tope viejo
        # de 80 000 el anio se cortaba a la mitad.
        assert contexto.MAX_FILAS_RAW_DATA >= 164507

    def test_pero_no_tanto_como_para_no_caber_en_whatsapp(self):
        # Medido con el pivot ya armado: 500 000 filas pesan 18.1 MB y ya no
        # pasan el limite de 16 MB; 300 000 quedan en ~11.6 MB.
        assert contexto.MAX_FILAS_RAW_DATA <= 400000


class TestDatosCrudosParaElPivot:
    """La hoja Raw_Data que alimenta el PIVOT: 'quiero las ventas de Elektra
    junio julio agosto y el share por tienda y por mes'. A diferencia de
    'Ventas por tienda', esta no depende de una lista de modelos -- trae
    TODA la cadena en el periodo, agrupada por tienda, marca y mes.
    """

    def _resultado_por_modelo(self):
        # El mismo resultado con el que se prueba "Ventas por tienda": lo
        # que importa aqui es que tenga periodo, no los modelos.
        return pd.DataFrame(
            {
                "Año": [2026, 2026, 2026],
                "Mes": [1, 3, 6],
                "HOMOLOGACION MODELO": ["OPPO A6X", "OPPO A5", "OPPO A6X"],
                "Total Vendido": [100, 500, 300],
            }
        )

    def _motor_con_crudo(self, filas=None):
        return _MotorDeCatalogos(
            {
                "EKT_B2B": (
                    ["Region", "Tienda", "Marca", "Mes", "Unidades"],
                    filas or [("R9", "929797 ELEKTRA.MX", "Samsung", 6, 176)],
                )
            }
        )

    def test_se_genera_junto_con_ventas_por_tienda(self):
        motor = self._motor_con_crudo()
        hojas = contexto_del_resultado(
            self._resultado_por_modelo(), motor, "FROM EKT_B2B"
        )

        assert "Raw_Data" in hojas
        assert list(hojas["Raw_Data"].columns) == [
            "Region", "Tienda", "Marca", "Mes", "Unidades",
        ]

    def test_no_filtra_por_los_modelos_del_resultado(self):
        # A diferencia de _ventas_por_tienda_y_modelo, esta trae la cadena
        # entera: no hay IN (...) de modelos en el SQL.
        motor = self._motor_con_crudo()
        contexto_del_resultado(self._resultado_por_modelo(), motor, "FROM EKT_B2B")

        sql_crudo = next(
            sql for sql, _ in motor.consultas
            if "GROUP BY cs.[Region], v.[Tienda #], v.[Marca]" in sql
        )
        assert "IN (" not in sql_crudo.split("WHERE")[1].split("GROUP BY")[0]

    def test_se_agrupa_por_marca_no_por_modelo(self):
        # Al nivel de modelo la misma consulta da cuatro veces mas filas
        # (medido: 127 602 contra 28 887 en Elektra); esta hoja se queda en
        # marca porque el detalle de modelo ya tiene su propia hoja.
        motor = self._motor_con_crudo()
        contexto_del_resultado(self._resultado_por_modelo(), motor, "FROM EKT_B2B")
        sql_crudo = next(
            sql for sql, _ in motor.consultas if "v.[Marca] AS [Marca]" in sql
        )
        assert "HOMOLOGACION" not in sql_crudo

    def test_topea_las_filas(self):
        # Medido: Elektra 3 meses da 28 887 filas asi, Coppel (la tabla mas
        # grande) 58 066. El tope deja margen sin acercarse al limite de
        # adjunto de WhatsApp.
        motor = self._motor_con_crudo()
        contexto_del_resultado(self._resultado_por_modelo(), motor, "FROM EKT_B2B")
        sql_crudo = next(
            sql for sql, _ in motor.consultas if "v.[Marca] AS [Marca]" in sql
        )
        assert f"TOP {contexto.MAX_FILAS_RAW_DATA}" in sql_crudo

    def test_sin_cadena_identificada_no_se_genera(self):
        # Sin tabla cruda no hay de donde sacar datos crudos.
        motor = _MotorDeCatalogos()
        hojas = contexto_del_resultado(self._resultado_por_modelo(), motor, "")
        assert "Raw_Data" not in hojas

    def test_un_fallo_en_raw_data_no_tira_ventas_por_tienda(self):
        motor = _MotorDeCatalogos(
            {
                "EKT_B2B": (
                    ["Tienda", "Modelo", "Unidades"],
                    [("929797 ELEKTRA.MX", "SAMSUNG A17 128GB 4G", 176)],
                )
            },
            falla=("v.[Marca] AS [Marca]",),
        )
        hojas = contexto_del_resultado(
            self._resultado_por_modelo(), motor, "FROM EKT_B2B"
        )
        assert "Ventas por tienda" in hojas
        assert "Raw_Data" not in hojas


class TestFallbackDeRegion:
    """A las tiendas sin region se les da una segunda oportunidad contra el
    catalogo, con el nombre normalizado. Se pidio esto porque "L  TAMPICO"
    (dos espacios, tal cual lo escribe Liverpool) no casaba con
    "L TAMPICO" del catalogo: medido, esa sola diferencia perdia 17 743
    unidades del periodo, el 5.8% del total de Liverpool.
    """

    def test_las_que_se_identifican_por_nombre_llevan_el_segundo_intento(self):
        for clave in ("COPPEL", "TELCEL", "AT&T", "LIVERPOOL"):
            sql, region, parametros = contexto._join_region(contexto.CRUDAS[clave])
            assert "COALESCE" in region, clave
            assert sql.count("LEFT JOIN") == 2, clave
            assert len(parametros) == 2, clave

    def test_las_que_se_identifican_por_numero_no_lo_necesitan(self):
        # Medido: sus pocas tiendas sin catalogar (13 de 1272 en Elektra) de
        # verdad no estan dadas de alta; normalizar el nombre no ayuda
        # cuando el enlace ya es por numero.
        for clave in ("ELEKTRA", "SEARS"):
            sql, region, parametros = contexto._join_region(contexto.CRUDAS[clave])
            assert region == "cs.[Region]", clave
            assert sql.count("LEFT JOIN") == 1, clave
            assert len(parametros) == 1, clave

    def test_ventas_crudas_arma_los_dos_joins_con_sus_parametros(self):
        motor = _MotorDeCatalogos(
            {"LIVERPOOL_B2B$": (["Region", "Tienda", "Marca", "Mes", "Unidades"], [])}
        )
        with motor.connect() as conexion:
            contexto.ventas_crudas(
                conexion, contexto.CRUDAS["LIVERPOOL"], "2026-06-01", "2026-09-01"
            )

        sql, parametros = motor.consultas[0]
        assert sql.count("?") == len(parametros)
        assert parametros[0] == parametros[1] == "LIVERPOOL"

    def test_normaliza_el_nbsp_y_los_espacios_de_mas(self):
        _, _, _ = contexto._join_region(contexto.CRUDAS["LIVERPOOL"])
        sql, _, _ = contexto._join_region(contexto.CRUDAS["LIVERPOOL"])
        assert "NCHAR(160)" in sql
        assert "LTRIM(RTRIM(" in sql


class TestCadaCrudaTieneSuMarca:
    def test_las_seis_declaran_la_columna_de_marca(self):
        # Verificado contra el servidor: Marca en Coppel/Elektra/Telcel,
        # Brand en Liverpool/Sears, BRAND en AT&T.
        esperadas = {
            "COPPEL": "Marca", "ELEKTRA": "Marca", "TELCEL": "Marca",
            "LIVERPOOL": "Brand", "AT&T": "BRAND", "SEARS": "Brand",
        }
        for clave, columna in esperadas.items():
            assert contexto.CRUDAS[clave].marca == columna, clave


class TestDesglosePorTienda:
    """La hoja que se pidio: cuanto vendio cada tienda en el periodo.

    Va por PERIODO y no por modelo. La primera version filtraba los modelos
    del resultado uniendo la consolidada con UNICOS$ por [DESCRIPCION], y esa
    union esta mal por dos lados: en la consolidada [Material_Description] es
    SKU + retailer + descripcion, asi que casaba el 8% del volumen del mes
    (165 941 de 2 030 760 unidades en agosto de 2026), y de eso una parte se
    contaba de mas, porque UNICOS$ repite 1 249 descripciones y algunas
    apuntan a 25 homologaciones distintas. Las reglas del propio catalogo ya
    prohibian ese cruce.
    """

    def _resultado_por_modelo(self):
        return pd.DataFrame(
            {
                "Año": [2026, 2026, 2026],
                "Mes": [1, 3, 6],
                "HOMOLOGACION MODELO": ["OPPO A6X", "OPPO A5", "OPPO A6X"],
                "Total Vendido": [100, 500, 300],
            }
        )

    def _motor_con_ventas(self):
        return _MotorDeCatalogos(
            {
                "ETL_Consolidado_B2B": (
                    ["Retail", "Store", "Unidades"],
                    [("Coppel", "1114 CUAUHTEMOC", 2575)],
                )
            }
        )

    def test_la_hoja_trae_las_tiendas_del_periodo(self):
        hojas = contexto_del_resultado(
            self._resultado_por_modelo(), self._motor_con_ventas()
        )

        assert "Ventas por tienda" in hojas
        assert hojas["Ventas por tienda"].iloc[0]["Store"] == "1114 CUAUHTEMOC"

    def test_la_consulta_no_pasa_por_la_homologacion(self):
        # El cruce con UNICOS$ perdia el 92% del volumen y duplicaba parte
        # del resto. Que no vuelva a entrar sin que nadie lo note.
        motor = self._motor_con_ventas()
        contexto_del_resultado(self._resultado_por_modelo(), motor)

        ventas = [sql for sql, _ in motor.consultas if "ETL_Consolidado_B2B" in sql]
        assert ventas, "deberia consultar la vista de ventas"
        for sql in ventas:
            assert "UNICOS" not in sql.upper()
            assert "JOIN" not in sql.upper()

    def test_el_periodo_viaja_como_parametro(self):
        motor = self._motor_con_ventas()
        contexto_del_resultado(self._resultado_por_modelo(), motor)

        sql, parametros = next(
            (s, p) for s, p in motor.consultas if "ETL_Consolidado_B2B" in s
        )
        assert sql.count("?") == 2
        assert parametros == ("2026-01-01", "2026-07-01")

    def _motor_con_crudas(self):
        return _MotorDeCatalogos(
            {
                "EKT_B2B": (
                    ["Tienda", "Modelo", "Unidades"],
                    [("929797 ELEKTRA.MX", "SAMSUNG A17 128GB 4G", 176)],
                ),
                "COPPEL_B2B": (
                    ["Tienda", "Modelo", "Unidades"],
                    [("800 • COPPEL INTERNET", "SAMSUNG A17 128GB 4G", 374)],
                ),
            }
        )

    def _sql_de_ventas(self, motor):
        return [
            (sql, par)
            for sql, par in motor.consultas
            if "_B2B" in sql or "ETL_Consolidado" in sql
        ]

    def test_la_hoja_lleva_la_region_de_cada_tienda(self):
        # Se pidio poder buscar por region; la region no esta en la venta,
        # hay que traerla de CustomerStores.
        motor = _MotorDeCatalogos(
            {
                "EKT_B2B": (
                    ["Region", "Tienda", "Modelo", "Unidades"],
                    [("R9", "929797 ELEKTRA.MX", "SAMSUNG A17 128GB 4G", 176)],
                )
            }
        )
        hojas = contexto_del_resultado(
            self._resultado_por_modelo(), motor, "FROM EKT_B2B"
        )

        assert list(hojas["Ventas por tienda"].columns)[0] == "Region"
        sql, parametros = self._sql_de_ventas(motor)[0]
        assert "CustomerStores" in sql
        # Filtrado por cadena, para no cruzar nombres entre retailers.
        assert "ELEKTRA" in parametros
        # Y en LEFT JOIN: una tienda sin region no puede borrar su venta.
        assert "LEFT JOIN" in sql

    def test_elektra_y_sears_se_unen_por_numero_de_tienda(self):
        # Sus nombres no se parecen a los del catalogo; el numero vive entre
        # parentesis dentro de [HONOR NAME].
        for clave in ("ELEKTRA", "SEARS"):
            cruda = contexto.CRUDAS[clave]
            enlace = cruda.enlace_con_catalogo.format(
                etiqueta=cruda.etiqueta, clave=cruda.clave
            )
            assert "numero" in enlace, clave
            assert cruda.clave in enlace, clave

    def test_las_demas_se_unen_por_nombre(self):
        for clave in ("COPPEL", "TELCEL", "AT&T", "LIVERPOOL"):
            cruda = contexto.CRUDAS[clave]
            enlace = cruda.enlace_con_catalogo.format(
                etiqueta=cruda.etiqueta, clave=cruda.clave
            )
            assert "nombre" in enlace, clave

    def test_coppel_arma_la_llave_concatenando_marca_y_modelo(self):
        # En Coppel, [MATERIAL DESCRIPTION FINAL] ES marca + descripcion
        # ("ACER ANV15-41-R4WJ"). Armando esa llave casa el 100% del mes,
        # contra el 97.8% que casa [Modelo] por [DESCRIPCION].
        df = self._resultado_por_modelo()
        df["Retail"] = "Coppel"
        motor = self._motor_con_crudas()
        contexto_del_resultado(df, motor, "")
        sql, _ = self._sql_de_ventas(motor)[0]

        assert "CONCAT(v.[Marca], ' ', v.[Modelo])" in sql
        assert "[MATERIAL DESCRIPTION FINAL]" in sql

    def test_sears_antepone_la_palabra_sears(self):
        # Ahi la final es "Sears " + la descripcion ("Sears APPLE IPHONE 11
        # 64GB"), literal.
        cruda = contexto.CRUDAS["SEARS"]
        assert cruda.llave == "CONCAT('Sears ', v.[Model])"
        assert cruda.columna_en_unicos == "MATERIAL DESCRIPTION FINAL"

    def test_att_se_queda_con_la_descripcion(self):
        # En AT&T la final casa 0%: la llave es [Model] por [DESCRIPCION].
        cruda = contexto.CRUDAS["AT&T"]
        assert cruda.llave == "v.[Model]"
        assert cruda.columna_en_unicos == "DESCRIPCION"

    def test_cada_llave_nombra_una_columna_de_su_tabla(self):
        # Una llave que mencione una columna de otra tabla revienta en
        # ejecucion y solo se veria en produccion.
        for clave, cruda in contexto.CRUDAS.items():
            assert cruda.llave.startswith(("v.[", "CONCAT(")), clave
            assert cruda.columna_en_unicos in (
                "DESCRIPCION",
                "MATERIAL DESCRIPTION FINAL",
            ), clave

    def test_con_una_sola_cadena_la_hoja_lleva_el_modelo(self):
        # La ruta buena al modelo: la tabla CRUDA del retailer contra
        # UNICOS$.[DESCRIPCION]. Desde la consolidada casaria el 8%.
        motor = self._motor_con_crudas()
        hojas = contexto_del_resultado(
            self._resultado_por_modelo(), motor, "FROM [RAW_DATA_B2B].dbo.[EKT_B2B]"
        )

        assert list(hojas["Ventas por tienda"].columns) == [
            "Tienda", "Modelo", "Unidades"
        ]
        sql, parametros = self._sql_de_ventas(motor)[0]
        assert "EKT_B2B" in sql
        assert "ETL_Consolidado_B2B" not in sql
        # El retailer va pegado a la condicion, no en el WHERE de la venta.
        assert parametros[0] == "ELEKTRA"

    def test_el_retailer_sale_del_resultado_antes_que_del_sql(self):
        # Si la tabla que el usuario esta viendo dice Coppel, es Coppel,
        # aunque el SQL haya salido de la consolidada.
        df = self._resultado_por_modelo()
        df["Retail"] = "Coppel"
        motor = self._motor_con_crudas()
        contexto_del_resultado(df, motor, "FROM [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B]")

        assert "COPPEL_B2B" in self._sql_de_ventas(motor)[0][0]

    def test_con_dos_cadenas_la_hoja_vuelve_a_ser_por_periodo(self):
        # Un reporte que compara Coppel contra Elektra no se puede desglosar
        # por la tabla cruda de ninguno de los dos.
        df = self._resultado_por_modelo()
        df["Retail"] = ["Coppel", "ELEKTRA", "Coppel"]
        motor = self._motor_con_crudas()
        contexto_del_resultado(df, motor, "")

        sql, _ = self._sql_de_ventas(motor)[0]
        assert "ETL_Consolidado_B2B" in sql
        assert "UNICOS" not in sql.upper()

    def test_sin_modelos_la_hoja_es_por_periodo(self):
        sin_modelo = pd.DataFrame({"Año": [2026], "Mes": [8], "QTY": [10]})
        motor = self._motor_con_crudas()
        contexto_del_resultado(sin_modelo, motor, "FROM EKT_B2B")

        assert "ETL_Consolidado_B2B" in self._sql_de_ventas(motor)[0][0]

    def test_la_homologacion_se_colapsa_para_no_duplicar(self):
        # En Liverpool hay 137 descripciones que apuntan a mas de una
        # homologacion; sin agruparlas, cada venta se une dos veces y las
        # unidades salen infladas.
        motor = self._motor_con_crudas()
        contexto_del_resultado(
            self._resultado_por_modelo(), motor, "FROM EKT_B2B"
        )
        sql, _ = self._sql_de_ventas(motor)[0]

        assert "GROUP BY [DESCRIPCION]" in sql
        assert "MIN([HOMOLOGACION MODELO])" in sql

    def test_se_dejan_fuera_devoluciones_y_ajustes(self):
        # Las vistas oficiales filtran `>= 1`: la hoja tiene que sumar como
        # el reporte al que acompaña.
        motor = self._motor_con_crudas()
        contexto_del_resultado(self._resultado_por_modelo(), motor, "FROM EKT_B2B")
        sql, _ = self._sql_de_ventas(motor)[0]

        assert ">= 1" in sql

    def test_los_modelos_van_por_lo_que_vendieron(self):
        # Un reporte con cien modelos no se desglosa entero: cada uno alarga
        # la consulta. Se toman los que de verdad pesan.
        muchos = pd.DataFrame(
            {
                "HOMOLOGACION MODELO": [f"MODELO {i}" for i in range(40)],
                "Total": list(range(40)),
                "Año": [2026] * 40,
            }
        )
        modelos = contexto.modelos_a_desglosar(muchos)

        assert len(modelos) == contexto.MAX_MODELOS_DESGLOSADOS
        assert modelos[0] == "MODELO 39"

    def test_cada_cruda_declara_columnas_completas(self):
        # El mapa se verifico tabla por tabla contra el servidor; si alguien
        # agrega una cadena a medias, aqui se ve.
        for clave, cruda in contexto.CRUDAS.items():
            assert cruda.tabla and cruda.retail_en_unicos, clave
            assert all(
                getattr(cruda, campo)
                for campo in ("llave", "columna_en_unicos", "clave", "etiqueta",
                              "fecha", "unidades", "retail_en_catalogo",
                              "enlace_con_catalogo")
            ), clave

    def test_sin_periodo_no_se_consulta_la_vista_de_ventas(self):
        # Sin acotar la fecha la consulta recorre la vista entera, que es la
        # operacion cara que este modulo evita.
        sin_fecha = pd.DataFrame({"Tienda": ["1114 CUAUHTEMOC"], "Total": [10]})
        motor = _MotorDeCatalogos()
        contexto_del_resultado(sin_fecha, motor)

        assert not any("ETL_Consolidado_B2B" in sql for sql, _ in motor.consultas)

    def test_el_periodo_sale_del_año_y_el_mes(self):
        assert contexto.periodo(self._resultado_por_modelo()) == ("2026-01-01", "2026-07-01")

    def test_el_periodo_sale_tambien_de_una_columna_de_fecha(self):
        df = pd.DataFrame(
            {
                "Día": pd.to_datetime(["2026-03-05", "2026-03-20"]),
                "Modelo": ["OPPO A6X", "OPPO A6X"],
                "QTY": [1, 2],
            }
        )
        assert contexto.periodo(df) == ("2026-03-05", "2026-03-21")

    def test_un_desglose_que_no_contesta_no_cuesta_el_reporte(self):
        # La vista de ventas es la lenta; si no llega a tiempo, el reporte
        # sale sin esa hoja en vez de hacer esperar al usuario.
        motor = _MotorDeCatalogos(falla=("ETL_Consolidado_B2B",))
        hojas = contexto_del_resultado(self._resultado_por_modelo(), motor)

        assert "Ventas por tienda" not in hojas
