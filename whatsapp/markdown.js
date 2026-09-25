// El modelo escribe markdown y WhatsApp no lo entiende. Medido sobre 22
// respuestas reales del bot: 11 traian **negrita** y 3 un enlace
// [texto](url). En el telefono eso no se ve en negrita -- se ven los
// asteriscos, literales, en medio de cada nombre de modelo:
//
//   1. **SAMSUNG A07** - 334,285 unidades
//
// WhatsApp tiene su propio formato, mas pobre: *negrita*, _cursiva_,
// ~tachado~ y ```monoespaciado```. Esto traduce de uno al otro. Lo que no
// tiene equivalente (los enlaces) se desarma para que al menos se lea, y la
// URL queda suelta, que es como WhatsApp la vuelve tocable.

// **negrita** -> *negrita*. No cruza lineas: un asterisco doble suelto en un
// parrafo y otro tres parrafos mas abajo no son una negrita.
const NEGRITA = /\*\*([^\n*][^\n]*?)\*\*/g;
// __negrita__ -> *negrita*, que es lo que significa en markdown.
const NEGRITA_BAJA = /__([^\n_][^\n]*?)__/g;
// ### Titulo -> *Titulo*. WhatsApp no tiene encabezados.
const ENCABEZADO = /^#{1,6}\s+(.+?)\s*$/gm;
// [texto](https://...) -> texto: https://...
const ENLACE = /\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g;
// [texto](lo que sea) -> texto. Un destino que no es http no se puede abrir;
// el servicio ya quita los que el modelo se inventa, esto es el cinturon.
const ENLACE_SIN_DESTINO = /\[([^\]\n]+)\]\((?!https?:\/\/)[^)\n]*\)/g;

/**
 * Traduce el markdown del modelo al formato que WhatsApp si entiende.
 *
 * Deja en paz lo que ya es valido: un `*` suelto (el `SELECT *` de una
 * explicacion, una vineta) no se toca, porque la negrita exige el par.
 */
export function aFormatoWhatsApp(texto) {
  if (!texto) return texto;
  return texto
    .replace(ENCABEZADO, "*$1*")
    .replace(NEGRITA, "*$1*")
    .replace(NEGRITA_BAJA, "*$1*")
    .replace(ENLACE, "$1: $2")
    .replace(ENLACE_SIN_DESTINO, "$1");
}
