import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";

import { autorizado, preguntar } from "../agentClient.js";

async function servidorFalso(manejador) {
  const servidor = http.createServer(manejador);
  await new Promise((resolve) => servidor.listen(0, "127.0.0.1", resolve));
  const { port } = servidor.address();
  return {
    url: `http://127.0.0.1:${port}`,
    cerrar: () => new Promise((resolve) => servidor.close(resolve)),
  };
}

function responder(codigo, cuerpo) {
  return (req, res) => {
    res.writeHead(codigo, { "Content-Type": "application/json" });
    res.end(JSON.stringify(cuerpo));
  };
}

// Para mandar un cuerpo crudo (no pasado por JSON.stringify), asi se puede
// simular texto que no es JSON valido, o un cuerpo vacio.
function responderCrudo(codigo, textoCrudo) {
  return (req, res) => {
    res.writeHead(codigo, { "Content-Type": "application/json" });
    res.end(textoCrudo);
  };
}

test("200 devuelve la respuesta y los archivos", async () => {
  const s = await servidorFalso(responder(200, { reply: "listo", files: ["/tmp/a.xlsx"] }));
  try {
    const r = await preguntar(s.url, "chat@s.whatsapp.net", "hola");
    assert.deepEqual(r, { reply: "listo", files: ["/tmp/a.xlsx"] });
  } finally {
    await s.cerrar();
  }
});

test("manda chat_id y text en el cuerpo", async () => {
  let recibido = null;
  const s = await servidorFalso((req, res) => {
    let datos = "";
    req.on("data", (c) => (datos += c));
    req.on("end", () => {
      recibido = JSON.parse(datos);
      responder(200, { reply: "ok", files: [] })(req, res);
    });
  });
  try {
    await preguntar(s.url, "chat@s.whatsapp.net", "ventas de agosto");
    assert.deepEqual(recibido, { chat_id: "chat@s.whatsapp.net", text: "ventas de agosto" });
  } finally {
    await s.cerrar();
  }
});

test("403 devuelve null: no se contesta nada", async () => {
  const s = await servidorFalso(responder(403, { detail: "No autorizado" }));
  try {
    assert.equal(await preguntar(s.url, "chat@s.whatsapp.net", "hola"), null);
  } finally {
    await s.cerrar();
  }
});

test("409 devuelve null: a esa pregunta la reemplazo otra", async () => {
  // La cancelo un mensaje posterior del mismo chat, y es ese el que va a
  // contestar. Mandar ademas un aviso por la vieja seria ruido.
  const s = await servidorFalso(responder(409, { detail: "Consulta cancelada" }));
  try {
    assert.equal(await preguntar(s.url, "chat@s.whatsapp.net", "hola"), null);
  } finally {
    await s.cerrar();
  }
});

test("autorizado: 200 es true y 403 es false", async () => {
  const ok = await servidorFalso(responder(200, { ok: true }));
  try {
    assert.equal(await autorizado(ok.url, "chat@s.whatsapp.net"), true);
  } finally {
    await ok.cerrar();
  }

  const no = await servidorFalso(responder(403, { detail: "No autorizado" }));
  try {
    assert.equal(await autorizado(no.url, "chat@s.whatsapp.net"), false);
  } finally {
    await no.cerrar();
  }
});

test("autorizado: si el servicio no contesta, se asume que NO", async () => {
  // Ante la duda, callar. Un false de mas solo ignora un mensaje; un true de
  // mas le muestra señales de vida a un numero ajeno.
  assert.equal(await autorizado("http://127.0.0.1:1", "chat@s.whatsapp.net", 300), false);
});

test("autorizado: manda el chat_id y no el texto del mensaje", async () => {
  let recibido = null;
  const s = await servidorFalso((req, res) => {
    let cuerpo = "";
    req.on("data", (t) => (cuerpo += t));
    req.on("end", () => {
      recibido = JSON.parse(cuerpo);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: true }));
    });
  });
  try {
    await autorizado(s.url, "chat@s.whatsapp.net");
    assert.deepEqual(recibido, { chat_id: "chat@s.whatsapp.net" });
  } finally {
    await s.cerrar();
  }
});

test("429 avisa que la consulta anterior sigue", async () => {
  const s = await servidorFalso(responder(429, { detail: "Consulta en curso" }));
  try {
    const r = await preguntar(s.url, "chat@s.whatsapp.net", "hola");
    assert.match(r.reply, /anterior/i);
    assert.deepEqual(r.files, []);
  } finally {
    await s.cerrar();
  }
});

test("504 se disculpa y sugiere acotar", async () => {
  const s = await servidorFalso(responder(504, { detail: "La consulta tardo demasiado" }));
  try {
    const r = await preguntar(s.url, "chat@s.whatsapp.net", "hola");
    assert.match(r.reply, /tard/i);
    assert.deepEqual(r.files, []);
  } finally {
    await s.cerrar();
  }
});

test("si el servicio no responde lo dice, no se queda callado", async () => {
  // Puerto cerrado a proposito.
  const r = await preguntar("http://127.0.0.1:1", "chat@s.whatsapp.net", "hola");
  assert.match(r.reply, /no est(a|á) disponible/i);
  assert.deepEqual(r.files, []);
});

test("un 500 inesperado tambien produce un mensaje", async () => {
  const s = await servidorFalso(responder(500, { detail: "boom" }));
  try {
    const r = await preguntar(s.url, "chat@s.whatsapp.net", "hola");
    assert.ok(r.reply.length > 0);
    assert.ok(!r.reply.includes("boom"));
  } finally {
    await s.cerrar();
  }
});

test("200 con cuerpo que no es JSON resuelve con un mensaje, no lanza", async () => {
  const s = await servidorFalso(responderCrudo(200, "esto no es json {{{"));
  try {
    const r = await preguntar(s.url, "chat@s.whatsapp.net", "hola");
    assert.equal(typeof r.reply, "string");
    assert.ok(r.reply.length > 0);
    assert.deepEqual(r.files, []);
  } finally {
    await s.cerrar();
  }
});

test("200 con cuerpo vacio resuelve con un mensaje, no lanza", async () => {
  const s = await servidorFalso(responderCrudo(200, ""));
  try {
    const r = await preguntar(s.url, "chat@s.whatsapp.net", "hola");
    assert.equal(typeof r.reply, "string");
    assert.ok(r.reply.length > 0);
    assert.deepEqual(r.files, []);
  } finally {
    await s.cerrar();
  }
});

test("200 con cuerpo JSON `null` resuelve con un mensaje, no lanza", async () => {
  const s = await servidorFalso(responderCrudo(200, "null"));
  try {
    const r = await preguntar(s.url, "chat@s.whatsapp.net", "hola");
    assert.equal(typeof r.reply, "string");
    assert.ok(r.reply.length > 0);
    assert.deepEqual(r.files, []);
  } finally {
    await s.cerrar();
  }
});

test("200 con files como cadena se descarta y devuelve []", async () => {
  const s = await servidorFalso(responder(200, { reply: "ok", files: "no-es-array" }));
  try {
    const r = await preguntar(s.url, "chat@s.whatsapp.net", "hola");
    assert.deepEqual(r.files, []);
  } finally {
    await s.cerrar();
  }
});

test("200 con files como numero se descarta y devuelve []", async () => {
  const s = await servidorFalso(responder(200, { reply: "ok", files: 5 }));
  try {
    const r = await preguntar(s.url, "chat@s.whatsapp.net", "hola");
    assert.deepEqual(r.files, []);
  } finally {
    await s.cerrar();
  }
});

test("200 sin reply devuelve un mensaje utilizable", async () => {
  const s = await servidorFalso(responder(200, { files: [] }));
  try {
    const r = await preguntar(s.url, "chat@s.whatsapp.net", "hola");
    assert.equal(typeof r.reply, "string");
    assert.ok(r.reply.length > 0);
  } finally {
    await s.cerrar();
  }
});

test("200 con reply que no es cadena devuelve un mensaje utilizable", async () => {
  const s = await servidorFalso(responder(200, { reply: 12345, files: [] }));
  try {
    const r = await preguntar(s.url, "chat@s.whatsapp.net", "hola");
    assert.equal(typeof r.reply, "string");
    assert.ok(r.reply.length > 0);
  } finally {
    await s.cerrar();
  }
});

test("si el servicio acepta la conexion y nunca responde, el timeout corta y avisa que no esta disponible", async () => {
  const s = await servidorFalso(() => {
    // Nunca se llama a res.end(): la conexion queda abierta a proposito.
  });
  try {
    const r = await preguntar(s.url, "chat@s.whatsapp.net", "hola", 50);
    assert.match(r.reply, /no est(a|á) disponible/i);
    assert.deepEqual(r.files, []);
  } finally {
    await s.cerrar();
  }
});
