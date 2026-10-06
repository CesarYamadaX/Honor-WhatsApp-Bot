"""Pruebas del panel: administrar usuarios y mirar que hace el bot.

El panel manda sobre la allowlist, asi que dos cosas importan mas que el
resto: que un cambio hecho aqui se note de inmediato en quien puede usar el
bot, y que el panel no quede expuesto fuera de la maquina.
"""

import base64

import pytest
from fastapi.testclient import TestClient

from api import crear_app
from bitacora import Bitacora
from config import Config
from usuarios import RegistroUsuarios

AUTORIZADO = "5215512345678@s.whatsapp.net"
NUEVO = "5215599998888"


def _config(host="127.0.0.1"):
    return Config(
        allowlist=frozenset({"5215512345678"}),
        timeout_segundos=90,
        ttl_minutos=60,
        api_host=host,
        api_port=8000,
        espera_cancelacion=0.2,
    )


class AgenteFalso:
    idioma = "es"

    def __init__(self):
        self.preguntas = []

    def responder(self, pregunta, cancelacion=None, cadenas=None):
        self.preguntas.append(pregunta)
        return "listo", []


@pytest.fixture
def usuarios():
    return RegistroUsuarios(semilla=["5215512345678"])


@pytest.fixture
def diario():
    return Bitacora()


@pytest.fixture
def cliente(usuarios, diario):
    app = crear_app(_config(), AgenteFalso, usuarios, diario)
    with TestClient(app) as c:
        yield c


class TestPantalla:
    def test_el_panel_se_sirve(self, cliente):
        respuesta = cliente.get("/panel")
        assert respuesta.status_code == 200
        assert "Panel del bot" in respuesta.text


class TestAdministracionDeUsuarios:
    def test_se_listan_los_usuarios(self, cliente):
        datos = cliente.get("/panel/usuarios").json()
        assert [u["numero"] for u in datos["usuarios"]] == ["5215512345678"]

    def test_dar_de_alta_habilita_a_alguien_de_inmediato(self, cliente, usuarios):
        # Sin reiniciar: es la razon de ser del panel. Antes habia que editar
        # el .env y bajar los dos procesos.
        chat = f"{NUEVO}@s.whatsapp.net"
        assert cliente.post("/ask", json={"chat_id": chat, "text": "hola"}).status_code == 403

        cliente.post("/panel/usuarios", json={"numero": NUEVO, "nombre": "Laura"})
        assert cliente.post("/ask", json={"chat_id": chat, "text": "hola"}).status_code == 200

    def test_suspender_corta_el_acceso_de_inmediato(self, cliente):
        cliente.post("/panel/usuarios/suspender", json={"numero": "5215512345678"})
        respuesta = cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "hola"})
        assert respuesta.status_code == 403

    def test_reactivar_devuelve_el_acceso(self, cliente):
        cliente.post("/panel/usuarios/suspender", json={"numero": "5215512345678"})
        cliente.post("/panel/usuarios/reactivar", json={"numero": "5215512345678"})
        assert cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "x"}).status_code == 200

    def test_eliminar_lo_saca_de_la_lista(self, cliente):
        cliente.post("/panel/usuarios/eliminar", json={"numero": "5215512345678"})
        assert cliente.get("/panel/usuarios").json()["usuarios"] == []
        assert cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "x"}).status_code == 403

    def test_un_numero_invalido_se_explica_en_vez_de_reventar(self, cliente):
        respuesta = cliente.post("/panel/usuarios", json={"numero": "no soy numero"})
        assert respuesta.status_code == 400
        assert "digito" in respuesta.json()["detail"]

    def test_dar_de_alta_dos_veces_avisa(self, cliente):
        respuesta = cliente.post("/panel/usuarios", json={"numero": "5215512345678"})
        assert respuesta.status_code == 400
        assert "ya esta" in respuesta.json()["detail"]

    def test_un_suspendido_no_recibe_ni_el_indicador_de_escribiendo(self, cliente):
        # /authorized es lo que el sidecar consulta ANTES de mostrar señales
        # de vida. Si aqui dijera que si, un suspendido veria al bot
        # "escribiendo" y sabria que sigue existiendo.
        cliente.post("/panel/usuarios/suspender", json={"numero": "5215512345678"})
        assert cliente.post("/authorized", json={"chat_id": AUTORIZADO}).status_code == 403


class TestVerLoQueHaceElBot:
    def test_una_conversacion_queda_registrada(self, cliente, diario):
        cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "ventas de Elektra"})
        turno = diario.en_curso()
        assert turno == []  # ya termino

    def test_las_conversaciones_se_pueden_listar(self, cliente, tmp_path, usuarios):
        diario = Bitacora(tmp_path / "bitacora")
        app = crear_app(_config(), AgenteFalso, usuarios, diario)
        with TestClient(app) as c:
            c.post("/ask", json={"chat_id": AUTORIZADO, "text": "ventas de Elektra"})
            chats = c.get("/panel/conversaciones").json()["chats"]
            assert chats[0]["chat_id"] == AUTORIZADO
            assert chats[0]["ultima_pregunta"] == "ventas de Elektra"

            turnos = c.get(f"/panel/conversaciones?chat_id={AUTORIZADO}").json()["turnos"]
            assert turnos[0]["respuesta"] == "listo"

    def test_la_actividad_dice_cuantas_conversaciones_hay_vivas(self, cliente):
        cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "hola"})
        datos = cliente.get("/panel/actividad").json()
        assert datos["sesiones"] == 1
        assert datos["en_curso"] == []

    def test_un_error_del_agente_queda_registrado_como_error(self, tmp_path, usuarios):
        # Para eso sirve mirar la bitacora: saber que fallo sin leer el log
        # del proceso, donde el motivo esta entre stack traces.
        class AgenteQueFalla:
            idioma = "es"

            def responder(self, pregunta, cancelacion=None, cadenas=None):
                raise RuntimeError("timeout expired")

        diario = Bitacora(tmp_path / "bitacora")
        app = crear_app(_config(), AgenteQueFalla, usuarios, diario)
        with TestClient(app) as c:
            c.post("/ask", json={"chat_id": AUTORIZADO, "text": "algo pesado"})
            turnos = c.get(f"/panel/conversaciones?chat_id={AUTORIZADO}").json()["turnos"]

        assert turnos[0]["estado"] == "error"
        assert "timeout" in turnos[0]["respuesta"]


class TestNoQuedaExpuesto:
    def test_fuera_de_loopback_el_panel_se_apaga(self, usuarios, diario):
        # El servicio no tiene autenticacion; su unica defensa es escuchar
        # solo en la maquina. El panel da de alta usuarios y deja leer
        # conversaciones, asi que atado a 0.0.0.0 no puede quedar servido.
        app = crear_app(_config(host="0.0.0.0"), AgenteFalso, usuarios, diario)
        with TestClient(app) as c:
            for ruta in ["/panel", "/panel/usuarios", "/panel/actividad", "/panel/conversaciones"]:
                assert c.get(ruta).status_code == 403, ruta
            assert c.post("/panel/usuarios", json={"numero": NUEVO}).status_code == 403

    def test_pero_el_bot_sigue_funcionando(self, usuarios, diario):
        # Apagar el panel no puede dejar sin servicio a WhatsApp.
        app = crear_app(_config(host="0.0.0.0"), AgenteFalso, usuarios, diario)
        with TestClient(app) as c:
            assert c.post("/ask", json={"chat_id": AUTORIZADO, "text": "x"}).status_code == 200


class TestVerTodoElHistorial:
    def _conversacion(self, tmp_path, usuarios, cuantos):
        diario = Bitacora(tmp_path / "bitacora")
        app = crear_app(_config(), AgenteFalso, usuarios, diario)
        cliente = TestClient(app)
        with cliente:
            for i in range(cuantos):
                cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": f"pregunta {i}"})
            yield cliente

    def test_una_conversacion_larga_se_trae_por_paginas(self, tmp_path, usuarios):
        # Traerla entera dejaria al panel pintando meses de historia cada vez
        # que se refresca.
        for cliente in self._conversacion(tmp_path, usuarios, 60):
            datos = cliente.get(
                f"/panel/conversaciones?chat_id={AUTORIZADO}&limite=50"
            ).json()
            assert len(datos["turnos"]) == 50
            assert datos["hay_mas"] is True
            assert datos["total"] == 60

    def test_la_segunda_pagina_trae_los_anteriores(self, tmp_path, usuarios):
        for cliente in self._conversacion(tmp_path, usuarios, 60):
            segunda = cliente.get(
                f"/panel/conversaciones?chat_id={AUTORIZADO}&limite=50&salto=50"
            ).json()
            assert [t["pregunta"] for t in segunda["turnos"]] == [
                f"pregunta {i}" for i in range(9, -1, -1)
            ]
            assert segunda["hay_mas"] is False

    def test_sin_mas_turnos_no_se_ofrece_seguir(self, tmp_path, usuarios):
        # `hay_mas` es lo que decide si el panel muestra "ver anteriores";
        # un boton que no trae nada es peor que no tenerlo.
        for cliente in self._conversacion(tmp_path, usuarios, 3):
            datos = cliente.get(
                f"/panel/conversaciones?chat_id={AUTORIZADO}&limite=50"
            ).json()
            assert datos["hay_mas"] is False
            assert len(datos["turnos"]) == 3


class TestEditarUsuarios:
    def _cliente(self, tmp_path, usuarios):
        from fotos import AlmacenDeFotos

        app = crear_app(
            _config(), AgenteFalso, usuarios, Bitacora(), AlmacenDeFotos(tmp_path / "fotos")
        )
        return TestClient(app)

    def _imagen(self):
        import io

        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (600, 400), (30, 120, 200)).save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode()

    def test_se_le_puede_poner_nombre_a_un_numero(self, cliente):
        cliente.post("/panel/usuarios/renombrar", json={"numero": "5215512345678", "nombre": "Laura, ventas"})
        usuario = cliente.get("/panel/usuarios").json()["usuarios"][0]
        assert usuario["nombre"] == "Laura, ventas"

    def test_el_nombre_se_puede_dejar_vacio(self, cliente):
        # Quitar el nombre es una edicion valida, no un error de validacion.
        cliente.post("/panel/usuarios/renombrar", json={"numero": "5215512345678", "nombre": "Laura"})
        cliente.post("/panel/usuarios/renombrar", json={"numero": "5215512345678", "nombre": ""})
        assert cliente.get("/panel/usuarios").json()["usuarios"][0]["nombre"] == ""

    def test_renombrar_no_toca_el_estado(self, cliente):
        # El nombre es una etiqueta; cambiarla no puede devolverle el acceso
        # a alguien a quien se suspendio.
        cliente.post("/panel/usuarios/suspender", json={"numero": "5215512345678"})
        cliente.post("/panel/usuarios/renombrar", json={"numero": "5215512345678", "nombre": "Laura"})

        assert cliente.get("/panel/usuarios").json()["usuarios"][0]["estado"] == "suspendido"
        assert cliente.post("/ask", json={"chat_id": AUTORIZADO, "text": "x"}).status_code == 403

    def test_una_foto_se_guarda_y_se_sirve(self, tmp_path, usuarios):
        with self._cliente(tmp_path, usuarios) as cliente:
            respuesta = cliente.post(
                "/panel/usuarios/foto",
                json={"numero": "5215512345678", "imagen": self._imagen()},
            )
            assert respuesta.status_code == 200
            archivo = respuesta.json()["usuario"]["foto"]
            assert archivo.startswith("5215512345678-")

            foto = cliente.get(f"/panel/foto/{archivo}")
            assert foto.status_code == 200
            assert foto.headers["content-type"] == "image/jpeg"

    def test_cambiar_la_foto_borra_la_anterior(self, tmp_path, usuarios):
        # Si no, la carpeta acumula una foto por cada vez que alguien
        # cambio de opinion, y ninguna se puede distinguir de la buena.
        import io

        from PIL import Image

        otra = io.BytesIO()
        Image.new("RGB", (300, 300), (240, 10, 10)).save(otra, format="PNG")

        with self._cliente(tmp_path, usuarios) as cliente:
            primera = cliente.post(
                "/panel/usuarios/foto",
                json={"numero": "5215512345678", "imagen": self._imagen()},
            ).json()["usuario"]["foto"]
            segunda = cliente.post(
                "/panel/usuarios/foto",
                json={
                    "numero": "5215512345678",
                    "imagen": base64.b64encode(otra.getvalue()).decode(),
                },
            ).json()["usuario"]["foto"]

            assert primera != segunda
            assert cliente.get(f"/panel/foto/{primera}").status_code == 404
            assert cliente.get(f"/panel/foto/{segunda}").status_code == 200

    def test_quitar_la_foto_la_borra_del_disco(self, tmp_path, usuarios):
        with self._cliente(tmp_path, usuarios) as cliente:
            archivo = cliente.post(
                "/panel/usuarios/foto",
                json={"numero": "5215512345678", "imagen": self._imagen()},
            ).json()["usuario"]["foto"]

            cliente.post("/panel/usuarios/quitar-foto", json={"numero": "5215512345678"})
            assert cliente.get("/panel/usuarios").json()["usuarios"][0]["foto"] == ""
            assert cliente.get(f"/panel/foto/{archivo}").status_code == 404

    def test_lo_que_no_es_una_imagen_se_explica(self, tmp_path, usuarios):
        with self._cliente(tmp_path, usuarios) as cliente:
            respuesta = cliente.post(
                "/panel/usuarios/foto",
                json={
                    "numero": "5215512345678",
                    "imagen": base64.b64encode(b"no soy una imagen").decode(),
                },
            )
            assert respuesta.status_code == 400
            assert "imagen" in respuesta.json()["detail"]

    def test_un_nombre_de_foto_con_ruta_no_sirve_archivos_de_fuera(self, tmp_path, usuarios):
        # El nombre viene del navegador. Sin la validacion del almacen,
        # esto serviria cualquier archivo de la maquina.
        with self._cliente(tmp_path, usuarios) as cliente:
            for intento in ["..%2F..%2F.env", "....//....//.env", "cualquiera.jpg"]:
                assert cliente.get(f"/panel/foto/{intento}").status_code in (404, 400), intento

    def test_fuera_de_loopback_tampoco_se_editan_usuarios(self, usuarios, diario):
        app = crear_app(_config(host="0.0.0.0"), AgenteFalso, usuarios, diario)
        with TestClient(app) as cliente:
            assert cliente.post(
                "/panel/usuarios/renombrar", json={"numero": "5215512345678", "nombre": "x"}
            ).status_code == 403
            assert cliente.get("/panel/foto/5215512345678-a1b2c3d4.jpg").status_code == 403


class TestIconoDeLaApp:
    def test_se_sirve_el_tamaño_que_se_pide(self, cliente):
        respuesta = cliente.get("/panel/icono/icono-32.png")
        assert respuesta.status_code == 200
        assert respuesta.headers["content-type"] == "image/png"
        assert len(respuesta.content) > 200

    def test_el_html_ofrece_varios_tamaños(self, cliente):
        # El navegador elige; a 16 px quiere el de 16, no uno de 256
        # reducido a la carrera.
        pagina = cliente.get("/panel").text
        for lado in (16, 32, 48):
            assert f'sizes="{lado}x{lado}"' in pagina

    def test_un_nombre_inventado_no_sirve_archivos_de_fuera(self, cliente):
        # El nombre viene del navegador, como el de las fotos.
        for intento in ["../../.env", "icono-32.png.txt", "cualquiera.png"]:
            assert cliente.get(f"/panel/icono/{intento}").status_code in (404, 400), intento

    def test_fuera_de_loopback_tampoco(self, usuarios, diario):
        app = crear_app(_config(host="0.0.0.0"), AgenteFalso, usuarios, diario)
        with TestClient(app) as c:
            assert c.get("/panel/icono/icono-32.png").status_code == 403
