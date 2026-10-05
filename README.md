# Bot de WhatsApp para consultas SQL — GTM Honor Mexico

Permite consultar la base de datos en lenguaje natural desde WhatsApp y
recibir la respuesta como texto o como archivo Excel.

## Estado

En uso contra WhatsApp y el SQL Server de GTM Honor Mexico desde septiembre
de 2026. Ademas tiene pruebas automaticas por partes, con dobles de prueba:
651 del lado Python y 96 del lado Node (ver "Pruebas", al final).

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
| `whatsapp/repartir.js` | Atiende en paralelo los mensajes de chats distintos que llegan juntos; los de un mismo chat, en orden |
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
| `sql_agent/registro_de_logs.py` | Los logs tecnicos en disco (uno por dia) y como los lee el panel |
| `sql_agent/actualizar_catalogo.py` | Relee el esquema de las bases y actualiza `catalogo.json` y `valores.json`; sin `--escribir` solo dice que cambio |
| `sql_agent/resumen_local.py` | Resumen de ventas en un SQLite local (unidades por cadena, marca, modelo, carrier, año, mes y semana), refrescado cada 3 horas; contesta en un segundo lo que en el servidor tarda minutos, y lo demas sigue yendo al servidor |
| `sql_agent/limpieza.py` | Borra los reportes de `exports/` con mas de 7 dias |
| `sql_agent/inventario.py` | Semanas de inventario de Telcel: stock de la foto mas reciente entre la venta semanal promedio de 4 semanas, de todos los canales, cruzado por SKU |
| `sql_agent/panel/` | Panel de administracion, servido por el mismo servicio |
| `panel.py` | Abre el panel como aplicacion, en su propia ventana |

## Panel de administracion

Se abre solo al arrancar el bot, como **aplicacion**: ventana propia, sin
barra de direcciones ni pestañas, con el logo del bot en la barra de tareas.
Si ya hay una ventana abierta no abre otra.

El icono son PNG dibujados aparte (`sql_agent/panel/hacer_iconos.py`, que
los regenera) y no el SVG: a 16 o 32 px, que es donde vive el icono, el
rasterizado del navegador saca el borde con escalones. Estos se dibujan ocho
veces mas grandes y se reducen con Lanczos.

La ventana usa un perfil propio en `sql_agent/datos/ventana`, y ahi el
navegador se guarda el icono. Si algun dia se cambia el logo y la ventana
sigue mostrando el anterior, hay que borrar esa carpeta: es desechable, se
vuelve a crear sola al abrir el panel. Para
abrirlo por separado (si se cerro la ventana, con el bot ya encendido):

    python panel.py

Y `python iniciar.py --sin-panel` arranca sin abrirlo, para las vueltas de
depuracion en las que el bot se reinicia cada minuto. Por dentro es la pagina que
sirve el mismo servicio en <http://127.0.0.1:8000/panel>, abierta en modo
aplicacion de Edge o Chrome; asi no hay que instalar ni mantener un
empaquetado aparte. Si no hay ninguno de los dos, cae al navegador normal.

Arriba a la izquierda hay dos interruptores: **tema** e **idioma** (ES/EN).
Los dos recuerdan la eleccion. El tema tiene tres modos y el boton los cicla:

- **automatico** (reloj, el de casa): oscuro de las 17:00 a las 07:00, hora
  de Mexico -- la del negocio, no la del equipo, asi que se oscurece cuando
  alla anochece aunque la maquina este en otro huso. El cambio entra solo al
  dar la hora, sin recargar.
- **siempre claro** (sol) y **siempre oscuro** (luna), que ignoran el reloj. El idioma es el de la
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
  **todo lo que penso e hizo**: cuando le pregunto al modelo y cuanto tardo,
  lo que el modelo escribio, que herramienta decidio usar y con que
  argumentos, el SQL, cuantas filas volvieron y que le devolvio cada
  herramienta. Lo va anotando un observador enganchado al agente
  (`Observador` en `agent.py`), asi que tambien se ve en vivo desde la vista
  de actividad mientras la consulta corre. Se guardan TODAS: la lista mira la historia
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

### Logs

La tercera pestaña del panel, **Logs**, muestra lo tecnico: la conexion con
WhatsApp, los errores del servicio y los avisos de arranque. Antes eso solo
se veia en la ventana de la terminal de `iniciar.py`, y cerrarla lo perdia.

- `iniciar.py` escribe todo lo que imprime en `sql_agent/datos/logs/`, un
  archivo por dia (`bot-AAAA-MM-DD.log`), y borra los de mas de 14 dias. No
  se guarda el ruido: el sondeo del panel cada 2 segundos y los `/health`.
- A la izquierda, los dias con cuantos errores y avisos tuvo cada uno; en la
  pestaña, los errores de hoy. Arriba de las lineas, el estado de WhatsApp
  ("conectado desde las 09:08").
- Se filtra por origen (WhatsApp, Servicio, Arranque), por "solo errores y
  avisos", y el buscador de la izquierda busca texto dentro del log.
- El log de hoy se actualiza solo mientras esta abierto.

Un **aviso** (ambar) es algo que el bot resuelve solo, como una desconexion
de WhatsApp que se reintenta. Un **error** (rojo) es algo que fallo: un
traceback, un 500, el sidecar que se cayo.

Solo hay logs si el bot se arranca con `iniciar.py`: si los dos procesos se
levantan a mano, no se escribe nada. Como el resto del panel, solo se ven
desde la propia maquina (los logs traen consultas SQL y numeros).

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

## Requisitos (antes de instalar)

Esto tiene que existir ANTES de instalar nada. Lo que se instala con
`pip`/`npm` esta en la seccion siguiente.

**Maquina**

- **Windows 10 u 11.** El bot usa `taskkill` para apagarse, abre el panel en
  una ventana de Microsoft Edge y arma las tablas dinamicas con Excel por COM
  (`pywin32`), asi que no corre tal cual en Linux ni en Mac.
- **Python 3.13** (probado en 3.13.15), desde python.org. Al instalarlo,
  marcar "Add python.exe to PATH".
- **Node.js 20 o superior** (probado en 24.19), desde nodejs.org. Trae `npm`.
- **ODBC Driver 18 for SQL Server**, de Microsoft. Es el que usa `DB_URI`;
  sin el, `pyodbc` no se puede conectar. Para ver si ya esta, en PowerShell:
  `Get-OdbcDriver | Where-Object Name -like '*SQL Server*'`.
- **Microsoft Excel** (opcional). Solo para la hoja `PIVOT` con la tabla
  dinamica de los reportes; sin Excel el reporte sale igual, sin esa hoja.
- **Git**, para clonar el repositorio.

**Accesos**

- **Red hacia el SQL Server** (`DESKTOP-GOE584A`): la maquina tiene que estar
  en la red de la oficina o en su VPN. Desde fuera el nombre ni siquiera
  resuelve y el bot contesta que no se puede conectar a la base.
- **Un login de SQL Server de SOLO lectura** (`db_datareader` y nada mas). El
  bot nunca escribe, y ese permiso es la unica garantia real: las reglas del
  prompt el modelo las puede desobedecer.
- **Salida a internet** hacia `web.whatsapp.com` (WhatsApp) y
  `openrouter.ai` (el modelo). Una VPN o un proxy que las bloquee se ve en el
  log como `desconectado (connectionLost, codigo 408)` en bucle.
- **Una API key de OpenRouter** con saldo (openrouter.ai, seccion Keys).
- **Un telefono con WhatsApp y un chip dedicado** para vincular el bot. No el
  numero personal ni el de la empresa: Baileys va contra los terminos de
  WhatsApp y el riesgo es que baneen el numero (ver "Advertencia sobre
  Baileys").

## Instalacion

En PowerShell, desde la carpeta donde se quiera dejar el bot:

    git clone https://github.com/CesarYamadaX/Honor-WhatsApp-Bot.git
    cd Honor-WhatsApp-Bot

**1. Dependencias de Python** (servicio `sql_agent/`). Van en un entorno
virtual `.venv` dentro de `sql_agent`, que es donde `iniciar.py` lo busca:

    cd sql_agent
    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    cd ..

`requirements.txt` fija las versiones exactas. Lo principal: `langchain`,
`langchain-community` y `langchain-openai` (el agente), `sqlalchemy` y
`pyodbc` (la base), `fastapi` y `uvicorn` (el servicio HTTP), `pandas`,
`openpyxl` y `matplotlib` (Excel y graficas), `pywin32` (la tabla dinamica)
y `Pillow` (las fotos del panel).

**2. Dependencias de Node** (sidecar `whatsapp/`):

    cd whatsapp
    npm install
    cd ..

Instala `@whiskeysockets/baileys` (la conexion con WhatsApp), `pino` (sus
logs) y `qrcode-terminal` (el QR en la consola).

**3. Configuracion.** Copiar la plantilla y llenarla:

    copy sql_agent\.env.example sql_agent\.env

| Variable | Obligatoria | Que es |
|---|---|---|
| `DB_URI` | si | Conexion al SQL Server con el login de solo lectura. Dejar `driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes` al final |
| `OPENROUTER_API_KEY` | si | La key de OpenRouter |
| `OPENROUTER_MODEL` | si | El modelo a usar, p. ej. `openai/gpt-6-luna` |
| `WHATSAPP_ALLOWED_NUMBERS` | si | Numeros que pueden usar el bot, formato internacional sin signos, separados por coma. Sin esto el servicio no arranca |
| `AGENT_TIMEOUT_SECONDS` | no | Cuanto puede tardar una pregunta (600; los reportes grandes tardan varios minutos) |
| `SESSION_TTL_MINUTES` | no | 0: la memoria de cada conversacion dura el dia y se borra a medianoche |
| `CANCEL_WAIT_SECONDS` | no | Espera para cancelar una consulta cuando llega otro mensaje (25) |
| `API_HOST` / `API_PORT` | no | Donde escucha el servicio (127.0.0.1:8000). No exponerlo fuera de la maquina |

El sidecar no necesita `.env` propio.

**4. Comprobar.** `python iniciar.py` revisa que exista el entorno virtual,
el `.env` y `node_modules` antes de lanzar nada, y si falta algo dice que y
no arranca. La primera vez imprime un QR: en el telefono del bot, WhatsApp >
Dispositivos vinculados > Vincular un dispositivo.

## Arranque

Un solo comando levanta los dos procesos y abre el panel:

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

La instalacion y la configuracion estan en "Instalacion", arriba. Si falta
algo, `iniciar.py` lo dice y no arranca nada.

Para levantarlos por separado, ver `whatsapp/README.md`.

Ningun `.env` con valores reales se versiona.

## Pruebas

Las del lado Python necesitan ademas `requirements-dev.txt` (pytest y lo
que usa para probar el servicio HTTP):

    cd sql_agent
    .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
    .venv\Scripts\python.exe -m pytest tests -q
    cd ..\whatsapp
    npm test

Ninguna necesita la base de datos ni WhatsApp: usan dobles de prueba. Las de
la tabla dinamica abren Excel de verdad; si hay otro Excel ocupado pueden
fallar por contencion y pasar al repetirlas solas.
