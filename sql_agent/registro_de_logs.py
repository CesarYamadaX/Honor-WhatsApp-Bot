"""Los logs del bot en disco: quien los escribe y como se leen desde el panel.

Antes los logs solo existian en la ventana de la terminal donde corre
`iniciar.py`: si se cerraba la ventana, o si uno queria saber por que se
desconecto WhatsApp anoche, no habia de donde sacarlo. Ahora `iniciar.py`
escribe ademas cada linea en un archivo por dia, y el panel los muestra.

El formato de una linea en disco es la fecha, la hora y la linea tal como se
vio en la consola, con su etiqueta de origen:

    2026-10-01 10:32:05 [whatsapp] conectado

Este modulo usa solo la libreria estandar a proposito: lo importa
`iniciar.py`, que corre con el Python del sistema y no con el del venv.
"""

import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

CARPETA_POR_DEFECTO = Path(__file__).resolve().parent / "datos" / "logs"
PATRON_DE_ARCHIVO = "bot-{dia}.log"
_ARCHIVO = re.compile(r"^bot-(\d{4}-\d{2}-\d{2})\.log$")

# Cuantos dias se guardan. Los logs sirven para entender lo que paso esta
# semana; dos semanas dan margen para un fin de semana largo.
DIAS_QUE_SE_GUARDAN = 14

# Lo que NO se escribe. El panel le pregunta al servicio cada 2 segundos
# como va (/panel/actividad), y iniciar.py y el sidecar consultan /health:
# cada una de esas peticiones deja una linea de uvicorn. Serian ~43 000
# lineas al dia que taparian todo lo demas. Lo mismo el sidecar preguntando
# cada 5 s si hay que cambiar la foto del bot: 5 617 lineas el 19/10/2026.
_RUIDO = re.compile(r'"GET /(panel|health|foto-bot/cambio)[^"]*" \d{3}')
# El QR de vinculacion se dibuja con bloques: en un archivo no sirve.
_BLOQUES = set("█▀▄▌▐░▒▓")

_LINEA = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) (.*)$")
_FUENTE = re.compile(r"^\[(whatsapp|api|iniciar)\]")

# Que tan grave es una linea. "error" es algo que fallo; "aviso" es algo que
# conviene mirar pero que el bot maneja solo (una desconexion de WhatsApp
# que se reintenta, por ejemplo).
# Distingue mayusculas a proposito: el nivel de logging va en mayusculas
# (ERROR, CRITICAL), y una palabra "Error" suelta no es un error del bot. Con
# IGNORECASE, "[whatsapp] desconectado (...): WebSocket Error (Opening
# handshake has timed out)" -- una reconexion que el sidecar resuelve solo --
# salia como error.
_ES_ERROR = re.compile(
    r"\b(ERROR|CRITICAL)\b|Traceback|Exception|excepcion no capturada|"
    r"rechazo no manejado|\b[Ff]allo\b|\b[Mm]urio\b|\" 5\d\d\b|termino \(codigo|"
    r"^\S+(Error|Exception):|"
    # Lo que une dos tracebacks encadenados: sin esto cada falla contaba
    # como tres errores (12/10/2026).
    r"^(The above exception was the direct cause|During handling of the above exception)"
)
_ES_AVISO = re.compile(
    r"\bWARNING\b|desconectado|reintentando|no se pudo|sin conexion|"
    r"no contesta",
    re.IGNORECASE,
)


class EscritorDeLogs:
    """Escribe las lineas de la consola en el archivo del dia.

    Lo usan los dos hilos de `iniciar.py` (uno por proceso), asi que escribe
    bajo un candado. Un fallo al escribir no puede tumbar el bot: el log es
    un extra, la consola sigue mostrando todo.
    """

    def __init__(self, carpeta: Path = CARPETA_POR_DEFECTO,
                 dias: int = DIAS_QUE_SE_GUARDAN, reloj=datetime.now):
        self.carpeta = Path(carpeta)
        self.dias = dias
        self._reloj = reloj
        self._candado = threading.Lock()
        try:
            self.carpeta.mkdir(parents=True, exist_ok=True)
            self.podar()
        except OSError:
            pass

    def escribir(self, linea: str) -> bool:
        """Anota una linea. Devuelve False si era ruido o no se pudo."""
        linea = linea.rstrip("\r\n")
        if not linea.strip() or _RUIDO.search(linea) or any(c in _BLOQUES for c in linea):
            return False
        ahora = self._reloj()
        archivo = self.carpeta / PATRON_DE_ARCHIVO.format(dia=ahora.strftime("%Y-%m-%d"))
        try:
            with self._candado, archivo.open("a", encoding="utf-8") as destino:
                destino.write(f"{ahora:%Y-%m-%d %H:%M:%S} {linea}\n")
            return True
        except OSError:
            return False

    def podar(self) -> int:
        """Borra los archivos de mas de `dias` dias. Devuelve cuantos borro."""
        limite = self._reloj().date() - timedelta(days=self.dias)
        borrados = 0
        for archivo in self.carpeta.glob("bot-*.log"):
            coincidencia = _ARCHIVO.match(archivo.name)
            if not coincidencia:
                continue
            try:
                if date.fromisoformat(coincidencia.group(1)) < limite:
                    archivo.unlink()
                    borrados += 1
            except (ValueError, OSError):
                continue
        return borrados


def clasificar(texto: str) -> tuple[str, str]:
    """(fuente, nivel) de una linea ya sin fecha ni hora."""
    fuente = _FUENTE.match(texto)
    resto = texto[fuente.end():] if fuente else texto
    # La etiqueta va separada por UN espacio: se quita ese y nada mas, para
    # que la sangria de un traceback ("  File ...") se siga viendo como tal.
    if fuente and resto.startswith(" "):
        resto = resto[1:]
    if _ES_ERROR.search(resto) or resto.startswith(("  File ", "    ")):
        nivel = "error"
    elif _ES_AVISO.search(resto):
        nivel = "aviso"
    else:
        nivel = "info"
    return (fuente.group(1) if fuente else "otro"), nivel


def _leer_lineas(archivo: Path) -> list[dict]:
    try:
        crudas = archivo.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    lineas = []
    for cruda in crudas:
        partes = _LINEA.match(cruda)
        if not partes:
            continue
        fuente, nivel = clasificar(partes.group(3))
        lineas.append({
            "fecha": partes.group(1),
            "hora": partes.group(2),
            "fuente": fuente,
            "nivel": nivel,
            "texto": partes.group(3),
        })
    return lineas


def _incidentes(lineas: list[dict]) -> int:
    """Cuantos errores hubo, no cuantas lineas de error.

    Un traceback son decenas de lineas seguidas ("  File ...", "raise ..."),
    y cada una se clasifica como error para que se vea en rojo. Contarlas
    todas decia "1 360 errores" el 12/10/2026 por 11 fallas de conexion
    (cada una, tres tracebacks encadenados de ~40 lineas). Un bloque de
    lineas de error seguidas es UN error.
    """
    incidentes, previa = 0, False
    for linea in lineas:
        # Las lineas en blanco entre tracebacks encadenados no cortan el error.
        if not _FUENTE.sub("", linea["texto"]).strip():
            continue
        es_error = linea["nivel"] == "error"
        if es_error and not previa:
            incidentes += 1
        previa = es_error
    return incidentes


def dias(carpeta: Path = CARPETA_POR_DEFECTO) -> list[dict]:
    """Los dias que tienen log, el mas reciente primero, con sus problemas."""
    resultado = []
    for archivo in sorted(Path(carpeta).glob("bot-*.log"), reverse=True):
        coincidencia = _ARCHIVO.match(archivo.name)
        if not coincidencia:
            continue
        lineas = _leer_lineas(archivo)
        resultado.append({
            "dia": coincidencia.group(1),
            "lineas": len(lineas),
            "errores": _incidentes(lineas),
            "avisos": sum(1 for l in lineas if l["nivel"] == "aviso"),
            "ultima": lineas[-1]["hora"] if lineas else None,
        })
    return resultado


def leer(dia: str, carpeta: Path = CARPETA_POR_DEFECTO, limite: int = 500,
         fuente: str | None = None, solo_problemas: bool = False) -> dict:
    """Las ultimas `limite` lineas de ese dia, con los filtros pedidos.

    `dia` tiene que ser AAAA-MM-DD: se valida antes de armar la ruta, para
    que nadie lea otro archivo del disco pasando "../" en el parametro.
    """
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dia or ""):
        raise ValueError(f"Dia invalido: {dia!r}")
    lineas = _leer_lineas(Path(carpeta) / PATRON_DE_ARCHIVO.format(dia=dia))
    if fuente:
        lineas = [l for l in lineas if l["fuente"] == fuente]
    if solo_problemas:
        lineas = [l for l in lineas if l["nivel"] != "info"]
    return {
        "dia": dia,
        "total": len(lineas),
        "lineas": lineas[-limite:] if limite > 0 else lineas,
    }


_ESTADO_WHATSAPP = re.compile(
    r"^\[whatsapp\] (conectado$|desconectado.*|Escanea este codigo.*)"
)


def estado_whatsapp(carpeta: Path = CARPETA_POR_DEFECTO) -> dict:
    """Lo ultimo que se sabe de la conexion con WhatsApp, segun los logs.

    Busca hacia atras, empezando por el dia mas reciente, la ultima linea
    que dice "conectado", "desconectado" o "escanea el QR".
    """
    for archivo in sorted(Path(carpeta).glob("bot-*.log"), reverse=True):
        for linea in reversed(_leer_lineas(archivo)):
            coincidencia = _ESTADO_WHATSAPP.match(linea["texto"])
            if not coincidencia:
                continue
            texto = coincidencia.group(1)
            estado = (
                "conectado" if texto == "conectado"
                else "qr" if texto.startswith("Escanea")
                else "desconectado"
            )
            return {
                "estado": estado,
                "fecha": linea["fecha"],
                "hora": linea["hora"],
                "detalle": linea["texto"],
            }
    return {"estado": "desconocido", "fecha": None, "hora": None, "detalle": None}
