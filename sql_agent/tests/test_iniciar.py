"""Pruebas de iniciar.py, el arranque de los dos procesos.

Lo que se prueba aqui son las decisiones que toma ANTES de lanzar nada: que
interprete usar, que falta para poder arrancar y a donde preguntarle al
servicio si ya esta listo. Equivocarse en cualquiera de las tres deja al
usuario mirando dos procesos que no se hablan, que es justo lo que este
script existe para evitar.
"""

import sys
import time
from pathlib import Path

import pytest

# iniciar.py vive en la raiz del repo, un nivel arriba de sql_agent, que es
# lo que pytest pone en el path para el resto de las pruebas.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import iniciar


def _proyecto(tmp_path: Path, *, venv=True, env_py=True, node_modules=True):
    """Arma un arbol de proyecto de mentira, con las piezas que se pidan."""
    servicio = tmp_path / "sql_agent"
    sidecar = tmp_path / "whatsapp"
    servicio.mkdir()
    sidecar.mkdir()
    if venv:
        binarios = servicio / ".venv" / ("Scripts" if sys.platform == "win32" else "bin")
        binarios.mkdir(parents=True)
        (binarios / ("python.exe" if sys.platform == "win32" else "python")).touch()
    if env_py:
        (servicio / ".env").write_text("DB_URI=x\n", encoding="utf-8")
    if node_modules:
        (sidecar / "node_modules").mkdir()
    return tmp_path


class TestInterprete:
    def test_encuentra_el_venv_del_servicio(self, tmp_path):
        raiz = _proyecto(tmp_path)
        encontrado = iniciar.python_del_entorno(raiz / "sql_agent")
        assert encontrado is not None
        assert encontrado.parent.parent.name == ".venv"

    def test_sin_venv_devuelve_None_en_vez_de_adivinar(self, tmp_path):
        # Arrancar api.py con el python del sistema lo mata importando pyodbc,
        # y el error que se ve es un ImportError, no "falta el entorno".
        raiz = _proyecto(tmp_path, venv=False)
        assert iniciar.python_del_entorno(raiz / "sql_agent") is None


class TestRequisitos:
    def test_un_proyecto_completo_no_tiene_problemas(self, tmp_path, monkeypatch):
        monkeypatch.setattr(iniciar.shutil, "which", lambda _: "/usr/bin/node")
        assert iniciar.revisar_requisitos(_proyecto(tmp_path)) == []

    def test_nombra_cada_pieza_que_falta(self, tmp_path, monkeypatch):
        monkeypatch.setattr(iniciar.shutil, "which", lambda _: None)
        problemas = iniciar.revisar_requisitos(
            _proyecto(tmp_path, venv=False, env_py=False, node_modules=False)
        )
        assert len(problemas) == 4
        todo = " ".join(problemas)
        for esperado in ["entorno virtual", ".env", "npm install", "node"]:
            assert esperado in todo

    def test_el_env_del_sidecar_no_hace_falta(self, tmp_path, monkeypatch):
        # index.js no carga dotenv: lee process.env directo y todos sus
        # valores tienen respaldo. Exigir whatsapp/.env bloquearia una
        # instalacion que funciona, y de hecho la de esta maquina no lo tiene.
        monkeypatch.setattr(iniciar.shutil, "which", lambda _: "node")
        assert iniciar.revisar_requisitos(_proyecto(tmp_path)) == []

    def test_el_env_del_servicio_si_hace_falta(self, tmp_path, monkeypatch):
        # Ese si se lee (agent.py llama a load_dotenv) y sin DB_URI ni
        # OPENROUTER_API_KEY el servicio no levanta.
        monkeypatch.setattr(iniciar.shutil, "which", lambda _: "node")
        problemas = iniciar.revisar_requisitos(_proyecto(tmp_path, env_py=False))
        assert len(problemas) == 1
        assert "sql_agent" in problemas[0]


class TestUrlDeSalud:
    def test_usa_el_puerto_por_defecto_si_no_dice_nada(self, tmp_path):
        assert iniciar.url_de_salud(_proyecto(tmp_path)) == "http://127.0.0.1:8000/health"

    def test_respeta_el_puerto_del_env(self, tmp_path):
        # Si el servicio escucha en otro puerto y aqui se sondea el 8000, la
        # espera se agota y WhatsApp nunca arranca, con el servicio vivo.
        raiz = _proyecto(tmp_path)
        (raiz / "sql_agent" / ".env").write_text("API_PORT=9100\n", encoding="utf-8")
        assert iniciar.url_de_salud(raiz) == "http://127.0.0.1:9100/health"

    def test_a_una_interfaz_comodin_se_le_pregunta_por_loopback(self, tmp_path):
        # 0.0.0.0 significa "escucho en todas", no es una direccion a la que
        # se pueda llamar. urlopen contra http://0.0.0.0 no es portable.
        raiz = _proyecto(tmp_path)
        (raiz / "sql_agent" / ".env").write_text("API_HOST=0.0.0.0\n", encoding="utf-8")
        assert iniciar.url_de_salud(raiz).startswith("http://127.0.0.1:")


class TestSalidaDeLosProcesos:
    def test_cada_linea_dice_de_que_proceso_viene(self):
        assert iniciar.formatear("[api]", "escuchando") == "[api] escuchando"

    def test_no_repite_la_etiqueta_que_el_proceso_ya_puso(self):
        # El sidecar ya escribe "[whatsapp]" al frente de lo suyo, y sin esto
        # cada linea suya salia como "[whatsapp] [whatsapp] conectado".
        assert (
            iniciar.formatear("[whatsapp]", "[whatsapp] conectado")
            == "[whatsapp] conectado"
        )

    def test_el_qr_pasa_intacto(self):
        # El QR de vinculacion se dibuja con bloques. Un prefijo por linea lo
        # empuja y le mete texto al lado; el telefono deja de leerlo, y es la
        # unica salida del arranque que el usuario TIENE que poder usar.
        linea = "██▀▀██"
        assert iniciar.formatear("[whatsapp]", linea) == linea


class TestAnotacionDeEncendido:
    def test_sin_archivo_no_hay_nada_anotado(self, tmp_path):
        assert iniciar.leer_encendido(tmp_path / "no-existe.json") == []

    def test_un_archivo_corrupto_se_trata_como_vacio(self, tmp_path):
        # Preferible a reventar: un archivo a medio escribir no puede dejar
        # `apagar.py` inservible, que es justo cuando hace falta.
        archivo = tmp_path / "pids.json"
        archivo.write_text("{esto no es json", encoding="utf-8")
        assert iniciar.leer_encendido(archivo) == []

    def test_lo_anotado_se_lee_igual_y_en_el_mismo_orden(self, tmp_path):
        # El orden importa: WhatsApp se apaga primero, para que no entre un
        # mensaje cuando el servicio ya no esta para contestarlo.
        archivo = tmp_path / "pids.json"
        entradas = [
            {"nombre": "el sidecar de WhatsApp", "pid": 11, "marca": "index.js"},
            {"nombre": "el servicio Python", "pid": 22, "marca": "python.exe"},
        ]
        iniciar.anotar_encendido(entradas, archivo)
        assert iniciar.leer_encendido(archivo) == entradas

    def test_olvidar_no_se_queja_si_ya_no_estaba(self, tmp_path):
        iniciar.olvidar_encendido(tmp_path / "no-existe.json")


class TestApagarNoDejaHuerfanos:
    """Ninguno de los dos procesos que se lanzan es una hoja: el python.exe
    de un venv es un stub que re-ejecuta el interprete de verdad, asi que el
    que abre el puerto 8000 es un NIETO. Con `proceso.terminate()`, que solo
    le pega al hijo directo, ese nieto sobrevive y se queda con el puerto: el
    siguiente `python iniciar.py` se niega a arrancar con "Ya hay un servicio
    contestando".

    Se vieron huerfanos asi el 25 de septiembre de 2026, aunque en ese caso
    la causa fue otra -- algo mato al script por fuera y nunca llego a su
    `finally` --. El arbol de procesos es real igual, y estas pruebas lo
    ejercen con un padre y un nieto de mentira.
    """

    def _arbol(self, tmp_path):
        """Lanza un proceso que a su vez lanza otro, y devuelve los dos."""
        nieto = tmp_path / "nieto.py"
        nieto.write_text(
            "import time\nwhile True: time.sleep(0.2)\n", encoding="utf-8"
        )
        padre = tmp_path / "padre.py"
        padre.write_text(
            "import subprocess, sys, time\n"
            f"h = subprocess.Popen([sys.executable, r'{nieto}'])\n"
            "print(h.pid, flush=True)\n"
            "while True: time.sleep(0.2)\n",
            encoding="utf-8",
        )
        proceso = iniciar._lanzar([sys.executable, str(padre)], tmp_path)
        pid_nieto = int(proceso.stdout.readline().strip())
        return proceso, pid_nieto

    def _vivo(self, pid):
        return iniciar.linea_de_comando(pid) is not None

    def test_se_lleva_tambien_al_nieto(self, tmp_path):
        proceso, pid_nieto = self._arbol(tmp_path)
        assert self._vivo(pid_nieto), "el nieto tiene que arrancar vivo"

        iniciar._terminar(proceso, "el arbol de prueba", espera=5)

        assert proceso.poll() is not None, "el hijo directo murio"
        limite = time.monotonic() + 10
        while self._vivo(pid_nieto) and time.monotonic() < limite:
            time.sleep(0.3)
        assert not self._vivo(pid_nieto), "el nieto se quedo vivo (el bug)"

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="en POSIX el grupo de procesos sobrevive a su lider y el nieto si se alcanza",
    )
    def test_si_el_padre_ya_murio_el_nieto_queda_fuera_de_alcance(self, tmp_path):
        # Lo que este apagado NO puede hacer, escrito para que nadie lo
        # asuma: `taskkill /T` recorre el arbol desde el PID del padre, y si
        # ese PID ya no existe sus nietos quedan re-parentados y son
        # inalcanzables por PID. `apagar.py` tampoco los alcanza: solo mira
        # los PIDs que quedaron anotados.
        proceso, pid_nieto = self._arbol(tmp_path)
        proceso.kill()
        proceso.wait(timeout=10)

        iniciar._terminar(proceso, "el arbol de prueba", espera=5)
        assert self._vivo(pid_nieto), "si esto cambia, la limitacion se fue"

        # Sin dejar el nieto tirado en la maquina de quien corra las pruebas.
        iniciar.detener_pid(pid_nieto)

    def test_sobre_un_proceso_ya_muerto_no_revienta(self, tmp_path):
        proceso, _ = self._arbol(tmp_path)
        iniciar._terminar(proceso, "el arbol de prueba", espera=5)
        # Dos veces seguidas: el PID ya no existe y taskkill no lo encuentra.
        iniciar._terminar(proceso, "el arbol de prueba", espera=5)


class TestIdentidadDelProceso:
    def test_reconoce_un_proceso_vivo_por_su_marca(self):
        import os

        assert iniciar.sigue_siendo_el_nuestro(os.getpid(), "python")

    def test_un_pid_vivo_con_otra_marca_no_es_el_nuestro(self):
        # El caso que esto evita: el archivo de PIDs quedo de un arranque
        # viejo, el sistema reciclo el numero, y apagar.py mataria a un
        # proceso ajeno que no tiene nada que ver con el bot.
        import os

        assert not iniciar.sigue_siendo_el_nuestro(os.getpid(), "netscape-navigator")

    def test_un_pid_que_no_existe_no_es_el_nuestro(self):
        assert not iniciar.sigue_siendo_el_nuestro(999_999_999, "python")


class TestElPanelSeAbreSolo:
    def test_por_defecto_el_panel_se_abre(self):
        # Es un programa con ventana: al arrancarlo uno espera ver algo, no
        # una terminal con una direccion que hay que copiar.
        assert iniciar.opciones([])["abrir_el_panel"] is True

    def test_sin_panel_lo_evita(self):
        # Para las vueltas de depuracion, donde el bot se reinicia cada
        # minuto y una ventana nueva cada vez estorba.
        assert iniciar.opciones(["--sin-panel"])["abrir_el_panel"] is False

    def test_un_argumento_cualquiera_no_apaga_el_panel(self):
        assert iniciar.opciones(["--otra-cosa"])["abrir_el_panel"] is True


class TestLaConsolaQuedaEnElLog:
    """Antes los logs solo vivian en la ventana de la terminal: cerrarla los
    perdia. Ahora todo lo que se imprime pasa a la consola igual que antes y
    ademas queda escrito, una vez, en el log del dia.
    """

    class _Escritor:
        def __init__(self):
            self.lineas = []

        def escribir(self, linea):
            self.lineas.append(linea)

    def test_la_consola_ve_lo_mismo_y_el_log_recibe_cada_linea(self):
        import io

        destino, escritor = io.StringIO(), self._Escritor()
        consola = iniciar._ConsolaConLog(destino, escritor)
        print("[whatsapp] conectado", file=consola)
        print("[iniciar] el sidecar de WhatsApp termino (codigo 1).", file=consola)

        assert destino.getvalue() == (
            "[whatsapp] conectado\n[iniciar] el sidecar de WhatsApp termino (codigo 1).\n"
        )
        assert escritor.lineas == [
            "[whatsapp] conectado",
            "[iniciar] el sidecar de WhatsApp termino (codigo 1).",
        ]

    def test_una_linea_que_llega_en_pedazos_se_anota_entera_y_una_sola_vez(self):
        import io

        escritor = self._Escritor()
        consola = iniciar._ConsolaConLog(io.StringIO(), escritor)
        consola.write("[api] INFO: ")
        consola.write("Uvicorn running")
        assert escritor.lineas == []
        consola.write("\n")
        assert escritor.lineas == ["[api] INFO: Uvicorn running"]

    def test_lo_demas_del_flujo_sigue_disponible(self):
        import io

        consola = iniciar._ConsolaConLog(io.StringIO(), self._Escritor())
        # flush y los atributos del flujo original (encoding, etc.) siguen
        # ahi: hay codigo que los consulta.
        consola.flush()
        assert hasattr(consola, "getvalue")
