# ==========================================================
# LANZADOR DEL SISTEMA DE ORDENES DE FABRICACION
#
# Hace lo mismo que INICIAR.bat, pero se compila a .exe
# para poder ponerle icono.
#
# Compilar:
#   pyinstaller --onefile --icon herramientas.ico --name "INICIAR" iniciar.py
# ==========================================================

import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path


def carpeta_base() -> Path:
    """Carpeta donde vive el .exe (o el .py si se corre suelto)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def buscar_python() -> str | None:
    """Busca el Python instalado en la PC. El .exe no lo trae adentro."""
    for candidato in ("python", "python3", "py"):
        ruta = shutil.which(candidato)
        if ruta:
            return ruta
    return None


def salir_con_error(mensaje: str) -> None:
    print()
    print("=" * 55)
    print(mensaje)
    print("=" * 55)
    print()
    input("Apreta ENTER para cerrar esta ventana...")
    sys.exit(1)


def abrir_navegador(url: str) -> None:
    """Espera a que el servidor levante y recien ahi abre el navegador."""
    time.sleep(2.5)
    webbrowser.open(url)


def main() -> None:
    base = carpeta_base()
    os.chdir(base)

    python = buscar_python()
    if not python:
        salir_con_error(
            "No se encontro Python en esta PC.\n"
            "Instalalo desde https://www.python.org/downloads/\n"
            'y marca la opcion "Add Python to PATH".'
        )

    if not (base / "app.py").exists():
        salir_con_error(
            f"No se encontro app.py en:\n  {base}\n\n"
            "El INICIAR.exe tiene que estar en la misma carpeta que el sistema."
        )

    # Primera vez: arma la base de datos
    if not (base / "datos" / "sistema_of.db").exists():
        print("Primera vez: importando datos...")
        print()
        resultado = subprocess.run([python, "importar.py"])
        if resultado.returncode != 0:
            salir_con_error("Fallo la importacion inicial. El detalle esta arriba.")
        print()

    puerto = os.environ.get("PUERTO_OF") or "8000"
    threading.Thread(
        target=abrir_navegador,
        args=(f"http://localhost:{puerto}",),
        daemon=True,
    ).start()

    resultado = subprocess.run([python, "app.py"])

    if resultado.returncode != 0:
        salir_con_error("El sistema se detuvo por un error. El detalle esta arriba.")

    print()
    print("El sistema se detuvo.")
    input("Apreta ENTER para cerrar esta ventana...")


if __name__ == "__main__":
    main()
