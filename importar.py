# -*- coding: utf-8 -*-
"""
Migracion inicial (una sola vez): trae a la base nueva
  - las tablas de molde de reglas.yaml
  - las fichas historicas ya extraidas en una base legada (cartuchos.db)

NOTA (portfolio): la base legada tiene datos reales y NO se incluye en este
repo. Para probar el sistema, usar seed_demo.py, que arma datos/sistema_of.db
a partir de sample_data/ (productos y ordenes ficticios). Este archivo queda
como referencia de la logica de migracion.

Reglas de importacion (las acordadas):

  1. La IDENTIDAD sale del nombre de la carpeta (SPF-XXXX-A-CLIENTE-SPYYYY),
     no del PDF. El codigo que traia la base legada se usa solo para cruzar.
  2. Si el codigo del nombre y el del PDF no coinciden, se importa igual pero
     queda marcado para revisar (validacion 'codigo_no_coincide').
  3. Nunca se descarta un dato. Lo que no se entiende se guarda crudo y se
     marca para revisar.

Se puede correr las veces que haga falta: reemplaza lo importado y no toca
lo que se haya cargado a mano despues.

    python importar.py
"""

import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import reglas as R

AQUI = Path(__file__).parent
# Base legada de la que se migraron las fichas historicas la primera vez.
# En el repo de portfolio no se incluye (datos reales): para la demo, usar
# seed_demo.py, que arma la base a partir de sample_data/.
ORIGEN = Path(os.environ.get("CARTUCHOS_DB", AQUI / "cartuchos.db"))
DESTINO = AQUI / "datos" / "sistema_of.db"
ESQUEMA = AQUI / "esquema.sql"
ORIGEN_TAG = "import:cartuchos.db"

AHORA = datetime.now().isoformat(timespec="seconds")


# =============================================================================
#  Nombre de carpeta -> identidad
# =============================================================================

RX_NOMBRE = re.compile(
    r"^\s*(?P<lote>C?SPF-\d+[A-Za-z]?)"      # SPF-1038 / SPF-890R / CSPF-036
    r"(?:-(?P<nro>\d+))?"                    # -7   (numero de linea, opcional)
    r"(?:-(?P<resto>.*))?$",                 # CLIENTE-SP2022
    re.I)

# Un codigo de producto: 1 a 4 letras + digito + lo que siga (SP1024, HC1151,
# TH4-PM100 SAE, SP2706A, MM1112-400)
RX_CODIGO = re.compile(r"\b([A-Za-z]{1,4}[\s\-]?\d[\w.\-]*)", re.I)

# Ruido conocido al final de algunos nombres
RX_RUIDO = re.compile(r"[\s\-]*(para imprimir|mec remache|restante|v\d+)\s*$", re.I)


def _compacto(s):
    """'FC-FHH-S-17' -> 'fcfhhs17'. Para comparar codigos escritos distinto."""
    return re.sub(r"[^0-9a-z]", "", R.normalizar(s))


def _buscar_compacto(texto, aguja):
    """
    Busca 'aguja' dentro de 'texto' ignorando guiones, espacios y acentos, y
    devuelve el trozo ORIGINAL de 'texto' que le corresponde (o None).
    Asi 'SP2481-D' encuentra 'SP2481D' y se recorta como esta en el nombre.
    """
    posiciones = []
    compacto = []
    for i, c in enumerate(texto):
        cc = _compacto(c)
        if cc:
            compacto.append(cc)
            posiciones.append(i)
    compacto = "".join(compacto)
    a = _compacto(aguja)
    if not a:
        return None
    j = compacto.find(a)
    if j < 0:
        return None
    return texto[posiciones[j]:posiciones[j + len(a) - 1] + 1]


def partir_nombre(nombre, codigo_pdf=None):
    """
    Devuelve (lote, nro_linea, cliente, codigo, notas).
    'codigo_pdf' es lo que traia cartuchos.db: se usa como pista y como cruce.
    """
    notas = []
    m = RX_NOMBRE.match((nombre or "").strip())
    if not m:
        return None, None, None, None, ["No se pudo interpretar el nombre"]

    lote = m.group("lote").upper()
    nro = int(m.group("nro")) if m.group("nro") else None
    resto = (m.group("resto") or "").strip()
    resto = RX_RUIDO.sub("", resto).strip(" -")

    if not resto:
        return lote, nro, None, None, ["El nombre no tiene cliente ni codigo"]

    codigo = None

    # 1) Si el codigo que traia el PDF aparece en el nombre (aunque este escrito
    #    con otros guiones o espacios), se toma como esta escrito en el nombre.
    if codigo_pdf:
        codigo = _buscar_compacto(resto, codigo_pdf)
        if codigo:
            codigo = codigo.strip()

    # 2) Si no, el ultimo token del nombre que tenga forma de codigo.
    if not codigo:
        candidatos = [c.strip() for c in RX_CODIGO.findall(resto)]
        if candidatos:
            codigo = candidatos[-1]

    if codigo:
        pos = resto.rfind(codigo)
        cliente = resto[:pos].strip(" -") or None
    else:
        cliente = resto
        notas.append("No se encontro codigo de producto en el nombre")

    # 3) Cruce con lo que decia el PDF  ->  validacion codigo_no_coincide
    #    Solo es un problema si son codigos REALMENTE distintos, no si es el
    #    mismo escrito con otro guion.
    if codigo and codigo_pdf and _compacto(codigo) != _compacto(codigo_pdf):
        notas.append(f"El codigo del plano decia '{codigo_pdf}' y el de la carpeta "
                     f"dice '{codigo}'. Se tomo el de la carpeta.")

    return lote, nro, (cliente or None), (codigo.upper() if codigo else None), notas


# =============================================================================
#  Texto de nucleo / jaula -> campos estructurados
# =============================================================================

RX_PERFORACION = re.compile(r"Ø\s*(\d+(?:[.,]\d+)?)", re.I)
RX_ESPESOR = re.compile(r"esp(?:esor)?\.?\s*:?\s*(\d+(?:[.,]\d+)?)", re.I)
RX_KGM2 = re.compile(r"(\d+(?:[.,]\d+)?)\s*kg\s*/?\s*m\s*2", re.I)
RX_MOLDE = re.compile(r"molde\s*Ø?\s*(\d+(?:[.,]\d+)?)", re.I)


def partir_estructura(crudo, diam_crudo, reg):
    """
    'AISI 304 | Ø5 Esp 1,5 mm'  ->  material, tipo_chapa, perforacion, espesor...
    Devuelve un dict. Lo que no se reconoce queda en None; el texto crudo
    siempre se conserva aparte.
    """
    d = {"material": None, "conformado": None, "tipo_chapa": None,
         "perforacion": None, "espesor": None, "kg_m2": None,
         "molde": None, "diam": R.numero(diam_crudo)}

    if not crudo or str(crudo).strip() in ("-", ""):
        return d

    texto = str(crudo)
    izq = texto.split("|")[0].strip()
    d["material"] = izq or None

    d["conformado"] = reg.canonico("conformado", texto)
    d["tipo_chapa"] = reg.canonico("tipo_chapa", texto)

    m = RX_PERFORACION.search(texto)
    if m:
        d["perforacion"] = float(m.group(1).replace(",", "."))
    m = RX_ESPESOR.search(texto)
    if m:
        d["espesor"] = float(m.group(1).replace(",", "."))
    m = RX_KGM2.search(texto)
    if m:
        d["kg_m2"] = float(m.group(1).replace(",", "."))
    m = RX_MOLDE.search(texto)
    if m:
        d["molde"] = float(m.group(1).replace(",", "."))

    # Si dice kg/m2 es metal desplegado aunque no lo diga con todas las letras
    if d["kg_m2"] is not None and not d["tipo_chapa"]:
        d["tipo_chapa"] = "desplegado"
    # Si tiene Ø de perforacion y no dice otra cosa, es perforado
    if d["perforacion"] is not None and not d["tipo_chapa"]:
        d["tipo_chapa"] = "perforado"

    # Toda la chapa espiralada es de 0,5 mm: si el espesor es mayor, no puede
    # ser espiralada, es rolada. (Inferencia; en el plano normalizado va a
    # venir dicho y esto deja de hacer falta.)
    esp_esp = reg.parametros.get("espesor_espiralado_mm")
    if d["conformado"] is None and d["espesor"] and esp_esp and d["espesor"] > esp_esp:
        d["conformado"] = "rolado"

    if d["conformado"] == "espiralado" and d["molde"] is None:
        d["molde"] = d["diam"]

    return d


# =============================================================================
#  Importacion
# =============================================================================

def crear_base(con):
    con.executescript(ESQUEMA.read_text(encoding="utf-8"))
    con.commit()


def importar_moldes(con, reg):
    con.execute("DELETE FROM molde")
    n = 0
    for tipo in ("perforado", "ranurado", "desplegado"):
        for m in reg.moldes(tipo):
            con.execute("""
                INSERT OR REPLACE INTO molde
                    (tipo, diam_molde, diam_externo, diam_interno, espesor,
                     peso_galvanizada, peso_hojalata)
                VALUES (?,?,?,?,?,?,?)""",
                (m["tipo"], m.get("diam_molde"), m.get("diam_externo"),
                 m.get("diam_interno"), m.get("espesor"),
                 m.get("peso_galvanizada"), m.get("peso_hojalata")))
            n += 1
    con.commit()
    return n


def importar_cartuchos(con, reg):
    if not ORIGEN.exists():
        print(f"  !! No se encuentra {ORIGEN}")
        return {}

    ori = sqlite3.connect(f"file:{ORIGEN}?mode=ro", uri=True)
    ori.row_factory = sqlite3.Row
    filas = ori.execute("SELECT * FROM cartuchos").fetchall()

    cuenta = {"productos": 0, "lotes": 0, "lineas": 0, "planos": 0,
              "revisar": 0, "sin_codigo": 0, "mismatch": 0}
    clientes_por_producto = {}

    for f in filas:
        lote, nro, cliente, codigo, notas = partir_nombre(f["archivo"], f["sp"])

        if any("no coincide" in n or "decia" in n for n in notas):
            cuenta["mismatch"] += 1

        if not codigo:
            # Sin codigo no hay ficha de producto posible: se guarda la linea
            # igual, marcada, para no perder el dato.
            cuenta["sin_codigo"] += 1
            codigo = None

        # --- LOTE ------------------------------------------------------------
        if lote:
            cli = cliente or (f["cliente"] or None)
            existe = con.execute("SELECT 1 FROM lote WHERE codigo=?", (lote,)).fetchone()
            if not existe:
                con.execute("INSERT INTO lote (codigo, cliente, creado) VALUES (?,?,?)",
                            (lote, cli, AHORA))
                cuenta["lotes"] += 1
            elif cli:
                con.execute("UPDATE lote SET cliente=COALESCE(cliente,?) WHERE codigo=?",
                            (cli, lote))

        # --- PRODUCTO --------------------------------------------------------
        if codigo:
            nuc = partir_estructura(f["nucleo"], f["nucleo_id"], reg)
            jau = partir_estructura(f["jaula"], f["jaula_id"], reg)
            nuci = partir_estructura(f["nucleo_intermedio"],
                                     f["nucleo_intermedio_id"], reg)

            tipo_medio = None
            if (f["plisado"] or "").strip() not in ("", "-"):
                tipo_medio = "plisado"
            elif (f["rolado"] or "").strip() not in ("", "-"):
                tipo_medio = "rolado"

            ya = con.execute("SELECT origen FROM producto WHERE codigo=?",
                             (codigo,)).fetchone()
            # No pisar fichas cargadas o corregidas a mano
            if ya and ya[0] != ORIGEN_TAG:
                pass
            else:
                notas_prod = [n for n in notas if n]
                con.execute("""
                    INSERT OR REPLACE INTO producto (
                        codigo, familia, altura, od, id_int,
                        medio, soporte, tipo_medio, q, hp, vueltas, q_crudo, hp_crudo,
                        nucleo_crudo, nucleo_material, nucleo_conformado, nucleo_tipo_chapa,
                        nucleo_perforacion, nucleo_espesor, nucleo_kg_m2, nucleo_molde,
                        nucleo_diam, nucleo_diam_crudo,
                        nucleo_int_crudo, nucleo_int_diam,
                        jaula_crudo, jaula_material, jaula_conformado, jaula_tipo_chapa,
                        jaula_perforacion, jaula_espesor, jaula_kg_m2, jaula_molde,
                        jaula_diam, jaula_diam_crudo,
                        origen, revisar, nota_revision, reglas_version, creado, actualizado
                    ) VALUES (?,?,?,?,?, ?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?,?, ?,?,
                              ?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?)""",
                    (codigo, "cartucho", f["altura"], f["od"], f["id_interior"],
                     f["plisado"] or f["rolado"], f["soporte_plisado"], tipo_medio,
                     int(R.numero(f["cant_pliegues"]) or 0) or None,
                     R.numero(f["altura_pliegue"]), R.numero(f["cant_vueltas"]),
                     f["cant_pliegues"], f["altura_pliegue"],
                     f["nucleo"], nuc["material"], nuc["conformado"], nuc["tipo_chapa"],
                     nuc["perforacion"], nuc["espesor"], nuc["kg_m2"], nuc["molde"],
                     nuc["diam"], f["nucleo_id"],
                     f["nucleo_intermedio"], nuci["diam"],
                     f["jaula"], jau["material"], jau["conformado"], jau["tipo_chapa"],
                     jau["perforacion"], jau["espesor"], jau["kg_m2"], jau["molde"],
                     jau["diam"], f["jaula_id"],
                     ORIGEN_TAG, 1 if notas_prod else 0,
                     " / ".join(notas_prod) or None,
                     reg.version, AHORA, AHORA))
                cuenta["productos"] += 1
                if notas_prod:
                    cuenta["revisar"] += 1

            if cliente:
                clientes_por_producto.setdefault(codigo, set()).add(cliente)

        # --- LINEA -----------------------------------------------------------
        if lote:
            con.execute("""
                INSERT OR IGNORE INTO linea
                    (lote_codigo, nro, producto_codigo, estado, nombre_origen,
                     ruta_origen, revisar, nota_revision, creado, actualizado)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (lote, nro, codigo, "finalizada", f["archivo"], f["ruta_carpeta"],
                 1 if notas else 0, " / ".join(notas) or None, AHORA, AHORA))
            if con.total_changes:
                cuenta["lineas"] += 1

        # --- PLANO -----------------------------------------------------------
        if codigo and f["ruta_carpeta"]:
            con.execute("""
                INSERT INTO plano (producto_codigo, nombre, ruta, creado)
                VALUES (?,?,?,?)""",
                (codigo, f["archivo"], f["ruta_carpeta"], AHORA))
            cuenta["planos"] += 1

    ori.close()
    con.commit()

    # --- variantes: SP1398H -> SP1398 ---------------------------------------
    todos = {r[0] for r in con.execute("SELECT codigo FROM producto")}
    for cod in todos:
        m = re.match(r"^([A-Z]+\d+)[\s\-]?([A-Z]\w*|V\d+)$", cod, re.I)
        if m and m.group(1).upper() in todos and m.group(1).upper() != cod:
            con.execute("UPDATE producto SET variante_de=? WHERE codigo=?",
                        (m.group(1).upper(), cod))
    con.commit()

    # --- indice de busqueda --------------------------------------------------
    con.execute("DELETE FROM producto_fts")
    for r in con.execute("""SELECT codigo, medio, soporte, nucleo_crudo, jaula_crudo
                            FROM producto"""):
        cli = " ".join(sorted(clientes_por_producto.get(r[0], [])))
        con.execute("""INSERT INTO producto_fts
                       (codigo, medio, soporte, nucleo_crudo, jaula_crudo, clientes)
                       VALUES (?,?,?,?,?,?)""",
                    (r[0], r[1] or "", r[2] or "", r[3] or "", r[4] or "", cli))
    con.commit()
    return cuenta


def main():
    print("=" * 70)
    reg = R.cargar()
    print(f"Reglas version {reg.version} (vigente desde {reg.vigente_desde})")

    DESTINO.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DESTINO)
    con.execute("PRAGMA foreign_keys=ON")

    print(f"Base:    {DESTINO}")
    crear_base(con)

    n = importar_moldes(con, reg)
    print(f"\nMoldes importados: {n}")
    for t in ("perforado", "ranurado", "desplegado"):
        c = con.execute("SELECT COUNT(*) FROM molde WHERE tipo=?", (t,)).fetchone()[0]
        print(f"   {t:12} {c}")

    print(f"\nImportando {ORIGEN.name} ...")
    c = importar_cartuchos(con, reg)

    # Contar sobre la base, no sobre los INSERT: un mismo codigo puede venir
    # en varias filas del origen y se guarda una sola ficha.
    def cuantos(sql):
        return con.execute(sql).fetchone()[0]

    print(f"   productos       {cuantos('SELECT COUNT(*) FROM producto')}")
    print(f"   lotes           {cuantos('SELECT COUNT(*) FROM lote')}")
    print(f"   lineas          {cuantos('SELECT COUNT(*) FROM linea')}")
    print(f"   planos          {cuantos('SELECT COUNT(*) FROM plano')}")
    print(f"   --")
    print(f"   marcados para revisar        "
          f"{cuantos('SELECT COUNT(*) FROM producto WHERE revisar=1')}")
    print(f"   codigo del plano != carpeta  {c.get('mismatch', 0)}")
    print(f"   sin codigo en el nombre      {c.get('sin_codigo', 0)}")

    con.close()
    print("\nListo. Ahora: INICIAR.bat")
    print("=" * 70)


if __name__ == "__main__":
    sys.exit(main())
