// Las fotos de perfil de WhatsApp de los usuarios, para el panel (pedido el
// 13/10/2026: que se vea la foto de cada quien y no un circulo con numeros).
//
// El panel no habla con WhatsApp; el sidecar si. Pregunta al servicio a quien
// le toca (no revisados en 24 h), trae cada foto y se la manda. Si WhatsApp
// dice que no hay foto -- no tiene, o solo la ven sus contactos -- se manda
// sin imagen y el servicio borra la que hubiera. Una falla de red no manda
// nada: se reintenta en la siguiente vuelta.
import { enviarFotoDelBot, enviarFotoDePerfil, fotosPendientes } from "./agentClient.js";

const PAUSA_ENTRE_FOTOS_MS = 1500; // no pedirle a WhatsApp decenas de golpe

const dormir = (ms) => new Promise((listo) => setTimeout(listo, ms));

// Baileys avisa con un Boom: 404 (no tiene foto) o 401 (privada).
export function esSinFoto(error) {
  const codigo = error?.output?.statusCode ?? error?.data?.status;
  return codigo === 404 || codigo === 401 || /item-not-found|not-authorized/i.test(String(error?.message));
}

export async function traerFoto(sock, numero, descargar = fetch) {
  let url;
  try {
    url = await sock.profilePictureUrl(`${numero}@s.whatsapp.net`, "image");
  } catch (error) {
    if (esSinFoto(error)) return { numero, imagen: null };
    throw error;
  }
  if (!url) return { numero, imagen: null };
  const respuesta = await descargar(url);
  if (!respuesta.ok) throw new Error(`la descarga de la foto contesto ${respuesta.status}`);
  return { numero, imagen: Buffer.from(await respuesta.arrayBuffer()).toString("base64") };
}

// El numero del propio bot: "5215512345678:12@s.whatsapp.net" -> "5215512345678".
export function numeroDelBot(sock) {
  const id = sock?.user?.id ?? "";
  const numero = id.split("@")[0].split(":")[0];
  return /^\d{6,20}$/.test(numero) ? numero : null;
}

export async function actualizarFotos(sock, baseUrl, { pausaMs = PAUSA_ENTRE_FOTOS_MS, descargar = fetch } = {}) {
  // Primero la del bot, para el circulo de la marca del panel.
  const propio = numeroDelBot(sock);
  if (propio) {
    try {
      const { imagen } = await traerFoto(sock, propio, descargar);
      await enviarFotoDelBot(baseUrl, propio, imagen);
    } catch (error) {
      console.error(`[whatsapp] no pude traer la foto del bot: ${error.message}`);
    }
  }
  const numeros = await fotosPendientes(baseUrl);
  let revisadas = 0;
  for (const numero of numeros) {
    try {
      const { imagen } = await traerFoto(sock, numero, descargar);
      await enviarFotoDePerfil(baseUrl, numero, imagen);
      revisadas += 1;
    } catch (error) {
      console.error(`[whatsapp] no pude traer la foto de ${numero}: ${error.message}`);
    }
    if (pausaMs) await dormir(pausaMs);
  }
  if (numeros.length) console.log(`[whatsapp] fotos de perfil revisadas: ${revisadas} de ${numeros.length}`);
  return revisadas;
}
