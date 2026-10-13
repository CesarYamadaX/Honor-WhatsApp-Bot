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
