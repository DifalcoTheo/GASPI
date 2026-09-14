-- =============================================================================
--  SISTEMA DE ORDENES DE FABRICACION - ESQUEMA
--
--  Modelo acordado:
--     LOTE (SPF-1038)  ->  LINEA (SPF-1038-7)  ->  PRODUCTO (SP2022)
--
--  El LOTE agrupa. La LINEA es lo que se produce (un plano, un producto,
--  una cantidad, un estado). El PRODUCTO es la ficha tecnica reutilizable:
--  vive por codigo y se reusa en todas las lineas que lo fabriquen.
--
--  Principio general: nunca se pierde el dato original. Todo campo
--  interpretado guarda al lado el texto crudo del que salio (*_crudo).
-- =============================================================================


-- -----------------------------------------------------------------------------
--  PRODUCTO - la ficha tecnica, por codigo. Es lo que se reutiliza.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS producto (
    codigo              TEXT PRIMARY KEY,   -- SP1949, SP1056L, HC1151, TH4-PM100 SAE
    familia             TEXT NOT NULL DEFAULT 'cartucho',
    variante_de         TEXT,               -- SP1398H -> SP1398 (variante/modificacion)

    -- Dimensiones generales (salen de las cotas del plano)
    altura              REAL,
    od                  REAL,               -- diametro externo
    id_int              REAL,               -- diametro interno

    -- Medio filtrante (texto libre: se copia exacto, nunca se interpreta)
    medio               TEXT,
    soporte             TEXT,
    tipo_medio          TEXT,               -- plisado | rolado
    medio_2             TEXT,               -- filtros de varias etapas
    tipo_medio_2        TEXT,
    medio_3             TEXT,
    tipo_medio_3        TEXT,
    q                   INTEGER,            -- cantidad de pliegues
    hp                  REAL,               -- altura de pliegue
    vueltas             REAL,               -- si es rolado
    q_crudo             TEXT,
    hp_crudo            TEXT,

    -- Tapas, sello y adhesivo (texto libre, tal como figura en el plano)
    tapa                TEXT,
    tapa_2              TEXT,
    sello               TEXT,
    sello_2             TEXT,
    adhesivo            TEXT,
    observaciones       TEXT,               -- notas del plano sin campo propio

    -- Estructuras: nucleo, nucleo intermedio y jaula.
    -- conformado: espiralado | rolado    tipo_chapa: perforado | ranurado | desplegado
    nucleo_crudo        TEXT,
    nucleo_material     TEXT,
    nucleo_conformado   TEXT,
    nucleo_tipo_chapa   TEXT,
    nucleo_perforacion  REAL,               -- diametro de la perforacion
    nucleo_espesor      REAL,
    nucleo_kg_m2        REAL,               -- si es metal desplegado
    nucleo_molde        REAL,               -- si es espiralado
    nucleo_sin_molde    INTEGER,            -- el plano dice "espiralado (sin molde)"
    nucleo_diam         REAL,               -- diametro interno declarado
    nucleo_diam_crudo   TEXT,

    nucleo_int_crudo    TEXT,
    nucleo_int_material TEXT,
    nucleo_int_conformado TEXT,
    nucleo_int_tipo_chapa TEXT,
    nucleo_int_perforacion REAL,
    nucleo_int_espesor  REAL,
    nucleo_int_kg_m2    REAL,
    nucleo_int_molde    REAL,
    nucleo_int_sin_molde INTEGER,
    nucleo_int_diam     REAL,

    jaula_crudo         TEXT,
    jaula_material      TEXT,
    jaula_conformado    TEXT,
    jaula_tipo_chapa    TEXT,
    jaula_perforacion   REAL,
    jaula_espesor       REAL,
    jaula_kg_m2         REAL,
    jaula_molde         REAL,
    jaula_sin_molde     INTEGER,
    jaula_diam          REAL,
    jaula_diam_crudo    TEXT,

    -- Trazabilidad de la ficha
    origen              TEXT,               -- de donde salio (import, plano, manual)
    revisar             INTEGER NOT NULL DEFAULT 0,
    nota_revision       TEXT,
    reglas_version      INTEGER,            -- con que version de reglas se calculo
    creado              TEXT,
    actualizado         TEXT
);

CREATE INDEX IF NOT EXISTS ix_producto_familia  ON producto(familia);
CREATE INDEX IF NOT EXISTS ix_producto_revisar  ON producto(revisar);


-- -----------------------------------------------------------------------------
--  LOTE - SPF-1038. Agrupa lineas. El cliente vive aca.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lote (
    codigo              TEXT PRIMARY KEY,   -- SPF-1038, SPF-890R, CSPF-036
    cliente             TEXT,               -- o STOCK
    fecha_oc            TEXT,               -- fecha de ingreso de la orden de compra
    fecha_entrega       TEXT,               -- por defecto fecha_oc + 45 dias, editable
    vendedor            TEXT,
    observaciones       TEXT,
    creado              TEXT
);

CREATE INDEX IF NOT EXISTS ix_lote_cliente ON lote(cliente);


-- -----------------------------------------------------------------------------
--  LINEA - SPF-1038-7. Lo que efectivamente se produce.
--  El estado va sobre la linea, no sobre el lote: cada plano avanza a su ritmo.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS linea (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    lote_codigo         TEXT NOT NULL REFERENCES lote(codigo),
    nro                 INTEGER,            -- 1, 2, 3...  NULL si el lote tiene una sola
    producto_codigo     TEXT REFERENCES producto(codigo),
    cantidad            INTEGER,
    n_lote_cliente      TEXT,
    estado              TEXT NOT NULL DEFAULT 'pendiente',
                        -- pendiente | en_produccion | pausada | finalizada
    prioridad           TEXT,
    grabado             TEXT,
    etiquetado          TEXT,
    observaciones       TEXT,
    responsable         TEXT,

    -- LOS DATOS TECNICOS QUEDAN CONGELADOS ACA al lanzar la linea.
    -- Si manana alguien corrige la ficha del producto, esta orden NO cambia:
    -- avisa que la ficha cambio y una persona decide. Una orden que ya esta
    -- en produccion no puede moverse sola bajo los pies del operario.
    datos_json          TEXT,               -- la ficha completa al momento de lanzar
    reglas_version      INTEGER,            -- con que reglas se calculo
    plano_nombre        TEXT,               -- el plano principal de esta linea
    procesos_json       TEXT,               -- tildes de proceso completo (tapas, etc.)
    compra_json         TEXT,               -- materiales marcados para compra interna + obs.

    nombre_origen       TEXT,               -- nombre de carpeta/archivo del que salio
    ruta_origen         TEXT,
    revisar             INTEGER NOT NULL DEFAULT 0,
    nota_revision       TEXT,
    creado              TEXT,
    actualizado         TEXT,
    UNIQUE(lote_codigo, nro, producto_codigo)
);

CREATE INDEX IF NOT EXISTS ix_linea_estado   ON linea(estado);
CREATE INDEX IF NOT EXISTS ix_linea_producto ON linea(producto_codigo);


-- -----------------------------------------------------------------------------
--  PLANO - el PDF. Se guarda la RUTA, nunca el archivo adentro de la base.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS plano (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    producto_codigo     TEXT REFERENCES producto(codigo),
    linea_id            INTEGER REFERENCES linea(id),
    nombre              TEXT,
    ruta                TEXT,
    normalizado         INTEGER NOT NULL DEFAULT 0,   -- tiene la marca FT-v1
    creado              TEXT
);

CREATE INDEX IF NOT EXISTS ix_plano_producto ON plano(producto_codigo);


-- -----------------------------------------------------------------------------
--  MOLDES - las tablas de la seccion 3 de reglas.yaml.
--  Se cargan desde ahi y despues se editan desde el sistema.
--  Un diametro que no esta aca no se puede fabricar espiralado.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS molde (
    tipo                TEXT NOT NULL,      -- perforado | ranurado | desplegado
    diam_molde          REAL,
    diam_externo        REAL,
    diam_interno        REAL,               -- solo desplegado
    espesor             REAL,
    peso_galvanizada    REAL,               -- g por metro
    peso_hojalata       REAL,               -- g por metro
    PRIMARY KEY (tipo, diam_molde, diam_interno)
);


-- -----------------------------------------------------------------------------
--  COMENTARIOS - produccion no edita la orden, pero puede escribir.
--  Un comentario es una observacion; una propuesta es un cambio sugerido que
--  ingenieria tiene que aceptar o rechazar.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS comentario (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    linea_id            INTEGER NOT NULL REFERENCES linea(id),
    fecha               TEXT NOT NULL,
    usuario             TEXT,
    tipo                TEXT NOT NULL DEFAULT 'comentario',  -- comentario | propuesta
    texto               TEXT NOT NULL,
    campo               TEXT,               -- si propone cambiar un dato puntual
    valor_propuesto     TEXT,
    estado              TEXT NOT NULL DEFAULT 'abierto'      -- abierto | aceptado | rechazado
);

CREATE INDEX IF NOT EXISTS ix_comentario_linea ON comentario(linea_id, estado);


-- -----------------------------------------------------------------------------
--  HISTORIAL - toda modificacion queda registrada.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS historial (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha               TEXT NOT NULL,
    usuario             TEXT,
    entidad             TEXT NOT NULL,      -- producto | lote | linea | molde
    entidad_id          TEXT NOT NULL,
    campo               TEXT,
    valor_anterior      TEXT,
    valor_nuevo         TEXT,
    nota                TEXT
);

CREATE INDEX IF NOT EXISTS ix_historial_entidad ON historial(entidad, entidad_id);


-- -----------------------------------------------------------------------------
--  BUSCADOR - indice de texto completo sobre los productos.
-- -----------------------------------------------------------------------------
-- Tabla propia (no 'content='''): con 355 productos el espacio es irrelevante
-- y asi se pueden leer las columnas al hacer el JOIN.
CREATE VIRTUAL TABLE IF NOT EXISTS producto_fts USING fts5(
    codigo, medio, soporte, nucleo_crudo, jaula_crudo, clientes
);
