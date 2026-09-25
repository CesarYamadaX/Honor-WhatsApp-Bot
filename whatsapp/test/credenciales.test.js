import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";

import { repararCredenciales, respaldarCredenciales } from "../credenciales.js";

// Una sesion minima pero con la forma que se comprueba: sin noiseKey no es
// utilizable, aunque sea JSON valido.
const SESION = { noiseKey: { private: "abc", public: "def" }, registered: true };

function carpeta(contenido = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "creds-"));
  for (const [nombre, valor] of Object.entries(contenido)) {
    fs.writeFileSync(
      path.join(dir, nombre),
      typeof valor === "string" ? valor : JSON.stringify(valor),
    );
  }
  return dir;
}

const leer = (dir, n) => fs.readFileSync(path.join(dir, n), "utf-8");
const hay = (dir, n) => fs.existsSync(path.join(dir, n));

test("el caso real: creds.json en 0 bytes se repone del respaldo", () => {
  // Paso el 25 de septiembre de 2026 tras varios reinicios seguidos. Sin
  // esto, Baileys lo ve vacio, asume que no hay sesion y pide un QR.
  const dir = carpeta({ "creds.json": "", "creds.json.bak": SESION });

  assert.equal(repararCredenciales(dir), true);
  assert.deepEqual(JSON.parse(leer(dir, "creds.json")), SESION);
});

test("un creds.json a medio escribir tambien se repone", () => {
  const dir = carpeta({ "creds.json": '{"noiseKey":{"priv', "creds.json.bak": SESION });

  assert.equal(repararCredenciales(dir), true);
  assert.deepEqual(JSON.parse(leer(dir, "creds.json")), SESION);
});

test("un JSON valido pero sin sesion usable no se da por bueno", () => {
  const dir = carpeta({ "creds.json": { registered: true }, "creds.json.bak": SESION });

  assert.equal(repararCredenciales(dir), true);
  assert.deepEqual(JSON.parse(leer(dir, "creds.json")), SESION);
});

test("unas credenciales sanas no se tocan", () => {
  const otra = { noiseKey: { private: "zzz" }, registered: true };
  const dir = carpeta({ "creds.json": otra, "creds.json.bak": SESION });

  assert.equal(repararCredenciales(dir), false);
  assert.deepEqual(JSON.parse(leer(dir, "creds.json")), otra);
});

test("sin respaldo no hay nada que reponer, y no revienta", () => {
  const dir = carpeta({ "creds.json": "" });
  assert.equal(repararCredenciales(dir), false);

  // Ni con un respaldo que tampoco sirve.
  const otro = carpeta({ "creds.json": "", "creds.json.bak": "" });
  assert.equal(repararCredenciales(otro), false);
});

test("una carpeta que no existe no revienta", () => {
  assert.equal(repararCredenciales(path.join(os.tmpdir(), "no-existe-nunca")), false);
});

test("respaldar copia las credenciales buenas", () => {
  const dir = carpeta({ "creds.json": SESION });

  assert.equal(respaldarCredenciales(dir), true);
  assert.deepEqual(JSON.parse(leer(dir, "creds.json.bak")), SESION);
});

test("respaldar NO pisa una copia buena con una rota", () => {
  // Este es el punto: si se respaldara sin mirar, el primer guardado a medias
  // se llevaria por delante la unica copia sana que quedaba.
  const dir = carpeta({ "creds.json": "", "creds.json.bak": SESION });

  assert.equal(respaldarCredenciales(dir), false);
  assert.deepEqual(JSON.parse(leer(dir, "creds.json.bak")), SESION);
});

test("respaldar no deja archivos temporales tirados", () => {
  const dir = carpeta({ "creds.json": SESION });
  respaldarCredenciales(dir);

  assert.ok(!hay(dir, "creds.json.bak.tmp"));
  assert.deepEqual(fs.readdirSync(dir).sort(), ["creds.json", "creds.json.bak"]);
});

test("guardar y reponer, de punta a punta", () => {
  const dir = carpeta({ "creds.json": SESION });
  respaldarCredenciales(dir);

  // Y ahora el proceso muere a medio escribir.
  fs.writeFileSync(path.join(dir, "creds.json"), "");

  assert.equal(repararCredenciales(dir), true);
  assert.deepEqual(JSON.parse(leer(dir, "creds.json")), SESION);
});
