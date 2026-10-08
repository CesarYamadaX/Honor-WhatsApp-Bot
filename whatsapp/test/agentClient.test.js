import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";

import { autorizado, idiomaDelChat, preguntar, reaccionar, registrarEnviados } from "../agentClient.js";
import { texto } from "../textos.js";

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
  const s = await servidorFalso(
    responder(200, { reply: "listo", files: ["/tmp/a.xlsx"], turno: "20261005-120000-abc123" }),
  );
  try {
    const r = await preguntar(s.url, "chat@s.whatsapp.net", "hola");
    // El turno viaja para poder calificar la respuesta con 👍/👎.
    assert.deepEqual(r, {
      reply: "listo",
      files: ["/tmp/a.xlsx"],
      idioma: "es",
      turno: "20261005-120000-abc123",
    });
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

test("el idioma de la respuesta viaja al sidecar", async () => {
  // Lo que el sidecar agrega por su cuenta despues de la respuesta (avisos de
  // archivo, "no tengo respuesta") tiene que salir en el idioma en que acaba
  // de contestar el bot, no en el suyo.
  const s = await servidorFalso(responder(200, { reply: "done", files: [], idioma: "en" }));
  try {
    const r = await preguntar(s.url, "chat@s.whatsapp.net", "total sales");
    assert.equal(r.idioma, "en");
  } finally {
    await s.cerrar();
  }
});

test("los avisos del sidecar salen en el idioma del chat", async () => {
  // El 504 no lo escribe el modelo sino el sidecar, asi que hay que
  // traducirlo. El idioma no puede venir en la respuesta -- justamente no hay
  // respuesta --, se pregunta a /idioma, que es donde vive la deteccion.
  const s = await servidorFalso((req, res) => {
    res.writeHead(req.url === "/idioma" ? 200 : 504, { "Content-Type": "application/json" });
    res.end(JSON.stringify(req.url === "/idioma" ? { idioma: "en" } : { detail: "timeout" }));
  });
  try {
    const r = await preguntar(s.url, "chat@s.whatsapp.net", "sales by store");
    assert.equal(r.idioma, "en");
    assert.equal(r.reply, texto("demasiado_lenta", "en"));
    assert.ok(!r.reply.includes("cancele"));
  } finally {
    await s.cerrar();
  }
});

test("si no se puede saber el idioma, se responde en espanol", async () => {
  // El caso del servicio caido: /idioma tampoco contesta. Un fallback es
  // obligatorio, si no el usuario se queda sin ningun aviso.
  const r = await preguntar("http://127.0.0.1:1", "chat@s.whatsapp.net", "hola", 300);
  assert.equal(r.reply, texto("servicio_caido", "es"));
});

test("idiomaDelChat: manda solo el chat_id", async () => {
  // Mismo motivo que en /authorized: el texto del mensaje no cruza al
  // servicio por un detalle de presentacion.
  let recibido = null;
  const s = await servidorFalso((req, res) => {
    let cuerpo = "";
    req.on("data", (t) => (cuerpo += t));
    req.on("end", () => {
      recibido = JSON.parse(cuerpo);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ idioma: "zh" }));
    });
  });
  try {
    assert.equal(await idiomaDelChat(s.url, "chat@s.whatsapp.net"), "zh");
    assert.deepEqual(recibido, { chat_id: "chat@s.whatsapp.net" });
  } finally {
    await s.cerrar();
  }
});

test("los mensajes enviados y las reacciones van al servicio", async () => {
  const recibidos = [];
  const s = await servidorFalso((req, res) => {
    let datos = "";
    req.on("data", (trozo) => (datos += trozo));
    req.on("end", () => {
      recibidos.push({ ruta: req.url, cuerpo: JSON.parse(datos) });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ calificado: true }));
    });
  });
  try {
    await registrarEnviados(s.url, "chat", "t1", ["wamid-1", "wamid-2"], { "wamid-2": "C:/x/reporte.xlsx" });
    assert.deepEqual(await reaccionar(s.url, "chat", "wamid-2", "👎"), { calificado: true });
    assert.deepEqual(recibidos, [
      { ruta: "/enviados", cuerpo: { chat_id: "chat", turno: "t1", mensajes: ["wamid-1", "wamid-2"], partes: { "wamid-2": "C:/x/reporte.xlsx" } } },
      { ruta: "/reaccion", cuerpo: { chat_id: "chat", mensaje_id: "wamid-2", emoji: "👎" } },
    ]);
  } finally {
    await s.cerrar();
  }
});

test("si el servicio no contesta, calificar no truena", async () => {
  assert.equal(await reaccionar("http://127.0.0.1:1", "chat", "m", "👍"), null);
});
