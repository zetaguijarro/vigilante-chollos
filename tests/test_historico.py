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


class LecturasDelVigilante(unittest.TestCase):
    """Las lecturas del vigilante van a `lecturas_vigilante`, aparte de la edición de Zeta (que `importar` sustituye)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "h.db"
        self.ahora = "2026-11-20T14:05:00+01:00"
        self.lec = [{"tienda": "mediamarkt.es", "nombre": "Samsung TV QN90D 55", "url": "https://www.mediamarkt.es/es/product/x-1.html",
                     "precio": 799.0, "precio_antes": 999.0, "clave": "mediamarkt.es|x", "ahora": self.ahora}]

    def test_registrar_es_idempotente_y_marca_que_no_esta_validado(self):
        self.assertEqual(h.registrar_lecturas(self.lec, db=self.db), 1)
        self.assertEqual(h.registrar_lecturas(self.lec, db=self.db), 0, "el mismo producto, precio y minuto es el mismo evento")
        con = sqlite3.connect(self.db)
        fila = con.execute("SELECT price_minor, currency, identity_status, price_scope, admissible, source_id, conditions_json "
                           "FROM lecturas_vigilante").fetchone()
        self.assertEqual(fila[:6], (79900, "EUR", "URL_ONLY_UNRESOLVED_VARIANT", "listing_price", "0", "vigilante-rondas"))
        self.assertEqual(json.loads(fila[6]), {"precio_antes": 999.0})
        self.assertEqual(con.execute("SELECT COUNT(*) FROM sources WHERE id='vigilante-rondas'").fetchone()[0], 1)

    def test_reimportar_la_edicion_no_borra_las_lecturas_del_vigilante(self):
        zip_ = Path(tempfile.mkdtemp()) / "p1.zip"
        base = ImportacionHistorico("test_reimportar_no_aumenta_los_conteos")
        base.setUp()
        h.registrar_lecturas(self.lec, db=base.db)
        h.importar([base.zip], db=base.db)
        con = sqlite3.connect(base.db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM lecturas_vigilante").fetchone()[0], 1)
        self.assertEqual(con.execute("SELECT origen_dato, COUNT(*) FROM observaciones_todas GROUP BY 1 ORDER BY 1").fetchall(),
                         [("vigilante", 1), ("zeta", 1)], "la vista junta las dos sin mezclarlas")

    def test_sin_precio_no_se_registra(self):
        self.assertEqual(h.registrar_lecturas([{**self.lec[0], "precio": None}], db=self.db), 0)


class RutasYPaginacion(unittest.TestCase):
    """Cómo pagina cada tienda que Zeta lee (prompt "Cómo leo cada tienda", 20/11/2026)."""

    def test_paginacion_por_tienda_desde_la_primera_pagina(self):
        import vigilante as v
        casos = {
            "https://www.carrefour.es/gaming/black-friday/cat28765042/c": "https://www.carrefour.es/gaming/black-friday/cat28765042/c?offset=24",
            "https://www.carrefour.es/gaming/black-friday/cat28765042/c?offset=24": "https://www.carrefour.es/gaming/black-friday/cat28765042/c?offset=48",
            "https://www.decathlon.es/es/deals/descuentos-hombre": "https://www.decathlon.es/es/deals/descuentos-hombre?from=40&size=40",
            "https://www.decathlon.es/es/deals/descuentos-hombre?from=40&size=40": "https://www.decathlon.es/es/deals/descuentos-hombre?from=80&size=40",
            "https://www.coolmod.com/black-friday/monitores/": "https://www.coolmod.com/black-friday/monitores/?pagina=2",
            "https://www.coolmod.com/black-friday/monitores/?pagina=2": "https://www.coolmod.com/black-friday/monitores/?pagina=3",
            "https://www.sportsdirect.es/black-friday/nike": "https://www.sportsdirect.es/black-friday/nike?dcp=2",
            "https://www.privatesportshop.es/event/30-seven": "https://www.privatesportshop.es/event/30-seven?p=2",
            "https://www.deporte-outlet.es/mejores-ofertas?p=3": "https://www.deporte-outlet.es/mejores-ofertas?p=4",
            "https://www.elcorteingles.es/black-friday/electronica/tv/": "https://www.elcorteingles.es/black-friday/electronica/tv/2/",
            "https://www.elcorteingles.es/black-friday/electronica/tv/2/": "https://www.elcorteingles.es/black-friday/electronica/tv/3/",
        }
        for url, esperada in casos.items():
            self.assertEqual(v.pagina_siguiente_por_tienda(url), esperada, url)
        self.assertEqual(v.pagina_siguiente_por_tienda("https://www.toysrus.es/LEGO/c/LEGO?q=%3Arelevance&page=2"),
                         "https://www.toysrus.es/LEGO/c/LEGO?q=:relevance&page=3")
        self.assertIsNone(v.pagina_siguiente_por_tienda("https://www.otratienda.es/ofertas"))
        self.assertEqual(v.siguiente_pagina("<html></html>", "https://www.coolmod.com/black-friday/monitores/"),
                         "https://www.coolmod.com/black-friday/monitores/?pagina=2")

    def test_sembrar_rutas_de_campana_sin_duplicar(self):
        import vigilante as v
        tmp = Path(tempfile.mkdtemp())
        orig = v.DATOS
        v.DATOS = tmp
        try:
            (tmp / "rutas.json").write_text(json.dumps({"rutas": {"coolmod.com": ["https://www.coolmod.com/black-friday/monitores/"]},
                                                       "pendientes_de_alta": {"nike.com/es": ["https://www.nike.com/es/w/x"]}}), encoding="utf-8")
            v.guardar_catalogo({"coolmod.com": ["https://www.coolmod.com/otra/"]})
            self.assertEqual(v.sembrar_rutas_campana(tmp / "rutas.json"), {"coolmod.com": 1})
            self.assertEqual(v.sembrar_rutas_campana(tmp / "rutas.json"), {}, "repetirlo no cambia nada")
            self.assertEqual(len(v.urls_catalogo()["coolmod.com"]), 2)
            self.assertNotIn("nike.com/es", v.urls_catalogo(), "las pendientes de alta solo se nombran")
        finally:
            v.DATOS = orig

    def test_el_archivo_de_rutas_de_zeta_es_valido(self):
        datos = json.loads((RAIZ / "config" / "rutas_campana.json").read_text(encoding="utf-8"))
        self.assertGreater(len(datos["rutas"]["coolmod.com"]), 40)
        self.assertGreater(len(datos["rutas"]["privatesportshop.es"]), 80)
        for rutas in datos["rutas"].values():
            for u in rutas:
                self.assertTrue(u.startswith("https://"), u)


if __name__ == "__main__":
    unittest.main()
