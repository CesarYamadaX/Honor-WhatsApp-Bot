"""Borra los reportes viejos de la carpeta de exportaciones.

Cada Excel y cada grafica que el bot manda se queda en `exports/` para
siempre: nada los borraba. En la compu de pruebas ya eran 1 011 archivos y
131 MB el 2 de octubre de 2026, y un reporte con todas las filas crudas pesa
decenas de MB. El archivo ya viajo por WhatsApp; guardarlo una semana alcanza
para reenviarlo o revisar que se mando.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger("sql_agent.limpieza")

DIAS_DE_RETENCION = float(os.getenv("EXPORTS_RETENTION_DAYS", "7"))


def carpeta_de_exportaciones() -> Path:
    """La misma que usan excel_exporter.py y grafica.py."""
    return Path(
        os.environ.get("EXPORT_DIR")
        or Path(__file__).resolve().parent / "exports"
    )


def borrar_viejos(carpeta: Path, dias: float = DIAS_DE_RETENCION,
                  ahora: float | None = None) -> int:
    """Borra los archivos con mas de `dias` sin tocarse. Devuelve cuantos.

    Solo archivos sueltos de la carpeta, nunca subcarpetas. Uno que no se
    pueda borrar (abierto en Excel, por ejemplo) se deja para la proxima.
    Con `dias` <= 0 no borra nada: es la forma de apagarlo.
    """
    carpeta = Path(carpeta)
    if dias <= 0 or not carpeta.is_dir():
        return 0
    limite = (ahora if ahora is not None else time.time()) - dias * 86400
    borrados = 0
    for archivo in carpeta.iterdir():
        try:
            if archivo.is_file() and archivo.stat().st_mtime < limite:
                archivo.unlink()
                borrados += 1
        except OSError:
            logger.debug("No se pudo borrar %s; se reintenta despues", archivo)
    if borrados:
        logger.info("Se borraron %d reportes de mas de %g dias en %s", borrados, dias, carpeta)
    return borrados


def mantener_limpia(carpeta: Path | None = None, cada_horas: float = 6,
                    detener: threading.Event | None = None) -> threading.Thread:
    """Un hilo que limpia al arrancar y despues cada `cada_horas`."""
    detener = detener or threading.Event()

    def ciclo():
        while not detener.is_set():
            try:
                borrar_viejos(Path(carpeta or carpeta_de_exportaciones()))
            except Exception:  # noqa: BLE001 - limpiar nunca debe tumbar al bot
                logger.exception("Fallo la limpieza de exportaciones")
            detener.wait(cada_horas * 3600)

    hilo = threading.Thread(target=ciclo, name="limpieza-exports", daemon=True)
    hilo.start()
    return hilo
