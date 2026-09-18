"""Pruebas de apagar.py.

Lo delicado de apagar no es matar, es decidir A QUIEN. Estas pruebas cubren
las tres decisiones: que se apaga primero, que pasa cuando lo anotado ya no
existe, y que NO se mata nada que no hayamos arrancado nosotros.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import apagar


def _anotado(nombre, pid, marca="x"):
    return {"nombre": nombre, "pid": pid, "marca": marca}


def _preparar(monkeypatch, *, anotados, vivos=(), contesta=False):
    """Deja apagar.py hablando con procesos de mentira."""
    detenidos = []
    monkeypatch.setattr(apagar, "leer_encendido", lambda: list(anotados))
    monkeypatch.setattr(apagar, "olvidar_encendido", lambda: detenidos.append("olvidado"))
    monkeypatch.setattr(
        apagar, "sigue_siendo_el_nuestro", lambda pid, marca: pid in vivos
    )
    monkeypatch.setattr(
        apagar, "detener_pid", lambda pid: detenidos.append(pid) or True
    )
    monkeypatch.setattr(apagar, "responde", lambda url: contesta)
    return detenidos


class TestApagado:
    def test_apaga_whatsapp_antes_que_el_servicio(self, monkeypatch):
        # En ese orden y no al reves: si el servicio se va primero, un mensaje
        # que entre en ese hueco recibe "el sistema no esta disponible".
        anotados = [_anotado("el sidecar de WhatsApp", 11), _anotado("el servicio", 22)]
        detenidos = _preparar(monkeypatch, anotados=anotados, vivos={11, 22})

        assert apagar.main() == 0
        assert detenidos == [11, 22, "olvidado"]

    def test_lo_que_ya_no_estaba_no_impide_apagar_el_resto(self, monkeypatch):
        # Pasa a menudo: uno de los dos se cayo solo hace rato.
        anotados = [_anotado("el sidecar", 11), _anotado("el servicio", 22)]
        detenidos = _preparar(monkeypatch, anotados=anotados, vivos={22})

        assert apagar.main() == 0
        assert detenidos == [22, "olvidado"]

    def test_sin_nada_anotado_y_sin_nadie_contestando_no_hay_nada_que_hacer(
        self, monkeypatch
    ):
        detenidos = _preparar(monkeypatch, anotados=[])
        assert apagar.main() == 0
        assert detenidos == []

    def test_no_mata_un_servicio_que_no_arrancamos_nosotros(self, monkeypatch):
        # Levantado a mano en otra terminal: sin archivo de PIDs no se sabe
        # cual es su proceso, y adivinar por nombre puede matar algo ajeno.
        # Se avisa y se sale con error, sin tocar nada.
        detenidos = _preparar(monkeypatch, anotados=[], contesta=True)

        assert apagar.main() == 1
        assert detenidos == []

    def test_avisa_si_el_servicio_sigue_contestando_despues_de_apagar(
        self, monkeypatch
    ):
        # Los procesos anotados murieron pero el puerto sigue ocupado: hay
        # otro servicio levantado aparte. Decirlo evita que el siguiente
        # arranque falle sin explicacion.
        anotados = [_anotado("el servicio", 22)]
        _preparar(monkeypatch, anotados=anotados, vivos={22}, contesta=True)

        assert apagar.main() == 1

    def test_si_un_proceso_no_se_deja_matar_lo_dice(self, monkeypatch):
        anotados = [_anotado("el servicio", 22)]
        _preparar(monkeypatch, anotados=anotados, vivos={22})
        monkeypatch.setattr(apagar, "detener_pid", lambda pid: False)

        assert apagar.main() == 1
