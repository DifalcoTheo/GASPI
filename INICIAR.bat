@echo off
title Ordenes de Fabricacion - NO CERRAR
cd /d "%~dp0"

if not exist "datos\sistema_of.db" (
    echo Primera vez: importando datos...
    python importar.py
    echo.
)

if "%PUERTO_OF%"=="" set PUERTO_OF=8000
start "" http://localhost:%PUERTO_OF%
python app.py

echo.
echo El sistema se detuvo. Si fue por un error, el detalle esta arriba.
pause
