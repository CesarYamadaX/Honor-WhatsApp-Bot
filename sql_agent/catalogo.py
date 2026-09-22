"""Catalogo de esquema del servidor GTM BI, leido de `catalogo.json`.

Existe porque DB_URI apunta a `master`: las herramientas de esquema de
LangChain (`sql_db_list_tables`, `sql_db_schema`) reflejan la base del
connection string, asi que ahi solo ven los objetos de sistema de master y el
agente queda ciego ante los datos reales, que viven en otras cinco bases del
mismo servidor. En vez de que el modelo adivine nombres, el catalogo le da el
indice completo en el prompt y las columnas exactas bajo demanda.

`catalogo.json` es una foto del esquema, no una lectura en vivo: si alguien
agrega o renombra columnas en el servidor, el bot sigue creyendo lo que dice el
archivo. Se regenera desde INFORMATION_SCHEMA y sys.partitions.
"""

import json
from functools import lru_cache
from pathlib import Path

from langchain.tools import tool

RUTA_CATALOGO = Path(__file__).with_name("catalogo.json")
RUTA_VALORES = Path(__file__).with_name("valores.json")


@lru_cache(maxsize=1)
def cargar_valores() -> dict:
    """Valores conocidos de las columnas que el agente consulta seguido.

    Son una foto, calculada una vez contra el servidor. Existen porque un
    SELECT DISTINCT sobre ETL_Consolidado_B2B (39M filas, UNION ALL de nueve
    vistas agregadas) tarda minutos: en produccion cada consulta de esas se
    comia los 20s de timeout de `valores_distintos` sin devolver nada, y tres
    llamadas agotaban los 90 segundos de la peticion entera antes de que la
    consulta de verdad alcanzara a correr.

    Son columnas de baja cardinalidad y poco movimiento (retailers, marcas,
    carriers, anios): si aparece un retailer nuevo, se regenera el archivo.
    """
    if not RUTA_VALORES.exists():
        return {}
    return json.loads(RUTA_VALORES.read_text(encoding="utf-8"))


def valores_conocidos(objeto: str, columna: str) -> list[str] | None:
    """Los valores precalculados de esa columna, o None si no hay foto."""
    por_objeto = cargar_valores().get(objeto, {})
    for nombre, valores in por_objeto.items():
        if nombre.casefold() == columna.casefold():
            return valores
    return None

# Lo que el esquema NO dice y hace que el agente se equivoque con seguridad.
#
# Viven aqui y no en catalogo.json a proposito: el JSON es un volcado de
# INFORMATION_SCHEMA que se regenera, y una regeneracion borraria cualquier
# nota escrita a mano. Esto es conocimiento de negocio curado, y se revisa
# como codigo.
#
# El caso que las motivo: a "cuantas ventas hubo en Elektra en marzo 2026" el
# agente respondio "no se encontraron ventas" porque genero
# `WHERE [Carrier] = 'Elektra'` sobre EKT_B2B. Ahi Carrier son operadores
# telefonicos, no retailers, asi que el filtro daba cero filas -- y un cero
# se reporto como "no hay ventas" en vez de como "mi filtro esta mal".
NOTAS_GENERALES = [
    "Las tablas de RAW_DATA_B2B son la descarga cruda de UN solo retailer cada "
    "una, identificado por el nombre de la tabla y no por una columna: "
    "EKT_B2B=Elektra, COPPEL_B2B y Coppel_IOT=Coppel, TELCEL_B2B=Telcel, "
    "ATT_B2B=AT&T, LIVERPOOL_B2B$ y Liverpool_IOT=Liverpool, MVS_B2B=Movistar, "
    "SEARS_B2B=Sears. Salvo SEARS_B2B, ninguna tiene columna [Retail]: filtrar "
    "por el nombre del retailer dentro de ellas siempre da cero filas.",
    "[Carrier] es el operador telefonico (TELCEL, AT&T, MOVISTAR, ABIERTA), "
    "NUNCA el retailer. Para filtrar por retailer se usa [Retail].",
    "La vista [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B] es la fuente OFICIAL de "
    "venta B2B, la que usa el negocio: unifica a todos los retailers con "
    "columnas [Retail], [Carrier], [Year], [Month], [Day] y [QTY]. Usala por "
    "defecto, y escribe su nombre COMPLETO tal cual: vive en RAW_DATA_B2B y "
    "NO en [GTM BI Database] -- escribirla ahi es el error mas comun y falla "
    "con 'Invalid object name'. Las tablas crudas son su insumo y no cuadran con ella (filtran "
    "distinto y no estan agregadas); ve a ellas solo si te piden el dato "
    "crudo, y entonces avisa que no es la cifra oficial.",
    "Este es el BI de Honor Mexico, pero las tablas de venta traen TODAS las "
    "marcas (Samsung, Motorola, Oppo, Xiaomi, Apple...): son datos de "
    "competencia. Por eso 'las ventas de Honor' NO son el total de la tabla: "
    "exigen `[Brand] = 'HONOR'` explicito. Si el usuario nombra una marca y no "
    "filtras por ella, le estaras reportando el mercado entero como si fuera "
    "de esa marca, y el numero saldra varias veces mas grande de lo real.",
    "NUNCA filtres ETL_Consolidado_B2B por [Retail]. Si la pregunta es de UN "
    "retailer, consulta directamente la vista de ESE retailer -- SALVO que "
    "pidan detalle POR MODELO, que va por la tabla cruda unida a la "
    "homologacion (ver la regla de UNICOS$: es la unica forma de no partir un "
    "mismo modelo en varias filas, y ademas tarda 2s en vez de 60). Las "
    "vistas por retailer son para totales, tendencias y cortes por tienda o "
    "carrier: "
    "[RAW_DATA_B2B].dbo.[vw_ETL_COPPEL] para Coppel, [vw_ETL_Elektra] para "
    "Elektra, [vw_ETL_Liverpool] para Liverpool, [vw_ETL_TELCEL_B2B] para "
    "Telcel (cubre sus seis canales), [vw_ETL_ATT_Postpaid] para AT&T, "
    "[vw_ETL_Movistar_CAC] para Movistar y [vw_ETL_Sears] para Sears. Traen "
    "las mismas columnas que la consolidada --es la union de estas nueve-- "
    "asi que el dato es igual de oficial. "
    "El motivo es de rendimiento, medido: filtrar la consolidada por [Retail] "
    "tarda entre 60 y 210 SEGUNDOS, porque [Retail] es una constante fija en "
    "cada rama de la union y el motor no puede descartar ninguna: las calcula "
    "todas. Acotar por fecha NO ayuda (un solo SUM de un mes tarda 63s igual). "
    "Deja la consolidada para comparar entre retailers o filtrar por [Brand], "
    "que si es rapido (~9s).",
    "HOMOLOGACION: SOLO existen DOS formas validas de unir con UNICOS$, y "
    "cada tabla va con SU columna. Cruzarlas pierde casi todo el volumen en "
    "silencio.\n"
    "  (A) tabla CRUDA  +  u.[DESCRIPCION]\n"
    "      ON u.[Retail]='COPPEL' AND u.[DESCRIPCION] = v.[Modelo]\n"
    "      Sirve para TODOS los retailers. Es la opcion por defecto.\n"
    "  (B) vw_ETL_COPPEL o vw_ETL_COPPEL_IOT  +  "
    "u.[MATERIAL DESCRIPTION FINAL]\n"
    "      ON u.[Retail]='COPPEL' AND "
    "u.[MATERIAL DESCRIPTION FINAL] = v.[Material_Description]\n"
    "      Solo esas dos vistas, solo esa columna.\n"
    "PROHIBIDO: una vista vw_ETL_* con u.[DESCRIPCION]. Medido, pierde el "
    "96% del volumen y devuelve un top que parece razonable pero es falso.\n"
    "AGRUPAR POR MODELO SE HACE SIEMPRE POR LA HOMOLOGACION, no por la "
    "descripcion cruda del retailer. Cada cadena escribe el mismo telefono a "
    "su manera ('IP. 15 PRO MAX 256GB', 'IPHONE 15 128 GB'), asi que agrupar "
    "por el texto del retailer parte un mismo modelo en varias filas y los "
    "totales salen mal. La homologacion vive en "
    "[GTM BI Database].dbo.[UNICOS$] y la columna a agrupar es "
    "[HOMOLOGACION MODELO] ('APPLE IPHONE 15 128GB 5G').\n"
    "La RUTA SEGURA es unir contra la TABLA CRUDA del retailer por "
    "[DESCRIPCION], que es la descripcion tal cual la escribe la cadena:\n"
    "  FROM [RAW_DATA_B2B].dbo.[COPPEL_B2B] v\n"
    "  JOIN [GTM BI Database].dbo.[UNICOS$] u\n"
    "    ON u.[Retail] = 'COPPEL' AND u.[DESCRIPCION] = v.[Modelo]\n"
    "  GROUP BY u.[HOMOLOGACION MODELO]\n"
    "La columna de descripcion cambia por tabla: COPPEL_B2B y Coppel_IOT "
    "[Modelo], EKT_B2B [Desc Sku], TELCEL_B2B [Descripción], LIVERPOOL_B2B$ y "
    "Liverpool_IOT [Material Description], ATT_B2B, MVS_B2B y SEARS_B2B "
    "[Model]. Medido: tarda 1-3s y deja sin casar 0% en Telcel, 0.7% en "
    "Elektra y 2.5% en Coppel.\n"
    "[Retail] en UNICOS$ va en MAYUSCULAS y no se escribe como en las ventas: "
    "COPPEL, ELEKTRA, TELCEL, LIVERPOOL, SEARS, 'AT&T DEALERS', 'AT&T CACS', "
    "'MOVISTAR CACS', 'MOVISTAR CALL CENTER'.",
    "CUIDADO al homologar desde una vista vw_ETL_*: su [Material_Description] "
    "no es la descripcion del retailer, es un CONCAT que cada vista arma "
    "distinto. Solo en vw_ETL_COPPEL y vw_ETL_COPPEL_IOT es "
    "CONCAT(Marca, Modelo), y ahi SI casa --al 100%-- pero contra "
    "[MATERIAL DESCRIPTION FINAL] de UNICOS$, no contra [DESCRIPCION]. En las "
    "demas lleva un SKU o un codigo delante (Elektra CONCAT(SKU, desc sku), "
    "Telcel CONCAT(Material, Descripción), Sears CONCAT('Sears ', Model)) y "
    "NO casa con ninguna columna de UNICOS$: medido, el join pierde el 100% "
    "en Telcel y en Elektra. Para esos retailers homologa desde la tabla "
    "cruda.\n"
    "REGLA GENERAL: despues de un join de homologacion, comprueba cuanto "
    "volumen quedo sin casar (LEFT JOIN y contar los NULL). Si se perdio una "
    "parte grande, la llave esta mal y el top que salga sera falso aunque "
    "parezca razonable: paso de verdad, un top 10 construido con el 3.6% de "
    "las ventas.",
    "UNICOS$ tiene ademas [CLASSIFICATION], que separa SMARTPHONE de TABLET, "
    "LAPTOP, COMPUTER, SMARTWATCH, AUDIO y ACCESORIO. Es la forma limpia de "
    "responder 'solo celulares' o 'solo accesorios' sin adivinar por el "
    "nombre del modelo.",
    "LA COLUMNA DE FECHA SE LLAMA DISTINTO EN CADA TABLA. En las vistas "
    "vw_ETL_* y en ETL_Consolidado_B2B siempre es [Date] (datetime) mas "
    "[Year], [Month] y [Day] como ENTEROS: ahi se filtra con "
    "`[Year]=2026 AND [Month]=3`. En las tablas crudas cambia por retailer: "
    "COPPEL_B2B y Coppel_IOT [Día], LIVERPOOL_B2B$ y Liverpool_IOT "
    "[Día/Periodo], EKT_B2B [Day], TELCEL_B2B, MVS_B2B y SEARS_B2B [Fecha], "
    "ATT_B2B [Fecha de Transacción]; todas datetime, se filtran por rango "
    "(`>= '2026-01-01' AND < '2026-02-01'`).\n"
    "TRAMPA: [Day] no significa lo mismo en los dos lados. En EKT_B2B es la "
    "fecha completa (datetime); en las vistas vw_ETL_* es el dia del mes "
    "(entero 1-31). Y [Month] es entero en las vistas pero texto en EKT_B2B "
    "('Mar', 'Nov'), MVS_B2B y SEARS_B2B. Comprueba con `describir_objeto` "
    "antes de filtrar por fecha en una tabla que no hayas usado.",
    "PRECIOS: hay DOS fuentes y no son lo mismo. "
    "[GTM BI Database].dbo.[Prices] es el precio interno, el que maneja el "
    "negocio, y es la fuente por defecto; su [Material Description] se escribe "
    "como la descripcion del retailer, igual que [DESCRIPCION] de UNICOS$. "
    "Si ahi no aparece el modelo, usa la herramienta `precio_en_internet`, "
    "que consulta el scraping de las tiendas en linea ([Scrap]): trae el "
    "precio publicado, la fecha, el carrier y la liga del producto. "
    "Al responder con esa fuente hay que decir SIEMPRE que es el precio "
    "publicado en internet ese dia --no el interno-- y dar la liga y el "
    "carrier de cada uno: sin el carrier no se sabe si es con plan o libre, y "
    "sin la liga el usuario no puede verificarlo.",
    "Las vistas y tablas con IOT en el nombre (vw_ETL_COPPEL_IOT, "
    "vw_ETL_Liverpool_IOT, Coppel_IOT, Liverpool_IOT) son la linea que el "
    "negocio llama accesorios: NO son telefonos. En los datos son tablets, "
    "laptops, equipos gamer y computo (iPad, Galaxy Tab, MacBook, Alienware, "
    "Acer Aspire). Si preguntan por celulares no las incluyas; si preguntan "
    "por accesorios o por tablets y laptops, son ESAS y no las normales.",
    "En las tablas de venta no hay columna [Model] ni [Modelo]. El modelo del "
    "telefono va dentro de [Material_Description] en ETL_Consolidado_B2B (y en "
    "[Modelo Homologado] o [Desc Sku] en las crudas). [Brand] es el "
    "fabricante, no el modelo.",
    "En ETL_Consolidado_B2B cada fila es una combinacion de tienda, SKU, "
    "carrier y dia ya sumada, no un movimiento suelto. Para unidades vendidas "
    "usa SUM([QTY]); COUNT(*) responde otra pregunta y casi nunca es la que "
    "te hicieron.",
    "Los nombres de retailer no son uniformes ni una sola etiqueta por cadena: "
    "Elektra aparece como 'ELEKTRA' en mayusculas, y Telcel esta repartido en "
    "'TELCEL CAC', 'TELCEL RETAIL', 'TELCEL DISTR. ASOC.', 'TELCEL WEB', "
    "'TELCEL CORPO.' y 'TELCEL VENTA EMP.'. Filtra con LIKE, o mira antes con "
    "`valores_distintos` como estan escritos.",
    "EL MODELO SIEMPRE SE AGRUPA POR SU NOMBRE HOMOLOGADO, nunca por el "
    "texto que escribe el retailer, y cada base tiene SU columna homologada:\n"
    "  - venta      -> [GTM BI Database].dbo.[UNICOS$].[HOMOLOGACION MODELO]\n"
    "  - precios    -> [Scrap].dbo.[Complete_Scrap].[approved_name]\n"
    "Las dos usan el mismo vocabulario ('HONOR MAGIC 6 PRO 512GB'), asi que "
    "es por ahi por donde se cruzan precio y venta. Medido: de los 183 "
    "modelos que Elektra vendio en agosto de 2026, 156 tienen precio en "
    "Scrap.\n"
    "CUIDADO con las columnas que dicen 'homologado' y no lo son: "
    "EKT_B2B.[Modelo Homologado] es la homologacion INTERNA de Elektra "
    "('Oppo A6', 'Samsung Galaxy A37') y con la oficial casa el 2.5% del "
    "volumen (4 250 de 167 845 unidades en agosto de 2026). No la uses para "
    "agrupar ni para unir: usa [Desc Sku] contra UNICOS$.[DESCRIPCION].",
    "PRECIOS: lo que cuesta un telefono en la tienda online no esta en las "
    "tablas de venta, esta en [Scrap].dbo.[Complete_Scrap] -- el "
    "web-scraping de los e-commerce, con una fila por SKU y fecha de "
    "consulta. Trae OCHO retailers, y dos de ellos no existen en las tablas "
    "de venta: Suburbia y Mercado Libre. Pide `describir_objeto` de "
    "Complete_Scrap antes de consultarla: [price] trae ceros y el modelo "
    "solo se agrupa bien por [approved_name].",
    "CAPA es la capacidad de venta de una tienda: el MAXIMO que esa tienda "
    "llego a vender en un periodo. No es una columna de ninguna tabla, se "
    "calcula: unidades por tienda y por mes, y de esos meses el MAX. Di "
    "siempre de que ventana salio ('el maximo mensual desde 2025'), porque el "
    "CAPA depende de cuanto historial mires. "
    "OJO con la tienda: la misma tienda puede estar escrita de varias formas. "
    "En Elektra el numero 9797 aparece como 'ELEKTRA.MX', 'ELEKTRA.COM' y "
    "'ELETRA.MX', y medido en 2026 el 73% del volumen (927 913 de 1 268 566 "
    "unidades) esta en numeros de tienda con mas de un nombre. Agrupar por "
    "[Store] parte una tienda en varias y su CAPA sale MAS BAJO de lo que es "
    "-- la tienda 6169 da 670 como 'EKT MEGA ZUMPANGO' y 908 como 'MEGA "
    "ZUMPANGO', y su CAPA real es 908. Ahi agrupa por el numero: "
    "LEFT([Store], CHARINDEX(' ', [Store]+' ')-1). "
    "En Coppel, AT&T, Movistar y Sears el nombre es estable (medido: 0.2% o "
    "menos del volumen en nombres repetidos) y [Store] sirve tal cual.",
    "SHARE es el porcentaje que una marca representa dentro de una tienda (o "
    "de un retailer, o de una region): unidades de la marca entre unidades "
    "TOTALES de esa tienda, en el mismo periodo. El denominador son TODAS las "
    "marcas, asi que sale de UNA sola consulta con un CASE, nunca de dos:\n"
    "  SUM(CASE WHEN [Brand]='HONOR' THEN [QTY] ELSE 0 END) * 100.0\n"
    "    / NULLIF(SUM([QTY]), 0) AS [Share %]\n"
    "NUNCA pongas `[Brand]='HONOR'` en el WHERE si vas a calcular share: te "
    "quedas sin denominador y todo da 100%. Medido en Elektra, agosto 2026: "
    "la tienda mas grande vendio 1 012 unidades, 56 de HONOR, share 5.5%.",
    "PROMOTORIA es cuanta gente de Honor hay en la tienda (promotor fijo, "
    "promotor doble, supervisor, merchandiser, trainer). NO esta en las "
    "tablas de venta: vive en [GTM BI Database].dbo.[HCReport], una fila por "
    "tienda y SEMANA. Para un reporte MENSUAL usa la vista "
    "[GTM BI Database].dbo.[V_HCReport_Informes], que ya toma la ULTIMA "
    "SEMANA COMPLETA de cada mes y ya trae el nombre y la region de la "
    "tienda; su [Period] es texto en ingles ('2026-August') y "
    "[status_of_promoter] es promotor fijo + promotor doble. Para la semana "
    "suelta, [HCReport] o [HCReport_VW]. Pide `describir_objeto` de la que "
    "vayas a usar: cada una tiene su trampa y estan anotadas ahi. "
    "Para cruzar promotoria con VENTA ('las tiendas con promotor venden mas "
    "Honor?'), une el [Coppel Name] de la vista mensual con el [Store] de la "
    "vista de venta del retailer. Eso casa en AT&T (1732 de 1732 tiendas de "
    "2026), Coppel (1792/1800) y Liverpool (193/204); en Elektra casa la "
    "mitad (1253/2307) y en Telcel y Sears NO casa nada (0 de 675 y 0 de "
    "91), asi que ahi no hay cruce por nombre y no hay que forzarlo.",
    "Si una consulta devuelve cero filas, NO concluyas que no hay datos: casi "
    "siempre el filtro esta mal. Revisa con `valores_distintos` la columna que "
    "filtraste antes de responderle al usuario que no hay informacion. Esa "
    "comprobacion va SIEMPRE por `valores_distintos`, nunca por un SELECT "
    "DISTINCT en `consultar_sql`: eso generaria un reporte que pisa el bueno.",
]

# Notas de un objeto concreto, que salen cuando el agente lo consulta.
NOTAS_POR_OBJETO = {
    "UNICOS$": (
        "LA tabla de homologacion: convierte la descripcion que usa cada "
        "retailer en un nombre de modelo unico, y toda agrupacion por modelo "
        "pasa por aqui. Se agrupa por [HOMOLOGACION MODELO].\n"
        "Tiene DOS columnas para unir, y cual usar depende del otro lado:\n"
        "- [DESCRIPCION] es la descripcion tal cual la escribe la cadena, y "
        "casa con la TABLA CRUDA del retailer (COPPEL_B2B [Modelo], EKT_B2B "
        "[Desc Sku], TELCEL_B2B [Descripción], LIVERPOOL_B2B$ [Material "
        "Description], ATT_B2B/MVS_B2B/SEARS_B2B [Model]). Es la ruta que "
        "funciona para todos: medido, pierde 0% en Telcel, 0.7% en Elektra, "
        "2.5% en Coppel.\n"
        "- [MATERIAL DESCRIPTION FINAL] es marca + descripcion, y casa al "
        "100% con [Material_Description] de vw_ETL_COPPEL y "
        "vw_ETL_COPPEL_IOT, que son las unicas vistas que lo arman como "
        "CONCAT(Marca, Modelo). Contra las otras vw_ETL_* no casa nada.\n"
        "[Retail] va EN MAYUSCULAS y no como en las ventas: COPPEL, ELEKTRA, "
        "TELCEL, LIVERPOOL, SEARS, 'AT&T DEALERS', 'AT&T CACS', "
        "'MOVISTAR CACS', 'MOVISTAR CALL CENTER'.\n"
        "[CLASSIFICATION] separa SMARTPHONE de TABLET, LAPTOP, COMPUTER, "
        "SMARTWATCH, AUDIO y ACCESORIO: es la forma limpia de responder "
        "'solo celulares' sin adivinar por el nombre."
    ),
    "EKT_B2B": (
        "Solo ventas de Elektra; no tiene columna [Retail]. OJO con las fechas: "
        "[Month] es texto con la abreviatura inglesa del mes ('Mar', 'Nov'), "
        "no un numero, asi que `[Month] = 3` falla al convertir. Para acotar un "
        "mes usa el rango de [Day], que si es datetime.\n"
        "Y OJO con [Modelo Homologado]: es la homologacion INTERNA de "
        "Elektra ('Oppo A6', 'Samsung Galaxy A37'), NO la del negocio. "
        "Contra UNICOS$.[HOMOLOGACION MODELO] casa el 2.5% del volumen "
        "(4 250 de 167 845 unidades en agosto de 2026). Para agrupar por "
        "modelo, une [Desc Sku] con UNICOS$.[DESCRIPCION] filtrando "
        "u.[Retail] = 'ELEKTRA'."
    ),
    "ETL_Consolidado_B2B": (
        "Se escribe [RAW_DATA_B2B].dbo.[ETL_Consolidado_B2B]; no esta en "
        "[GTM BI Database]. "
        "NO tiene columna [Model] ni [Modelo]: el modelo del telefono vive "
        "dentro de [Material_Description] (que es el SKU mas la descripcion, "
        "p. ej. 'N.HMG8LTNBDLCH AT&T Dealers HONOR MAGIC8 LITE 512GB 5G'). "
        "Si te piden agrupar o mostrar 'el modelo', esa es la columna. "
        "[Brand] es el fabricante (HONOR, SAMSUNG), no el modelo: no la uses "
        "como reemplazo. "
        "LA fuente oficial de venta B2B: es la que usa el negocio, asi que sus "
        "numeros son los buenos. Union de las nueve vistas vw_ETL_*, una por "
        "retailer, ya normalizadas. [Year], [Month] y [Day] son enteros. "
        "Dos cosas que la separan de las tablas crudas: "
        "(1) cada vw_ETL_* filtra la cantidad con `>= 1`, asi que quedan fuera "
        "las devoluciones y ajustes (QTY negativo) y las filas en cero; "
        "ademas vw_ETL_ATT_Postpaid se limita a ciertos canales y "
        "vw_ETL_Movistar_CAC solo trae 'Propio MX'. "
        "(2) viene AGREGADA con GROUP BY tienda/marca/SKU/carrier/dia y "
        "SUM(cantidad), o sea una fila por combinacion y no una por "
        "movimiento. Por eso COUNT(*) aqui NO es el numero de ventas: para "
        "unidades siempre SUM([QTY]). "
        "Reconciliacion verificada en Elektra marzo 2026: 134 049 filas "
        "crudas en EKT_B2B, menos 246 que no pasan el `>= 1` (233 negativas y "
        "13 en cero), menos 263 que el GROUP BY fusiona, dan las 133 540 de la "
        "vista; en unidades, 144 190 crudas + 250 de las negativas = 144 440."
    ),
    "HCReport": (
        "La PROMOTORIA semanal: una fila por tienda y semana, con cuanta "
        "gente de Honor hubo. [FIXED PROMOTER] y [DOUBLE PROMOTER] son los "
        "promotores (el 'status of promoter' del negocio es la SUMA de esos "
        "dos); [Supervisor's visit], [City Manager visit], [Merchandiser], "
        "[Trainer], [FIELD REP] y [SUPERVISOR MIX] son visitas y apoyos, no "
        "promotores.\n"
        "Las fechas NO son fechas: [Week] es texto 'W01'..'W52' (con el cero, "
        "asi que `[Week] = 'W5'` no encuentra nada) y [YEAR] es un numero "
        "decimal (2026.0). Para ordenar semanas usa [YEAR] y [Week], que "
        "siendo 'Wnn' ordena bien como texto.\n"
        "[ID] es la tienda (APSMX222602) y casa con [ID] de CustomerStores, "
        "que es de donde salen el nombre y la region. Se une por ID, NUNCA "
        "por nombre: [NAME] aqui es el nombre del operador ('TELCEL C.A.C. "
        "BCS LA PAZ III') y no coincide con el del catalogo.\n"
        "Trae ~8 330 tiendas por semana, mas de las que hay en CustomerStores."
    ),
    "HCReport_VW": (
        "La misma promotoria semanal que [HCReport], resumida: [PromtsQTY] es "
        "promotor fijo + promotor doble, y [AsigdenPromts] lo dice en texto "
        "('No Promotors', 'One Promotor', 'Two Promotors').\n"
        "NO tiene columnas de tienda ni de fecha sueltas: [FKEY_HR] es una "
        "llave compuesta, CONCAT(ano, semana, id) -- '2026W10APSMX222602'. "
        "Para cruzarla con otra cosa, arma la misma llave del otro lado "
        "(CONCAT([YEAR],[Week],[ID])), o mejor usa [HCReport] directamente, "
        "que trae los tres campos separados y es la misma informacion."
    ),
    "V_HCReport_Informes": (
        "La PROMOTORIA MENSUAL, que es la forma en que el negocio la reporta: "
        "de cada mes se toma la ULTIMA SEMANA COMPLETA y esa semana "
        "representa el mes entero (2026: W05 enero, W09 febrero, W13 marzo, "
        "W17 abril, W22 mayo, W26 junio, W30 julio, W35 agosto, W39 "
        "septiembre, W44 octubre, W48 noviembre, W52 diciembre).\n"
        "[Period] es texto con el mes en INGLES, '2026-August': no es fecha y "
        "no se puede comparar con YEAR()/MONTH(). Para 'agosto de 2026' el "
        "filtro es `[Period] = '2026-August'`. [status_of_promoter] es "
        "promotor fijo + promotor doble de esa semana. La tienda es "
        "[Coppel Name] (viene de CustomerStores) y no hay columna de ID.\n"
        "DOS AVISOS, los dos medidos:\n"
        "  1. La vista es un INNER JOIN con CustomerStores, y se queda con la "
        "mitad: en W35 de 2026, de las 8 326 tiendas de HCReport solo 4 529 "
        "estan en el catalogo. Sirve para cortes por tienda o por region; "
        "para un TOTAL de promotores del pais va [HCReport], o avisa que el "
        "numero es solo de las tiendas catalogadas.\n"
        "  2. El mapa de semana->mes esta escrito A MANO dentro de la vista y "
        "SOLO cubre 2025 y 2026. De 2027 en adelante [Period] sale NULL: si "
        "te piden un mes de 2027, no lo inventes calculando la semana tu "
        "mismo -- dile al usuario que hay que actualizar la vista."
    ),
    "CustomerStores": (
        "El catalogo de tiendas: [ID] (APSMX222602), [Coppel Name] (el nombre "
        "con el que aparecen en casi todo), [HONOR NAME], [Region] (R1 a R9 "
        "y RM), "
        "[StoreLevel] y [RetailName]. Es el puente entre la promotoria (que "
        "va por [ID]) y las ventas (que van por nombre de tienda).\n"
        "Cruzar ventas con este catalogo por nombre funciona en unos "
        "retailers y en otros no. Medido con las tiendas de 2026 contra "
        "[Coppel Name]: AT&T 1732/1732, Coppel 1792/1800, Liverpool 193/204, "
        "Elektra 1253/2307, y Telcel 0/675 y Sears 0/91 -- en esos dos el "
        "nombre de la venta no existe en el catalogo, asi que no hay cruce "
        "por ahi y no hay que forzarlo con LIKE."
    ),
    "Complete_Scrap": (
        "Precios y fichas de e-commerce: lo que cada tienda online publicaba "
        "de cada SKU el dia de la consulta ([consultation_date]). Va de 2025 "
        "a la fecha de hoy y trae ocho retailers -- Coppel, Telcel CACS, "
        "Sears, Liverpool, Elektra, Movistar CACS y dos que NO estan en las "
        "tablas de venta, Suburbia y Mercado Libre.\n"
        "EL MODELO SE AGRUPA POR [approved_name], que es la homologacion de "
        "esta base. [model] es el codigo del sitio del retailer ('BVL-N49', "
        "'CLK-NX3'): agrupar por el parte un mismo telefono en varias filas "
        "y ademas repite codigos entre productos. De 2 286 749 filas, solo 3 "
        "no traen [approved_name]; un mismo [approved_name] junta hasta 4 "
        "codigos distintos y hasta 7 retailers. "
        "[approved_name_without_capacity] es lo mismo sin los GB, para "
        "comparar familias.\n"
        "[approved_name] usa el mismo vocabulario que "
        "UNICOS$.[HOMOLOGACION MODELO], asi que es la llave para cruzar "
        "precio con venta.\n"
        "DOS TRAMPAS medidas:\n"
        "  1. [price] es la unica columna de precio que es numero "
        "([lauch_price], [original_price] y [discount_price] son texto), y "
        "trae CEROS. Filtra `[price] > 0` o el promedio sale hundido.\n"
        "  2. Hay varias filas del mismo modelo, tienda y fecha. Para 'el "
        "precio de hoy' toma el MAX([consultation_date]) y agrega; contar "
        "filas no cuenta productos.\n"
        "[store] no siempre es una tienda fisica: en los CACs es el nombre "
        "del retailer. Hay una vista por retailer (Complete_Scrap_COPPEL, "
        "Complete_Scrap_LIVERPOOL) con las mismas columnas."
    ),
    "TBL_DB_FOR_DOS": (
        "Su columna [Retail] solo contiene 'Coppel', 'Liverpool Offline' y "
        "'TELCEL CAC'. No incluye Elektra ni el resto de cadenas: para esas hay "
        "que ir a RAW_DATA_B2B."
    ),
}


@lru_cache(maxsize=1)
def cargar_catalogo() -> dict:
    return json.loads(RUTA_CATALOGO.read_text(encoding="utf-8"))


def _corchetes(nombre: str) -> str:
    """Nombre delimitado, que es como siempre hay que escribirlo en el SQL.

    Los objetos reales de este servidor llevan espacios (`GTM BI Database`,
    `Store Name`), sufijos `$` heredados de las hojas de Excel importadas
    (`UNICOS$`) y palabras reservadas de T-SQL como nombre de columna (`KEY`,
    `DESC`): sin corchetes ninguno de esos parsea, y `KEY` ademas lo rechaza
    la lista blanca de `agent._evaluar_sql` por reservada.
    """
    return "[" + nombre.replace("]", "]]") + "]"


def _filas_humanas(filas: int | None) -> str:
    if filas is None:
        return ""
    if filas >= 1_000_000:
        return f"{filas / 1_000_000:.1f}M filas"
    if filas >= 1_000:
        return f"{filas // 1_000}k filas"
    return f"{filas} filas"


def _resumen(obj: dict) -> str:
    partes = [p for p in (_filas_humanas(obj["filas"]), f"{len(obj['columnas'])} col") if p]
    return f"{obj['nombre']} ({', '.join(partes)})"


@lru_cache(maxsize=1)
def indice() -> str:
    """Indice compacto de todo el servidor, para inyectar en el system prompt.

    Van los nombres y el volumen, no las columnas: las 893 columnas del
    catalogo completo son demasiado para repetirlas en cada mensaje, y para
    eso esta la herramienta `describir_objeto`.
    """
    lineas = ["COMO LEER ESTE SERVIDOR:"]
    lineas += [f"- {nota}" for nota in NOTAS_GENERALES]

    for base in cargar_catalogo()["bases"]:
        tablas = [o for o in base["objetos"] if o["tipo"] == "tabla"]
        vistas = [o for o in base["objetos"] if o["tipo"] == "vista"]
        lineas.append(
            f"\n### {_corchetes(base['nombre'])} "
            f"- {len(tablas)} tablas, {len(vistas)} vistas"
        )
        lineas.append(base["descripcion"])
        if tablas:
            lineas.append("  Tablas: " + " | ".join(_resumen(o) for o in tablas))
        if vistas:
            lineas.append("  Vistas: " + " | ".join(_resumen(o) for o in vistas))
    return "\n".join(lineas)


def buscar(nombre: str) -> list[tuple[str, dict]]:
    """Objetos cuyo nombre coincide, como (base, objeto).

    Acepta tanto `TBL_DB_FOR_DOS` como `GTM BI Database.dbo.TBL_DB_FOR_DOS`:
    se queda con el ultimo segmento y limpia corchetes, porque el modelo tiende
    a pasar el nombre ya calificado que leyo en el indice. La coincidencia es
    exacta salvo mayusculas; buscar por subcadena haria que `Coppel` arrastre
    las nueve tablas que lo contienen.
    """
    limpio = nombre.strip().split(".")[-1].strip().strip("[]").casefold()
    return [
        (base["nombre"], obj)
        for base in cargar_catalogo()["bases"]
        for obj in base["objetos"]
        if obj["nombre"].casefold() == limpio
    ]


def _ficha(base: str, obj: dict) -> str:
    cabecera = (
        f"{obj['tipo'].upper()} {_corchetes(base)}.dbo.{_corchetes(obj['nombre'])}"
    )
    if obj["filas"] is not None:
        cabecera += f" -- {obj['filas']:,} filas".replace(",", " ")
    columnas = "\n".join(
        f"  {_corchetes(c['nombre'])} {c['tipo']}" for c in obj["columnas"]
    )
    ficha = f"{cabecera}\n{columnas}"
    nota = NOTAS_POR_OBJETO.get(obj["nombre"])
    if nota:
        # Despues de las columnas, no antes: es la ultima linea que el modelo
        # lee antes de escribir el SELECT.
        ficha += f"\nNOTA: {nota}"
    return ficha


@tool
def describir_objeto(nombre: str) -> str:
    """Devuelve las columnas y tipos de una tabla o vista del servidor.

    Usa esta herramienta ANTES de escribir un SELECT, para confirmar los
    nombres exactos de las columnas. Recibe el nombre de la tabla o vista tal
    como aparece en el catalogo, por ejemplo "TBL_DB_FOR_DOS".
    """
    encontrados = buscar(nombre)
    if not encontrados:
        return (
            f"No hay ninguna tabla o vista llamada {nombre!r} en el catalogo. "
            "Revisa el indice de bases de datos del prompt para ver los nombres "
            "disponibles."
        )
    return "\n\n".join(_ficha(base, obj) for base, obj in encontrados)
