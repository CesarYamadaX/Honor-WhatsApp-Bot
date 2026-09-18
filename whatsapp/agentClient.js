// Traduce el contrato HTTP del servicio Python a algo que el usuario pueda
// leer en un chat. Devolver null significa "no contestes nada".

import { IDIOMA_POR_DEFECTO, texto as traducir } from "./textos.js";

const MENSAJES = {
  429: "ocupado",
  504: "demasiado_lenta",
  caido: "servicio_caido",
  desconocido: "desconocido",
};

// Red de seguridad para cuando el servicio nunca contesta (proceso Python
// muerto a media peticion, socket a medio abrir, etc). Deliberadamente por
// encima del peor caso del lado Python: hasta CANCEL_WAIT_SECONDS (25s)
// esperando a que la consulta anterior se de por cancelada, MAS los
// AGENT_TIMEOUT_SECONDS (300s) de la consulta nueva. Si este numero fuera
// menor que esa suma, el sidecar abandonaria una peticion que todavia iba a
// contestar y le diria al usuario que el sistema no esta disponible, que es
// falso. En el caso normal el 504 del servicio gana primero y da un mensaje
// mas especifico; esto solo cubre que ni siquiera eso llegue.
const TIMEOUT_MS_DEFAULT = 400_000;

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
 * En que idioma le esta hablando este chat al bot.
 *
 * El aviso de espera sale ANTES de que la respuesta exista, asi que su idioma
 * no puede venir en ella. Se pregunta al servicio, que ya dedujo el idioma de
 * la pregunta que esta corriendo. No se manda el texto del mensaje: la
 * deteccion es cosa del lado Python (una sola copia de la heuristica) y a
 * /authorized, que se contesta antes de mirar la allowlist, no debe cruzar el
 * mensaje de un numero ajeno.
 *
 * Ante cualquier falla devuelve el idioma por defecto: es un detalle de
 * presentacion, nunca vale la pena que tumbe la respuesta.
 *
 * @returns {Promise<string>}
 */
export async function idiomaDelChat(baseUrl, chatId, timeoutMs = TIMEOUT_AUTORIZACION_MS) {
  const controlador = new AbortController();
  const temporizador = setTimeout(() => controlador.abort(), timeoutMs);
  try {
    const respuesta = await fetch(`${baseUrl}/idioma`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId }),
      signal: controlador.signal,
    });
    if (respuesta.status !== 200) return IDIOMA_POR_DEFECTO;
    const cuerpo = await respuesta.json();
    return typeof cuerpo?.idioma === "string" ? cuerpo.idioma : IDIOMA_POR_DEFECTO;
  } catch (error) {
    console.error(`[agentClient] no pude saber el idioma del chat: ${error.message}`);
    return IDIOMA_POR_DEFECTO;
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
 * @returns {Promise<null | {reply: string, files: string[], idioma: string}>}
 */
export async function preguntar(baseUrl, chatId, texto, timeoutMs = TIMEOUT_MS_DEFAULT) {
  // Los mensajes de abajo los escribe el sidecar, no el modelo, asi que hay
  // que traducirlos. El idioma se pregunta solo cuando hace falta -- es decir,
  // casi nunca: son los caminos de error. Si el servicio no contesta, cae al
  // idioma por defecto, que es justo lo unico razonable ahi.
  const aviso = async (clave) => {
    const idioma = await idiomaDelChat(baseUrl, chatId);
    return { reply: traducir(clave, idioma), files: [], idioma };
  };
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
    return aviso(MENSAJES.caido);
  } finally {
    clearTimeout(temporizador);
  }

  // El servicio ya decidio que este chat no existe para nosotros.
  if (respuesta.status === 403) return null;

  // 409: a esta pregunta la cancelo un mensaje posterior del mismo chat, y es
  // ese el que va a contestar. Silencio: mandar ademas un aviso por la vieja
  // seria ruido justo cuando el usuario ya cambio de tema.
  if (respuesta.status === 409) return null;

  if (respuesta.status === 429) return aviso(MENSAJES[429]);
  if (respuesta.status === 504) return aviso(MENSAJES[504]);

  if (!respuesta.ok) {
    console.error(`[agentClient] respuesta inesperada: ${respuesta.status}`);
    return aviso(MENSAJES.desconocido);
  }

  let cuerpo;
  try {
    cuerpo = await respuesta.json();
  } catch (error) {
    console.error(`[agentClient] cuerpo de respuesta invalido: ${error.message}`);
    return aviso(MENSAJES.desconocido);
  }

  if (typeof cuerpo !== "object" || cuerpo === null) {
    console.error("[agentClient] cuerpo de respuesta no es un objeto");
    return aviso(MENSAJES.desconocido);
  }

  const idioma = typeof cuerpo.idioma === "string" ? cuerpo.idioma : IDIOMA_POR_DEFECTO;
  if (typeof cuerpo.reply !== "string") return aviso(MENSAJES.desconocido);
  const files = Array.isArray(cuerpo.files) ? cuerpo.files : [];

  return { reply: cuerpo.reply, files, idioma };
}
