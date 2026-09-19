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
| `iniciar.py` | Arranca los dos procesos en orden y los baja juntos |
| `apagar.py` | Apaga el bot desde otra terminal, WhatsApp primero |
| `sql_agent/usuarios.py` | Quien puede usar el bot y en que estado esta |
| `sql_agent/fotos.py` | Normaliza y guarda las fotos de los usuarios |
| `sql_agent/bitacora.py` | Que se pregunto, que contesto el bot y que hizo para contestarlo |
| `sql_agent/panel/` | Panel de administracion, servido por el mismo servicio |
| `panel.py` | Abre el panel como aplicacion, en su propia ventana |

## Panel de administracion

Se enciende con el bot. Para verlo:

    python panel.py

(o `panel.bat`, o `python iniciar.py --abrir` para que se abra al arrancar).
Se abre como **aplicacion**: ventana propia, sin barra de direcciones ni
pestañas, con su icono en la barra de tareas. Por dentro es la pagina que
sirve el mismo servicio en <http://127.0.0.1:8000/panel>, abierta en modo
aplicacion de Edge o Chrome; asi no hay que instalar ni mantener un
empaquetado aparte. Si no hay ninguno de los dos, cae al navegador normal.

Arriba a la izquierda hay dos interruptores: **tema** (claro/oscuro) e
**idioma** (ES/EN). Los dos recuerdan la eleccion. El tema arranca siguiendo
al sistema y solo deja de hacerlo cuando alguien elige. El idioma es el de la
pantalla, no el del bot: las preguntas y respuestas se muestran como se
escribieron, y en que idioma contesta el bot lo decide cada mensaje de
WhatsApp (ver `sql_agent/idioma.py`).

La disposicion es la de WhatsApp Web: la lista de conversaciones a la
izquierda, la conversacion a la derecha, con la pregunta del usuario y la
respuesta del bot como burbujas. Cada vista tiene su direccion (`#usuarios`,
`#actividad`, `#chat=<numero>`), asi que se puede guardar en favoritos la que
se use mas.

Tres vistas:

- **Ahora mismo** — que consulta esta corriendo, desde hace cuanto y en que
  paso va (que SQL lanzo, cuantas filas trajo). Una consulta pesada tarda
  minutos y sin esto se ve igual que un bot colgado. En reposo muestra las
  ultimas consultas atendidas.
- **Usuarios** — alta, suspension y baja, mas nombre y foto. Los cambios
  entran de inmediato, sin reiniciar. El nombre y la foto son para este lado:
  el bot sigue hablandole al numero, pero en la lista de conversaciones se
  reconoce a la persona sin memorizar diez digitos. Las fotos se recortan al
  cuadrado del centro y se reducen a 256 px (una de 17 KB queda en 3), y
  viven en `sql_agent/datos/fotos/`.
- **Conversaciones** — cada turno con la respuesta del bot y, desplegable,
  lo que hizo para construirla. Se guardan TODAS: la lista mira la historia
  completa, y dentro de una conversacion se traen 50 turnos a la vez con un
  boton para ver los anteriores. No se guarda lo que no llega al agente (un
  numero no autorizado, un "cancela", un mensaje que rebota con 429), y las
  preguntas y respuestas se recortan a 4000 caracteres: el texto entero ya
  viajo por WhatsApp. Un turno ocupa unos 330 bytes.

Los estados de un usuario:

| Estado | Puede usar el bot | Como se sale de ahi |
|---|---|---|
| activo | si | — |
| suspendido | no | Reactivar, cuando alguien lo decida |
| eliminado | no | no existe: si vuelve a escribir es un desconocido |

Hubo tambien un estado `bloqueado` y una suspension con fecha de regreso.
Se quitaron los dos: en el panel eran tres botones que hacian casi lo mismo
y no se distinguian de un vistazo. Los registros viejos que digan
`bloqueado` se leen como suspendidos.

Suspendido y eliminado se comportan igual de cara a quien escribe:
**silencio total**, como un numero que nunca estuvo. Contestarle "estas
suspendido" le confirmaria que hay un bot del otro lado, que es justo lo que
la allowlist evita. La diferencia es de este lado: al suspendido se le
conserva su historial y se le puede devolver el acceso con un clic.

El panel **solo funciona con el servicio atado a loopback**. El servicio no
tiene autenticacion propia, y el panel ademas da de alta usuarios y deja leer
conversaciones: si `API_HOST` no es `127.0.0.1`, las rutas del panel
responden 403 y el arranque lo avisa en el log. El bot sigue funcionando.

Los usuarios viven en `sql_agent/datos/usuarios.json` y las conversaciones en
`sql_agent/datos/bitacora/AAAA-MM-DD.jsonl`. Nada de eso se versiona. La
primera vez, el registro se siembra con `WHATSAPP_ALLOWED_NUMBERS` para que
estrenar el panel no deje a nadie fuera; de ahi en adelante manda el archivo,
y la semilla nunca revive a un bloqueado.

## Seguridad

Tres controles, y conviene entender que hace cada uno:

1. **Registro de usuarios** (`sql_agent/datos/usuarios.json`, sembrado de
   `WHATSAPP_ALLOWED_NUMBERS`, editable desde el panel). Sin la variable el
   servicio no arranca. Un numero que no este activo recibe silencio total: ni
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

Un solo comando levanta los dos procesos:

    python iniciar.py

(en Windows tambien sirve `iniciar.bat`, con doble clic). Arranca el servicio
Python, espera a que conteste y recien entonces arranca el sidecar; si uno se
cae, el otro se baja tambien. La primera vez imprime el QR para vincular
WhatsApp.

Para apagarlo: Ctrl+C en esa ventana, o desde cualquier otra terminal

    python apagar.py

(o `apagar.bat`), que baja primero WhatsApp y despues el servicio. Se guia por
los PID que anota `iniciar.py` en `.bot-encendido.json`, y comprueba que cada
proceso siga siendo el suyo antes de matarlo.

Antes hay que copiar `sql_agent/.env.example` a `.env` y llenarlo
(`DB_URI`, `OPENROUTER_API_KEY`, `WHATSAPP_ALLOWED_NUMBERS`), y correr
`npm install` en `whatsapp/`. Si falta algo, `iniciar.py` lo dice y no
arranca nada.

Para levantarlos por separado, ver `whatsapp/README.md`.

Ningun `.env` con valores reales se versiona.

## Pruebas

    cd sql_agent && venv/bin/python -m pytest tests/ -v
    cd whatsapp && npm test
