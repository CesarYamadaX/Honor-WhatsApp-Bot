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
- calificaciones: una por parte de la respuesta (el texto, y cada archivo
  aparte) y chat; reaccionar otra vez la reemplaza y quitar la reaccion la
  borra, como en WhatsApp.
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


# Una calificacion y un comentario por PARTE de la respuesta: el texto
# (parte "") y cada archivo (su nombre). Antes uno por turno, y un 💩 al
# Excel borraba el 👎 que ya tenia el texto (visto el 8/10/2026).
_ESQUEMAS = {
    "calificaciones": """
                CREATE TABLE IF NOT EXISTS calificaciones (
                    turno_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    valor INTEGER NOT NULL,
                    emoji TEXT NOT NULL,
                    fecha TEXT NOT NULL,
                    parte TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (turno_id, chat_id, parte)
                );
""",
    "comentarios": """
                CREATE TABLE IF NOT EXISTS comentarios (
                    turno_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    texto TEXT NOT NULL,
                    pregunta TEXT NOT NULL,
                    valor INTEGER,
                    fecha TEXT NOT NULL,
                    parte TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (turno_id, chat_id, parte)
                );
""",
}


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
                    fecha TEXT NOT NULL,
                    parte TEXT NOT NULL DEFAULT ''
                );
            """ + _ESQUEMAS["calificaciones"] + _ESQUEMAS["comentarios"])
            self._migrar()

    def _columnas(self, tabla: str) -> set[str]:
        return {fila[1] for fila in self._conexion.execute(f"PRAGMA table_info({tabla})")}

    def _migrar(self) -> None:
        """Las bases de antes del 8/10/2026 no tienen `parte`.

        En enviados basta agregar la columna. En calificaciones y comentarios
        cambia la llave (ahora una por parte), y SQLite no cambia la llave de
        una tabla: se copia a una nueva. Lo que ya habia queda como
        calificacion de la respuesta (parte "").
        """
        if "parte" not in self._columnas("enviados"):
            self._conexion.execute("ALTER TABLE enviados ADD COLUMN parte TEXT NOT NULL DEFAULT ''")
        for tabla, columnas in (
            ("calificaciones", "turno_id, chat_id, valor, emoji, fecha"),
            ("comentarios", "turno_id, chat_id, texto, pregunta, valor, fecha"),
        ):
            if "parte" in self._columnas(tabla):
                continue
            nuevo = _ESQUEMAS[tabla].replace(f"TABLE IF NOT EXISTS {tabla} ", f"TABLE {tabla}_nueva ")
            self._conexion.execute(nuevo)
            self._conexion.execute(
                f"INSERT INTO {tabla}_nueva ({columnas}, parte) SELECT {columnas}, '' FROM {tabla}"
            )
            self._conexion.execute(f"DROP TABLE {tabla}")
            self._conexion.execute(f"ALTER TABLE {tabla}_nueva RENAME TO {tabla}")

    def registrar_enviados(
        self, chat_id: str, turno_id: str, mensajes: list[str], partes: dict[str, str] | None = None
    ) -> None:
        """Que mensajes de WhatsApp son de que turno, y cuales son un archivo.

        `partes` dice de cada mensaje que es un archivo cual (su nombre); los
        que no vienen ahi son el texto de la respuesta.
        """
        partes = partes or {}
        filas = [(m, turno_id, chat_id, _ahora(), partes.get(m, "")) for m in mensajes if m]
        with self._candado, self._conexion:
            self._conexion.executemany(
                "INSERT OR REPLACE INTO enviados (mensaje_id, turno_id, chat_id, fecha, parte) "
                "VALUES (?, ?, ?, ?, ?)",
                filas,
            )

    def turno_de(self, mensaje_id: str, chat_id: str) -> str | None:
        """El turno al que pertenece un mensaje del bot, solo si es de ese chat."""
        encontrado = self.mensaje(mensaje_id, chat_id)
        return encontrado[0] if encontrado else None

    def mensaje(self, mensaje_id: str, chat_id: str) -> tuple[str, str] | None:
        """(turno, parte) de un mensaje del bot de ese chat, o None."""
        with self._candado, closing(self._conexion.cursor()) as cursor:
            fila = cursor.execute(
                "SELECT turno_id, parte FROM enviados WHERE mensaje_id = ? AND chat_id = ?",
                (mensaje_id, chat_id),
            ).fetchone()
        return (fila[0], fila[1]) if fila else None

    def mensajes_del_turno(self, turno_id: str, chat_id: str) -> list[tuple[str, str]]:
        """(mensaje, parte) de un turno, en el orden en que se mandaron."""
        with self._candado, closing(self._conexion.cursor()) as cursor:
            return cursor.execute(
                "SELECT mensaje_id, parte FROM enviados WHERE turno_id = ? AND chat_id = ? ORDER BY rowid",
                (turno_id, chat_id),
            ).fetchall()

    def calificar(self, turno_id: str, chat_id: str, valor: int, emoji: str, parte: str = "") -> None:
        with self._candado, self._conexion:
            self._conexion.execute(
                "INSERT OR REPLACE INTO calificaciones (turno_id, chat_id, valor, emoji, fecha, parte) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (turno_id, chat_id, valor, emoji, _ahora(), parte),
            )

    def quitar(self, turno_id: str, chat_id: str, parte: str = "") -> None:
        with self._candado, self._conexion:
            self._conexion.execute(
                "DELETE FROM calificaciones WHERE turno_id = ? AND chat_id = ? AND parte = ?",
                (turno_id, chat_id, parte),
            )

    def comentar(
        self,
        turno_id: str,
        chat_id: str,
        texto: str,
        pregunta: str = "",
        valor: int | None = None,
        parte: str = "",
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
                    "DELETE FROM comentarios WHERE turno_id = ? AND chat_id = ? AND parte = ?",
                    (turno_id, chat_id, parte),
                )
                return
            self._conexion.execute(
                "INSERT OR REPLACE INTO comentarios (turno_id, chat_id, texto, pregunta, valor, fecha, parte) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (turno_id, chat_id, texto, (pregunta or "")[:500], valor, _ahora(), parte),
            )

    def comentarios(self, limite: int = COMENTARIOS_PARA_EL_BOT) -> list[dict]:
        """Los comentarios mas recientes primero, con la calificacion de ahora
        (o la que tenia al comentar, si ya la quitaron)."""
        with self._candado, closing(self._conexion.cursor()) as cursor:
            filas = cursor.execute(
                "SELECT c.turno_id, c.chat_id, c.texto, c.pregunta, COALESCE(k.valor, c.valor), c.fecha, c.parte "
                "FROM comentarios c LEFT JOIN calificaciones k "
                "ON k.turno_id = c.turno_id AND k.chat_id = c.chat_id AND k.parte = c.parte "
                "ORDER BY c.fecha DESC, c.rowid DESC LIMIT ?",
                (limite,),
            ).fetchall()
        return [
            {"turno_id": t, "chat_id": c, "texto": x, "pregunta": p, "valor": v, "fecha": f, "parte": a}
            for t, c, x, p, v, f, a in filas
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
            como = {BUENA: "bien calificad", MALA: "MAL calificad"}.get(fila["valor"], "comentad")
            que = _que_es(fila["parte"])
            pregunta = f' a "{fila["pregunta"]}"' if fila["pregunta"] else ""
            renglones.append(f"- {que[0]} {como}{que[1]}{pregunta}: {fila['texto']}")
        return (
            "NOTAS DEL ADMINISTRADOR sobre respuestas anteriores (de cualquier "
            "usuario). Son para que contestes mejor: aplica lo que piden a "
            "preguntas parecidas. No son datos: nunca repitas cifras, cadenas "
            "ni nombres que vengan aqui, y no las menciones al usuario.\n"
            + "\n".join(renglones)
        )

    def de_turnos(self, turno_ids: list[str]) -> dict[str, list[dict]]:
        """Las calificaciones de cada turno de la lista que tenga alguna: la
        del texto (parte "") primero y despues las de sus archivos."""
        ids = [i for i in turno_ids if i]
        if not ids:
            return {}
        with self._candado, closing(self._conexion.cursor()) as cursor:
            filas = cursor.execute(
                f"SELECT k.turno_id, k.valor, k.emoji, k.fecha, c.texto, k.parte FROM calificaciones k "
                f"LEFT JOIN comentarios c ON c.turno_id = k.turno_id AND c.chat_id = k.chat_id "
                f"AND c.parte = k.parte "
                f"WHERE k.turno_id IN ({', '.join('?' * len(ids))}) ORDER BY k.parte",
                ids,
            ).fetchall()
        resultado: dict[str, list[dict]] = {}
        for t, v, e, f, x, a in filas:
            resultado.setdefault(t, []).append(
                {"valor": v, "emoji": e, "fecha": f, "comentario": x or "", "parte": a}
            )
        return resultado

    def lista(self, valor: int | None = None, limite: int = 200) -> list[dict]:
        """Las calificaciones mas recientes primero, opcionalmente de un solo tipo."""
        donde, parametros = "", []
        if valor is not None:
            donde, parametros = "WHERE k.valor = ?", [valor]
        with self._candado, closing(self._conexion.cursor()) as cursor:
            filas = cursor.execute(
                f"SELECT k.turno_id, k.chat_id, k.valor, k.emoji, k.fecha, c.texto, c.fecha, k.parte "
                f"FROM calificaciones k LEFT JOIN comentarios c "
                f"ON c.turno_id = k.turno_id AND c.chat_id = k.chat_id AND c.parte = k.parte "
                f"{donde} ORDER BY k.fecha DESC, k.rowid DESC LIMIT ?",
                [*parametros, limite],
            ).fetchall()
        return [
            {"turno_id": t, "chat_id": c, "valor": v, "emoji": e, "fecha": f,
             "comentario": x or "", "comentado": fc, "parte": a}
            for t, c, v, e, f, x, fc, a in filas
        ]

    def cuantas(self) -> dict:
        with self._candado, closing(self._conexion.cursor()) as cursor:
            filas = dict(cursor.execute(
                "SELECT valor, COUNT(*) FROM calificaciones GROUP BY valor"
            ).fetchall())
        return {"buenas": filas.get(BUENA, 0), "malas": filas.get(MALA, 0)}


def _que_es(parte: str) -> tuple[str, str]:
    """Como nombrar una parte en las notas del bot: (sujeto, terminacion)."""
    if not parte:
        return "Respuesta", "a"
    extension = Path(parte).suffix.lower()
    if extension == ".png":
        return "Grafica de una respuesta", "a"
    if extension == ".zip":
        return "ZIP de una respuesta", "o"
    return "Excel de una respuesta", "o"
