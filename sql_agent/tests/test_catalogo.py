import catalogo


class TestIndice:
    def test_estan_las_cinco_bases_con_sus_objetos(self):
        texto = catalogo.indice()
        for base in catalogo.cargar_catalogo()["bases"]:
            assert f"[{base['nombre']}]" in texto
            for objeto in base["objetos"]:
                assert objeto["nombre"] in texto

    def test_lleva_las_notas_de_negocio(self):
        # Sin ellas el agente vuelve a confundir retailer con operador; ver
        # TestNotasDeNegocio.
        texto = catalogo.indice()
        for nota in catalogo.NOTAS_GENERALES:
            assert nota in texto


class TestNotasDeNegocio:
    """Cubre el falso negativo que reporto otra sesion.

    A "cuantas ventas hubo en Elektra en marzo 2026" el agente contesto que no
    habia ninguna, porque genero `WHERE [Carrier] = 'Elektra'` sobre EKT_B2B.
    En esa tabla Carrier son operadores telefonicos (ABIERTA, AT&T, MOVISTAR,
    TELCEL), asi que el filtro daba cero filas, y el cero se reporto como "no
    hay ventas" en vez de como un filtro mal escrito. Las ventas existen: unas
    144 mil unidades ese mes.

    Estas pruebas no verifican que el modelo acierte -- eso depende del LLM --
    sino que la informacion que lo evitaria le llegue al prompt.
    """

    def test_el_mapeo_de_tabla_a_retailer_esta_en_el_prompt(self):
        texto = catalogo.indice()
        assert "EKT_B2B=Elektra" in texto
        assert "COPPEL_B2B" in texto

    def test_se_advierte_que_carrier_no_es_el_retailer(self):
        texto = catalogo.indice().casefold()
        assert "carrier" in texto and "operador telefonico" in texto

    def test_se_advierte_que_cero_filas_no_es_ausencia_de_datos(self):
        assert "cero filas" in catalogo.indice()

    def test_la_ficha_de_ekt_avisa_del_mes_en_texto(self):
        # [Month] es nvarchar con 'Mar'/'Nov', no un numero: `[Month] = 3`
        # revienta con un error de conversion en SQL Server.
        ficha = catalogo.describir_objeto.invoke({"nombre": "EKT_B2B"})
        assert "NOTA:" in ficha
        assert "[Month]" in ficha

    def test_la_ficha_del_consolidado_lo_ofrece_como_punto_de_entrada(self):
        ficha = catalogo.describir_objeto.invoke({"nombre": "ETL_Consolidado_B2B"})
        assert "NOTA:" in ficha
        assert "[Retail]" in ficha

    def test_cada_columna_con_valores_existe_de_verdad(self):
        # Una foto de valores sobre una columna renombrada nunca se mostraria
        # y el agente volveria al DISTINCT lento sin que nadie lo note.
        for objeto, columnas in catalogo.cargar_valores().items():
            encontrados = catalogo.buscar(objeto)
            assert encontrados, f"{objeto} ya no existe en el catalogo"
            reales = {c["nombre"].casefold() for c in encontrados[0][1]["columnas"]}
            for columna in columnas:
                assert columna.casefold() in reales, f"{objeto}.{columna} ya no existe"

    def test_cada_objeto_anotado_existe_en_el_catalogo(self):
        # Una nota sobre una tabla renombrada o borrada nunca se mostraria, y
        # nadie se enteraria de que quedo muerta.
        for nombre in catalogo.NOTAS_POR_OBJETO:
            assert catalogo.buscar(nombre), f"{nombre} ya no existe en el catalogo"


class TestDescribirObjeto:
    def _describir(self, nombre: str) -> str:
        return catalogo.describir_objeto.invoke({"nombre": nombre})

    def test_devuelve_columnas_con_su_tipo(self):
        ficha = self._describir("HonorCarriers")
        assert "[GTM BI Database].dbo.[HonorCarriers]" in ficha
        assert "[Carrier] nvarchar(255)" in ficha

    def test_acepta_el_nombre_ya_calificado(self):
        # El modelo lee el indice, que muestra las bases entre corchetes, y
        # tiende a devolver el nombre de tres partes en vez del simple.
        assert self._describir("[GTM BI Database].dbo.[HonorCarriers]") == self._describir(
            "HonorCarriers"
        )

    def test_nombre_repetido_en_dos_bases_devuelve_las_dos(self):
        # VW_TELCEL_B2B_SO_LC existe en GTM BI Database y en GTM_BI_INVENTORIES
        # con distinto numero de columnas; quedarse con una sola haria que el
        # modelo escribiera columnas inexistentes en la otra.
        ficha = self._describir("VW_TELCEL_B2B_SO_LC")
        assert "[GTM BI Database].dbo.[VW_TELCEL_B2B_SO_LC]" in ficha
        assert "[GTM_BI_INVENTORIES].dbo.[VW_TELCEL_B2B_SO_LC]" in ficha

    def test_no_confunde_un_nombre_con_los_que_lo_contienen(self):
        # Coppel es una vista real, y ademas subcadena de Coppel_2024,
        # Coppel_test, Coppel_Monthly_Excel, COPPEL_B2B... Una busqueda por
        # subcadena devolveria todas y ahogaria la respuesta.
        assert self._describir("Coppel").count("VISTA") == 1

    def test_objeto_inexistente_no_inventa_nada(self):
        respuesta = self._describir("VentasDelAnioPasado")
        assert "No hay ninguna tabla o vista" in respuesta
