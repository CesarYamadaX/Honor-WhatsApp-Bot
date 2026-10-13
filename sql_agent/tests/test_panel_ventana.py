"""Pruebas de panel.py, el que abre el panel como aplicacion.

Lo unico delicado aqui es no abrir una ventana que no va a servir de nada:
un panel contra un bot apagado no muestra mas que errores, y uno contra un
servicio abierto a la red ni siquiera carga.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import panel


class TestCuandoNoTieneSentidoAbrir:
    def test_con_el_bot_apagado_avisa_y_no_abre_nada(self, monkeypatch, capsys):
        abiertos = []
        monkeypatch.setattr(panel, "responde", lambda url: False)
        monkeypatch.setattr(panel, "abrir", lambda url: abiertos.append(url))

        assert panel.main() == 1
        assert abiertos == []
        assert "iniciar.py" in capsys.readouterr().err

    def test_con_el_panel_apagado_por_no_ser_loopback_tampoco(self, monkeypatch, capsys):
        # url_del_panel devuelve None cuando API_HOST no es loopback: el
        # servicio responde 403 a todo el panel, asi que la ventana saldria
        # en blanco sin explicar por que.
        abiertos = []
        monkeypatch.setattr(panel, "url_del_panel", lambda: None)
        monkeypatch.setattr(panel, "abrir", lambda url: abiertos.append(url))

        assert panel.main() == 1
        assert abiertos == []
        assert "loopback" in capsys.readouterr().err


class TestCuandoSiAbre:
    def test_abre_la_direccion_del_panel(self, monkeypatch, capsys):
        abiertos = []
        monkeypatch.setattr(panel, "responde", lambda url: True)
        monkeypatch.setattr(panel, "url_del_panel", lambda: "http://127.0.0.1:8000/panel")
        # Sin esto la prueba mira las ventanas de ESTA maquina y cambia de
        # resultado segun si el panel esta abierto mientras corre.
        monkeypatch.setattr(panel, "ya_esta_abierta", lambda url: False)
        monkeypatch.setattr(panel, "abrir", lambda url: abiertos.append(url) or "de mentira")

        assert panel.main() == 0
        assert abiertos == ["http://127.0.0.1:8000/panel"]
        assert "http://127.0.0.1:8000/panel" in capsys.readouterr().out


class TestElegirNavegador:
    def test_prefiere_uno_que_sepa_abrir_ventana_de_aplicacion(self, monkeypatch, tmp_path):
        falso = tmp_path / "msedge.exe"
        falso.touch()
        monkeypatch.setattr(panel, "NAVEGADORES", (str(falso),))
        assert panel.navegador_para_app() == str(falso)

    def test_si_no_hay_ninguno_lo_dice_devolviendo_None(self, monkeypatch):
        # Quien llama cae entonces al navegador por defecto: mejor una
        # pestaña normal que quedarse sin panel.
        monkeypatch.setattr(panel, "NAVEGADORES", ())
        monkeypatch.setattr(panel.shutil, "which", lambda _: None)
        assert panel.navegador_para_app() is None


class TestNoAbrirDosVeces:
    def test_si_ya_hay_una_ventana_no_abre_otra(self, monkeypatch, capsys):
        # Cada reinicio del bot abria otra ventana encima de la anterior.
        abiertos = []
        monkeypatch.setattr(panel, "responde", lambda url: True)
        monkeypatch.setattr(panel, "url_del_panel", lambda: "http://127.0.0.1:8000/panel")
        monkeypatch.setattr(panel, "ya_esta_abierta", lambda url: True)
        monkeypatch.setattr(panel, "abrir", lambda url: abiertos.append(url))

        assert panel.main() == 0
        assert abiertos == []
        assert "ya esta abierto" in capsys.readouterr().out

    def test_reconoce_la_ventana_por_su_linea_de_comando(self, monkeypatch):
        # `--app=<url>` es lo unico que distingue la ventana del panel de
        # una ventana normal del navegador.
        class Salida:
            stdout = 'msedge.exe --app=http://127.0.0.1:8000/panel --user-data-dir=x'

        monkeypatch.setattr(panel.subprocess, "run", lambda *a, **k: Salida())
        assert panel.ya_esta_abierta("http://127.0.0.1:8000/panel")
        assert not panel.ya_esta_abierta("http://127.0.0.1:9999/panel")

    def test_si_no_se_puede_averiguar_se_abre(self, monkeypatch):
        # Una ventana de mas molesta menos que ninguna.
        def revienta(*a, **k):
            raise OSError("no hay powershell")

        monkeypatch.setattr(panel.subprocess, "run", revienta)
        assert not panel.ya_esta_abierta("http://127.0.0.1:8000/panel")


def test_al_abrir_la_ventana_se_tira_su_cache_y_no_la_sesion(tmp_path):
    # Visto el 13/10/2026: la ventana seguia con la pagina de las 8:00
    # despues de un git pull y dos reinicios.
    for nombre in ("Cache/Cache_Data", "Code Cache/js", "Local Storage"):
        (tmp_path / "Default" / nombre).mkdir(parents=True)
        (tmp_path / "Default" / nombre / "f_0001").write_text("x")
    panel.borrar_cache(tmp_path)
    assert not (tmp_path / "Default" / "Cache").exists()
    assert not (tmp_path / "Default" / "Code Cache").exists()
    assert (tmp_path / "Default" / "Local Storage" / "f_0001").exists()
