@echo off
rem Abre el panel del bot como aplicacion (ventana propia, sin navegador
rem alrededor). El bot tiene que estar encendido; si no, lo dice.
setlocal
cd /d "%~dp0"
if exist "sql_agent\.venv\Scripts\python.exe" (
  "sql_agent\.venv\Scripts\python.exe" panel.py %*
) else (
  python panel.py %*
)
endlocal
