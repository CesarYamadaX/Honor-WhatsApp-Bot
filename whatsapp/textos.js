// Textos que escribe el sidecar por su cuenta, sin pasar por el modelo: el
// aviso de espera y los errores de transporte. El idioma lo decide el
// servicio Python (ver sql_agent/idioma.py) y viaja en la respuesta de
// /authorized, que ya se pedia para cada mensaje. La deteccion NO se repite
// aqui: dos implementaciones de lo mismo se separan a la primera correccion
// que solo se aplica a una.

export const IDIOMA_POR_DEFECTO = "es";

const TEXTOS = {
  // No dice "se esta tardando": eso suena a que algo salio mal, y lo normal
  // aqui es que tarde -- las vistas de venta se miden en minutos. Dice que
  // esta trabajando, que es lo que esta pasando, y como cancelar.
  espera: {
    es:
      "Estoy trabajando en lo que me pediste, esto puede tardar un poco. " +
      "Si quieres, mandame otra pregunta y cancelo esta.",
    en:
      "I'm working on what you asked for; this may take a little while. " +
      "If you want, send me another question and I'll cancel this one.",
    zh: "我正在处理你的请求，这可能需要一点时间。如果需要，你可以再发一个问题，我会取消这一个。",
  },
  ocupado: {
    es: "Sigo trabajando en tu pregunta anterior. Dame un momento.",
    en: "I'm still working on your previous question. Give me a moment.",
    zh: "我还在处理你上一个问题，请稍等。",
  },
  demasiado_lenta: {
    es: "La consulta tardo demasiado y la cancele. Intenta acotarla, por ejemplo limitando el rango de fechas.",
    en: "The query took too long and I cancelled it. Try narrowing it down, for example by limiting the date range.",
    zh: "查询用时过长，我已取消。请缩小范围，例如限定日期区间。",
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
