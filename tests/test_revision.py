"""Revisión externa del 20/11/2026: "misma tienda" sin subcadenas y señales de título sospechoso."""
import sys
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))
import vigilante as v  # noqa: E402


class MismaTienda(unittest.TestCase):
    def test_casos_de_la_revision(self):
        self.assertTrue(v.misma_tienda("mi.com/es", "", "Xiaomi"), "raíz mi y comercio Xiaomi: misma tienda")
        self.assertTrue(v.misma_tienda("mi.com/es", "www.mi.com", ""), "raíz mi y host mi.com")
        self.assertFalse(v.misma_tienda("game.es", "", "Gameplanet"), "game no casa por subcadena con Gameplanet")
        self.assertTrue(v.misma_tienda("game.es", "www.game.es", "GAME"))
        self.assertTrue(v.misma_tienda("elcorteingles.es", "www.hipercor.es", "Hipercor"), "alias El Corte Inglés / Hipercor")
        self.assertTrue(v.misma_tienda("hipercor.es", "", "El Corte Inglés"))

    def test_amazon_por_pais_y_acortador(self):
        self.assertTrue(v.misma_tienda("amazon.es", "amzn.eu", "Amazon"), "el acortador de Amazon con comercio Amazon")
        self.assertFalse(v.misma_tienda("amazon.es", "www.amazon.it", "Amazon.it"), "Amazon Italia es otra tienda")
        self.assertFalse(v.misma_tienda("mediamarkt.es", "www.pccomponentes.com", "PcComponentes"))


class TitulosSospechosos(unittest.TestCase):
    def test_senales_sin_descartar(self):
        ps = [{"nombre": f"Zapatillas Nike Pegasus {i}"} for i in range(6)]
        ps += [{"nombre": "Envío gratis en pedidos de más de 50 €"}, {"nombre": "Ver más"}, {"nombre": "Ver más"},
               {"nombre": "Ver más"}, {"nombre": "Nike " + "muy largo " * 40}]
        motivos = {n[:12]: m for n, m in v.titulos_sospechosos(ps)}
        self.assertIn("precio o envío", motivos["Envío gratis"])
        self.assertIn("repetido", motivos["Ver más"])
        self.assertIn("mucho más largo", motivos["Nike muy lar"])
        self.assertNotIn("Zapatillas N", motivos)
        colores = [{"nombre": "Nike V5 Rnr - Mujer Zapatillas"}] * 4 + [{"nombre": "adidas Samba OG - Zapatillas"}] * 3
        self.assertEqual(v.titulos_sospechosos(colores), [], "el mismo modelo en varios colores no es sospechoso")

    def test_un_nombre_largo_legitimo_no_es_sospechoso_si_todos_son_largos(self):
        largo = "Portátil gaming ASUS TUF Gaming A15 FA507NV-LP023 Ryzen 7 7735HS 16GB 512GB SSD RTX 4060 15,6 FHD 144Hz"
        ps = [{"nombre": largo + f" v{i}"} for i in range(5)]
        self.assertEqual(v.titulos_sospechosos(ps), [])


class CholloPrevioSinCodigosFalsos(unittest.TestCase):
    """#295 (20/11/2026): una cafetera Jocca salió con «Ya fue chollo: 2650° a 51,77 €», que era un perfume Azzaro
    The Most Wanted 100ml: "100ML" contaba como código de modelo."""

    def test_capacidades_no_son_codigo(self):
        n = "Cafetera portátil - Jocca 2902, 3 en 1: Compatible con cápsulas Nespresso, Dolce Gusto y café molido, 15 bar, 100ml"
        self.assertNotIn("100ML", v.lista_codigos(n))
        for c in ("250ML", "33CL", "500MG", "16OZ"):
            self.assertEqual(v.lista_codigos(f"Producto marca {c}"), [], c)

    def test_el_aviso_no_dice_copia_local_si_tambien_se_busco(self):
        texto = v.mensaje_candidato(1, {"tienda": "t.es", "nombre": "x", "precio": 5, "ch": "no"})
        self.assertIn("sin hilos de este producto (copia local y buscador)", texto)


class TituloDeTarjeta(unittest.TestCase):
    def test_el_titulo_corto_gana_al_reclamo_largo(self):
        html = ('<div><a href="https://www.tienda.es/producto/cafetera-x-123.html">'
                '<h3>Cafetera De\'Longhi Dedica EC685</h3>'
                '<span>Llévatela hoy con entrega en 24 horas y devolución gratuita durante 30 días</span></a>'
                '<span class="precio">129,99 €</span></div>'
                '<div><a href="https://www.tienda.es/producto/tostadora-y-456.html">Tostadora Philips HD2581 2 ranuras</a>'
                '<span class="precio">24,99 €</span></div>')
        ps = {p["url"].rsplit("/", 1)[-1]: p["name"] for p in v.tarjetas_html(html, "https://www.tienda.es/ofertas")}
        self.assertEqual(ps.get("cafetera-x-123.html"), "Cafetera De'Longhi Dedica EC685")
        self.assertEqual(ps.get("tostadora-y-456.html"), "Tostadora Philips HD2581 2 ranuras", "sin título, el texto del enlace")


if __name__ == "__main__":
    unittest.main()
