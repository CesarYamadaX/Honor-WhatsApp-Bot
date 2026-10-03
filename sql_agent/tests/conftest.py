import pytest

import resumen_local


@pytest.fixture(autouse=True)
def _sin_resumen_local(tmp_path, monkeypatch):
    # El resumen real (datos/resumen_ventas.sqlite) existe en la compu donde
    # corre el bot: sin esto, una prueba que consulta ETL_Consolidado_B2B se
    # contestaria desde ahi y dependeria de que maquina la corre. Las pruebas
    # del resumen pasan su propio archivo.
    monkeypatch.setattr(resumen_local, "ARCHIVO", tmp_path / "sin_resumen.sqlite")
