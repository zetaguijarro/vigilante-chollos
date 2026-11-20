"""Rutas de campaña (Zeta, 20/11/2026): qué da cada ruta, cuándo retirarla y cómo descubrir las nuevas desde los hubs.

Tres cosas, todas con fecha y motivo en la tabla `rutas` de vistos.db:
1. Rendimiento: por ruta, lecturas, productos, nuevos y cuántos pasan los filtros de tienda. Las que más dan se leen antes
   (`factor_rutas`, que `catalogo_turno` multiplica por el peso de demanda); las que no dan nada, menos.
2. Limpieza: una ruta sale del catálogo tras 3 lecturas seguidas sin productos o 2 que redirigen a la portada. Los 404,
   antibot y robots ya salían solos. Una ruta retirada no se vuelve a sembrar en 14 días.
3. Hubs: una vez al día se lee el hub de campaña de cada tienda (Black Friday, promociones), se sacan los enlaces
   candidatos y solo entra al catálogo una ruta que traiga productos que el vigilante no tenía (50 % o más nuevos).
No se inventan rutas: solo se usan enlaces reales de la página del hub.
"""
import json
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, urlunsplit

VACIAS_PARA_RETIRAR = 3
PORTADAS_PARA_RETIRAR = 2
DIAS_SIN_RESEMBRAR = 14
MIN_PRODUCTOS_RUTA_NUEVA = 8
FRACCION_NUEVA = 0.5
MAX_CANDIDATAS_POR_HUB = 4
HUB_CADA_HORAS = 24
# Un enlace del hub es candidato si su ruta habla de campaña u oferta y no es la ficha de un producto.
RE_CAMPANA = re.compile(r"black-?friday|black-?days|blackweek|cyber|promoc|oferta|outlet|rebajas|campaign|/cp/|/event/|"
                        r"/list/|/promotion|/sale|/deals|descuento|semanas-black", re.I)
RE_PRODUCTO = re.compile(r"/(?:product|producto|productos|p|dp|item|articulo|art)/", re.I)
RE_IDIOMA = re.compile(r"^(?:es|es-es|es_es|en-es|ca|eu|gl)$", re.I)

CREAR = ("CREATE TABLE IF NOT EXISTS rutas (tienda TEXT, url TEXT, origen TEXT, desde TEXT, ultima TEXT, "
         "lecturas INTEGER DEFAULT 0, vacias_seguidas INTEGER DEFAULT 0, portadas_seguidas INTEGER DEFAULT 0, "
         "productos_ult INTEGER, productos_tot INTEGER DEFAULT 0, nuevos_tot INTEGER DEFAULT 0, filtros_tot INTEGER DEFAULT 0, "
         "estado TEXT DEFAULT 'activa', motivo TEXT, retirada TEXT, PRIMARY KEY (tienda, url))")


def crear_tabla(con):
    con.execute(CREAR)


def redirige_a_portada(pedida, final):
    """¿La ruta pedida (campaña o categoría con 2 o más tramos) acabó en la portada de la tienda o en la de su idioma?"""
    p, f = urlparse(pedida or ""), urlparse(final or "")
    tramos = lambda u: [t for t in u.path.split("/") if t]
    if not p.netloc or p.netloc.split(".")[-2:] != f.netloc.split(".")[-2:]:
        return False
    sp, sf = tramos(p), tramos(f)
    return len(sp) >= 2 and (not sf or (len(sf) == 1 and bool(RE_IDIOMA.match(sf[0]))))


def registrar_lectura(con, tienda, url, productos, error=None, nuevos=0, pasan=0, origen="catalogo", ahora=None):
    """Apunta una lectura de una ruta. -> motivo de retirada ("3 lecturas seguidas sin productos"...) o None."""
    crear_tabla(con)
    ahora = ahora or datetime.now().isoformat(timespec="minutes")
    con.execute("INSERT OR IGNORE INTO rutas (tienda, url, origen, desde) VALUES (?,?,?,?)", (tienda, url, origen, ahora))
    vacias = portadas = 0
    if error and "portada" in error:
        portadas = 1
    elif not error and productos == 0:
        vacias = 1
    fila = con.execute("SELECT vacias_seguidas, portadas_seguidas, estado FROM rutas WHERE tienda=? AND url=?", (tienda, url)).fetchone()
    v_ant, p_ant, estado = fila
    nuevas_vacias = (v_ant + 1) if vacias else 0
    nuevas_portadas = (p_ant + 1) if portadas else (0 if not error else p_ant)
    con.execute("UPDATE rutas SET ultima=?, lecturas=lecturas+?, productos_ult=?, productos_tot=productos_tot+?, "
                "nuevos_tot=nuevos_tot+?, filtros_tot=filtros_tot+?, vacias_seguidas=?, portadas_seguidas=? "
                "WHERE tienda=? AND url=?",
                (ahora, 0 if error else 1, None if error else productos, 0 if error else productos, nuevos, pasan,
                 nuevas_vacias, nuevas_portadas, tienda, url))
    motivo = None
    if nuevas_vacias >= VACIAS_PARA_RETIRAR:
        motivo = f"{nuevas_vacias} lecturas seguidas sin productos"
    elif nuevas_portadas >= PORTADAS_PARA_RETIRAR:
        motivo = f"redirige a la portada ({nuevas_portadas} veces)"
    if motivo:
        marcar_retirada(con, tienda, url, motivo, ahora)
    return motivo


def marcar_retirada(con, tienda, url, motivo, ahora=None):
    crear_tabla(con)
    ahora = ahora or datetime.now().isoformat(timespec="minutes")
    con.execute("INSERT OR IGNORE INTO rutas (tienda, url, origen, desde) VALUES (?,?,?,?)", (tienda, url, "catalogo", ahora))
    con.execute("UPDATE rutas SET estado='retirada', motivo=?, retirada=? WHERE tienda=? AND url=?", (motivo, ahora, tienda, url))


def retirada_reciente(con, tienda, url, dias=DIAS_SIN_RESEMBRAR):
    """¿Se retiró hace poco? Entonces `sembrar_rutas_campana` no la vuelve a meter."""
    crear_tabla(con)
    fila = con.execute("SELECT retirada FROM rutas WHERE tienda=? AND url=? AND estado='retirada'", (tienda, url)).fetchone()
    return bool(fila and fila[0] and fila[0] > (datetime.now() - timedelta(days=dias)).isoformat(timespec="minutes"))


def factor_rutas(con, tienda, urls):
    """{url: factor entre 0,5 y 3} para el orden del catálogo: una ruta nueva (menos de 3 lecturas) vale 1; con 3 o más,
    según lo que da (promedio de candidatas que pasan los filtros de tienda por lectura): 0 -> 0,5; 1 -> 1; 3 o más -> 2,5."""
    crear_tabla(con)
    filas = {u: (l, f) for u, l, f in con.execute(
        "SELECT url, lecturas, filtros_tot FROM rutas WHERE tienda=? AND estado='activa'", (tienda,))}
    salida = {}
    for u in urls:
        lecturas, filtros = filas.get(u, (0, 0))
        salida[u] = 1.0 if lecturas < 3 else max(0.5, min(3.0, 0.5 + filtros / lecturas * 0.67))
    return salida


# ------------------------------------------------------------------ descubrimiento desde los hubs

def enlaces_candidatos(v, html, base, tienda):
    """Enlaces del hub que parecen campañas u ofertas de la misma tienda (sin fichas de producto ni parámetros de seguimiento)."""
    parser = v.Enlaces()
    try:
        parser.feed(html)
    except Exception:
        pass
    dominio = ".".join(urlparse(base).netloc.lower().split(".")[-2:])
    vistos, salida = set(), []
    for href, _ in parser.enlaces:
        if not href or href.startswith(("#", "javascript", "mailto")):
            continue
        u = urlparse(urljoin(base, href.replace("&amp;", "&")))
        if u.scheme not in ("http", "https") or ".".join(u.netloc.lower().split(".")[-2:]) != dominio:
            continue
        if RE_PRODUCTO.search(u.path) or not RE_CAMPANA.search(u.path + "?" + u.query):
            continue
        limpia = urlunsplit((u.scheme, u.netloc, u.path, u.query, ""))
        if limpia not in vistos and limpia.rstrip("/") != base.rstrip("/"):
            vistos.add(limpia)
            salida.append(limpia)
    return salida


RE_BLOQUEO = re.compile(r"403|429|antibot|captcha|reto", re.I)


def modo_lectura(v, cfg, tienda):
    """Cómo lee la ronda esta tienda: 'descarga', 'normal' (Chrome normal: Miravia, AliExpress) o 'perfil' (Chrome del
    perfil del vigilante). El hub y sus candidatas se leen igual que la ronda (20/11/2026: MediaMarkt con Chrome daba 1-5
    productos y por descarga da 12 por página; Carrefour con el Chrome normal daba 403)."""
    if tienda in v.tiendas_navegador_normal(cfg):
        return "normal"
    if any(tienda in c.get("navegador", []) for c in cfg.get("categorias", {}).values()):
        return "perfil"
    return "descarga"


class _Descarga:
    """Lector con la misma forma que el navegador (`leer` -> html, url final, datos) para las tiendas de descarga."""

    def __init__(self, v):
        self.v = v

    def leer(self, url, completo=False):
        html, final = self.v.descargar(url)
        return html, final, []


def descubrir_hub(con, v, tienda, hub, nav, ahora=None):
    """Lee el hub de una tienda, comprueba sus enlaces candidatos y añade al catálogo los que traen productos nuevos.
    `nav` es el lector de la tienda (navegador o `_Descarga`). Un 403, antibot o reto para la tienda en el acto: no se
    insiste. -> [(url, productos, nuevos)] de las rutas añadidas."""
    crear_tabla(con)
    ahora = ahora or datetime.now().isoformat(timespec="minutes")
    html, final, _ = nav.leer(hub)
    cat = v.urls_catalogo()
    conocidas = set(cat.get(tienda, []))
    candidatas = [u for u in enlaces_candidatos(v, html, final, tienda)
                  if u not in conocidas and not retirada_reciente(con, tienda, u)]
    anadidas = []
    for u in candidatas[:MAX_CANDIDATAS_POR_HUB]:
        time.sleep(3)  # ritmo de lectura humana: nunca ráfagas
        _, _, productos, error = v.leer_tienda(tienda, {}, None, None if isinstance(nav, _Descarga) else nav, 1, u)
        con.execute("INSERT OR IGNORE INTO rutas (tienda, url, origen, desde, estado) VALUES (?,?,?,?,?)",
                    (tienda, u, f"hub:{hub}", ahora, "candidata"))
        if error or len(productos) < MIN_PRODUCTOS_RUTA_NUEVA:
            con.execute("UPDATE rutas SET motivo=? WHERE tienda=? AND url=?",
                        (error or f"solo {len(productos)} productos", tienda, u))
            if error and RE_BLOQUEO.search(error):
                break  # bloqueo: no se insiste con más candidatas de esta tienda
            continue
        nuevos = 0
        for p in productos:
            if not con.execute("SELECT 1 FROM productos WHERE clave=?", (v.clave_vista(tienda, p),)).fetchone():
                nuevos += 1
        if nuevos / len(productos) >= FRACCION_NUEVA:
            cat.setdefault(tienda, []).append(u)
            con.execute("UPDATE rutas SET estado='activa', motivo=? WHERE tienda=? AND url=?",
                        (f"nueva desde {hub}: {len(productos)} productos, {nuevos} sin ver", tienda, u))
            anadidas.append((u, len(productos), nuevos))
        else:
            con.execute("UPDATE rutas SET motivo=? WHERE tienda=? AND url=?",
                        (f"no aporta: solo {nuevos} de {len(productos)} productos son nuevos", tienda, u))
    if anadidas:
        v.guardar_catalogo(cat)
    con.commit()
    return anadidas


def descubrir_hubs(con, v, cfg, archivo=None, max_tiendas=4):
    """Una vez al día por tienda (HUB_CADA_HORAS), con el Chrome normal. -> {tienda: [rutas añadidas]}."""
    datos = json.loads((archivo or v.RUTAS_CAMPANA).read_text(encoding="utf-8"))
    hubs = datos.get("hubs", {})
    ahora = datetime.now()
    toca = []
    for tienda in hubs:
        fila = con.execute("SELECT v FROM meta WHERE k=?", (f"hub_ultimo:{tienda}",)).fetchone()
        if not (fila and datetime.fromisoformat(fila[0]) > ahora - timedelta(hours=HUB_CADA_HORAS)):
            toca.append(tienda)
    toca = toca[:max_tiendas]
    if not toca:
        return {}
    resultado = {}
    for modo in ("descarga", "normal", "perfil"):  # cada tienda con su forma de leer, un navegador por tipo como mucho
        suyas = [t for t in toca if modo_lectura(v, cfg, t) == modo]
        if not suyas:
            continue
        lector = (_Descarga(v) if modo == "descarga" else v.NavegadorNormal(cfg) if modo == "normal" else v.Navegador(cfg))
        try:
            nav = lector if modo == "descarga" else lector.__enter__()
            for tienda in suyas:
                nuevas = []
                for hub in hubs[tienda]:
                    try:
                        nuevas += descubrir_hub(con, v, tienda, hub, nav)
                    except Exception as e:
                        v.log(f"hub {tienda} {hub[:70]}: {str(e)[:100]}")
                con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"hub_ultimo:{tienda}", ahora.isoformat(timespec="minutes")))
                con.commit()
                if nuevas:
                    resultado[tienda] = nuevas
                    v.log(f"hub {tienda}: rutas nuevas {[(u[-60:], n) for u, n, _ in nuevas]}")
        finally:
            if modo != "descarga":
                try:
                    lector.__exit__(None, None, None)
                except Exception:
                    pass
    return resultado
