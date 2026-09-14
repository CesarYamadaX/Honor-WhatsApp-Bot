// WhatsApp corta los mensajes cerca de los 4096 caracteres. Troceamos con
// margen, prefiriendo cortar donde el texto ya tenia una pausa.
const LIMITE_POR_DEFECTO = 4000;

/**
 * Recorta whitespace (espacios, tabs, saltos) al final de una cadena
 * y devuelve solo si queda contenido significativo.
 * Usado para evitar fragmentos vacíos tras recorte.
 */
function recortarYValidar(fragmento) {
  let recortado = fragmento;
  while (recortado.length > 0 && /[ \t\n\r]$/.test(recortado)) {
    recortado = recortado.slice(0, -1);
  }
  return recortado.length > 0 ? recortado : null;
}

/**
 * Parte un texto en duro, respetando límites de caracteres (code points)
 * para no romper emojis.
 *
 * Usa Array.from() que agrupa surrogates de UTF-16 correctamente.
 * Cada fragmento tiene .length <= maximo, excepto cuando un único
 * carácter ocupa más unidades UTF-16 que maximo: en ese caso se emite
 * completo, sin partirlo, permitiendo que exceda maximo.
 */
function partirEnDuro(texto, maximo) {
  const caracteres = Array.from(texto);
  const partes = [];
  let actual = "";

  for (const char of caracteres) {
    // Si el carácter actual cabe, lo añadimos
    if (actual.length + char.length <= maximo) {
      actual += char;
    } else {
      // Si no cabe y ya tenemos contenido, empujamos lo actual
      if (actual) {
        partes.push(actual);
      }
      // Si el carácter sigue siendo mayor que maximo, lo metemos de todas formas
      // (porque no podemos partir un carácter)
      actual = char;
    }
  }

  if (actual) {
    partes.push(actual);
  }

  return partes;
}

/**
 * Parte un texto en fragmentos enviables por WhatsApp.
 *
 * Jerarquía de corte:
 * 1. Preferentemente entre párrafos (separados por "\n\n")
 * 2. Luego entre líneas (separadas por "\n")
 * 3. Solo parte una línea a la mitad si ella sola excede el límite
 *
 * Respeta límites de caracteres (code points), no code units, para evitar
 * romper emojis y caracteres multibyte con surrogates.
 *
 * Normaliza \r\n a \n. Recorta whitespace (espacios, tabs, saltos) del final
 * de cada fragmento. No produce fragmentos vacíos.
 *
 * @param {string} texto - Texto a trocear
 * @param {number} maximo - Número máximo de bytes .length por fragmento.
 *                          Excepción: si un único carácter ocupa más unidades UTF-16
 *                          que maximo, se emite completo sin partirlo, permitiendo exceder.
 * @returns {string[]} Array de fragmentos, sin vacíos, cada uno con .length <= maximo
 *                    salvo el caso degenerado documentado arriba.
 */
export function trocear(texto, maximo = LIMITE_POR_DEFECTO) {
  // Validación: maximo debe ser >= 1
  if (maximo < 1) {
    throw new Error(`maximo debe ser >= 1 (recibido: ${maximo})`);
  }

  // Normalizar saltos de línea: \r\n -> \n
  texto = texto.replace(/\r\n/g, "\n");

  // Texto vacío o solo espacios
  if (!texto || !texto.trim()) {
    return [];
  }

  // Texto cabe en un fragmento
  if (texto.length <= maximo) {
    return [texto];
  }

  // Partir en párrafos (separados por \n\n)
  const parrafos = texto.split("\n\n");
  const fragmentos = [];
  let actual = "";

  const empujar = (fragmento) => {
    // Usar función helper que recorta whitespace y valida
    const valido = recortarYValidar(fragmento);
    if (valido) {
      fragmentos.push(valido);
    }
  };

  for (let i = 0; i < parrafos.length; i++) {
    const parrafo = parrafos[i];

    // Calcular el costo de agregar este párrafo
    const separador = actual ? "\n\n" : "";
    const costo = actual.length + separador.length + parrafo.length;

    if (costo <= maximo) {
      // El párrafo cabe junto
      actual = actual ? `${actual}\n\n${parrafo}` : parrafo;
      continue;
    }

    // El párrafo no cabe. Empujar lo actual y procesar el párrafo.
    empujar(actual);
    actual = "";

    // Empaquetar líneas dentro del párrafo
    const lineas = parrafo.split("\n");
    let actualLineas = "";

    for (let j = 0; j < lineas.length; j++) {
      const linea = lineas[j];
      const sep = actualLineas ? "\n" : "";
      const costoLinea = actualLineas.length + sep.length + linea.length;

      if (costoLinea <= maximo) {
        // La línea cabe
        actualLineas = actualLineas ? `${actualLineas}\n${linea}` : linea;
        continue;
      }

      // La línea no cabe. Usar la función helper que valida y recorta.
      empujar(actualLineas);
      actualLineas = "";

      // Si la línea es muy larga, partirla en duro
      if (linea.length > maximo) {
        const trozos = partirEnDuro(linea, maximo);
        // Todos menos el último van como fragmentos finales
        for (let k = 0; k < trozos.length - 1; k++) {
          fragmentos.push(trozos[k]);
        }
        // El último queda para posible empaquetamiento
        actualLineas = trozos[trozos.length - 1];
      } else {
        // La línea cabe sola
        actualLineas = linea;
      }
    }

    // Empaquetar lo que quedó de líneas
    if (actualLineas) {
      actual = actualLineas;
    }
  }

  // Empaquetar el último fragmento
  empujar(actual);

  return fragmentos;
}
