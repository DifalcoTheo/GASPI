@echo off
setlocal
title GASPI - Demo (no cerrar esta ventana)
cd /d "%~dp0"

echo ============================================================
echo   GASPI - demo con datos ficticios
echo ============================================================
echo.

rem Si la demo ya esta corriendo, solo abrir el navegador
netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul
if not errorlevel 1 (
    echo La demo ya esta abierta en otra ventana.
    start "" http://127.0.0.1:8000
    pause
    exit /b 0
)

rem Buscar Python
set "PY="
python --version >nul 2>&1
if not errorlevel 1 set "PY=python"
if not defined PY (
    py -3 --version >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    echo No se encontro Python en esta PC.
    echo Instalalo desde https://www.python.org/downloads/
    echo y marca la opcion "Add python.exe to PATH".
    pause
    exit /b 1
)

rem Primera vez: entorno aislado + dependencias
if not exist ".venv\demo_ok.txt" (
    echo Primera vez: instalando dependencias, puede tardar un par de minutos...
    if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :error
    echo ok> ".venv\demo_ok.txt"
)

rem Datos ficticios de la demo
if not exist "datos\sistema_of.db" (
    echo Generando los datos de la demo...
    ".venv\Scripts\python.exe" seed_demo.py || goto :error
)

echo.
echo Usuarios: ingenieria / produccion / compras    Clave: changeme
echo Para cerrar la demo, cerra esta ventana.
echo.
start "" /min cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:8000"
".venv\Scripts\python.exe" app.py
pause
exit /b 0

:error
echo.
echo Algo fallo. El detalle del error esta arriba.
pause
exit /b 1
