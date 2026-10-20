import { test } from "node:test";
import assert from "node:assert/strict";
import http from "node:http";

import { entregarPendientes } from "../entregas.js";

// Un servicio de mentira: GET /entregas da `entregas`; guarda los POST.
async function servicio(entregas) {
  const avisos = [];
  const servidor = http.createServer((pedido, respuesta) => {
    let cuerpo = "";
    pedido.on("data", (trozo) => (cuerpo += trozo));
    pedido.on("end", () => {
      if (pedido.method === "POST") avisos.push([pedido.url, JSON.parse(cuerpo)]);
      respuesta.setHeader("Content-Type", "application/json");
      respuesta.end(JSON.stringify(pedido.method === "GET" ? { entregas } : {}));
    });
  });
  await new Promise((listo) => servidor.listen(0, "127.0.0.1", listo));
  return { url: `http://127.0.0.1:${servidor.address().port}`, avisos, cerrar: () => servidor.close() };
}

const ENTREGA = {
  id: "e1", chat_id: "5215512345678@s.whatsapp.net", turno: "20261019-172727-abc",
  idioma: "es", texto: "Listo, aqui esta el reporte", archivos: ["C:/x/reporte_Coppel.xlsx", "C:/x/reporte_Sears.xlsx"],
};

test("manda el texto y cada archivo al chat de quien lo pidio, los registra y confirma", async () => {
  const falso = await servicio([ENTREGA]);
  const mandado = [];
  let n = 0;
  try {
    const mandadas = await entregarPendientes(falso.url, {
      jidDe: (telefono) => (telefono === ENTREGA.chat_id ? "88382122250279@lid" : undefined),
      enviarTexto: async (jid, texto) => { mandado.push([jid, texto]); return [`m${++n}`]; },
      enviarArchivo: async (jid, ruta, idioma) => { mandado.push([jid, ruta, idioma]); return [`m${++n}`]; },
    });
    assert.equal(mandadas, 1);
    assert.deepEqual(mandado.map((m) => m[0]), ["88382122250279@lid", "88382122250279@lid", "88382122250279@lid"]);
    assert.deepEqual(falso.avisos, [
      ["/enviados", { chat_id: ENTREGA.chat_id, turno: ENTREGA.turno, mensajes: ["m1", "m2", "m3"],
                      partes: { m2: ENTREGA.archivos[0], m3: ENTREGA.archivos[1] } }],
      ["/entregas/confirmar", { id: "e1" }],
    ]);
  } finally {
    falso.cerrar();
  }
});

test("si falla a la mitad no se confirma: el servicio la vuelve a dar", async () => {
  const falso = await servicio([ENTREGA]);
  try {
    const mandadas = await entregarPendientes(falso.url, {
      jidDe: () => undefined,
      enviarTexto: async () => ["m1"],
      enviarArchivo: async () => { throw new Error("socket cerrado"); },
    });
    assert.equal(mandadas, 0);
    assert.deepEqual(falso.avisos, []);
  } finally {
    falso.cerrar();
  }
});

test("sin servicio no hace nada", async () => {
  const mandadas = await entregarPendientes("http://127.0.0.1:1", {
    jidDe: () => undefined, enviarTexto: async () => assert.fail(), enviarArchivo: async () => assert.fail(),
  });
  assert.equal(mandadas, 0);
});
