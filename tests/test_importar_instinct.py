"""Importador del histórico de Instinct (20/11/2026) con una edición pequeña de mentira."""
import csv
import gzip
import hashlib
import io
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import importar_instinct as ii  # noqa: E402

ESQUEMA = {
    "legacy_series": ["identity_id", "store", "reference", "minimum_minor", "dated_count"],
    "observations": ["event_id", "store", "price_minor", "admissible"],
    "comparator_queries": ["consulta", "comparator", "minimum_minor"],
    "evidence_records": ["record_id", "store", "source_sha256", "source_status"],
    "evidence_source_metadata": ["source_sha256", "status"],
    "evidence_card_annotations": ["record_id", "scope"],
    "sources": ["id", "kind"],
    "coverage_flags": ["scope", "issue", "affected_records"],
}
MM = "b4" * 32
DATOS = {
    "legacy_series": [(["legacy:1", "a.es", "", "5999", "6"], []), (["legacy:2", "", "", "", "1"], [2, 3])],
    "observations": [(["E17", "SharkNinja ES", "8999", "1"], []), (["E18", "x", "100", "0"], [])],
    "comparator_queries": [(["1", "Idealo ES", "9999"], [])],
    "evidence_records": [(["r1", "MediaMarkt", MM, "superseded"], []), (["r2", "MediaMarkt", MM, "superseded"], []),
                         (["r3", "PSS", "cc" * 32, "current"], [])],
    "evidence_source_metadata": [([MM, "superseded"], [])],
    "evidence_card_annotations": [(["r3", "solo proyección"], [])],
    "sources": [(["legacy-v6", "legacy_series_extremes"], [])],
    "coverage_flags": [(["legacy", "provisional", "2"], []), (["legacy", "provisional", "2"], [])],
}


def gz(tabla):
    texto = io.StringIO()
    w = csv.writer(texto)
    w.writerow(ESQUEMA[tabla] + ["__null_fields"])
    for fila, nulos in DATOS[tabla]:
        w.writerow(fila + [json.dumps(nulos)])
    return gzip.compress(texto.getvalue().encode("utf-8"))


def edicion(carpeta, corte="2026-11-20T12:18:02+00:00", romper=None):
    archivos = {f"{t}.part1.csv.gz": gz(t) for t in ESQUEMA}
    manifiesto = {
        "fecha_corte": corte, "esquema": ESQUEMA,
        "tablas_y_filas": {t: len(DATOS[t]) for t in ESQUEMA},
        "filas_totales_tablas": sum(len(v) for v in DATOS.values()),
        "datos_17nov": {"event_ids": ["E17"]},
        "correccion_mediamarkt": {"source_sha256": MM, "filas_originales_marcadas_superseded": 2},
        "recursos": [{"tabla": n.split(".")[0], "nombre": n, "filas": len(DATOS[n.split(".")[0]]), "bytes": len(b),
                      "sha256": hashlib.sha256(b).hexdigest()} for n, b in archivos.items()],
    }
    if romper:
        archivos[romper] = archivos[romper] + b"x"
    nombres = list(archivos)
    for i in range(3):
        with zipfile.ZipFile(Path(carpeta) / f"Historico-Claude-actualizado17-parte{i + 1}.zip", "w") as z:
            z.writestr("00-LEEME.txt", "léeme")
            z.writestr("manifiesto-comercial.json", json.dumps(manifiesto))
            z.writestr("17-noviembre-identificados.csv", "event_id,store\nE17,SharkNinja ES\n")
            for n in nombres[i::3]:
                z.writestr(n, archivos[n])


class ImportarInstinct(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "instinct.db"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_importa_comprueba_y_reimportar_no_suma(self):
        edicion(self.tmp)
        inf = ii.importar([str(self.tmp)], self.db)
        self.assertEqual(inf["conteos"], inf["esperados"])
        self.assertEqual(inf["eventos_17"], {"E17": 1})
        self.assertTrue(inf["vista_17_coincide"])
        self.assertEqual((inf["mediamarkt_filas"], inf["mediamarkt_superseded"]), (2, 2))
        self.assertEqual(ii.importar([str(self.tmp)], self.db)["conteos"], inf["conteos"])
        con = sqlite3.connect(self.db)
        # NULL restituido solo donde __null_fields lo dice; vacío fuera de la lista es cadena vacía
        self.assertEqual(con.execute("SELECT store, reference, minimum_minor FROM legacy_series WHERE identity_id='legacy:2'").fetchone(),
                         ("", None, None))
        self.assertEqual(con.execute("SELECT reference, minimum_minor FROM legacy_series WHERE identity_id='legacy:1'").fetchone(),
                         ("", 5999))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM coverage_flags").fetchone()[0], 2, "se conservan sus filas")

    def test_si_un_archivo_no_cuadra_no_se_importa(self):
        edicion(self.tmp, romper="observations.part1.csv.gz")
        with self.assertRaises(ii.ErrorImportacion) as e:
            ii.importar([str(self.tmp)], self.db)
        self.assertIn("observations.part1.csv.gz", str(e.exception))
        self.assertFalse(self.db.exists())

    def test_otra_edicion_sustituye_y_guarda_copia_de_la_anterior(self):
        edicion(self.tmp, corte="2026-11-13T10:00:00+00:00")
        ii.importar([str(self.tmp)], self.db)
        edicion(self.tmp)
        inf = ii.importar([str(self.tmp)], self.db)
        self.assertTrue(Path(inf["copia_anterior"]).exists())
        self.assertEqual(inf["conteos"], inf["esperados"], "sustituye, no suma")

    def test_faltan_partes(self):
        edicion(self.tmp)
        (self.tmp / "Historico-Claude-actualizado17-parte3.zip").unlink()
        with self.assertRaises(ii.ErrorImportacion):
            ii.importar([str(self.tmp)], self.db)


if __name__ == "__main__":
    unittest.main()
