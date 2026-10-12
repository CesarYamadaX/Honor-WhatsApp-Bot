// libsignal, la biblioteca de cifrado que trae Baileys, imprime con console.*
// la sesion ENTERA cuando la cierra o la reemplaza -- con sus llaves privadas
// (privKey, rootKey, chainKey) --, y iniciar.py guarda todo lo que sale por
// consola en el log, que se lee desde el panel. Visto el 12/10/2026: tres
// volcados al conectar, ~50 lineas que el panel contaba como errores.
//
// Se deja una linea que dice que paso (sirve para saber que se renovo una
// sesion) y se tira el objeto con las llaves.
export const RUIDO_DE_LIBSIGNAL =
  /^(Closing session|Closing open session|Removing old closed session|Session already closed|Opening session|Migrating session)/i;

export function sinLlaves(consola = console) {
  for (const metodo of ["log", "info", "warn", "error", "debug"]) {
    const original = consola[metodo].bind(consola);
    consola[metodo] = (...args) => {
      if (typeof args[0] === "string" && RUIDO_DE_LIBSIGNAL.test(args[0])) {
        original(`${args[0].replace(/[:\s]+$/, "")} (detalle de la sesion omitido)`);
        return;
      }
      original(...args);
    };
  }
}
