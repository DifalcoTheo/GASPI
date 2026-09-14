# -*- coding: utf-8 -*-
"""
Sistema de Ordenes de Fabricacion - servidor.

Fase 1: buscador de productos, ficha tecnica con los calculos aplicados,
tablas de molde y pantalla de reglas vigentes.

    INICIAR.bat        (o:  python app.py)

Queda escuchando en toda la red local: desde otra PC se entra con
http://<ip-de-esta-pc>:8000
"""

import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pymupdf
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import lector
import reglas as R

AQUI = Path(__file__).parent
BASE = AQUI / "datos" / "sistema_of.db"
PLANOS = AQUI / "datos" / "planos"
WEB = AQUI / "web"
EN_PROCESO = AQUI / "datos" / "en_proceso"   # OF derivadas desde Ariana (SPF-XXXX-CLIENTE/)


def _migrar():
    """Columnas agregadas después de la Fase 1 (idempotente)."""
    try:
        con = sqlite3.connect(BASE)
        cols = [r[1] for r in con.execute("PRAGMA table_info(linea)")]
        if "etiqueta" not in cols:
            con.execute("ALTER TABLE linea ADD COLUMN etiqueta TEXT")
            con.commit()
        con.close()
    except sqlite3.Error:
        pass


_migrar()

app = FastAPI(title="Ordenes de Fabricacion")

# ───────────────────── Login (3 roles con contraseña) ─────────────────────
SECRET_FILE = AQUI / "datos" / "secret.key"
USUARIOS_GASPI = AQUI / "datos" / "usuarios_gaspi.json"
ROLES = ("ingenieria", "produccion", "compras")
_DEF_CLAVES = {"ingenieria": "changeme", "produccion": "changeme", "compras": "changeme"}


def _secret():
    if not SECRET_FILE.exists():
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_bytes(os.urandom(32))
    return SECRET_FILE.read_bytes()


def _hash_clave(clave, salt_hex):
    return hashlib.pbkdf2_hmac("sha256", clave.encode(), bytes.fromhex(salt_hex), 100_000).hex()


def _usuarios():
    if not USUARIOS_GASPI.exists():
        data = {}
        for r, p in _DEF_CLAVES.items():
            salt = os.urandom(16).hex()
            data[r] = {"salt": salt, "hash": _hash_clave(p, salt)}
        USUARIOS_GASPI.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return json.loads(USUARIOS_GASPI.read_text(encoding="utf-8"))


def _firmar(rol):
    return rol + "." + hmac.new(_secret(), rol.encode(), hashlib.sha256).hexdigest()


def _rol_de_token(token):
    try:
        rol, firma = token.split(".", 1)
    except (ValueError, AttributeError):
        return None
    esperada = hmac.new(_secret(), rol.encode(), hashlib.sha256).hexdigest()
    return rol if rol in ROLES and hmac.compare_digest(firma, esperada) else None


def _es_ariana(request):
    """Ariana (el proxy) firma sus pedidos con el mismo secret (comparten disco)."""
    tok = request.headers.get("X-Gaspi-Auth", "")
    esperada = hmac.new(_secret(), b"ariana", hashlib.sha256).hexdigest()
    return bool(tok) and hmac.compare_digest(tok, esperada)


class Credenciales(BaseModel):
    rol: str
    clave: str


@app.post("/api/login")
def gaspi_login(cred: Credenciales):
    u = _usuarios().get(cred.rol)
    if not u or not hmac.compare_digest(u["hash"], _hash_clave(cred.clave, u["salt"])):
        raise HTTPException(401, "Rol o contraseña incorrectos")
    resp = JSONResponse({"ok": True, "rol": cred.rol})
    resp.set_cookie("gaspi_sesion", _firmar(cred.rol), max_age=60 * 60 * 12,
                    httponly=True, samesite="lax", path="/")
    return resp


@app.post("/api/logout")
def gaspi_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("gaspi_sesion", path="/")
    return resp


@app.middleware("http")
async def gaspi_auth(request, call_next):
    p = request.url.path
    if p.startswith("/api/") and p != "/api/login":
        rol = _rol_de_token(request.cookies.get("gaspi_sesion", ""))
        if not rol and not _es_ariana(request):
            return JSONResponse({"detail": "no autenticado"}, status_code=401)
    return await call_next(request)


def conexion():
    if not BASE.exists():
        raise HTTPException(500, "No existe la base. Correr primero: python importar.py")
    con = sqlite3.connect(f"file:{BASE}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def conexion_escritura():
    con = sqlite3.connect(BASE)
    con.row_factory = sqlite3.Row
    return con


def como_dict(fila):
    return {k: fila[k] for k in fila.keys()}


# =============================================================================
#  Ficha calculada
# =============================================================================

def pieza_de(p, prefijo):
    """Arma el dict de nucleo o jaula a partir de la fila del producto."""
    return {
        "crudo": p[f"{prefijo}_crudo"],
        "material": p[f"{prefijo}_material"],
        "conformado": p[f"{prefijo}_conformado"],
        "tipo_chapa": p[f"{prefijo}_tipo_chapa"],
        "perforacion": p[f"{prefijo}_perforacion"],
        "espesor": p[f"{prefijo}_espesor"],
        "kg_m2": p[f"{prefijo}_kg_m2"],
        "molde": p[f"{prefijo}_molde"],
        "diam": p[f"{prefijo}_diam"],
        "diam_crudo": p[f"{prefijo}_diam_crudo"],
    }


def calcular(p, reg):
    """Todo lo que el sistema deduce del producto. Siempre con su explicacion."""
    d = como_dict(p)
    calc, avisos = calcular_dict(d, reg)
    if d.get("revisar"):
        avisos.append({"id": "importado_con_dudas", "nivel": "avisa",
                       "mensaje": d.get("nota_revision") or "Revisar la ficha"})
    return calc, avisos


# =============================================================================
#  API
# =============================================================================

@app.get("/api/resumen")
def resumen():
    con = conexion()
    r = {
        "productos": con.execute("SELECT COUNT(*) FROM producto").fetchone()[0],
        "lotes": con.execute("SELECT COUNT(*) FROM lote").fetchone()[0],
        "lineas": con.execute("SELECT COUNT(*) FROM linea").fetchone()[0],
        "revisar": con.execute("SELECT COUNT(*) FROM producto WHERE revisar=1").fetchone()[0],
        "clientes": con.execute("SELECT COUNT(DISTINCT cliente) FROM lote WHERE cliente IS NOT NULL").fetchone()[0],
        "moldes": con.execute("SELECT COUNT(*) FROM molde").fetchone()[0],
    }
    reg = R.cargar()
    r["reglas_version"] = reg.version
    r["reglas_desde"] = str(reg.vigente_desde)
    con.close()
    return r


@app.get("/api/buscar")
def buscar(q: str = "", solo_revisar: int = 0, limite: int = 60):
    """Busca por codigo, cliente, medio filtrante, material de nucleo o jaula."""
    con = conexion()
    q = (q or "").strip()

    # Los clientes salen de los lotes en los que se fabrico el producto.
    CLIENTES = """(SELECT GROUP_CONCAT(DISTINCT lo.cliente)
                   FROM linea l JOIN lote lo ON lo.codigo = l.lote_codigo
                   WHERE l.producto_codigo = p.codigo) AS clientes"""

    if q:
        # FTS con prefijos: "boro" encuentra "borosilicato"
        termino = " ".join(f'"{t}"*' for t in q.replace('"', " ").split() if t)
        try:
            filas = con.execute(
                f"""SELECT p.*, {CLIENTES}
                    FROM producto_fts f JOIN producto p ON p.codigo = f.codigo
                    WHERE producto_fts MATCH ? """
                + ("AND p.revisar=1 " if solo_revisar else "")
                + "LIMIT ?", (termino, limite)).fetchall()
        except sqlite3.OperationalError:
            filas = []
        if not filas:
            # si el FTS no encuentra nada, buscar por codigo parecido
            filas = con.execute(
                f"""SELECT p.*, {CLIENTES} FROM producto p
                    WHERE p.codigo LIKE ? LIMIT ?""", (f"%{q}%", limite)).fetchall()
    else:
        filas = con.execute(
            f"""SELECT p.*, {CLIENTES} FROM producto p """
            + ("WHERE p.revisar=1 " if solo_revisar else "")
            + "ORDER BY p.codigo LIMIT ?", (limite,)).fetchall()

    salida = []
    for p in filas:
        clientes = sorted(set(filter(None, (p["clientes"] or "").split(","))))
        salida.append({
            "codigo": p["codigo"], "altura": p["altura"], "od": p["od"],
            "id_int": p["id_int"], "medio": p["medio"],
            "tipo_medio": p["tipo_medio"], "revisar": p["revisar"],
            "variante_de": p["variante_de"],
            "clientes": ", ".join(clientes),
        })
    con.close()
    return {"total": len(salida), "resultados": salida}


@app.get("/api/producto/{codigo}")
def producto(codigo: str):
    con = conexion()
    p = con.execute("SELECT * FROM producto WHERE codigo=?", (codigo,)).fetchone()
    if not p:
        con.close()
        raise HTTPException(404, "No existe ese codigo")

    reg = R.cargar()
    calc, avisos = calcular(p, reg)

    historial = con.execute("""
        SELECT l.lote_codigo, l.nro, l.estado, l.cantidad, l.nombre_origen,
               lo.cliente
        FROM linea l LEFT JOIN lote lo ON lo.codigo = l.lote_codigo
        WHERE l.producto_codigo = ?
        ORDER BY l.lote_codigo DESC""", (codigo,)).fetchall()

    planos = con.execute(
        "SELECT nombre, ruta FROM plano WHERE producto_codigo=? LIMIT 10",
        (codigo,)).fetchall()

    variantes = con.execute(
        """SELECT codigo FROM producto
           WHERE variante_de=? OR codigo=(SELECT variante_de FROM producto WHERE codigo=?)""",
        (codigo, codigo)).fetchall()

    con.close()
    return {
        "producto": como_dict(p),
        "nucleo": pieza_de(p, "nucleo"),
        "jaula": pieza_de(p, "jaula"),
        "calculado": calc,
        "avisos": avisos,
        "tolerancias": reg.tolerancias,
        "criterios": reg.criterios,
        "historial": [como_dict(h) for h in historial],
        "planos": [como_dict(x) for x in planos],
        "variantes": [v[0] for v in variantes],
    }


@app.get("/api/moldes")
def moldes():
    con = conexion()
    filas = con.execute(
        "SELECT * FROM molde ORDER BY tipo, diam_molde, diam_interno").fetchall()
    con.close()
    return [como_dict(f) for f in filas]


@app.get("/api/reglas")
def ver_reglas():
    """La pantalla 'Reglas vigentes': sale del mismo archivo que se ejecuta."""
    reg = R.cargar(recargar=True)
    return {
        "version": reg.version,
        "vigente_desde": str(reg.vigente_desde),
        "archivo": str(reg.ruta),
        "parametros": reg.parametros,
        "formulas": reg.formulas,
        "validaciones": reg.validaciones,
        "tolerancias": reg.tolerancias,
        "criterios": reg.criterios,
        "lectura_plano": reg.lectura_plano,
    }


# =============================================================================
#  Lectura de planos
# =============================================================================

def nombre_seguro(nombre):
    return re.sub(r"[^\w.\- ]", "_", nombre or "plano.pdf")[:120]


@app.post("/api/leer")
async def leer_plano(archivo: UploadFile = File(...)):
    """Recibe un PDF, lo guarda y devuelve todo lo que pudo interpretar."""
    if not (archivo.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Solo se pueden leer archivos PDF")

    PLANOS.mkdir(parents=True, exist_ok=True)
    nombre = nombre_seguro(archivo.filename)
    destino = PLANOS / nombre
    destino.write_bytes(await archivo.read())

    try:
        lectura = lector.leer(destino)
    except Exception as e:
        raise HTTPException(400, f"No se pudo leer el PDF: {e}")

    lectura["archivo"] = nombre
    lectura["propuesta"] = lector.a_producto(lectura)

    # Si el codigo ya existe, se compara contra la ficha guardada
    codigo = (lectura["campos"].get("codigo") or {}).get("valor")
    lectura["ya_existe"] = None
    if codigo:
        con = conexion()
        fila = con.execute("SELECT * FROM producto WHERE codigo=?",
                           (codigo.strip().upper(),)).fetchone()
        if fila:
            guardado = como_dict(fila)
            difs = []
            for campo in ("altura", "od", "id_int", "q", "hp"):
                nuevo = lectura["propuesta"].get(campo)
                viejo = guardado.get(campo)
                if nuevo is not None and viejo is not None and abs(float(nuevo) - float(viejo)) > 0.05:
                    difs.append({"campo": campo, "guardado": viejo, "leido": nuevo})
            veces = con.execute(
                "SELECT COUNT(*) FROM linea WHERE producto_codigo=?",
                (guardado["codigo"],)).fetchone()[0]
            # La ficha guardada viaja entera: si el codigo ya se fabrico, no hay
            # que volver a confirmar todo, solo lo que difiere.
            lectura["ya_existe"] = {"codigo": guardado["codigo"],
                                    "diferencias": difs, "veces": veces,
                                    "ficha": guardado}
        con.close()

    return lectura


@app.get("/api/plano/{nombre}/imagen")
def imagen_plano(nombre: str, zoom: float = 2.0):
    """La primera pagina del plano como imagen, para mostrarla y marcarla."""
    ruta = PLANOS / nombre_seguro(nombre)
    if not ruta.exists():
        raise HTTPException(404, "No se encuentra el plano")
    doc = pymupdf.open(ruta)
    pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    png = pix.tobytes("png")
    doc.close()
    return Response(png, media_type="image/png")


@app.get("/api/plano/{nombre}/pdf")
def pdf_plano(nombre: str):
    ruta = PLANOS / nombre_seguro(nombre)
    if not ruta.exists():
        raise HTTPException(404, "No se encuentra el plano")
    return FileResponse(ruta, media_type="application/pdf")


class FichaGuardar(BaseModel):
    producto: dict
    archivo: str | None = None
    nota: str | None = None


@app.post("/api/guardar_ficha")
def guardar_ficha(datos: FichaGuardar):
    """Guarda la ficha confirmada por la persona. Lo leido nunca se guarda solo."""
    p = dict(datos.producto)
    codigo = (p.get("codigo") or "").strip().upper()
    if not codigo:
        raise HTTPException(400, "Falta el codigo del producto")
    p["codigo"] = codigo

    reg = R.cargar()
    ahora = datetime.now().isoformat(timespec="seconds")
    con = conexion_escritura()

    columnas = {c[1] for c in con.execute("PRAGMA table_info(producto)")}
    p = {k: v for k, v in p.items() if k in columnas}
    p.update({"origen": "plano", "revisar": 0, "reglas_version": reg.version,
              "actualizado": ahora, "familia": p.get("familia") or "cartucho"})

    anterior = con.execute("SELECT * FROM producto WHERE codigo=?", (codigo,)).fetchone()
    if anterior:
        sets = ", ".join(f"{k}=?" for k in p if k != "codigo")
        con.execute(f"UPDATE producto SET {sets} WHERE codigo=?",
                    [v for k, v in p.items() if k != "codigo"] + [codigo])
        for k, v in p.items():
            viejo = anterior[k] if k in anterior.keys() else None
            if str(viejo or "") != str(v or ""):
                con.execute("""INSERT INTO historial
                    (fecha, entidad, entidad_id, campo, valor_anterior, valor_nuevo, nota)
                    VALUES (?,?,?,?,?,?,?)""",
                    (ahora, "producto", codigo, k, str(viejo), str(v),
                     datos.nota or "Confirmado desde el plano"))
    else:
        p["creado"] = ahora
        con.execute(f"INSERT INTO producto ({','.join(p)}) VALUES ({','.join('?' * len(p))})",
                    list(p.values()))
        con.execute("""INSERT INTO historial (fecha, entidad, entidad_id, nota)
                       VALUES (?,?,?,?)""",
                    (ahora, "producto", codigo, datos.nota or "Creado desde el plano"))

    if datos.archivo:
        con.execute("DELETE FROM plano WHERE producto_codigo=? AND nombre=?",
                    (codigo, datos.archivo))
        con.execute("""INSERT INTO plano (producto_codigo, nombre, ruta, normalizado, creado)
                       VALUES (?,?,?,?,?)""",
                    (codigo, datos.archivo, str(PLANOS / datos.archivo), 0, ahora))

    # reindexar el buscador para este producto
    con.execute("DELETE FROM producto_fts WHERE codigo=?", (codigo,))
    f = con.execute("""SELECT codigo, medio, soporte, nucleo_crudo, jaula_crudo
                       FROM producto WHERE codigo=?""", (codigo,)).fetchone()
    con.execute("""INSERT INTO producto_fts
                   (codigo, medio, soporte, nucleo_crudo, jaula_crudo, clientes)
                   VALUES (?,?,?,?,?,'')""",
                (f[0], f[1] or "", f[2] or "", f[3] or "", f[4] or ""))
    con.commit()
    con.close()
    return {"ok": True, "codigo": codigo, "creado": anterior is None}


# =============================================================================
#  Ordenes de fabricacion
# =============================================================================

DIAS_ENTREGA = 45          # fecha de entrega por defecto = fecha de OC + 45 dias
ESTADOS = ["pendiente", "en_produccion", "pausada", "finalizada"]


def materiales_de(d):
    """Los materiales de un elemento, candidatos a compra interna."""
    out = []

    def add(clave, etiqueta, texto):
        t = (str(texto).strip() if texto is not None else "")
        if t and t != "-":
            out.append({"clave": clave, "etiqueta": etiqueta, "texto": t})

    add("medio", "Medio filtrante", d.get("medio"))
    add("medio_2", "Medio filtrante 2", d.get("medio_2"))
    add("medio_3", "Medio filtrante 3", d.get("medio_3"))
    add("nucleo", "Material del núcleo", d.get("nucleo_material") or d.get("nucleo_crudo"))
    add("nucleo_int", "Material del núcleo interm.",
        d.get("nucleo_int_material") or d.get("nucleo_int_crudo"))
    add("jaula", "Material de la jaula", d.get("jaula_material") or d.get("jaula_crudo"))
    add("tapa", "Tapa", d.get("tapa"))
    add("tapa_2", "Tapa 2", d.get("tapa_2"))
    add("adhesivo", "Adhesivo", d.get("adhesivo"))
    add("sello", "Sello / junta", d.get("sello"))
    add("sello_2", "Sello / junta 2", d.get("sello_2"))
    return out


def _resumen_linea(fila, reg):
    """Los datos congelados de una linea, con sus calculos."""
    d = json.loads(fila["datos_json"]) if fila["datos_json"] else {}
    calc, avisos = calcular_dict(d, reg)
    return d, calc, avisos


RX_MOLDE_TXT = re.compile(r"molde\s*[ØO]?\s*(\d+(?:[.,]\d+)?)", re.I)


def molde_de_texto(texto):
    """Saca el numero de molde del texto crudo ('...Molde Ø105' -> 105.0).
    Para los espiralados viejos que tienen el molde en el texto pero no en el campo."""
    if not texto:
        return None
    m = RX_MOLDE_TXT.search(str(texto))
    return float(m.group(1).replace(",", ".")) if m else None


def calcular_dict(d, reg):
    """Igual que calcular(), pero sobre un dict en vez de una fila."""
    calc = {}
    # Desarrollo de cada medio presente, segun su propia forma (plisado/rolado).
    # El rolado se calcula sobre el diametro de la estructura elegida:
    #   - estructura rolada:     Ø interno + sobreespesor
    #   - estructura espiralada: Ø externo del molde (tabla)
    calc["medio"] = {"valor": None, "detalle": "Sin medio"}
    sob = reg.parametros.get("sobreespesor_medio_rolado_mm", 2.5)
    for suf in ("", "_2", "_3"):
        mk = "medio" + suf
        if not d.get(mk):
            continue
        forma = d.get("tipo_medio" + suf)
        diam_rolado = None
        if forma == "rolado":
            pref = d.get(mk + "_sobre")
            if pref and d.get(f"{pref}_conformado") == "espiralado":
                molde = d.get(f"{pref}_molde") or molde_de_texto(d.get(f"{pref}_crudo"))
                if molde is not None:
                    diam_rolado = reg.aplicar("diametro_externo_espiralado", molde=float(molde))
            elif pref:
                di = d.get(f"{pref}_diam")
                if di is not None:
                    diam_rolado = float(di) + sob
        v, expl = R.desarrollo_medio(reg, d, forma, diam_rolado)
        calc[mk] = {"valor": round(v, 1) if v else None, "detalle": expl,
                    "forma": forma, "sobre": d.get(mk + "_sobre")}

    # Altura de CADA estructura: la mas alta (principal) lleva la altura base;
    # las demas, 1 mm menos. Cual es la mas alta lo deciden las reglas de altura.
    base, alturas, ppal, expl = R.alturas_estructuras(reg, d)
    alt_est = round(base, 1) if base else None
    calc["altura_estructuras"] = {"valor": alt_est, "detalle": expl, "principal": ppal}

    for prefijo in ("nucleo", "nucleo_int", "jaula"):
        pieza = {k: d.get(f"{prefijo}_{k}") for k in
                 ("material", "conformado", "tipo_chapa", "espesor", "diam",
                  "molde", "kg_m2", "sin_molde")}
        if not d.get(f"{prefijo}_crudo"):
            calc[prefijo] = {"valor": None, "detalle": "No lleva"}
            continue
        alt_pieza = (alturas.get(prefijo) or {}).get("valor")
        alt_pieza = round(alt_pieza, 1) if alt_pieza is not None else None
        try:
            v, expl = R.desarrollo_estructura(reg, pieza)
        except R.ErrorDeRegla as e:
            v, expl = None, str(e)
        item = {"valor": round(v, 1) if v else None, "detalle": expl,
                "altura": alt_pieza,
                "principal": (alturas.get(prefijo) or {}).get("principal", False)}
        # etiqueta corta para la tabla de metalurgica: como cierra la chapa.
        # Los numeros salen de reglas.yaml (solapa y limite de espesor).
        if item["valor"]:
            tipo_chapa = pieza.get("tipo_chapa")
            esp = pieza.get("espesor")
            if tipo_chapa == "desplegado":
                item["corto"] = f"{reg.parametros['solapa_mm']:g} mm de solapa"
            elif esp is not None:
                limite = reg.parametros["espesor_limite_solapa_mm"]
                item["corto"] = (f"{reg.parametros['solapa_mm']:g} mm de solapa"
                                 if esp < limite else "a tope")
        if pieza.get("conformado") == "espiralado":
            try:
                # el material se consume por la altura de ESTA estructura
                g, det = R.consumo_espiralado(reg, pieza, alt_pieza or d.get("altura"),
                                              pieza.get("material"))
                if g:
                    item["consumo_g"] = round(g)
                    item["consumo_detalle"] = det
            except R.ErrorDeRegla:
                pass
        calc[prefijo] = item

    datos = {k: v for k, v in
             {"ID": d.get("id_int"), "OD": d.get("od"),
              "Q": d.get("q"), "Hp": d.get("hp")}.items() if v is not None}
    return calc, reg.validar(datos)


# =============================================================================
#  Editar y borrar
# =============================================================================

class DatosLinea(BaseModel):
    datos: dict
    usuario: str | None = None
    nota: str | None = None


@app.post("/api/linea/{linea_id}/datos")
def editar_linea(linea_id: int, cambio: DatosLinea):
    """
    Corrige los datos tecnicos de una linea ya lanzada.
    No toca la ficha del producto: corrige ESTA orden. Cada campo modificado
    queda en el historial.
    """
    ahora = datetime.now().isoformat(timespec="seconds")
    con = conexion_escritura()
    f = con.execute("SELECT datos_json FROM linea WHERE id=?", (linea_id,)).fetchone()
    if not f:
        con.close()
        raise HTTPException(404, "No existe esa linea")

    antes = json.loads(f["datos_json"] or "{}")
    despues = dict(antes)
    cambiados = []
    for k, v in cambio.datos.items():
        if str(antes.get(k) or "") != str(v or ""):
            cambiados.append({"campo": k, "antes": antes.get(k), "despues": v})
            despues[k] = v

    con.execute("UPDATE linea SET datos_json=?, actualizado=? WHERE id=?",
                (json.dumps(despues, ensure_ascii=False), ahora, linea_id))
    for c in cambiados:
        con.execute("""INSERT INTO historial
            (fecha, usuario, entidad, entidad_id, campo, valor_anterior, valor_nuevo, nota)
            VALUES (?,?,?,?,?,?,?,?)""",
            (ahora, cambio.usuario, "linea", str(linea_id), c["campo"],
             str(c["antes"]), str(c["despues"]),
             cambio.nota or "Corregido a mano en la orden"))
    con.commit()
    con.close()
    return {"ok": True, "cambiados": cambiados}


@app.delete("/api/of/{lote}")
def borrar_orden(lote: str):
    """Borra un lote entero con sus lineas. No toca las fichas de la biblioteca."""
    con = conexion_escritura()
    f = con.execute("SELECT 1 FROM lote WHERE codigo=?", (lote,)).fetchone()
    if not f:
        con.close()
        raise HTTPException(404, "No existe ese lote")
    ids = [r[0] for r in con.execute("SELECT id FROM linea WHERE lote_codigo=?", (lote,))]
    for i in ids:
        con.execute("DELETE FROM comentario WHERE linea_id=?", (i,))
        con.execute("DELETE FROM plano WHERE linea_id=?", (i,))
    con.execute("DELETE FROM linea WHERE lote_codigo=?", (lote,))
    con.execute("DELETE FROM lote WHERE codigo=?", (lote,))
    con.execute("""INSERT INTO historial (fecha, entidad, entidad_id, nota)
                   VALUES (?,?,?,?)""",
                (datetime.now().isoformat(timespec="seconds"), "lote", lote,
                 f"Lote borrado con sus {len(ids)} linea(s)"))
    con.commit()
    con.close()
    return {"ok": True, "lineas_borradas": len(ids)}


@app.get("/api/of/proximo")
def proximo_lote(fecha_oc: str = None):
    """
    Propone el proximo numero de lote y la fecha de entrega.
    El numero es una propuesta: los SPF no siempre van de a uno, asi que
    queda editable.
    """
    con = conexion()
    numeros = []
    for (cod,) in con.execute("SELECT codigo FROM lote"):
        m = re.match(r"^SPF-(\d+)", cod or "", re.I)
        if m:
            numeros.append(int(m.group(1)))
    con.close()
    # Incluir las OF en proceso (carpeta que alimenta Ariana) para no repetir un SPF ya reservado
    if EN_PROCESO.is_dir():
        for d in EN_PROCESO.iterdir():
            m = re.match(r"^SPF-(\d+)", d.name, re.I)
            if m:
                numeros.append(int(m.group(1)))
    proximo = f"SPF-{max(numeros) + 1}" if numeros else "SPF-1"

    try:
        base = datetime.fromisoformat(fecha_oc) if fecha_oc else datetime.now()
    except ValueError:
        base = datetime.now()
    return {
        "lote": proximo,
        "ultimo": f"SPF-{max(numeros)}" if numeros else None,
        "fecha_oc": base.date().isoformat(),
        "fecha_entrega": (base + timedelta(days=DIAS_ENTREGA)).date().isoformat(),
        "dias_entrega": DIAS_ENTREGA,
    }


@app.get("/api/en_proceso")
def en_proceso():
    """OF EN PROCESO: lee en vivo la carpeta que alimenta Ariana (SPF-XXXX-CLIENTE/)."""
    items = []
    if EN_PROCESO.is_dir():
        exts = {".pdf", ".png", ".jpg", ".jpeg", ".dwg", ".dxf"}
        for d in sorted(EN_PROCESO.iterdir(), key=lambda p: p.name):
            if not d.is_dir():
                continue
            datos = {}
            dj = d / "datos.json"
            if dj.exists():
                try:
                    datos = json.loads(dj.read_text(encoding="utf-8"))
                except Exception:
                    datos = {}
            planos = [p.name for p in d.iterdir() if p.suffix.lower() in exts]
            spf = datos.get("spf") or "-".join(d.name.split("-")[:2])
            cliente = datos.get("cliente") or d.name.split("-", 2)[-1]
            items.append({
                "carpeta": d.name, "spf": spf, "cliente": cliente,
                "oc_numero": datos.get("oc_numero"), "vendedor": datos.get("vendedor"),
                "fecha_entrega": datos.get("fecha_entrega"), "origen": datos.get("origen", "manual"),
                "planos": planos, "tiene_plano": bool(planos),
            })
    return items


@app.delete("/api/en_proceso/{carpeta}")
def quitar_en_proceso(carpeta: str):
    """Se llama al confirmar una OF que venia de la carpeta: ya no esta 'en proceso'."""
    if "/" in carpeta or "\\" in carpeta or ".." in carpeta or not carpeta.upper().startswith("SPF-"):
        raise HTTPException(400, "Nombre de carpeta invalido")
    d = EN_PROCESO / carpeta
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": True}


@app.get("/api/clientes")
def clientes():
    con = conexion()
    filas = con.execute("""SELECT cliente, COUNT(*) n FROM lote
                           WHERE cliente IS NOT NULL AND TRIM(cliente) <> ''
                           GROUP BY cliente ORDER BY n DESC""").fetchall()
    con.close()
    return [f["cliente"] for f in filas]


class LineaNueva(BaseModel):
    producto: dict
    cantidad: int | None = None
    archivos: list[str] = []          # una linea puede tener varios planos
    guardar_ficha: bool = True
    etiqueta: str | None = None       # heredada al repetir un producto


class OrdenNueva(BaseModel):
    lote: str
    cliente: str
    fecha_oc: str | None = None
    fecha_entrega: str | None = None
    vendedor: str | None = None
    observaciones: str | None = None
    lineas: list[LineaNueva]


@app.post("/api/of")
def crear_orden(orden: OrdenNueva):
    """
    Crea el lote y una linea por plano.
    Los datos tecnicos de cada linea quedan CONGELADOS en datos_json.
    """
    lote = (orden.lote or "").strip().upper()
    if not lote:
        raise HTTPException(400, "Falta el numero de lote")
    if not orden.lineas:
        raise HTTPException(400, "La orden no tiene ninguna linea")

    reg = R.cargar()
    ahora = datetime.now().isoformat(timespec="seconds")
    con = conexion_escritura()

    if con.execute("SELECT 1 FROM lote WHERE codigo=?", (lote,)).fetchone():
        con.close()
        raise HTTPException(400, f"El lote {lote} ya existe")

    con.execute("""INSERT INTO lote
        (codigo, cliente, fecha_oc, fecha_entrega, vendedor, observaciones, creado)
        VALUES (?,?,?,?,?,?,?)""",
        (lote, orden.cliente, orden.fecha_oc, orden.fecha_entrega,
         orden.vendedor, orden.observaciones, ahora))

    columnas = {c[1] for c in con.execute("PRAGMA table_info(producto)")}
    creadas = []

    for i, ln in enumerate(orden.lineas, start=1):
        p = dict(ln.producto)
        codigo = (p.get("codigo") or "").strip().upper()
        if not codigo:
            con.rollback(); con.close()
            raise HTTPException(400, f"La linea {i} no tiene codigo de producto")
        p["codigo"] = codigo

        # La ficha del producto se actualiza (es la biblioteca)...
        if ln.guardar_ficha:
            campos = {k: v for k, v in p.items() if k in columnas}
            campos.update({"origen": "plano", "revisar": 0,
                           "reglas_version": reg.version, "actualizado": ahora,
                           "familia": campos.get("familia") or "cartucho"})
            existe = con.execute("SELECT 1 FROM producto WHERE codigo=?",
                                 (codigo,)).fetchone()
            if existe:
                sets = ", ".join(f"{k}=?" for k in campos if k != "codigo")
                con.execute(f"UPDATE producto SET {sets} WHERE codigo=?",
                            [v for k, v in campos.items() if k != "codigo"] + [codigo])
            else:
                campos["creado"] = ahora
                con.execute(
                    f"INSERT INTO producto ({','.join(campos)}) "
                    f"VALUES ({','.join('?' * len(campos))})", list(campos.values()))
            con.execute("DELETE FROM producto_fts WHERE codigo=?", (codigo,))
            f = con.execute("""SELECT codigo, medio, soporte, nucleo_crudo, jaula_crudo
                               FROM producto WHERE codigo=?""", (codigo,)).fetchone()
            con.execute("""INSERT INTO producto_fts
                (codigo, medio, soporte, nucleo_crudo, jaula_crudo, clientes)
                VALUES (?,?,?,?,?,?)""",
                (f[0], f[1] or "", f[2] or "", f[3] or "", f[4] or "", orden.cliente or ""))

        # ...pero la LINEA se queda con una copia congelada.
        # Se congela la FICHA COMO QUEDO, no lo que leyo el plano: la ficha
        # puede tener datos que el plano no menciona (los que ya estaban de
        # antes). Si congelaramos solo la lectura, el aviso "la ficha cambio"
        # saltaria en falso apenas creada la orden.
        guardada = con.execute("SELECT * FROM producto WHERE codigo=?",
                               (codigo,)).fetchone()
        congelado = como_dict(guardada) if guardada else p

        # el primer plano queda como el "principal" (la miniatura de la lista)
        principal = ln.archivos[0] if ln.archivos else None
        cur = con.execute("""INSERT INTO linea
            (lote_codigo, nro, producto_codigo, cantidad, estado, datos_json,
             reglas_version, plano_nombre, etiqueta, creado, actualizado)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (lote, i, codigo, ln.cantidad, "pendiente",
             json.dumps(congelado, ensure_ascii=False), reg.version, principal,
             (ln.etiqueta or "").strip() or None, ahora, ahora))
        creadas.append({"id": cur.lastrowid, "nro": i, "codigo": codigo})

        for archivo in ln.archivos:
            con.execute("""INSERT INTO plano
                (producto_codigo, linea_id, nombre, ruta, creado)
                VALUES (?,?,?,?,?)""",
                (codigo, cur.lastrowid, archivo, str(PLANOS / archivo), ahora))

        con.execute("""INSERT INTO historial (fecha, entidad, entidad_id, nota)
                       VALUES (?,?,?,?)""",
                    (ahora, "linea", str(cur.lastrowid),
                     f"Linea creada en {lote} desde el plano"))

    con.commit()
    con.close()
    return {"ok": True, "lote": lote, "lineas": creadas}


@app.get("/api/of")
def listar_of(estado: str = "activas"):
    """Lotes con el resumen del estado de sus lineas."""
    con = conexion()
    filas = con.execute("""
        SELECT lo.codigo, lo.cliente, lo.fecha_oc, lo.fecha_entrega, lo.vendedor,
               COUNT(l.id) AS lineas,
               SUM(l.estado = 'finalizada')    AS finalizadas,
               SUM(l.estado = 'en_produccion') AS en_produccion,
               SUM(l.estado = 'pausada')       AS pausadas,
               SUM(l.estado = 'pendiente')     AS pendientes,
               SUM(l.compra_json IS NOT NULL AND l.compra_json <> '') AS con_compra,
               SUM(COALESCE(l.cantidad, 0))    AS total_piezas
        FROM lote lo JOIN linea l ON l.lote_codigo = lo.codigo
        GROUP BY lo.codigo
        ORDER BY lo.fecha_entrega IS NULL, lo.fecha_entrega, lo.codigo DESC""").fetchall()
    con.close()

    salida = []
    for f in filas:
        d = como_dict(f)
        d["completo"] = d["lineas"] > 0 and d["finalizadas"] == d["lineas"]
        if estado == "activas" and d["completo"]:
            continue
        if estado == "finalizadas" and not d["completo"]:
            continue
        salida.append(d)
    return {"total": len(salida), "lotes": salida}


@app.get("/api/of/{lote}")
def ver_of(lote: str):
    con = conexion()
    lo = con.execute("SELECT * FROM lote WHERE codigo=?", (lote,)).fetchone()
    if not lo:
        con.close()
        raise HTTPException(404, "No existe ese lote")
    lineas = con.execute("""
        SELECT l.*, p.medio, p.altura, p.od, p.id_int,
               (SELECT COUNT(*) FROM comentario c
                WHERE c.linea_id = l.id AND c.estado='abierto') AS comentarios
        FROM linea l LEFT JOIN producto p ON p.codigo = l.producto_codigo
        WHERE l.lote_codigo=? ORDER BY l.nro, l.id""", (lote,)).fetchall()
    con.close()

    salida = []
    for f in lineas:
        d = como_dict(f)
        cong = json.loads(d.pop("datos_json") or "{}")
        d["altura"] = cong.get("altura", d.get("altura"))
        d["od"] = cong.get("od", d.get("od"))
        d["id_int"] = cong.get("id_int", d.get("id_int"))
        d["medio"] = cong.get("medio", d.get("medio"))
        salida.append(d)
    return {"lote": como_dict(lo), "lineas": salida, "estados": ESTADOS}


@app.get("/api/producto/{codigo}/repetir")
def repetir_producto(codigo: str):
    """Para repetir un producto ya fabricado: devuelve la ficha CONGELADA de la
    ultima fabricacion, sus planos, la ultima etiqueta y los comentarios."""
    codigo = codigo.strip().upper()
    con = conexion()
    p = con.execute("SELECT * FROM producto WHERE codigo=?", (codigo,)).fetchone()
    ult = con.execute("""SELECT id, lote_codigo, etiqueta, creado, datos_json
                         FROM linea WHERE producto_codigo=?
                         ORDER BY creado DESC, id DESC LIMIT 1""", (codigo,)).fetchone()
    if not p and not ult:
        con.close()
        raise HTTPException(404, f"{codigo} no está en la biblioteca ni fue fabricado antes")

    planos, coments = [], []
    if ult:
        planos = [r[0] for r in con.execute(
            "SELECT nombre FROM plano WHERE linea_id=? ORDER BY id", (ult["id"],))]
        coments = [{"fecha": r["fecha"], "usuario": r["usuario"], "tipo": r["tipo"],
                    "texto": r["texto"], "estado": r["estado"]}
                   for r in con.execute(
                       "SELECT fecha, usuario, tipo, texto, estado FROM comentario "
                       "WHERE linea_id=? ORDER BY fecha", (ult["id"],))]
    if not planos:   # sin linea previa (o sin planos en ella): los de la biblioteca
        planos = [r[0] for r in con.execute(
            "SELECT nombre FROM plano WHERE producto_codigo=? "
            "ORDER BY creado DESC, id DESC LIMIT 3", (codigo,))]
    con.close()

    ficha = {}
    if ult and ult["datos_json"]:
        ficha = json.loads(ult["datos_json"])
    elif p:
        ficha = como_dict(p)
    ficha["codigo"] = codigo
    return {"codigo": codigo, "producto": ficha,
            "etiqueta": ult["etiqueta"] if ult else None,
            "ultimo_lote": ult["lote_codigo"] if ult else None,
            "fecha_ultima": ult["creado"] if ult else None,
            "planos": planos, "comentarios": coments}


class EtiquetaIn(BaseModel):
    etiqueta: str | None = None


@app.post("/api/linea/{linea_id}/etiqueta")
def poner_etiqueta(linea_id: int, e: EtiquetaIn):
    """Etiqueta manual de Ingeniería para la línea."""
    con = conexion_escritura()
    con.execute("UPDATE linea SET etiqueta=?, actualizado=? WHERE id=?",
                ((e.etiqueta or "").strip() or None,
                 datetime.now().isoformat(timespec="seconds"), linea_id))
    con.commit()
    con.close()
    return {"ok": True}


@app.get("/api/linea/{linea_id}")
def ver_linea(linea_id: int):
    """Detalle de una linea: datos congelados, calculos, plano y comentarios."""
    con = conexion()
    f = con.execute("""SELECT l.*, lo.cliente, lo.fecha_entrega, lo.fecha_oc
                       FROM linea l JOIN lote lo ON lo.codigo = l.lote_codigo
                       WHERE l.id=?""", (linea_id,)).fetchone()
    if not f:
        con.close()
        raise HTTPException(404, "No existe esa linea")

    reg = R.cargar()
    d = json.loads(f["datos_json"] or "{}")
    calc, avisos = calcular_dict(d, reg) if d else ({}, [])

    # ¿La ficha del producto cambio desde que se lanzo esta orden?
    cambios = []
    actual = con.execute("SELECT * FROM producto WHERE codigo=?",
                         (f["producto_codigo"],)).fetchone()
    if actual and d:
        for k in ("altura", "od", "id_int", "q", "hp", "medio",
                  "nucleo_crudo", "jaula_crudo"):
            if k in actual.keys():
                viejo, nuevo = d.get(k), actual[k]
                if str(viejo or "") != str(nuevo or ""):
                    cambios.append({"campo": k, "en_la_orden": viejo,
                                    "en_la_ficha": nuevo})

    comentarios = con.execute("""SELECT * FROM comentario WHERE linea_id=?
                                 ORDER BY fecha DESC""", (linea_id,)).fetchall()
    planos = con.execute("""SELECT nombre FROM plano WHERE linea_id=?
                            ORDER BY id""", (linea_id,)).fetchall()
    con.close()

    # estructuras con desarrollo numerico: las que se pueden optimizar el corte
    estructuras = []
    for pref, etq in (("nucleo", "Núcleo"), ("nucleo_int", "Núcleo intermedio"),
                      ("jaula", "Jaula")):
        item = calc.get(pref) or {}
        if item.get("valor") and calc.get("altura_estructuras", {}).get("valor"):
            estructuras.append({
                "pieza": pref, "etiqueta": etq,
                "desarrollo": item["valor"],
                "altura": item.get("altura") or calc["altura_estructuras"]["valor"],
                "material": d.get(f"{pref}_material"),
                "espesor": d.get(f"{pref}_espesor"),
                "tipo_chapa": d.get(f"{pref}_tipo_chapa"),
            })

    procesos = json.loads(f["procesos_json"] or "{}") if "procesos_json" in f.keys() else {}
    compra = json.loads(f["compra_json"] or "{}") if "compra_json" in f.keys() else {}

    return {
        "linea": {k: f[k] for k in f.keys()
                  if k not in ("datos_json", "procesos_json", "compra_json")},
        "datos": d, "calculado": calc, "avisos": avisos,
        "ficha_cambio": cambios,
        "planos": [p["nombre"] for p in planos],
        "estructuras_cortables": estructuras,
        "procesos": procesos,
        "materiales": materiales_de(d),
        "compra": compra,
        "tolerancias": reg.tolerancias, "criterios": reg.criterios,
        "comentarios": [como_dict(c) for c in comentarios],
        "estados": ESTADOS,
    }


class CompraLinea(BaseModel):
    materiales: dict = {}          # {clave: bool} — qué materiales van a compra
    observaciones: str | None = None
    usuario: str | None = None


@app.post("/api/linea/{linea_id}/compra")
def marcar_compra(linea_id: int, c: CompraLinea):
    """
    Producción marca qué materiales de la línea van a compra interna.
    Con que uno quede marcado, el lote aparece en Compras (sin salir de activas).
    """
    ahora = datetime.now().isoformat(timespec="seconds")
    con = conexion_escritura()
    f = con.execute("SELECT compra_json FROM linea WHERE id=?", (linea_id,)).fetchone()
    if not f:
        con.close()
        raise HTTPException(404, "No existe esa linea")
    marcados = {k: True for k, v in c.materiales.items() if v}
    payload = {"materiales": marcados, "observaciones": c.observaciones or "",
               "por": c.usuario, "fecha": ahora}
    con.execute("UPDATE linea SET compra_json=?, actualizado=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), ahora, linea_id))
    con.execute("""INSERT INTO historial
        (fecha, usuario, entidad, entidad_id, campo, valor_nuevo)
        VALUES (?,?,?,?,?,?)""",
        (ahora, c.usuario, "linea", str(linea_id), "compra",
         ", ".join(marcados) or "sin materiales"))
    con.commit()
    con.close()
    return {"ok": True, "compra": payload}


@app.get("/api/compras")
def compras():
    """
    Lotes con al menos un material marcado para compra interna.
    El lote sigue en OF activas; acá se ve además el pedido de compra.
    """
    con = conexion()
    filas = con.execute("""
        SELECT l.id, l.lote_codigo, l.nro, l.producto_codigo, l.cantidad,
               l.compra_json, l.datos_json, lo.cliente, lo.fecha_entrega
        FROM linea l JOIN lote lo ON lo.codigo = l.lote_codigo
        WHERE l.compra_json IS NOT NULL AND l.compra_json <> ''
        ORDER BY lo.fecha_entrega IS NULL, lo.fecha_entrega, l.lote_codigo, l.nro""").fetchall()
    con.close()

    lotes = {}
    for f in filas:
        compra = json.loads(f["compra_json"] or "{}")
        marcados = compra.get("materiales") or {}
        if not marcados:
            continue
        d = json.loads(f["datos_json"] or "{}")
        todos = {m["clave"]: m for m in materiales_de(d)}
        items = [{"clave": k, "etiqueta": todos.get(k, {}).get("etiqueta", k),
                  "texto": todos.get(k, {}).get("texto", k),
                  "cantidad": f["cantidad"]}
                 for k in marcados]
        lo = lotes.setdefault(f["lote_codigo"], {
            "lote": f["lote_codigo"], "cliente": f["cliente"],
            "fecha_entrega": f["fecha_entrega"], "lineas": []})
        lo["lineas"].append({
            "linea_id": f["id"], "nro": f["nro"], "producto": f["producto_codigo"],
            "cantidad": f["cantidad"], "materiales": items,
            "observaciones": compra.get("observaciones", ""),
            "por": compra.get("por"), "fecha": compra.get("fecha")})
    return {"total": len(lotes), "lotes": list(lotes.values())}


class Proceso(BaseModel):
    proceso: str
    completo: bool
    usuario: str | None = None


@app.post("/api/linea/{linea_id}/proceso")
def marcar_proceso(linea_id: int, p: Proceso):
    """Producción tilda un proceso como completo (tapas, etc.)."""
    ahora = datetime.now().isoformat(timespec="seconds")
    con = conexion_escritura()
    f = con.execute("SELECT procesos_json FROM linea WHERE id=?", (linea_id,)).fetchone()
    if not f:
        con.close()
        raise HTTPException(404, "No existe esa linea")
    procesos = json.loads(f["procesos_json"] or "{}")
    if p.completo:
        procesos[p.proceso] = {"por": p.usuario, "fecha": ahora}
    else:
        procesos.pop(p.proceso, None)
    con.execute("UPDATE linea SET procesos_json=?, actualizado=? WHERE id=?",
                (json.dumps(procesos, ensure_ascii=False), ahora, linea_id))
    con.execute("""INSERT INTO historial
        (fecha, usuario, entidad, entidad_id, campo, valor_nuevo)
        VALUES (?,?,?,?,?,?)""",
        (ahora, p.usuario, "linea", str(linea_id), "proceso:" + p.proceso,
         "completo" if p.completo else "pendiente"))
    con.commit()
    con.close()
    return {"ok": True, "procesos": procesos}


class TapaTipo(BaseModel):
    tipo: str


@app.post("/api/linea/{linea_id}/tapa_tipo")
def set_tapa_tipo(linea_id: int, t: TapaTipo):
    """Guarda el tipo de proceso de tapa elegido (define cual checklist se muestra)."""
    ahora = datetime.now().isoformat(timespec="seconds")
    con = conexion_escritura()
    f = con.execute("SELECT procesos_json FROM linea WHERE id=?", (linea_id,)).fetchone()
    if not f:
        con.close()
        raise HTTPException(404, "No existe esa linea")
    procesos = json.loads(f["procesos_json"] or "{}")
    procesos["_tapa_tipo"] = t.tipo
    con.execute("UPDATE linea SET procesos_json=?, actualizado=? WHERE id=?",
                (json.dumps(procesos, ensure_ascii=False), ahora, linea_id))
    con.commit()
    con.close()
    return {"ok": True, "tipo": t.tipo}


@app.get("/api/chapas")
def chapas():
    return R.cargar().chapas_estandar


class PiezaCorte(BaseModel):
    label: str = ""
    desarrollo: float
    altura: float
    cantidad: int


class Optimizar(BaseModel):
    ancho: float
    alto: float
    rotar: bool = True
    piezas: list[PiezaCorte]


@app.post("/api/optimizar")
def optimizar(o: Optimizar):
    reg = R.cargar()
    piezas = [{"label": p.label, "desarrollo": p.desarrollo,
               "altura": p.altura, "cantidad": p.cantidad} for p in o.piezas]
    return R.optimizar_multiple(reg, o.ancho, o.alto, piezas, o.rotar)


class CambioEstado(BaseModel):
    estado: str
    usuario: str | None = None


@app.post("/api/linea/{linea_id}/estado")
def cambiar_estado(linea_id: int, cambio: CambioEstado):
    if cambio.estado not in ESTADOS:
        raise HTTPException(400, f"Estado invalido. Son: {', '.join(ESTADOS)}")
    ahora = datetime.now().isoformat(timespec="seconds")
    con = conexion_escritura()
    f = con.execute("SELECT estado FROM linea WHERE id=?", (linea_id,)).fetchone()
    if not f:
        con.close()
        raise HTTPException(404, "No existe esa linea")
    con.execute("UPDATE linea SET estado=?, actualizado=? WHERE id=?",
                (cambio.estado, ahora, linea_id))
    con.execute("""INSERT INTO historial
        (fecha, usuario, entidad, entidad_id, campo, valor_anterior, valor_nuevo)
        VALUES (?,?,?,?,?,?,?)""",
        (ahora, cambio.usuario, "linea", str(linea_id), "estado",
         f["estado"], cambio.estado))
    con.commit()
    con.close()
    return {"ok": True, "estado": cambio.estado}


class ComentarioNuevo(BaseModel):
    texto: str
    usuario: str | None = None
    tipo: str = "comentario"
    campo: str | None = None
    valor_propuesto: str | None = None


@app.post("/api/linea/{linea_id}/comentario")
def comentar(linea_id: int, c: ComentarioNuevo):
    """Produccion no edita la orden, pero puede comentar o proponer un cambio."""
    if not (c.texto or "").strip():
        raise HTTPException(400, "El comentario esta vacio")
    ahora = datetime.now().isoformat(timespec="seconds")
    con = conexion_escritura()
    if not con.execute("SELECT 1 FROM linea WHERE id=?", (linea_id,)).fetchone():
        con.close()
        raise HTTPException(404, "No existe esa linea")
    con.execute("""INSERT INTO comentario
        (linea_id, fecha, usuario, tipo, texto, campo, valor_propuesto)
        VALUES (?,?,?,?,?,?,?)""",
        (linea_id, ahora, c.usuario, c.tipo, c.texto.strip(), c.campo,
         c.valor_propuesto))
    con.commit()
    con.close()
    return {"ok": True}


@app.post("/api/comentario/{cid}/{accion}")
def resolver_comentario(cid: int, accion: str):
    if accion not in ("aceptado", "rechazado"):
        raise HTTPException(400, "Accion invalida")
    con = conexion_escritura()
    con.execute("UPDATE comentario SET estado=? WHERE id=?", (accion, cid))
    con.commit()
    con.close()
    return {"ok": True, "estado": accion}


@app.get("/api/calcular")
def calcular_suelto(tipo: str, diam: float = None, esp: float = None,
                    q: float = None, hp: float = None):
    """Calculadora suelta, para verificar una cuenta a mano."""
    reg = R.cargar()
    try:
        if tipo == "plisado":
            v = reg.aplicar("desarrollo_medio_plisado", Q=q, Hp=hp)
            return {"valor": round(v, 1), "formula": reg.formulas["desarrollo_medio_plisado"]["formula"]}
        if tipo == "chapa":
            pieza = {"conformado": "rolado", "tipo_chapa": "perforado",
                     "espesor": esp, "diam": diam}
            v, expl = R.desarrollo_estructura(reg, pieza)
            return {"valor": round(v, 1) if v else None, "detalle": expl}
        if tipo == "molde":
            return reg.buscar_molde("perforado", diam) or {"error": "No existe ese molde"}
    except R.ErrorDeRegla as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    raise HTTPException(400, "Tipo desconocido")


# =============================================================================
#  Web
# =============================================================================

@app.get("/")
def inicio():
    return FileResponse(WEB / "index.html")


app.mount("/web", StaticFiles(directory=WEB), name="web")


PUERTO = int(os.environ.get("PUERTO_OF", "8000"))


def mis_ips():
    """Todas las direcciones de esta PC en la red, no una adivinada."""
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith(("127.", "169.254.")) and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    # la que efectivamente sale a la red local, primero
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 1))
        principal = s.getsockname()[0]
        s.close()
        if principal in ips:
            ips.remove(principal)
        ips.insert(0, principal)
    except Exception:
        pass
    return ips or ["127.0.0.1"]


if __name__ == "__main__":
    import uvicorn
    ips = mis_ips()
    print()
    print("=" * 64)
    print("  SISTEMA DE ORDENES DE FABRICACION")
    print("=" * 64)
    print(f"  En esta PC:     http://localhost:{PUERTO}")
    for i, ip in enumerate(ips):
        etiqueta = "Desde la red:  " if i == 0 else "               "
        print(f"  {etiqueta} http://{ip}:{PUERTO}")
    print()
    print("  Esta ventana tiene que quedar abierta.")
    print("  Si desde otra PC no entra, falta abrir el puerto en el Firewall")
    print("  (ver ABRIR-PUERTO.txt).")
    print("=" * 64)
    print()
    uvicorn.run(app, host="0.0.0.0", port=PUERTO, log_level="warning")
