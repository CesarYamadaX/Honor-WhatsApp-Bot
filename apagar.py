"""Apaga el bot: baja el sidecar de WhatsApp y el servicio Python.

    python apagar.py

Sirve cuando la ventana donde se hizo `python iniciar.py` ya no esta a mano
(se minimizo, se arranco con doble clic, se cerro la sesion). Si esa ventana
esta abierta, Ctrl+C ahi hace lo mismo.

Se apaga por el archivo de PIDs que deja `iniciar.py`, y antes de matar nada
se comprueba que el proceso siga siendo el nuestro: los numeros de proceso se
reciclan, y una anotacion vieja mas un PID reutilizado es la forma de matarle
a alguien algo que no tiene nada que ver.
"""

import sys

from iniciar import (
    ARCHIVO_DE_PIDS,
    detener_pid,
    leer_encendido,
    olvidar_encendido,
    responde,
    sigue_siendo_el_nuestro,
    url_de_salud,
)


def main() -> int:
    for flujo in (sys.stdout, sys.stderr):
        if hasattr(flujo, "reconfigure"):
            flujo.reconfigure(encoding="utf-8", errors="replace")

    anotados = leer_encendido()
    salud = url_de_salud()

    if not anotados:
        if responde(salud):
            # Hay algo vivo pero no lo arrancamos nosotros: lo normal es que
            # sean los dos procesos a mano, en sus propias terminales.
            print(
                f"Hay un servicio contestando en {salud}, pero no lo arranque yo:\n"
                f"no existe {ARCHIVO_DE_PIDS.name}. Cierralo desde la ventana donde\n"
                "lo levantaste (Ctrl+C), o matalo por el administrador de tareas.",
                file=sys.stderr,
            )
            return 1
        print("El bot no esta encendido.")
        return 0

    fallidos = []
    for proceso in anotados:
        nombre = proceso.get("nombre", "un proceso")
        pid, marca = proceso.get("pid"), proceso.get("marca", "")
        if not isinstance(pid, int):
            continue
        if not sigue_siendo_el_nuestro(pid, marca):
            print(f"[apagar] {nombre} ya no estaba corriendo.")
            continue
        print(f"[apagar] deteniendo {nombre} (pid {pid})...", flush=True)
        if detener_pid(pid):
            print(f"[apagar] {nombre} detenido.")
        else:
            fallidos.append(f"{nombre} (pid {pid})")

    olvidar_encendido()

    if fallidos:
        print(
            "No pude detener: " + ", ".join(fallidos) + ". Matalos a mano.",
            file=sys.stderr,
        )
        return 1

    # Lo unico que de verdad demuestra que se apago: que ya no conteste.
    if responde(salud):
        print(
            f"Los procesos que anote ya no estan, pero {salud} sigue contestando.\n"
            "Hay otro servicio levantado aparte; revisalo antes de volver a arrancar.",
            file=sys.stderr,
        )
        return 1

    print("Bot apagado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
