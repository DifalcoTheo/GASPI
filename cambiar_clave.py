# -*- coding: utf-8 -*-
"""Cambia la contraseña de un rol de GASPI (ingenieria / produccion / compras).
Correr con:  .venv\\Scripts\\python.exe cambiar_clave.py   — la clave se tipea localmente."""
import getpass
import hashlib
import json
import os
import sys
from pathlib import Path

F = Path(__file__).parent / "datos" / "usuarios_gaspi.json"

users = json.loads(F.read_text(encoding="utf-8"))
print("Roles:", ", ".join(users))
r = input("Rol a cambiar: ").strip().lower()
if r not in users:
    print("Ese rol no existe."); sys.exit(1)

p1 = getpass.getpass("Nueva contraseña: ")
p2 = getpass.getpass("Repetir contraseña: ")
if not p1 or p1 != p2:
    print("Vacía o no coinciden. No se cambió nada."); sys.exit(1)

salt = os.urandom(16).hex()
users[r] = {"salt": salt,
            "hash": hashlib.pbkdf2_hmac("sha256", p1.encode(), bytes.fromhex(salt), 100_000).hex()}
F.write_text(json.dumps(users, indent=2), encoding="utf-8")
print(f"Contraseña de '{r}' actualizada. Vale desde el próximo login (no hay que reiniciar).")
