import logging
import os
import uuid
from datetime import datetime

import pandas as pd
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.chart.axis import ChartLines
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.drawing.line import LineProperties

logger = logging.getLogger("sql_agent.excel_exporter")

HOJA_DATOS = "Datos"
HOJA_GRAFICA = "Grafica"

# Paleta categorica en orden fijo, validada con el validador de la guia de
# visualizacion contra el blanco de Excel (#FFFFFF): banda de luminosidad,
# piso de croma, separacion CVD (peor par adyacente dE 9.1) y piso de vision
# normal (19.6) pasan. Tres slots quedan bajo 3:1 de contraste, lo que obliga
# a "relieve": aqui lo da la hoja de datos, que siempre viaja en el mismo
# archivo con todos los valores.
#
# El orden importa y no es cosmetico: es el mecanismo que garantiza la
# separacion para daltonismo. Nunca reordenar ni generar un color nuevo para
# una septima serie -- por eso MAX_SERIES corta antes.
PALETA_SERIES = ("2A78D6", "EB6834", "1BAF7A", "EDA100", "E87BA4", "008300")
COLOR_REJILLA = "E1E0D9"
COLOR_EJE = "C3C2B7"

FORMATO_FECHA = "yyyy-mm-dd"

# Ancho de linea en EMU (12700 = 1 pt). La rejilla y los ejes van de
# hairline para que no compitan con los datos.
GROSOR_HAIRLINE = 9525
GROSOR_LINEA_SERIE = 25400

# Mas series que esto y los colores dejan de distinguirse; mas categorias y
# las etiquetas del eje se amontonan. La tabla completa sigue en la hoja de
# datos, asi que recortar la grafica no esconde nada.
MAX_SERIES = 6
MAX_CATEGORIAS = 25


def _hairline(color: str) -> GraphicalProperties:
    return GraphicalProperties(ln=LineProperties(solidFill=color, w=GROSOR_HAIRLINE))


def _planear_grafica(df: pd.DataFrame):
    """Elige que graficar, o None si ninguna grafica ayudaria.

    Devuelve (columna_categoria, columnas_de_valor, es_serie_temporal).

    Las reglas salen de la guia de visualizacion: una sola fila es un numero,
    no una grafica; sin columna numerica no hay magnitud que mostrar; y una
    columna de fecha convierte la pregunta en "como evoluciona" (linea) en vez
    de "quien es mayor" (barras).
    """
    if len(df) < 2:
        return None

    numericas = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    if not numericas:
        return None

    fechas = [c for c in df.columns if pd.api.types.is_datetime64_any_dtype(df[c])]
    if fechas:
        return fechas[0], numericas[:MAX_SERIES], True

    etiquetas = [c for c in df.columns if c not in numericas]
    if etiquetas:
        return etiquetas[0], numericas[:MAX_SERIES], False

    # Todo numerico (p. ej. [Month] y [QTY] de un GROUP BY): la primera hace
    # de etiqueta y el resto son los valores. Con una sola columna numerica no
    # queda nada que medir contra que.
    if len(numericas) >= 2:
        return numericas[0], numericas[1 : MAX_SERIES + 1], False
    return None


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


def _agregar_grafica(libro, df: pd.DataFrame) -> None:
    plan = _planear_grafica(df)
    if plan is None:
        return
    categoria, valores, es_temporal = plan

    hoja_datos = libro[HOJA_DATOS]
    filas = min(len(df), MAX_CATEGORIAS)
    columna_de = {nombre: i + 1 for i, nombre in enumerate(df.columns)}

    grafica = LineChart() if es_temporal else BarChart()
    if not es_temporal:
        grafica.type = "col"

    for nombre in valores:
        grafica.add_data(
            Reference(
                hoja_datos,
                min_col=columna_de[nombre],
                min_row=1,
                max_row=1 + filas,
            ),
            titles_from_data=True,
        )
    grafica.set_categories(
        Reference(
            hoja_datos,
            min_col=columna_de[categoria],
            min_row=2,
            max_row=1 + filas,
        )
    )

    for indice, serie in enumerate(grafica.series):
        color = PALETA_SERIES[indice % len(PALETA_SERIES)]
        if es_temporal:
            serie.graphicalProperties.line.solidFill = color
            serie.graphicalProperties.line.width = GROSOR_LINEA_SERIE
            # Sin esto Excel interpola con una curva suave que se sale del
            # rango real: con datos entre 3 y 9 la curva llega a 2.8 y 9.2.
            # Son valores que nadie midio, dibujados como si fueran datos.
            serie.smooth = False
        else:
            # Un solo solidFill para toda la serie pinta TODAS las barras
            # igual. Es deliberado: colorear cada barra segun su tamano
            # duplicaria en el color lo que la longitud ya dice, y gastaria
            # el unico canal libre que queda.
            serie.graphicalProperties.solidFill = color
            serie.graphicalProperties.line.noFill = True

    titulo = ", ".join(str(v) for v in valores) + f" por {categoria}"
    if len(df) > filas:
        titulo += f" (primeras {filas} de {len(df):,})".replace(",", " ")
    grafica.title = titulo

    # Una sola serie se nombra sola en el titulo; una leyenda de un elemento
    # es ruido. Con dos o mas, la leyenda es obligatoria: el color no puede
    # ser lo unico que distinga las series.
    if len(grafica.series) < 2:
        grafica.legend = None
    else:
        grafica.legend.position = "b"

    grafica.y_axis.majorGridlines = ChartLines(spPr=_hairline(COLOR_REJILLA))
    grafica.x_axis.majorGridlines = None
    for eje in (grafica.x_axis, grafica.y_axis):
        eje.spPr = _hairline(COLOR_EJE)
        # openpyxl escribe delete=1 por defecto y Excel esconde el eje.
        eje.delete = False

    if es_temporal:
        # El eje muestra el dia y nada mas. Por defecto la marca hereda el
        # formato de la celda, asi que una columna con hora real llenaria el
        # eje de "2026-03-01 08:30" partido en dos lineas. Aqui se fija el
        # formato del eje sin tocar la hoja de datos, que conserva la hora
        # cuando la hora es un dato.
        grafica.x_axis.number_format = FORMATO_FECHA
        grafica.x_axis.numFmt.sourceLinked = False

    grafica.height = 10
    grafica.width = 24

    libro.create_sheet(HOJA_GRAFICA).add_chart(grafica, "B2")


class ExcelExporter:
    @staticmethod
    def export_to_excel(df: pd.DataFrame, filename: str = None) -> str:
        """
        Exporta un DataFrame de Pandas a un archivo Excel.
        Si no se proporciona un nombre de archivo, se genera uno con la fecha actual
        mas un sufijo aleatorio.
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

        # Exportar. La grafica va en su propia hoja, despues de los datos.
        with pd.ExcelWriter(filepath, engine="openpyxl") as escritor:
            df.to_excel(escritor, index=False, sheet_name=HOJA_DATOS)
            try:
                _formatear_columnas_de_fecha(escritor.book[HOJA_DATOS], df)
                _agregar_grafica(escritor.book, df)
            except Exception:
                # Los datos son lo que el usuario pidio; la grafica es un
                # extra. Un resultado con una forma que no se previo aqui no
                # puede costarle el reporte entero.
                logger.exception("No se pudo componer la grafica; se envia solo la tabla")

        # Absoluta siempre, sin importar si output_dir ya lo era: es lo que
        # consume Node al otro lado del contrato HTTP (ver comentario arriba).
        return os.path.abspath(filepath)
