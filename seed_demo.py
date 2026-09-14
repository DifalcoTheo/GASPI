# -*- coding: utf-8 -*-
"""
Demo seed for GASPI (sanitized portfolio version).

Builds datos/sistema_of.db from the fictional data in sample_data/ so the
system can be explored end to end without any real drawings, database or
mailbox. It runs the REAL schema and the REAL rules engine (reglas.yaml):
the molde tables, the calculated developments and the validations are all
produced by the same code the production system uses.

    python seed_demo.py      # builds datos/sistema_of.db
    python app.py            # http://127.0.0.1:8000

Login (generated on first run): ingenieria / produccion / compras  —  password: changeme
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import reglas as R
from importar import importar_moldes

AQUI = Path(__file__).parent
DATOS = AQUI / "datos"
BASE = DATOS / "sistema_of.db"
ESQUEMA = AQUI / "esquema.sql"
MUESTRA = AQUI / "sample_data"
EN_PROCESO = DATOS / "en_proceso"
AHORA = datetime.now().isoformat(timespec="seconds")


def como_dict(fila):
    return {k: fila[k] for k in fila.keys()}


def crear_base(con):
    con.executescript(ESQUEMA.read_text(encoding="utf-8"))
    con.commit()


def cargar_productos(con, reg):
    columnas = {c[1] for c in con.execute("PRAGMA table_info(producto)")}
    productos = json.loads((MUESTRA / "productos.json").read_text(encoding="utf-8"))
    for p in productos:
        p = {k: v for k, v in p.items() if k in columnas}
        p.update({"origen": "demo", "revisar": 0, "reglas_version": reg.version,
                  "creado": AHORA, "actualizado": AHORA})
        p.setdefault("familia", "cartucho")
        con.execute(
            f"INSERT INTO producto ({','.join(p)}) VALUES ({','.join('?' * len(p))})",
            list(p.values()))
    con.commit()
    return len(productos)


def cargar_ordenes(con, reg):
    ordenes = json.loads((MUESTRA / "ordenes.json").read_text(encoding="utf-8"))
    n_lineas = 0
    for o in ordenes:
        con.execute("""INSERT INTO lote
            (codigo, cliente, fecha_oc, fecha_entrega, vendedor, creado)
            VALUES (?,?,?,?,?,?)""",
            (o["lote"], o.get("cliente"), o.get("fecha_oc"),
             o.get("fecha_entrega"), o.get("vendedor"), AHORA))
        for i, ln in enumerate(o["lineas"], start=1):
            codigo = ln["codigo"]
            # la linea CONGELA la ficha del producto tal como esta al lanzarla
            fila = con.execute("SELECT * FROM producto WHERE codigo=?", (codigo,)).fetchone()
            congelado = como_dict(fila) if fila else {"codigo": codigo}
            con.execute("""INSERT INTO linea
                (lote_codigo, nro, producto_codigo, cantidad, estado, datos_json,
                 reglas_version, creado, actualizado)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (o["lote"], i, codigo, ln.get("cantidad"),
                 ln.get("estado", "pendiente"),
                 json.dumps(congelado, ensure_ascii=False), reg.version,
                 AHORA, AHORA))
            n_lineas += 1
    con.commit()
    return len(ordenes), n_lineas


def indexar_busqueda(con):
    con.execute("DELETE FROM producto_fts")
    # clientes por producto (de los lotes en los que se fabrico)
    clientes = {}
    for r in con.execute("""SELECT l.producto_codigo, lo.cliente
                            FROM linea l JOIN lote lo ON lo.codigo = l.lote_codigo
                            WHERE lo.cliente IS NOT NULL"""):
        clientes.setdefault(r[0], set()).add(r[1])
    for r in con.execute("""SELECT codigo, medio, soporte, nucleo_crudo, jaula_crudo
                            FROM producto"""):
        cli = " ".join(sorted(clientes.get(r[0], [])))
        con.execute("""INSERT INTO producto_fts
                       (codigo, medio, soporte, nucleo_crudo, jaula_crudo, clientes)
                       VALUES (?,?,?,?,?,?)""",
                    (r[0], r[1] or "", r[2] or "", r[3] or "", r[4] or "", cli))
    con.commit()


def crear_en_proceso():
    """OF derivadas desde Ariana, esperando el plano de Ingenieria."""
    items = json.loads((MUESTRA / "en_proceso.json").read_text(encoding="utf-8"))
    for it in items:
        carpeta = EN_PROCESO / f"{it['spf']}-{it['cliente'].split()[0]}"
        carpeta.mkdir(parents=True, exist_ok=True)
        (carpeta / "datos.json").write_text(
            json.dumps({**it, "creado": AHORA}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    return len(items)


def main():
    print("=" * 64)
    print("  GASPI — demo seed")
    print("=" * 64)
    reg = R.cargar()
    print(f"Reglas version {reg.version} (vigente desde {reg.vigente_desde})")

    DATOS.mkdir(parents=True, exist_ok=True)
    if BASE.exists():
        BASE.unlink()
    con = sqlite3.connect(BASE)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    crear_base(con)

    n_moldes = importar_moldes(con, reg)
    n_prod = cargar_productos(con, reg)
    n_lotes, n_lineas = cargar_ordenes(con, reg)
    indexar_busqueda(con)
    con.close()
    n_proc = crear_en_proceso()

    print(f"  moldes            {n_moldes}")
    print(f"  productos         {n_prod}")
    print(f"  lotes             {n_lotes}")
    print(f"  lineas            {n_lineas}")
    print(f"  OF en proceso     {n_proc}")
    print("-" * 64)
    print(f"  Base: {BASE}")
    print("  Ahora:  python app.py    ->  http://127.0.0.1:8000")
    print("  Login: ingenieria / produccion / compras   —   clave: changeme")
    print("=" * 64)


if __name__ == "__main__":
    main()
