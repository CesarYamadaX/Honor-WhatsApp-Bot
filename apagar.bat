@echo off
rem Apaga el bot: baja el sidecar de WhatsApp y el servicio Python.
setlocal
cd /d "%~dp0"
if exist "sql_agent\.venv\Scripts\python.exe" (
  "sql_agent\.venv\Scripts\python.exe" apagar.py %*
) else (
  python apagar.py %*
)
endlocal
