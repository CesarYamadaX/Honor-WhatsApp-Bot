import test from "node:test";
import assert from "node:assert/strict";

import { calcularEsperaReconexion } from "../backoff.js";

test("el intento 0 usa la base", () => {
  assert.equal(calcularEsperaReconexion(0, { baseMs: 2000, techoMs: 300_000 }), 2000);
});

test("cada intento duplica la espera anterior", () => {
  const opciones = { baseMs: 2000, techoMs: 300_000 };
  assert.equal(calcularEsperaReconexion(1, opciones), 4000);
  assert.equal(calcularEsperaReconexion(2, opciones), 8000);
  assert.equal(calcularEsperaReconexion(3, opciones), 16000);
});

test("no supera el techo aunque el intento sea grande", () => {
  const opciones = { baseMs: 2000, techoMs: 300_000 };
  assert.equal(calcularEsperaReconexion(20, opciones), 300_000);
  assert.equal(calcularEsperaReconexion(1000, opciones), 300_000);
});

test("usa los valores por defecto (base 2s, techo 5min) si no se pasan opciones", () => {
  assert.equal(calcularEsperaReconexion(0), 2000);
  assert.equal(calcularEsperaReconexion(10), 5 * 60 * 1000);
});

test("lanza si el intento es negativo", () => {
  assert.throws(() => calcularEsperaReconexion(-1));
});

test("lanza si el intento es NaN", () => {
  assert.throws(() => calcularEsperaReconexion(NaN));
});

test("lanza si el intento es Infinity", () => {
  assert.throws(() => calcularEsperaReconexion(Infinity));
});
