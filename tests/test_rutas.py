"""Rutas de campaña (src/rutas.py): rendimiento, retirada y descubrimiento desde los hubs (Zeta, 20/11/2026)."""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))
import rutas as r  # noqa: E402
import vigilante as v  # noqa: E402


def con_memoria():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    con.execute("CREATE TABLE productos (clave TEXT PRIMARY KEY, tienda TEXT, nombre TEXT, ean TEXT, url TEXT)")
    return con


class RendimientoYRetirada(unittest.TestCase):
    def setUp(self):
        self.con = con_memoria()
        self.t, self.u = "coolmod.com", "https://www.coolmod.com/black-friday/monitores/"

    def test_tres_lecturas_vacias_seguidas_retiran_la_ruta(self):
        self.assertIsNone(r.registrar_lectura(self.con, self.t, self.u, 0))
        self.assertIsNone(r.registrar_lectura(self.con, self.t, self.u, 0))
        self.assertIn("3 lecturas seguidas sin productos", r.registrar_lectura(self.con, self.t, self.u, 0))
        estado = self.con.execute("SELECT estado FROM rutas WHERE url=?", (self.u,)).fetchone()[0]
        self.assertEqual(estado, "retirada")

    def test_una_lectura_con_productos_reinicia_las_vacias(self):
        r.registrar_lectura(self.con, self.t, self.u, 0)
        r.registrar_lectura(self.con, self.t, self.u, 0)
        r.registrar_lectura(self.con, self.t, self.u, 30, nuevos=5, pasan=2)
        self.assertIsNone(r.registrar_lectura(self.con, self.t, self.u, 0))
        fila = self.con.execute("SELECT lecturas, productos_tot, nuevos_tot, filtros_tot, vacias_seguidas FROM rutas").fetchone()
        self.assertEqual(fila, (4, 30, 5, 2, 1))

    def test_dos_redirecciones_a_la_portada_retiran(self):
        self.assertIsNone(r.registrar_lectura(self.con, self.t, self.u, 0, "redirige a la portada"))
        self.assertIn("redirige a la portada", r.registrar_lectura(self.con, self.t, self.u, 0, "redirige a la portada"))

    def test_un_fallo_normal_no_cuenta_como_vacia(self):
        for _ in range(5):
            self.assertIsNone(r.registrar_lectura(self.con, self.t, self.u, 0, "HTTP Error 503"))

    def test_retirada_reciente_no_se_resiembra_hasta_14_dias(self):
        r.marcar_retirada(self.con, self.t, self.u, "prueba")
        self.assertTrue(r.retirada_reciente(self.con, self.t, self.u))
        antes = (datetime.now() - timedelta(days=15)).isoformat(timespec="minutes")
        self.con.execute("UPDATE rutas SET retirada=?", (antes,))
        self.assertFalse(r.retirada_reciente(self.con, self.t, self.u))

    def test_factor_sube_con_lo_que_da_y_baja_si_no_da_nada(self):
        buena, mala, nueva = "https://x.es/buena", "https://x.es/mala", "https://x.es/nueva"
        for _ in range(4):
            r.registrar_lectura(self.con, "x.es", buena, 40, pasan=3)
            r.registrar_lectura(self.con, "x.es", mala, 40, pasan=0)
        f = r.factor_rutas(self.con, "x.es", [buena, mala, nueva])
        self.assertEqual(f[nueva], 1.0, "una ruta con menos de 3 lecturas no se juzga")
        self.assertGreater(f[buena], 2.0)
        self.assertEqual(f[mala], 0.5)

    def test_el_orden_del_catalogo_lee_mas_las_rutas_que_dan(self):
        lista = ["https://x.es/a", "https://x.es/b"]
        orden = v.orden_catalogo(lista, None, factor={"https://x.es/a": 0.5, "https://x.es/b": 2.5})
        self.assertGreater(orden.count("https://x.es/b"), orden.count("https://x.es/a"))
        self.assertEqual(v.orden_catalogo(lista, None), lista, "sin pesos ni factores, la lista tal cual")


class RedireccionAPortada(unittest.TestCase):
    def test_detecta_la_portada_y_la_de_idioma(self):
        self.assertTrue(r.redirige_a_portada("https://www.carrefour.es/tv-y-sonido/black-friday/cat1/c", "https://www.carrefour.es/"))
        self.assertTrue(r.redirige_a_portada("https://www.mediamarkt.es/es/list/x-black-friday", "https://www.mediamarkt.es/es"))
        self.assertFalse(r.redirige_a_portada("https://www.mediamarkt.es/es/list/x", "https://www.mediamarkt.es/es/list/x?page=1"))
        self.assertFalse(r.redirige_a_portada("https://www.x.es/ofertas", "https://www.x.es/"), "una sola sección: no es una ruta de campaña")
        self.assertFalse(r.redirige_a_portada("https://www.x.es/a/b", "https://www.otro.com/"), "otro dominio: no se compara")

    def test_leer_tienda_devuelve_error_si_la_ruta_acaba_en_la_portada(self):
        orig = v.descargar
        v.descargar = lambda url, **k: ("<html><body>portada</body></html>", "https://www.tienda.es/")
        try:
            t, u, productos, error = v.leer_tienda("tienda.es", {}, None, None, 1, "https://www.tienda.es/black-friday/tv/")
            self.assertEqual((productos, error), ([], "redirige a la portada"))
            t, u, productos, error = v.leer_tienda("tienda.es", {}, None, None, 1)  # la página principal no se comprueba
            self.assertNotEqual(error, "redirige a la portada")
        finally:
            v.descargar = orig


class Hubs(unittest.TestCase):
    HUB = ('<html><body>'
           '<a href="/es/list/televisores-black-friday?promotions=Semanas%20Black">TV</a>'
           '<a href="/es/campaign/outlet">Outlet</a>'
           '<a href="/es/product/samsung-tv-123.html">Un producto</a>'
           '<a href="https://www.otra.com/black-friday">Otra tienda</a>'
           '<a href="/es/aviso-legal">Legal</a>'
           '<a href="/es/campaign/black-friday#x">El propio hub</a></body></html>')

    def test_enlaces_candidatos_solo_campanas_de_la_misma_tienda(self):
        sal = r.enlaces_candidatos(v, self.HUB, "https://www.mediamarkt.es/es/campaign/black-friday", "mediamarkt.es")
        self.assertEqual(sal, ["https://www.mediamarkt.es/es/list/televisores-black-friday?promotions=Semanas%20Black",
                               "https://www.mediamarkt.es/es/campaign/outlet"])

    def test_solo_entra_la_ruta_que_trae_productos_nuevos(self):
        tmp = Path(tempfile.mkdtemp())
        orig = (v.DATOS, r.time.sleep, v.leer_tienda)
        v.DATOS = tmp
        r.time.sleep = lambda s: None
        con = con_memoria()
        for i in range(20):  # el vigilante ya conoce los productos de /outlet
            con.execute("INSERT INTO productos VALUES (?,?,?,?,?)", (f"mediamarkt.es|https://m/p{i}", "mediamarkt.es", f"P{i}", None, f"https://m/p{i}"))

        class Nav:
            def leer(self, url, completo=False):
                return TestHub.HUB, "https://www.mediamarkt.es/es/campaign/black-friday", []

        TestHub = Hubs

        def leer_falso(tienda, urls, cat, nav, pags, url):
            if "outlet" in url:
                return tienda, url, [{"nombre": f"P{i}", "url": f"https://m/p{i}", "precio": 10.0} for i in range(20)], None
            return tienda, url, [{"nombre": f"Nuevo{i}", "url": f"https://m/n{i}", "precio": 10.0} for i in range(12)], None
        v.leer_tienda = leer_falso
        try:
            v.guardar_catalogo({"mediamarkt.es": ["https://www.mediamarkt.es/es/list/ya-conocida"]})
            anadidas = r.descubrir_hub(con, v, "mediamarkt.es", "https://www.mediamarkt.es/es/campaign/black-friday", Nav())
            self.assertEqual([u for u, _, _ in anadidas], ["https://www.mediamarkt.es/es/list/televisores-black-friday?promotions=Semanas%20Black"])
            self.assertIn("https://www.mediamarkt.es/es/list/televisores-black-friday?promotions=Semanas%20Black",
                          v.urls_catalogo()["mediamarkt.es"])
            self.assertNotIn("https://www.mediamarkt.es/es/campaign/outlet", v.urls_catalogo()["mediamarkt.es"])
            motivo = con.execute("SELECT motivo FROM rutas WHERE url LIKE '%outlet'").fetchone()[0]
            self.assertIn("no aporta", motivo)
        finally:
            v.DATOS, r.time.sleep, v.leer_tienda = orig


class SembrarSinRetiradas(unittest.TestCase):
    def test_una_ruta_retirada_hace_poco_no_se_vuelve_a_sembrar(self):
        tmp = Path(tempfile.mkdtemp())
        orig = v.DATOS
        v.DATOS = tmp
        try:
            (tmp / "rutas.json").write_text(json.dumps({"rutas": {"coolmod.com": ["https://www.coolmod.com/a/", "https://www.coolmod.com/b/"]}}),
                                            encoding="utf-8")
            con = con_memoria()
            r.marcar_retirada(con, "coolmod.com", "https://www.coolmod.com/a/", "3 lecturas seguidas sin productos")
            v.guardar_catalogo({})
            self.assertEqual(v.sembrar_rutas_campana(tmp / "rutas.json", con=con), {"coolmod.com": 1})
            self.assertEqual(v.urls_catalogo()["coolmod.com"], ["https://www.coolmod.com/b/"])
        finally:
            v.DATOS = orig


if __name__ == "__main__":
    unittest.main()
