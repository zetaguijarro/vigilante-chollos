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


class HilosDudosos(unittest.TestCase):
    """Zeta, 20/11/2026: "abre 2 o 3 hilos dudosos por candidata"."""
    P = {"nombre": "Samsung Galaxy S25 256GB Azul", "tienda": "mediamarkt.es"}

    def pagina(self, titulo, descripcion):
        sys.path.insert(0, str(RAIZ / "tests"))
        from test_vigilante import cm_html, hilo
        return cm_html([hilo(titulo, "Amazon", 700.0)]) + f"<div class='desc'>{descripcion}</div>"

    def test_clasifica_exacto_variante_distinto_y_posible(self):
        p = {"nombre": "Auriculares Sony WH-1000XM5 Negro", "tienda": "x.es"}
        clase, _ = v.comprobar_hilo(p, {}, self.pagina("Sony auriculares cancelación de ruido", "Modelo WH1000XM5, negro"))
        self.assertEqual(clase, "exacto", "el código está en la descripción aunque escrito sin guion")
        clase, _ = v.comprobar_hilo(p, {}, self.pagina("Sony WH-1000XM4", "Modelo anterior WH-1000XM4"))
        self.assertEqual(clase, "distinto")
        m = {"nombre": "Xiaomi Redmi Note 14 Pro 23117RA68G 256GB", "tienda": "x.es"}
        clase, motivo = v.comprobar_hilo(m, {}, self.pagina("Redmi Note 14 Pro 128GB", "Ref. 23117RA68G"))
        self.assertEqual(clase, "variante", motivo)
        sin_codigo = {"nombre": "Cafetera italiana de aluminio 6 tazas", "tienda": "x.es"}
        self.assertEqual(v.comprobar_hilo(sin_codigo, {}, self.pagina("Cafetera de aluminio", "6 tazas"))[0], "posible")

    def test_solo_se_abren_los_dudosos_y_como_mucho_tres(self):
        self.assertFalse(v.hilo_dudoso({"coincide": "mismo enlace"}))
        self.assertFalse(v.hilo_dudoso({"coincide": "coincide 100 %"}))
        self.assertTrue(v.hilo_dudoso({"coincide": "coincide 85 %"}))
        tmp = Path(tempfile.mkdtemp())
        orig = (v.DATOS, v.DB, v.descargar, v.time.sleep)
        v.DATOS, v.DB = tmp, tmp / "v.db"
        v.time.sleep = lambda s: None
        pedidas = []
        v.descargar = lambda url, **k: (pedidas.append(url) or self.pagina("Sony WH-1000XM4", "WH-1000XM4"), url)
        try:
            con = v.bd()
            p = {"nombre": "Auriculares Sony WH-1000XM5 Negro", "tienda": "x.es",
                 "_iguales": [{"id": i, "coincide": "coincide 80 %", "url": f"https://www.chollometro.com/ofertas/x-{i}",
                               "fecha": "01/11/2026", "precio": 200.0, "temp": 300, "tienda": "Amazon"} for i in range(5)]}
            distintos = v.comprobar_dudosos(con, p)
            self.assertEqual(len(pedidas), 3, "como mucho 3 por candidata")
            self.assertEqual(distintos, {0, 1, 2})
            self.assertNotIn("x-0", v.lineas_iguales(p), "un hilo comprobado como distinto no sale en el aviso")
            v.comprobar_dudosos(con, p)
            self.assertEqual(len(pedidas), 5, "caché de 24 h: los ya abiertos no se vuelven a pedir; sí los 2 que faltaban")
        finally:
            v.DATOS, v.DB, v.descargar, v.time.sleep = orig


if __name__ == "__main__":
    unittest.main()
