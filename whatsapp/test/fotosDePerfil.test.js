import { test } from "node:test";
import assert from "node:assert/strict";

import { esSinFoto, traerFoto } from "../fotosDePerfil.js";

const sinFoto = Object.assign(new Error("item-not-found"), { output: { statusCode: 404 } });
const privada = Object.assign(new Error("not-authorized"), { output: { statusCode: 401 } });

test("sin foto o privada es 'no hay foto'; una falla de red no", () => {
  assert.ok(esSinFoto(sinFoto) && esSinFoto(privada));
  assert.ok(!esSinFoto(new Error("Timed Out")));
});

test("trae la foto y la manda en base64", async () => {
  const sock = { profilePictureUrl: async (jid) => (jid === "521555@s.whatsapp.net" ? "https://foto" : null) };
  const descargar = async () => ({ ok: true, arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer });
  assert.deepEqual(await traerFoto(sock, "521555", descargar), { numero: "521555", imagen: "AQID" });
});

test("sin foto devuelve imagen null; la red caida lanza para reintentar", async () => {
  assert.deepEqual(await traerFoto({ profilePictureUrl: async () => { throw privada; } }, "1"), { numero: "1", imagen: null });
  await assert.rejects(traerFoto({ profilePictureUrl: async () => { throw new Error("Timed Out"); } }, "1"));
});

test("el numero del bot sale de su propio id", async () => {
  const { numeroDelBot } = await import("../fotosDePerfil.js");
  assert.equal(numeroDelBot({ user: { id: "5215512345678:12@s.whatsapp.net" } }), "5215512345678");
  assert.equal(numeroDelBot({}), null);
});

// --- cambiar la foto del bot desde el panel (pedido el 13/10/2026) ---

import http from "node:http";

// Un servicio de mentira: entrega `cambio` en GET y guarda lo que se le avisa.
async function servicio(cambio) {
  const avisos = [];
  const servidor = http.createServer((pedido, respuesta) => {
    let cuerpo = "";
    pedido.on("data", (trozo) => (cuerpo += trozo));
    pedido.on("end", () => {
      if (pedido.method === "POST") avisos.push(JSON.parse(cuerpo));
      respuesta.setHeader("Content-Type", "application/json");
      respuesta.end(JSON.stringify(pedido.method === "GET" ? cambio : {}));
    });
  });
  await new Promise((listo) => servidor.listen(0, "127.0.0.1", listo));
  return { url: `http://127.0.0.1:${servidor.address().port}`, avisos, cerrar: () => servidor.close() };
}

const BOT = { id: "5215512345678:12@s.whatsapp.net" };

test("pone la foto en el perfil del propio bot y avisa que salio", async () => {
  const { aplicarCambioDeFoto } = await import("../fotosDePerfil.js");
  const falso = await servicio({ id: "abc", imagen: "AQID" });
  const subidas = [];
  const sock = { user: BOT, updateProfilePicture: async (jid, buffer) => subidas.push([jid, [...buffer]]) };
  try {
    assert.equal(await aplicarCambioDeFoto(sock, falso.url), true);
    assert.deepEqual(subidas, [["5215512345678@s.whatsapp.net", [1, 2, 3]]]);
    assert.deepEqual(falso.avisos, [{ id: "abc", numero: "5215512345678", error: null }]);
  } finally {
    falso.cerrar();
  }
});

test("si WhatsApp la rechaza, avisa el error", async () => {
  const { aplicarCambioDeFoto } = await import("../fotosDePerfil.js");
  const falso = await servicio({ id: "abc", imagen: "AQID" });
  const sock = { user: BOT, updateProfilePicture: async () => { throw new Error("not-authorized"); } };
  try {
    assert.equal(await aplicarCambioDeFoto(sock, falso.url), false);
    assert.equal(falso.avisos[0].error, "not-authorized");
  } finally {
    falso.cerrar();
  }
});

test("sin cambio pendiente, o sin servicio, no toca WhatsApp", async () => {
  const { aplicarCambioDeFoto } = await import("../fotosDePerfil.js");
  const falso = await servicio({});
  const sock = { user: BOT, updateProfilePicture: async () => assert.fail("no debia subir nada") };
  try {
    assert.equal(await aplicarCambioDeFoto(sock, falso.url), false);
    assert.equal(await aplicarCambioDeFoto(sock, "http://127.0.0.1:1"), false);
    assert.deepEqual(falso.avisos, []);
  } finally {
    falso.cerrar();
  }
});
