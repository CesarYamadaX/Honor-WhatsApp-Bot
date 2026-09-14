# Bot de WhatsApp sobre sql_agent — Diseño

Fecha: 2026-09-12
Estado: aprobado, pendiente de plan de implementación

## Problema

`sql_agent/` es una CLI interactiva: un solo usuario, en una terminal, en la Mac
donde vive el código. El acceso a la base de datos está controlado por el hecho
de que hay que estar sentado frente a esa máquina.

Se quiere que el equipo consulte la base desde WhatsApp: preguntar en lenguaje
natural y recibir la respuesta, o un Excel, en el teléfono.

El cambio no es solo de interfaz. Pasar de una CLI de un usuario a un canal
multiusuario y remoto rompe tres supuestos del código actual:

1. Hay un único agente por proceso (`_db_instance` global, `agent.py:18`).
2. El control de acceso es físico; en WhatsApp cualquiera que sepa el número entra.
3. La respuesta se lee en una terminal ancha; un teléfono no tolera 200 filas de texto.

## Decisiones ya tomadas

| Decisión | Elección | Motivo |
|---|---|---|
| Proveedor WhatsApp | Baileys (no oficial) | Elección del usuario. Sin trámites ni costo por mensaje. |
| Base del agente | Reusar `sql_agent` | Ya tiene loop LLM (OpenRouter) y exportador a Excel. |
| Arquitectura | Sidecar Node + servicio Python | Cada lado usa la librería madura de su ecosistema. |

### Sobre Baileys

Baileys se conecta haciéndose pasar por WhatsApp Web. Esto **va contra los
términos de servicio de WhatsApp**: el riesgo concreto es el baneo del número.
Mitigación acordada: usar un chip dedicado, nunca el número personal ni el
principal de la empresa.

Se elige Baileys sobre whatsapp-web.js porque este último levanta un Chromium
con Puppeteer (cientos de MB de RAM, mucho más frágil), mientras que Baileys
habla el protocolo directamente.

## Arquitectura

    WhatsApp
       |
       v
    [Node: whatsapp/index.js]      QR, reconexión, envío de documentos
       |  HTTP localhost:8000
       v
    [Python: sql_agent/api.py]     FastAPI
       |
       v
    [sql_agent/session.py]         una AgentSession por chat_id
       |
       v
    SQL Server (solo lectura)  +  Excel

El sidecar Node no sabe nada de SQL ni de LLMs. El servicio Python no sabe nada
de WhatsApp. Todo el acoplamiento vive en un contrato HTTP de un endpoint, lo
que permite migrar después a la API oficial de Meta reescribiendo solo `index.js`.

### Estructura de archivos

    whatsapp/                 (nuevo, Node)
      index.js                  conexión, QR, listeners, envío de documentos
      package.json
      .gitignore                auth_info/, node_modules/, .env
      .env.example
    sql_agent/
      api.py                  (nuevo)  FastAPI, expone el agente por HTTP
      session.py              (nuevo)  registro de sesiones por chat_id
      formatting.py           (nuevo)  troceado y decisión texto-vs-Excel
      agent.py                (refactor) muere el global _db_instance
      excel_exporter.py       (sin cambios de fondo)
      .env.example            (actualizado)

## El contrato HTTP

Único punto de acoplamiento entre los dos procesos.

    POST http://localhost:8000/ask
    Content-Type: application/json

      { "chat_id": "5215512345678@s.whatsapp.net",
        "text": "ventas de agosto por tienda" }

    200 OK
      { "reply": "Las ventas de agosto fueron...",
        "files": ["/ruta/abs/reporte_20260912_101500.xlsx"] }

    403 Forbidden   — chat_id fuera de la allowlist
    429 Too Many Requests — ya hay una consulta en curso para ese chat_id
    504 Gateway Timeout   — el agente excedió AGENT_TIMEOUT_SECONDS

`reply` viaja como texto completo, sin trocear. El troceado lo hace el sidecar
Node al momento de enviar, porque el límite de 4096 caracteres es una
restricción del canal y no del agente.

`files` son rutas absolutas en el sistema de archivos local. Ambos procesos
corren en la misma máquina; esto es un supuesto explícito del diseño. Si algún
día se separan, `files` pasa a ser contenido en base64 o una URL firmada.

También se expone `GET /health` para que el sidecar distinga "Python caído" de
"Python lento".

## Autorización

### Allowlist

`WHATSAPP_ALLOWED_NUMBERS`: lista de números separados por coma, en formato
internacional sin `+` (ej. `5215512345678,5215587654321`). El servicio Python
normaliza el `chat_id` recibido (quita el sufijo `@s.whatsapp.net`) y compara.

- Mensaje de un número fuera de la lista: **se ignora en silencio**. Responder
  cualquier cosa, aunque sea un rechazo, confirma que el número existe y que hay
  un bot vivo del otro lado.
- Si `WHATSAPP_ALLOWED_NUMBERS` está vacía o ausente, el servicio **no arranca**.
  Un default permisivo aquí expone la base de datos de la empresa.
- Los mensajes de grupo (`chat_id` que termina en `@g.us`) se ignoran.

La validación vive en el lado Python, no en Node: es la frontera que protege el
recurso, y así sigue vigente aunque mañana se cambie de sidecar.

### Solo-lectura real

Hoy la regla de solo-lectura de `sql_agent` es únicamente texto en el prompt
(`agent.py:83-93`). El toolkit de `create_sql_agent` ejecuta el SQL que el
modelo le dé; un prompt no es un control de seguridad.

A diferencia del servidor MCP —que no puede usar un login restringido porque
`load_excel_to_table` necesita INSERT/TRUNCATE con el mismo login— `sql_agent`
solo lee. Por lo tanto:

**El `DB_URI` del bot debe apuntar a un login de SQL Server con `db_datareader`
y nada más.** Esto convierte el "solo SELECT" de una instrucción que el modelo
puede desobedecer en una garantía impuesta por el motor.

Es configuración, no código. Se documenta en `.env.example` y se deja como
requisito de despliegue. El prompt de solo-lectura se conserva como defensa en
profundidad, no como la defensa.

## Estado y concurrencia

### Sesiones

`_db_instance` global se reemplaza por un registro `chat_id -> AgentSession`.
Cada `AgentSession` tiene su propia memoria de conversación, de modo que
"ahora solo las de Telcel" funcione como seguimiento de la pregunta anterior.

El objeto `SQLDatabase` **se comparte** entre sesiones: su pool de conexiones de
SQLAlchemy es seguro entre hilos, y crear uno por chat multiplicaría las
conexiones a SQL Server sin ganar nada.

Las sesiones expiran a las `SESSION_TTL_MINUTES` (default 60) sin actividad,
para que la memoria no crezca sin límite.

De paso se elimina el uso de la API privada `db._engine` (`agent.py:34` y
`agent.py:131`), pasando el engine explícitamente a quien lo necesite.

### Concurrencia

Una consulta puede tardar 30s o más. El agente de LangChain es síncrono, así que
FastAPI lo ejecuta en un threadpool (`run_in_threadpool`) para no bloquear el
event loop.

**Una consulta en curso por chat.** Si llega un segundo mensaje del mismo chat
mientras el primero se procesa, se responde 429 y el sidecar avisa "sigo
trabajando en tu pregunta anterior". Así un usuario ansioso no dispara cinco
consultas paralelas contra la base.

Mientras espera, el sidecar manda el indicador de "escribiendo..."
(`sendPresenceUpdate('composing')`), renovándolo cada ~8s porque WhatsApp lo
expira solo.

## Respuestas y archivos

### Texto vs Excel

Una tabla de 200 filas en texto plano es ilegible en un teléfono. Regla:

- Resultado de **hasta 15 filas**: se manda como texto.
- **Más de 15 filas**: se genera el Excel y se manda como documento, con un
  resumen en texto ("Son 1,203 registros, te los mando en Excel").

El umbral es la constante `MAX_ROWS_AS_TEXT` en `formatting.py`.

### Troceado

WhatsApp corta cerca de los 4096 caracteres. El sidecar parte la respuesta en
fragmentos de `MAX_MESSAGE_CHARS` (4000, con margen), cortando preferentemente
en saltos de línea dobles, luego simples, y solo en duro si no hay dónde. Los
fragmentos se envían en orden con una pausa corta entre ellos.

### Envío de documentos

Los archivos de `files` se mandan con `sendMessage(jid, { document, fileName,
mimetype })`. Si el archivo no existe o supera el límite de WhatsApp (~100 MB,
pero en la práctica se acota a `MAX_FILE_MB`, default 16), se avisa en texto en
lugar de fallar en silencio.

## Manejo de errores

| Situación | Comportamiento |
|---|---|
| Agente excede `AGENT_TIMEOUT_SECONDS` (default 90) | 504; el sidecar responde una disculpa con sugerencia de acotar la pregunta |
| El servicio Python no responde | El sidecar responde que el sistema no está disponible; no se traga el mensaje en silencio |
| Error de SQL / de LLM dentro del agente | 200 con `reply` explicando el fallo en lenguaje llano; sin stack traces al usuario |
| WhatsApp se desconecta | Baileys reconecta solo, salvo `DisconnectReason.loggedOut` |
| Sesión cerrada desde el teléfono | Se borra `auth_info/` y se re-imprime el QR en la consola |
| Chat fuera de la allowlist | Silencio total |

Los errores se registran con el `chat_id` para poder diagnosticar, pero **sin el
texto de la pregunta ni los datos devueltos**, que pueden ser información
comercial sensible.

## Configuración

Variables nuevas, todas documentadas en `.env.example`. Ningún `.env` real se
commitea; `whatsapp/.gitignore` cubre `auth_info/`, `node_modules/` y `.env`.

    # sql_agent/.env
    DB_URI=...                        # login con db_datareader, ver "Solo-lectura real"
    OPENROUTER_API_KEY=...
    OPENROUTER_MODEL=...
    WHATSAPP_ALLOWED_NUMBERS=5215512345678,5215587654321
    AGENT_TIMEOUT_SECONDS=90
    SESSION_TTL_MINUTES=60
    API_HOST=127.0.0.1                # nunca 0.0.0.0: el servicio no tiene auth propia
    API_PORT=8000

    # whatsapp/.env
    AGENT_API_URL=http://127.0.0.1:8000
    MAX_FILE_MB=16

`API_HOST` se fija en loopback a propósito: el endpoint `/ask` no tiene
autenticación porque su única defensa es no ser alcanzable desde la red.

## Pruebas

El SQL Server (`DESKTOP-GOE584A`) no es alcanzable desde esta Mac: es un nombre
NetBIOS que no resuelve por DNS ni mDNS. **El end-to-end real queda bloqueado**
hasta que haya IP fija, máquina encendida en la misma red y el 1433 abierto.

Lo que sí se verifica sin la base:

| Qué | Cómo |
|---|---|
| Allowlist (permitido, denegado, grupo, lista vacía) | pytest contra `api.py` con `TestClient` |
| Ruteo por sesión y expiración por TTL | pytest sobre `session.py`, agente mockeado |
| Decisión texto-vs-Excel en el umbral | pytest sobre `formatting.py` |
| Troceado de mensajes largos | pruebas en el sidecar Node |
| Contrato HTTP, lado Python | `TestClient` + SQLite en memoria |
| Contrato HTTP, lado Node | stub del endpoint `/ask` |
| Una consulta por chat (429) | pytest con dos peticiones concurrentes |

Se entrega verificado por partes y explícitamente **no** probado contra la base
real. Cuando haya acceso, la prueba de humo es: preguntar algo de una tabla
conocida y pedir el mismo resultado en Excel.

## Fuera de alcance

- Migración a la API oficial de Meta (el diseño la deja barata, no la incluye).
- Autenticación por usuario más allá de la allowlist de números.
- Despliegue, supervisión de procesos y arranque automático.
- Tocar `mssql_mcp_server.py` o el catálogo de esquema.
- Limpiar `sql_agent/.env` del historial de git (el usuario lo declinó
  explícitamente). **Sigue pendiente rotar el `OPENROUTER_API_KEY` y el password
  de SQL Server**, que están en el commit 459672c ya pusheado a origin.
