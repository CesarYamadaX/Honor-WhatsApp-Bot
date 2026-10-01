"""Pruebas de los logs en disco y de como los lee el panel.

Antes los logs solo vivian en la ventana de la terminal: cerrarla los perdia.
Lo que importa aqui es que se guarde lo util y no el ruido, que se clasifique
bien lo que es un error de lo que el bot resuelve solo, y que el panel no
pueda leer otra cosa del disco.
"""

from datetime import datetime, timedelta
from itertools import chain

import pytest
from fastapi.testclient import TestClient

import registro_de_logs as logs
from api import crear_app
from config import Config


def _reloj(inicio, paso=timedelta(seconds=30)):
    # La primera consulta es la del propio escritor al crearse (para podar
    # los dias viejos): no es una linea, asi que no adelanta la hora.
    momentos = chain([inicio], (inicio + paso * i for i in range(100_000)))
    return lambda: next(momentos)


def _escritor(carpeta, inicio=datetime(2026, 10, 1, 9, 0)):
    return logs.EscritorDeLogs(carpeta, reloj=_reloj(inicio))


class TestQueSeGuarda:
    def test_cada_linea_va_con_fecha_y_hora(self, tmp_path):
        _escritor(tmp_path).escribir("[whatsapp] conectado")
        contenido = (tmp_path / "bot-2026-10-01.log").read_text(encoding="utf-8")
        assert contenido == "2026-10-01 09:00:00 [whatsapp] conectado\n"

    def test_el_sondeo_del_panel_y_el_health_no_se_guardan(self, tmp_path):
        # El panel pregunta cada 2 segundos: serian ~43 000 lineas al dia que
        # taparian todo lo demas.
        escritor = _escritor(tmp_path)
        assert not escritor.escribir('[api] INFO:     127.0.0.1:1 - "GET /panel/actividad HTTP/1.1" 200 OK')
        assert not escritor.escribir('[api] INFO:     127.0.0.1:1 - "GET /health HTTP/1.1" 200 OK')
        assert escritor.escribir('[api] INFO:     127.0.0.1:1 - "POST /ask HTTP/1.1" 200 OK')

    def test_el_qr_y_las_lineas_vacias_no_se_guardan(self, tmp_path):
        escritor = _escritor(tmp_path)
        assert not escritor.escribir("█▀▀▀▀▀█ ▀▄█ █▀▀▀▀▀█")
        assert not escritor.escribir("   ")

    def test_un_archivo_por_dia(self, tmp_path):
        escritor = logs.EscritorDeLogs(
            tmp_path, reloj=_reloj(datetime(2026, 10, 1, 23, 59, 50), timedelta(seconds=20))
        )
        escritor.escribir("[iniciar] antes de medianoche")
        escritor.escribir("[iniciar] despues de medianoche")
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "bot-2026-10-01.log", "bot-2026-10-02.log",
        ]

    def test_borra_los_dias_viejos_al_arrancar(self, tmp_path):
        for dia in ("2026-09-01", "2026-09-20", "2026-09-30"):
            (tmp_path / f"bot-{dia}.log").write_text("", encoding="utf-8")
        (tmp_path / "otra-cosa.log").write_text("", encoding="utf-8")
        logs.EscritorDeLogs(tmp_path, dias=14, reloj=lambda: datetime(2026, 10, 1, 9, 0))
        # Se van los de mas de 14 dias; lo que no es un log del bot se queda.
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "bot-2026-09-20.log", "bot-2026-09-30.log", "otra-cosa.log",
        ]

    def test_un_fallo_al_escribir_no_revienta(self, tmp_path):
        archivo = tmp_path / "no-es-carpeta"
        archivo.write_text("", encoding="utf-8")
        assert logs.EscritorDeLogs(archivo).escribir("[iniciar] hola") is False


class TestComoSeClasifica:
    @pytest.mark.parametrize("linea, esperado", [
        ("[whatsapp] conectado", ("whatsapp", "info")),
        ("[whatsapp] desconectado (connectionClosed, codigo 428): Connection Terminated", ("whatsapp", "aviso")),
        ("[whatsapp] reintentando conexion en 2000ms (intento 1)", ("whatsapp", "aviso")),
        # Una reconexion que el sidecar resuelve solo, aunque diga "Error".
        ("[whatsapp] desconectado (connectionLost, codigo 408): WebSocket Error (Opening handshake has timed out)", ("whatsapp", "aviso")),
        ("[api] ERROR:sql_agent.api:Fallo el agente para x", ("api", "error")),
        ("[api] Traceback (most recent call last):", ("api", "error")),
        ('[api]   File "agent.py", line 10, in responder', ("api", "error")),
        ("[api] pyodbc.ProgrammingError: ('42000', 'x')", ("api", "error")),
        ('[api] INFO:     127.0.0.1:1 - "POST /ask HTTP/1.1" 500 Internal Server Error', ("api", "error")),
        ("[iniciar] el sidecar de WhatsApp termino (codigo 1). Bajo el otro tambien.", ("iniciar", "error")),
        ("[whatsapp] fallo al (re)conectar: x", ("whatsapp", "error")),
        ("  El bot esta encendido.", ("otro", "info")),
    ])
    def test_fuente_y_nivel(self, linea, esperado):
        assert logs.clasificar(linea) == esperado


@pytest.fixture
def con_logs(tmp_path):
    _escritor(tmp_path, datetime(2026, 9, 30, 9, 0)).escribir("[whatsapp] conectado")
    escritor = _escritor(tmp_path)
    for linea in (
        "[iniciar] arrancando el servicio Python (python.exe)...",
        "[whatsapp] conectado",
        "[whatsapp] desconectado (connectionClosed, codigo 428): Connection Terminated",
        "[api] ERROR:sql_agent.api:La base de datos no contesta (x)",
        "[whatsapp] conectado",
    ):
        escritor.escribir(linea)
    return tmp_path


class TestComoSeLee:
    def test_los_dias_van_del_mas_nuevo_al_mas_viejo_con_sus_problemas(self, con_logs):
        assert logs.dias(con_logs) == [
            {"dia": "2026-10-01", "lineas": 5, "errores": 1, "avisos": 1, "ultima": "09:02:00"},
            {"dia": "2026-09-30", "lineas": 1, "errores": 0, "avisos": 0, "ultima": "09:00:00"},
        ]

    def test_filtros(self, con_logs):
        assert logs.leer("2026-10-01", con_logs)["total"] == 5
        assert logs.leer("2026-10-01", con_logs, fuente="whatsapp")["total"] == 3
        problemas = logs.leer("2026-10-01", con_logs, solo_problemas=True)
        assert [l["nivel"] for l in problemas["lineas"]] == ["aviso", "error"]

    def test_el_limite_se_queda_con_lo_mas_nuevo(self, con_logs):
        leido = logs.leer("2026-10-01", con_logs, limite=2)
        assert leido["total"] == 5
        assert [l["hora"] for l in leido["lineas"]] == ["09:01:30", "09:02:00"]

    def test_no_se_puede_leer_otro_archivo_del_disco(self, con_logs):
        for malo in ("../../.env", "2026-10-01/../x", "", "hoy"):
            with pytest.raises(ValueError):
                logs.leer(malo, con_logs)

    def test_un_dia_sin_archivo_viene_vacio(self, con_logs):
        assert logs.leer("2020-01-01", con_logs) == {"dia": "2020-01-01", "total": 0, "lineas": []}

    def test_el_estado_de_whatsapp_es_lo_ultimo_que_se_dijo(self, con_logs):
        estado = logs.estado_whatsapp(con_logs)
        assert estado["estado"] == "conectado" and estado["hora"] == "09:02:00"

    def test_desconectado_y_qr(self, tmp_path):
        escritor = _escritor(tmp_path)
        escritor.escribir("[whatsapp] conectado")
        escritor.escribir("[whatsapp] desconectado (connectionLost, codigo 408): x")
        assert logs.estado_whatsapp(tmp_path)["estado"] == "desconectado"
        escritor.escribir("[whatsapp] Escanea este codigo con WhatsApp > Dispositivos vinculados:")
        assert logs.estado_whatsapp(tmp_path)["estado"] == "qr"

    def test_sin_logs_el_estado_es_desconocido(self, tmp_path):
        assert logs.estado_whatsapp(tmp_path)["estado"] == "desconocido"


def _config(host="127.0.0.1"):
    return Config(allowlist=frozenset({"5215512345678"}), timeout_segundos=90,
                  ttl_minutos=0, api_host=host, api_port=8000)


class TestEndpoints:
    def test_dias_y_estado(self, con_logs):
        cliente = TestClient(crear_app(_config(), lambda: None, carpeta_logs=con_logs))
        datos = cliente.get("/panel/logs/dias").json()
        assert [d["dia"] for d in datos["dias"]] == ["2026-10-01", "2026-09-30"]
        assert datos["whatsapp"]["estado"] == "conectado"

    def test_lineas_con_filtros(self, con_logs):
        cliente = TestClient(crear_app(_config(), lambda: None, carpeta_logs=con_logs))
        r = cliente.get("/panel/logs", params={"dia": "2026-10-01", "fuente": "api"})
        assert r.status_code == 200 and r.json()["total"] == 1
        r = cliente.get("/panel/logs", params={"dia": "2026-10-01", "solo_problemas": True})
        assert r.json()["total"] == 2

    def test_rechaza_dias_y_fuentes_invalidos(self, con_logs):
        cliente = TestClient(crear_app(_config(), lambda: None, carpeta_logs=con_logs))
        assert cliente.get("/panel/logs", params={"dia": "../../.env"}).status_code == 400
        assert cliente.get(
            "/panel/logs", params={"dia": "2026-10-01", "fuente": "x"}
        ).status_code == 400

    def test_fuera_de_loopback_el_panel_no_muestra_logs(self, con_logs):
        # Los logs traen consultas SQL y numeros de telefono: sin
        # autenticacion, solo se ven desde la propia maquina.
        cliente = TestClient(crear_app(_config("0.0.0.0"), lambda: None, carpeta_logs=con_logs))
        assert cliente.get("/panel/logs/dias").status_code == 403
        assert cliente.get("/panel/logs", params={"dia": "2026-10-01"}).status_code == 403
