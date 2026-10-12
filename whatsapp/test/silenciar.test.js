import { test } from "node:test";
import assert from "node:assert/strict";

import { sinLlaves } from "../silenciar.js";

function consolaFalsa() {
  const escrito = [];
  const consola = {};
  for (const metodo of ["log", "info", "warn", "error", "debug"]) {
    consola[metodo] = (...args) => escrito.push(args);
  }
  return { consola, escrito };
}

test("la sesion de libsignal no llega al log, con o sin llaves", () => {
  const { consola, escrito } = consolaFalsa();
  sinLlaves(consola);
  consola.info("Closing session:", { currentRatchet: { ephemeralKeyPair: { privKey: Buffer.from("secreto") } } });
  consola.warn("Closing open session in favor of incoming prekey bundle");
  assert.deepEqual(escrito, [
    ["Closing session (detalle de la sesion omitido)"],
    ["Closing open session in favor of incoming prekey bundle (detalle de la sesion omitido)"],
  ]);
  assert.ok(!JSON.stringify(escrito).includes("secreto"));
});

test("lo demas sale igual que siempre", () => {
  const { consola, escrito } = consolaFalsa();
  sinLlaves(consola);
  consola.log("[whatsapp] conectado");
  consola.error("[whatsapp] fallo", { codigo: 428 });
  assert.deepEqual(escrito, [["[whatsapp] conectado"], ["[whatsapp] fallo", { codigo: 428 }]]);
});
