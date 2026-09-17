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
        "mes usa el rango de [Day], que si es datetime."
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
