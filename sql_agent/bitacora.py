"""Que se pregunto, que contesto el bot y que hizo por dentro para contestar.

Dos cosas distintas viven aqui:

- La BITACORA: lo que ya paso. Un archivo JSONL por dia con un renglon por
  turno (pregunta, respuesta, cuanto tardo, que SQL corrio). Sirve para
  revisar despues por que el bot contesto lo que contesto -- hasta ahora eso
  solo se podia reconstruir leyendo el log del proceso.

- La ACTIVIDAD: lo que esta pasando AHORA. Que pregunta esta corriendo, desde
  hace cuanto y en que paso va. Una consulta pesada tarda minutos y desde
  fuera el bot se ve igual que si estuviera colgado.

Los pasos los reportan las herramientas del agente llamando a `paso()`. Como
el turno vive en un ContextVar, una herramienta no necesita saber de que chat
es: escribe en el turno de su propio contexto, y si no hay ninguno (la CLI,
las pruebas) no hace nada.
"""

import json
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

MAX_PASOS = 40
# Recortes para que un turno no ocupe media pantalla del panel ni engorde el
# archivo: la pregunta y la respuesta completas ya viajaron por WhatsApp.
MAX_TEXTO = 4000
MAX_PASO = 500


def _ahora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _recortar(texto: str, tope: int) -> str:
    texto = texto or ""
    return texto if len(texto) <= tope else texto[:tope] + " [...]"


@dataclass
class Turno:
    chat_id: str
    pregunta: str
    inicio: str = field(default_factory=_ahora)
    pasos: list[str] = field(default_factory=list)
    respuesta: str = ""
    archivos: list[str] = field(default_factory=list)
    idioma: str = ""
    segundos: float = 0.0
    # "en curso" mientras corre; despues "listo", "error" o "cancelado".
    estado: str = "en curso"

    def como_dict(self) -> dict:
        return {
            "chat_id": self.chat_id,
            "pregunta": self.pregunta,
            "inicio": self.inicio,
            "pasos": list(self.pasos),
            "respuesta": self.respuesta,
            "archivos": list(self.archivos),
            "idioma": self.idioma,
            "segundos": round(self.segundos, 1),
            "estado": self.estado,
        }


_turno_actual: ContextVar[Turno | None] = ContextVar("_turno_actual", default=None)


class Bitacora:
    """Escribe los turnos terminados y responde que hay en curso."""

    def __init__(self, carpeta: Path | None = None):
        self.carpeta = Path(carpeta) if carpeta else None
        self._candado = threading.Lock()
        self._en_curso: dict[str, Turno] = {}
        # archivo -> (tamaño cuando se leyo, resumen por chat)
        self._resumenes: dict[Path, tuple[int, dict[str, dict]]] = {}

    # --- lo que esta pasando ahora ---------------------------------------

    def comenzar(self, chat_id: str, pregunta: str) -> Turno:
        turno = Turno(chat_id=chat_id, pregunta=_recortar(pregunta, MAX_TEXTO))
        with self._candado:
            self._en_curso[chat_id] = turno
        _turno_actual.set(turno)
        return turno

    def terminar(
        self,
        turno: Turno | None,
        *,
        respuesta: str = "",
        archivos: list[str] | None = None,
        idioma: str = "",
        estado: str = "listo",
        segundos: float = 0.0,
    ) -> None:
        if turno is None:
            return
        turno.respuesta = _recortar(respuesta, MAX_TEXTO)
        turno.archivos = list(archivos or [])
        turno.idioma = idioma
        turno.estado = estado
        turno.segundos = segundos
        with self._candado:
            # Solo si sigue siendo el suyo: una pregunta cancelada termina
            # DESPUES de que la nueva ya se registro, y borrar por chat_id a
            # secas dejaria la nueva fuera de la vista de actividad.
            if self._en_curso.get(turno.chat_id) is turno:
                del self._en_curso[turno.chat_id]
        _turno_actual.set(None)
        self._escribir(turno)

    def en_curso(self) -> list[dict]:
        with self._candado:
            turnos = list(self._en_curso.values())
        ahora = datetime.now()
        vista = []
        for turno in turnos:
            datos = turno.como_dict()
            try:
                datos["segundos"] = round(
                    (ahora - datetime.fromisoformat(turno.inicio)).total_seconds(), 1
                )
            except ValueError:
                pass
            vista.append(datos)
        return sorted(vista, key=lambda t: t["inicio"])

    # --- lo que ya paso ---------------------------------------------------

    def _archivo_del_dia(self, dia: str | None = None) -> Path | None:
        if self.carpeta is None:
            return None
        dia = dia or datetime.now().strftime("%Y-%m-%d")
        return self.carpeta / f"{dia}.jsonl"

    def _escribir(self, turno: Turno) -> None:
        archivo = self._archivo_del_dia()
        if archivo is None:
            return
        try:
            archivo.parent.mkdir(parents=True, exist_ok=True)
            with archivo.open("a", encoding="utf-8") as salida:
                salida.write(json.dumps(turno.como_dict(), ensure_ascii=False) + "\n")
        except OSError:
            # Registrar es util, no critico: si el disco falla, el usuario
            # igual debe recibir su respuesta.
            pass

    def _archivos(self) -> list[Path]:
        """Los archivos de la bitacora, del dia mas reciente al mas viejo."""
        if self.carpeta is None or not self.carpeta.exists():
            return []
        return sorted(self.carpeta.glob("*.jsonl"), reverse=True)

    @staticmethod
    def _leer(archivo: Path) -> list[dict]:
        try:
            lineas = archivo.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        turnos = []
        for linea in lineas:
            try:
                turnos.append(json.loads(linea))
            except json.JSONDecodeError:
                continue
        return turnos

    def turnos(
        self, limite: int = 100, chat_id: str | None = None, salto: int = 0
    ) -> list[dict]:
        """Los turnos mas recientes primero, opcionalmente de un solo chat.

        `salto` descarta los N mas recientes antes de empezar a contar, que es
        como el panel pide "los anteriores a los que ya tengo". De atras hacia
        adelante por dia: para la primera pagina basta con abrir el archivo de
        hoy, aunque haya meses de historia detras.
        """
        encontrados: list[dict] = []
        descartados = 0
        for archivo in self._archivos():
            for turno in reversed(self._leer(archivo)):
                if chat_id and turno.get("chat_id") != chat_id:
                    continue
                if descartados < salto:
                    descartados += 1
                    continue
                encontrados.append(turno)
                if len(encontrados) >= limite:
                    return encontrados
        return encontrados

    def _resumen_de(self, archivo: Path) -> dict[str, dict]:
        """Cuantos turnos y cual fue el ultimo, por chat, dentro de UN archivo.

        El de un dia ya pasado no vuelve a cambiar nunca, asi que se recuerda
        lo calculado y solo se relee el de hoy, y solo cuando creció. Sin esto
        habria que releer toda la historia cada vez que el panel se refresca.
        """
        try:
            firma = archivo.stat().st_size
        except OSError:
            return {}
        with self._candado:
            guardado = self._resumenes.get(archivo)
            if guardado is not None and guardado[0] == firma:
                return guardado[1]

        resumen: dict[str, dict] = {}
        for turno in self._leer(archivo):
            chat = turno.get("chat_id", "")
            fila = resumen.setdefault(
                chat, {"chat_id": chat, "turnos": 0, "ultimo": "", "ultima_pregunta": ""}
            )
            fila["turnos"] += 1
            if turno.get("inicio", "") >= fila["ultimo"]:
                fila["ultimo"] = turno.get("inicio", "")
                fila["ultima_pregunta"] = turno.get("pregunta", "")

        with self._candado:
            self._resumenes[archivo] = (firma, resumen)
        return resumen

    def chats(self, limite: int = 500) -> list[dict]:
        """Un renglon por conversacion, mirando TODA la historia.

        Antes solo se miraban los ultimos 2000 turnos, y una conversacion sin
        actividad reciente desaparecia de la lista aunque sus datos siguieran
        en disco. Leerlo todo cuesta poco: un turno ocupa unos 330 bytes.
        """
        total: dict[str, dict] = {}
        for archivo in self._archivos():
            for chat, fila in self._resumen_de(archivo).items():
                acumulado = total.setdefault(
                    chat, {"chat_id": chat, "turnos": 0, "ultimo": "", "ultima_pregunta": ""}
                )
                acumulado["turnos"] += fila["turnos"]
                if fila["ultimo"] >= acumulado["ultimo"]:
                    acumulado["ultimo"] = fila["ultimo"]
                    acumulado["ultima_pregunta"] = fila["ultima_pregunta"]
        return sorted(total.values(), key=lambda c: c["ultimo"], reverse=True)[:limite]

    def cuantos(self, chat_id: str) -> int:
        """Cuantos turnos tiene una conversacion en total."""
        return sum(
            resumen.get(chat_id, {}).get("turnos", 0)
            for resumen in (self._resumen_de(a) for a in self._archivos())
        )


def paso(texto: str) -> None:
    """Anota un paso en el turno en curso. Sin turno, no hace nada.

    Lo llaman las herramientas del agente. Que sea inofensivo fuera de una
    peticion es lo que permite que la CLI y las pruebas usen las mismas
    herramientas sin montar nada alrededor.
    """
    turno = _turno_actual.get()
    if turno is None:
        return
    if len(turno.pasos) >= MAX_PASOS:
        return
    turno.pasos.append(f"{datetime.now().strftime('%H:%M:%S')}  {_recortar(texto, MAX_PASO)}")


def turno_actual() -> Turno | None:
    return _turno_actual.get()
