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

// Timeout corto: es una comprobacion en memoria contra la allowlist, sin base
// de datos ni modelo de por medio.
const TIMEOUT_AUTORIZACION_MS = 5000;

/**
 * Pregunta si un chat esta autorizado, sin correr ninguna consulta.
 *
 * Se llama ANTES de atender el mensaje para poder mostrar "escribiendo" y el
 * aviso de espera solo a quien corresponde. Ante la duda devuelve false: si el
 * servicio no contesta, es preferible quedarse callado que arriesgarse a
 * darle señales de vida a un numero ajeno.
 *
 * @returns {Promise<boolean>}
 */
export async function autorizado(baseUrl, chatId, timeoutMs = TIMEOUT_AUTORIZACION_MS) {
  const controlador = new AbortController();
  const temporizador = setTimeout(() => controlador.abort(), timeoutMs);
  try {
    const respuesta = await fetch(`${baseUrl}/authorized`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId }),
      signal: controlador.signal,
    });
    return respuesta.status === 200;
  } catch (error) {
    console.error(`[agentClient] no pude verificar autorizacion: ${error.message}`);
    return false;
  } finally {
    clearTimeout(temporizador);
  }
}

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

  // 409: a esta pregunta la cancelo un mensaje posterior del mismo chat, y es
  // ese el que va a contestar. Silencio: mandar ademas un aviso por la vieja
  // seria ruido justo cuando el usuario ya cambio de tema.
  if (respuesta.status === 409) return null;

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
