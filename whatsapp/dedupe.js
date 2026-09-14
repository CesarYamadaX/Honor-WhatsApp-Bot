// Registro en memoria de mensajes ya atendidos, para no procesar dos veces el
// mismo mensaje cuando WhatsApp lo reentrega (por ejemplo, tras una
// reconexion a media respuesta).
//
// TTL fijo por entrada para que el registro no crezca sin limite con el
// tiempo, mas un tope duro de tamano como red de seguridad si llegara un
// pico de mensajes en poco tiempo.

const TTL_MS_DEFAULT = 60 * 60 * 1000; // 1 hora
const TOPE_DEFAULT = 500;

export class RegistroDeVistos {
  constructor({ ttlMs = TTL_MS_DEFAULT, tope = TOPE_DEFAULT } = {}) {
    if (ttlMs <= 0) throw new Error(`ttlMs debe ser > 0 (recibido: ${ttlMs})`);
    if (tope <= 0) throw new Error(`tope debe ser > 0 (recibido: ${tope})`);
    this.ttlMs = ttlMs;
    this.tope = tope;
    // Map preserva orden de insercion, asi que las mas viejas quedan primero.
    this.vistos = new Map(); // id -> instante en que expira
  }

  _purgar(ahora) {
    for (const [id, expira] of this.vistos) {
      if (expira <= ahora) this.vistos.delete(id);
    }
    while (this.vistos.size > this.tope) {
      const masVieja = this.vistos.keys().next().value;
      this.vistos.delete(masVieja);
    }
  }

  /**
   * Marca `id` como visto y dice si ya lo estaba.
   *
   * El marcado y la verificacion son un solo paso sincrono: no hay ventana
   * entre "ver si esta" y "marcarlo" en la que dos llamadas casi simultaneas
   * (dos entregas del mismo mensaje llegando una tras otra) puedan colarse
   * ambas como "primera vez".
   *
   * @param {string} id
   * @param {number} [ahora] - pensado para pruebas; por defecto Date.now().
   * @returns {boolean} true si `id` ya estaba registrado (es un duplicado);
   *   false si es la primera vez que se ve (y a partir de ahora ya queda
   *   marcado).
   */
  marcarYVerificar(id, ahora = Date.now()) {
    this._purgar(ahora);
    if (this.vistos.has(id)) return true;
    this.vistos.set(id, ahora + this.ttlMs);
    return false;
  }

  /**
   * Quita `id` del registro, como si nunca se hubiera visto.
   *
   * Pensado para cuando el procesamiento de un mensaje ya marcado termino en
   * error DESPUES de que el trabajo de fondo tuvo exito (por ejemplo: la
   * consulta ya corrio, pero el envio de la respuesta fallo porque el socket
   * murio a media espera). Dejar el id marcado para siempre haria que un
   * reenvio legitimo del mismo mensaje se descartara como duplicado y el
   * usuario se quedara sin respuesta de forma permanente y silenciosa.
   *
   * Si `id` no estaba registrado, no hace nada (no lanza).
   */
  desmarcar(id) {
    this.vistos.delete(id);
  }
}
