import { test } from "node:test";
import assert from "node:assert/strict";

import { EnCurso, VENTANA_PARA_SUMAR_MS, esAcuse, esCancelacion } from "../seguimiento.js";

const CHAT = "5215512345678@s.whatsapp.net";

test("un ok, un gracias o un emoji son acuses; una pregunta no", () => {
  for (const acuse of ["Ok", "ok.", "Gracias!", "va gracias", "👍", "🙏🏼", "Sí", "thanks"]) {
    assert.ok(esAcuse(acuse), acuse);
  }
  for (const pregunta of ["Y precio please", "de 2026", "ok y de Coppel?", "si, de Elektra"]) {
    assert.ok(!esAcuse(pregunta), pregunta);
  }
});

test("las cancelaciones son las mismas que entiende el servicio", () => {
  assert.ok(esCancelacion("Cancela") && esCancelacion("olvídalo!") && esCancelacion("stop"));
  assert.ok(!esCancelacion("cuantas ventas se cancelaron en marzo"));
});

test("sin nada en curso, el mensaje va solo", () => {
  const enCurso = new EnCurso();
  const llegada = enCurso.llega(CHAT, "ventas de Sears", 0);
  assert.equal(llegada.accion, "nueva");
  assert.equal(llegada.ficha.pregunta, "ventas de Sears");
});

// Los casos del 19/10/2026 en produccion.
test("dentro de los 30 s se suma y la ventana corre desde el ultimo mensaje", () => {
  const enCurso = new EnCurso();
  enCurso.llega(CHAT, "Quiero un reporte de 2026 de mayo a la fecha", 0);
  const segunda = enCurso.llega(CHAT, "Y precio please", 10_000);
  assert.equal(segunda.accion, "sumar");
  assert.match(segunda.pregunta, /^Quiero un reporte de 2026 de mayo a la fecha\n\n.*Y precio please\)$/s);
  // Una tercera a 35 s de la primera pero a 25 s de la segunda: tambien.
  const tercera = enCurso.llega(CHAT, "y por tienda", 35_000);
  assert.equal(tercera.accion, "sumar");
  assert.match(tercera.pregunta, /Y precio please.*y por tienda/s);
});

test("un ok a media consulta no la toca", () => {
  const enCurso = new EnCurso();
  enCurso.llega(CHAT, "reporte de todos los canales", 0);
  assert.equal(enCurso.llega(CHAT, "Ok", 163_000).accion, "acuse");
  assert.ok(enCurso.porChat.has(CHAT));
});

test("cancela no se suma: va al servicio, que es el que cancela", () => {
  const enCurso = new EnCurso();
  enCurso.llega(CHAT, "reporte de todos los canales", 0);
  const llegada = enCurso.llega(CHAT, "cancela", 5_000);
  assert.equal(llegada.accion, "nueva");
  assert.equal(llegada.ficha.pregunta, "cancela");
});

test("pasada la ventana, otra pregunta espera a que acabe la anterior", async () => {
  const enCurso = new EnCurso();
  const primera = enCurso.llega(CHAT, "reporte de todos los canales", 0).ficha;
  const llegada = enCurso.llega(CHAT, "y de Coppel?", VENTANA_PARA_SUMAR_MS + 1);
  assert.equal(llegada.accion, "esperar");
  assert.equal(llegada.avisar, true);
  let tomo = null;
  llegada.turno.then((ficha) => (tomo = ficha));
  await new Promise((listo) => setImmediate(listo));
  assert.equal(tomo, null);
  enCurso.termina(CHAT, primera);
  const segunda = await llegada.turno;
  assert.equal(segunda.pregunta, "y de Coppel?");
  assert.equal(enCurso.porChat.get(CHAT), segunda);
});

test("dos en espera no arrancan juntas", async () => {
  const enCurso = new EnCurso();
  const primera = enCurso.llega(CHAT, "uno", 0).ficha;
  const a = enCurso.llega(CHAT, "dos", 60_000);
  const b = enCurso.llega(CHAT, "tres", 61_000);
  enCurso.termina(CHAT, primera);
  const tomada = await Promise.race([a.turno, b.turno]);
  await new Promise((listo) => setImmediate(listo));
  // Solo una tiene el chat; la otra sigue esperando.
  assert.equal(enCurso.porChat.get(CHAT), tomada);
  enCurso.termina(CHAT, tomada);
  const otra = await (tomada.pregunta === "dos" ? b.turno : a.turno);
  assert.notEqual(otra, tomada);
});

test("con la respuesta ya en camino no se suma: espera, sin avisar", () => {
  const enCurso = new EnCurso();
  const ficha = enCurso.llega(CHAT, "ventas de Sears", 0).ficha;
  ficha.respondida = true;
  const llegada = enCurso.llega(CHAT, "y de Coppel?", 2_000);
  assert.equal(llegada.accion, "esperar");
  assert.equal(llegada.avisar, false);
  assert.equal(enCurso.llega(CHAT, "gracias", 2_500).accion, "acuse");
});

test("la que se sumo no suelta el chat cuando acaba la vieja", () => {
  const enCurso = new EnCurso();
  const vieja = enCurso.llega(CHAT, "uno", 0).ficha;
  const nueva = enCurso.llega(CHAT, "dos", 5_000).ficha;
  enCurso.termina(CHAT, vieja); // la vieja vuelve con 409
  assert.equal(enCurso.porChat.get(CHAT), nueva);
  enCurso.termina(CHAT, nueva);
  assert.ok(!enCurso.porChat.has(CHAT));
});
