import assert from "node:assert/strict";
import http from "node:http";
import { test } from "node:test";

import { transcribir } from "../agentClient.js";
import { audioDelMensaje, esDemasiadoLargo, formatoDelAudio } from "../notasDeVoz.js";

test("una nota de voz de WhatsApp va como ogg; un audio reenviado, en su formato", () => {
  assert.equal(formatoDelAudio("audio/ogg; codecs=opus"), "ogg");
  assert.equal(formatoDelAudio("audio/mpeg"), "mp3");
  assert.equal(formatoDelAudio("audio/mp4"), "m4a");
  assert.equal(formatoDelAudio(undefined), "ogg");
});

test("se reconoce el audio del mensaje", () => {
  const audio = { seconds: 5, mimetype: "audio/ogg; codecs=opus" };
  assert.equal(audioDelMensaje({ message: { audioMessage: audio } }), audio);
  assert.equal(audioDelMensaje({ message: { conversation: "hola" } }), null);
  assert.equal(audioDelMensaje({}), null);
});

test("un audio largo no se transcribe; uno sin duracion si", () => {
  assert.equal(esDemasiadoLargo({ seconds: 121 }, 120), true);
  assert.equal(esDemasiadoLargo({ seconds: 120 }, 120), false);
  assert.equal(esDemasiadoLargo({}, 120), false);
});

// Un servicio de mentira que contesta /transcribir con lo que se le diga.
async function conServicio(status, cuerpo, prueba) {
  const recibidos = [];
  const servidor = http.createServer((req, res) => {
    let datos = "";
    req.on("data", (t) => (datos += t));
    req.on("end", () => {
      recibidos.push({ url: req.url, cuerpo: JSON.parse(datos) });
      res.writeHead(status, { "Content-Type": "application/json" });
      res.end(JSON.stringify(cuerpo));
    });
  });
  await new Promise((r) => servidor.listen(0, "127.0.0.1", r));
  try {
    const { port } = servidor.address();
    return await prueba(`http://127.0.0.1:${port}`, recibidos);
  } finally {
    servidor.close();
  }
}

test("manda el audio en base64 y devuelve el texto", async () => {
  await conServicio(200, { texto: "cuantas vendio Honor", idioma: "es" }, async (url, recibidos) => {
    const r = await transcribir(url, "521@s.whatsapp.net", Buffer.from("OggS"), "ogg");
    assert.deepEqual(r, { texto: "cuantas vendio Honor", idioma: "es" });
    assert.equal(recibidos[0].url, "/transcribir");
    assert.deepEqual(recibidos[0].cuerpo, {
      chat_id: "521@s.whatsapp.net",
      audio: Buffer.from("OggS").toString("base64"),
      formato: "ogg",
    });
  });
});

test("cada falla trae el texto que hay que mandarle al usuario", async () => {
  for (const [status, clave] of [[413, "audio_largo"], [422, "audio_no_entendido"], [502, "audio_fallo"]]) {
    await conServicio(status, {}, async (url) => {
      assert.deepEqual(await transcribir(url, "x", Buffer.from("a"), "ogg"), { falla: clave });
    });
  }
});

test("un chat no autorizado se queda en silencio", async () => {
  await conServicio(403, {}, async (url) => {
    assert.equal(await transcribir(url, "x", Buffer.from("a"), "ogg"), null);
  });
});

test("sin servicio, falla con aviso", async () => {
  assert.deepEqual(
    await transcribir("http://127.0.0.1:1", "x", Buffer.from("a"), "ogg"),
    { falla: "audio_fallo" },
  );
});
