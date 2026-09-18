"""Levanta el bot completo: el servicio Python y el sidecar de WhatsApp.

Son dos procesos y hay que arrancarlos en orden -- el sidecar comprueba el
servicio al arrancar y se queja si todavia no esta --, asi que a mano son dos
terminales y esperar en medio. Esto lo hace solo:

    python iniciar.py

Ctrl+C baja los dos. Si uno se cae, el otro se baja tambien: un sidecar vivo
contra un servicio muerto le contesta a todo el mundo "el sistema no esta
disponible", que parece que el bot atiende cuando no atiende.
"""

import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
SERVICIO = RAIZ / "sql_agent"
SIDECAR = RAIZ / "whatsapp"

# El QR de la primera vinculacion se dibuja con bloques. Esas lineas pasan
# tal cual, sin prefijo: un QR con adornos no se escanea.
_BLOQUES = "█▀▄▌▐░▒▓"

ESPERA_MAXIMA_SEGUNDOS = 90
INTERVALO_DE_SONDEO = 0.5


def python_del_entorno(servicio: Path = SERVICIO) -> Path | None:
    """Busca el interprete del venv del servicio.

    El servicio necesita SU venv (pyodbc, langchain, fastapi); el python con
    el que se lance este script casi nunca los tiene. Si no aparece ninguno,
    quien llama avisa en vez de arrancar algo que va a morir importando.
    """
    for carpeta in (".venv", "venv", "env"):
        for relativa in ("Scripts/python.exe", "bin/python"):
            candidato = servicio / carpeta / relativa
            if candidato.exists():
                return candidato
    return None


def revisar_requisitos(raiz: Path = RAIZ) -> list[str]:
    """Devuelve los problemas que impiden arrancar, en lenguaje llano.

    Se revisan antes de lanzar nada. Un .env que falta se manifiesta, si no,
    como un proceso que arranca, escupe un stack trace y muere: el usuario ve
    dos procesos peleados en vez de una linea que dice que falta.
    """
    servicio, sidecar = raiz / "sql_agent", raiz / "whatsapp"
    problemas = []

    if python_del_entorno(servicio) is None:
        problemas.append(
            f"No encontre el entorno virtual de Python en {servicio}. Crealo con: "
            "python -m venv .venv && .venv/Scripts/pip install -r requirements.txt"
        )
    if not (servicio / ".env").exists():
        problemas.append(
            f"Falta {servicio / '.env'}. Copia .env.example y llenalo "
            "(DB_URI, OPENROUTER_API_KEY, WHATSAPP_ALLOWED_NUMBERS)."
        )
    # El .env del sidecar NO se revisa: index.js no carga dotenv, lee
    # process.env directo y todos sus valores tienen respaldo. Exigirlo aqui
    # bloquearia una instalacion que funciona -- comprobado: el sidecar lleva
    # dias corriendo sin ese archivo.
    if not (sidecar / "node_modules").exists():
        problemas.append(
            f"Faltan las dependencias de Node. Corre: cd {sidecar} && npm install"
        )
    if shutil.which("node") is None:
        problemas.append("No encontre `node` en el PATH. Hace falta Node 20 o superior.")
    return problemas


def url_de_salud(raiz: Path = RAIZ) -> str:
    """Donde escucha el servicio, segun su .env (no siempre es el 8000)."""
    host, puerto = "127.0.0.1", "8000"
    archivo = raiz / "sql_agent" / ".env"
    if archivo.exists():
        for linea in archivo.read_text(encoding="utf-8", errors="replace").splitlines():
            clave, _, valor = linea.partition("=")
            clave, valor = clave.strip(), valor.strip()
            if clave == "API_HOST" and valor:
                host = valor
            elif clave == "API_PORT" and valor:
                puerto = valor
    # 0.0.0.0 es "escucha en todas las interfaces"; para preguntarle hay que
    # ir a una direccion de verdad.
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{puerto}/health"


def responde(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as respuesta:
            return respuesta.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def formatear(prefijo: str, linea: str) -> str:
    """Etiqueta la linea con el proceso del que viene, salvo si es del QR."""
    if any(c in linea for c in _BLOQUES):
        return linea
    # El sidecar ya escribe "[whatsapp]" al frente de lo suyo; repetirlo
    # gasta ancho de la terminal sin decir nada nuevo.
    if linea.startswith(prefijo):
        return linea
    return f"{prefijo} {linea}"


def _bombear(proceso: subprocess.Popen, prefijo: str, salida) -> None:
    """Vuelca la salida de un proceso a la consola, linea por linea."""
    for linea in proceso.stdout:
        print(formatear(prefijo, linea.rstrip("\r\n")), file=salida, flush=True)


def _lanzar(comando: list[str], cwd: Path) -> subprocess.Popen:
    return subprocess.Popen(
        comando,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # El QR y los acentos vienen en utf-8; sin esto la consola de Windows
        # los lee como cp1252 y el QR sale hecho pedazos.
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
    )


def _terminar(proceso: subprocess.Popen, nombre: str) -> None:
    if proceso.poll() is not None:
        return
    print(f"[iniciar] deteniendo {nombre}...", flush=True)
    proceso.terminate()
    try:
        proceso.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proceso.kill()


def main() -> int:
    # El QR y los logs traen caracteres que la consola de Windows no imprime
    # en su codificacion por defecto.
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(encoding="utf-8", errors="replace")

    problemas = revisar_requisitos()
    if problemas:
        print("No puedo arrancar el bot:\n", file=sys.stderr)
        for problema in problemas:
            print(f"  - {problema}", file=sys.stderr)
        return 1

    salud = url_de_salud()
    if responde(salud):
        print(
            f"Ya hay un servicio contestando en {salud}. El bot parece estar "
            "encendido; cierralo antes de volver a arrancarlo.",
            file=sys.stderr,
        )
        return 1

    interprete = python_del_entorno()
    procesos: list[tuple[str, subprocess.Popen]] = []

    try:
        print(f"[iniciar] arrancando el servicio Python ({interprete.name})...", flush=True)
        api = _lanzar([str(interprete), "api.py"], SERVICIO)
        procesos.append(("el servicio Python", api))
        threading.Thread(
            target=_bombear, args=(api, "[api]", sys.stdout), daemon=True
        ).start()

        # Se espera a que el servicio conteste, no un numero fijo de segundos:
        # la primera importacion de langchain y pyodbc tarda bastante mas en
        # una maquina fria que en una caliente.
        limite = time.monotonic() + ESPERA_MAXIMA_SEGUNDOS
        while not responde(salud):
            if api.poll() is not None:
                print(
                    "[iniciar] el servicio Python murio al arrancar "
                    "(el motivo esta en el log de arriba).",
                    file=sys.stderr,
                )
                return api.returncode or 1
            if time.monotonic() > limite:
                print(
                    f"[iniciar] el servicio no contesto en {ESPERA_MAXIMA_SEGUNDOS}s. "
                    "No arranco WhatsApp.",
                    file=sys.stderr,
                )
                return 1
            time.sleep(INTERVALO_DE_SONDEO)

        print(f"[iniciar] servicio listo en {salud}", flush=True)
        print("[iniciar] arrancando el sidecar de WhatsApp...", flush=True)
        wa = _lanzar(["node", "index.js"], SIDECAR)
        procesos.append(("el sidecar de WhatsApp", wa))
        threading.Thread(
            target=_bombear, args=(wa, "[whatsapp]", sys.stdout), daemon=True
        ).start()

        print("[iniciar] los dos arriba. Ctrl+C para bajarlos.", flush=True)

        while True:
            for nombre, proceso in procesos:
                if proceso.poll() is not None:
                    print(
                        f"[iniciar] {nombre} termino (codigo {proceso.returncode}). "
                        "Bajo el otro tambien.",
                        file=sys.stderr,
                    )
                    return proceso.returncode or 1
            time.sleep(INTERVALO_DE_SONDEO)

    except KeyboardInterrupt:
        print("\n[iniciar] Ctrl+C, bajando el bot.", flush=True)
        return 0
    finally:
        # Al reves de como se levantaron: primero WhatsApp, para que no entre
        # un mensaje justo cuando el servicio ya no esta para contestarlo.
        for nombre, proceso in reversed(procesos):
            _terminar(proceso, nombre)


if __name__ == "__main__":
    sys.exit(main())
