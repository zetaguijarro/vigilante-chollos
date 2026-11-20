"""Importación del histórico comercial de Zeta (edición "actualizado17", corte 20/11/2026) a `data/historico.db`.

Tres ZIP independientes con 17 CSV.gz y un manifiesto. Reglas del encargo de Zeta (19-20/11/2026):
- Se comprueba lo que declara el manifiesto (archivos, tamaños, SHA256, filas) y las cabeceras de cada tabla; si algo no
  cuadra, se para y se dice qué.
- `__null_fields` (índices de columna que eran NULL) restituye los NULL; una celda vacía fuera de ese array es una cadena vacía.
- Los `*_minor` son enteros en la unidad menor de `currency` (399 = 3,99 EUR).
- Se carga como SUSTITUCIÓN de la edición (tablas nuevas dentro de una transacción), nunca como suma: reimportar la misma
  edición no cambia los conteos. Las claves son las del encargo. No se valida ni se aprueba nada: es una copia consultable.
- La base es aparte de `vistos.db` (el histórico de Zeta no son lecturas del vigilante).
"""
import csv
import gzip
import hashlib
import io
import json
import sqlite3
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

csv.field_size_limit(sys.maxsize)

DB_HISTORICO = Path(__file__).resolve().parent.parent / "data" / "historico.db"
CLAVES = {"legacy_series": "identity_id", "observations": "event_id", "comparator_queries": "consulta",
          "evidence_records": "record_id", "evidence_source_metadata": "source_sha256",
          "evidence_card_annotations": "record_id", "sources": "id", "coverage_flags": None}
# Columnas enteras (unidad menor, contadores y posición). Si una celda no es un entero, se conserva como texto.
ENTERAS = {"minimum_minor", "maximum_minor", "current_minor", "price_minor", "dated_count", "undated_count",
           "excluded_count", "position", "affected_records"}


class ErrorImportacion(Exception):
    pass


def _sha256(datos):
    return hashlib.sha256(datos).hexdigest()


def leer_zips(rutas):
    """-> (manifiesto, {nombre del recurso: bytes}). Comprueba que README y manifiesto son idénticos en los tres ZIP."""
    manifiestos, leemes, recursos = [], [], {}
    for ruta in rutas:
        with zipfile.ZipFile(ruta) as z:
            for info in z.infolist():
                datos = z.read(info)
                if info.filename == "manifiesto-comercial.json":
                    manifiestos.append(datos)
                elif info.filename == "00-LEEME.txt":
                    leemes.append(datos)
                elif info.filename.endswith(".csv.gz"):
                    recursos[info.filename] = datos
    if len(manifiestos) != len(rutas) or len(set(manifiestos)) != 1:
        raise ErrorImportacion("el manifiesto no se repite igual en los ZIP")
    if len(set(leemes)) != 1:
        raise ErrorImportacion("00-LEEME.txt no coincide entre los ZIP")
    return json.loads(manifiestos[0].decode("utf-8")), recursos


def comprobar_recursos(manifiesto, recursos):
    """Nombres, tamaños y SHA256 de los CSV.gz frente al manifiesto. -> lista de problemas (vacía si todo cuadra)."""
    problemas = []
    declarados = {r["nombre"]: r for r in manifiesto["recursos"]}
    for nombre in declarados:
        if nombre not in recursos:
            problemas.append(f"falta {nombre}")
    for nombre in recursos:
        if nombre not in declarados:
            problemas.append(f"sobra {nombre} (no está en el manifiesto)")
    for nombre, d in declarados.items():
        b = recursos.get(nombre)
        if b is None:
            continue
        if len(b) != d["bytes"]:
            problemas.append(f"{nombre}: {len(b)} bytes y el manifiesto dice {d['bytes']}")
        if _sha256(b) != d["sha256"]:
            problemas.append(f"{nombre}: SHA256 distinto del manifiesto")
    return problemas


def filas_del_recurso(datos):
    """Itera (cabecera, fila como lista de str) de un CSV.gz UTF-8 con cabecera."""
    texto = io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(datos)), encoding="utf-8", newline="")
    lector = csv.reader(texto)
    cabecera = next(lector)
    for fila in lector:
        yield cabecera, fila


def _valor(columna, texto):
    if columna in ENTERAS:
        t = texto.strip()
        if t.lstrip("-").isdigit():
            return int(t)
    return texto


def importar(rutas, db=None, verificar_antes=True):
    """Carga la edición en `db` como sustitución. -> {"filas": {tabla: n}, "problemas": [...], "avisos": [...]}.
    Si algo no cuadra con el manifiesto, lanza ErrorImportacion y NO toca la base."""
    manifiesto, recursos = leer_zips(rutas)
    problemas = comprobar_recursos(manifiesto, recursos)
    esquema = manifiesto["esquema"]
    por_tabla = {}
    for r in manifiesto["recursos"]:
        por_tabla.setdefault(r["tabla"], []).append(r)
    if problemas and verificar_antes:
        raise ErrorImportacion("; ".join(problemas))
    ruta_db = Path(db) if db else DB_HISTORICO
    ruta_db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ruta_db, timeout=60)
    avisos, contados = [], {}
    try:
        con.execute("BEGIN")
        for tabla, columnas in esquema.items():
            con.execute(f"DROP TABLE IF EXISTS {tabla}")
            definiciones = ", ".join(f'"{c}" INTEGER' if c in ENTERAS else f'"{c}"' for c in columnas)
            clave = CLAVES.get(tabla)
            if clave:
                definiciones += f', PRIMARY KEY ("{clave}")'
            con.execute(f"CREATE TABLE {tabla} ({definiciones})")
            marcas = ",".join("?" * len(columnas))
            total = 0
            for r in sorted(por_tabla.get(tabla, []), key=lambda x: x["parte"]):
                lote, n = [], 0
                for cabecera, fila in filas_del_recurso(recursos[r["nombre"]]):
                    esperada = columnas + ["__null_fields"]
                    if cabecera != esperada:
                        raise ErrorImportacion(f"{r['nombre']}: la cabecera difiere del manifiesto: {cabecera} frente a {esperada}")
                    if len(fila) != len(esperada):
                        raise ErrorImportacion(f"{r['nombre']}: una fila tiene {len(fila)} celdas y se esperaban {len(esperada)}")
                    nulos = set(json.loads(fila[-1] or "[]"))
                    lote.append([None if i in nulos else _valor(c, fila[i]) for i, c in enumerate(columnas)])
                    n += 1
                    if len(lote) >= 20000:
                        con.executemany(f"INSERT INTO {tabla} VALUES ({marcas})", lote)
                        lote = []
                if lote:
                    con.executemany(f"INSERT INTO {tabla} VALUES ({marcas})", lote)
                if n != r["filas"]:
                    raise ErrorImportacion(f"{r['nombre']}: {n} filas y el manifiesto dice {r['filas']}")
                total += n
            contados[tabla] = total
            if total != manifiesto["tablas_y_filas"][tabla]:
                raise ErrorImportacion(f"{tabla}: {total} filas y el manifiesto dice {manifiesto['tablas_y_filas'][tabla]}")
        _crear_tablas_propias(con)  # lecturas_vigilante y enlace_cm no son de la edición: no se tocan
        _vista_observaciones(con)
        con.execute("DROP TABLE IF EXISTS edicion")
        con.execute("CREATE TABLE edicion (clave TEXT PRIMARY KEY, valor TEXT)")
        con.executemany("INSERT INTO edicion VALUES (?,?)", [
            ("edicion", manifiesto.get("edicion", "")), ("fecha_corte", manifiesto.get("fecha_corte", "")),
            ("sustituye", manifiesto.get("sustituye", "")), ("filas_totales", str(sum(contados.values())))])
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return {"filas": contados, "problemas": problemas, "avisos": avisos}


def comprobaciones(manifiesto, db=None):
    """Las comprobaciones finales del encargo, ejecutadas sobre la base."""
    con = sqlite3.connect(db or DB_HISTORICO)
    try:
        ids = manifiesto["datos_17nov"]["event_ids"]
        unico = {e: con.execute("SELECT COUNT(*) FROM observations WHERE event_id=?", (e,)).fetchone()[0] for e in ids}
        sha = manifiesto["correccion_mediamarkt"]["source_sha256"]
        mm = con.execute("SELECT source_status, COUNT(*) FROM evidence_records WHERE source_sha256=? GROUP BY 1", (sha,)).fetchall()
        estados = con.execute("SELECT source_status, COUNT(*) FROM evidence_records GROUP BY 1 ORDER BY 2 DESC").fetchall()
        return {"event_ids_del_17": unico, "mediamarkt_250": mm, "estados_evidencia": estados}
    finally:
        con.close()


# ---------------------------------------------------------------- enlace con los hilos de Chollometro
# Zeta (20/11/2026): "la importación intenta casar los registros con los de Chollometro para ampliar la información".
# Solo se enlaza (una tabla aparte con el método y la similitud); no se fusionan productos ni se valida nada del histórico:
# - método "url": el enlace del registro es el mismo producto al que lleva "Ir al chollo" de un hilo (cm_destino);
# - método "codigo": comparten código de modelo, pasan `mismo_producto` (marca y modelo) y se parecen al 80 % o más.
# Un parecido de nombre sin código ni enlace NO enlaza: es lo que pidió el encargo (no fusionar por nombre parecido).

COLUMNAS_LECTURA = ["event_id", "identity_id", "store", "url", "observed_at", "price_minor", "currency", "identity_status",
                    "price_scope", "conditions_json", "source_id", "source_sha256", "date_accredited", "admissible", "name", "text"]
FUENTE_VIGILANTE = "vigilante-rondas"


class Enlazador:
    """Enlaza un registro (nombre + url) con hilos de la copia de Chollometro."""

    def __init__(self, v, con_vistos):
        self.v = v
        self.copia = v.cargar_chollometro(con_vistos)
        self.idx = self.copia.get("idx") or {}
        self.por_clave = {}
        for hilo, clave in con_vistos.execute("SELECT hilo, clave FROM cm_destino WHERE clave IS NOT NULL"):
            self.por_clave.setdefault(clave, []).append(hilo)
        self.cache = {}

    def por_codigo(self, nombre):
        if nombre in self.cache:
            return self.cache[nombre]
        v, res = self.v, []
        codigos = [c for c in v.codigos_modelo(nombre)
                   if not v.RE_UNIDAD.match(c.lower()) and any(ch.isdigit() for ch in c) and any(ch.isalpha() for ch in c)]
        ids = set()
        for c in codigos:
            ids.update(self.idx.get("#" + c, ()))
        for i in sorted(ids):
            x = self.copia["hilos"][i]
            sim = v.coincidencia(nombre, x["titulo"])
            if sim >= 100 * v.SIMILITUD_MINIMA and v.mismo_producto(nombre, x["titulo"]):  # el 80 % de Zeta
                res.append((x["id"], sim))
        self.cache[nombre] = res
        return res

    def enlaces(self, nombre, url):
        """-> [(hilo_id, método, similitud)]"""
        salida, hechos = [], set()
        clave = self.v.clave_producto(url) if url else None
        for hilo in self.por_clave.get(clave, ()):
            salida.append((hilo, "url", 100))
            hechos.add(hilo)
        if nombre and len(nombre) > 6:
            for hilo, sim in self.por_codigo(nombre):
                if hilo not in hechos:
                    salida.append((hilo, "codigo", sim))
        return salida


def _crear_tablas_propias(h):
    """Tablas que NO son de la edición de Zeta y por tanto no las toca `importar`: las lecturas del vigilante y los enlaces."""
    h.execute("CREATE TABLE IF NOT EXISTS lecturas_vigilante (" + ", ".join(
        f'"{c}" INTEGER' if c == "price_minor" else f'"{c}"' for c in COLUMNAS_LECTURA) + ', PRIMARY KEY ("event_id"))')
    h.execute("CREATE TABLE IF NOT EXISTS enlace_cm (origen TEXT, id_origen TEXT, hilo_id INTEGER, metodo TEXT, "
              "similitud INTEGER, PRIMARY KEY (origen, id_origen, hilo_id))")
    h.execute("CREATE TABLE IF NOT EXISTS meta_historico (clave TEXT PRIMARY KEY, valor TEXT)")


def _vista_observaciones(h):
    h.execute("DROP VIEW IF EXISTS observaciones_todas")
    cols = ", ".join(f'"{c}"' for c in COLUMNAS_LECTURA)
    h.execute(f"CREATE VIEW observaciones_todas AS SELECT {cols}, 'zeta' AS origen_dato FROM observations "
              f"UNION ALL SELECT {cols}, 'vigilante' AS origen_dato FROM lecturas_vigilante")


def enlazar_con_chollometro(db=None, con_vistos=None, vigilante=None):
    """Rehace `enlace_cm` entera (legacy_series, observations, evidence_records y las lecturas del vigilante) y la vista
    `cm_historico_zeta`. -> conteos. Se hace al importar y como mucho una vez al día (`refrescar_enlaces`)."""
    if vigilante is None:
        import vigilante as vigilante
    v = vigilante
    propia = con_vistos is None
    con_v = con_vistos or v.bd()
    h = sqlite3.connect(db or DB_HISTORICO, timeout=60)
    try:
        _crear_tablas_propias(h)
        _vista_observaciones(h)
        enl = Enlazador(v, con_v)
        h.execute("DELETE FROM enlace_cm")
        cuenta = {"url": 0, "codigo": 0, "registros_enlazados": 0}
        fuentes = [("legacy_series", "SELECT identity_id, name, url FROM legacy_series"),
                   ("observations", "SELECT event_id, COALESCE(name, text), url FROM observations"),
                   ("evidence_records", "SELECT record_id, NULL, url FROM evidence_records"),
                   ("lecturas_vigilante", "SELECT event_id, name, url FROM lecturas_vigilante")]
        for origen, sql in fuentes:
            lote, enlazados = [], 0
            for id_origen, nombre, url in h.execute(sql).fetchall():
                encontrados = enl.enlaces(nombre, url)
                for hilo, metodo, sim in encontrados:
                    lote.append((origen, id_origen, hilo, metodo, sim))
                    cuenta[metodo] += 1
                enlazados += 1 if encontrados else 0
            h.executemany("INSERT OR IGNORE INTO enlace_cm VALUES (?,?,?,?,?)", lote)
            cuenta[f"registros_enlazados:{origen}"] = enlazados
            cuenta["registros_enlazados"] += enlazados
        h.execute("DROP VIEW IF EXISTS cm_historico_zeta")
        # Por hilo: lo que el histórico provisional de Zeta dice de los registros enlazados. NO es un mínimo demostrado
        # (legacy_series es provisional y puede mezclar variantes); sirve para ampliar la ficha del hilo.
        h.execute("""CREATE VIEW cm_historico_zeta AS
            SELECT e.hilo_id, e.metodo, COUNT(*) AS series, l.currency,
                   MIN(l.minimum_minor) / 100.0 AS minimo_eur, MAX(l.maximum_minor) / 100.0 AS maximo_eur,
                   MIN(l.first_at) AS desde, MAX(l.last_at) AS hasta
            FROM enlace_cm e JOIN legacy_series l ON e.origen = 'legacy_series' AND l.identity_id = e.id_origen
            WHERE l.currency = 'EUR' AND l.minimum_minor IS NOT NULL
            GROUP BY e.hilo_id, e.metodo, l.currency""")
        h.execute("INSERT OR REPLACE INTO meta_historico VALUES ('enlaces_hechos', ?)", (datetime.now().isoformat(timespec="minutes"),))
        h.commit()
        cuenta["hilos_con_historico"] = h.execute("SELECT COUNT(DISTINCT hilo_id) FROM cm_historico_zeta").fetchone()[0]
        return cuenta
    finally:
        h.close()
        if propia:
            con_v.close()


def refrescar_enlaces(con_vistos=None, db=None, vigilante=None, cada_horas=24):
    """Enlaces nuevos entre el histórico y los hilos de Chollometro que han llegado desde la última vez: se rehacen como
    mucho cada `cada_horas` (47 s). -> conteos o None si todavía no tocaba."""
    h = sqlite3.connect(db or DB_HISTORICO, timeout=60)
    try:
        _crear_tablas_propias(h)
        fila = h.execute("SELECT valor FROM meta_historico WHERE clave='enlaces_hechos'").fetchone()
        h.commit()
    finally:
        h.close()
    if fila and datetime.fromisoformat(fila[0]) > datetime.now() - timedelta(hours=cada_horas):
        return None
    return enlazar_con_chollometro(db=db, con_vistos=con_vistos, vigilante=vigilante)


def registrar_lecturas(lecturas, db=None, con_vistos=None, vigilante=None):
    """Guarda las lecturas del vigilante (cada cambio de precio que lee en una tienda) en `lecturas_vigilante`, con las
    columnas de `observations`, sin tocar la edición de Zeta (que `importar` sustituye entera). `lecturas`:
    [{"tienda", "nombre", "url", "precio", "precio_antes", "cupon", "socio", "clave", "ahora"}]. Es el precio de LISTADO
    con la variante sin resolver y sin admitir (`admissible` 0): nada se valida aquí. Idempotente: el mismo producto, al
    mismo precio y minuto, es el mismo evento. Si hay `con_vistos`, enlaza las nuevas con los hilos de Chollometro.
    -> filas nuevas."""
    if not lecturas:
        return 0
    h = sqlite3.connect(db or DB_HISTORICO, timeout=60)
    try:
        _crear_tablas_propias(h)
        h.execute("CREATE TABLE IF NOT EXISTS sources (id, kind, path_name, source_sha256, scope, PRIMARY KEY (id))")
        h.execute("INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?)",
                  (FUENTE_VIGILANTE, "live_reading", "data/vistos.db", None,
                   "Lecturas del vigilante: precio de listado, variante sin resolver, sin admitir ni validar"))
        filas = []
        for x in lecturas:
            precio = x.get("precio")
            if not precio:
                continue
            ahora = x["ahora"]  # ISO con zona horaria
            minuto = ahora[:16]
            base = f"{x['tienda']}|{x.get('url') or x['nombre']}|{precio}|{minuto}"
            id_evento = "V-" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:24]
            condiciones = {k: x[k] for k in ("precio_antes", "cupon", "socio") if x.get(k)}
            filas.append((id_evento, x.get("clave"), x["tienda"], x.get("url"), ahora, int(round(precio * 100)), "EUR",
                          "URL_ONLY_UNRESOLVED_VARIANT", "listing_price", json.dumps(condiciones, ensure_ascii=False),
                          FUENTE_VIGILANTE, None, "1", "0", x["nombre"], None))
        antes = h.execute("SELECT COUNT(*) FROM lecturas_vigilante").fetchone()[0]
        marcas = ",".join("?" * len(COLUMNAS_LECTURA))
        h.executemany(f"INSERT OR IGNORE INTO lecturas_vigilante VALUES ({marcas})", filas)
        nuevas = h.execute("SELECT COUNT(*) FROM lecturas_vigilante").fetchone()[0] - antes
        if con_vistos is not None and nuevas:
            if vigilante is None:
                import vigilante as vigilante
            enl = Enlazador(vigilante, con_vistos)
            for f in filas:
                for hilo, metodo, sim in enl.enlaces(f[14], f[3]):
                    h.execute("INSERT OR IGNORE INTO enlace_cm VALUES (?,?,?,?,?)", ("lecturas_vigilante", f[0], hilo, metodo, sim))
        h.commit()
        return nuevas
    finally:
        h.close()
