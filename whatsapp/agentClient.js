// Traduce el contrato HTTP del servicio Python a algo que el usuario pueda
// leer en un chat. Devolver null significa "no contestes nada".

const MENSAJES = {
  429: "Sigo trabajando en tu pregunta anterior. Dame un momento.",
  504: "La consulta tardo demasiado y la cancele. Intenta acotarla, por ejemplo limitando el rango de fechas.",
  caido: "El sistema no esta disponible en este momento. Avisale al administrador.",
  desconocido: "No pude procesar tu pregunta. Intenta de nuevo en un momento.",
};

// Red de seguridad para cuando el servicio nunca contesta (proceso Python
// muerto a media peticion, socket a medio abrir, etc). Deliberadamente por
// encima de los 90s de timeout del lado Python: en el caso normal el 504
// del servicio gana primero y da un mensaje mas especifico. Este timeout
// solo cubre el caso en que ni siquiera eso llega.
const TIMEOUT_MS_DEFAULT = 120_000;

/**
 * Le pregunta al agente SQL.
 * @param {string} baseUrl
 * @param {string} chatId
 * @param {string} texto
 * @param {number} [timeoutMs] - por defecto 120s (ver TIMEOUT_MS_DEFAULT).
 *   Parametro pensado para pruebas; la Tarea 9 no necesita pasarlo.
 * @returns {Promise<null | {reply: string, files: string[]}>}
 */
export async function preguntar(baseUrl, chatId, texto, timeoutMs = TIMEOUT_MS_DEFAULT) {
  const controlador = new AbortController();
  const temporizador = setTimeout(() => controlador.abort(), timeoutMs);

  let respuesta;
  try {
    respuesta = await fetch(`${baseUrl}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text: texto }),
      signal: controlador.signal,
    });
  } catch (error) {
    if (error.name === "AbortError") {
      console.error(`[agentClient] el servicio nunca contesto (timeout de ${timeoutMs}ms)`);
    } else {
      console.error(`[agentClient] sin conexion con el servicio: ${error.message}`);
    }
    return { reply: MENSAJES.caido, files: [] };
  } finally {
    clearTimeout(temporizador);
  }

  // El servicio ya decidio que este chat no existe para nosotros.
  if (respuesta.status === 403) return null;

  if (respuesta.status === 429) return { reply: MENSAJES[429], files: [] };
  if (respuesta.status === 504) return { reply: MENSAJES[504], files: [] };

  if (!respuesta.ok) {
    console.error(`[agentClient] respuesta inesperada: ${respuesta.status}`);
    return { reply: MENSAJES.desconocido, files: [] };
  }

  let cuerpo;
  try {
    cuerpo = await respuesta.json();
  } catch (error) {
    console.error(`[agentClient] cuerpo de respuesta invalido: ${error.message}`);
    return { reply: MENSAJES.desconocido, files: [] };
  }

  if (typeof cuerpo !== "object" || cuerpo === null) {
    console.error("[agentClient] cuerpo de respuesta no es un objeto");
    return { reply: MENSAJES.desconocido, files: [] };
  }

  const reply = typeof cuerpo.reply === "string" ? cuerpo.reply : MENSAJES.desconocido;
  const files = Array.isArray(cuerpo.files) ? cuerpo.files : [];

  return { reply, files };
}
