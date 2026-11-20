"""Importa el histórico comercial de Instinct (edición "actualizado17", tres ZIP) a una base aparte.

Uso:  & $py src\\importar_instinct.py <carpeta con los tres zip | zip1 zip2 zip3> [--db data\\instinct.db]

Reglas (del prompt de importación de Zeta, 20/11/2026):
- Base APARTE (data/instinct.db). Nunca se mezcla con vistos.db ni con la tabla `precios` del vigilante.
- Los tres ZIP son una sola edición que SUSTITUYE a la anterior: si la base tiene otra edición, se guarda una copia
  (instinct_<edición anterior>.db) y se carga esta en limpio. Reimportar la misma edición no cambia los conteos.
- Se comprueba el manifiesto (igual en los tres ZIP), los 17 CSV.gz (nombre, bytes, SHA256, filas) y las cabeceras
  contra el esquema. Si algo no cuadra, se para y se dice qué.
- __null_fields restituye los NULL; una celda vacía fuera de esa lista es cadena vacía.
- *_minor son enteros en céntimos. Nada de esto es un precio validado ni una oferta aceptada.
- 17-noviembre-identificados.csv es una vista de observations: se comprueba, no se importa.
"""

import argparse
import csv
import gzip
import hashlib
import io
import json
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path

csv.field_size_limit(10 ** 9)

CLAVES = {  # clave sin duplicados por tabla (coverage_flags: se conservan sus filas tal cual, sin clave)
    "legacy_series": "identity_id", "observations": "event_id", "comparator_queries": "consulta",
    "evidence_records": "record_id", "evidence_source_metadata": "source_sha256",
    "evidence_card_annotations": "record_id", "sources": "id", "coverage_flags": None,
}
ENTEROS = ("_minor", "_count")  # columnas que se guardan como entero si traen número
ENTEROS_SUELTOS = {"admissible", "date_accredited", "position", "consulta"}
VISTA_17 = "17-noviembre-identificados.csv"


class ErrorImportacion(Exception):
    pass


def zips_de(rutas):
    if len(rutas) == 1 and Path(rutas[0]).is_dir():
        zs = sorted(Path(rutas[0]).glob("*Historico-Claude-actualizado17-parte*.zip"))
    else:
        zs = [Path(r) for r in rutas]
    if len(zs) != 3:
        raise ErrorImportacion(f"Hacen falta las tres partes; hay {len(zs)}: {[z.name for z in zs]}")
    return zs


def leer_zips(zs):
    """-> (manifiesto, leeme, {nombre: bytes}, vista17 en texto). Comprueba que manifiesto y LEEME coinciden."""
    manifiestos, leemes, archivos, vista = set(), set(), {}, None
    for z in zs:
        with zipfile.ZipFile(z) as zf:
            nombres = zf.namelist()
            for fijo in ("00-LEEME.txt", "manifiesto-comercial.json"):
                if fijo not in nombres:
                    raise ErrorImportacion(f"{z.name} no trae {fijo}")
            manifiestos.add(zf.read("manifiesto-comercial.json"))
            leemes.add(zf.read("00-LEEME.txt"))
            if VISTA_17 in nombres:
                vista = zf.read(VISTA_17).decode("utf-8-sig")
            for n in nombres:
                if n.endswith(".csv.gz"):
                    if n in archivos:
                        raise ErrorImportacion(f"{n} aparece en más de una parte")
                    archivos[n] = zf.read(n)
    if len(manifiestos) != 1 or len(leemes) != 1:
        raise ErrorImportacion("El manifiesto o el LEEME no son iguales en las tres partes")
    return json.loads(manifiestos.pop()), leemes.pop().decode("utf-8"), archivos, vista


def comprobar_recursos(manifiesto, archivos):
    """Nombre, bytes y SHA256 de cada CSV.gz contra el manifiesto. -> lista de problemas (vacía si todo cuadra)."""
    problemas = []
    declarados = {r["nombre"]: r for r in manifiesto["recursos"]}
    for nombre, r in declarados.items():
        datos = archivos.get(nombre)
        if datos is None:
            problemas.append(f"falta {nombre}")
            continue
        if len(datos) != r["bytes"]:
            problemas.append(f"{nombre}: {len(datos)} bytes, el manifiesto dice {r['bytes']}")
        if hashlib.sha256(datos).hexdigest() != r["sha256"]:
            problemas.append(f"{nombre}: el SHA256 no coincide")
    for nombre in archivos:
        if nombre not in declarados:
            problemas.append(f"{nombre} no está en el manifiesto")
    return problemas


def valor(col, texto, es_nulo):
    if es_nulo:
        return None
    if (col.endswith(ENTEROS) or col in ENTEROS_SUELTOS) and texto.lstrip("-").isdigit():
        return int(texto)
    return texto


def filas_csv(datos, columnas, nombre):
    lector = csv.reader(io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(datos)), encoding="utf-8", newline=""))
    cabecera = next(lector)
    if cabecera != columnas + ["__null_fields"]:
        raise ErrorImportacion(f"{nombre}: la cabecera no coincide con el esquema.\n  real:    {cabecera}\n"
                               f"  esperada: {columnas + ['__null_fields']}")
    for fila in lector:
        if len(fila) != len(cabecera):
            raise ErrorImportacion(f"{nombre}: una fila tiene {len(fila)} columnas y no {len(cabecera)}")
        nulos = set(json.loads(fila[-1] or "[]"))
        yield [valor(c, fila[i], i in nulos) for i, c in enumerate(columnas)]


def crear_tablas(con, esquema):
    for tabla, columnas in esquema.items():
        clave = CLAVES.get(tabla)
        defs = ", ".join(f'"{c}"' + (" PRIMARY KEY" if c == clave else "") for c in columnas)
        con.execute(f'CREATE TABLE IF NOT EXISTS "{tabla}" ({defs})')
    con.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    for nombre, tabla, col in (("legacy_ref", "legacy_series", "reference"), ("legacy_store", "legacy_series", "store"),
                               ("obs_ident", "observations", "identity_id")):
        if col in esquema.get(tabla, []):
            con.execute(f'CREATE INDEX IF NOT EXISTS {nombre} ON "{tabla}"("{col}")')


def edicion_de(manifiesto):
    return manifiesto["fecha_corte"]


def importar(rutas, db):
    zs = zips_de(rutas)
    manifiesto, _, archivos, vista = leer_zips(zs)
    problemas = comprobar_recursos(manifiesto, archivos)
    if problemas:
        raise ErrorImportacion("Los archivos no cuadran con el manifiesto:\n  " + "\n  ".join(problemas))
    esquema = manifiesto["esquema"]
    if set(esquema) != set(CLAVES):
        raise ErrorImportacion(f"Tablas del manifiesto inesperadas: {sorted(esquema)}")
    db = Path(db)
    db.parent.mkdir(parents=True, exist_ok=True)
    copia = None
    if db.exists():
        con = sqlite3.connect(db)
        try:
            anterior = con.execute("SELECT v FROM meta WHERE k='edicion'").fetchone()
        except sqlite3.Error:
            anterior = None
        con.close()
        if anterior and anterior[0] != edicion_de(manifiesto):
            copia = db.with_name(f"{db.stem}_{anterior[0][:10]}{db.suffix}")
            shutil.copy2(db, copia)
    con = sqlite3.connect(db)
    crear_tablas(con, esquema)
    leidas = {t: 0 for t in esquema}
    with con:  # todo o nada: sustitución de la edición entera
        for tabla in esquema:
            con.execute(f'DELETE FROM "{tabla}"')
        for r in manifiesto["recursos"]:
            tabla, columnas = r["tabla"], esquema[r["tabla"]]
            n = 0
            marcas = ",".join("?" * len(columnas))
            verbo = "INSERT" if CLAVES[tabla] else "INSERT"
            lote = []
            for fila in filas_csv(archivos[r["nombre"]], columnas, r["nombre"]):
                lote.append(fila)
                n += 1
                if len(lote) >= 20000:
                    con.executemany(f'{verbo} INTO "{tabla}" VALUES ({marcas})', lote)
                    lote = []
            if lote:
                con.executemany(f'{verbo} INTO "{tabla}" VALUES ({marcas})', lote)
            if n != r["filas"]:
                raise ErrorImportacion(f"{r['nombre']}: {n} filas, el manifiesto dice {r['filas']}")
            leidas[tabla] += n
        con.execute("INSERT OR REPLACE INTO meta VALUES ('edicion', ?)", (edicion_de(manifiesto),))
        con.execute("INSERT OR REPLACE INTO meta VALUES ('manifiesto', ?)", (json.dumps(manifiesto, ensure_ascii=False),))
    informe = comprobar(con, manifiesto, vista)
    informe.update(partes=[z.name for z in zs], csv=len(archivos), leidas=leidas, copia_anterior=str(copia) if copia else None)
    con.close()
    return informe


def comprobar(con, manifiesto, vista):
    conteos = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in manifiesto["tablas_y_filas"]}
    ids17 = manifiesto["datos_17nov"]["event_ids"]
    veces17 = {e: con.execute("SELECT COUNT(*) FROM observations WHERE event_id=?", (e,)).fetchone()[0] for e in ids17}
    vista_ids = sorted(f["event_id"] for f in csv.DictReader(io.StringIO(vista or ""))) if vista else []
    sha_mm = manifiesto["correccion_mediamarkt"]["source_sha256"]
    mm = con.execute("SELECT COUNT(*), SUM(source_status='superseded') FROM evidence_records WHERE source_sha256=?",
                     (sha_mm,)).fetchone()
    return {
        "edicion": edicion_de(manifiesto),
        "conteos": conteos,
        "esperados": manifiesto["tablas_y_filas"],
        "total": sum(conteos.values()),
        "total_esperado": manifiesto["filas_totales_tablas"],
        "eventos_17": veces17,
        "vista_17_coincide": vista_ids == sorted(ids17),
        "mediamarkt_filas": mm[0], "mediamarkt_superseded": mm[1] or 0,
        "mediamarkt_esperadas": manifiesto["correccion_mediamarkt"]["filas_originales_marcadas_superseded"],
    }


def resumen(inf, inf2=None):
    lineas = [f"Edición {inf['edicion']} · partes: {', '.join(inf['partes'])} · {inf['csv']} CSV.gz comprobados "
              "(nombre, bytes, SHA256 y filas contra el manifiesto)"]
    for t, n in inf["conteos"].items():
        e = inf["esperados"][t]
        lineas.append(f"  {t}: {n}" + ("" if n == e else f"  ¡el manifiesto dice {e}!"))
    lineas.append(f"  total: {inf['total']} (manifiesto {inf['total_esperado']}; no son productos únicos ni ofertas)")
    ok17 = all(v == 1 for v in inf["eventos_17"].values())
    lineas.append(f"17/11: {'los 4 event_id están una sola vez' if ok17 else inf['eventos_17']}; la vista "
                  f"{'coincide' if inf['vista_17_coincide'] else 'NO coincide'} y no se importa aparte")
    mm_ok = inf["mediamarkt_filas"] == inf["mediamarkt_superseded"] == inf["mediamarkt_esperadas"]
    lineas.append(f"MediaMarkt: {inf['mediamarkt_superseded']} de {inf['mediamarkt_filas']} filas superseded "
                  f"(esperadas {inf['mediamarkt_esperadas']}) {'OK' if mm_ok else '¡NO CUADRA!'}")
    if inf.get("copia_anterior"):
        lineas.append(f"Edición anterior guardada en {inf['copia_anterior']}")
    if inf2 is not None:
        igual = inf2["conteos"] == inf["conteos"]
        lineas.append("Reimportación de la misma edición: " + ("sin incremento de filas" if igual else
                                                              f"¡CAMBIAN LOS CONTEOS! {inf2['conteos']}"))
    return "\n".join(lineas)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("rutas", nargs="+")
    ap.add_argument("--db", default=str(Path(__file__).resolve().parent.parent / "data" / "instinct.db"))
    ap.add_argument("--sin-reimportar", action="store_true", help="no repetir la carga para la prueba de reimportación")
    a = ap.parse_args(argv)
    try:
        inf = importar(a.rutas, a.db)
        inf2 = None if a.sin_reimportar else importar(a.rutas, a.db)
    except ErrorImportacion as e:
        print("IMPORTACIÓN NO COMPLETADA:", e)
        return 1
    print(resumen(inf, inf2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
