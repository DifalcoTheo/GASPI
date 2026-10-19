# -*- coding: utf-8 -*-
"""
Lector de planos.

No trabaja por posiciones fijas ni busca texto suelto: usa la GEOMETRIA del
plano, que es lo unico consistente entre planos viejos.

Lo que se verifico contra los planos reales (5 de 5):

  - El CODIGO es el texto de mayor tamano de letra, arriba de todo.
  - La ALTURA es la unica cota vertical del borde derecho.
  - Las NOTAS estan en la columna izquierda; las COTAS en el centro/derecha.
  - De las cotas horizontales de arriba, la de mas arriba es el Ø externo
    y la de abajo el Ø interno (SolidWorks apila la cota mayor mas afuera).
  - Las lineas de una misma nota estan a ~1,2 alturas de letra; entre notas
    distintas, al menos al doble.

Todo lo que devuelve trae de donde salio y con que confianza, para que la
pantalla pueda pedir confirmacion en vez de dar por bueno lo que dedujo.
"""

import re
from dataclasses import dataclass, field, asdict

import pymupdf

import reglas as R


# =============================================================================
#  Elementos del plano
# =============================================================================

@dataclass
class Item:
    """Un texto del plano, con donde esta y como esta escrito."""
    texto: str
    x: float
    y: float
    x1: float
    y1: float
    tamano: float
    vertical: bool

    def dict(self):
        return asdict(self)


@dataclass
class Dato:
    """Un dato leido: el valor, de donde salio y si hay que confirmarlo."""
    valor: object = None
    origen: str = ""          # el texto del plano del que se dedujo
    confianza: str = "baja"   # alta | media | baja
    motivo: str = ""          # por que se dedujo eso

    def dict(self):
        return asdict(self)


def _leer_items(pagina):
    """Todos los textos de la pagina, con su geometria."""
    items = []
    for b in pagina.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            texto = "".join(s["text"] for s in l["spans"]).strip()
            if not texto:
                continue
            x0, y0, x1, y1 = l["bbox"]
            items.append(Item(
                texto=texto, x=round(x0, 1), y=round(y0, 1),
                x1=round(x1, 1), y1=round(y1, 1),
                tamano=round(l["spans"][0]["size"], 1),
                vertical=abs(l["dir"][1]) > 0.3,
            ))
    return items


# =============================================================================
#  Separar cotas de notas
# =============================================================================

RX_SOLO_NUMERO = re.compile(r"^Ø?\s*\d+(?:[.,]\d+)?\s*(?:mm)?$", re.I)


def _es_cota(item, ancho_pagina):
    """Una cota es un numero solo, y no esta en la columna de notas."""
    if not RX_SOLO_NUMERO.match(item.texto):
        return False
    return item.x > ancho_pagina * 0.25


def _interlineado(items):
    """
    Deduce el interlineado real del plano, en vez de suponerlo.

    Dentro de una nota el salto entre lineas es constante (SolidWorks usa el
    mismo interlineado en todo el plano). Entre notas distintas siempre es
    mayor. Tomando la mediana de los saltos chicos se obtiene el interlineado
    exacto de ESE plano, y con eso se separan las notas sin numeros magicos.
    """
    saltos = []
    for a, b in zip(items, items[1:]):
        if abs(b.x - a.x) < 25 and 0 < b.y - a.y < max(a.tamano, b.tamano) * 1.4:
            saltos.append(b.y - a.y)
    if len(saltos) < 3:
        return None
    saltos.sort()
    return saltos[len(saltos) // 2]


def _agrupar_notas(items):
    """
    Junta las lineas que forman una misma nota.
    Dos lineas son de la misma nota si estan en la misma columna y separadas
    por poco mas del interlineado del plano.
    """
    if not items:
        return []
    items = sorted(items, key=lambda i: (round(i.x / 30), i.y))

    interlineado = _interlineado(items)
    grupos, actual = [], [items[0]]

    for anterior, item in zip(items, items[1:]):
        mismo_x = abs(item.x - anterior.x) < 25
        salto = item.y - anterior.y
        if interlineado:
            # 1,4 interlineados: los saltos dentro de una nota dan 1,0 y los
            # saltos entre notas dan 1,5 o mas. Medido en planos reales.
            limite = interlineado * 1.4
        else:
            limite = max(anterior.tamano, item.tamano) * 1.25
        if mismo_x and 0 < salto <= limite:
            actual.append(item)
        else:
            grupos.append(actual)
            actual = [item]
    grupos.append(actual)

    notas = []
    for g in grupos:
        notas.append({
            "texto": " ".join(i.texto for i in g),
            "x": min(i.x for i in g), "y": min(i.y for i in g),
            "x1": max(i.x1 for i in g), "y1": max(i.y1 for i in g),
            "lineas": [i.dict() for i in g],
        })
    return notas


# =============================================================================
#  Interpretacion de las notas
# =============================================================================

RX_Q = re.compile(r"\bq\s*[:=]\s*(\d+)", re.I)
RX_HP = re.compile(r"\bhp\s*[:=]\s*(\d+(?:[.,]\d+)?)", re.I)
# Iguales pero incluyendo la unidad, para poder recortarlas del texto del medio
RX_Q_ENTERA = re.compile(r"\bq\s*[:=]\s*\d+\s*(?:mm)?", re.I)
RX_HP_ENTERA = re.compile(r"\bhp\s*[:=]\s*\d+(?:[.,]\d+)?\s*(?:mm)?", re.I)
RX_PERFORACION = re.compile(r"Ø\s*(\d+(?:[.,]\d+)?)", re.I)
RX_ESPESOR = re.compile(r"esp(?:esor)?\.?\s*:?\s*(\d+(?:[.,]\d+)?)", re.I)
RX_KGM2 = re.compile(r"(\d+(?:[.,]\d+)?)\s*kg\s*/?\s*m\s*2", re.I)
# El Ø a veces viene escrito con una O mayuscula
RX_MOLDE = re.compile(r"molde\s*[ØO]?\s*(\d+(?:[.,]\d+)?)", re.I)
RX_SIN_MOLDE = re.compile(r"sin\s+molde", re.I)
RX_MATERIAL = re.compile(
    r"\b(AISI\s*\d+|SAE\s*\d+|acero\s+\w+|electrocincad\w+|galvanizad\w+|"
    r"cincad\w+|hojalata|inox\w*)\b", re.I)

# Palabras que identifican de que habla cada nota
CLAVES = {
    # el intermedio va primero: "Nucleo intermedio" tambien dice "Nucleo"
    "nucleo_int": [r"n[uú]cleo\s+intermedio", r"nucleo\s+intermedio"],
    "nucleo":   [r"\bn[uú]cleo\b", r"\bnucleo\b"],
    "jaula":    [r"\bjaula\b"],
    "chapa":    [r"chapa perforada", r"chapa ranurada", r"metal desplegado",
                 r"chapa\b"],
    "tapa":     [r"\btapa[s]?\b"],
    "sello":    [r"junta", r"o'?ring", r"\boring\b"],
    "adhesivo": [r"adhesivo", r"pegamento", r"ep[oó]xi", r"poliuret"],
    "medio":    [r"mesh", r"microfibra", r"papel", r"fieltro", r"celulosic",
                 r"borosilicato", r"medio filtrante", r"reps\b", r"fibra de vidrio",
                 r"velo de vidrio", r"guata", r"punzonado", r"polyester",
                 r"aramida", r"carbon activado", r"goma espuma", r"espuma"],
}

# Una nota habla de UNA cosa. Si coincide con varias etiquetas, gana la
# primera de esta lista. Sin esto, "malla epoxidada" dentro del medio
# filtrante se lee como si fuera el adhesivo.
# "tapa" va antes que "sello": una nota de tapa puede nombrar la junta
# ("Tapa repujada inferior abierta sin junta"), pero una nota de junta no
# nombra la tapa.
PRIORIDAD = ["nucleo_int", "nucleo", "jaula", "tapa", "sello", "medio",
             "adhesivo", "chapa"]

# Una nota que dice "Adhesivo" o "Medio filtrante" con todas las letras habla
# de eso, aunque tambien nombre otra cosa. Sin esta regla, la nota
# "Medio filtrante rolado ... entre nucleo intermedio y jaula" se lee como si
# fuera el nucleo intermedio.
RX_ADHESIVO_LITERAL = re.compile(r"\badhesivo\b", re.I)
RX_MEDIO_LITERAL = re.compile(r"\bmedio filtrante\b", re.I)

# Una nota de tapa EMPIEZA con "tapa". Si solo la menciona al pasar
# ("2 remaches por tapa", "Pieza de sujecion ... soldada a las tapas"),
# no es la tapa: es una observacion.
RX_EMPIEZA_TAPA = re.compile(r"^\s*tapa", re.I)


def _de_que_habla(texto):
    """Devuelve las etiquetas que le corresponden a una nota."""
    t = R.normalizar(texto)
    etiquetas = []
    for etiqueta, patrones in CLAVES.items():
        if any(re.search(p, t, re.I) for p in patrones):
            etiquetas.append(etiqueta)
    if "tapa" in etiquetas and not RX_EMPIEZA_TAPA.search(t):
        etiquetas.remove("tapa")
    return etiquetas


def _etiqueta_principal(texto, etiquetas):
    """De que habla la nota, cuando coincide con mas de una etiqueta."""
    if not etiquetas:
        return None
    t = R.normalizar(texto)
    orden = list(PRIORIDAD)
    for literal, clave in ((RX_MEDIO_LITERAL, "medio"),
                           (RX_ADHESIVO_LITERAL, "adhesivo")):
        if literal.search(t):
            orden.remove(clave)
            orden.insert(0, clave)
    for e in orden:
        if e in etiquetas:
            return e
    return etiquetas[0]


def _pieza_desde_nota(texto, reg):
    """Saca material, conformado, espesor, etc. del texto de una nota."""
    d = {"material": None, "conformado": None, "tipo_chapa": None,
         "perforacion": None, "espesor": None, "kg_m2": None, "molde": None,
         "sin_molde": False}

    # "espiralado (sin molde)": se hace espiralado pero no sale de la tabla
    if RX_SIN_MOLDE.search(texto):
        d["sin_molde"] = True

    m = RX_MATERIAL.search(texto)
    if m:
        d["material"] = re.sub(r"\s+", " ", m.group(1)).strip()
    d["conformado"] = reg.canonico("conformado", texto)
    d["tipo_chapa"] = reg.canonico("tipo_chapa", texto)

    for clave, rx in (("perforacion", RX_PERFORACION), ("espesor", RX_ESPESOR),
                      ("kg_m2", RX_KGM2), ("molde", RX_MOLDE)):
        m = rx.search(texto)
        if m:
            d[clave] = float(m.group(1).replace(",", "."))

    if d["kg_m2"] is not None and not d["tipo_chapa"]:
        d["tipo_chapa"] = "desplegado"
    if d["perforacion"] is not None and not d["tipo_chapa"]:
        d["tipo_chapa"] = "perforado"
    return d


# =============================================================================
#  Lectura completa
# =============================================================================

MARCA_NORMALIZADO = "FT-v1"


def leer(ruta_pdf, reg=None):
    """
    Lee un plano y devuelve todo lo que pudo deducir.
    Nunca inventa: lo que no puede resolver queda con confianza baja y su
    motivo, para que la pantalla lo pregunte.
    """
    reg = reg or R.cargar()
    doc = pymupdf.open(ruta_pdf)
    pagina = doc[0]
    ancho, alto = pagina.rect.width, pagina.rect.height
    items = _leer_items(pagina)
    doc.close()

    resultado = {
        "paginas": 1, "ancho": ancho, "alto": alto,
        "normalizado": False, "items": [i.dict() for i in items],
        "cotas": [], "notas": [], "campos": {}, "sin_asignar": [],
    }
    if not items:
        resultado["error"] = ("El plano no tiene texto: puede ser un escaneo. "
                              "Hay que cargarlo a mano.")
        return resultado

    texto_todo = " ".join(i.texto for i in items)
    resultado["normalizado"] = MARCA_NORMALIZADO.lower() in texto_todo.lower()

    campos = resultado["campos"]

    # --- CODIGO: el texto mas grande, arriba de todo -------------------------
    mayor = max(i.tamano for i in items)
    candidatos = sorted([i for i in items if i.tamano >= mayor - 0.5],
                        key=lambda i: i.y)
    if candidatos:
        c = candidatos[0]
        campos["codigo"] = Dato(
            valor=c.texto.strip(), origen=c.texto, confianza="alta",
            motivo=f"Es el texto mas grande del plano ({c.tamano} pt), arriba de todo",
        ).dict()

    # --- TIPO DE ELEMENTO (por el codigo): acelera la lectura de cotas -------
    codigo_val = (campos.get("codigo") or {}).get("valor")
    tipo_key, tipo_def = R.tipo_elemento_de(reg, codigo_val)
    resultado["tipo_elemento"] = tipo_key
    if tipo_def.get("familia"):
        campos["familia"] = Dato(
            valor=tipo_def["familia"], origen=codigo_val, confianza="alta",
            motivo=f"{tipo_def.get('nombre', 'Elemento')} (por el código)").dict()

    # --- COTAS ---------------------------------------------------------------
    cotas = [i for i in items if _es_cota(i, ancho)]
    resultado["cotas"] = [i.dict() for i in cotas]

    verticales = sorted([c for c in cotas if c.vertical], key=lambda c: -c.x)
    horizontales = [c for c in cotas if not c.vertical]

    def num(t):
        m = re.search(r"(\d+(?:[.,]\d+)?)", t)
        return float(m.group(1).replace(",", ".")) if m else None

    # ALTURA: la cota vertical mas a la derecha
    if verticales:
        v = verticales[0]
        campos["altura"] = Dato(
            valor=num(v.texto), origen=v.texto,
            confianza="alta" if v.x > ancho * 0.8 else "media",
            motivo="Unica cota vertical del borde derecho" if len(verticales) == 1
                   else f"Cota vertical mas a la derecha (hay {len(verticales)} verticales)",
        ).dict()

    # DIAMETROS: las cotas horizontales se separan por altura en la hoja.
    #   arriba (primer 15%)  -> Ø externo y Ø interno del cartucho
    #   al medio (15% a 80%) -> Ø interno del nucleo (la cota del medio del dibujo)
    #   abajo (ultimo 20%)   -> junta, espesores: no se asignan solas
    if horizontales:
        ordenadas = sorted(horizontales, key=lambda c: c.y)
        grupo, medio_dibujo, resto = [ordenadas[0]], [], []
        for c in ordenadas[1:]:
            if c.y - grupo[-1].y < alto * 0.15 and not medio_dibujo:
                grupo.append(c)
            elif c.y < alto * 0.80:
                medio_dibujo.append(c)
            else:
                resto.append(c)

        # La cota que queda en el medio del dibujo es el Ø interno del nucleo.
        # No esta en todos los planos; cuando esta, es este.
        if len(medio_dibujo) == 1:
            c = medio_dibujo[0]
            campos["nucleo_diam"] = Dato(
                valor=num(c.texto), origen=c.texto, confianza="media",
                motivo="Cota suelta en el medio del dibujo: es el Ø interno del nucleo. "
                       "Confirmalo.").dict()
        else:
            for c in medio_dibujo:
                resultado["sin_asignar"].append(
                    {"texto": c.texto, "x": c.x, "y": c.y, "tipo": "cota",
                     "nota": "Hay varias cotas en el medio del dibujo: "
                             "indicá cuál es el Ø interno del núcleo."})

        if len(grupo) >= 2:
            ext, inte = grupo[0], grupo[1]
            v_ext, v_int = num(ext.texto), num(inte.texto)
            coherente = v_ext is not None and v_int is not None and v_ext > v_int
            campos["od"] = Dato(
                valor=v_ext, origen=ext.texto,
                confianza="alta" if coherente else "baja",
                motivo="Cota horizontal de mas arriba" + (
                    "" if coherente else " — PERO da menor que la de abajo, revisar"),
            ).dict()
            campos["id_int"] = Dato(
                valor=v_int, origen=inte.texto,
                confianza="alta" if coherente else "baja",
                motivo="Cota horizontal inmediatamente debajo del Ø externo" + (
                    "" if coherente else " — PERO da mayor que la de arriba, revisar"),
            ).dict()
            for c in grupo[2:]:
                resultado["sin_asignar"].append(
                    {"texto": c.texto, "x": c.x, "y": c.y, "tipo": "cota",
                     "nota": "Tercera cota del grupo superior"})
        elif len(grupo) == 1:
            c = grupo[0]
            destino = tipo_def.get("cota_horizontal_unica")
            if destino in ("od", "id_int"):
                campos[destino] = Dato(
                    valor=num(c.texto), origen=c.texto, confianza="alta",
                    motivo=f"{tipo_def.get('nombre', 'Elemento')}: única cota horizontal = "
                           + ("Ø externo" if destino == "od" else "Ø interno"),
                ).dict()
            else:
                resultado["sin_asignar"].append(
                    {"texto": c.texto, "x": c.x, "y": c.y,
                     "tipo": "cota", "nota": "Unica cota horizontal: no se sabe si es ext o int"})

        for c in resto:
            resultado["sin_asignar"].append(
                {"texto": c.texto, "x": c.x, "y": c.y, "tipo": "cota",
                 "nota": "Cota de la parte de abajo (junta, espesor u otro)"})

    # Segundas cotas verticales: pueden ser espesores, no la altura
    for otra in verticales[1:]:
        resultado["sin_asignar"].append(
            {"texto": otra.texto, "x": otra.x, "y": otra.y,
             "tipo": "cota vertical", "nota": "Otra cota vertical sin asignar"})

    # --- NOTAS ---------------------------------------------------------------
    notas = _agrupar_notas([i for i in items if i not in cotas
                            and i.tamano < mayor - 0.5])
    resultado["notas"] = notas

    ambiguas = []
    tapas, sellos, medios, sueltas = [], [], [], []
    avisos_medio = []
    for n in notas:
        texto = n["texto"]
        etiquetas = _de_que_habla(texto)
        principal = _etiqueta_principal(texto, etiquetas)
        n["etiquetas"] = etiquetas
        n["principal"] = principal

        if principal == "adhesivo":
            if "adhesivo" not in campos:
                campos["adhesivo"] = Dato(
                    valor=re.sub(r"(?i)^adhesivo\s*", "", texto).strip() or texto,
                    origen=texto, confianza="alta",
                    motivo="La nota dice 'Adhesivo'").dict()

        elif principal == "sello":
            sellos.append(texto)

        elif principal == "tapa":
            tapas.append(texto)

        elif principal == "medio":
            # el medio filtrante se copia EXACTO, nunca se interpreta;
            # solo se le sacan el q y el Hp, que son datos aparte. Se guardan POR
            # MEDIO: si el medio es ROLADO, la "Q:" del plano es la cantidad de
            # VUELTAS (no pliegues); si es plisado, Q son pliegues y Hp la altura.
            mq = RX_Q.search(texto)
            mhp = RX_HP.search(texto)
            q_val = int(mq.group(1)) if mq else None
            hp_val = float(mhp.group(1).replace(",", ".")) if mhp else None
            limpio = RX_HP_ENTERA.sub("", RX_Q_ENTERA.sub("", texto))
            limpio = re.sub(r"\s{2,}", " ", limpio).strip(" -·")

            # se reconoce contra la lista de medios y se revisa que este completo
            reconocido, avisos = reg.reconocer_medio(limpio)
            avisos_medio.extend(avisos)
            forma = reg.canonico("tipo_medio", limpio)
            if reconocido and forma and not reg.forma_admitida(reconocido, forma):
                avisos_medio.append({
                    "id": "medio_forma_no_admitida", "nivel": "avisa",
                    "mensaje": f"El plano pide '{reconocido['nombre']}' {forma}, pero en la "
                               f"lista ese medio figura solo como "
                               f"{' y '.join(reconocido['formas'])}.",
                })
            medios.append({"texto": limpio, "forma": forma, "q": q_val, "hp": hp_val,
                           "reconocido": reconocido["nombre"] if reconocido else None})

        # --- nucleo / jaula: lo unico que no se puede resolver por texto -----
        elif principal == "nucleo_int" and "nucleo_int" not in campos:
            campos["nucleo_int"] = Dato(
                valor=_pieza_desde_nota(texto, reg), origen=texto,
                confianza="alta", motivo="La nota dice 'Nucleo intermedio'").dict()
        elif principal == "nucleo" and "nucleo" not in campos:
            campos["nucleo"] = Dato(
                valor=_pieza_desde_nota(texto, reg), origen=texto,
                confianza="alta", motivo="La nota dice 'Nucleo'").dict()
        elif principal == "jaula" and "jaula" not in campos:
            campos["jaula"] = Dato(
                valor=_pieza_desde_nota(texto, reg), origen=texto,
                confianza="alta", motivo="La nota dice 'Jaula'").dict()
        elif principal == "chapa":
            ambiguas.append(n)
        elif principal is None:
            # No habla de nada conocido ("2 remaches por tapa", "Pieza de
            # sujecion"...). No se tira: va a observaciones, copiada exacta.
            sueltas.append(texto)

    # Tapas, sellos y medios pueden venir de a dos (superior/inferior, o un
    # filtro de dos etapas). Se guardan los dos, en el orden en que aparecen
    # de arriba hacia abajo en el plano.
    def _de_a_dos(lista, clave, etiqueta):
        if not lista:
            return
        campos[clave] = Dato(
            valor=lista[0], origen=lista[0], confianza="alta",
            motivo=f"La nota nombra {etiqueta}" + (
                f" (es la primera de {len(lista)})" if len(lista) > 1 else "")).dict()
        if len(lista) > 1:
            campos[clave + "_2"] = Dato(
                valor=lista[1], origen=lista[1], confianza="alta",
                motivo=f"Segunda nota de {etiqueta} del plano").dict()
        for extra in lista[2:]:
            resultado["sin_asignar"].append(
                {"texto": extra, "x": 0, "y": 0, "tipo": clave,
                 "nota": f"Tercera nota de {etiqueta}: asignala vos."})

    _de_a_dos(tapas, "tapa", "una tapa")
    _de_a_dos(sellos, "sello", "una junta u O'ring")

    if medios:
        # Cada medio con su forma y sus datos (POR MEDIO): si es rolado la "Q:"
        # del plano son las VUELTAS; si es plisado, Q = pliegues y Hp = altura.
        for i, suf in ((0, ""), (1, "_2"), (2, "_3")):
            if i >= len(medios):
                break
            med = medios[i]
            campos["medio" + suf] = Dato(
                valor=med["texto"], origen=med["texto"], confianza="alta",
                motivo=(f"Medio filtrante {i + 1} del plano" if i else "La nota nombra un medio filtrante") + (
                    f" — {med['reconocido']}" if med["reconocido"]
                    else ("" if i else " (no figura en la lista de medios: revisar)"))).dict()
            if med["forma"]:
                campos["tipo_medio" + suf] = Dato(
                    valor=med["forma"], origen=med["texto"],
                    confianza="alta", motivo="Lo dice la nota del medio filtrante").dict()
            if med["forma"] == "rolado":
                if med["q"] is not None:
                    campos["vueltas" + suf] = Dato(
                        valor=med["q"], origen=f"Q: {med['q']}", confianza="media",
                        motivo="En un medio rolado la 'Q:' del plano es la cantidad de vueltas").dict()
            else:   # plisado (o sin forma): Q = pliegues, Hp = altura de pliegue
                if med["q"] is not None:
                    campos["q" + suf] = Dato(
                        valor=med["q"], origen=f"Q: {med['q']}", confianza="alta",
                        motivo="Etiqueta 'Q:' (pliegues) del medio").dict()
                if med["hp"] is not None:
                    campos["hp" + suf] = Dato(
                        valor=med["hp"], origen=f"Hp: {med['hp']}", confianza="alta",
                        motivo="Etiqueta 'Hp:' (altura de pliegue) del medio").dict()
        for extra in medios[3:]:
            resultado["sin_asignar"].append(
                {"texto": extra["texto"], "x": 0, "y": 0, "tipo": "medio",
                 "nota": "Cuarto medio filtrante: asignalo vos."})

    # Notas que no hablan de nada conocido: se copian a observaciones.
    if sueltas:
        campos["observaciones"] = Dato(
            valor="\n".join(sueltas), origen="\n".join(sueltas), confianza="alta",
            motivo=f"{len(sueltas)} nota(s) del plano que no corresponden a ningun "
                   f"campo. Se copian tal cual.").dict()

    resultado["avisos"] = avisos_medio

    # El molde de la nota y el Ø interno acotado tienen que darse la mano
    nuc = (campos.get("nucleo") or {}).get("valor") or {}
    molde = nuc.get("molde")
    diam = (campos.get("nucleo_diam") or {}).get("valor")
    if molde and diam and abs(float(molde) - float(diam)) > 3:
        resultado["avisos"].append({
            "id": "molde_no_coincide_con_cota", "nivel": "avisa",
            "mensaje": f"La nota dice molde Ø{molde:g} y la cota del medio del dibujo "
                       f"dice Ø{diam:g}. Verificar cuál es el Ø interno del núcleo.",
        })

    # Chapas sin nombre propio: no se puede saber si son nucleo o jaula.
    # No se adivina: se devuelven para que la persona las asigne con un click.
    for n in ambiguas:
        resultado["sin_asignar"].append({
            "texto": n["texto"], "x": n["x"], "y": n["y"], "tipo": "chapa",
            "nota": "No dice si es nucleo o jaula. Asignala vos.",
            "datos": _pieza_desde_nota(n["texto"], reg),
        })

    # --- lo que falta --------------------------------------------------------
    # Si el tipo de elemento define sus cotas necesarias (ej. filtro Y: no lleva
    # núcleo/jaula/pliegues), se piden SOLO esas; si no, la lista genérica.
    esperados = tipo_def.get("cotas_necesarias") or [
        "codigo", "altura", "od", "id_int", "medio", "q", "hp"]
    resultado["faltantes"] = [c for c in esperados if c not in campos]

    return resultado


def a_producto(lectura):
    """Convierte una lectura en los campos de la ficha de producto."""
    c = lectura["campos"]
    val = lambda k: (c.get(k) or {}).get("valor")
    org = lambda k: (c.get(k) or {}).get("origen")
    p = {
        "codigo": (val("codigo") or "").strip() or None,
        "familia": val("familia"),
        "altura": val("altura"), "od": val("od"), "id_int": val("id_int"),
        "medio": val("medio"), "medio_2": val("medio_2"), "medio_3": val("medio_3"),
        "tipo_medio": val("tipo_medio") or ("plisado" if val("q") else None),
        "tipo_medio_2": val("tipo_medio_2"), "tipo_medio_3": val("tipo_medio_3"),
        # Q/Hp POR MEDIO (plisado) y vueltas POR MEDIO (rolado).
        "q": val("q"), "hp": val("hp"), "vueltas": val("vueltas"),
        "q_2": val("q_2"), "hp_2": val("hp_2"), "vueltas_2": val("vueltas_2"),
        "q_3": val("q_3"), "hp_3": val("hp_3"), "vueltas_3": val("vueltas_3"),
        "q_crudo": org("q"), "hp_crudo": org("hp"),
        "tapa": val("tapa"), "tapa_2": val("tapa_2"),
        "sello": val("sello"), "sello_2": val("sello_2"),
        "adhesivo": val("adhesivo"), "observaciones": val("observaciones"),
    }
    # Las piezas: los campos interpretados MAS el texto original del plano
    for pieza in ("nucleo", "jaula", "nucleo_int"):
        d = val(pieza) or {}
        for k, v in d.items():
            p[f"{pieza}_{k}"] = v
        p[f"{pieza}_crudo"] = org(pieza)
        p.setdefault(f"{pieza}_diam", None)

    # El Ø interno del nucleo es la cota del medio del dibujo, cuando esta
    if val("nucleo_diam") is not None:
        p["nucleo_diam"] = val("nucleo_diam")
    return p


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(leer(sys.argv[1]), indent=2, ensure_ascii=False))
