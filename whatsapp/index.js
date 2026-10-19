import fs from "node:fs";
import path from "node:path";

import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  isJidBroadcast,
  isJidGroup,
  useMultiFileAuthState,
} from "@whiskeysockets/baileys";
import pino from "pino";
import qrcode from "qrcode-terminal";

import {
  autorizado,
  idiomaDelChat,
  preguntar,
  reaccionar,
  registrarEnviados,
  transcribir,
} from "./agentClient.js";
import { programarAvisos } from "./avisos.js";
import { calcularEsperaReconexion } from "./backoff.js";
import { repararCredenciales, respaldarCredenciales } from "./credenciales.js";
import { trocear } from "./chunk.js";
import { RegistroDeVistos } from "./dedupe.js";
import { atenderPorChat } from "./repartir.js";
import { aFormatoWhatsApp } from "./markdown.js";
import { audioDelMensaje, esDemasiadoLargo, formatoDelAudio, tipoDeDocumento } from "./notasDeVoz.js";
import { texto as traducir, textoArchivoMuyGrande } from "./textos.js";
import { sinLlaves } from "./silenciar.js";
import { actualizarFotos, aplicarCambioDeFoto } from "./fotosDePerfil.js";
import { EnCurso } from "./seguimiento.js";

// Antes de conectar: libsignal vuelca sesiones con llaves privadas por consola
// y la consola termina en el log (ver silenciar.js).
sinLlaves();

const AGENT_API_URL = process.env.AGENT_API_URL ?? "http://127.0.0.1:8000";
const MAX_FILE_MB = leerMaxFileMB();
const MAX_IMAGE_MB = leerMaxImagenMB();
const CARPETA_AUTH = "auth_info";

// A partir de aqui la consulta se considera "de las dificiles" y se avisa que
// sigue viva. Por debajo, el indicador de "escribiendo" basta y un mensaje
// seria ruido. Una consulta tipica se responde en pocos segundos; las que
// cruzan tablas grandes pasan del medio minuto y ahi el silencio se siente
// como que el bot se cayo -- en los logs se ve al usuario reenviando.
const AVISO_DE_ESPERA_MS = leerEnteroPositivo("WAIT_NOTICE_SECONDS", 12) * 1000;

// Y el segundo aviso, el que pide paciencia y promete avisar al terminar.
// Un reporte de un anio de Coppel tarda ~290s y uno de toda la historia
// ~440s, medido. Era a los 120 s; el 19/10/2026 se pidio que saliera cuando
// "se tarda mucho", y en la bitacora la mitad de las consultas acaba en 11 s
// y 9 de cada 10 antes de 90 s: a los 60 s solo lo ven las de verdad largas.
const AVISO_DE_PACIENCIA_MS = leerEnteroPositivo("PATIENCE_NOTICE_SECONDS", 60) * 1000;
// Los textos salen traducidos segun el idioma del chat (ver textos.js).

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
// El telefono real de cada chat "@lid", aprendido de sus mensajes (que traen
// key.senderPn). Una reaccion puede llegar sin senderPn, y sin esto se
// autorizaria con el @lid, que el servicio no reconoce.
const telefonoDelLid = new Map();

// sendPresenceUpdate esta referenciado por jid, no por mensaje: si llegan dos
// mensajes casi simultaneos del mismo chat, comparten un solo intervalo de
// "escribiendo" y solo se manda "paused" cuando el ultimo termina. Sin esto,
// el primero en terminar apaga el indicador mientras el segundo sigue
// esperando respuesta.
const presenciaPorJid = new Map(); // jid -> { contador, temporizador }

// Lo que esta corriendo en cada chat, para decidir que hacer con un mensaje
// que llega mientras tanto: sumarlo, ignorarlo o formarlo (ver seguimiento.js).
const enCurso = new EnCurso();

// Un aviso en el idioma del chat. Nunca lanza: es un acompañamiento, y un
// fallo aqui no puede dejar un chat sin su respuesta.
async function avisarEnSuIdioma(jid, idAutorizacion, clave) {
  try {
    const idioma = await idiomaDelChat(AGENT_API_URL, idAutorizacion);
    await enviarMensajeSimple(jid, traducir(clave, idioma));
  } catch (error) {
    console.error(`[whatsapp] no pude mandar el aviso "${clave}" a ${jid}: ${error.message}`);
  }
}

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

// WhatsApp tiene DOS limites distintos, y usar el chico para todo era el
// error: 16 MB es el tope de MEDIA (imagenes, video, audio), y un .xlsx no
// viaja como media sino como DOCUMENTO, que admite mucho mas. Con el limite
// de media aplicado al Excel, un reporte grande no se entregaba y el usuario
// recibia "el archivo es demasiado grande" por un archivo que WhatsApp si
// habria aceptado.
//
// Los 100 MB son el limite documentado de WhatsApp para documentos, no uno
// medido aqui: mandar un archivo de prueba de ese tamano habria significado
// escribirle de verdad a alguien. Se deja configurable por si el limite real
// resulta ser otro.
function leerMaxFileMB() {
  return leerEnteroPositivo("MAX_FILE_MB", 100);
}

// El de la grafica, que si es media. Una grafica pesa ~55 KB, asi que esto
// no se acerca nunca; esta por correccion, no por necesidad.
function leerMaxImagenMB() {
  return leerEnteroPositivo("MAX_IMAGE_MB", 16);
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

// Devuelve los ids de los mensajes que mando: una reaccion 👍/👎 a
// cualquiera de ellos califica la respuesta (ver calificaciones.py).
async function enviarTexto(jid, texto) {
  const ids = [];
  // La traduccion va ANTES de trocear: `**` convertido a `*` cambia el
  // largo, y hacerlo despues podria dejar un par partido entre dos
  // fragmentos, que se veria peor que el markdown crudo.
  const fragmentos = trocear(aFormatoWhatsApp(texto));
  for (const [indice, fragmento] of fragmentos.entries()) {
    if (!socketActual) {
      console.error(`[whatsapp] sin conexion activa, se perdio la respuesta para ${jid}`);
      return ids;
    }
    const enviado = await socketActual.sendMessage(jid, { text: fragmento });
    if (enviado?.key?.id) ids.push(enviado.key.id);
    if (indice < fragmentos.length - 1) await dormir(PAUSA_ENTRE_FRAGMENTOS_MS);
  }
  return ids;
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

  const esImagen = path.extname(ruta).toLowerCase() === ".png";
  const limite = esImagen ? MAX_IMAGE_MB : MAX_FILE_MB;
  if (tamano > limite * 1024 * 1024) {
    await enviarMensajeSimple(jid, textoArchivoMuyGrande(limite, idioma));
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
  const enviado = esImagen
    ? await socketActual.sendMessage(jid, { image: contenido, mimetype: "image/png" })
    : await socketActual.sendMessage(jid, {
        document: contenido,
        fileName: path.basename(ruta),
        mimetype: tipoDeDocumento(ruta),
      });
  return enviado?.key?.id ? [enviado.key.id] : [];
}

// Un mensaje que no se atiende se descarta en silencio, y hay cinco motivos
// distintos por los que puede pasar. Sin esta linea, "le escribo y no
// contesta" no deja NADA en el log y no hay por donde empezar a mirar.
//
// Se anota el motivo y de quien venia, nunca el contenido: el log es para
// operar el bot, no para leer las conversaciones de nadie. Y sigue sin
// contestarle al que escribe -- el silencio hacia fuera es lo que evita
// confirmarle a un numero ajeno que hay un bot aqui.
function descartado(motivo, jid, extra = "") {
  console.log(`[whatsapp] mensaje descartado (${motivo}) de ${jid ?? "?"}${extra}`);
}

function textoDelMensaje(mensaje) {
  return (
    mensaje.message?.conversation ??
    mensaje.message?.extendedTextMessage?.text ??
    null
  );
}

// Una nota de voz convertida en la pregunta, o null si no hay que seguir
// (no se entendio, es muy larga, fallo la transcripcion: en esos casos al
// usuario ya se le dijo por que). Solo se llama con el chat ya autorizado.
async function preguntaDeLaNota(jid, idAutorizacion, mensaje, audio) {
  const avisar = async (clave) => {
    const idioma = await idiomaDelChat(AGENT_API_URL, idAutorizacion);
    await enviarMensajeSimple(jid, traducir(clave, idioma));
  };
  if (esDemasiadoLargo(audio)) {
    console.log(`[whatsapp] nota de voz de ${jid} demasiado larga (${audio.seconds}s)`);
    await avisar("audio_largo");
    return null;
  }
  const detener = mantenerEscribiendo(jid);
  try {
    let datos;
    try {
      datos = await downloadMediaMessage(
        mensaje,
        "buffer",
        {},
        {
          logger: pino({ level: "silent" }),
          reuploadRequest: (m) => socketActual.updateMediaMessage(m),
        },
      );
    } catch (error) {
      console.error(`[whatsapp] no pude descargar la nota de voz de ${jid}: ${error.message}`);
      await avisar("audio_fallo");
      return null;
    }
    const resultado = await transcribir(AGENT_API_URL, idAutorizacion, datos, formatoDelAudio(audio.mimetype));
    if (resultado === null) return null;
    if (resultado.falla) {
      await avisar(resultado.falla);
      return null;
    }
    console.log(`[whatsapp] nota de voz de ${jid} (${audio.seconds ?? "?"}s): ${resultado.texto}`);
    // Lo que se entendio, antes de correr la consulta: si salio mal, el
    // usuario lo ve ya y no despues de esperar un reporte de otra cosa.
    await enviarMensajeSimple(jid, `${traducir("audio_entendido", resultado.idioma)} _${resultado.texto}_`);
    return resultado.texto;
  } finally {
    detener();
  }
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
    if (!jid || mensaje.key?.fromMe) {
      if (jid) descartado("lo envio este mismo numero", jid);
      return;
    }

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
    if (mensaje.key?.senderPn) telefonoDelLid.set(jid, mensaje.key.senderPn);
    const idAutorizacion = mensaje.key?.senderPn ?? telefonoDelLid.get(jid) ?? jid;

    // Grupos y difusiones se descartan ANTES de tocar nada: ni dedupe, ni
    // presencia, ni HTTP. Si el chip llega a agregarse a un grupo, el bot no
    // debe ni enterarse de que hay conversacion ahi -- la allowlist de
    // Python ya los rechaza (config.normalizar_chat_id), pero para cuando
    // esa respuesta vuelve el indicador de "escribiendo" ya se habria
    // mostrado en el grupo, en cada mensaje que cualquiera escriba, para
    // siempre. Ese descarte tiene que vivir aqui, del lado de Node, antes de
    // que exista la tentacion de generar cualquier trafico o presencia.
    if (isJidGroup(jid) || isJidBroadcast(jid)) {
      descartado("grupo o difusion", jid);
      return;
    }

    idMensaje = mensaje.key?.id;
    if (idMensaje && registroVistos.marcarYVerificar(idMensaje)) {
      console.log(`[whatsapp] mensaje duplicado ignorado (${idMensaje})`);
      return;
    }

    // Una reaccion (👍/👎) a un mensaje del bot es una calificacion de esa
    // respuesta. No se contesta nada: es una señal, no una pregunta. El
    // servicio decide si cuenta (solo pulgares, y solo si el id es de un
    // mensaje que mando el bot: los tiene registrados).
    //
    // Antes aqui se exigia reaccion.key.fromMe, y ninguna reaccion llego
    // nunca (6/10/2026, tabla de calificaciones vacia en produccion): esa
    // llave viene como la ve QUIEN REACCIONA, y para el el mensaje del bot no
    // es suyo. El id basta: el servicio lo busca entre los enviados.
    const reaccion = mensaje.message?.reactionMessage;
    if (reaccion) {
      const reaccionado = reaccion.key?.id;
      // fromMe es desde quien reacciona: true es un mensaje SUYO, no del bot.
      // El servicio guarda cualquier otra reaccion aunque el mensaje no este
      // registrado, asi que esta es la unica forma de no calificar al bot por
      // un 👍 que el usuario se puso a si mismo.
      if (reaccion.key?.fromMe) {
        descartado("reaccion a un mensaje propio", jid);
        return;
      }
      if (reaccionado && (await autorizado(AGENT_API_URL, idAutorizacion))) {
        const resultado = await reaccionar(AGENT_API_URL, idAutorizacion, reaccionado, reaccion.text ?? "");
        console.log(
          `[whatsapp] reaccion ${reaccion.text || "(quitada)"} de ${jid} a ${reaccionado}: ` +
            (resultado?.calificado ? "calificada" : resultado?.motivo ?? "sin respuesta del servicio"),
        );
      } else {
        descartado("reaccion que no califica", jid, ` (id ${reaccionado ?? "?"})`);
      }
      return;
    }

    const texto = textoDelMensaje(mensaje);
    const audio = audioDelMensaje(mensaje);
    if ((!texto || !texto.trim()) && !audio) {
      // Una foto, un sticker o un tipo de mensaje que todavia no se sabe
      // leer (los audios si: se transcriben, ver preguntaDeLaNota). Se
      // anota cual, que es justo lo que hace falta para decidir si vale la
      // pena soportarlo.
      descartado("sin texto", jid, ` (${Object.keys(mensaje.message ?? {}).join(", ") || "vacio"})`);
      return;
    }

    // Nada de señales de vida antes de saber que el chat esta autorizado: si
    // un numero ajeno ve al bot "escribiendo", eso ya confirma que hay un bot
    // del otro lado, y a los no autorizados se los ignora en silencio.
    // Por eso se pregunta primero, con una llamada que solo mira la allowlist
    // y vuelve en milisegundos. Antes esto no existia y la unica forma de
    // saberlo era mandar la pregunta entera y esperar: para cuando se sabia,
    // la consulta ya habia terminado y el indicador no cubria nada.
    if (!(await autorizado(AGENT_API_URL, idAutorizacion))) {
      descartado("no autorizado", idAutorizacion, idAutorizacion === jid ? "" : ` (chat ${jid})`);
      return;
    }

    let pregunta = texto?.trim();
    if (!pregunta) {
      pregunta = await preguntaDeLaNota(jid, idAutorizacion, mensaje, audio);
      if (!pregunta) return;
    }

    // ¿Sigue corriendo algo de este chat? Entonces este mensaje se suma, se
    // ignora (un "ok") o espera su turno, en vez de cancelar lo que ya lleva
    // hecho (ver seguimiento.js).
    const llegada = enCurso.llega(jid, pregunta);
    if (llegada.accion === "acuse") {
      descartado("acuse mientras trabaja", jid);
      return;
    }
    let ficha = llegada.ficha;
    try {
      if (llegada.accion === "sumar") {
        console.log(`[whatsapp] ${jid} agrego algo a lo que pidio: se vuelve a preguntar todo junto`);
        pregunta = llegada.pregunta;
        await avisarEnSuIdioma(jid, idAutorizacion, "se_suma");
      } else if (llegada.accion === "esperar") {
        console.log(`[whatsapp] ${jid} pregunto otra cosa mientras trabaja: espera su turno`);
        if (llegada.avisar) await avisarEnSuIdioma(jid, idAutorizacion, "en_cola");
        ficha = await llegada.turno;
      }
      await responderEnElChat(jid, idAutorizacion, pregunta, ficha);
    } finally {
      if (ficha) enCurso.termina(jid, ficha);
      else llegada.turno?.then((tomada) => enCurso.termina(jid, tomada));
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

// La pregunta al servicio y la entrega de lo que conteste.
async function responderEnElChat(jid, idAutorizacion, pregunta, ficha) {
  // Ya se puede acompañar la espera: "escribiendo" desde el primer segundo,
  // un aviso a los pocos segundos para que sepa que la consulta sigue
  // viva, y otro mas si la cosa se alarga de verdad.
  const detener = mantenerEscribiendo(jid);
  // Si salio el aviso de "te aviso cuando acabe", al acabar se avisa.
  let prometioAvisar = false;
  const cancelarAvisos = programarAvisos(
    async (clave, vigente) => {
      const idioma = await idiomaDelChat(AGENT_API_URL, idAutorizacion);
      // Mientras se averiguaba el idioma pudo llegar la respuesta: un
      // "ten paciencia" DESPUES del reporte es peor que ningun aviso.
      if (!vigente()) return;
      // Antes de mandarlo: la respuesta puede llegar mientras se envia.
      if (clave === "espera_larga") prometioAvisar = true;
      await enviarMensajeSimple(jid, traducir(clave, idioma));
    },
    { esperaMs: AVISO_DE_ESPERA_MS, pacienciaMs: AVISO_DE_PACIENCIA_MS },
  );

  try {
    const resultado = await preguntar(AGENT_API_URL, idAutorizacion, pregunta);
    // Desde aqui la respuesta se esta entregando: un mensaje que llegue
    // ahora va despues, no se suma (ver seguimiento.js).
    if (ficha) ficha.respondida = true;
    // Los avisos se cortan en cuanto hay respuesta, no al terminar de
    // entregarla: subir un Excel grande tarda, y en ese rato el de
    // paciencia ("te aviso en cuanto este") saldria con el reporte ya a
    // medio llegar. El finally lo repite por si preguntar() lanza.
    cancelarAvisos();
    // null = 403 (dejo de estar autorizado entre una llamada y otra) o 409
    // (a esta pregunta la cancelo un mensaje posterior, que es el que va a
    // contestar). En ambos casos, silencio.
    if (resultado === null) return;

    // El servicio dice en que idioma contesto; lo que agregue el sidecar
    // (un archivo ilegible, "no tengo respuesta") va en el mismo.
    const idioma = resultado.idioma;
    // Lo prometido: "te aviso en cuanto este". Va como mensaje propio, que
    // suena en el telefono aunque haya dejado el chat.
    if (prometioAvisar) await enviarMensajeSimple(jid, traducir("listo", idioma));
    let enviadoAlgo = false;
    // Los ids de lo que se mando, para que una reaccion a cualquiera de
    // esos mensajes (el texto, el Excel, la grafica) califique este turno.
    const ids = [];
    // Y de los que son un archivo, cual: el Excel se califica aparte del
    // texto (un 👎 a cada uno son dos calificaciones, no una que pisa a otra).
    const partes = {};
    if (resultado.reply && resultado.reply.trim()) {
      ids.push(...((await enviarTexto(jid, resultado.reply)) ?? []));
      enviadoAlgo = true;
    }
    if (resultado.files.length > 0) {
      for (const ruta of resultado.files) {
        for (const id of (await enviarArchivo(jid, ruta, idioma)) ?? []) {
          ids.push(id);
          partes[id] = ruta;
        }
      }
      enviadoAlgo = true;
    }
    if (resultado.turno && ids.length) {
      await registrarEnviados(AGENT_API_URL, idAutorizacion, resultado.turno, ids, partes);
    }
    // reply en blanco (trocear lo descarta) o {reply:"", files:[]}: sin
    // esto el usuario se queda sin nada, sin saber si el bot lo escucho.
    if (!enviadoAlgo) {
      await enviarMensajeSimple(jid, traducir("sin_respuesta", idioma));
    }
  } finally {
    cancelarAvisos();
    detener();
  }
}

const CADA_CUANTO_FOTOS_MS = 6 * 60 * 60 * 1000;
// Cada cuanto se pregunta si el panel pidio cambiar la foto del bot. Es una
// llamada local y casi siempre vacia; 5 s es lo que tarda en sentirse lento.
const CADA_CUANTO_CAMBIO_DE_FOTO_MS = 5000;
let relojDeFotos = null;
let relojDelCambioDeFoto = null;

function programarFotos(sock) {
  if (relojDeFotos) clearInterval(relojDeFotos);
  if (relojDelCambioDeFoto) clearInterval(relojDelCambioDeFoto);
  const correr = () =>
    actualizarFotos(sock, AGENT_API_URL).catch((error) =>
      console.error(`[whatsapp] no se pudieron revisar las fotos de perfil: ${error.message}`),
    );
  setTimeout(correr, 30_000); // despues de que termine de sincronizar
  relojDeFotos = setInterval(correr, CADA_CUANTO_FOTOS_MS);

  // Una subida a la vez: si WhatsApp tarda, la siguiente vuelta no empieza
  // otra encima.
  let cambiando = false;
  relojDelCambioDeFoto = setInterval(async () => {
    if (cambiando) return;
    cambiando = true;
    try {
      await aplicarCambioDeFoto(sock, AGENT_API_URL);
    } catch (error) {
      console.error(`[whatsapp] no se pudo revisar el cambio de foto del bot: ${error.message}`);
    } finally {
      cambiando = false;
    }
  }, CADA_CUANTO_CAMBIO_DE_FOTO_MS);
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
  // Antes de que Baileys lea nada: si creds.json quedo truncado por un
  // apagon o un kill a medio escribir, se repone del respaldo. Sin esto,
  // Baileys lo ve vacio, asume que no hay sesion y pide un QR -- y el bot
  // deja de atender hasta que alguien vuelva a vincularlo con el telefono.
  if (repararCredenciales(CARPETA_AUTH)) {
    console.log(
      "[whatsapp] creds.json estaba ilegible y se repuso del respaldo. " +
        "Suele significar que el proceso murio a medio guardar.",
    );
  }

  const { state, saveCreds } = await useMultiFileAuthState(CARPETA_AUTH);

  const sock = makeWASocket({
    auth: state,
    logger: pino({ level: "silent" }),
    markOnlineOnConnect: false,
  });
  socketActual = sock;

  sock.ev.on("creds.update", async () => {
    await saveCreds();
    // El respaldo se hace DESPUES de guardar y solo si lo guardado sirve,
    // para no copiar un archivo a medio escribir.
    respaldarCredenciales(CARPETA_AUTH);
  });

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
        // Las fotos de perfil para el panel: al conectar y cada 6 horas (el
        // servicio solo devuelve a quien no se reviso en 24 h).
        programarFotos(sock);
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
    // "notify" es un mensaje que acaba de llegar; "append" y "prepend" son
    // el historial que el telefono sincroniza al conectar, y atenderlos
    // seria contestar conversaciones viejas.
    if (type !== "notify") {
      descartado(`upsert ${type}`, messages?.[0]?.key?.remoteJid, ` (${messages?.length ?? 0} mensajes)`);
      return;
    }
    // Cada chat en paralelo; dentro de un chat, en orden (ver repartir.js).
    await atenderPorChat(messages, atender);
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
