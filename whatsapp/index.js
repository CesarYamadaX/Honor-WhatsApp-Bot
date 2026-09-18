import fs from "node:fs";
import path from "node:path";

import makeWASocket, {
  DisconnectReason,
  isJidBroadcast,
  isJidGroup,
  useMultiFileAuthState,
} from "@whiskeysockets/baileys";
import pino from "pino";
import qrcode from "qrcode-terminal";

import { autorizado, idiomaDelChat, preguntar } from "./agentClient.js";
import { calcularEsperaReconexion } from "./backoff.js";
import { trocear } from "./chunk.js";
import { RegistroDeVistos } from "./dedupe.js";
import { texto as traducir, textoArchivoMuyGrande } from "./textos.js";

const AGENT_API_URL = process.env.AGENT_API_URL ?? "http://127.0.0.1:8000";
const MAX_FILE_MB = leerMaxFileMB();
const CARPETA_AUTH = "auth_info";

// A partir de aqui la consulta se considera "de las dificiles" y se avisa que
// sigue viva. Por debajo, el indicador de "escribiendo" basta y un mensaje
// seria ruido. Una consulta tipica se responde en pocos segundos; las que
// cruzan tablas grandes pasan del medio minuto y ahi el silencio se siente
// como que el bot se cayo -- en los logs se ve al usuario reenviando.
const AVISO_DE_ESPERA_MS = leerEnteroPositivo("WAIT_NOTICE_SECONDS", 12) * 1000;
// El texto sale traducido segun el idioma del chat (ver textos.js).

// WhatsApp expira el indicador de "escribiendo" solo, hay que renovarlo.
const INTERVALO_PRESENCIA_MS = 8000;
const PAUSA_ENTRE_FRAGMENTOS_MS = 400;

// Ultimo socket vivo. atender() y las funciones de envio leen esta variable
// en vez de cerrar sobre el socket que estaba activo cuando llego el mensaje:
// una respuesta puede tardar hasta 120s (ver agentClient.js) y en ese tiempo
// el socket original puede haber muerto por una reconexion. Enviar contra el
// socket vigente evita que la respuesta se pierda en silencio.
let socketActual = null;

// Cuenta reconexiones fallidas consecutivas, para el backoff exponencial.
// Se resetea a 0 en cuanto una conexion abre con exito.
let intentosReconexion = 0;

// Evita procesar el mismo mensaje dos veces si WhatsApp lo reentrega (por
// ejemplo, tras una reconexion a media respuesta).
const registroVistos = new RegistroDeVistos();

// sendPresenceUpdate esta referenciado por jid, no por mensaje: si llegan dos
// mensajes casi simultaneos del mismo chat, comparten un solo intervalo de
// "escribiendo" y solo se manda "paused" cuando el ultimo termina. Sin esto,
// el primero en terminar apaga el indicador mientras el segundo sigue
// esperando respuesta.
const presenciaPorJid = new Map(); // jid -> { contador, temporizador }

function leerEnteroPositivo(clave, defecto) {
  const crudo = process.env[clave];
  if (crudo === undefined) return defecto;

  const valor = Number(crudo);
  if (!Number.isFinite(valor) || valor <= 0) {
    console.error(
      `[whatsapp] ${clave}="${crudo}" no es un numero valido, usando el default de ${defecto}`,
    );
    return defecto;
  }
  return valor;
}

function leerMaxFileMB() {
  return leerEnteroPositivo("MAX_FILE_MB", 16);
}

const dormir = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Timeout corto: este chequeo es solo un aviso de arranque, no debe demorar
// el resto del proceso si el servicio Python esta caido o la red esta lenta.
const TIMEOUT_HEALTH_MS = 5000;

// Consulta GET /health UNA sola vez, al arrancar (no es un chequeo
// periodico). El endpoint ya estaba implementado en api.py segun el spec,
// pero nada de este sidecar lo llamaba: quien levantara el bot se enteraba
// de que falto arrancar sql_agent/api.py hasta que llegaba el primer mensaje
// real. Esto lo deja saber de inmediato, por consola.
async function verificarServicioPython() {
  const controlador = new AbortController();
  const temporizador = setTimeout(() => controlador.abort(), TIMEOUT_HEALTH_MS);
  try {
    const respuesta = await fetch(`${AGENT_API_URL}/health`, { signal: controlador.signal });
    if (respuesta.ok) {
      console.log(`[whatsapp] servicio Python OK (${AGENT_API_URL}/health)`);
    } else {
      console.error(`[whatsapp] servicio Python respondio ${respuesta.status} en /health`);
    }
  } catch (error) {
    console.error(
      `[whatsapp] no se pudo contactar al servicio Python en ${AGENT_API_URL}/health: ` +
        `${error.message}. Verifica que sql_agent/api.py este corriendo.`,
    );
  } finally {
    clearTimeout(temporizador);
  }
}

function mantenerEscribiendo(jid) {
  let entrada = presenciaPorJid.get(jid);
  if (!entrada) {
    const enviar = () => {
      socketActual?.sendPresenceUpdate("composing", jid).catch(() => {});
    };
    enviar();
    entrada = { contador: 0, temporizador: setInterval(enviar, INTERVALO_PRESENCIA_MS) };
    presenciaPorJid.set(jid, entrada);
  }
  entrada.contador += 1;

  return () => {
    entrada.contador -= 1;
    if (entrada.contador > 0) return;
    clearInterval(entrada.temporizador);
    presenciaPorJid.delete(jid);
    socketActual?.sendPresenceUpdate("paused", jid).catch(() => {});
  };
}

async function enviarMensajeSimple(jid, texto) {
  if (!socketActual) {
    console.error(`[whatsapp] sin conexion activa, se perdio un aviso para ${jid}`);
    return;
  }
  try {
    await socketActual.sendMessage(jid, { text: texto });
  } catch (error) {
    console.error(`[whatsapp] fallo enviando aviso a ${jid}: ${error.message}`);
  }
}

async function enviarTexto(jid, texto) {
  const fragmentos = trocear(texto);
  for (const [indice, fragmento] of fragmentos.entries()) {
    if (!socketActual) {
      console.error(`[whatsapp] sin conexion activa, se perdio la respuesta para ${jid}`);
      return;
    }
    await socketActual.sendMessage(jid, { text: fragmento });
    if (indice < fragmentos.length - 1) await dormir(PAUSA_ENTRE_FRAGMENTOS_MS);
  }
}

async function enviarArchivo(jid, ruta, idioma) {
  let tamano;
  try {
    tamano = fs.statSync(ruta).size;
  } catch {
    console.error(`[whatsapp] archivo no encontrado: ${ruta}`);
    await enviarMensajeSimple(jid, traducir("archivo_ilegible", idioma));
    return;
  }

  if (tamano > MAX_FILE_MB * 1024 * 1024) {
    await enviarMensajeSimple(jid, textoArchivoMuyGrande(MAX_FILE_MB, idioma));
    return;
  }

  let contenido;
  try {
    contenido = fs.readFileSync(ruta);
  } catch (error) {
    // Puede desaparecer entre el statSync y aqui (p. ej. una limpieza del
    // directorio de exportacion). Sin este catch, el usuario se queda sin
    // avisar de que el reporte no llego, a diferencia de los otros dos
    // casos de falla de esta funcion.
    console.error(`[whatsapp] el archivo desaparecio antes de poder leerlo: ${ruta}: ${error.message}`);
    await enviarMensajeSimple(jid, traducir("archivo_ilegible", idioma));
    return;
  }

  if (!socketActual) {
    console.error(`[whatsapp] sin conexion activa, se perdio el archivo para ${jid}: ${ruta}`);
    return;
  }

  // La grafica va como IMAGEN, no como documento: asi se ve en la propia
  // conversacion, sin abrir nada. Antes viajaba dentro del Excel, en una
  // segunda hoja que en el telefono nadie abre.
  if (path.extname(ruta).toLowerCase() === ".png") {
    await socketActual.sendMessage(jid, { image: contenido, mimetype: "image/png" });
    return;
  }

  await socketActual.sendMessage(jid, {
    document: contenido,
    fileName: path.basename(ruta),
    mimetype: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
}

function textoDelMensaje(mensaje) {
  return (
    mensaje.message?.conversation ??
    mensaje.message?.extendedTextMessage?.text ??
    null
  );
}

async function atender(mensaje) {
  // Declarado fuera del try para que el catch de abajo pueda desmarcarlo.
  let idMensaje;

  // Todo el cuerpo va dentro del try: un shape de mensaje inesperado (algun
  // subtipo sin `key`, por ejemplo) no debe convertirse en un rechazo sin
  // capturar que tumbe el proceso.
  try {
    const jid = mensaje.key?.remoteJid;
    // fromMe evita responder a lo que el propio numero envia.
    if (!jid || mensaje.key?.fromMe) return;

    // WhatsApp esta migrando a JIDs "@lid" (un identificador interno, sin
    // relacion numerica con el telefono) en vez de "@s.whatsapp.net" para
    // varios chats 1:1. normalizar_chat_id() del lado Python solo reconoce
    // "@s.whatsapp.net", asi que un remoteJid en "@lid" nunca haria match
    // contra WHATSAPP_ALLOWED_NUMBERS sin importar que numero se configure
    // ahi. Baileys adjunta el numero real en key.senderPn cuando esto pasa
    // (viene del atributo sender_pn del stanza); se usa ESE valor solo para
    // la autorizacion/HTTP hacia Python, nunca para enviar: las respuestas
    // deben ir al jid original (jid), que es el que WhatsApp realmente usa
    // para enrutar el chat.
    const idAutorizacion = mensaje.key?.senderPn ?? jid;

    // Grupos y difusiones se descartan ANTES de tocar nada: ni dedupe, ni
    // presencia, ni HTTP. Si el chip llega a agregarse a un grupo, el bot no
    // debe ni enterarse de que hay conversacion ahi -- la allowlist de
    // Python ya los rechaza (config.normalizar_chat_id), pero para cuando
    // esa respuesta vuelve el indicador de "escribiendo" ya se habria
    // mostrado en el grupo, en cada mensaje que cualquiera escriba, para
    // siempre. Ese descarte tiene que vivir aqui, del lado de Node, antes de
    // que exista la tentacion de generar cualquier trafico o presencia.
    if (isJidGroup(jid) || isJidBroadcast(jid)) return;

    idMensaje = mensaje.key?.id;
    if (idMensaje && registroVistos.marcarYVerificar(idMensaje)) {
      console.log(`[whatsapp] mensaje duplicado ignorado (${idMensaje})`);
      return;
    }

    const texto = textoDelMensaje(mensaje);
    if (!texto || !texto.trim()) return;

    // Nada de señales de vida antes de saber que el chat esta autorizado: si
    // un numero ajeno ve al bot "escribiendo", eso ya confirma que hay un bot
    // del otro lado, y a los no autorizados se los ignora en silencio.
    // Por eso se pregunta primero, con una llamada que solo mira la allowlist
    // y vuelve en milisegundos. Antes esto no existia y la unica forma de
    // saberlo era mandar la pregunta entera y esperar: para cuando se sabia,
    // la consulta ya habia terminado y el indicador no cubria nada.
    if (!(await autorizado(AGENT_API_URL, idAutorizacion))) return;

    // Ya se puede acompañar la espera: "escribiendo" desde el primer segundo
    // y, si la consulta se alarga, un aviso para que el usuario sepa que
    // sigue viva y que puede cancelarla.
    const detener = mantenerEscribiendo(jid);
    const avisoDeEspera = setTimeout(async () => {
      const idioma = await idiomaDelChat(AGENT_API_URL, idAutorizacion);
      enviarMensajeSimple(jid, traducir("espera", idioma));
    }, AVISO_DE_ESPERA_MS);

    try {
      const resultado = await preguntar(AGENT_API_URL, idAutorizacion, texto.trim());
      // null = 403 (dejo de estar autorizado entre una llamada y otra) o 409
      // (a esta pregunta la cancelo un mensaje posterior, que es el que va a
      // contestar). En ambos casos, silencio.
      if (resultado === null) return;

      // El servicio dice en que idioma contesto; lo que agregue el sidecar
      // (un archivo ilegible, "no tengo respuesta") va en el mismo.
      const idioma = resultado.idioma;
      let enviadoAlgo = false;
      if (resultado.reply && resultado.reply.trim()) {
        await enviarTexto(jid, resultado.reply);
        enviadoAlgo = true;
      }
      if (resultado.files.length > 0) {
        for (const ruta of resultado.files) await enviarArchivo(jid, ruta, idioma);
        enviadoAlgo = true;
      }
      // reply en blanco (trocear lo descarta) o {reply:"", files:[]}: sin
      // esto el usuario se queda sin nada, sin saber si el bot lo escucho.
      if (!enviadoAlgo) {
        await enviarMensajeSimple(jid, traducir("sin_respuesta", idioma));
      }
    } finally {
      clearTimeout(avisoDeEspera);
      detener();
    }
  } catch (error) {
    console.error(`[whatsapp] fallo atendiendo ${mensaje.key?.remoteJid ?? "mensaje desconocido"}: ${error.message}`);
    // Ruling explicito: desmarcar aqui, no dejar el id marcado para siempre.
    // preguntar() ya absorbe internamente fallos de red/timeout y resuelve
    // con un mensaje amigable, asi que casi nunca llega hasta aqui. Lo que
    // SI llega esta dominado por el caso en que preguntar() tuvo exito -la
    // consulta SQL ya corrio- y lo unico que fallo fue la entrega (el socket
    // murio a media respuesta y sendMessage lanzo). Si el id quedara
    // marcado, el reenvio legitimo de WhatsApp tras reconectar se
    // descartaria como duplicado y el usuario se quedaria sin respuesta de
    // forma permanente y silenciosa: justo lo que la deduplicacion venia a
    // evitar, por otra via. Desmarcar cuesta, en el peor caso, una consulta
    // repetida, y el backend ya devuelve 429 si la primera sigue viva.
    if (idMensaje) registroVistos.desmarcar(idMensaje);
  }
}

// Reintenta arrancar() con backoff exponencial, protegido con su propio
// catch. Se usa tanto para reconexiones (tras un "close") como para el
// arranque inicial: un fallo aqui (disco lleno, credenciales a medio
// escribir, permisos, lo que sea) debe registrarse y programar otro
// intento, nunca tumbar el proceso en silencio ni dejarlo vivo pero sin
// conexion para siempre.
function programarReconexion() {
  const espera = calcularEsperaReconexion(intentosReconexion);
  intentosReconexion += 1;
  console.log(`[whatsapp] reintentando conexion en ${espera}ms (intento ${intentosReconexion})`);
  setTimeout(() => {
    arrancar().catch((error) => {
      console.error(`[whatsapp] fallo al (re)conectar: ${error.message}`);
      programarReconexion();
    });
  }, espera);
}

async function arrancar() {
  const { state, saveCreds } = await useMultiFileAuthState(CARPETA_AUTH);

  const sock = makeWASocket({
    auth: state,
    logger: pino({ level: "silent" }),
    markOnlineOnConnect: false,
  });
  socketActual = sock;

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("connection.update", ({ connection, lastDisconnect, qr }) => {
    // Baileys dispara este evento de forma SINCRONA desde dentro de su
    // propio manejador interno del cierre del websocket (ws.on('close', ...)
    // -> end() -> ev.emit('connection.update', ...)). Una excepcion aqui no
    // es una promesa rechazada: sube sincrona y se convierte en
    // uncaughtException, no en unhandledRejection -- por eso todo el cuerpo
    // va en try/catch, no solo el rmSync. Preferimos loguear y seguir
    // reintentando a que un solo fallo (p. ej. rmSync con EACCES/EPERM/EBUSY;
    // force:true solo absorbe ENOENT) tumbe el proceso entero.
    try {
      if (qr) {
        console.log("\nEscanea este codigo con WhatsApp > Dispositivos vinculados:\n");
        qrcode.generate(qr, { small: true });
      }

      if (connection === "open") {
        console.log("[whatsapp] conectado");
        intentosReconexion = 0;
      }

      if (connection === "close") {
        // Sin esto, socketActual sigue apuntando al socket muerto mientras
        // se espera el backoff de la reconexion, y el guard `if
        // (!socketActual)` de las funciones de envio nunca dispara: el envio
        // fallaria con una excepcion generica en vez del aviso explicito.
        socketActual = null;

        const codigo = lastDisconnect?.error?.output?.statusCode;
        // El motivo, no solo el hecho: un bucle de reconexion se ve igual en
        // el log venga de una sesion cerrada, de un reemplazo por otra
        // instancia o de un error del servidor, y sin el codigo no hay forma
        // de saber cual de los tres es.
        const motivo =
          Object.keys(DisconnectReason).find((k) => DisconnectReason[k] === codigo) ??
          "desconocido";
        console.log(
          `[whatsapp] desconectado (${motivo}, codigo ${codigo ?? "sin codigo"})` +
            `${lastDisconnect?.error?.message ? `: ${lastDisconnect.error.message}` : ""}`,
        );

        if (codigo === DisconnectReason.loggedOut) {
          // La sesion se cerro desde el telefono: hay que volver a parear.
          console.log("[whatsapp] sesion cerrada, borrando credenciales");
          try {
            fs.rmSync(CARPETA_AUTH, { recursive: true, force: true });
          } catch (error) {
            console.error(`[whatsapp] no se pudo borrar ${CARPETA_AUTH}: ${error.message}`);
          }
          intentosReconexion = 0;
        }
      }
    } catch (error) {
      console.error(`[whatsapp] fallo procesando connection.update: ${error.message}`);
    } finally {
      // En su propio finally: se programa la reconexion tanto si el cuerpo
      // de arriba corrio limpio como si exploto a medio camino.
      if (connection === "close") programarReconexion();
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const mensaje of messages) await atender(mensaje);
  });
}

// Red de seguridad final: si algo se nos escapa de todos los try/catch de
// arriba, preferimos loguearlo y seguir vivo a que el proceso muera en
// silencio y deje de responder a todos los chats.
process.on("unhandledRejection", (error) => {
  console.error(`[whatsapp] rechazo no manejado: ${error?.message ?? error}`);
});

// Complementa al anterior: cubre las excepciones que suben SINCRONAS (por
// ejemplo, desde dentro del propio emit sincrono de connection.update que
// dispara Baileys al cerrar el websocket) y que por eso nunca pasan por un
// unhandledRejection.
process.on("uncaughtException", (error) => {
  console.error(`[whatsapp] excepcion no capturada: ${error?.message ?? error}`);
});

// Sin await: es un aviso, no una dependencia. La conexion a WhatsApp no debe
// esperar a que Python responda para empezar (por ejemplo, para mostrar el
// QR), aunque cualquier consulta real fallara hasta que ambos esten arriba.
verificarServicioPython();

arrancar().catch((error) => {
  console.error(`[whatsapp] fallo el arranque inicial: ${error.message}`);
  programarReconexion();
});
