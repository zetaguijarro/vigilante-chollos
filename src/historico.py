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

def enlazar_con_chollometro(db=None, con_vistos=None, vigilante=None):
    """Rellena `enlace_cm` (sustituye lo anterior) y la vista `cm_historico_zeta`. -> {"url": n, "codigo": n, ...}."""
    if vigilante is None:
        import vigilante as vigilante
    v = vigilante
    propia = con_vistos is None
    con_v = con_vistos or v.bd()
    h = sqlite3.connect(db or DB_HISTORICO, timeout=60)
    try:
        copia = v.cargar_chollometro(con_v)
        por_id = {x["id"]: x for x in copia["hilos"]}
        por_clave = {}
        for hilo, clave in con_v.execute("SELECT hilo, clave FROM cm_destino WHERE clave IS NOT NULL"):
            por_clave.setdefault(clave, []).append(hilo)
        idx = copia.get("idx") or {}
        h.execute("DROP TABLE IF EXISTS enlace_cm")
        h.execute("CREATE TABLE enlace_cm (origen TEXT, id_origen TEXT, hilo_id INTEGER, metodo TEXT, similitud INTEGER, "
                  "PRIMARY KEY (origen, id_origen, hilo_id))")
        cuenta = {"url": 0, "codigo": 0, "registros_enlazados": 0}
        cache = {}  # nombre -> [(hilo_id, similitud)]

        def por_codigo(nombre):
            if nombre in cache:
                return cache[nombre]
            res = []
            codigos = [c for c in v.codigos_modelo(nombre)
                       if not v.RE_UNIDAD.match(c.lower()) and any(ch.isdigit() for ch in c) and any(ch.isalpha() for ch in c)]
            ids = set()
            for c in codigos:
                ids.update(idx.get("#" + c, ()))
            for i in sorted(ids):
                x = copia["hilos"][i]
                sim = v.coincidencia(nombre, x["titulo"])
                if sim >= 100 * v.SIMILITUD_MINIMA and v.mismo_producto(nombre, x["titulo"]):  # el 80 % de Zeta
                    res.append((x["id"], sim))
            cache[nombre] = res
            return res

        fuentes = [("legacy_series", "SELECT identity_id, name, url FROM legacy_series"),
                   ("observations", "SELECT event_id, COALESCE(name, text), url FROM observations"),
                   ("evidence_records", "SELECT record_id, NULL, url FROM evidence_records")]
        for origen, sql in fuentes:
            lote, enlazados = [], 0
            for id_origen, nombre, url in h.execute(sql).fetchall():
                hechos = set()
                clave = v.clave_producto(url) if url else None
                for hilo in por_clave.get(clave, ()):
                    lote.append((origen, id_origen, hilo, "url", 100)); hechos.add(hilo); cuenta["url"] += 1
                if nombre and len(nombre) > 6:
                    for hilo, sim in por_codigo(nombre):
                        if hilo not in hechos:
                            lote.append((origen, id_origen, hilo, "codigo", sim)); hechos.add(hilo); cuenta["codigo"] += 1
                enlazados += 1 if hechos else 0
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
        h.commit()
        cuenta["hilos_con_historico"] = h.execute("SELECT COUNT(DISTINCT hilo_id) FROM cm_historico_zeta").fetchone()[0]
        return cuenta
    finally:
        h.close()
        if propia:
            con_v.close()
