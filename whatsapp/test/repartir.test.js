import assert from "node:assert/strict";
import { test } from "node:test";

import { atenderPorChat } from "../repartir.js";

function mensaje(chat, id) {
  return { key: { remoteJid: chat, id } };
}

// Un atender() de mentira: cada mensaje tarda lo que se le diga, y se anota
// cuando empieza y cuando termina.
function registro(tardanzas) {
  const eventos = [];
  const atender = async (m) => {
    eventos.push(`inicio ${m.key.id}`);
    await new Promise((r) => setTimeout(r, tardanzas[m.key.id] ?? 0));
    eventos.push(`fin ${m.key.id}`);
  };
  return { eventos, atender };
}

test("chats distintos no se esperan entre si", async () => {
  // Al reconectar llegan juntos: el de B no puede esperar la consulta de A.
  const { eventos, atender } = registro({ a1: 50, b1: 0 });
  await atenderPorChat([mensaje("A", "a1"), mensaje("B", "b1")], atender);
  assert.ok(eventos.indexOf("fin b1") < eventos.indexOf("fin a1"));
});

test("dentro de un chat se sigue en orden", async () => {
  // "ventas de Sears" y luego "y de Coppel?": la segunda necesita la primera.
  const { eventos, atender } = registro({ a1: 30, a2: 0 });
  await atenderPorChat([mensaje("A", "a1"), mensaje("A", "a2")], atender);
  assert.deepEqual(eventos, ["inicio a1", "fin a1", "inicio a2", "fin a2"]);
});

test("un mensaje que revienta no deja al resto sin atender", async () => {
  const atendidos = [];
  await atenderPorChat(
    [mensaje("A", "a1"), mensaje("A", "a2"), mensaje("B", "b1")],
    async (m) => {
      if (m.key.id === "a1") throw new Error("se cayo");
      atendidos.push(m.key.id);
    },
  );
  assert.deepEqual(atendidos.sort(), ["a2", "b1"]);
});

test("un lote vacio o sin chat no truena", async () => {
  await atenderPorChat(undefined, async () => {});
  const vistos = [];
  await atenderPorChat([{}, { key: {} }], async (m) => vistos.push(m));
  assert.equal(vistos.length, 2);
});
