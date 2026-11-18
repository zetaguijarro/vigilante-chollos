"""Pruebas del vigilante. Ejecutar desde la raíz:  python -m unittest discover -s tests -v"""
import sys
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
CONF = RAIZ / "config"
sys.path.insert(0, str(RAIZ / "src"))

import gzip
import io
import os
import json
import shutil
import sqlite3
import tempfile
import threading
import re
import time
import zlib
import unittest
from collections import Counter
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import vigilante as v


def escape_attr(s):
    return s.replace("&", "&amp;").replace('"', "&quot;")


def tienda_html(productos):
    """Página de tienda con JSON-LD (tipo MediaMarkt) + __NEXT_DATA__ (tipo PcComponentes) + ruido."""
    ld = [{"@type": "Product", "name": p["n"], "brand": {"name": p.get("m", "")},
           "gtin13": p.get("e", ""), "url": p.get("u", "/p/" + p["n"].replace(" ", "-")),
           "image": "/img.jpg",
           "offers": {"price": str(p["p"]).replace(".", ","),
                      "priceSpecification": {"priceType": "https://schema.org/StrikethroughPrice",
                                             "price": str(p["a"]).replace(".", ",")} if p.get("a") else None}}
          for p in productos if p.get("ld", True)]
    nxt = [{"title": p["n"], "price": {"value": p["p"], "listPrice": p.get("a")},
            "pdpUrl": p.get("u", "/p/" + p["n"].replace(" ", "-"))}
           for p in productos if not p.get("ld", True)]
    ruido = [{"name": "Peso", "value": "2,5 kg"}, {"name": "Valoración", "ratingValue": 4.5}]
    return (f'<script type="application/ld+json">{json.dumps({"@type": "ItemList", "itemListElement": ld})}</script>'
            f'<script id="__NEXT_DATA__" type="application/json">{json.dumps({"p": {"items": nxt, "f": ruido}})}</script>'
            '<script>var x = 1;</script>')


def hilo(titulo, tienda="Amazon", precio=10.0, temp=50, dias=0, caducado=False):
    """Un post de Chollometro como viene en sus páginas."""
    return {"threadId": zlib.crc32(f"{titulo}|{tienda}|{precio}".encode()), "title": titulo, "price": precio,
            "temperature": temp, "publishedAt": int(time.time() - dias * 86400), "isExpired": caducado,
            "merchant": {"merchantName": tienda}, "linkHost": f"www.{tienda.lower().replace(' ', '')}.es"}


def cm_html(hilos):
    return '<script type="application/json">' + json.dumps({"threads": hilos}) + "</script>"


RELLENO_CM = hilo("Cafetera Nespresso Vertuo Pop", "Amazon", 59.0)  # /nuevos nunca está vacío


class Red:
    """Simula Internet: tiendas, Idealo y Chollometro. Cuenta las llamadas."""

    def __init__(self):
        self.tiendas = {}      # dominio -> lista de productos
        self.idealo = {}       # texto que aparece en la consulta -> precio
        self.chollometro = []  # posts que salen en /nuevos y en la portada (ver hilo())
        self.caidas = set()
        self.llamadas = {"tienda": 0, "idealo": 0, "chollometro": 0, "cm_busqueda": 0}
        self.pedidas = []      # URLs de tienda pedidas, en orden

    def __call__(self, url, timeout=20):
        if "idealo" in url:
            self.llamadas["idealo"] += 1
            for clave, precio in self.idealo.items():
                if clave in url:
                    return (f'<script type="application/ld+json">{{"@type":"Product","name":"{clave}",'
                            f'"offers":{{"lowPrice":"{precio}"}}}}</script>'), url
            return "<html>sin resultados</html>", url
        if "chollometro" in url:
            self.llamadas["chollometro"] += 1
            if "/search" in url:  # desde el 17/11/2026 se usa (Zeta: robots.txt en ninguna web)
                self.llamadas["cm_busqueda"] += 1
                return cm_html(self.chollometro), url
            if "page=" in url and "page=1" not in url:
                return "<html></html>", url
            if url.rstrip("/").endswith(("/nuevos", "chollometro.com")):
                return cm_html([RELLENO_CM] + self.chollometro), url
            return "<html></html>", url
        self.llamadas["tienda"] += 1
        self.pedidas.append(url)
        for dom, prods in self.tiendas.items():
            if dom in url:
                if dom in self.caidas:
                    raise OSError("HTTP 403 bloqueado")
                return tienda_html(prods), url
        raise OSError("dominio desconocido")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copy(CONF / "marcas.txt", self.tmp / "marcas.txt")
        cfg = json.loads((CONF / "tiendas.json").read_text(encoding="utf-8"))
        cfg["categorias"]["tecnologia"]["tiendas"] = ["mediamarkt.es", "pccomponentes.com"]
        for c in cfg["categorias"].values():  # las rondas simuladas no abren el navegador real
            c["navegador"] = []
        cfg["entrega"] = {"modo": "hermes"}  # estas pruebas miran lo que recibe la IA
        # Muchas pruebas usan productos sin precio en Idealo y miran otras reglas (cupos, silencio...):
        # para ellas vale la regla antigua del descuento mínimo. La nueva tiene su propia prueba.
        cfg["filtros_codigo"]["exigir_precio_mercado"] = False
        cfg["filtros_codigo"]["max_por_tienda_y_ronda"] = 1000  # las pruebas de cupos usan una sola tienda
        cfg["filtros_codigo"]["respetar_robots"] = True  # las pruebas de robots.txt miran el modo estricto
        cfg["filtros_codigo"]["confirmar_ficha"] = False  # la red simulada no tiene fichas; tiene su prueba
        cfg["filtros_codigo"]["ofertas_refresco_dias"] = 0  # el refresco semanal de campañas tiene su prueba
        # Igual con el histórico propio (necesita días de seguimiento): tiene sus propias pruebas.
        cfg["filtros_codigo"].update({"historial_propio_dias": 0, "error_precio_dias_min": 0,
                                      "error_precio_min_calientes": 0,  # tienda pequeña: tiene su prueba
                                      "solo_tiendas_con_demanda": False, "descuento_real_min": 0,
                                      "navegador_si_vacia": False,  # tienen su propia prueba
                                      "idealo_navegador": False,    # no abrir Chrome de verdad
                                      "camelcamelcamel": False})
        for c in cfg["categorias"].values():
            c.pop("historial_propio_dias", None)
        (self.tmp / "tiendas.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        self._orig = {k: getattr(v, k) for k in ("DATOS", "CONFIG", "MARCAS", "URLS", "DB", "LOG",
                                                 "descargar", "en_horario", "CM_REFRESCO_MIN")}
        v.CM_REFRESCO_MIN = 0  # las rondas simuladas van seguidas: la copia de Chollometro se pone al día siempre
        v.DATOS = self.tmp / "vigilante_data"
        v.CONFIG, v.MARCAS = self.tmp / "tiendas.json", self.tmp / "marcas.txt"
        v.URLS, v.DB, v.LOG = v.DATOS / "urls.json", v.DATOS / "vistos.db", v.DATOS / "log.txt"
        self.red = Red()
        v.descargar = self.red
        v.en_horario = lambda cfg: True
        self._sleep, v.time.sleep = v.time.sleep, lambda s: None

    def tearDown(self):
        for k, val in self._orig.items():
            setattr(v, k, val)
        v.time.sleep = self._sleep
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ronda(self, cat="tecnologia"):
        buf = io.StringIO()
        with redirect_stdout(buf):
            v.ronda(cat)
        salida = buf.getvalue().strip().splitlines()
        self.assertEqual(len(salida), 1, "debe imprimir exactamente una línea")
        datos = json.loads(salida[0])
        return [] if datos == {"wakeAgent": False} else datos["c"]


# ----------------------------------------------------------- piezas sueltas

class ModosNavegador(unittest.TestCase):
    """modos: la misma página con cada modo del navegador; un reto para los modos que quedan (17/11/2026)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = v.DATOS, v.cargar_config, v.urls_ofertas
        v.DATOS = self.tmp
        v.cargar_config = lambda: {"navegador": {"visible": True, "canal": "chrome", "pestanas": 4}}
        v.urls_ofertas = lambda: {"miravia.es": "https://www.miravia.es/ofertas"}

    def tearDown(self):
        v.DATOS, v.cargar_config, v.urls_ofertas = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def nav(self, respuestas):
        usados = []

        class Nav:
            def __init__(s, cfg):
                usados.append(cfg["navegador"])

            def __enter__(s):
                return s

            def __exit__(s, *e):
                pass

            def leer(s, url):
                return respuestas[len(usados) - 1], url, []
        return Nav, usados

    def test_cuatro_modos_con_su_configuracion(self):
        tarjetas = "".join(f'<a href="/p/x-{i}">Zapatillas modelo {i}</a><span>{20 + i},00 €</span>' for i in range(5))
        Nav, usados = self.nav(["<html></html>"] * 2 + ["<html>" + tarjetas + "</html>"] * 2)
        with redirect_stdout(io.StringIO()):
            filas = v.modos(["miravia.es"], nav_cls=Nav)
        self.assertEqual([(u["visible"], u["canal"], u["bloquear_recursos"]) for u in usados],
                         [(False, None, True), (False, "chrome", True), (True, "chrome", True), (True, "chrome", False)])
        self.assertEqual([f[2] for f in filas], [0, 0, 5, 5])
        self.assertIn("visible+imágenes", (self.tmp / "modos.txt").read_text(encoding="utf-8"))

    def test_un_reto_para_los_modos_que_quedan(self):
        Nav, usados = self.nav(["<html>captcha-delivery datadome</html>"] * 4)
        with redirect_stdout(io.StringIO()):
            filas = v.modos(["https://www.aliexpress.com/ofertas"], nav_cls=Nav)
        self.assertEqual(len(usados), 1, "con un reto no se prueba ningún modo más")
        self.assertIn("reto", filas[0][4])
class Cupones(unittest.TestCase):
    """Cupones de AliExpress y Miravia desde los hilos de Chollometro o a mano (17/11/2026)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = v.DATOS, v.DB
        v.DATOS, v.DB = self.tmp, self.tmp / "vistos.db"
        self.con = v.bd()

    def tearDown(self):
        self.con.close()
        v.DATOS, v.DB = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_formatos_de_los_hilos(self):
        texto = ("Cupón 2€: Pedido Mínimo 29€<br>-12€ en pedidos de 109€ (válido hasta las 00:00h)<br>"
                 "<li>10€ de descuento en compras de 60€ (¡Nuevo!).</li><p>$7 / $50 código MES06</p>"
                 "15% en compras de 100€\nEnvío gratis desde 9.90€ si compras desde la APP")
        self.assertEqual([(c["descuento"], c["tipo"], c["minimo"], c["codigo"]) for c in v.cupones_de_texto(texto)],
                         [(2, "€", 29, ""), (12, "€", 109, ""), (10, "€", 60, ""), (7, "€", 50, "MES06"),
                          (15, "%", 100, "")])

    def test_mejor_cupon_minimo_sello_y_caducidad(self):
        v.guardar_cupones(self.con, "miravia.es", [{"descuento": 3, "tipo": "€", "minimo": 29},
                                                    {"descuento": 7, "tipo": "€", "minimo": 59}], hasta="2026-11-18")
        v.guardar_cupones(self.con, "miravia.es", [{"descuento": 20, "tipo": "%", "minimo": 50}], sello="MegaPROMO")
        hoy = datetime(2026, 11, 17)
        self.assertEqual(v.mejor_cupon(self.con, "miravia.es", 25, ahora=hoy), (25, None))  # no llega al mínimo
        final, c = v.mejor_cupon(self.con, "miravia.es", 60, ahora=hoy)
        self.assertEqual((final, c["descuento"]), (53, 7))  # el de 20 % es de sello y no está marcado
        self.assertEqual(v.mejor_cupon(self.con, "miravia.es", 60, elegible=True, ahora=hoy)[0], 48)
        self.assertEqual(v.mejor_cupon(self.con, "miravia.es", 60, ahora=datetime(2026, 11, 19))[0], 60)
        self.assertEqual(v.texto_cupon(c), "cupón de 7 € desde 59 €")

    def test_hilos_de_cupones_en_la_copia(self):
        ahora = int(datetime.now().timestamp())
        v.guardar_hilos(self.con, [
            {"id": 1, "titulo": "Nuevos Cupones Miravia (Desde el 1 de Noviembre)", "precio": None, "tienda": "Miravia",
             "host": "miravia.es", "temp": 50, "publicado": ahora, "caducado": 0},
            {"id": 2, "titulo": "Recopilación Cupones 11.11 AliExpress", "precio": None, "tienda": "AliExpress",
             "host": "aliexpress.com", "temp": 300, "publicado": ahora, "caducado": 0},
            {"id": 3, "titulo": "Zapatillas adidas", "precio": 30, "tienda": "Miravia", "host": "miravia.es",
             "temp": 10, "publicado": ahora, "caducado": 0},
            {"id": 4, "titulo": "Cupón 10 % en Zalando", "precio": None, "tienda": "Zalando", "host": "zalando.es",
             "temp": 10, "publicado": ahora, "caducado": 0},
            {"id": 5, "titulo": "Volante Logitech G29 con tres pedales; cupón 12D1109", "precio": 159, "tienda": "Miravia",
             "host": "miravia.es", "temp": 300, "publicado": ahora, "caducado": 0}])
        self.assertEqual(sorted((i, t) for i, t, _ in v.hilos_de_cupones(self.con)),
                         [(1, "miravia.es"), (2, "aliexpress.com")], "una oferta que menciona un cupón no es un hilo de cupones")

    @staticmethod
    def voucher(codigo, caption, terms, fin="2026-11-19T08:59:00.000Z", titulo="Cupón"):
        return ('{\\"voucher\\":{\\"idPool\\":\\"x\\",\\"title\\":\\"%s\\",\\"caption1\\":\\"%s\\",\\"endTime\\":\\"%s\\",'
                '\\"encryptedAffiliateUrl\\":\\"abc\\",\\"code\\":%s,\\"description\\":null,'
                '\\"termsAndConditions\\":\\"%s\\"}}' % (titulo, caption, fin,
                                                         '\\"%s\\"' % codigo if codigo else "null", terms))

    def test_pagina_de_cupones_de_chollometro(self):
        # /cupones/<tienda> (18/11/2026): el código va en los datos aunque la web lo esconda tras "Ver cupón".
        html = "".join([
            self.voucher("AEES12", "12€", "AliExpress ofrece una promoción para pedidos a España, con un gasto mínimo de 69€."),
            self.voucher("ESD1112", "12€", "Cupón aplicable en compras mínimas de 69€, para pedidos enviados a España."),
            self.voucher("04D1102", "4€", "Código aplicable a compras de al menos 35€ en artículos del «Festival de descuentos 11.11»."),
            self.voucher("4D1102", "4€", "Para compras de al menos 49€ en artículos identificados con la etiqueta «MegaPROMO»."),
            self.voucher(None, "70%", "Ofertas del día con hasta un 70%."),                       # sin código: es una oferta
            self.voucher("BIENV15", "15€", "Descuento de bienvenida para nuevos clientes, mínimo de compra de 30€."),
            self.voucher("SINMIN", "5€", "Sujeto a disponibilidad.")])                            # sin mínimo conocido
        cs = v.cupones_de_pagina(html)
        self.assertEqual([(c["codigo"], c["descuento"], c["tipo"], c["minimo"], c["sello"], c["hasta"]) for c in cs], [
            ("AEES12", 12, "€", 69, "", "2026-11-19"), ("ESD1112", 12, "€", 69, "", "2026-11-19"),
            ("04D1102", 4, "€", 35, "Festival de descuentos 11.11", "2026-11-19"),
            ("4D1102", 4, "€", 49, "MegaPROMO", "2026-11-19")])

    def test_aplicar_cupon_y_posible(self):
        v.guardar_cupones(self.con, "aliexpress.com", [{"descuento": 12, "tipo": "€", "minimo": 69, "codigo": "AEES12"},
                                                       {"descuento": 3, "tipo": "€", "minimo": 18, "codigo": "ESD1103"}])
        v.guardar_cupones(self.con, "miravia.es", [{"descuento": 7, "tipo": "€", "minimo": 49, "codigo": "07D1102b"}],
                          sello="Festival de descuentos 11.11")
        p = {"precio": 80.0}
        self.assertTrue(v.aplicar_cupon(self.con, "aliexpress.com", p))
        self.assertEqual((p["precio"], p["precio_sin_cupon"]), (68.0, 80.0))
        self.assertEqual(p["cupon"], "cupón de 12 € desde 69 € con el código AEES12")
        p = {"precio": 50.0}
        self.assertTrue(v.aplicar_cupon(self.con, "aliexpress.com", p))
        self.assertEqual(p["precio"], 47.0, "el de 12 € no llega al mínimo: el de 3 €")
        # Miravia: los de sello no bajan el precio si la tarjeta no dice que el producto entra; se avisa como posible.
        p = {"precio": 60.0}
        self.assertFalse(v.aplicar_cupon(self.con, "miravia.es", p))
        self.assertEqual(p["precio"], 60.0)
        self.assertIn("07D1102b", p["cupon_posible"])
        p = {"precio": 60.0, "cupon_elegible": True}
        self.assertTrue(v.aplicar_cupon(self.con, "miravia.es", p))
        self.assertEqual(p["precio"], 53.0)
        # Un cupón nuevo cambia la firma de la tienda (la ronda revisa sus productos aunque no cambie el precio).
        antes = v.firma_cupones(self.con, "aliexpress.com")
        v.guardar_cupones(self.con, "aliexpress.com", [{"descuento": 23, "tipo": "€", "minimo": 135, "codigo": "ESD1123"}])
        self.assertNotEqual(antes, v.firma_cupones(self.con, "aliexpress.com"))

    def test_actualizar_cupones_sustituye_los_de_la_pagina(self):
        paginas = {"miravia": self.voucher("04D1102", "4€", "Compras de al menos 35€ en artículos del «Festival».")}
        paginas["aliexpress"] = self.voucher("AEES12", "12€", "Gasto mínimo de 69€.")
        orig_d, orig_s = v.descargar, v.time.sleep
        v.descargar = lambda url, **k: (paginas["miravia" if "miravia" in url else "aliexpress"], url)
        v.time.sleep = lambda s: None
        try:
            v.guardar_cupones(self.con, "aliexpress.com", [{"descuento": 5, "tipo": "€", "minimo": 40, "codigo": "AMANO5"}])
            self.assertEqual(v.actualizar_cupones(self.con), {"miravia.es": 1, "aliexpress.com": 1})
            self.assertEqual(v.actualizar_cupones(self.con), {}, "como mucho cada 6 h")
            paginas["aliexpress"] = self.voucher("ESD1123", "23€", "Gasto mínimo de 135€.")
            v.actualizar_cupones(self.con, forzar=True)
            codigos = sorted(c["codigo"] for c in v.cupones_activos(self.con, "aliexpress.com"))
            self.assertEqual(codigos, ["AMANO5", "ESD1123"], "el retirado sale; el puesto a mano se queda")
        finally:
            v.descargar, v.time.sleep = orig_d, orig_s

    def test_comando_cupon_a_mano(self):
        with redirect_stdout(io.StringIO()) as buf:
            v.comando_cupon(["aliexpress", "10%/60", "es11", "sello", "Día", "de", "marca"])
        c = v.cupones_activos(v.bd(), "aliexpress.com")[0]
        self.assertEqual((c["descuento"], c["tipo"], c["minimo"], c["codigo"], c["sello"]), (10, "%", 60, "ES11", "Día de marca"))
        self.assertIn("cupón de 10 % desde 60 € con el código ES11", buf.getvalue())


class PrecargaHistorial(unittest.TestCase):
    """Historial largo de Destacados (17/11/2026): parar en una fecha o cuando la página vuelve a hoy,
    y guardar los calientes 800 días."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = v.DATOS, v.DB, v.descargar, v.time.sleep, v.enviar_telegram
        v.DATOS, v.DB = self.tmp, self.tmp / "vistos.db"
        v.time.sleep = lambda s: None
        self.telegram = []  # el token sale del .env de Hermes: sin esto, las pruebas mandarían mensajes de verdad
        v.enviar_telegram = lambda token, chat, texto, *a, **k: self.telegram.append(texto)

    def tearDown(self):
        v.DATOS, v.DB, v.descargar, v.time.sleep, v.enviar_telegram = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sesion_solo_en_precarga(self):
        """18/11/2026: con sesión se pasa de la página 334. Solo las cookies de sesión (no Cloudflare) y solo
        mientras corre la precarga."""
        (self.tmp / v.SESION_CM).write_text(json.dumps([{"name": "pepper_session", "value": "a"},
                                                         {"name": "remember_x1", "value": "b"},
                                                         {"name": "cf_clearance", "value": "c"}]), encoding="utf-8")
        vistas = []
        v.descargar = lambda url, timeout=20, reintentos=1: (vistas.append(v.cookie_cm()),
                                                             cm_html([hilo("h", "Amazon", 10, dias=1)]))[1:] + (url,)
        self.assertIsNone(v.cookie_cm())
        v.precargar_chollometro(dias_nuevos=0, paginas_portada=1, pausa=0)
        self.assertEqual(vistas, ["pepper_session=a; remember_x1=b"])
        self.assertIsNone(v.cookie_cm())

    def red(self, ultima):
        pedidas = []

        def red(url, timeout=20, reintentos=1):
            pedidas.append(url)
            if "nuevos" in url:
                return cm_html([hilo("nuevo hoy", "Amazon", 5, dias=0), hilo("viejo", "Amazon", 5, dias=5)]), url
            n = int(url.split("page=")[1]) if "page=" in url else 1
            dias = n if n <= ultima else 0  # pasado el final, Chollometro devuelve lo de hoy
            return cm_html([hilo(f"hilo {n}-{i}", "Amazon", 10 + i, temp=250, dias=dias) for i in range(3)]), url
        v.descargar = red
        return pedidas

    def portada(self, pedidas):
        return [u for u in pedidas if "nuevos" not in u]

    def test_para_cuando_vuelve_a_hoy(self):
        pedidas = self.red(ultima=5)
        with redirect_stdout(io.StringIO()) as buf:
            v.precargar_chollometro(1, 20)
        self.assertEqual(len(self.portada(pedidas)), 6)
        self.assertIn("fin del historial", buf.getvalue())

    def test_un_hilo_fijado_antiguo_en_la_pagina_1_no_corta(self):
        # 18/11/2026: la página 1 de la portada llevaba un hilo fijado del 04/11 entre los del 17/11, y la página 2
        # (del 16/11) parecía "volver a hoy": la carga paró en la página 2.
        pedidas = []

        def red(url, timeout=20, reintentos=1):
            pedidas.append(url)
            if "nuevos" in url:
                return cm_html([hilo("viejo", "Amazon", 5, dias=5)]), url
            n = int(url.split("page=")[1]) if "page=" in url else 1
            dias = n - 1 if n <= 6 else 0
            hilos = [hilo(f"p{n}-{i}", "Amazon", 10 + i, temp=250, dias=dias) for i in range(5)]
            if n == 1:
                hilos.append(hilo("fijado", "Amazon", 99, temp=900, dias=14))
            return cm_html(hilos), url
        v.descargar = red
        with redirect_stdout(io.StringIO()) as buf:
            v.precargar_chollometro(1, 20)
        self.assertEqual(len(self.portada(pedidas)), 7, buf.getvalue())
        self.assertIn("fin del historial", buf.getvalue())

    def test_subiendo_para_cuando_repite_la_pagina_1(self):
        pedidas = []

        def red(url, timeout=20, reintentos=1):
            pedidas.append(url)
            if "nuevos" in url:
                return cm_html([hilo("viejo", "Amazon", 5, dias=5)]), url
            n = int(url.split("page=")[1]) if "page=" in url else 1
            n = 1 if n > 4 else n  # pasado el final, Chollometro devuelve la página 1
            # Subiendo no va ordenado por fecha: la página 3 trae hilos más nuevos que la 2 y no debe parar
            return cm_html([hilo(f"sube {n}-{i}", "Amazon", 10 + i, temp=150, dias=[0, 9, 2, 20][n - 1]) for i in range(3)]), url
        v.descargar = red
        with redirect_stdout(io.StringIO()) as buf:
            v.precargar_chollometro(1, 50, listado="subiendo")
        portada = self.portada(pedidas)
        self.assertTrue(all("/subiendo" in u for u in portada))
        self.assertEqual(len(portada), 5)
        self.assertIn("fin del historial", buf.getvalue())

    def test_el_log_va_a_la_carpeta_de_datos_en_uso(self):
        real = v.LOG
        antes = real.stat().st_size if real.exists() else 0
        v.log("prueba: no debe ir al log de verdad")
        self.assertIn("no debe ir", (self.tmp / "log.txt").read_text(encoding="utf-8"))
        self.assertEqual(real.stat().st_size if real.exists() else 0, antes)

    def test_avisa_por_telegram_cada_50_paginas(self):
        pedidas = self.red(ultima=120)
        orig = v.token_telegram
        v.token_telegram = lambda: "x"
        try:
            with redirect_stdout(io.StringIO()):
                v.precargar_chollometro(0, 200)
        finally:
            v.token_telegram = orig
        progreso = [t for t in self.telegram if "página 50" in t or "página 100" in t]
        self.assertEqual(len(progreso), 2, self.telegram)
        self.assertTrue(any("fin del historial" in t for t in self.telegram))
        self.assertTrue(any("terminado" in t for t in self.telegram))

    def test_sin_nuevos_con_cero_dias(self):
        pedidas = self.red(ultima=100)
        with redirect_stdout(io.StringIO()):
            v.precargar_chollometro(0, 3, listado="subiendo")
        self.assertFalse(any("nuevos" in u for u in pedidas), pedidas)
        self.assertEqual(len(pedidas), 3)

    def test_empieza_en_la_pagina_que_se_le_diga(self):
        pedidas = self.red(ultima=100)
        with redirect_stdout(io.StringIO()):
            v.precargar_chollometro(1, 3, listado="subiendo", inicio=40)
        self.assertEqual([u.split("page=")[1] for u in self.portada(pedidas)], ["40", "41", "42"])

    def test_para_a_la_hora_y_dice_por_donde_seguir(self):
        pedidas = self.red(ultima=100)
        reloj = iter([datetime(2026, 11, 18, 8, 0)] * 2 + [datetime(2026, 11, 18, 8, 10)] * 3 + [datetime(2026, 11, 18, 8, 31)] * 50)

        class Reloj(datetime):
            @classmethod
            def now(cls, tz=None):
                return next(reloj)
        orig, v.datetime = v.datetime, Reloj
        try:
            with redirect_stdout(io.StringIO()) as buf:
                v.precargar_chollometro(1, 100, listado="subiendo", inicio=10, hasta_hora="08:30")
        finally:
            v.datetime = orig
        self.assertIn("página de inicio", buf.getvalue())
        self.assertLess(len(self.portada(pedidas)), 100)

    def test_sin_rondas_mientras_carga(self):
        dentro = []

        def red(url, timeout=20, reintentos=1):
            dentro.append(v.carga_en_marcha())
            return cm_html([hilo(f"h {url}", dias=1)]), url
        v.descargar = red
        with redirect_stdout(io.StringIO()):
            v.precargar_chollometro(1, 2)
        self.assertTrue(dentro and all(dentro), "mientras carga, el cerrojo está puesto")
        self.assertFalse(v.carga_en_marcha(), "al terminar se quita")
        (self.tmp / "precarga.lock").write_text("1")
        orig = v.ronda
        v.ronda = lambda *a, **k: self.fail("no debe haber ronda durante la carga")
        try:
            with redirect_stdout(io.StringIO()) as buf:
                v.ronda_grupo("rapidas")
        finally:
            v.ronda = orig
        self.assertEqual(buf.getvalue().strip(), v.SILENCIO)
        viejo = time.time() - 1000
        os.utime(self.tmp / "precarga.lock", (viejo, viejo))
        self.assertFalse(v.carga_en_marcha(), "un cerrojo de una carga muerta no cuenta")

    def test_guarda_grupo_enlace_y_cupon_para_analizar(self):
        h = {**hilo("Café en grano Lavazza 1 kg", "Amazon", 12.9, temp=420),
             "mainGroup": {"threadGroupName": "Supermercado", "threadGroupUrlName": "supermercado"},
             "link": "https://www.amazon.es/dp/B000SDKDM4", "voucherCode": "", "commentCount": 37,
             "description": "texto largo que no hace falta " * 50}
        con = v.bd()
        v.guardar_hilos(con, v.hilos_chollometro(cm_html([h])))
        extra = json.loads(con.execute("SELECT extra FROM cm").fetchone()[0])
        self.assertEqual(extra["mainGroup"]["threadGroupName"], "Supermercado")
        self.assertEqual(extra["link"], "https://www.amazon.es/dp/B000SDKDM4")
        self.assertEqual(extra["commentCount"], 37)
        self.assertNotIn("description", extra)
        self.assertNotIn("voucherCode", extra, "los vacíos no se guardan")
        v.guardar_hilos(con, [{**v.hilos_chollometro(cm_html([h]))[0], "extra": None, "temp": 500}])
        self.assertIsNotNone(con.execute("SELECT extra FROM cm").fetchone()[0], "una página sin datos no borra los guardados")

    def test_si_falla_guarda_por_donde_seguir_y_seguir_retoma(self):
        avisos = []
        orig = v.aviso_precarga
        v.aviso_precarga = lambda texto, seguir=False: avisos.append((texto, seguir))
        caida = {"desde": 4}

        def red(url, timeout=20, reintentos=1):
            n = int(url.split("page=")[1]) if "page=" in url else 1
            if n >= caida["desde"]:
                raise OSError("HTTP Error 429")
            return cm_html([hilo(f"s {n}-{i}", "Amazon", 10 + i, temp=250, dias=n) for i in range(3)]), url
        v.descargar = red
        try:
            with redirect_stdout(io.StringIO()):
                v.precargar_chollometro(0, 100, pausa=5, desde="2025-01-01", listado="subiendo")
            self.assertTrue(any(s for _, s in avisos), "el aviso de la parada lleva el botón Seguir")
            datos = json.loads((self.tmp / v.SEGUIR).read_text(encoding="utf-8"))
            self.assertEqual((datos["listado"], datos["inicio"], datos["paginas_portada"], datos["pausa"]),
                             ("subiendo", 4, 97, 5))
            caida["desde"] = 7
            pedidas = []
            red_orig = v.descargar
            v.descargar = lambda url, **k: (pedidas.append(url), red_orig(url))[1]
            with redirect_stdout(io.StringIO()):
                self.assertTrue(v.seguir_precarga())
            self.assertTrue(pedidas[0].endswith("subiendo?page=4"), "sigue donde se paró")
            self.assertEqual(json.loads((self.tmp / v.SEGUIR).read_text(encoding="utf-8"))["inicio"], 7)
            (self.tmp / "precarga.lock").write_text("1")
            with redirect_stdout(io.StringIO()):
                self.assertFalse(v.seguir_precarga(), "con una carga en marcha no lanza otra")
        finally:
            v.aviso_precarga = orig

    def test_410_pasada_la_primera_pagina_es_el_final_del_listado(self):
        # 18/11/2026: Destacados acaba en la 334; la 335 da "410 Gone". No es un bloqueo: sin botón Seguir.
        from urllib.error import HTTPError
        avisos = []
        orig = v.aviso_precarga
        v.aviso_precarga = lambda texto, seguir=False: avisos.append((texto, seguir))

        def red(url, timeout=20, reintentos=1):
            n = int(url.split("page=")[1]) if "page=" in url else 1
            if n > 3:
                raise HTTPError(url, 410, "Gone", {}, None)
            return cm_html([hilo(f"d {n}-{i}", "Amazon", 10 + i, temp=250, dias=n) for i in range(3)]), url
        v.descargar = red
        try:
            with redirect_stdout(io.StringIO()):
                v.precargar_chollometro(0, 100, desde="2025-01-01")
        finally:
            v.aviso_precarga = orig
        self.assertFalse(any(s for _, s in avisos), avisos)
        self.assertTrue(any("fin del listado" in t for t, _ in avisos))
        self.assertFalse((self.tmp / v.SEGUIR).exists())

    def test_si_acaba_bien_no_queda_nada_que_seguir(self):
        self.red(ultima=3)
        (self.tmp / v.SEGUIR).write_text("{}")
        with redirect_stdout(io.StringIO()):
            v.precargar_chollometro(0, 20)
        self.assertFalse((self.tmp / v.SEGUIR).exists())

    def test_para_en_la_fecha_desde(self):
        pedidas = self.red(ultima=100)
        desde = (datetime.now() - timedelta(days=3, hours=12)).date().isoformat()
        with redirect_stdout(io.StringIO()):
            v.precargar_chollometro(1, 50, desde=desde)
        self.assertLessEqual(len(self.portada(pedidas)), 5)

    def test_los_calientes_se_guardan_mas_tiempo(self):
        con = v.bd()
        v.guardar_hilos(con, v.hilos_chollometro(cm_html([hilo("caliente", temp=250, dias=500),
                                                          hilo("frio", temp=50, dias=500),
                                                          hilo("muy viejo", temp=900, dias=900)])))
        v.descargar = lambda url, **k: (cm_html([hilo("hoy", dias=0)]), url)
        v.actualizar_chollometro(con, forzar=True, max_paginas=1)
        self.assertEqual([t for (t,) in con.execute("SELECT titulo FROM cm ORDER BY titulo")], ["caliente", "hoy"])


class ComparaPropio(unittest.TestCase):
    """Habitual propio y mercado propio, con el historial que ya guarda el vigilante (17/11/2026)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = v.DATOS, v.DB
        v.DATOS, v.DB = self.tmp, self.tmp / "vistos.db"
        self.con = v.bd()
        self.ahora = datetime(2026, 12, 1, 12, 0)

    def tearDown(self):
        self.con.close()
        v.DATOS, v.DB = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def precios(self, clave, *tramos):
        for dias, precio in tramos:
            f = (self.ahora - timedelta(days=dias)).isoformat(timespec="minutes")
            self.con.execute("INSERT INTO precios VALUES (?,?,?)", (clave, f, precio))

    def test_enlaces_canonicos(self):
        # Amazon mete el puesto en la lista y la sesión en el enlace: sin esto, cada lectura era un producto nuevo.
        for url in ("https://www.amazon.es/Xiaomi-Redmi-Buds-Pro/dp/B0CXYZ1234/ref=zg_bs_g_electronics_d_sccl_3/261-1234567?psc=1",
                    "https://www.amazon.es/dp/b0cxyz1234?th=1",
                    "https://www.amazon.es/gp/product/B0CXYZ1234/ref=ox_sc_act_title_1"):
            self.assertEqual(v.url_canonica(url), "https://www.amazon.es/dp/B0CXYZ1234", url)
        self.assertEqual(v.asin_de("https://www.amazon.es/x/dp/B0CXYZ1234/ref=a"), "B0CXYZ1234")
        self.assertEqual(v.url_canonica("https://es.aliexpress.com/item/1005006123456789.html?spm=a2g0o&algo_pvid=x"),
                         "https://es.aliexpress.com/item/1005006123456789.html")
        otra = "https://www.mediamarkt.es/es/product/_xiaomi-123.html?utm_source=x"
        self.assertEqual(v.url_canonica(otra), otra, "las demás tiendas no cambian: su historial sigue igual")
        self.assertIsNone(v.url_canonica(None))
        self.assertEqual(v.url_canonica("https://www.amazon.es/gp/bestsellers/electronics"),
                         "https://www.amazon.es/gp/bestsellers/electronics")

    def test_habitual_propio(self):
        h = lambda c: v.habitual_propio(self.con, c, self.ahora.isoformat(timespec="minutes"))
        # 49,99 € durante 20 días, un pico de un día a 69,99 €, y ahora 34,99 € desde hace 2 días.
        self.precios("a", (25, 49.99), (10, 69.99), (9, 49.99), (2, 34.99))
        r = h("a")
        self.assertEqual((r["habitual"], r["minimo"], r["dias"]), (49.99, 49.99, 23))
        # Solo 4 días de historial antes de la bajada: no se sabe cuál es el habitual.
        self.precios("b", (6, 49.99), (2, 34.99))
        self.assertIsNone(h("b"))
        # El precio actual ya dura 20 días: es el habitual, no una bajada.
        self.precios("c", (60, 49.99), (20, 34.99))
        self.assertIsNone(h("c"))
        self.assertIsNone(h("nada"))
        # La ventana es de 60 días: lo de hace 200 días no cuenta.
        self.precios("d", (200, 999.0), (40, 59.99), (1, 44.99))
        self.assertEqual(h("d")["habitual"], 59.99)

    def test_mercado_propio(self):
        reciente = (datetime.now() - timedelta(hours=3)).isoformat(timespec="minutes")
        viejo = (datetime.now() - timedelta(days=5)).isoformat(timespec="minutes")
        for clave, tienda, ean, precio, visto in (
                ("pcc|1", "pccomponentes.com", "06925281982170", 139.0, reciente),   # con un 0 delante
                ("amz|1", "amazon.es", "6925281982170", 149.0, reciente),
                ("mm2|1", "mediamarkt.es", "6925281982170", 119.0, reciente),       # la misma tienda: no cuenta
                ("fnac|1", "fnac.es", "6925281982170", 99.0, viejo),                  # visto hace 5 días: no cuenta
                ("raro|1", "otra.es", "6925281982170", 29.0, reciente),               # menos de la mitad: no cuenta
                ("hip|1", "hipercor.es", "1234567890123", 50.0, reciente)):
            self.con.execute("INSERT INTO productos VALUES (?,?,?,?,?)", (clave, tienda, "JBL", ean, f"https://{tienda}/x"))
            self.con.execute("INSERT INTO vistos (clave, precio, minimo, visto, desde) VALUES (?,?,?,?,?)",
                             (clave, precio, precio, visto, visto))
        r = v.mercado_propio(self.con, {"ean": "6925281982170", "precio": 99.0}, "mediamarkt.es")
        self.assertEqual((r["precio"], r["tienda"]), (139.0, "pccomponentes.com"))
        self.assertIsNone(v.mercado_propio(self.con, {"ean": None, "precio": 99.0}, "mediamarkt.es"))
        self.assertIsNone(v.mercado_propio(self.con, {"ean": "1234567890123", "precio": 45.0}, "elcorteingles.es"),
                          "El Corte Inglés e Hipercor cuentan como una tienda")
        self.assertEqual(v.mercado_propio(self.con, {"ean": "1234567890123", "precio": 45.0}, "amazon.es")["precio"], 50.0)

    def test_etiquetas_del_tachado_de_amazon(self):
        # /deals (17/11/2026): cada producto empieza por {"asin","title"}; las variantes de color no llevan title.
        bloque = lambda asin, nombre, precio, etiqueta, antes: (
            f'{{"asin":"{asin}","title":"{nombre}","link":"/x/dp/{asin}","variations":[{{"asin":"B0VARIANTE1",'
            f'"link":"/dp/B0VARIANTE1"}}],"price":{{"priceToPay":{{"label":"Precio de la oferta:","price":"{precio}",'
            f'"strikethrough":false}},"basisPrice":{{"label":"{etiqueta}","price":"{antes}","strikethrough":true}}}}}}')
        html = "[" + ",".join([bloque("B07M98CL6Y", "Dodot Pañales Talla 4", "60.99", "Mediano:", "91.99"),
                               bloque("B0HHMXSFFC", "Xiaomi 18 Pro", "1499.0", "Más bajo:", "1799.9"),
                               bloque("B09B8X9RGM", "Echo Dot", "39.99", "Recomendado:", "89.99")]) + "]"
        self.assertEqual(v.etiquetas_amazon(html), {"B07M98CL6Y": ("mediano", 91.99), "B0HHMXSFFC": ("mas_bajo", 1799.9),
                                                    "B09B8X9RGM": ("recomendado", 89.99)})
        p = lambda rt, precio, antes: {"ref_tienda": rt, "precio": precio, "precio_antes": antes}
        dodot = p("mediano", 60.99, 91.99)
        self.assertTrue(v.contraste_ref_tienda(dodot))
        self.assertEqual(dodot["_hist"]["habitual"], 91.99)
        self.assertIn("90 días", dodot["_hist"]["fuente_corta"])
        self.assertFalse(v.contraste_ref_tienda(p("mas_bajo", 1499.0, 1799.9)), "solo un 17 % por debajo")
        self.assertIsNone(v.contraste_ref_tienda(p("recomendado", 39.99, 89.99)), "el PVPR no es contraste")
        self.assertIsNone(v.contraste_ref_tienda(p("mediano", 60.99, 91.99), {"ref_amazon": False}))
        # En la ficha, el tachado va como su histórico, no como "PVP de la tienda".
        item = {"num": 1, "n": "Dodot Pañales Talla 4", "p": 60.99, "a": 91.99, "rt": "mediano",
                "historico": {"habitual": 91.99, "minimo": None, "fuente": v.REF_TIENDA_HISTORICO["mediano"]}}
        b = v.bloques_de_codigo(item, {"precio": 60.99, "etiqueta": "DESTACADOS"})
        self.assertNotIn("PVP", b["precios"])
        self.assertIn("91,99", b["precios"])

    CAMEL = ('<div class="table-scroll camelegend"><table><tbody>'
             '<tr class="pt amazon on" data-field="amazon"><td class="fixed nowrap amazon"> Amazon </td>'
             '<td> 45,49€ <small>(Jul 09, 2020)</small> </td><td> 99,80€ <small>(Feb 01, 2023)</small> </td>'
             '<td> 91,99€ <small>(Sep 04, 2026)</small> </td><td> 81,06€ </td></tr>'
             '<tr class="pt new " data-field="new"><td> 3rd Party New </td><td> 47,49€ <small>(Nov 28, 2019)</small></td>'
             '<td> 85,79€ </td><td> - </td><td> 66,83€ </td></tr></tbody></table></div>')

    def test_camelcamelcamel(self):
        self.assertEqual(v.camel_de_html(self.CAMEL), {"minimo": 45.49, "minimo_fecha": "2020-07-09", "maximo": 99.8,
                                                       "media": 81.06, "fila": "amazon"})
        solo_terceros = re.sub(r'<tr class="pt amazon.*?</tr>', "", self.CAMEL, flags=re.S)
        self.assertEqual(v.camel_de_html(solo_terceros)["media"], 66.83)
        self.assertIsNone(v.camel_de_html("<html>nada</html>"))
        pedidas = []

        class Falso:
            def __init__(self, cfg=None): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def leer(s, url, completo=False):
                pedidas.append(url)
                if "B0RETO0000" in url:
                    return "<html>Just a moment...<div class='cf-chl'></div></html>", url, []
                return TestCaso.CAMEL, url, []
        TestCaso = type(self)
        orig = v.NavegadorNormal
        v.NavegadorNormal = Falso
        try:
            ps = [{"url": "https://www.amazon.es/dp/B07M98CL6Y"}, {"url": "https://www.amazon.es/x/dp/B09B8X9RGM/ref=a"}]
            r = v.camel_lote(self.con, {}, ps)
            self.assertEqual([x["minimo"] for x in r], [45.49, 45.49])
            self.assertEqual(pedidas, ["https://es.camelcamelcamel.com/product/B07M98CL6Y",
                                       "https://es.camelcamelcamel.com/product/B09B8X9RGM"])
            v.camel_lote(self.con, {}, ps)
            self.assertEqual(len(pedidas), 2, "caché de 24 h por ASIN")
            self.assertEqual(v.camel_guardado(self.con, "https://www.amazon.es/dp/B07M98CL6Y")["media"], 81.06)
            # Reto: no se resuelve, pausa fija de 30 min y no se piden más.
            r = v.camel_lote(self.con, {}, [{"url": "https://www.amazon.es/dp/B0RETO0000"},
                                            {"url": "https://www.amazon.es/dp/B0OTRO0000"}])
            self.assertEqual(r, [None, None])
            self.assertNotIn("https://es.camelcamelcamel.com/product/B0OTRO0000", pedidas)
            hasta = datetime.fromisoformat(self.con.execute(
                "SELECT v FROM meta WHERE k='pausa:camelcamelcamel.com'").fetchone()[0])
            self.assertTrue(28 < (hasta - datetime.now()).total_seconds() / 60 <= 30)
            antes = len(pedidas)
            v.camel_lote(self.con, {}, [{"url": "https://www.amazon.es/dp/B0OTRO0000"}])
            self.assertEqual(len(pedidas), antes, "en pausa no se pide nada")
        finally:
            v.NavegadorNormal = orig

    def test_decidir_con_el_habitual_propio(self):
        # Al verificar, si no hay PCComTracker, el dossier trae el habitual propio y el código decide con él.
        d = v.decidir_item({"num": 1, "n": "Sony WF-C710N", "t": "mediamarkt.es", "u": "u", "p": 64.0, "validada": True,
                            "ficha": {"precio_ahora": 64.0, "titulo": "Sony WF-C710N", "stock": "sí"},
                            "historico": {"habitual": 89.0, "minimo": 84.0, "fuente": "el historial del vigilante"}})
        self.assertEqual((d["veredicto"], d["etiqueta"]), ("pasa", "DESTACADOS"))
        self.assertIn("historial del vigilante", d["prueba"])


class Piezas(unittest.TestCase):
    def test_numeros(self):
        casos = {"1.299,00 €": 1299.0, "19.99": 19.99, "1,299.50": 1299.5, "39,99": 39.99,
                 "1.299": 1299.0, 25: 25.0, "": None, "Negro": None, None: None, True: None}
        for entrada, esperado in casos.items():
            self.assertEqual(v.a_numero(entrada), esperado, entrada)

    def test_extraccion_y_ruido(self):
        html = tienda_html([
            {"n": "Xiaomi Redmi Buds 6 Pro", "m": "Xiaomi", "e": "6941812790000", "p": 39.99, "a": 79.99},
            {"n": "Cosori Freidora 5,5 L", "p": 59.99, "a": 99.99, "ld": False},
        ]) + '<script>window.__STATE__ = {"x":[{"name":"Philips Afeitadora","salePrice":"29,99","oldPrice":"49,99"}]};</script>'
        ps = {p["nombre"]: p for p in v.extraer(html, "https://www.tienda.es/ofertas")}
        self.assertEqual(set(ps), {"Xiaomi Redmi Buds 6 Pro", "Cosori Freidora 5,5 L", "Philips Afeitadora"})
        b = ps["Xiaomi Redmi Buds 6 Pro"]
        self.assertEqual((b["precio"], b["precio_antes"], b["ean"], b["marca"]), (39.99, 79.99, "6941812790000", "Xiaomi"))
        self.assertEqual(b["url"], "https://www.tienda.es/p/Xiaomi-Redmi-Buds-6-Pro")
        self.assertEqual(ps["Cosori Freidora 5,5 L"]["precio_antes"], 99.99)
        self.assertEqual(ps["Philips Afeitadora"]["precio_antes"], 49.99)

    def test_microdatos_html(self):
        html = """<div itemscope itemtype="https://schema.org/Product">
          <h2 itemprop="name">JBL Flip 6</h2><meta itemprop="gtin13" content="6925281993145">
          <img itemprop="image" src="/f6.jpg"><a itemprop="url" href="/jbl-flip-6">ver</a>
          <div itemprop="offers" itemscope itemtype="https://schema.org/Offer">
            <span itemprop="price" content="79.99">79,99 €</span></div></div>
          <div itemscope itemtype="https://schema.org/Product"><span itemprop="name">Sony WH-CH520</span>
          <span itemprop="offers" itemscope><span itemprop="price">39,99 €</span></span></div>"""
        ps = {p["nombre"]: p for p in v.extraer(html, "https://t.es/")}
        self.assertEqual(ps["JBL Flip 6"]["precio"], 79.99)
        self.assertEqual(ps["JBL Flip 6"]["ean"], "6925281993145")
        self.assertEqual(ps["JBL Flip 6"]["url"], "https://t.es/jbl-flip-6")
        self.assertEqual(ps["Sony WH-CH520"]["precio"], 39.99)

    def test_claves_snake_y_anidadas(self):
        html = ('<script type="application/json">' + json.dumps({"items": [
            {"product_name": "Philips Airfryer", "final_price": "59,99", "regular_price": "99,99"},
            {"title": "Braun Series 7", "price": {"current": 89.0, "original": 149.0}},
            {"name": "Xiaomi 17T", "price": 549.99, "market_price": 649.99}]}) + "</script>")
        ps = {p["nombre"]: (p["precio"], p["precio_antes"]) for p in v.extraer(html, "https://t.es/")}
        self.assertEqual(ps["Philips Airfryer"], (59.99, 99.99))
        self.assertEqual(ps["Braun Series 7"], (89.0, 149.0))
        self.assertEqual(ps["Xiaomi 17T"], (549.99, 649.99))

    def test_aggregateoffer_no_es_precio_tachado(self):
        html = ('<script type="application/ld+json">' + json.dumps(
            {"@type": "Product", "name": "ELECTRO CHOLLOS",
             "offers": {"@type": "AggregateOffer", "lowPrice": 29, "highPrice": 1049.95}}) + "</script>")
        self.assertIsNone(v.extraer(html, "https://t.es/")[0]["precio_antes"])

    def test_precio_tachado_en_html(self):
        datos = {"@type": "ItemList", "itemListElement": [
            {"@type": "Product", "name": "Samsung Galaxy S26", "url": "/es/product/_samsung-galaxy-s26-123",
             "offers": {"price": 799}},
            {"@type": "Product", "name": "Xiaomi Redmi Note 15", "url": "/es/product/_xiaomi-note-15-456",
             "offers": {"price": 199}}]}
        html = ('<script type="application/ld+json">' + json.dumps(datos) + "</script>"
                '<div><a href="/es/product/_samsung-galaxy-s26-123">Samsung Galaxy S26</a>'
                '<span class="sc-strike-through">999,00 €</span><span>799,– €</span></div>'
                '<div><a href="/es/product/_xiaomi-note-15-456">Xiaomi Redmi Note 15</a>'
                '<span>199 €</span><del>1.990 €</del></div>')  # 10x: precio absurdo, se ignora
        ps = {p["nombre"]: p["precio_antes"] for p in v.extraer(html, "https://www.mediamarkt.es/")}
        self.assertEqual(ps["Samsung Galaxy S26"], 999.0)
        self.assertIsNone(ps["Xiaomi Redmi Note 15"])

    def test_precio_promocion_manda_sobre_price(self):
        # Samsung: "price" es el precio normal y "promotionPrice" el de oferta.
        html = ('<script type="application/json">' + json.dumps({"items": [
            {"displayName": "Galaxy S26 Ultra", "price": "1549.01", "promotionPrice": "1249",
             "listPrice": "1549.01"},
            {"displayName": "Galaxy Buds4", "price": "199", "promotionPrice": "149"},
            {"displayName": "Galaxy Watch8", "price": "349", "promotionPrice": None}]}) + "</script>")
        ps = {p["nombre"]: (p["precio"], p["precio_antes"]) for p in v.extraer(html, "https://t.es/")}
        self.assertEqual(ps["Galaxy S26 Ultra"], (1249.0, 1549.01))
        self.assertEqual(ps["Galaxy Buds4"], (149.0, 199.0))
        self.assertEqual(ps["Galaxy Watch8"], (349.0, None))

    def test_precio_tachado_data_test_y_tarjeta_larga(self):
        # MediaMarkt: el tachado va en data-test (no en class) y lejos del enlace del producto.
        datos = {"@type": "ItemList", "itemListElement": [
            {"@type": "Product", "name": "Galaxy S26 Ultra", "url": "/es/product/_s26-ultra-1",
             "offers": {"price": 1299}},
            {"@type": "Product", "name": "Galaxy S26", "url": "/es/product/_s26-2",
             "offers": {"price": 899}}]}
        relleno = "<div><span>Disponible online</span></div>" * 150  # ~6000 caracteres
        tarjeta = lambda u, tach, precio: (
            f'<a href="{u}"><h3>x</h3></a>{relleno}'
            + (f'<div data-test="mms-strike-price-type-xop"><span>{tach},&ndash;&nbsp;&euro;</span>'
               f'<span>{tach},00€</span></div>' if tach else "")
            + f'<div><span>{precio},00€</span></div>')
        html = ('<script type="application/ld+json">' + json.dumps(datos) + "</script>"
                + tarjeta("/es/product/_s26-ultra-1", 1749, 1299) + tarjeta("/es/product/_s26-2", None, 899))
        ps = {p["nombre"]: p["precio_antes"] for p in v.extraer(html, "https://www.mediamarkt.es/")}
        self.assertEqual(ps["Galaxy S26 Ultra"], 1749.0)
        self.assertIsNone(ps["Galaxy S26"], "no toma el tachado de la tarjeta anterior")

    def test_atributos_data(self):
        # Coolmod: datos en atributos data-item* del enlace y precio anterior tachado en la tarjeta.
        html = """<div class="card"><a href="/nanocable-cat6/" data-itemname="Nanocable Latiguillo Cat 6 "
             data-itemid="144792" data-itemprice="1.95" data-itempricetaxexc="4.92" data-itembrand="Nanocable">
             <img src="/n.jpg"></a><p><a href="/nanocable-cat6/">Nanocable Latiguillo Cat 6</a></p>
             <span class=" line-through text-sm price-old-product">5,95&nbsp;&euro;</span>
             <div data-price-type="sale"><span>1</span><sup>95 &euro;</sup></div></div>
           <div class="card"><a href="/logitech-g502/" data-itemname="Logitech G502 X" data-itemprice="59.90"
             data-itembrand="Logitech"></a><span>59,90 &euro;</span></div>
           <button data-product='{"name": "Kingston NV3 1TB", "price": "54.90", "regular_price": "79.90"}'>x</button>"""
        ps = {p["nombre"]: p for p in v.extraer(html, "https://www.coolmod.com/outlet/")}
        self.assertEqual(set(ps), {"Nanocable Latiguillo Cat 6", "Logitech G502 X", "Kingston NV3 1TB"})
        n = ps["Nanocable Latiguillo Cat 6"]
        self.assertEqual((n["precio"], n["precio_antes"], n["marca"], n["url"]),
                         (1.95, 5.95, "Nanocable", "https://www.coolmod.com/nanocable-cat6/"))
        self.assertIsNone(ps["Logitech G502 X"]["precio_antes"])
        self.assertEqual(ps["Kingston NV3 1TB"]["precio_antes"], 79.9)

    def test_url_desde_la_tarjeta(self):
        # ElectroDepot: el JSON de la lista no trae URL; está en el <a> de la tarjeta.
        capa = json.dumps([{"item": {"name": 'TV Valberg 55" 4K', "price": "249.96"}},
                           {"item": {"name": "Microondas Valberg  20L", "price": "49.96"}},
                           {"item": {"name": "Plancha sin tarjeta", "price": "19.96"}}])
        html = (f'<div data-datalayerproducts="{capa.replace("&", "&amp;").replace(chr(34), "&quot;")}"></div>'
                '<a href="/tv-valberg-55.html" class="item"><img title="TV Valberg 55&quot; 4K">'
                '<h2>TV Valberg 55&quot; 4K</h2></a>'
                '<a href="https://www.electrodepot.es/microondas-20l.html"><div><h2>Microondas Valberg  20L</h2>'
                '</div></a><a href="/otra.html">otra</a></div><h2>Plancha sin tarjeta</h2>')
        ps = {p["nombre"]: p["url"] for p in v.extraer(html, "https://www.electrodepot.es/electrochollos.html")}
        self.assertEqual(ps['TV Valberg 55" 4K'], "https://www.electrodepot.es/tv-valberg-55.html")
        self.assertEqual(ps["Microondas Valberg 20L"], "https://www.electrodepot.es/microondas-20l.html")
        self.assertIsNone(ps["Plancha sin tarjeta"], "fuera de un enlace: no se inventa la URL")

    def test_tarjetas_visibles_sin_datos(self):
        # Euronics: sin JSON-LD ni microdatos; solo tarjetas con enlace, nombre y precios.
        tarjeta = lambda slug, nombre, antes, ahora, extra="": (
            f'<li><article><a href="/{slug}.html"><img alt="{nombre}" src="/f.jpg"></a>'
            f'<span class="hm26-card__brand">Marca</span><h3><a title="acceder a ficha producto" '
            f'href="/{slug}.html">{nombre}</a></h3>{extra}<div class="bloquePrecio">'
            + (f'<span class="hm26-card__old precioDescuento">{antes} &euro;</span>' if antes else "")
            + f'<span class="hm26-card__now">{ahora} &euro;</span></div>'
            f'<a href="/{slug}.html">Ver producto</a></article></li>')
        html = ('<nav><a href="/promociones.html">Promociones</a><a href="/tv-y-video/">TV</a></nav>'
                '<p>Envío gratis desde 50 €</p>'
                + tarjeta("samsung-rb38c607as9-ef-frigorificos-combi", "Frigorífico combi Samsung RB38C607AS9/EF",
                          "999", "899")
                + tarjeta("lg-oled55c5-televisor-oled-55", "TV LG OLED55C5 55 pulgadas", "1.499", "1.199",
                          '<p>Ahorra 300 € | Desde 49,96 €/mes</p>')
                + tarjeta("bosch-serie-4-lavavajillas-sms4", "Lavavajillas Bosch Serie 4", None, "449"))
        ps = {p["nombre"]: p for p in v.extraer(html, "https://www.euronics.es/promocion-black-friday.html")}
        self.assertEqual(set(ps), {"Frigorífico combi Samsung RB38C607AS9/EF", "TV LG OLED55C5 55 pulgadas",
                                   "Lavavajillas Bosch Serie 4"})
        f = ps["Frigorífico combi Samsung RB38C607AS9/EF"]
        self.assertEqual((f["precio"], f["precio_antes"], f["url"]),
                         (899.0, 999.0, "https://www.euronics.es/samsung-rb38c607as9-ef-frigorificos-combi.html"))
        self.assertEqual((ps["TV LG OLED55C5 55 pulgadas"]["precio"], ps["TV LG OLED55C5 55 pulgadas"]["precio_antes"]),
                         (1199.0, 1499.0), "ni el ahorro ni la cuota mensual son el precio")
        self.assertEqual((ps["Lavavajillas Bosch Serie 4"]["precio"], ps["Lavavajillas Bosch Serie 4"]["precio_antes"]),
                         (449.0, None))

    def test_tarjetas_precio_partido_y_badges(self):
        # Back Market parte el precio en etiquetas; HP pone '50€ extra de dto.' antes del precio.
        html = ('<a href="/es-es/p/playstation-5/7b0d7cf2-735b">PlayStation 5 Muy bueno 825 GB</a>'
                '<span>Precio reacondicionado:</span><span>684</span><span>,23</span><span>€</span>'
                '<span>40 € descontados al finalizar la compra</span>'
                '<a href="/es-es/shop/products/laptops/hp-omnibook-3-14-hu0011nsx">x</a><span>50€ extra de dto.</span>'
                '<h3><a href="/es-es/shop/products/laptops/hp-omnibook-3-14-hu0011nsx">Portátil HP OmniBook 3 14</a></h3>'
                '<span>Desde 25,00 € al mes</span><span class="price">549,00 €</span>'
                '<a href="/es-es/p/iphone-15-128gb/aa11bb22-ccdd">iPhone 15 128 GB Negro</a><span>499 €</span>')
        ps = {p["nombre"]: p["precio"] for p in v.extraer(html, "https://www.backmarket.es/es-es/e/good-deals")}
        self.assertEqual(ps["PlayStation 5 Muy bueno 825 GB"], 684.23)
        self.assertEqual(ps["Portátil HP OmniBook 3 14"], 549.0)

    def test_tarjetas_euro_delante(self):
        # Alternate ('€ 265,00') y Expert ('€58 <small>,90</small>') ponen el euro delante: antes se cogía
        # el número anterior al símbolo ('32:9' -> 9 €, 'KP2431' -> 2.431 €). 17/11/2026.
        alternate = lambda id_, nombre, specs, precio, antes="": (
            f'<div class="card"><a class="track_this_link" href="https://www.alternate.es/X/Y/html/product/{id_}">'
            f'<img alt="{nombre}" src="/p/{id_}.jpg"><div class="product-name"><strong>{nombre}</strong></div>'
            f'<ul><li>{specs}</li></ul><div class="price">'
            + (f'<span class="line-through">€ {antes}</span>' if antes else "")
            + f'<span>€ {precio}</span></div></a></div>')
        html = (alternate("100203689", "Corsair XENEON EDGE, Pantalla negro", "Formato: 32:9", "265,00", "299,00")
                + alternate("100011432", "Bosch Taladro percutor UniversalImpact 750", "Potencia: 750 W, 1", "59,90")
                + alternate("1702855", "Sharkoon ELITE SHARK CA700 Torre Negro", "Ventiladores: 7", "1.149,00"))
        ps = {p["nombre"]: (p["precio"], p["precio_antes"])
              for p in v.extraer(html, "https://www.alternate.es/")}
        self.assertEqual(ps["Corsair XENEON EDGE, Pantalla negro"], (265.0, 299.0))
        self.assertEqual(ps["Bosch Taladro percutor UniversalImpact 750"], (59.9, None))
        self.assertEqual(ps["Sharkoon ELITE SHARK CA700 Torre Negro"], (1149.0, None))
        expert = lambda slug, nombre, euros, cent: (
            f'<div class="uk-card"><a href="https://www.expert.es/producto/{slug}" class="uk-position-cover"></a>'
            f'<h3><a href="https://www.expert.es/producto/{slug}">{nombre}</a></h3>'
            f'<span class="gtag-precio"><strong>€{euros} <small>,{cent}</small></strong></span>'
            f'<a href="#" title="Añadir al carrito">Añadir al carrito</a></div>')
        html = (expert("cafetera-krups-kp2431as", "Cafetera de cápsulas Krups Genio S blanca KP2431", "58", "90")
                + expert("cafetera-exp-delongh-ecam290-22-b", "Cafetera DeLonghi Magnífica EVO ECAM290.22", "431", "00")
                + expert("cepillo-rowenta-ub5920-seca-y-alisa", "Cepillo Secador Rowenta UB5920F0", "51", "90"))
        ps = {p["nombre"]: p["precio"] for p in v.extraer(html, "https://www.expert.es/")}
        self.assertEqual(ps, {"Cafetera de cápsulas Krups Genio S blanca KP2431": 58.9,
                              "Cafetera DeLonghi Magnífica EVO ECAM290.22": 431.0,
                              "Cepillo Secador Rowenta UB5920F0": 51.9})

    def test_euro_delante_no_cambia_tiendas_de_siempre(self):
        self.assertFalse(v.euro_delante('<span>899,00 €</span><span>1.199,00 €</span><p>Envío gratis desde 50 €</p>'))
        self.assertTrue(v.euro_delante('<span>€ 265,00</span><span>€58 <small>,90</small></span>'))

    def test_tarjetas_no_pisan_datos_estructurados(self):
        # Con 3 o más productos en JSON-LD no se usan las tarjetas (las tiendas que ya funcionan no cambian).
        html = tienda_html([{"n": f"JBL Charge {i}", "p": 99.0} for i in range(3)]) + (
            '<a href="/otro-producto-raro-123456.html">Producto que solo sale en la tarjeta</a><span>5 &euro;</span>')
        self.assertEqual(len(v.extraer(html, "https://t.es/")), 3)

    def test_json_en_atributo_cualquiera(self):
        # Dell: los productos van en JSON dentro de un atributo del componente, con dellPrice y pdUrl.
        datos = {"pv15250_so_12": {"productId": "pv15250_so_12", "title": "Portátil Dell Pro 15 Essential",
                                   "marketPrice": "1.749,66 €", "dellPrice": "1.599,62 €",
                                   "totalSavings": "150,04 €", "dellPriceWithoutTax": "1.322,01 €",
                                   "pdUrl": "https://www.dell.com/es-es/shop/laptop-computers/spd/pv15250_so_12"}}
        html = f'<div data-x="1" product-stack="{escape_attr(json.dumps(datos, ensure_ascii=False))}"></div>'
        p = v.extraer(html, "https://www.dell.com/es-es/shop/deals/dc/laptop-deals")[0]
        self.assertEqual((p["nombre"], p["precio"], p["precio_antes"], p["url"]),
                         ("Portátil Dell Pro 15 Essential", 1599.62, 1749.66,
                          "https://www.dell.com/es-es/shop/laptop-computers/spd/pv15250_so_12"))

    def test_huawei_compareprice_y_gbomcode(self):
        api = {"data": {"prdDisplayInfos": [{"sbomList": [
            {"name": "HUAWEI FreeBuds Neo Blanco Hielo", "price": 99.0, "comparePrice": 129.0,
             "gbomCode": "6942711308331"},
            {"name": "HUAWEI FreeClip 2 Negro", "price": 199.0, "comparePrice": 199.0, "gbomCode": "6942711300000"}]}]}}
        ps = {p["nombre"]: p for p in v.extraer("<html></html>", "https://consumer.huawei.com/es/offer/", [api])}
        self.assertEqual((ps["HUAWEI FreeBuds Neo Blanco Hielo"]["precio_antes"], ps["HUAWEI FreeBuds Neo Blanco Hielo"]["ean"]),
                         (129.0, "6942711308331"))
        self.assertIsNone(ps["HUAWEI FreeClip 2 Negro"]["precio_antes"], "comparePrice igual: sin rebaja")

    def test_tienda_via_comparador(self):
        # PcComponentes leída por PCComTracker: solo filas con enlace directo a PcComponentes,
        # con su URL y su precio (no los del comparador ni los de Amazon/MediaMarkt).
        ld = {"@type": "ItemList", "itemListElement": [
            {"@type": "Product", "name": "Logitech G733 Lightspeed Lilas", "url": "https://pccomtracker.com/producto/g733-1",
             "offers": {"price": 99.0, "priceSpecification": {"priceType": "https://schema.org/StrikethroughPrice",
                                                              "price": 149.0}}}]}
        fila = lambda n, tienda, dom, precio: (
            f'<a href="/producto/{n.lower().replace(" ", "-")}-1">{n}</a>'
            f'<a href="https://{dom}/{n.lower().replace(" ", "-")}" target="_blank" '
            f'aria-label="Ver {n} en {tienda} por {precio} €" class="x">{precio} €</a>')
        html = ('<script type="application/ld+json">' + json.dumps(ld) + "</script>"
                + fila("Logitech G733 Lightspeed Lilas", "PcComponentes", "www.pccomponentes.com", "122,98")
                + fila("Logitech G733 Lightspeed Lilas", "Amazon", "www.amazon.es", "119,00")
                + fila("Oral-B Vitality Pro Morado", "PcComponentes", "www.pccomponentes.com", "22,99")
                + '<a href="/goto/amazon?url=x" aria-label="Ver algo en Amazon por 5 €">x</a>')
        base = "https://pccomtracker.com/black-friday"
        ps = {p["nombre"]: p for p in v.via_comparador(html, v.extraer(html, base), "pccomponentes.com", base)}
        self.assertEqual(set(ps), {"Logitech G733 Lightspeed Lilas", "Oral-B Vitality Pro Morado"})
        g = ps["Logitech G733 Lightspeed Lilas"]
        self.assertEqual((g["precio"], g["precio_antes"], g["url"]),
                         (122.98, 149.0, "https://www.pccomponentes.com/logitech-g733-lightspeed-lilas"))
        self.assertEqual(g["historico_url"], "https://pccomtracker.com/producto/logitech-g733-lightspeed-lilas-1",
                         "la ficha del tracker de la misma fila, para leer el histórico")
        ficha = ('<p>Su precio habitual es de 1303.78€, con un mínimo en el último año de 1069.00€ '
                 '(alcanzado el 4 de noviembre de 2026)</p>')
        orig, v.descargar = v.descargar, lambda url, timeout=20, reintentos=1: (ficha, url)
        try:
            r = v.historico({"historicos": {}}, {"n": "x", "t": "pccomponentes.com", "p": 1239.0,
                                                 "h": "https://pccomtracker.com/producto/book6-1"})
        finally:
            v.descargar = orig
        self.assertEqual((r["minimo"], r["habitual"], r["fuente"], r["es_minimo"]),
                         (1069.0, 1303.78, "pccomtracker.com", False))
        # Página 2 del comparador sin enlaces a la tienda: nada (antes salían con la URL del tracker).
        p1 = html
        p2 = '<script type="application/ld+json">' + json.dumps({"@type": "ItemList", "itemListElement": [
            {"@type": "Product", "name": "Samsung Galaxy Book6", "url": "https://pccomtracker.com/producto/book6-1",
             "offers": {"price": 1239.0}}]}) + "</script>"
        paginas = {base: p1, base + "?page=2": p2}
        orig, v.descargar = v.descargar, lambda url, timeout=20, reintentos=1: (paginas[url], url)
        sleep, v.time.sleep = v.time.sleep, lambda s: None
        try:
            _, _, ps2, _ = v.leer_tienda("pccomponentes.com", {"pccomponentes.com": base}, None, None, 3)
        finally:
            v.descargar, v.time.sleep = orig, sleep
        self.assertEqual(sorted(p["nombre"] for p in ps2), ["Logitech G733 Lightspeed Lilas", "Oral-B Vitality Pro Morado"])
        self.assertTrue(all("pccomponentes.com" in p["url"] for p in ps2))
        # En la web de la propia tienda no cambia nada.
        propios = [{"nombre": "X", "precio": 1.0, "precio_antes": None, "url": None}]
        self.assertIs(v.via_comparador("", propios, "pccomponentes.com", "https://www.pccomponentes.com/ofertas"), propios)

    def test_precioreal_si_idealo_bloquea(self):
        # Idealo devuelve 403 (Akamai) -> PrecioReal, solo con el mismo modelo y sin accesorios.
        pr = '<script type="application/ld+json">' + json.dumps({"@type": "ItemList", "itemListElement": [
            {"@type": "Product", "name": n, "offers": {"price": precio}} for n, precio in
            (("Xiaomi Redmi Buds 6 Pro", 44.99), ("Xiaomi Redmi Buds 6", 32.95), ("Xiaomi Redmi Buds 6 Play", 10.7),
             ("Xiaomi Redmi Buds 6 estuche", 7.19))]}) + "</script>"
        pedidas = []

        def red(url, timeout=20, reintentos=1):
            pedidas.append(url)
            if "idealo" in url:
                raise OSError("HTTP Error 403: Forbidden")
            return pr, url
        orig, v.descargar = v.descargar, red
        sleep, v.time.sleep = v.time.sleep, lambda s: None
        v._IDEALO_BLOQUEADO[0] = False
        # Carpeta propia: sin ella, el finally borraba data\idealo_pausa.txt de producción (17/11/2026).
        datos_orig, v.DATOS = v.DATOS, Path(tempfile.mkdtemp())
        try:
            p = {"nombre": "Xiaomi Redmi Buds 6 Blanco TWS ANC Bluetooth 5.4", "precio": 34.99, "ean": None}
            self.assertEqual(v.idealo_red(p), 32.95, "ni la versión Play ni el accesorio")
            # Moda y deporte: sin PrecioReal de reserva (17/11: Levi's 726 contra una ficha del modelo sin lavado).
            self.assertIsNone(v.idealo_red(p, precioreal=False))
            real = json.loads((CONF / "tiendas.json").read_text(encoding="utf-8"))["categorias"]
            self.assertTrue(real["moda"]["sin_precioreal"] and real["deporte"]["sin_precioreal"])
            self.assertFalse(real["tecnologia"].get("sin_precioreal", False))
            self.assertIn("precioreal.com/comparador/catalogo?q=Xiaomi+Redmi+Buds+6+TWS", pedidas[-1])
            antes = len([u for u in pedidas if "idealo" in u])
            self.assertEqual(v.idealo_red(p), 32.95)
            self.assertEqual(len([u for u in pedidas if "idealo" in u]), antes, "tras un 403 no se vuelve a pedir a Idealo")
            # La pausa dura 3 h y vale para las rondas siguientes (otro proceso: la marca del proceso empieza apagada).
            self.assertTrue(v.idealo_en_pausa())
            v._IDEALO_BLOQUEADO[0] = False
            self.assertEqual(v.idealo_red(p), 32.95)
            self.assertEqual(len([u for u in pedidas if "idealo" in u]), antes, "en pausa: ni una petición más")
            # Pasadas las 3 h se vuelve a probar, y si Idealo ya responde se usa.
            vieja = time.time() - (v.IDEALO_REINTENTO_H + 1) * 3600
            os.utime(v._archivo_pausa_idealo(), (vieja, vieja))
            self.assertFalse(v.idealo_en_pausa())
            v._IDEALO_BLOQUEADO[0] = False
            idealo_ok = '<script type="application/ld+json">' + json.dumps({"@type": "ItemList", "itemListElement": [
                {"@type": "Product", "name": "Xiaomi Redmi Buds 6 Blanco TWS ANC Bluetooth 5.4", "offers": {"price": 29.9}}]}) + "</script>"
            v.descargar = lambda url, timeout=20, reintentos=1: (idealo_ok, url)
            self.assertEqual(v.idealo_red(p), 29.9)
            self.assertFalse(v.idealo_en_pausa() and v._IDEALO_BLOQUEADO[0], "si responde, no se pausa")
            # Enlace para mirarlo a mano en cada aviso sin precio de mercado.
            self.assertIn("idealo.es/resultados.html?q=", v.enlace_idealo(p))
            self.assertIn("Idealo, a mano: https://www.idealo.es/", v.mensaje_candidato(
                5, {**p, "tienda": "t.es", "precio_antes": 50.0, "ch": "no", "url": "https://t.es/p"}))
        finally:
            v.descargar, v.time.sleep = orig, sleep
            v._IDEALO_BLOQUEADO[0] = False
            v._archivo_pausa_idealo().unlink(missing_ok=True)
            v.DATOS = datos_orig
        self.assertFalse(v.mismo_modelo("Samsung Galaxy S26 256GB Negro", "Samsung Galaxy S26 512GB"))
        self.assertTrue(v.mismo_modelo("Samsung Galaxy S26 256GB Negro", "Samsung Galaxy S26 256GB"))
        # Mismo código exacto aunque el comparador añada palabras; código distinto, fuera.
        self.assertTrue(v.mismo_modelo("Televisor LG OLED65C66LB", 'LG OLED evo C6 OLED65C66LB 65" 4K UHD Smart TV'))
        self.assertFalse(v.mismo_modelo("Televisor LG OLED65C66LB", 'LG OLED C6 OLED55C66LB 55" 4K Smart TV'))
        # Caso real: el Book6 (Core Ultra 5, Windows 11 Pro) se emparejó con el Book6 Pro (Ultra 7) a 1.634 €.
        book6 = 'Portátil Samsung Galaxy Book6 16" Intel Core Ultra 5 325 16GB 512GB SSD Intel Graphics Windows 11 Pro'
        self.assertFalse(v.mismo_modelo(book6, 'Portátiles Samsung Samsung Galaxy Book6 Pro 16" Intel Core Ultra 7 358H 32GB'))
        self.assertTrue(v.mismo_modelo(book6, 'Samsung Galaxy Book6 16" Intel Core Ultra 5 325 16GB 512GB'))
        self.assertEqual(v.codigos_modelo(book6), set(), "BOOK6 es la familia, no un código de modelo")
        # 17/11/2026: el procesador no es el modelo ('lenovo I5-13420H' buscaba cualquier Lenovo, '3700U' placas
        # de inducción) y dos portátiles con el mismo procesador no son "el mismo código exacto".
        for cpu in ("I5-13420H", "I7-1185G7", "7533HS", "3700U", "I3-1315U"):
            self.assertEqual(v.lista_codigos(f"Portátil Lenovo IdeaPad {cpu} 16GB 512GB"), [], cpu)
        self.assertEqual(v.lista_codigos("Portátil Lenovo IdeaPad Slim 3 15IRH10 i5-13420H"), ["15IRH10"])
        self.assertEqual(v.consulta_comparador({"nombre": "Portátil Lenovo IdeaPad Slim 3 15IRH10 i5-13420H",
                                                "precio": 579.0, "tienda": "mediamarkt.es"}).split()[-1], "15IRH10")
        self.assertFalse(v.mismo_modelo("Portátil Lenovo IdeaPad 15,6 i5-13420H 16GB", "Lenovo ThinkPad L14 i5-13420H 16GB"))
        # Caso real: 'Bluetooth 5.4' hacía pasar a los Buds 4 y Buds 5 por los Buds 6.
        buds6 = "Xiaomi Redmi Buds 6 Blanco TWS ANC Bluetooth 5.4 42h IP54 Auriculares"
        self.assertFalse(v.mismo_modelo(buds6, "Xiaomi Redmi Buds 4"))
        self.assertFalse(v.mismo_modelo(buds6, "Xiaomi Buds 5"))
        self.assertTrue(v.mismo_modelo(buds6, "Xiaomi Redmi Buds 6"))
        # Tienda oficial: el nombre no lleva la marca; se busca por marca + código.
        sony = {"nombre": "Auriculares inalámbricos con Noise Cancelling WF-C710N", "marca": None, "tienda": "sony.es"}
        self.assertEqual(v.consulta_comparador(sony), "sony WF-C710N")
        self.assertTrue(v.mismo_modelo(v.nombre_con_marca(sony), "Sony WF-C710N"))
        self.assertFalse(v.mismo_modelo(v.nombre_con_marca(sony), "Sony WF-C700N"))
        # Tienda que no es de marca: no se le añade su nombre; IP54 no es un código de modelo.
        buds = {"nombre": "Xiaomi Redmi Buds 6 Blanco TWS ANC 42h IP54", "marca": None, "tienda": "powerplanetonline.com"}
        self.assertEqual(v.consulta_comparador(buds), "Xiaomi Redmi Buds 6 TWS")

    def test_ssl_usa_almacen_de_windows_y_verifica(self):
        import ssl
        ctx = v._contexto_ssl()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED, "nunca se desactiva la verificación")
        self.assertTrue(ctx.check_hostname)

    def test_precio_antes_igual_se_ignora(self):
        html = tienda_html([{"n": "Sony WH-1000XM5", "p": 299, "a": 299}])
        self.assertIsNone(v.extraer(html, "https://x.es")[0]["precio_antes"])

    def test_ean_invalido(self):
        html = tienda_html([{"n": "Sony WH-1000XM5", "p": 299, "e": "12AB"}])
        self.assertIsNone(v.extraer(html, "https://x.es")[0]["ean"])

    def test_marcas(self):
        v_marcas, v.MARCAS = v.MARCAS, CONF / "marcas.txt"
        try:
            rx = v.regex_marcas()
        finally:
            v.MARCAS = v_marcas
        ok = lambda n, m=None: v.marca_ok({"nombre": n, "marca": m}, rx)
        self.assertTrue(ok("Auriculares JBL Tune 520BT"))
        self.assertTrue(ok("Freidora", "Cosori"))
        self.assertTrue(ok("Cafetera De'Longhi Dedica"))
        self.assertTrue(ok("Nothing Phone (3a)"))
        self.assertFalse(ok("Auriculares Bluetooth KJHGF Pro"))
        self.assertFalse(ok("Me queda poco de batería"))
        self.assertFalse(ok("Nothing special cable"))
        self.assertFalse(ok("Funda para Applewatch"))  # no coincide con trozos de palabra
        # Tiendas oficiales: el producto no repite la marca en el nombre
        self.assertTrue(v.marca_ok({"nombre": "Galaxy S26+", "marca": None}, rx, "samsung.com/es"))
        self.assertTrue(v.marca_ok({"nombre": "Redmi Pad 2", "marca": None}, rx, "mi.com/es"))
        self.assertFalse(v.marca_ok({"nombre": "Secador 1800W", "marca": None}, rx, "electrodepot.es"))

    def test_horario_cruza_medianoche(self):
        orig = v.ahora_madrid
        try:
            for hora, esperado in {"07:59": False, "08:00": True, "15:00": True, "23:59": True,
                                   "00:30": True, "00:45": True, "00:46": False, "03:00": False}.items():
                v.ahora_madrid = lambda h=hora: datetime.strptime(h, "%H:%M")
                self.assertEqual(v.en_horario({"horario": {"inicio": "08:00", "fin": "00:45"}}), esperado, hora)
        finally:
            v.ahora_madrid = orig

    def test_urls(self):
        self.assertEqual(v.url_tienda("mi.com/es"), "https://www.mi.com/es/")
        self.assertEqual(v.url_tienda("store.steampowered.com"), "https://store.steampowered.com/")
        self.assertEqual(v.raiz("mediamarkt.es"), "mediamarkt")
        self.assertEqual(v.raiz("samsung.com/es"), "samsung")


# --------------------------------------------------------------- rondas

class Rondas(Base):
    def preparar(self):
        self.red.tiendas = {
            "mediamarkt": [{"n": "Xiaomi Redmi Buds 6 Pro", "m": "Xiaomi", "e": "6941812790000", "p": 39.99, "a": 79.99},
                           {"n": "Cosori Freidora 5,5 L", "p": 79.99, "a": 99.99, "ld": False}],
            "pccomponentes": [{"n": "Xiaomi Redmi Buds 6 Pro", "m": "Xiaomi", "e": "6941812790000", "p": 41.99, "a": 79.99}],
        }
        self.red.idealo = {"6941812790000": "45,00"}  # 34,99 € = 22 % menos (guía: 20 %)

    def test_primera_ronda_solo_memoriza(self):
        self.preparar()
        self.assertEqual(self.ronda(), [])
        self.assertEqual(self.red.llamadas["idealo"], 0)

    def test_sin_cambios_no_despierta(self):
        self.preparar()
        self.ronda()
        self.assertEqual(self.ronda(), [])
        self.assertEqual(self.red.llamadas["idealo"] + self.red.llamadas["chollometro"], 0)

    def test_bajada_y_duplicados(self):
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 34.99
        self.red.tiendas["pccomponentes"][0]["p"] = 35.99
        c = self.ronda()
        self.assertEqual(len(c), 1, "mismo EAN en 2 tiendas: solo el más barato")
        self.assertEqual((c[0]["t"], c[0]["p"], c[0]["id"], c[0]["ch"], c[0]["baja"]),
                         ("mediamarkt.es", 34.99, 45.0, "no", 1))
        self.assertLess(len(json.dumps(c, separators=(",", ":"))), 400, "JSON compacto")

    def test_no_repite_si_sube_y_vuelve_a_bajar(self):
        self.preparar()
        self.ronda()
        buds = self.red.tiendas["mediamarkt"][0]
        buds["p"] = 34.99
        self.assertEqual(len(self.ronda()), 1)
        buds["p"] = 39.99
        self.assertEqual(self.ronda(), [])
        buds["p"] = 34.99
        self.assertEqual(self.ronda(), [], "mismo precio ya avisado")
        buds["p"] = 29.99
        self.assertEqual(len(self.ronda()), 1, "nuevo mínimo: sí avisa")

    def test_producto_nuevo(self):
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"].append({"n": "JBL Flip 6", "m": "JBL", "e": "6925281993145", "p": 79.0, "a": 139.0})
        self.red.idealo["6925281993145"] = "99,00"
        c = self.ronda()
        self.assertEqual([x["n"] for x in c], ["JBL Flip 6"])
        self.assertNotIn("baja", c[0])

    def test_filtros_gratis(self):
        self.preparar()
        c = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        c["filtros_codigo"]["precio_max_eur"] = 600  # el tope sigue funcionando si se pone
        v.CONFIG.write_text(json.dumps(c), encoding="utf-8")
        self.ronda()
        self.red.tiendas["mediamarkt"] += [
            {"n": "Auriculares KJHGF Pro", "p": 9.99, "a": 59.99},              # sin marca conocida
            {"n": "Samsung TV QLED 75", "m": "Samsung", "p": 999, "a": 1999},   # demasiado caro
            {"n": "Sony WH-CH520", "m": "Sony", "p": 45, "a": 49},              # descuento < 20 %
        ]
        self.assertEqual(self.ronda(), [])
        self.assertEqual(self.red.llamadas["idealo"], 0, "descartados antes de consultar Idealo")

    def test_idealo_mas_barato_descarta(self):
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"].append({"n": "JBL Flip 6", "m": "JBL", "e": "6925281993145", "p": 99.0, "a": 139.0})
        self.red.idealo["6925281993145"] = "79,00"
        self.assertEqual(self.ronda(), [])

    def test_precioreal_antes_que_idealo(self):
        # 18/11/2026 (Zeta): primero PrecioReal; Idealo solo para lo que PrecioReal no encuentre.
        self._exigir_mercado()
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"].append({"n": "JBL Flip 6", "m": "JBL", "e": "6925281993145", "p": 79.0, "a": 139.0})
        self.red.tiendas["precioreal"] = [{"n": "JBL Flip 6", "m": "JBL", "p": 99.0, "a": 139.0}]
        self.red.idealo["6925281993145"] = "99,00"
        antes = self.red.llamadas["idealo"]
        c = self.ronda()
        self.assertEqual([x["n"] for x in c], ["JBL Flip 6"])
        self.assertEqual(c[0]["id"], 99.0, "el precio de mercado es el de PrecioReal")
        self.assertEqual(self.red.llamadas["idealo"], antes, "con PrecioReal no se consulta Idealo")

    def test_sin_precio_de_mercado_no_se_envia(self):
        # Sony WF-C710N "antes 199 €" a 89 €: sin comparador no se sabe si el 'antes' es real.
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"]["exigir_precio_mercado"] = True
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"] += [
            {"n": "Sony WF-C710N", "m": "Sony", "p": 89.0, "a": 199.0},          # sin Idealo: fuera
            {"n": "JBL Flip 6", "m": "JBL", "e": "6925281993145", "p": 79.0, "a": 139.0}]
        self.red.idealo["6925281993145"] = "99,00"                               # con Idealo: pasa
        self.assertEqual([x["n"] for x in self.ronda()], ["JBL Flip 6"])

    def _exigir_mercado(self):
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"]["exigir_precio_mercado"] = True
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")

    def test_habitual_propio_en_la_ronda(self):
        # 17/11/2026: sin comparador ni PCComTracker, el precio habitual que ha visto el propio vigilante.
        self._exigir_mercado()
        self.preparar()
        self.red.tiendas["mediamarkt"].append({"n": "Sony WF-C710N", "m": "Sony", "p": 89.0, "a": 129.0})
        self.ronda()
        con = sqlite3.connect(v.DB)
        hace_20 = (datetime.now() - timedelta(days=20)).isoformat(timespec="minutes")
        con.execute("UPDATE precios SET fecha=? WHERE clave LIKE 'mediamarkt.es|%sony%'", (hace_20,))
        con.commit()
        con.close()
        self.red.tiendas["mediamarkt"][-1]["p"] = 79.0   # -11 % sobre su habitual de 89 €: no llega al 20 %
        self.assertEqual([x["n"] for x in self.ronda()], [])
        self.red.tiendas["mediamarkt"][-1]["p"] = 64.0   # -28 %: pasa
        c = self.ronda()
        self.assertEqual([x["n"] for x in c], ["Sony WF-C710N"])
        self.assertEqual(c[0]["hab"], "89 € (historial del vigilante)")

    def test_mercado_propio_en_la_ronda(self):
        # El mismo EAN en otra tienda que lee el vigilante es precio de mercado, sin consultar a nadie.
        self._exigir_mercado()
        self.preparar()
        self.red.tiendas["mediamarkt"].append({"n": "JBL Charge 5", "m": "JBL", "e": "6925281982170", "p": 129.0, "a": 179.0})
        self.red.tiendas["pccomponentes"].append({"n": "JBL Charge 5", "m": "JBL", "e": "6925281982170", "p": 139.0, "a": 179.0})
        self.ronda()
        self.red.tiendas["mediamarkt"][-1]["p"] = 135.0   # baja de su mínimo, pero no es más barato que la otra tienda... sí lo es por poco
        self.assertEqual([x["n"] for x in self.ronda()], [], "solo un 3 % por debajo de PcComponentes")
        self.red.tiendas["mediamarkt"][-1]["p"] = 99.0
        c = self.ronda()
        self.assertEqual([x["n"] for x in c], ["JBL Charge 5"])
        self.assertEqual(c[0]["id"], 139.0)
        self.assertEqual(c[0]["mp"]["tienda"], "pccomponentes.com")

    def test_categoria_sin_comparador(self):
        # Moda/hogar: sin precio de mercado vale un descuento grande (50 %), y el global sigue exigiendo mercado.
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"]["exigir_precio_mercado"] = True
        cfg["categorias"]["tecnologia"].update({"exigir_precio_mercado": False, "descuento_min_sin_mercado": 0.5})
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"] += [
            {"n": "Desigual Abrigo Acolchado", "m": "Desigual", "p": 59.0, "a": 149.0},   # -60 %: pasa
            {"n": "Desigual Bolso Shopper", "m": "Desigual", "p": 39.0, "a": 59.0}]       # -34 %: no
        cfg["filtros_codigo"]["descuento_min_sin_idealo"] = 0.3
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        v_marcas = v.MARCAS.read_text(encoding="utf-8")
        v.MARCAS.write_text(v_marcas + "\ndesigual\n", encoding="utf-8")
        self.assertEqual([x["n"] for x in self.ronda()], ["Desigual Abrigo Acolchado"])

    def test_tiene_que_estar_bajo_el_mercado(self):
        # Caso real: LG QNED70 a 348,12 € ("antes" PVPR 449 €) con el mercado a 339 €: no es chollo.
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"] += [
            {"n": "LG QNED70 43", "m": "LG", "e": "8806096000001", "p": 348.12, "a": 449.0},
            {"n": "LG QNED80 50", "m": "LG", "e": "8806096000002", "p": 399.0, "a": 549.0}]
        self.red.idealo.update({"8806096000001": "339,00", "8806096000002": "520,00"})  # 399 € = 23 % menos
        self.assertEqual([x["n"] for x in self.ronda()], ["LG QNED80 50"])

    def test_amazon_deals_en_script(self):
        # Amazon /deals: los productos van en el TERCER objeto de un script, con priceToPay/basisPrice.
        datos = {"productSearchResponse": {"products": [
            {"asin": "B0BVMS3QTW", "title": "Dodot Pañales Bebé Sensitive Talla 5, 152 Pañales",
             "link": "/Pa%C3%B1ales-Sensitive/dp/B0BVMS3QTW",
             "price": {"priceToPay": {"label": "Precio de la oferta:", "price": "59.77", "strikethrough": False},
                       "basisPrice": {"label": "Mediano:", "price": "82.85", "strikethrough": True}}}]}}
        html = ("<script>(function(){ var a = {\"uno\": \"" + "x" * 60 + "\"}; f({\"dos\": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]});"
                + " P.declare('deals', " + json.dumps(datos) + ");})();</script>")
        p = v.extraer(html, "https://www.amazon.es/deals")[0]
        self.assertEqual((p["precio"], p["precio_antes"], p["url"]),
                         (59.77, 82.85, "https://www.amazon.es/Pa%C3%B1ales-Sensitive/dp/B0BVMS3QTW"))

    def test_epic_marca_lo_gratis(self):
        api = {"data": {"Catalog": {"searchStore": {"elements": [
            {"title": "Flotsam", "productSlug": "flotsam",
             "promotions": {"promotionalOffers": [{"promotionalOffers": [{"discountSetting": {"discountPercentage": 0}}]}]},
             "price": {"totalPrice": {"discountPrice": 0, "originalPrice": 1999}}},
            {"title": "LISA: Definitive Edition", "productSlug": "lisa",
             "promotions": {"promotionalOffers": [{}]},
             "price": {"totalPrice": {"discountPrice": 1451, "originalPrice": 2419}}}]}}}}
        orig, v.descargar = v.descargar, lambda url, timeout=20, reintentos=1: (json.dumps(api), url)
        try:
            ps = {p["nombre"]: p for p in v.epic_gratis()}
        finally:
            v.descargar = orig
        self.assertTrue(ps["Flotsam"]["gratis"])
        self.assertEqual((ps["Flotsam"]["precio"], ps["Flotsam"]["precio_antes"]), (0.0, 19.99))
        self.assertFalse(ps["LISA: Definitive Edition"]["gratis"])
        self.assertIn("store-site-backend-static.ak.epicgames.com", v.EPIC_PROMOS)

    def test_antes_desde_porcentaje(self):
        # GOG: precio + "discount": "-77%"; Humble: full_price.
        api = {"products": [
            {"title": "Celeste", "price": {"amount": "4.99"}, "discount": "-77%"},
            {"human_name": "x", "title": "Hades", "current_price": {"amount": 9.99}, "full_price": {"amount": 24.99}},
            {"title": "Juego sin rebaja", "price": 19.99, "discount": "CODIGO"}]}
        ps = {p["nombre"]: p["precio_antes"] for p in v.extraer("<html></html>", "https://t.es/", [api])}
        self.assertEqual(ps["Celeste"], 21.7)
        self.assertEqual(ps["Hades"], 24.99)
        self.assertIsNone(ps["Juego sin rebaja"])

    def test_antes_desde_etiqueta_de_descuento(self):
        # Instant Gaming: sin tachado, solo la etiqueta "-75%" en la tarjeta.
        ld = {"@type": "ItemList", "itemListElement": [
            {"@type": "Product", "name": "Hollow Knight", "url": "/es/123-hollow-knight/", "offers": {"price": 3.74}},
            {"@type": "Product", "name": "Hades II", "url": "/es/456-hades-ii/", "offers": {"price": 24.99}}]}
        html = ('<script type="application/ld+json">' + json.dumps(ld) + "</script>"
                '<div><a href="/es/123-hollow-knight/">Hollow Knight</a><div class="discount">-75%</div><span>3,74 €</span></div>'
                '<div><a href="/es/456-hades-ii/">Hades II</a><span>24,99 €</span><p>-10% extra en app</p></div>')
        ps = {p["nombre"]: p["precio_antes"] for p in v.extraer(html, "https://www.instant-gaming.com/es/")}
        self.assertEqual(ps["Hollow Knight"], 14.96)
        self.assertIsNone(ps["Hades II"], "'-10% extra en app' es una promoción, no el descuento del juego")

    def test_feeds_de_viajes(self):
        rss = ('<?xml version="1.0"?><rss><channel><item><title>LUJO en el Mar Rojo con TODO INCLUIDO</title>'
               '<link>https://www.viajerospiratas.es/mar-rojo/</link><description>&lt;p&gt;Un viaje de lujo en Sharm El Sheik '
               'desde solo 332€. Es una zona...&lt;/p&gt;</description></item>'
               '<item><title>Sin precio</title><link>https://x/</link><description>nada</description></item></channel></rss>')
        listings = ('<?xml version="1.0"?><listings><title>Busco un chollo</title><listing><id>1</id>'
                    '<title>¡Vive las Fiestas de Benidorm!</title><url>https://www.buscounchollo.com/reserva-chollo/40176/x</url>'
                    '<price>39.00 EUR</price><description>2 días y 1 noche en Benidorm</description></listing></listings>')
        orig = v.descargar
        v.descargar = lambda url, timeout=20, reintentos=1: (listings if "buscounchollo" in url else rss, url)
        try:
            vp = v.leer_rss("viajerospiratas.es")
            bc = v.leer_rss("buscounchollo.com")
        finally:
            v.descargar = orig
        self.assertEqual([(p["nombre"], p["precio"]) for p in vp], [("LUJO en el Mar Rojo con TODO INCLUIDO", 332.0)])
        self.assertEqual([(p["precio"], p["url"]) for p in bc], [(39.0, "https://www.buscounchollo.com/reserva-chollo/40176/x")])

    def test_importes_en_centimos(self):
        # Groupon: {"amount": 1499, "currencyExponent": 2} = 14,99 €.
        api = {"deals": [{"title": "Higiene facial con tratamiento en Cuídate Córdoba",
                          "url": "https://www.groupon.es/deals/cuidate-cordoba-12",
                          "prices": {"price": {"amount": 1499, "currencyCode": "EUR", "currencyExponent": 2},
                                     "strikeThroughPrice": {"amount": 3000, "currencyCode": "EUR", "currencyExponent": 2}}}]}
        p = v.extraer("<html></html>", "https://www.groupon.es/", [api])[0]
        self.assertEqual((p["precio"], p["precio_antes"]), (14.99, 30.0))

    def test_url_del_listitem(self):
        # Outlet de LG: {"@type":"ListItem","url":...,"item":{"@type":"Product",...}} sin url dentro.
        html = '<script type="application/ld+json">' + json.dumps({"@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "url": "https://www.lg.com/es/outlet/qned70-43/",
             "item": {"@type": "Product", "name": "Outlet LG QNED70 43", "offers": {"price": "348.12"}}}]}) + "</script>"
        self.assertEqual(v.extraer(html, "https://www.lg.com/es/outlet/")[0]["url"], "https://www.lg.com/es/outlet/qned70-43/")

    def test_pccomponentes_con_historico_del_tracker(self):
        # Sin precio de mercado, PcComponentes se compara con su propio historial (PCComTracker):
        # pasa cerca del mínimo o un 10 % bajo lo habitual; el Galaxy Book6 a 1.239 € (mín. 1.069, habitual 1.303), no.
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"]["exigir_precio_mercado"] = True
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        hist = {"Samsung Galaxy Book6": {"minimo": 1069.0, "habitual": 1303.78},
                "Samsung Odyssey G5": {"minimo": 199.0, "habitual": 279.0}}
        orig = (v.via_comparador, v.historico_comparador)
        v.via_comparador = lambda html, ps, t, base, estricto=False: [
            {**p, "historico_url": "https://pccomtracker.com/producto/" + p["nombre"].replace(" ", "-")} for p in ps
        ] if "pccomponentes" in t else ps
        v.historico_comparador = lambda url: {**hist[url.rsplit("/", 1)[1].replace("-", " ")], "url": url}
        try:
            self.red.tiendas = {"mediamarkt": [], "pccomponentes": [{"n": "Logitech G502 X", "m": "Logitech", "p": 59.0}]}
            self.ronda()  # primera lectura: solo memoriza
            self.red.tiendas["pccomponentes"] += [{"n": "Samsung Galaxy Book6", "m": "Samsung", "p": 1239.0},
                                                  {"n": "Samsung Odyssey G5", "m": "Samsung", "p": 204.0}]
            self.assertEqual([x["n"] for x in self.ronda()], ["Samsung Odyssey G5"])
        finally:
            v.via_comparador, v.historico_comparador = orig

    def test_sin_idealo_exige_mas_descuento(self):
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"] += [
            {"n": "Philips Airfryer XL", "m": "Philips", "p": 69.0, "a": 99.0},   # 30 %: no basta
            {"n": "Braun Series 7", "m": "Braun", "p": 59.0, "a": 129.0},         # 54 %: sí
        ]
        self.assertEqual([x["n"] for x in self.ronda()], ["Braun Series 7"])

    def test_cache_idealo(self):
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 34.99
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 32.99
        self.ronda()
        self.assertEqual(self.red.llamadas["idealo"], 1, "segunda consulta sale de la caché")

    def test_chollometro(self):
        # Copia local de Chollometro (/search está prohibido en su robots.txt).
        p = {"nombre": "Xiaomi Redmi Buds 6 Pro Negro", "tienda": "mediamarkt.es", "precio": 34.99}

        def estado(*hs, fresca=True):
            q = dict(p)
            return v.estado_chollometro(q, {"hilos": v.hilos_chollometro(cm_html(list(hs))), "fresca": fresca}), q

        self.assertEqual(estado(hilo("Xiaomi Redmi Buds 6 Pro", "MediaMarkt", 34.99))[0], "misma")
        self.assertEqual(estado(hilo("Xiaomi Redmi Buds 6 Pro", "MediaMarkt", 39.99))[0], "misma:39.99")
        self.assertEqual(estado(hilo("Redmi Buds 6 Pro a precio mínimo", "Amazon", 36.0))[0], "otra:36")
        self.assertEqual(estado(hilo("Xiaomi Redmi Buds 4 Pro", "Amazon", 20.0))[0], "no", "otro modelo")
        self.assertEqual(estado(fresca=False)[0], "?", "copia sin actualizar: no se sabe")
        ch, q = estado(hilo("Xiaomi Redmi Buds 6 Pro", "Amazon", 36.0, temp=850, dias=90, caducado=True))
        self.assertEqual(ch, "no", "post viejo y caducado: no es duplicado")
        self.assertEqual((q["_cm_hot"]["temp"], q["_cm_hot"]["precio"]), (850, 36.0))
        self.assertTrue(v.demanda_probada_ok(q), "ahora más barato que cuando fue chollo caliente")
        # En una ronda: la copia se pone al día leyendo /nuevos y la portada.
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 32.99
        self.red.chollometro = [hilo("Xiaomi Redmi Buds 6 Pro", "Amazon", 36.0)]
        self.assertEqual(self.ronda()[0]["ch"], "otra:36")

    def test_tarjetas_amazon_con_enlace_a_opiniones(self):
        # 17/11: los más vendidos de Amazon salían con el nombre "4,6 de 5 estrellas 292.057".
        tarjeta = ('<div><a href="/Tapo-C210-Vigilancia/dp/B07XLML2YS/ref=zg_bs"><img alt="Tapo C210 camara">'
                   '<div>Tapo C210 - Cámara de Vigilancia WiFi Interior 360º, 2K</div></a>'
                   '<a href="/product-reviews/B07XLML2YS/ref=zg_bs_rv" title="4,6 de 5 estrellas 292.057">'
                   '4,6 de 5 estrellas 292.057</a><span>22,99 €</span></div>')
        ps = v.extraer("<html>" + tarjeta * 1 + "</html>", "https://www.amazon.es/gp/bestsellers/electronics")
        self.assertEqual([(p["nombre"][:9], p["precio"]) for p in ps], [("Tapo C210", 22.99)])
        # Menú de Carrefour: las categorías de la tienda sí, los subdominios (seguros) y los servicios no.
        menu = ('<a href="https://www.carrefour.es/informatica">Informática</a>'
                '<a href="https://seguros.carrefour.es/hogar">Seguro de hogar</a>'
                '<a href="https://www.carrefour.es/servicios/instalacion.e">Instalación electrodomésticos</a>')
        self.assertEqual(v.categorias_de_portada(menu, "https://www.carrefour.es/"),
                         ["https://www.carrefour.es/informatica"])

    def test_multicompra(self):
        # 17/11/2026, Zeta: "si es la segunda unidad al 70 % no te enteras".
        casos = {"2ª unidad al 70%": ("2.ª unidad al -70 %", 2, 0.65), "Segunda unidad -50 %": ("2.ª unidad al -50 %", 2, 0.75),
                 "-70% en la 2ª unidad": ("2.ª unidad al -70 %", 2, 0.65), "2a ud. gratis": ("2.ª unidad gratis", 2, 0.5),
                 "Oferta 3x2 en toda la tienda": ("3x2", 3, 0.6667), "2 x 1": ("2x1", 2, 0.5),
                 "Llévate 3, paga 2": ("3x2", 3, 0.6667)}
        for texto, esperado in casos.items():
            self.assertEqual(v.promo_multicompra(texto), esperado, texto)
        for texto in ("Cable eléctrico 2x1,5 mm", "Alfombra 2x1 m", "Monitor 27 2560x1440", "Pack 4x4 pilas AA",
                      "Sofá de 3 plazas", "Samsung Galaxy S26 128GB", "Descuento del 70 %",
                      "2ª unidad -50% acum en tu tarjeta",       # Alcampo: saldo, no descuento en caja
                      "Guisantes Bonduelle 3 x 130 g",
                      # 17/11/2026: características del producto, no promociones.
                      "Aire acondicionado Split 2x1 Daikin 2MXM50A + 2 x FTXM25A", "Mitsubishi MXZ-2F42 multisplit 2x1",
                      "Switch HDMI 3x1 4K", "Conmutador KVM 2x1 USB", "Regleta 4x1 enchufes"):
            self.assertIsNone(v.promo_multicompra(texto), texto)
        # En el nombre, un NxM solo cuenta si dice que es una promoción.
        self.assertIsNone(v.promo_multicompra("Daikin Sensira 2x1", nombre=True))
        self.assertEqual(v.promo_multicompra("Oferta 2x1 Coca-Cola Zero 2 L", nombre=True), ("2x1", 2, 0.5))
        self.assertEqual(v.promo_multicompra("Llévate 3, paga 2 en pañales Dodot", nombre=True), ("3x2", 3, 0.6667))
        split = ('<a href="/p/daikin-split-2x1-1">Aire acondicionado Split 2x1 Daikin</a><span>1.299,00 €</span>'
                 '<a href="/p/otro-2">Ventilador Cecotec</a><span>39,00 €</span>')
        ps = v.extraer("<html>" + split + "</html>", "https://tienda.es/ofertas")
        self.assertFalse([p for p in ps if p.get("promo")], "el Split 2x1 no es un 2x1")
        # Zeta: "muchas veces no viene en el título sino en una etiqueta". La etiqueta de la tarjeta sí cuenta.
        etiquetas = ('<a href="/p/cola-1">Coca-Cola Zero 2 L</a><span>2,00 €</span><span class="badge">2x1</span>'
                     '<a href="/p/fanta-2">Fanta Naranja 2 L</a><span>1,80 €</span><span class="tag">3x2</span>'
                     '<a href="/p/split-3">Aire acondicionado Split 2x1 Daikin</a><span>999,00 €</span>'
                     '<a href="/p/x-4">Ventilador Cecotec</a><span>39,00 €</span>')
        promos = {p["nombre"][:9]: (p.get("promo"), p.get("precio_unidad"))
                  for p in v.extraer("<html>" + etiquetas + "</html>", "https://t.es/ofertas")}
        self.assertEqual(promos["Coca-Cola"], ("2x1", 1.0))
        self.assertEqual(promos["Fanta Nar"], ("3x2", 1.2))
        self.assertEqual(promos["Aire acon"], (None, None))
        tarjetas = ('<a href="/p/gel-ducha-123456">Gel de ducha Sanex 600 ml</a><span>4,00 €</span>'
                    '<span class="badge">2ª unidad al 70%</span>'
                    '<a href="/p/champu-654321">Champú H&S 300 ml</a><span>5,00 €</span>') * 1
        ps = v.extraer("<html>" + tarjetas + "</html>", "https://tienda.es/ofertas")
        por_nombre = {p["nombre"][:5]: p for p in ps}
        self.assertEqual((por_nombre["Gel d"].get("promo"), por_nombre["Gel d"].get("precio_unidad")), ("2.ª unidad al -70 %", 2.6))
        self.assertIsNone(por_nombre["Champ"].get("promo"), "la promoción de una tarjeta no pasa a la siguiente")
        # En la ficha: precio por unidad, unidad suelta y total, y el aviso de confirmarla en la cesta.
        item = {"num": 1, "n": "Gel de ducha Sanex 600 ml", "u": "https://tienda.es/p/gel", "p": 2.6, "ps": 4.0,
                "promo": "2.ª unidad al -70 %", "uds": 2}
        b = v.bloques_de_codigo(item, {"precio": 2.6, "etiqueta": "SUBIENDO", "prueba": "x"})
        self.assertTrue(b["precios"].startswith("2,60 € por unidad comprando 2 (2.ª unidad al -70 %)\nUnidad suelta: 4 €. Total por 2: 5,20 €."))
        self.assertIn("⚠️ Promoción 2.ª unidad al -70 % leída en el listado: hay que comprar 2 y confirmarla en la cesta.", b["info"])
        # Verificar: la ficha da la unidad suelta (4 €); con la promoción sigue siendo 2,60 €, no "ha subido".
        cfg = v.cargar_config()
        d = v.decidir_item({**item, "validada": True, "id": 4.5, "ficha": {"precio_ahora": 4.0, "titulo": item["n"]}}, cfg)
        self.assertEqual(d.get("precio"), 2.6, d)

    def test_pcct_y_chollo_previo(self):
        # 1) Ficha de PCComTracker del mismo modelo, guardada al leer su catálogo, para un producto de Amazon.
        con = v.bd()
        v.guardar_pcct(con, [{"nombre": "Fuente MSI MAG A750GLS PCIe5 750W", "historico_url": "https://pccomtracker.com/producto/msi-99470"},
                             {"nombre": "Ratón sin ficha", "historico_url": None}])
        self.assertEqual(v.pcct_de(con, {"nombre": "MSI MAG A750GLS PCIE5 750W 80 Plus Gold"}), "https://pccomtracker.com/producto/msi-99470")
        self.assertIsNone(v.pcct_de(con, {"nombre": "MSI MAG A850GLS PCIE5 850W"}), "otro modelo")
        self.assertIsNone(v.pcct_de(con, {"nombre": "Fuente genérica 750W"}), "sin código no se busca")
        # 2) Chollo previo: mismo código, 200° o más en los últimos 6 meses, y ahora igual o más barato.
        v.guardar_hilos(con, v.hilos_chollometro(cm_html([
            hilo("Samsung TQ55Q7F5AU 55 QLED", "Amazon", 399.0, temp=250, dias=40),
            hilo("Samsung TQ55Q7F5AU 55 QLED 4K", "Amazon", 349.0, temp=120, dias=10),     # poco caliente
            hilo("Samsung TQ55Q7F5AU QLED", "MediaMarkt", 330.0, temp=900, dias=300)])))   # demasiado antiguo
        con.close()
        tv = "Samsung TQ55Q7F5AU 55 pulgadas 4K QLED HDR10+ Tizen Smart TV"
        self.assertEqual(v.chollo_previo({"nombre": tv, "precio": 389.0})["precio"], 399.0)
        self.assertIsNone(v.chollo_previo({"nombre": tv, "precio": 410.0}), "más caro que el chollo")
        self.assertIsNone(v.chollo_previo({"nombre": tv, "precio": 150.0}), "demasiado barato: otro producto")
        self.assertIsNone(v.chollo_previo({"nombre": "Televisor 55 pulgadas QLED", "precio": 300.0}), "sin código")

    def test_buscadores_de_chollometro_y_pccomtracker(self):
        # 17/11/2026: robots.txt en ninguna web. Chollometro: el buscador guarda en la copia lo que encuentra.
        viejo = hilo("Samsung TQ55Q7F5AU QLED 55", "Amazon", 389.0, temp=162, dias=150, caducado=True)
        pedidas = []

        def red(url, timeout=20, reintentos=1):
            pedidas.append(url)
            if "chollometro.com/search" in url:
                return cm_html([viejo]), url
            if "pccomtracker.com/api/search" in url:
                return json.dumps({"products": [{"id": "1", "name": "TV Samsung TQ55Q7F5AU 55 QLED 4K"},
                                                {"id": "2", "name": "TV Samsung TQ65Q7F5AU 65"}]}), url
            raise OSError("no esperada: " + url)
        v.descargar = red
        con = v.bd()
        p = {"nombre": "Samsung TQ55Q7F5AU 55 pulgadas 4K QLED", "tienda": "amazon.es"}
        self.assertEqual(v.buscar_en_chollometro(con, p), 1)
        self.assertIn("search?q=samsung+Q7F+55", pedidas[-1], "televisor Samsung: serie y pulgadas (18/11/2026)")
        self.assertEqual(con.execute("SELECT precio, temp FROM cm WHERE id=?", (viejo["threadId"],)).fetchone(), (389.0, 162.0))
        self.assertEqual(v.buscar_en_chollometro(con, p), 1)
        self.assertEqual(len(pedidas), 1, "caché de 6 h: no se repite la búsqueda")
        # PCComTracker: su buscador da la ficha del mismo modelo (no la de 65") y queda en el índice.
        url = v.pcct_de(con, p)
        self.assertEqual(url, "https://pccomtracker.com/producto/tv-samsung-tq55q7f5au-55-qled-4k-1")
        n = len(pedidas)
        self.assertEqual(v.pcct_de(con, p), url)
        self.assertEqual(len(pedidas), n, "la segunda vez sale del índice")
        con.close()

    def test_duplicados_reales_del_17_11(self):
        # #206: el mismo LEGO en Amazon al mismo precio no se detectaba ("7+ años" frente al set 21268, y "Bebé").
        self.assertTrue(v.mismo_post("Lego Minecraft La Casa-Cerdo Bebé, Granja con Figuras de Videojuego para Niños de 7+ Años",
                                     "LEGO Minecraft La Casa-Cerdo Bebé 21268"))
        # #205: un Garmin Instinct 3 no es un Fenix 8, aunque compartan "garmin mm pulgadas amoled".
        self.assertFalse(v.mismo_post("Garmin Instinct 3 Tactical Edition 50 mm Negro 1,3 pulgadas AMOLED 24 días 10ATM Smartwatch",
                                      "Garmin Fenix 8 47 mm Acero Negro 1,4 pulgadas AMOLED GPS NFC 3 años garantía"))
        self.assertFalse(v.mismo_post("Xiaomi Redmi Buds 6 Pro Negro", "Xiaomi Redmi Buds 4 Pro"), "otro modelo")
        self.assertTrue(v.mismo_post("Super Dragon Ball Heroes Switch Nintendo Switch (Code in a Box)",
                                     "Super Dragon Ball Heroes Switch - Nintendo Switch (CIAB)"))
        self.assertEqual(v.codigos_modelo("Garmin Instinct 3 10ATM 5BAR"), set(), "resistencia al agua, no modelo")
        self.assertIn("bebe", v.tokens("Casa-Cerdo Bebé"))

    def test_historial_de_precios(self):
        # 17/11/2026: solo se guarda una fila cuando cambia el precio; el mínimo sale con su fecha.
        con = v.bd()
        p = {"nombre": "JBL Flip 6 Negro", "url": "https://t.es/jbl", "precio": 99.0, "ean": "6925281993145"}
        clave = v.clave_vista("t.es", p)
        v.novedad(con, "t.es", dict(p))
        con.execute("UPDATE precios SET fecha='2026-11-09T10:00' WHERE clave=?", (clave,))
        v.novedad(con, "t.es", dict(p))                    # mismo precio: no apunta nada
        v.novedad(con, "t.es", dict(p, precio=79.0))
        con.execute("UPDATE precios SET fecha='2026-11-10T09:00' WHERE clave=? AND precio=79", (clave,))
        v.novedad(con, "t.es", dict(p, precio=89.0))
        self.assertEqual([r[0] for r in con.execute("SELECT precio FROM precios WHERE clave=? ORDER BY fecha", (clave,))],
                         [99.0, 79.0, 89.0])
        self.assertEqual(v.minimo_con_fecha(con, clave)[:2], (79.0, "2026-11-10T09:00"))
        self.assertEqual(con.execute("SELECT nombre, ean FROM productos WHERE clave=?", (clave,)).fetchone(),
                         ("JBL Flip 6 Negro", "6925281993145"))
        con.commit()
        con.close()
        for consulta in ("flip jbl", "6925281993145", "https://t.es/jbl"):
            with redirect_stdout(io.StringIO()) as salida:
                v.historial(consulta)
            self.assertIn("mínimo 79 € el 10/11/2026", salida.getvalue(), consulta)
        # En la INFO de la ficha, con su fecha.
        item = {"num": 1, "n": "JBL Flip 6", "u": "https://t.es/jbl", "p": 89.0,
                "minimo_propio": (79.0, "2026-11-10T09:00", "2026-11-09T10:00")}
        info = v.bloques_de_codigo(item, {"precio": 89.0, "etiqueta": "SUBIENDO", "prueba": "x"})["info"]
        self.assertIn("📉 Mínimo visto por el vigilante en esta tienda: 79 € (10/11/2026); seguido desde el 09/11.", info)

    def test_portada_refresca_temperaturas(self):
        # 17/11: el 92 % de los hilos guardaba la temperatura de recién publicado (la de /nuevos).
        h = hilo("Samsung Galaxy S26 256GB", "Amazon", 599.0, temp=40, dias=3)
        caliente = dict(h, temperature=720)
        pedidas = []

        def red(url, timeout=20, reintentos=1):
            pedidas.append(url)
            if "/nuevos" in url:
                return cm_html([RELLENO_CM, h]), url
            if url.endswith("?page=2"):
                return cm_html([caliente]), url
            return cm_html([RELLENO_CM]), url
        v.descargar = red
        con = v.bd()
        v.actualizar_chollometro(con, forzar=True)
        self.assertEqual(con.execute("SELECT temp FROM cm WHERE id=?", (h["threadId"],)).fetchone()[0], 720)
        for _ in range(v.CM_PORTADA_PAGINAS):
            v.actualizar_chollometro(con, forzar=True)
        portada = [u.rsplit("=", 1)[-1] for u in pedidas if "chollometro.com/?page=" in u]
        self.assertEqual(portada, [str(n) for n in [2, 3, 4, 5, 6, 7, 8, 2, 3]], "rota de la 2 a la 8")
        con.close()

    def test_descuento_real_minimo(self):
        # Regla de Instinct: BUENA = 25 % real sobre comparables exactos; el "antes" de la tienda no cuenta.
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"].update({"descuento_real_min": 0.25, "min_bajo_mercado": 0.05, "ahorro_min_eur": 0})
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"] += [
            {"n": "JBL Flip 6", "m": "JBL", "e": "6925281993145", "p": 79.0, "a": 199.0},      # mercado 95: -17 %
            {"n": "JBL Charge 5", "m": "JBL", "e": "6925281900002", "p": 99.0, "a": 179.0}]    # mercado 149: -34 %
        self.red.idealo.update({"6925281993145": "95,00", "6925281900002": "149,00"})
        c = self.ronda()
        self.assertEqual([x["n"] for x in c], ["JBL Charge 5"])
        self.assertTrue(c[0]["dr"].startswith("-34 % sobre mercado"))
        self.assertEqual(v.descuento_real({"precio": 50, "precio_antes": 200}, None), (None, None),
                         "solo el 'antes' de la tienda: no hay descuento real")

    def test_idealo_con_navegador_si_la_descarga_esta_en_pausa(self):
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"].update({"idealo_navegador": True, "exigir_precio_mercado": True,
                                      "min_bajo_mercado": 0.05, "ahorro_min_eur": 0})
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"] += [
            {"n": "JBL Charge 5", "m": "JBL", "e": "6925281900002", "p": 99.0, "a": 179.0},
            {"n": "JBL Flip 6", "m": "JBL", "e": "6925281993145", "p": 79.0, "a": 199.0}]
        vistos = []

        def nav(productos, cfg=None):
            vistos.extend(x["nombre"] for x in productos)
            return [149.0 if "Charge" in x["nombre"] else None for x in productos]
        orig = v.idealo_navegador
        v.idealo_navegador = nav
        try:
            v.pausar_idealo()
            c = self.ronda()
        finally:
            v.idealo_navegador = orig
            v._IDEALO_BLOQUEADO[0] = False
        self.assertIn("JBL Charge 5", vistos)
        self.assertEqual([x["n"] for x in c], ["JBL Charge 5"], "Flip 6 sin precio de mercado: no sale")
        con = v.bd()
        self.assertEqual(con.execute("SELECT precio FROM idealo WHERE consulta='6925281900002'").fetchone()[0], 149.0)
        con.close()

    def test_minimo_propio_necesita_14_dias(self):
        # 17/11: B&O Beoplay HX pasó con "-28 % sobre mínimo visto por el vigilante en 1 días".
        p = {"precio": 429.0, "estado": "bajada", "_min_previo": 599.0, "_seguido_dias": 1}
        self.assertEqual(v.descuento_real(p, None), (None, None))
        p["_seguido_dias"] = 14
        self.assertIn("14 días", v.descuento_real(p, None)[1])

    def test_accesorios_para_otro_producto(self):
        for n in ("Carcasa Inferior para Portátil Acer Aspire 5750/5750G/5750Z",
                  'HP Protective Funda Reversible Negra/Plata para Portátil 15"',
                  "Cable de carga para DJI Power 1000", "Filtro HEPA para Conga 2090"):
            self.assertTrue(v.es_accesorio(n), n)
        for n in ('HP Campus XL Mochila Tie Dye para Portátil de 16"',   # mochilas se calientan como la media
                  "Green Cell Ad74p Adaptador de Corriente 45W para HP",
                  "Funda Nórdica 150x220 algodón",                        # sin "para": es el producto
                  "Portátil Lenovo V15 G5 con funda para transporte",     # la funda no va al principio
                  "Fuente de Alimentación MSI 750W 80 PLUS Gold"):
            self.assertFalse(v.es_accesorio(n), n)

    def test_mensaje_sin_emojis_y_con_busqueda_manual(self):
        p = {"tienda": "mediamarkt.es", "nombre": "JBL Charge 5 Negro", "precio": 99.0, "precio_antes": 179.0,
             "idealo": 149.0, "ch": "no", "url": "https://x.es/jbl", "estado": "bajada", "gratis": True,
             "dto_real": 0.336, "ref_real": "mercado", "_cm_hot": {"temp": 850, "precio": 110.0, "dias": 90}}
        p["_iguales"] = [{"fecha": "10/03/2026", "precio": 110.0, "temp": 850, "tienda": "Amazon", "agotado": True,
                          "url": "https://www.chollometro.com/ofertas/jbl-charge-5-1"}]
        texto = v.mensaje_candidato(7, p)
        self.assertFalse(re.search(r"[\U0001F300-\U0001FAFF☀-➿⬇]", texto), texto)
        # 18/11/2026 (Zeta): los hilos encontrados con su precio, no el enlace de búsqueda manual
        self.assertIn("Hilo: 10/03/2026 · 110 € · 850° · Amazon · agotado\nhttps://www.chollometro.com/ofertas/jbl-charge-5-1",
                      texto)
        self.assertNotIn("search?q=", texto)
        p["_iguales"][0]["coincide"] = "coincide 92 %"
        self.assertIn("Amazon · agotado · coincide 92 %", v.mensaje_candidato(7, p))
        self.assertEqual(v.coincidencia("Sony WH-CH720N Azul Auriculares", "Sony WH-CH720N Auriculares Bluetooth"), 100)
        self.assertIn("referencia de la tienda: 179 €", texto)
        self.assertIn("Descuento real: -34 % sobre mercado", texto)

    def test_etiquetas_honestas(self):
        # Instrucciones de lectura, paso 7: lo no comprobado se dice.
        p = {"tienda": "x.es", "nombre": "Sofá Kivik", "precio": 99.0, "precio_antes": 199.0, "ch": "no",
             "url": "https://x.es/sofa", "estado": "bajada"}
        texto = v.mensaje_candidato(3, p)
        for frase in ("Mercado: sin comparar con otras tiendas", "Histórico: sin verificar",
                      "duplicado sin revisar", "Sin verificar: envío, stock y tallas"):
            self.assertIn(frase, texto)
        p.update({"_min_previo": 129.0, "_seguido_dias": 21, "idealo": 140.0})
        texto = v.mensaje_candidato(3, p)
        self.assertIn("Histórico propio: mínimo 129 € en 21 días", texto)
        self.assertIn("Mercado: 140 € (otras tiendas)", texto)
        self.assertNotIn("mínimo histórico", texto.lower())

    def test_ficha_con_fotos_referencia_y_tallas(self):
        html = ('<html><head><meta property="og:image" content="https://x.es/a.jpg"><title>Nike Pegasus 41</title></head>'
                '<h1>Nike Pegasus 41</h1><script type="application/ld+json">{"@type":"ProductGroup","name":"Nike Pegasus 41",'
                '"sku":"FD2722-002","image":["https://x.es/a.jpg","https://x.es/b.jpg","https://x.es/c.jpg"],'
                '"hasVariant":[{"@type":"Product","name":"Nike Pegasus 41","size":"42","offers":{"price":"89.99",'
                '"availability":"https://schema.org/InStock"}},{"@type":"Product","name":"Nike Pegasus 41","size":"43",'
                '"offers":{"price":"89.99","availability":"https://schema.org/OutOfStock"}}]}</script></html>')
        f = v.analizar_ficha(html, "https://x.es/nike")
        self.assertEqual(f["fotos"], ["https://x.es/a.jpg", "https://x.es/b.jpg"])
        self.assertEqual(f["referencia"], "FD2722-002")
        self.assertEqual(f["variantes"], ["42: hay stock", "43: agotada"])
        # Sin datos: el desplegable de tallas (sin stock declarado: "seleccionable").
        dom = ('<select name="talla"><option value="">Elige talla</option><option value="1">S</option>'
               '<option value="2" disabled>M - Agotado</option></select>')
        self.assertEqual(v.variantes_de([], dom), ["S: seleccionable", "M: agotada"])

    def test_confirmar_en_ficha(self):
        # 16/11/2026: Hermes descartaba al verificar lo que el listado leía mal. Ahora lo mira el código.
        orig = v.leer_ficha
        p = {"nombre": "Mochila adidas Backpack Mini", "precio": 36.0, "url": "https://spartoo.es/p/1", "tienda": "spartoo.es"}
        try:
            v.leer_ficha = lambda url, nav=None: {"titulo": "Mochila adidas Backpack Mini", "precio_ahora": 73.51}
            ok, motivo = v.confirmar_en_ficha(p)
            self.assertIs(ok, False, "36 € en el listado y 73,51 € en la ficha")
            self.assertIn("73.51", motivo)
            v.leer_ficha = lambda url, nav=None: {"titulo": "Rains Bolso bandolera impermeable", "precio_ahora": 36.0}
            self.assertIs(v.confirmar_en_ficha(p)[0], False, "la ficha es de otro producto")
            v.leer_ficha = lambda url, nav=None: {"titulo": "Mochila adidas Backpack Mini", "precio_ahora": 36.5}
            self.assertIs(v.confirmar_en_ficha(p)[0], True, "dentro de la tolerancia del 2 %")
            v.leer_ficha = lambda url, nav=None: {"titulo": "Mochila adidas Backpack Mini", "precio_ahora": 29.99}
            self.assertIs(v.confirmar_en_ficha(p)[0], True, "más barato en la ficha no es motivo de descarte")
            v.leer_ficha = lambda url, nav=None: {"antibot": True}
            self.assertIsNone(v.confirmar_en_ficha(p)[0], "sin poder leer, ni confirma ni desmiente")
            v.leer_ficha = lambda url, nav=None: {"titulo": "Mochila adidas Backpack Mini", "precio_ahora": 36.0, "stock": "no"}
            self.assertIs(v.confirmar_en_ficha(p)[0], False, "agotado en la ficha")
            v.leer_ficha = lambda url, nav=None: {"titulo": "Mochila adidas Backpack Mini", "precio_ahora": 36.0,
                                                  "stock": "posiblemente agotado"}
            self.assertIs(v.confirmar_en_ficha(p)[0], True, "'posiblemente' sale de un texto suelto: no descarta")
            v.leer_ficha = lambda url, nav=None: {"titulo": "Mochila adidas Backpack Mini"}
            self.assertIsNone(v.confirmar_en_ficha(p)[0], "ficha sin precio")

            def rota(url, nav=None):
                raise OSError("HTTP 403")
            v.leer_ficha = rota
            self.assertIsNone(v.confirmar_en_ficha(p)[0])
        finally:
            v.leer_ficha = orig
        # Solo por descarga: ni tiendas de navegador ni las de antibot conocido (no insistir contra su muro).
        cfg_real = json.loads((CONF / "tiendas.json").read_text(encoding="utf-8"))
        self.assertTrue(v.ficha_se_intenta(cfg_real, "spartoo.es"))
        self.assertTrue(v.ficha_se_intenta(cfg_real, "mediamarkt.es"))
        self.assertFalse(v.ficha_se_intenta(cfg_real, "amazon.es"), "se lee con navegador")
        self.assertFalse(v.ficha_se_intenta(cfg_real, "pccomponentes.com"), "antibot: ficha_no_leer")
        # Idealo vuelve a intentarse (17/11/2026): ya no está en robots_siempre; el 403 lo gestiona la pausa de 3 h.
        self.assertNotIn("idealo.es", cfg_real["filtros_codigo"]["robots_siempre"])
        self.assertEqual(cfg_real["filtros_codigo"]["robots_siempre"], [], "17/11/2026: Zeta lo vacía")
        real = json.loads((CONF / "tiendas.json").read_text(encoding="utf-8"))["filtros_codigo"]
        self.assertIs(real["confirmar_ficha"], True)
        # Una sola vara con la habilidad: SUBIENDO empieza en el 20 % real (Modo Clásico, 16/11/2026).
        self.assertEqual(real["descuento_real_min"], 0.2)

    def test_validar_ficha(self):
        # Manual de Instinct: 200 con página genérica, shell de JS o muro antibot = "sin validar".
        c = {"n": "JBL Charge 5 Altavoz Bluetooth Negro", "p": 99.0, "e": "6925281900002"}
        buena = ('<html><head><title>JBL Charge 5 | Tienda</title></head><body><h1>JBL Charge 5 Altavoz Bluetooth</h1>'
                 '<script type="application/ld+json">{"@type":"Product","name":"JBL Charge 5","gtin13":"6925281900002",'
                 '"offers":{"price":"99.00","availability":"https://schema.org/InStock"}}</script>' + "x " * 900 + "</body></html>")
        f = v.analizar_ficha(buena, "https://x.es/jbl")
        self.assertEqual(v.validar_ficha(f, c), (True, "ficha real con título y precio"))
        muro = '<html><title>Just a moment...</title><script src="https://ct.captcha-delivery.com/c.js"></script></html>'
        self.assertFalse(v.validar_ficha(v.analizar_ficha(muro, "https://x.es/jbl"), c)[0])
        generica = '<html><title>Ofertas de la semana</title><h1>Ofertas</h1>' + "y " * 900 + "</html>"
        self.assertFalse(v.validar_ficha(v.analizar_ficha(generica, "https://x.es/"), c)[0], "sin precio del producto")
        otra = buena.replace("6925281900002", "6925281900099")
        ok, motivo = v.validar_ficha(v.analizar_ficha(otra, "https://x.es/jbl"), c)
        self.assertFalse(ok)
        self.assertIn("EAN distinto", motivo)
        enlaces = v.busquedas_chollometro({"n": "Sony WH-1000XM5 Negro", "e": "4548736132610"})
        self.assertEqual(set(enlaces), {"referencia", "modelo", "nombre"})
        self.assertTrue(enlaces["modelo"].endswith("q=WH-1000XM5"))

    def test_tallas_de_nino_no_son_las_de_adulto(self):
        self.assertFalse(v.mismo_modelo("Nike Air Max 90 - Bebés Zapatillas", "Nike Air Max 90"))
        self.assertFalse(v.mismo_modelo("Nike P-6000 - Pre escolar Zapatillas", "Nike P-6000"))
        self.assertTrue(v.mismo_modelo("Nike Air Max 90 Zapatillas", "Nike Air Max 90"))

    def test_jardin_y_motor_fuera(self):
        cfg = v.cargar_config()
        self.assertTrue(v.excluido({"nombre": "Manguera extensible de jardín 30 m"}, "amazon.es", cfg))
        self.assertTrue(v.excluido({"nombre": "Casco moto integral"}, "amazon.es", cfg))
        self.assertTrue(v.excluido({"nombre": "Chaqueta"}, "motocard.com", cfg))
        self.assertFalse(v.excluido({"nombre": "Motorola Moto G85 5G 256GB"}, "amazon.es", cfg), "Motorola no es motor")
        self.assertFalse(v.excluido({"nombre": "Taladro percutor Einhell 18V"}, "amazon.es", cfg), "bricolaje sí")

    def test_solo_tiendas_con_demanda(self):
        # 117 tiendas sin ningún post caliente en 3,5 meses daban casi todos los candidatos malos.
        con = v.bd()
        hace = lambda d: int(time.time() - d * 86400)
        filas = [(i, "x", 10.0, "MediaMarkt", "www.mediamarkt.es", 500.0, hace(i % 90), 1, "") for i in range(600)]
        con.executemany("INSERT INTO cm (id, titulo, precio, tienda, host, temp, publicado, caducado, visto) VALUES (?,?,?,?,?,?,?,?,?)", filas)
        con.commit()
        cfg = {"filtros_codigo": {"solo_tiendas_con_demanda": True, "siempre_leer": ["bricoking.es"]}}
        tiendas = ["mediamarkt.es", "pccomponentes.com", "bricoking.es", "prenatal.es"]
        self.assertEqual(v.filtrar_por_demanda(con, cfg, tiendas), ["mediamarkt.es", "bricoking.es"])
        cfg["filtros_codigo"]["solo_tiendas_con_demanda"] = False
        self.assertEqual(v.filtrar_por_demanda(con, cfg, tiendas), tiendas)
        con.execute("DELETE FROM cm WHERE id >= 100")
        cfg["filtros_codigo"]["solo_tiendas_con_demanda"] = True
        self.assertEqual(v.filtrar_por_demanda(con, cfg, tiendas), tiendas, "pocos datos: no se filtra")
        con.close()
        self.assertTrue(v.con_demanda("gog.com/es", {"gog.com": 3}))
        self.assertTrue(v.con_demanda("mi.com", {"mi.com": 1}))
        self.assertFalse(v.con_demanda("mi.com", {"xiaomi.com": 5}))

    def test_tiendas_del_ranking_de_chollometro(self):
        # 16/11/2026: las tiendas del ranking de Chollometro (docs/SPEC.md) se leen siempre. Una tienda de
        # `siempre_leer` que no esté en ninguna lista, o que esté apartada, no se leería nunca sin avisar.
        cfg = json.loads((CONF / "tiendas.json").read_text(encoding="utf-8"))
        cats = cfg["categorias"]
        leidas = {t for c in cats.values() for k in ("tiendas", "navegador") for t in c.get(k, [])}
        apartadas = {t for c in cats.values() for t in c.get("apartadas", {})}
        siempre = cfg["filtros_codigo"]["siempre_leer"]
        self.assertEqual(len(siempre), len(set(siempre)), "sin repetidas")
        for t in siempre:
            self.assertIn(t, leidas, t)
            self.assertNotIn(t, apartadas, t)
        for t in ("runningemotion.com", "amazon.es", "mediamarkt.es"):
            self.assertIn(t, siempre)
        # Repaso del 18/11/2026 con Zeta: de 183 tiendas a 63. Se apartan las que no se pueden leer (antibot, Worten
        # sin venta online) y las que no tienen demanda; quedan en "apartadas" con su motivo.
        self.assertEqual(len(leidas), 63)
        for t in ("fnac.es", "adidas.es", "worten.es", "www.aeg.com.es"):
            self.assertIn(t, apartadas)
            self.assertNotIn(t, siempre)
        # Primera ronda real (16/11/2026): aeg.com.es sin www no responde; NBA Store da 403 por descarga;
        # y el radar (PrecioReal no es una tienda de Chollometro) se quedaba fuera por "sin demanda".
        self.assertEqual(v.url_tienda("www.aeg.com.es"), "https://www.aeg.com.es/")
        # 17/11/2026: NBA Store dio 0 productos en todas sus lecturas con navegador: apartada.
        self.assertIn("nbastore.eu", cats["deporte"]["apartadas"])
        self.assertIn("precioreal.com", siempre)
        # Portátil de Zeta (17/11/2026): 16 GB de RAM, i5-8265U de 4 núcleos, dedicado solo al vigilante.
        self.assertEqual((cfg["navegador"]["pestanas"], cfg["navegador"]["paginas"]), (4, 3))
        # MediaMarkt: 16 % de lo que triunfa y sus campañas dan 13 productos por descarga: más catálogo.
        self.assertEqual(cfg["catalogo_por_tienda"]["mediamarkt.es"]["por_ronda"], 4)
        self.assertEqual(cfg["catalogo_por_tienda"]["mediamarkt.es"]["cada_min"], 10)
        self.assertEqual(leidas & apartadas, set(), "una tienda apartada no puede estar en una lista de lectura")
        # Ventas privadas (16/11/2026, tras el `login` de Zeta): se leen con navegador, con su sesión.
        self.assertIn("privalia.com", cats["moda"]["navegador"])
        # 17/11/2026: Zalando Privé y Showroomprivé, 0 productos en todas sus lecturas (la portada solo enseña
        # campañas): apartadas hasta leer las campañas de dentro. Sus sesiones y logins se conservan.
        for t in ("zalando-prive.es", "showroomprive.es"):
            self.assertIn(t, cats["moda"]["apartadas"])
        self.assertIn("privatesportshop.es", cats["deporte"]["navegador"])
        for t in ("zalando-prive.es", "showroomprive.es"):
            self.assertIn(t, cfg["requiere_login"])
            self.assertIn(t, cfg["logins"])
        # 18/11/2026: Privalia se lee por campañas; la portada da 0 productos a propósito (no es sesión caducada).
        self.assertNotIn("privalia.com", cfg["requiere_login"])
        self.assertIn("privalia.com", cfg["logins"])
        self.assertIn("privalia.com", cfg["catalogo_por_tienda"])
        # 18/11/2026: Private Sport Shop enseña productos y precios sin sesión con el Chrome normal.
        self.assertNotIn("privatesportshop.es", cfg["requiere_login"])
        self.assertIn("privatesportshop.es", cfg["navegador_normal"]["tiendas"])
        for t in ("privalia.com", "privatesportshop.es"):
            self.assertIn(t, siempre)
        self.assertIn("veepee.es", cats["moda"]["apartadas"], "Veepee sigue apartada: 11 % de éxito")
        # La lista manda sobre el filtro de demanda aunque la copia de Chollometro no las cuente.
        con = v.bd()
        hace = lambda d: int(time.time() - d * 86400)
        con.executemany("INSERT INTO cm (id, titulo, precio, tienda, host, temp, publicado, caducado, visto) VALUES (?,?,?,?,?,?,?,?,?)",
                        [(i, "x", 10.0, "MediaMarkt", "www.mediamarkt.es", 500.0, hace(i % 90), 1, "") for i in range(600)])
        con.commit()
        self.assertEqual(v.filtrar_por_demanda(con, cfg, ["runningemotion.com", "footlocker.es", "jdsports.es"]),
                         ["runningemotion.com", "footlocker.es"])
        con.close()

    def test_demanda_probada(self):
        # Sin comparador ni histórico, pero ya fue chollo caliente (850°) a 36 €: a 34,99 € sí se envía.
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"].update({"exigir_precio_mercado": True})
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        self.preparar()
        self.red.idealo = {}
        self.red.chollometro = [hilo("JBL Charge 5", "Amazon", 99.0, temp=850, dias=90, caducado=True)]
        self.ronda()
        self.red.tiendas["mediamarkt"] += [{"n": "JBL Charge 5", "m": "JBL", "p": 95.0, "a": 179.0},
                                           {"n": "JBL Flip 6", "m": "JBL", "p": 79.0, "a": 149.0}]
        c = self.ronda()
        self.assertEqual([x["n"] for x in c], ["JBL Charge 5"])
        self.assertIn("850°", c[0]["hot"])

    def test_ya_publicado_igual_o_mas_barato(self):
        # Caso real: Endzone en GOG a 1,99 € con Chollometro ya a 1,06 €: Hermes siempre lo descartaba.
        # Guía del 18/11/2026: otra tienda no es duplicado (antes se descartaba el Endzone de GOG a 1,99 €).
        self.assertFalse(v.ya_publicado_igual_o_mas_barato("otra:1.06", 1.99))
        self.assertTrue(v.ya_publicado_igual_o_mas_barato("misma:2", 1.99))
        self.assertFalse(v.ya_publicado_igual_o_mas_barato("otra:6", 0.99), "ahora más barato: sí")
        self.assertFalse(v.ya_publicado_igual_o_mas_barato("no", 5))
        # Misma tienda: regla de Chollometro, más de un 5 % y más de 1 € de bajada.
        self.assertTrue(v.ya_publicado_igual_o_mas_barato("misma:56.99", 54.90), "DualSense: baja un 3,7 %")
        self.assertFalse(v.ya_publicado_igual_o_mas_barato("misma:56.99", 49.90), "baja un 12 % y 7 €")
        self.assertTrue(v.ya_publicado_igual_o_mas_barato("misma:10", 9.20), "baja un 8 % pero menos de 1 €")
        self.assertFalse(v.ya_publicado_igual_o_mas_barato("otra:56.99", 54.90), "otra tienda: no es duplicado")
        self.assertFalse(v.ya_publicado_igual_o_mas_barato("?", 5))
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 34.99
        self.red.chollometro = [hilo("Xiaomi Redmi Buds 6 Pro", "Amazon", 33.0)]
        c = self.ronda()  # guía del 18/11/2026: publicado en otra tienda no es duplicado
        self.assertEqual([x["ch"] for x in c], ["otra:33"])

    def test_ahorro_minimo_en_euros(self):
        # Caso real: cuchillo Arcos a 10,99 € con el mercado a 11,96 €: 1 € de ahorro no es chollo.
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"].update({"ahorro_min_eur": 5, "min_bajo_mercado": 0.05})
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"] += [
            {"n": "JBL Go 4", "m": "JBL", "e": "6925281900001", "p": 12.0, "a": 29.0},       # mercado 15,50: 3,50 €
            {"n": "JBL Flip 6", "m": "JBL", "e": "6925281993145", "p": 79.0, "a": 139.0}]    # mercado 99: 20 €
        self.red.idealo.update({"6925281900001": "15,50", "6925281993145": "99,00"})
        self.assertEqual([x["n"] for x in self.ronda()], ["JBL Flip 6"])
        self.assertTrue(v.ahorro_suficiente({"precio": 0, "gratis": True}, None, {}, {"ahorro_min_eur": 5}))
        self.assertFalse(v.ahorro_suficiente({"precio": 3.89, "precio_antes": 14.99}, None,
                                             {"ahorro_min_eur": 15}, {"ahorro_min_eur": 5}), "la categoría manda")

    def test_bajo_mercado_por_categoria(self):
        # Hogar: PrecioReal empareja productos genéricos; se pide un 15 % bajo el mercado, no un 5 %.
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"]["min_bajo_mercado"] = 0.05
        cfg["categorias"]["tecnologia"]["min_bajo_mercado"] = 0.15
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"].append({"n": "JBL Flip 6", "m": "JBL", "e": "6925281993145", "p": 79.0, "a": 139.0})
        self.red.idealo["6925281993145"] = "88,00"  # 10 % por debajo: no llega al 15 %
        self.assertEqual(self.ronda(), [])

    def test_precio_original_minimo(self):
        # GOG: Might and Magic 8 a 0,99 € (antes 4,95 €) no es publicable aunque sea -80 %.
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["reglas_tienda"]["mediamarkt.es"] = {"antes_min_eur": 15}
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"] += [{"n": "JBL Go Mini", "m": "JBL", "p": 2.0, "a": 9.99},
                                           {"n": "JBL Charge 5", "m": "JBL", "p": 50.0, "a": 150.0}]
        self.assertEqual([x["n"] for x in self.ronda()], ["JBL Charge 5"])

    def _envejecer(self, dias):
        """Simula que el vigilante lleva `dias` días siguiendo los precios."""
        con = sqlite3.connect(v.DB)
        hace = (datetime.now() - timedelta(days=dias)).isoformat(timespec="minutes")
        con.execute("UPDATE vistos SET desde=?", (hace,))
        con.commit()
        con.close()

    def test_historial_propio(self):
        # Sin precio de mercado, el 'antes' de la tienda no prueba nada: solo pasa lo que baja de su
        # mínimo visto tras 14 días de seguimiento.
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"].update({"exigir_precio_mercado": True, "historial_propio_dias": 14,
                                      "bajada_min_propia": 0.10})
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"].append({"n": "JBL Charge 5", "m": "JBL", "p": 50.0, "a": 150.0})
        self.assertEqual(self.ronda(), [], "nuevo con -67 % de la tienda: no basta")
        cosori = self.red.tiendas["mediamarkt"][1]
        cosori["p"] = 69.99
        self.assertEqual(self.ronda(), [], "baja de su mínimo, pero solo lleva unas horas de seguimiento")
        self._envejecer(20)
        cosori["p"] = 66.0
        self.assertEqual(self.ronda(), [], "baja, pero menos de un 10 % bajo su mínimo (69,99)")
        cosori["p"] = 55.0
        self.assertEqual([x["n"] for x in self.ronda()], ["Cosori Freidora 5,5 L"])

    def test_juegos_con_historial_propio(self):
        # Steam: "-84 % recurrente, 10 veces en 12 meses". El primer -80 % que se ve no se envía.
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["categorias"]["tecnologia"].update({"sin_idealo": True, "historial_propio_dias": 14})
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"].append({"n": "JBL Charge 5", "m": "JBL", "p": 50.0, "a": 150.0})
        self.assertEqual(self.ronda(), [])
        self._envejecer(30)
        self.red.tiendas["mediamarkt"][-1]["p"] = 40.0
        self.assertEqual([x["n"] for x in self.ronda()], ["JBL Charge 5"])

    def test_generico_no_se_compara(self):
        # Estor Decoscreen emparejado con un colchón Pikolin en PrecioReal: sin marca, código ni EAN, no se busca.
        estor = {"nombre": "ESTOR ENROLLABLE DECOSCREEN PERLA GRIS 135X190", "ean": None, "marca": None,
                 "tienda": "bricoking.es", "precio": 79.99}
        self.assertFalse(v.identificable(estor))
        self.assertEqual(v.lista_codigos("REVESTIMIENTO VINILICO 75X45CM PACK"), [])
        self.assertIsNone(v.precioreal_red(estor))
        self.assertTrue(v.identificable({**estor, "nombre": "Sony WF-C710N"}))

    def test_cupo_diario(self):
        self.preparar()
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"]["max_verificaciones_ia_por_dia"] = 2
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        self.ronda()
        nuevos = [{"n": f"JBL Charge {i}", "m": "JBL", "p": 50.0, "a": 150.0} for i in range(5)]
        self.red.tiendas["mediamarkt"] += nuevos
        self.assertEqual(len(self.ronda()), 2)
        self.red.tiendas["mediamarkt"].append({"n": "JBL Go 4", "m": "JBL", "p": 20.0, "a": 60.0})
        antes = self.red.llamadas["tienda"]
        self.assertEqual(self.ronda(), [])
        self.assertEqual(self.red.llamadas["tienda"], antes, "cupo agotado: ni descarga")

    def test_maximo_por_tienda_y_ronda(self):
        # Forumsport: 14 productos al -50 % de golpe -> como mucho 2 de la misma tienda por ronda.
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"]["max_por_tienda_y_ronda"] = 2
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        self.preparar()
        self.ronda()
        self.red.tiendas["mediamarkt"] += [{"n": f"JBL Charge {i}", "m": "JBL", "p": 50.0, "a": 150.0} for i in range(6)]
        self.red.tiendas["pccomponentes"] += [{"n": f"Sony WH-{i}", "m": "Sony", "p": 50.0, "a": 150.0} for i in range(6)]
        c = self.ronda()
        self.assertEqual(sorted(Counter(x["t"] for x in c).values()), [2, 2])

    def test_tope_por_tanda(self):
        self.preparar()
        self.ronda()
        tope = json.loads(v.CONFIG.read_text(encoding="utf-8"))["filtros_codigo"]["max_candidatos_ia_por_tanda"]
        self.red.tiendas["mediamarkt"] += [{"n": f"JBL Charge {i}", "m": "JBL", "p": 50.0, "a": 150.0} for i in range(tope + 5)]
        self.assertEqual(len(self.ronda()), tope)

    def test_tienda_caida_no_rompe(self):
        self.preparar()
        self.ronda()
        self.red.caidas.add("pccomponentes")
        self.red.tiendas["mediamarkt"][0]["p"] = 34.99
        self.assertEqual(len(self.ronda()), 1)
        self.assertIn("403", v.LOG.read_text(encoding="utf-8"))

    def test_url_nueva_de_tienda_no_inunda(self):
        self.preparar()
        self.ronda()
        v.URLS.write_text(json.dumps({"tecnologia:mediamarkt.es": "https://www.mediamarkt.es/outlet"}), encoding="utf-8")
        self.red.tiendas["mediamarkt"].append({"n": "JBL Flip 6", "m": "JBL", "p": 79.0, "a": 139.0})
        self.assertEqual(self.ronda(), [], "página nueva: primero se siembra")

    def test_fuera_de_horario(self):
        self.preparar()
        v.en_horario = lambda cfg: False
        self.assertEqual(self.ronda(), [])
        self.assertEqual(sum(self.red.llamadas.values()), 0)

    def test_dos_categorias_a_la_vez(self):
        self.preparar()
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["categorias"]["moviles"]["tiendas"] = ["mediamarkt.es", "pccomponentes.com"]
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        errores = []

        def lanzar(cat):
            try:
                for _ in range(5):
                    v.ronda(cat)
            except Exception as e:  # pragma: no cover
                errores.append(e)

        hilos = [threading.Thread(target=lanzar, args=(c,)) for c in ("tecnologia", "moviles")]
        with redirect_stdout(io.StringIO()):  # una sola redirección para los dos hilos
            for h in hilos:
                h.start()
            for h in hilos:
                h.join()
        self.assertEqual(errores, [])
        n = sqlite3.connect(v.DB).execute("SELECT COUNT(*) FROM vistos").fetchone()[0]
        self.assertEqual(n, 3, "sin duplicados en la base de datos")


class Robots(unittest.TestCase):
    TEXTO = """User-agent: *
Allow: /
Disallow: /search
Disallow: /*?sortBy=*
Disallow: /api/1.0/
Allow: /api/1.0/publico$

User-agent: ChatGPT-User
Disallow: /
"""

    def setUp(self):
        # Configuración propia en una carpeta temporal y en modo estricto: estas pruebas no dependen de
        # lo que diga el tiendas.json real ni lo tocan.
        self.cfg_tmp = Path(tempfile.mkdtemp())
        self._config = v.CONFIG
        v.CONFIG = self.cfg_tmp / "tiendas.json"
        v.CONFIG.write_text(json.dumps({"filtros_codigo": {"respetar_robots": True}}), encoding="utf-8")
        v._ROBOTS_CFG[:] = [None, None]

    def tearDown(self):
        v.CONFIG = self._config
        v._ROBOTS_CFG[:] = [None, None]
        shutil.rmtree(self.cfg_tmp, ignore_errors=True)

    def test_reglas(self):
        r = v.reglas_robots(self.TEXTO)
        self.assertTrue(v.robots_permite(r, "/nuevas"))
        self.assertFalse(v.robots_permite(r, "/search?q=killing+floor"), "caso real: Chollometro prohíbe /search")
        self.assertFalse(v.robots_permite(r, "/ofertas?sortBy=temp&page=2"))
        self.assertTrue(v.robots_permite(r, "/ofertas?page=2"))
        self.assertFalse(v.robots_permite(r, "/api/1.0/games?id=1"))
        self.assertTrue(v.robots_permite(r, "/api/1.0/publico"), "la regla más larga gana")
        self.assertEqual(v.reglas_robots("User-agent: *\nDisallow:\n"), [], "Disallow vacío = todo permitido")

    def test_robots_desactivado_por_configuracion(self):
        # 16/11/2026, decisión de Zeta: no se mira robots.txt, salvo en los hosts de `robots_siempre`.
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"].update({"respetar_robots": False, "robots_siempre": ["chollometro.com"]})
        v.CONFIG.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        v._ROBOTS_CFG[:] = [None, None]
        tmp = Path(tempfile.mkdtemp())
        orig = v.DATOS, v._descargar
        v._ROBOTS_MEM.clear()
        pedidas = []

        def falsa(url, timeout):
            pedidas.append(url)
            if url.endswith("/robots.txt"):
                return self.TEXTO, url
            return "<html>ok</html>", url
        try:
            v.DATOS, v._descargar = tmp, falsa
            self.assertFalse(v.robots_activo("web.es"))
            self.assertTrue(v.robots_activo("www.chollometro.com"))
            self.assertEqual(v.descargar("https://web.es/search?q=x")[0], "<html>ok</html>")
            self.assertNotIn("https://web.es/robots.txt", pedidas, "ni se pide el robots.txt")
            with self.assertRaises(v.BloqueadoPorRobots):
                v.descargar("https://www.chollometro.com/search?q=x")
            # Sin la clave en la configuración se respeta: es lo prudente.
            cfg["filtros_codigo"].pop("respetar_robots")
            v.CONFIG.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
            v._ROBOTS_CFG[:] = [None, None]
            self.assertTrue(v.robots_activo("web.es"))
        finally:
            v.DATOS, v._descargar = orig
            v._ROBOTS_MEM.clear()
            v._ROBOTS_CFG[:] = [None, None]
            shutil.rmtree(tmp, ignore_errors=True)
        # El archivo real lleva la decisión: desactivado, sin excepciones desde el 17/11/2026.
        real = json.loads((CONF / "tiendas.json").read_text(encoding="utf-8"))["filtros_codigo"]
        self.assertIs(real["respetar_robots"], False)
        self.assertEqual(real["robots_siempre"], [])

    def test_descargar_respeta_robots(self):
        tmp = Path(tempfile.mkdtemp())
        orig = v.DATOS, v._descargar
        v._ROBOTS_MEM.clear()
        pedidas = []

        def falsa(url, timeout):
            pedidas.append(url)
            if url.endswith("/robots.txt"):
                if "sinrobots" in url:
                    raise v.HTTPError(url, 404, "no", {}, None)
                return self.TEXTO, url
            return "<html>ok</html>", url
        try:
            v.DATOS, v._descargar = tmp, falsa
            self.assertEqual(v.descargar("https://web.es/nuevas")[0], "<html>ok</html>")
            with self.assertRaises(v.BloqueadoPorRobots):
                v.descargar("https://web.es/search?q=x")
            self.assertNotIn("https://web.es/search?q=x", pedidas, "ni siquiera se pide")
            self.assertEqual(pedidas.count("https://web.es/robots.txt"), 1, "robots.txt en caché")
            v._ROBOTS_MEM.clear()
            v.descargar("https://web.es/otra")
            self.assertEqual(pedidas.count("https://web.es/robots.txt"), 1, "caché en disco")
            self.assertTrue(v.permitido("https://sinrobots.es/lo-que-sea"))
        finally:
            v.DATOS, v._descargar = orig
            v._ROBOTS_MEM.clear()
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------ red real

class Descarga(unittest.TestCase):
    """Prueba descargar() de verdad contra un servidor local (gzip, charset y errores)."""

    def setUp(self):
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/cookie":
                    if "ok=1" not in (self.headers.get("Cookie") or ""):
                        self.send_response(307)
                        self.send_header("Set-Cookie", "ok=1; Path=/")
                        self.send_header("Location", "/cookie")
                        self.end_headers()
                        return
                if self.path == "/503-una-vez":
                    H.veces = getattr(H, "veces", 0) + 1
                    if H.veces == 1:
                        self.send_response(503)
                        self.end_headers()
                        return
                if self.path == "/latin":
                    cuerpo = '<meta charset="iso-8859-1"><p>Cafetera económica 59,99</p>'.encode("iso-8859-1")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(cuerpo)
                    return
                if self.path == "/403":
                    self.send_response(403)
                    self.end_headers()
                    return
                cuerpo = gzip.compress("<p>Freidora 59,99 € ñ</p>".encode("windows-1252"))
                self.send_response(200)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Type", "text/html; charset=windows-1252")
                self.end_headers()
                self.wfile.write(cuerpo)

            def log_message(self, *a):
                pass

        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown()

    def test_gzip_y_charset(self):
        html, _ = v.descargar(self.base + "/")
        self.assertIn("59,99 € ñ", html)

    def test_redireccion_con_cookies(self):
        html, _ = v.descargar(self.base + "/cookie")
        self.assertIn("59,99", html)

    def test_reintenta_un_503(self):
        html, _ = v.descargar(self.base + "/503-una-vez")
        self.assertIn("59,99", html)

    def test_charset_desde_meta(self):
        html, _ = v.descargar(self.base + "/latin")
        self.assertIn("económica", html)

    def test_error_http(self):
        with self.assertRaises(Exception):
            v.descargar(self.base + "/403")


# ------------------------------------------------------------- paginación

class Grupos(Base):
    def test_rapidas_no_abre_navegador_y_navegador_no_descarga(self):
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["categorias"]["tecnologia"]["navegador"] = ["coolmod.com"]
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        self.red.tiendas = {"mediamarkt": [{"n": "JBL Flip 6", "p": 79.0}], "pccomponentes": []}
        nav = []
        orig = v.leer_con_pestanas
        v.leer_con_pestanas = lambda cfg, trabajos: nav.extend(t for t, _, _ in trabajos) or [
            (t, u, [], None) for t, u, _ in trabajos]
        try:
            with redirect_stdout(io.StringIO()):
                v.ronda_grupo("rapidas")
            self.assertEqual(nav, [], "rápidas no abre Chrome")
            self.assertTrue(any("mediamarkt" in u for u in self.red.pedidas))
            self.red.pedidas.clear()
            with redirect_stdout(io.StringIO()):
                v.ronda_grupo("navegador")
            self.assertEqual(nav, ["coolmod.com"], "navegador solo lee las tiendas de navegador")
            self.assertEqual(self.red.pedidas, [], "y no descarga las demás")
        finally:
            v.leer_con_pestanas = orig


class Descubrir(Base):
    def test_no_elige_una_ficha_suelta(self):
        # Euronics, Tien21, PCBox...: la ficha de un móvil (1 producto, rebajado) ganaba al listado.
        listado = tienda_html([{"n": f"JBL Charge {i}", "p": 99.0} for i in range(4)])
        ficha = tienda_html([{"n": "Samsung Galaxy A17", "p": 149.0, "a": 199.0}])
        portada = ('<a href="/ofertas">Ofertas</a><a href="/samsung-galaxy-a17-black.html">Oferta Galaxy</a>'
                   '<a href="/promociones">Promociones</a>')
        paginas = {"/ofertas": listado, "/samsung-galaxy-a17-black.html": ficha, "/promociones": "<p>nada</p>"}

        def red(url, timeout=20, reintentos=1):
            ruta = v.urlparse(url).path
            return paginas.get(ruta, portada), url
        v.descargar = red
        with redirect_stdout(io.StringIO()):
            v.descubrir("tecnologia")
        urls = json.loads(v.URLS.read_text(encoding="utf-8"))
        self.assertEqual(urls["mediamarkt.es"], "https://www.mediamarkt.es/ofertas")

    def test_no_elige_la_pagina_de_ninos(self):
        # Spartoo, Fútbol Emotion, Women'secret (16/11/2026): la página de ofertas de niños tiene más
        # rebajas y ganaba a la general. Gana la general aunque tenga menos productos.
        ninos = tienda_html([{"n": f"Zapatilla infantil {i}", "p": 19.0, "a": 39.0} for i in range(30)])
        general = tienda_html([{"n": f"JBL Charge {i}", "p": 99.0, "a": 129.0} for i in range(5)])
        portada = '<a href="/ofertas-ninos.php">Ofertas niños</a><a href="/ofertas">Ofertas</a>'
        paginas = {"/ofertas-ninos.php": ninos, "/ofertas": general}
        v.descargar = lambda url, timeout=20, reintentos=1: (paginas.get(v.urlparse(url).path, portada), url)
        with redirect_stdout(io.StringIO()):
            v.descubrir("tecnologia")
        urls = json.loads(v.URLS.read_text(encoding="utf-8"))
        self.assertEqual(urls["mediamarkt.es"], "https://www.mediamarkt.es/ofertas")
        for ruta in ("/Ofertas-st-11977-0-ninos.php", "/es/categoria/ninos/oferta", "/promociones/favoritos/teen",
                     "/rebajas/bebe", "/outlet/kids"):
            self.assertTrue(v.RE_SEGMENTO.search(ruta), ruta)
        for ruta in ("/ofertas", "/outlet/hombre", "/promociones/teenage-engineering", "/ofertas/kidsroom"):
            self.assertFalse(v.RE_SEGMENTO.search(ruta), ruta)

    def test_descubrir_guarda_tambien_las_campanas(self):
        # Zeta (17/11/2026): una sola página por tienda se queda corta. Principal permanente + campañas activas.
        bf = tienda_html([{"n": f"Samsung TV {i}", "p": 399.0, "a": 599.0} for i in range(20)])
        perm = tienda_html([{"n": f"JBL Charge {i}", "p": 99.0, "a": 129.0} for i in range(4)])
        outlet = tienda_html([{"n": f"Sony WH {i}", "p": 199.0, "a": 299.0} for i in range(6)])
        sin_rebajas = tienda_html([{"n": f"Cable {i}", "p": 9.0} for i in range(9)])
        portada = ('<a href="/promocion-black-friday.html">Black Friday</a><a href="/ofertas">Ofertas</a>'
                   '<a href="/outlet">Outlet</a><a href="/promociones">Promociones</a>')
        paginas = {"/promocion-black-friday.html": bf, "/ofertas": perm, "/outlet": outlet, "/promociones": sin_rebajas}
        v.descargar = lambda url, timeout=20, reintentos=1: (paginas.get(v.urlparse(url).path, portada), url)
        with redirect_stdout(io.StringIO()):
            v.descubrir("tecnologia")
        urls = json.loads(v.URLS.read_text(encoding="utf-8"))
        self.assertEqual(urls["mediamarkt.es"], "https://www.mediamarkt.es/outlet", "permanente con más rebajas")
        self.assertEqual(urls["mediamarkt.es+"], ["https://www.mediamarkt.es/promocion-black-friday.html",
                                                  "https://www.mediamarkt.es/ofertas"], "sin la que no tiene rebajas")
        # Refresco automático: la campaña ha terminado. La principal no cambia; las adicionales, sí.
        paginas["/promocion-black-friday.html"] = "<p>terminada</p>"
        con = v.bd()
        cfg = {"filtros_codigo": {"ofertas_refresco_dias": 7}}
        v._REFRESCO_HECHO[0] = False
        try:
            self.assertTrue(v.refrescar_extras(con, cfg, "tecnologia", "descarga", {"mediamarkt.es"}))
            urls = json.loads(v.URLS.read_text(encoding="utf-8"))
            self.assertEqual(urls["mediamarkt.es"], "https://www.mediamarkt.es/outlet")
            self.assertEqual(urls["mediamarkt.es+"], ["https://www.mediamarkt.es/ofertas"])
            self.assertFalse(v.refrescar_extras(con, cfg, "moviles", "descarga", {"mediamarkt.es"}), "una por ronda")
            v._REFRESCO_HECHO[0] = False
            self.assertFalse(v.refrescar_extras(con, cfg, "tecnologia", "descarga", {"mediamarkt.es"}), "una vez por semana")
            cfg["filtros_codigo"]["ofertas_refresco_dias"] = 0
            self.assertFalse(v.refrescar_extras(con, cfg, "moviles", "descarga", {"mediamarkt.es"}), "apagado")
        finally:
            v._REFRESCO_HECHO[0] = False
            con.close()

    def test_la_pagina_general_gana_a_la_de_una_marca(self):
        # Zalando (17/11/2026): /rebajas/nike/ (115 productos) ganaba a /rebajas/ (114) por un producto.
        general = tienda_html([{"n": f"JBL Charge {i}", "p": 99.0, "a": 129.0} for i in range(20)])
        marca = tienda_html([{"n": f"Nike Air {i}", "p": 59.0, "a": 99.0} for i in range(21)])
        portada = '<a href="/rebajas/nike/">Nike rebajas</a><a href="/rebajas/">Rebajas</a>'
        paginas = {"/rebajas/": general, "/rebajas/nike/": marca}
        v.descargar = lambda url, timeout=20, reintentos=1: (paginas.get(v.urlparse(url).path, portada), url)
        with redirect_stdout(io.StringIO()):
            v.descubrir("tecnologia")
        urls = json.loads(v.URLS.read_text(encoding="utf-8"))
        self.assertEqual(urls["mediamarkt.es"], "https://www.mediamarkt.es/rebajas/")
        self.assertEqual(urls["mediamarkt.es+"], ["https://www.mediamarkt.es/rebajas/nike/"], "la de marca queda de adicional")

    def test_prueba_muestra_cada_tienda(self):
        self.red.tiendas = {"mediamarkt": [{"n": "JBL Flip 6", "m": "JBL", "p": 79.0, "a": 129.0}]}
        self.red.caidas.add("pccomponentes")
        buf = io.StringIO()
        with redirect_stdout(buf):
            v.prueba("tecnologia")
        salida = buf.getvalue()
        self.assertRegex(salida, r"mediamarkt\.es\s+1 prod \|\s+1 con precio anterior \|\s+1 de marca")
        self.assertRegex(salida, r"pccomponentes\.com\s+ERROR")

    def test_prefiere_pagina_permanente_a_campana(self):
        # El Black Friday tiene más rebajas, pero caduca: gana la de ofertas permanente.
        bf = tienda_html([{"n": f"Samsung TV {i}", "p": 399.0, "a": 599.0} for i in range(20)])
        perm = tienda_html([{"n": f"JBL Charge {i}", "p": 99.0, "a": 129.0} for i in range(4)])
        portada = ('<a href="/promocion-black-friday.html">Black Friday</a><a href="/event/oferta-2026/">x</a>'
                   '<a href="/ofertas">Ofertas</a>')
        paginas = {"/promocion-black-friday.html": bf, "/event/oferta-2026/": bf, "/ofertas": perm}
        v.descargar = lambda url, timeout=20, reintentos=1: (paginas.get(v.urlparse(url).path, portada), url)
        with redirect_stdout(io.StringIO()):
            v.descubrir("tecnologia")
        urls = json.loads(v.URLS.read_text(encoding="utf-8"))
        self.assertEqual(urls["mediamarkt.es"], "https://www.mediamarkt.es/ofertas")
        self.assertTrue(v.RE_TEMPORAL.search("/es/event/black-friday-2026/"))
        self.assertFalse(v.RE_TEMPORAL.search("/es/outlet/ofertas-del-mes"))


class Paginacion(unittest.TestCase):
    def test_siguiente_pagina(self):
        sp = v.siguiente_pagina
        self.assertEqual(sp('<link rel="next" href="/ofertas?page=2">', "https://t.es/ofertas"),
                         "https://t.es/ofertas?page=2")
        self.assertEqual(sp('<a class="x" rel="next" href="?p=3&amp;orden=desc">', "https://t.es/o?p=2"),
                         "https://t.es/o?p=3&orden=desc")
        self.assertEqual(sp('<a href="/o/pagina/2"> Siguiente </a>', "https://t.es/o"), "https://t.es/o/pagina/2")
        self.assertEqual(sp('<a href="#">»</a>', "https://t.es/o?page=4"), "https://t.es/o?page=5")
        self.assertEqual(sp("<p>nada</p>", "https://t.es/o/page/7/"), "https://t.es/o/page/8/")
        self.assertIsNone(sp("<p>sin paginación</p>", "https://t.es/ofertas"))

    def test_paginas_por_tienda(self):
        cfg = {"paginas": {"defecto": 3, "mi.com/es": 1}}
        self.assertEqual(v.paginas_de(cfg, "mi.com/es"), 1)
        self.assertEqual(v.paginas_de(cfg, "mediamarkt.es"), 3)
        self.assertEqual(v.paginas_de({}, "x"), 3)

    def test_lee_varias_paginas_y_para_a_tiempo(self):
        paginas = {
            "https://www.t.es/ofertas": tienda_html([{"n": "JBL Flip 6", "p": 79, "a": 129}]) + '<link rel="next" href="/ofertas?page=2">',
            "https://www.t.es/ofertas?page=2": tienda_html([{"n": "Sony WH-CH520", "p": 35, "a": 59}]) + '<link rel="next" href="/ofertas?page=3">',
            # la 3 repite la 2: debe parar sin pedir la 4
            "https://www.t.es/ofertas?page=3": tienda_html([{"n": "Sony WH-CH520", "p": 35, "a": 59}]) + '<link rel="next" href="/ofertas?page=4">',
        }
        pedidas = []

        def falso(url, timeout=20):
            pedidas.append(url)
            return paginas[url], url
        orig, v.descargar = v.descargar, falso
        sleep, v.time.sleep = v.time.sleep, lambda s: None
        try:
            _, _, ps, err = v.leer_tienda("t.es", {"t.es": "https://www.t.es/ofertas"}, None, None, 10)
            self.assertIsNone(err)
            self.assertEqual([p["nombre"] for p in ps], ["JBL Flip 6", "Sony WH-CH520"])
            self.assertEqual(len(pedidas), 3)
            pedidas.clear()
            _, _, ps, _ = v.leer_tienda("t.es", {"t.es": "https://www.t.es/ofertas"}, None, None, 1)
            self.assertEqual((len(ps), len(pedidas)), (1, 1), "respeta el máximo de páginas")
        finally:
            v.descargar, v.time.sleep = orig, sleep

    def test_page_2_sin_enlace(self):
        # MediaMarkt / Electrocosto: botón 'Ver más' sin enlace, pero ?page=2 funciona.
        # Euronics: ?page=2 devuelve lo mismo -> una sola petición de más y para.
        paginas = {
            "https://www.mm.es/ofertas": tienda_html([{"n": "JBL Flip 6", "p": 79, "a": 129}]),
            "https://www.mm.es/ofertas?page=2": tienda_html([{"n": "Sony WH-CH520", "p": 35, "a": 59}]),
            "https://www.mm.es/ofertas?page=3": tienda_html([{"n": "Bose QC45", "p": 199, "a": 299}]),
            "https://www.eu.es/bf?x=1": tienda_html([{"n": "LG OLED55", "p": 999, "a": 1299}]),
            "https://www.eu.es/bf?x=1&page=2": tienda_html([{"n": "LG OLED55", "p": 999, "a": 1299}]),
        }
        pedidas = []

        def falso(url, timeout=20):
            pedidas.append(url)
            return paginas[url], url
        orig, v.descargar = v.descargar, falso
        sleep, v.time.sleep = v.time.sleep, lambda s: None
        try:
            _, _, ps, err = v.leer_tienda("mm.es", {"mm.es": "https://www.mm.es/ofertas"}, None, None, 3)
            self.assertIsNone(err)
            self.assertEqual([p["nombre"] for p in ps], ["JBL Flip 6", "Sony WH-CH520", "Bose QC45"])
            pedidas.clear()
            _, _, ps, _ = v.leer_tienda("eu.es", {"eu.es": "https://www.eu.es/bf?x=1"}, None, None, 3)
            self.assertEqual((len(ps), pedidas), (1, ["https://www.eu.es/bf?x=1", "https://www.eu.es/bf?x=1&page=2"]))
        finally:
            v.descargar, v.time.sleep = orig, sleep

    def test_fallo_en_pagina_2_conserva_la_1(self):
        def falso(url, timeout=20):
            if "page=2" in url:
                raise OSError("timeout")
            return tienda_html([{"n": "JBL Flip 6", "p": 79, "a": 129}]) + '<link rel="next" href="?page=2">', url
        orig, v.descargar = v.descargar, falso
        sleep, v.time.sleep = v.time.sleep, lambda s: None
        try:
            _, _, ps, err = v.leer_tienda("t.es", {"t.es": "https://www.t.es/o"}, None, None, 3)
        finally:
            v.descargar, v.time.sleep = orig, sleep
        self.assertIsNone(err)
        self.assertEqual(len(ps), 1)


class Cerrojo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._datos, v.DATOS = v.DATOS, self.tmp

    def tearDown(self):
        v.DATOS = self._datos
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_segundo_espera_y_cerrojo_viejo_se_libera(self):
        a, b = v.Navegador(), v.Navegador()
        a._cerrojo()
        with self.assertRaises(TimeoutError):
            b._cerrojo(espera_max=0)
        a._lock.unlink()
        b._cerrojo(espera_max=0)  # libre: entra
        os.utime(b._lock, (0, 0))   # simula un proceso muerto hace horas
        c = v.Navegador()
        c._cerrojo(espera_max=0)
        self.assertTrue(c._lock.exists())


# ------------------------------------------------- modo mixto por Telegram

class ModoTelegram(Base):
    def setUp(self):
        super().setUp()
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["entrega"] = {"modo": "telegram", "chat_id": "456808276"}
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        self.enviados, self.teclados = [], []
        self._orig_tg = (v.enviar_telegram, v.token_telegram)
        v.enviar_telegram = lambda tok, chat, texto, foto=None, teclado=None, **k: (self.enviados.append((chat, texto, foto)), self.teclados.append(teclado)) and True
        v.token_telegram = lambda: "123:ABC"
        self.red.tiendas = {"mediamarkt": [{"n": "Xiaomi Redmi Buds 6 Pro Negro", "m": "Xiaomi", "e": "6941812790000",
                                            "p": 39.99, "a": 79.99}],
                            "pccomponentes": []}
        self.red.idealo = {"6941812790000": "45,00"}  # 34,99 € = 22 % menos (guía: 20 %)

    def tearDown(self):
        v.enviar_telegram, v.token_telegram = self._orig_tg
        super().tearDown()

    def test_la_ronda_no_avisa_si_la_ficha_lo_desmiente(self):
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"]["confirmar_ficha"] = True
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        orig = v.leer_ficha
        try:
            v.leer_ficha = lambda url, nav=None: {"titulo": "Xiaomi Redmi Buds 6 Pro Negro", "precio_ahora": 79.99}
            self.ronda()  # la primera ronda solo siembra
            self.red.tiendas["mediamarkt"][0]["p"] = 34.99
            self.ronda()
            self.assertEqual(self.enviados, [], "el listado decía 34,99 € y la ficha 79,99 €: no se avisa")
        finally:
            v.leer_ficha = orig

    def test_la_ronda_avisa_si_la_ficha_confirma_o_no_se_lee(self):
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"]["confirmar_ficha"] = True
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        orig = v.leer_ficha
        try:
            v.leer_ficha = lambda url, nav=None: {"antibot": True}
            self.ronda()
            self.red.tiendas["mediamarkt"][0]["p"] = 34.99
            self.ronda()
            self.assertEqual(len(self.enviados), 1, "sin poder leer la ficha, el aviso sale como antes")
        finally:
            v.leer_ficha = orig

    def test_error_de_precio_necesita_ficha_confirmada(self):
        # HP EliteBook de PCBox (16/11/2026): "posible error de precio" sin histórico ni referencia y con la
        # ficha de otro producto. Un error de precio se salta el contraste: sin ficha confirmada no sale.
        self.red.idealo = {}
        self.red.tiendas["mediamarkt"].append({"n": "JBL Flip 6", "m": "JBL", "e": "6925281993145", "p": 100.0, "a": 149.0})
        self.ronda()  # siembra los dos
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        orig = v.leer_ficha
        try:
            v.leer_ficha = lambda url, nav=None: {"antibot": True}
            cfg["filtros_codigo"]["confirmar_ficha"] = True
            v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
            self.red.tiendas["mediamarkt"][0]["p"] = 9.99  # baja a la cuarta parte: "error de precio"
            self.ronda()
            self.assertEqual(self.enviados, [], "con la ficha sin leer, el error de precio no se envía")
            cfg["filtros_codigo"]["confirmar_ficha"] = False
            v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
            self.red.tiendas["mediamarkt"][1]["p"] = 20.0
            self.ronda()
            self.assertEqual(len(self.enviados), 1, "sin la comprobación de ficha, el mismo caso sí sale")
        finally:
            v.leer_ficha = orig

    def test_diario_del_recorrido(self):
        # Zeta (16/11/2026): un log de los recorridos de las tiendas y de lo que se lee.
        self.ronda()
        texto = (v.DATOS / "recorrido.txt").read_text(encoding="utf-8")
        self.assertIn("mediamarkt.es [descarga]", texto)
        self.assertIn("1 productos, 1 con precio anterior, 0 nuevos o más baratos", texto)
        self.assertIn("primera lectura de esta página: solo memoriza", texto)
        self.red.tiendas["mediamarkt"][0]["p"] = 34.99
        self.ronda()
        texto = (v.DATOS / "recorrido.txt").read_text(encoding="utf-8")
        self.assertIn("1 nuevos o más baratos, 1 pasan los filtros de tienda", texto)
        self.assertIn("AVISO mediamarkt.es: Xiaomi Redmi Buds 6 Pro Negro a 34.99 €", texto)
        self.assertRegex(texto, r"=== tecnologia: \d+ lecturas, \d+ productos, 1 pasan filtros de tienda, "
                                r"1 con contraste real, 1 avisos, \d+ s")
        # Se apaga por configuración.
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"]["log_recorrido"] = False
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        (v.DATOS / "recorrido.txt").unlink()
        self.ronda()
        self.assertFalse((v.DATOS / "recorrido.txt").exists())

    # ---- decisión por código (Modo Clásico), 17/11/2026

    def _item(self, **k):
        base = {"num": 7, "n": "Xiaomi Redmi Buds 6 Pro Negro", "t": "mediamarkt.es", "u": "https://www.mediamarkt.es/p/1",
                "p": 30.0, "validada": True, "validacion": "ficha real con título y precio", "comprobado": "17/11 08:50",
                "ficha": {"precio_ahora": 30.0, "titulo": "Xiaomi Redmi Buds 6 Pro Negro", "stock": "sí"}}
        ficha = {**base["ficha"], **k.pop("ficha", {})}
        return {**base, **k, "ficha": ficha}

    def test_decidir_con_historico(self):
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        # #189 real: 30,00 € con habitual 39,99 € en PCComTracker es un 25 % menos. La IA lo descartó.
        d = v.decidir_item(self._item(historico={"minimo": 29.0, "habitual": 39.99, "fuente": "pccomtracker.com"}), cfg)
        self.assertEqual((d["veredicto"], d["etiqueta"]), ("pasa", "DESTACADOS"))
        self.assertIn("25 % menos", d["prueba"])
        # #188 real: el aviso decía 15,98 € y la ficha marca 21,99 €, que es su precio habitual.
        d = v.decidir_item(self._item(p=15.98, ficha={"precio_ahora": 21.99},
                                      historico={"minimo": 15.98, "habitual": 21.99, "fuente": "pccomtracker.com"}), cfg)
        self.assertEqual((d["veredicto"], d["memoria"]), ("descartado", "caducada"))
        self.assertIn("baja un 0 %", d["motivo"])
        self.assertIn("se exige un 20 %", d["motivo"])
        self.assertIn("el aviso decía 15.98 €", d["motivo"])
        # Entre el 20 % y el 25 %: SUBIENDO. Mínimo histórico y más barato que el mercado: EXCEPCIONAL.
        d = v.decidir_item(self._item(p=31.5, ficha={"precio_ahora": 31.5}, historico={"minimo": 30.0, "habitual": 39.99}), cfg)
        self.assertEqual(d["etiqueta"], "SUBIENDO")
        d = v.decidir_item(self._item(id=38.0, historico={"minimo": 30.0, "habitual": 39.99}), cfg)
        self.assertEqual(d["etiqueta"], "EXCEPCIONAL")
        # Baja lo suficiente, pero queda muy por encima de su mínimo.
        d = v.decidir_item(self._item(historico={"minimo": 22.0, "habitual": 39.99}), cfg)
        self.assertEqual(d["veredicto"], "descartado")
        self.assertIn("por encima de su mínimo", d["motivo"])

    def test_decidir_sin_historico(self):
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        d = v.decidir_item(self._item(id=38.5), cfg)  # 22 % menos que otras tiendas
        self.assertEqual((d["veredicto"], d["etiqueta"]), ("pasa", "SUBIENDO"))
        self.assertIn("otras tiendas", d["prueba"])
        d = v.decidir_item(self._item(id=41.5), cfg)  # 28 %: llega al 25 % de DESTACADOS y es el mejor precio
        self.assertEqual((d["veredicto"], d["etiqueta"]), ("pasa", "DESTACADOS"))
        d = v.decidir_item(self._item(id=33.0), cfg)
        self.assertEqual(d["veredicto"], "descartado")
        self.assertIn("solo un 9 % por debajo del mercado", d["motivo"])
        d = v.decidir_item(self._item(hot="812° a 34 € hace 40 días"), cfg)
        self.assertEqual((d["veredicto"], d["etiqueta"]), ("pasa", "SUBIENDO"))
        d = v.decidir_item(self._item(), cfg)
        self.assertEqual((d["veredicto"], d["memoria"]), ("descartado", "dudosa"))
        self.assertIn("sin prueba de ventaja", d["motivo"])
        d = v.decidir_item(self._item(gratis=1), cfg)
        self.assertEqual(d["veredicto"], "pasa")

    def test_guia_modo_clasico(self):
        """Guía "Cómo leer y comprobar ofertas" (18/11/2026), apartados 3 y 4."""
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        u = v.CLASICO["tec"]
        # Con histórico: 20 % bajo el habitual y como mucho un 15 % sobre el mínimo.
        self.assertTrue(v.historico_clasico_ok(79.0, 100.0, 70.0, u))
        self.assertFalse(v.historico_clasico_ok(85.0, 100.0, 70.0, u), "solo un 15 % bajo el habitual")
        self.assertFalse(v.historico_clasico_ok(79.0, 100.0, 60.0, u), "un 32 % por encima de su mínimo")
        # Moda: 40 % anunciado por la tienda (30 % con gancho) y tallas (3; 4 en calzado para DESTACADOS).
        moda = dict(n="Zapatillas Joma Kaiser", t="adidas.es", p=60.0, id=80.0,
                    ficha={"precio_ahora": 60.0, "titulo": "Zapatillas Joma Kaiser",
                           "variantes": ["41: hay stock", "42: hay stock", "43: hay stock"]})
        d = v.decidir_item(self._item(**moda, a=85.0), cfg)
        self.assertEqual(d["veredicto"], "descartado")
        self.assertIn("anuncia un 29 %", d["motivo"])
        d = v.decidir_item(self._item(**moda, a=130.0), cfg)  # 54 % anunciado, 25 % frente a otras tiendas
        self.assertEqual((d["veredicto"], d["etiqueta"]), ("pasa", "SUBIENDO"), "3 tallas de calzado: no DESTACADOS")
        moda["ficha"]["variantes"].append("44: hay stock")
        self.assertEqual(v.decidir_item(self._item(**moda, a=130.0), cfg)["etiqueta"], "DESTACADOS")
        # Un mínimo de 30 días no es el mínimo histórico: no da EXCEPCIONAL.
        h = {"minimo": 30.0, "habitual": 40.0, "fuente": "Amazon", "min_30d": True}
        d = v.decidir_item(self._item(id=45.0, historico=h), cfg)
        self.assertEqual((d["veredicto"], d["etiqueta"]), ("pasa", "DESTACADOS"))
        self.assertEqual(v.decidir_item(self._item(id=45.0, historico={**h, "min_30d": False}), cfg)["etiqueta"],
                         "EXCEPCIONAL")
        # DESTACADOS exige el mejor precio: otra tienda más barata lo deja en SUBIENDO.
        self.assertEqual(v.decidir_item(self._item(id=29.0, historico={"habitual": 40.0}), cfg)["etiqueta"], "SUBIENDO")
        # Cosmética: solo grandes chollos (30 %).
        self.assertEqual(v.clase_clasico(cfg, "druni.es", "Eau de parfum Carolina Herrera 100 ml"), "cosmetica")
        self.assertEqual(v.decidir_item(self._item(n="Eau de parfum Carolina Herrera 100 ml", t="druni.es",
                                                   historico={"habitual": 40.0}), cfg)["veredicto"], "descartado")

    def test_decidir_descartes_y_sin_validar(self):
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        buena = {"id": 41.5}
        # #163 real: DualSense en Amazon a 54,90 € con hilo de Amazon a 56,99 €.
        d = v.decidir_item(self._item(p=54.9, ficha={"precio_ahora": 54.9}, id=80.0, ch_ahora="misma:56.99"), cfg)
        self.assertEqual(d["veredicto"], "descartado")
        self.assertIn("Chollometro", d["motivo"])
        self.assertEqual(v.decidir_item(self._item(ficha={"stock": "no"}, **buena), cfg)["memoria"], "caducada")
        # #184 real: la ficha se lee y es de otro producto. Con muro antibot no se sabe: lo mira la IA.
        d = v.decidir_item(self._item(validada=False, validacion="el título de la ficha no es el producto esperado", **buena), cfg)
        self.assertEqual(d["veredicto"], "descartado")
        d = v.decidir_item(self._item(validada=False, validacion="muro antibot o página de comprobación",
                                      ficha={"precio_ahora": None, "titulo": None}, **buena), cfg)
        self.assertEqual(d["veredicto"], "sin_validar")
        d = v.decidir_item(self._item(memoria="dudosa (2026-11-16): precio habitual", **buena), cfg)
        self.assertEqual(d["veredicto"], "descartado")
        self.assertEqual(v.decidir_item({"num": 9, "error": "no existe"}, cfg)["veredicto"], "descartado")
        # Moda: tallas de Chollometro (3 en calzado) y filtro del 15 % frente a otras tiendas.
        zap = dict(n="Zapatillas adidas Gazelle", t="adidas.es", p=60.0, id=75.0, a=110.0)  # moda: 45 % anunciado
        d = v.decidir_item(self._item(**zap, ficha={"precio_ahora": 60.0, "titulo": "Zapatillas adidas Gazelle",
                                                    "variantes": ["41: hay stock", "42: hay stock", "43: agotada"]}), cfg)
        self.assertEqual(d["veredicto"], "descartado")
        self.assertIn("quedan 2 tallas", d["motivo"])
        d = v.decidir_item(self._item(**zap, ficha={"precio_ahora": 60.0, "titulo": "Zapatillas adidas Gazelle",
                                                    "variantes": ["41: hay stock", "42: hay stock", "44: hay stock"]}), cfg)
        self.assertEqual((d["veredicto"], d["clase"]), ("pasa", "moda"))
        self.assertEqual(v.clase_clasico(cfg, "store.steampowered.com", "Hades II"), "digital")
        self.assertEqual(v.clase_clasico(cfg, "mediamarkt.es", "Detergente Ariel 60 lavados"), "consumible")

    def test_decidir_entrega_y_redaccion(self):
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 34.99
        self.ronda()  # deja un candidato en la lista, con su número
        con = sqlite3.connect(v.DB)
        n = con.execute("SELECT max(id) FROM lista").fetchone()[0]
        con.close()
        self.enviados.clear()
        orig = v.dossier_items
        item = self._item(num=n, id=38.5, a=59.99, img="https://img/x.jpg",
                          ficha={"specs": ["Autonomía: 36 h", "Bluetooth: 5.3"], "resumen": "Auriculares con cancelación de ruido."},
                          buscar_chollometro={"nombre": "https://www.chollometro.com/search?q=x"})
        try:
            v.dossier_items = lambda ids, **k: [item]
            with redirect_stdout(io.StringIO()) as salida:
                d = v.decidir(n)
            self.assertEqual(json.loads(salida.getvalue().strip().splitlines()[-1])["veredicto"], "pasa")
            self.assertEqual(self.enviados, [], "si pasa, el código no manda nada todavía")
            with redirect_stdout(io.StringIO()) as salida:
                v.redaccion(n)
            r = json.loads(salida.getvalue())
            self.assertEqual((r["etiqueta"], r["specs"][0]), ("SUBIENDO", "Autonomía: 36 h"))
            # La IA solo escribe título y descripción (y aunque ponga otra etiqueta o precios, manda el código).
            (v.DATOS / f"entrega_{n}.json").write_text(json.dumps(
                {"titulo": "[EXCEPCIONAL] Xiaomi Redmi Buds 6 Pro", "descripcion": "Texto.", "precios": "inventado"}), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertTrue(v.entregar(n))
            # Reglas de formato de Zeta (17/11/2026): 6 mensajes, foto, título, enlace, precio, INFO, descripción.
            textos = [t for _, t, _ in self.enviados]
            self.assertEqual(len(textos), 6)
            self.assertEqual((textos[0], self.enviados[0][2]), ("", "https://img/x.jpg"), "la foto, sola")
            self.assertEqual(textos[1], "Xiaomi Redmi Buds 6 Pro", "sin etiqueta en el título")
            self.assertTrue(textos[2].startswith("http"))
            self.assertTrue(textos[3].startswith("30 €"), textos[3])
            self.assertIn("PVP de la tienda: 59,99 €", textos[3])
            self.assertIn("Envío: portes no verificados", textos[3])
            self.assertNotIn("41,5", textos[3], "el mercado no es una referencia del bloque de precio")
            info = textos[4]
            self.assertTrue(info.startswith("🏷️ SUBIENDO"))
            self.assertIn("💶 30 € frente a PVP de 59,99 €: -50 % sobre esa referencia, no sobre habitual probado.", info)
            self.assertIn("📉 Comparación puntual: siguiente precio en otras tiendas 38,50 €", info)
            self.assertRegex(info, r"🕒 Detectada \S+ · Enviada \d\d:\d\d del \d\d/\d\d/\d{4}")
            self.assertIn("🔎 Chollometro: no localizado en la consulta. Búsqueda manual: https://www.chollometro.com/search", info)
            for proceso in ("Comprobado:", "Prueba:", "PENDIENTE", "Precio inicial publicado"):
                self.assertNotIn(proceso, info + textos[3])
            self.assertEqual(textos[5], "Texto.")
            self.assertFalse((v.DATOS / f"decision_{n}.json").exists())
            # Descarte: lo dice el código por Telegram, con números, y guarda el veredicto del modelo.
            self.enviados.clear()
            v.dossier_items = lambda ids, **k: [self._item(num=n, id=33.0)]
            with redirect_stdout(io.StringIO()):
                v.decidir(n)
            self.assertEqual(len(self.enviados), 1)
            self.assertTrue(self.enviados[0][1].startswith(f"#{n} descartado: solo un 9 % por debajo del mercado"))
            con = sqlite3.connect(v.DB)
            self.assertEqual(con.execute("SELECT veredicto FROM modelos").fetchall()[-1][0], "dudosa")
            con.close()
            # Sin Hermes: redacción por plantilla, 0 tokens.
            self.enviados.clear()
            cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
            cfg["entrega"]["redaccion"] = "plantilla"
            v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
            item.pop("memoria", None)
            v.dossier_items = lambda ids, **k: [item]
            with redirect_stdout(io.StringIO()):
                d = v.decidir(n)
            self.assertTrue(d["entregado"])
            self.assertEqual(len(self.enviados), 6)
            self.assertIn("📋 <b>Características</b>\n• Autonomía: 36 h", self.enviados[5][1], "negrita real")

        finally:
            v.dossier_items = orig

    def test_formato_de_ficha(self):
        self.assertEqual((v.eur(44.95), v.eur(60.0), v.eur(1100), v.eur(41.5)), ("44,95 €", "60 €", "1.100 €", "41,50 €"))
        self.assertEqual(v.a_html_telegram("🧥 **Diseño & materiales**\n• 100% <algodón>"),
                         "🧥 <b>Diseño &amp; materiales</b>\n• 100% &lt;algodón&gt;")
        self.assertTrue(v.enlace_hilo({"titulo": "Lorus Reloj Analógico", "id": 2074140})
                        .endswith("/ofertas/lorus-reloj-analogico-2074140"))
        copia = {"hilos": v.hilos_chollometro(cm_html([
            hilo("Xiaomi Redmi Buds 6 Pro", "Amazon", 36.0, temp=850, dias=40, caducado=True),
            hilo("Xiaomi Redmi Buds 6 Pro", "MediaMarkt", 39.0, temp=120, dias=5)])), "fresca": True}
        a = v.antecedentes_chollometro("Xiaomi Redmi Buds 6 Pro Negro", copia)
        self.assertEqual([(x["precio"], x["temp"], x["caducado"]) for x in a], [(39.0, 120, False), (36.0, 850, True)],
                         "del más reciente al más antiguo")
        self.assertTrue(a[0]["url"].startswith("https://www.chollometro.com/ofertas/xiaomi-redmi-buds-6-pro-"))
        # Otro televisor del mismo tamaño no es antecedente de una Samsung TQ55Q7F5AU.
        tv = {"hilos": v.hilos_chollometro(cm_html([hilo("TV Philips LED 55PUS7000 55 4K Ultra HD Smart TV HDR10+ DVB-T2", "Amazon", 297.99),
                                                     hilo("Samsung TQ55Q7F5AU 55 QLED 4K", "Amazon", 399.0)])), "fresca": True}
        self.assertEqual([x["precio"] for x in v.antecedentes_chollometro(
            "Samsung TQ55Q7F5AU 55 pulgadas 4K QLED HDR10+ Tizen Smart TV", tv)], [399.0])
        # Índice por código de modelo (sin copia: va a la base) y mejor precio publicado, con su fecha.
        con = v.bd()
        v.guardar_hilos(con, v.hilos_chollometro(cm_html([
            hilo("Samsung TQ55Q7F5AU 55 QLED 4K", "Amazon", 399.0, temp=640, dias=20),
            hilo("TV Samsung TQ55Q7F5AU 55 pulgadas", "MediaMarkt", 349.0, temp=210, dias=3),
            hilo("TV Philips LED 55PUS7000 HDR10+", "Amazon", 297.99)])))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM cm_codigos WHERE codigo='TQ55Q7F5AU'").fetchone()[0], 2)
        con.close()
        nombre = "Samsung TQ55Q7F5AU 55 pulgadas 4K QLED HDR10+ Tizen Smart TV"
        self.assertEqual([x["precio"] for x in v.antecedentes_chollometro(nombre)], [349.0, 399.0])
        mejor = v.mejor_chollometro(nombre)
        self.assertEqual((mejor["precio"], mejor["temp"]), (349.0, 210))
        info = v.bloques_de_codigo({"num": 1, "n": nombre, "u": "https://x.es/tv", "p": 365.95, "mejor_cm": mejor,
                                    "historico": {"minimo": 330.0, "minimo_fecha": "2026-11-16", "fuente": "pccomtracker.com"}},
                                   {"precio": 365.95, "etiqueta": "SUBIENDO", "prueba": "x"})["info"]
        self.assertIn(f"📉 Mejor precio publicado en Chollometro: 349 € ({mejor['fecha']}, 210°); ahora 5 % más caro.", info)
        self.assertIn("📉 Mínimo registrado en pccomtracker.com: 330 € (16/11/2026).", info)
        self.assertEqual(v.limpiar_envio("Envío: Desde 15,95 € Devolución: Gratuita 30 días Garantía: 3 años"), "Desde 15,95 €")

    def test_decidir_con_la_ficha_que_lee_hermes(self):
        # #189 (17/11/2026): PcComponentes da 403 al script. Hermes abre la ficha y pasa precio y stock.
        self.assertEqual(v.ficha_de_args(["--precio", "30,00", "--stock", "si", "--vendedor", "PcComponentes"]),
                         {"precio_ahora": 30.0, "stock": "sí", "vendedor": "PcComponentes"})
        self.assertEqual(v.ficha_de_args(["--precio", "1.299,90", "--stock", "agotado"])["precio_ahora"], 1299.9)
        self.assertEqual(v.ficha_de_args(["--precio", "12", "--stock", "agotado"])["stock"], "no")
        self.assertEqual(v.ficha_de_args(["--precio", "12", "--vendedor", "Tech", "Store", "SL"])["vendedor"], "Tech Store SL")
        self.assertIsNone(v.ficha_de_args([]))
        self.assertIsNone(v.ficha_de_args(["--callado"]))
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 34.99
        self.ronda()
        con = sqlite3.connect(v.DB)
        n = con.execute("SELECT max(id) FROM lista").fetchone()[0]
        con.close()
        leidas = []
        orig_ficha, orig_hist = v.leer_ficha, v.historico
        try:
            v.leer_ficha = lambda url, nav=None: leidas.append(url) or {"error": "HTTP Error 403: Forbidden"}
            v.historico = lambda cfg, item: {"minimo": 29.0, "habitual": 39.99, "fuente": "pccomtracker.com"}
            with redirect_stdout(io.StringIO()):
                d = v.decidir(n, avisar=False)
            self.assertEqual(d["veredicto"], "sin_validar", "sin datos de Hermes: no se puede decidir")
            # Hermes choca con el muro (#193, 17/11/2026): `muro N` y las siguientes van a Zeta, sin IA.
            with redirect_stdout(io.StringIO()):
                self.assertEqual(v.muro(n, avisar=False)["veredicto"], "a_mano")
            self.enviados.clear()
            with redirect_stdout(io.StringIO()):
                d = v.decidir(n)
            self.assertEqual(d["veredicto"], "a_mano")
            self.assertEqual(len(self.enviados), 1)
            self.assertIn("Mírala tú:", self.enviados[0][1])
            self.assertIn(f"comprueba {n} precio", self.enviados[0][1])
            con = sqlite3.connect(v.DB)
            k = con.execute("SELECT k FROM meta WHERE k LIKE 'muro:%'").fetchone()[0]
            con.execute("UPDATE meta SET v=? WHERE k=?",
                        ((datetime.now() - timedelta(hours=v.MURO_HORAS + 1)).isoformat(timespec="minutes"), k))
            con.commit()
            con.close()
            with redirect_stdout(io.StringIO()):
                d = v.decidir(n, avisar=False)
            self.assertEqual(d["veredicto"], "sin_validar", "pasadas las horas del muro, Hermes vuelve a probar")
            leidas.clear()
            with redirect_stdout(io.StringIO()):
                d = v.decidir(n, avisar=False, ficha_manual=v.ficha_de_args(["--precio", "30", "--stock", "si"]))
            self.assertEqual(leidas, [], "con la ficha de Hermes no se vuelve a pedir la de la tienda")
            self.assertEqual((d["veredicto"], d["etiqueta"]), ("pasa", "DESTACADOS"))
            with redirect_stdout(io.StringIO()):
                d = v.decidir(n, avisar=False, ficha_manual=v.ficha_de_args(["--precio", "38,00", "--stock", "si"]))
            self.assertEqual(d["veredicto"], "descartado", "38 € con habitual de 39,99 € baja solo un 5 %")
            self.assertIn("baja un 5 %", d["motivo"])
            with redirect_stdout(io.StringIO()):
                d = v.decidir(n, avisar=False, ficha_manual=v.ficha_de_args(["--precio", "30", "--stock", "no"]))
            self.assertIn("agotado", d["motivo"])
        finally:
            v.leer_ficha, v.historico = orig_ficha, orig_hist

    def test_plugin_del_boton_decide_por_codigo(self):
        import importlib.util
        ruta = CONF.parent / "hermes" / "plugins" / "vigilante-botones" / "__init__.py"
        spec = importlib.util.spec_from_file_location("vigilante_botones", ruta)
        b = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(b)
        self.assertEqual(b.texto_de("vg:verifica:47"), "verifica 47")
        # Botón "Seguir" de la precarga (18/11/2026): lanza `vigilante.py seguir` con las rutas de la habilidad.
        self.assertRegex("vg:seguir:1", b.PATRON_SEGUIR)
        self.assertEqual(b.orden_seguir(("py.exe", r"C:\v\src\vigilante.py")), ["py.exe", r"C:\v\src\vigilante.py", "seguir"])
        self.assertIsNone(b.orden_seguir(None))
        self.assertIsNone(b.texto_de("otra:cosa"))
        # Descartado: no se despierta a la IA. Pasa: tarea corta. Sin validar o fallo: el camino completo.
        self.assertEqual(b.accion_de('{"num":47,"veredicto":"descartado","motivo":"x"}', "47"), (None, "Descartado #47"))
        self.assertEqual(b.accion_de('ruido\n{"num":47,"veredicto":"pasa","etiqueta":"DESTACADOS"}', "47"),
                         ("redacta 47", "Redactando #47 (DESTACADOS)"))
        self.assertEqual(b.accion_de('{"num":47,"veredicto":"pasa","etiqueta":"SUBIENDO","entregado":true}', "47"),
                         (None, "Enviado #47"))
        # Sin validar (PcComponentes da 403 al script): Hermes solo lee la ficha y el código decide.
        self.assertEqual(b.accion_de('{"num":47,"veredicto":"sin_validar","motivo":"HTTP Error 403"}', "47")[0], "comprueba 47")
        self.assertEqual(b.accion_de('{"num":47,"veredicto":"a_mano","motivo":"muro"}', "47"), (None, "Lo miras tú #47"))
        for roto in (None, "", "Traceback (most recent call last)", "{}"):
            self.assertEqual(b.accion_de(roto, "47")[0], "verifica 47", repr(roto))
        # El plugin usa el mismo python y el mismo script que declara la habilidad instalada.
        for nombre in ("verifica-chollo", "redacta-chollo", "comprueba-chollo"):
            skill = (CONF.parent / "hermes" / "skills" / nombre / "SKILL.md").read_text(encoding="utf-8")
            py, script = b.rutas_de_skill(skill)
            self.assertTrue(py.endswith("python.exe"), nombre)
            self.assertTrue(script.endswith("vigilante-chollos\\src\\vigilante.py"), nombre)
        self.assertIsNone(b.rutas_de_skill("sin rutas"))
        redacta = (CONF.parent / "hermes" / "skills" / "redacta-chollo" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("V redaccion N", redacta)
        self.assertIn("V entregar N", redacta)
        comprueba = (CONF.parent / "hermes" / "skills" / "comprueba-chollo" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("V decidir N --precio", comprueba)
        self.assertIn("V muro N", comprueba)
        self.assertIn("comprueba-chollo", (CONF.parent / "scripts" / "instalar_en_hermes.sh").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((CONF / "tiendas.json").read_text(encoding="utf-8"))["entrega"]["redaccion"], "hermes")

    def test_boton_para_verificar(self):
        # Un botón "Verificar #N" debajo de CADA aviso (Zeta: "si no, es un lío"). Lo atiende el plugin
        # de Hermes vigilante-botones, que entrega "verifica N" como si lo hubiera escrito Zeta.
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 34.99
        self.red.tiendas["mediamarkt"].append({"n": "JBL Flip 6", "m": "JBL", "e": "6925281993145", "p": 79.0, "a": 149.0})
        self.red.idealo["6925281993145"] = "99,00"
        self.ronda()
        self.assertEqual(len(self.enviados), 2)
        for (_, texto, _), teclado in zip(self.enviados, self.teclados):
            n = re.match(r"#(\d+)", texto).group(1)
            boton = json.loads(teclado)["inline_keyboard"]
            self.assertEqual(boton, [[{"text": f"Verificar #{n}", "callback_data": f"vg:verifica:{n}"}]])
        plugin = RAIZ / "hermes" / "plugins" / "vigilante-botones" / "__init__.py"
        if plugin.exists():  # el plugin traduce el botón al texto que entiende la habilidad
            import importlib.util
            spec = importlib.util.spec_from_file_location("vigilante_botones", plugin)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self.assertEqual(mod.texto_de("vg:verifica:47"), "verifica 47")
            self.assertIsNone(mod.texto_de("mp:modelo"))
            self.assertTrue(re.match(mod.PATRON, "vg:verifica:47"))
            self.assertFalse(re.match(mod.PATRON, "ea:once:3"))

    def test_sin_enlace_no_se_envia(self):
        # Foot Locker sin URL: Hermes no puede abrir la ficha ("sin url ni ficha").
        self.ronda()
        orig = v.extraer

        def sin_url(html, base, extra=()):
            ps = orig(html, base, extra)
            for p in ps:
                p["url"] = None
            return ps
        v.extraer = sin_url
        try:
            self.red.tiendas["mediamarkt"][0]["p"] = 34.99
            self.ronda()
        finally:
            v.extraer = orig
        self.assertEqual(self.enviados, [])

    def test_envia_lista_sin_despertar_a_la_ia(self):
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 34.99
        self.assertEqual(self.ronda(), [], "Hermes no se despierta")
        self.assertEqual(len(self.enviados), 1)
        chat, texto, foto = self.enviados[0]
        self.assertEqual(chat, "456808276")
        self.assertIn("#1 · mediamarkt.es", texto)
        self.assertIn("34.99 € (referencia de la tienda: 79.99 €, -56 %)", texto)
        self.assertIn("Mercado: 45 €", texto)
        self.assertEqual(json.loads(self.teclados[0])["inline_keyboard"][0][0]["callback_data"], "vg:verifica:1")
        self.assertTrue(foto.endswith("/img.jpg"))
        n = sqlite3.connect(v.DB).execute("SELECT COUNT(*) FROM cupo").fetchone()[0]
        self.assertEqual(n, 0, "la lista gratis no gasta cupo de verificaciones")

    def test_freno_si_llegan_muchos_de_golpe(self):
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"].update({"max_avisos_telegram_hora": 5, "max_avisos_telegram_dia": 8})
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        self.ronda()
        # Una tienda "cambia sus enlaces": 12 productos nuevos de golpe (la tanda deja pasar 15).
        self.red.tiendas["mediamarkt"] += [{"n": f"JBL Charge {i}", "m": "JBL", "p": 50.0, "a": 150.0} for i in range(12)]
        self.ronda()
        candidatos = [t for _, t, _ in self.enviados if t.startswith("#")]
        avisos = [t for _, t, _ in self.enviados if "Freno del vigilante" in t]
        self.assertEqual(len(candidatos), 5, "solo hasta el límite por hora")
        self.assertEqual(len(avisos), 1, "un único aviso")
        self.assertIn("en una hora", avisos[0])
        # Siguiente ronda dentro de la pausa: no se manda nada más ni se repite el aviso.
        self.red.tiendas["mediamarkt"].append({"n": "JBL Go 4", "m": "JBL", "p": 20.0, "a": 60.0})
        self.ronda()
        self.assertEqual(len(self.enviados), 6)
        self.assertIn("FRENO telegram", v.LOG.read_text(encoding="utf-8"))
        if datetime.now().hour < 1:
            return  # entre las 00:00 y la 01:00 no existe "hoy, hace más de una hora": el resto no se puede simular
        # Pasada la pausa: sale lo pendiente hasta el tope del día (8) y avisa del tope diario.
        con = sqlite3.connect(v.DB)
        con.execute("UPDATE meta SET v='2000-01-01T00:00' WHERE k='freno_telegram'")
        con.execute("UPDATE lista SET fecha=? WHERE enviado=1", (datetime.now().strftime("%Y-%m-%dT00:00"),))
        con.commit()
        self.ronda()
        candidatos = [t for _, t, _ in self.enviados if t.startswith("#")]
        avisos = [t for _, t, _ in self.enviados if "Freno del vigilante" in t]
        self.assertEqual(len(candidatos), 8)
        self.assertEqual(len(avisos), 2)
        self.assertIn("8 avisos hoy", avisos[1])

    def test_no_repite_otro_color_ni_otra_tienda(self):
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 34.99
        self.ronda()
        self.red.tiendas["pccomponentes"] = [{"n": "Xiaomi Redmi Buds 6 Pro Blanco", "m": "Xiaomi", "p": 34.99, "a": 79.99}]
        self.ronda()  # siembra pccomponentes
        self.red.tiendas["pccomponentes"][0]["p"] = 34.50
        self.ronda()
        self.assertEqual(len(self.enviados), 1, "mismo modelo, otro color, casi mismo precio: no se repite")
        self.red.tiendas["pccomponentes"][0]["p"] = 29.99
        self.ronda()
        self.assertEqual(len(self.enviados), 2, "baja de verdad: sí avisa")

    def test_memoria_dudosa_descarta_sin_buscar(self):
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 34.99
        self.ronda()
        with redirect_stdout(io.StringIO()):
            v.recordar(1, "dudosa", "modelo descontinuado")
        self.red.tiendas["mediamarkt"][0]["p"] = 29.99
        self.ronda()
        self.assertEqual(len(self.enviados), 1)

    def test_oferta_caducada_vuelve_a_avisar(self):
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 34.99
        self.ronda()
        self.assertEqual(len(self.enviados), 1)
        with redirect_stdout(io.StringIO()):
            v.recordar(1, "caducada", "la oferta flash se acabó")
        self.red.tiendas["mediamarkt"][0]["p"] = 49.99
        self.ronda()
        self.assertEqual(len(self.enviados), 1, "sube: nada")
        self.red.tiendas["mediamarkt"][0]["p"] = 36.99
        self.ronda()
        self.assertEqual(len(self.enviados), 1, "baja, pero no al precio de la oferta: nada")
        self.red.tiendas["mediamarkt"][0]["p"] = 34.99
        self.ronda()
        self.assertEqual(len(self.enviados), 2, "vuelve al precio de la oferta: avisa otra vez")

    def test_dossier(self):
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 34.99
        self.ronda()
        ficha = ('<meta property="og:image" content="/foto-buds.jpg">'
                 '<meta name="description" content="Auriculares con cancelación de ruido y 36 h de batería.">'
                 '<script type="application/ld+json">' + json.dumps({"@type": "Product", "name": "Xiaomi Redmi Buds 6 Pro",
                 "offers": {"price": "32,99", "availability": "https://schema.org/InStock",
                            "seller": {"@type": "Organization", "name": "MediaMarkt"}}}) + "</script>"
                 "<p>Envío gratis a partir de 50 €</p>")
        red = self.red

        def falso(url, timeout=20):
            if "/p/" in url:
                return ficha, url
            return red(url, timeout)
        v.descargar = falso
        with redirect_stdout(io.StringIO()):
            v.recordar(1, "buena", "precio mínimo en Idealo")
        buf = io.StringIO()
        with redirect_stdout(buf):
            v.dossier(["1", "99"])
        d = json.loads(buf.getvalue())
        self.assertIn("ficha", d[0], d)
        f = d[0]["ficha"]
        self.assertEqual((f["precio_ahora"], f["stock"], f["vendedor"]), (32.99, "sí", "MediaMarkt"))
        self.assertIn("Envío gratis", f["envio"])
        self.assertTrue(f["foto"].endswith("/foto-buds.jpg"))
        self.assertIn("cancelación", f["resumen"])
        self.assertIn("34.99 € -> 32.99 €", d[0]["aviso"])
        self.assertTrue(d[0]["memoria"].startswith("buena"))
        self.assertEqual(d[0]["num"], 1)
        self.assertEqual(d[1], {"num": 99, "error": "no existe"})

    def test_token_desde_env_de_hermes(self):
        v.token_telegram = self._orig_tg[1]
        (self.tmp / "scripts").mkdir()
        (self.tmp / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-x\nTELEGRAM_BOT_TOKEN=\"123456:AbC-dEf\"\n", encoding="utf-8")
        orig_base = v.BASE
        v.BASE = self.tmp / "scripts"
        viejo = {k: os.environ.pop(k, None) for k in ("VIGILANTE_TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN")}
        try:
            self.assertEqual(v.token_telegram(), "123456:AbC-dEf")
        finally:
            v.BASE = orig_base
            for k, val in viejo.items():
                if val is not None:
                    os.environ[k] = val

    def test_modelo_base(self):
        self.assertEqual(v.modelo_base("Xiaomi Redmi Buds 6 Pro Negro"), v.modelo_base("Xiaomi Redmi Buds 6 Pro (Blanco)"))
        self.assertNotEqual(v.modelo_base("Galaxy S26 128GB"), v.modelo_base("Galaxy S26 256GB"))


# ------------------------------------------- reglas aprendidas de Instinct

class ReglasInstinct(Base):
    def cfg(self, **cambios):
        c = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        for k, val in cambios.items():
            c[k] = val
        v.CONFIG.write_text(json.dumps(c), encoding="utf-8")
        return c

    def test_valoracion_y_resenas_se_leen(self):
        html = ('<script type="application/ld+json">' + json.dumps({"@type": "Product", "name": "Xiaomi Redmi Buds 6",
                "offers": {"price": 25}, "aggregateRating": {"ratingValue": "4,6", "reviewCount": "1.234"}}) + "</script>")
        p = v.extraer(html, "https://t.es/")[0]
        self.assertEqual((p["valoracion"], p["resenas"]), (4.6, 1234))
        self.assertEqual(v.normalizar_nota(4.6), 92.0)
        self.assertEqual(v.normalizar_nota(95), 95)

    def test_reglas_por_tienda(self):
        c = json.loads((CONF / "tiendas.json").read_text(encoding="utf-8"))
        r = v.reglas_de(c, "videojuegos", "store.steampowered.com")
        self.assertEqual((r["descuento_min"], r["min_resenas"], r["exigir_descuento"]), (0.80, 500, True))
        self.assertEqual(v.reglas_de(c, "tecnologia", "mediamarkt.es")["descuento_min"], 0.20)
        self.assertEqual(v.reglas_de(c, "hogar", "ikea.com/es")["descuento_min"], 0.30)

    def test_sin_tope_de_precio_por_defecto(self):
        c = json.loads((CONF / "tiendas.json").read_text(encoding="utf-8"))
        self.assertEqual(v.reglas_de(c, "tecnologia", "mediamarkt.es")["precio_max"], float("inf"))
        c["categorias"]["tecnologia"]["precio_max"] = 1500
        self.assertEqual(v.reglas_de(c, "tecnologia", "mediamarkt.es")["precio_max"], 1500)

    def test_entrega_en_6_mensajes(self):
        enviados = []
        orig = (v.enviar_telegram, v.token_telegram)
        v.enviar_telegram = lambda tok, chat, texto, foto=None, teclado=None, **k: enviados.append((texto, foto)) or True
        v.token_telegram = lambda: "x"
        c = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        c["entrega"] = {"modo": "telegram", "chat_id": "1"}
        v.CONFIG.write_text(json.dumps(c), encoding="utf-8")
        try:
            con = v.bd()
            con.execute("INSERT INTO lista (id, fecha, base, precio, datos) VALUES (7, '2026-11-07', 'x', 1, ?)",
                        (json.dumps({"img": "https://t.es/f.jpg"}),))
            con.commit()
            con.close()
            bloques = {"titulo": "[MUY BUENA] JBL Flip 6 altavoz Bluetooth", "precios": "Precio inicial: 149 €",
                       "info": "Mínimo histórico: 79 €", "enlace": "https://t.es/jbl", "descripcion": "Altavoz portátil."}
            (v.DATOS / "entrega_7.json").write_text(json.dumps(bloques), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertTrue(v.entregar(7))
            # Reglas de formato de Zeta (17/11/2026): foto sola, título sin etiqueta, enlace, precio, INFO, descripción.
            self.assertEqual([e[0] for e in enviados], ["", "JBL Flip 6 altavoz Bluetooth", "https://t.es/jbl",
                                                        "Precio inicial: 149 €", "Mínimo histórico: 79 €", "Altavoz portátil."])
            self.assertEqual(enviados[0][1], "https://t.es/f.jpg", "la foto, sola, en el primer mensaje")
            self.assertTrue(all(f is None for _, f in enviados[1:]))
            self.assertFalse((v.DATOS / "entrega_7.json").exists())
            (v.DATOS / "entrega_7.json").write_text(json.dumps({"titulo": "x"}), encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertFalse(v.entregar(7), "si faltan bloques no envía nada a medias")
        finally:
            v.enviar_telegram, v.token_telegram = orig

    def test_exclusiones(self):
        cfg = {"filtros_codigo": {"excluir": ["xbox", "shein"]}}
        self.assertTrue(v.excluido({"nombre": "Mando Xbox Wireless"}, "mediamarkt.es", cfg))
        self.assertTrue(v.excluido({"nombre": "Vestido"}, "shein.com", cfg))
        self.assertFalse(v.excluido({"nombre": "Xboxer Pro"}, "x.es", cfg))

    def test_pocas_resenas_y_nota_baja_se_descartan(self):
        c = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        c["reglas_tienda"] = {"mediamarkt.es": {"min_resenas": 100, "min_valoracion": 90}}
        v.CONFIG.write_text(json.dumps(c), encoding="utf-8")
        self.red.tiendas = {"mediamarkt": [], "pccomponentes": []}
        self.ronda()
        prods = [{"n": "Xiaomi Buds A", "m": "Xiaomi", "p": 20, "a": 50}, {"n": "Xiaomi Buds B", "m": "Xiaomi", "p": 20, "a": 50},
                 {"n": "Xiaomi Buds C", "m": "Xiaomi", "p": 20, "a": 50}]
        self.red.tiendas["mediamarkt"] = prods
        orig = v.extraer

        def con_notas(html, base, extra=()):
            ps = orig(html, base, extra)
            notas = {"Xiaomi Buds A": (4.8, 5000), "Xiaomi Buds B": (4.8, 12), "Xiaomi Buds C": (3.9, 9000)}
            for p in ps:
                p["valoracion"], p["resenas"] = notas.get(p["nombre"], (None, None))
            return ps
        v.extraer = con_notas
        try:
            self.ronda()  # siembra con productos
            for x in prods:
                x["p"] = 18
            nombres = [c["n"] for c in self.ronda()]
        finally:
            v.extraer = orig
        self.assertEqual(nombres, ["Xiaomi Buds A"])

    def test_tus_publicaciones_no_se_repiten(self):
        perfil = '<a href="/ofertas/xiaomi-redmi-buds-6-pro-2057000">Xiaomi Redmi Buds 6 Pro auriculares ANC</a>'
        red = self.red

        def falso(url, timeout=20):
            if "/profile/" in url:
                return perfil, url
            return red(url, timeout)
        v.descargar = falso
        c = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        c["entrega"] = {"modo": "hermes", "perfil_chollometro": "PorLa_Jeta"}
        v.CONFIG.write_text(json.dumps(c), encoding="utf-8")
        self.red.tiendas = {"mediamarkt": [{"n": "Xiaomi Redmi Buds 6 Pro Negro", "m": "Xiaomi", "p": 39.99, "a": 79.99},
                                           {"n": "JBL Flip 6", "m": "JBL", "p": 99, "a": 149}], "pccomponentes": []}
        self.ronda()
        for x in self.red.tiendas["mediamarkt"]:
            x["p"] = round(x["p"] * 0.8, 2)
        self.assertEqual([x["n"] for x in self.ronda()], ["JBL Flip 6"])


class SilencioYUltimaComprobacion(Base):
    def setUp(self):
        super().setUp()
        c = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        c["entrega"] = {"modo": "telegram", "chat_id": "1"}
        c["horario"]["silencio_entregas"] = [["14:00", "15:00"]]
        v.CONFIG.write_text(json.dumps(c), encoding="utf-8")
        self.enviados = []
        self._orig_sil = (v.enviar_telegram, v.token_telegram, v.ahora_madrid)
        v.enviar_telegram = lambda tok, chat, texto, foto=None, teclado=None, **k: self.enviados.append(texto) or True
        v.token_telegram = lambda: "x"
        self.hora = "14:30"
        v.ahora_madrid = lambda: datetime.strptime(self.hora, "%H:%M")
        self.red.tiendas = {"mediamarkt": [{"n": "JBL Flip 6", "m": "JBL", "p": 99, "a": 149}], "pccomponentes": []}

    def tearDown(self):
        v.enviar_telegram, v.token_telegram, v.ahora_madrid = self._orig_sil
        super().tearDown()

    def test_guarda_en_silencio_y_envia_despues(self):
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 79
        self.ronda()
        self.assertEqual(self.enviados, [], "de 14 a 15 no se envía")
        self.hora = "15:05"
        self.ronda()
        self.assertEqual(len(self.enviados), 1)
        self.assertIn("JBL Flip 6", self.enviados[0])
        self.ronda()
        self.assertEqual(len(self.enviados), 1, "no se reenvía")

    def test_ultima_comprobacion_antes_de_enviar(self):
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 79
        self.ronda()  # guardada en silencio
        # mientras tanto alguien la publica en la misma tienda
        self.red.chollometro = [hilo("JBL Flip 6", "MediaMarkt", 79.0)]
        self.hora = "15:05"
        self.ronda()
        self.assertEqual(self.enviados, [])
        estado = sqlite3.connect(v.DB).execute("SELECT enviado FROM lista").fetchone()[0]
        self.assertEqual(estado, 2)


# ----------------------------------------------- videojuegos por su fuente

STEAM = json.dumps({"results_html": """
<a href="https://store.steampowered.com/app/413150/Stardew_Valley/?snr=1" data-ds-appid="413150" class="search_result_row ds_collapse_flag">
 <div class="col search_name ellipsis"><span class="title">Stardew Valley</span></div>
 <span class="search_review_summary positive" data-tooltip-html="Extremadamente positivos&lt;br&gt;El 98 % de las 612.345 reseñas de los usuarios para este juego son positivas."></span>
 <div class="discount_block"><div class="discount_pct">-80%</div><div class="discount_prices">
 <div class="discount_original_price">13,99€</div><div class="discount_final_price">2,79€</div></div></div></a>
<a href="https://store.steampowered.com/app/999/Juego_Raro/" data-ds-appid="999" class="search_result_row">
 <span class="title">Juego Raro</span>
 <span class="search_review_summary" data-tooltip-html="Positivos&lt;br&gt;El 85 % de las 37 reseñas de los usuarios para este juego son positivas."></span>
 <div class="discount_original_price">9,99€</div><div class="discount_final_price">0,99€</div></a>
<a href="https://store.steampowered.com/app/555/Solo_50/" data-ds-appid="555" class="search_result_row">
 <span class="title">Solo Cincuenta</span>
 <span class="search_review_summary" data-tooltip-html="Muy positivos&lt;br&gt;El 91 % de las 20.000 reseñas de los usuarios para este juego son positivas."></span>
 <div class="discount_original_price">20,00€</div><div class="discount_final_price">10,00€</div></a>"""})

EPIC = json.dumps({"data": {"Catalog": {"searchStore": {"elements": [
    {"title": "Juego Gratis", "productSlug": "juego-gratis", "keyImages": [{"type": "OfferImageWide", "url": "https://cdn/x.jpg"}],
     "price": {"totalPrice": {"discountPrice": 0, "originalPrice": 2499}},
     "promotions": {"promotionalOffers": [{"promotionalOffers": [{"discountSetting": {"discountPercentage": 0}}]}]}},
    {"title": "Próximamente", "productSlug": "luego", "price": {"totalPrice": {"discountPrice": 1999, "originalPrice": 1999}},
     "promotions": {"promotionalOffers": [], "upcomingPromotionalOffers": [{}]}}]}}}})

REDDIT = json.dumps({"data": {"children": [
    {"data": {"title": "[Game] Monument Valley 2 ($4.99 -> Free)", "url": "https://play.google.com/store/apps/details?id=a.b"}},
    {"data": {"title": "[App] Nova Launcher Prime - 4,99 € → 0,99 €", "url": "https://play.google.com/store/apps/details?id=c.d"}},
    {"data": {"title": "[Game] Algo sin precio", "url": "https://play.google.com/store/apps/details?id=e.f"}},
    {"data": {"title": "Discusión semanal", "url": "https://www.reddit.com/r/x"}}]}})


class FuentesJuegos(unittest.TestCase):
    def setUp(self):
        self._orig = (v.descargar, v.time.sleep)
        v.time.sleep = lambda s: None

        def falso(url, timeout=20):
            if "steampowered.com/search/results" in url:
                return (STEAM if "start=0" in url else json.dumps({"results_html": ""})), url
            if "freeGamesPromotions" in url:
                return EPIC, url
            if "reddit.com" in url:
                return REDDIT, url
            raise OSError("no esperado: " + url)
        v.descargar = falso

    def tearDown(self):
        v.descargar, v.time.sleep = self._orig

    def test_steam(self):
        ps = {p["nombre"]: p for p in v.steam_ofertas()}
        sv = ps["Stardew Valley"]
        self.assertEqual((sv["precio"], sv["precio_antes"], sv["valoracion"], sv["resenas"]), (2.79, 13.99, 98.0, 612345))
        self.assertEqual(sv["url"], "https://store.steampowered.com/app/413150/Stardew_Valley/")
        self.assertTrue(sv["imagen_url"].endswith("/413150/header.jpg"))
        self.assertEqual(ps["Juego Raro"]["resenas"], 37)

    def test_epic_solo_lo_que_esta_en_promocion(self):
        ps = v.epic_gratis()
        self.assertEqual([(p["nombre"], p["precio"], p["precio_antes"]) for p in ps], [("Juego Gratis", 0.0, 24.99)])
        self.assertEqual(ps[0]["url"], "https://store.epicgames.com/es-ES/p/juego-gratis")

    def test_apps_desde_reddit(self):
        ps = {p["nombre"]: (p["precio"], p["precio_antes"]) for p in v.ofertas_apps("googleplaydeals", "play.google.com")}
        self.assertEqual(ps, {"Monument Valley 2": (0.0, 4.99), "Nova Launcher Prime": (0.99, 4.99)})

    def test_reglas_de_juegos_en_la_ronda(self):
        """80 % o más y 500 reseñas: pasa Stardew; fuera el de 37 reseñas y el del 50 %."""
        cfg = json.loads((CONF / "tiendas.json").read_text(encoding="utf-8"))
        r = v.reglas_de(cfg, "videojuegos", "store.steampowered.com")
        pasan = []
        for p in v.steam_ofertas():
            d = 1 - p["precio"] / p["precio_antes"]
            if d >= r["descuento_min"] and (p["resenas"] or 0) >= r["min_resenas"]:
                pasan.append(p["nombre"])
        self.assertEqual(pasan, ["Stardew Valley"])

    def test_fuente_caida_no_rompe(self):
        v.descargar = lambda url, timeout=20: (_ for _ in ()).throw(OSError("403"))
        t, url, ps, err = v.leer_api("store.epicgames.com")
        self.assertEqual((ps, "403" in err), ([], True))


# ------------------------------------------------- sesiones, socio e informe

class Sesiones(Base):
    def setUp(self):
        super().setUp()
        c = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        c["entrega"] = {"modo": "hermes", "chat_id": "1"}
        c["requiere_login"] = ["pccomponentes.com"]
        v.CONFIG.write_text(json.dumps(c), encoding="utf-8")
        self.avisos = []
        self._o = (v.enviar_telegram, v.token_telegram)
        v.enviar_telegram = lambda tok, chat, texto, foto=None, teclado=None, **k: self.avisos.append(texto) or True
        v.token_telegram = lambda: "x"
        self.red.tiendas = {"mediamarkt": [{"n": "JBL Flip 6", "m": "JBL", "p": 99, "a": 149}],
                            "pccomponentes": [{"n": "Sony WH-CH520", "m": "Sony", "p": 40, "a": 60}]}

    def tearDown(self):
        v.enviar_telegram, v.token_telegram = self._o
        super().tearDown()

    def test_aviso_cuando_caduca_la_sesion(self):
        self.ronda()
        self.red.tiendas["pccomponentes"] = []  # sesión caducada: ya no ve productos
        self.ronda()
        self.assertEqual(self.avisos, [], "un fallo suelto no avisa")
        self.ronda()
        self.assertEqual(len(self.avisos), 1)
        self.assertIn("login pccomponentes.com", self.avisos[0])
        self.ronda()
        self.assertEqual(len(self.avisos), 1, "solo un aviso al día")
        self.red.tiendas["pccomponentes"] = [{"n": "Sony WH-CH520", "m": "Sony", "p": 40, "a": 60}]
        self.ronda()  # vuelve a ver productos: se reinicia el contador
        n = sqlite3.connect(v.DB).execute("SELECT COUNT(*) FROM meta WHERE k='login_fallos:pccomponentes.com'").fetchone()[0]
        self.assertEqual(n, 0)

    def test_informe_por_tienda(self):
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 79
        self.ronda()
        buf = io.StringIO()
        with redirect_stdout(buf):
            v.informe()
        texto = buf.getvalue()
        self.assertIn("mediamarkt.es", texto)
        fila = sqlite3.connect(v.DB).execute(
            "SELECT rondas, leidos, candidatos, enviados FROM stats WHERE tienda='mediamarkt.es'").fetchone()
        self.assertEqual(fila, (2, 2, 1, 1))

    def test_precio_socio(self):
        c = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        c["reglas_tienda"] = {"mediamarkt.es": {"socio": True}}
        v.CONFIG.write_text(json.dumps(c), encoding="utf-8")
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 79
        c = self.ronda()[0]
        self.assertEqual(c["socio"], 1)
        self.assertIn("(precio socio)", v.mensaje_candidato(1, {"tienda": "x", "nombre": "y", "precio": 1, "socio": True}))


class Login(unittest.TestCase):
    def test_abre_la_tienda_y_espera_a_que_cierres(self):
        tmp = Path(tempfile.mkdtemp())
        orig = {k: getattr(v, k) for k in ("DATOS", "DB", "CONFIG", "Navegador")}
        v.DATOS, v.DB, v.CONFIG = tmp, tmp / "v.db", tmp / "t.json"
        v.CONFIG.write_text(json.dumps({"logins": {"showroomprive.es": "https://www.showroomprive.es/login"}}), encoding="utf-8")
        visto = {}

        class Pagina:
            def goto(self, url, **kw):
                visto["url"] = url

        class Ctx:
            pages = [Pagina()]

            def wait_for_event(self, ev, timeout=None):
                visto["espera"] = ev

        class NavFalso:
            def __init__(self, cfg):
                visto["visible"] = cfg["navegador"]["visible"]
                self._ctx = Ctx()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                visto["cerrado"] = self._ctx is None

        v.Navegador = NavFalso
        try:
            with redirect_stdout(io.StringIO()):
                v.login("showroomprive.es")
        finally:
            for k, val in orig.items():
                setattr(v, k, val)
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(visto, {"visible": True, "url": "https://www.showroomprive.es/login", "espera": "close", "cerrado": True})

    def test_varias_tiendas_en_pestanas(self):
        tmp = Path(tempfile.mkdtemp())
        orig = {k: getattr(v, k) for k in ("DATOS", "DB", "CONFIG", "Navegador")}
        v.DATOS, v.DB, v.CONFIG = tmp, tmp / "v.db", tmp / "t.json"
        v.CONFIG.write_text(json.dumps({"logins": {"veepee.es": "https://www.veepee.es/"}}), encoding="utf-8")
        abiertas = []

        class Pagina:
            def goto(self, url, **kw):
                abiertas.append(url)

        class Ctx:
            pages = [Pagina()]

            def new_page(self):
                return Pagina()

            def wait_for_event(self, ev, timeout=None):
                pass

        class NavFalso:
            def __init__(self, cfg):
                self._ctx = Ctx()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        v.Navegador = NavFalso
        try:
            with redirect_stdout(io.StringIO()):
                v.login("veepee.es", "privalia.com")
        finally:
            for k, val in orig.items():
                setattr(v, k, val)
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(len(abiertas), 2)
        self.assertEqual(abiertas[0], "https://www.veepee.es/")


# ------------------------------------------------------------------ viajes

FEED = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
<item><title>Escapada a Oporto: vuelos + 2 noches de hotel por 79€</title><link>https://www.blog.es/oporto</link>
<media:content url="https://www.blog.es/oporto.jpg"/></item>
<item><title>Nueva York 5 noches con vuelos desde 429 euros</title><link>https://www.blog.es/nyc</link></item>
<item><title>Maldivas de lujo por 2.300€</title><link>https://www.blog.es/maldivas</link></item>
<item><title>Consejos para hacer la maleta</title><link>https://www.blog.es/maleta</link></item>
</channel></rss>"""


class Viajes(Base):
    def setUp(self):
        super().setUp()
        self.feed = FEED
        red = self.red

        def falso(url, timeout=20):
            if url.endswith("/feed/"):
                return self.feed, url
            return red(url, timeout)
        v.descargar = falso

    def test_lee_el_feed(self):
        ps = {p["nombre"]: (p["precio"], p["url"], p["imagen_url"]) for p in v.leer_rss("blog.es")}
        self.assertEqual(ps["Escapada a Oporto: vuelos + 2 noches de hotel por 79€"],
                         (79.0, "https://www.blog.es/oporto", "https://www.blog.es/oporto.jpg"))
        self.assertEqual(ps["Nueva York 5 noches con vuelos desde 429 euros"][0], 429.0)
        self.assertNotIn("Consejos para hacer la maleta", ps, "sin precio no entra")

    def test_ronda_de_viajes(self):
        c = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        c["categorias"]["viajes"]["fuentes"] = ["blog.es"]
        v.CONFIG.write_text(json.dumps(c), encoding="utf-8")
        self.assertEqual(self.ronda("viajes"), [], "primera vez: memoriza")
        self.feed = FEED.replace("</channel>", """<item><title>Budapest 4 días con vuelos y hotel por 85€</title>
            <link>https://www.blog.es/budapest</link></item><item><title>Bali 10 noches por 1.900€</title>
            <link>https://www.blog.es/bali</link></item></channel>""")
        nombres = [x["n"] for x in self.ronda("viajes")]
        self.assertEqual(nombres, ["Budapest 4 días con vuelos y hotel por 85€"], "Bali supera los 450 €")
        self.assertEqual(self.red.llamadas["idealo"], 0, "en viajes no se consulta Idealo")


# ----------------------------------------------- pausa de tiendas que fallan

class PausaPorFallos(Base):
    def test_pausa_y_vuelta(self):
        self.red.tiendas = {"mediamarkt": [{"n": "JBL Flip 6", "m": "JBL", "p": 99, "a": 149}], "pccomponentes": []}
        self.red.caidas.add("pccomponentes")
        for _ in range(3):
            self.ronda()
        con = sqlite3.connect(v.DB)
        self.assertIsNotNone(con.execute("SELECT v FROM meta WHERE k='pausa:pccomponentes.com'").fetchone())
        antes = len(self.red.pedidas)
        self.ronda()
        pedidas = self.red.pedidas[antes:]
        self.assertTrue(pedidas and all("mediamarkt" in u for u in pedidas),
                        f"solo mediamarkt (con su ?page=2): pccomponentes está en pausa -> {pedidas}")
        # pasa la pausa y la tienda vuelve a funcionar: se reinicia
        con.execute("UPDATE meta SET v='2000-01-01T00:00' WHERE k='pausa:pccomponentes.com'")
        con.commit()
        self.red.caidas.clear()
        self.ronda()
        self.assertIsNone(con.execute("SELECT v FROM meta WHERE k='fallos:pccomponentes.com'").fetchone())
        con.close()

    def test_pausa_crece(self):
        con = v.bd()
        for n in range(1, 7):
            v.registrar_fallos(con, [("x.es", "", [], "403")])
        fila = con.execute("SELECT v FROM meta WHERE k='pausa:x.es'").fetchone()[0]
        minutos = (datetime.fromisoformat(fila) - datetime.now()).total_seconds() / 60
        self.assertTrue(230 < minutos <= 240, minutos)  # 30 * 2**3
        con.close()

    def test_navegador_ocupado_no_es_fallo_de_la_tienda(self):
        # 17/11: una ventana de `login` tuvo el navegador una hora y Fnac acabó fuera 24 h.
        con = v.bd()
        for _ in range(12):
            v.registrar_fallos(con, [("fnac.es", "", [], "navegador: navegador ocupado por otra categoría")])
        self.assertIsNone(con.execute("SELECT v FROM meta WHERE k='pausa:fnac.es'").fetchone())
        self.assertIsNone(con.execute("SELECT v FROM meta WHERE k='fallos:fnac.es'").fetchone())
        con.close()


class NoSeLee(Base):
    """Instrucciones de lectura, paso 4: si una web bloquea, "no se lee" y a la siguiente, sin bucles."""

    def test_robots_o_antibot_fuera_24h_a_la_primera(self):
        con = v.bd()
        v.registrar_fallos(con, [("x.es", "", [], "robots.txt no permite leer https://x.es/")])
        hasta = datetime.fromisoformat(con.execute("SELECT v FROM meta WHERE k='pausa:x.es'").fetchone()[0])
        self.assertGreater((hasta - datetime.now()).total_seconds() / 3600, 23.9)
        self.assertIn("robots.txt", con.execute("SELECT v FROM meta WHERE k='no_se_lee:x.es'").fetchone()[0])
        v.registrar_fallos(con, [("x.es", "", [{"nombre": "a"}], None)])  # vuelve a leerse: se borra
        self.assertIsNone(con.execute("SELECT v FROM meta WHERE k='no_se_lee:x.es'").fetchone())
        con.close()

    def test_la_palabra_captcha_en_un_script_no_es_un_muro(self):
        # 17/11/2026: el RE_ANTIBOT amplio de `volcar` pisaba al estricto y MediaMarkt salía como reto por la
        # palabra "Captcha" de su código (con 13 productos en la página).
        html = '<html><script>window.captchaConfig={enabled:false};var Captcha=1;</script><p>Cloudflare CDN</p></html>'
        self.assertIsNone(v.RE_ANTIBOT.search(html))
        self.assertIsNotNone(v.RE_ANTIBOT_PISTAS.search(html), "volcar sí lo enseña como pista")
        self.assertIsNotNone(v.RE_ANTIBOT.search('<div class="cf-chl-widget">Just a moment...</div>'))

    def test_muro_antibot_en_el_listado(self):
        v.descargar = lambda url, timeout=20: ('<html><title>Just a moment...</title>'
                                               '<div class="cf-chl-widget"></div></html>', url)
        t, url, productos, error = v.leer_tienda("x.es", {"x.es": "https://x.es/ofertas"})
        self.assertEqual(productos, [])
        self.assertIn("antibot", error)


class Catalogo(Base):
    """Instrucciones de lectura, paso 3: también las categorías normales, no solo ofertas."""

    def test_categorias_de_portada(self):
        html = ('<nav><a href="/es/zapatillas-hombre">Zapatillas hombre</a><a href="/es/ropa-mujer/">Ropa mujer</a>'
                '<a href="/es/ofertas">Ofertas</a><a href="/es/mi-cuenta">Mi cuenta</a>'
                '<a href="/es/p/nike-pegasus-41-123456">Nike Pegasus 41</a><a href="https://otra.com/calzado">Calzado</a>'
                '<a href="/es/ayuda/devoluciones">Devoluciones</a><a href="/es/zapatillas-hombre#top">Zapatillas</a>'
                '<a href="/es/electrodomesticos">Electrodomésticos</a></nav>')
        self.assertEqual(v.categorias_de_portada(html, "https://www.tienda.es/"),
                         ["https://www.tienda.es/es/zapatillas-hombre", "https://www.tienda.es/es/ropa-mujer/",
                          "https://www.tienda.es/es/electrodomesticos"])

    def test_direcciones_con_tildes(self):
        # MediaMarkt (17/11/2026): sus categorías llevan tildes y eñes en la dirección y la descarga fallaba
        # con "'ascii' codec can't encode character".
        self.assertEqual(v.url_ascii("https://www.mediamarkt.es/es/category/televisión-398.html"),
                         "https://www.mediamarkt.es/es/category/televisi%C3%B3n-398.html")
        self.assertEqual(v.url_ascii("https://www.mediamarkt.es/es/category/pequeños-electrodomésticos-6201.html?page=2"),
                         "https://www.mediamarkt.es/es/category/peque%C3%B1os-electrodom%C3%A9sticos-6201.html?page=2")
        ya = "https://www.mediamarkt.es/es/category/port%C3%A1tiles-153.html?a=1&b=x%20y"
        self.assertEqual(v.url_ascii(ya), ya, "lo que ya viene codificado no se toca")
        self.assertEqual(v.url_ascii("https://t.es/búsqueda?q=cámara réflex"), "https://t.es/b%C3%BAsqueda?q=c%C3%A1mara%20r%C3%A9flex")
        from urllib.request import Request
        Request(v.url_ascii("https://www.mediamarkt.es/es/category/televisión-398.html")).full_url.encode("ascii")

    def test_catalogo_sin_repetidas_y_con_subcategorias(self):
        # MediaMarkt (17/11/2026): la portada daba 12, con "electrodomésticos-553" dos veces (con tildes y
        # codificada), y faltaban auriculares, consolas o tablets, que cuelgan de los índices.
        portada = ('<a href="/es/category/electrodomésticos-553.html">Electrodomésticos</a>'
                   '<a href="/es/category/electrodom%C3%A9sticos-553.html">Electrodomésticos</a>'
                   '<a href="/es/category/informática-9.html">Informática</a>')
        self.assertEqual(len(v.categorias_de_portada(portada, "https://www.mediamarkt.es/")), 2, "sin la repetida")
        indice = ('<a href="/es/category/portátiles-153.html">Portátiles</a><a href="/es/category/tablets-300.html">Tablets</a>'
                  '<a href="/es/category/informática-9.html">Informática</a>')

        def red(url, timeout=20, reintentos=1):
            return (indice if "informática-9" in v.unquote(url) else portada if url.endswith(".es/") else "<p></p>"), url
        v.descargar = red
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["catalogo_por_tienda"] = {"mediamarkt.es": {"niveles": 2, "max": 40}}
        cfg["filtros_codigo"]["respetar_robots"] = False  # sin red real no hay robots.txt que consultar
        v._ROBOTS_CFG[:] = [None, None]
        v.CONFIG.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            v.catalogo("mediamarkt.es")
        rutas = [v.unquote(v.urlparse(u).path) for u in v.urls_catalogo()["mediamarkt.es"]]
        self.assertEqual(rutas, ["/es/category/electrodomésticos-553.html", "/es/category/informática-9.html",
                                 "/es/category/portátiles-153.html", "/es/category/tablets-300.html"])
        real = json.loads((CONF / "tiendas.json").read_text(encoding="utf-8"))["catalogo_por_tienda"]["mediamarkt.es"]
        self.assertEqual((real["niveles"], real["max"]), (2, 40))

    def test_lee_una_categoria_por_turno(self):
        self.red.tiendas = {"mediamarkt": [{"n": "JBL Flip 6", "m": "JBL", "p": 99, "a": 149}], "pccomponentes": []}
        red, catalogo = self.red, [{"n": "JBL Tune 520BT", "m": "JBL", "p": 30, "a": 60}]

        def falso(url, timeout=20):
            if "/category/" in url:
                red.pedidas.append(url)
                return (tienda_html(catalogo), url) if "page=" not in url else ("<html></html>", url)
            return red(url, timeout)
        v.descargar = falso
        v.guardar_catalogo({"mediamarkt.es": ["https://www.mediamarkt.es/es/category/auriculares",
                                              "https://www.mediamarkt.es/es/category/televisores"]})
        self.ronda()  # siembra ofertas y la primera categoría
        self.assertIn("https://www.mediamarkt.es/es/category/auriculares", self.red.pedidas)
        antes = len(self.red.pedidas)
        self.ronda()
        self.assertFalse([u for u in self.red.pedidas[antes:] if "/category/" in u], "como mucho una por hora")
        con = sqlite3.connect(v.DB)
        con.execute("UPDATE meta SET v='2000-01-01T00:00' WHERE k='cat_ultimo:mediamarkt.es'")
        con.execute("UPDATE meta SET v='0' WHERE k='cat_turno:mediamarkt.es'")
        con.commit()
        con.close()
        catalogo.append({"n": "JBL Live 770NC", "m": "JBL", "p": 50.0, "a": 150.0})
        c = self.ronda()
        self.assertEqual([x["n"] for x in c], ["JBL Live 770NC"], "un producto nuevo de la categoría es candidato")

    def test_la_ronda_lee_las_paginas_adicionales(self):
        self.red.tiendas = {"mediamarkt": [{"n": "JBL Flip 6", "m": "JBL", "p": 99, "a": 149}], "pccomponentes": []}
        red, campana = self.red, [{"n": "JBL Tune 520BT", "m": "JBL", "p": 30, "a": 60}]

        def falso(url, timeout=20):
            if "/campana/" in url:
                red.pedidas.append(url)
                return (tienda_html(campana), url) if "page=" not in url else ("<html></html>", url)
            return red(url, timeout)
        v.descargar = falso
        v.DATOS.mkdir(exist_ok=True)
        v.URLS.write_text(json.dumps({"mediamarkt.es+": ["https://www.mediamarkt.es/es/campana/black"]}), encoding="utf-8")
        self.ronda()  # siembra la principal y la campaña
        self.assertIn("https://www.mediamarkt.es/es/campana/black", self.red.pedidas)
        con = sqlite3.connect(v.DB)
        con.execute("UPDATE meta SET v='2000-01-01T00:00' WHERE k='extra_ultimo:mediamarkt.es'")
        con.commit()
        con.close()
        campana.append({"n": "JBL Live 770NC", "m": "JBL", "p": 50.0, "a": 150.0})
        c = self.ronda()
        self.assertEqual([x["n"] for x in c], ["JBL Live 770NC"], "un producto nuevo de la campaña es candidato")

    def test_varias_por_ronda_y_turno_circular(self):
        con = v.bd()
        v.guardar_catalogo({"a.es": ["u1", "u2", "u3", "u4", "u5"]})
        cfg = {"catalogo_por_tienda": {"a.es": {"cada_min": 0, "por_ronda": 2}}}
        vistos = [u for _ in range(3) for _, u in v.catalogo_turno(con, cfg, ["a.es"])]
        self.assertEqual(vistos, ["u1", "u2", "u3", "u4", "u5", "u1"])
        con.close()

    def test_familias_y_peso_por_demanda(self):
        # Zeta (17/11/2026): no pesa lo mismo una categoría de móviles que una de impresoras.
        for texto, fam in (("Xiaomi Redmi Note 14 Pro 5G 256GB", "moviles"), ("/es/category/smartphones-263.html", "moviles"),
                           ("https://pccomtracker.com/categoria/impresoras-laser", "impresoras"),
                           ("HP DeskJet 2820e Impresora multifunción", "impresoras"),
                           ("Zapatillas adidas Gazelle", "calzado"), ("LEGO Technic 42171", "lego"),
                           ("Mando DualSense PS5 Galactic Purple", "consolas"), ("/portatiles-gaming", "portatiles")):
            self.assertEqual(v.familia_de(texto), fam, texto)
        self.assertIsNone(v.familia_de("Tarjeta regalo 50 EUR"))
        self.assertEqual(v.familia_de("Juego PS5 Astro Bot"), "consolas")
        self.assertEqual(v.familia_de("Juegos gratis en Epic"), "videojuegos")
        self.assertNotEqual(v.familia_de("Juego de sartenes Tefal Ingenio"), "videojuegos")
        self.assertNotEqual(v.familia_de("Juego de mancuernas ajustables 10 kg"), "videojuegos")
        con = v.bd()
        self.assertIsNone(v.pesos_demanda(con, {}), "sin datos en la copia de Chollometro: todo pesa igual")
        self.assertEqual(v.peso_de(None, "Impresora HP"), 1.0)
        hace = lambda d: int(time.time() - d * 86400)
        filas, n = [], 0
        for titulo, total, calientes in (("Xiaomi Redmi Note 14 smartphone", 300, 180), ("Impresora HP DeskJet", 100, 10),
                                         ("Tarjeta regalo", 200, 50)):
            for i in range(total):
                n += 1
                filas.append((n, f"{titulo} {i}", 10.0, "X", "x.es", 500.0 if i < calientes else 20.0, hace(i % 60), 0, ""))
        con.executemany("INSERT INTO cm (id, titulo, precio, tienda, host, temp, publicado, caducado, visto) VALUES (?,?,?,?,?,?,?,?,?)", filas)
        con.commit()
        pesos = v.pesos_demanda(con, {})
        # Media: 240 de 600 = 40 %. Móviles 60 % -> 1,5. Impresoras 10 % -> 0,25, que se queda en el mínimo 0,3.
        self.assertEqual(pesos["moviles"], 1.5)
        self.assertEqual(pesos["impresoras"], v.PESO_MIN)
        self.assertEqual(v.peso_de(pesos, "Samsung Galaxy S25 smartphone"), 1.5)
        self.assertEqual(v.peso_de(pesos, "Tarjeta regalo"), 1.0, "sin familia: neutro")
        self.assertIsNone(v.pesos_demanda(con, {"filtros_codigo": {"peso_por_demanda": False}}), "se apaga")
        # Catálogo: móviles sale más veces por vuelta que impresoras, y ninguna se queda sin leer.
        lista = ["https://t.es/c/impresoras", "https://t.es/c/smartphones", "https://t.es/c/varios"]
        orden = v.orden_catalogo(lista, pesos)
        self.assertEqual(orden.count("https://t.es/c/impresoras"), 1)
        self.assertEqual(orden.count("https://t.es/c/smartphones"), 4, "tope de 4 turnos por vuelta")
        self.assertEqual(orden.count("https://t.es/c/varios"), 3)
        self.assertEqual(v.orden_catalogo(lista, None), lista, "sin pesos, el turno circular de siempre")
        v.guardar_catalogo({"t.es": lista})
        cfg = {"catalogo_por_tienda": {"t.es": {"cada_min": 0, "por_ronda": 2}}}
        vuelta = [u for _ in range(8) for _, u in v.catalogo_turno(con, cfg, ["t.es"])]
        self.assertGreater(vuelta.count(lista[1]), vuelta.count(lista[0]) * 2)
        self.assertIn(lista[0], vuelta)
        for a, b in zip(vuelta[::2], vuelta[1::2]):
            self.assertNotEqual(a, b, "la misma categoría no sale dos veces en una ronda")
        con.close()

    def test_paginas_de_ofertas_adicionales(self):
        con = v.bd()
        urls = {"a.es": "https://a.es/ofertas", "a.es+": ["https://a.es/black", "https://a.es/outlet"], "b.es": "https://b.es/"}
        self.assertEqual(v.ofertas_extra_turno(con, {}, urls, ["a.es", "b.es"]),
                         [("a.es", "https://a.es/black"), ("a.es", "https://a.es/outlet")])
        self.assertEqual(v.ofertas_extra_turno(con, {}, urls, ["a.es", "b.es"]), [], "como mucho cada 15 min por tienda")
        con.execute("UPDATE meta SET v='2000-01-01T00:00' WHERE k='extra_ultimo:a.es'")
        self.assertEqual(len(v.ofertas_extra_turno(con, {}, urls, ["a.es"])), 2)
        # Campaña terminada: la página daba 120 productos y ahora da 3. Avisa una vez al día.
        self.assertIsNone(v.pagina_caida(con, "a.es|u", 120))
        self.assertIsNone(v.pagina_caida(con, "a.es|u", 90), "baja, pero no se hunde")
        self.assertEqual(v.pagina_caida(con, "a.es|u", 3), 120)
        self.assertIsNone(v.pagina_caida(con, "a.es|u", 3), "una vez al día")
        self.assertIsNone(v.pagina_caida(con, "b.es|u", 5))
        self.assertIsNone(v.pagina_caida(con, "b.es|u", 0), "páginas pequeñas (menos de 10) no avisan")
        con.close()

    def test_enlace_del_comparador_sin_visitar_goto(self):
        # PCComTracker enlaza a PcComponentes por /goto/ (prohibido en su robots.txt): la URL real va dentro.
        href = "/goto/pccomponentes?url=https%3A%2F%2Fwww.pccomponentes.com%2Fjbl-flip-6&amp;source=web&amp;productId=5"
        self.assertEqual(v.enlace_real(v.unescape(href)), "https://www.pccomponentes.com/jbl-flip-6")
        self.assertEqual(v.enlace_real("https://x.es/a"), "https://x.es/a")
        html = ('<a href="/producto/jbl-flip-6-5">JBL Flip 6</a>'
                f'<a href="{href}" aria-label="Ver JBL Flip 6 en PcComponentes por 89,99 €, mejor precio">ir</a>')
        ps = v.via_comparador(html, [], "pccomponentes.com", "https://pccomtracker.com/black-friday")
        self.assertEqual([(p["url"], p["precio"], p["historico_url"]) for p in ps],
                         [("https://www.pccomponentes.com/jbl-flip-6", 89.99, "https://pccomtracker.com/producto/jbl-flip-6-5")])
        # Tarjeta con el nombre en <h3> (subcategorías del comparador).
        tarjeta = ('<a class="card" href="/producto/radio-blow-x-pro-20110331"><p>Blow</p><h3>Radio coche Blow X-PRO 1 '
                   'Bluetooth</h3><span>20.99<!-- -->€</span><span>PCComponentes</span></a>') * 1
        otras = "".join(f'<a href="/producto/otro-{i}-2011033{i}"><h3>Altavoz Blow modelo {i}</h3><span>{30 + i}€</span></a>'
                        for i in range(3))
        nombres = [p["name"] for p in v.tarjetas_html(tarjeta + otras, "https://pccomtracker.com/categoria/sonido/altavoz")]
        self.assertIn("Radio coche Blow X-PRO 1 Bluetooth", nombres)

    def test_categoria_que_no_existe_sale_de_la_lista(self):
        self.red.tiendas = {"mediamarkt": [{"n": "JBL Flip 6", "m": "JBL", "p": 99, "a": 149}], "pccomponentes": []}
        red = self.red

        def falso(url, timeout=20):
            if "/category/" in url:
                raise OSError("HTTP Error 404: Not Found")
            return red(url, timeout)
        v.descargar = falso
        v.guardar_catalogo({"mediamarkt.es": ["https://www.mediamarkt.es/es/category/vieja"]})
        self.ronda()
        self.assertEqual(v.urls_catalogo(), {"mediamarkt.es": []})
        con = sqlite3.connect(v.DB)
        self.assertIsNone(con.execute("SELECT v FROM meta WHERE k='pausa:mediamarkt.es'").fetchone(),
                          "la tienda sigue leyéndose")
        con.close()

    def test_vacia_en_descarga_pasa_a_chrome(self):
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"]["navegador_si_vacia"] = True
        v.CONFIG.write_text(json.dumps(cfg), encoding="utf-8")
        self.red.tiendas = {"mediamarkt": [{"n": "JBL Flip 6", "m": "JBL", "p": 99, "a": 149}], "pccomponentes": []}
        con_chrome = []
        orig = v.leer_con_pestanas
        v.leer_con_pestanas = lambda cfg, trabajos: con_chrome.extend(t for t, _, _ in trabajos) or [
            (t, u, [], None) for t, u, _ in trabajos]
        try:
            self.ronda()
            self.ronda()
            self.assertEqual(con_chrome, [], "aún no")
            antes = len(self.red.pedidas)
            self.ronda()
        finally:
            v.leer_con_pestanas = orig
        self.assertEqual(con_chrome, ["pccomponentes.com"])
        self.assertFalse([u for u in self.red.pedidas[antes:] if "pccomponentes" in u], "ya no se descarga")


class IdealoRutas(unittest.TestCase):
    def test_prueba_la_segunda_ruta_y_la_recuerda(self):
        pedidas = []

        def falso(url, timeout=20):
            pedidas.append(url)
            if "resultados.html" in url:
                return "<html>nada</html>", url
            return ('<script type="application/ld+json">{"@type":"Product","name":"JBL Flip 6",'
                    '"offers":{"lowPrice":"85,00"}}</script>'), url
        orig = (v.descargar, v.time.sleep, v._IDEALO_BUENA[0], v.DATOS, v._IDEALO_BLOQUEADO[0])
        v.descargar, v.time.sleep = falso, (lambda s: None)
        v._IDEALO_BUENA[0] = None
        # Sin carpeta propia leía data\idealo_pausa.txt de producción y se iba a PrecioReal.
        v.DATOS, v._IDEALO_BLOQUEADO[0] = Path(tempfile.mkdtemp()), False
        try:
            p = {"nombre": "JBL Flip 6", "ean": "6925281993145"}
            self.assertEqual(v.idealo_red(p), 85.0)
            self.assertEqual(len(pedidas), 2)
            pedidas.clear()
            self.assertEqual(v.idealo_red(p), 85.0)
            self.assertEqual(len(pedidas), 1, "la siguiente vez va directa a la ruta buena")
        finally:
            v.descargar, v.time.sleep, v._IDEALO_BUENA[0], v.DATOS, v._IDEALO_BLOQUEADO[0] = orig


# --------------------------------------------- dossier más completo y fuentes nuevas

class DatosPorCodigo(unittest.TestCase):
    def test_cupon_vigencia_y_especificaciones(self):
        ficha = ('<p>Usa el código BLACK15 y ahorra un 15 %. Oferta válida hasta el 12/11 a las 23:59.</p>'
                 '<table><tr><th>Potencia</th><td>1.700 W</td></tr><tr><th>Capacidad</th><td>5,5 L</td></tr>'
                 '<tr><th>Programas</th><td>8</td></tr></table>'
                 '<script type="application/ld+json">' + json.dumps({"@type": "Product", "name": "Cosori Dual Blaze",
                 "offers": {"price": 99}, "additionalProperty": [{"name": "Color", "value": "Negro"}]}) + "</script>")
        f = v.analizar_ficha(ficha, "https://t.es/p")
        self.assertEqual(f["cupones"], ["BLACK15"])
        self.assertIn("12/11", f["vigencia"])
        self.assertIn("Potencia: 1.700 W", f["specs"])
        self.assertIn("Color: Negro", f["specs"])

    def test_sin_falsos_cupones(self):
        self.assertEqual(v.cupones_en("Introduce tu código postal 23660 para ver el envío"), [])

    def test_minimo_en_comparador(self):
        self.assertEqual(v.minimo_en_texto("<div>Precio mínimo histórico: <b>79,90 €</b></div>"), 79.9)
        self.assertIsNone(v.minimo_en_texto("<div>Sin datos</div>"))

    def test_historico_en_dos_pasos(self):
        paginas = {
            "https://comp.es/buscar?q=JBL+Flip+6": '<a href="/p/jbl-flip-6">JBL Flip 6 altavoz</a><a href="/p/otro">Sony XB13</a>',
            "https://comp.es/p/jbl-flip-6": "<p>Mínimo histórico 84,99 €</p>",
        }
        orig = v.descargar
        v.descargar = lambda url, timeout=20: (paginas[url], url)
        try:
            self.assertEqual(v.historico_generico("https://comp.es/buscar?q={q}", "JBL Flip 6"),
                             (84.99, "https://comp.es/p/jbl-flip-6"))
            cfg = {"historicos": {"defecto": "comp", "busquedas": {"comp": "https://comp.es/buscar?q={q}"}}}
            h = v.historico(cfg, {"n": "JBL Flip 6", "t": "mediamarkt.es", "p": 79})
            self.assertEqual((h["minimo"], h["fuente"], h["es_minimo"]), (84.99, "comp", True))
        finally:
            v.descargar = orig

    def test_verificador_por_tienda_y_categoria(self):
        c = json.loads((CONF / "tiendas.json").read_text(encoding="utf-8"))
        self.assertEqual(v.verificador_de(c, {"t": "pccomponentes.com"}), "pccomtracker")
        self.assertEqual(v.verificador_de(c, {"t": "store.steampowered.com"}), "isthereanydeal")
        self.assertEqual(v.verificador_de(c, {"t": "druni.es"}), "perfumon")
        self.assertEqual(v.verificador_de(c, {"t": "movistar.es", "cat": "moviles"}), "kimovil")
        self.assertEqual(v.verificador_de(c, {"t": "ikea.com/es"}), "precioreal")

    def test_juegos_gratis(self):
        datos = json.dumps({"data": {"children": [
            {"data": {"title": "[Steam] (Game) Juego Regalado", "url": "https://store.steampowered.com/app/1/"}},
            {"data": {"title": "[Epic] (Game) Otro Regalo - Free until 13/11", "url": "https://store.epicgames.com/p/otro"}},
            {"data": {"title": "[Steam] (Beta) Prueba", "url": "https://store.steampowered.com/app/2/"}},
            {"data": {"title": "[GOG] (Game) Caducado", "url": "https://gog.com/x", "link_flair_text": "Expired"}}]}})
        orig = v.descargar
        v.descargar = lambda url, timeout=20: (datos, url)
        try:
            ps = v.juegos_gratis()
        finally:
            v.descargar = orig
        self.assertEqual([(p["nombre"], p["precio"], p["gratis"]) for p in ps],
                         [("Juego Regalado (Steam)", 0.0, True), ("Otro Regalo (Epic)", 0.0, True)])

    def test_canal_telegram(self):
        html = ('<div class="tgme_widget_message_text js-message_text">🔥 Xiaomi Redmi Buds 6 Pro por 34,99€ (antes 79,99€)<br/>'
                '<a href="https://www.mediamarkt.es/es/product/_buds-123">https://www.mediamarkt.es/es/product/_buds-123</a></div>'
                '<div class="tgme_widget_message_text">Buenos días a todos</div>')
        orig = v.descargar
        v.descargar = lambda url, timeout=20: (html, url)
        try:
            ps = v.leer_telegram("canalchollos")
        finally:
            v.descargar = orig
        self.assertEqual(len(ps), 1)
        self.assertEqual((ps[0]["precio"], ps[0]["precio_antes"]), (34.99, 79.99))
        self.assertTrue(ps[0]["nombre"].startswith("Xiaomi Redmi Buds 6 Pro"))
        self.assertEqual(ps[0]["url"], "https://www.mediamarkt.es/es/product/_buds-123")


class ErrorDePrecio(Base):
    def test_caida_fuerte_sin_precio_tachado(self):
        # Sin precio anterior en la web y sin Idealo: normalmente no pasaría.
        self.red.tiendas = {"mediamarkt": [{"n": "Philips Airfryer XXL", "m": "Philips", "p": 199}], "pccomponentes": []}
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 59  # -70 %: posible error de precio
        c = self.ronda()
        self.assertEqual([x["n"] for x in c], ["Philips Airfryer XXL"])
        self.assertEqual(c[0]["err"], 1)
        texto = v.mensaje_candidato(1, {"tienda": "mediamarkt.es", "nombre": "x", "precio": 59,
                                        "error_precio": True, "_min_previo": 199})
        self.assertIn("posible error de precio (antes 199 €)", texto)

    def test_tienda_pequena_no_cuenta_como_error(self):
        """Chollometro no acepta errores de precio de tiendas pequeñas (18/11/2026)."""
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["filtros_codigo"]["error_precio_min_calientes"] = 100
        v.CONFIG.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        v._GRANDES.clear()
        self.red.tiendas = {"mediamarkt": [{"n": "Philips Airfryer XXL", "m": "Philips", "p": 199}], "pccomponentes": []}
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 59
        self.assertEqual(self.ronda(), [], "sin hilos calientes de la tienda en la copia: no es error de precio")

    def test_bajada_normal_no_es_error(self):
        self.red.tiendas = {"mediamarkt": [{"n": "Philips Airfryer XXL", "m": "Philips", "p": 199}], "pccomponentes": []}
        self.ronda()
        self.red.tiendas["mediamarkt"][0]["p"] = 179  # -10 %, sin tachado ni Idealo: no pasa
        self.assertEqual(self.ronda(), [])


# ------------------------------------------------------- navegador real

try:
    import playwright  # noqa: F401
    HAY_PW = True
except ImportError:
    HAY_PW = False


@unittest.skipUnless(HAY_PW, "Playwright no instalado (fase 2)")
class NavegadorReal(unittest.TestCase):
    """Página que carga los productos con JavaScript desde una API, como PcComponentes o Coolmod."""

    def setUp(self):
        api = json.dumps({"results": [
            {"productName": "Logitech MX Master 3S", "promotionalPrice": 69.99, "originalPrice": 119.99,
             "slug": "/logitech-mx-master-3s"},
            {"productName": "Kingston NV3 1TB", "promotionalPrice": 54.9, "originalPrice": 79.9,
             "slug": "/kingston-nv3"}]}).encode()
        pagina = rb"""<html><body><div id=app>Cargando...</div><script>
          fetch('/api/productos').then(r => r.json()).then(d => {
            document.getElementById('app').innerHTML = d.results.map(p =>
              '<div>' + p.productName + ' ' + p.promotionalPrice + ' \u20ac</div>').join('');
          });</script></body></html>"""

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/scroll"):
                    if getattr(self, "retraso", 0):
                        time.sleep(self.retraso)  # tienda lenta
                    cuerpo, tipo = self.pagina, "text/html"
                else:
                    cuerpo, tipo = (api, "application/json") if self.path.startswith("/api") else (pagina, "text/html")
                self.send_response(200)
                self.send_header("Content-Type", tipo)
                self.end_headers()
                self.wfile.write(cuerpo)

            def log_message(self, *a):
                pass

        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}/ofertas"
        self.tmp = Path(tempfile.mkdtemp())
        self._datos, v.DATOS = v.DATOS, self.tmp

    def tearDown(self):
        self.srv.shutdown()
        v.DATOS = self._datos
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scroll_infinito(self):
        pagina = b"""<html><body style="margin:0"><div id=l></div><script>
          let n = 0; const l = document.getElementById('l');
          function carga() { if (n >= 30) return; for (let i = 0; i < 10; i++, n++) {
              const d = document.createElement('div'); d.style.height = '200px';
              d.innerHTML = '<span itemscope itemtype="https://schema.org/Product"><b itemprop="name">Producto Philips ' + n +
                '</b><span itemprop="offers" itemscope><meta itemprop="price" content="' + (10 + n) + '"></span></span>';
              l.appendChild(d); } }
          carga(); window.addEventListener('scroll', () => {
            if (innerHeight + scrollY >= document.body.scrollHeight - 50) setTimeout(carga, 100); });
          </script></body></html>"""
        self.srv.RequestHandlerClass.pagina = pagina
        with v.Navegador({"navegador": {"espera_ms": 900, "scroll_max": 10}}) as nav:
            html, final, _ = nav.leer(self.url.replace("/ofertas", "/scroll"))
        self.assertEqual(len(v.extraer(html, final)), 30)

    def test_descarga_normal_no_ve_nada(self):
        self.assertEqual(v.extraer(*v.descargar(self.url)), [])

    def test_navegador_captura_la_api(self):
        with v.Navegador({"navegador": {"espera_ms": 300}}) as nav:
            t, url, productos, error = v.leer_tienda("prueba.es", {"prueba.es": self.url}, None, nav)
        self.assertIsNone(error)
        ps = {p["nombre"]: p for p in productos}
        self.assertEqual(set(ps), {"Logitech MX Master 3S", "Kingston NV3 1TB"})
        self.assertEqual((ps["Logitech MX Master 3S"]["precio"], ps["Logitech MX Master 3S"]["precio_antes"]),
                         (69.99, 119.99))

    def test_sin_playwright_no_rompe_la_ronda(self):
        orig = v.leer_con_pestanas

        def falla(cfg, trabajos):
            raise ImportError("no playwright")
        v.leer_con_pestanas = falla
        try:
            lect = v.leer_todas([], ["pccomponentes.com"], {}, "tecnologia", {})
        finally:
            v.leer_con_pestanas = orig
        self.assertEqual(lect[0][3], "falta playwright (fase 2)")

    def test_pestanas_en_paralelo(self):
        import time as _t
        lento = b"""<html><body><div id=a></div><script>
          setTimeout(() => { document.getElementById('a').innerHTML =
            '<span itemscope itemtype="https://schema.org/Product"><b itemprop="name">Philips Hue ' + location.pathname +
            '</b><span itemprop="offers" itemscope><meta itemprop="price" content="20"></span></span>'; }, 100);
          </script><img src="/foto.jpg"></body></html>"""
        self.srv.RequestHandlerClass.pagina = lento
        self.srv.RequestHandlerClass.retraso = 1.5
        base = self.url.replace("/ofertas", "")
        trabajos = [(f"t{i}.es", f"{base}/scroll{i}", 1) for i in range(4)]
        cfg = {"navegador": {"pestanas": 4, "espera_ms": 600, "espera_red_ms": 2000, "scroll_max": 1}}
        ini = _t.monotonic()
        res = v.leer_con_pestanas(cfg, trabajos)
        paralelo = _t.monotonic() - ini
        self.assertEqual([len(r[2]) for r in res], [1, 1, 1, 1])
        self.assertEqual([r[3] for r in res], [None] * 4)
        cfg["navegador"]["pestanas"] = 1
        ini = _t.monotonic()
        v.leer_con_pestanas(cfg, trabajos)
        secuencial = _t.monotonic() - ini
        self.assertLess(paralelo, secuencial * 0.75, f"paralelo {paralelo:.1f}s vs una a una {secuencial:.1f}s")


class AjusteBasura16Nov(unittest.TestCase):
    """Casos reales de avisos malos del 15-16/11/2026."""

    def test_precio_sospechoso(self):
        # Secadora AEG leída a 9 € con el mercado a 738,90 €; DJI Power 1000 Mini a 49 € con 577,95 €.
        self.assertTrue(v.precio_sospechoso({"precio": 9.0}, 738.9))
        self.assertTrue(v.precio_sospechoso({"precio": 49.0}, 577.95))
        # Localizador a 1 € sin mercado pero con mínimo histórico de PCComTracker.
        self.assertTrue(v.precio_sospechoso({"precio": 1.0, "_hist": {"minimo": 14.9}}, None))
        # Una bajada fuerte pero creíble sí pasa (-45 % sobre mercado).
        self.assertFalse(v.precio_sospechoso({"precio": 55.0}, 100.0))
        self.assertFalse(v.precio_sospechoso({"precio": 5.0}, None), "sin referencia no se juzga")

    def test_sin_precio_oficial(self):
        n = "AEG Secadora de bomba de calor AEG Series 8000 TR839T4PBC 9 kg Sin precio oficial de PCComponentes"
        self.assertTrue(v.RE_SIN_PRECIO_OFICIAL.search(n))
        self.assertFalse(v.RE_SIN_PRECIO_OFICIAL.search("Apple AirPods Max Naranja"))

    def test_infantil(self):
        for n in ("Zapatos Niña Zapatillas bajas Puma Smash 3.0 Hearty V Inf Blanco / Rosa",
                  "Zapatillas bajas Puma Puma Caven III AC+ PS Blanco / Rosa",
                  "Nike Air Max 90 - Bebés", "Camiseta infantil Adidas", "Nike Revolution 7 TD",
                  "Adidas Gazelle Bold júnior rosa", "Nike Dunk Low GS", "Sujetador teen"):  # junior fuera desde el 17/11
            self.assertTrue(v.RE_INFANTIL.search(n), n)
        for n in ("New Balance 550 blancas", "Sonic Racing CrossWorlds - PS5",
                  "Asics Gel-Rocket 12 Hombre"):
            self.assertFalse(v.RE_INFANTIL.search(n), n)

    def test_demanda_exige_mismo_producto_y_precio_creible(self):
        # AirPods Max a 130 € contra un hilo caliente a 378,99 €: demasiado barato para ser lo mismo.
        p = {"precio": 130.0, "_cm_hot": {"precio": 378.99, "caducado": True, "temp": 2817}}
        self.assertFalse(v.demanda_probada_ok(p))
        p = {"precio": 34.99, "_cm_hot": {"precio": 36.0, "caducado": True, "temp": 850}}
        self.assertTrue(v.demanda_probada_ok(p))
        self.assertFalse(v.mismo_producto_hot("Monitor Philips 27E1N1800AE 27\" UltraHD 4K",
                                              "Monitor Samsung Odyssey G7 32\" 4K 144 Hz"))
        self.assertTrue(v.mismo_producto_hot("Monitor Philips 27E1N1800AE 27\" UltraHD 4K IPS",
                                             "Philips 27E1N1800AE monitor 4K"))
        self.assertTrue(v.mismo_producto_hot("JBL Charge 5 altavoz", "JBL Charge 5"))
        self.assertFalse(v.mismo_producto_hot("JBL Charge 5 altavoz", "JBL Charge 6"))

    def test_repetido_reciente(self):
        recientes = [("amazon.es", "PlayStation 5 - Mando inalámbrico DualSense Starlight Blue - Exclusivo para PS5", 54.99)]
        p = {"tienda": "amazon.es", "precio": 54.9,
             "nombre": "PlayStation 5 - Mando inalámbrico DualSense Galactic Purple - Exclusivo para PS5"}
        self.assertTrue(v.repetido_reciente(p, recientes))
        self.assertFalse(v.repetido_reciente({**p, "tienda": "mediamarkt.es"}, recientes), "otra tienda sí")
        self.assertFalse(v.repetido_reciente({**p, "precio": 44.99}, recientes), "más barato sí")
        self.assertFalse(v.repetido_reciente({**p, "nombre": "Xbox Wireless Controller Carbon Black"}, recientes))
        jbl = [("mediamarkt.es", "JBL Charge 4", 50.0)]
        self.assertFalse(v.repetido_reciente({"tienda": "mediamarkt.es", "precio": 50.0, "nombre": "JBL Charge 5"}, jbl))


class MiraviaAliExpress(Base):
    """17/11/2026: Miravia y AliExpress con un Chrome visible normal (autorizado por Zeta). Lectores propios
    de sus tarjetas y pausa fija de 30 min si sale reto."""

    MIRAVIA = ('<a href="//www.miravia.es/p/i1374705818185578-s6188116389412714.html?clickTrackInfo=item_pool_ids'
               '%3A1%3Bitem_discount_price%3A3793%3Bitem_id%3A1374705818185578%3Bitem_price%3A9893%3Bmt%3Ax" '
               'class="w"><div><img src="//img/a.avif" alt="ARIEL Pods Original 132 Lavados"></div><div><span>37'
               '</span><span>,93</span><span>€</span></div></a>'
               '<a href="//www.miravia.es/p/i2-s3.html?" class="w"><img alt="Gillette Fusion5 12 recambios">'
               '<span>23</span><span>,99</span><span>€</span></a>')
    ALI = ('{"discount":10,"title":"Báscula de café TIMEMORE \\"Basic 2\\"","stocks":3,"trace":"{\\"utLogMap\\":'
           '\\"%7B%22salePriceAmount%22%3A%225799%22%2C%22originPriceAmount%22%3A%225883%22%2C%22currency%22%3A'
           '%22EUR%22%7D\\"}","reviews":713,"id":1005004983617928,"brandName":"TIMEMORE","reviewStar":"4.8",'
           '"value":"Elegible con cupones","detailUrl":"//es.aliexpress.com/item/1005004983617928.html?pvid=x"}')

    def test_tarjetas_de_miravia(self):
        ps = v.extraer(self.MIRAVIA, "https://www.miravia.es/ch/pg/ofertas")
        self.assertEqual([(p["nombre"], p["precio"], p["precio_antes"]) for p in ps],
                         [("ARIEL Pods Original 132 Lavados", 37.93, 98.93), ("Gillette Fusion5 12 recambios", 23.99, None)])
        self.assertEqual(ps[0]["url"], "https://www.miravia.es/p/i1374705818185578-s6188116389412714.html")

    def test_productos_de_aliexpress(self):
        ps = v.extraer(self.ALI, "https://es.aliexpress.com/ssr/300002660/Deals-HomePage")
        self.assertEqual(len(ps), 1)
        p = ps[0]
        self.assertEqual((p["nombre"], p["precio"], p["precio_antes"], p["marca"], p["resenas"], p["valoracion"]),
                         ('Báscula de café TIMEMORE "Basic 2"', 57.99, 58.83, "TIMEMORE", 713, 96))
        self.assertEqual(p["url"], "https://es.aliexpress.com/item/1005004983617928.html")
        self.assertTrue(p["cupon_elegible"])

    def test_reto_pausa_fija_30_min_sin_doblar(self):
        con = v.bd()
        for _ in range(5):
            v.registrar_fallos(con, [("aliexpress.com", "", [], "reto antibot (no se resuelve)")],
                               {"aliexpress.com": 30})
            hasta = datetime.fromisoformat(con.execute("SELECT v FROM meta WHERE k='pausa:aliexpress.com'").fetchone()[0])
            self.assertTrue(28 < (hasta - datetime.now()).total_seconds() / 60 <= 30)
        self.assertIsNone(con.execute("SELECT v FROM meta WHERE k='no_se_lee:aliexpress.com'").fetchone(),
                          "no se aparta 24 h como las demás")
        v.registrar_fallos(con, [("miravia.es", "", [], "HTTP 500 en navegador")], {"miravia.es": 30})
        self.assertIsNone(con.execute("SELECT v FROM meta WHERE k='pausa:miravia.es'").fetchone(),
                          "un fallo normal suelto no la para")
        con.close()

    def test_van_al_chrome_normal_y_no_al_perfil(self):
        cfg = json.loads(v.CONFIG.read_text(encoding="utf-8"))
        cfg["navegador_normal"] = {"tiendas": ["miravia.es"], "paginas": 1, "pausa_min": 30}
        perfil, normal = [], []
        orig = v.leer_con_pestanas, v.leer_navegador_normal
        v.leer_con_pestanas = lambda c, trabajos: perfil.extend(trabajos) or [(t, u, [], None) for t, u, _ in trabajos]
        v.leer_navegador_normal = lambda c, trabajos: normal.extend(trabajos) or [(t, u, [], None) for t, u, _ in trabajos]
        try:
            res = v.leer_todas([], ["miravia.es", "coolmod.com"], {}, "generalistas", cfg)
        finally:
            v.leer_con_pestanas, v.leer_navegador_normal = orig
        self.assertEqual([t for t, _, _ in perfil], ["coolmod.com"])
        self.assertEqual([(t, p) for t, _, p in normal], [("miravia.es", 1)])
        self.assertEqual(sorted(r[0] for r in res), ["coolmod.com", "miravia.es"])

    def test_private_sport_shop(self):
        """18/11/2026: productos y precios llegan en respuestas distintas; se juntan por id."""
        datos = [{"data": [{"productId": 5207575, "productName": "Zapatillas de running Alphafly 3", "brandName": "Nike"},
                           {"productId": 1, "productName": "Sin precio", "brandName": "X"}]},
                 [{"id": 5207575, "current": "139,99 €", "specialPrice": 139.99, "retailPrice": 309.99}],
                 [{"id": 9, "specialPrice": 5, "retailPrice": 10}]]
        ps = v.extraer("<html></html>", "https://www.privatesportshop.es/", datos)
        self.assertEqual([(p["nombre"], p["precio"], p["precio_antes"], p["url"]) for p in ps],
                         [("Nike Zapatillas de running Alphafly 3", 139.99, 309.99,
                           "https://www.privatesportshop.es/catalog/product/view/id/5207575/")])

    def test_privalia_campanas_y_productos(self):
        """18/11/2026: la portada da campañas; cada campaña, productos. El tachado es el recomendado: no es precio_antes."""
        portada = ('<a href="https://es.privalia.com/gr/catalog/910371">x</a><a href="/gr/catalog/897331">y</a>'
                   '<a href="https://es.privalia.com/gr/catalog/910371">x</a>')
        self.assertEqual(v.campanas_privalia(portada), ["https://es.privalia.com/gr/catalog/910371",
                                                        "https://es.privalia.com/gr/catalog/897331"])
        item = ('{"pricing":{"currency":{"symbol":"€"},"price":{"value":19.99},"retailPrice":{"tooltip":{"description":'
                '"no indica una reducción"},"value":49.9},"discount":59},"isLocked":false,"saleId":"910371",'
                '"catalogId":"25858289","name":"Camiseta - algodón - verde","medias":[{"url":"m"}],"id":"104028267"}')
        bloqueado = item.replace('"isLocked":false', '"isLocked":true').replace("104028267", "1")
        html = f'<script>{{"brandSlug":"tommy-hilfiger","items":[{item},{bloqueado}]}}</script>'
        ps = v.extraer(html, "https://es.privalia.com/gr/catalog/910371/25858289")
        self.assertEqual([(p["nombre"], p["precio"], p["precio_antes"], p["pvpr"], p["url"]) for p in ps],
                         [("Tommy Hilfiger Camiseta - algodón - verde", 19.99, None, 49.9,
                           "https://es.privalia.com/gr/catalog/910371/25858289?producto=104028267")])
        self.assertEqual(v.clave_producto(ps[0]["url"]), "privalia:104028267")
        self.assertIsNone(v.clave_producto("https://es.privalia.com/gr/catalog/910371"), "una campaña no es un producto")
        self.assertEqual(v.confirmar_en_ficha(ps[0])[0], None, "sin ficha propia: ni confirma ni descarta")

    def test_busqueda_de_aliexpress(self):
        html = ('{"productId":"1005011741324622","title":{"displayTitle":"Consola PS5 Chassis E Digital"},'
                '"prices":{"salePrice":{"discount":20,"currencyCode":"EUR","minPrice":400.0,"cent":40000}},'
                '"evaluation":{"starRating":4.5},"trade":{"tradeDesc":"800+ vendidos"}}'
                '{"productId":"1005000000000001","title":{"displayTitle":"Funda"},'
                '"prices":{"salePrice":{"currencyCode":"EUR","minPrice":3.5}}}')
        ps = v.extraer(html, "https://es.aliexpress.com/w/wholesale-ps5.html")
        self.assertEqual([(p["nombre"], p["precio"], p["precio_antes"], p["valoracion"]) for p in ps],
                         [("Consola PS5 Chassis E Digital", 400.0, 500.0, 90), ("Funda", 3.5, None, None)])
        self.assertEqual(ps[0]["url"], "https://es.aliexpress.com/item/1005011741324622.html")

    def test_busquedas_por_familia_por_turnos_y_por_grados(self):
        con = v.bd()
        # Copia de Chollometro: en esta tienda las consolas llegan a 200° siempre; los taladros, 1 de cada 4.
        filas = [(i, f"PS5 Slim {i}", 400, "AliExpress", "es.aliexpress.com", 500, int(time.time()), 0, "") for i in range(8)]
        filas += [(100 + i, f"Taladro {i}", 40, "AliExpress", "es.aliexpress.com", 300 if i % 4 == 0 else 50, int(time.time()), 0, "")
                  for i in range(8)]
        con.executemany("INSERT INTO cm (id, titulo, precio, tienda, host, temp, publicado, caducado, visto) VALUES (?,?,?,?,?,?,?,?,?)", filas)
        cfg = {"busquedas_familia": {"por_ronda": 2, "cada_min": 15, "tiendas": {"aliexpress.com": {
            "url": "https://es.aliexpress.com/w/wholesale-{q}.html", "espacio": "-",
            "consultas": {"consolas": ["ps5", "switch 2"], "herramientas": ["taladro", "sierra"]}}}}}
        primera = v.busquedas_turno(con, cfg, ["aliexpress.com"])
        self.assertEqual([u for _, u in primera], ["https://es.aliexpress.com/w/wholesale-ps5.html",
                                                   "https://es.aliexpress.com/w/wholesale-switch-2.html"])
        self.assertEqual(v.busquedas_turno(con, cfg, ["aliexpress.com"]), [], "cada 15 min como mucho")
        self.assertEqual(v.busquedas_turno(con, cfg, ["miravia.es"]), [], "solo tiendas de esta ronda")
        cuenta = Counter(u for _, u in primera)
        for _ in range(11):
            con.execute("DELETE FROM meta WHERE k LIKE 'busq_ultimo:%'")
            cuenta.update(u for _, u in v.busquedas_turno(con, cfg, ["aliexpress.com"]))
        ps5, taladro = cuenta["https://es.aliexpress.com/w/wholesale-ps5.html"], \
            cuenta["https://es.aliexpress.com/w/wholesale-taladro.html"]
        self.assertGreater(taladro, 0, "ninguna se queda sin leer")
        self.assertGreaterEqual(ps5, 3 * taladro, (ps5, taladro))
        con.close()

    def test_pesos_por_volumen_de_cada_tienda(self):
        """18/11/2026: la copia es casi toda caliente; el peso es cuántos hilos calientes da cada familia en esa
        tienda (500° cuentan doble), no la tasa."""
        con = v.bd()
        ahora = int(time.time())
        filas = [(i, f"Consola PS5 Slim {i}", 400, "", "", 600, ahora, 0, "", '{"linkHost":"www.mediamarkt.es"}')
                 for i in range(9)]
        filas += [(50 + i, f"Smartphone Xiaomi {i}", 200, "", "", 250, ahora, 0, "", '{"linkHost":"www.mediamarkt.es"}')
                  for i in range(3)]
        filas += [(90 + i, f"Zapatillas adidas {i}", 50, "", "", 900, ahora, 0, "", '{"linkHost":"www.zalando.es"}')
                  for i in range(20)]
        filas.append((200, "PS5 antigua", 400, "", "", 900, ahora - 400 * 86400, 0, "", '{"linkHost":"www.mediamarkt.es"}'))
        con.executemany("INSERT INTO cm (id, titulo, precio, tienda, host, temp, publicado, caducado, visto, extra) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)", filas)
        p = v.pesos_volumen(con, {}, "mediamarkt.es")
        # Consolas 18 puntos, móviles 3: media 10,5.
        self.assertEqual((p["consolas"], p["moviles"], p["calzado"]), (round(18 / 10.5, 2), v.PESO_MIN, v.PESO_MIN))
        # Sin datos de la tienda, las de todas: calzado 40, consolas 18, móviles 3 (media 61/3).
        self.assertEqual(v.pesos_volumen(con, {}, "otra.es")["calzado"], round(40 / (61 / 3), 2))
        self.assertIsNone(v.pesos_volumen(con, {"filtros_codigo": {"peso_por_demanda": False}}, "mediamarkt.es"))
        con.close()

    def test_redireccion_a_reto_falla(self):
        class Falso(v.Navegador):
            def leer(self, url, completo=False):
                return "<html></html>", "https://es.aliexpress.com/punish?x5secdata=abc", []
        n = v.NavegadorNormal.__new__(v.NavegadorNormal)
        orig = v.Navegador.leer
        v.Navegador.leer = Falso.leer
        try:
            with self.assertRaises(OSError) as e:
                n.leer("https://es.aliexpress.com/ssr/x")
        finally:
            v.Navegador.leer = orig
        self.assertIn("reto", str(e.exception))




class DestinoDeHilos(unittest.TestCase):
    """18/11/2026: adónde lleva cada hilo (cm_destino) y su clave de producto, para duplicados por enlace y para
    el precio con que triunfó (cm_ref). Antes solo ASIN de Amazon (asins/vigilancia)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = v.DATOS, v.DB, v.destino_hilo, v.time.sleep, v.cargar_config, v.build_opener
        v.DATOS, v.DB = self.tmp, self.tmp / "vistos.db"
        v.time.sleep = lambda s: None
        v.cargar_config = lambda: {"categorias": {"x": {"tiendas": ["amazon.es", "mediamarkt.es"]}}}
        self.ahora = int(time.time())
        con = v.bd()
        filas = [(1, "Café A", 10.0, 500, '{"linkHost":"www.amazon.es"}'),
                 (2, "Café A otra vez", 8.0, 300, '{"linkHost":"www.amazon.es"}'),
                 (3, "Frío", 5.0, 50, '{"linkHost":"www.amazon.es"}'),
                 (4, "Zapatillas", 50.0, 900, '{"linkHost":"www.zalando.es"}'),
                 (5, "Tele", 300.0, 250, '{"linkHost":"www.mediamarkt.es"}')]
        for i, t, p, temp, e in filas:
            con.execute("INSERT INTO cm (id, titulo, precio, tienda, host, temp, publicado, caducado, visto, extra) "
                        "VALUES (?,?,?,?,?,?,?,1,'',?)", (i, t, p, "", "", temp, self.ahora - 86400 * i, e))
        con.commit()
        con.close()

    def tearDown(self):
        v.DATOS, v.DB, v.destino_hilo, v.time.sleep, v.cargar_config, v.build_opener = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clave_producto(self):
        self.assertEqual(v.clave_producto("https://www.amazon.es/Cafe-X/dp/b0fcxr32cr?tag=a"), "amazon:B0FCXR32CR")
        self.assertEqual(v.clave_producto("https://es.aliexpress.com/item/1005006001.html?spm=1"), "aliexpress:1005006001")
        self.assertEqual(v.clave_producto("https://www.miravia.es/p/i3058766-s51.html"), "miravia:3058766")
        self.assertEqual(v.clave_producto("https://www.miravia.es/p/cafetera-xx-i3058766.html"), "miravia:3058766")
        self.assertEqual(v.clave_producto("https://www.mediamarkt.es/es/product/_tv-lg-1234.html?x=1"),
                         "mediamarkt.es/es/product/_tv-lg-1234.html")
        self.assertIsNone(v.clave_producto("https://www.mediamarkt.es/"), "una portada no es un producto")
        self.assertIsNone(v.clave_producto("https://www.chollometro.com/ofertas/x-1"))
        self.assertIsNone(v.clave_producto(None))

    def test_destino_sigue_cabeceras_y_parametros(self):
        saltos = {"https://www.chollometro.com/visit/threadmain/7":
                  "https://www.awin1.com/cread.php?awinmid=1&ued=https%3A%2F%2Fwww.mediamarkt.es%2Fes%2Fproduct%2F_tv-1.html",
                  "https://amzn.eu/d/abc": "https://www.amazon.es/dp/B000000009?tag=x"}
        pedidas = []

        class Abridor:
            def open(self, req, timeout=None):
                pedidas.append(req.full_url)
                if req.full_url in saltos:
                    raise v.HTTPError(req.full_url, 302, "Found", {"Location": saltos[req.full_url]}, None)
                raise AssertionError("no debe pedir la tienda")
        v.build_opener = lambda *a: Abridor()
        self.assertEqual(v.destino_hilo(7), "https://www.mediamarkt.es/es/product/_tv-1.html")
        self.assertEqual(pedidas, ["https://www.chollometro.com/visit/threadmain/7"], "el afiliado no se pide")
        saltos["https://www.chollometro.com/visit/threadmain/8"] = "https://amzn.eu/d/abc"
        self.assertEqual(v.destino_hilo(8), "https://www.amazon.es/dp/B000000009?tag=x")

    def test_destinos_guarda_clave_y_cm_ref(self):
        destinos = {1: "https://www.amazon.es/dp/B000000001?tag=x", 2: "https://www.amazon.es/dp/B000000001",
                    4: "https://www.zalando.es/", 5: "https://www.mediamarkt.es/es/product/_tele-55.html"}
        pedidos = []
        v.destino_hilo = lambda h: (pedidos.append(h), destinos[h])[1]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(v.destinos_cm(), (4, 3))
        self.assertEqual(pedidos[-1], 4, "las tiendas que no leemos, al final; el frío, nunca")
        con = v.bd()
        self.assertEqual(con.execute("SELECT hilos, precio_min, precio_max, temp_max FROM cm_ref "
                                     "WHERE clave='amazon:B000000001'").fetchone(), (2, 8.0, 10.0, 500))
        self.assertEqual(con.execute("SELECT url, clave FROM cm_destino WHERE hilo=4").fetchone(),
                         ("https://www.zalando.es/", None))
        con.close()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(v.destinos_cm(), (0, 0), "ya mirados")

    def test_vigilancia_amazon_por_grupos(self):
        """18/11/2026 (Zeta): los ASIN que ya triunfaron, en grupos por categoría y tipo, mirados por turnos."""
        con = v.bd()
        con.execute("UPDATE cm SET extra='{\"mainGroup\":{\"threadGroupUrlName\":\"hogar\"}}' WHERE id IN (1,2)")
        con.execute("INSERT INTO cm (id, titulo, precio, tienda, host, temp, publicado, caducado, visto, extra) VALUES "
                    "(6, 'LEGO Star Wars 75433', 49.99, '', '', 1458, ?, 1, '', "
                    "'{\"mainGroup\":{\"threadGroupUrlName\":\"familia-e-hijos\"}}')", (self.ahora - 86400,))
        con.executemany("INSERT INTO cm_destino VALUES (?,?,?,'')",
                        [(1, "u", "amazon:B000000001"), (2, "u", "amazon:B000000001"), (3, "u", "amazon:B000000003"),
                         (6, "u", "amazon:B0DWDK2V4R"), (5, "u", "mediamarkt.es/x")])
        con.commit()
        self.assertEqual(v.actualizar_vigilancia(con), 2, "solo Amazon a 200° o más (el frío no)")
        self.assertEqual(con.execute("SELECT grupo, exito, hilos, temp FROM vigilancia WHERE asin='B000000001'").fetchone(),
                         ("hogar", 8.0, 2, 500), "el precio de éxito es el más bajo con que llegó a 200°")
        cfg = {"vigilancia_amazon": {"por_ronda": 1}}
        primero = v.vigilancia_turno(con, cfg, ["amazon.es"])
        segundo = v.vigilancia_turno(con, cfg, ["amazon.es"])
        self.assertEqual(len(primero), 1)
        self.assertNotEqual(primero, segundo, "por turnos: el siguiente grupo")
        self.assertEqual(set(primero.values()) | set(segundo.values()), {8.0, 49.99})
        self.assertEqual(v.vigilancia_turno(con, cfg, ["miravia.es"]), {}, "sin Amazon en la ronda, nada")
        con.close()

    def test_para_con_429_sin_marcar(self):
        def falla(h):
            raise v.HTTPError("u", 429, "Too Many", {}, None)
        v.destino_hilo = falla
        with redirect_stdout(io.StringIO()):
            self.assertEqual(v.destinos_cm(), (0, 0))
        con = v.bd()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM cm_destino").fetchone()[0], 0)
        con.close()

    def test_duplicado_y_contraste_por_enlace(self):
        con = v.bd()
        con.executemany("INSERT INTO cm_destino VALUES (?,?,?,'')",
                        [(1, "https://www.amazon.es/dp/B000000001", "amazon:B000000001"),
                         (2, "https://www.amazon.es/dp/B000000001", "amazon:B000000001")])
        con.commit()
        copia = v.cargar_chollometro(con)
        con.close()
        # Título que no se parece en nada: por el enlace es el mismo producto.
        p = {"nombre": "Nescafé Dolce Gusto cápsulas espresso intenso 30 uds", "tienda": "amazon.es", "precio": 8.0,
             "url": "https://www.amazon.es/dp/B000000001"}
        self.assertEqual(v.estado_chollometro(p, copia), "misma", "publicado hace 2 días al mismo precio")
        self.assertEqual(v.estado_chollometro({**p, "precio": 9.0}, copia), "misma:8")
        self.assertEqual(p["_cm_hot"]["temp"], 500)
        self.assertEqual(v.chollo_previo(p)["id"], 1, "sin código de modelo, pero por el enlace (el más caliente)")
        self.assertIsNone(v.ya_salio_mas_barato(p))
        self.assertEqual(v.ya_salio_mas_barato({**p, "precio": 12.0}), 10.0, "más de un 10 % más caro que cuando triunfó")
        self.assertIsNone(v.ya_salio_mas_barato({**p, "precio": 10.9}), "Zeta: hasta un 10 % más caro todavía vale")
        self.assertIsNone(v.ya_salio_mas_barato({**p, "url": "https://www.amazon.es/dp/B000000002", "precio": 99}))


class ReglasPublicacionChollometro(unittest.TestCase):
    """Tallas, una unidad, envío a España, códigos de un solo uso y enlace limpio (18/11/2026)."""

    def test_tallas(self):
        ropa, zapas = {"nombre": "Sudadera Nike Club con capucha"}, {"nombre": "Zapatillas adidas Samba OG"}
        self.assertEqual(v.regla_tallas(ropa, {"variantes": ["S: agotada", "M: hay stock", "L: agotada"]})[0], False)
        self.assertEqual(v.regla_tallas(ropa, {"variantes": ["M: hay stock", "L: seleccionable"]})[0], True)
        self.assertEqual(v.regla_tallas(zapas, {"variantes": ["42: hay stock", "43: hay stock", "44: agotada"]})[0], False)
        self.assertEqual(v.regla_tallas(zapas, {"variantes": ["40", "41", "42.5"]})[0], True)
        self.assertEqual(v.regla_tallas(zapas, {}), (None, "calzado: tallas sin comprobar"))
        self.assertEqual(v.regla_tallas({"nombre": "Freidora de aire Cosori"}, {}), (None, ""))
        self.assertEqual(v.regla_tallas(ropa, {"variantes": ["Talla única: hay stock"]})[0], True)

    def test_ficha_una_unidad_y_envio(self):
        f = v.analizar_ficha("<p>¡Solo queda 1 en stock!</p><p>Este artículo no se envía a España</p>",
                             "https://t.es/p/1")
        self.assertTrue(f.get("una_unidad"))
        self.assertTrue(f.get("no_envia_es"))
        self.assertFalse(v.analizar_ficha("<p>Quedan 12 unidades. Envío gratis a España</p>", "https://t.es/p/1")
                         .get("una_unidad"))

    def test_codigos_de_un_solo_uso(self):
        self.assertTrue(v.RE_CUPON_NUEVOS.search("Código de un solo uso enviado por email"))
        self.assertTrue(v.RE_CUPON_NUEVOS.search("Código personal e intransferible"))
        self.assertFalse(v.RE_CUPON_NUEVOS.search("Válido un solo uso por cliente"))

    def test_enlace_limpio(self):
        self.assertEqual(v.enlace_limpio("https://www.t.es/p/x?utm_source=a&color=rojo&tag=yo-21&gclid=1"),
                         "https://www.t.es/p/x?color=rojo")
        self.assertEqual(v.enlace_limpio("https://es.privalia.com/gr/catalog/1/2?producto=3"),
                         "https://es.privalia.com/gr/catalog/1/2?producto=3")


class PestanasTelegram(unittest.TestCase):
    """18/11/2026: pestañas en el chat del bot (candidatos por categoría, para publicar y sistema)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = v.DATOS, v._post, v.DB
        v.DATOS = self.tmp
        self.enviados = []
        v._post = lambda url, datos, timeout=20: self.enviados.append(datos) or {"ok": True}

    def tearDown(self):
        v.DATOS, v._post, v.DB = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_casi_chollos_al_radar(self):
        v.DB = self.tmp / "vistos.db"
        orig = v.token_telegram, v.enviar_telegram
        enviados = []
        v.token_telegram = lambda: "t"
        v.enviar_telegram = lambda token, chat, texto, foto=None, *a, **k: enviados.append((texto, k.get("hilo"))) or True
        try:
            con = v.bd()
            cfg = {"entrega": {"modo": "telegram", "chat_id": "1"}}
            casi = [{"tienda": "t.es", "nombre": "Cosori Freidora A", "precio": 40, "precio_antes": 100, "url": "https://t.es/a"},
                    {"tienda": "t.es", "nombre": "Cosori Freidora B", "precio": 70, "precio_antes": 100, "url": "https://t.es/b"},
                    {"tienda": "t.es", "nombre": "Sony Consola C", "precio": 70, "precio_antes": 100, "peso": 2.0,
                     "url": "https://t.es/c"},
                    {"tienda": "t.es", "nombre": "Sin antes", "precio": 10, "precio_antes": None, "url": "https://t.es/d"}]
            self.assertEqual(v.avisar_casi_chollos(con, cfg, casi), 2, "-60 % y -30 % de una familia que se calienta")
            self.assertEqual([h for _, h in enviados], ["cat:radar", "cat:radar"])
            self.assertIn("Freidora A", enviados[0][0])
            self.assertIn("Detectado ? · descartado ", enviados[0][0])
            self.assertIn("Detectado 18/11 11:05 · avisado ",
                          v.mensaje_candidato(1, {"tienda": "t.es", "nombre": "x", "precio": 5, "_detectado": "18/11 11:05"}))
            self.assertEqual(v.avisar_casi_chollos(con, cfg, casi), 0, "el mismo producto, una vez por semana")
            con.close()
        finally:
            v.token_telegram, v.enviar_telegram = orig

    def test_cada_mensaje_a_su_pestana(self):
        (self.tmp / v.TEMAS).write_text(json.dumps({"sistema": 7, "cat:moda": 9}), encoding="utf-8")
        v.enviar_telegram("t", "1", "hola", hilo="sistema")
        v.enviar_telegram("t", "1", "hola", hilo="cat:moda")
        v.enviar_telegram("t", "1", "hola", hilo="cat:hogar")  # sin pestaña: al chat general
        v.enviar_telegram("t", "1", "hola", hilo=12)
        self.assertEqual([e.get("message_thread_id") for e in self.enviados], ["7", "9", None, "12"])


class EmparejamientoPorTipo(unittest.TestCase):
    """#213 (18/11/2026): camiseta Calvin Klein Jeans de Spartoo emparejada con "Calvin Klein jeans mujer" (415°,
    una recopilación de vaqueros) como demanda probada, y con unas zapatillas como "publicado en otra tienda"."""
    NOMBRE = "textil Mujer Camisetas sin mangas Calvin Klein Jeans WOVEN LABEL RIB TANK TOP Blanco"

    def test_tipos_distintos_no_son_el_mismo_producto(self):
        self.assertFalse(v.mismo_post(self.NOMBRE, "CALVIN KLEIN JEANS , Zapatillas tallas de la 36 a la 41"))
        self.assertTrue(v.mismo_post("Zapatillas Nike Air Force 1 07 blancas", "Nike air force 1 07 zapatillas"))

    def test_titulo_vago_no_es_demanda_probada(self):
        self.assertFalse(v.mismo_producto_hot(self.NOMBRE, "Calvin Klein jeans mujer"))

    def test_marca_y_modelo(self):
        """18/11/2026 (Zeta: "comparar por marca y por modelo"): casos reales de la revisión de 400 productos."""
        mp = v.mismo_producto
        a = "Smartphone Xiaomi POCO M7 6GB 128GB"
        self.assertTrue(mp(a, "Xiaomi POCO M7 - 6/128GB, Pantalla de 6'9\" con 144Hz, Snapdragon 685, 7000mAh"))
        self.assertTrue(mp(a, "POCO M7 (6GB/128GB) con Batería de 7000mAh y 144Hz"), "gama implica marca; 'con' no es accesorio")
        self.assertFalse(mp(a, "Xiaomi POCO M7 Pro 5G 8+256GB"), "variante Pro")
        self.assertFalse(mp("Xiaomi Redmi 15C 4GB 256GB", "Xiaomi POCO C85 6GB+128GB"), "otra gama")
        self.assertFalse(mp("Móvil Xiaomi Redmi Note 17 5G 256GB + 6GB RAM", "Xiaomi Redmi Note 17 - 6/128GB"), "otro almacenamiento")
        self.assertFalse(mp("Silla de oficina ergonómica con reposabrazos", "Silla Oficina MITSAI Ergonómica Tokyo"), "sin marca")
        self.assertFalse(mp("Vidrio Templado Sunnylife para Mando DJI RC Pro", "DJI Mini 4K con mando RC-N1C"), "accesorio")
        self.assertFalse(mp("Nothing Ear (3a) TWS ANC Auriculares", "Nothing Phone 3a 5G 8GB 128GB"), "otra familia")
        self.assertFalse(mp('TV 65" OLED S93H Samsung', 'Samsung TV 55" QLED QEF1 ( 50" a 341€ / 65" a 549€ )'), "recopilación")
        self.assertFalse(mp("Joma Camiseta Manga Corta Hombre", "Joma Camiseta Manga Corta Mujer"), "género")
        self.assertFalse(mp("Converse Chuck Taylor All Star Lift Clean OX", "Converse Chuck Taylor All Star High"), "corte")
        self.assertTrue(mp("Sony WH-CH720N Azul Auriculares inalámbricos", "Sony WH-CH720N Auriculares Bluetooth"))
        self.assertTrue(mp("Zapatos Botas Dr. Martens 1460 Cherry Red", "Botas DR. Martens 1460"))
        # #225: duplicado no visto ("+3" de edad y "15 cm" contaban como número de modelo; juguete/juguetes)
        self.assertTrue(mp("Paw Patrol Patrulla Canina Camión Bomberos y Marshall Juguetes niños +3 | Camión de bomberos "
                           "Marshall de 15 cm con figura de Marshall para recrear aventuras de Patrulla Canina. +3 años.",
                           "Juguete Patrulla Canina Camión de Bomberos 15 cm con Figura de Marshall (Paw Patrol Spin "
                           "Master, +3 Años)"))
        # Zeta (18/11/2026): los reacondicionados, fuera; y un hilo de reacondicionado no es antecedente del nuevo
        self.assertFalse(mp("Apple iPhone 16 128GB Negro", "Apple iPhone 16 128GB Reacondicionado Grado A"))
        for n in ("iPhone 15 128GB (Reacondicionado)", "Amazon Renewed Galaxy S24", "BOSS Segunda Mano Jersey",
                  "Portátil HP como nuevo"):
            self.assertTrue(v.RE_REACONDICIONADO.search(n), n)
        self.assertFalse(v.RE_REACONDICIONADO.search("Lavadora Bosch 8 kg nueva"))
        self.assertTrue(v.RE_REACONDICIONADO.search("https://x.com/samsung-galaxy-s23-ultra-renovado-estado-excelente"))

    def test_lector_corte_ingles_y_enlaces_del_mapa(self):
        """18/11/2026: El Corte Inglés lleva los precios en el bloque de datos del listado; el mapa prueba primero las
        páginas de ofertas (black-friday, rebajas), que el catálogo antiguo apartaba."""
        html = ('<script>{"products":[{"brand":"LG","code_a":"A200840937","gtin":"8806096685648","name":"TV OLED LG '
                'OLED65C66LB","price":{"f_price":1549,"o_price":2699}}]}</script>'
                '<a href="/electronica/A200840937-8806096685648-pr-tv-oled-lg/">x</a>')
        ps = v.extraer(html, "https://www.elcorteingles.es/black-friday/electronica/tv/")
        self.assertEqual([(p["precio"], p["precio_antes"], p["ean"], p["marca"]) for p in ps],
                         [(1549.0, 2699.0, "8806096685648", "LG")])
        self.assertEqual(ps[0]["url"], "https://www.elcorteingles.es/electronica/A200840937-8806096685648-pr-tv-oled-lg/")
        portada = ('<a href="/black-friday/">Black Friday</a><a href="/mi-cuenta/">Mi cuenta</a>'
                   '<a href="/electrodomesticos/">Electrodomésticos</a><a href="/rebajas/">Rebajas</a>')
        enlaces = v.enlaces_de_mapa(portada, "https://www.elcorteingles.es/")
        self.assertEqual(enlaces[:2], ["https://www.elcorteingles.es/black-friday/", "https://www.elcorteingles.es/rebajas/"])
        self.assertNotIn("https://www.elcorteingles.es/mi-cuenta/", enlaces)
        # Samsung enlaza las portadas de otros países: el mapa de samsung.com/es no sale de /es/
        self.assertTrue(v.en_seccion("samsung.com/es", "https://www.samsung.com/es/offer/"))
        self.assertFalse(v.en_seccion("samsung.com/es", "https://www.samsung.com/uk/offer/"))
        self.assertFalse(v.en_seccion("samsung.com/es", "https://www.samsung.com/estonia/"))
        self.assertTrue(v.en_seccion("elcorteingles.es", "https://www.elcorteingles.es/rebajas/"))
        # Zeta: "las categorías más frecuentes en Chollometro": la página de una familia caliente pesa más
        pesos = {"televisores": 2.5, "ropa": 0.3}
        self.assertEqual(v._peso_pagina(pesos, "https://www.elcorteingles.es/black-friday/electronica/tv/"), 2.5)
        self.assertEqual(v._peso_pagina(pesos, "https://www.elcorteingles.es/moda-hombre/ropa/"), 0.3)

    def test_pccomtracker_sin_precio_oficial(self):
        """18/11/2026: Philips 48OLED770 "a 609 €" era el precio de Amazon; PcComponentes no lo vendía."""
        html = ('<h1>Philips OLED 48OLED770</h1><p>Sin precio oficial de PcComponentes</p><a href="/goto/amazon?url='
                'https%3A%2F%2Fwww.amazon.es%2Fdp%2FB0F33QY8R5%3FlinkCode%3Dosi&amp;source=web">Comprar en Amazon por '
                '609,00 €</a><p>Precio habitual de 919,00 €</p><p>Precio mínimo 766,66 €</p>')
        orig = v.descargar
        try:
            v.descargar = lambda url, *a, **k: (html, url)
            h = v.historico_comparador("https://pccomtracker.com/producto/philips-oled-48oled770-60968")
        finally:
            v.descargar = orig
        self.assertTrue(h and h.get("sin_precio_tienda"))
        # Lo que sale en pantalla es el precio de Amazon: la oferta pasa a ser de Amazon, con su enlace.
        self.assertEqual((h["amazon_url"], h["amazon_precio"]), ("https://www.amazon.es/dp/B0F33QY8R5", 609.0))
        self.assertTrue(v.RE_SIN_PRECIO_TIENDA.search("Hisense 55A6N 55\" Smart TV Precio no disponible"))

    def test_televisores_samsung_por_serie_y_pulgadas(self):
        """18/11/2026 (Zeta): TQ77S93HATXXC y TQ100QN80HAUXXC ya estaban en Chollometro con otro título."""
        eci = 'TV OLED 195cm (77") Samsung TQ77S93HATXXC 4K Smart TV SIN reflejos con Vision AI. 165Hz Ultimate Gaming Pack'
        self.assertTrue(v.mismo_producto(eci, 'TV 77" OLED S93H 4K Sin reflejos 165Hz Samsung Vision Al Smart TV (2026) 77"'))
        self.assertTrue(v.mismo_producto(eci, 'Smart TV Samsung 77" OLED TQ77S93H (4K UHD, 144Hz, NQ4 AI)'))
        self.assertFalse(v.mismo_producto(eci, 'TV Samsung OLED 65" S93H 4K Sin reflejos 165Hz'), "otras pulgadas")
        self.assertFalse(v.mismo_producto(eci, 'TV Samsung OLED 77" S90H 4K'), "otra serie")
        qn = 'TV Neo QLED Mini LED 254cm (100") Samsung TQ100QN80HAUXXC 4K Smart TV con Vision AI'
        self.assertTrue(v.mismo_producto(qn, 'TV 100" Neo QLED QN80H 4K Samsung Vision AI Smart TV (2026)'))
        self.assertFalse(v.mismo_producto(qn, 'Samsung TV 100 Pulgadas Neo QLED QN80F 4K [TQ100QN80FUXXC]'), "QN80F es de 2025")
        self.assertEqual(v._consulta_busqueda({"nombre": qn}), "samsung QN80H 100")
        self.assertEqual(v.modelo_tv("Samsung UE43DU7100KXXC"), (frozenset({43}), "DU7100"))
        self.assertIsNone(v.modelo_tv("Samsung Galaxy S25 Ultra 256 GB"))
        # Las demás marcas (Zeta: "para todos")
        self.assertTrue(v.mismo_producto('TV OLED 65" - LG OLED65C65LA, OLED evo 4K',
                                         'Smart TV LG OLED evo AI C6 65 pulgadas 4K 2026'))
        self.assertFalse(v.mismo_producto('LG OLED65C65LA', 'LG OLED evo C6 55" 4K'), "otras pulgadas")
        self.assertFalse(v.mismo_producto('LG OLED65C65LA 65"', 'LG OLED65G64LW 65"'), "C6 no es G6")
        self.assertTrue(v.mismo_producto('LG 65QNED81B6A 65 pulgadas', 'TV QNED Mini LED 65" - LG 65QNED81B6A, UHD 4K'))
        self.assertTrue(v.mismo_producto('Sony Bravia 8 K-65XR80 OLED', 'Sony XR80 Bravia 8 OLED 65"'))
        self.assertTrue(v.mismo_producto('Philips 65OLED801/12 65"', 'TV Philips 65 OLED 65OLED801 4K Ultra HD'))
        self.assertFalse(v.mismo_producto('TV QLED 85" - Hisense 85E7S, QLED 4K', 'TV QLED 85" - Hisense 85E7S PRO, 144Hz'),
                         "el PRO es otro modelo")
        self.assertEqual(v.codigo_tv("Panasonic TX-65MZ1500E OLED"), ("panasonic", 65, "MZ1500"))
        self.assertEqual(v._consulta_busqueda({"nombre": "LG OLED65C65LA"}), "lg C6 65")
        self.assertIsNone(v.codigo_tv("Funda 65C855 para móvil"), "sin la marca no es un televisor")

    def test_ficha_amazon_precio_del_bloque_de_compra(self):
        """18/11/2026: la ficha del LEGO 75433 daba 55,99 € (un LEGO del carrusel); Zeta lo veía a 49,99 €."""
        html = ('<span id="productTitle" class="a-size-medium">  LEGO Star Wars Nave Estelar de Jango Fett 75433  </span>'
                '<span class="a-price a-text-price" data-a-strike="true"><span class="basisPrice">Precio recomendado: '
                '<span class="a-offscreen">69,99€</span></span></span>'
                '{"desktop_buybox_group_1":[{"displayPrice":"49,99&nbsp;€","priceAmount":49.99,"currencySymbol":"€"}]}'
                '<div data-asin="B0CARRUSEL"><a href="/dp/B0CARRUSEL">LEGO KPop Demon Hunters</a>'
                '<span class="a-offscreen">55,99€</span></div>')
        ps = v.extraer(html, "https://www.amazon.es/dp/B0DWDK2V4R")
        self.assertEqual((ps[0]["nombre"], ps[0]["precio"], ps[0]["precio_antes"]),
                         ("LEGO Star Wars Nave Estelar de Jango Fett 75433", 49.99, 69.99))
        self.assertEqual(ps[0]["url"], "https://www.amazon.es/dp/B0DWDK2V4R")

    def test_lectores_game_y_jysk(self):
        """18/11/2026: GAME daba 4 productos de 24 y Jysk 1 de 36; ahora sus tarjetas, con el mínimo de 30 días."""
        game = ('<article class="ProductListCard-x"><a href="/game-hh-st-rgb-negro-auriculares-gaming/232337"><h3>GAME '
                'HH-ST RGB Negro - Auriculares Gaming</h3></a><p>Precio recomendado</p><span>24,99€</span>'
                '<span>17,99€</span><span>-28%</span><p>-10% <!-- -->Mín. últimos 30 días: <!-- -->19,99€</p></article>')
        ps = v.extraer(game, "https://www.game.es/ofertas")
        self.assertEqual([(p["precio"], p["precio_antes"], p["min30"]) for p in ps], [(17.99, 24.99, 19.99)])
        self.assertEqual(ps[0]["url"], "https://www.game.es/game-hh-st-rgb-negro-auriculares-gaming/232337")
        jysk = ('<a aria-label="Ver producto Toldo lateral PEJELT A3xA1,4m gris" href="/jardin/toldo-lateral-pejelt">'
                '<img alt="x"></a><div aria-label="Precio del artículo - 40,00 € /u"></div>'
                '<div>Precio más bajo últimos 30 días: 65,00 € /u (-38%)</div><div>Precio normal: 65,00 € /u</div>')
        ps = v.extraer(jysk, "https://jysk.es/ofertas")
        self.assertEqual([(p["nombre"], p["precio"], p["precio_antes"], p["min30"]) for p in ps],
                         [("Toldo lateral PEJELT A3xA1,4m gris", 40.0, 65.0, 65.0)])

    def test_marca_y_precio_de_la_tarjeta(self):
        """18/11/2026: Toys R Us pone la marca encima del nombre; Forumsport la deja en la dirección ("CLUB" eran
        unas Nike); Neobyte enseña "-200,10 €" de descuento y se cogía como precio (#222)."""
        html = ('<a href="/p/K1"><p class="product-brand">KLEIN</p><a class="name" href="/Hot-Wheels-Maletin/p/K1">'
                'Hot Wheels - Maletín Metálico</a></a>')
        self.assertEqual(v.marca_de_tarjeta(html, "Hot Wheels - Maletín Metálico"), "KLEIN")
        self.assertEqual(v.nombre_con_marca_de_ficha(
            {"nombre": "CLUB", "url": "https://www.forumsport.com/es-es/nike-sudadera-hombre-club-fz0772-1000982276-p"}),
            "Nike Sudadera Hombre Club Fz0772")
        tarjetas = "".join(f'<a class="card" href="/producto/nas-synology-ds72{i}-2011033{i}"><h3>Synology DS72{i}neo '
                           f'Servidor NAS</h3><span class="product-flag discount">-200,10€</span>'
                           f'<span class="product-price">59{i},89€</span></a>' for i in range(3))
        ps = v.tarjetas_html(tarjetas, "https://t.es/ofertas")
        self.assertEqual([p["price"] for p in ps], [590.89, 591.89, 592.89])

    def test_configuracion_sin_concretar(self):
        """#214 (18/11/2026): ThinkPad sin RAM ni disco en el nombre contra el precio de Idealo de otra configuración."""
        self.assertTrue(v.configuracion_ambigua("ThinkPad P14s Gen 7 14 inch AMD"))
        self.assertTrue(v.configuracion_ambigua("Samsung Galaxy S25 Ultra"))
        self.assertFalse(v.configuracion_ambigua("Portátil ASUS Vivobook 15 i5-13420H 16GB 512GB"))
        self.assertFalse(v.configuracion_ambigua("TP-Link TL-SG105 switch 5 puertos"))

    def test_busqueda_manual_con_marca_y_modelo(self):
        self.assertEqual(v.enlace_busqueda_chollometro(self.NOMBRE, "Calvin Klein Jeans"),
                         "https://www.chollometro.com/search?q=Calvin+Klein+Jeans+WOVEN+LABEL+RIB")
        self.assertNotIn("textil", v.enlace_busqueda_chollometro(self.NOMBRE))


class ReglasDuplicadosChollometro(unittest.TestCase):
    """Reglas de publicación de Chollometro (chollometro.com/reglas, leídas el 18/11/2026)."""

    def copia(self, *hilos, indice=True):
        hs = [{"id": i + 1, "titulo": t, "precio": pr, "tienda": ti, "host": ti.lower().replace(" ", "") + ".es",
               "temp": 100, "publicado": int(time.time() - d * 86400), "caducado": c}
              for i, (t, pr, ti, d, c) in enumerate(hilos)]
        return {"hilos": hs, "fresca": True, "por_clave": {}, "idx": v._indice_copia(hs) if indice else None}

    def setUp(self):
        v._INDICE.update(firma=None, idx=None)

    def p(self, tienda="amazon.es", precio=50.0):
        return {"nombre": "Auriculares Sony WH-1000XM5 inalámbricos", "tienda": tienda, "precio": precio}

    def test_anterior_de_mas_de_un_mes(self):
        t = "Sony WH-1000XM5 auriculares"
        # Precio distinto: se mantiene el nuevo. Mismo precio y agotado: el nuevo. Mismo precio sin agotar: el antiguo.
        self.assertEqual(v.estado_chollometro(self.p(), self.copia((t, 60.0, "Amazon", 40, 0))), "no")
        self.assertEqual(v.estado_chollometro(self.p(), self.copia((t, 50.0, "Amazon", 40, 1))), "no")
        self.assertEqual(v.estado_chollometro(self.p(), self.copia((t, 50.0, "Amazon", 40, 0))), "misma")
        self.assertEqual(v.estado_chollometro(self.p(), self.copia((t, 50.0, "MediaMarkt", 40, 0))), "no",
                         "un hilo viejo de otra tienda no cuenta")

    def test_anterior_de_menos_de_un_mes(self):
        t = "Sony WH-1000XM5 auriculares"
        c = self.copia((t, 52.0, "Amazon", 5, 1))
        self.assertTrue(v.ya_publicado_igual_o_mas_barato(v.estado_chollometro(self.p(), c), 50.0),
                        "baja menos de un 5 %: se mantiene el antiguo")
        self.assertFalse(v.ya_publicado_igual_o_mas_barato(v.estado_chollometro(self.p(precio=45.0), c), 45.0))

    def test_otra_tienda_y_grupo_corte_ingles(self):
        t = "Sony WH-1000XM5 auriculares"
        self.assertEqual(v.estado_chollometro(self.p(), self.copia((t, 45.0, "MediaMarkt", 5, 1))), "no",
                         "regla 6: la misma oferta en otra tienda se permite; una ya agotada no la empeora")
        self.assertEqual(v.estado_chollometro(self.p(), self.copia((t, 45.0, "MediaMarkt", 5, 0))), "otra:45")
        self.assertEqual(v.estado_chollometro(self.p("hipercor.es"), self.copia((t, 50.0, "El Corte Ingles", 5, 0))),
                         "misma", "El Corte Inglés e Hipercor comparten stock")

    def test_indice_da_lo_mismo(self):
        hilos = [("Sony WH-1000XM5 auriculares", 50.0, "Amazon", 3, 0), ("Bose QC45", 50.0, "Amazon", 3, 0),
                 ("Auriculares Sony inalámbricos negros", 49.0, "Amazon", 2, 0)]
        con_idx, sin_idx = self.copia(*hilos), self.copia(*hilos, indice=False)
        for p in (self.p(), {**self.p(), "nombre": "Bose QuietComfort 45"}, {**self.p(), "nombre": "Tostadora"}):
            self.assertEqual(v.estado_chollometro(dict(p), con_idx), v.estado_chollometro(dict(p), sin_idx), p["nombre"])


if __name__ == "__main__":
    unittest.main()
