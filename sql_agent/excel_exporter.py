import logging
import os
import uuid
from datetime import datetime

import pandas as pd
from openpyxl.styles import Alignment
from openpyxl.utils import get_column_letter

from tiempo import ordenar_por_tiempo

logger = logging.getLogger("sql_agent.excel_exporter")

HOJA_DATOS = "Datos"

FORMATO_FECHA = "yyyy-mm-dd"

# Ancho de columna, en caracteres del tipo por defecto. El minimo evita que
# una columna de numeros cortos quede ridicula junto a su encabezado; el
# maximo evita que una descripcion de producto de 300 caracteres empuje el
# resto de la tabla fuera de la pantalla del telefono. El margen es el aire
# que Excel deja a los lados al autoajustar.
ANCHO_MINIMO = 8
ANCHO_MAXIMO = 50
MARGEN_COLUMNA = 2

# Cuantas filas de datos caben en UNA hoja de Excel. El formato admite
# 1 048 576 en total y la primera se va en el encabezado. No es una decision
# nuestra ni un presupuesto que se pueda ajustar: es el techo del archivo, y
# pasarse no da un Excel grande, da uno que no abre.
MAX_FILAS_POR_HOJA_DE_EXCEL = 1_048_575


def partir_en_hojas(nombre: str, df: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    """Esa tabla, en tantas hojas como Excel necesite para no perder una fila.

    Devuelve [(nombre, df)] mientras quepa en una, que es el caso normal --
    el peor caso medido son 301 319 filas, un tercio del limite. Si alguna
    vez no cabe, salen "Raw_Data", "Raw_Data (2)", ... en vez de cortarse.

    Los nombres se recortan a los 31 caracteres que Excel admite CONTANDO el
    sufijo: si se recortara antes, dos trozos de una hoja de nombre largo
    terminarian llamandose igual y el archivo no se escribiria.
    """
    if df is None or df.empty:
        return [(nombre, df)]

    total = len(df)
    if total <= MAX_FILAS_POR_HOJA_DE_EXCEL:
        return [(str(nombre)[:31], df)]

    trozos = []
    for numero, comienzo in enumerate(
        range(0, total, MAX_FILAS_POR_HOJA_DE_EXCEL), start=1
    ):
        sufijo = "" if numero == 1 else f" ({numero})"
        trozos.append((
            f"{str(nombre)[:31 - len(sufijo)]}{sufijo}",
            df.iloc[comienzo:comienzo + MAX_FILAS_POR_HOJA_DE_EXCEL],
        ))
    return trozos


def _formatear_columnas_de_fecha(hoja, df: pd.DataFrame) -> None:
    """Muestra las fechas como dia, sin hora.

    pandas escribe datetime64 con formato de fecha y hora, asi que una fecha a
    medianoche se rinde "2026-01-01 00:00:00": una columna ancha llena de
    ceros que nadie lee.

    Esto cambia el FORMATO, no el valor: la celda sigue guardando el datetime
    completo, asi que la hora se ve en la barra de formulas y vuelve con
    cambiarle el formato a la columna. No se pierde nada.
    """
    for indice, nombre in enumerate(df.columns, start=1):
        if not pd.api.types.is_datetime64_any_dtype(df[nombre]):
            continue
        for (celda,) in hoja.iter_rows(min_col=indice, max_col=indice, min_row=2):
            celda.number_format = FORMATO_FECHA


def _ajustar_ancho_de_columnas(hoja, df: pd.DataFrame) -> None:
    """Da a cada columna el ancho de su contenido, como el autoajuste de Excel.

    openpyxl no tiene autoajuste: el "ajustar al contenido" de Excel lo calcula
    Excel al abrir el archivo y solo si alguien lo pide a mano. Aqui se mide el
    texto mas largo de cada columna y se fija el ancho, para que el reporte
    llegue legible sin que el usuario toque nada.
    """
    for indice, nombre in enumerate(df.columns, start=1):
        columna = df[nombre]
        if pd.api.types.is_datetime64_any_dtype(columna):
            # Se mide lo que se VE, no lo que se guarda: la celda conserva la
            # hora pero se muestra como yyyy-mm-dd. En un formato de fecha
            # numerico cada caracter del codigo rinde un caracter en pantalla,
            # asi que su largo es el ancho mostrado.
            ancho_del_dato = len(FORMATO_FECHA)
        else:
            textos = columna.dropna().astype(str)
            ancho_del_dato = int(textos.str.len().max()) if not textos.empty else 0

        ancho = max(len(str(nombre)), ancho_del_dato) + MARGEN_COLUMNA
        hoja.column_dimensions[get_column_letter(indice)].width = min(
            max(ancho, ANCHO_MINIMO), ANCHO_MAXIMO
        )

        if ancho > ANCHO_MAXIMO:
            # La columna no cabe en el tope. Ensancharla hasta el texto mas
            # largo (una descripcion de producto puede pasar de 90 caracteres)
            # empujaria el resto de la tabla fuera de la pantalla del
            # telefono, pero recortarla escondería datos. Se ajusta el texto:
            # ocupa varias lineas y se ve completo. Excel calcula solo el alto
            # de la fila mientras nadie lo fije a mano.
            for (celda,) in hoja.iter_rows(min_col=indice, max_col=indice, min_row=2):
                celda.alignment = Alignment(wrap_text=True, vertical="top")


class ExcelExporter:
    @staticmethod
    def export_to_excel(
        df: pd.DataFrame,
        filename: str = None,
        contexto: dict[str, pd.DataFrame] | None = None,
        con_datos: bool = True,
    ) -> str:
        """
        Exporta un DataFrame de Pandas a un archivo Excel.
        Si no se proporciona un nombre de archivo, se genera uno con la fecha actual
        mas un sufijo aleatorio.

        `contexto` son hojas adicionales con la ficha de las tiendas, modelos
        y carriers que aparecen en el resultado (ver contexto.py). Van DESPUES
        de los datos: la primera hoja es siempre lo que se pregunto.

        `con_datos=False` deja fuera la hoja "Datos" (el resultado de la
        consulta) cuando hay otras hojas: si el resultado es un numero que ya
        llego entero en el mensaje, esa hoja no aporta nada (pedido el
        5/10/2026). Sin otras hojas se pone igual: un Excel no puede ir vacio.
        """
        if df is None or df.empty:
            raise ValueError("No hay datos para exportar. El DataFrame está vacío.")

        # Generar nombre de archivo por defecto si no existe.
        # El timestamp solo resuelve al segundo: dos peticiones concurrentes
        # (por ejemplo de dos conversaciones de WhatsApp distintas) caerian
        # en el mismo nombre y una pisaria el archivo de la otra. El sufijo
        # aleatorio evita esa colision sin cambiar el comportamiento cuando
        # el llamador si especifica un filename.
        if not filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            sufijo = uuid.uuid4().hex[:8]
            filename = f"reporte_sql_{timestamp}_{sufijo}.xlsx"
            
        # Asegurarse de que termine en .xlsx
        if not filename.endswith('.xlsx'):
            filename += '.xlsx'

        # Directorio de exportaciones. `EXPORT_DIR` permite fijarlo desde
        # afuera (y es lo que usan las pruebas para aislar los archivos que
        # generan); si no esta definida, el default es "exports" junto a
        # este modulo, NO relativo al cwd del proceso que lo importa.
        #
        # Esto es Critico 1 de la revision final: la ruta viaja por HTTP a
        # otro proceso (el sidecar de Node), que la resuelve contra SU
        # propio cwd. Si aqui se devuelve una ruta relativa como "exports/x.xlsx",
        # Node la busca en "whatsapp/exports/x.xlsx" (su cwd), que nunca
        # existe, y CADA consulta de mas de MAX_ROWS_AS_TEXT filas falla al
        # enviar el archivo. El contrato HTTP exige rutas absolutas
        # justamente porque ambos procesos no comparten cwd aunque compartan
        # maquina.
        output_dir = os.environ.get("EXPORT_DIR") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "exports"
        )
        os.makedirs(output_dir, exist_ok=True)

        filepath = os.path.join(output_dir, filename)

        # Solo datos: la grafica ya no vive aqui, se manda como imagen aparte
        # (ver grafica.py). En el telefono nadie abre la segunda hoja de un
        # adjunto; una imagen se ve en la conversacion misma.
        # El archivo sale en orden cronologico, del mes mas viejo al mas
        # reciente. La consulta casi nunca viene asi -- el agente ordena por
        # cantidad, buscando los mas vendidos -- y el reporte llegaba con
        # agosto arriba y marzo al final. Ver tiempo.ordenar_por_tiempo: el
        # orden es estable, asi que dentro de cada mes se conserva el de la
        # consulta, y un resultado sin tiempo se deja como esta.
        df = ordenar_por_tiempo(df)
        contexto = {
            nombre: ordenar_por_tiempo(ficha)
            for nombre, ficha in (contexto or {}).items()
        }

        # Cada tabla puede ocupar MAS de una hoja: ninguna fila se tira por no
        # caber. Se lleva el mapa hoja -> tabla porque despues hay que dar
        # formato a cada una con SU trozo, no con la tabla entera.
        por_hoja: list[tuple[str, pd.DataFrame]] = []
        for nombre, ficha in (contexto or {}).items():
            if ficha is None or ficha.empty:
                continue
            por_hoja.extend(partir_en_hojas(nombre, ficha))
        if con_datos or not por_hoja:
            por_hoja = partir_en_hojas(HOJA_DATOS, df) + por_hoja

        with pd.ExcelWriter(filepath, engine="openpyxl") as escritor:
            for nombre, trozo in por_hoja:
                trozo.to_excel(escritor, index=False, sheet_name=nombre)
            try:
                for nombre, trozo in por_hoja:
                    _formatear_columnas_de_fecha(escritor.book[nombre], trozo)
                    _ajustar_ancho_de_columnas(escritor.book[nombre], trozo)
            except Exception:
                # Los datos son lo que el usuario pidio; el formato es un
                # extra. Un resultado con una forma que no se previo aqui no
                # puede costarle el reporte entero.
                logger.exception("No se pudo dar formato a la tabla; se envia igual")

        # Absoluta siempre, sin importar si output_dir ya lo era: es lo que
        # consume Node al otro lado del contrato HTTP (ver comentario arriba).
        return os.path.abspath(filepath)
