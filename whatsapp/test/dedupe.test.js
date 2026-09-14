import test from "node:test";
import assert from "node:assert/strict";

import { RegistroDeVistos } from "../dedupe.js";

test("la primera vez que se ve un id, no es duplicado", () => {
  const registro = new RegistroDeVistos();
  assert.equal(registro.marcarYVerificar("abc"), false);
});

test("la segunda vez que se ve el mismo id, es duplicado", () => {
  const registro = new RegistroDeVistos();
  registro.marcarYVerificar("abc");
  assert.equal(registro.marcarYVerificar("abc"), true);
});

test("ids distintos no interfieren entre si", () => {
  const registro = new RegistroDeVistos();
  assert.equal(registro.marcarYVerificar("uno"), false);
  assert.equal(registro.marcarYVerificar("dos"), false);
  assert.equal(registro.marcarYVerificar("uno"), true);
  assert.equal(registro.marcarYVerificar("dos"), true);
});

test("una entrada expira pasado el TTL", () => {
  const registro = new RegistroDeVistos({ ttlMs: 1000 });
  const t0 = 1_000_000;
  registro.marcarYVerificar("abc", t0);
  // Justo antes de expirar, sigue siendo duplicado.
  assert.equal(registro.marcarYVerificar("abc", t0 + 999), true);
  // Pasado el TTL, ya no.
  assert.equal(registro.marcarYVerificar("abc", t0 + 1001), false);
});

test("respeta un tope de tamano, descartando las entradas mas viejas", () => {
  const registro = new RegistroDeVistos({ tope: 3, ttlMs: 10_000_000 });
  const t0 = 1_000_000;
  registro.marcarYVerificar("a", t0);
  registro.marcarYVerificar("b", t0 + 1);
  registro.marcarYVerificar("c", t0 + 2);
  // Al agregar una cuarta entrada por encima del tope, "a" (la mas vieja) cae.
  registro.marcarYVerificar("d", t0 + 3);
  assert.equal(registro.marcarYVerificar("a", t0 + 4), false, "a debio haber sido descartada");
  assert.equal(registro.marcarYVerificar("d", t0 + 5), true, "d sigue registrada");
});

test("marcar y verificar es un solo paso: no hay hueco entre ver y marcar", () => {
  const registro = new RegistroDeVistos();
  const resultados = ["x", "x", "x"].map((id) => registro.marcarYVerificar(id));
  // Solo la primera de tres llamadas "simultaneas" (mismo tick) debe pasar
  // como no-duplicado; las otras dos, aunque disparadas antes de que
  // cualquiera terminara de procesar el mensaje, deben verse como duplicadas.
  assert.deepEqual(resultados, [false, true, true]);
});

test("constructor rechaza ttlMs o tope no positivos", () => {
  assert.throws(() => new RegistroDeVistos({ ttlMs: 0 }));
  assert.throws(() => new RegistroDeVistos({ tope: 0 }));
  assert.throws(() => new RegistroDeVistos({ ttlMs: -5 }));
});

test("desmarcar permite que un id vuelva a tratarse como nuevo", () => {
  const registro = new RegistroDeVistos();
  registro.marcarYVerificar("abc");
  registro.desmarcar("abc");
  assert.equal(registro.marcarYVerificar("abc"), false);
});

test("desmarcar un id que nunca se vio no lanza y no afecta a otros", () => {
  const registro = new RegistroDeVistos();
  registro.marcarYVerificar("otro");
  assert.doesNotThrow(() => registro.desmarcar("nunca-visto"));
  assert.equal(registro.marcarYVerificar("otro"), true);
});
