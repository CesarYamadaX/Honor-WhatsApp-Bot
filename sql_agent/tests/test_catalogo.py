import catalogo


class TestIndice:
    def test_estan_las_cinco_bases_con_sus_objetos(self):
        texto = catalogo.indice()
        for base in catalogo.cargar_catalogo()["bases"]:
            assert f"[{base['nombre']}]" in texto
            for objeto in base["objetos"]:
                assert objeto["nombre"] in texto


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
