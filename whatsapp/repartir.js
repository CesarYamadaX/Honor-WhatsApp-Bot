// Como se atiende un lote de mensajes que WhatsApp entrega junto.
//
// Antes era `for (const m of mensajes) await atender(m)`: uno tras otro,
// aunque fueran de personas distintas. Casi siempre llega un mensaje por
// lote y no se notaba, pero al reconectarse WhatsApp entrega de golpe lo que
// se junto mientras el bot estaba caido, y ahi la segunda persona esperaba a
// que terminara la consulta entera de la primera -- con una pesada, minutos.
//
// Ahora cada chat va por su lado, en paralelo. Dentro de un mismo chat se
// sigue en orden: "ventas de Sears" y luego "y de Coppel?" necesitan que la
// primera ya este contestada para entender la segunda, y mandarlas juntas
// haria que la segunda cancelara a la primera en el servicio.

export async function atenderPorChat(mensajes, atender) {
  const porChat = new Map();
  for (const mensaje of mensajes ?? []) {
    const chat = mensaje?.key?.remoteJid ?? "";
    if (!porChat.has(chat)) porChat.set(chat, []);
    porChat.get(chat).push(mensaje);
  }

  await Promise.all(
    [...porChat.values()].map(async (delChat) => {
      for (const mensaje of delChat) {
        // atender() ya captura sus errores; esto es por si alguno se escapa:
        // un mensaje que falla no puede dejar sin atender al resto del chat.
        try {
          await atender(mensaje);
        } catch (error) {
          console.error(`[whatsapp] fallo atendiendo un mensaje del lote: ${error?.message ?? error}`);
        }
      }
    }),
  );
}
