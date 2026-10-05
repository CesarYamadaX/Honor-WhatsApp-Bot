// Traduce el contrato HTTP del servicio Python a algo que el usuario pueda
// leer en un chat. Devolver null significa "no contestes nada".

import http from "node:http";
import https from "node:https";

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
// AGENT_TIMEOUT_SECONDS (600s) de la consulta nueva. Si este numero fuera
// menor que esa suma, el sidecar abandonaria una peticion que todavia iba a
// contestar y le diria al usuario que el sistema no esta disponible, que es
// falso. En el caso normal el 504 del servicio gana primero y da un mensaje
// mas especifico; esto solo cubre que ni siquiera eso llegue.
const TIMEOUT_MS_DEFAULT = 900_000;

/**
 * POST con cuerpo JSON por node:http, SIN el tope de cabeceras de fetch.
 *
 * El fetch de Node (undici) abandona cualquier peticion cuyas cabeceras no
 * lleguen en 300s (headersTimeout), por encima de cualquier AbortController:
 * medido, falla a los ~303s con UND_ERR_HEADERS_TIMEOUT. /ask no manda nada
 * hasta tener la respuesta entera, y un reporte de toda la historia de
 * Coppel tarda ~440s, asi que con fetch el usuario recibia "el sistema no
 * esta disponible" a los cinco minutos y el reporte se perdia. Aqui el unico
 * limite es la `signal` de quien llama.
 *
 * Devuelve lo poco de Response que usa `preguntar`: status, ok y json().
 */
function postSinTopeDeCabeceras(url, cuerpo, signal) {
  return new Promise((resolve, reject) => {
    const destino = new URL(url);
    const cliente = destino.protocol === "https:" ? https : http;
    const datos = JSON.stringify(cuerpo);
    const peticion = cliente.request(
      destino,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(datos),
        },
        signal,
      },
      (respuesta) => {
        const trozos = [];
        respuesta.on("data", (trozo) => trozos.push(trozo));
        respuesta.on("error", reject);
        respuesta.on("end", () => {
          const texto = Buffer.concat(trozos).toString("utf-8");
          const status = respuesta.statusCode;
          resolve({
            status,
            ok: status >= 200 && status < 300,
            json: async () => JSON.parse(texto),
          });
        });
      },
    );
    peticion.on("error", reject);
    peticion.end(datos);
  });
}

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
 * @param {number} [timeoutMs] - por defecto 900s (ver TIMEOUT_MS_DEFAULT).
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
    respuesta = await postSinTopeDeCabeceras(
      `${baseUrl}/ask`,
      { chat_id: chatId, text: texto },
      controlador.signal,
    );
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

// Una nota de voz de un minuto tarda unos segundos en transcribirse; esto es
// solo la red de seguridad si el servicio nunca contesta.
const TIMEOUT_TRANSCRIPCION_MS = 90_000;

// Que decirle al usuario segun como fallo /transcribir.
const FALLAS_DE_AUDIO = {
  413: "audio_largo",
  422: "audio_no_entendido",
};

/**
 * Transcribe una nota de voz en el servicio Python.
 *
 * @param {string} baseUrl
 * @param {string} chatId
 * @param {Buffer} audio
 * @param {string} formato - "ogg", "mp3"... (ver formatoDelAudio en notasDeVoz.js)
 * @returns {Promise<null | {texto: string, idioma: string} | {falla: string}>}
 *   null si el chat no esta autorizado (silencio); `falla` es la clave del
 *   texto que hay que mandarle al usuario.
 */
export async function transcribir(baseUrl, chatId, audio, formato, timeoutMs = TIMEOUT_TRANSCRIPCION_MS) {
  const controlador = new AbortController();
  const temporizador = setTimeout(() => controlador.abort(), timeoutMs);
  let respuesta;
  try {
    respuesta = await fetch(`${baseUrl}/transcribir`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, audio: audio.toString("base64"), formato }),
      signal: controlador.signal,
    });
  } catch (error) {
    console.error(`[agentClient] no pude transcribir: ${error.message}`);
    return { falla: "audio_fallo" };
  } finally {
    clearTimeout(temporizador);
  }
  if (respuesta.status === 403) return null;
  if (!respuesta.ok) {
    console.error(`[agentClient] /transcribir contesto ${respuesta.status}`);
    return { falla: FALLAS_DE_AUDIO[respuesta.status] ?? "audio_fallo" };
  }
  try {
    const cuerpo = await respuesta.json();
    if (typeof cuerpo?.texto !== "string" || !cuerpo.texto.trim()) {
      return { falla: "audio_no_entendido" };
    }
    return { texto: cuerpo.texto.trim(), idioma: cuerpo.idioma ?? IDIOMA_POR_DEFECTO };
  } catch (error) {
    console.error(`[agentClient] respuesta ilegible de /transcribir: ${error.message}`);
    return { falla: "audio_fallo" };
  }
}
