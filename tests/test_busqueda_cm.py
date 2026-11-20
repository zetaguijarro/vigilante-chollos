"""Búsqueda en Chollometro con el método de Zeta (20/11/2026): de lo preciso a lo amplio, sin prometer cobertura."""
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))
import vigilante as v  # noqa: E402


class Consultas(unittest.TestCase):
    def test_de_lo_preciso_a_lo_amplio(self):
        p = {"nombre": "Auriculares Sony WH-1000XM5 inalámbricos negro", "tienda": "amazon.es", "ean": "4548736132610",
             "url": "https://www.amazon.es/dp/B09Y2MYL5C"}
        q = v.consultas_chollometro(p)
        self.assertEqual(q[0], "4548736132610", "primero el EAN")
        self.assertEqual(q[1], "B09Y2MYL5C", "después el ASIN")
        self.assertTrue(any("WH-1000XM5" in x for x in q) and any("WH1000XM5" in x for x in q), q)
        self.assertEqual(len(q), len({x.lower() for x in q}), "sin repetir")

    def test_variantes_del_codigo_sin_quitar_sufijos(self):
        self.assertEqual(v.variantes_codigo("WH-1000XM5"), ["WH-1000XM5", "WH1000XM5"])
        self.assertEqual(v.variantes_codigo("WH1000XM5"), ["WH1000XM5", "WH-1000XM5"])
        self.assertNotIn("WH-1000XM", v.variantes_codigo("WH-1000XM5"))


class Busqueda(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.orig = (v.DATOS, v.DB, v.descargar, v.time.sleep)
        v.DATOS, v.DB = self.tmp, self.tmp / "v.db"
        v.time.sleep = lambda s: None
        self.pedidas = []

    def tearDown(self):
        v.DATOS, v.DB, v.descargar, v.time.sleep = self.orig

    def test_sin_resultados_prueba_varias_consultas_y_lo_dice_en_el_aviso(self):
        v.descargar = lambda url, **k: (self.pedidas.append(url) or "<html></html>", url)
        con = v.bd()
        p = {"nombre": "Sony WH-1000XM5 negro", "tienda": "mediamarkt.es"}
        self.assertEqual(v.buscar_en_chollometro(con, p), 0)
        self.assertGreaterEqual(len(self.pedidas), 2, "no se queda en una sola consulta")
        self.assertLessEqual(len(self.pedidas), v.CM_CONSULTAS_MAX)
        texto = v.lineas_iguales(p)
        self.assertIn("no he localizado coincidencias en las búsquedas realizadas", texto)
        self.assertIn("no demuestra que no existan", texto)
        n = len(self.pedidas)
        v.buscar_en_chollometro(con, p)
        self.assertEqual(len(self.pedidas), n, "caché de 6 h por consulta")

    def test_para_en_cuanto_encuentra_el_mismo_producto(self):
        sys.path.insert(0, str(RAIZ / "tests"))
        from test_vigilante import cm_html, hilo
        h = hilo("Sony WH-1000XM5 Auriculares Bluetooth", "Amazon", 249.0, temp=400, dias=200, caducado=True)
        v.descargar = lambda url, **k: (self.pedidas.append(url) or cm_html([h]), url)
        con = v.bd()
        p = {"nombre": "Sony WH-1000XM5 negro", "tienda": "mediamarkt.es"}
        self.assertEqual(v.buscar_en_chollometro(con, p), 1)
        self.assertEqual(len(self.pedidas), 1, "con el mismo producto en la primera consulta, no hace falta más")
        self.assertEqual(con.execute("SELECT caducado FROM cm WHERE id=?", (h["threadId"],)).fetchone(), (1,),
                         "los agotados también se guardan: son antecedentes")


if __name__ == "__main__":
    unittest.main()
