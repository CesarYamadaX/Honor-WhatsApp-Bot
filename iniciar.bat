@echo off
rem Arranca el bot completo con doble clic o escribiendo `iniciar` en la
rem terminal. Usa el python del venv del servicio si existe; si no, el del
rem sistema, para que iniciar.py pueda explicar que falta el entorno en vez
rem de fallar aqui con un mensaje de cmd.
setlocal
cd /d "%~dp0"
if exist "sql_agent\.venv\Scripts\python.exe" (
  "sql_agent\.venv\Scripts\python.exe" iniciar.py %*
) else (
  python iniciar.py %*
)
endlocal
