"""Importación del histórico de Zeta (src/historico.py) con una edición ficticia mínima."""
import csv
import gzip
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))
import historico as h  # noqa: E402

ESQUEMA = {"legacy_series": ["identity_id", "store", "name", "minimum_minor", "dated_count"],
           "observations": ["event_id", "name", "price_minor", "admissible"]}
FILAS = {"legacy_series": [["L1", "tienda.es", "Producto A", "399", "2"], ["L2", "tienda.es", "", "", ""]],
         "observations": [["E1", "", "1250", "1"]]}
NULOS = {("legacy_series", "L2"): [2, 3, 4], ("observations", "E1"): [1]}  # índices de columnas NULL


def csv_gz(tabla):
    columnas = ESQUEMA[tabla] + ["__null_fields"]
    sal = io.StringIO()
    w = csv.writer(sal)
    w.writerow(columnas)
    for fila in FILAS[tabla]:
        w.writerow(fila + [json.dumps(NULOS.get((tabla, fila[0]), []))])
    return gzip.compress(sal.getvalue().encode("utf-8"))


class ImportacionHistorico(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        recursos, self.datos = [], {}
        for tabla in ESQUEMA:
            nombre = f"{tabla}.part1.csv.gz"
            b = csv_gz(tabla)
            self.datos[nombre] = b
            recursos.append({"tabla": tabla, "parte": 1, "nombre": nombre, "filas": len(FILAS[tabla]), "bytes": len(b),
                             "sha256": hashlib.sha256(b).hexdigest()})
        self.manifiesto = {"esquema": ESQUEMA, "recursos": recursos,
                           "tablas_y_filas": {t: len(f) for t, f in FILAS.items()}}
        self.zip = self.tmp / "p1.zip"
        self.escribir_zip(self.zip, self.manifiesto, self.datos)
        self.db = self.tmp / "h.db"

    def escribir_zip(self, ruta, manifiesto, datos):
        with zipfile.ZipFile(ruta, "w") as z:
            z.writestr("manifiesto-comercial.json", json.dumps(manifiesto))
            z.writestr("00-LEEME.txt", "leeme")
            for n, b in datos.items():
                z.writestr(n, b)

    def test_nulos_enteros_y_cadenas_vacias(self):
        h.importar([self.zip], db=self.db)
        con = sqlite3.connect(self.db)
        l2 = con.execute("SELECT name, minimum_minor, dated_count FROM legacy_series WHERE identity_id='L2'").fetchone()
        self.assertEqual(l2, (None, None, None), "los índices de __null_fields vuelven a ser NULL")
        l1 = con.execute("SELECT name, minimum_minor, dated_count FROM legacy_series WHERE identity_id='L1'").fetchone()
        self.assertEqual(l1, ("Producto A", 399, 2), "los *_minor son enteros (399 = 3,99 EUR)")
        e1 = con.execute("SELECT name, price_minor FROM observations").fetchone()
        self.assertEqual(e1, (None, 1250))
        # cadena vacía que NO está en __null_fields: sigue siendo cadena vacía
        FILAS["observations"].append(["E2", "", "10", "0"])
        try:
            self.setUp()
            h.importar([self.zip], db=self.db)
            self.assertEqual(sqlite3.connect(self.db).execute("SELECT name FROM observations WHERE event_id='E2'").fetchone(), ("",))
        finally:
            FILAS["observations"].pop()

    def test_reimportar_no_aumenta_los_conteos(self):
        a = h.importar([self.zip], db=self.db)["filas"]
        b = h.importar([self.zip], db=self.db)["filas"]
        self.assertEqual(a, b)
        self.assertEqual(sqlite3.connect(self.db).execute("SELECT COUNT(*) FROM legacy_series").fetchone()[0], 2)

    def test_sha256_distinto_para_sin_tocar_la_base(self):
        h.importar([self.zip], db=self.db)
        mal = dict(self.datos)
        nombre = next(iter(mal))
        mal[nombre] = mal[nombre] + b"x"
        self.escribir_zip(self.zip, self.manifiesto, mal)
        with self.assertRaises(h.ErrorImportacion):
            h.importar([self.zip], db=self.db)
        self.assertEqual(sqlite3.connect(self.db).execute("SELECT COUNT(*) FROM legacy_series").fetchone()[0], 2,
                         "si algo no cuadra, la edición anterior queda como estaba")

    def test_cabecera_distinta_se_para(self):
        manifiesto = json.loads(json.dumps(self.manifiesto))
        manifiesto["esquema"]["observations"] = ["event_id", "otro_nombre", "price_minor", "admissible"]
        self.escribir_zip(self.zip, manifiesto, self.datos)
        with self.assertRaises(h.ErrorImportacion):
            h.importar([self.zip], db=self.db)


class CodigosSinEspecificaciones(unittest.TestCase):
    def test_especificaciones_no_son_codigo_de_modelo(self):
        import vigilante as v
        for nombre in ("Avène Ultra Fluid SPF50+ 50ml", "Logitech Ratón 8000DPI", "Casco SCORPION ECE22.06", "Bosch GKE 18V-40"):
            self.assertEqual(v.lista_codigos(nombre), [], nombre)
        self.assertFalse(v.mismo_producto("Avène Ultra Fluid Tinted SPF50+ 50ml", "La Roche Posay SPF50+, Protección Alta"))
        self.assertIn("EC685", v.lista_codigos("Cafetera De'Longhi Dedica EC685.M"))  # un modelo de verdad sigue valiendo


if __name__ == "__main__":
    unittest.main()
