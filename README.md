# Bot de WhatsApp para consultas SQL — GTM Honor Mexico

Permite consultar la base de datos en lenguaje natural desde WhatsApp y
recibir la respuesta como texto o como archivo Excel.

## Estado

**Nunca se ha ejecutado contra WhatsApp ni contra SQL Server reales.** Todo
esta probado por partes con dobles de prueba: 153 pruebas del lado Python y
54 del lado Node. La verificacion de punta a punta esta pendiente y sus pasos
estan en `whatsapp/README.md`.

## Como funciona

    WhatsApp
       |
       v
    [Node: whatsapp/]        Baileys: QR, reconexion, envio de documentos
       |  HTTP en localhost
       v
    [Python: sql_agent/]     FastAPI + LangChain (OpenRouter)
       |
       v
    SQL Server (solo lectura)  +  Excel

El sidecar de Node no sabe nada de SQL ni de modelos de lenguaje. El servicio
Python no sabe nada de WhatsApp. Todo el acoplamiento vive en un endpoint HTTP,
asi que migrar a la API oficial de Meta seria reescribir solo el sidecar.

## Componentes

| Ruta | Responsabilidad |
|---|---|
| `whatsapp/index.js` | Conexion con WhatsApp: QR, reconexion con backoff, presencia, envio de documentos |
| `whatsapp/agentClient.js` | Cliente HTTP del servicio Python; traduce los codigos de estado a mensajes |
| `whatsapp/chunk.js` | Trocea respuestas largas respetando parrafos, lineas y emojis |
| `whatsapp/dedupe.js` | Evita atender dos veces el mismo mensaje tras una reconexion |
| `whatsapp/backoff.js` | Espera exponencial entre reintentos de conexion |
| `sql_agent/api.py` | Servicio HTTP: allowlist, una consulta por conversacion, timeout |
| `sql_agent/session.py` | Una sesion con memoria propia por conversacion, con expiracion |
| `sql_agent/config.py` | Lectura y validacion de configuracion; normaliza los JID de WhatsApp |
| `sql_agent/agent.py` | Agente Text-to-SQL y el filtro de solo-lectura |
| `sql_agent/formatting.py` | Decide si el resultado va como texto o como Excel |
| `sql_agent/main.py` | CLI interactiva, alternativa a WhatsApp |

## Seguridad

Tres controles, y conviene entender que hace cada uno:

1. **Allowlist de numeros** (`WHATSAPP_ALLOWED_NUMBERS`). Sin ella el servicio
   no arranca. Un numero que no este en la lista recibe silencio total: ni
   respuesta ni indicador de "escribiendo", porque cualquier senal confirmaria
   que hay un bot del otro lado. Los grupos se ignoran siempre.

2. **Login de SQL Server con permisos `db_datareader` y nada mas.** Esta es la
   garantia real de solo-lectura, y es configuracion, no codigo: el `DB_URI`
   debe apuntar a un usuario que no pueda escribir.

3. **Filtro `es_select()`** (`sql_agent/agent.py`), defensa en profundidad.
   Tokeniza con `sqlparse` y exige que toda palabra clave de nivel de sentencia
   este en una lista blanca. Rechaza lotes pegados, DML tras un CTE, comandos
   reservados de T-SQL y la familia OPENQUERY. No sustituye al punto 2.

## Advertencia sobre Baileys

El sidecar usa Baileys, que se conecta haciendose pasar por WhatsApp Web. Eso
va **contra los terminos de servicio de WhatsApp** y el riesgo concreto es que
bloqueen el numero. Usa un chip dedicado, nunca el personal ni el principal de
la empresa.

## Arranque

Ver `whatsapp/README.md`. Resumen: copiar los dos `.env.example` a `.env`,
llenarlos, levantar `sql_agent/api.py` y despues `whatsapp/` con `npm start`,
y escanear el QR.

Ningun `.env` con valores reales se versiona.

## Pruebas

    cd sql_agent && venv/bin/python -m pytest tests/ -v
    cd whatsapp && npm test
