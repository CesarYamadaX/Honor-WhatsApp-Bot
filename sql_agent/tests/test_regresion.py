"""La prueba semanal: que revise lo que tiene que revisar (pedido el 9/10/2026)."""

from datetime import date, datetime, timedelta

import regresion


def _ventas(anio, mes, marca="HONOR"):
    if marca is None:
        return {"Telcel": 1_000_000.0, "Coppel": 400_000.0}
    if marca == "SAMSUNG":
        return {"Sears": 3_435.0}
    if anio == 2025:
        return {"Coppel": 47_132.0}
    return {"Telcel": 73_628.0, "Coppel": 37_280.0, "AT&T": 16_334.0, "Elektra": 5_193.0, "Sears": 3_602.0}


def _casos(monkeypatch):
    monkeypatch.setattr(regresion, "ventas", _ventas)
    monkeypatch.setattr(regresion, "top_modelo", lambda a, m: "HONOR X5D 128GB 4G")
    return {c.clave: c for c in regresion.casos(hoy=date(2026, 10, 9))}


def test_las_cifras_se_leen_como_las_escribe_el_bot():
    assert regresion.menciona_cifra("Honor vendió **136,037 unidades**", 136_037)
    assert regresion.menciona_cifra("4 210 unidades", 4_210)
    assert not regresion.menciona_cifra("113,790 unidades", 136_037)


def test_prueba_el_mes_anterior(monkeypatch):
    casos = _casos(monkeypatch)
    assert casos["total_del_mes"].pregunta == "cuantas unidades vendio Honor en septiembre de 2026"


def test_el_total_tiene_que_avisar_las_cadenas_que_faltan(monkeypatch):
    revisar = _casos(monkeypatch)["total_del_mes"].revisar
    assert revisar("Honor vendió 136,037 unidades, sin Liverpool ni Movistar.", []) == []
    problemas = revisar("Honor vendió 136,037 unidades.", [])
    assert "no avisa que falta Liverpool" in problemas


def test_por_cadena_no_acepta_unknown(monkeypatch):
    revisar = _casos(monkeypatch)["por_cadena"].revisar
    texto = "Telcel 73,628; Coppel 37,280; AT&T 16,334; Elektra 5,193; Sears 3,602"
    assert revisar(texto, []) == []
    assert any("Unknown" in p for p in revisar(texto + "; Unknown 12", []))


def test_sears_por_semana_tiene_que_decir_el_mes(monkeypatch):
    revisar = _casos(monkeypatch)["sears_semana"].revisar
    assert revisar("Sears reporta por mes: en septiembre Samsung vendió 3,435.", []) == []
    assert len(revisar("No tengo datos de esa semana.", [])) == 2


def test_permiso_no_puede_dar_la_cifra(monkeypatch):
    caso = _casos(monkeypatch)["permiso"]
    assert caso.cadenas == ["COPPEL"]
    assert caso.revisar("No tienes permiso para consultar Liverpool.", []) == []


def test_corre_una_vez_por_semana_y_con_el_resumen_fresco(tmp_path, monkeypatch):
    monkeypatch.setattr(regresion, "CARPETA", tmp_path)
    fresco = {"construido": datetime.now().isoformat()}
    monkeypatch.setattr(regresion.resumen_local, "estado", lambda: fresco)
    a_las_11 = datetime.now().replace(hour=11)
    assert regresion.toca_correr(a_las_11)
    assert not regresion.toca_correr(a_las_11.replace(hour=8))
    (tmp_path / f"{a_las_11 - timedelta(days=2):%Y-%m-%d_%H%M}.json").write_text("{}")
    assert not regresion.toca_correr(a_las_11)
    monkeypatch.setattr(regresion.resumen_local, "estado", lambda: {})
    (tmp_path / f"{a_las_11 - timedelta(days=2):%Y-%m-%d_%H%M}.json").unlink()
    assert not regresion.toca_correr(a_las_11)


def test_un_caso_que_truena_no_tumba_los_demas(tmp_path, monkeypatch):
    monkeypatch.setattr(regresion, "CARPETA", tmp_path)

    class Agente:
        def responder(self, pregunta, cadenas=None):
            if "rompe" in pregunta:
                raise RuntimeError("se cayo")
            return "Fueron 500 unidades.", []

    casos = [
        regresion.Caso("roto", "rompe", lambda t, a: []),
        regresion.Caso("bien", "cuanto", lambda t, a: [] if regresion.menciona_cifra(t, 500) else ["no"]),
    ]
    informe = regresion.correr(None, Agente, casos)
    assert informe["bien"] == 1 and informe["total"] == 2
    assert list(tmp_path.glob("*.json"))


def test_si_todo_falla_por_error_no_cuenta_como_la_corrida_de_la_semana(tmp_path, monkeypatch):
    monkeypatch.setattr(regresion, "CARPETA", tmp_path)

    class SinConexion:
        def responder(self, pregunta, cadenas=None):
            raise ConnectionError("Connection error.")

    casos = [regresion.Caso("uno", "a", lambda t, a: []), regresion.Caso("dos", "b", lambda t, a: [])]
    regresion.correr(None, SinConexion, casos)
    assert [p.name for p in tmp_path.glob("*.json")][0].endswith("_sin_probar.json")
    assert regresion.ultima_corrida() is None  # se vuelve a intentar


def test_un_informe_viejo_donde_todo_fallo_no_cuenta(tmp_path, monkeypatch):
    # El de produccion del 12/10/2026 (0 de 11 por un certificado) quedo con
    # el nombre normal: no debe esperar 7 dias para reintentar.
    import json
    monkeypatch.setattr(regresion, "CARPETA", tmp_path)
    (tmp_path / "2026-10-12_1029.json").write_text(json.dumps(
        {"resultados": [{"problemas": ["error: Connection error."]}, {"problemas": ["error: Connection error."]}]}))
    assert regresion.ultima_corrida() is None
    (tmp_path / "2026-10-05_1000.json").write_text(json.dumps({"resultados": [{"problemas": []}]}))
    assert regresion.ultima_corrida().day == 5
