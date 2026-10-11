"""El proveedor de modelos se decide en .env (migracion a Comagic, 10/10/2026)."""

import proveedor


def _sin_variables(monkeypatch):
    for nombre in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "OPENROUTER_API_KEY", "OPENROUTER_MODEL"):
        monkeypatch.delenv(nombre, raising=False)


def test_sin_direccion_propia_sigue_openrouter(monkeypatch):
    _sin_variables(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-6-luna")
    # Aunque ya este la clave de la otra plataforma: sin su direccion no se usa.
    monkeypatch.setenv("LLM_API_KEY", "sk-comagic")
    assert proveedor.url_chat() == "https://openrouter.ai/api/v1/chat/completions"
    assert proveedor.clave() == "sk-or"
    assert proveedor.modelo("MODEL") == "openai/gpt-6-luna"
    assert "X-Title" in proveedor.encabezados()


def test_con_direccion_propia_todo_cambia_junto(monkeypatch):
    _sin_variables(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-6-luna")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.comagic.example/v1/")
    monkeypatch.setenv("LLM_API_KEY", "sk-comagic")
    monkeypatch.setenv("LLM_MODEL", "GPT-5.6-Terra-SaaS")
    assert proveedor.url_chat() == "https://api.comagic.example/v1/chat/completions"
    assert proveedor.encabezados() == {"Authorization": "Bearer sk-comagic"}
    # Un nombre de OpenRouter no existe en la otra plataforma: no se hereda.
    assert proveedor.modelo("MODEL") == "GPT-5.6-Terra-SaaS"
    assert proveedor.modelo("REVISOR_MODEL") is None


def test_sin_clave_dice_cual_falta(monkeypatch):
    _sin_variables(monkeypatch)
    monkeypatch.setenv("LLM_BASE_URL", "https://api.comagic.example/v1")
    assert proveedor.clave() is None and proveedor.nombre_de_la_clave() == "LLM_API_KEY"
