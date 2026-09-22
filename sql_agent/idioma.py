"""Deteccion del idioma del usuario y textos fijos del bot.

El bot se usa en tres idiomas: espanol, ingles y chino. La deteccion vive
aqui y no en el prompt porque un mensaje de WhatsApp es corto ("y de
Elektra?", "top 10") y el modelo, con la memoria conversacional encima,
tiende a quedarse en el idioma del turno anterior. Detectar aparte deja
elegir explicitamente y, cuando el mensaje no alcanza para decidir,
sostener el idioma que ya traia la conversacion en lugar de adivinar.
"""

import re
import unicodedata

ESPANOL = "es"
INGLES = "en"
CHINO = "zh"
POR_DEFECTO = ESPANOL

# Han unificado (chino simplificado y tradicional). Basta un caracter: nadie
# escribe uno por accidente, y un "Magic 7" incrustado en una pregunta en
# chino no debe hacerla pasar por inglesa.
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

_PALABRAS = re.compile(r"[a-z]+")

# Palabras funcionales que solo existen en uno de los dos idiomas. Las que se
# escriben igual en ambos ("no", "total", "a", "me", "excel") no estan: suman
# a los dos lados y solo agregan ruido.
_MARCAS_ESPANOL = frozenset("""
    que cual cuales cuanto cuantos cuanta cuantas como donde cuando quien
    dame damelo muestra muestrame dime necesito quiero puedes podrias
    porfavor favor gracias hola
    ventas venta vendio vendieron vendidas unidades piezas tienda tiendas
    sucursal sucursales precio precios modelo modelos marca marcas
    del los las una unos unas por para con sin sobre entre desde hasta
    mas menos hay fue fueron son tiene tienen tuvo esta este estos estas
    mes meses anio anios dia dias semana semanas pasado pasada ultimo ultima
    proximo proxima grafica archivo reporte resultado
    y o pero tambien ademas ahora todavia
""".split())

_MARCAS_INGLES = frozenset("""
    the what which who how many much when where why
    show give list tell send please thanks hello hi
    sales sold units pieces store stores shop price prices model models brand
    of in on for from with without by between during since until
    is are was were be been have has had do does did can could would should
    and or but also than more less most least about each
    last this that these those next previous
    month months year years day days week weeks yesterday today
    chart graph file report result number count average
""".split())

INSTRUCCION = {
    ESPANOL: (
        "El usuario escribe en espanol: responde TODO en espanol, incluidas "
        "las aclaraciones y las notas al final."
    ),
    INGLES: (
        "The user is writing in English. Write your ENTIRE reply in English, "
        "including any caveats, notes or units. Do not answer in Spanish, "
        "even if earlier turns of this conversation were in Spanish. The "
        "database columns are in Spanish; translate what you mention of them."
    ),
    CHINO: (
        "用户在用中文提问。请用简体中文回答全部内容，包括说明、注意事项和单位。"
        "即使对话前面的轮次是西班牙语，也不要用西班牙语回答。"
        "数据库的列名是西班牙语，提到时请翻译成中文。"
    ),
}

# Mensajes que manda el servicio sin pasar por el modelo. Si estos siguieran
# siempre en espanol, un usuario que pregunta en ingles recibiria la respuesta
# en ingles y el error en espanol.
TEXTOS = {
    "error_interno": {
        ESPANOL: (
            "Tuve un problema al consultar la base de datos. "
            "Intenta de nuevo en un momento, o reformula la pregunta."
        ),
        INGLES: (
            "I ran into a problem querying the database. "
            "Try again in a moment, or rephrase your question."
        ),
        CHINO: "查询数据库时出现了问题。请稍后再试，或者换一种问法。",
    },
    "servicio_caido": {
        ESPANOL: (
            "Ahorita no puedo responder preguntas: el servicio de inteligencia "
            "artificial que uso esta fuera de linea. No se arregla reformulando "
            "la pregunta ni esperando un rato. Comunicate con "
            "lauragabrielacastellanos o con alguien del equipo de GTM Mexico "
            "para que lo revisen."
        ),
        INGLES: (
            "I can't answer questions right now: the AI service I rely on is "
            "offline. Rephrasing the question or waiting won't help. Please "
            "contact lauragabrielacastellanos or someone on the GTM Mexico "
            "team so they can look into it."
        ),
        CHINO: (
            "我现在无法回答问题：我使用的人工智能服务已离线。"
            "换一种问法或者等一会儿都没有用。"
            "请联系 lauragabrielacastellanos 或 GTM Mexico 团队的同事来处理。"
        ),
    },
    "cancelado": {
        ESPANOL: "Listo, cancele la consulta anterior.",
        INGLES: "Done, I cancelled the previous query.",
        CHINO: "好的，我已经取消了上一个查询。",
    },
    "nada_que_cancelar": {
        ESPANOL: "No tenias ninguna consulta en curso.",
        INGLES: "You had no query running.",
        CHINO: "你没有正在进行的查询。",
    },
}


# Las piezas de la ficha que acompana a un pronostico. Van aparte de TEXTOS
# porque llevan numeros dentro. El pronostico NO puede salir sin ella: un
# numero del futuro sin decir de que sale invita a leerlo como un dato
# cerrado, y quien lo recibe no tiene forma de juzgarlo.
#
# Lo que NO va aqui es el nombre de la tabla: la regla 6 del prompt prohibe
# ensenarle al usuario nombres del esquema, y "32 meses de enero 2024 a
# agosto 2026" dice lo mismo sin filtrarlos.
_BASE_DEL_PRONOSTICO = {
    ESPANOL: {
        "titulo": "En que me base:",
        "historia": "- {meses} meses de historia, de {desde} a {hasta}.",
        "estacional": (
            "- Metodo: el mismo mes del ano anterior, ajustado por el "
            "crecimiento de los ultimos 6 meses."
        ),
        "tendencia": (
            "- Metodo: la tendencia de los ultimos {ventana} meses. Ojo: no "
            "hay un ano completo de historia, asi que NO toma en cuenta la "
            "estacionalidad, que en estos datos pesa mucho."
        ),
        "precision": (
            "- Precision: este metodo se equivoca alrededor de {error} en "
            "pruebas contra meses que ya ocurrieron."
        ),
        "descartado": (
            "- Deje fuera {mes} porque ese mes todavia no termina y habria "
            "jalado el pronostico hacia abajo."
        ),
    },
    INGLES: {
        "titulo": "What this is based on:",
        "historia": "- {meses} months of history, from {desde} to {hasta}.",
        "estacional": (
            "- Method: the same month last year, adjusted by the growth of "
            "the last 6 months."
        ),
        "tendencia": (
            "- Method: the trend of the last {ventana} months. Note: there "
            "isn't a full year of history, so it does NOT account for "
            "seasonality, which is strong in this data."
        ),
        "precision": (
            "- Accuracy: this method is off by about {error} when tested "
            "against months that already happened."
        ),
        "descartado": (
            "- I left {mes} out because that month isn't over yet and it "
            "would have dragged the forecast down."
        ),
    },
    CHINO: {
        "titulo": "预测依据：",
        "historia": "- {meses} 个月的历史数据，从 {desde} 到 {hasta}。",
        "estacional": "- 方法：去年同月的数据，按最近 6 个月的增长率调整。",
        "tendencia": (
            "- 方法：最近 {ventana} 个月的趋势。注意：历史数据不足一年，"
            "因此没有考虑季节性，而这些数据的季节性很强。"
        ),
        "precision": "- 准确度：用已经发生的月份检验时，该方法的误差约为 {error}。",
        "descartado": "- {mes} 没有计入，因为该月尚未结束，会把预测拉低。",
    },
}


def base_del_pronostico(
    idioma: str,
    *,
    meses_usados: int,
    desde: tuple[int, int] | None,
    hasta: tuple[int, int] | None,
    estacional: bool,
    ventana: int,
    error: str,
    descartado: tuple[int, int] | None,
) -> str:
    """La ficha de en que se baso un pronostico, en el idioma del usuario."""
    piezas = _BASE_DEL_PRONOSTICO.get(idioma, _BASE_DEL_PRONOSTICO[ESPANOL])
    lineas = [piezas["titulo"]]
    if desde and hasta:
        lineas.append(
            piezas["historia"].format(
                meses=meses_usados,
                desde=f"{desde[0]}-{desde[1]:02d}",
                hasta=f"{hasta[0]}-{hasta[1]:02d}",
            )
        )
    if estacional:
        lineas.append(piezas["estacional"])
        lineas.append(piezas["precision"].format(error=error))
    else:
        lineas.append(piezas["tendencia"].format(ventana=ventana))
    if descartado:
        lineas.append(
            piezas["descartado"].format(mes=f"{descartado[0]}-{descartado[1]:02d}")
        )
    return "\n".join(lineas)


# Lo que se ofrece al final de una respuesta. El texto es la PREGUNTA que el
# usuario tendria que hacer, no una descripcion: asi puede copiarla o decir
# "la primera" y el bot ya sabe seguir, porque la conversacion tiene memoria.
_SUGERENCIAS = {
    ESPANOL: {
        "titulo": "Tambien te puedo decir:",
        "share_honor": "que parte de eso fue HONOR (el share)",
        "por_tienda": "como se reparte por tienda",
        "capa": "el CAPA de esas tiendas, para ver cuanto les falta para su maximo",
        "mismo_periodo_anterior": "como venia el mismo periodo del ano pasado",
        "por_modelo": "que modelos lo movieron",
        "otros_retailers": "como les fue a las otras cadenas",
        "ventas_de_esas_tiendas": "cuanto vendieron esas tiendas, para ver si la promotoria se nota",
    },
    INGLES: {
        "titulo": "I can also tell you:",
        "share_honor": "how much of that was HONOR (the share)",
        "por_tienda": "how it breaks down by store",
        "capa": "the CAPA of those stores, to see how far they are from their peak",
        "mismo_periodo_anterior": "how the same period looked last year",
        "por_modelo": "which models drove it",
        "otros_retailers": "how the other retailers did",
        "ventas_de_esas_tiendas": "how much those stores sold, to see whether the promoter shows up in the numbers",
    },
    CHINO: {
        "titulo": "我还可以告诉你：",
        "share_honor": "其中荣耀（HONOR）占多少（份额）",
        "por_tienda": "按门店的分布情况",
        "capa": "这些门店的 CAPA，看看距离各自的峰值还有多远",
        "mismo_periodo_anterior": "去年同期的情况",
        "por_modelo": "哪些机型带动的",
        "otros_retailers": "其他渠道的表现",
        "ventas_de_esas_tiendas": "这些门店卖了多少，看看促销员有没有体现在数字上",
    },
}


def sugerencias(idioma: str, claves: list) -> str:
    """Lo que se puede preguntar despues, en el idioma del usuario.

    Cadena vacia si no hay nada que ofrecer, para que quien llama no tenga
    que acordarse de comprobarlo.
    """
    piezas = _SUGERENCIAS.get(idioma, _SUGERENCIAS[ESPANOL])
    lineas = [f"- {piezas[c]}" for c in claves if c in piezas]
    if not lineas:
        return ""
    return "\n".join([piezas["titulo"], *lineas])


def sin_acentos(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto) if not unicodedata.combining(c)
    )


def detectar(texto: str) -> str | None:
    """Devuelve 'es', 'en', 'zh', o None si el mensaje no alcanza para decidir.

    None no es un fallo: "top 10 Elektra" o "2026?" no tienen idioma, y quien
    llama debe quedarse con el que ya venia usando la conversacion. Inventar
    uno ahi seria peor que no decidir -- cambiaria de idioma a media charla
    por un mensaje de dos palabras.
    """
    if not texto:
        return None
    if _HAN.search(texto):
        return CHINO

    palabras = _PALABRAS.findall(sin_acentos(texto).casefold())
    espanol = sum(1 for p in palabras if p in _MARCAS_ESPANOL)
    ingles = sum(1 for p in palabras if p in _MARCAS_INGLES)

    # Acentos, enes y signos de apertura: no existen en ingles, asi que pesan
    # mas que una palabra suelta. Con tope, para que "¿cuántas?" no arrastre
    # una pregunta que por lo demas esta en ingles.
    espanol += min(len(re.findall(r"[áéíóúüñ¿¡]", texto.casefold())), 4)

    if espanol > ingles:
        return ESPANOL
    if ingles > espanol:
        return INGLES
    return None


def texto(clave: str, idioma: str) -> str:
    """Mensaje fijo en el idioma pedido, con el espanol como respaldo."""
    traducciones = TEXTOS[clave]
    return traducciones.get(idioma, traducciones[ESPANOL])
