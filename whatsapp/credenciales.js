// Un respaldo de las credenciales de WhatsApp, y su restauracion automatica.
//
// Baileys guarda la sesion en auth_info/creds.json y lo reescribe cada vez
// que cambia algo. Si el proceso muere a medio escribir -- un Ctrl+C con mala
// suerte, un apagon, un `taskkill /T /F` --, el archivo queda TRUNCADO A CERO
// BYTES. No hay error, no hay aviso: en el siguiente arranque el sidecar
// dibuja un QR y el bot deja de atender a todo el mundo hasta que alguien
// vuelva a vincularlo con el telefono.
//
// Paso de verdad el 25 de septiembre de 2026, tras varios reinicios seguidos
// durante una sesion de pruebas: creds.json quedo en 0 bytes y la sesion se
// perdio. auth_info/ esta en .gitignore -- y debe estarlo, son credenciales
// --, asi que no habia ninguna copia de donde sacarla.
//
// El seguro es chico: despues de cada guardado se deja una copia buena al
// lado, y al arrancar, si la de Baileys no sirve, se repone. La copia vale
// porque creds.json cambia poco: son las llaves de la sesion, no el estado
// de los mensajes.

import fs from "node:fs";
import path from "node:path";

const CREDENCIALES = "creds.json";
const RESPALDO = "creds.json.bak";

/** Lee ese JSON, o null si no existe, esta vacio o no se puede parsear. */
function leerJson(ruta) {
  try {
    const crudo = fs.readFileSync(ruta, "utf-8");
    if (!crudo.trim()) return null;
    const datos = JSON.parse(crudo);
    // Un objeto sin `noiseKey` no es una sesion utilizable, aunque sea JSON
    // valido: mejor tratarlo como ausente que restaurar algo inservible.
    return datos && datos.noiseKey ? datos : null;
  } catch {
    return null;
  }
}

/**
 * Deja una copia de las credenciales, si las actuales sirven.
 *
 * Se escribe primero a un temporal y despues se renombra, que en el mismo
 * volumen es atomico: asi el respaldo nunca queda a medias, que es
 * exactamente el problema del que protege.
 */
export function respaldarCredenciales(carpeta) {
  const origen = path.join(carpeta, CREDENCIALES);
  if (leerJson(origen) === null) return false;

  const destino = path.join(carpeta, RESPALDO);
  const temporal = `${destino}.tmp`;
  try {
    fs.copyFileSync(origen, temporal);
    fs.renameSync(temporal, destino);
    return true;
  } catch (error) {
    console.error(`[credenciales] no se pudo respaldar: ${error.message}`);
    try {
      fs.rmSync(temporal, { force: true });
    } catch {
      // Un temporal que sobra no rompe nada; el proximo guardado lo pisa.
    }
    return false;
  }
}

/**
 * Repone las credenciales desde el respaldo si las de Baileys no sirven.
 *
 * Devuelve true solo si de verdad hubo que reponerlas, para que quien llama
 * lo registre: perder la sesion y recuperarla en silencio esconderia que
 * algo esta matando el proceso a medio escribir.
 */
export function repararCredenciales(carpeta) {
  const ruta = path.join(carpeta, CREDENCIALES);
  if (leerJson(ruta) !== null) return false;

  const respaldo = path.join(carpeta, RESPALDO);
  if (leerJson(respaldo) === null) return false;

  try {
    fs.copyFileSync(respaldo, ruta);
    return true;
  } catch (error) {
    console.error(`[credenciales] no se pudo reponer: ${error.message}`);
    return false;
  }
}
