import test from "node:test";
import assert from "node:assert/strict";

import { trocear } from "../chunk.js";

// ═══════════════════════════════════════════════════════════════════════════
// PRUEBAS BÁSICAS
// ═══════════════════════════════════════════════════════════════════════════

test("un texto corto sale en un solo fragmento", () => {
  assert.deepEqual(trocear("hola"), ["hola"]);
});

test("un texto vacio no produce fragmentos", () => {
  assert.deepEqual(trocear("   "), []);
});

test("corta en el limite dado", () => {
  const partes = trocear("a".repeat(250), 100);
  assert.equal(partes.length, 3);
  assert.ok(partes.every((p) => p.length <= 100));
  assert.equal(partes.join(""), "a".repeat(250));
});

test("prefiere cortar en parrafos", () => {
  const texto = ["uno".repeat(10), "dos".repeat(10), "tres".repeat(10)].join("\n\n");
  const partes = trocear(texto, 70);
  assert.ok(partes.length > 1);
  assert.ok(partes.every((p) => !p.startsWith("\n")));
  assert.ok(partes.every((p) => p.length <= 70));
});

test("si no hay parrafos corta en saltos de linea simples", () => {
  const texto = Array.from({ length: 10 }, (_, i) => `${i}. fila con datos`).join("\n");
  const partes = trocear(texto, 40);
  assert.ok(partes.length > 1);
  assert.ok(partes.every((p) => p.length <= 40));
});

test("una sola palabra mas larga que el limite se parte en duro", () => {
  const partes = trocear("x".repeat(50), 20);
  assert.equal(partes.length, 3);
  assert.equal(partes.join(""), "x".repeat(50));
});

// ═══════════════════════════════════════════════════════════════════════════
// PRUEBAS DE CONSERVACIÓN DE CONTENIDO
// ═══════════════════════════════════════════════════════════════════════════

test("conserva todos los caracteres (propiedad exacta)", () => {
  // Garantía: partes.join("") contiene todos los caracteres del texto normalizado,
  // en el mismo orden. Excepción: saltos de línea que se consumen al trocear en duro.
  // En este test, inputs simples que no disparan corte duro deben reconstruir exactamente.

  // Caso 1: Líneas cortas sin corte duro
  const texto1 = "linea1\nlinea2\nlinea3";
  const partes1 = trocear(texto1, 50);
  assert.equal(partes1.join(""), texto1, "Caso 1: líneas cortas sin corte duro");

  // Caso 2: Con emoji, sin corte duro
  const texto2 = "hola 😀 mundo";
  const partes2 = trocear(texto2, 50);
  assert.equal(partes2.join(""), texto2, "Caso 2: con emoji, sin corte duro");

  // Caso 3: Párrafos, sin corte duro
  const texto3 = "parr1\n\nparr2\n\nparr3";
  const partes3 = trocear(texto3, 50);
  assert.equal(partes3.join(""), texto3, "Caso 3: párrafos sin corte duro");

  // Caso 4: Corte duro crítico (30 emojis, límite 21)
  // Esto ejercita el push final de partirEnDuro: sin él, faltan emojis.
  const texto4 = "😀".repeat(30);
  const partes4 = trocear(texto4, 21);
  assert.equal(
    partes4.join(""),
    texto4,
    "Caso 4: 30 emojis con límite 21 (el push final importa)"
  );
});

// ═══════════════════════════════════════════════════════════════════════════
// PRUEBAS DE JERARQUÍA PÁRRAFO > LÍNEA
// ═══════════════════════════════════════════════════════════════════════════

test("respeta límites de párrafo cuando hay espacio", () => {
  const texto = "1234567890\n\nabcdefgh\nijklmnop";
  const partes = trocear(texto, 20);
  // Primer párrafo solo: "1234567890" cabe sólo
  // Segundo párrafo: "abcdefgh\nijklmnop" no cabe junto, pero líneas sí
  // Esperado: primer fragmento es el párrafo 1, luego líneas del párrafo 2
  assert.ok(partes[0].includes("1234567890"));
  assert.ok(partes.every((p) => p.length <= 20));
});

test("parte párrafo solo cuando su contenido no cabe de otra forma", () => {
  // Un párrafo muy largo que necesita corte duro
  const parrafo1 = "x".repeat(30);
  const parrafo2 = "y".repeat(30);
  const texto = parrafo1 + "\n\n" + parrafo2;
  const partes = trocear(texto, 25);
  // Ambos párrafos son mayores que 25, así que serán partidos
  assert.ok(partes.every((p) => p.length <= 25));
  assert.ok(partes.length >= 2);
});

// ═══════════════════════════════════════════════════════════════════════════
// PRUEBAS DE NORMALIZACIÓN Y LIMPIEZA
// ═══════════════════════════════════════════════════════════════════════════

test("normaliza \\r\\n a \\n", () => {
  const texto = "linea1\r\nlinea2\r\nlinea3";
  const partes = trocear(texto, 50);
  assert.ok(!partes.some((p) => p.includes("\r")));
  // Los saltos normalizados están presentes
  assert.ok(partes.join("").includes("\n"));
});

test("recorta espacios en blanco al final de fragmentos", () => {
  const texto = "hola   \n\nmundo";
  const partes = trocear(texto, 50);
  assert.ok(partes.every((p) => !p.endsWith(" ")));
});

// ═══════════════════════════════════════════════════════════════════════════
// PRUEBAS DE VALIDACIÓN DE LÍMITES
// ═══════════════════════════════════════════════════════════════════════════

test("lanza error si maximo < 1", () => {
  assert.throws(
    () => trocear("texto", 0),
    { message: /maximo debe ser >= 1/ }
  );
  assert.throws(
    () => trocear("texto", -5),
    { message: /maximo debe ser >= 1/ }
  );
});

test("texto exactamente del tamaño maximo", () => {
  const texto = "x".repeat(50);
  const partes = trocear(texto, 50);
  assert.deepEqual(partes, [texto]);
});

test("texto con maximo + 1 caracteres", () => {
  const texto = "x".repeat(51);
  const partes = trocear(texto, 50);
  assert.equal(partes.length, 2);
  assert.ok(partes.every((p) => p.length <= 50));
  assert.equal(partes.join(""), texto);
});

// ═══════════════════════════════════════════════════════════════════════════
// PRUEBAS DE CARACTERES ESPECIALES (EMOJIS Y SURROGATES)
// ═══════════════════════════════════════════════════════════════════════════

/**
 * Detecta si un string contiene un surrogate UTF-16 huérfano
 * (high sin low, o low sin high).
 */
function tieneSurrogateHuerfano(str) {
  for (let i = 0; i < str.length; i++) {
    const code = str.charCodeAt(i);
    const esHigh = code >= 0xd800 && code <= 0xdbff;
    const esLow = code >= 0xdc00 && code <= 0xdfff;

    if (esHigh) {
      // Debe tener un low surrogate después
      if (i + 1 >= str.length) return true;
      const nextCode = str.charCodeAt(i + 1);
      if (!(nextCode >= 0xdc00 && nextCode <= 0xdfff)) return true;
      i++; // Saltar el low surrogate
    } else if (esLow) {
      // Un low surrogate sin high antes es un error
      return true;
    }
  }
  return false;
}

test("no rompe emojis en fragmentos", () => {
  const emoji = "😀";
  const texto = emoji.repeat(30);
  const partes = trocear(texto, 21);

  // Ningún fragmento debe contener surrogates huérfanos
  assert.ok(partes.every((p) => !tieneSurrogateHuerfano(p)));

  // Todos los emojis deben estar presentes (sin corrupción)
  assert.equal(partes.join(""), texto);

  // Verificar que el troceado funcionó (hay múltiples fragmentos)
  assert.ok(partes.length > 1, "Se esperaban múltiples fragmentos para 30 emojis con límite 21");
});

test("caso específico de revisión: 30 emojis con límite 21", () => {
  const partes = trocear("😀".repeat(30), 21);

  // Verificar que no hay surrogates rotos
  for (const parte of partes) {
    assert.ok(!tieneSurrogateHuerfano(parte), `Surrogate huérfano en: ${JSON.stringify(parte)}`);
  }

  // Todos los caracteres presentes
  assert.equal(partes.join(""), "😀".repeat(30));

  // Cada fragmento <= 21 caracteres
  assert.ok(partes.every((p) => p.length <= 21));
});

test("mezcla de texto ASCII y emojis", () => {
  const texto = "Hola 😀 mundo 🌍 test 🚀";
  const partes = trocear(texto, 15);

  assert.ok(partes.every((p) => !tieneSurrogateHuerfano(p)));
  assert.equal(partes.join(""), texto);
  assert.ok(partes.every((p) => p.length <= 15));
});

test("emoji con modificadores (familias)", () => {
  // Algunos emojis complejos pueden usar ZWJ o modificadores
  const texto = "👨‍👩‍👧‍👦 familia"; // family emoji
  const partes = trocear(texto, 10);

  assert.ok(partes.every((p) => !tieneSurrogateHuerfano(p)));
  assert.ok(partes.every((p) => p.length <= 10));
});

// ═══════════════════════════════════════════════════════════════════════════
// PRUEBAS DE PROPIEDADES (CARACTERÍSTICAS GENERALES)
// ═══════════════════════════════════════════════════════════════════════════

test("ningún fragmento excede maximo (límite 10 con ASCII)", () => {
  const casos = [
    "hello",
    "hello world",
    "hola\nmundo",
    "a\n\nb",
    "x".repeat(50),
  ];
  for (const texto of casos) {
    const partes = trocear(texto, 10);
    assert.ok(partes.every((p) => p.length <= 10), `Fallo para: ${JSON.stringify(texto)}`);
  }
});

test("ningún fragmento excede maximo (límite 4 con emojis)", () => {
  // Con límite 4, podemos meter 2 emojis (cada uno es .length === 2)
  const casos = [
    "😀😀",           // 4 bytes exactos
    "😀😀😀",         // 6 bytes, debería ser [2+2=4, 2]
    "😀a",            // 3 bytes
    "😀😀😀😀😀",     // 10 bytes
  ];
  for (const texto of casos) {
    const partes = trocear(texto, 4);
    assert.ok(partes.every((p) => p.length <= 4), `Fallo para: ${JSON.stringify(texto)}`);
  }
});

test("ningún fragmento sale vacío", () => {
  const casos = [
    "hola",
    "hola mundo",
    "hola\nmundo",
    "hola\n\nmundo",
    "😀".repeat(30),
    "x".repeat(50),
    // Casos críticos que disparan líneas puro-espacios tras corte duro
    "line1\n    \nline2",       // Línea solo espacios entre líneas normales
    "abc\n\t\ndef",            // Línea solo tabs
    "x".repeat(20) + "\n  \n" + "y".repeat(20),  // Línea espacios entre líneas largas
  ];
  for (const texto of casos) {
    const partes = trocear(texto, 4);  // Límite pequeño para forzar corte duro
    assert.ok(
      partes.every((p) => p.length > 0),
      `Fragmento vacío en: ${JSON.stringify(texto)} -> ${JSON.stringify(partes)}`
    );
  }
});

test("todos los fragmentos no contienen surrogates huérfanos", () => {
  const casos = [
    "😀".repeat(100),
    "hola 😀 mundo 🌍",
    "😀\nmundo\n😀",
    "👨‍👩‍👧‍👦 familia",
  ];
  for (const texto of casos) {
    const partes = trocear(texto, 10);
    for (const parte of partes) {
      assert.ok(
        !tieneSurrogateHuerfano(parte),
        `Surrogate huérfano en: ${JSON.stringify(parte)}`
      );
    }
  }
});

// ═══════════════════════════════════════════════════════════════════════════
// PRUEBA DE PROPIEDAD: CONSERVACIÓN CON GENERADOR ALEATORIO (SEMILLA FIJA)
// ═══════════════════════════════════════════════════════════════════════════

/**
 * Generador pseudoaleatorio con semilla fija para reproducibilidad.
 * Usa algoritmo LCG simple.
 */
class SeededRandom {
  constructor(seed) {
    this.seed = seed;
  }

  next() {
    this.seed = (this.seed * 1103515245 + 12345) & 0x7fffffff;
    return this.seed / 0x7fffffff;
  }

  choose(arr) {
    return arr[Math.floor(this.next() * arr.length)];
  }
}

/**
 * Genera un texto aleatorio combinando piezas que ejercitan diferentes ramas.
 * Garantiza múltiples párrafos que no caben juntos, párrafos multiline, etc.
 * NO produce múltiples \n\n seguidos (esos se pierden en split("\n\n") de todas formas).
 */
function generarTextoPrueba(rng, maximo) {
  const piezas = [
    // ASCII corto
    () => "abc",
    () => "hola",
    () => "test123",
    // Palabras largas que pueden no caber
    () => "x".repeat(Math.floor(rng.next() * 50) + 10),
    () => "y".repeat(Math.floor(rng.next() * 50) + 10),
    // Emojis simples
    () => "😀",
    () => "🎉",
    () => "❤️",
    // Emoji con ZWJ (familia)
    () => "👨‍👩‍👧‍👦",
    () => "👩‍💻",
    // Saltos individuales y párrafos (NO múltiples dobles seguidos)
    () => "\n",
    () => "\n\n",
    // Combinaciones: palabras + emoji
    () => "hola 😀 mundo",
    () => "test 🎉 end",
  ];

  const partes = [];
  const numPiezas = Math.floor(rng.next() * 15) + 5; // 5-20 piezas
  let ultimaFueParrafo = false;

  for (let i = 0; i < numPiezas; i++) {
    const pieza = rng.choose(piezas)();

    // Evitar múltiples \n\n seguidos
    if (pieza === "\n\n" && ultimaFueParrafo) {
      partes.push("\n"); // Reemplazar con salto simple
      ultimaFueParrafo = false;
    } else {
      partes.push(pieza);
      ultimaFueParrafo = pieza === "\n\n";
    }
  }

  return partes.join("");
}

test("conservación de contenido (propiedad con generador aleatorio, semilla fija)", () => {
  const rng = new SeededRandom(42); // Semilla fija para reproducibilidad
  const numCasos = 300;
  let casosConMultiplesPárrafos = 0;
  let casosConParrafosNoEncajados = 0;
  let casosConLíneasLargas = 0;

  for (let caseIdx = 0; caseIdx < numCasos; caseIdx++) {
    const maximo = Math.floor(rng.next() * 30) + 1; // 1-30
    const texto = generarTextoPrueba(rng, maximo);

    // Normalizar el texto como lo hace trocear
    const textoNorm = texto.replace(/\r\n/g, "\n");

    // Si está vacío, saltamos
    if (!textoNorm.trim()) {
      continue;
    }

    // Trocear
    const partes = trocear(texto, maximo);

    // Contar ramas ejercitadas
    if (textoNorm.includes("\n\n")) {
      casosConMultiplesPárrafos++;
    }
    if (
      textoNorm.includes("\n\n") &&
      textoNorm.split("\n\n").some((p) => p.length > maximo)
    ) {
      casosConParrafosNoEncajados++;
    }
    if (textoNorm.split("\n").some((l) => l.length > maximo)) {
      casosConLíneasLargas++;
    }

    // Verificar conservación de contenido
    // La garantía real es: partes.join("") contiene todos los caracteres no-salto
    // del texto, en orden, sin perder ni duplicar. Los saltos al final de fragmentos
    // se pierden (se recortan) y los saltos entre fragmentos del corte duro se pierden.
    const concatenado = partes.join("");

    // Verificación 1: Ningún fragmento termina en salto
    assert.ok(
      partes.every((p) => !/[\n\r]$/.test(p)),
      `Fragmento termina en salto en caso ${caseIdx}: maximo=${maximo}`
    );

    // Verificación 2: Todos los caracteres no-salto están presentes en orden
    const origNoSaltos = textoNorm.replace(/[\n\r]/g, "");
    const concatNoSaltos = concatenado.replace(/[\n\r]/g, "");
    assert.equal(
      concatNoSaltos,
      origNoSaltos,
      `Pérdida de caracteres en caso ${caseIdx}: ` +
        `maximo=${maximo}, ` +
        `texto_orig=${JSON.stringify(texto.slice(0, 50))}, ` +
        `partes_count=${partes.length}`
    );

    // Verificación 3: Saltos simples DENTRO de líneas empaquetadas se preservan
    // (Esto es implícito en la verificación anterior pero lo documentamos)
    const hasInternalNewlines = textoNorm.includes("\n") && !textoNorm.startsWith("\n");
    if (hasInternalNewlines) {
      // Verificar que hay ALGUNOS saltos en la concatenación
      assert.ok(
        concatenado.includes("\n") || partes.length > 1,
        `Saltos internos se perdieron completamente en caso ${caseIdx}`
      );
    }

    // Verificar que no hay fragmentos vacíos
    assert.ok(
      partes.every((p) => p.length > 0),
      `Fragmento vacío en caso ${caseIdx}: maximo=${maximo}`
    );
  }

  // Verificar que el generador ejercitó las ramas críticas
  assert.ok(
    casosConMultiplesPárrafos > 0,
    "Generador no creó textos con múltiples párrafos"
  );
  assert.ok(
    casosConParrafosNoEncajados > 0,
    "Generador no creó textos donde párrafos no encajan juntos (rama crítica)"
  );
  assert.ok(
    casosConLíneasLargas > 0,
    "Generador no creó textos con líneas largas (rama de corte duro)"
  );
});
