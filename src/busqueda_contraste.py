"""Búsqueda pública y comparación de prestaciones. Sin API ni servicios pagados."""
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus, urlsplit


class BusquedaNoDisponible(Exception):
    pass


_pausa_hasta = 0
_ultima_busqueda = 0


def enlaces_web(consulta, descargar):
    """Bing RSS, una lectura sin sesión. Los resultados solo son pistas."""
    global _pausa_hasta, _ultima_busqueda
    if time.monotonic() < _pausa_hasta:
        raise BusquedaNoDisponible('buscador en pausa')
    url = 'https://www.bing.com/search?format=rss&q=' + quote_plus(consulta)
    espera = 3 - (time.monotonic() - _ultima_busqueda)
    if espera > 0:
        time.sleep(espera)
    _ultima_busqueda = time.monotonic()
    try:
        html, final = descargar(url)
        raiz = ET.fromstring(html)
        if raiz.tag != 'rss':
            raise ET.ParseError('respuesta no RSS')
    except Exception as exc:
        # No resolver retos ni buscar rutas para saltarlos. Pausa fija, sin reintento aquí.
        _pausa_hasta = time.monotonic() + 1800
        raise BusquedaNoDisponible('buscador no disponible, pausa de 30 minutos') from exc
    salida = []
    for item in raiz.findall('./channel/item')[:10]:
        enlace = item.findtext('link') or ''
        partes = urlsplit(enlace)
        host = partes.hostname or ''
        # No ejecutar esquemas ni destinos internos tomados de resultados externos.
        if partes.scheme != 'https' or not host or host == 'localhost' or ':' in host or re.fullmatch(r'[\d.]+', host):
            continue
        salida.append({'nombre': item.findtext('title') or '', 'url': enlace,
                       'tienda': host, 'fuente_busqueda': final})
    return salida


PATRONES = {
    'cpu': r'procesador|\bcpu\b',
    'gpu': r'gr[aá]fica|\bgpu\b',
    'ram': r'\bram\b|memoria del sistema',
    'ssd': r'\bssd\b|almacenamiento',
    'pantalla': r'pantalla|display',
    'potencia': r'potencia|power output',
    'bateria': r'autonom[ií]a|duraci[oó]n.*bater|battery life|playtime',
    'resistencia': r'resistencia|protecci[oó]n|waterproof|\bip[0-9x]{2}\b',
    'funciones': r'funciones|conectividad|bluetooth|wifi|wi-fi',
}
CAMPOS = {'portatil': ('cpu', 'gpu', 'ram', 'ssd', 'pantalla'),
          'altavoz': ('potencia', 'bateria', 'resistencia', 'funciones')}


def categoria(p):
    nombre = p.get('nombre', '').lower()
    if re.search(r'port[aá]til|laptop|notebook', nombre):
        return 'portatil'
    if re.search(r'altavoz|speaker', nombre):
        return 'altavoz'
    return p.get('categoria') if p.get('categoria') in CAMPOS else None


def prestaciones(datos, clase):
    """Solo etiquetas explícitas. No convertir una marca o título en especificaciones."""
    if isinstance(datos, dict):
        return {k: datos[k] for k in CAMPOS.get(clase, ()) if datos.get(k) is not None}
    salida = {}
    for dato in datos or []:
        etiqueta, sep, valor = str(dato).partition(':')
        if not sep or not valor.strip():
            continue
        for campo in CAMPOS.get(clase, ()):
            if campo not in salida and re.search(PATRONES[campo], etiqueta, re.I):
                salida[campo] = valor.strip()
    return salida


def consulta_equivalente(p):
    clase = categoria(p)
    specs = prestaciones(p.get('specs'), clase)
    if not clase or not specs:
        return None
    # Sin marca y sin anclar al modelo original. Prestaciones verificadas en su ficha.
    terminos = ['portátil' if clase == 'portatil' else 'altavoz portátil']
    for campo in CAMPOS[clase][:3]:
        if campo in specs:
            terminos.append(str(specs[campo])[:60])
    return ' '.join(terminos) + ' comprar precio España'


def comparar(p, r):
    clase = categoria(p)
    if not clase or categoria(r) != clase:
        return {'resultado': 'equivalente no medido: categoría distinta o desconocida', 'diferencias': []}
    a, b = prestaciones(p.get('specs'), clase), prestaciones(r.get('specs'), clase)
    diferencias = [{'campo': k, 'candidata': a.get(k), 'alternativa': b.get(k),
                    'igual': str(a[k]).casefold() == str(b[k]).casefold() if k in a and k in b else None}
                   for k in CAMPOS[clase]]
    completo = all(k in a and k in b for k in CAMPOS[clase])
    # Diferentes CPU/GPU o perfiles requieren revisión, no una jerarquía inventada.
    iguales = completo and all(d['igual'] for d in diferencias)
    return {'resultado': ('equivalente verificado, sin descuento entre modelos' if iguales else
                           'equivalente propuesto: diferencias o prestaciones por verificar'),
            'diferencias': diferencias, 'prestaciones_completas': completo,
            'equivalencia_verificada': iguales, 'aprobacion_automatica': False,
            'precio_candidata': p.get('precio'), 'precio_alternativa': r.get('precio'),
            'diferencia_precio': round(r['precio'] - p['precio'], 2)
            if isinstance(r.get('precio'), (int, float)) and isinstance(p.get('precio'), (int, float)) else None}
