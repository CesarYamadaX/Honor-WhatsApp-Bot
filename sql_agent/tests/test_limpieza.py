import os
import time

import limpieza

_DIA = 86400


def _archivo(carpeta, nombre, dias_atras):
    ruta = carpeta / nombre
    ruta.write_bytes(b"x")
    hace = time.time() - dias_atras * _DIA
    os.utime(ruta, (hace, hace))
    return ruta


def test_borra_lo_de_mas_de_siete_dias_y_deja_lo_reciente(tmp_path):
    viejo = _archivo(tmp_path, "reporte_viejo.xlsx", 8)
    grafica = _archivo(tmp_path, "grafica_vieja.png", 30)
    reciente = _archivo(tmp_path, "reporte_de_ayer.xlsx", 1)

    assert limpieza.borrar_viejos(tmp_path, dias=7) == 2
    assert not viejo.exists() and not grafica.exists()
    assert reciente.exists()


def test_no_toca_subcarpetas(tmp_path):
    sub = tmp_path / "algo"
    sub.mkdir()
    hace = time.time() - 30 * _DIA
    os.utime(sub, (hace, hace))
    limpieza.borrar_viejos(tmp_path, dias=7)
    assert sub.exists()


def test_en_cero_no_borra_nada(tmp_path):
    viejo = _archivo(tmp_path, "reporte.xlsx", 100)
    assert limpieza.borrar_viejos(tmp_path, dias=0) == 0
    assert viejo.exists()


def test_una_carpeta_que_no_existe_no_truena(tmp_path):
    assert limpieza.borrar_viejos(tmp_path / "no_existe") == 0


def test_la_carpeta_es_la_misma_que_usa_el_exportador(monkeypatch, tmp_path):
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    assert limpieza.carpeta_de_exportaciones() == tmp_path
