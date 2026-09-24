"""Agrega un PivotTable de Excel de verdad a un archivo ya guardado.

openpyxl no arma PivotTables: puede leerlos, pero escribir uno de la nada
significa fabricar a mano el cache y sus registros en XML, sin ejemplos
documentados, y sin forma de probar que Excel lo va a abrir sin pedir
reparacion. La unica manera confiable de que el archivo tenga un pivot de
verdad -- arrastrable, refrescable, igual al que arma alguien desde el menu
Insertar -- es dejar que Excel mismo lo arme, por COM.

Eso trae su propio riesgo, y aparecio en la primera prueba: un libro que se
`Add()` y no se cierra explicitamente deja el proceso EXCEL.EXE vivo despues
de `Quit()`, invisible porque `Visible=False` le impide mostrar el dialogo
que lo estaria reteniendo. Por eso aqui CADA workbook que se abre se cierra
en su propio finally, y ademas se verifica que el proceso murio de verdad y
se le da un empujon si no.

Y porque abrir Excel por COM no es gratis (2-7s medido, mas lo que tarde el
pivot), y una llamada colgada bloquearia el turno de otra pregunta, todo el
trabajo corre en un hilo aparte con plazo, bajo un candado: dos peticiones
armando un pivot al mismo tiempo abririan dos Excel por separado, que es
mas lento y mas fragil que hacerlo de a uno.
"""

import logging
import os
import threading
import time

import pandas as pd

logger = logging.getLogger("sql_agent.excel_pivot")

# Cuanto se le da a Excel antes de rendirse. La prueba mas pesada medida (80
# mil filas, el tope de ventas_crudas) tardo unos segundos; el margen es
# generoso porque un Excel que tarda de mas casi siempre es uno que se colgo,
# y ahi ya no vale la pena esperar mas.
SEGUNDOS_PARA_EL_PIVOT = 90

# Solo un Excel COM a la vez. Dos peticiones armando un pivot en paralelo
# abririan dos procesos por separado -- mas lento, y el doble de riesgo de
# dejar uno colgado.
_CANDADO = threading.Lock()

# Se calcula una sola vez por proceso: si Excel no esta instalado o COM no
# esta registrado, no tiene sentido volver a intentarlo en cada reporte.
_disponible: bool | None = None


def pivot_disponible() -> bool:
    """True si se puede abrir Excel por COM en esta maquina.

    El bot corre en la maquina de un usuario que tiene Excel, no en un
    servidor generico: por eso esta funcion existe (para el dia en que no
    sea asi, o Excel no responda) y no una comprobacion de version.
    """
    global _disponible
    if _disponible is not None:
        return _disponible
    try:
        import pythoncom
        import win32com.client as win32

        # Este chequeo corre en el hilo que llame primero -- en el servicio
        # real, uno de los hilos del threadpool de FastAPI, no
        # necesariamente el mismo dos veces -- asi que necesita su propio
        # apartamento COM igual que el hilo dedicado de `agregar_pivot`.
        pythoncom.CoInitialize()
        try:
            excel = win32.DispatchEx("Excel.Application")
            try:
                excel.Quit()
            except Exception:  # noqa: BLE001
                pass
        finally:
            pythoncom.CoUninitialize()
        _disponible = True
    except Exception:  # noqa: BLE001
        logger.warning("Excel no esta disponible por COM; se omiten los pivots")
        _disponible = False
    return _disponible


def _cerrar_excel(excel, pid: int | None) -> None:
    """Cierra Excel y comprueba que el proceso de verdad termino.

    `Quit()` no siempre basta -- un workbook que quedo abierto sin cerrar
    detiene el proceso en silencio, sin dialogo que lo delate porque
    `Visible=False` lo esconde. Se le da un segundo y, si sigue vivo, se
    termina por PID: mejor un proceso menos que uno acumulandose en cada
    reporte.
    """
    try:
        excel.Quit()
    except Exception:  # noqa: BLE001
        logger.exception("Excel.Quit() fallo")
    if pid is None:
        return
    time.sleep(1)
    try:
        import win32api
        import win32con

        identificador = win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, pid)
    except Exception:  # noqa: BLE001
        return
    if identificador:
        try:
            win32api.TerminateProcess(identificador, 0)
            logger.warning("Excel (pid %s) seguia vivo tras Quit(); se termino", pid)
        except Exception:  # noqa: BLE001
            pass
        finally:
            win32api.CloseHandle(identificador)


def _escribir_pivot(ruta_xlsx: str, hoja_datos: str, hoja_pivot: str,
                     filas: list[str], columnas: list[str],
                     medida: str, nombre_medida: str,
                     con_share: bool) -> None:
    """El trabajo real, pensado para correr dentro del hilo con plazo.

    COM es por hilo: un apartamento no se comparte con el hilo que lo creo.
    Esta funcion SIEMPRE corre en un hilo nuevo (el de `agregar_pivot`, para
    poder ponerle plazo), asi que necesita su propio `CoInitialize` aunque
    el hilo que llama ya haya usado COM. Sin esto revienta con "CoInitialize
    has not been called" -- y lo hizo solo la SEGUNDA vez que se probo: la
    primera, `gencache.EnsureDispatch` todavia no tenia en disco el
    envoltorio de la libreria de Excel, y construirlo de cero disparaba un
    CoInitialize de casualidad. En cuanto ese cache ya existe, la ruta
    rapida de `EnsureDispatch` se lo salta y el hilo se queda sin
    apartamento COM.
    """
    import pythoncom
    import win32com.client as win32
    from win32com.client import gencache
    from win32com.client import constants as C

    pythoncom.CoInitialize()
    try:
        gencache.EnsureDispatch("Excel.Application")
        _construir_pivot(
            win32, C, ruta_xlsx, hoja_datos, hoja_pivot, filas, columnas,
            medida, nombre_medida, con_share,
        )
    finally:
        pythoncom.CoUninitialize()


def _construir_pivot(win32, C, ruta_xlsx: str, hoja_datos: str,
                      hoja_pivot: str, filas: list[str], columnas: list[str],
                      medida: str, nombre_medida: str,
                      con_share: bool) -> None:
    """La parte que de verdad habla con Excel, ya con COM inicializado."""
    excel = win32.DispatchEx("Excel.Application")
    excel.Visible = False
    excel.DisplayAlerts = False
    pid = None
    try:
        import win32process

        _, pid = win32process.GetWindowThreadProcessId(excel.Hwnd)

        libro = excel.Workbooks.Open(os.path.abspath(ruta_xlsx))
        try:
            datos = libro.Worksheets(hoja_datos)
            ultima_fila = datos.Cells(datos.Rows.Count, 1).End(-4162).Row  # xlUp
            ultima_columna = datos.Cells(1, datos.Columns.Count).End(-4159).Column  # xlToLeft
            rango = datos.Range(
                datos.Cells(1, 1), datos.Cells(ultima_fila, ultima_columna)
            )

            pivot = libro.Worksheets.Add()
            pivot.Name = hoja_pivot

            cache = libro.PivotCaches().Create(
                SourceType=C.xlDatabase, SourceData=rango
            )
            tabla = cache.CreatePivotTable(
                TableDestination=pivot.Range("B3"), TableName="Pivot1"
            )

            for indice, nombre in enumerate(filas, start=1):
                campo = tabla.PivotFields(nombre)
                campo.Orientation = C.xlRowField
                campo.Position = indice
            for nombre in columnas:
                tabla.PivotFields(nombre).Orientation = C.xlColumnField

            # La etiqueta NO puede ser igual al nombre de la columna fuente:
            # Excel la rechaza (probado). "Suma de X" nunca choca con la
            # columna que la origina.
            etiqueta_suma = f"Suma de {nombre_medida}"
            tabla.AddDataField(tabla.PivotFields(medida), etiqueta_suma, C.xlSum)
            if con_share and filas:
                # % del padre: la porcion de la ultima fila (la mas
                # especifica, ej. Marca) dentro de la fila que la contiene
                # (ej. Tienda). Es el "share por tienda" tal cual se pidio.
                campo_share = tabla.AddDataField(
                    tabla.PivotFields(medida), "Share %", C.xlSum
                )
                campo_share.Calculation = C.xlPercentOfParentRow
                campo_share.NumberFormat = "0.0%"

            # Sin totales generales: un "Grand Total" de Share % suma
            # porcentajes de filas distintas y no significa nada.
            tabla.RowGrand = False
            tabla.ColumnGrand = False

            libro.Save()
        finally:
            libro.Close(SaveChanges=False)
    finally:
        # Ademas del libro que abrimos: si Excel trae otro por su cuenta al
        # arrancar (un Book1 en blanco, segun la instalacion), Quit() se
        # queda esperando por el sin avisar -- Visible=False le quita hasta
        # el dialogo que delataria por que. Cerrar todo lo que haya, a
        # ciegas, cuesta nada y evita depender solo del kill de emergencia.
        try:
            for libro_abierto in list(excel.Workbooks):
                try:
                    libro_abierto.Close(SaveChanges=False)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass
        _cerrar_excel(excel, pid)


def agregar_pivot(
    ruta_xlsx: str,
    hoja_datos: str,
    filas: list[str],
    columnas: list[str],
    medida: str,
    nombre_medida: str = "Unidades",
    hoja_pivot: str = "PIVOT",
    con_share: bool = True,
) -> bool:
    """Agrega una hoja con un PivotTable real al archivo ya guardado.

    Devuelve False sin lanzar nada si Excel no esta disponible, si el
    trabajo no termino a tiempo, o si algo salio mal: el archivo que ya
    existe (con "Datos" y el resto de las hojas) no se toca, y el reporte
    sale igual sin el pivot en vez de hacer esperar al usuario.
    """
    if not pivot_disponible():
        return False

    if not _CANDADO.acquire(timeout=SEGUNDOS_PARA_EL_PIVOT):
        logger.warning("Otro pivot ya estaba en curso; se omite este")
        return False
    try:
        error: list[BaseException] = []

        def _correr():
            try:
                _escribir_pivot(
                    ruta_xlsx, hoja_datos, hoja_pivot, filas, columnas,
                    medida, nombre_medida, con_share,
                )
            except BaseException as exc:  # noqa: BLE001
                error.append(exc)

        hilo = threading.Thread(target=_correr, daemon=True)
        hilo.start()
        hilo.join(SEGUNDOS_PARA_EL_PIVOT)
        if hilo.is_alive():
            # No hay forma limpia de matar un hilo de Python desde afuera;
            # se deja corriendo (es un daemon, no bloquea el apagado) y se
            # sigue sin el pivot. El proceso de Excel que haya quedado
            # atras es cosa de la maquina, no de este turno.
            logger.warning(
                "El pivot no termino en %ss; se entrega el reporte sin el",
                SEGUNDOS_PARA_EL_PIVOT,
            )
            return False
        if error:
            logger.exception("No se pudo armar el pivot", exc_info=error[0])
            return False
        return True
    finally:
        _CANDADO.release()


def config_ventas_por_tienda(df: pd.DataFrame) -> dict | None:
    """La config de pivot para una hoja de ventas_crudas (contexto.py).

    Filas Tienda > Marca, columnas Mes, con el share de cada marca dentro de
    su tienda. None si la hoja no trae las columnas esperadas -- un archivo
    a medio construir no debe intentar un pivot que va a fallar seguro.
    """
    esperadas = {"Tienda", "Marca", "Mes", "Unidades"}
    if not esperadas.issubset(df.columns):
        return None
    return {
        "filas": ["Tienda", "Marca"],
        "columnas": ["Mes"],
        "medida": "Unidades",
        "nombre_medida": "Unidades",
        "con_share": True,
    }
