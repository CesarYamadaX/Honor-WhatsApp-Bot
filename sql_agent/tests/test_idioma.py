"""Pruebas de idioma.py: en que idioma le contesta el bot a cada quien."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import idioma


class TestDeteccion:
    def test_reconoce_las_preguntas_tipicas_en_espanol(self):
        for texto in [
            "ventas totales de Elektra en 2026",
            "cuantas unidades vendio Coppel el mes pasado",
            "dame el excel por tienda",
            "y de Elektra?",
            "cuanto va a vender Elektra el proximo mes",
        ]:
            assert idioma.detectar(texto) == idioma.ESPANOL, texto

    def test_reconoce_las_mismas_preguntas_en_ingles(self):
        for texto in [
            "total sales for Elektra in 2026",
            "how many units did Coppel sell last month",
            "give me the excel by store",
            "show me sales by region",
            "what will Elektra sell next month",
        ]:
            assert idioma.detectar(texto) == idioma.INGLES, texto

    def test_un_solo_caracter_chino_basta(self):
        # Las preguntas reales mezclan: "Magic 7", "Elektra" y "Excel" se
        # escriben igual en chino. Si se decidiera por mayoria de caracteres,
        # una pregunta en chino con dos nombres propios pasaria por inglesa.
        assert idioma.detectar("2026年Elektra的总销量") == idioma.CHINO
        assert idioma.detectar("请给我Excel") == idioma.CHINO

    def test_los_acentos_y_signos_de_apertura_delatan_el_espanol(self):
        # No hay palabra de la lista en esta pregunta; la unica pista es como
        # esta escrita.
        assert idioma.detectar("¿Elektra 2026?") == idioma.ESPANOL

    def test_un_mensaje_sin_idioma_devuelve_None(self):
        # Y NO el idioma por defecto: quien llama tiene que poder distinguir
        # "esto es espanol" de "no se", porque en el segundo caso hay que
        # conservar el idioma que traia la conversacion. La mitad de los
        # mensajes de WhatsApp son de este tipo.
        for texto in ["top 10", "Elektra 2026", "", "?", "Magic 7 Pro"]:
            assert idioma.detectar(texto) is None, texto

    def test_no_decide_por_una_palabra_prestada(self):
        # "excel", "total" y "no" se escriben igual en los dos idiomas: si
        # contaran, cualquier pregunta con una de ellas se iria al idioma
        # equivocado.
        assert idioma.detectar("excel total") is None


class TestInstruccion:
    def test_hay_instruccion_para_los_tres_idiomas(self):
        assert set(idioma.INSTRUCCION) == {
            idioma.ESPANOL,
            idioma.INGLES,
            idioma.CHINO,
        }

    def test_cada_instruccion_esta_escrita_en_su_idioma(self):
        # Una instruccion en espanol para responder en ingles es una pelea
        # entre lo que dice y como esta escrito. El modelo hace mas caso a un
        # mensaje escrito en el idioma que se le pide.
        assert "English" in idioma.INSTRUCCION[idioma.INGLES]
        assert "中文" in idioma.INSTRUCCION[idioma.CHINO]
        assert "espanol" in idioma.INSTRUCCION[idioma.ESPANOL]


class TestTextos:
    def test_todos_los_mensajes_estan_en_los_tres_idiomas(self):
        for clave, traducciones in idioma.TEXTOS.items():
            assert set(traducciones) == {
                idioma.ESPANOL,
                idioma.INGLES,
                idioma.CHINO,
            }, clave

    def test_un_idioma_desconocido_cae_al_espanol(self):
        # Si algun dia se agrega un idioma al detector y no a los textos, el
        # usuario recibe espanol y no un KeyError a media respuesta.
        assert idioma.texto("cancelado", "pt") == idioma.texto(
            "cancelado", idioma.ESPANOL
        )

    def test_el_mensaje_de_servicio_caido_dice_a_quien_buscar(self):
        # Es el unico texto que sirve para algo mas que informar: el usuario
        # no puede hacer nada, pero alguien del equipo si.
        for lengua in (idioma.ESPANOL, idioma.INGLES, idioma.CHINO):
            assert "lauragabrielacastellanos" in idioma.texto(
                "servicio_caido", lengua
            )
