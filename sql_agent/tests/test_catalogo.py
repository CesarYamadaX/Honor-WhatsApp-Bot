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


class TestConceptosDelNegocio:
    """CAPA, SHARE y promotoria: tres cosas que el negocio nombra a diario y
    que no son columnas de ninguna tabla.

    Como en TestNotasDeNegocio, esto no verifica que el modelo acierte --eso
    depende del LLM-- sino que la informacion que lo evitaria le llegue.
    """

    def test_capa_esta_definido_y_avisa_de_la_tienda_repetida(self):
        texto = catalogo.indice()
        assert "CAPA" in texto
        # Que es: el maximo, no el promedio ni el ultimo mes.
        assert "MAX" in texto
        # Y la trampa medida: el mismo numero de tienda con varios nombres
        # parte la tienda y hunde su CAPA.
        assert "ELEKTRA.COM" in texto and "CHARINDEX" in texto

    def test_share_se_calcula_en_una_sola_pasada(self):
        texto = catalogo.indice()
        assert "SHARE" in texto
        # Lo que arruina un share: filtrar la marca en el WHERE deja el
        # denominador sin las demas marcas y todo da 100%.
        assert "CASE WHEN [Brand]" in texto
        assert "WHERE" in texto and "100%" in texto

    def test_la_promotoria_apunta_a_sus_tablas(self):
        texto = catalogo.indice()
        assert "PROMOTORIA" in texto
        assert "HCReport" in texto and "V_HCReport_Informes" in texto
        assert "SEMANA COMPLETA" in texto

    def test_la_ficha_mensual_trae_el_mapa_de_semanas_y_sus_dos_avisos(self):
        ficha = catalogo.describir_objeto.invoke({"nombre": "V_HCReport_Informes"})
        # El mes se escribe en ingles: sin esto el agente prueba '2026-08',
        # no encuentra nada y reporta que no hay promotoria.
        assert "2026-August" in ficha
        # La semana que representa cada mes.
        assert "W35 agosto" in ficha
        # Los dos limites medidos de la vista.
        assert "4 529" in ficha and "8 326" in ficha
        assert "2027" in ficha

    def test_un_filtro_con_un_valor_que_no_existe_se_detecta(self):
        # Medido el 25 de septiembre de 2026: en la consolidada la cadena se
        # llama 'Liverpool Offline' y 'Liverpool Online'. 'LIVERPOOL' a secas
        # no existe, y ese filtro se lleva Liverpool entero SIN dar error.
        avisos = catalogo.filtros_que_no_casan(
            "SELECT * FROM ETL_Consolidado_B2B WHERE [Retail] IN ('LIVERPOOL')"
        )
        assert len(avisos) == 1
        assert "no existe 'LIVERPOOL'" in avisos[0]
        assert "Liverpool Offline" in avisos[0]

    def test_avisa_aunque_los_demas_valores_del_IN_si_existan(self):
        # Este es el peor caso: la consulta devuelve filas, asi que ni
        # siquiera viene vacia. Solo falta Liverpool.
        avisos = catalogo.filtros_que_no_casan(
            "SELECT * FROM ETL_Consolidado_B2B WHERE [Retail] IN ('LIVERPOOL', 'Coppel')"
        )
        assert len(avisos) == 1 and "'LIVERPOOL'" in avisos[0]

    def test_los_valores_buenos_no_molestan(self):
        for query in (
            "SELECT * FROM ETL_Consolidado_B2B WHERE [Retail] IN ('Liverpool Offline')",
            # El servidor no distingue mayusculas; esta comprobacion tampoco.
            "SELECT * FROM ETL_Consolidado_B2B v WHERE v.[Retail] = 'elektra'",
            "SELECT * FROM ETL_Consolidado_B2B WHERE [Brand] = 'HONOR'",
        ):
            assert catalogo.filtros_que_no_casan(query) == []

    def test_la_misma_palabra_puede_existir_en_otra_tabla(self):
        # 'Liverpool' a secas SI es un valor de Complete_Scrap.retail. La
        # comprobacion es por objeto, no por nombre de columna.
        assert catalogo.filtros_que_no_casan(
            "SELECT * FROM Complete_Scrap WHERE [retail] = 'Liverpool'"
        ) == []

    def test_lo_que_no_se_puede_comprobar_se_deja_pasar(self):
        for query in (
            # Sin foto de esa columna no hay con que comparar.
            "SELECT * FROM ETL_Consolidado_B2B WHERE [Store] = 'CUAUHTEMOC'",
            # Ni de esa tabla.
            "SELECT * FROM [UNICOS$] WHERE [Retail] = 'LIVERPOOL'",
            # Un LIKE lleva comodines: 'LIVERPOOL' no tiene que existir tal cual.
            "SELECT * FROM ETL_Consolidado_B2B WHERE [Retail] LIKE '%LIVERPOOL%'",
            # Y un <> con un valor que no existe no esconde ninguna fila.
            "SELECT * FROM ETL_Consolidado_B2B WHERE [Retail] <> 'LIVERPOOL'",
        ):
            assert catalogo.filtros_que_no_casan(query) == []

    def test_no_se_vuelcan_las_222_marcas_en_el_aviso(self):
        # Volcarlas seria gastar el contexto de la vuelta en la que tiene que
        # corregir. Lo parecido a lo que pidio va primero.
        aviso = catalogo.filtros_que_no_casan(
            "SELECT * FROM ETL_Consolidado_B2B WHERE [Brand] = 'HUAWEI HONOR'"
        )[0]
        assert aviso.count(",") <= catalogo.MAX_VALORES_SUGERIDOS
        assert "mas)" in aviso
        listados = aviso.split("son: ")[1]
        assert listados.startswith("Honor") or listados.startswith("HUAWEI")

    def test_la_ficha_mensual_dice_que_el_promotor_es_un_numero(self):
        # Paso en produccion: la nota decia "[status_of_promoter] es promotor
        # fijo + promotor doble" y el modelo lo leyo como una lista de
        # valores de texto. Escribio `IN ('promotor fijo','promotor doble')`
        # contra una columna float, reviento con "Error converting data type
        # varchar to float", lo reintento igual y se rindio: la pregunta se
        # quedo sin contestar.
        ficha = catalogo.describir_objeto.invoke({"nombre": "V_HCReport_Informes"})
        assert "NUMERO" in ficha
        assert ">= 1" in ficha
        assert "varchar to float" in ficha

    def test_la_ficha_semanal_avisa_del_cero_en_la_semana(self):
        ficha = catalogo.describir_objeto.invoke({"nombre": "HCReport"})
        assert "W01" in ficha
        assert "[ID]" in ficha and "CustomerStores" in ficha

    def test_la_llave_compuesta_esta_explicada(self):
        ficha = catalogo.describir_objeto.invoke({"nombre": "HCReport_VW"})
        assert "FKEY_HR" in ficha
        assert "2026W10APSMX222602" in ficha

    def test_el_catalogo_de_tiendas_dice_con_quien_casa_y_con_quien_no(self):
        ficha = catalogo.describir_objeto.invoke({"nombre": "CustomerStores"})
        # Telcel y Sears no casan por nombre: forzarlo con LIKE inventa
        # cruces que no existen.
        assert "Telcel 0/675" in ficha and "Sears 0/91" in ficha

    def test_el_mes_de_promotoria_no_se_adivina(self):
        # Esta en valores.json para que `valores_distintos` lo conteste sin
        # ir a la base, que es justo cuando el agente inventaria '2026-08'.
        periodos = catalogo.valores_conocidos("V_HCReport_Informes", "Period")
        assert "2026-August" in periodos
        assert not any(p.endswith("-08") for p in periodos)


class TestLaHomologacion:
    """El modelo se agrupa por su nombre homologado, y cada base tiene el
    suyo. Es la regla que mas veces ha salido mal: agrupar por el texto del
    retailer parte un mismo telefono en varias filas y los totales salen
    mal, sin que nada falle.
    """

    def test_el_prompt_nombra_la_columna_homologada_de_cada_base(self):
        texto = catalogo.indice()
        assert "[HOMOLOGACION MODELO]" in texto  # venta
        assert "[approved_name]" in texto  # precios

    def test_se_avisa_de_la_homologacion_interna_de_elektra(self):
        # [Modelo Homologado] suena a la buena y no lo es: con la oficial
        # casa el 2.5% del volumen.
        ficha = catalogo.describir_objeto.invoke({"nombre": "EKT_B2B"})
        assert "Modelo Homologado" in ficha
        assert "2.5%" in ficha
        assert "[Desc Sku]" in ficha

    def test_la_ficha_de_scrap_prohibe_agrupar_por_model(self):
        ficha = catalogo.describir_objeto.invoke({"nombre": "Complete_Scrap"})
        assert "[approved_name]" in ficha
        assert "BVL-N49" in ficha  # el codigo del sitio, el que NO se agrupa

    def test_la_ficha_de_scrap_avisa_de_los_precios_en_cero(self):
        # Promediar [price] sin filtrar hunde el precio medio.
        ficha = catalogo.describir_objeto.invoke({"nombre": "Complete_Scrap"})
        assert "[price] > 0" in ficha
        # Y de que hay repetidos por fecha.
        assert "consultation_date" in ficha

    def test_scrap_trae_retailers_que_no_estan_en_venta(self):
        # Suburbia y Mercado Libre solo existen aqui: si el agente no lo
        # sabe, contesta que no hay datos de esas cadenas.
        ficha = catalogo.describir_objeto.invoke({"nombre": "Complete_Scrap"})
        assert "Suburbia" in ficha and "Mercado Libre" in ficha
        assert "Suburbia" in catalogo.valores_conocidos("Complete_Scrap", "retail")


class TestLaRegion:
    """Se pidio poder buscar por region. La region no esta en la venta: vive
    en CustomerStores y cada cadena se enlaza distinto."""

    def test_el_prompt_dice_que_la_region_va_por_la_tabla_cruda(self):
        texto = catalogo.indice()
        assert "REGION" in texto
        assert "CustomerStores" in texto and "TABLA CRUDA" in texto

    def test_se_avisa_de_las_dos_cadenas_que_no_casan_por_nombre(self):
        # Elektra y Sears: forzarlo con LIKE inventaria cruces.
        texto = catalogo.indice()
        assert "Elektra y Sears NO casan por nombre" in texto
        assert "HONOR NAME" in texto

    def test_la_ficha_del_catalogo_trae_el_enlace_de_cada_cadena(self):
        ficha = catalogo.describir_objeto.invoke({"nombre": "CustomerStores"})
        for tabla in ("COPPEL_B2B", "TELCEL_B2B", "ATT_B2B", "LIVERPOOL_B2B$",
                      "EKT_B2B", "SEARS_B2B"):
            assert tabla in ficha, tabla
        # Y como sacar el numero, que es la parte que nadie adivina.
        assert "CHARINDEX" in ficha and "TRY_CONVERT" in ficha

    def test_unicos_dice_como_se_arma_la_llave_de_cada_cruda(self):
        ficha = catalogo.describir_objeto.invoke({"nombre": "UNICOS$"})
        # Coppel: la final es marca + descripcion.
        assert "CONCAT" in ficha and "[Marca]" in ficha
        # Sears: la final antepone la palabra.
        assert "'Sears '" in ficha
        # AT&T: ahi la final no sirve.
        assert "0%" in ficha


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
