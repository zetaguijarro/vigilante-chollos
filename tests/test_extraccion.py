"""Extracción declarativa por tienda (src/extraccion.py; revisión del 20/11/2026, propuesta 3). Sin conectar con tiendas."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ / "src"))
import extraccion as e  # noqa: E402
import vigilante as v  # noqa: E402

TOYS = """<div class="product__listing">
<a href="/Calzado-Infantil/c/calzado">Calzado Infantil 2-10 años</a> <span>49,00 €</span>
<div class="product-item" data-product-code="K1">
  <div class="details"><div class="product-brand">LEGO</div>
  <a class="name" href="/LEGO-Icons-Esquina-Tudor-10350/p/K1">LEGO Icons - Esquina Tudor - 10350</a>
  <div class="price">244,99 €</div></div></div>
<div class="product-item" data-product-code="K2">
  <div class="details"><div class="product-brand">HOT WHEELS</div>
  <a class="name" href="/Hot-Wheels-Maletin-K2/p/K2">Maletín de transporte 100 coches</a>
  <div class="price">39,99 €</div></div></div>
</div>"""


class Selectores(unittest.TestCase):
    def test_clase_atributo_descendiente_indice_y_atributo(self):
        raiz = e.arbol('<article class="c x"><h3><span>Nike</span><span>AIR MAX - Zapatillas</span></h3>'
                       '<a data-id="7" href="/p/1">ver</a><img src="a.jpg"><p>texto <b>en negrita</b></p></article>')
        art = e.seleccionar(raiz, "article.c")[0]
        self.assertEqual(e.valor(art, "h3 span:0"), "Nike")
        self.assertEqual(e.valor(art, "h3 span:1"), "AIR MAX - Zapatillas")
        self.assertEqual(e.valor(art, "a[data-id=7]@href"), "/p/1")
        self.assertEqual(e.valor(art, "a[href^=/p/]@href"), "/p/1")
        self.assertEqual(e.valor(art, "p"), "texto en negrita")
        self.assertIsNone(e.valor(art, "h3 span:5"))

    def test_html_mal_cerrado_no_rompe(self):
        raiz = e.arbol("<div class='t'><p>uno<p>dos</div><div class='t'>tres")
        self.assertEqual([n.texto() for n in e.seleccionar(raiz, "div.t")], ["uno dos", "tres"])


class ReglasPorTienda(unittest.TestCase):
    def test_toysrus_marca_aparte_y_sin_enlaces_de_categoria(self):
        ps = v.extraer(TOYS, "https://www.toysrus.es/LEGO/c/LEGO")
        nombres = {p["nombre"]: p.get("marca") for p in ps}
        self.assertNotIn("Calzado Infantil 2-10 años", nombres, "solo_tarjetas: una categoría no es un producto")
        self.assertEqual(nombres.get("LEGO Icons - Esquina Tudor - 10350"), "LEGO", "la marca ya estaba en el título: no se repite")
        self.assertEqual(nombres.get("HOT WHEELS Maletín de transporte 100 coches"), "HOT WHEELS", "la marca de su elemento propio")

    def test_tienda_sin_reglas_sigue_con_el_generico(self):
        self.assertIsNone(e.reglas_de("otratienda.es"))

    def test_las_reglas_del_proyecto_son_validas(self):
        datos = json.loads((RAIZ / "config" / "extraccion.json").read_text(encoding="utf-8"))
        for tienda, r in datos.items():
            if tienda.startswith("_"):
                continue
            self.assertIn("tarjeta", r, tienda)
            self.assertIn("titulo", r, tienda)

    def test_reglas_desde_otro_archivo(self):
        tmp = Path(tempfile.mkdtemp()) / "r.json"
        tmp.write_text(json.dumps({"x.es": {"tarjeta": "li", "titulo": "h2"}}), encoding="utf-8")
        r = e.reglas_de("x.es", tmp)
        sal = e.tarjetas('<ul><li><a href="/a">a</a><h2>Producto A</h2></li><li><h2>Sin enlace</h2></li></ul>', "https://x.es/", r)
        self.assertEqual(sal, [{"url": "https://x.es/a", "titulo": "Producto A", "marca": None}])


if __name__ == "__main__":
    unittest.main()
