"""Contrastes auditables. No hace red ni decide umbrales por su cuenta."""
import hashlib
import json
from datetime import datetime, timedelta


def preparar(con):
    con.execute('''CREATE TABLE IF NOT EXISTS contrastes (
        clave TEXT NOT NULL, fecha TEXT NOT NULL, datos TEXT NOT NULL,
        PRIMARY KEY(clave, fecha))''')


def clave(p):
    # No reutilizar entre tiendas, versiones, precios, cupones o vendedores distintos.
    campos = {k: p.get(k) for k in ('ean', 'nombre', 'url', 'tienda', 'precio',
                                   'variante', 'estado_producto', 'vendedor', 'cupon', 'envio')}
    return hashlib.sha256(json.dumps(campos, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def guardar(con, p, resultado, ahora=None):
    preparar(con)
    fecha = ahora or datetime.now().isoformat(timespec='microseconds')
    con.execute('INSERT OR REPLACE INTO contrastes VALUES (?,?,?)',
                (clave(p), fecha, json.dumps(resultado, ensure_ascii=False)))
    con.commit()
    return resultado


def recuperar(con, p, horas=6, ahora=None):
    preparar(con)
    ahora = ahora or datetime.now()
    fila = con.execute('SELECT fecha, datos FROM contrastes WHERE clave=? ORDER BY fecha DESC LIMIT 1',
                       (clave(p),)).fetchone()
    if fila and ahora - timedelta(hours=horas) <= datetime.fromisoformat(fila[0]) <= ahora:
        return json.loads(fila[1])
    return None


def consultas(p, modelo):
    valores = [p.get('ean') or modelo(p), p.get('nombre')]
    return list(dict.fromkeys(str(x).strip() for x in valores if x and str(x).strip()))


def resolver(p, fuentes, buscar, confirmar, coincide, misma_tienda, ahora=None):
    """Dos comparadores como pistas y como máximo dos fichas de otras tiendas.

    confirmar devuelve evidencia de la ficha, nunca del resultado de búsqueda.
    Los equivalentes se conservan aparte y jamás producen precio de mercado.
    """
    fecha = ahora or datetime.now().isoformat(timespec='seconds')
    intentos, refs, vistas = [], [], set()
    consultas_p = p.get('_consultas_contraste') or [p.get('ean') or p['nombre']]
    for fuente in fuentes:
        for consulta in consultas_p:
            try:
                pistas = buscar(fuente, consulta) or []
                intentos.append({'fuente': fuente, 'consulta': consulta, 'resultado': 'consultado', 'fecha': fecha})
            except Exception as exc:
                intentos.append({'fuente': fuente, 'consulta': consulta, 'resultado': 'no disponible',
                                 'error': type(exc).__name__, 'fecha': fecha})
                continue
            for pista in pistas:
                url = pista.get('url')
                if not url or url in vistas or len(vistas) >= 2:
                    continue
                if not coincide(p, pista):
                    continue
                vistas.add(url)
                try:
                    r = confirmar(url) or {}
                except Exception as exc:
                    intentos.append({'fuente': fuente, 'url': url, 'resultado': 'ficha no verificada',
                                     'error': type(exc).__name__, 'fecha': fecha})
                    continue
                r = dict(r, fuente=fuente, fecha=fecha, url=r.get('url') or url)
                if r.get('tipo') == 'equivalente':
                    r['resultado'] = valorar_equivalente(p, r)
                elif not coincide(p, r) or misma_tienda(p.get('tienda', ''), r.get('tienda', '')):
                    r['resultado'] = 'otro producto o propia tienda'
                elif r.get('estado_producto', 'nuevo') != p.get('estado_producto', 'nuevo'):
                    r['resultado'] = 'estado distinto'
                elif not r.get('precio') or r.get('stock') != 'si' or not r.get('vendedor'):
                    r['resultado'] = 'condiciones no verificadas'
                else:
                    r['resultado'] = 'referencia alternativa'
                refs.append(r)
            if len(vistas) >= 2:
                break
        if len(vistas) >= 2:
            break
    validas = [r for r in refs if r['resultado'] == 'referencia alternativa']
    return {'estado': 'referencia alternativa' if validas else 'no medido',
            'precio_actual': p.get('precio'), 'habitual': None, 'minimo': None,
            'mercado': min((r['precio'] for r in validas), default=None),
            'referencias': refs, 'intentos': intentos, 'fecha': fecha,
            'stock': p.get('stock') or 'no verificado'}


CAMPOS_EQUIVALENTES = {
    'portatil': ('cpu', 'gpu', 'ram', 'ssd', 'pantalla'),
    'altavoz': ('potencia', 'bateria', 'resistencia', 'funciones'),
}

def valorar_equivalente(p, referencia):
    """Comparar prestaciones explícitas, sin convertirlas en % ni aprobar.
    Solo sirve para categorías y campos medidos; una marca más cara no prueba nada.
    """
    campos = CAMPOS_EQUIVALENTES.get(p.get('categoria'), ())
    a, b = p.get('specs') or {}, referencia.get('specs') or {}
    if not campos or any(a.get(k) is None or b.get(k) is None for k in campos):
        return 'equivalente no medido: faltan prestaciones verificadas'
    return 'equivalente, sin descuento entre modelos'
