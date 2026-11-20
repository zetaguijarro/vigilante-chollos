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
    intentos, refs, vistas, tiendas = [], [], set(), set()
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
                if not coincide(p, pista) and pista.get('tipo') != 'equivalente':
                    continue
                tienda_pista = pista.get('tienda')
                if tienda_pista and (tienda_pista in tiendas or misma_tienda(p.get('tienda', ''), tienda_pista)):
                    continue
                vistas.add(url)
                if tienda_pista:
                    tiendas.add(tienda_pista)
                try:
                    r = confirmar(url) or {}
                except Exception as exc:
                    refs.append({'fuente': fuente, 'url': url, 'tienda': tienda_pista,
                                 'precio': None, 'variante': None, 'fecha': fecha,
                                 'resultado': 'ficha no verificada', 'error': type(exc).__name__})
                    continue
                r = dict(r, fuente=fuente, fecha=fecha, url=r.get('url') or url)
                for campo in ('tienda', 'precio', 'variante'):
                    r.setdefault(campo, None)
                r['precio_sin_cupon'] = r.get('precio')
                if r.get('cupon'):
                    # Un código sin importe o elegibilidad no permite afirmar ventaja.
                    if r.get('cupon_verificado') and isinstance(r.get('precio_con_cupon'), (int, float)):
                        r['precio'] = r['precio_con_cupon']
                    else:
                        r['condiciones_pendientes'] = True
                envio = r.get('envio_importe')
                r['total'] = round(r['precio'] + envio, 2) if isinstance(r.get('precio'), (int, float)) and isinstance(envio, (int, float)) else None
                if r.get('tipo') == 'equivalente':
                    r['resultado'] = valorar_equivalente(p, r)
                elif not coincide(p, r) or misma_tienda(p.get('tienda', ''), r.get('tienda', '')):
                    r['resultado'] = 'otro producto o propia tienda'
                elif r.get('estado_producto', 'nuevo') != p.get('estado_producto', 'nuevo'):
                    r['resultado'] = 'estado distinto'
                elif (not isinstance(r.get('precio'), (int, float)) or r['precio'] <= 0
                      or r.get('stock') != 'si' or not r.get('vendedor') or r.get('condiciones_pendientes')):
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
            'etiqueta': 'referencia alternativa',
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
