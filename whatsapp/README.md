# Sidecar de WhatsApp

Conecta WhatsApp con el agente SQL. Son **dos procesos** y hay que levantar
ambos, en este orden.

## Advertencia

Baileys se conecta haciendose pasar por WhatsApp Web, lo cual va contra los
terminos de servicio de WhatsApp. El riesgo concreto es que baneen el numero.
**Usa un chip dedicado**, no el numero personal ni el principal de la empresa.

## Requisitos

- Node 20 o superior (probado en 24.19).
- El venv de `sql_agent` instalado (`sql_agent/.venv`; ver "Instalacion" en el
  README de la raiz).
- Un login de SQL Server con permisos `db_datareader` y nada mas.

## Arquitectura del sidecar

Ademas de `index.js` (el proceso principal), el directorio tiene tres modulos
que `index.js` usa directamente:

- `agentClient.js` — llama a `POST /ask` del servicio Python y traduce sus
  codigos de estado (403, 429, 504, caido) a mensajes o a `null` (silencio).
- `chunk.js` — trocea una respuesta larga en varios mensajes de WhatsApp.
- `dedupe.js` — evita procesar el mismo mensaje dos veces si WhatsApp lo
  reentrega (por ejemplo, tras una reconexion a media respuesta).
- `repartir.js` — reparte un lote de mensajes: chats distintos en paralelo, los
  de un mismo chat en orden (al reconectar llegan varios juntos).
- `backoff.js` — calcula la espera entre reintentos de reconexion, con backoff
  exponencial y techo, para no martillar los servidores de WhatsApp.

## Arranque

Lo normal es levantar los dos de una, desde la raiz del repo:

```bash
python iniciar.py
```

Ctrl+C baja los dos, y `python apagar.py` hace lo mismo desde otra terminal.
Lo de abajo es el arranque manual, util para depurar uno sin el otro (ojo:
`apagar.py` no los encuentra, porque no quedan anotados en ningun lado).

Terminal 1 — el servicio Python:

```bash
cd sql_agent
cp .env.example .env   # y llenarlo con los valores reales
venv/bin/python api.py
```

Terminal 2 — el sidecar de WhatsApp:

```bash
cd whatsapp
npm install
npm start
```

(`.env.example` esta de referencia, pero `index.js` no carga dotenv: lee
`process.env` directo y todos sus valores tienen respaldo. Para cambiarlos hay
que exportarlos en el entorno, no escribirlos en un `.env`.)

La primera vez imprime un codigo QR en la terminal. Escanearlo desde
WhatsApp > Dispositivos vinculados. La sesion queda guardada en `auth_info/`
y no vuelve a pedir el QR.

`auth_info/` da acceso completo a la cuenta de WhatsApp. Esta en `.gitignore`;
no la copies ni la subas a ningun lado.

## Pruebas

```bash
cd sql_agent && venv/bin/python -m pytest tests/ -v
cd whatsapp && npm test
```

Hoy: 151 pruebas del lado Python, 54 del lado Node. Ambas suites son sobre
codigo aislado con dobles de prueba (fakes de la base de datos, del LLM, del
socket de WhatsApp) — ninguna de las dos toca una base SQL Server real ni un
WhatsApp real. Ver la seccion siguiente.

## Verificacion end-to-end (pendiente)

**Esto nunca se ha ejecutado.** Todo lo que se probo hasta ahora fue con
dobles de prueba: las 151 pruebas de `sql_agent` corren contra un engine de
SQLAlchemy con SQLite o mocks en vez de SQL Server, y un LLM simulado en vez
de OpenRouter; las 54 de `whatsapp` prueban `chunk.js`, `dedupe.js` y
`backoff.js` en aislamiento, mas `agentClient.js` (15 de esas 54, en
`test/agentClient.test.js`) contra un `fetch` simulado que cubre las ramas
403, 429, 504, timeout y respuestas malformadas — pero ninguna abre un socket
de Baileys de verdad ni manda un mensaje real. Lo que **no** se ha probado ni
una vez es:

- Que Baileys de verdad abra una sesion de WhatsApp y entregue un QR
  escaneable.
- Que `sql_agent` se conecte a un SQL Server real y ejecute una consulta.
- El viaje completo mensaje de WhatsApp → `index.js` → `agentClient.js` →
  `POST /ask` → `SQLAgent.responder()` → SQL Server → respuesta de vuelta.

La razon es la misma en ambos lados del repo: el SQL Server `DESKTOP-GOE584A`
no es alcanzable desde la Mac de desarrollo (nombre NetBIOS que no resuelve
por DNS ni mDNS), y sin base de datos real no tiene sentido probar el flujo
completo aunque WhatsApp si estuviera disponible.

Cuando haya IP fija, la maquina este encendida en la misma red y el puerto
1433 este abierto, la prueba de humo es:

1. `curl http://127.0.0.1:8000/health` responde `{"status":"ok",...}`.
2. Desde un numero **en** la allowlist, preguntar algo de una tabla conocida.
   Debe llegar la respuesta en texto.
3. Pedir algo que devuelva mas de 15 filas. Debe llegar un archivo `.xlsx`.
4. Desde un numero **fuera** de la allowlist, escribir cualquier cosa.
   No debe llegar ninguna respuesta.
5. Pedir "borra la tabla X". El agente debe negarse, y aunque no lo hiciera,
   el login `db_datareader` lo impide.

Ademas, antes de dar por buena la verificacion, conviene forzar a mano al
menos un caso de cada rama de `agentClient.js` (servicio caido, timeout de
90s del lado Python, 429 por consulta en curso) para confirmar que el mensaje
que le llega al usuario es el esperado y no un error generico.
