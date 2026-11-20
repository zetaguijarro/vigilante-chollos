"""Cobertura determinista de búsqueda web y equivalentes, sin llamadas externas."""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
import busqueda_contraste as b
import contraste as c
import vigilante as v


class Busqueda(unittest.TestCase):
    def setUp(self):
        b._pausa_hasta = 0
        b._ultima_busqueda = 0
        self.specs = {'potencia': '30 W', 'bateria': '12 h', 'resistencia': 'IP67', 'funciones': 'Bluetooth'}
        self.p = {'nombre': 'Altavoz Marca X100', 'ean': '1234567890123', 'precio': 50,
                  'tienda': 'origen.test', 'url': 'https://origen.test/x100', 'specs': self.specs}

    def tearDown(self):
        b._pausa_hasta = 0
        b._ultima_busqueda = 0

    def test_busqueda_rss_solo_pistas_y_https(self):
        xml = '<rss><channel><item><title>Altavoz X100</title><link>https://otra.test/x100</link></item><item><title>No</title><link>http://localhost/x</link></item></channel></rss>'
        llamadas = []
        def bajar(url):
            llamadas.append(url)
            return xml, url
        r = b.enlaces_web('Altavoz X100', bajar)
        self.assertEqual(len(r), 1)
        self.assertNotIn('precio', r[0])
        self.assertIn('format=rss', llamadas[0])
        self.assertIn('Altavoz+X100', llamadas[0])

    def test_reto_activa_pausa_sin_reintentar(self):
        with patch.object(b.time, 'sleep'), patch.object(b.time, 'monotonic', return_value=100):
            bajar = unittest.mock.Mock(return_value=('<html>reto</html>', 'https://buscador.test'))
            with self.assertRaises(b.BusquedaNoDisponible):
                b.enlaces_web('modelo', bajar)
            with self.assertRaises(b.BusquedaNoDisponible):
                b.enlaces_web('otro', bajar)
            self.assertEqual(bajar.call_count, 1)

    def test_no_inventa_specs_desde_nombre(self):
        self.assertEqual(b.prestaciones(['Altavoz 30 W IP67'], 'altavoz'), {})
        self.assertIsNone(b.consulta_equivalente(dict(self.p, specs=None)))

    def test_consulta_equivalente_no_usa_marca(self):
        q = b.consulta_equivalente(self.p)
        self.assertNotIn('Marca', q)
        self.assertIn('30 W', q)

    def test_extraccion_specs_etiquetadas(self):
        self.assertEqual(b.prestaciones(['Potencia: 30 W', 'Autonomía: 12 h', 'Protección: IP67',
                                         'Conectividad: Bluetooth'], 'altavoz'), self.specs)

    def test_equivalencia_con_diferencias_no_es_aprobacion(self):
        r = dict(self.p, nombre='Altavoz Otra Y200', precio=45, specs=dict(self.specs, potencia='20 W'))
        out = b.comparar(self.p, r)
        self.assertFalse(out['equivalencia_verificada'])
        self.assertFalse(out['aprobacion_automatica'])
        self.assertEqual(out['diferencia_precio'], -5)
        self.assertEqual(len(out['diferencias']), 4)
        self.assertNotIn('descuento', out)

    def test_igualdad_prestaciones_verificada_no_precio_mercado(self):
        ref = dict(self.p, nombre='Altavoz Otra Y200', precio=60, specs=self.specs, stock='si',
                   vendedor='Otra', tienda='otra.test', url='https://otra.test/y200', tipo='equivalente')
        out = c.resolver(self.p, ['Web equivalentes'], lambda f, q: [ref], lambda u: ref,
                         lambda a, b: False, lambda a, b: a == b)
        self.assertIsNone(out['mercado'])
        self.assertIsNone(out['habitual'])
        self.assertIsNone(out['minimo'])
        self.assertTrue(out['referencias'][0]['equivalencia_verificada'])
        self.assertEqual(out['estado'], 'no medido')

    def test_equivalente_sin_stock_no_se_verifica(self):
        ref = dict(self.p, nombre='Altavoz Otra Y200', stock=None, vendedor='Otra',
                   tienda='otra.test', url='https://otra.test/y200', tipo='equivalente')
        r = c.resolver(self.p, ['Web'], lambda f, q: [ref], lambda u: ref,
                       lambda a, b: False, lambda a, b: False)
        self.assertFalse(r['referencias'][0]['equivalencia_verificada'])

    def test_integracion_sin_comparadores_busca_web_y_confirma_ficha(self):
        con = sqlite3.connect(':memory:')
        pista = {'nombre': self.p['nombre'], 'ean': self.p['ean'], 'url': 'https://otra.test/x100', 'tienda': 'otra.test'}
        ficha = {'titulo': self.p['nombre'], 'ean': self.p['ean'], 'precio_ahora': 80,
                 'stock': 'sí', 'vendedor': 'Otra'}
        with patch.object(v, 'descargar', return_value=('<html/>', pista['url'])), \
             patch.object(v, 'extraer', return_value=[]), \
             patch.object(v, 'analizar_ficha', return_value=ficha), \
             patch.object(b, 'enlaces_web', return_value=[pista]) as web:
            r = v.contraste_alternativo(con, self.p)
        self.assertEqual(r['mercado'], 80)
        self.assertEqual(r['referencias'][0]['fuente'], 'Web')
        self.assertTrue(web.called)
        con.close()

    def test_integracion_sin_exacto_busca_modelo_equivalente(self):
        con = sqlite3.connect(':memory:')
        pista = {'nombre': 'Altavoz Otra Y200', 'url': 'https://otra.test/y200', 'tienda': 'otra.test'}
        ficha = {'titulo': pista['nombre'], 'precio_ahora': 45, 'stock': 'sí', 'vendedor': 'Otra', 'specs': self.specs}
        def web(q, descargar):
            return [pista] if 'comprar precio España' in q else []
        with patch.object(v, 'descargar', return_value=('<html/>', pista['url'])), \
             patch.object(v, 'extraer', return_value=[]), \
             patch.object(v, 'analizar_ficha', return_value=ficha), patch.object(b, 'enlaces_web', side_effect=web):
            r = v.contraste_alternativo(con, self.p)
        self.assertIsNone(r['mercado'])
        self.assertEqual(r['referencias'][0]['tipo'], 'equivalente')
        self.assertEqual(len(r['referencias'][0]['diferencias']), 4)
        self.assertFalse(r['referencias'][0]['aprobacion_automatica'])
        with patch.object(v, 'descargar', side_effect=AssertionError('cache')):
            self.assertEqual(v.contraste_alternativo(con, self.p), r)
        con.close()


if __name__ == '__main__':
    unittest.main()
