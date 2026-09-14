import pandas as pd
import os
import uuid
from datetime import datetime

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

        # Exportar
        df.to_excel(filepath, index=False, engine='openpyxl')

        # Absoluta siempre, sin importar si output_dir ya lo era: es lo que
        # consume Node al otro lado del contrato HTTP (ver comentario arriba).
        return os.path.abspath(filepath)
