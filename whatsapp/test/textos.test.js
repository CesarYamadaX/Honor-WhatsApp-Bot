import assert from "node:assert/strict";
import { test } from "node:test";

import { IDIOMA_POR_DEFECTO, texto, textoArchivoMuyGrande } from "../textos.js";

const IDIOMAS = ["es", "en", "zh"];
const CLAVES = [
  "espera",
  "espera_larga",
  "ocupado",
  "demasiado_lenta",
  "servicio_caido",
  "desconocido",
  "sin_respuesta",
  "archivo_ilegible",
];

test("cada texto existe en los tres idiomas y ninguno viene vacio", () => {
  for (const clave of CLAVES) {
    const vistos = new Set();
    for (const idioma of IDIOMAS) {
      const t = texto(clave, idioma);
      assert.ok(t && t.trim().length > 0, `${clave}/${idioma} vacio`);
      vistos.add(t);
    }
    // Tres traducciones distintas: si dos coinciden es que falto traducir.
    assert.equal(vistos.size, 3, `${clave} tiene traducciones repetidas`);
  }
});

test("el aviso largo pide paciencia y desalienta repetir la pregunta", () => {
  // Reenviar es lo peor que puede hacer quien espera: el mensaje nuevo
  // cancela el trabajo ya hecho y la espera vuelve a empezar. Medido: un
  // reporte de un anio de Coppel tarda ~290s y uno de toda la historia ~440s.
  assert.match(texto("espera_larga", "es"), /paciencia/i);
  assert.match(texto("espera_larga", "es"), /no hace falta que repitas/i);
  assert.match(texto("espera_larga", "en"), /bear with me/i);
  assert.match(texto("espera_larga", "en"), /no need to resend/i);
});

test("el aviso de lento NO sugiere acotar por fechas", () => {
  // Esta medido que no ayuda: contra la vista consolidada, un SUM de un solo
  // mes tarda lo mismo que el de un anio. Lo que si ayuda es una sola cadena.
  for (const idioma of IDIOMAS) {
    const t = texto("demasiado_lenta", idioma);
    assert.doesNotMatch(t, /fecha|date|日期/i);
  }
  assert.match(texto("demasiado_lenta", "es"), /una sola cadena/i);
});

test("un idioma que no se conoce cae al de por defecto", () => {
  assert.equal(texto("espera", "pt"), texto("espera", IDIOMA_POR_DEFECTO));
  assert.equal(texto("espera_larga", undefined), texto("espera_larga", "es"));
});

test("una clave que no existe lanza en vez de devolver undefined", () => {
  // Un undefined viajaria hasta el chat como "undefined".
  assert.throws(() => texto("no_existe", "es"), /texto desconocido/);
});

test("el aviso de archivo grande lleva el limite que se le pasa", () => {
  assert.match(textoArchivoMuyGrande(100, "es"), /100 MB/);
  assert.match(textoArchivoMuyGrande(16, "en"), /16 MB/);
  assert.equal(textoArchivoMuyGrande(100, "pt"), textoArchivoMuyGrande(100, "es"));
});
