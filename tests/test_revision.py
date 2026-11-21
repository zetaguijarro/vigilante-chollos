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


class Roadmap(unittest.TestCase):
    def test_una_linea_por_pagina_con_tienda_categoria_y_ruta(self):
        import json, tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        orig = (v.DATOS, v.descargar, v.time.sleep)
        v.DATOS = tmp
        v.time.sleep = lambda s: None
        paginas = {"https://t.es/ofertas": '<a href="https://t.es/p/1">Producto Uno largo</a> 10,00 € <a rel="next" href="https://t.es/ofertas?page=2">',
                   "https://t.es/ofertas?page=2": "<html></html>"}
        v.descargar = lambda url, **k: (paginas.get(url, "<html></html>"), url)
        v._CTX.update(ronda="prueba 2026-11-20 22:00", grupo="rapidas", categoria="tecnologia")
        try:
            v.leer_tienda("t.es", {}, None, None, 3, "https://t.es/ofertas")
            lineas = [json.loads(l) for l in (tmp / "roadmap.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertGreaterEqual(len(lineas), 1)
            d = lineas[0]
            self.assertEqual((d["ronda"], d["categoria"], d["tienda"], d["modo"], d["pagina"]),
                             ("prueba 2026-11-20 22:00", "tecnologia", "t.es", "descarga", 1))
            self.assertIn("productos", d)
        finally:
            v.DATOS, v.descargar, v.time.sleep = orig
            v._CTX.update(ronda=None, grupo=None, categoria=None)


class ParecidosYTallas(unittest.TestCase):
    def test_tallas_disponibles_solo_en_ropa_y_calzado(self):
        ficha = {"variantes": ["38", "39: agotada", "40", "41", "Talla 42"]}
        self.assertEqual(v.tallas_disponibles({"nombre": "Zapatillas Nike Pegasus 41"}, ficha), ["38", "40", "41", "42"])
        self.assertEqual(v.tallas_disponibles({"nombre": "TV Samsung 55"}, ficha), [])

    def test_aviso_con_tallas_y_parecidos(self):
        p = {"tienda": "t.es", "nombre": "Zapatillas Nike Pegasus 41", "precio": 70, "ch": "no", "_tallas": ["40", "41", "42"],
             "_parecidos": [{"coincide": 75, "titulo": "Nike Pegasus 40", "precio": 65.0, "temp": 300, "tienda": "Amazon",
                             "fecha": "01/10/2026", "agotado": True, "url": "https://www.chollometro.com/ofertas/x-1"}]}
        texto = v.mensaje_candidato(1, p)
        self.assertIn("<b>Tallas disponibles:</b> 40, 41, 42", texto)
        self.assertIn("<b>Parecido (75 %):</b> Nike Pegasus 40", texto)


class AlertasYDescartadas(unittest.TestCase):
    def test_descartadas_con_motivo_a_su_pestana(self):
        import json, tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp())
        orig = (v.CONFIG, v.enviar_telegram, v.token_telegram, v.tema_id, v.log)
        v.log = lambda *a, **k: None  # no ensuciar data/log.txt de verdad
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["entrega"] = {"modo": "telegram", "chat_id": "1", "descartadas": True}
        (tmp / "t.json").write_text(json.dumps(cfg), encoding="utf-8")
        enviados = []
        v.CONFIG = tmp / "t.json"
        v.enviar_telegram = lambda tok, chat, texto, foto=None, teclado=None, **k: enviados.append((texto, k.get("hilo"))) or True
        v.token_telegram = lambda: "x"
        v.tema_id = lambda clave: 9
        try:
            v.log_descarte({"tienda": "t.es", "nombre": "Cafetera X", "precio": 45.0, "url": "https://t.es/x"},
                           "ficha no confirma, no se envía: t.es Cafetera X: la ficha marca 60 € y el listado 45 €")
            self.assertEqual(v.enviar_descartadas(), 1)
            texto, hilo = enviados[0]
            self.assertEqual(hilo, "descartadas")
            self.assertIn("Cafetera X", texto)
            self.assertIn("<b>Motivo:</b> ficha no confirma: t.es Cafetera X: la ficha marca 60 € y el listado 45 €", texto)
        finally:
            v.CONFIG, v.enviar_telegram, v.token_telegram, v.tema_id, v.log = orig


class PalabrasDeModeloYTfidf(unittest.TestCase):
    """#297 (21/11/2026): el Xiaomi Robot Vacuum S40 de AliExpress ya estaba como "Robot aspirador y friegasuelos Xiaomi
    S40 – 10.000 Pa, 5200 mAh" en la misma tienda al mismo precio."""

    def test_mismo_robot_en_ingles_y_en_espanol(self):
        a = ("Official | Xiaomi Robot Vacuum S40, Ventilador de succión tipo huracán de 10 000 Pa, Batería de duración "
             "prolongada con 5200 mAh, Tecnología antienredos multidimensional")
        self.assertTrue(v.mismo_post(a, "Robot aspirador y friegasuelos Xiaomi S40 – 10.000 Pa, láser LDS, 5200 mAh (180 min, app)"))
        self.assertFalse(v.mismo_post(a, "Robot friegasuelos - Xiaomi Vacuum S40 Pro, 160 W, Ventilador de Succión 15000 Pa"),
                         "el S40 Pro es otro")

    def test_lo_que_no_debe_casar(self):
        self.assertFalse(v.mismo_post("Gigabyte B760 Gaming X DDR4 Gen5 - Placa base 1700 ATX", "Placa Base Gigabyte B760 Gaming X DDR5"))
        self.assertFalse(v.mismo_post("Tablet Samsung Galaxy Tab A11 4G 8,7\" 8GB 128GB Plata", "Samsung Galaxy Tab A11 Wi-Fi"))
        self.assertFalse(v.mismo_post("Nintendo Switch 2", "Dragon Quest I & II HD-2D Remake - Nintendo Switch 2"))
        self.assertFalse(v.mismo_post("TV OLED 163cm (65\") Samsung TQ65S95HFTXXC 4K Smart TV",
                                      "TV OLED 65\" - Samsung TQ65S85FAEXXC, OLED 4K, NQ4 AI Gen2"))

    def test_especificaciones_y_similitud(self):
        self.assertIn("10000pa", v._tokens_er("succión de 10 000 Pa"))
        self.assertIn("5200mah", v._tokens_er("batería 5.200 mAh"))
        self.assertGreater(v.similitud_idf("HP 305 negro 3YM61AE", "HP 305 Negro, 3YM61AE, cartucho"), 50)
