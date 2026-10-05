// Notas de voz: que audios se atienden y en que formato van al servicio.
//
// Antes cualquier mensaje sin texto se descartaba en silencio, audios
// incluidos. Quien pregunta desde la calle o desde una tienda prefiere
// dictar; ahora el audio se transcribe en el servicio Python y se pregunta
// como si lo hubieran escrito.

// Una pregunta dictada dura segundos. Un audio de varios minutos casi nunca
// es una pregunta (un audio reenviado, una junta) y cuesta transcribirlo.
export const SEGUNDOS_MAXIMOS_DE_AUDIO = leerEntero("VOICE_MAX_SECONDS", 120);

function leerEntero(nombre, porDefecto) {
  const valor = Number.parseInt(process.env[nombre] ?? "", 10);
  return Number.isFinite(valor) && valor > 0 ? valor : porDefecto;
}

/** El audioMessage del mensaje, o null si no es un audio. */
export function audioDelMensaje(mensaje) {
  return mensaje?.message?.audioMessage ?? null;
}

/**
 * El formato que entiende el modelo, sacado del mimetype de WhatsApp.
 * Una nota de voz es "audio/ogg; codecs=opus"; un audio reenviado puede ser
 * mp3, m4a o wav.
 */
export function formatoDelAudio(mimetype) {
  const tipo = String(mimetype ?? "").toLowerCase().split(";")[0].trim();
  const formatos = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/aac": "aac",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
  };
  return formatos[tipo] ?? "ogg";
}

/** true si el audio dura mas de lo que se atiende. Sin duracion, se atiende. */
export function esDemasiadoLargo(audio, maximo = SEGUNDOS_MAXIMOS_DE_AUDIO) {
  const segundos = Number(audio?.seconds);
  return Number.isFinite(segundos) && segundos > maximo;
}
