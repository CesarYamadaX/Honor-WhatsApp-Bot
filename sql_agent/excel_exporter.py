import logging
import os
import uuid
from datetime import date, datetime

import pandas as pd
import homologacion
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


def _es_fecha(columna: pd.Series) -> bool:
    """Columna de fechas: la de pandas, o una de objetos date/datetime, que es
    como el driver entrega a veces una columna DATE de SQL Server."""
    if pd.api.types.is_datetime64_any_dtype(columna):
        return True
    if columna.dtype != object:
        return False
    muestra = columna.dropna().head(200)
    return not muestra.empty and all(isinstance(v, (date, datetime)) for v in muestra)


def _ancho(nombre, columna: pd.Series) -> int:
    """El ancho de la columna en caracteres, como el autoajuste de Excel.

    Se mide lo que se VE: una fecha se muestra como yyyy-mm-dd aunque guarde
    la hora, asi que su ancho es el del formato. El resto, el texto mas largo
    de la columna entera (con pandas, sin recorrer celda por celda).
    """
    if _es_fecha(columna):
        ancho_del_dato = len(FORMATO_FECHA)
    else:
        textos = columna.dropna().astype(str)
        ancho_del_dato = int(textos.str.len().max()) if not textos.empty else 0
    return max(len(str(nombre)), ancho_del_dato) + MARGEN_COLUMNA


def _valores(columna: pd.Series) -> list:
    """La columna como valores de Python que xlsxwriter escribe tal cual;
    lo vacio (NaN, NaT, None) como None, que deja la celda en blanco."""
    if pd.api.types.is_datetime64_any_dtype(columna):
        fechas = columna.dt.tz_localize(None) if getattr(columna.dt, "tz", None) else columna
        return [None if pd.isna(v) else v.to_pydatetime() for v in fechas]
    if pd.api.types.is_bool_dtype(columna):
        return [None if pd.isna(v) else bool(v) for v in columna.tolist()]
    if pd.api.types.is_numeric_dtype(columna):
        return [None if pd.isna(v) else v for v in columna.astype(object).tolist()]
    return [None if (v is None or (isinstance(v, float) and v != v) or v is pd.NaT or v is pd.NA) else v
            for v in columna.astype(object).tolist()]


def _escribir(ruta: str, por_hoja: list[tuple[str, pd.DataFrame]]) -> None:
    """Escribe las hojas con xlsxwriter, columna por columna.

    Medido el 20/10/2026 con una hoja de 100 mil filas x 22 columnas (un mes
    de Elektra): openpyxl, como era antes, 47.5 s; pandas con xlsxwriter
    29.9 s; columna por columna 11 s. En un reporte de cadena el Excel era lo
    mas lento (Coppel, un mes: 48 de 66 s; Telcel: 78 s).

    Mismo resultado que antes: fechas como dia (la hora se queda en la celda),
    cada columna al ancho de su contenido, y la que no cabe en el ancho
    maximo, topada y con el texto ajustado en varias lineas. El formato va en
    cada celda, no solo en la columna, para que cualquier lector lo vea.
    """
    import xlsxwriter

    libro = xlsxwriter.Workbook(ruta, {
        # Que un texto se quede texto: "0012" o "=algo" de una descripcion no
        # son un numero ni una formula.
        "strings_to_numbers": False, "strings_to_formulas": False, "strings_to_urls": False,
    })
    try:
        encabezado = libro.add_format({"bold": True})
        fecha = libro.add_format({"num_format": FORMATO_FECHA})
        ajustado = libro.add_format({"text_wrap": True, "valign": "top"})
        for nombre, trozo in por_hoja:
            hoja = libro.add_worksheet(nombre)
            hoja.write_row(0, 0, [str(c) for c in trozo.columns], encabezado)
            for indice, columna in enumerate(trozo.columns):
                serie = trozo.iloc[:, indice]
                ancho = _ancho(columna, serie)
                es_fecha = _es_fecha(serie)
                formato = fecha if es_fecha else (ajustado if ancho > ANCHO_MAXIMO else None)
                hoja.set_column(indice, indice, min(max(ancho, ANCHO_MINIMO), ANCHO_MAXIMO))
                if len(trozo):
                    hoja.write_column(1, indice, _valores(serie), formato)
            hoja.freeze_panes(1, 0)
    finally:
        libro.close()


class ExcelExporter:
    @staticmethod
    def export_to_excel(
        df: pd.DataFrame,
        filename: str = None,
        contexto: dict[str, pd.DataFrame] | None = None,
        con_datos: bool = True,
        rapido: bool = False,
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

        `rapido` ya no cambia nada: desde el 20/10/2026 todo se escribe con
        el escritor rapido (ver _escribir). Se deja para no romper a quien
        lo pasa.
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
        # Y en cada hoja con descripciones de producto, su modelo homologado
        # al lado (ver homologacion.py): pedido el 8/10/2026, SIEMPRE.
        df = ordenar_por_tiempo(homologacion.con_modelo(df))
        contexto = {
            nombre: ordenar_por_tiempo(homologacion.con_modelo(ficha))
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

        _escribir(filepath, por_hoja)

        # Absoluta siempre, sin importar si output_dir ya lo era: es lo que
        # consume Node al otro lado del contrato HTTP (ver comentario arriba).
        return os.path.abspath(filepath)
