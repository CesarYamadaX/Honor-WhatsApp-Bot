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

    def test_la_promotoria_sale_de_hcreport_con_fijo_o_doble(self):
        # Regla del negocio (2 de octubre de 2026): HCReport, y hay
        # promotoria si [FIXED PROMOTER] o [DOUBLE PROMOTER] vale 1.
        texto = catalogo.indice()
        assert "[FIXED PROMOTER] = 1 O [DOUBLE PROMOTER] = 1" in texto
        # El mes es su ultima semana completa.
        assert "W35 agosto" in texto
        # La cadena por COALESCE, para no perder Movistar.
        assert catalogo.CADENA_DE_PROMOTORIA in texto
        assert "MOVISTAR 32" in texto

    def test_la_ficha_mensual_dice_que_no_se_use(self):
        ficha = catalogo.describir_objeto.invoke({"nombre": "V_HCReport_Informes"})
        assert "NO la uses para promotoria" in ficha
        assert "1 289" in ficha and "1 323" in ficha

    def test_liverpool_vive_partido_en_dos_vistas(self):
        # Medido el 25 de septiembre de 2026: vw_ETL_Liverpool son 99 040
        # unidades de agosto y vw_ETL_Liverpool_IOT otras 73 608. Juntas dan
        # 172 648, que es lo que la consolidada reporta para 'Liverpool
        # Offline' + 'Liverpool Online'. El bot contesto 99 040 a "cuanto
        # vendio Liverpool en agosto": el 57%, sin una señal de que faltaba.
        avisos = catalogo.avisos_de_media_cadena(
            "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_Liverpool] WHERE [Year]=2026"
        )
        assert len(avisos) == 1
        assert "vw_ETL_Liverpool_IOT" in avisos[0]
        assert "172 648" in avisos[0]

    def test_si_ya_estan_las_dos_no_falta_nada(self):
        for query in (
            "SELECT * FROM vw_ETL_Liverpool UNION ALL SELECT * FROM vw_ETL_Liverpool_IOT",
            "SELECT * FROM vw_ETL_COPPEL UNION ALL SELECT * FROM vw_ETL_COPPEL_IOT",
            # El sufijo _IOT no puede leerse como la vista de al lado.
            "SELECT SUM([QTY]) FROM vw_ETL_Liverpool_IOT",
            "SELECT SUM([QTY]) FROM vw_ETL_COPPEL_IOT",
            "SELECT * FROM ETL_Consolidado_B2B WHERE [Retail] LIKE 'Liverpool%'",
        ):
            assert catalogo.avisos_de_media_cadena(query) == []

    def test_coppel_tambien_va_partido_y_la_medicion_de_un_mes_lo_escondia(self):
        # Esto empezo siendo una prueba de que Coppel SI cuadraba, escrita
        # sobre una medicion de agosto de 2026 -- el unico mes de 2026 en que
        # vw_ETL_COPPEL_IOT viene vacio. Sobre toda la historia no cuadra:
        # 19 542 084 + 880 689 = 20 422 773, que es 'Coppel' en la
        # consolidada. En produccion, "ventas de Coppel por mes en 2026"
        # contestaba 4 186 855 de 5 067 544: faltaba el 17%.
        avisos = catalogo.avisos_de_media_cadena("SELECT * FROM vw_ETL_COPPEL")
        assert len(avisos) == 1 and "vw_ETL_COPPEL_IOT" in avisos[0]

    def test_tambien_cuenta_la_tabla_cruda_no_solo_la_vista(self):
        # El caso de produccion fue justo por ahi: el modelo consulto
        # COPPEL_B2B directo. Y la tabla de Liverpool se llama
        # LIVERPOOL_B2B$, con un $ que un \b no sabe delimitar.
        for tabla, falta in (
            ("[RAW_DATA_B2B].dbo.[COPPEL_B2B]", "Coppel_IOT"),
            ("[RAW_DATA_B2B].dbo.[LIVERPOOL_B2B$]", "Liverpool_IOT"),
        ):
            avisos = catalogo.avisos_de_media_cadena(f"SELECT * FROM {tabla}")
            assert len(avisos) == 1 and falta in avisos[0]
            # A las crudas no se les ofrece el UNION: Coppel_IOT no tiene ni
            # fecha ni cantidad, y ponerlas en la misma forma es lo que hace
            # justamente la vista vw_ETL_*.
            assert "UNION ALL" not in avisos[0]

    def test_las_cadenas_de_una_sola_vista_no_se_avisan(self):
        for vista in ("vw_ETL_Elektra", "vw_ETL_TELCEL_B2B", "vw_ETL_Sears",
                      "vw_ETL_Movistar_CAC", "EKT_B2B"):
            assert catalogo.avisos_de_media_cadena(f"SELECT * FROM {vista}") == []

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
        # Sin importar mayusculas: la base escribio 'Honor' y desde el
        # 1/10/2026 escribe 'HONOR'.
        assert listados.upper().startswith(("HONOR", "HUAWEI"))

    def test_la_ficha_semanal_tiene_la_regla(self):
        ficha = catalogo.describir_objeto.invoke({"nombre": "HCReport"})
        assert "[FIXED PROMOTER] = 1 O [DOUBLE PROMOTER] = 1" in ficha
        # Solo el doble existe: filtrar solo el fijo se las salta.
        assert "47 filas" in ficha

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


class TestLasVistasPartidasSeCompletanSolas:
    """El aviso de media cadena no alcanzo: tres veces seguidas (30 de
    septiembre de 2026) el modelo lo leyo, contesto "no se puede determinar
    el total de Coppel" y siguio con las demas cadenas. Ahora la consulta a
    la VISTA se completa sola con su otra mitad. Verificado contra la base:
    Coppel 2026 da 5 067 544 y Liverpool agosto 172 648, lo mismo que la
    consolidada.
    """

    def test_la_vista_de_coppel_sola_se_completa(self):
        nueva, hechas = catalogo.completar_media_cadena(
            "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[vw_ETL_COPPEL] WHERE [Year]=2026"
        )
        assert hechas == ["vw_ETL_COPPEL con vw_ETL_COPPEL_IOT"]
        assert "UNION ALL" in nueva and "[vw_ETL_COPPEL_IOT]" in nueva
        # Y ya no la bloquea la guardia: la consulta nombra las dos mitades.
        assert catalogo.avisos_de_media_cadena(nueva) == []

    def test_se_une_por_columnas_y_no_con_select_asterisco(self):
        # A seis vistas les agregaron [Key prices] y a tres no: un SELECT *
        # entre dos de ellas ya no es seguro.
        nueva, _ = catalogo.completar_media_cadena("SELECT SUM(QTY) FROM vw_ETL_Liverpool")
        assert "SELECT *" not in nueva
        assert "[QTY]" in nueva and "Key prices" not in nueva

    def test_respeta_el_alias_que_traia(self):
        nueva, _ = catalogo.completar_media_cadena(
            "SELECT v.[Store], SUM(v.[QTY]) FROM vw_ETL_Liverpool v GROUP BY v.[Store]"
        )
        assert ") AS v GROUP BY" in nueva

    def test_sin_alias_toma_el_nombre_de_la_vista(self):
        # Para que un [vw_ETL_COPPEL].[Brand] en el SELECT siga funcionando.
        nueva, _ = catalogo.completar_media_cadena(
            "SELECT [vw_ETL_COPPEL].[Brand] FROM [vw_ETL_COPPEL] WHERE [Year]=2026"
        )
        assert "AS [vw_ETL_COPPEL] WHERE" in nueva
        assert nueva.startswith("SELECT [vw_ETL_COPPEL].[Brand] FROM (")

    def test_no_toca_lo_que_no_hace_falta(self):
        for query in (
            "SELECT SUM(QTY) FROM vw_ETL_COPPEL_IOT",
            "SELECT * FROM vw_ETL_COPPEL UNION ALL SELECT * FROM vw_ETL_COPPEL_IOT",
            "SELECT * FROM vw_ETL_Elektra",
            # Las crudas no se completan: siguen bloqueadas por la guardia.
            "SELECT * FROM [RAW_DATA_B2B].dbo.[COPPEL_B2B]",
        ):
            assert catalogo.completar_media_cadena(query) == (query, [])


class TestConsolidadaDeRepuesto:
    """Con ETL_Consolidado_B2B rota en el servidor, la consulta se reconstruye
    desde sus nueve vistas. Verificado contra la base el 30/09/2026: 160 909
    unidades de Honor en agosto y 2 056 940 del mercado, lo mismo que la
    consolidada cuando funcionaba.
    """

    def test_cambia_la_consolidada_por_sus_nueve_vistas(self):
        nueva = catalogo.consolidada_de_repuesto(
            "SELECT SUM([QTY]) FROM [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B] WHERE [Brand]='HONOR'"
        )
        for vista in catalogo._VISTAS_DE_LA_CONSOLIDADA:
            assert f"[{vista}]" in nueva
        assert nueva.count("UNION ALL") == 8
        assert "SELECT *" not in nueva
        assert "AS [ETL_Consolidado_B2B] WHERE" in nueva

    def test_respeta_el_alias(self):
        nueva = catalogo.consolidada_de_repuesto(
            "SELECT v.[Retail] FROM ETL_Consolidado_B2B v GROUP BY v.[Retail]"
        )
        assert ") AS v GROUP BY" in nueva

    def test_sin_consolidada_no_hay_repuesto(self):
        assert catalogo.consolidada_de_repuesto("SELECT * FROM vw_ETL_Elektra") is None


class TestPromotoriaNoCuentaDeMas:
    """La promotoria se saca de HCReport: una tienda la tiene si [FIXED
    PROMOTER] o [DOUBLE PROMOTER] vale 1. "Cuantas tiendas de Coppel tienen
    promotor" salio 1 721 sin semana ni cadena; en W35 de 2026 eran 676.
    """

    PREGUNTA = "cuantas tiendas de Coppel tienen promotor de Honor"
    BIEN = (
        "SELECT COUNT(DISTINCT CASE WHEN h.[FIXED PROMOTER] = 1 OR "
        "h.[DOUBLE PROMOTER] = 1 THEN h.[ID] END) "
        "FROM [GTM BI Database].dbo.[HCReport] h "
        f"{catalogo._UNION_DE_PROMOTORIA} "
        "WHERE h.[YEAR] = 2026 AND h.[Week] = 'W35' "
        f"AND {catalogo.CADENA_DE_PROMOTORIA} = 'COPPEL'"
    )

    def test_bien_hecha_pasa(self):
        assert catalogo.avisos_de_promotoria(self.BIEN, self.PREGUNTA) == []

    def test_las_vistas_viejas_se_mandan_a_hcreport(self):
        for vista in ("V_HCReport_Informes", "HCReport_VW"):
            avisos = catalogo.avisos_de_promotoria(
                f"SELECT * FROM [GTM BI Database].dbo.[{vista}] WHERE [Period] = '2026-August'",
                self.PREGUNTA,
            )
            assert len(avisos) == 1 and "[HCReport]" in avisos[0]
            assert "1 323" in avisos[0]

    def test_sin_semana_no_pasa(self):
        avisos = catalogo.avisos_de_promotoria(
            self.BIEN.replace("h.[YEAR] = 2026 AND h.[Week] = 'W35' AND ", ""),
            self.PREGUNTA,
        )
        assert any("semana" in a for a in avisos)

    def test_una_tendencia_por_semana_si_pasa(self):
        assert catalogo.avisos_de_promotoria(
            "SELECT h.[YEAR], h.[Week], COUNT(DISTINCT CASE WHEN h.[FIXED PROMOTER] = 1 "
            "OR h.[DOUBLE PROMOTER] = 1 THEN h.[ID] END) FROM HCReport h "
            "GROUP BY h.[YEAR], h.[Week]",
            "promotoria por semana",
        ) == []

    def test_solo_el_fijo_no_pasa(self):
        avisos = catalogo.avisos_de_promotoria(
            "SELECT COUNT(*) FROM HCReport h WHERE h.[YEAR] = 2026 AND "
            "h.[Week] = 'W35' AND h.[FIXED PROMOTER] = 1",
            "cuantas tiendas tienen promotoria",
        )
        assert len(avisos) == 1 and "DOS columnas" in avisos[0]

    def test_si_preguntan_por_el_fijo_si_pasa(self):
        assert catalogo.avisos_de_promotoria(
            "SELECT COUNT(*) FROM HCReport h WHERE h.[YEAR] = 2026 AND "
            "h.[Week] = 'W35' AND h.[FIXED PROMOTER] = 1",
            "cuantas tiendas tienen promotor fijo",
        ) == []

    def test_sin_cadena_no_pasa(self):
        avisos = catalogo.avisos_de_promotoria(
            "SELECT COUNT(*) FROM HCReport h WHERE h.[YEAR] = 2026 AND h.[Week] = 'W35' "
            "AND (h.[FIXED PROMOTER] = 1 OR h.[DOUBLE PROMOTER] = 1)",
            self.PREGUNTA,
        )
        assert len(avisos) == 1 and "'COPPEL'" in avisos[0]

    def test_un_join_normal_pierde_movistar(self):
        avisos = catalogo.avisos_de_promotoria(
            self.BIEN.replace("LEFT JOIN", "JOIN"), self.PREGUNTA
        )
        assert len(avisos) == 1 and "LEFT JOIN" in avisos[0]

    def test_la_cadena_solo_del_catalogo_pierde_movistar(self):
        avisos = catalogo.avisos_de_promotoria(
            self.BIEN.replace(catalogo.CADENA_DE_PROMOTORIA, "cs.[RetailName]"),
            self.PREGUNTA,
        )
        assert len(avisos) == 1 and "Movistar" in avisos[0]

    def test_movistar_se_reconoce_en_la_pregunta(self):
        avisos = catalogo.avisos_de_promotoria(
            self.BIEN, "cuantas tiendas de Movistar tienen promotor"
        )
        assert len(avisos) == 1 and "'MOVISTAR'" in avisos[0]

    def test_otras_tablas_no_se_tocan(self):
        assert catalogo.avisos_de_promotoria("SELECT * FROM CustomerStores", self.PREGUNTA) == []

    def test_cuantos_promotores_cuenta_personas(self):
        # Fijo y doble en 1 son DOS promotores: contar tiendas da de menos.
        pregunta = "cuantos promotores de Honor hay en Coppel"
        avisos = catalogo.avisos_de_promotoria(self.BIEN, pregunta)
        assert len(avisos) == 1 and "DOS promotores" in avisos[0]

        personas = self.BIEN.replace(
            "COUNT(DISTINCT CASE WHEN h.[FIXED PROMOTER] = 1 OR "
            "h.[DOUBLE PROMOTER] = 1 THEN h.[ID] END)",
            "SUM(h.[FIXED PROMOTER] + h.[DOUBLE PROMOTER])",
        )
        assert catalogo.avisos_de_promotoria(personas, pregunta) == []
        separadas = personas.replace(
            "SUM(h.[FIXED PROMOTER] + h.[DOUBLE PROMOTER])",
            "SUM(h.[FIXED PROMOTER]) + SUM(h.[DOUBLE PROMOTER])",
        )
        assert catalogo.avisos_de_promotoria(separadas, pregunta) == []

    def test_la_nota_dice_como_contestar(self):
        # 1/10/2026: "59 promotores distribuidos en 92 tiendas" -- 92 eran
        # todas las de Sears; con promotor, 59. Y "el ano pasado" se contesto
        # con el rango de 2025 en vez de la misma semana.
        texto = catalogo.indice()
        assert "nunca '59 promotores distribuidos en 92 tiendas'" in texto
        assert "MISMA semana del ano anterior" in texto

    def test_la_nota_dice_que_uno_y_uno_son_dos(self):
        assert "tiene DOS promotores" in catalogo.indice()
        ficha = catalogo.describir_objeto.invoke({"nombre": "HCReport"})
        assert "son DOS promotores" in ficha


class TestCoberturaDeDatos:
    def test_se_avisa_desde_cuando_hay_datos_de_cada_cadena(self):
        # "Elektra julio 2025: 0 unidades" -- no es que no vendiera, es que
        # la carga empieza en octubre de 2025.
        nota = " ".join(catalogo.NOTAS_GENERALES)
        assert "OCTUBRE DE 2025" in nota
        assert "NUNCA pongas '0 unidades'" in nota

