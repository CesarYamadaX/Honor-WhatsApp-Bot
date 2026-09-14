import argparse
from dotenv import load_dotenv
from agent import SQLAgent
from excel_exporter import ExcelExporter
import sys

def main():
    print("Iniciando Agente Text-to-SQL...")
    
    try:
        # Cargar variables y conectar
        agent = SQLAgent()
        print("✅ Conexión exitosa a la base de datos y modelo LLM inicializado.")
    except Exception as e:
        print(f"❌ Error de inicialización: {e}")
        print("Por favor verifica tu archivo .env")
        sys.exit(1)

    print("\n--- Modo Interactivo ---")
    print("Escribe tu pregunta o 'salir' para terminar.")
    
    while True:
        try:
            question = input("\nPregunta: ")
            
            if question.strip().lower() in ['salir', 'exit', 'quit']:
                print("Hasta luego!")
                break
                
            if not question.strip():
                continue
                
            # Ejecutar el agente
            print("Analizando...")
            texto, archivos = agent.responder(question)

            print(f"\n🤖 Respuesta:\n{texto}\n")

            if archivos:
                print("Archivos generados:")
                for archivo in archivos:
                    print(archivo)
            
            # Opción para exportar si el usuario quiere, asumiendo que la pregunta fue de exportación
            # o podemos hacerlo más interactivo preguntando:
            # Para hacer el export, idealmente el agente debe devolver el SQL que ejecutó, 
            # pero para simplificar, en este flujo básico le pedimos al usuario que si 
            # quiere exportar, debe proveer una consulta específica o se la pedimos al agente.
            # En una versión más avanzada, la herramienta de exportación estaría DENTRO del agente.
            
        except KeyboardInterrupt:
            print("\nSaliendo...")
            break
        except Exception as e:
            print(f"❌ Error durante la ejecución: {e}")

if __name__ == "__main__":
    main()
