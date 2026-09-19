"""Pruebas del registro de usuarios.

Este archivo ES la allowlist, o sea la unica cosa que separa la base de datos
de la empresa de cualquiera que tenga el numero del bot. Casi todas las
pruebas de aqui son sobre no dejar entrar a quien no debe.
"""

import json

import pytest
from usuarios import (
    ACTIVO,
    SUSPENDIDO,
    ErrorDeUsuarios,
    RegistroUsuarios,
    normalizar_numero,
)

NUMERO = "5215512345678"
OTRO = "5215599998888"


@pytest.fixture
def registro(tmp_path):
    return RegistroUsuarios(tmp_path / "usuarios.json")


class TestNormalizacion:
    def test_quita_todo_lo_que_no_sea_digito(self):
        # El mismo telefono se teclea de cinco formas; sin normalizar, entra
        # cinco veces y ninguna casa con lo que manda el sidecar.
        for crudo in ["+52 1 55 1234 5678", "52-1-5512345678", "(521) 5512345678"]:
            assert normalizar_numero(crudo) == NUMERO

    def test_rechaza_lo_que_no_es_un_numero(self):
        with pytest.raises(ErrorDeUsuarios):
            normalizar_numero("no soy un numero")

    def test_rechaza_un_numero_demasiado_corto(self):
        # Un dedazo como "55123" daria de alta algo que nunca va a casar.
        with pytest.raises(ErrorDeUsuarios):
            normalizar_numero("55123")


class TestAltasYBajas:
    def test_un_numero_nuevo_queda_activo(self, registro):
        usuario = registro.agregar(NUMERO, nombre="Laura")
        assert usuario.estado == ACTIVO
        assert registro.puede_usar(NUMERO)

    def test_no_se_puede_dar_de_alta_dos_veces(self, registro):
        registro.agregar(NUMERO)
        with pytest.raises(ErrorDeUsuarios):
            registro.agregar(NUMERO)

    def test_el_alta_acepta_el_numero_como_lo_escriban(self, registro):
        registro.agregar("+52 1 55 1234 5678")
        assert registro.puede_usar(NUMERO)

    def test_eliminar_lo_saca_de_la_lista(self, registro):
        registro.agregar(NUMERO)
        registro.eliminar(NUMERO)
        assert not registro.puede_usar(NUMERO)
        assert registro.obtener(NUMERO) is None

    def test_no_se_puede_eliminar_a_quien_no_esta(self, registro):
        with pytest.raises(ErrorDeUsuarios):
            registro.eliminar(NUMERO)


class TestEstados:
    def test_un_suspendido_no_puede_usar_el_bot(self, registro):
        registro.agregar(NUMERO)
        registro.suspender(NUMERO, nota="mando consultas a las 3 am")
        assert not registro.puede_usar(NUMERO)
        assert registro.obtener(NUMERO).estado == SUSPENDIDO

    def test_reactivar_devuelve_el_acceso(self, registro):
        registro.agregar(NUMERO)
        registro.suspender(NUMERO)
        registro.reactivar(NUMERO)
        assert registro.puede_usar(NUMERO)

    def test_la_suspension_no_se_levanta_sola(self, registro):
        # Es una decision, no un plazo. Hubo una suspension con fecha de
        # regreso y se quito: en el panel eran dos botones que no se
        # distinguian de un vistazo.
        registro.agregar(NUMERO)
        registro.suspender(NUMERO)
        otra_vez = RegistroUsuarios(registro._archivo)
        assert not otra_vez.puede_usar(NUMERO)

    def test_no_se_puede_cambiar_a_quien_no_existe(self, registro):
        for accion in (registro.suspender, registro.reactivar):
            with pytest.raises(ErrorDeUsuarios):
                accion(NUMERO)


class TestDesconocidos:
    def test_un_numero_que_nunca_estuvo_no_puede(self, registro):
        assert not registro.puede_usar(NUMERO)

    def test_un_registro_vacio_no_deja_entrar_a_nadie(self, registro):
        # Lo importante es el sentido del fallo: vacio significa "nadie", no
        # "todos". Un archivo recien creado no puede abrir la base entera.
        assert not registro.puede_usar("999999999999")


class TestPersistencia:
    def test_lo_que_se_guarda_se_vuelve_a_leer(self, tmp_path):
        archivo = tmp_path / "usuarios.json"
        primero = RegistroUsuarios(archivo)
        primero.agregar(NUMERO, nombre="Laura")
        primero.suspender(NUMERO, nota="prueba")

        segundo = RegistroUsuarios(archivo)
        usuario = segundo.obtener(NUMERO)
        assert usuario.nombre == "Laura"
        assert usuario.estado == SUSPENDIDO
        assert usuario.nota == "prueba"

    def test_un_archivo_corrupto_niega_a_todos_en_vez_de_reventar(self, tmp_path):
        # El peor final posible seria que un JSON a medio escribir dejara al
        # servicio sin allowlist. Arrancar vacio niega a todo el mundo, que
        # es un fallo molesto pero seguro.
        archivo = tmp_path / "usuarios.json"
        archivo.write_text("{roto", encoding="utf-8")
        registro = RegistroUsuarios(archivo)
        assert registro.listar() == []
        assert not registro.puede_usar(NUMERO)

    def test_la_semilla_da_de_alta_a_los_del_env(self, tmp_path):
        registro = RegistroUsuarios(tmp_path / "u.json", semilla=[NUMERO, OTRO])
        assert registro.puede_usar(NUMERO)
        assert registro.puede_usar(OTRO)

    def test_la_semilla_no_revive_a_un_suspendido(self, tmp_path):
        # WHATSAPP_ALLOWED_NUMBERS se sigue leyendo en cada arranque. Si la
        # semilla pisara lo guardado, suspender a alguien duraria hasta el
        # siguiente reinicio y nadie entenderia por que volvio.
        archivo = tmp_path / "u.json"
        registro = RegistroUsuarios(archivo, semilla=[NUMERO])
        registro.suspender(NUMERO)

        otra_vez = RegistroUsuarios(archivo, semilla=[NUMERO])
        assert not otra_vez.puede_usar(NUMERO)

    def test_un_archivo_viejo_con_bloqueados_se_sigue_leyendo(self, tmp_path):
        # Los registros escritos antes traen estado "bloqueado" y un campo
        # suspendido_hasta que ya no existe. Si eso tumbara la carga, el bot
        # arrancaria sin allowlist y sin que nadie se entere.
        archivo = tmp_path / "u.json"
        archivo.write_text(
            json.dumps({"usuarios": [{
                "numero": NUMERO, "nombre": "Laura", "estado": "bloqueado",
                "suspendido_hasta": "2026-01-01", "nota": "", 
                "creado": "2026-01-01T00:00:00", "actualizado": "2026-01-01T00:00:00",
            }]}),
            encoding="utf-8",
        )
        registro = RegistroUsuarios(archivo)
        usuario = registro.obtener(NUMERO)
        assert usuario.nombre == "Laura"
        assert usuario.estado == SUSPENDIDO
        assert not registro.puede_usar(NUMERO)

    def test_la_semilla_ignora_basura_sin_tumbar_el_arranque(self, tmp_path):
        registro = RegistroUsuarios(tmp_path / "u.json", semilla=["", "x", NUMERO])
        assert [u.numero for u in registro.listar()] == [NUMERO]

    def test_el_archivo_queda_legible_para_un_humano(self, tmp_path):
        # Se edita a mano cuando el panel no esta a la mano.
        archivo = tmp_path / "u.json"
        RegistroUsuarios(archivo).agregar(NUMERO, nombre="Laura")
        datos = json.loads(archivo.read_text(encoding="utf-8"))
        assert datos["usuarios"][0]["numero"] == NUMERO
        assert "\n" in archivo.read_text(encoding="utf-8")

    def test_sin_archivo_funciona_igual_en_memoria(self):
        # Asi lo usan las pruebas de la API y la CLI, sin dejar basura.
        registro = RegistroUsuarios(semilla=[NUMERO])
        assert registro.puede_usar(NUMERO)
        registro.suspender(NUMERO)
        assert not registro.puede_usar(NUMERO)
