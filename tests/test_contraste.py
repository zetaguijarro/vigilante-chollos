"""Pruebas sin red ni datos de producción del contraste alternativo."""
import sys
import sqlite3
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
import contraste as c
import vigilante as v


class Contrastes(unittest.TestCase):
    def setUp(self):
        self.p = {'nombre': 'Altavoz Marca X100', 'ean': '1234567890123', 'precio': 50,
                  'url': 'https://origen.test/x100', 'tienda': 'origen.test'}
        self.ref = {'nombre': self.p['nombre'], 'ean': self.p['ean'], 'precio': 100,
                    'url': 'https://otra.test/x100', 'tienda': 'otra.test', 'stock': 'si',
                    'vendedor': 'Otra', 'envio': 'no verificado', 'variante': 'negro'}

    def resolver(self, ref=None, pistas=None):
        return c.resolver(self.p, ['principal', 'alternativo'],
                          lambda f, q: pistas or [self.ref], lambda u: ref or self.ref,
                          lambda a, b: a['ean'] == b.get('ean'), lambda a, b: a == b)

    def test_evidencia_de_ficha_no_del_resultado(self):
        r = self.resolver(dict(self.ref, precio=120))
        self.assertEqual(r['mercado'], 120)
        self.assertIsNone(r['habitual'])
        self.assertIsNone(r['minimo'])
        self.assertEqual(r['referencias'][0]['envio'], 'no verificado')

    def test_stock_desconocido_pendiente_no_confirmado(self):
        r = self.resolver(dict(self.ref, stock='no verificado'))
        self.assertEqual(r['estado'], 'no medido')
        self.assertIsNone(r['mercado'])

    def test_no_utiliza_ficha_otro_modelo(self):
        self.assertIsNone(self.resolver(dict(self.ref, ean='999'))['mercado'])

    def test_no_utiliza_propia_tienda(self):
        self.assertIsNone(self.resolver(dict(self.ref, tienda='origen.test'))['mercado'])

    def test_reacondicionado_no_es_nuevo(self):
        self.assertIsNone(self.resolver(dict(self.ref, estado_producto='reacondicionado'))['mercado'])

    def test_equivalente_no_produce_descuento(self):
        r = self.resolver(dict(self.ref, tipo='equivalente'))
        self.assertIsNone(r['mercado'])
        self.assertIn('equivalente', r['referencias'][0]['resultado'])

    def test_no_infiere_vendedor(self):
        self.assertIsNone(self.resolver(dict(self.ref, vendedor=None))['mercado'])

    def test_maximo_dos_fichas(self):
        llamadas = []
        pistas = [dict(self.ref, url='https://otra%d.test/x100' % i, tienda='otra%d.test' % i) for i in range(8)]
        def confirmar(u):
            llamadas.append(u)
            return dict(self.ref, url=u)
        c.resolver(self.p, ['uno', 'dos'], lambda f, q: pistas, confirmar,
                   lambda a, b: True, lambda a, b: False)
        self.assertEqual(len(llamadas), 2)

    def test_cupon_sin_importe_no_inventa_ventaja(self):
        r = self.resolver(dict(self.ref, cupon=['AHORRA20']))
        self.assertIsNone(r['mercado'])
        self.assertEqual(r['estado'], 'no medido')

    def test_cupon_verificado_y_envio_separado(self):
        r = self.resolver(dict(self.ref, cupon='DTO20', cupon_verificado=True,
                               precio_con_cupon=80, envio_importe=4))
        self.assertEqual(r['mercado'], 80)
        self.assertEqual(r['referencias'][0]['total'], 84)
        self.assertEqual(r['referencias'][0]['precio_sin_cupon'], 100)

    def test_fichas_de_dos_tiendas_no_dos_urls_de_la_misma(self):
        llamadas = []
        pistas = [dict(self.ref, url='https://otra.test/' + str(i)) for i in range(3)]
        def confirmar(u):
            llamadas.append(u)
            return dict(self.ref, url=u)
        c.resolver(self.p, ['uno'], lambda f, q: pistas, confirmar,
                   lambda a, b: True, lambda a, b: False)
        self.assertEqual(len(llamadas), 1)

    def test_ficha_fallida_guarda_trazabilidad(self):
        def fallo(u):
            raise OSError('sin red')
        r = c.resolver(self.p, ['uno'], lambda f, q: [self.ref], fallo,
                        lambda a, b: True, lambda a, b: False)
        self.assertEqual(r['referencias'][0]['resultado'], 'ficha no verificada')
        self.assertIn('variante', r['referencias'][0])

    def test_fallo_fuente_no_descarta_ni_bloquea(self):
        def fallo(f, q):
            raise OSError('sin conexión')
        r = c.resolver(self.p, ['a', 'b'], fallo, lambda u: {}, lambda a, b: True, lambda a, b: False)
        self.assertEqual(r['estado'], 'no medido')
        self.assertEqual(len(r['intentos']), 2)

    def test_persistencia_caducidad_y_precio(self):
        con = sqlite3.connect(':memory:')
        ahora = datetime(2026, 11, 20, 20, 0)
        r = self.resolver()
        c.guardar(con, self.p, r, ahora.isoformat())
        self.assertEqual(c.recuperar(con, self.p, ahora=ahora), r)
        self.assertIsNone(c.recuperar(con, dict(self.p, precio=51), ahora=ahora))
        self.assertIsNone(c.recuperar(con, self.p, ahora=ahora + timedelta(hours=7)))
        self.assertIsNone(c.recuperar(con, self.p, ahora=ahora - timedelta(hours=1)))
        con.close()

    def test_orden_consultas(self):
        self.assertEqual(c.consultas(self.p, lambda p: 'Marca X100'),
                         [self.p['ean'], self.p['nombre']])

    def test_integracion_cache_no_vuelve_a_red(self):
        con = sqlite3.connect(':memory:')
        r = self.resolver()
        c.guardar(con, self.p, r)
        with patch.object(v, 'descargar', side_effect=AssertionError('no red')):
            self.assertEqual(v.contraste_alternativo(con, self.p), r)
        con.close()

    def test_integracion_comparador_es_solo_pista(self):
        con = sqlite3.connect(':memory:')
        p = dict(self.p, marca='Marca')
        ficha = {'titulo': self.p['nombre'], 'ean': self.p['ean'], 'precio_ahora': 110,
                 'stock': 'sí', 'vendedor': 'Otra'}
        with patch.object(v, 'descargar', return_value=('<html/>', self.ref['url'])), \
             patch.object(v, 'extraer', return_value=[self.ref]), \
             patch.object(v, 'analizar_ficha', return_value=ficha):
            r = v.contraste_alternativo(con, p)
        self.assertEqual(r['mercado'], 110)
        self.assertEqual(r['estado'], 'referencia alternativa')
        con.close()

    def test_equivalentes_exigen_datos_no_marca(self):
        p = dict(self.p, categoria='altavoz')
        self.assertIn('no medido', c.valorar_equivalente(p, self.ref))
        specs = dict(potencia=30, bateria=12, resistencia='IP67', funciones=['bluetooth'])
        self.assertEqual(c.valorar_equivalente(dict(p, specs=specs), dict(self.ref, specs=specs)),
                         'equivalente, sin descuento entre modelos')

    def test_no_medido_no_descarta_modelo(self):
        item = dict(num=1, n='Altavoz JBL Xtreme 4', t='tienda.test', p=50, validada=True,
                    ficha={'titulo': 'Altavoz JBL Xtreme 4', 'precio_ahora': 50}, ch='no')
        decision = v.decidir_item(item, {'filtros_codigo': {}})
        self.assertEqual(decision['veredicto'], 'sin_validar')
        self.assertEqual(decision['contraste'], 'no medido')
        self.assertNotIn('memoria', decision)

    def test_pvpr_no_sirve_solo_para_aprobar(self):
        item = dict(num=1, n='Altavoz JBL Xtreme 4', t='tienda.test', p=50, a=500, validada=True,
                    ficha={'precio_ahora': 50}, ch='no')
        self.assertNotEqual(v.decidir_item(item, {'filtros_codigo': {}})['veredicto'], 'pasa')


if __name__ == '__main__':
    unittest.main()
