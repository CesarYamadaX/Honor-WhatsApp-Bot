// Textos que escribe el sidecar por su cuenta, sin pasar por el modelo: el
// aviso de espera y los errores de transporte. El idioma lo decide el
// servicio Python (ver sql_agent/idioma.py) y viaja en la respuesta de
// /authorized, que ya se pedia para cada mensaje. La deteccion NO se repite
// aqui: dos implementaciones de lo mismo se separan a la primera correccion
// que solo se aplica a una.

export const IDIOMA_POR_DEFECTO = "es";

const TEXTOS = {
  // No dice "se esta tardando": eso suena a que algo salio mal, y lo normal
  // aqui es que tarde -- las vistas de venta se miden en minutos. Dice lo
  // que esta pasando y nada mas.
  //
  // Tampoco dice que mandar otra pregunta cancela esta, aunque sigue siendo
  // cierto: leerlo mientras se espera invita a reescribir la pregunta, que
  // es justo lo que tira el trabajo que ya lleva hecho.
  espera: {
    es: "Estoy trabajando en lo que me pediste, esto puede tardar un poco.",
    en: "I'm working on what you asked for; this may take a little while.",
    zh: "我正在处理你的请求，这可能需要一点时间。",
  },
  // El segundo aviso, para las de verdad largas. El primero dice "un poco" y
  // a los dos minutos eso ya se quedo corto: un reporte de un anio de Coppel
  // tarda ~290s y uno de toda la historia ~440s (medido). Sin esta segunda
  // señal quedan cinco o siete minutos de silencio, y ahi es donde en los
  // logs se ve al usuario reenviando la pregunta -- que es lo peor que puede
  // hacer, porque el mensaje nuevo cancela el trabajo ya hecho y la espera
  // vuelve a empezar. Por eso pide paciencia Y dice que no hace falta
  // repetirla, sin explicar el mecanismo de cancelacion: saber que se puede
  // cancelar invita a reescribir la pregunta.
  espera_larga: {
    es:
      "Lo que pediste es grande y lo estoy armando; puede tardar unos " +
      "minutos, tenme un poco de paciencia. No hace falta que repitas la " +
      "pregunta: yo te aviso cuando acabe y te mando el resultado.",
    en:
      "What you asked for is a big one and I'm putting it together; it can " +
      "take a few minutes, so please bear with me. No need to resend your " +
      "question: I'll let you know when it's done and send you the result.",
    zh:
      "你要的内容比较大，我正在生成，可能需要几分钟。" +
      "不用重复发送问题：做好后我会告诉你并把结果发给你。",
  },
  // Lo que se prometio en espera_larga: avisar al acabar (19/10/2026).
  listo: {
    es: "Listo, ya terminé lo que me pediste:",
    en: "Done, here's what you asked for:",
    zh: "好了，这是你要的结果：",
  },
  // Un mensaje que llega a los pocos segundos de otro, con el bot aun
  // trabajando, se suma a lo que pidio (ver seguimiento.js).
  se_suma: {
    es: "Va, lo sumo a lo que me pediste y te contesto todo junto.",
    en: "Got it, I'll add that to what you asked and answer it all together.",
    zh: "好的，我把这条加到你刚才的请求里，一起回复你。",
  },
  // Y uno que llega despues, con el bot aun trabajando: espera su turno.
  en_cola: {
    es: "Sigo con lo que me pediste antes para no perder lo que llevo; en cuanto te lo mande, sigo con esto.",
    en: "I'm still on your previous request so I don't lose the progress; as soon as I send it, I'll get to this.",
    zh: "我还在处理你上一个请求，以免前功尽弃；发给你之后马上处理这一条。",
  },
  ocupado: {
    es: "Sigo trabajando en tu pregunta anterior. Dame un momento.",
    en: "I'm still working on your previous question. Give me a moment.",
    zh: "我还在处理你上一个问题，请稍等。",
  },
  // No dice "limita el rango de fechas": esta medido que NO ayuda -- contra
  // la vista consolidada, un SUM de un solo mes tarda lo mismo que el de un
  // anio. Lo que si ayuda es preguntar por UNA cadena.
  demasiado_lenta: {
    es: "La consulta tardo demasiado y la cancele. Intenta preguntar por una sola cadena (Coppel, Elektra, Telcel...).",
    en: "The query took too long and I cancelled it. Try asking about a single retailer (Coppel, Elektra, Telcel...).",
    zh: "查询用时过长，我已取消。请只针对一个渠道提问（Coppel、Elektra、Telcel…）。",
  },
  servicio_caido: {
    es: "El sistema no esta disponible en este momento. Avisale al administrador.",
    en: "The system is unavailable right now. Please let the administrator know.",
    zh: "系统当前不可用。请通知管理员。",
  },
  desconocido: {
    es: "No pude procesar tu pregunta. Intenta de nuevo en un momento.",
    en: "I couldn't process your question. Try again in a moment.",
    zh: "我无法处理你的问题，请稍后再试。",
  },
  sin_respuesta: {
    es: "No tengo una respuesta para eso. Intenta reformular tu pregunta.",
    en: "I don't have an answer for that. Try rephrasing your question.",
    zh: "我没有找到答案。请换一种问法。",
  },
  archivo_ilegible: {
    es: "Genere el archivo pero no lo pude leer para enviartelo.",
    en: "I generated the file but couldn't read it to send it to you.",
    zh: "我生成了文件，但无法读取并发送给你。",
  },
  // Notas de voz. Lo que se entendio se le muestra ANTES de correr la
  // consulta: si el audio se entendio mal, lo ve en seguida y no despues de
  // esperar un reporte de otra cosa.
  audio_entendido: {
    es: "🎤 Entendí:",
    en: "🎤 I heard:",
    zh: "🎤 我听到的是：",
  },
  audio_no_entendido: {
    es: "No alcancé a entender el audio. ¿Me lo repites o me lo escribes?",
    en: "I couldn't make out the audio. Could you say it again or type it?",
    zh: "我没听清这段语音。可以再说一遍或者打字发给我吗？",
  },
  audio_largo: {
    es: "El audio es muy largo para mí. Mándame uno más corto o escríbeme la pregunta.",
    en: "That audio is too long for me. Send a shorter one or type your question.",
    zh: "这段语音太长了。请发一段更短的，或者直接打字提问。",
  },
  audio_fallo: {
    es: "No pude procesar el audio en este momento. Escríbeme la pregunta, por favor.",
    en: "I couldn't process the audio right now. Please type your question.",
    zh: "我暂时无法处理这段语音，请打字提问。",
  },
};

// Lleva el tamano maximo, asi que se arma en vez de buscarse.
const ARCHIVO_MUY_GRANDE = {
  es: (mb) => `El reporte pesa mas de ${mb} MB y no lo puedo mandar por aqui. Acota la consulta.`,
  en: (mb) => `The report is larger than ${mb} MB and I can't send it here. Narrow down the query.`,
  zh: (mb) => `报告超过 ${mb} MB，无法通过这里发送。请缩小查询范围。`,
};

/** Texto fijo en el idioma pedido, con el espanol como respaldo. */
export function texto(clave, idioma) {
  const traducciones = TEXTOS[clave];
  if (!traducciones) throw new Error(`texto desconocido: ${clave}`);
  return traducciones[idioma] ?? traducciones[IDIOMA_POR_DEFECTO];
}

/** Aviso de archivo demasiado grande, con el limite en MB. */
export function textoArchivoMuyGrande(mb, idioma) {
  const armar = ARCHIVO_MUY_GRANDE[idioma] ?? ARCHIVO_MUY_GRANDE[IDIOMA_POR_DEFECTO];
  return armar(mb);
}
