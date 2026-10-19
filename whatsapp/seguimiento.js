// Que hacer con un mensaje que llega mientras el bot sigue con el anterior
// del mismo chat (pedido el 19/10/2026).
//
// Antes, cualquier mensaje nuevo cancelaba la consulta en curso y se
// contestaba solo. Visto ese dia en produccion: "un reporte de todos los
// canales de mayo a la fecha" y, por audio, "de 2026" -- se contesto "de
// 2026" solo ("¿te refieres al precio del iPhone 17?"); un "Ok" al aviso de
// "te aviso en cuanto este" tiro un reporte que llevaba 162 s; y "Y precio
// please" 10 s despues de pedir un reporte lo cancelo y se pregunto "¿de que
// modelo?". Ahora:
//
//   - dentro de la ventana (30 s desde su ultimo mensaje): se SUMA a lo que
//     pidio y la consulta vuelve a empezar con todo junto;
//   - un "ok", "gracias" o 👍: se ignora, no cancela nada;
//   - "cancela": cancela, como siempre (lo resuelve el servicio);
//   - cualquier otra cosa: ESPERA su turno y se contesta al terminar.

export const VENTANA_PARA_SUMAR_MS = 30_000;

const normalizar = (texto) =>
  String(texto ?? "")
    .normalize("NFD")
    .replace(/\p{M}/gu, "")
    .toLowerCase()
    .replace(/[\s.,;:!¡?¿…。！，]+/gu, " ")
    .trim();

// Las mismas que _PALABRAS_DE_CANCELACION en sql_agent/api.py: el servicio
// es el que cancela; aqui solo se evita sumar un "cancela" a la pregunta.
const CANCELACIONES = new Set([
  "cancela", "cancelar", "cancelalo", "cancelala", "detente", "olvidalo", "ya no",
  "cancel", "cancel it", "cancel that", "stop", "never mind", "nevermind", "forget it",
  "取消", "停", "停止", "算了", "不用了",
]);

const ACUSES = new Set([
  "ok", "okay", "oki", "okis", "ok gracias", "va", "vale", "sale", "si", "sip", "bien",
  "gracias", "muchas gracias", "mil gracias", "perfecto", "excelente", "listo",
  "enterado", "de acuerdo", "esta bien", "ok perfecto", "va gracias", "sale gracias",
  "thanks", "thank you", "thx", "great", "got it", "sure", "ok thanks",
  "好", "好的", "谢谢", "收到",
]);

export function esCancelacion(texto) {
  return CANCELACIONES.has(normalizar(texto));
}

// Un acuse: una palabra de las de arriba, o solo emojis (👍, 🙏).
export function esAcuse(texto) {
  const limpio = normalizar(texto);
  if (!limpio) return true;
  if (ACUSES.has(limpio)) return true;
  return !/[\p{L}\p{N}]/u.test(limpio);
}

// La pregunta que se manda al servicio. El agregado va aparte y dicho como
// tal: el modelo tiene que saber que es la misma peticion, y que si
// contradice lo primero, manda lo ultimo.
export function unirPreguntas(anterior, nuevo) {
  return (
    `${anterior}\n\n(El usuario agrego esto enseguida, antes de recibir respuesta; ` +
    `completa o corrige lo anterior, y si lo contradice vale esto: ${nuevo})`
  );
}

/** Lo que esta corriendo en cada chat. */
export class EnCurso {
  constructor() {
    this.porChat = new Map(); // jid -> { pregunta, llego, termino, avisar }
  }

  _empezar(jid, pregunta, ahora) {
    let avisar;
    const termino = new Promise((listo) => (avisar = listo));
    const ficha = { pregunta, llego: ahora, termino, avisar, respondida: false };
    this.porChat.set(jid, ficha);
    return ficha;
  }

  /**
   * Decide que hacer con un mensaje. Todo lo que registra lo hace en el acto,
   * sin await de por medio: dos mensajes casi juntos no pueden ver los dos el
   * chat libre.
   *
   * Devuelve { accion, ficha?, pregunta?, turno? }:
   *   "nueva"   -> nada en curso (o un "cancela"): ficha registrada.
   *   "sumar"   -> pregunta = todo junto; ficha registrada.
   *   "acuse"   -> no hacer nada.
   *   "esperar" -> turno: promesa de la ficha cuando el chat quede libre;
   *                avisar: si conviene decirle que espera su turno.
   */
  llega(jid, texto, ahora = Date.now()) {
    const previo = this.porChat.get(jid);
    if (!previo || esCancelacion(texto)) {
      return { accion: "nueva", ficha: this._empezar(jid, texto, ahora) };
    }
    // La respuesta anterior ya se esta entregando: sumarle algo la haria
    // llegar dos veces. Lo nuevo va despues, sin avisar nada.
    if (previo.respondida) {
      if (esAcuse(texto)) return { accion: "acuse" };
      return { accion: "esperar", avisar: false, turno: this._turno(jid, texto) };
    }
    if (ahora - previo.llego <= VENTANA_PARA_SUMAR_MS) {
      const pregunta = unirPreguntas(previo.pregunta, texto);
      return { accion: "sumar", pregunta, ficha: this._empezar(jid, pregunta, ahora) };
    }
    if (esAcuse(texto)) return { accion: "acuse" };
    return { accion: "esperar", avisar: true, turno: this._turno(jid, texto) };
  }

  async _turno(jid, texto) {
    // La comprobacion y el registro van juntos tras cada await: si dos
    // esperan, el primero que despierta toma el chat y el otro sigue
    // esperando.
    while (this.porChat.has(jid)) await this.porChat.get(jid).termino;
    return this._empezar(jid, texto, Date.now());
  }

  /** Esa consulta acabo (bien, mal o cancelada). */
  termina(jid, ficha) {
    if (this.porChat.get(jid) === ficha) this.porChat.delete(jid);
    ficha.avisar();
  }
}
