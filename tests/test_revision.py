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


class CompactoSinMercado(unittest.TestCase):
    """21/11/2026 08:40: un casi chollo subido por su ficha no traía 'idealo' y compacto() tumbaba los avisos."""

    def test_compacto_sin_idealo_ni_ean(self):
        import vigilante as v
        p = {"nombre": "Braun Series 9", "tienda": "miravia.es", "url": "https://x", "precio": 176.01,
             "estado": "nuevo"}
        c = v.compacto(p)
        self.assertEqual(c["p"], 176.01)
        self.assertNotIn("id", c)


class FichaReacoYCincoMensajes(unittest.TestCase):
    """Zeta, 21/11/2026: reacondicionados fuera mirando la ficha; la candidata en 5 mensajes con separador."""

    def test_reacondicionado_en_ficha(self):
        import vigilante as v
        self.assertTrue(v.estado_no_nuevo('<h1>iPhone 14 128GB - Reacondicionado</h1>', "", "https://t.es/p/1"))
        self.assertTrue(v.estado_no_nuevo('<h1>iPhone 14</h1>', "", "https://t.es/reacondicionados/iphone-14"))
        self.assertTrue(v.estado_no_nuevo('<h1>iPhone 14</h1>{"itemCondition": "https://schema.org/RefurbishedCondition"}',
                                          "", "https://t.es/p/1"))
        self.assertTrue(v.estado_no_nuevo('<h1>iPhone 14</h1>', "Precio 500 € Estado: Como nuevo", "https://t.es/p/1"))
        # Nuevo, con un enlace de menú a la sección de reacondicionados y una reseña: no cuenta.
        self.assertIsNone(v.estado_no_nuevo('<h1>iPhone 14</h1>{"itemCondition": "https://schema.org/NewCondition"}',
                                            "Menú Reacondicionados Ofertas · Llegó en estado excelente", "https://t.es/p/1"))

    def test_cinco_mensajes(self):
        import vigilante as v
        partes = v.mensajes_candidato(9, {"tienda": "t.es", "nombre": "Cafetera X", "precio": 50, "precio_antes": 80,
                                          "url": "https://t.es/x", "ch": "no", "_descripcion": "Cafetera de 15 bares"})
        self.assertEqual(len(partes), 6)
        self.assertIn("NUEVA CANDIDATA", partes[0])
        self.assertEqual(partes[1], "<b>Cafetera X</b>")  # el título solo, sin enlace ni nada
        self.assertIn("50 €", partes[2])
        self.assertIn("80 €", partes[2])
        self.assertIn("https://t.es/x", partes[3])
        self.assertIn("Chollometro", partes[4])
        self.assertIn("15 bares", partes[5])


class PerfumesYLiquidos(unittest.TestCase):
    """Zeta, 21/11/2026: perfumes, colonias y líquidos, sin falsos positivos al comparar con Chollometro."""

    def test_distintos(self):
        import vigilante as v
        a = "Dior Sauvage Eau de Toilette 100 ml"
        self.assertFalse(v.mismo_post(a, "Dior Sauvage Eau de Toilette 200 ml"))
        self.assertFalse(v.mismo_post(a, "Dior Sauvage Eau de Parfum 100 ml"))
        self.assertFalse(v.mismo_post(a, "Dior Sauvage Elixir 100 ml"))
        self.assertFalse(v.mismo_post("Paco Rabanne 1 Million EDT 100ml", "Paco Rabanne 1 Million EDT 100ml estuche"))
        self.assertFalse(v.mismo_post("Carolina Herrera 212 VIP EDP mujer 80 ml", "Carolina Herrera 212 VIP EDP hombre 80 ml"))
        self.assertFalse(v.mismo_post("Aceite de oliva virgen extra Carbonell 1 L", "Aceite de oliva virgen extra Carbonell 5 L"))

    def test_iguales(self):
        import vigilante as v
        self.assertIsNone(v.conflicto_liquido("Dior Sauvage EDT 100 ml", "Dior Sauvage Eau de Toilette 100ml"))
        self.assertIsNone(v.conflicto_liquido("Perfume Hugo Boss Bottled 3.4 fl oz", "Hugo Boss Bottled EDT 100 ml"))
        self.assertIsNone(v.conflicto_liquido("Cafetera Jocca 1,5 L", "Cafetera Jocca 1.5 l"))
        self.assertIsNone(v.conflicto_liquido("Auriculares Sony WH-1000XM5", "Sony WH-1000XM5 negros"))


class AmpliarCobertura(unittest.TestCase):
    """Zeta, 21/11/2026: ampliar Amazon y promociones; más tiempo de lectura en vez de quitar rutas."""

    def test_mas_vendidos_de_amazon_solo_dos_paginas(self):
        import vigilante as v
        u = "https://www.amazon.es/gp/bestsellers/kitchen"
        self.assertEqual(v.url_pagina(u, 6), u)  # antes ?page=6, que devolvía la primera otra vez
        self.assertIn("pg=2", v.pagina_siguiente_por_tienda(u))
        self.assertEqual(v.pagina_siguiente_por_tienda(u + "?pg=2"), u + "?pg=2")
        self.assertEqual(v.tope_paginas(u), 2)
        self.assertEqual(v.tope_paginas("https://www.amazon.es/deals"), 20)

    def test_apuntar_pagina_vuelve_a_la_primera(self):
        import sqlite3
        import vigilante as v
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
        u = "https://www.amazon.es/gp/bestsellers/kitchen"
        v.apuntar_pagina(con, "amazon.es", u, 1, 5, 100)
        self.assertEqual(v.pagina_de_inicio(con, "amazon.es", u), 1)

    def test_lectura_larga_por_tienda(self):
        import vigilante as v
        n = {"scroll_max": 8, "espera_ms": 2500, "por_tienda": {"amazon.es": {"scroll_max": 20, "pulsar_mas": True}}}
        self.assertEqual(v.ajuste_lectura(n, "https://www.amazon.es/deals"), (20, 2500, True))
        self.assertEqual(v.ajuste_lectura(n, "https://www.miravia.es/x"), (8, 2500, False))

    def test_hub_reconoce_promos_por_cantidad(self):
        import rutas
        for p in ("/supermercado/3x2/cat1/c", "/promociones/segunda-unidad-50", "/coupons", "/everydayessentials"):
            self.assertTrue(rutas.RE_CAMPANA.search(p), p)


class AmazonMinimoYTallas(unittest.TestCase):
    """Zeta, 21/11/2026: Amazon con "precio más bajo en 30/365 días" sale siempre; ropa y calzado con 3-4 tallas al
    precio de la oferta."""

    def test_insignia_por_tarjeta(self):
        import vigilante as v
        html = ('<div data-asin="B0AAAAAAA1"><a href="/dp/B0AAAAAAA1">Cafetera</a><span>Precio más bajo de los últimos '
                '30 días</span></div><div data-asin="B0AAAAAAA2"><a href="/dp/B0AAAAAAA2">Tostadora</a></div>'
                '<div data-asin="B0AAAAAAA3">Precio más bajo en 365 días</div>')
        self.assertEqual(v.insignias_minimo_amazon(html), {"B0AAAAAAA1": 30, "B0AAAAAAA3": 365})

    def test_tallas_al_precio_de_la_oferta(self):
        import vigilante as v
        p = {"nombre": "Zapatillas Nike Pegasus 41", "precio": 60.0}
        ficha = {"variantes": ["42: hay stock · 60 €", "43: hay stock · 60 €", "44: hay stock · 89.99 €",
                               "45: hay stock · 89.99 €", "41: agotada"]}
        ok, motivo = v.regla_tallas(p, ficha)
        self.assertFalse(ok)
        self.assertIn("2 más a otro precio", motivo)
        ficha["variantes"] += ["40: hay stock · 60 €", "39: hay stock · 59.5 €"]
        self.assertTrue(v.regla_tallas(p, ficha)[0])
        self.assertEqual(v.tallas_disponibles(p, ficha), ["42", "43", "40", "39"])

    def test_variantes_traen_precio(self):
        import vigilante as v
        ld = [{"hasVariant": [{"size": "M", "offers": {"price": "19.99", "availability": "InStock"}},
                              {"size": "L", "offers": {"price": "19.99", "availability": "OutOfStock"}}]}]
        self.assertEqual(v.variantes_de(ld, ""), ["M: hay stock · 19.99 €", "L: agotada"])


class PromoAmazonAlComprar(unittest.TestCase):
    """Zeta, 21/11/2026 (Scottex de Amazon): "Ahorra 70 % en 1 al comprar 2" no se reconocía."""

    def test_frases_de_amazon(self):
        import vigilante as v
        self.assertEqual(v.promo_multicompra("Ahorra 70 % en 1 al comprar 2 Comprar artículos seleccionados"),
                         ("2.ª unidad al -70 %", 2, 0.65))
        self.assertEqual(v.promo_multicompra("Ahorra 10 % al comprar 2"), ("-10 % comprando 2", 2, 0.9))
        self.assertEqual(v.promo_multicompra("Ahorra 50 % en 1 al comprar 3")[1:], (3, round((3 - 0.5) / 3, 4)))

    def test_ficha_de_amazon(self):
        import vigilante as v
        html = ('<span id="productTitle">Scottex Sensitive 42 rollos</span><div>20,93 €</div>'
                '<div>Ahorra 70&nbsp;% en 1 al comprar 2 Comprar art&iacute;culos seleccionados</div>')
        self.assertEqual(v.analizar_ficha(html, "https://www.amazon.es/dp/B07BQD5J18").get("promo"),
                         ("2.ª unidad al -70 %", 2, 0.65))


class CabeceraYCantidades(unittest.TestCase):
    """Zeta, 21/11/2026: el Hot Wheels Megagaraje del T-Rex del Radar ya estaba en Chollometro."""

    def test_cabecera_corta_en_barra(self):
        import vigilante as v
        self.assertEqual(v.cabecera("Hot Wheels City Megagaraje del T-Rex y 2 coches | Incluye 2 vehículos, JHL98"),
                         "Hot Wheels City Megagaraje del T-Rex y 2 coches")
        self.assertEqual(v.cabecera("Official | Xiaomi Robot Vacuum S40, 10 000 Pa"), "Xiaomi Robot Vacuum S40")

    def test_packs_con_otra_cantidad(self):
        import vigilante as v
        self.assertTrue(v.conflicto_cantidad("Dodot Toallitas Pure Aqua, 864 Toallitas (18 Packs x 48)",
                                             "1800 TOALLITAS Dodot 99% Aqua Toallitas 30 Paquetes De 60"))
        self.assertTrue(v.conflicto_cantidad("Scottex Original, 96 rollos", "Scottex Acolchado 63 rollos"))
        self.assertFalse(v.conflicto_cantidad("Dodot Aqua 18 packs x 48 toallitas", "Dodot Aqua 864 toallitas"))
