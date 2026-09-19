"""Abre el panel del bot como una aplicacion, no como una pestaña del navegador.

    python panel.py

Usa el modo aplicacion de Edge o Chrome (`--app=`): una ventana propia, sin
barra de direcciones ni pestañas, con su icono en la barra de tareas. Es lo
mismo que hacen casi todas las "apps" de escritorio que por dentro son web, y
no obliga a instalar Electron ni a mantener un segundo empaquetado.

Si no hay ninguno de los dos, cae al navegador por defecto en vez de dejar al
usuario sin panel.
"""

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

from iniciar import responde, url_de_salud, url_del_panel  # noqa: E402

# Edge esta en toda instalacion de Windows 11; Chrome es lo habitual si no.
NAVEGADORES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

TAMANO = "1180,800"


def navegador_para_app() -> str | None:
    """El primer navegador que sepa abrir una ventana de aplicacion."""
    for ruta in NAVEGADORES:
        if Path(ruta).exists():
            return ruta
    for nombre in ("msedge", "chrome", "chromium", "brave"):
        encontrado = shutil.which(nombre)
        if encontrado:
            return encontrado
    return None


def ya_esta_abierta(url: str) -> bool:
    """True si ya hay una ventana del panel en esta maquina.

    Sin esto, cada reinicio del bot deja otra ventana encima de la anterior:
    la de antes no se entera de que el servicio se fue y vuelve, y termina
    uno con cuatro paneles identicos en la barra de tareas.

    Se reconoce por la linea de comando -- `--app=<la url>` --, que es lo
    unico que distingue a la ventana del panel de una ventana normal del
    navegador.
    """
    marca = f"--app={url}"
    if sys.platform == "win32":
        comando = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-CimInstance Win32_Process | Select-Object -ExpandProperty CommandLine",
        ]
    else:
        comando = ["ps", "-eo", "args="]
    try:
        salida = subprocess.run(comando, capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.SubprocessError):
        # Si no se puede averiguar, se abre: una ventana de mas molesta
        # menos que ninguna.
        return False
    return marca in (salida.stdout or "")


def abrir(url: str) -> str:
    """Abre el panel. Devuelve como se abrio, para poder decirlo."""
    navegador = navegador_para_app()
    if navegador is None:
        webbrowser.open(url)
        return "en el navegador por defecto (no encontre Edge ni Chrome)"

    # Un perfil aparte para que la ventana sea independiente de la sesion de
    # navegacion: ni hereda pestañas, ni las extensiones del usuario, ni deja
    # el panel dentro de su historial.
    perfil = RAIZ / "sql_agent" / "datos" / "ventana"
    perfil.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            navegador,
            f"--app={url}",
            f"--user-data-dir={perfil}",
            f"--window-size={TAMANO}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        close_fds=True,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    return f"como aplicacion ({Path(navegador).stem})"


def main() -> int:
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(encoding="utf-8", errors="replace")

    url = url_del_panel()
    if url is None:
        print(
            "El panel esta apagado: API_HOST no es loopback y el servicio no "
            "tiene autenticacion.",
            file=sys.stderr,
        )
        return 1

    if not responde(url_de_salud()):
        print(
            "El bot no esta encendido, asi que el panel no tiene a quien "
            "preguntarle.\nArrancalo primero con: python iniciar.py",
            file=sys.stderr,
        )
        return 1

    if ya_esta_abierta(url):
        print("El panel ya esta abierto; buscalo en la barra de tareas.")
        return 0

    print(f"Abriendo el panel {abrir(url)}: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
