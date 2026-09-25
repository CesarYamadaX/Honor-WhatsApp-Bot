import assert from "node:assert/strict";
import { test } from "node:test";
import { setTimeout as esperar } from "node:timers/promises";

import { programarAvisos } from "../avisos.js";

// Plazos de milisegundos para no hacer esperar a la suite. Lo que se prueba
// es el ORDEN y la cancelacion, no los numeros reales (12s y 120s).
const PLAZOS = { esperaMs: 10, pacienciaMs: 30 };

test("los dos avisos llegan, y en orden", async () => {
  const mandados = [];
  const cancelar = programarAvisos((clave) => mandados.push(clave), PLAZOS);

  await esperar(20);
  assert.deepEqual(mandados, ["espera"], "primero el corto");

  await esperar(30);
  assert.deepEqual(mandados, ["espera", "espera_larga"]);
  cancelar();
});

test("una consulta que termina rapido no manda ninguno", async () => {
  // El caso normal: la mayoria se responde en segundos y el usuario no
  // deberia ver ningun aviso.
  const mandados = [];
  const cancelar = programarAvisos((clave) => mandados.push(clave), PLAZOS);
  cancelar();

  await esperar(50);
  assert.deepEqual(mandados, []);
});

test("cancelar corta el segundo aviso aunque el primero ya haya salido", async () => {
  // Este es el que importa: sin cancelar, el usuario recibe "ten paciencia"
  // DESPUES de su reporte, que es peor que no avisar nada.
  const mandados = [];
  const cancelar = programarAvisos((clave) => mandados.push(clave), PLAZOS);

  await esperar(20);
  cancelar();
  await esperar(40);

  assert.deepEqual(mandados, ["espera"]);
});

test("cancelar dos veces no revienta", async () => {
  const cancelar = programarAvisos(() => {}, PLAZOS);
  cancelar();
  assert.doesNotThrow(cancelar);
});

test("si el envio falla, el aviso siguiente igual sale", async () => {
  // Un fallo al avisar no puede llevarse el resto: la respuesta de verdad
  // viaja por otro lado.
  const mandados = [];
  const cancelar = programarAvisos((clave) => {
    mandados.push(clave);
    if (clave === "espera") throw new Error("sin conexion");
  }, PLAZOS);

  await esperar(50);
  assert.deepEqual(mandados, ["espera", "espera_larga"]);
  cancelar();
});

test("una promesa rechazada tampoco queda sin manejar", async () => {
  const mandados = [];
  const cancelar = programarAvisos(async (clave) => {
    mandados.push(clave);
    throw new Error("el servicio no contesto");
  }, PLAZOS);

  await esperar(50);
  assert.deepEqual(mandados, ["espera", "espera_larga"]);
  cancelar();
});

test("sin una funcion para enviar, falla al programar y no despues", () => {
  assert.throws(() => programarAvisos(null, PLAZOS), TypeError);
});
