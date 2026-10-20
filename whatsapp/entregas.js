// Los reportes grandes que el servicio armo aparte (pedido el 19/10/2026: sin
// limite de tiempo para los archivos grandes). La herramienta ya le dijo al
// usuario "tardara unos N minutos, te lo mando aqui"; cuando acaba, queda una
// entrega pendiente en el servicio, y aqui se recoge y se manda al chat.
//
// El servicio no le puede escribir al sidecar: el sidecar pregunta cada pocos
// segundos. Una entrega se confirma al terminar de mandarla; si algo falla a
// la mitad, el servicio la vuelve a dar a los dos minutos.
import { confirmarEntrega, entregasPendientes, registrarEnviados } from "./agentClient.js";

/**
 * Manda lo que este listo. `jidDe(chatId)` da el chat al que se responde (el
 * telefono puede venir de un chat "@lid"); `enviarTexto(jid, texto)` y
 * `enviarArchivo(jid, ruta, idioma)` devuelven los ids de lo mandado.
 * Devuelve cuantas entregas se mandaron.
 */
export async function entregarPendientes(baseUrl, { jidDe, enviarTexto, enviarArchivo }) {
  const pendientes = await entregasPendientes(baseUrl);
  let mandadas = 0;
  for (const entrega of pendientes) {
    const jid = jidDe(entrega.chat_id) ?? entrega.chat_id;
    try {
      const ids = [];
      const partes = {};
      if (entrega.texto?.trim()) ids.push(...((await enviarTexto(jid, entrega.texto)) ?? []));
      for (const ruta of entrega.archivos ?? []) {
        for (const id of (await enviarArchivo(jid, ruta, entrega.idioma)) ?? []) {
          ids.push(id);
          partes[id] = ruta;
        }
      }
      // Con el turno de la pregunta: un 👍 o 👎 al reporte califica esa pregunta.
      if (entrega.turno && ids.length) {
        await registrarEnviados(baseUrl, entrega.chat_id, entrega.turno, ids, partes);
      }
      await confirmarEntrega(baseUrl, entrega.id);
      mandadas += 1;
      console.log(`[whatsapp] reporte armado aparte entregado a ${jid} (${(entrega.archivos ?? []).length} archivos)`);
    } catch (error) {
      console.error(`[whatsapp] no pude entregar un reporte a ${jid}: ${error.message}`);
    }
  }
  return mandadas;
}
