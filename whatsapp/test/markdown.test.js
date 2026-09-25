import assert from "node:assert/strict";
import { test } from "node:test";

import { aFormatoWhatsApp } from "../markdown.js";

test("la negrita del modelo se ve en negrita y no con asteriscos", () => {
  // El caso medido: 11 de 22 respuestas reales venian asi, y en el telefono
  // se leen los asteriscos en medio de cada nombre de modelo.
  assert.equal(
    aFormatoWhatsApp("1. **SAMSUNG A07** - 334,285 unidades"),
    "1. *SAMSUNG A07* - 334,285 unidades",
  );
});

test("varias negritas en el mismo parrafo", () => {
  assert.equal(
    aFormatoWhatsApp("**Septiembre**: 143,828 y **Octubre**: 139,354"),
    "*Septiembre*: 143,828 y *Octubre*: 139,354",
  );
});

test("__negrita__ tambien es negrita en markdown", () => {
  assert.equal(aFormatoWhatsApp("__HONOR X5C__"), "*HONOR X5C*");
});

test("un encabezado se convierte en negrita, que es lo mas cerca que hay", () => {
  assert.equal(
    aFormatoWhatsApp("### Ventas de agosto\nHONOR vendio 160,909."),
    "*Ventas de agosto*\nHONOR vendio 160,909.",
  );
});

test("un enlace deja la URL suelta, que es lo que WhatsApp vuelve tocable", () => {
  assert.equal(
    aFormatoWhatsApp("[Ver precio](https://www.coppel.com/x)"),
    "Ver precio: https://www.coppel.com/x",
  );
});

test("un enlace sin destino real se queda solo con su texto", () => {
  assert.equal(
    aFormatoWhatsApp("[Descargar Excel](sandbox:/tmp/reporte.xlsx)"),
    "Descargar Excel",
  );
});

test("un asterisco suelto no es negrita y no se toca", () => {
  for (const texto of [
    "La consulta era SELECT * FROM ventas",
    "* Elektra\n* Coppel",
    "El margen es del 5*",
  ]) {
    assert.equal(aFormatoWhatsApp(texto), texto);
  }
});

test("un par de asteriscos que cruza parrafos no es una negrita", () => {
  const texto = "Un ** suelto aqui.\n\nY otro ** tres lineas abajo.";
  assert.equal(aFormatoWhatsApp(texto), texto);
});

test("una respuesta sin markdown sale igual que entro", () => {
  const texto = "En agosto HONOR vendio 160,909 unidades.";
  assert.equal(aFormatoWhatsApp(texto), texto);
});

test("vacio y nulo no revientan", () => {
  assert.equal(aFormatoWhatsApp(""), "");
  assert.equal(aFormatoWhatsApp(null), null);
  assert.equal(aFormatoWhatsApp(undefined), undefined);
});
