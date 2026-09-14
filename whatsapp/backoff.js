// Calculo del tiempo de espera entre reintentos de reconexion a WhatsApp.
//
// Backoff exponencial con techo: si la desconexion es transitoria, reconecta
// rapido; si es persistente (numero marcado, sesion corrupta, conflicto de
// dispositivo), deja de martillar los servidores de WhatsApp cada pocos
// segundos. Ese patron de reintento agresivo es justo lo que los sistemas
// antiabuso usan para detectar y banear automatizacion.

const BASE_MS_DEFAULT = 2000;
const TECHO_MS_DEFAULT = 5 * 60 * 1000; // 5 minutos

/**
 * @param {number} intento - 0 para el primer reintento tras una conexion
 *   exitosa, incrementando por cada reconexion fallida consecutiva.
 * @param {object} [opciones]
 * @param {number} [opciones.baseMs] - espera del primer intento.
 * @param {number} [opciones.techoMs] - espera maxima, sin importar cuantos
 *   intentos lleve.
 * @returns {number} milisegundos a esperar antes de reintentar.
 */
export function calcularEsperaReconexion(
  intento,
  { baseMs = BASE_MS_DEFAULT, techoMs = TECHO_MS_DEFAULT } = {},
) {
  if (!Number.isFinite(intento) || intento < 0) {
    throw new Error(`intento debe ser un numero finito >= 0 (recibido: ${intento})`);
  }
  const espera = baseMs * 2 ** intento;
  // 2**intento puede desbordar a Infinity con un intento muy grande; el
  // techo lo resuelve igual (Math.min con Infinity da el techo).
  return Math.min(espera, techoMs);
}
