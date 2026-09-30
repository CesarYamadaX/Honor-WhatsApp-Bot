// Los avisos que acompañan una espera larga.
//
// Son dos, y en dos tiempos, porque una sola señal no alcanza desde que los
// reportes grandes se entregan completos: un anio de Coppel tarda ~290s y
// toda su historia ~440s (medido). El primero llega a los pocos segundos y
// dice que la consulta sigue viva; el segundo, a los dos minutos, pide
// paciencia y aclara que no hace falta repetir la pregunta.
//
// Eso ultimo no es cortesia: reenviar es lo peor que puede hacer quien
// espera, porque el mensaje nuevo cancela el trabajo ya hecho y la cuenta
// vuelve a cero. En los logs se ve pasar justo eso durante los silencios
// largos.
//
// Vive aparte de index.js para poder probarlo: que un temporizador quede
// colgado no se nota hasta que el usuario recibe "ten paciencia" DESPUES de
// su reporte, y eso es exactamente lo que una prueba si puede vigilar.

/**
 * Programa los dos avisos y devuelve como cancelarlos.
 *
 * `cancelar()` es obligatorio y tiene que llamarse pase lo que pase (en un
 * `finally`): sin eso, una consulta que termina en 3s igual manda el aviso
 * de paciencia dos minutos despues, cuando el usuario ya tiene su respuesta.
 *
 * `enviar` recibe, ademas de la clave, un `vigente()` que dice si los avisos
 * siguen en pie. Hace falta porque enviar suele esperar algo antes de mandar
 * (el idioma del chat): si la respuesta llega en ese intervalo, el
 * temporizador ya disparo y `cancelar()` no lo alcanza, asi que quien envia
 * tiene que volver a mirar justo antes de mandar.
 *
 * @param {(clave: string, vigente: () => boolean) => Promise<void>|void} enviar - manda ese aviso.
 * @param {{esperaMs: number, pacienciaMs: number}} plazos
 * @returns {() => void} cancelar
 */
export function programarAvisos(enviar, { esperaMs, pacienciaMs }) {
  if (typeof enviar !== "function") {
    throw new TypeError("programarAvisos necesita una funcion para enviar");
  }

  let cancelado = false;
  const vigente = () => !cancelado;

  // Un fallo al avisar no puede tumbar nada: el aviso es un acompañamiento,
  // y la respuesta de verdad viene por otro lado. Se registra y se sigue.
  const mandar = (clave) => () => {
    if (cancelado) return;
    try {
      Promise.resolve(enviar(clave, vigente)).catch((error) => {
        console.error(`[avisos] no se pudo mandar "${clave}": ${error?.message ?? error}`);
      });
    } catch (error) {
      console.error(`[avisos] no se pudo mandar "${clave}": ${error?.message ?? error}`);
    }
  };

  const temporizadores = [
    setTimeout(mandar("espera"), esperaMs),
    setTimeout(mandar("espera_larga"), pacienciaMs),
  ];

  return () => {
    // Idempotente: que se llame dos veces (un finally mas un catch de mas
    // arriba) no debe ser un problema.
    if (cancelado) return;
    cancelado = true;
    for (const t of temporizadores) clearTimeout(t);
  };
}
