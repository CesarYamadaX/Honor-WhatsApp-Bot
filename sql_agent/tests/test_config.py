import pytest

from config import Config, ConfigError, normalizar_chat_id


class TestNormalizarChatId:
    def test_chat_individual_devuelve_el_numero(self):
        assert normalizar_chat_id("5215512345678@s.whatsapp.net") == "5215512345678"

    def test_ignora_el_sufijo_de_dispositivo(self):
        # Baileys a veces incluye el id de dispositivo despues de dos puntos
        assert normalizar_chat_id("5215512345678:12@s.whatsapp.net") == "5215512345678"

    def test_grupo_devuelve_none(self):
        assert normalizar_chat_id("120363001234567890@g.us") is None

    def test_difusion_devuelve_none(self):
        assert normalizar_chat_id("status@broadcast") is None

    def test_numero_no_numerico_devuelve_none(self):
        assert normalizar_chat_id("abc@s.whatsapp.net") is None


class TestConfigDesdeEntorno:
    def _entorno_valido(self, monkeypatch, **extra):
        monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "5215512345678,5215587654321")
        for clave, valor in extra.items():
            monkeypatch.setenv(clave, valor)

    def test_lee_la_allowlist(self, monkeypatch):
        self._entorno_valido(monkeypatch)
        config = Config.desde_entorno()
        assert config.allowlist == frozenset({"5215512345678", "5215587654321"})

    def test_tolera_espacios_y_signo_de_mas(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", " +5215512345678 , 5215587654321 ")
        config = Config.desde_entorno()
        assert config.allowlist == frozenset({"5215512345678", "5215587654321"})

    def test_sin_allowlist_no_arranca(self, monkeypatch):
        monkeypatch.delenv("WHATSAPP_ALLOWED_NUMBERS", raising=False)
        with pytest.raises(ConfigError, match="WHATSAPP_ALLOWED_NUMBERS"):
            Config.desde_entorno()

    def test_allowlist_vacia_no_arranca(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "   ,  ")
        with pytest.raises(ConfigError, match="WHATSAPP_ALLOWED_NUMBERS"):
            Config.desde_entorno()

    def test_numero_invalido_no_arranca(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ALLOWED_NUMBERS", "5215512345678,no-es-numero")
        with pytest.raises(ConfigError, match="no-es-numero"):
            Config.desde_entorno()

    def test_valores_por_defecto(self, monkeypatch):
        self._entorno_valido(monkeypatch)
        config = Config.desde_entorno()
        assert config.timeout_segundos == 90
        assert config.ttl_minutos == 60
        assert config.api_host == "127.0.0.1"
        assert config.api_port == 8000

    def test_sobrescribe_valores_por_defecto(self, monkeypatch):
        self._entorno_valido(monkeypatch, AGENT_TIMEOUT_SECONDS="30", API_PORT="9001")
        config = Config.desde_entorno()
        assert config.timeout_segundos == 30
        assert config.api_port == 9001


class TestAutorizado:
    def test_jid_individual_autorizado(self):
        config = Config(
            allowlist=frozenset({"5215512345678"}),
            timeout_segundos=90,
            ttl_minutos=60,
            api_host="127.0.0.1",
            api_port=8000,
        )
        assert config.autorizado("5215512345678@s.whatsapp.net") is True

    def test_jid_individual_no_autorizado(self):
        config = Config(
            allowlist=frozenset({"5215512345678"}),
            timeout_segundos=90,
            ttl_minutos=60,
            api_host="127.0.0.1",
            api_port=8000,
        )
        assert config.autorizado("5215587654321@s.whatsapp.net") is False

    def test_grupo_nunca_autorizado(self):
        config = Config(
            allowlist=frozenset({"120363001234567890"}),
            timeout_segundos=90,
            ttl_minutos=60,
            api_host="127.0.0.1",
            api_port=8000,
        )
        assert config.autorizado("120363001234567890@g.us") is False

    def test_dispositivo_ignorado_y_autorizado(self):
        config = Config(
            allowlist=frozenset({"5215512345678"}),
            timeout_segundos=90,
            ttl_minutos=60,
            api_host="127.0.0.1",
            api_port=8000,
        )
        assert config.autorizado("5215512345678:12@s.whatsapp.net") is True

    def test_cadena_invalida_no_autorizado(self):
        config = Config(
            allowlist=frozenset({"5215512345678"}),
            timeout_segundos=90,
            ttl_minutos=60,
            api_host="127.0.0.1",
            api_port=8000,
        )
        assert config.autorizado("not-a-valid-jid") is False
