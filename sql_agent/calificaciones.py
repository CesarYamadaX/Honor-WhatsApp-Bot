"""Las respuestas que los usuarios califican reaccionando en WhatsApp.

Antes la unica forma de saber que el bot contesto mal era que alguien lo
reportara a mano (asi se encontro lo de la promotoria). Ahora una reaccion
al mensaje del bot (👍, ❤️, 🙏... buena; 👎, 😡, 😢... mala, ver
REACCIONES_BUENAS y REACCIONES_MALAS) queda guardada junto al turno, y el
panel muestra las malas con su pregunta, su respuesta y lo que hizo el bot
por dentro.

Dos tablas:
- enviados: que mensajes de WhatsApp pertenecen a que turno. Una respuesta
  puede ser varios mensajes (texto troceado, Excel, grafica), y la reaccion
  llega con el id del mensaje, no del turno.
- calificaciones: una por turno y chat; reaccionar otra vez la reemplaza y
  quitar la reaccion la borra, como en WhatsApp.
- comentarios: por que se califico asi, escrito desde el panel (pedido el
  8/10/2026). Aparte de la calificacion para que no se pierda si el usuario
  cambia o quita su reaccion. El bot lee los mas recientes antes de cada
  pregunta (ver `notas_para_el_bot`).
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path

BUENA = 1
MALA = -1
# El "turno" de una calificacion a un mensaje del bot que no quedo registrado
# (ver api.reaccion): no hay pregunta a la que ligarla, se liga al mensaje.
SIN_REGISTRO = "sin-registro:"

_TONOS_Y_VARIANTES = {chr(c) for c in range(0x1F3FB, 0x1F400)} | {"️"}
_UNION = "‍"  # une emojis: 🤦‍♂️ es 🤦 + ZWJ + ♂

# Que reacciones cuentan como buena y como mala. Pedido el 6/10/2026: no
# solo los pulgares, porque la gente reacciona con lo que tiene a la mano
# (❤️ y 🙏 vienen en la barra rapida de WhatsApp). Van sin tono de piel ni
# selector de variante: `valor_de` los quita antes de buscar.
#
# Fuera a proposito 😂 y 😮, los otros dos de la barra rapida: 😂 lo mismo
# es "que buena" que "que mal salio esto", y 😮 lo mismo sorpresa que susto.
# Contarlos llenaria la pestaña de calificaciones de ruido.
REACCIONES_BUENAS = (
    "👍", "❤", "♥", "🙏", "👏", "🙌", "🔥", "💯", "✅", "👌", "💪", "🎉",
    "⭐", "🌟", "🏆", "😍", "🥰", "😊", "😀", "😃", "😄", "😁", "🤩", "😎",
    "🫶", "💙", "💚", "🤝",
)
REACCIONES_MALAS = (
    "👎", "😡", "😠", "🤬", "😤", "😞", "😔", "😢", "😭", "😩", "😕", "🙁",
    "☹", "😒", "🤦", "🤨", "🤔", "❌", "🚫", "⚠", "💩",
)
_VALOR = {e: BUENA for e in REACCIONES_BUENAS} | {e: MALA for e in REACCIONES_MALAS}

# Un comentario es una nota, no un documento; y todos van en el prompt.
MAX_COMENTARIO = 1000
# Cuantos comentarios lee el bot: los mas recientes. Mas seria prompt que
# paga cada pregunta, y los viejos casi siempre ya estan corregidos.
COMENTARIOS_PARA_EL_BOT = 15


def valor_de(emoji: str) -> int | None:
    """+1 si es una reaccion buena, -1 si es mala, None si no cuenta.

    Con cualquier tono de piel o variante: 👍🏽, ❤️ (que es ❤ + FE0F) y 🤦‍♀️
    (🤦 unido a ♀) valen lo mismo que su emoji base.
    """
    base = (emoji or "").split(_UNION)[0]
    base = "".join(c for c in base if c not in _TONOS_Y_VARIANTES)
    return _VALOR.get(base)


def _ahora() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Calificaciones:
    def __init__(self, archivo: Path | None = None):
        # Sin archivo, en memoria: es lo que usan las pruebas y crear_app sin
        # datos en disco. Una sola conexion bajo candado; son pocas escrituras.
        if archivo is not None:
            Path(archivo).parent.mkdir(parents=True, exist_ok=True)
        self._conexion = sqlite3.connect(
            str(archivo) if archivo else ":memory:", check_same_thread=False
        )
        self._candado = threading.Lock()
        with self._candado, self._conexion:
            self._conexion.executescript("""
                CREATE TABLE IF NOT EXISTS enviados (
                    mensaje_id TEXT PRIMARY KEY,
                    turno_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    fecha TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS calificaciones (
                    turno_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    valor INTEGER NOT NULL,
                    emoji TEXT NOT NULL,
                    fecha TEXT NOT NULL,
                    PRIMARY KEY (turno_id, chat_id)
                );
                CREATE TABLE IF NOT EXISTS comentarios (
                    turno_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    texto TEXT NOT NULL,
                    pregunta TEXT NOT NULL,
                    valor INTEGER,
                    fecha TEXT NOT NULL,
                    PRIMARY KEY (turno_id, chat_id)
                );
            """)

    def registrar_enviados(self, chat_id: str, turno_id: str, mensajes: list[str]) -> None:
        filas = [(m, turno_id, chat_id, _ahora()) for m in mensajes if m]
        with self._candado, self._conexion:
            self._conexion.executemany(
                "INSERT OR REPLACE INTO enviados VALUES (?, ?, ?, ?)", filas
            )

    def turno_de(self, mensaje_id: str, chat_id: str) -> str | None:
        """El turno al que pertenece un mensaje del bot, solo si es de ese chat."""
        with self._candado, closing(self._conexion.cursor()) as cursor:
            fila = cursor.execute(
                "SELECT turno_id FROM enviados WHERE mensaje_id = ? AND chat_id = ?",
                (mensaje_id, chat_id),
            ).fetchone()
        return fila[0] if fila else None

    def calificar(self, turno_id: str, chat_id: str, valor: int, emoji: str) -> None:
        with self._candado, self._conexion:
            self._conexion.execute(
                "INSERT OR REPLACE INTO calificaciones VALUES (?, ?, ?, ?, ?)",
                (turno_id, chat_id, valor, emoji, _ahora()),
            )

    def quitar(self, turno_id: str, chat_id: str) -> None:
        with self._candado, self._conexion:
            self._conexion.execute(
                "DELETE FROM calificaciones WHERE turno_id = ? AND chat_id = ?",
                (turno_id, chat_id),
            )

    def comentar(
        self, turno_id: str, chat_id: str, texto: str, pregunta: str = "", valor: int | None = None
    ) -> None:
        """Guarda (o reemplaza) el comentario de una respuesta; vacio lo borra.

        La pregunta y la calificacion se copian: el bot lee los comentarios
        sin abrir la bitacora, y el comentario sigue diciendo de que era
        aunque el usuario quite su reaccion.
        """
        texto = (texto or "").strip()[:MAX_COMENTARIO]
        with self._candado, self._conexion:
            if not texto:
                self._conexion.execute(
                    "DELETE FROM comentarios WHERE turno_id = ? AND chat_id = ?",
                    (turno_id, chat_id),
                )
                return
            self._conexion.execute(
                "INSERT OR REPLACE INTO comentarios VALUES (?, ?, ?, ?, ?, ?)",
                (turno_id, chat_id, texto, (pregunta or "")[:500], valor, _ahora()),
            )

    def comentarios(self, limite: int = COMENTARIOS_PARA_EL_BOT) -> list[dict]:
        """Los comentarios mas recientes primero, con la calificacion de ahora
        (o la que tenia al comentar, si ya la quitaron)."""
        with self._candado, closing(self._conexion.cursor()) as cursor:
            filas = cursor.execute(
                "SELECT c.turno_id, c.chat_id, c.texto, c.pregunta, COALESCE(k.valor, c.valor), c.fecha "
                "FROM comentarios c LEFT JOIN calificaciones k "
                "ON k.turno_id = c.turno_id AND k.chat_id = c.chat_id "
                "ORDER BY c.fecha DESC, c.rowid DESC LIMIT ?",
                (limite,),
            ).fetchall()
        return [
            {"turno_id": t, "chat_id": c, "texto": x, "pregunta": p, "valor": v, "fecha": f}
            for t, c, x, p, v, f in filas
        ]

    def notas_para_el_bot(self, limite: int = COMENTARIOS_PARA_EL_BOT) -> str:
        """Los comentarios recientes como instruccion para el modelo, o "".

        Los escribe solo quien administra el bot (el panel es local), asi que
        valen como instrucciones. Pero pueden traer cifras o nombres de una
        cadena que el usuario de ahora no ve: van como guia de COMO
        contestar, nunca como datos que repetir.
        """
        filas = self.comentarios(limite)
        if not filas:
            return ""
        renglones = []
        for fila in reversed(filas):  # del mas viejo al mas nuevo: lo ultimo pesa mas
            como = {BUENA: "bien calificada", MALA: "MAL calificada"}.get(fila["valor"], "comentada")
            pregunta = f' a "{fila["pregunta"]}"' if fila["pregunta"] else ""
            renglones.append(f"- Respuesta {como}{pregunta}: {fila['texto']}")
        return (
            "NOTAS DEL ADMINISTRADOR sobre respuestas anteriores (de cualquier "
            "usuario). Son para que contestes mejor: aplica lo que piden a "
            "preguntas parecidas. No son datos: nunca repitas cifras, cadenas "
            "ni nombres que vengan aqui, y no las menciones al usuario.\n"
            + "\n".join(renglones)
        )

    def de_turnos(self, turno_ids: list[str]) -> dict[str, dict]:
        """La calificacion de cada turno de la lista que tenga una."""
        ids = [i for i in turno_ids if i]
        if not ids:
            return {}
        with self._candado, closing(self._conexion.cursor()) as cursor:
            filas = cursor.execute(
                f"SELECT k.turno_id, k.valor, k.emoji, k.fecha, c.texto FROM calificaciones k "
                f"LEFT JOIN comentarios c ON c.turno_id = k.turno_id AND c.chat_id = k.chat_id "
                f"WHERE k.turno_id IN ({', '.join('?' * len(ids))})",
                ids,
            ).fetchall()
        return {
            t: {"valor": v, "emoji": e, "fecha": f, "comentario": x or ""}
            for t, v, e, f, x in filas
        }

    def lista(self, valor: int | None = None, limite: int = 200) -> list[dict]:
        """Las calificaciones mas recientes primero, opcionalmente de un solo tipo."""
        donde, parametros = "", []
        if valor is not None:
            donde, parametros = "WHERE k.valor = ?", [valor]
        with self._candado, closing(self._conexion.cursor()) as cursor:
            filas = cursor.execute(
                f"SELECT k.turno_id, k.chat_id, k.valor, k.emoji, k.fecha, c.texto, c.fecha "
                f"FROM calificaciones k LEFT JOIN comentarios c "
                f"ON c.turno_id = k.turno_id AND c.chat_id = k.chat_id "
                f"{donde} ORDER BY k.fecha DESC LIMIT ?",
                [*parametros, limite],
            ).fetchall()
        return [
            {"turno_id": t, "chat_id": c, "valor": v, "emoji": e, "fecha": f,
             "comentario": x or "", "comentado": fc}
            for t, c, v, e, f, x, fc in filas
        ]

    def cuantas(self) -> dict:
        with self._candado, closing(self._conexion.cursor()) as cursor:
            filas = dict(cursor.execute(
                "SELECT valor, COUNT(*) FROM calificaciones GROUP BY valor"
            ).fetchall())
        return {"buenas": filas.get(BUENA, 0), "malas": filas.get(MALA, 0)}
