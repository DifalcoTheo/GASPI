# -*- coding: utf-8 -*-
"""
Motor de reglas.

Lee reglas.yaml y EJECUTA lo que dice. Las formulas y las expresiones de
validacion se evaluan tal como estan escritas en el archivo: cambiar una
formula ahi cambia el calculo, sin tocar este programa.

No hay ninguna formula escrita en este archivo. Si aparece un numero magico
aca adentro, es un error.
"""

import ast
import math
import operator
import re
import unicodedata
from pathlib import Path

import yaml

RUTA_REGLAS = Path(__file__).parent / "reglas.yaml"


# =============================================================================
#  Evaluador seguro de expresiones
#
#  Evalua "(Oi + esp) * PI + solapa_mm" con un diccionario de variables.
#  Solo admite numeros, nombres de variables y + - * / // % ** ( ) y
#  comparaciones. No admite llamadas a funciones, atributos, ni nada que
#  pueda ejecutar codigo: una formula mal escrita en el YAML da error, no
#  hace dano.
# =============================================================================

_BINARIOS = {
    ast.Add: operator.add,      ast.Sub: operator.sub,
    ast.Mult: operator.mul,     ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARIOS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_COMPARACIONES = {
    ast.Lt: operator.lt, ast.LtE: operator.le,
    ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
}


class ErrorDeRegla(Exception):
    """La formula del YAML no se puede evaluar (mal escrita o falta un dato)."""


def evaluar(expresion, variables):
    """Evalua una expresion del archivo de reglas con las variables dadas."""
    try:
        arbol = ast.parse(str(expresion), mode="eval").body
    except SyntaxError as e:
        raise ErrorDeRegla(f"Formula mal escrita: {expresion!r} ({e})")

    def _ev(n):
        if isinstance(n, ast.Constant):
            if isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
                return n.value
            raise ErrorDeRegla(f"Valor no permitido en la formula: {n.value!r}")
        if isinstance(n, ast.Name):
            if n.id not in variables:
                raise ErrorDeRegla(f"Falta el dato '{n.id}' para calcular {expresion!r}")
            v = variables[n.id]
            if v is None:
                raise ErrorDeRegla(f"El dato '{n.id}' esta vacio")
            return v
        if isinstance(n, ast.BinOp) and type(n.op) in _BINARIOS:
            return _BINARIOS[type(n.op)](_ev(n.left), _ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UNARIOS:
            return _UNARIOS[type(n.op)](_ev(n.operand))
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in _COMPARACIONES:
            return _COMPARACIONES[type(n.ops[0])](_ev(n.left), _ev(n.comparators[0]))
        raise ErrorDeRegla(f"Operacion no permitida en la formula: {expresion!r}")

    try:
        return _ev(arbol)
    except ZeroDivisionError:
        raise ErrorDeRegla(f"Division por cero al evaluar {expresion!r}")


# =============================================================================
#  Utilidades de texto
# =============================================================================

def normalizar(texto):
    """Minusculas, sin acentos, sin espacios de mas. Para comparar sinonimos."""
    if texto is None:
        return ""
    t = unicodedata.normalize("NFKD", str(texto))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", t).strip().lower()


def numero(texto):
    """Primer numero de un texto. Acepta coma o punto decimal. None si no hay."""
    if texto is None:
        return None
    if isinstance(texto, (int, float)):
        return float(texto)
    m = re.search(r"(\d+(?:[.,]\d+)?)", str(texto))
    return float(m.group(1).replace(",", ".")) if m else None


# =============================================================================
#  Reglas
# =============================================================================

class Reglas:
    """Todo lo que dice reglas.yaml, cargado."""

    def __init__(self, ruta=RUTA_REGLAS):
        self.ruta = Path(ruta)
        with open(self.ruta, encoding="utf-8") as f:
            self.d = yaml.safe_load(f)

        self.version = self.d.get("version")
        self.vigente_desde = self.d.get("vigente_desde")
        self.parametros = self.d.get("parametros", {})
        self.formulas = self.d.get("formulas", {})
        self.tablas_molde = self.d.get("tablas_molde", {})
        # Tabla molde -> Ø externo para estructuras espiraladas sobre las que se
        # enrolla un medio filtrante. Fuente de verdad (reemplaza a la fórmula
        # molde+4 cuando el molde está en la tabla).
        self.tabla_externo_rolado = self.d.get("tabla_externo_espiralado_rolado", {})
        # Tipos de elemento filtrante reconocidos por el código (filtro Y, etc.):
        # aceleran la lectura de cotas (ver reglas.yaml, sección "tipos_elemento").
        self.tipos_elemento = self.d.get("tipos_elemento", {})
        self.validaciones = self.d.get("validaciones", [])
        self.tolerancias = self.d.get("tolerancias", {})
        self.criterios = self.d.get("criterios_de_aceptacion", [])
        self.lectura_plano = self.d.get("lectura_plano", {})
        self.medios = self.d.get("medios_filtrantes", [])
        self.acompanamiento = self.d.get("acompanamiento", {})
        self.chapas_estandar = self.d.get("chapas_estandar", [])
        self.orden_importancia = (self.d.get("orden_importancia_estructuras")
                                  or ["nucleo_int", "jaula", "nucleo"])

        # Sinonimos -> {texto normalizado: canonico}, por grupo
        self._sin = {}
        for grupo, mapa in (self.d.get("sinonimos") or {}).items():
            tabla = {}
            for canonico, variantes in (mapa or {}).items():
                tabla[normalizar(canonico)] = canonico
                for v in variantes or []:
                    tabla[normalizar(v)] = canonico
            self._sin[grupo] = tabla

    # -- sinonimos ------------------------------------------------------------

    def canonico(self, grupo, texto):
        """'Electrocincada' -> 'galvanizada'. None si no reconoce nada."""
        tabla = self._sin.get(grupo, {})
        t = normalizar(texto)
        if not t:
            return None
        if t in tabla:
            return tabla[t]
        # buscar la variante mas larga contenida en el texto
        mejor = None
        for variante, canonico in tabla.items():
            if variante and variante in t:
                if mejor is None or len(variante) > len(mejor[0]):
                    mejor = (variante, canonico)
        return mejor[1] if mejor else None

    # -- medios filtrantes ----------------------------------------------------

    def reconocer_medio(self, texto):
        """
        Identifica el medio filtrante nombrado en un texto y revisa que este
        completo. El texto del plano NO se toca: esto solo reconoce y avisa.

        Devuelve (medio_de_la_lista_o_None, [avisos]).
        """
        t = normalizar(texto)
        if not t:
            return None, []

        encontrado, largo = None, 0
        for m in self.medios:
            for forma in m.get("se_escribe", []) + [m["nombre"]]:
                f = normalizar(forma)
                if f and f in t and len(f) > largo:
                    encontrado, largo = m, len(f)

        avisos = []
        if not encontrado:
            return None, avisos

        # ¿Le falta el dato que siempre lo acompana?
        pide = encontrado.get("debe_indicar")
        if pide:
            patrones = self.acompanamiento.get(pide, [])
            if not any(re.search(p, t, re.I) for p in patrones):
                avisos.append({
                    "id": "medio_incompleto", "nivel": "avisa",
                    "mensaje": f"'{encontrado['nombre']}' tiene que venir con {pide}. "
                               f"En el plano dice: \"{texto.strip()}\"",
                })
        return encontrado, avisos

    def forma_admitida(self, medio, forma):
        """¿Ese medio se puede hacer de esa forma? (rolado / plisado / otro)"""
        if not medio or not forma:
            return None
        return forma in (medio.get("formas") or [])

    # -- formulas -------------------------------------------------------------

    def _entorno(self, extra=None):
        env = {"PI": math.pi}
        env.update(self.parametros)
        if extra:
            env.update({k: v for k, v in extra.items() if v is not None})
        return env

    def aplicar(self, nombre_formula, **datos):
        """Evalua una formula de reglas.yaml por su nombre."""
        f = self.formulas.get(nombre_formula)
        if not f:
            raise ErrorDeRegla(f"No existe la formula '{nombre_formula}' en reglas.yaml")
        if "formula" not in f:
            raise ErrorDeRegla(
                f"'{nombre_formula}' no se calcula: {f.get('fuente', 'sin formula')}")
        return evaluar(f["formula"], self._entorno(datos))

    # -- moldes ---------------------------------------------------------------

    def externo_espiralado_rolado(self, molde):
        """Ø externo de una estructura ESPIRALADA sobre la que se enrolla un medio
        filtrante, tomado de la tabla molde->externo. Si el molde no está en la
        tabla, cae a la fórmula 'diametro_externo_espiralado' (molde + 4)."""
        if molde is None:
            return None
        m = float(molde)
        for fila_m, ext in (self.tabla_externo_rolado or {}).items():
            if abs(float(fila_m) - m) < 0.01:
                return float(ext)
        return self.aplicar("diametro_externo_espiralado", molde=m)

    def buscar_molde(self, tipo, diametro):
        """
        Busca un molde por su diametro. tipo: perforado | ranurado | desplegado.
        Devuelve el dict del molde, o None si ese diametro no existe.
        """
        if diametro is None:
            return None
        d = float(diametro)
        if tipo == "desplegado":
            tabla = self.tablas_molde.get("espiralado_desplegado", {})
            for v in tabla.get("diametros_internos", []):
                if abs(float(v) - d) < 0.01:
                    return {"tipo": "desplegado", "diam_interno": float(v),
                            "espesor": self.parametros.get("espesor_desplegado_mm")}
            return None

        tabla = self.tablas_molde.get(f"espiralado_{tipo}")
        if not tabla:
            return None
        esp = tabla.get("espesor_mm")
        for fila in tabla.get("filas", []):
            molde, ext, galv, hoja = fila
            # se puede buscar por diametro de molde o por diametro externo
            if abs(float(molde) - d) < 0.01 or abs(float(ext) - d) < 0.01:
                return {"tipo": tipo, "diam_molde": float(molde),
                        "diam_externo": float(ext), "espesor": esp,
                        "peso_galvanizada": float(galv), "peso_hojalata": float(hoja)}
        return None

    def moldes(self, tipo):
        """Todas las filas de una tabla de molde, para mostrar o importar."""
        if tipo == "desplegado":
            t = self.tablas_molde.get("espiralado_desplegado", {})
            esp = self.parametros.get("espesor_desplegado_mm")
            return [{"tipo": "desplegado", "diam_interno": float(v), "espesor": esp}
                    for v in t.get("diametros_internos", [])]
        t = self.tablas_molde.get(f"espiralado_{tipo}", {})
        esp = t.get("espesor_mm")
        return [{"tipo": tipo, "diam_molde": float(m), "diam_externo": float(e),
                 "espesor": esp, "peso_galvanizada": float(g), "peso_hojalata": float(h)}
                for m, e, g, h in t.get("filas", [])]

    # -- validaciones ---------------------------------------------------------

    def validar(self, datos):
        """
        Corre las validaciones que tienen 'expresion' en reglas.yaml.
        Las que no la tienen son estructurales y las resuelve quien llama.
        Devuelve la lista de las que dieron positivo (o sea, los problemas).
        """
        problemas = []
        env = self._entorno(datos)
        for v in self.validaciones:
            expr = v.get("expresion")
            if not expr:
                continue
            try:
                if evaluar(expr, env):
                    problemas.append({"id": v["id"], "nivel": v.get("nivel", "avisa"),
                                      "mensaje": v.get("mensaje", v["id"])})
            except ErrorDeRegla:
                # falta un dato para evaluarla: no es un problema, es que no aplica
                continue
        return problemas


# =============================================================================
#  Calculo de desarrollos
#
#  Elige QUE formula corresponde segun el caso; la formula en si sale del YAML.
#  Devuelve siempre de donde salio el numero, para poder mostrarselo al usuario.
# =============================================================================

def desarrollo_estructura(reglas, pieza):
    """
    Desarrollo de un nucleo o una jaula.
    'pieza' es un dict con: conformado, tipo_chapa, espesor, diam, molde, kg_m2.
    Devuelve (valor_mm, explicacion) o (None, motivo).
    """
    conformado = pieza.get("conformado")
    tipo_chapa = pieza.get("tipo_chapa")
    diam = pieza.get("diam")
    esp = pieza.get("espesor")

    if conformado == "espiralado":
        if pieza.get("sin_molde"):
            return None, "Espiralado sin molde (lo dice el plano): no sale de tabla"
        molde = reglas.buscar_molde(tipo_chapa or "perforado",
                                    pieza.get("molde") or diam)
        if molde:
            if tipo_chapa == "desplegado":
                return None, f"Espiralado desplegado Ø{molde['diam_interno']:g} interno (de tabla, no se calcula desarrollo)"
            return None, (f"Espiralado {tipo_chapa}: molde Ø{molde['diam_molde']:g} "
                          f"-> Ø externo {molde['diam_externo']:g} (de tabla, no se calcula desarrollo)")
        return None, "Espiralado sin molde identificado"

    if diam is None:
        return None, "Falta el Ø interno de la pieza"

    if tipo_chapa == "desplegado":
        v = reglas.aplicar("desarrollo_desplegado_rolado", Oi=diam)
        return v, f"Desplegado rolado: (Ø{diam:g} + {reglas.parametros['espesor_desplegado_mm']}) x PI + {reglas.parametros['solapa_mm']} de solapa"

    if esp is None:
        return None, "Falta el espesor de la chapa"

    limite = reglas.parametros["espesor_limite_solapa_mm"]
    if esp < limite:
        v = reglas.aplicar("desarrollo_chapa_rolada_solapada", Oi=diam, esp=esp)
        return v, f"Chapa rolada de {esp:g} mm (menor a {limite:g}): solapada, + {reglas.parametros['solapa_mm']} mm"
    v = reglas.aplicar("desarrollo_chapa_rolada_a_tope", Oi=diam, esp=esp)
    return v, f"Chapa rolada de {esp:g} mm ({limite:g} o mas): a tope, sin solapa"


def desarrollo_medio(reglas, producto, forma=None, diam_rolado=None,
                     q=None, hp=None, vueltas=None):
    """Desarrollo del medio filtrante. Devuelve (valor_mm, explicacion).
    'forma' fuerza plisado/rolado, para filtros con mas de un medio de distinta
    forma (Q y Hp son de los plisados; vueltas, de los rolados).
    'q', 'hp' y 'vueltas' son POR MEDIO: cada medio (medio, medio_2, medio_3)
    trae los suyos (q/q_2/q_3, hp/hp_2/hp_3, vueltas/vueltas_2/vueltas_3). Los
    pasa calcular_dict; asi dos rolados o dos plisados del mismo filtro pueden
    tener distinta cantidad de vueltas o de pliegues.
    'diam_rolado' es el Ø sobre el que se enrolla (Ø interno de la estructura
    elegida + sobreespesor); si no se pasa, cae al Ø externo del filtro."""
    tipo = forma or producto.get("tipo_medio")

    if tipo == "plisado":
        if q is None or hp is None:
            return None, "Faltan Q o Hp"
        v = reglas.aplicar("desarrollo_medio_plisado", Q=q, Hp=hp)
        return v, f"Plisado: {q:g} x {hp:g} x 2 + {q:g}"

    if tipo == "rolado":
        if vueltas is None:
            return None, "Falta la cantidad de vueltas"
        if diam_rolado is not None:
            oext = diam_rolado
        else:
            return None, "Falta elegir sobre qué estructura va el rolado"
        v = reglas.aplicar("desarrollo_medio_rolado", Oext=oext, vueltas=vueltas)
        return v, (f"Rolado: PI x Ø{oext:g} x {vueltas:g} vueltas + "
                   f"{reglas.parametros['solapa_mm']} de solapa")

    return None, "Tipo de medio sin definir"


# Tipo de tapa que elige Ingenieria (plural, del proceso) -> tipo de descuento.
_TIPO_TAPA_A_DESCUENTO = {
    "mecanizadas": "mecanizada", "repujadas": "repujada",
    "impresas3d": "otra", "plasticas": "otra",
}


def _descuento_tapa(reglas, tipo_elegido=None, texto=None):
    """Descuento de altura de UNA tapa. 'tipo_elegido' es lo que marca Ingenieria
    (mecanizadas/repujadas/impresas3d/plasticas); si no está, se detecta del texto."""
    tipo = _TIPO_TAPA_A_DESCUENTO.get(tipo_elegido) if tipo_elegido else None
    if not tipo:
        tipo = reglas.canonico("tipo_tapa", texto)
    if tipo == "mecanizada":
        return reglas.parametros["descuento_altura_tapa_mecanizada_mm"], "mecanizada"
    if tipo == "repujada":
        return reglas.parametros["descuento_altura_tapa_repujada_mm"], "repujada"
    return reglas.parametros["descuento_altura_tapa_otra_mm"], (tipo_elegido or "otra")


def altura_estructuras(reglas, altura, texto_tapa, texto_tapa_2=None,
                       tipo1=None, tipo2=None, distintas=False):
    """
    Altura del nucleo, del nucleo intermedio y de la jaula = altura del filtro
    menos el descuento por tapa. Con UNA tapa (o dos iguales) el descuento sale
    de esa tapa. Con dos tapas de tipo DISTINTO, cada extremo aporta la mitad de
    su descuento (promedio): asi dos tapas iguales dan el mismo resultado de antes.
    Devuelve (valor_mm, explicacion) o (None, motivo).
    """
    if altura is None:
        return None, "Falta la altura del filtro"

    d1, l1 = _descuento_tapa(reglas, tipo1, texto_tapa)
    if distintas and (texto_tapa_2 or tipo2):
        d2, l2 = _descuento_tapa(reglas, tipo2, texto_tapa_2)
        desc = (d1 + d2) / 2
        de_donde = f"tapas distintas: {l1} + {l2}, descuento por extremo"
    else:
        desc = d1
        de_donde = ("tapa " + l1) if (texto_tapa or tipo1) else "sin dato de tapa, descuento general"

    v = evaluar(reglas.formulas["altura_estructuras"]["formula"],
                reglas._entorno({"altura": altura, "descuento": desc}))
    return v, f"{altura:g} - {desc:g} ({de_donde})"


def tipo_elemento_de(reglas, codigo):
    """(clave, definicion) del tipo de elemento cuyo 'codigo_regex' matchea el
    código del plano, o (None, {}). Ej.: 'Y-6-40' -> ('filtro_y', {...})."""
    if not codigo:
        return None, {}
    cod = str(codigo).strip()
    for clave, d in (reglas.tipos_elemento or {}).items():
        patron = d.get("codigo_regex")
        if not patron:
            continue
        try:
            if re.search(patron, cod, re.IGNORECASE):
                return clave, d
        except re.error:
            continue
    return None, {}


def lleva(v):
    """True si el campo indica que la estructura SÍ está presente. '-' (y vacío /
    'no' / 'no lleva') significan 'no lleva', igual que en la interfaz. Evita que
    una estructura ausente (ej. núcleo intermedio '-') se cuente como presente."""
    if v is None:
        return False
    s = str(v).strip().lower()
    return s not in ("", "-", "--", "no", "no lleva", "n/a", "na", "sin", "0")


def _estructura_principal(reglas, presentes):
    """
    Cual de las estructuras presentes es la MAS ALTA (la principal).
    'presentes' es una lista de dicts {pref, conformado, tipo_chapa, espesor}.
    Las reglas estan documentadas en reglas.yaml (seccion 1b).
    """
    orden = reglas.orden_importancia

    def prio(e):
        # mas importante = numero mas alto
        try:
            return len(orden) - orden.index(e["pref"])
        except ValueError:
            return 0

    def es_desplegado(e):
        return e.get("tipo_chapa") == "desplegado"

    def es_espiralado(e):
        return (e.get("conformado") or "") == "espiralado"

    # Metal desplegado: si TODAS son desplegado, vale la importancia NI>JA>NU.
    if all(es_desplegado(e) for e in presentes):
        return max(presentes, key=prio)

    # El metal desplegado nunca es la principal: se lo saca de los candidatos.
    cand = [e for e in presentes if not es_desplegado(e)] or presentes

    # La rolada (todo lo que no es espiralado) siempre gana a la espiralada.
    rolados = [e for e in cand if not es_espiralado(e)]
    if rolados:
        # mayor espesor; si empatan, la mas importante (NI > JA > NU)
        return max(rolados, key=lambda e: ((e.get("espesor") or 0), prio(e)))

    # Solo espiraladas: importancia NI > JA > NU.
    return max(cand, key=prio)


def alturas_estructuras(reglas, d):
    """
    Altura de CADA estructura presente. La principal (la mas alta) lleva la
    altura base (altura del filtro menos el descuento por tapa); las demas,
    'descuento_estructura_no_principal_mm' menos.

    Devuelve (base, {pref: {'valor', 'principal'}}, principal_pref, explicacion).

    Si la orden trae 'altura_estructuras_manual' (cargada a mano por ingenieria),
    ese valor manda: es la altura de la mas alta y las demas van 1 mm menos.
    """
    _distintas = bool(d.get("tapas_distintas"))
    _, _tipo = tipo_elemento_de(reglas, d.get("codigo"))
    manual = d.get("altura_estructuras_manual")
    if manual not in (None, ""):
        try:
            base, expl = float(manual), "Cargada a mano"
        except (TypeError, ValueError):
            base, expl = None, "Altura manual inválida"
    elif _tipo.get("sin_tapas"):
        # Filtro Y (y similares SIN tapas): la altura del plano ES la de la jaula,
        # NO se le descuenta nada de tapa. Solo aplica a los tipos que lo declaran.
        if d.get("altura") in (None, ""):
            base, expl = None, "Falta la altura del filtro"
        else:
            base, expl = d.get("altura"), (
                f"{_tipo.get('nombre', 'Elemento')}: sin tapas, la altura del plano "
                "es la de la jaula")
    else:
        base, expl = altura_estructuras(reglas, d.get("altura"), d.get("tapa"),
                                        d.get("tapa_2"), d.get("tapa_tipo"),
                                        d.get("tapa_2_tipo"), _distintas)
    if base is None:
        return None, {}, None, expl

    presentes = []
    for pref in ("nucleo", "nucleo_int", "jaula"):
        if lleva(d.get(f"{pref}_crudo")):
            presentes.append({
                "pref": pref,
                "conformado": d.get(f"{pref}_conformado"),
                "tipo_chapa": d.get(f"{pref}_tipo_chapa"),
                "espesor": d.get(f"{pref}_espesor"),
            })
    if not presentes:
        return base, {}, None, expl

    ppal = _estructura_principal(reglas, presentes)["pref"]
    baja = reglas.parametros.get("descuento_estructura_no_principal_mm", 1)
    alturas = {}
    for e in presentes:
        es_ppal = e["pref"] == ppal
        alturas[e["pref"]] = {"valor": base if es_ppal else base - baja,
                              "principal": es_ppal}
    return base, alturas, ppal, expl


def consumo_espiralado(reglas, pieza, altura_mm, material):
    """Consumo en gramos de una chapa espiralada, por lookup en la tabla."""
    if altura_mm is None:
        return None, "Falta la altura"
    molde = reglas.buscar_molde(pieza.get("tipo_chapa") or "perforado",
                                pieza.get("molde") or pieza.get("diam"))
    if not molde or "peso_galvanizada" not in molde:
        return None, "Sin molde en tabla"
    col = reglas.canonico("material", material) or "galvanizada"
    peso_m = molde["peso_galvanizada"] if col == "galvanizada" else molde["peso_hojalata"]
    v = reglas.aplicar("consumo_chapa_espiralada",
                       peso_por_metro=peso_m, altura_mm=altura_mm)
    return v, f"{peso_m:g} g/m ({col}) x {altura_mm:g} mm"


# =============================================================================
#  Optimizador de corte
#
#  Las piezas de una estructura son rectangulos iguales de
#  (desarrollo x altura). Se busca acomodar la mayor cantidad posible en una
#  chapa rectangular. Como la separacion es 0 (guillotina no resta material),
#  es un empaque de rectangulos iguales con cortes tipo guillotina.
#
#  El resultado es una lista de piezas con su posicion, para poder dibujar el
#  plano de corte, mas cuantas chapas hacen falta para la cantidad pedida.
# =============================================================================

def _empacar(W, H, w, h, rotar, sep=0.0, prof=3):
    """
    Acomoda rectangulos w x h dentro de W x H con cortes guillotina.
    Devuelve [(x, y, ancho, alto), ...]. 'sep' se suma al tamano de la pieza.
    """
    pw0, ph0 = w + sep, h + sep
    if W + 1e-9 < min(pw0, ph0) or H + 1e-9 < min(pw0, ph0):
        return []

    orientaciones = [(pw0, ph0)]
    if rotar:
        orientaciones.append((ph0, pw0))

    mejor = []
    for pw, ph in orientaciones:
        if pw > W + 1e-9 or ph > H + 1e-9:
            continue
        cols = int((W + 1e-9) // pw)
        filas = int((H + 1e-9) // ph)
        # el rectangulo dibujado es del tamano ORIENTADO (sin la separacion),
        # no del original: si la pieza va girada, ocupa ph x pw
        piezas = [(c * pw, r * ph, pw - sep, ph - sep)
                  for r in range(filas) for c in range(cols)]

        # franja derecha (guillotina vertical), a la altura completa
        franja_der = W - cols * pw
        if prof > 0 and franja_der >= min(pw0, ph0) - 1e-9:
            for x, y, a, b in _empacar(franja_der, H, w, h, rotar, sep, prof - 1):
                piezas.append((cols * pw + x, y, a, b))
        # franja inferior, solo bajo el bloque principal
        franja_inf = H - filas * ph
        if prof > 0 and franja_inf >= min(pw0, ph0) - 1e-9:
            for x, y, a, b in _empacar(cols * pw, franja_inf, w, h, rotar, sep, prof - 1):
                piezas.append((x, filas * ph + y, a, b))

        if len(piezas) > len(mejor):
            mejor = piezas
    return mejor


def optimizar_corte(reglas, ancho_chapa, alto_chapa, desarrollo, altura,
                    cantidad=None, rotar=True):
    """
    Plano de corte para una estructura.
    ancho/alto_chapa: la chapa de origen.  desarrollo x altura: la pieza.
    Devuelve las piezas ubicadas, cuantas entran, y cuantas chapas hacen falta.
    """
    if not (ancho_chapa and alto_chapa and desarrollo and altura):
        return {"error": "Faltan medidas (chapa, desarrollo o altura)"}
    if desarrollo > max(ancho_chapa, alto_chapa) + 1e-9 or \
       altura > max(ancho_chapa, alto_chapa) + 1e-9:
        return {"error": "La pieza no entra en la chapa ni girada"}

    sep = reglas.parametros.get("separacion_corte_mm", 0) or 0
    piezas = _empacar(ancho_chapa, alto_chapa, desarrollo, altura, rotar, sep)
    por_chapa = len(piezas)
    if por_chapa == 0:
        return {"error": "La pieza no entra en la chapa"}

    area_chapa = ancho_chapa * alto_chapa
    area_util = por_chapa * desarrollo * altura
    resultado = {
        "ancho_chapa": ancho_chapa, "alto_chapa": alto_chapa,
        "pieza_ancho": desarrollo, "pieza_alto": altura,
        "piezas_por_chapa": por_chapa,
        "aprovechamiento": round(area_util / area_chapa * 100, 1),
        "rotar": rotar,
        "piezas": [{"x": round(x, 1), "y": round(y, 1),
                    "w": round(a, 1), "h": round(b, 1)} for x, y, a, b in piezas],
    }
    if cantidad:
        import math
        chapas = math.ceil(cantidad / por_chapa)
        resultado.update({
            "cantidad": cantidad, "chapas_necesarias": chapas,
            "sobran": chapas * por_chapa - cantidad,
        })
    return resultado


def optimizar_multiple(reglas, ancho_chapa, alto_chapa, piezas, rotar=True):
    """
    Acomoda VARIAS piezas distintas (nucleo, jaula, ...), cada una con su
    cantidad, en la menor cantidad de chapas. Solo se cortan las piezas
    pedidas: no se llena la chapa al maximo.

    'piezas': lista de dicts {label, desarrollo, altura, cantidad}.

    Empaque por estantes (First-Fit por altura). Con las piezas de un filtro,
    que comparten la altura de la estructura, cada estante es una fila y entran
    varias piezas de distinto desarrollo en la misma chapa.
    """
    eps = 1e-9
    if not (ancho_chapa and alto_chapa):
        return {"error": "Falta la medida de la chapa"}
    W, H = float(ancho_chapa), float(alto_chapa)

    # expandir a unidades individuales
    unidades = []
    for p in piezas:
        w, h, n = p.get("desarrollo"), p.get("altura"), int(p.get("cantidad") or 0)
        if not (w and h) or n <= 0:
            continue
        for _ in range(n):
            unidades.append([p.get("label", ""), float(w), float(h)])
    if not unidades:
        return {"error": "No hay piezas para cortar (falta desarrollo, altura o cantidad)"}

    # ninguna pieza puede ser mas grande que la chapa (probando el giro)
    for label, w, h in unidades:
        cabe = (w <= W + eps and h <= H + eps) or (rotar and h <= W + eps and w <= H + eps)
        if not cabe:
            return {"error": f"La pieza {label} ({w:g}x{h:g}) no entra en la chapa ni girada"}

    # orientacion base: ancha y baja, para apilar mas estantes por chapa
    def orientar(w, h):
        return (w, h) if (not rotar or w >= h) else (h, w)
    unidades = [[label, *orientar(w, h)] for label, w, h in unidades]
    unidades.sort(key=lambda u: (-u[2], -u[1]))   # las mas altas primero

    hojas = []   # cada hoja: {estantes:[{y,h,x}], alto_usado, piezas:[]}
    for label, w, h in unidades:
        colocada = False
        for hoja in hojas:
            for est in hoja["estantes"]:
                if h <= est["h"] + eps and est["x"] + w <= W + eps:
                    hoja["piezas"].append({"label": label, "x": est["x"], "y": est["y"],
                                           "w": w, "h": h})
                    est["x"] += w
                    colocada = True
                    break
            if colocada:
                break
            if hoja["alto_usado"] + h <= H + eps and w <= W + eps:
                y = hoja["alto_usado"]
                hoja["estantes"].append({"y": y, "h": h, "x": w})
                hoja["alto_usado"] += h
                hoja["piezas"].append({"label": label, "x": 0.0, "y": y, "w": w, "h": h})
                colocada = True
                break
        if not colocada:
            if w > W + eps or h > H + eps:      # no entra en base: va girada
                w, h = h, w
            hojas.append({"estantes": [{"y": 0.0, "h": h, "x": w}], "alto_usado": h,
                          "piezas": [{"label": label, "x": 0.0, "y": 0.0, "w": w, "h": h}]})

    area_chapa = W * H
    area_util = sum(p["w"] * p["h"] for hoja in hojas for p in hoja["piezas"])
    return {
        "ancho_chapa": W, "alto_chapa": H, "rotar": rotar,
        "chapas_necesarias": len(hojas),
        "piezas_total": len(unidades),
        "aprovechamiento": round(area_util / (area_chapa * len(hojas)) * 100, 1) if hojas else 0,
        "hojas": [{"piezas": [{"label": p["label"], "x": round(p["x"], 1), "y": round(p["y"], 1),
                               "w": round(p["w"], 1), "h": round(p["h"], 1)}
                              for p in hoja["piezas"]]} for hoja in hojas],
    }


# Instancia unica, para no releer el archivo en cada consulta.
_cache = None

def cargar(recargar=False):
    global _cache
    if _cache is None or recargar:
        _cache = Reglas()
    return _cache
