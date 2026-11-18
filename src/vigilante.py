"""Vigilante de chollos: capa SIN IA para Hermes (0 tokens).

Por cada categoría: lee la página de ofertas de cada tienda, se queda solo con lo
nuevo o lo que baja de su mínimo visto, filtra por marca, precio y descuento,
quita duplicados entre tiendas, comprueba Idealo y Chollometro, y solo entonces
despierta al agente con una lista compacta. Si no queda nada: {"wakeAgent": false}.

Uso (desde la raíz del proyecto: python src/vigilante.py ...):
  python vigilante.py descubrir <categoria>   busca la página de ofertas de cada tienda
  python vigilante.py prueba <categoria>      diagnóstico por tienda (no guarda nada)
  python vigilante.py volcar <tienda> [...]   por qué una tienda da 0 productos
  python vigilante.py dossier <n> [<n>...]      datos de la ficha para "verifica N" (lo usa Hermes)
  python vigilante.py login <tienda>             abre la tienda para que inicies sesión (se guarda)
  python vigilante.py informe [días]             qué aporta cada tienda (para quitar las que no)
  python vigilante.py historial <texto|EAN|url>  cambios de precio de un producto y su mínimo con fecha
  python vigilante.py entregar <n>                manda la oferta verificada en 6 mensajes (formato móvil)
  python vigilante.py recordar <n> <buena|dudosa|caducada> <motivo>   guarda el veredicto del modelo
                                                (caducada: la oferta se acabó; avisa si vuelve a ese precio)
  python vigilante.py wrappers                crea vig_<categoria>.py para cada categoría
  python vigilante.py <categoria>             ronda normal (la lanza el cron)

Solo usa la librería estándar de Python: no hay que instalar nada.
"""
import functools
import gzip
import json
import os
import random
import re
import sqlite3
import sys
import time
import unicodedata
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, parse_qsl, quote, quote_plus, unquote, urlencode, urljoin, urlparse, urlsplit, urlunsplit
from http.cookiejar import CookieJar
import threading
import ssl
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, HTTPSHandler, Request, build_opener

# Estructura del proyecto: src/ (este archivo), config/ (tiendas.json, marcas.txt), data/ (base y cachés).
# VIGILANTE_HOME cambia la raíz y VIGILANTE_DATOS solo la carpeta de datos (p. ej. la de Hermes).
BASE = Path(os.environ.get("VIGILANTE_HOME") or Path(__file__).resolve().parent.parent)
DATOS = Path(os.environ.get("VIGILANTE_DATOS") or BASE / "data")
CONFIG = BASE / "config" / "tiendas.json"
MARCAS = BASE / "config" / "marcas.txt"
HERMES = Path(os.environ.get("HERMES_HOME") or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "hermes")
URLS = DATOS / "urls_ofertas.json"
DB = DATOS / "vistos.db"
LOG = DATOS / "log.txt"
SILENCIO = '{"wakeAgent": false}'
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
CACHE_IDEALO_H = 6          # horas que se reutiliza una consulta a Idealo
OLVIDAR_DIAS = 120          # productos no vistos en este tiempo se borran (largo: es el histórico propio)

# ------------------------------------------------------------------ utilidades

def log(msg):
    DATOS.mkdir(exist_ok=True)
    # Siempre en la carpeta de datos en uso: las pruebas que cambian DATOS a una temporal y no LOG escribían en el
    # data/log.txt de verdad (18/11/2026: "429 en subiendo", "camelcamelcamel: reto"... que nunca pasaron).
    ruta = LOG if LOG.parent == DATOS else DATOS / "log.txt"
    if ruta.exists() and ruta.stat().st_size > 1_000_000:
        ruta.replace(ruta.with_suffix(".old.txt"))
    with ruta.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def recorrido(msg):
    """Diario del recorrido de tiendas (data/recorrido.txt): qué se lee en cada ronda y qué sale de ahí.
    Aparte de log.txt, que queda para errores y decisiones. Se activa con filtros_codigo.log_recorrido."""
    DATOS.mkdir(exist_ok=True)
    archivo = DATOS / "recorrido.txt"
    if archivo.exists() and archivo.stat().st_size > 2_000_000:
        archivo.replace(archivo.with_suffix(".old.txt"))
    with archivo.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


_COOKIES = threading.local()


def _abridor():
    """Un abridor con cookies por hilo: muchas tiendas redirigen en bucle (307) sin cookies."""
    if not hasattr(_COOKIES, "op"):
        _COOKIES.op = build_opener(HTTPSHandler(context=_contexto_ssl()), HTTPCookieProcessor(CookieJar()))
    return _COOKIES.op


def _contexto_ssl():
    """Certificados verificados con el almacén de Windows (truststore), como hace Chrome: completa
    las cadenas a las que les falta el certificado intermedio (Huawei, Logitech, Rebuy daban
    CERTIFICATE_VERIFY_FAILED). Sin truststore, el contexto normal de Python."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return ssl.create_default_context()


RE_META_CHARSET = re.compile(rb"<meta[^>]+charset=[\"']?([\w-]+)", re.I)


# ------------------------------------------------------------ robots.txt
# Regla de Zeta: no se salta ni antibots ni robots.txt. Se comprueba por código en TODA descarga y en
# cada página que abre el navegador (antes se miraba a mano y se coló /search de Chollometro).

class BloqueadoPorRobots(Exception):
    """La web no permite a programas leer esa ruta (robots.txt). No es OSError: no se reintenta."""


ROBOTS_TTL_H = 24
_ROBOTS_LOCK = threading.Lock()
_ROBOTS_MEM = {}


def reglas_robots(texto, agente="*"):
    """Reglas (permite, patrón) del grupo `agente` de un robots.txt."""
    grupos, actuales, en_reglas = {}, [], False
    for linea in texto.splitlines():
        linea = linea.split("#", 1)[0].strip()
        if ":" not in linea:
            continue
        k, val = (x.strip() for x in linea.split(":", 1))
        k = k.lower()
        if k == "user-agent":
            if en_reglas:
                actuales, en_reglas = [], False
            actuales.append(val.lower())
            grupos.setdefault(val.lower(), [])
        elif k in ("allow", "disallow") and actuales:
            en_reglas = True
            if val:  # "Disallow:" vacío = todo permitido
                for a in actuales:
                    grupos[a].append((k == "allow", val))
    return grupos.get(agente, [])


def robots_permite(reglas, ruta):
    """Como Google: gana la regla más larga que encaja; si empatan, Allow. Admite * y $."""
    mejor = None
    for permite, patron in reglas:
        rx = re.escape(patron).replace(r"\*", ".*")
        if rx.endswith(r"\$"):
            rx = rx[:-2] + "$"
        if re.match(rx, ruta) and (mejor is None or len(patron) > len(mejor[1])
                                   or (len(patron) == len(mejor[1]) and permite)):
            mejor = (permite, patron)
    return True if mejor is None else mejor[0]


def _reglas_de_host(base):
    """Reglas del robots.txt de `base` (con caché de 24 h en disco), [] si no tiene, None si no se
    puede saber (error de red: esta vez no se lee) o si lo prohíbe todo (401/403 en robots.txt)."""
    ahora = datetime.now()
    with _ROBOTS_LOCK:
        if base in _ROBOTS_MEM:
            return _ROBOTS_MEM[base]
        archivo = DATOS / "robots.json"
        try:
            cache = json.loads(archivo.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
        e = cache.get(base)
        if e and datetime.fromisoformat(e["t"]) > ahora - timedelta(hours=ROBOTS_TTL_H):
            _ROBOTS_MEM[base] = e["r"]
            return e["r"]
    try:
        texto, _ = _descargar(base + "/robots.txt", 15)
        reglas = reglas_de_respuesta(200, texto)
    except HTTPError as err:
        if err.code in (401, 403):
            reglas = None  # ni el robots.txt deja leer: se trata como "todo prohibido"
        elif err.code < 500 and err.code != 429:
            reglas = []    # sin robots.txt: todo permitido
        else:
            return None    # pasajero: esta vez no, sin guardar
    except Exception:
        return None
    guardar_robots(base, reglas)
    return reglas


def guardar_robots(base, reglas):
    """Guarda en caché las reglas de una web (también las que lee el navegador)."""
    ahora = datetime.now()
    with _ROBOTS_LOCK:
        _ROBOTS_MEM[base] = reglas
        try:
            cache = json.loads((DATOS / "robots.json").read_text(encoding="utf-8"))
        except Exception:
            cache = {}
        cache[base] = {"t": ahora.isoformat(timespec="minutes"), "r": reglas}
        try:
            DATOS.mkdir(parents=True, exist_ok=True)
            tmp = DATOS / f"robots.{os.getpid()}.tmp"
            tmp.write_text(json.dumps(cache), encoding="utf-8")
            tmp.replace(DATOS / "robots.json")
        except OSError:
            pass
    return reglas


def reglas_de_respuesta(estado, texto):
    """Respuesta a /robots.txt -> reglas; None si no se puede saber (página de bloqueo, 403...)."""
    if estado == 200:
        return None if "<html" in (texto or "")[:500].lower() else reglas_robots(texto or "")
    if estado in (404, 410):
        return []
    return None


def robots_pendiente_de_navegador(url):
    """El robots.txt dio 403 al programa (antibot) y la tienda se lee con navegador: que lo lea él."""
    u = urlparse(url)
    base = f"{u.scheme}://{u.netloc}"
    return base, (base in _ROBOTS_MEM and _ROBOTS_MEM[base] is None)


_ROBOTS_CFG = [None, None]  # (mtime de tiendas.json, (respetar, hosts que se respetan siempre))


def robots_activo(host):
    """¿Se mira el robots.txt de este host? Decisión de Zeta (16/11/2026): no se mira
    (`filtros_codigo.respetar_robots` false), salvo en los hosts de `robots_siempre`. Sin configuración
    legible se respeta, que es lo prudente. Esto NO toca antibot ni captcha: eso sigue sin saltarse."""
    try:
        marca = CONFIG.stat().st_mtime
        if _ROBOTS_CFG[0] != (str(CONFIG), marca):
            f = cargar_config().get("filtros_codigo", {})
            _ROBOTS_CFG[:] = [(str(CONFIG), marca), (f.get("respetar_robots", True),
                                                    [h.lower() for h in f.get("robots_siempre", [])])]
        respetar, siempre = _ROBOTS_CFG[1]
    except (OSError, ValueError):
        return True
    if respetar:
        return True
    host = (host or "").lower().removeprefix("www.")
    return any(host == h or host.endswith("." + h) for h in siempre)


def permitido(url):
    """¿Deja el robots.txt de la web leer esta URL?"""
    u = urlparse(url)
    if u.hostname in ("localhost", "127.0.0.1") or u.path.endswith("/robots.txt"):
        return True
    if not robots_activo(u.hostname):
        return True
    reglas = _reglas_de_host(f"{u.scheme}://{u.netloc}")
    if reglas is None:
        return False
    return robots_permite([tuple(r) for r in reglas], (u.path or "/") + (f"?{u.query}" if u.query else ""))


def descargar(url, timeout=20, reintentos=1):
    """Descarga una página. Un reintento si el fallo es pasajero (tiempo agotado, 5xx, conexión
    cortada); los 403/404 no se reintentan porque no van a cambiar."""
    if not permitido(url):
        raise BloqueadoPorRobots(f"robots.txt no permite leer {url}")
    for intento in range(reintentos + 1):
        try:
            return _descargar(url, timeout)
        except HTTPError as e:
            if e.code < 500 or intento == reintentos:
                raise
        except (TimeoutError, ConnectionError, OSError) as e:
            if intento == reintentos or isinstance(e, HTTPError):
                raise
        time.sleep(2)


def url_ascii(url):
    """La misma dirección con tildes, eñes y espacios codificados. urllib no admite caracteres que no
    sean ASCII: "https://www.mediamarkt.es/es/category/televisión-398.html" fallaba con
    "'ascii' codec can't encode character" (17/11/2026). Lo que ya viene codificado no se toca."""
    try:
        url.encode("ascii")
        return url
    except UnicodeEncodeError:
        pass
    p = urlsplit(url)
    try:
        host = p.netloc.encode("idna").decode("ascii")
    except UnicodeError:
        host = p.netloc
    seguro = "/%:@!$&'()*+,;=~-._"
    return urlunsplit((p.scheme, host, quote(p.path, safe=seguro), quote(p.query, safe=seguro + "?"),
                       quote(p.fragment, safe=seguro)))


SESION_CM = "cm_sesion.json"  # cookies de la cuenta de Chollometro para la precarga (comando `sesion_cm`)
RE_COOKIE_SESION_CM = re.compile(r"^(pepper_session|remember_\w+|xsrf_t|u_l)$")
_cookie_cm = {"activa": False, "cabecera": None}


def cookie_cm():
    """Cabecera Cookie de la sesión de Chollometro, solo mientras corre la precarga (_cookie_cm["activa"]).
    18/11/2026: sin sesión, todos los listados acaban en la página 334 (la 335 da 410); con sesión Destacados
    llega a 2024 y Subiendo a 2025. Es otra cuenta, no PorLa_Jeta. Solo las cookies de sesión, no la de
    Cloudflare."""
    if not _cookie_cm["activa"]:
        return None
    if _cookie_cm["cabecera"] is None:
        try:
            cks = json.loads((DATOS / SESION_CM).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cks = []
        _cookie_cm["cabecera"] = "; ".join(f"{c['name']}={c['value']}" for c in cks
                                           if RE_COOKIE_SESION_CM.match(c.get("name", "")))
    return _cookie_cm["cabecera"] or None


def _descargar(url, timeout):
    cab = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    }
    if urlparse(url).netloc.endswith("chollometro.com") and cookie_cm():
        cab["Cookie"] = cookie_cm()
    req = Request(url_ascii(url), headers=cab)
    with _abridor().open(req, timeout=timeout) as r:
        data = r.read()
        enc = r.headers.get("Content-Encoding", "")
        if enc == "gzip":
            data = gzip.decompress(data)
        elif enc == "deflate":
            data = zlib.decompress(data)
        charset = r.headers.get_content_charset()
        if not charset:  # webs antiguas en ISO-8859-1 que solo lo dicen en el <meta>
            m = RE_META_CHARSET.search(data[:4096])
            charset = m.group(1).decode() if m else "utf-8"
        try:
            return data.decode(charset, errors="replace"), r.geturl()
        except LookupError:
            return data.decode("utf-8", errors="replace"), r.geturl()


def en_seccion(tienda, url):
    """Si la tienda va con ruta (samsung.com/es), el enlace debe quedarse dentro: la portada de Samsung enlaza las de
    otros países (/uk/, /de/...) y el mapa acababa leyendo páginas en otro idioma (Zeta, 18/11/2026)."""
    seccion = urlparse(url_tienda(tienda)).path.rstrip("/").lower()
    ruta = unquote(urlparse(url).path).lower()
    return not seccion or ruta == seccion or ruta.startswith(seccion + "/")


def url_tienda(dominio):
    if dominio.startswith("http"):
        return dominio
    host = dominio.split("/")[0]
    prefijo = "https://" if host.count(".") >= 2 else "https://www."
    return prefijo + dominio.rstrip("/") + "/"


def raiz(dominio):
    """'mediamarkt.es' -> 'mediamarkt' ; 'store.steampowered.com' -> 'steampowered'"""
    partes = dominio.split("/")[0].split(".")
    return partes[-2] if len(partes) >= 2 else partes[0]


def a_numero(v):
    """'1.299,00 €' -> 1299.0 ; '19.99' -> 19.99 ; 1999 -> 1999.0"""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = re.sub(r"[^\d,.]", "", v)
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):  # "1.299" en España son mil doscientos
        s = s.replace(".", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def ahora_madrid():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Madrid"))
    except Exception:  # Windows sin tzdata: el portátil ya está en hora de Madrid
        return datetime.now()


def en_horario(cfg):
    h = cfg.get("horario", {})
    ini, fin = h.get("inicio", "08:00"), h.get("fin", "00:45")
    t = ahora_madrid().strftime("%H:%M")
    return (ini <= t <= fin) if ini <= fin else (t >= ini or t <= fin)


STOP = {"de", "con", "para", "y", "el", "la", "los", "las", "en", "del", "al", "pack", "nuevo"}


def tokens(s):
    # Los números sueltos cuentan: "Galaxy A5" y "Galaxy A6" o "Pro 2" y "Pro 3" son distintos.
    # Sin tildes (17/11/2026): "Bebé" se partía en "beb" y "Niños" en "ni" + "os".
    s = unicodedata.normalize("NFKD", s.lower()).encode("ascii", "ignore").decode()
    return {t for t in re.findall(r"[a-z0-9]+", s) if t not in STOP and (len(t) > 1 or t.isdigit())}


def parecido(a, b):
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / len(ta) if ta else 0.0

# --------------------------------------------------- extracción de productos

K_NOMBRE = ("name", "title", "productName", "nombre", "titulo", "displayName", "product_name")
# Sin "value"/"amount" arriba: solo se aceptan dentro de un objeto de precio.
# Se incluyen variantes camelCase y snake_case (Magento, Shopify, PrestaShop...).
K_PRECIO = ("price", "salePrice", "finalPrice", "currentPrice", "precio", "lowPrice",
            "priceAmount", "sellingPrice", "offerPrice", "promotionalPrice", "promoPrice",
            "discountedPrice", "specialPrice", "priceWithTax", "sale_price", "final_price",
            "special_price", "current_price", "price_incl_tax", "current", "final", "sale", "dellPrice",
            "priceToPay")
# Precios de oferta explícitos: mandan sobre el "price" genérico, que en muchas tiendas
# (Samsung: price=1549, promotionPrice=1249) es el precio normal.
K_OFERTA = ("promotionPrice", "promotionalPrice", "promoPrice", "salePrice", "sale_price", "finalPrice",
            "final_price", "specialPrice", "special_price", "offerPrice", "discountedPrice",
            "currentPrice", "current_price", "dellPrice", "priceToPay")  # priceToPay: Amazon
# Sin "highPrice": en un AggregateOffer es el producto más caro de una lista, no el precio tachado.
K_ANTES = ("listPrice", "originalPrice", "oldPrice", "strikePrice", "strikeThroughPrice",
           "strikethroughPrice", "crossedPrice", "crossPrice", "previousPrice", "regularPrice",
           "pvp", "pvpr", "pvpPrice", "basePrice", "priceBefore", "wasPrice", "referencePrice",
           "marketPrice", "market_price", "originPrice", "origin_price", "compareAtPrice", "compare_at_price", "msrp", "rrp",
           "old_price", "original_price", "list_price", "regular_price", "price_before",
           "formerPrice", "former_price", "crossedOutPrice", "strikeoutPrice", "priceWas",
           "original", "old", "before", "regular", "was", "comparePrice", "basisPrice",  # basisPrice: Amazon
           "full_price", "fullPrice", "baseAmount", "base_price")
# Porcentaje de descuento: si no hay precio anterior, se calcula (GOG: 4,99 € con "-77%" -> 21,70 €).
K_DESCUENTO = ("discount", "discountPercentage", "discountPercent", "discount_percent", "percentOff",
               "percent_off", "savingsPercent", "discountRate")
K_URL = ("url", "link", "href", "productUrl", "canonicalUrl", "pdpUrl", "pdUrl")
K_MARCA = ("brand", "brandName", "marca", "manufacturer")
K_EAN = ("gtin13", "gtin", "ean", "gtin8", "gtin14", "eanCode", "ean13", "gbomCode")  # gbomCode: Huawei
K_IMG = ("image", "imageUrl", "img", "thumbnail", "picture", "mainImage", "imageURL")
K_VALORACION = ("ratingValue", "averageRating", "rating", "stars", "starRating", "score", "evaluation")
K_RESENAS = ("reviewCount", "ratingCount", "numReviews", "reviewsCount", "totalReviews", "review_count",
             "reviews", "totalRatings", "tradeCount", "sold", "orders")
SUB = ("offers", "offer", "prices", "price", "pricing", "priceInfo", "brand", "media", "images",
       "aggregateRating", "rating", "reviews", "trade")


def buscar(obj, claves, prof=2):
    """Primer valor escalar de alguna clave, mirando también en subobjetos."""
    if not isinstance(obj, dict):
        return None
    for k in claves:
        if k in obj:
            v = obj[k]
            if isinstance(v, list) and v:
                v = v[0]
            if isinstance(v, dict):
                v = buscar(v, ("name", "url", "value", "amount", "price", "src") + claves, 0) if prof else None
            if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                return v
    if prof:
        for k in SUB:
            sub = obj.get(k)
            if isinstance(sub, list) and sub:
                sub = sub[0]
            if isinstance(sub, dict):
                v = buscar(sub, claves, prof - 1)
                if v is not None:
                    return v
    return None


def nombre_de(obj):
    for k in K_NOMBRE:
        v = obj.get(k)
        if isinstance(v, str) and 3 <= len(v.strip()) <= 250:
            return " ".join(unescape(v).split())  # 'TV 65&quot;' -> 'TV 65"'
    return None


def normalizar_importes(obj, prof=0):
    """{"amount": 1499, "currencyExponent": 2} -> {"amount": 14.99, ...} (Groupon y otras APIs dan céntimos)."""
    if prof > 40:
        return
    if isinstance(obj, dict):
        exp = obj.get("currencyExponent", obj.get("decimalPlaces"))
        if isinstance(obj.get("amount"), int) and isinstance(exp, int) and 0 < exp <= 4:
            obj["amount"] = obj["amount"] / 10 ** exp
            obj.pop("currencyExponent", None), obj.pop("decimalPlaces", None)
        for val in obj.values():
            if isinstance(val, (dict, list)):
                normalizar_importes(val, prof + 1)
    elif isinstance(obj, list):
        for val in obj:
            if isinstance(val, (dict, list)):
                normalizar_importes(val, prof + 1)


def recorrer(obj, salida, cont):
    cont[0] += 1
    if cont[0] > 300_000:
        return
    if isinstance(obj, dict):
        # schema.org ItemList: la URL va en el ListItem y no dentro del producto (outlet de LG).
        if isinstance(obj.get("item"), dict) and isinstance(obj.get("url"), str):
            obj["item"].setdefault("url", obj["url"])
        precio = a_numero(buscar(obj, K_PRECIO))
        if precio and 0.5 <= precio <= 20000 and nombre_de(obj):
            salida.append(obj)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                recorrer(v, salida, cont)
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, (dict, list)):
                recorrer(v, salida, cont)


RE_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)
DEC = json.JSONDecoder()


def bloques_json(html):
    for attrs, cuerpo in RE_SCRIPT.findall(html):
        cuerpo = cuerpo.strip()
        if len(cuerpo) < 30:
            continue
        if "json" in attrs.lower():
            try:
                yield json.loads(cuerpo)
                continue
            except ValueError:
                pass
        # window.__STATE__ = {...}. Un script puede llevar varios objetos (Amazon: los productos de
        # /deals van en el tercero o cuarto), así que se siguen leyendo hasta el final (con tope).
        hasta, leidos = 0, 0
        for m in re.finditer(r"[=(:]\s*([\[{])", cuerpo):
            if m.start(1) < hasta:
                continue
            try:
                obj, fin = DEC.raw_decode(cuerpo, m.start(1))
            except ValueError:
                continue
            hasta = fin
            if isinstance(obj, (dict, list)) and fin - m.start(1) > 40:
                yield obj
                leidos += 1
                if leidos >= 30:
                    break


VACIAS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
RE_DATA_PREFIJO = re.compile(r"^data-(?:gtm-|ga-|analytics-|track-|ecommerce-)?(?:item|product|prod)?[-_]?", re.I)
DATA_CAMPOS = {"name": "name", "title": "name", "nombre": "name",
               "price": "price", "precio": "price", "saleprice": "salePrice", "finalprice": "salePrice",
               "oldprice": "oldPrice", "regularprice": "oldPrice", "listprice": "oldPrice",
               "originalprice": "oldPrice", "pvp": "oldPrice",
               "brand": "brand", "marca": "brand", "ean": "gtin13", "gtin": "gtin13", "gtin13": "gtin13"}


class Microdatos(HTMLParser):
    """Lee productos marcados en el HTML con itemprop (schema.org) o en atributos data-*, sin JSON."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.raices, self._pila, self._texto, self._prof = [], [], [], 0

    def _atributos_data(self, a):
        """Producto en atributos data-* del enlace o la tarjeta (Coolmod, PrestaShop, GA4...):
        data-itemname="..." data-itemprice="1.95" data-itembrand="..."; o data-product='{json}'."""
        obj = {}
        for k, val in a.items():
            if not val:
                continue
            # JSON con productos en cualquier atributo (Dell lo mete en un atributo del componente).
            if val[0] in "{[" and len(val) > 20 and re.search(r"pric|preci", val, re.I):
                try:
                    self.raices.append(json.loads(val))
                except ValueError:
                    pass
                continue
            if not k.startswith("data-"):
                continue
            campo = DATA_CAMPOS.get(RE_DATA_PREFIJO.sub("", k).lower())
            if campo:
                obj.setdefault(campo, val.strip())
        if "name" in obj and "price" in obj:
            if a.get("href"):
                obj.setdefault("url", a["href"])
            self.raices.append(obj)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self._atributos_data(a)
        if tag not in VACIAS:
            self._prof += 1
        prop = a.get("itemprop")
        padre = self._pila[-1][1] if self._pila else None
        if "itemscope" in a:
            nuevo = {"@type": a.get("itemtype", "")}
            if prop and padre is not None:
                padre.setdefault(prop, nuevo)
            elif padre is None:
                self.raices.append(nuevo)
            if tag not in VACIAS:
                self._pila.append((self._prof, nuevo))
            return
        if prop and padre is not None:
            valor = a.get("content") or a.get("href") or a.get("src") or a.get("datetime")
            if valor is not None:
                padre.setdefault(prop, valor.strip())
            elif tag not in VACIAS:
                self._texto.append([self._prof, prop, padre, []])

    def handle_data(self, data):
        for t in self._texto:
            t[3].append(data)

    def handle_endtag(self, tag):
        if tag in VACIAS:
            return
        while self._texto and self._texto[-1][0] >= self._prof:
            _, prop, padre, partes = self._texto.pop()
            padre.setdefault(prop, " ".join("".join(partes).split()))
        if self._pila and self._pila[-1][0] >= self._prof:
            self._pila.pop()
        self._prof = max(0, self._prof - 1)


def microdatos(html):
    if "itemprop" not in html and "data-" not in html:
        return []
    p = Microdatos()
    try:
        p.feed(html)
    except Exception:  # HTML muy roto: nos quedamos con lo leído
        pass
    return p.raices


def objetos_producto(html):
    crudos, cont = [], [0]
    for bloque in bloques_json(html):
        normalizar_importes(bloque)
        recorrer(bloque, crudos, cont)
    for raiz_md in microdatos(html):
        recorrer(raiz_md, crudos, cont)
    return crudos


def precio_tachado_jsonld(o):
    """schema.org: offers.priceSpecification con priceType StrikethroughPrice / ListPrice."""
    ofertas = o.get("offers")
    for of in ofertas if isinstance(ofertas, list) else [ofertas]:
        if not isinstance(of, dict):
            continue
        specs = of.get("priceSpecification")
        specs = specs if isinstance(specs, list) else [specs]
        for s in specs:
            if isinstance(s, dict):
                tipo = str(s.get("priceType", ""))
                if "Strikethrough" in tipo or "ListPrice" in tipo or (not tipo and len(specs) == 1):
                    v = a_numero(s.get("price"))
                    if v:
                        return v
    return None


RE_MIRAVIA_TARJETA = re.compile(
    r'<a\b[^>]*\bhref="(?:https?:)?(?://www\.miravia\.es)?/p/i(\d+)-s(\d+)\.html([^"]*)"[^>]*>(.*?)</a>', re.S | re.I)
RE_ALT = re.compile(r'\balt="([^"]+)"', re.I)


def tarjetas_miravia(html):
    """Ofertas de Miravia (17/11/2026). El lector general sacaba 11 de 87 y con nombres en inglés. En cada
    tarjeta el nombre va en el alt de la foto, el precio a la vista y el original en el propio enlace
    (item_price, en céntimos; item_discount_price es el de oferta)."""
    salida, vistos = [], set()
    for item, sku, resto, cuerpo in RE_MIRAVIA_TARJETA.findall(html):
        alt = RE_ALT.search(cuerpo)
        if item in vistos or not alt:
            continue
        q = unquote(unescape(resto))
        oferta, original = re.search(r"item_discount_price:(\d+)", q), re.search(r"item_price:(\d+)", q)
        precio = int(oferta.group(1)) / 100 if oferta else a_numero(re.sub(r"\s+", "", _texto(cuerpo)))
        if not precio:
            continue
        antes = int(original.group(1)) / 100 if original else None
        vistos.add(item)
        salida.append({"nombre": " ".join(unescape(alt.group(1)).split()), "precio": round(precio, 2),
                       "precio_antes": antes if antes and antes > precio else None, "marca": None, "ean": None,
                       "url": f"https://www.miravia.es/p/i{item}-s{sku}.html", "imagen_url": None,
                       "valoracion": None, "resenas": None})
    return salida


def productos_pss(extra_json):
    """Private Sport Shop (18/11/2026, Chrome normal autorizado por Zeta): las respuestas de su web traen por un lado
    los productos (productId, productName, brandName) y por otro los precios ({id, specialPrice, retailPrice}).
    Se juntan por id. Enlace: /catalog/product/view/id/<id>/."""
    nombres, precios = {}, {}

    def mirar(x):
        if isinstance(x, dict):
            if x.get("productId") and x.get("productName"):
                nombres[x["productId"]] = (x["productName"], x.get("brandName"))
            elif x.get("id") and isinstance(x.get("specialPrice"), (int, float)):
                precios[x["id"]] = (x["specialPrice"], x.get("retailPrice"))
            for y in x.values():
                mirar(y)
        elif isinstance(x, list):
            for y in x:
                mirar(y)
    for bloque in extra_json:
        mirar(bloque)
    salida = []
    for pid, (nombre, marca) in nombres.items():
        if pid not in precios or not precios[pid][0]:
            continue
        precio, antes = precios[pid]
        if marca and not nombre.lower().startswith(marca.lower()):
            nombre = f"{marca} {nombre}"
        salida.append({"nombre": " ".join(nombre.split()), "precio": round(float(precio), 2),
                       "precio_antes": float(antes) if antes and float(antes) > precio else None, "marca": marca,
                       "ean": None, "url": f"https://www.privatesportshop.es/catalog/product/view/id/{pid}/",
                       "imagen_url": None, "valoracion": None, "resenas": None})
    return salida


def productos_game(html):
    """GAME (18/11/2026): web en Next.js; el precio va antes del nombre en la tarjeta y el lector general lo
    asignaba mal (4 productos de 24). Por tarjeta <article class="ProductListCard...">: nombre en <h3>, precio
    anterior y actual en dos <span>, y "Mín. últimos 30 días" (el mínimo legal de 30 días) en "min30"."""
    salida = []
    for b in re.split(r'<article class="ProductListCard', html)[1:]:
        b = b.split("</article>")[0]
        nombre = re.search(r"<h3[^>]*>(.*?)</h3>", b, re.S)
        url = re.search(r'href="(/[^"]+/\d{4,})"', b)
        precios = [a_numero(x) for x in re.findall(r"<span[^>]*>\s*(\d{1,3}(?:\.\d{3})*,\d{2})\s*€\s*</span>", b)]
        if not (nombre and url and precios):
            continue
        min30 = re.search(r"M[ií]n\. últimos 30 d[ií]as:.*?(\d{1,3}(?:\.\d{3})*,\d{2})\s*€", b, re.S)
        precio = precios[-1]
        antes = precios[0] if len(precios) > 1 and precios[0] > precio else None
        salida.append({"nombre": " ".join(unescape(re.sub(r"<[^>]+>", " ", nombre.group(1))).split()),
                       "precio": precio, "precio_antes": antes, "marca": None, "ean": None,
                       "url": "https://www.game.es" + url.group(1), "imagen_url": None, "valoracion": None,
                       "resenas": None, "min30": a_numero(min30.group(1)) if min30 else None})
    return salida


def productos_eci(html):
    """El Corte Inglés / Hipercor (18/11/2026): los precios no están en las tarjetas (6 importes para 44 productos);
    van en el bloque de datos del listado, "products":[{brand, name, gtin, code_a, price:{f_price, o_price}}]. El
    enlace se busca en la página por el código (A200835701). Con EAN: sirve para el mercado propio."""
    dec = json.JSONDecoder()
    salida, vistos = [], set()
    for m in re.finditer(r'"products"\s*:\s*\[', html):
        try:
            lista, _ = dec.raw_decode(html, m.end() - 1)
        except ValueError:
            continue
        for o in lista if isinstance(lista, list) else []:
            if not isinstance(o, dict):
                continue
            precio = (o.get("price") or {}) if isinstance(o.get("price"), dict) else {}
            final, original, codigo = precio.get("f_price"), precio.get("o_price"), o.get("code_a")
            if not (o.get("name") and isinstance(final, (int, float)) and codigo) or codigo in vistos:
                continue
            vistos.add(codigo)
            enlace = re.search(r'href="((?:https://www\.(?:elcorteingles|hipercor)\.es)?/[^"]*' + re.escape(codigo) +
                               r'[^"]*)"', html)
            url = enlace.group(1) if enlace else None
            if url and url.startswith("/"):
                url = ("https://www.hipercor.es" if "hipercor" in html[:5000] else "https://www.elcorteingles.es") + url
            marca = o.get("brand") if o.get("brand") not in (None, "Multi Brand") else None
            nombre = " ".join(str(o["name"]).split())
            salida.append({"nombre": nombre, "precio": float(final),
                           "precio_antes": float(original) if isinstance(original, (int, float)) and original > final
                           else None, "marca": marca, "ean": o.get("gtin"), "url": url, "imagen_url": None,
                           "valoracion": None, "resenas": None})
    return salida


def productos_jysk(html):
    """JYSK (18/11/2026): el lector general sacaba 1 producto de 69 (HTML escapado dentro de la página). Cada tarjeta
    trae el nombre en aria-label="Ver producto ...", el precio en aria-label="Precio del artículo - 40,00 € /u", el
    normal en "Precio normal: 65,00 €" y el mínimo legal de 30 días en "Precio más bajo últimos 30 días: ..."."""
    num = r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*€"
    salida, vistos = [], set()
    for b in re.split(r'(?=<a[^>]*aria-label="Ver producto )', html)[1:]:
        nombre = re.search(r'aria-label="Ver producto ([^"]+)"', b)
        url = re.search(r'href="(/[^"]+)"', b)
        precio = re.search(r'aria-label="Precio del art[ií]culo - ' + num, b)
        if not (nombre and url and precio) or url.group(1) in vistos:
            continue
        vistos.add(url.group(1))
        normal = re.search(r"Precio normal:\s*" + num, b)
        min30 = re.search(r"Precio m[aá]s bajo [uú]ltimos 30 d[ií]as:\s*" + num, b)
        p, antes = a_numero(precio.group(1)), a_numero(normal.group(1)) if normal else None
        salida.append({"nombre": " ".join(unescape(nombre.group(1)).split()), "precio": p,
                       "precio_antes": antes if antes and antes > p else None, "marca": None, "ean": None,
                       "url": "https://jysk.es" + url.group(1), "imagen_url": None, "valoracion": None, "resenas": None,
                       "min30": a_numero(min30.group(1)) if min30 else None})
    return salida


RE_PRIVALIA_CAMPANA = re.compile(r'(?:https://es\.privalia\.com)?/gr/catalog/(\d+)(?=["/?#])')


def campanas_privalia(html):
    """Campañas de la portada de Privalia (18/11/2026): /gr/catalog/<id>. Los productos están dentro de cada una."""
    ids = list(dict.fromkeys(RE_PRIVALIA_CAMPANA.findall(html)))
    return [f"https://es.privalia.com/gr/catalog/{i}" for i in ids]


def productos_privalia(html):
    """Productos de una campaña de Privalia (18/11/2026, con la sesión de Zeta en el perfil). Vienen en los datos
    de la página: {"pricing": {"price": {"value"}, "retailPrice": {"value"}}, "saleId", "catalogId", "name", "id"}.
    El tachado NO es un precio anterior: su letra pequeña dice que es el recomendado por el fabricante y que "no
    indica una reducción". Por eso va en "pvpr" y no en precio_antes (contraste real: mercado o Chollometro).
    El producto no tiene página propia (se abre encima de la campaña): el enlace es la campaña con ?producto=<id>."""
    dec = json.JSONDecoder()
    m = re.search(r'"brandSlug":"([^"]+)"', html)
    marca = m.group(1).replace("-", " ").title() if m else None
    salida, vistos = [], set()
    for m in re.finditer(r'\{"pricing":\{', html):
        try:
            obj, _ = dec.raw_decode(html, m.start())
        except ValueError:
            continue
        pid, venta, cat, nombre = obj.get("id"), obj.get("saleId"), obj.get("catalogId"), obj.get("name")
        precio = ((obj.get("pricing") or {}).get("price") or {}).get("value")
        pvpr = ((obj.get("pricing") or {}).get("retailPrice") or {}).get("value")
        if not (pid and venta and nombre and isinstance(precio, (int, float))) or obj.get("isLocked") or pid in vistos:
            continue
        vistos.add(pid)
        nombre = " ".join(nombre.split())
        if marca and marca.lower() not in nombre.lower():
            nombre = f"{marca} {nombre}"
        salida.append({"nombre": nombre, "precio": round(float(precio), 2), "precio_antes": None,
                       "pvpr": float(pvpr) if isinstance(pvpr, (int, float)) and pvpr > precio else None,
                       "marca": marca, "ean": None, "imagen_url": None, "valoracion": None, "resenas": None,
                       "url": f"https://es.privalia.com/gr/catalog/{venta}/{cat or ''}".rstrip("/") + f"?producto={pid}"})
    return salida


def guardar_campanas(tienda, campanas):
    """Las campañas de la portada pasan a ser el catálogo de la tienda (se leen por turnos, catalogo_por_tienda).
    Cambian cada pocos días: se sustituye la lista entera."""
    cat = urls_catalogo()
    if cat.get(tienda) != campanas:
        cat[tienda] = campanas
        guardar_catalogo(cat)


RE_ALI_ITEM_JSON = re.compile(r'"title":"((?:[^"\\]|\\.)*)"(.{0,8000}?)"detailUrl":"(?:https?:)?//[^"]*?/item/(\d+)\.html',
                              re.S)


def productos_aliexpress(html):
    """AliExpress (SuperDeals, 17/11/2026): los productos vienen en los datos de la página. Por producto:
    título, precio de venta y original (salePriceAmount / originPriceAmount, en céntimos de euro), reseñas,
    estrellas (reviewStar, sobre 5), marca y si admite cupones ("Elegible con cupones")."""
    salida, vistos = [], set()
    for titulo, medio, item in RE_ALI_ITEM_JSON.findall(html):
        m = unquote(unquote(medio))
        venta = re.search(r'"salePriceAmount":"(\d+)"', m)
        if item in vistos or not venta or not re.search(r'"currency":"EUR"', m):
            continue
        original = re.search(r'"originPriceAmount":"(\d+)"', m)
        precio, antes = int(venta.group(1)) / 100, int(original.group(1)) / 100 if original else None
        res, estrellas = re.search(r'"reviews":(\d+)', m), re.search(r'"reviewStar":"([\d.]+)"', m)
        marca = re.search(r'"brandName":"([^"]+)"', m)
        try:
            nombre = json.loads(f'"{titulo}"')
        except ValueError:
            nombre = titulo
        vistos.add(item)
        p = {"nombre": " ".join(nombre.split()), "precio": round(precio, 2),
             "precio_antes": antes if antes and antes > precio else None,
             "marca": marca.group(1) if marca and marca.group(1).lower() not in ("none", "null", "") else None,
             "ean": None, "url": f"https://es.aliexpress.com/item/{item}.html", "imagen_url": None,
             "valoracion": round(float(estrellas.group(1)) * 20) if estrellas else None,
             "resenas": int(res.group(1)) if res else None}
        if "Elegible con cupones" in m:
            p["cupon_elegible"] = True
        salida.append(p)
    return salida or productos_aliexpress_busqueda(html)


RE_ALI_BUSQ_ID = re.compile(r'"productId":"(\d{8,20})"')


def productos_aliexpress_busqueda(html):
    """Búsquedas de AliExpress (/w/wholesale-<palabras>.html, 17/11/2026): otro formato de datos. Por producto:
    productId, title.displayTitle, prices.salePrice (minPrice en euros y "discount" en %; el original no
    viene, se calcula con el descuento), evaluation.starRating (sobre 5) y trade.tradeDesc ("800+ vendidos")."""
    salida, vistos = [], set()
    marcas = list(RE_ALI_BUSQ_ID.finditer(html))
    for i, m in enumerate(marcas):
        item = m.group(1)
        trozo = html[m.end():marcas[i + 1].start() if i + 1 < len(marcas) else m.end() + 8000][:8000]
        titulo = re.search(r'"displayTitle":"((?:[^"\\]|\\.)*)"', trozo)
        venta = re.search(r'"salePrice":\{([^{}]*)\}', trozo)
        if item in vistos or not titulo or not venta or '"currencyCode":"EUR"' not in venta.group(1):
            continue
        precio = re.search(r'"minPrice":([\d.]+)', venta.group(1))
        if not precio:
            continue
        precio = float(precio.group(1))
        dto = re.search(r'"discount":(\d+)', venta.group(1))
        antes = round(precio / (1 - int(dto.group(1)) / 100), 2) if dto and 0 < int(dto.group(1)) < 95 else None
        estrellas = re.search(r'"starRating":"?([\d.]+)', trozo)
        try:
            nombre = json.loads(f'"{titulo.group(1)}"')
        except ValueError:
            nombre = titulo.group(1)
        vistos.add(item)
        salida.append({"nombre": " ".join(nombre.split()), "precio": round(precio, 2),
                       "precio_antes": antes if antes and antes > precio else None, "marca": None, "ean": None,
                       "url": f"https://es.aliexpress.com/item/{item}.html", "imagen_url": None,
                       "valoracion": round(float(estrellas.group(1)) * 20) if estrellas else None, "resenas": None})
    return salida


RE_AMZ_ASIN_LINK = re.compile(r'\{"asin":"([A-Z0-9]{10})","title":"')  # las variantes de color no llevan "title"
RE_AMZ_BASE = re.compile(r'"basisPrice":\{"label":"([^"]{1,40})","price":"([\d.]+)"')
ETIQUETAS_AMAZON = {"mediano": "mediano", "más bajo": "mas_bajo", "mas bajo": "mas_bajo", "recomendado": "recomendado"}


REF_TIENDA_HISTORICO = {"mediano": "Amazon: mediana de lo pagado en 90 días",
                        "mas_bajo": "Amazon: más bajo de los últimos 30 días"}


CAMEL_URL = "https://es.camelcamelcamel.com/product/{asin}"
CAMEL_MAX_RONDA = 8      # consultas nuevas por ronda (lo que está en caché no cuenta)
CAMEL_CACHE_H = 24
CAMEL_PAUSA_MIN = 30     # si sale reto: pausa fija, como Miravia y AliExpress
RE_RETO_PAGINA = re.compile(r"cf-chl|challenge-platform|just a moment\.\.\.|captcha|verify you are (?:a )?human", re.I)
RE_CAMEL_FILA = re.compile(r'<tr class="pt (amazon|new)\b[^"]*"[^>]*>(.*?)</tr>', re.S | re.I)
RE_CAMEL_CELDA = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)
MESES_EN = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                         "dec"), 1)}


def _fecha_camel(txt):
    """'(Jul 09, 2020)' -> '2020-07-09'."""
    m = re.search(r"([A-Za-z]{3})\w*\s+(\d{1,2}),\s*(\d{4})", txt or "")
    if not m or m.group(1).lower() not in MESES_EN:
        return None
    return f"{m.group(3)}-{MESES_EN[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"


def camel_de_html(html):
    """Tabla de CamelCamelCamel -> {minimo, minimo_fecha, maximo, media, fila} o None. Fila de Amazon (lo que vende
    Amazon); si no tiene precios, la de otros vendedores nuevos ("new")."""
    filas = {m.group(1).lower(): [c for c in RE_CAMEL_CELDA.findall(m.group(2))] for m in RE_CAMEL_FILA.finditer(html)}
    for fila in ("amazon", "new"):
        celdas = filas.get(fila) or []
        if len(celdas) < 5:
            continue
        minimo, maximo, media = (a_numero(re.sub(r"<small>.*?</small>", "", c, flags=re.S)) for c in
                                 (celdas[1], celdas[2], celdas[4]))
        if minimo and media:
            return {"minimo": minimo, "minimo_fecha": _fecha_camel(celdas[1]), "maximo": maximo, "media": media,
                    "fila": fila}
    return None


def _camel_cache(con, asin):
    fila = con.execute("SELECT v FROM meta WHERE k=?", (f"camel:{asin}",)).fetchone()
    if fila:
        try:
            g = json.loads(fila[0])
            if datetime.fromisoformat(g["t"]) > datetime.now() - timedelta(hours=CAMEL_CACHE_H):
                return True, g["h"]
        except (ValueError, KeyError, TypeError):
            pass
    return False, None


def camel_lote(con, cfg, productos):
    """Histórico de CamelCamelCamel para productos de Amazon (por ASIN), en una lista paralela (dict o None).
    Chrome visible normal (`NavegadorNormal`), autorizado por Zeta el 17/11/2026: la descarga da 403. Caché de 24 h
    por ASIN, también de lo que no tiene datos. Si sale reto o captcha no se resuelve: pausa fija de 30 min."""
    salida, faltan = [None] * len(productos), []
    for i, p in enumerate(productos):
        hay, h = _camel_cache(con, asin_de(p.get("url")))
        if hay:
            salida[i] = h
        else:
            faltan.append(i)
    pausa = con.execute("SELECT v FROM meta WHERE k='pausa:camelcamelcamel.com'").fetchone()
    if not faltan or (pausa and pausa[0] > datetime.now().isoformat(timespec="minutes")):
        return salida
    try:
        with NavegadorNormal(cfg) as nav:
            for i in faltan:
                asin = asin_de(productos[i].get("url"))
                try:
                    html, _, _ = nav.leer(CAMEL_URL.format(asin=asin))
                except OSError as e:
                    if "reto" in str(e) or RE_NO_SE_LEE.search(str(e)):
                        hasta = (datetime.now() + timedelta(minutes=CAMEL_PAUSA_MIN)).isoformat(timespec="minutes")
                        con.execute("INSERT OR REPLACE INTO meta VALUES ('pausa:camelcamelcamel.com', ?)", (hasta,))
                        log(f"camelcamelcamel: {str(e)[:60]}, en pausa {CAMEL_PAUSA_MIN} min")
                        break
                    continue
                if RE_RETO_PAGINA.search(html[:60000]) and "camelegend" not in html:
                    hasta = (datetime.now() + timedelta(minutes=CAMEL_PAUSA_MIN)).isoformat(timespec="minutes")
                    con.execute("INSERT OR REPLACE INTO meta VALUES ('pausa:camelcamelcamel.com', ?)", (hasta,))
                    log(f"camelcamelcamel: reto, en pausa {CAMEL_PAUSA_MIN} min")
                    break
                salida[i] = camel_de_html(html)
                con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (
                    f"camel:{asin}", json.dumps({"t": datetime.now().isoformat(timespec="minutes"), "h": salida[i]})))
    except Exception as e:  # sin navegador: sigue el resto del contraste
        log(f"camelcamelcamel: {str(e)[:100]}")
    con.commit()
    return salida


def camel_guardado(con, url):
    """Lo que ya se sabe de CamelCamelCamel de un enlace de Amazon (caché), sin abrir nada."""
    asin = asin_de(url)
    return _camel_cache(con, asin)[1] if asin else None


def contraste_ref_tienda(p, f=None):
    """Amazon dice qué es su precio tachado (Omnibus). "Mediano" (90 días) y "Más bajo" (30 días) son su propio
    histórico; "Recomendado" no cuenta. Autorizado por Zeta el 17/11/2026. Misma bajada que el habitual propio
    (`bajada_min_habitual`, un 20 %). -> True/False, o None si no aplica (sin etiqueta útil o apagado con
    `filtros_codigo.ref_amazon` false). Deja la referencia en p["_hist"]."""
    f = f or {}
    if p.get("ref_tienda") not in REF_TIENDA_HISTORICO or not p.get("precio_antes") or not f.get("ref_amazon", True):
        return None
    ref = p["precio_antes"]
    p["_hist"] = {"habitual": ref, "minimo": ref if p["ref_tienda"] == "mas_bajo" else None,
                  "fuente_corta": REF_TIENDA_HISTORICO[p["ref_tienda"]]}
    return p["precio"] <= ref * (1 - f.get("bajada_min_habitual", 0.20))


def etiquetas_amazon(html):
    """{ASIN: (etiqueta, precio)} del precio tachado de las ofertas de Amazon (17/11/2026). Por la norma europea
    de precios (Omnibus) Amazon dice qué es: "Recomendado" (PVPR del fabricante, no sirve de contraste),
    "Mediano" (mediana de lo pagado en Amazon en 90 días) o "Más bajo" (el más bajo de los últimos 30 días).
    En los datos de la página cada producto empieza por {"asin", "title"...} y lleva después su precio."""
    salida, asins = {}, [(m.start(), m.group(1)) for m in RE_AMZ_ASIN_LINK.finditer(html)]
    for m in RE_AMZ_BASE.finditer(html):
        previos = [a for pos, a in asins if pos < m.start()]
        etiqueta = ETIQUETAS_AMAZON.get(m.group(1).strip().rstrip(":").lower())
        if previos and etiqueta and previos[-1] not in salida:
            salida[previos[-1]] = (etiqueta, float(m.group(2)))
    return salida


def extraer(html, base_url, extra_json=()):
    """Productos de una página. extra_json: respuestas JSON capturadas por el navegador."""
    host = urlparse(base_url).netloc.lower()
    if "amazon." in host:
        productos = _extraer(html, base_url, extra_json)
        etiquetas = etiquetas_amazon(html) if '"basisPrice"' in html else {}
        for p in productos:
            e = etiquetas.get(asin_de(p.get("url")) or "")
            if e and p.get("precio_antes") and abs(p["precio_antes"] - e[1]) < 0.015:
                p["ref_tienda"] = e[0]
        principal = ficha_amazon(html, base_url)
        if principal:  # en una ficha, el producto de la página va primero (no el primero del carrusel)
            productos = [principal]  # el carrusel de recomendados no es de esta página
        return productos
    productos = _extraer(html, base_url, extra_json)
    for p in productos:
        if not p.get("marca"):
            p["marca"] = marca_de_tarjeta(html, p.get("nombre"))
        nombre = nombre_zalando(p.get("url")) if "zalando." in host else None
        p["nombre"] = nombre or nombre_con_marca_de_ficha(p)
    return productos


RE_AMZ_TITULO = re.compile(r'id="productTitle"[^>]*>\s*([^<]+?)\s*<')
RE_AMZ_COMPRA = re.compile(r'"desktop_buybox_group_1":\[\{[^\]]*?"priceAmount":([\d.]+)')
RE_AMZ_RECOMENDADO = re.compile(r'class="[^"]*basisPrice[^"]*"[^>]*>.{0,300}?a-offscreen">\s*([\d.,]+)\s*(?:&nbsp;)?€', re.S)


def ficha_amazon(html, url):
    """Ficha de Amazon (/dp/ASIN): título y precio del bloque de compra (lo que ve el comprador, sin sesión), y el
    precio recomendado tachado. Sin esto la ficha daba el primer producto del carrusel de recomendados (LEGO 75433:
    salía un LEGO KPop de 55,99 € en vez de 49,99 €, Zeta 18/11/2026)."""
    asin = asin_de(url)
    t, c = RE_AMZ_TITULO.search(html or ""), RE_AMZ_COMPRA.search(html or "")
    if not (asin and t and c and "/dp/" in urlparse(url).path):
        return None
    r = RE_AMZ_RECOMENDADO.search(html)
    antes = a_numero(r.group(1)) if r else None
    precio = float(c.group(1))
    return {"nombre": unescape(t.group(1)).strip(), "precio": precio, "precio_antes": antes if antes and antes > precio else None,
            "url": f"https://www.amazon.es/dp/{asin}", "ean": None, "marca": None}


RE_MARCA_TARJETA = re.compile(r'class="[^"]*\b(?:product-)?(?:brand|marca|manufacturer)\w*[^"]*"[^>]*>\s*([^<]{2,40}?)\s*<',
                              re.I)


def marca_de_tarjeta(html, nombre):
    """La marca que la tarjeta pone en su propio elemento encima del nombre (Toys R Us: <p class="product-brand">
    KLEIN</p> y debajo "Hot Wheels - Maletín..."). Se busca justo antes del nombre en la página."""
    if not html or not nombre:
        return None
    i = html.find(escape(nombre[:40], quote=False)) if escape(nombre[:40], quote=False) in html else html.find(nombre[:40])
    if i < 0:
        return None
    marcas = RE_MARCA_TARJETA.findall(html[max(0, i - 1200):i])
    return unescape(marcas[-1]).strip() if marcas else None


RE_ID_FINAL = re.compile(r"^(?:p|k?\d{5,}|[a-z]{0,3}\d{5,}[a-z0-9]*|\d+)$", re.I)


def nombre_con_marca_de_ficha(p):
    """La marca que la tienda pone aparte (encima del título en Toys R Us, en la dirección en Forumsport: el
    nombre era solo "CLUB" y eran unas Nike) delante del nombre (Zeta, 18/11/2026). Orden: el campo marca de la
    tienda; si el nombre no lleva ninguna marca conocida, la que diga la dirección; y si el nombre es de una o dos
    palabras, el de la dirección entero."""
    nombre = p.get("nombre") or ""
    rx = rx_marcas()
    tiene = bool(rx and rx.search(sin_tildes(nombre.lower())))
    marca = (p.get("marca") or "").strip()
    if marca and sin_tildes(marca.lower()) not in sin_tildes(nombre.lower()):
        return f"{marca} {nombre}".strip()
    if tiene:
        return nombre
    ruta = unquote(urlparse(p.get("url") or "").path).strip("/").split("/")
    trozo = max(ruta, key=len) if ruta and ruta != [""] else ""
    palabras = [w for w in re.split(r"[-_\s]+", re.sub(r"\.(?:html?|php|aspx?)$", "", trozo)) if w]
    while palabras and RE_ID_FINAL.match(palabras[-1]):
        palabras.pop()
    desde_url = " ".join(palabras)
    marca_url = rx.search(sin_tildes(desde_url.lower())) if rx and desde_url else None
    corto = len(nombre.split()) == 1 or (len(nombre.split()) <= 2 and nombre.isupper())  # "CLUB", "CLUB GFX"
    if corto and marca_url and len(palabras) >= 3:
        return " ".join(w.capitalize() for w in palabras)
    if marca_url:
        return f"{marca_url.group(0).title()} {nombre}".strip()
    return nombre


RE_ZALANDO_SLUG = re.compile(r"zalando\.[a-z.]+/([a-z0-9-]+?)-[a-z0-9]{9}-[a-z0-9]{3}\.html", re.I)


def nombre_zalando(url):
    """Zalando: el nombre de la tarjeta era el texto de la foto ("Zapatilla blanca de lona alta con suela...") y no
    decía ni marca ni modelo (18/11/2026). El de verdad va en la dirección: /converse-chuck-classic-unisex-zapatillas-
    altas-white-monochrome-co411a00u-a11.html -> "Converse Chuck Classic Unisex Zapatillas Altas White Monochrome"."""
    m = RE_ZALANDO_SLUG.search(url or "")
    if not m:
        return None
    return " ".join(w.capitalize() for w in m.group(1).split("-") if w)


def _extraer(html, base_url, extra_json=()):
    host = urlparse(base_url).netloc.lower()
    especifico = (tarjetas_miravia(html) if host.endswith("miravia.es")
                  else productos_aliexpress(html) if "aliexpress." in host
                  else productos_pss(extra_json) if host.endswith("privatesportshop.es")
                  else productos_privalia(html) if "privalia." in host
                  else productos_game(html) if host.endswith("game.es")
                  else productos_jysk(html) if host.endswith("jysk.es")
                  else productos_eci(html) if host.endswith(("elcorteingles.es", "hipercor.es")) else [])
    if especifico:
        return especifico
    objetos = objetos_producto(html)
    cont = [0]
    for bloque in extra_json:
        normalizar_importes(bloque)
        recorrer(bloque, objetos, cont)
    productos = de_objetos(objetos, base_url)
    if len(productos) < MIN_LISTADO:  # sin datos estructurados: tarjetas visibles (Euronics, Back Market...)
        ya = {p["nombre"].lower() for p in productos}
        productos += [p for p in de_objetos(tarjetas_html(html, base_url), base_url)
                      if p["nombre"].lower() not in ya]
    sin_url = [p for p in productos if not p["url"]]
    if sin_url:
        url_desde_html(html, sin_url, base_url)
    sin_antes = [p for p in productos if not p["precio_antes"]]
    if sin_antes:
        precio_antes_html(html, sin_antes, todos=productos)
    if productos:
        promos_html(html, productos)
    return productos


RE_A_ABRE = re.compile(r"<a\b[^>]*\bhref=\"([^\"#]+)\"[^>]*>|</a>", re.I)


def url_desde_html(html, productos, base_url):
    """Productos sin URL en los datos (p. ej. ElectroDepot: el JSON solo trae sku y precio):
    se toma el enlace <a href> de la tarjeta que contiene el nombre del producto."""
    usadas = set()
    for p in productos:
        # En el HTML el nombre puede ir escapado ('TV 65&quot;') y con espacios dobles.
        cuerpo = "|".join(r"\s+".join(map(re.escape, n.split())) for n in {p["nombre"], escape(p["nombre"])})
        for patron in (rf"\"\s*(?:{cuerpo})\s*\"", rf">\s*(?:{cuerpo})\s*<"):
            m_nombre = re.search(patron, html)
            if not m_nombre:
                continue
            i = m_nombre.start()
            abierto = None
            for m in RE_A_ABRE.finditer(html, max(0, i - 4000), i):
                abierto = m.group(1)  # None si es </a>: el enlace anterior ya se cerró
            if abierto and abierto.startswith(("/", "http")):
                url = urljoin(base_url, abierto)
                if url not in usadas and url.rstrip("/") != base_url.rstrip("/"):
                    p["url"] = url
                    usadas.add(url)
            break


RE_A_COMPLETO = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
RE_ATR_HREF = re.compile(r"\bhref=\"([^\"#]+)\"", re.I)
RE_ATR_TITULO = re.compile(r"\b(?:title|alt)=\"([^\"]{8,250})\"", re.I)
# Ficha de producto: /tv-lg-55-oled-123.html, /p/123456, /product/..., /dp/..., /spd/...
RE_RUTA_FICHA = re.compile(r"/(?:p|pd|dp|spd|product|producto|productos|item|articulo)/|[\w]*\d{5,}|"
                           r"/[^/?]*-[^/?]*-[^/?]*[^/?]{4,}/?(?:\?|$)", re.I)
RE_NO_PRECIO = re.compile(r"ahorr|env[ií]o|reembolso|cashback|regalo|hasta|cuota|financ|\+", re.I)
# Lo que va detrás del importe: cuota mensual o descuento ('50€ extra de dto.', '40 € descontados').
RE_NO_PRECIO_DESPUES = re.compile(r"\s*(?:/\s*mes|al mes|mes\b|extra|de dto|dto|de descuento|descuento|"
                                  r"descontad|de ahorro|menos|off\b)", re.I)
RE_PRECIO_PARTIDO = re.compile(r"(\d)\s*([.,])\s*(\d{2})(?=\s*(?:€|EUR))")
RE_ENLACE_OPINIONES = re.compile(r"product-reviews|customer-?reviews|/opiniones/|/reviews?/", re.I)
RE_VALORACION = re.compile(r"\d(?:[.,]\d)?\s+(?:de|out of)\s+5\s+estrellas|\d(?:[.,]\d)? de 5\b", re.I)
RE_TEXTO_GENERICO =re.compile(r"^(?:ver|comprar|añadir|acceder|más|mas|detalles|ficha)\b", re.I)


# Etiqueta HTML respetando las comillas: Jysk (Tailwind) lleva ">" dentro de los atributos ("[&>svg]:...") y
# "<[^>]+>" cortaba ahí, dejaba restos de HTML en el texto y no llegaba al precio (1 producto de 69, 18/11/2026).
RE_ETIQUETA_HTML = re.compile(r"<(?:[^>\"']|\"[^\"]*\"|'[^']*')*>")
# Sin tocar lo que sea un precio: en Jysk el precio rebajado es "theme-product-price-discount".
RE_ETIQUETA_DESCUENTO = re.compile(r'<(\w+)[^>]*class="(?![^"]*price)[^"]*(?:discount|descuento|ahorro|saving|you-save|'
                                   r'product-flag)[^"]*"[^>]*>[^<]{0,40}</\1>', re.I)


def tarjetas_html(html, base_url, tope=6000):
    """Último recurso para listados sin datos estructurados: cada enlace a una ficha de producto
    con un precio en € en su tarjeta (hasta el enlace a la siguiente ficha) es un producto.
    El precio es el primer importe que no esté tachado ni sea una cuota, un ahorro o el envío;
    el precio anterior lo pone luego precio_antes_html con el tachado de la misma tarjeta."""
    plano = re.sub(r"<script\b.*?</script>|<style\b.*?</style>|<svg\b.*?</svg>|<!--.*?-->|<noscript\b.*?</noscript>",
                   "", html, flags=re.S | re.I)
    host = urlparse(base_url).netloc.replace("www.", "")
    fichas = {}  # url -> [primera posición, [nombres candidatos]]
    for m in RE_A_COMPLETO.finditer(plano):
        h = RE_ATR_HREF.search(m.group(1))
        if not h:
            continue
        url = urljoin(base_url, unescape(h.group(1)))
        partes = urlparse(url)
        if not partes.netloc.replace("www.", "").endswith(host) or not RE_RUTA_FICHA.search(partes.path + "?"):
            continue
        if RE_ENLACE_OPINIONES.search(partes.path):
            continue  # Amazon: el enlace a las opiniones partía la tarjeta y se quedaba con su precio
        texto = " ".join(RE_ETIQUETA_HTML.sub(" ", unescape(RE_ETIQUETA_HTML.sub(" ", m.group(2)))).split())
        f = fichas.setdefault(url, [m.start(), []])
        if texto:
            f[1].append(texto)
        # Tarjeta entera dentro del enlace (marca + nombre + precio): el nombre va en su título <h2>/<h3>.
        f[1] += [" ".join(unescape(re.sub(r"<[^>]+>", " ", x)).split())
                 for x in re.findall(r"<h[2-4]\b[^>]*>(.*?)</h[2-4]>", m.group(2), re.S | re.I)]
        f[1] += [unescape(t) for t in RE_ATR_TITULO.findall(m.group(0))]
    orden = sorted(fichas.items(), key=lambda kv: kv[1][0])
    rx_euros = RE_EUROS_DELANTE if euro_delante(plano) else RE_EUROS
    salida = []
    for n, (url, (ini, nombres)) in enumerate(orden):
        fin = min(ini + tope, orden[n + 1][1][0] if n + 1 < len(orden) else len(plano))
        # La etiqueta de descuento ("-200,10 €" en Neobyte, 18/11/2026) no es el precio: fuera antes de buscarlo.
        trozo = RE_ETIQUETA_DESCUENTO.sub(" ", plano[ini:fin])
        visible = RE_ETIQUETA_HTML.sub(" ", unescape(RE_ETIQUETA_HTML.sub(" ", RE_TACHADO.sub(" ", trozo))))
        visible = RE_PRECIO_PARTIDO.sub(r"\1\2\3", visible)  # Back Market: '684 ,23 €' -> '684,23 €'
        visible = RE_PARTIDO_DELANTE.sub(r"\1\2\3", visible)  # Expert: '€58 ,90' -> '€58,90'
        precio = None
        for e in rx_euros.finditer(visible):
            antes_txt, despues_txt = visible[max(0, e.start() - 20):e.start()], visible[e.end():e.end() + 16]
            if RE_NO_PRECIO.search(antes_txt) or RE_NO_PRECIO_DESPUES.match(despues_txt):
                continue
            if re.search(r"[-−–]\s*$", antes_txt):
                continue  # "-200,10 €": lo que se descuenta, no el precio
            precio = a_numero(e.group(1).replace(" ", ""))
            break
        buenos = [t for t in nombres if 8 <= len(t) <= 250 and re.search(r"[a-zA-Z]{3}", t)
                  and not RE_TEXTO_GENERICO.match(t) and not RE_EUROS.search(t) and not RE_EUROS_DELANTE.search(t)
                  and not RE_VALORACION.match(t)]
        if precio and 0.5 <= precio <= 20000 and buenos:
            salida.append({"name": max(buenos, key=len), "price": precio, "url": url})
    return salida


def _anclas(p):
    anclas = []
    if p["url"]:
        ruta = urlparse(p["url"]).path
        if len(ruta) > 8:
            anclas.append(ruta)
    anclas.append(p["nombre"][:30])
    return anclas


RE_TACHADO = re.compile(
    r"<(del|s|strike)\b[^>]*>(.*?)</\1>"
    r"|<(\w+)\b[^>]*(?:class|data-test|data-testid|data-qa)=\"[^\"]*"
    r"(?:strike|tachad|crossed|old-?price|price-?old|(?:__|-)old\b|before|was-?price|"
    r"anterior|original|pvpr?\b|line-through|regular)[^\"]*\"[^>]*>(.*?)</\3>",
    re.I | re.S)
RE_EUROS = re.compile(r"(\d{1,3}(?:[.\s]\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(?:€|&euro;|EUR)", re.I)
# Euro delante del importe (Alternate: '€ 265,00'; Expert: '€58 ,90'). Con RE_EUROS se cogía
# el número de antes del símbolo: el '9' de 'Formato: 32:9' o el '2431' de 'Krups KP2431' (17/11/2026).
RE_EUROS_DELANTE = re.compile(r"(?:€|&euro;|EUR)\s*(\d{1,3}(?:\.\d{3})*(?:,\d{1,2})?|\d+(?:[.,]\d{1,2})?)(?![\d.,])",
                              re.I)
RE_PARTIDO_DELANTE = re.compile(r"((?:€|EUR)\s*\d[\d.]*)\s+([.,])\s*(\d{2})(?!\d)")
RE_CENTIMOS_DELANTE = re.compile(r"(?:€|EUR)\s*\d[\d.]*,\d{2}(?!\d)")
RE_CENTIMOS_DETRAS = re.compile(r"\d,\d{2}\s*(?:€|EUR)")


def euro_delante(plano):
    """True si la página escribe los precios con el euro delante: más importes con céntimos
    del tipo '€ 58,90' que del tipo '58,90 €'. Se decide por página para no tocar las tiendas de siempre."""
    texto = unescape(re.sub(r"<[^>]+>", " ", plano))
    texto = RE_PARTIDO_DELANTE.sub(r"\1\2\3", RE_PRECIO_PARTIDO.sub(r"\1\2\3", texto))
    return len(RE_CENTIMOS_DELANTE.findall(texto)) > len(RE_CENTIMOS_DETRAS.findall(texto))


# "-75%" suelto (no "-10% extra", "hasta -50%" ni "-5% en app", que son promociones, no el descuento del producto).
RE_ETIQUETA_DTO = re.compile(r"(?<![\w%])-\s?(\d{1,2})\s?%(?!\s*(?:extra|adicional|en\s+app|dto\.?\s+extra))", re.I)


def precio_antes_html(html, productos, ventana=12000, todos=None):
    """Busca el precio tachado junto al producto en el HTML visible (<del>, <s> o clases /
    data-test tipo 'old-price', 'strike'...). Mira la tarjeta del producto: desde su enlace hasta
    el enlace del siguiente producto (máximo 'ventana' caracteres). Solo acepta precios mayores
    que el actual y menores de 4 veces. todos: todos los productos de la página (para los límites)."""
    plano = re.sub(r"<script\b.*?</script>|<style\b.*?</style>|<svg\b.*?</svg>", "", html, flags=re.S | re.I)
    inicios = sorted({plano.find(a) for q in (todos or productos) for a in _anclas(q)} - {-1})
    for p in productos:
        for ancla in _anclas(p):
            i = plano.find(ancla)
            if i < 0:
                continue
            # Desde el enlace hacia delante: mirar hacia atrás cogía el tachado de la tarjeta anterior.
            siguientes = [x for x in inicios if x > i]
            trozo = plano[i:min(i + ventana, siguientes[0] if siguientes else len(plano))]
            if not RE_TACHADO.search(trozo):
                # Sin precio tachado pero con etiqueta de descuento en la tarjeta (Instant Gaming: "-75%").
                m = RE_ETIQUETA_DTO.search(re.sub(r"<[^>]+>", " ", trozo))
                pct = porcentaje(m.group(1) + "%") if m else None
                if pct:
                    p["precio_antes"] = round(p["precio"] / (1 - pct), 2)
                    break
            for m in RE_TACHADO.finditer(trozo):
                contenido = m.group(2) or m.group(4) or ""
                txt = unescape(re.sub(r"<[^>]+>", " ", contenido))
                for e in RE_EUROS.findall(txt) or RE_EUROS_DELANTE.findall(txt):
                    v = a_numero(e.replace(" ", ""))
                    if v and p["precio"] * 1.01 < v < p["precio"] * 4:
                        p["precio_antes"] = round(v, 2)
                        break
                if p["precio_antes"]:
                    break
            if p["precio_antes"]:
                break


# Multicompra (17/11/2026, Zeta: "si es la segunda unidad al 70 % no te enteras"). "2.ª unidad al 70 %" en
# las tiendas españolas (Carrefour, Druni...) es un 70 % de descuento en la segunda, igual que "-70 %".
_SEGUNDA = r"(?:2\s*[ªºa]\.?|segunda)\s*(?:unidad|ud\.?)"
RE_SEGUNDA_PCT = re.compile(rf"{_SEGUNDA}\s*(?:al|a|con)?\s*(?:un\s*)?-?\s*(\d{{1,3}})\s*%"
                            rf"|-?\s*(\d{{1,3}})\s*%\s*(?:de\s+descuento\s+|dto\.?\s+)?(?:en|a)\s+(?:la\s+)?{_SEGUNDA}", re.I)
RE_ACUMULADO = re.compile(r"[\s.,:;()-]*(?:de\s+)?(?:dto\.?\s+)?(?:acum|en\s+(?:tu\s+)?(?:tarjeta|saldo|cheque|club)|"
                          r"para\s+(?:tu\s+)?pr[oó]xima|reembols|cashback)", re.I)
RE_SEGUNDA_GRATIS = re.compile(rf"{_SEGUNDA}\s*(?:gratis|de\s+regalo|a\s+0\s*€)", re.I)
RE_NXM = re.compile(r"(?<![\d.,x×])([2-5])\s*[x×]\s*([1-4])(?![\d.,x×])(?!\s*(?:m|cm|mm|metros?|kg|g|l)\b)"
                    r"|ll[eé]vate\s+([2-5])\s*,?\s*paga\s+([1-4])|([3-5])\s+por\s+([2-4])\b(?!\s*(?:cm|m)\b)", re.I)


# "NxM" que es una característica del producto, no una promoción (17/11/2026: un aire acondicionado "Split 2x1",
# dos unidades interiores y una exterior, salió como oferta 2x1 a mitad de precio).
RE_NXM_TECNICO = re.compile(r"split|multi\s*-?split|inverter|frigor|btu|\bkw\b|\bw\b|conductor|cable|hilo|manguera|"
                            r"hdmi|usb|\bvga\b|displayport|kvm|conmutador|splitter|switch|matriz|puertos?|entradas?|"
                            r"salidas?|enchufes?|regleta|bocas|v[ií]as|tomas|canales|altavoces|"
                            r"tracci[oó]n|rueda|neum[aá]tico|ejes?|\bcm\b|\bmm\b|pulgadas|\bpanel", re.I)
# En el NOMBRE del producto, un NxM solo es promoción si lo dice ("oferta 2x1", "promoción 3x2", "llévate"...).
RE_NXM_PROMO = re.compile(r"oferta|promo|ll[eé]vate|paga|regalo|gratis|en\s+toda|comprando|\bpack\s+ahorro", re.I)


def promo_multicompra(texto, nombre=False):
    """-> (descripción, unidades, factor del precio por unidad) o None.
    '2.ª unidad al -70 %' -> ('2.ª unidad al -70 %', 2, 0.65); '3x2' -> ('3x2', 3, 0.667).
    nombre=True: el texto es el nombre del producto, donde "2x1" suele ser una característica (Split 2x1,
    switch HDMI 2x1): solo cuenta si va con una palabra de promoción."""
    t = unescape(texto or "")
    for m in RE_SEGUNDA_PCT.finditer(t):
        # "2.ª unidad -50 % acum. en tu tarjeta" (Alcampo): es saldo para otra compra, no descuento en caja.
        # Reglas de formato de Zeta: un reembolso nunca se resta del pago directo.
        if RE_ACUMULADO.match(t[m.end():m.end() + 40]):
            continue
        pct = int(m.group(1) or m.group(2))
        if 1 <= pct <= 100:
            return f"2.ª unidad al -{pct} %", 2, round((2 - pct / 100) / 2, 4)
    if RE_SEGUNDA_GRATIS.search(t):
        return "2.ª unidad gratis", 2, 0.5
    for m in RE_NXM.finditer(t):
        n, pagas = [int(x) for x in m.groups() if x][:2]
        if n <= pagas:
            continue
        cerca = t[max(0, m.start() - 30):m.end() + 30]
        if RE_NXM_TECNICO.search(cerca) and not RE_NXM_PROMO.search(cerca):
            continue  # "Split 2x1", "switch HDMI 3x1": es el producto, no la oferta
        if nombre and not RE_NXM_PROMO.search(cerca) and not m.group(3):
            continue
        return f"{n}x{pagas}", n, round(pagas / n, 4)
    return None


def promos_html(html, productos, ventana=4000):
    """Marca en cada producto la multicompra de su tarjeta (desde su enlace hasta el del siguiente producto) o de
    su nombre: p['promo'], p['unidades'] y p['precio_unidad']. No cambia p['precio']: eso lo decide la ronda."""
    plano = re.sub(r"<script\b.*?</script>|<style\b.*?</style>|<svg\b.*?</svg>", "", html, flags=re.S | re.I)
    # Primera ancla encontrada de cada producto (enlace o nombre). La tarjeta va hasta la del producto
    # siguiente: con todas las anclas, se cortaba en el nombre del propio producto, antes de la etiqueta.
    primera = {}
    for k, q in enumerate(productos):
        posiciones = [x for x in (plano.find(a) for a in _anclas(q)) if x >= 0]
        if posiciones:
            primera[k] = min(posiciones)
    inicios = sorted(set(primera.values()))
    for k, p in enumerate(productos):
        hallada = promo_multicompra(p["nombre"], nombre=True)
        if not hallada and k in primera:
            i = primera[k]
            siguientes = [x for x in inicios if x > i]
            trozo = plano[i:min(i + ventana, siguientes[0] if siguientes else len(plano))]
            # Sin el nombre del producto: su "2x1" de característica no es la etiqueta de la tarjeta.
            trozo = re.sub(r"<[^>]+>", " ", trozo)
            for a in {p["nombre"], escape(p["nombre"])}:
                trozo = trozo.replace(a, " ")
            hallada = promo_multicompra(trozo)
        if hallada:
            p["promo"], p["unidades"] = hallada[0], hallada[1]
            p["precio_unidad"] = round(p["precio"] * hallada[2], 2)


def porcentaje(v):
    """'-77%' / 77 / 0.77 -> 0.77 ; entre 1 % y 95 % (fuera de eso no es un descuento creíble)."""
    if isinstance(v, str):
        if "%" not in v and not re.fullmatch(r"\s*-?\d+(?:[.,]\d+)?\s*", v):
            return None  # "discount": "BLACKFRIDAY" u otro texto
        v = a_numero(v)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = abs(v)
    v = v / 100 if v > 1 else v
    return v if 0.01 <= v <= 0.95 else None


def de_objetos(objetos, base_url):
    productos, vistos = [], set()
    for o in objetos:
        nombre = nombre_de(o)
        general = a_numero(buscar(o, K_PRECIO))
        if not general or not nombre:
            continue
        oferta = a_numero(buscar(o, K_OFERTA))
        precio = oferta if oferta and 0.5 <= oferta <= general else general
        antes = a_numero(buscar(o, K_ANTES)) or precio_tachado_jsonld(o)
        if (antes is None or antes <= precio * 1.01) and general > precio:
            antes = general  # el "price" genérico era el precio normal
        if antes is None:
            pct = porcentaje(buscar(o, K_DESCUENTO, 1))
            if pct:
                antes = precio / (1 - pct)
        if antes is not None and antes <= precio * 1.01:
            antes = None
        url = buscar(o, K_URL, 0)
        url = urljoin(base_url, url) if isinstance(url, str) and url.startswith(("/", "http")) else None
        clave = (nombre.lower(), precio)
        if clave in vistos:
            continue
        vistos.add(clave)
        img = buscar(o, K_IMG)
        ean = re.sub(r"\D", "", str(buscar(o, K_EAN, 0) or ""))
        val = a_numero(buscar(o, K_VALORACION))
        res = a_numero(buscar(o, K_RESENAS))
        productos.append({
            "nombre": nombre,
            "precio": round(precio, 2),
            "precio_antes": round(antes, 2) if antes else None,
            "marca": str(buscar(o, K_MARCA) or "") or None,
            "ean": ean if len(ean) in (8, 12, 13, 14) else None,
            "url": url,
            "imagen_url": urljoin(base_url, img) if isinstance(img, str) else None,
            "valoracion": val if val is not None and 0 < val <= 100 else None,
            "resenas": int(res) if res is not None and res >= 0 else None,
        })
    return productos

# ----------------------------------------------------------------- filtros

def regex_marcas():
    if not MARCAS.exists():
        return None
    marcas = [m.strip().lower() for m in MARCAS.read_text(encoding="utf-8").splitlines()
              if m.strip() and not m.lstrip().startswith("#")]
    if not marcas:
        return None
    marcas.sort(key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, marcas)) + r")(?!\w)")


def marca_ok(p, rx, tienda=None):
    """Marca conocida en el producto, o tienda oficial de una marca conocida (samsung.com, mi.com...)."""
    if not rx:
        return False
    if rx.search(f"{p.get('marca') or ''} {p['nombre']}".lower()):
        return True
    if tienda:
        dueno = TIENDAS_MARCA.get(raiz(tienda), raiz(tienda))
        return bool(rx.fullmatch(dueno))
    return False


# Tiendas oficiales cuyo dominio no es exactamente el nombre de la marca.
TIENDAS_MARCA = {"mi": "xiaomi", "nike": "nike", "puma": "puma", "newbalance": "new balance"}

# ------------------------------------------------------------------ estado

def _columna_nueva(con, sql):
    """ALTER TABLE ... ADD COLUMN que no falla si otra ronda (otra categoría a la vez) ya la ha añadido."""
    try:
        con.execute(sql)
    except sqlite3.OperationalError as e:
        if "duplicate column" not in str(e):
            raise


def bd():
    DATOS.mkdir(exist_ok=True)
    con = sqlite3.connect(DB, timeout=30)  # varias categorías pueden coincidir
    con.executescript("""
        CREATE TABLE IF NOT EXISTS vistos (clave TEXT PRIMARY KEY, precio REAL, minimo REAL, visto TEXT);
        CREATE TABLE IF NOT EXISTS sembradas (tienda TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS cupo (fecha TEXT PRIMARY KEY, usadas INTEGER);
        CREATE TABLE IF NOT EXISTS idealo (consulta TEXT PRIMARY KEY, precio REAL, fecha TEXT);
        CREATE TABLE IF NOT EXISTS lista (id INTEGER PRIMARY KEY AUTOINCREMENT, fecha TEXT, base TEXT,
                                          precio REAL, datos TEXT);
        CREATE TABLE IF NOT EXISTS modelos (base TEXT PRIMARY KEY, veredicto TEXT, motivo TEXT, fecha TEXT);
        CREATE TABLE IF NOT EXISTS perfil (titulo TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS cm (id INTEGER PRIMARY KEY, titulo TEXT, precio REAL, tienda TEXT, host TEXT,
                                       temp REAL, publicado INTEGER, caducado INTEGER, visto TEXT);
        CREATE TABLE IF NOT EXISTS stats (fecha TEXT, tienda TEXT, rondas INTEGER DEFAULT 0,
            leidos INTEGER DEFAULT 0, errores INTEGER DEFAULT 0, candidatos INTEGER DEFAULT 0,
            enviados INTEGER DEFAULT 0, PRIMARY KEY (fecha, tienda));
        -- Historial de precios (17/11/2026). productos: una fila por producto leído. precios: una fila SOLO
        -- cuando cambia el precio. WITHOUT ROWID con clave (producto, fecha): el historial de un producto está
        -- junto en disco y sale con una lectura; el índice por fecha da "qué ha cambiado hoy".
        CREATE TABLE IF NOT EXISTS productos (clave TEXT PRIMARY KEY, tienda TEXT, nombre TEXT, ean TEXT,
                                              url TEXT) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS productos_ean ON productos(ean) WHERE ean IS NOT NULL;
        CREATE TABLE IF NOT EXISTS precios (clave TEXT, fecha TEXT, precio REAL,
                                            PRIMARY KEY (clave, fecha)) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS precios_fecha ON precios(fecha);
        -- Códigos de modelo de cada hilo de Chollometro (17/11/2026): cruce inmediato producto -> hilos.
        CREATE TABLE IF NOT EXISTS cm_codigos (codigo TEXT, id INTEGER, PRIMARY KEY (codigo, id)) WITHOUT ROWID;
        -- Cupones de tienda (17/11/2026), sacados de los hilos de cupones de Chollometro o puestos a mano.
        -- descuento en € (tipo '€') o en % (tipo '%'); sello: solo para productos marcados (MegaPROMO...).
        CREATE TABLE IF NOT EXISTS cupones (tienda TEXT, codigo TEXT DEFAULT '', descuento REAL, tipo TEXT,
            minimo REAL DEFAULT 0, hasta TEXT, sello TEXT DEFAULT '', hilo INTEGER, alta TEXT,
            PRIMARY KEY (tienda, codigo, descuento, tipo, minimo));
        -- Fichas de PCComTracker por código de modelo (17/11/2026): sirven de histórico para el mismo modelo en
        -- Amazon, MediaMarkt... Se llena al leer su catálogo; su buscador va por /api/ (prohibido en su robots.txt).
        CREATE TABLE IF NOT EXISTS pcct (codigo TEXT, url TEXT, nombre TEXT, PRIMARY KEY (codigo, url)) WITHOUT ROWID;
        -- Adónde lleva "Ir al chollo" de cada hilo (18/11/2026), con la clave del producto (clave_producto:
        -- amazon:ASIN, aliexpress:ID, miravia:ID u host+ruta). Une Chollometro con lo que leen las rondas:
        -- duplicados por enlace y precio con que triunfó. clave NULL = mirado sin enlace de producto.
        CREATE TABLE IF NOT EXISTS cm_destino (hilo INTEGER PRIMARY KEY, url TEXT, clave TEXT, fecha TEXT);
        CREATE INDEX IF NOT EXISTS cm_destino_clave ON cm_destino(clave) WHERE clave IS NOT NULL;
    """)
    if con.execute("SELECT 1 FROM sqlite_master WHERE name='asin_hilos'").fetchone():
        # 18/11/2026: asin_hilos y vigilancia (solo Amazon) pasan a cm_destino (todas las tiendas) y cm_ref.
        con.executescript("""
            INSERT OR IGNORE INTO cm_destino SELECT hilo,
                CASE WHEN asin IS NOT NULL THEN 'https://www.amazon.es/dp/' || asin END,
                CASE WHEN asin IS NOT NULL THEN 'amazon:' || asin END, fecha FROM asin_hilos;
            DROP TABLE asin_hilos;
            DROP TABLE IF EXISTS vigilancia;
        """)
    if not con.execute("SELECT 1 FROM meta WHERE k='cm_codigos_v1'").fetchone():
        # Una vez: los hilos que ya estaban en la copia. Los nuevos los indexa guardar_hilos.
        con.executemany("INSERT OR IGNORE INTO cm_codigos VALUES (?,?)",
                        [(c, i) for i, t in con.execute("SELECT id, titulo FROM cm").fetchall()
                         for c in codigos_modelo(t or "")])
        con.execute("INSERT OR REPLACE INTO meta VALUES ('cm_codigos_v1', '1')")
        con.commit()
    columnas = {c[1] for c in con.execute("PRAGMA table_info(lista)")}
    if "enviado" not in columnas:  # bases de versiones anteriores
        _columna_nueva(con, "ALTER TABLE lista ADD COLUMN enviado INTEGER DEFAULT 1")
    if "extra" not in {c[1] for c in con.execute("PRAGMA table_info(cm)")}:
        # 18/11/2026: datos del hilo para analizar categorías y productos (grupo, enlace, cupón...).
        _columna_nueva(con, "ALTER TABLE cm ADD COLUMN extra TEXT")
        con.commit()
    # Una fila por producto: cuántos hilos, el último, y el precio más bajo y más alto con que llegó a 200°.
    # La lista de vigilancia de Amazon (encargo 9) es WHERE clave LIKE 'amazon:%'. Después de la columna extra.
    con.execute("""CREATE VIEW IF NOT EXISTS cm_ref AS SELECT d.clave, COUNT(*) AS hilos, MAX(c.publicado) AS ultimo,
        MIN(CASE WHEN c.temp>=200 AND c.precio>0 THEN c.precio END) AS precio_min,
        MAX(CASE WHEN c.temp>=200 AND c.precio>0 THEN c.precio END) AS precio_max,
        MAX(c.temp) AS temp_max, MAX(c.extra LIKE '%"voucherCode":"_%') AS cupon
        FROM cm_destino d JOIN cm c ON c.id = d.hilo WHERE d.clave IS NOT NULL GROUP BY d.clave""")
    # Vigilancia de Amazon (Zeta, 18/11/2026): los ASIN que ya llegaron a 200° en Chollometro, por grupo y tipo.
    con.execute("""CREATE TABLE IF NOT EXISTS vigilancia (asin TEXT PRIMARY KEY, grupo TEXT, tipo TEXT, exito REAL,
        temp REAL, hilos INTEGER, ultimo INTEGER, nombre TEXT, mirado TEXT)""")
    if "desde" not in {c[1] for c in con.execute("PRAGMA table_info(vistos)")}:
        # Desde cuándo se sigue el precio de cada producto (histórico propio). Las filas viejas
        # empiezan a contar ahora: no se sabe cuándo se vieron por primera vez.
        _columna_nueva(con, "ALTER TABLE vistos ADD COLUMN desde TEXT")
        con.execute("UPDATE vistos SET desde=?", (datetime.now().isoformat(timespec="minutes"),))
        con.commit()
    return con


ERROR_PRECIO = 0.40


RE_ASIN = re.compile(r"/(?:dp|gp/product|gp/aw/d|exec/obidos/ASIN)/([A-Z0-9]{10})(?=[/?#]|$)", re.I)
RE_ALI_ITEM = re.compile(r"/item/(\d{8,20})\.html", re.I)


def url_canonica(url):
    """El mismo producto siempre con el mismo enlace. Amazon pone en el enlace el puesto en la lista
    ("/ref=zg_bs_electronics_3") y parámetros de sesión: cada lectura daba un enlace distinto, así que el
    producto contaba como nuevo cada vez y nunca acumulaba historial (17/11/2026). Amazon -> /dp/ASIN;
    AliExpress -> /item/<id>.html sin parámetros. El resto, tal cual."""
    if not url:
        return url
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if "amazon." in host:
        m = RE_ASIN.search(u.path)
        if m:
            return f"https://{host}/dp/{m.group(1).upper()}"
    elif "aliexpress." in host:
        m = RE_ALI_ITEM.search(u.path)
        if m:
            return f"https://{host}/item/{m.group(1)}.html"
    return url


RE_PARAM_SEGUIMIENTO = re.compile(
    r"^(?:utm_\w+|tag|ref|ref_|aff\w*|affiliate\w*|partner\w*|clickid|click_id|irclickid|gclid|fbclid|msclkid|"
    r"dclid|yclid|awc|cjevent|sscid|ranmid|raneaid|ransiteid|siteid|admitad_uid|tduid|wt\.mc_id|mc_cid|mc_eid|"
    r"campaign\w*|cmp\w*|source|src|spm|scm|pvid|algo_\w+|_trkparams|trk\w*|psc|th|smid|ascsubtag|linkcode|"
    r"creative\w*|camp|crid|sprefix|qid|sr|dib\w*|content-id|pd_rd_\w+|pf_rd_\w+)$", re.I)


def enlace_limpio(url):
    """El enlace sin parámetros de afiliado ni de seguimiento (Chollometro no admite afiliados, 18/11/2026). Los
    demás parámetros se quedan (talla, color, ?producto= de Privalia...)."""
    if not url or "?" not in url:
        return url
    u = urlsplit(url)
    qs = [(k, val) for k, val in parse_qsl(u.query, keep_blank_values=True) if not RE_PARAM_SEGUIMIENTO.match(k)]
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(qs), ""))


def asin_de(url):
    """El código ASIN de un enlace de Amazon, o None."""
    m = RE_ASIN.search(urlparse(url or "").path)
    return m.group(1).upper() if m else None


RE_MIRAVIA_ITEM = re.compile(r"/p/(?:[^/]*-)?i(\d{5,})", re.I)


def clave_producto(url):
    """El mismo producto con la misma clave, venga de un hilo de Chollometro o de una ronda (18/11/2026):
    amazon:ASIN, aliexpress:ID, miravia:ID; en el resto, host sin www + ruta sin parámetros. None si el enlace no
    es de un producto (portada, sección corta, Chollometro)."""
    if not url:
        return None
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if not host or "chollometro" in host:
        return None
    if "amazon." in host:
        a = asin_de(url)
        return f"amazon:{a}" if a else None
    if "aliexpress." in host:
        m = RE_ALI_ITEM.search(u.path)
        return f"aliexpress:{m.group(1)}" if m else None
    if "miravia." in host:
        m = RE_MIRAVIA_ITEM.search(u.path)
        return f"miravia:{m.group(1)}" if m else None
    if "privalia." in host:  # el producto no tiene página propia: va en ?producto=; una campaña no es un producto
        pid = parse_qs(u.query).get("producto", [None])[0]
        return f"privalia:{pid}" if pid else None
    ruta = unquote(u.path).rstrip("/").lower()
    if len(ruta) < 8:
        return None
    return re.sub(r"^(www\d?|m|es|tienda|shop)\.", "", host) + ruta


def clave_vista(tienda, p):
    """Clave de un producto en vistos, productos y precios: tienda + enlace (o el nombre si no hay enlace)."""
    return f"{tienda}|{p.get('url') or p['nombre'].lower()}"


def minimo_con_fecha(con, clave):
    """(precio mínimo, fecha de la primera vez a ese precio, fecha desde la que hay historial) o None."""
    fila = con.execute("SELECT precio, fecha FROM precios WHERE clave=? ORDER BY precio, fecha LIMIT 1",
                       (clave,)).fetchone()
    if not fila:
        return None
    desde = con.execute("SELECT MIN(fecha) FROM precios WHERE clave=?", (clave,)).fetchone()[0]
    return fila[0], fila[1], desde


def historial(texto, maximo=10):
    """`historial <texto|EAN|enlace>`: los productos que encajan y sus cambios de precio, del más antiguo al último."""
    con = bd()
    t = texto.strip()
    if re.fullmatch(r"\d{8,14}", t):
        filas = con.execute("SELECT clave, tienda, nombre FROM productos WHERE ean=? LIMIT ?", (t, maximo)).fetchall()
    elif t.startswith("http"):
        filas = con.execute("SELECT clave, tienda, nombre FROM productos WHERE url=? LIMIT ?", (t, maximo)).fetchall()
    else:  # todas las palabras, en cualquier orden (unos 57.000 productos: milisegundos sin índice de texto)
        palabras = [w for w in t.lower().split() if w]
        filas = con.execute("SELECT clave, tienda, nombre FROM productos WHERE "
                            + " AND ".join(["lower(nombre) LIKE ?"] * len(palabras)) + " LIMIT ?",
                            [f"%{w}%" for w in palabras] + [maximo]).fetchall()
    if not filas:
        print(f"Sin productos para «{texto}» (el historial empieza el 17/11/2026).")
    for clave, tienda, nombre in filas:
        cambios = con.execute("SELECT fecha, precio FROM precios WHERE clave=? ORDER BY fecha", (clave,)).fetchall()
        m = minimo_con_fecha(con, clave)
        print(f"{tienda} | {nombre}")
        print("   " + " -> ".join(f"{eur(pr)} ({f[8:10]}/{f[5:7]})" for f, pr in cambios[-12:]))
        if m:
            print(f"   mínimo {eur(m[0])} el {m[1][8:10]}/{m[1][5:7]}/{m[1][:4]}; historial desde el {m[2][8:10]}/{m[2][5:7]}")
    con.close()


def novedad(con, tienda, p):
    """'nuevo' | 'bajada' (por debajo de su mínimo visto) | None. Guarda el precio."""
    clave = clave_vista(tienda, p)
    ahora = datetime.now().isoformat(timespec="minutes")
    # Historial: el producto una vez; su precio la primera vez que entra en el historial.
    if con.execute("INSERT OR IGNORE INTO productos (clave, tienda, nombre, ean, url) VALUES (?,?,?,?,?)",
                   (clave, tienda, p["nombre"][:200], p.get("ean") or None, p.get("url") or None)).rowcount == 1:
        con.execute("INSERT OR REPLACE INTO precios VALUES (?,?,?)", (clave, ahora, p["precio"]))
    # INSERT OR IGNORE: seguro aunque otra categoría lea la misma tienda a la vez.
    cur = con.execute("INSERT OR IGNORE INTO vistos (clave, precio, minimo, visto, desde) VALUES (?,?,?,?,?)",
                      (clave, p["precio"], p["precio"], ahora, ahora))
    if cur.rowcount == 1:
        p["_seguido_dias"] = 0
        return "nuevo"
    previo, minimo, desde = con.execute("SELECT precio, minimo, desde FROM vistos WHERE clave=?",
                                        (clave,)).fetchone()
    p["_precio_previo"] = previo
    if previo is None or abs(previo - p["precio"]) >= 0.005:
        con.execute("INSERT OR REPLACE INTO precios VALUES (?,?,?)", (clave, ahora, p["precio"]))
    con.execute("UPDATE vistos SET precio=?, minimo=?, visto=? WHERE clave=?",
                (p["precio"], min(minimo, p["precio"]), ahora, clave))
    try:
        p["_seguido_dias"] = (datetime.now() - datetime.fromisoformat(desde)).days if desde else 0
    except ValueError:
        p["_seguido_dias"] = 0
    # Solo avisa si baja de su mínimo: evita repetir ofertas que suben y bajan.
    if p["precio"] < minimo * 0.98:
        p["_min_previo"] = minimo
        return "bajada"
    return None


def vuelve_oferta_caducada(con, p):
    """True si el producto acaba de bajar (desde un precio más alto) a un precio igual o menor que el de
    un aviso que Hermes marcó 'caducada' (era bueno, pero la oferta se acabó antes de verificarla)."""
    if not p.get("_precio_previo") or p["precio"] >= p["_precio_previo"] * 0.98:
        return False
    base = modelo_base(p["nombre"])
    m = con.execute("SELECT veredicto FROM modelos WHERE base=?", (base,)).fetchone()
    if not m or m[0] != "caducada":
        return False
    enviado = con.execute("SELECT MIN(precio) FROM lista WHERE base=?", (base,)).fetchone()[0]
    return enviado is not None and p["precio"] <= enviado


def historial_propio_ok(p, dias, bajada_min):
    """Histórico propio: el vigilante guarda el precio de cada producto en cada ronda. Sin comparador,
    solo es chollo si baja de lo más barato que se le ha visto en al menos `dias` días de seguimiento.
    Lo nuevo no pasa: el 'antes' de la tienda no prueba nada (Hermes descartaba casi todos)."""
    return (p.get("estado") == "bajada" and p.get("_seguido_dias", 0) >= dias
            and bool(p.get("_min_previo")) and p["precio"] <= p["_min_previo"] * (1 - bajada_min))


# --------------------------------------------- comparar con lo que ya guarda el vigilante (17/11/2026)
# Zeta: "quiero que mejore a la hora de comparar para sacar las mejores ofertas". Casi todo lo que salía era de
# PcComponentes (la única con histórico, por PCComTracker) y el comparador de mercado solo encontraba precio en un
# tercio de las consultas. El vigilante ya guarda cada cambio de precio de unos 57.000 productos de todas las
# tiendas (`productos` y `precios`): con eso se calcula, sin consultar a nadie, (1) el precio habitual de un
# producto en su tienda y (2) lo que cuesta ahora el mismo EAN en las otras tiendas que se leen.
HABITUAL_VENTANA_DIAS = 60     # cuánto historial se mira antes de la bajada
HABITUAL_MIN_DIAS = 7          # el precio anterior tiene que haberse visto al menos 7 días (regla del soporte)
BAJADA_MAX_DIAS = 14           # una bajada de hace más de 14 días ya es el precio habitual
MERCADO_PROPIO_HORAS = 48      # un precio de otra tienda solo cuenta si se ha visto en las últimas 48 h


def _dias(a, b):
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 86400


def habitual_propio(con, clave, ahora=None):
    """Precio habitual de un producto en su tienda, con el historial del vigilante.
    El precio actual empieza en el último cambio guardado. Antes de él, en los `HABITUAL_VENTANA_DIAS` previos,
    el habitual es la mediana ponderada por días de los precios que tuvo (un pico de un día no cuenta).
    -> {"habitual", "minimo", "dias", "bajada" (fecha), "fuente"} o None si no hay al menos
    `HABITUAL_MIN_DIAS` de historial previo, o si el precio actual ya dura más de `BAJADA_MAX_DIAS`."""
    filas = con.execute("SELECT fecha, precio FROM precios WHERE clave=? ORDER BY fecha", (clave,)).fetchall()
    if len(filas) < 2:
        return None
    ahora = ahora or datetime.now().isoformat(timespec="minutes")
    inicio_actual = filas[-1][0]
    if _dias(inicio_actual, ahora) > BAJADA_MAX_DIAS:
        return None
    desde = (datetime.fromisoformat(inicio_actual) - timedelta(days=HABITUAL_VENTANA_DIAS)).isoformat(timespec="minutes")
    tramos = []  # (precio, días que duró), solo dentro de la ventana
    for (f1, pr), (f2, _) in zip(filas[:-1], filas[1:]):
        f1 = max(f1, desde)
        if f2 > f1:
            tramos.append((pr, _dias(f1, f2)))
    total = sum(d for _, d in tramos)
    if total < HABITUAL_MIN_DIAS:
        return None
    acumulado, habitual = 0, None
    for pr, d in sorted(tramos):
        acumulado += d
        if acumulado >= total / 2:
            habitual = pr
            break
    return {"habitual": habitual, "minimo": min(pr for pr, _ in tramos), "dias": int(total),
            "bajada": inicio_actual, "fuente": "el historial del vigilante"}


def _variantes_ean(e):
    e = str(e or "").strip()
    if not re.fullmatch(r"\d{8,14}", e):
        return []
    base = e.lstrip("0")
    return sorted({e, base, base.zfill(13), base.zfill(14)})


def mercado_propio(con, p, tienda, horas=MERCADO_PROPIO_HORAS):
    """El mismo EAN en otras tiendas que lee el vigilante, con su precio de las últimas `horas`.
    Es precio de mercado sin consultar a ningún comparador. -> {"precio", "tienda", "url"} (el más barato)
    o None. No cuenta la propia tienda ni las del mismo grupo (El Corte Inglés e Hipercor), ni precios de
    menos de la mitad o más del triple (otro formato, un pack o una lectura mala)."""
    variantes = _variantes_ean(p.get("ean"))
    if not variantes:
        return None
    limite = (datetime.now() - timedelta(hours=horas)).isoformat(timespec="minutes")
    filas = con.execute(
        "SELECT pr.tienda, pr.url, v.precio FROM productos pr JOIN vistos v ON v.clave = pr.clave "
        f"WHERE pr.ean IN ({','.join('?' * len(variantes))}) AND v.visto >= ?",
        (*variantes, limite)).fetchall()
    grupo = {"elcorteingles", "hipercor"}
    propia = raiz(tienda)
    mejor = None
    for t, url, precio in filas:
        r = raiz(t)
        if r == propia or (r in grupo and propia in grupo) or not precio:
            continue
        if not (p["precio"] * 0.5 <= precio <= p["precio"] * 3):
            continue
        if not mejor or precio < mejor["precio"]:
            mejor = {"precio": precio, "tienda": t, "url": url}
    return mejor


def limpiar(con):
    limite = (datetime.now() - timedelta(days=OLVIDAR_DIAS)).isoformat(timespec="minutes")
    con.execute("DELETE FROM vistos WHERE visto < ?", (limite,))
    con.execute("DELETE FROM idealo WHERE fecha < ?", (limite,))
    hace_60 = (datetime.now() - timedelta(days=60)).isoformat(timespec="minutes")
    hace_90 = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    con.execute("DELETE FROM lista WHERE fecha < ?", (hace_60,))
    con.execute("DELETE FROM stats WHERE fecha < ?", (hace_90,))
    # Historial de precios: 400 días (por el índice de fecha, barato). Productos sin precios ni vistos: una vez al día.
    hace_400 = (datetime.now() - timedelta(days=400)).isoformat(timespec="minutes")
    con.execute("DELETE FROM precios WHERE fecha < ?", (hace_400,))
    hoy = datetime.now().strftime("%Y-%m-%d")
    if (con.execute("SELECT v FROM meta WHERE k='limpieza_productos'").fetchone() or [None])[0] != hoy:
        con.execute("DELETE FROM productos WHERE clave NOT IN (SELECT clave FROM vistos) "
                    "AND clave NOT IN (SELECT clave FROM precios)")
        con.execute("INSERT OR REPLACE INTO meta VALUES ('limpieza_productos', ?)", (hoy,))

# ------------------------------------------------- comprobaciones externas

def clave_idealo(p):
    # sorted(): un set cambia de orden entre ejecuciones y rompería la caché.
    return p["ean"] or " ".join(sorted(tokens(p["nombre"])))


def idealo_cache(con, p):
    """(True, precio) si está en caché y es reciente; (False, None) si hay que consultar."""
    fila = con.execute("SELECT precio, fecha FROM idealo WHERE consulta=?", (clave_idealo(p),)).fetchone()
    if fila and datetime.fromisoformat(fila[1]) > datetime.now() - timedelta(hours=CACHE_IDEALO_H):
        return True, fila[0]
    return False, None


def mejor_precio_idealo(con, p):
    """Precio más bajo actual en Idealo (con caché). None si no se encuentra."""
    hay, precio = idealo_cache(con, p)
    if hay:
        return precio
    mejor = idealo_red(p)
    if mejor is not False:
        con.execute("INSERT OR REPLACE INTO idealo VALUES (?,?,?)",
                    (clave_idealo(p), mejor, datetime.now().isoformat(timespec="minutes")))
    return mejor or None


IDEALO_BUSQUEDAS = ["https://www.idealo.es/resultados.html?q={q}",
                    "https://www.idealo.es/precios/MainSearchProductCategory.html?q={q}"]
_IDEALO_BUENA = [None]
_IDEALO_BLOQUEADO = [False]  # por proceso (= por ronda): Idealo bloquea con Akamai
IDEALO_REINTENTO_H = 3  # tras un 403 no se le vuelve a pedir en estas horas (en todas las rondas, no solo en la actual)


def _archivo_pausa_idealo():
    return DATOS / "idealo_pausa.txt"


def idealo_en_pausa(horas=IDEALO_REINTENTO_H):
    """True si Idealo dio 403 hace menos de `horas`. Su antibot (Akamai) cierra el paso a programas y navegadores
    (17/11/2026: 403 también con Chromium), así que no se insiste; pero tampoco se deja fuera: pasado el plazo se
    vuelve a probar una vez, y si ya responde se usa solo."""
    try:
        return time.time() - _archivo_pausa_idealo().stat().st_mtime < horas * 3600
    except OSError:
        return False


def pausar_idealo():
    _IDEALO_BLOQUEADO[0] = True
    try:
        DATOS.mkdir(parents=True, exist_ok=True)
        _archivo_pausa_idealo().write_text(datetime.now().isoformat(timespec="minutes"), encoding="utf-8")
    except OSError:
        pass


def enlace_idealo(p):
    """Búsqueda de Idealo para que la mire Zeta a mano cuando el script no puede."""
    return IDEALO_BUSQUEDAS[0].format(q=quote_plus(p.get("ean") or consulta_comparador(p)))


def idealo_red(p, precioreal=True):
    """Consulta Idealo por red (sin tocar la base de datos: se puede lanzar en paralelo).
    Devuelve el precio, None si no lo encuentra, o False si la consulta falló.
    precioreal=False: sin PrecioReal de reserva (moda y deporte: empareja por modelo, sin color ni lavado)."""
    consulta = clave_idealo(p)
    if not identificable(p):
        return None  # genérico: cualquier coincidencia sería otro producto
    reserva = precioreal_red if precioreal else (lambda p: None)
    if _IDEALO_BLOQUEADO[0] or idealo_en_pausa():
        return reserva(p)  # dio 403 hace poco: no insistir producto por producto ni ronda por ronda
    q = quote_plus(p["ean"] or consulta_comparador(p))  # 18/11/2026 (Zeta): sin EAN, marca + modelo, no el nombre entero
    rutas = sorted(IDEALO_BUSQUEDAS, key=lambda r: r != _IDEALO_BUENA[0])  # primero la que ya funcionó
    resultados, fallo = [], None
    for ruta in rutas:
        try:
            html, url = descargar(ruta.format(q=q))
            time.sleep(1.5)
        except Exception as e:
            fallo = e
            if "403" in str(e) or isinstance(e, BloqueadoPorRobots):
                pausar_idealo()
                log(f"idealo no se puede leer ({str(e)[:40]}): PrecioReal durante {IDEALO_REINTENTO_H} h y se vuelve a probar")
                break
            continue
        resultados = extraer(html, url)
        if resultados:
            _IDEALO_BUENA[0] = ruta
            break
    if not resultados and fallo is not None:
        if not _IDEALO_BLOQUEADO[0]:
            log(f"idealo {consulta}: {fallo}")
        return reserva(p)  # Idealo bloquea los programas (Akamai, 403): PrecioReal sí lo permite
    candidatos = resultados[:3] if p["ean"] else [r for r in resultados
                                                  if parecido(p["nombre"], r["nombre"]) >= 0.6]
    precios = [r["precio"] for r in candidatos]
    return min(precios) if precios else None


IDEALO_NAV_MAX = 15  # consultas por ronda con navegador (~4 s cada una)


def _archivo_pausa_idealo_nav():
    return DATOS / "idealo_nav_pausa.txt"


def precio_mismo_modelo(p, resultados):
    """Precio más bajo del mismo modelo entre los resultados de un comparador. Menos de la mitad del
    precio de la tienda es casi siempre un accesorio con el mismo nombre (PETKIT Purobot a 7,19 € = bolsas)."""
    nombre = nombre_con_marca(p)
    precios = [r["precio"] for r in resultados
               if mismo_modelo(nombre, r["nombre"]) and r["precio"] >= p["precio"] * 0.5]
    return min(precios) if precios else None


def idealo_navegador(productos, cfg=None):
    """Idealo con un Chrome VISIBLE y normal (sin perfil, sin cambiar el agente ni ocultar la automatización).
    17/11/2026: a la descarga y al navegador oculto les da 403; a una ventana normal le responde. Zeta lo
    autorizó ese día ("a partir de ahora que sea así"). Una pestaña, de una en una, solo si la descarga está
    en pausa. Si también da 403, se para 3 h como la descarga.
    -> una lista paralela a `productos`: precio, None (no está el mismo modelo) o False (no se pudo)."""
    salida = [False] * len(productos)
    try:
        if time.time() - _archivo_pausa_idealo_nav().stat().st_mtime < IDEALO_REINTENTO_H * 3600:
            return salida
    except OSError:
        pass
    canal = ((cfg or {}).get("navegador") or {}).get("canal") or None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            nav = pw.chromium.launch(headless=False, channel=canal)
            try:
                page = nav.new_context(locale="es-ES").new_page()
                for i, p in enumerate(productos):
                    if not identificable(p):
                        salida[i] = None
                        continue
                    # Por marca + modelo, no por EAN: un EAN desconocido da 404 con productos al azar.
                    url = IDEALO_BUSQUEDAS[0].format(q=quote_plus(consulta_comparador(p)))
                    try:
                        resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    except Exception as e:  # una consulta lenta no tumba las demás
                        log(f"idealo con navegador {consulta_comparador(p)[:40]}: {str(e)[:80]}")
                        continue
                    estado = resp.status if resp else 0
                    if estado == 403:
                        DATOS.mkdir(parents=True, exist_ok=True)
                        _archivo_pausa_idealo_nav().write_text(datetime.now().isoformat(timespec="minutes"),
                                                               encoding="utf-8")
                        log(f"idealo con navegador: 403, se para {IDEALO_REINTENTO_H} h")
                        break
                    if estado == 404:
                        salida[i] = None
                        continue
                    page.wait_for_timeout(2500)
                    # Idealo a veces redirige (resultado exacto -> ficha) justo después de cargar: "page is
                    # navigating" al leer (17/11/2026). Se espera a que termine y se reintenta; si no, la siguiente.
                    html = None
                    for _ in range(3):
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=15_000)
                            html = page.content()
                            break
                        except Exception:
                            page.wait_for_timeout(1500)
                    if html is not None:
                        salida[i] = precio_mismo_modelo(p, extraer(html, page.url))
            finally:
                nav.close()
    except Exception as e:  # sin navegador o se cerró: siguen PrecioReal y lo que ya se tenga
        log(f"idealo con navegador: {str(e)[:120]}")
    return salida


PRECIOREAL_BUSQUEDA = "https://precioreal.com/comparador/catalogo?q={q}"
VARIANTES = {"pro", "plus", "max", "mini", "ultra", "lite", "se", "play", "active", "neo", "fe", "air", "slim",
             "xl", "xs", "edge", "prime", "go", "kids", "nano", "classic", "sport",
             # Tallas de niño: "Nike Air Max 90 - Bebés" a 33 € se comparaba con la de adulto a 84,90 €.
             "bebe", "bebes", "beb", "nino", "nina", "ninos", "ninas", "junior", "infantil", "escolar",
             "toddler", "youth", "baby"}
GENERICAS = {"auriculares", "inalambricos", "inalámbricos", "bluetooth", "smartphone", "movil", "móvil",
             "telefono", "teléfono", "libre", "tws", "anc", "con", "de", "y", "nuevo", "oferta"}


RE_SOFTWARE = re.compile(r"\b(?:windows|win)\s*\d*\s*(?:pro|home|s)?\b|\boffice\s*\w*|\bmicrosoft\s*365\b", re.I)


def primer_numero(nombre):
    """Primera palabra con cifras, en orden: 'Xiaomi Redmi Buds 6 ... 5.4' -> '6'; 'Galaxy Book6 16"' -> 'book6'."""
    for t in re.findall(r"[a-z0-9]+", nombre.lower()):
        if re.search(r"\d", t):
            return t
    return None


def mismo_modelo(producto, resultado):
    """¿El resultado del comparador es el mismo modelo que el producto? Más estricto que parecido():
    misma variante (Buds 6 ≠ Buds 6 Pro / Play / Active) y mismos números (256GB ≠ 512GB)."""
    # 'Windows 11 Pro' / 'Office Home' no son la variante del aparato (Galaxy Book6 ≠ Book6 Pro).
    producto, resultado = (RE_SOFTWARE.sub(" ", s) for s in (producto, resultado))
    ta = tokens(producto) - COLORES
    tb = tokens(resultado) - COLORES - GENERICAS
    if not tb or (ta & VARIANTES) != (tb & VARIANTES):
        return False
    if codigos_modelo(producto) & codigos_modelo(resultado):
        return True  # mismo código exacto (OLED65C66LB, RB38C607AS9): da igual que el nombre traiga más palabras
    # El primer número es el del modelo (Buds 6); los demás suelen ser especificaciones (Bluetooth 5.4),
    # así que no basta con que estén: 'Redmi Buds 4' no es 'Redmi Buds 6 ... Bluetooth 5.4'.
    na, nb = primer_numero(producto), primer_numero(resultado)
    if na and nb and na != nb:
        return False
    if any(re.search(r"\d", t) and t not in ta for t in tb):
        return False
    return len(ta & tb) / len(tb) >= 0.8


def consulta_corta(nombre, palabras=5):
    """'Xiaomi Redmi Buds 6 Blanco TWS ANC Bluetooth 5.4...' -> 'Xiaomi Redmi Buds 6 TWS': los buscadores
    no encuentran nada con el nombre largo de la tienda."""
    limpias = [w for w in re.split(r"[\s,/|()]+", nombre) if w and w.lower() not in COLORES]
    return " ".join(limpias[:palabras])


# Código de modelo: letras y números juntos (WF-C710N, TQ65S90H, 55QNED8EB3B, RB38C607AS9).
RE_CODIGO_MODELO = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9]+(?:-[A-Z0-9]+)*\b")


def nombre_con_marca(p):
    """Las tiendas oficiales no repiten la marca ('Auriculares ... WF-C710N' en sony.es)."""
    marca = p.get("marca") or ""
    if p.get("tienda"):
        dueno = TIENDAS_MARCA.get(raiz(p["tienda"]), raiz(p["tienda"]))
        rx = regex_marcas()
        oficial = bool(rx and rx.fullmatch(dueno.lower()))  # sony.es sí; euronics.es no
        if oficial and dueno.lower() not in p["nombre"].lower() and dueno.lower() not in marca.lower():
            marca = f"{marca} {dueno}".strip()
    return f"{marca} {p['nombre']}".strip() if marca and marca.lower() not in p["nombre"].lower() else p["nombre"]


# Procesadores: I5-13420H, I7-1185G7, 7533HS, 3700U. No son el modelo del portátil: la búsqueda 'lenovo I5-13420H'
# devolvía otros Lenovo y '3700U' placas de inducción, y dos portátiles con el mismo procesador contaban como
# "mismo código exacto" (17/11/2026: 5 de las 7 comparaciones fallidas de una página de portátiles de MediaMarkt).
RE_PROCESADOR = re.compile(r"I[3579]-\d{4,5}[A-Z]{0,2}\d?|\d{4}(?:U|H|HS|HX|HK|X|X3D|G|GE|P)")


def lista_codigos(nombre):
    """Códigos de modelo del nombre, en orden, sin especificaciones (IP54, 256GB, 120Hz, USB3...)."""
    # Al menos 2 cifras: 'BOOK6' o 'PIXEL9' son la familia, no el modelo exacto.
    return [c for c in RE_CODIGO_MODELO.findall(nombre.upper()) if len(c) >= 5 and len(re.findall(r"\d", c)) >= 2
            and not RE_PROCESADOR.fullmatch(c)
            and not re.fullmatch(r"\d+(?:GB|TB|MB|W|MAH|HZ|MM|CM|L|KG|RPM|H|K|G|P|ATM|BAR)?", c)
            and not re.fullmatch(r"(?:IP|IPX|USB|HDMI|DDR|PCIE|WIFI|BT|LTE|HDR|DVB-?[TSC]|H)\d*\w?", c)
            and not re.fullmatch(r"\d+X\d+(?:X\d+)?(?:CM|MM|M)?", c)]  # medidas: 135X190, 75X45CM


def codigos_modelo(nombre):
    return set(lista_codigos(nombre))


def consulta_comparador(p):
    """Marca + código de modelo si lo hay (el modelo suele ir al final del nombre); si no, el
    principio del nombre."""
    codigos = lista_codigos(p["nombre"])
    if codigos:
        nombre = nombre_con_marca(p)
        marca = nombre.split()[0] if nombre != p["nombre"] else ""
        if not marca:  # la marca ya está en el nombre: la primera palabra conocida
            rx = regex_marcas()
            m = rx.search(p["nombre"].lower()) if rx else None
            marca = m.group(0) if m else ""
        return f"{marca} {codigos[0]}".strip()
    return consulta_corta(nombre_con_marca(p))


def identificable(p):
    """¿Se puede buscar este producto en un comparador sin confundirlo? Hace falta EAN, código de modelo
    o una marca conocida. Caso real: 'ESTOR ENROLLABLE DECOSCREEN PERLA GRIS 135X190' se emparejó con un
    colchón Pikolin (precio de mercado y características falsos)."""
    if p.get("ean") or lista_codigos(p["nombre"]):
        return True
    rx = regex_marcas()
    return bool(rx and rx.search(nombre_con_marca(p).lower()))


def precioreal_red(p):
    """Mejor precio actual en PrecioReal (su robots.txt permite la búsqueda). Igual que idealo_red:
    precio, None si no está el mismo modelo, o False si la consulta falló."""
    if not identificable(p):
        return None  # genérico: cualquier coincidencia sería otro producto
    try:
        html, url = descargar(PRECIOREAL_BUSQUEDA.format(q=quote_plus(consulta_comparador(p))))
        time.sleep(1)
    except Exception as e:
        log(f"precioreal {p['nombre'][:60]}: {e}")
        return False
    return precio_mismo_modelo(p, extraer(html, url))


# ------------------------------------------------- Chollometro (copia local)
# Su robots.txt PROHÍBE /search (antes se usaba: corregido el 7/11/2026). Sí permite /nuevos (todo lo
# que se publica, ~30 por página) y la portada /?page=N (las destacadas: lo que se ha calentado).
# Se guarda una copia local: sirve para saber si ya está publicado y, sobre todo, qué se calienta.

CM_NUEVOS = "https://www.chollometro.com/nuevos"
CM_PORTADA = "https://www.chollometro.com/"
CM_LISTADOS = {"destacados": CM_PORTADA, "subiendo": CM_PORTADA + "subiendo"}  # pestañas de Chollometro
CM_REFRESCO_MIN = 4      # como mucho una actualización cada 4 minutos
CM_FRESCA_MIN = 30       # sin actualizar en 30 min, "no publicado" pasa a "sin comprobar"
CM_RECIENTE_DIAS = 30    # un post de hace más de 30 días y caducado ya no es duplicado
CM_HOT = 300             # temperatura a partir de la que un post "se calentó"
CM_GUARDAR_DIAS = 400
CM_GUARDAR_HOT_DIAS = 800  # los de 200° o más se guardan más tiempo: son la base de la lista de vigilancia
CM_PORTADA_PAGINAS = 8   # la portada son destacados por orden de llegada: 8 páginas ≈ una semana


def hilos_chollometro(html):
    """Posts de una página de Chollometro (datos de la propia página)."""
    hilos = {}
    for o in objetos_producto(html):
        if "threadId" not in o or not o.get("publishedAt") or not o.get("title"):
            continue
        m = o.get("merchant") if isinstance(o.get("merchant"), dict) else {}
        pr = o.get("price")
        pr = float(pr) if isinstance(pr, (int, float)) else (a_numero(str(pr)) if pr else None)
        hilos[int(o["threadId"])] = {
            "id": int(o["threadId"]), "titulo": unescape(str(o["title"])), "precio": pr,
            "tienda": (m or {}).get("merchantName") or "", "host": o.get("linkHost") or "",
            "temp": float(o.get("temperature") or 0), "publicado": int(o["publishedAt"]),
            "caducado": 1 if o.get("isExpired") else 0, "extra": extra_hilo(o)}
    return list(hilos.values())


RE_CLAVE_EXTRA = re.compile(r"group|categor|link|url|voucher|coupon|code|comment|nextBest|shipping|local|type|status", re.I)


def extra_hilo(o, maximo=900):
    """Lo que sirve para analizar después (categoría/grupo, enlace, cupón, comentarios, precio anterior...) sin
    guardar el hilo entero. Los nombres de los campos de Chollometro no están fijados: se guarda lo que
    encaje con RE_CLAVE_EXTRA, en JSON compacto y como mucho `maximo` caracteres."""
    def corto(v):
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v if not isinstance(v, str) else v[:160]
        if isinstance(v, dict):
            return {k: corto(x) for k, x in list(v.items())[:6] if isinstance(x, (str, int, float, bool))}
        if isinstance(v, list):
            return [corto(x) for x in v[:5]]
        return None
    datos = {k: corto(v) for k, v in o.items() if RE_CLAVE_EXTRA.search(k) and v not in (None, "", [], {})}
    texto = json.dumps(datos, ensure_ascii=False, separators=(",", ":"))
    return texto[:maximo] if datos else None


def guardar_hilos(con, hilos):
    ahora = datetime.now().isoformat(timespec="minutes")
    con.executemany("""INSERT INTO cm (id, titulo, precio, tienda, host, temp, publicado, caducado, visto, extra)
                       VALUES (:id, :titulo, :precio, :tienda, :host, :temp, :publicado, :caducado, :visto, :extra)
                       ON CONFLICT(id) DO UPDATE SET precio=excluded.precio, temp=excluded.temp,
                       caducado=excluded.caducado, visto=excluded.visto, extra=COALESCE(excluded.extra, cm.extra)""",
                    [{"extra": None, **h, "visto": ahora} for h in hilos])
    con.executemany("INSERT OR IGNORE INTO cm_codigos VALUES (?,?)",
                    [(c, h["id"]) for h in hilos for c in codigos_modelo(h["titulo"])])
    con.commit()


def _pagina(base, n):
    return base if n == 1 else f"{base.rstrip('/')}{'/' if base.endswith('.com/') else ''}?page={n}"


def actualizar_chollometro(con, forzar=False, max_paginas=8):
    """Lee lo nuevo de /nuevos (hasta enlazar con lo ya guardado) y la primera página de la portada
    (temperaturas de lo destacado). Como mucho cada CM_REFRESCO_MIN minutos. -> True si está al día."""
    ahora = datetime.now()
    fila = con.execute("SELECT v FROM meta WHERE k='cm_intento'").fetchone()
    if not forzar and fila and datetime.fromisoformat(fila[0]) > ahora - timedelta(minutes=CM_REFRESCO_MIN):
        return chollometro_fresca(con)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('cm_intento', ?)", (ahora.isoformat(timespec="minutes"),))
    con.commit()
    try:
        for n in range(1, max_paginas + 1):
            html, _ = descargar(_pagina(CM_NUEVOS, n))
            hilos = hilos_chollometro(html)
            if not hilos:
                if n == 1:  # la primera página siempre tiene posts: si no, ha cambiado el formato
                    raise ValueError("/nuevos sin posts")
                break
            ids = [h["id"] for h in hilos]
            conocidos = con.execute(f"SELECT COUNT(*) FROM cm WHERE id IN ({','.join('?' * len(ids))})", ids).fetchone()[0]
            guardar_hilos(con, hilos)
            if conocidos:
                break  # ya enlaza con lo guardado
            time.sleep(1)
        html, _ = descargar(CM_PORTADA)
        guardar_hilos(con, hilos_chollometro(html))
        # Una página más de la portada, rotando de la 2 a la CM_PORTADA_PAGINAS: cada una se refresca cada
        # ~30 min. Sin esto la temperatura se quedaba la de /nuevos, recién publicado (17/11/2026: el 92 % de
        # los hilos de hace 3-10 días congelados; los calientes al día bajaron de 150-300 a 14-43).
        fila = con.execute("SELECT v FROM meta WHERE k='cm_portada_rot'").fetchone()
        n = int(fila[0]) % (CM_PORTADA_PAGINAS - 1) + 2 if fila else 2
        con.execute("INSERT OR REPLACE INTO meta VALUES ('cm_portada_rot', ?)", (str(n - 1),))
        time.sleep(1)
        html, _ = descargar(_pagina(CM_PORTADA, n))
        guardar_hilos(con, hilos_chollometro(html))
    except Exception as e:
        log(f"chollometro (copia local): {str(e)[:120]}")
        return chollometro_fresca(con)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('cm_ok', ?)", (ahora.isoformat(timespec="minutes"),))
    con.execute("DELETE FROM cm WHERE publicado < ? AND (temp < 200 OR publicado < ?)",
                (int((ahora - timedelta(days=CM_GUARDAR_DIAS)).timestamp()),
                 int((ahora - timedelta(days=CM_GUARDAR_HOT_DIAS)).timestamp())))
    con.commit()
    return True


# ------------------------------------------------------------------ cupones de tienda (17/11/2026)
# Fuente: los hilos "Nuevos cupones AliExpress/Miravia" y las recopilaciones de Chollometro. Muchos van en
# imagen: lo que no se pueda leer del texto se pone a mano con `cupon` (Zeta).
TIENDAS_CUPONES = {"aliexpress": "aliexpress.com", "miravia": "miravia.es"}
# Hilos que SON de cupones ("Cupones Miravia...", "Nuevos cupones...", "Recopilación cupones 11.11"), no ofertas de
# un producto que mencionan un cupón ("Volante Logitech G29 ...; cupón 12D1109").
RE_HILO_CUPONES = re.compile(r"^\W*(?:\[[^\]]*\]\s*)?(?:nuevos?\s+|todos los\s+|lista de\s+)?cup[oó]n(?:es)?\b"
                             r"|recopila\w*[^.]{0,30}cup[oó]n|c[oó]digos? (?:de )?descuento", re.I)
_E = r"(\d+(?:[.,]\d+)?)\s*(?:€|eur(?:os)?\b)"
RE_CUPON_PAR = [  # (descuento, mínimo) en el orden en que aparecen
    re.compile(r"\$\s*(\d+(?:[.,]\d+)?)\s*/\s*\$\s*(\d+(?:[.,]\d+)?)"),                    # $7 / $50
    re.compile(_E + r"\s*/\s*" + _E, re.I),                                                     # 7€ / 50€
    re.compile(r"-?" + _E + r"[^.\n€]{0,40}?(?:compras?|pedidos?)(?:\s+m[ií]nimo)?\s+(?:de|desde)\s+" + _E, re.I),
    re.compile(r"cup[oó]n\s+(?:de\s+)?" + _E + r"\s*[:\-–]?\s*pedido\s+m[ií]nimo\s+(?:de\s+)?" + _E, re.I),
]
RE_CUPON_PCT = re.compile(r"(\d{1,2})\s*%[^.\n%]{0,40}?(?:compras?|pedidos?)(?:\s+m[ií]nimo)?\s+(?:de|desde)\s+" + _E, re.I)
RE_CODIGO_CUPON = re.compile(r"\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z][A-Z0-9]{3,14}\b")


def _num(x):
    return float(x.replace(",", "."))


def cupones_de_texto(texto):
    """Cupones escritos en el texto de un hilo: [{descuento, tipo, minimo, codigo}]. Uno por línea como mucho
    por patrón; el código, si lo hay, va en la misma línea (MES03, ES11...). Lo que va en imagen no sale."""
    salida, vistos = [], set()
    for linea in re.split(r"[\n\r]+|<br\s*/?>|</(?:p|li|div)>", texto):
        plano = unescape(re.sub(r"<[^>]+>", " ", linea))
        codigo = (RE_CODIGO_CUPON.findall(plano) or [""])[0]
        for rx in RE_CUPON_PAR:
            for m in rx.finditer(plano):
                d, mn = _num(m.group(1)), _num(m.group(2))
                if 0 < d < mn:
                    clave = (d, "€", mn, codigo)
                    if clave not in vistos:
                        vistos.add(clave)
                        salida.append({"descuento": d, "tipo": "€", "minimo": mn, "codigo": codigo})
        for m in RE_CUPON_PCT.finditer(plano):
            d, mn = _num(m.group(1)), _num(m.group(2))
            clave = (d, "%", mn, codigo)
            if 0 < d < 90 and clave not in vistos:
                vistos.add(clave)
                salida.append({"descuento": d, "tipo": "%", "minimo": mn, "codigo": codigo})
    return salida


def tienda_cupones(texto):
    t = (texto or "").lower()
    return next((d for k, d in TIENDAS_CUPONES.items() if k in t), None)


def hilos_de_cupones(con, dias=10):
    """Hilos de cupones de AliExpress y Miravia en la copia de Chollometro (sin caducar): [(id, tienda, titulo)]."""
    desde = int((datetime.now() - timedelta(days=dias)).timestamp())
    filas = con.execute("SELECT id, titulo, tienda, host FROM cm WHERE publicado >= ? AND caducado = 0 "
                        "ORDER BY publicado DESC", (desde,)).fetchall()
    return [(i, tienda_cupones(f"{t} {h} {ti}"), ti) for i, ti, t, h in filas
            if RE_HILO_CUPONES.search(ti) and tienda_cupones(f"{t} {h} {ti}")]


def guardar_cupones(con, tienda, cupones, hasta=None, sello="", hilo=None):
    ahora = datetime.now().isoformat(timespec="minutes")
    con.executemany("""INSERT OR REPLACE INTO cupones (tienda, codigo, descuento, tipo, minimo, hasta, sello, hilo, alta)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    [(tienda, c.get("codigo") or "", c["descuento"], c["tipo"], c.get("minimo") or 0, hasta,
                      sello or "", hilo, ahora) for c in cupones])
    con.commit()


def cupones_activos(con, tienda, ahora=None):
    hoy = (ahora or datetime.now()).date().isoformat()
    filas = con.execute("SELECT codigo, descuento, tipo, minimo, hasta, sello FROM cupones WHERE tienda = ? "
                        "AND (hasta IS NULL OR hasta >= ?)", (tienda, hoy)).fetchall()
    return [dict(zip(("codigo", "descuento", "tipo", "minimo", "hasta", "sello"), f)) for f in filas]


def mejor_cupon(con, tienda, precio, elegible=False, ahora=None):
    """Mejor cupón para UNA unidad: (precio_final, cupón) o (precio, None). El mínimo se cumple con el propio
    precio (no se suma nada al carrito). Los cupones con sello solo valen si la tarjeta dice que el producto
    los admite (AliExpress: "Elegible con cupones" -> cupon_elegible)."""
    mejor = (precio, None)
    for c in cupones_activos(con, tienda, ahora):
        if precio < (c["minimo"] or 0) or (c["sello"] and not elegible):
            continue
        final = precio - c["descuento"] if c["tipo"] == "€" else precio * (1 - c["descuento"] / 100)
        if final < mejor[0]:
            mejor = (round(final, 2), c)
    return mejor


def texto_cupon(c):
    d = f"{c['descuento']:g} €" if c["tipo"] == "€" else f"{c['descuento']:g} %"
    s = f"cupón de {d}" + (f" desde {c['minimo']:g} €" if c.get("minimo") else "")
    return s + (f" con el código {c['codigo']}" if c.get("codigo") else "") + (f" ({c['sello']})" if c.get("sello") else "")


def comando_cupon(args):
    """cupon <tienda> <descuento>/<mínimo> [hasta DD/MM] [CÓDIGO] [sello <texto>]
    Ej.: cupon miravia 7/59 hasta 18/11   ·   cupon aliexpress 10%/60 ES11 sello MegaPROMO"""
    tienda = TIENDAS_CUPONES.get(args[0].lower().split(".")[0], args[0].lower())
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)(%?)/(\d+(?:[.,]\d+)?)", args[1])
    if not m:
        raise SystemExit("Formato: cupon <tienda> <descuento>/<mínimo> [hasta DD/MM] [CÓDIGO] [sello <texto>]")
    resto, hasta, codigo, sello = args[2:], None, "", ""
    while resto:
        x = resto.pop(0)
        if x == "hasta" and resto:
            d, mes = map(int, resto.pop(0).split("/")[:2])
            hoy = datetime.now()
            hasta = datetime(hoy.year + (1 if mes < hoy.month else 0), mes, d).date().isoformat()
        elif x == "sello":
            sello, resto = " ".join(resto), []
        else:
            codigo = x.upper()
    con = bd()
    guardar_cupones(con, tienda, [{"descuento": _num(m.group(1)), "tipo": m.group(2) and "%" or "€",
                                    "minimo": _num(m.group(3)), "codigo": codigo}], hasta, sello)
    listar_cupones(con)


def listar_cupones(con=None):
    con = con or bd()
    for t in sorted(set(TIENDAS_CUPONES.values())):
        print(f"{t}:")
        for c in sorted(cupones_activos(con, t), key=lambda c: (c["minimo"], c["descuento"])):
            print("  " + texto_cupon(c) + (f" · hasta {c['hasta']}" if c["hasta"] else ""))
    hilos = hilos_de_cupones(con)
    if hilos:
        print("Hilos de cupones en Chollometro (si van en imagen, ponlos con 'cupon'):")
        for i, t, ti in hilos[:8]:
            print(f"  {t}: {ti[:70]}  https://www.chollometro.com/ofertas/{i}")


CUPONES_CM = {"miravia.es": "https://www.chollometro.com/cupones/miravia.es",
              "aliexpress.com": "https://www.chollometro.com/cupones/es.aliexpress.com"}
CUPONES_CADA_H = 6
RE_VOUCHER = re.compile(r'"voucher":\{"idPool"')
RE_CUPON_MINIMO = re.compile(r"(?:al menos|m[ií]nim[oa]s?(?: de compra)?(?: de)?|superiores? a|a partir de|desde)\s*"
                             r"(\d+(?:[.,]\d+)?)\s*(?:€|eur)", re.I)
RE_CUPON_NUEVOS = re.compile(r"nuevos? (?:clientes|usuarios|compradores)|primer pedido|primera compra|bienvenida|"
                             # Chollometro no acepta códigos de un solo uso (18/11/2026). "1 uso por cliente" sí vale.
                             r"c[oó]digos? (?:personal(?:es)?|[uú]nicos?|individual(?:es)?)\b|"
                             r"\bun solo uso\b(?! por)|\buso [uú]nico\b(?! por)|\bv[aá]lido para un (?:solo )?uso\b(?! por)",
                             re.I)
RE_CUPON_SELLO = re.compile(r"(?:etiqueta|sello|marcados? con|identificados? con)[^«\"]{0,20}[«\"]([^»\"]{2,40})[»\"]"
                            r"|art[ií]culos del [«\"]([^»\"]{2,40})[»\"]", re.I)


def cupones_de_pagina(html):
    """Cupones de la página de cupones de una tienda en Chollometro (/cupones/<tienda>, 18/11/2026). Vienen en
    los datos de la página, con su código aunque la web lo esconda tras "Ver cupón". Por cupón: código, descuento
    (caption1: "9€" o "10%"), fecha de fin (endTime) y condiciones (termsAndConditions), de donde salen el mínimo
    y el sello («MegaPROMO», «Festival de descuentos 11.11»). Fuera: sin código (son ofertas "hasta un 70 %"),
    sin mínimo conocido, y los de clientes nuevos o primer pedido (Chollometro no admite cupones personales)."""
    t = html.replace('\\"', '"')
    marcas = [m.start() for m in RE_VOUCHER.finditer(t)]
    salida, vistos = [], set()
    for i, a in enumerate(marcas):
        trozo = t[a:marcas[i + 1] if i + 1 < len(marcas) else a + 8000]
        codigo = re.search(r'"encryptedAffiliateUrl":(?:"[^"]*"|null),"code":"([^"]{2,30})"', trozo)
        cap = re.search(r'"caption1":"(\d+(?:[.,]\d+)?)\s*(€|%)"', trozo)
        terms = re.search(r'"termsAndConditions":"((?:[^"\\]|\\.)*)"', trozo)
        fin = re.search(r'"endTime":"(\d{4}-\d{2}-\d{2})', trozo)
        titulo = re.search(r'"title":"((?:[^"\\]|\\.)*)"', trozo)
        if not codigo or not cap:
            continue
        condiciones = unescape(terms.group(1)) if terms else ""
        if RE_CUPON_NUEVOS.search(condiciones + " " + (titulo.group(1) if titulo else "")):
            continue
        minimo = RE_CUPON_MINIMO.search(condiciones)
        if not minimo:
            continue  # sin mínimo conocido no se sabe a qué precio aplica
        sello = RE_CUPON_SELLO.search(condiciones)
        c = {"codigo": codigo.group(1), "descuento": _num(cap.group(1)), "tipo": cap.group(2),
             "minimo": _num(minimo.group(1)), "hasta": fin.group(1) if fin else None,
             "sello": (sello.group(1) or sello.group(2)).strip() if sello else ""}
        if c["codigo"] not in vistos and 0 < c["descuento"] and (c["tipo"] == "%" or c["descuento"] < c["minimo"]):
            vistos.add(c["codigo"])
            salida.append(c)
    return salida


def actualizar_cupones(con, forzar=False):
    """Pone al día los cupones de Miravia y AliExpress desde sus páginas de Chollometro, como mucho cada 6 h.
    Los de la página se guardan con hilo = 0 y se sustituyen enteros (uno retirado deja de valer); los puestos
    a mano con `cupon` no se tocan. -> {tienda: número de cupones} de lo leído."""
    fila = con.execute("SELECT v FROM meta WHERE k='cupones_ultimo'").fetchone()
    if not forzar and fila and datetime.fromisoformat(fila[0]) > datetime.now() - timedelta(hours=CUPONES_CADA_H):
        return {}
    leidos = {}
    for tienda, url in CUPONES_CM.items():
        try:
            html, _ = descargar(url)
        except Exception as e:
            log(f"cupones {tienda}: {str(e)[:80]}")
            continue
        lista = cupones_de_pagina(html)
        con.execute("DELETE FROM cupones WHERE tienda=? AND hilo=0", (tienda,))
        ahora = datetime.now().isoformat(timespec="minutes")
        con.executemany("""INSERT OR REPLACE INTO cupones (tienda, codigo, descuento, tipo, minimo, hasta, sello, hilo, alta)
                           VALUES (?,?,?,?,?,?,?,0,?)""",
                        [(tienda, c["codigo"], c["descuento"], c["tipo"], c["minimo"], c["hasta"], c["sello"], ahora)
                         for c in lista])
        leidos[tienda] = len(lista)
        time.sleep(1)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('cupones_ultimo', ?)", (datetime.now().isoformat(timespec="minutes"),))
    con.commit()
    return leidos


def aplicar_cupon(con, tienda, p, cupones=None):
    """Mejor cupón de la tienda para este producto (18/11/2026). Si uno vale, el precio pasa a ser el final con
    cupón (p["precio_sin_cupon"] guarda el de la tienda y p["cupon"] el texto). Los de sello (Miravia: «Festival
    11.11», «MegaPROMO») solo si la tarjeta dice que el producto los admite (`cupon_elegible`); si no, se apunta
    el mejor como p["cupon_posible"], que solo se enseña en el aviso. -> True si cambió el precio."""
    cupones = cupones_activos(con, tienda) if cupones is None else cupones
    precio, elegible = p["precio"], bool(p.get("cupon_elegible"))
    mejor, posible = (precio, None), (precio, None)
    for c in cupones:
        if precio < (c["minimo"] or 0):
            continue
        final = round(precio - c["descuento"] if c["tipo"] == "€" else precio * (1 - c["descuento"] / 100), 2)
        if c["sello"] and not elegible:
            if final < posible[0]:
                posible = (final, c)
        elif final < mejor[0]:
            mejor = (final, c)
    if mejor[1]:
        p["precio_sin_cupon"], p["precio"], p["cupon"] = precio, mejor[0], texto_cupon(mejor[1])
        return True
    if posible[1]:
        p["cupon_posible"] = f"{texto_cupon(posible[1])}: {eur(posible[0])} si el producto entra"
    return False


def firma_cupones(con, tienda):
    """Cambia cuando cambia el juego de cupones activos de la tienda (uno nuevo, otro retirado)."""
    return "|".join(sorted(f"{c['codigo']}:{c['descuento']:g}{c['tipo']}/{c['minimo']:g}"
                           for c in cupones_activos(con, tienda)))


def chollometro_fresca(con):
    fila = con.execute("SELECT v FROM meta WHERE k='cm_ok'").fetchone()
    return bool(fila and datetime.fromisoformat(fila[0]) > datetime.now() - timedelta(minutes=CM_FRESCA_MIN))


def cargar_chollometro(con):
    """Posts guardados (para comparar en memoria) y si la copia está al día."""
    filas = con.execute("SELECT id, titulo, precio, tienda, host, temp, publicado, caducado FROM cm").fetchall()
    claves = ("id", "titulo", "precio", "tienda", "host", "temp", "publicado", "caducado")
    por_clave = {}  # clave de producto -> ids de hilos que llevan a él (cm_destino, 18/11/2026)
    for clave, hilo in con.execute("SELECT clave, hilo FROM cm_destino WHERE clave IS NOT NULL"):
        por_clave.setdefault(clave, set()).add(hilo)
    hilos = [dict(zip(claves, f)) for f in filas]
    return {"hilos": hilos, "fresca": chollometro_fresca(con), "por_clave": por_clave, "idx": _indice_copia(hilos)}


_INDICE = {"firma": None, "idx": None}


def _palabras_post(texto):
    """Palabras para el índice de la copia: sin colores ni genéricas; los números de 2 cifras o más cuentan
    (LEGO 42171, Pegasus 41)."""
    return {t for t in tokens(texto) - COLORES - GENERICAS_POST if not (t.isdigit() and len(t) < 2)}


def _indice_copia(hilos):
    """Índice palabra -> posiciones de los hilos (y "#código" -> posiciones). 18/11/2026: con 92.000 hilos,
    estado_chollometro recorría la copia entera con mismo_post y tardaba 3 s por producto. mismo_post exige 2
    palabras en común o el mismo código de modelo: con el índice solo se miran esos hilos. Se rehace solo si la
    copia cambia (mismo número de hilos y mismo último id = la misma)."""
    firma = (len(hilos), max((h["id"] for h in hilos), default=0), sum(h["id"] for h in hilos[-50:]))
    if _INDICE["firma"] == firma:
        return _INDICE["idx"]
    idx = {}
    for i, h in enumerate(hilos):
        for t in _palabras_post(h["titulo"]):
            idx.setdefault(t, []).append(i)
        for c in codigos_modelo(h["titulo"]):
            idx.setdefault("#" + c, []).append(i)
    _INDICE.update(firma=firma, idx=idx)
    return idx


def hilos_candidatos(nombre, copia, ids_extra=()):
    """Los hilos de la copia que pueden ser este producto (los que mismo_post podría aceptar) y los de ids_extra.
    Sin índice, todos."""
    idx = copia.get("idx")
    if idx is None:
        return copia["hilos"]
    cuenta = Counter()
    for t in _palabras_post(nombre):
        cuenta.update(idx.get(t, ()))
    sel = {i for i, n in cuenta.items() if n >= 2}
    for c in codigos_modelo(nombre):
        sel.update(idx.get("#" + c, ()))
    if ids_extra:
        sel.update(i for i, h in enumerate(copia["hilos"]) if h["id"] in ids_extra)
    return [copia["hilos"][i] for i in sorted(sel)]


# Palabras que comparten productos distintos: no cuentan para decir que un hilo es el mismo producto
# (17/11/2026: un Garmin Instinct 3 casaba con un Garmin Fenix 8 por "garmin mm pulgadas amoled").
GENERICAS_POST = {"mm", "cm", "pulgadas", "pulgada", "gb", "tb", "hz", "amoled", "oled", "led", "gps", "nfc", "wifi",
                  "bluetooth", "smart", "tv", "4k", "uhd", "hd", "usb", "edition", "edicion", "smartwatch", "reloj",
                  "anos", "ninos", "nino", "nina", "adultos", "unisex", "hombre", "mujer", "talla", "tallas"}


# Tipo de producto (ropa, calzado, complementos...): dos títulos con tipos distintos no son el mismo producto
# aunque compartan marca (18/11/2026, #213: camiseta Calvin Klein Jeans emparejada con "CALVIN KLEIN JEANS,
# Zapatillas" y con "Calvin Klein jeans mujer").
TIPOS_PRODUCTO = [(t, re.compile(r, re.I)) for t, r in (
    ("camiseta", r"\bcamisetas?\b|\bt-?shirts?\b|\btank\s+tops?\b|\btops?\b"), ("camisa", r"\bcamisas?\b"),
    ("polo", r"\bpolos?\b"), ("sudadera", r"\bsudaderas?\b|\bhoodies?\b"), ("jersey", r"\bjerseys?\b|\bcardigans?\b"),
    ("pantalon", r"\bpantal[oó]n(?:es)?\b|\bjoggers?\b|\bleggings?\b|\bmallas\b|\bshorts?\b|\bbermudas?\b"),
    ("vaquero", r"\bvaqueros?\b"), ("chaqueta", r"\bchaquetas?\b|\bcazadoras?\b|\babrigos?\b|\bparkas?\b|\bplum[ií]feros?\b|"
                                               r"\bjackets?\b|\bcoats?\b"),
    ("auriculares", r"\bauriculares\b|\bcascos inal|\bheadphones\b|\bearbuds\b"),
    ("gorra", r"\bgorras?\b|\bgorros?\b|\bcaps?\b"), ("champu", r"\bchamp[uú]s?\b|\bshampoo\b"),
    ("acondicionador", r"\bacondicionador\w*"), ("serum", r"\bs[eé]r[uú]m\w*"), ("crema", r"\bcremas?\b"),
    ("gel", r"\bgel\b|\bgeles\b"), ("mascarilla", r"\bmascarillas?\b"), ("desodorante", r"\bdesodorantes?\b"),
    ("vestido", r"\bvestidos?\b|\bfaldas?\b"), ("calzado", r"\bzapatill\w*|\bzapatos?\b|\bsneakers?\b|\bbotas?\b|"
                                                          r"\bbotines?\b|\bsandalias?\b|\bchanclas?\b|\bdeportivas\b"),
    ("ropa_interior", r"\bcalzoncillos?\b|\bboxers?\b|\bbragas?\b|\bsujetadores?\b|\bcalcetines\b|\bpijamas?\b"),
    ("bolso", r"\bbolsos?\b|\bmochilas?\b|\bcarteras?\b|\bri[ñn]oneras?\b|\bmaletas?\b"),
    ("reloj", r"\brelojes?\b|\bsmartwatch\w*"), ("gafas", r"\bgafas\b"), ("perfume", r"\bperfumes?\b|\bcolonias?\b|"
                                                                                  r"\beau de\b"),
    ("bano", r"\bba[ñn]adores?\b|\bbikinis?\b"))]


def tipos_producto(texto):
    return {t for t, rx in TIPOS_PRODUCTO if rx.search(texto)}


# Cotejo por marca y modelo (Zeta, 18/11/2026: "lo que tienes que comparar es por marca y por modelo, y quizá
# alguna palabra suelta"). Lo que no es ni marca ni modelo no cuenta: tipo de producto, colores, precios, tallas,
# porcentajes y palabras de anuncio.
PALABRAS_ANUNCIO = {
    "pack", "set", "kit", "lote", "oferta", "ofertas", "chollo", "envio", "gratis", "gratuito", "precio", "minimo",
    "historico", "desde", "solo", "tallas", "talla", "varias", "colores", "unidades", "uds", "ud", "unidad", "nuevo",
    "nueva", "original", "oficial", "version", "global", "edicion", "para", "con", "sin", "por", "del", "las", "los",
    "una", "uno", "mas", "muy", "cupon", "codigo", "descuento", "rebaja", "rebajas", "black", "friday", "amazon",
    "app", "web", "tienda", "hombre", "mujer", "mujeres", "hombres", "unisex", "adulto", "adultos", "tamano",
    "disponible", "disponibles", "stock", "reacondicionado", "reaco", "garantia", "espana", "europa", "eu", "envios",
    "aplicando", "aplicar", "socios", "prime", "hoy", "ahora", "nuevos", "modelo", "modelos",
    "compatible", "incluye", "incluido", "incluidos", "regalo", "aniversario", "dia", "dias", "semana", "mes", "ano",
    # descriptivas de ropa y calzado: no son el modelo
    "manga", "corta", "larga", "cuello", "redondo", "capucha", "cremallera", "deportivo", "deportiva", "deportivos",
    "deportivas", "running", "casual", "ligero", "ligera", "transpirable", "impermeable", "algodon", "poliester",
    "ajustable", "comodo", "comoda", "bajas", "altas", "caña", "cana", "baja", "alta", "ninos", "nino", "nina",
    "ninas", "kids", "junior", "bebe", "cascos", "cancelacion", "ruido", "activa", "inalambricos", "inalambrico",
    "inalambricas", "inteligente", "pantalla", "pulgadas", "bateria", "carga", "rapida", "libre", "version",
    # plataformas: dicen dónde se juega, no qué juego es
    "ps5", "ps4", "playstation", "xbox", "series", "switch", "pc", "steam", "fisico", "juego", "videojuego",
    "digital", "pal", "esp", "espana", "mi", "points", "puntos", "smartphone", "movil", "telefono"}
RE_RUIDO_TITULO = re.compile(
    r"\d+(?:[.,]\d+)?\s?(?:€|eur\b|euros?\b)|\d+\s?%|\b(?:tallas?|n[uú]meros?)\b[\s\d.,/a-zA-Z-]{0,30}?"
    r"(?:\d+(?:[.,]5)?\s*(?:a|al|-|y|/)\s*(?:la\s+)?\d+(?:[.,]5)?)|\bde la \d+ a la \d+\b|\b20[12]\d\b|"
    r"\b\d+\s*\+?\s*(?:a[ñn]os|meses)\b", re.I)
_RX_MARCAS = {}


def sin_tildes(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()


def rx_marcas():
    """Marcas para el cotejo: marcas.txt (las del filtro de marca conocida) + marcas_cotejo.txt (moda, hogar,
    supermercado...: solo para comparar títulos, no relajan ese filtro)."""
    extra = MARCAS.parent / "marcas_cotejo.txt"
    clave = (str(MARCAS),) + tuple(f.stat().st_mtime if f.exists() else 0 for f in (MARCAS, extra))
    if clave not in _RX_MARCAS:
        _RX_MARCAS.clear()
        for f in ("_rasgos", "numero_modelo"):  # sus resultados dependen de la lista de marcas
            if f in globals():
                globals()[f].cache_clear()
        marcas = [sin_tildes(m.strip().lower()) for f in (MARCAS, extra) if f.exists()
                  for m in f.read_text(encoding="utf-8").splitlines() if m.strip() and not m.lstrip().startswith("#")]
        marcas = sorted(set(marcas), key=len, reverse=True)
        _RX_MARCAS[clave] = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, marcas)) + r")(?!\w)") if marcas else None
    return _RX_MARCAS[clave]


RE_REACONDICIONADO = re.compile(r"reacondicionad\w*|\breaco\b|refurbished|\brenewed\b|\bremanufacturad\w*|"
                                r"\brenovad[oa]s?\b|estado[\s-](?:excelente|muy[\s-]bueno|bueno|correcto)|"
                                r"segunda[\s-]mano|seminuev\w*|\busad[oa]s?\b|como nuevo|\bgrado [abc]\b|"
                                r"\bopen[\s-]box\b|\bexposici[oó]n\b|/reacondicionad|/segunda-mano", re.I)
MARCA_DE_GAMA = {"poco": "xiaomi", "redmi": "xiaomi", "galaxy": "samsung", "iphone": "apple", "ipad": "apple",
                 "macbook": "apple", "airpods": "apple", "pixel": "google", "thinkpad": "lenovo", "ideapad": "lenovo",
                 "legion": "lenovo", "vivobook": "asus", "zenbook": "asus", "rog": "asus", "omen": "hp",
                 "victus": "hp", "pavilion": "hp", "aspire": "acer", "nitro": "acer", "predator": "acer",
                 "surface": "microsoft", "playstation": "sony", "dualsense": "sony", "xbox": "microsoft"}
RE_RECOPILACION = re.compile(r"recopilaci[oó]n|selecci[oó]n de|\ba\s+\d+(?:[.,]\d+)?\s?€\s*/|varios modelos|"
                             r"varias? (?:referencias|figuras|juegos|marcas)", re.I)
# Accesorio o pieza de otro producto: un accesorio y el producto no son lo mismo aunque compartan marca y modelo
# (18/11/2026: "Vidrio templado para mando DJI RC Pro" = "DJI Mini 4K", "Nevox Galaxy S25 con tapa" = "Galaxy S25").
# "Funda...", "Puntas Bosch" sí; "POCO M7 con Batería de 7000mAh" no ("con ..." es parte del producto).
RE_ACCESORIO_INICIO = re.compile(r"^(?:(?!con\b|y\b|\+)\S+\s+){0,4}?(?:fundas?|carcasas?|protector\w*|vidrio|cristal|"
                                 r"cargador\w*|cables?|soportes?|recambios?|repuestos?|bater[ií]as?|correas?|puntas|"
                                 r"brocas|filtros?|cabezales|mochila|bolsa|estuche)(?!\w)", re.I)
RE_ACCESORIO_FRASE = re.compile(r"\bcon tapa\b|vidrio templado|cristal templado|\bcompatible con\b|"
                                r"\bpara (?:el |la )?(?:samsung|iphone|ipad|macbook|galaxy|pixel|xiaomi|apple|huawei|"
                                r"dji|nintendo|switch|ps5|ps4|playstation|xbox|garmin|amazfit|huami|port[aá]til|mando|"
                                r"coche|moto|bicicleta|tv|televisor|robot|aspirador\w*|cafetera|thermomix|dyson)\b|"
                                r"\bapple pencil\b", re.I)


def familias_de(texto):
    """Todas las familias que encajan con el texto ("LEGO ... Figuras de Videojuego" es lego y videojuegos)."""
    return {n for n, rx in _RE_FAMILIAS if rx.search(texto or "")}


def es_accesorio_de(texto):
    """¿Es un accesorio o pieza de otro producto? Por las primeras palabras ("Funda...", "Puntas Bosch") o por
    frases como "para Samsung", "compatible con". "Taladro Bosch (... 32 puntas)" no lo es."""
    texto = texto or ""
    return bool(RE_ACCESORIO_INICIO.search(texto) or RE_ACCESORIO_FRASE.search(texto))


RE_JUEGO = re.compile(r"\bjuego\b|\bvideojuego\b|\bedition\b|\bedici[oó]n\b|\bf[ií]sico\b|\bc[oó]digo de descarga\b",
                      re.I)
# Colores que distinguen producto (Chollometro: "enlace distinto con el mismo color suele ser el mismo producto").
COLORES_EXTRA = {"noir", "rouge", "bleu", "blanc", "marron", "burdeos", "camel", "kaki", "caqui", "crema", "violeta",
                 "turquesa"}
RE_GENERO = {"hombre": re.compile(r"\bhombres?\b|\bmen\b|\bmasculin", re.I),
             "mujer": re.compile(r"\bmujer(?:es)?\b|\bwomen\b|\bfemenin", re.I),
             "infantil": re.compile(r"\bni[ñn][oa]s?\b|\bkids?\b|\bjunior\b|\binfantil\b|\bbeb[eé]s?\b|\bjuvenil\b", re.I)}
CORTES = {"high", "low", "mid", "lift", "platform"}  # Chuck Taylor High ≠ OX ("plataforma móvil" no)
CORTE_ES = {"altas": "high", "alta": "high", "hi": "high", "bajas": "low", "baja": "low", "ox": "low"}


def genero(texto):
    return {g for g, rx in RE_GENERO.items() if rx.search(texto or "")}


@functools.lru_cache(maxsize=400_000)
def numero_modelo(texto, de_marca=frozenset()):
    """El primer número del modelo, sin medidas ni capacidades: "Xiaomi 18 Pro 12 GB" -> "18"; "Xiaomi 15t 12/256"
    -> "15"; "Galaxy Tab A11+" -> "11". None si no hay."""
    limpio = _limpiar_titulo(texto).lower()  # sin edades ("+3") y con "15 cm" -> "15cm" (una medida, no el modelo)
    for t in re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", limpio).encode("ascii", "ignore").decode()):
        if t in de_marca or RE_UNIDAD.match(t) or not any(c.isdigit() for c in t):
            continue
        m = re.search(r"\d+", t)
        return m.group(0) if m else None
    return None
# Gamas de una marca: tienen que coincidir (Redmi 15C ≠ POCO C85; Redmi Note 17 ≠ Xiaomi 17).
GAMAS = {"redmi", "poco", "note", "galaxy", "pixel", "iphone", "ipad", "macbook", "thinkpad", "ideapad", "legion",
         "vivobook", "zenbook", "rog", "tuf", "omen", "victus", "pavilion", "envy", "spectre", "aspire", "nitro",
         "predator", "swift", "yoga", "surface"}
RE_UNIDAD = re.compile(r"^\d+(?:hz|mhz|ghz|gb|tb|mm|cm|w|mah|kg|g|l|ml|m|mp|k|p|v|a|x|uds?|pcs|in|ft|cv|bar|ah|"
                       r"min|h|s|ms|db|lm|rpm|ppm|bits?)$")
PALABRA_COMUN = 1000  # títulos de la copia en los que sale una palabra para tratarla como descriptiva


def codigo_suelto(t):
    """Código de modelo dentro del nombre (s93h, qef1, wh1000xm5), no una medida (165hz, 256gb, 47mm)."""
    return len(t) >= 3 and any(c.isdigit() for c in t) and any(c.isalpha() for c in t) and not RE_UNIDAD.match(t)


def es_palabra_comun(t):
    """¿Sale en más de PALABRA_COMUN títulos de la copia? Sin copia cargada (índice), nada es común."""
    idx = _INDICE.get("idx")
    return bool(idx) and not any(c.isdigit() for c in t) and len(idx.get(t, ())) > PALABRA_COMUN


@functools.lru_cache(maxsize=400_000)
def _rasgos(texto):
    """Lo que mismo_producto mira de un título, calculado una vez (los títulos de la copia se repiten en cada
    comparación: sin esto, 1 s por producto con 100.000 hilos)."""
    return (marca_y_modelo(texto), es_accesorio_de(texto), frozenset(genero(texto)), frozenset(familias_de(texto)),
            bool(RE_JUEGO.search(texto)), frozenset(tokens(texto)), bool(RE_RECOPILACION.search(texto)))


# Televisores Samsung (18/11/2026, Zeta): la tienda pone el código (TQ77S93HATXXC = 77" + serie S93H; prefijo de
# región TQ/QE/GQ/UE..., sufijo AT/AU/AE/XXC...) y el hilo "77" OLED S93H" o "TQ77S93H". Se comparan pulgadas y serie.
RE_TV_SAMSUNG = re.compile(r"\b(?:QE|GQ|TQ|UE|GU|TU)(\d{2,3})((?:DU|U)\d{4}[A-H]?|[A-Z]{1,3}\d{1,3}[A-Z])[A-Z0-9]*\b")
RE_SERIE_TV = re.compile(r"\b((?:DU|U)\d{4}[A-H]?|(?:QN|S|Q|LS|LST)\d{1,3}[A-H])\b")
RE_PULGADAS = re.compile(r"(?<![\d.,])(\d{2,3})\s*(?:\"|”|″|''|pulgadas\b|pulg\b|inch\b|-inch\b)", re.I)
RE_CONTEXTO_TV = re.compile(r"\b(?:tv|televisor|televisi[oó]n|smart\s?tv|qled|oled|neo\s?qled|pulgadas)\b", re.I)


def modelo_tv(texto):
    """Televisor Samsung -> (pulgadas, serie) o None. Del código si lo hay; si no, de la serie y las pulgadas del
    título (pueden ser varias: "50", 55" y 65""; sin pulgadas, conjunto vacío)."""
    t = texto or ""
    m = RE_TV_SAMSUNG.search(t.upper())
    if m:
        return frozenset({int(m.group(1))}), m.group(2)
    if "samsung" not in t.lower() or not RE_CONTEXTO_TV.search(t):
        return None
    serie = RE_SERIE_TV.search(t.upper())
    if not serie:
        return None
    return frozenset(int(x) for x in RE_PULGADAS.findall(t) if 24 <= int(x) <= 115), serie.group(1)


def misma_serie_tv(a, b):
    """QN80H = QN80H; DU7100 = DU7100K (letra de sufijo); QN80H ≠ QN80F (otro año), S93H ≠ S90H."""
    return a == b or (abs(len(a) - len(b)) == 1 and (a.startswith(b) or b.startswith(a)) and a[-1:].isdigit() != b[-1:].isdigit())


# Códigos de televisor por marca -> (pulgadas, serie). 18/11/2026 (Zeta: "para todos"):
# LG OLED65C54LA (C5), 65QNED85A6A (QNED85), 55UT73006LA (UT73); Sony K-65XR80 (XR80), KD-55X85L (X85L);
# Philips 65OLED809/12 (OLED809), 55PUS8909/12 (PUS8909); TCL 65C855 (C855); Hisense 65U8NQ (U8NQ);
# Panasonic TX-65MZ1500E (MZ1500).
CODIGOS_TV = [("samsung", RE_TV_SAMSUNG),
              ("lg", re.compile(r"\bOLED(\d{2,3})([A-Z]\d)\d?[A-Z0-9]*\b")),
              ("lg", re.compile(r"\b(\d{2,3})((?:QNED|NANO)\d{2}|U[TRQA]\d{2})[A-Z0-9]*\b")),
              ("sony", re.compile(r"\b(?:K|KD|XR|KE)-?(\d{2,3})((?:XR|X|A)\d{2}[A-Z]?)\b")),
              ("philips", re.compile(r"\b(\d{2,3})((?:OLED|PUS|PML)\d{3,4})(?:/\d{2})?\b")),
              ("tcl", re.compile(r"\b(\d{2,3})((?:C|P|T)\d{3}|QM\d[A-Z]?)[A-Z]?\b")),
              ("hisense", re.compile(r"\b(\d{2,3})((?:U|A|E)\d[A-Z]{1,2})\b")),
              ("panasonic", re.compile(r"\bTX-?(\d{2,3})([A-Z]{1,3}\d{3,4})[A-Z]?\b"))]
PULGADAS_TV = {24, 27, 32, 40, 42, 43, 48, 50, 55, 58, 60, 65, 70, 75, 77, 83, 85, 86, 97, 98, 100, 115}


def codigo_tv(texto):
    """(marca, pulgadas, serie) del código de televisor que lleve el texto, o None. Salvo Samsung (su prefijo de
    región ya es inconfundible), la marca tiene que estar escrita: "65C855" suelto no se sabe qué es."""
    t = texto or ""
    up, bajo = t.upper(), t.lower()
    for marca, rx in CODIGOS_TV:
        if marca != "samsung" and not re.search(rf"\b{marca}\b", bajo):
            continue
        for m in rx.finditer(up):
            if int(m.group(1)) in PULGADAS_TV:
                # Hisense 85E7S y 85E7S PRO son modelos distintos: la coletilla pegada al código es parte de la serie.
                coletilla = re.match(r"\s*(PRO|ULTRA|MAX)\b", up[m.end():])
                return marca, int(m.group(1)), m.group(2) + (" " + coletilla.group(1) if coletilla else "")
    return None


def serie_en_texto(serie, texto):
    """¿El texto nombra esta serie, con o sin espacio ("QNED 85", "OLED evo C5", "UT7300" para UT73)?"""
    m = re.match(r"([A-Z]+)(\d+)(.*)", serie)
    if not m:
        return False
    rx = rf"(?<![A-Z0-9]){m.group(1)}[\s-]?{m.group(2)}{re.escape(m.group(3))}[A-Z0-9]{{0,2}}(?![A-Z0-9])"
    return bool(re.search(rx, (texto or "").upper()))


def tv_igual(nombre, titulo):
    """¿Mismo televisor? True/False, o None si no se puede decidir por aquí (sigue la comparación normal).
    Con código en los dos: misma marca, serie y pulgadas. Con código en uno: el otro tiene que nombrar la serie y
    decir las pulgadas. Sin código (Samsung): serie y pulgadas del título."""
    ca, cb = codigo_tv(nombre), codigo_tv(titulo)
    if ca and cb:
        return ca[0] == cb[0] and ca[1] == cb[1] and misma_serie_tv(ca[2], cb[2])
    if ca or cb:
        (marca, pulgadas, serie), otro = (ca, titulo) if ca else (cb, nombre)
        if not re.search(rf"\b{marca}\b", (otro or "").lower()):
            return None
        tv_otro = modelo_tv(otro) if marca == "samsung" else None
        if tv_otro and not misma_serie_tv(serie, tv_otro[1]):
            return False  # Samsung: el otro nombra otra serie (S90H frente a S93H)
        if not serie_en_texto(serie, otro):
            return None
        tamanos = {int(x) for x in RE_PULGADAS.findall(otro or "") if int(x) in PULGADAS_TV}
        return (pulgadas in tamanos) if tamanos else None
    tv_a, tv_b = modelo_tv(nombre), modelo_tv(titulo)
    if tv_a and tv_b:
        if not misma_serie_tv(tv_a[1], tv_b[1]):
            return False
        if tv_a[0] and tv_b[0]:
            return bool(tv_a[0] & tv_b[0])
    return None


def _expandir_tv(texto):
    """Al código de un televisor le añade su serie y pulgadas ("TQ77S93HATXXC S93H 77"") para que el índice y el
    modelo encuentren los hilos que no ponen el código."""
    c = codigo_tv(texto)
    return f"{texto} {c[2]} {c[1]}\"" if c else texto


def _limpiar_titulo(texto):
    """El título sin ruido (precios, tallas, edades, años) y con las medidas pegadas a su número."""
    limpio = RE_RUIDO_TITULO.sub(" ", RE_SOFTWARE.sub(" ", _expandir_tv(texto or "")))
    # "256 GB" -> "256gb"; "6+128" y "6/128" (RAM + almacenamiento de un móvil) -> "6gb 128gb"
    limpio = re.sub(r"\b(\d{1,2})\s*(?:gb)?\s*[+/]\s*(\d{2,4})(?:\s*gb)?\b(?!\s*(?:€|%))", r"\1gb \2gb", limpio,
                    flags=re.I)
    limpio = re.sub(r"(\d)\s+(gb|tb|mah|hz|mhz|mm|cm|w|ml|kg)\b", r"\1\2", limpio, flags=re.I)
    limpio = re.sub(r"\b([a-z]+\d+)\+", r"\1 plus", limpio, flags=re.I)  # "Tab A11+" -> "Tab A11 plus" (no "7+ años")
    limpio = re.sub(r"(?<![a-z0-9])\+\s?\d{1,2}\b", " ", limpio, flags=re.I)  # "niños +3": edad (#225, 18/11/2026)
    return re.sub(r"\bcode in a box\b|\bciab\b", " ", limpio, flags=re.I)  # formato de venta, no el juego


def _singular(t):
    """'juguetes' -> 'juguete', 'figuras' -> 'figura' (#225: "juguete" y "juguetes" contaban como distintas)."""
    return t[:-1] if len(t) > 4 and t.endswith("s") and not any(c.isdigit() for c in t) and t not in VARIANTES else t


def marca_y_modelo(texto, marca=None):
    """-> (palabras de la marca, palabras del modelo, códigos de modelo, tipos de producto)."""
    limpio = _limpiar_titulo(texto)
    rx = rx_marcas()
    marcas = set(rx.findall(sin_tildes(limpio.lower()))) if rx else set()  # Lancome = Lancôme
    if marca:
        marcas.add(sin_tildes(marca.lower()))
    for t in tokens(limpio) & set(MARCA_DE_GAMA):  # "POCO M7" es Xiaomi aunque el título no lo diga
        marcas.add(MARCA_DE_GAMA[t])
    de_marca = set().union(*(tokens(m) for m in marcas)) if marcas else set()
    modelo = {_singular(t) for t in tokens(limpio) if t not in de_marca and t not in COLORES and t not in GENERICAS_POST
              and t not in GENERICAS and t not in PALABRAS_ANUNCIO and not tipos_producto(t)}
    return de_marca, modelo, codigos_modelo(texto or ""), tipos_producto(texto or "")


def mismo_producto(nombre, titulo, marca=None, cobertura=0.7):
    """¿El hilo de Chollometro (titulo, corto) es este producto (nombre, de la tienda)? Por marca y modelo:
    mismo tipo; mismo código si los dos lo tienen; misma marca si los dos la dicen; los números del modelo del hilo
    están en el producto (S25 ≠ S24, 256 GB ≠ 128 GB); la misma variante (Pro, Max, Ultra...); y la mayor parte del
    modelo del hilo coincide. Un hilo que solo dice marca y tipo ("Calvin Klein jeans mujer") no es un producto."""
    nombre, titulo = nombre or "", titulo or ""
    (mm_b, acc_b, gb_, fb, juego_b, tok_b, recop_b) = _rasgos(titulo)
    if recop_b:
        return False  # una recopilación ("50" a 341 € / 65" a 549 €") no es un producto
    if bool(RE_REACONDICIONADO.search(nombre)) != bool(RE_REACONDICIONADO.search(titulo)):
        return False  # un reacondicionado no es el producto nuevo (ni su precio sirve de referencia)
    tv = tv_igual(nombre, titulo)  # televisores: serie y pulgadas, no el código entero (TQ77S93HATXXC = 77" S93H)
    if tv is not None:
        return tv
    (mm_a, acc_a, ga_, fa, juego_a, tok_a, _) = _rasgos(nombre)
    ma, mo_a, ca, ta = marca_y_modelo(nombre, marca) if marca else mm_a
    mb, mo_b, cb, tb = mm_b
    if ta and tb and not (ta & tb):
        return False
    if acc_a != acc_b:
        return False  # un accesorio y el producto
    if ga_ and gb_ and not (ga_ & gb_):
        return False  # la de hombre no es la de mujer
    if fa and fb and not (fa & fb):
        return False  # auriculares frente a móvil, tablet frente a móvil, juego frente a consola
    if juego_a and not juego_b and "consolas" in fb and len(fb) == 1:
        return False
    colores = (COLORES - {"color"}) | COLORES_EXTRA
    colores_a, colores_b = tok_a & colores, tok_b & colores
    if colores_a and colores_b and not (colores_a & colores_b):
        return False  # Seductive Noir ≠ Seductive Blue; el mismo producto en otro color es otra oferta
    # Un "2X16GB" o un "DDR5" no es un código de modelo: son medidas o tipos (Lexar DDR5 = DDR4 por "2X16GB").
    ca = {c for c in ca if not RE_UNIDAD.match(c.lower()) and not re.match(r"^(?:\d+X\d+\w*|DDR\d|USB\d?|WIFI\d|PCIE\d)$", c)}
    cb = {c for c in cb if not RE_UNIDAD.match(c.lower()) and not re.match(r"^(?:\d+X\d+\w*|DDR\d|USB\d?|WIFI\d|PCIE\d)$", c)}
    if ca & cb:
        return True
    if ca and cb:
        return False  # los dos tienen código de modelo y no es el mismo
    # Sin marca en los dos lados no se empareja por título (solo por enlace): "silla de oficina ergonómica" o
    # "almohada de El Corte Inglés" casaban con cualquier hilo que dijera lo mismo (18/11/2026).
    if not (ma and mb and ma & mb):
        return False
    misma_marca = True
    corte_a = {CORTE_ES.get(t, t) for t in tok_a if CORTE_ES.get(t, t) in CORTES}
    corte_b = {CORTE_ES.get(t, t) for t in tok_b if CORTE_ES.get(t, t) in CORTES}
    if (mo_a & VARIANTES) != (mo_b & VARIANTES) or (corte_a and corte_b and corte_a != corte_b):
        return False  # Buds 6 ≠ Buds 6 Pro; Find N5 ≠ Find X8 Pro; Chuck Taylor High (altas) ≠ OX (bajas)
    na, nb = numero_modelo(nombre, frozenset(ma)), numero_modelo(titulo, frozenset(mb))
    if na and nb and na != nb:
        return False  # Xiaomi 18 Pro frente a Xiaomi 15T
    # Lo que se repite en más de 1.000 títulos de la copia describe, no identifica ("switch", "acero", "ultra").
    mo_a = {t for t in mo_a if not es_palabra_comun(t)}
    mo_b = {t for t in mo_b if not es_palabra_comun(t)}
    # Capacidades (GB, TB): si los dos las dicen, alguna tiene que coincidir (4+256 no es 6+128). Las medidas no
    # cuentan como modelo al comparar ("70l" en "Horno 70l Candy").
    for medida in (r"\d+(?:gb|tb)", r"\d+mm"):  # capacidad; tamaño de caja de un reloj (Watch 8 40 mm ≠ 44 mm)
        cap_a = {t for t in mo_a if re.fullmatch(medida, t)}
        cap_b = {t for t in mo_b if re.fullmatch(medida, t)}
        if cap_a and cap_b and not (cap_a & cap_b):
            return False
    def almacenamiento(caps):  # la capacidad mayor es el almacenamiento (6+256: 256); la menor, la RAM
        valores = [int(re.match(r"\d+", t).group()) * (1000 if t.endswith("tb") else 1) for t in caps]
        return max(valores) if valores else None
    sa = almacenamiento({t for t in mo_a if re.fullmatch(r"\d+(?:gb|tb)", t)})
    sb = almacenamiento({t for t in mo_b if re.fullmatch(r"\d+(?:gb|tb)", t)})
    if sa and sb and sa >= 32 and sb >= 32 and sa != sb:
        return False  # Redmi Note 17 6+256 no es la 6+128
    mo_a = {t for t in mo_a if not RE_UNIDAD.match(t)}
    mo_b = {t for t in mo_b if not RE_UNIDAD.match(t)}
    idx = _INDICE.get("idx") or {}
    if idx:
        # Cada uno con su propio nombre de submodelo ("Chuck Taylor ... Move Platform" frente a "... Wedge Platform"):
        # una palabra poco frecuente en cada lado que el otro no tiene = modelos distintos.
        def raras(m, otro):
            return {t for t in m - otro if not any(c.isdigit() for c in t) and len(idx.get(t, ())) < 150}
        cubre = len(mo_a & mo_b) / max(1, min(len(mo_a), len(mo_b)))
        if raras(mo_a, mo_b) and raras(mo_b, mo_a) and cubre < 0.85:
            return False  # con el 85 % del modelo en común, una palabra rara suelta es descripción o fabricante (#225)
    if idx and not any(c.isdigit() for t in mo_b for c in t) and all(len(idx.get(t, ())) > 100 for t in mo_b):
        return False  # solo palabras corrientes ("adidas Originals bermuda", "Figuras Funko Pop"): no es un producto
    if mo_b and all(any(c.isdigit() for c in t) for t in mo_b) and idx \
            and familias_de(titulo) & {"consolas", "videojuegos"}:
        # El modelo del hilo es solo un número ("Nintendo Switch 2"): el producto no puede traer un nombre propio
        # que el hilo no tiene ("Bravely Default Flying Fairy ... Switch 2" es un juego, no la consola).
        if any(len(idx.get(t, ())) < 300 for t in mo_a - mo_b if not any(c.isdigit() for c in t)):
            return False
    sueltos_a, sueltos_b = {t for t in mo_a if codigo_suelto(t)}, {t for t in mo_b if codigo_suelto(t)}
    if sueltos_a and sueltos_b and not (sueltos_a & sueltos_b):
        return False  # S93H frente a QEF1: otro modelo
    if not mo_b:
        return False  # solo marca y tipo: demasiado vago para saber qué producto es
    if (mo_a & VARIANTES) != (mo_b & VARIANTES):
        return False  # Buds 6 ≠ Buds 6 Pro
    ga, gb = tokens(nombre or "") & GAMAS, tokens(titulo or "") & GAMAS
    if gb and ga != gb or (ga - {"galaxy"} and not gb):
        return False  # otra gama de la marca (Redmi frente a POCO; Redmi Note 17 frente a Xiaomi 17)
    def forma(t):  # "s25" y "s24" tienen la misma forma (letra + 2 cifras); "256gb" y "128gb" también
        return re.sub(r"\d", "0", t)
    numeros_a = {t for t in mo_a if any(c.isdigit() for c in t)}
    faltan = {t for t in mo_b if any(c.isdigit() for c in t)} - mo_a
    if any(forma(n) == forma(x) for n in faltan for x in numeros_a - mo_b):
        return False  # el producto dice otro número del mismo tipo: otro modelo o capacidad
    comunes = mo_a & mo_b
    # El porcentaje, sobre el más corto de los dos: un título de tienda corto ("POCO M7 6GB 128GB") frente a un hilo
    # largo lleno de especificaciones ("Snapdragon 685, 144Hz, 7000mAh...") también es el mismo producto.
    sin_numero = mo_b - faltan  # si la tienda no da el número del hilo (set de LEGO), cuenta el resto
    if faltan:
        base = max(1, min(len(sin_numero), len(mo_a)))
        return len(comunes) >= 2 and len(comunes) / base >= max(cobertura, 0.75)
    base = max(1, min(len(mo_b), len(mo_a)))
    return len(comunes) >= (1 if misma_marca else 2) and len(comunes) / base >= cobertura


def coincidencia(nombre, titulo, marca=None):
    """Porcentaje de las palabras del modelo (sin marca, relleno, medidas ni palabras comunes) que comparten el
    producto y el hilo, sobre el más corto de los dos. Para enseñarlo en el aviso (Zeta, 18/11/2026)."""
    ma, mo_a, _, _ = marca_y_modelo(nombre, marca) if marca else _rasgos(nombre or "")[0]
    mo_b = _rasgos(titulo or "")[0][1]
    limpiar = lambda m: {t for t in m if not es_palabra_comun(t) and not RE_UNIDAD.match(t)}
    mo_a, mo_b = limpiar(mo_a), limpiar(mo_b)
    if not mo_a or not mo_b:
        return 0
    return round(100 * len(mo_a & mo_b) / min(len(mo_a), len(mo_b)))


def mismo_post(nombre, titulo, marca=None):
    """¿El post de Chollometro es este producto? Desde el 18/11/2026, por marca y modelo (mismo_producto)."""
    return mismo_producto(nombre, titulo, marca)


def _mismo_post_antiguo(nombre, titulo):
    """Cotejo anterior al 18/11/2026 (palabras en común sobre el título más corto). Solo de referencia."""
    if codigos_modelo(nombre) & codigos_modelo(titulo):
        return True
    ta_tipo, tb_tipo = tipos_producto(nombre), tipos_producto(titulo)
    if ta_tipo and tb_tipo and not (ta_tipo & tb_tipo):
        return False  # camiseta frente a zapatillas: otro producto aunque sea la misma marca
    ta, tb = tokens(nombre) - COLORES - GENERICAS_POST, tokens(titulo) - COLORES - GENERICAS_POST
    pa, pb = {t for t in ta if not t.isdigit()}, {t for t in tb if not t.isdigit()}
    comunes = pa & pb
    if len(comunes) < 2:
        return False
    na, nb = primer_numero(nombre), primer_numero(titulo)
    if na and nb and na != nb and nb not in ta:
        # Dos números de modelo (Buds 4 / Buds 6, Instinct 3 / Fenix 8): otro producto. Pero un número corto
        # frente a uno de 4+ cifras son cosas distintas ("7+ años" frente al set LEGO 21268): no se comparan.
        if (len(na) >= 4) == (len(nb) >= 4):
            return False
    corto = min(len(pa), len(pb))
    return len(comunes) >= min(3, corto) and len(comunes) / corto >= 0.75


def estado_chollometro(p, copia=None):
    """Estado del producto en Chollometro, por la copia local (sin buscar: /search está prohibido):
    'misma'      misma tienda y precio parecido, reciente o activo -> se descarta
    'misma:X'    misma tienda a otro precio -> decide Zeta
    'otra:X'     publicado con otra tienda a X €
    'no'         la copia está al día y no aparece (en los últimos CM_RECIENTE_DIAS días)
    '?'          la copia no está al día -> lo mira la IA
    Además deja en p["_cm_hot"] el post más caliente del mismo producto (cualquier fecha): demanda probada."""
    if copia is None:
        con = bd()
        try:
            copia = cargar_chollometro(con)
        finally:
            con.close()
    tienda = raiz(p["tienda"]) if p.get("tienda") else ""
    limite = (datetime.now() - timedelta(days=CM_RECIENTE_DIAS)).timestamp()
    misma, otros, hot = [], [], None
    # Mismo enlace de producto que el hilo (cm_destino, 18/11/2026): es el mismo producto y la misma tienda
    # aunque los títulos no se parezcan.
    por_enlace = (copia.get("por_clave") or {}).get(clave_producto(p.get("url"))) or set()
    grupo = ("elcorteingles", "hipercor")  # comparten stock: para Chollometro son la misma tienda (regla 6)
    iguales = []
    for h in hilos_candidatos(p["nombre"], copia, por_enlace):
        enlace = h["id"] in por_enlace
        if not enlace and not mismo_post(p["nombre"], h["titulo"], p.get("marca")):
            continue
        iguales.append(h)
        if h["temp"] >= CM_HOT and (hot is None or h["temp"] > hot["temp"]) \
                and (enlace or mismo_producto_hot(p["nombre"], h["titulo"], p.get("marca"))):
            hot = h
        if not h["precio"]:
            continue
        donde = (h["host"] + " " + h["tienda"]).lower().replace(" ", "")
        es_misma = enlace or (len(tienda) >= 4 and tienda in donde) or (tienda in grupo and any(g in donde for g in grupo))
        if h["publicado"] < limite:
            # Reglas de publicación de Chollometro (regla 1, 18/11/2026): con el anterior de hace más de 1 mes, se
            # mantiene el nuevo si el precio es distinto o el anterior está agotado. Solo es duplicado al mismo
            # precio y sin agotar. Un hilo viejo de otra tienda no cuenta.
            if not es_misma or h["caducado"] or abs(h["precio"] - p["precio"]) > p["precio"] * 0.02:
                continue
        elif not es_misma and h["caducado"]:
            continue  # misma oferta en otra tienda: permitida (regla 6); una ya agotada no la empeora
        if es_misma:
            if abs(h["precio"] - p["precio"]) <= p["precio"] * 0.02:
                misma.append(None)
            else:
                misma.append(h["precio"])
        else:
            otros.append(h["precio"])
    # Los hilos de este mismo producto, para el aviso (Zeta, 18/11/2026: los enlaces con su precio, no una búsqueda).
    p["_iguales"] = [{"fecha": datetime.fromtimestamp(h["publicado"]).strftime("%d/%m/%Y"), "precio": h["precio"],
                      "temp": round(h["temp"] or 0), "tienda": h["tienda"], "agotado": bool(h["caducado"]),
                      "url": enlace_hilo(h),
                      "coincide": "mismo enlace" if h["id"] in por_enlace
                      else f"coincide {coincidencia(p['nombre'], h['titulo'], p.get('marca'))} %"}
                     for h in sorted(iguales, key=lambda h: -(h["publicado"] or 0))[:4]]
    if hot:
        p["_cm_hot"] = {"temp": round(hot["temp"]), "precio": hot["precio"], "titulo": hot["titulo"][:80],
                        "dias": int((datetime.now().timestamp() - hot["publicado"]) // 86400),
                        "caducado": bool(hot["caducado"])}
    if None in misma:
        return "misma"
    if misma:
        return f"misma:{min(misma):g}"
    if otros:
        return f"otra:{min(otros):g}"
    return "no" if copia["fresca"] else "?"


def _slug(texto, largo=80):
    return re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", (texto or "").lower())
                  .encode("ascii", "ignore").decode()).strip("-")[:largo].rstrip("-")


def enlace_hilo(h):
    """Enlace completo de un hilo de la copia local. Chollometro redirige /ofertas/<lo-que-sea>-<id> al hilo."""
    return f"https://www.chollometro.com/ofertas/{_slug(h['titulo']) or 'oferta'}-{h['id']}"


CM_BUSQUEDA = "https://www.chollometro.com/search?q={q}"
CM_BUSCAR_MAX = 5        # búsquedas en Chollometro por ronda (sale desde la conexión de casa de Zeta)
CM_BUSCAR_CACHE_H = 6


def _consulta_busqueda(p):
    """Marca + código de modelo si lo hay; si no, el principio del nombre."""
    tv = codigo_tv(p.get("nombre") or p.get("n", ""))
    if tv:  # "samsung QN80H 100": con el código entero solo salían los QN80F del año anterior
        return f"{tv[0]} {tv[2]} {tv[1]}"
    return consulta_comparador({"nombre": p.get("nombre") or p.get("n", ""), "marca": p.get("marca") or "",
                                "tienda": p.get("tienda") or p.get("t")})


def _cache_busqueda(con, clave, horas):
    """(True, valor) si se buscó hace menos de `horas`; (False, None) si toca buscar."""
    fila = con.execute("SELECT v FROM meta WHERE k=?", (clave,)).fetchone()
    if fila:
        fecha, _, valor = fila[0].partition("|")
        try:
            if datetime.fromisoformat(fecha) > datetime.now() - timedelta(hours=horas):
                return True, valor
        except ValueError:
            pass
    return False, None


def buscar_en_chollometro(con, p):
    """Busca el producto en el buscador de Chollometro y guarda en la copia local lo que encuentre (con su
    temperatura y fecha). Robots.txt ya no se mira (Zeta, 17/11/2026); se hace poco: caché de 6 h por consulta,
    pausa de 2 s y como mucho CM_BUSCAR_MAX por ronda. -> número de hilos encontrados, o None si no se buscó."""
    q = _consulta_busqueda(p).strip()
    if len(q) < 3:
        return None
    hecho, valor = _cache_busqueda(con, f"cm_q:{q.lower()}", CM_BUSCAR_CACHE_H)
    if hecho:
        return int(valor or 0)
    try:
        html, _ = descargar(CM_BUSQUEDA.format(q=quote_plus(q)))
        hilos = hilos_chollometro(html)
    except Exception as e:
        log(f"chollometro búsqueda {q[:40]}: {str(e)[:80]}")
        return None
    if hilos:
        guardar_hilos(con, hilos)
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                (f"cm_q:{q.lower()}", f"{datetime.now().isoformat(timespec='minutes')}|{len(hilos)}"))
    con.commit()
    time.sleep(2)
    return len(hilos)


def hilos_del_producto(nombre, copia=None):
    """Hilos de Chollometro del mismo producto, del más reciente al más antiguo. Solo el mismo producto: mismo
    código de modelo o, sin código, mismo modelo con la regla estricta (con mismo_post salían televisores de
    Philips y Xiaomi como antecedentes de una Samsung TQ55Q7F5AU, 17/11/2026). Con código y sin `copia`, va
    por el índice cm_codigos: no recorre los 15.000 títulos."""
    cods = codigos_modelo(nombre)
    if copia is None and cods:
        con = bd()
        try:
            filas = con.execute(
                "SELECT DISTINCT cm.id, titulo, precio, tienda, host, temp, publicado, caducado FROM cm_codigos "
                f"JOIN cm ON cm.id = cm_codigos.id WHERE codigo IN ({','.join('?' * len(cods))})", sorted(cods)).fetchall()
        finally:
            con.close()
        claves = ("id", "titulo", "precio", "tienda", "host", "temp", "publicado", "caducado")
        hilos = [dict(zip(claves, f)) for f in filas]
    else:
        if copia is None:
            con = bd()
            try:
                copia = cargar_chollometro(con)
            finally:
                con.close()
        hilos = [h for h in copia["hilos"] if (cods & codigos_modelo(h["titulo"]))
                 or (not cods and mismo_modelo(nombre, h["titulo"]))]
    return sorted(hilos, key=lambda h: -(h["publicado"] or 0))


def _hilo_ficha(h):
    return {"precio": h["precio"], "temp": round(h["temp"] or 0), "caducado": bool(h["caducado"]),
            "fecha": datetime.fromtimestamp(h["publicado"]).strftime("%d/%m/%Y"), "url": enlace_hilo(h),
            "exacto": True, "titulo": h["titulo"][:80]}


def antecedentes_chollometro(nombre, copia=None, n=3):
    """Hasta `n` hilos del mismo producto, del más reciente al más antiguo. Reglas de formato de Zeta
    (17/11/2026): precio, grados, enlace completo y fecha."""
    return [_hilo_ficha(h) for h in hilos_del_producto(nombre, copia)[:n]]


def mejor_chollometro(nombre, copia=None):
    """El precio más bajo al que se ha publicado el mismo producto en Chollometro (con fecha y grados), o None.
    Son precios de chollo, no el habitual: sirve para saber si el precio de ahora es bueno."""
    con_precio = [h for h in hilos_del_producto(nombre, copia) if h["precio"]]
    return _hilo_ficha(min(con_precio, key=lambda h: (h["precio"], -h["publicado"]))) if con_precio else None


# Familias de producto: no pesa lo mismo una categoría de móviles que una de impresoras (Zeta, 17/11/2026).
# El peso de cada familia sale de la copia local de Chollometro: qué parte de sus hilos se calienta frente
# a la media. Gana la primera que encaje, así que van de lo más concreto a lo más general.
FAMILIAS = [
    ("consolas", r"\bconsolas?\b|\bps[45]\b|playstation|nintendo switch|switch 2|\bxbox\b|steam deck|dualsense|joy-?con"),
    # "juego de sartenes / de mancuernas / de cubiertos" no es un videojuego (18/11/2026: inflaba la familia).
    ("videojuegos", r"videojuegos?|\bjuegos?\b(?!\s+del?\b)|\bsteam\b|\bepic\b|\bgog\b|eshop"),
    ("moviles", r"smartphones?|m[oó]vil(?:es)?\b|iphone|\bgalaxy [sazm]\d|redmi|\bpoco [xfmc]\d|\bpixel \d|telefon[ií]a"),
    ("tablets", r"tablets?\b|\bipad\b|e-?readers?|kindle"),
    ("portatiles", r"port[aá]til(?:es)?|macbook|notebooks?|ultrabook|chromebook|\blaptops?\b|thinkpad|ideapad|"
                   r"thinkbook|vivobook|zenbook|\bexpertbook\b|\baspire\b|\bswift\b|\bpavilion\b|\belitebook\b|"
                   r"\bprobook\b|\blatitude\b|\binspiron\b|\bxps\b|\bvictus\b|\bomen\b|\blegion\b|\bloq\b"),
    ("monitores", r"monitor(?:es)?\b"),
    ("componentes", r"\bssd\b|disco duro|tarjetas? gr[aá]ficas?|\brtx ?\d|\bryzen\b|procesador(?:es)?|placas? base|"
                    r"memoria ram|\bram\b|fuentes? de alimentaci[oó]n|componentes"),
    ("impresoras", r"impresoras?|t[oó]ner|cartuchos?|esc[aá]ner"),
    ("redes", r"routers?\b|\bwifi\b|repetidor(?:es)?|\bmesh\b|switch de red"),
    ("perifericos", r"teclados?\b|rat[oó]n(?:es)?\b|webcams?|alfombrillas?|perif[eé]ricos"),
    ("televisores", r"televisor(?:es)?|smart ?tv|\btv\b|\boled\b|\bqled\b|proyector(?:es)?"),
    ("audio", r"auriculares|altavo(?:z|ces)|barras? de sonido|airpods|earbuds|soundbar"),
    ("relojes", r"smartwatch(?:es)?|reloj(?:es)?\b|pulseras? de actividad|\bgarmin\b|amazfit"),
    ("aspiracion", r"aspirador(?:a|es|as)?\b|robots? aspirador|roomba|roborock|\bdreame\b|\bconga\b"),
    ("cocina_pequena", r"freidoras?|air ?fryer|cafeteras?|batidoras?|robots? de cocina|microondas|tostador|hervidor|"
                       r"peque[nñ]o electrodom[eé]stico"),
    ("gran_electro", r"lavadoras?|lavavajillas|frigor[ií]ficos?|secadoras?\b|horno[s]?\b|congelador|placas? de inducci[oó]n|"
                     r"electrodom[eé]sticos"),
    ("calzado", r"zapatillas?|zapatos?|bota[s]?\b|botines|sneakers?|sandalias?|calzado|deportivas"),
    ("ropa", r"camisetas?|sudaderas?|chaquetas?|pantal[oó]n(?:es)?|abrigos?|vaqueros?|jers[eé]is?|jersey|chalecos?|"
             r"vestidos?|camisas?|plum[ií]feros?|parkas?|cazadoras?|\bropa\b|moda\b"),
    ("bolsos", r"mochilas?|bolsos?|maletas?|carteras?\b|ri[nñ]oneras?"),
    ("lego", r"\blego\b"),
    ("juguetes", r"juguetes?|playmobil|mu[nñ]ec[ao]s?|funko|puzzles?|hot wheels|barbie|\bnerf\b|peluches?"),
    ("perfumes", r"perfumes?|colonias?|eau de (?:toilette|parfum)|fragancias?"),
    ("higiene", r"detergente|pa[nñ]ales?|toallitas|\bgel\b|champ[uú]|pasta de dientes|papel higi[eé]nico|suavizante|"
                r"desodorante|maquinillas?|cepillos? el[eé]ctricos?|droguer[ií]a|higiene"),
    ("bebidas", r"cervezas?|\bvinos?\b|whisky|ginebra|\bron\b|refrescos?|caf[eé]\b|c[aá]psulas"),
    ("alimentacion", r"aceite|chocolate|leche\b|snacks?|at[uú]n|pizza|galletas?|turr[oó]n|jam[oó]n|alimentaci[oó]n|supermercado"),
    ("herramientas", r"taladros?|atornillador|herramientas?|sierras?\b|destornillador|multiherramienta|compresor|bricolaje"),
    ("descanso", r"colch[oó]n(?:es)?|almohadas?|edred[oó]n|n[oó]rdicos?|s[aá]banas?"),
    ("muebles", r"sof[aá]s?|sillas?\b|mesas?\b|camas?\b|estanter[ií]as?|armarios?|muebles?|escritorios?"),
    ("menaje", r"sart[eé]n(?:es)?|ollas?\b|cuchillos?|vajillas?|menaje|l[aá]mparas?|decoraci[oó]n"),
    ("bicicletas", r"bicicletas?|patinetes?"),
    ("fitness", r"pesas|mancuernas?|cintas? de correr|esterillas?|prote[ií]nas?|creatina|suplementos?"),
    ("libros", r"libros?\b|ebooks?|c[oó]mics?|manga\b|blu-?ray|vinilos?"),
]
_RE_FAMILIAS = [(n, re.compile(r, re.I)) for n, r in FAMILIAS]
PESO_MIN, PESO_MAX = 0.3, 3.0


def familia_de(texto):
    """Familia de producto de un nombre, un título o la dirección de una categoría; None si no encaja."""
    t = re.sub(r"[-_/+.]+", " ", unquote(texto or ""))
    for nombre, rx in _RE_FAMILIAS:
        if rx.search(t):
            return nombre
    return None


def pesos_demanda(con, cfg=None, horas=6):
    """{familia: peso}: tasa de hilos que llegan a `peso_grados` (200) en esa familia, dividida por la tasa
    media. 1.0 = como la media; móviles puede salir a 1,3 e impresoras a 0,5. Sale de la copia local de
    Chollometro (últimos `demanda_dias`), con caché de 6 h. None si aún no hay datos suficientes o está
    apagado (`peso_por_demanda` false): entonces todo pesa lo mismo, como antes."""
    f = (cfg or {}).get("filtros_codigo", {})
    if not f.get("peso_por_demanda", True):
        return None
    fila = con.execute("SELECT v FROM meta WHERE k='pesos_demanda'").fetchone()
    if fila:
        try:
            guardado = json.loads(fila[0])
            if datetime.fromisoformat(guardado["t"]) > datetime.now() - timedelta(hours=horas):
                return guardado["p"]
        except (ValueError, KeyError, TypeError):
            pass
    grados = f.get("peso_grados", 200)
    desde = (datetime.now() - timedelta(days=f.get("demanda_dias", 120))).timestamp()
    filas = con.execute("SELECT titulo, temp FROM cm WHERE publicado>=?", (desde,)).fetchall()
    calientes = sum(1 for _, temp in filas if (temp or 0) >= grados)
    if len(filas) < 500 or not calientes:
        return None
    media = calientes / len(filas)
    total, hot = Counter(), Counter()
    for titulo, temp in filas:
        fam = familia_de(titulo)
        if fam:
            total[fam] += 1
            hot[fam] += (temp or 0) >= grados
    pesos = {fam: round(max(PESO_MIN, min(PESO_MAX, hot[fam] / n / media)), 2)
             for fam, n in total.items() if n >= f.get("peso_min_hilos", 20)}
    con.execute("INSERT OR REPLACE INTO meta VALUES ('pesos_demanda', ?)",
                (json.dumps({"t": datetime.now().isoformat(timespec="minutes"), "p": pesos}),))
    con.commit()
    return pesos


def pesos_volumen(con, cfg=None, tienda=None, horas=6, min_calientes=10):
    """{familia: peso} por VOLUMEN: cuántos hilos de la familia llegan a 200° (los de 500° cuentan doble) en esa
    tienda, dividido por la media de las familias. Para el orden de lectura (categorías y búsquedas).
    18/11/2026: la copia de Chollometro es casi toda caliente (Destacados y Subiendo solo traen lo que sube), así
    que la tasa de pesos_demanda sale ~1 en todo y no ordenaba nada. Una familia sin hilos calientes en la
    tienda pesa el mínimo; el texto sin familia, 1.0 (peso_de). Con menos de `min_calientes` hilos en la tienda,
    los de todas las tiendas. Últimos `volumen_dias` (365). None si no hay datos o está apagado."""
    f = (cfg or {}).get("filtros_codigo", {})
    if not f.get("peso_por_demanda", True):
        return None
    clave = f"pesos_volumen:{tienda or '*'}"
    fila = con.execute("SELECT v FROM meta WHERE k=?", (clave,)).fetchone()
    if fila:
        try:
            guardado = json.loads(fila[0])
            if datetime.fromisoformat(guardado["t"]) > datetime.now() - timedelta(hours=horas):
                return guardado["p"]
        except (ValueError, KeyError, TypeError):
            pass
    grados = f.get("peso_grados", 200)
    desde = (datetime.now() - timedelta(days=f.get("volumen_dias", 365))).timestamp()
    sql, args = "SELECT titulo, temp FROM cm WHERE temp>=? AND publicado>=?", [grados, desde]
    filas = []
    if tienda:
        nombre = raiz(tienda)
        filtro = " AND (host LIKE ? OR lower(tienda) LIKE ? OR extra LIKE ?" + \
                 (" OR extra LIKE '%\"linkHost\":\"amzn.%')" if nombre == "amazon" else ")")
        filas = con.execute(sql + filtro, args + [f"%{nombre}%", f"%{nombre}%", f'%"linkHost":"%{nombre}%']).fetchall()
    if len(filas) < min_calientes:
        filas = con.execute(sql, args).fetchall()
    if len(filas) < min_calientes:
        return None
    vol = Counter()
    for titulo, temp in filas:
        fam = familia_de(titulo)
        if fam:
            vol[fam] += 2 if (temp or 0) >= 500 else 1
    if not vol:
        return None
    media = sum(vol.values()) / len(vol)
    pesos = {fam: round(max(PESO_MIN, min(PESO_MAX, vol[fam] / media)), 2) for fam, _ in FAMILIAS}
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                (clave, json.dumps({"t": datetime.now().isoformat(timespec="minutes"), "p": pesos})))
    con.commit()
    return pesos


_GRANDES = {}


def tienda_grande(con, tienda, minimo=100, dias=180):
    """¿La tienda tiene `minimo` hilos de 200°+ en Chollometro en los últimos `dias`? Chollometro no acepta errores
    de precio de tiendas pequeñas (reglas de publicación, 18/11/2026). Se calcula una vez por tienda y día."""
    clave = (tienda, minimo, datetime.now().strftime("%Y-%m-%d"))
    if clave not in _GRANDES:
        nombre = raiz(tienda)
        desde = (datetime.now() - timedelta(days=dias)).timestamp()
        n = con.execute("SELECT COUNT(*) FROM cm WHERE temp>=200 AND publicado>=? AND (host LIKE ? OR extra LIKE ?)",
                        (desde, f"%{nombre}%", f'%"linkHost":"%{nombre}%')).fetchone()[0]
        _GRANDES[clave] = n >= minimo
    return _GRANDES[clave]


def peso_de(pesos, texto):
    """Peso de demanda de un producto o de una categoría; 1.0 si no hay datos o no encaja en ninguna familia."""
    if not pesos:
        return 1.0
    return pesos.get(familia_de(texto), 1.0)


def dominio_tienda(t):
    """'gog.com/es' -> 'gog.com' ; 'store.acer.com/es-es' -> 'store.acer.com'"""
    return t.split("/")[0].lower().removeprefix("www.")


def tiendas_con_demanda(con, dias=120, minimo_hot=1):
    """Dominios con al menos `minimo_hot` posts calientes en Chollometro en `dias` días, o None si la
    copia local aún no tiene datos suficientes (entonces no se filtra nada)."""
    desde = (datetime.now() - timedelta(days=dias)).timestamp()
    total, mas_viejo = con.execute("SELECT COUNT(*), MIN(publicado) FROM cm WHERE temp>=?", (CM_HOT,)).fetchone()
    if total < 500 or not mas_viejo or mas_viejo > (datetime.now() - timedelta(days=45)).timestamp():
        return None
    cuenta = Counter()
    for (host,) in con.execute("SELECT host FROM cm WHERE temp>=? AND publicado>=?", (CM_HOT, desde)):
        cuenta[(host or "").lower().removeprefix("www.")] += 1
    return cuenta


def con_demanda(tienda, cuenta, minimo_hot=1):
    d = dominio_tienda(tienda)
    return sum(n for h, n in cuenta.items() if h == d or h.endswith("." + d) or d.endswith("." + h)) >= minimo_hot


def filtrar_por_demanda(con, cfg, tiendas):
    """Solo se leen las tiendas que han tenido chollos calientes en Chollometro (datos de la copia local).
    Caso real (7/11/2026): 117 tiendas vigiladas con 0 posts calientes en 3,5 meses (Bricoking, Prenatal,
    Forumsport, Westwing...) generaban casi todos los candidatos malos. `siempre_leer` las salva."""
    f = cfg.get("filtros_codigo", {})
    if not f.get("solo_tiendas_con_demanda"):
        return tiendas
    cuenta = tiendas_con_demanda(con, f.get("demanda_dias", 120))
    if cuenta is None:
        return tiendas
    siempre = set(f.get("siempre_leer", []))
    minimo = f.get("demanda_min_hot", 1)
    return [t for t in tiendas if t in siempre or con_demanda(t, cuenta, minimo)]


def descuento_real(p, mejor, dias_min=14):
    """Descuento sobre referencias independientes, no sobre el "antes" de la tienda (regla de Instinct:
    BUENA = al menos un 25 % sobre comparables exactos actuales o sobre un precio inicial verificado).
    El mínimo propio solo cuenta con `dias_min` días de seguimiento (regla de Zeta: histórico propio de
    14+ días; el 17/11 pasaron unos B&O con "mínimo visto en 1 días").
    -> (descuento, de qué referencia) o (None, None)."""
    refs = []
    if mejor:
        refs.append((mejor, "mercado (mismo modelo en otras tiendas)"))
    h = p.get("_hist") or {}
    if h.get("habitual"):
        refs.append((h["habitual"], f"precio habitual en la tienda ({h.get('fuente_corta', 'PCComTracker')})"))
    if p.get("estado") == "bajada" and p.get("_min_previo") and p.get("_seguido_dias", 0) >= max(1, dias_min):
        refs.append((p["_min_previo"], f"mínimo visto por el vigilante en {p['_seguido_dias']} días"))
    if (demanda_probada_ok(p) or p.get("_chollo_previo")) and p.get("precio_antes"):
        refs.append((p["precio_antes"], "precio inicial de la tienda (ya fue chollo caliente a ese precio o más)"))
    mejores = [((r - p["precio"]) / r, etiqueta) for r, etiqueta in refs if r and r > 0]
    return max(mejores) if mejores else (None, None)


RE_PALABRA_RELLENO = re.compile(r"^(?:textil|ropa|calzado|zapatos|complementos|accesorios|mujer|hombre|unisex|"
                                r"ni[ñn][oa]s?|adultos?|junior|de|para|con|sin|y|el|la|los|las|en)$", re.I)


def lineas_iguales(p):
    """Los hilos de Chollometro de este mismo producto (estado_chollometro los deja en p["_iguales"]), uno por línea
    con fecha, precio, grados, tienda y enlace. Sin hilos, lo dice."""
    iguales = p.get("_iguales")
    if not iguales:
        return "Hilos en Chollometro: ninguno de este producto en la copia"
    def linea(h):
        precio = f" · {h['precio']:g} €" if h.get("precio") else ""
        extra = (" · agotado" if h.get("agotado") else "") + (f" · {h['coincide']}" if h.get("coincide") else "")
        return f"Hilo: {h['fecha']}{precio} · {h['temp']}° · {h['tienda']}{extra}\n{h['url']}"
    return "\n".join(linea(h) for h in iguales)


def enlace_busqueda_chollometro(nombre, marca=None):
    """Enlace de búsqueda para que Zeta la haga A MANO (su robots.txt prohíbe /search a los programas:
    el vigilante y Hermes nunca la abren). 18/11/2026 (#213): Spartoo empieza sus nombres por "textil Mujer
    Camisetas sin mangas..." y la búsqueda no encontraba nada. Ahora: marca y modelo, sin palabras de relleno."""
    palabras = [w for w in re.split(r"[\s,/|()]+", nombre or "")
                if w and w.lower() not in COLORES and not RE_PALABRA_RELLENO.match(w)]
    if marca:
        resto = [w for w in palabras if w.lower() not in marca.lower().split() and not tipos_producto(w)]
        modelo = [w for w in resto if w[0].isupper() or any(c.isdigit() for c in w)] or resto  # el modelo va en mayúsculas
        q = " ".join([marca] + modelo[:3])
    else:
        q = " ".join(palabras[:5])
    return "https://www.chollometro.com/search?q=" + quote_plus(q.strip())


def busquedas_chollometro(c):
    """Las búsquedas de duplicados que pide el manual (referencia, nombre, marca + modelo), como enlaces
    para que Zeta las haga a mano. Un índice propio no las sustituye."""
    enlaces = {}
    if c.get("e"):
        enlaces["referencia"] = "https://www.chollometro.com/search?q=" + quote_plus(str(c["e"]))
    codigos = lista_codigos(c["n"])
    if codigos:
        enlaces["modelo"] = "https://www.chollometro.com/search?q=" + quote_plus(codigos[0])
    enlaces["nombre"] = enlace_busqueda_chollometro(c["n"])
    return enlaces


HOT_PRECIO_MIN = 0.6  # más barato que el 60 % del hilo caliente = casi seguro otro producto (o un pack)
CHOLLO_PREVIO_GRADOS = 200
CHOLLO_PREVIO_DIAS = 180
PCCT_MAX_RONDA = 10  # fichas de PCComTracker que se piden por ronda para productos de otras tiendas


def hilos_por_enlace(url):
    """Hilos de Chollometro que llevan a este mismo producto (misma clave_producto en cm_destino)."""
    clave = clave_producto(url)
    if not clave:
        return []
    con = bd()
    try:
        filas = con.execute("SELECT cm.id, titulo, precio, tienda, host, temp, publicado, caducado FROM cm_destino d "
                            "JOIN cm ON cm.id = d.hilo WHERE d.clave=?", (clave,)).fetchall()
    finally:
        con.close()
    claves = ("id", "titulo", "precio", "tienda", "host", "temp", "publicado", "caducado")
    return [dict(zip(claves, f)) for f in filas]


def ya_salio_mas_barato(p, grados=CHOLLO_PREVIO_GRADOS, dias=CHOLLO_PREVIO_DIAS):
    """El mismo producto (mismo enlace) llegó a `grados` en los últimos `dias`, y siempre más barato que ahora:
    por encima del precio más alto con que triunfó, la comunidad no lo va a calentar (18/11/2026, propuesta
    aprobada por Zeta). -> el precio más alto con que triunfó, o None."""
    desde = (datetime.now() - timedelta(days=dias)).timestamp()
    precios = [h["precio"] for h in hilos_por_enlace(p.get("url")) if h["precio"] and (h["temp"] or 0) >= grados
               and (h["publicado"] or 0) >= desde]
    techo = max(precios, default=None)
    # Zeta (18/11/2026): hasta un 10 % más caro que un chollo que se calentó todavía puede ir bien; más, no.
    return techo if techo and p["precio"] > techo * 1.10 else None


RE_CAPACIDAD = re.compile(r"\b\d+\s?(?:gb|tb)\b", re.I)
RE_NO_ES_EL_EQUIPO = re.compile(r"\bbuds\b|auricular|earbuds|airpods|\bwatch\b|\bband\b|funda|carcasa|cargador|cable|"
                                r"protector|soporte|\bbase\b|teclado|rat[oó]n|mochila|malet[ií]n|bolsa|stylus|l[aá]piz",
                                re.I)


def configuracion_ambigua(nombre):
    """Portátil, sobremesa, móvil o tablet cuyo nombre no dice la RAM ni la capacidad: el mismo nombre se vende en
    configuraciones con precios muy distintos, así que un precio de comparador no es del mismo producto. Los
    auriculares, relojes y accesorios de esas marcas (Redmi Buds, fundas...) no tienen configuraciones."""
    return (familia_de(nombre) in ("portatiles", "moviles", "tablets", "sobremesa") and not RE_CAPACIDAD.search(nombre)
            and not RE_NO_ES_EL_EQUIPO.search(nombre))


def chollo_previo(p, grados=CHOLLO_PREVIO_GRADOS, dias=CHOLLO_PREVIO_DIAS):
    """El mismo modelo (mismo código, por el índice cm_codigos) se publicó en Chollometro en los últimos `dias` con
    `grados` o más, y ahora está igual o más barato (sin bajar del 60 %: sería otro producto). La comunidad validó
    ese precio: es contraste real aunque no haya comparador. Sin código de modelo no se intenta. -> hilo o None.
    Desde el 18/11/2026 también por el enlace del producto (cm_destino), con o sin código de modelo."""
    hilos = hilos_por_enlace(p.get("url"))
    if codigos_modelo(p["nombre"]):
        ya = {h["id"] for h in hilos}
        hilos += [h for h in hilos_del_producto(p["nombre"]) if h["id"] not in ya]
    desde = (datetime.now() - timedelta(days=dias)).timestamp()
    buenos = [h for h in hilos if h["precio"] and (h["temp"] or 0) >= grados
              and (h["publicado"] or 0) >= desde and h["precio"] * HOT_PRECIO_MIN <= p["precio"] <= h["precio"] * 1.0]
    return max(buenos, key=lambda h: h["temp"]) if buenos else None


def guardar_pcct(con, productos):
    """Apunta el código de modelo y la ficha de PCComTracker de cada producto que la trae."""
    filas = [(c, p["historico_url"], p["nombre"][:150]) for p in productos
             if "pccomtracker" in (p.get("historico_url") or "") for c in codigos_modelo(p["nombre"])]
    if filas:
        con.executemany("INSERT OR IGNORE INTO pcct VALUES (?,?,?)", filas)


PCCT_BUSQUEDA = "https://pccomtracker.com/api/search?q={q}&limit=8"


def slug_pcct(nombre):
    """La misma fórmula que su web (función de su JavaScript, 17/11/2026): sin tildes, se quita todo lo que no sea
    letra, número, espacio o guion (no se cambia por guion), espacios -> guion, guiones juntos, 100 caracteres.
    Con otra fórmula la ficha redirige a una página vacía."""
    t = unicodedata.normalize("NFD", nombre.lower())
    t = re.sub(r"[̀-ͯ]", "", t)
    t = re.sub(r"[^a-z0-9\s-]", "", t)
    t = re.sub(r"-+", "-", re.sub(r"\s+", "-", t)).strip("-")
    return t[:100]
# PCComTracker solo sigue el catálogo de PcComponentes (tecnología y algo de hogar y electrodomésticos).
PCCT_FUERA = {"moda", "deporte", "supermercado", "salud_belleza", "mascotas", "videojuegos", "viajes", "juguetes_bebe"}
PCCT_CACHE_H = 24


def pcct_de(con, p, buscar=True):
    """Ficha de PCComTracker del mismo modelo (mismo código y misma regla estricta) o None. Primero el índice
    local; si no está y `buscar`, su buscador (/api/search: robots.txt ya no se mira, Zeta 17/11/2026), con
    caché de 24 h por consulta. Lo encontrado se guarda en el índice."""
    cods = sorted(codigos_modelo(p["nombre"]))
    if not cods:
        return None
    for url, nombre in con.execute(f"SELECT url, nombre FROM pcct WHERE codigo IN ({','.join('?' * len(cods))})", cods):
        if mismo_producto_hot(p["nombre"], nombre):
            return url
    if not buscar:
        return None
    q = _consulta_busqueda(p)
    hecho, valor = _cache_busqueda(con, f"pcct_q:{q.lower()}", PCCT_CACHE_H)
    if hecho:
        return valor or None
    url = ""
    try:
        txt, _ = descargar(PCCT_BUSQUEDA.format(q=quote_plus(q)))
        for r in (json.loads(txt).get("products") or []):
            if r.get("id") and r.get("name") and mismo_producto_hot(p["nombre"], r["name"]):
                url = f"https://pccomtracker.com/producto/{slug_pcct(r['name'])}-{r['id']}"
                guardar_pcct(con, [{"nombre": r["name"], "historico_url": url}])
                break
    except Exception as e:
        log(f"pccomtracker búsqueda {q[:40]}: {str(e)[:80]}")
        return None
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                (f"pcct_q:{q.lower()}", f"{datetime.now().isoformat(timespec='minutes')}|{url}"))
    time.sleep(1)
    return url or None


def mismo_producto_hot(nombre, titulo, marca=None):
    """Para "ya fue chollo" no basta con parecerse (mismo_post): ese hilo deja pasar un producto sin
    comparador. Casos reales (15-16/11/2026): monitor Philips a 41 € emparejado con un hilo de 599 €,
    AirPods Max a 130 € con otro de 378,99 €, zapatillas Puma de niña con otro modelo.
    Hace falta el mismo código de modelo o el mismo modelo con la regla estricta del comparador.
    18/11/2026 (#213): un título vago, de 3 palabras o menos sin código ("Calvin Klein jeans mujer", una
    recopilación), no identifica un producto: no sirve de demanda probada. Ni un hilo de otro tipo de producto.
    Desde el 18/11/2026, por marca y modelo (mismo_producto) y más exigente: el 80 % del modelo del hilo."""
    return mismo_producto(nombre, titulo, marca, cobertura=0.8)


# Accesorios y recambios "para" otro producto: heredan la familia del producto principal (una carcasa de
# Acer 5750 pesaba como un portátil) pero casi nadie los busca. Solo los tipos que en la copia de
# Chollometro (17/11, 14.949 hilos) llegan poco a 300°: fundas 7 %, bolsas 10 %, cables y filtros 0 %,
# recambios 0 %, frente al 25 % de media. Mochilas, cargadores, adaptadores y teclados "para" algo
# se calientan como la media: no se cortan.
RE_ACCESORIO = re.compile(
    r"^(?:\S+\s+){0,3}?(?:carcasa|funda|bolsa|cable|repuesto|recambio|bisagra|t[oó]ner|cartucho|filtro|"
    r"kit\s+de\s+montaje)(?!\w)", re.I)


def es_accesorio(nombre):
    """Accesorio o recambio PARA otro producto ("Funda ... para Portátil 15"", "Adaptador de corriente
    para HP"). Una mochila o un cargador a secas no cuentan: sin "para" puede ser el producto buscado."""
    n = nombre or ""
    return bool(RE_ACCESORIO.search(n) and re.search(r"(?<!\w)para(?!\w)", n, re.I))


def demanda_probada_ok(p):
    """El mismo producto ya fue chollo caliente (≥ CM_HOT°) y ahora está igual o más barato: el precio y
    la demanda están probados por la comunidad. Solo si ese post ya no es un duplicado (caducado), y sin
    estar tan por debajo que sea otro producto."""
    h = p.get("_cm_hot")
    return bool(h and h.get("precio") and h["caducado"]
                and h["precio"] * HOT_PRECIO_MIN <= p["precio"] <= h["precio"] * 1.02)


# Tiendas leídas por un comparador: "Sin precio oficial de PCComponentes" = la tienda no lo vende y el
# número de la tarjeta es otro (secadora AEG a 9 €, DJI Power 1000 a 49 €, localizador a 1 €).
RE_SIN_PRECIO_OFICIAL = re.compile(r"sin precio oficial", re.I)
# Tallas de niño en ropa y calzado (Spartoo: "Zapatos Niña ... V Inf", "AC+ PS"). Desde el 17/11/2026 también
# júnior/GS/teen: reglas de formato de Zeta ("ropa y calzado infantil/junior quedan fuera de las próximas tandas").
RE_INFANTIL = re.compile(r"(?<!\w)(?:niñ[oa]s?|nin[oa]s?|beb[eé]s?|infantil|inf|ps|td|toddler|baby|kids?|"
                         r"preescolar|pre-?school|j[uú]nior|jr|gs|youth|teen)(?!\w)", re.I)
PRECIO_SOSPECHOSO = 0.4  # por debajo del 40 % del mercado o del mínimo histórico: error de lectura


def precio_sospechoso(p, mejor, factor=PRECIO_SOSPECHOSO):
    """Tu doc de Instinct: un precio muy por debajo de la referencia se relee antes de avisar. Aquí no
    se puede releer (PcComponentes tiene antibot), así que no se envía: es casi siempre un fallo de
    lectura, no un error de precio de la tienda."""
    refs = [r for r in (mejor, (p.get("_hist") or {}).get("minimo")) if r]
    return bool(refs) and p["precio"] < min(refs) * factor


def repetido_reciente(p, recientes):
    """Mismo aviso con otro color o variante (3 mandos DualSense a 54,99 € en Amazon el 16/11, 2 Puma a
    30 € en Spartoo): misma tienda, precio a ±2 % y nombre parecido. recientes = [(tienda, nombre, precio)]."""
    num, cods = primer_numero(p["nombre"]), codigos_modelo(p["nombre"])
    for t, n, precio in recientes:
        if t != p["tienda"] or not precio or abs(precio - p["precio"]) > p["precio"] * 0.02:
            continue
        otro_num, otros_cods = primer_numero(n), codigos_modelo(n)
        if (num and otro_num and num != otro_num) or (cods and otros_cods and not cods & otros_cods):
            continue  # otro modelo (JBL Charge 4 ≠ Charge 5), aunque el nombre se parezca
        if max(parecido(p["nombre"], n), parecido(n, p["nombre"])) >= 0.6:
            return True
    return False


def avisos_recientes(con, horas=24):
    desde = (datetime.now() - timedelta(hours=horas)).isoformat(timespec="minutes")
    salida = []
    for (datos,) in con.execute("SELECT datos FROM lista WHERE fecha>=?", (desde,)):
        try:
            d = json.loads(datos)
            salida.append((d.get("t"), d.get("n") or "", d.get("p")))
        except (ValueError, TypeError):
            pass
    return salida


def precargar_chollometro(dias_nuevos=3, paginas_portada=60, pausa=1.5, desde=None, listado="destacados", inicio=1,
                          hasta_hora=None):
    """Primera carga: /nuevos de los últimos días (duplicados) y la portada hacia atrás (lo que se
    calentó, ~1 día por página). Educada: una página cada `pausa` segundos.
    desde (fecha 'AAAA-MM-DD'): la portada para al llegar ahí. También para si una página no va más atrás
    que la anterior (pasado el final, Chollometro puede devolver otra vez lo de hoy) o repite la página 1.
    listado: "destacados" (portada) o "subiendo" (CM_LISTADOS). La pausa varía un ±20 % entre páginas.
    inicio: página del listado por la que empezar (para repartir una carga larga en varias noches).
    hasta_hora ("HH:MM"): para al llegar a esa hora (la de hoy o, si ya pasó, la de mañana) y dice por qué
    página seguir."""
    fin = None
    if hasta_hora:
        hh, mm = map(int, hasta_hora.split(":"))
        fin = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
        if fin <= datetime.now():
            fin += timedelta(days=1)
    DATOS.mkdir(parents=True, exist_ok=True)
    cerrojo = DATOS / "precarga.lock"
    cerrojo.write_text(str(os.getpid()), encoding="utf-8")  # las rondas no arrancan mientras exista
    _cookie_cm.update(activa=True, cabecera=None)
    if not cookie_cm():
        print("Sin sesión de Chollometro (data/cm_sesion.json): los listados acaban en la página 334. "
              "Para pasar de ahí: vigilante.py sesion_cm")
    try:
        _precargar(dias_nuevos, paginas_portada, pausa, desde, listado, inicio, fin, hasta_hora)
    finally:
        _cookie_cm["activa"] = False
        cerrojo.unlink(missing_ok=True)


class _SinRedireccion(HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


# Servicios de redirección y afiliados entre Chollometro y la tienda: se siguen sus cabeceras Location (o el
# destino que llevan en un parámetro) sin descargar nada. En cuanto el enlace sale de ellos, es la tienda: no se pide.
RE_REDIRECTOR = re.compile(
    r"(^|\.)(chollometro\.com|amzn\.(eu|to)|(s|click|a)\.aliexpress\.com|awin1\.com|tradedoubler\.com|prf\.hn|"
    r"tradetracker\.net|skimresources\.com|bit\.ly|tidd\.ly|linksynergy\.com|pntra\.com|anrdoezrs\.net|"
    r"dpbolvw\.net|jdoqocy\.com|kqzyfj\.com|tkqlhce\.com|webgains\.com|effiliation\.com|admitad\.com|"
    r"rover\.ebay\.[a-z.]+|ebay\.us)$", re.I)
PARAMS_DESTINO = ("ued", "url", "murl", "dest", "destination", "dl", "u", "r")


def destino_hilo(hilo, saltos=6):
    """Adónde lleva "Ir al chollo" de un hilo, leyendo solo cabeceras Location (no descarga la tienda).
    Chollometro -> path.chollometro.com -> amazon.es/dp/ASIN; amzn.eu y s.click.aliexpress dan un salto más; los
    afiliados que llevan el destino en un parámetro (awin "ued", "url"...) se saltan sin pedirlos. -> URL."""
    url = f"https://www.chollometro.com/visit/threadmain/{hilo}"
    op = build_opener(_SinRedireccion)
    for n in range(saltos):
        u = urlparse(url)
        if not RE_REDIRECTOR.search(u.hostname or ""):
            return url
        q = parse_qs(u.query)
        siguiente = next((q[k][0] for k in PARAMS_DESTINO if q.get(k, [""])[0].startswith("http")), None)
        if siguiente and n:
            url = siguiente
            continue
        try:
            op.open(Request(url, headers={"User-Agent": UA}), timeout=15).close()
            return url  # sin redirección
        except HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308):
                raise
            url = urljoin(url, e.headers.get("Location") or "")
    return url


def tiendas_que_leemos(cfg):
    """Raíces de las tiendas de tiendas.json (las de las categorías y las de Chrome normal)."""
    t = set((cfg.get("navegador_normal") or {}).get("tiendas") or [])
    for cat in (cfg.get("categorias") or {}).values():
        t.update(cat.get("tiendas") or [])
        t.update(cat.get("navegador") or [])
    return {raiz(x) for x in t if raiz(x)}


def destinos_cm(max_hilos=1000, pausa=3.0, grados=200):
    """`destinos [N]`: mira adónde lleva el botón de hasta N hilos de 200° o más aún no mirados (primero los de
    las tiendas que lee el vigilante, y de esos los más calientes) y lo guarda en cm_destino con su clave de
    producto. Para sola con un 403/429 o sin red (no reintenta)."""
    con = bd()
    nuestras = tiendas_que_leemos(cargar_config())
    filas = con.execute("""SELECT id, temp, host, tienda, extra FROM cm WHERE temp>=?
                           AND id NOT IN (SELECT hilo FROM cm_destino)""", (grados,)).fetchall()

    def de_las_nuestras(f):
        host = ((json.loads(f[4]).get("linkHost") if f[4] else None) or f[2] or f[3] or "").lower()
        return any(r in host for r in nuestras) or (("amzn." in host) and "amazon" in nuestras)
    filas.sort(key=lambda f: (not de_las_nuestras(f), -(f[1] or 0)))
    hechos = con_clave = 0
    for hilo, *_ in filas[:max_hilos]:
        try:
            url = destino_hilo(hilo)
        except Exception as e:
            log(f"destinos: hilo {hilo}: {e}")
            if getattr(e, "code", None) in (403, 429) or not getattr(e, "code", None):
                print(f"Paro en el hilo {hilo}: {str(e)[:80]}")
                break
            url = None
        clave = clave_producto(url)
        fila = (hilo, url_canonica(url) if clave else url, clave, datetime.now().isoformat(timespec="minutes"))
        for intento in range(20):  # una ronda escribiendo a la vez bloquea la base: se espera (18/11/2026)
            try:
                con.execute("INSERT OR REPLACE INTO cm_destino VALUES (?,?,?,?)", fila)
                con.commit()
                break
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or intento == 19:
                    raise
                time.sleep(15)
        hechos += 1
        con_clave += bool(clave)
        if hechos % 100 == 0:
            print(f"  {hechos} hilos, {con_clave} con producto")
        time.sleep(pausa * random.uniform(0.8, 1.2))
    total, productos = con.execute("SELECT COUNT(*), COUNT(DISTINCT clave) FROM cm_destino").fetchone()
    amazon = con.execute("SELECT COUNT(*) FROM cm_ref WHERE clave LIKE 'amazon:%'").fetchone()[0]
    falta = con.execute("SELECT COUNT(*) FROM cm WHERE temp>=? AND id NOT IN (SELECT hilo FROM cm_destino)",
                        (grados,)).fetchone()[0]
    con.close()
    print(f"{hechos} hilos mirados, {con_clave} con producto. cm_destino: {total} hilos, {productos} productos "
          f"({amazon} de Amazon). Faltan {falta} hilos de {grados}°+.")
    return hechos, con_clave


def sesion_cm():
    """`sesion_cm`: guarda las cookies de sesión de Chollometro del perfil del navegador del vigilante en
    data/cm_sesion.json para la precarga. Antes hay que iniciar sesión en ese perfil (`login chollometro.com`)."""
    cfg = cargar_config()
    with Navegador({**cfg, "navegador": {**cfg.get("navegador", {}), "visible": True}}) as nav:
        cks = [c for c in nav._ctx.cookies("https://www.chollometro.com") if RE_COOKIE_SESION_CM.match(c["name"])]
    DATOS.mkdir(parents=True, exist_ok=True)
    (DATOS / SESION_CM).write_text(json.dumps(cks), encoding="utf-8")
    print(f"{len(cks)} cookies de sesión guardadas" + ("" if any(c["name"].startswith("remember_") for c in cks)
                                                       else " (sin 'remember_': ¿has iniciado sesión?)"))
    return len(cks)


SEGUIR = "precarga_seguir.json"  # cómo seguir una carga que se paró (lo usa el botón "Seguir" de Telegram)
TECLADO_SEGUIR = json.dumps({"inline_keyboard": [[{"text": "▶️ Seguir", "callback_data": "vg:seguir:1"}]]})


def aviso_precarga(texto, seguir=False):
    """Precarga: el progreso también por Telegram (Zeta, 18/11/2026: "avísame cada 50 páginas"). Sin token o sin
    red, solo en pantalla. seguir: con el botón "Seguir" (Zeta, 18/11: seguir desde Telegram si se para)."""
    print(texto)
    try:
        token, chat = token_telegram(), str(cargar_config().get("entrega", {}).get("chat_id", ""))
        if token and chat:
            enviar_telegram(token, chat, "📚 Precarga Chollometro: " + texto.strip(),
                            teclado=TECLADO_SEGUIR if seguir else None, hilo="sistema")
    except Exception as e:
        log(f"aviso de precarga: {str(e)[:80]}")


def guardar_seguir(**datos):
    (DATOS / SEGUIR).write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")


def seguir_precarga(hasta_hora=None):
    """`seguir`: retoma la última carga que se paró (por error, reto o la hora), desde la página donde se quedó.
    Tras pararse por la hora sigue sin hora tope, salvo que se le dé otra ("seguir 08:30")."""
    if carga_en_marcha():
        aviso_precarga("ya hay una carga en marcha: no lanzo otra")
        return False
    try:
        datos = json.loads((DATOS / SEGUIR).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        aviso_precarga("no hay ninguna carga parada que seguir")
        return False
    datos["hasta_hora"] = hasta_hora
    aviso_precarga(f"sigo con {datos['listado']} desde la página {datos['inicio']}")
    precargar_chollometro(**datos)
    return True


def _precargar(dias_nuevos, paginas_portada, pausa, desde, listado, inicio, fin, hasta_hora):
    portada = CM_LISTADOS.get(listado, listado)
    parada = None  # página donde se paró antes de acabar (hora o fallo)
    con = bd()
    tope = datetime.fromisoformat(desde).timestamp() if desde else 0
    limite = (datetime.now() - timedelta(days=dias_nuevos)).timestamp()
    total = 0
    # dias_nuevos = 0: sin /nuevos, solo el listado (Zeta, 18/11/2026: "solo destacados y subiendo").
    tramos = ((CM_NUEVOS, 40 * dias_nuevos + 10, limite),) if dias_nuevos > 0 else ()
    for base, paginas, hasta in tramos + ((portada, paginas_portada, tope),):
        n, anterior, primera = 0, None, None
        for n in (range(inicio, inicio + paginas) if base == portada else range(1, paginas + 1)):
            if base == portada and fin and datetime.now() >= fin:
                parada = n
                aviso_precarga(f"  {listado}: son las {hasta_hora}, paro (página de inicio para seguir: {n}). "
                               "Pulsa Seguir cuando quieras continuar (seguirá sin hora tope).", seguir=True)
                break
            try:
                html, _ = descargar(_pagina(base, n))
            except Exception as e:
                log(f"precarga chollometro {base} p{n}: {e}")
                if base == portada and n > inicio and getattr(e, "code", None) in (404, 410):
                    # 18/11/2026: Destacados acaba en la 334 y la 335 da "410 Gone". Es el final del listado, no un
                    # bloqueo: sin botón Seguir (volvería a fallar en la misma página).
                    aviso_precarga(f"  {listado}: la página {n} ya no existe ({e.code}), fin del listado")
                elif base == portada:
                    parada = n
                    aviso_precarga(f"  {listado}: la página {n} falla ({str(e)[:60]}), paro. Si es un bloqueo, "
                                   "espera unas horas antes de pulsar Seguir.", seguir=True)
                break
            hilos = hilos_chollometro(html)
            if not hilos:
                if base == portada:
                    parada = n
                    aviso_precarga(f"  {listado}: la página {n} no trae hilos (¿reto?), paro. Espera unas horas "
                                   "antes de pulsar Seguir.", seguir=True)
                break
            (DATOS / "precarga.lock").touch()
            # Fecha de la página: la mediana, no el más antiguo. La página 1 de la portada lleva hilos fijados
            # antiguos (18/11/2026: uno del 04/11 entre los del 17/11) y la página 2 parecía "volver a hoy".
            # /nuevos va por fecha: ahí sí vale el más antiguo.
            fechas = sorted(h["publicado"] for h in hilos)
            viejo = fechas[len(fechas) // 2] if base == portada else fechas[0]
            ids = sorted(h["id"] for h in hilos)
            primera = primera or ids
            if base == portada and n > inicio and (ids == primera or (listado == "destacados" and anterior is not None
                                                                   and viejo >= anterior + 86400)):
                aviso_precarga(f"  {listado}: la página {n} vuelve a lo de hoy, fin del historial")
                break
            guardar_hilos(con, hilos)
            total += len(hilos)
            anterior = min(viejo, anterior) if anterior is not None else viejo
            if viejo < hasta:
                break
            if base == portada and n % 50 == 0:
                aviso_precarga(f"  {listado} página {n}: {datetime.fromtimestamp(viejo):%d/%m/%Y}, {total} hilos leídos")
            time.sleep(pausa * random.uniform(0.8, 1.2) if base == portada else pausa)
        print(f"{base}: hasta la página {n}, {total} posts guardados")
    if parada:
        guardar_seguir(dias_nuevos=0, paginas_portada=paginas_portada - (parada - inicio), pausa=pausa, desde=desde,
                       listado=listado, inicio=parada)
    else:
        (DATOS / SEGUIR).unlink(missing_ok=True)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('cm_ok', ?)", (datetime.now().isoformat(timespec="minutes"),))
    con.commit()
    n_hot = con.execute("SELECT COUNT(*) FROM cm WHERE temp>=?", (CM_HOT,)).fetchone()[0]
    aviso_precarga(f"{listado} terminado. Copia local: {con.execute('SELECT COUNT(*) FROM cm').fetchone()[0]} hilos, "
                   f"{n_hot} con {CM_HOT}° o más")
    con.close()

# ------------------------------------------------------------------ rondas

def cargar_config():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def tiendas_de(cfg, categoria):
    cat = cfg["categorias"][categoria]
    return cat.get("tiendas", []), cat.get("navegador", [])


def urls_ofertas():
    return json.loads(URLS.read_text(encoding="utf-8")) if URLS.exists() else {}


RECURSOS_PESADOS = {"image", "font", "media"}


class Navegador:
    """Chromium real controlado por código (0 tokens) para tiendas que cargan con JavaScript.

    Además del HTML final, captura las respuestas JSON de la propia web (XHR/fetch),
    que es donde suelen venir los productos. Usa un perfil persistente para guardar
    cookies entre rondas y parecer un visitante habitual.
    """

    def __init__(self, cfg=None):
        n = (cfg or {}).get("navegador", {})
        self.visible = n.get("visible", False)
        self.canal = n.get("canal") or None          # "chrome" usa el Chrome instalado
        self.espera_ms = n.get("espera_ms", 2500)
        self.scroll_max = n.get("scroll_max", 8)
        self.bloquear = n.get("bloquear_recursos", True)  # sin imágenes ni fuentes: carga mucho más rápido
        self.espera_red_ms = n.get("espera_red_ms", 8000)
        self._pw = self._ctx = None

    def _cerrojo(self, espera_max=600):
        """Solo un Chromium a la vez con el mismo perfil: si otra categoría lo usa, espera su turno."""
        DATOS.mkdir(parents=True, exist_ok=True)
        self._lock = DATOS / "navegador.lock"
        inicio = time.monotonic()
        while True:
            try:
                fd = os.open(self._lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return
            except FileExistsError:
                try:  # cerrojo abandonado (proceso que murió): más de 30 min
                    if time.time() - self._lock.stat().st_mtime > 1800:
                        self._lock.unlink()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() - inicio > espera_max:
                    raise TimeoutError("navegador ocupado por otra categoría")
                time.sleep(5)

    def __enter__(self):
        from playwright.sync_api import sync_playwright  # solo hace falta si hay tiendas de navegador
        self._cerrojo()
        try:
            self._pw = sync_playwright().start()
        except Exception:
            self._lock.unlink(missing_ok=True)
            raise
        (DATOS / "perfil").mkdir(parents=True, exist_ok=True)
        try:
            self._ctx = self._lanzar()
        except Exception:
            self._pw.stop()
            self._lock.unlink(missing_ok=True)
            raise
        return self

    def _lanzar(self):
        ctx = self._pw.chromium.launch_persistent_context(
            str(DATOS / "perfil"), headless=not self.visible, channel=self.canal,
            locale="es-ES", timezone_id="Europe/Madrid", user_agent=UA,
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"])
        if self.bloquear:
            ctx.route("**/*", lambda r: r.abort() if r.request.resource_type in RECURSOS_PESADOS else r.continue_())
        return ctx

    def __exit__(self, *exc):
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            try:
                if self._pw:
                    self._pw.stop()
            finally:
                self._lock.unlink(missing_ok=True)

    def leer(self, url, completo=False):
        """-> (html, url_final, [objetos JSON capturados])
        completo: segundo intento con otra configuración (instrucciones de lectura, paso 1d): con imágenes
        y fuentes, y el doble de espera y de scroll. Para webs que no pintan los precios sin eso."""
        # Solo peticiones TERMINADAS: leer el cuerpo de una respuesta que sigue abierta
        # (streaming, long-polling) bloquea para siempre (pasaba con MediaMarkt).
        if not permitido(url):
            base, pendiente = robots_pendiente_de_navegador(url)
            if pendiente:
                pr = self._ctx.new_page()
                try:
                    r = pr.goto(base + "/robots.txt", timeout=30_000)
                    reglas = reglas_de_respuesta(r.status, r.text()) if r else None
                except Exception:
                    reglas = None
                finally:
                    pr.close()
                if reglas is not None:
                    guardar_robots(base, reglas)
            if not permitido(url):
                raise BloqueadoPorRobots(f"robots.txt no permite leer {url}")
        terminadas = []
        page = self._ctx.new_page()
        x = 2 if completo else 1
        if completo:  # las rutas de la página mandan sobre las del contexto: deja pasar todo
            page.route("**/*", lambda r: r.continue_())
        page.on("requestfinished", lambda q: terminadas.append(q)
                if q.resource_type in ("xhr", "fetch") else None)
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            if resp is not None and resp.status >= 400:
                raise OSError(f"HTTP {resp.status} en navegador")
            try:
                page.wait_for_load_state("networkidle", timeout=self.espera_red_ms * x)
            except Exception:
                pass
            # Scroll infinito: baja hasta que la página deja de crecer (con tope).
            alto = 0
            for _ in range(self.scroll_max * x):
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(self.espera_ms * x // 3)
                nuevo = page.evaluate("document.body ? document.body.scrollHeight : 0")
                if nuevo <= alto:
                    break
                alto = nuevo
            html, final = page.content(), page.url
            datos = []
            for q in terminadas:
                if len(datos) >= 80:
                    break
                try:
                    r = q.response()
                    if r is not None and "json" in (r.headers.get("content-type") or ""):
                        datos.append(r.json())
                except Exception:
                    pass
            return html, final, datos
        finally:
            page.close()


RE_RETO_URL = re.compile(r"/punish|_____tmd_____|x5secdata|captcha|/login\.htm|baxia", re.I)


class NavegadorNormal(Navegador):
    """Chrome VISIBLE y normal para Miravia y AliExpress (autorizado por Zeta el 17/11/2026, con las condiciones
    de Idealo): sin perfil, sin cambiar el agente, sin ocultar la automatización y sin bloquear nada. Lo que
    se probó a mano ese día: Miravia 87 productos y AliExpress SuperDeals 49, sin reto. Si sale reto o
    captcha NO se resuelve: la lectura falla con "reto" y la tienda se para 30 min fijos (`registrar_fallos`)."""

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self.visible, self.bloquear = True, False
        self._nav = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            self._nav = self._pw.chromium.launch(headless=False, channel=self.canal)
            self._ctx = self._nav.new_context(locale="es-ES")
        except Exception:
            self._pw.stop()
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self._nav:
                self._nav.close()
        finally:
            if self._pw:
                self._pw.stop()

    def leer(self, url, completo=False):
        html, final, datos = super().leer(url, completo)
        if RE_RETO_URL.search(urlparse(final).path + "?" + urlparse(final).query) \
                or "login" in urlparse(final).netloc:
            raise OSError("reto antibot (no se resuelve)")
        return html, final, datos


def leer_navegador_normal(cfg, trabajos):
    """trabajos: [(tienda, url, max_paginas)] -> [(tienda, url, productos, error)], de una en una."""
    try:
        with NavegadorNormal(cfg) as nav:
            return [leer_tienda(t, {}, None, nav, pags, u) for t, u, pags in trabajos]
    except ImportError:
        return [(t, u, [], "falta playwright (fase 2)") for t, u, _ in trabajos]
    except Exception as e:
        return [(t, u, [], f"navegador normal: {str(e)[:80]}") for t, u, _ in trabajos]


def tiendas_navegador_normal(cfg):
    return set(((cfg or {}).get("navegador_normal") or {}).get("tiendas", []))


RE_REL_NEXT = re.compile(r"<(?:link|a)\b[^>]*\brel=[\"']?next[\"']?[^>]*>", re.I)
RE_HREF = re.compile(r"\bhref=[\"']([^\"']+)[\"']", re.I)
RE_TEXTO_SIGUIENTE = re.compile(r"^(?:siguiente|página siguiente|next|›|»|>|→)$", re.I)
RE_PARAM_PAGINA = re.compile(r"([?&](?:page|p|pagina|pag|pg)=)(\d+)", re.I)
RE_RUTA_PAGINA = re.compile(r"(/(?:page|pagina)/)(\d+)", re.I)


def siguiente_pagina(html, url_actual):
    """URL de la página siguiente de un listado, o None. Orden: rel=next, botón 'Siguiente',
    y si la URL ya lleva número de página, ese número + 1."""
    m = RE_REL_NEXT.search(html)
    if m:
        h = RE_HREF.search(m.group(0))
        if h:
            return urljoin(url_actual, h.group(1).replace("&amp;", "&"))
    parser = Enlaces()
    try:
        parser.feed(html)
    except Exception:
        pass
    for href, texto in parser.enlaces:
        if href and not href.startswith(("#", "javascript")) and RE_TEXTO_SIGUIENTE.match(texto.strip()):
            return urljoin(url_actual, href.replace("&amp;", "&"))
    for rx in (RE_PARAM_PAGINA, RE_RUTA_PAGINA):
        m = rx.search(url_actual)
        if m:
            return url_actual[:m.start(2)] + str(int(m.group(2)) + 1) + url_actual[m.end(2):]
    return None


def paginas_de(cfg, tienda):
    """Cuántas páginas leer de una tienda: tiendas.json -> "paginas": {"defecto": 3, "mi.com/es": 1}"""
    pag = (cfg or {}).get("paginas", {})
    return max(1, int(pag.get(tienda, pag.get("defecto", 3))))


def url_de(tienda, urls, categoria=None):
    return urls.get(f"{categoria}:{tienda}") or urls.get(tienda) or url_tienda(tienda)


RE_A_ETIQUETA = re.compile(r"<a\b[^>]*>", re.I)
RE_ARIA_TIENDA = re.compile(r'\baria-label="Ver (.+?) en ([^"]+?) por ([\d.,]+)\s*(?:€|&euro;|EUR)[^"]*"', re.I)


def enlace_real(href):
    """Enlace de redirección del comparador ('/goto/pccomponentes?url=https%3A%2F%2Fwww.pccomponentes.com%2F...')
    -> la URL de la tienda que lleva dentro. Se lee del propio enlace, SIN visitarlo: /goto/ está prohibido en
    el robots.txt de PCComTracker. Un enlace normal se devuelve tal cual."""
    url = (parse_qs(urlparse(href).query).get("url") or [""])[0]
    return url if url.startswith("http") else href


def via_comparador(html, productos, tienda, base_url, estricto=False):
    """Tienda leída a través de un comparador que respeta robots.txt (PcComponentes por PCComTracker,
    porque su antibot no deja entrar a ningún navegador automático). Se quedan solo los productos
    con enlace directo a la tienda ('Ver <nombre> en PcComponentes por 40,59 €'), con la URL y el
    precio de la tienda, no los del comparador. Si la página es de la propia tienda, no cambia nada."""
    marca = raiz(tienda)
    if marca in urlparse(base_url).netloc:
        return productos
    enlaces = {}
    for m in RE_A_ETIQUETA.finditer(html):
        h, a = RE_ATR_HREF.search(m.group(0)), RE_ARIA_TIENDA.search(m.group(0))
        destino = enlace_real(unescape(h.group(1))) if h else ""
        if h and a and marca in urlparse(destino).netloc:
            precio = a_numero(a.group(3))
            if precio:
                nombre = " ".join(unescape(a.group(1)).split())
                # La ficha del propio comparador (con el histórico) va en la misma fila, justo antes.
                fichas = RE_FICHA_COMPARADOR.findall(html[max(0, m.start() - 6000):m.start()])
                hist = urljoin(base_url, unescape(fichas[-1])) if fichas else None
                enlaces[nombre.lower()] = (nombre, destino, round(precio, 2), hist)
    if not enlaces:
        # estricto: ya se sabe que es un comparador (su página 1 enlazaba a la tienda). Sus productos
        # sin enlace a la tienda llevarían la URL del comparador (pasó con la página 2 de PCComTracker).
        # Si no, no es un comparador (o la tienda se lee en otro dominio): se deja tal cual.
        return [] if estricto else productos
    del_comparador = {p["nombre"].lower(): p for p in productos}
    salida = []
    for clave, (nombre, url, precio, hist) in enlaces.items():
        base = del_comparador.get(clave, {})
        antes = base.get("precio_antes")
        salida.append({
            "nombre": nombre, "precio": precio,
            "precio_antes": antes if antes and antes > precio * 1.01 else None,
            "marca": base.get("marca"), "ean": base.get("ean"), "url": url,
            "imagen_url": base.get("imagen_url"), "valoracion": base.get("valoracion"),
            "resenas": base.get("resenas"), "historico_url": hist})
    return salida


RE_FICHA_COMPARADOR = re.compile(r'href="((?:https?://[^"/]+)?/(?:producto|productos|product)/[^"]+)"', re.I)
RE_HABITUAL = re.compile(r"precio habitual (?:es )?de ([\d.,]+)\s*€", re.I)
RE_HABITUAL_DATOS = re.compile(r'averagePrice\\?"\s*:\s*([\d]+(?:\.\d+)?)')
RE_FECHA_MINIMO = re.compile(r'minPriceDate\\?"\s*:\s*\\?"\$?D?(\d{4}-\d{2}-\d{2})')


def _historico_seguro(url):
    try:
        return historico_comparador(url)
    except Exception as e:
        log(f"histórico {url[:80]}: {e}")
        return None


def historico_comparador(url):
    """Lee la ficha de un comparador con histórico (PCComTracker): mínimo y precio habitual.
    -> {"minimo": x, "habitual": y, "url": url} o None."""
    html, final = descargar(url)
    minimo = minimo_en_texto(html)
    if not minimo:
        return None
    texto = _texto(re.sub(r"<script\b.*?</script>|<style\b.*?</style>", " ", html, flags=re.S | re.I))
    m = RE_HABITUAL.search(texto)
    habitual = a_numero(m.group(1)) if m else None
    if not habitual:  # "Precio habitual · Mediana de los últimos 12 meses 153,98 €" = "averagePrice":153.98
        a = RE_HABITUAL_DATOS.search(html)
        habitual = float(a.group(1)) if a else None
    salida = {"minimo": minimo, "habitual": habitual, "url": final}
    if RE_SIN_PRECIO_TIENDA.search(texto):
        # 18/11/2026 (Zeta: "las últimas televisiones no coincide el precio"): PcComponentes no lo vende ahora; el
        # precio del listado era el de Amazon (Philips 48OLED770 a 609 €), un vendedor externo o el último visto.
        salida["sin_precio_tienda"] = True
        # Lo que sale en pantalla es el precio de Amazon ("Comprar en Amazon por 609,00 €"), con su enlace.
        a = re.search(r'href="([^"]*/goto/amazon\?[^"]+)"', html)
        pa = re.search(r"Comprar en Amazon por\s*([\d.,]+)\s*€", texto)
        asin = asin_de(enlace_real(urljoin(final, unescape(a.group(1))))) if a else None
        if asin and pa:
            salida["amazon_url"], salida["amazon_precio"] = f"https://www.amazon.es/dp/{asin}", a_numero(pa.group(1))
    # Fecha del mínimo (17/11/2026): viene en sus datos ("minPriceDate":"$D2026-11-16T06:06:39.000Z").
    f = RE_FECHA_MINIMO.search(html)
    if f:
        salida["minimo_fecha"] = f.group(1)
    # Enlace directo a la tienda (dentro del /goto/ del comparador, sin visitarlo).
    for h in RE_GOTO.findall(html):
        real = enlace_real(urljoin(final, unescape(h)))
        if real.startswith("http") and "pccomtracker" not in urlparse(real).netloc:
            salida["tienda_url"] = real
            break
    return salida


RE_SIN_PRECIO_TIENDA = re.compile(r"sin precio oficial de pccomponentes|no disponible ahora en pccomponentes|"
                                  r"precio no disponible", re.I)
RE_GOTO = re.compile(r'href="([^"]*/goto/pccomponentes\?[^"]+)"', re.I)


def comprobar_muro(html):
    """Página sin productos con un muro antibot o un captcha: no se lee (instrucciones de lectura, paso 4:
    nunca resolver captchas ni saltarse antibots). La tienda se aparta 24 h sin reintentar en bucle."""
    if RE_ANTIBOT.search(html[:30000]):
        raise OSError("antibot o captcha: no se lee")


def leer_tienda(tienda, urls, categoria=None, nav=None, max_paginas=1, url=None):
    """Lee el listado de ofertas (o la URL dada: una categoría del catálogo) y sus páginas siguientes.
    Para en cuanto una página no aporta productos nuevos (así no se piden páginas de más ni se repite
    la última en bucle)."""
    url = url or url_de(tienda, urls, categoria)
    productos, claves, visitadas, actual = [], set(), set(), url
    try:
        for n in range(max_paginas):
            if actual in visitadas:
                break
            visitadas.add(actual)
            try:
                if nav is not None:
                    html, final, datos = nav.leer(actual)
                    nuevos = extraer(html, final, datos)
                    if n == 0 and not nuevos and "privalia." in urlparse(final).netloc \
                            and (campanas := campanas_privalia(html)):
                        # Portada de Privalia: solo campañas. Pasan al catálogo y se leen por turnos (18/11/2026).
                        guardar_campanas(tienda, campanas)
                        return tienda, url, [], None
                    if n == 0 and not nuevos:
                        comprobar_muro(html)
                        html, final, datos = nav.leer(actual, completo=True)  # paso 1d: otra configuración
                        nuevos = extraer(html, final, datos)
                else:
                    html, final = descargar(actual)
                    nuevos = extraer(html, final)
                if n == 0 and not nuevos:
                    comprobar_muro(html)
            except Exception:
                if n == 0:
                    raise
                break  # falla una página siguiente: nos quedamos con lo leído
            if n == 0:
                comparador = raiz(tienda) not in urlparse(final).netloc and bool(
                    via_comparador(html, nuevos, tienda, final, estricto=True))
            nuevos = via_comparador(html, nuevos, tienda, final, estricto=comparador)
            nuevos = [p for p in nuevos if not RE_SIN_PRECIO_TIENDA.search(p["nombre"])]  # sin precio de la tienda
            for p in nuevos:
                # Listado del comparador sin enlace a la tienda (subcategorías de PCComTracker): la ficha
                # del comparador es el histórico; el enlace a la tienda se saca de ella si es candidato.
                if p.get("url") and not p.get("historico_url") and raiz(tienda) not in urlparse(p["url"]).netloc \
                        and re.search(r"/(?:producto|productos|product)/", urlparse(p["url"]).path):
                    p["historico_url"] = p["url"]
            nuevos = [p for p in nuevos if (p["nombre"].lower(), p["precio"]) not in claves]
            if not nuevos:
                break
            claves.update((p["nombre"].lower(), p["precio"]) for p in nuevos)
            productos += nuevos
            actual = siguiente_pagina(html, final)
            if not actual and n == 0 and nav is None:
                # Muchas tiendas paginan con un botón 'Ver más' sin enlace pero aceptan ?page=2
                # (MediaMarkt, Electrocosto). Si no trae nada nuevo, el bucle para en la siguiente vuelta.
                actual = final + ("&" if "?" in final else "?") + "page=2"
            if not actual:
                break
            if nav is None:
                time.sleep(1)  # cortesía con la tienda entre páginas
        return tienda, url, productos, None
    except Exception as e:
        return tienda, url, productos, str(e)[:120]


# ------------------------------------------- lectores específicos (videojuegos)
# Steam, Epic, Android e iOS no tienen una "página de ofertas" que se pueda leer de forma
# genérica, así que se leen por sus fuentes de datos públicas. Sin IA y sin tokens.

RE_STEAM_FILA = re.compile(r'<a[^>]+href="(https://store\.steampowered\.com/app/(\d+)/[^"]*)"[^>]*class="[^"]*search_result_row'
                           r'(.*?)</a>', re.S)
RE_STEAM_FILA2 = re.compile(r'<a[^>]+class="[^"]*search_result_row[^"]*"[^>]+href="(https://store\.steampowered\.com/app/(\d+)/[^"]*)"'
                            r'(.*?)</a>', re.S)
RE_RESENAS_STEAM = re.compile(r"(\d{1,3})\s?%.{0,40}?([\d.,]+)\s+(?:reseñas|user reviews)", re.S | re.I)


def _texto(html):
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def steam_ofertas(paginas=3):
    """Ofertas de Steam en euros, con % de descuento y reseñas (lo que pide la regla del 80 %)."""
    productos, vistos = [], set()
    for i in range(paginas):
        url = ("https://store.steampowered.com/search/results/?specials=1&cc=es&l=spanish"
               f"&infinite=1&count=50&start={i * 50}")
        txt, _ = descargar(url)
        html = json.loads(txt).get("results_html", "")
        filas = RE_STEAM_FILA.findall(html) or RE_STEAM_FILA2.findall(html)
        if not filas:
            break
        for enlace, appid, cuerpo in filas:
            if appid in vistos:
                continue
            vistos.add(appid)
            titulo = re.search(r'class="title"[^>]*>(.*?)<', cuerpo, re.S)
            final = re.search(r'discount_final_price[^>]*>(.*?)</div>', cuerpo, re.S)
            orig = re.search(r'discount_original_price[^>]*>(.*?)</div>', cuerpo, re.S)
            if not (titulo and final):
                continue
            precio = 0.0 if re.search(r"gratis|free", _texto(final.group(1)), re.I) else a_numero(_texto(final.group(1)))
            antes = a_numero(_texto(orig.group(1))) if orig else None
            val = res = None
            tip = re.search(r'data-tooltip-html="([^"]+)"', cuerpo)
            if tip:
                m = RE_RESENAS_STEAM.search(tip.group(1).replace("&lt;br&gt;", " "))
                if m:
                    val, res = float(m.group(1)), int(re.sub(r"\D", "", m.group(2)))
            if precio is None:
                continue
            productos.append({"nombre": _texto(titulo.group(1)), "precio": round(precio, 2),
                              "precio_antes": antes if antes and antes > precio else None, "marca": None,
                              "ean": None, "url": enlace.split("?")[0],
                              "imagen_url": f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg",
                              "valoracion": val, "resenas": res})
        time.sleep(1)
    return productos


# El host anterior (store-site-backend.static-ue1...) no existe: daba "getaddrinfo failed".
EPIC_PROMOS = ("https://store-site-backend-static.ak.epicgames.com/freeGamesPromotions"
               "?locale=es-ES&country=ES&allowCountries=ES")


def epic_gratis():
    """Juegos gratis o rebajados ahora mismo en Epic (su fuente pública de promociones)."""
    txt, _ = descargar(EPIC_PROMOS)
    elementos = json.loads(txt).get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
    productos = []
    for e in elementos:
        promos = ((e.get("promotions") or {}).get("promotionalOffers") or [])
        if not promos:
            continue  # no está en promoción ahora (los "próximamente" no cuentan)
        precio = (e.get("price") or {}).get("totalPrice") or {}
        final, orig = precio.get("discountPrice"), precio.get("originalPrice")
        if final is None or not orig:
            continue
        slug = e.get("productSlug") or next((m.get("pageSlug") for m in (e.get("catalogNs") or {}).get("mappings") or []
                                             if m.get("pageSlug")), None) or e.get("urlSlug")
        img = next((k.get("url") for k in e.get("keyImages") or [] if k.get("type") in ("OfferImageWide", "Thumbnail")), None)
        productos.append({"nombre": e.get("title", "").strip(), "precio": round(final / 100, 2),
                          "precio_antes": round(orig / 100, 2), "marca": None, "ean": None,
                          "url": f"https://store.epicgames.com/es-ES/p/{slug}" if slug else None,
                          "imagen_url": img, "valoracion": None, "resenas": None,
                          "gratis": final == 0})  # lo gratis es lo que más se calienta
    return productos


RE_PRECIOS_APP = re.compile(r"([$€£]?\s?\d+[.,]\d{2}\s?[$€£]?)\s*(?:->|→|=>|>|to|a)\s*([$€£]?\s?\d+[.,]\d{2}\s?[$€£]?|free|gratis)", re.I)
RE_GRATIS_ERA = re.compile(r"(?:free|gratis).{0,30}?(?:was|antes|era)\s*([$€£]?\s?\d+[.,]\d{2})", re.I)


def ofertas_apps(subreddit, dominio):
    """Apps y juegos de móvil rebajados, desde los subreddits de ofertas (r/googleplaydeals,
    r/AppHookup). Las reseñas las comprueba el 'verifica' en la ficha de la tienda."""
    txt, _ = descargar(f"https://www.reddit.com/r/{subreddit}/new.json?limit=50&raw_json=1")
    productos = []
    for hijo in json.loads(txt).get("data", {}).get("children", []):
        d = hijo.get("data", {})
        url, titulo = d.get("url") or "", d.get("title") or ""
        if dominio not in url:
            continue
        m = RE_PRECIOS_APP.search(titulo)
        if m:
            antes = a_numero(m.group(1))
            precio = 0.0 if re.match(r"free|gratis", m.group(2), re.I) else a_numero(m.group(2))
        else:
            m = RE_GRATIS_ERA.search(titulo)
            if not m:
                continue
            antes, precio = a_numero(m.group(1)), 0.0
        if antes is None or precio is None or antes <= precio:
            continue
        nombre = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", titulo)
        nombre = RE_PRECIOS_APP.sub("", nombre)
        nombre = re.split(r"\s[-–|]\s|\s+free\b|\s+gratis\b", nombre, flags=re.I)[0].strip(" -–:|")
        productos.append({"nombre": nombre or titulo[:80], "precio": round(precio, 2),
                          "precio_antes": round(antes, 2), "marca": None, "ean": None,
                          "url": url, "imagen_url": None, "valoracion": None, "resenas": None,
                          "nota": "precio del post (puede estar en $); confirmar en la ficha de España"})
    return productos


RE_ETIQUETA = re.compile(r"^\s*\[([^\]]+)\]\s*(?:\(([^)]+)\))?\s*(.+)$")


def juegos_gratis():
    """Juegos que se regalan ahora (Steam, Epic, GOG, Prime...) desde r/FreeGameFindings.
    Lo gratis es lo que más se calienta en Chollometro."""
    txt, _ = descargar("https://www.reddit.com/r/FreeGameFindings/new.json?limit=50&raw_json=1")
    productos = []
    for hijo in json.loads(txt).get("data", {}).get("children", []):
        d = hijo.get("data", {})
        m = RE_ETIQUETA.match(d.get("title") or "")
        if not m or (m.group(2) or "game").lower() not in ("game", "juego", "dlc"):
            continue
        if re.search(r"expired|ended|caducad", (d.get("link_flair_text") or "") + d.get("title", ""), re.I):
            continue
        url = d.get("url") or ""
        if not url.startswith("http") or "reddit.com" in url:
            continue
        nombre = re.sub(r"\s+[-–|]\s+.*$", "", m.group(3)).strip()
        productos.append({"nombre": f"{nombre} ({m.group(1)})", "precio": 0.0, "precio_antes": None, "marca": None,
                          "ean": None, "url": url, "imagen_url": None, "valoracion": None, "resenas": None,
                          "gratis": True})
    return productos


RE_TG_MENSAJE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)


def leer_telegram(canal):
    """Últimos mensajes de un canal PÚBLICO de chollos (t.me/s/canal). Precio y enlace salen del texto."""
    html, _ = descargar(f"https://t.me/s/{canal}")
    productos = []
    for cuerpo in RE_TG_MENSAJE.findall(html):
        enlaces = [h for h in re.findall(r'href="(https?://[^"]+)"', cuerpo) if "t.me/" not in h]
        texto = _texto(cuerpo.replace("<br/>", "\n").replace("<br>", "\n"))
        precios = [a_numero(x) for x in RE_EUROS_TITULO.findall(texto)]
        precios = [x for x in precios if x]
        if not (enlaces and precios):
            continue
        nombre = re.split(r"\s(?:\d{1,4}(?:[.,]\d{1,2})?\s?€|por\s|a\s\d)", texto)[0].strip(" -–:|🔥⚡️👉")[:120]
        precio, antes = min(precios), max(precios)
        productos.append({"nombre": nombre or texto[:80], "precio": precio,
                          "precio_antes": antes if antes > precio * 1.01 else None, "marca": None, "ean": None,
                          "url": enlaces[0], "imagen_url": None, "valoracion": None, "resenas": None})
    return productos


FUENTES_API = {
    "gratis.reddit": ("https://www.reddit.com/r/FreeGameFindings", juegos_gratis),
    "store.steampowered.com": ("https://store.steampowered.com/search/?specials=1", steam_ofertas),
    "store.epicgames.com": ("https://store.epicgames.com/es-ES/free-games", epic_gratis),
    "play.google.com": ("https://www.reddit.com/r/googleplaydeals", lambda: ofertas_apps("googleplaydeals", "play.google.com")),
    "apps.apple.com": ("https://www.reddit.com/r/AppHookup", lambda: ofertas_apps("AppHookup", "apps.apple.com")),
}


RE_EUROS_TITULO = re.compile(r"(\d{1,4}(?:[.,]\d{1,2})?)\s?(?:€|eur\b|euros)", re.I)


# Feeds que no están en /feed/ (los dos permiten leerlos en su robots.txt).
FEEDS = {"buscounchollo.com": "https://www.buscounchollo.com/xml/rss.xml"}
RE_DESDE = re.compile(r"desde\s+(?:solo\s+|s[oó]lo\s+)?(\d{1,4}(?:[.,]\d{1,2})?)\s?(?:€|eur\b|euros)", re.I)


def leer_rss(dominio):
    """Entradas nuevas de un blog de chollos (viajes) por su feed. Lee RSS (<item>) y el XML de
    BuscoUnChollo (<listing> con <price>). Precio: campo price, si no el título, si no la descripción
    ('desde solo 332€', ViajerosPiratas)."""
    import xml.etree.ElementTree as ET
    txt, final = descargar(FEEDS.get(dominio) or url_tienda(dominio).rstrip("/") + "/feed/")
    raiz_xml = ET.fromstring(txt.encode("utf-8") if isinstance(txt, str) else txt)
    productos = []
    for item in (e for e in raiz_xml.iter() if e.tag in ("item", "listing")):
        titulo = (item.findtext("title") or "").strip()
        enlace = (item.findtext("link") or item.findtext("url") or "").strip()
        descripcion = " ".join(re.sub(r"<[^>]+>", " ", " ".join(
            (d.text or "") for d in item if d.tag in ("description", "{http://purl.org/rss/1.0/modules/content/}encoded"))).split())
        precio = a_numero(item.findtext("price") or "")
        if not precio:
            m = RE_EUROS_TITULO.search(titulo) or RE_DESDE.search(descripcion) or RE_EUROS_TITULO.search(descripcion)
            precio = a_numero(m.group(1)) if m else None
        if not (titulo and enlace and precio):
            continue  # sin precio no se puede filtrar
        img = None
        for el in item.iter():
            if (el.tag.endswith("content") or el.tag == "enclosure") and el.get("url"):
                img = el.get("url")
                break
        productos.append({"nombre": titulo, "precio": precio, "precio_antes": None, "marca": None,
                          "ean": None, "url": enlace, "imagen_url": img, "valoracion": None, "resenas": None})
    return productos


def leer_api(tienda):
    if tienda.startswith("tg:"):
        canal = tienda[3:]
        try:
            return f"t.me/{canal}", f"https://t.me/s/{canal}", leer_telegram(canal), None
        except Exception as e:
            return f"t.me/{canal}", f"https://t.me/s/{canal}", [], str(e)[:120]
    if tienda.startswith("rss:"):
        dominio = tienda[4:]
        try:
            return dominio, url_tienda(dominio) + "feed/", leer_rss(dominio), None
        except Exception as e:
            return dominio, url_tienda(dominio) + "feed/", [], str(e)[:120]
    url, funcion = FUENTES_API[tienda]
    try:
        return tienda, url, funcion(), None
    except Exception as e:
        return tienda, url, [], str(e)[:120]


def leer_todas(tiendas, navegador, urls, categoria, cfg, apis=(), catalogo=()):
    """Tiendas normales: 6 a la vez. Tiendas de navegador: varias pestañas a la vez en un solo
    Chromium (las dos cosas al mismo tiempo).
    catalogo: [(tienda, url)] categorías normales de la tienda (no solo ofertas) que tocan en esta ronda;
    se leen igual que la tienda (descarga o navegador), con más páginas."""
    pc = (cfg or {}).get("paginas_catalogo", {})
    cat_desc = [(t, u) for t, u in catalogo if t not in navegador]
    cat_nav = [(t, u) for t, u in catalogo if t in navegador]
    with ThreadPoolExecutor(max_workers=6) as ex:
        normales = ex.map(lambda t: leer_tienda(t, urls, categoria, None, paginas_de(cfg, t)), tiendas)
        de_cat = ex.map(lambda tu: leer_tienda(tu[0], urls, categoria, None, int(pc.get("descarga", 5)), tu[1]),
                        cat_desc)
        de_api = ex.map(leer_api, [a for a in apis if a in FUENTES_API or a.startswith(("rss:", "tg:"))])
        nav = []
        trabajos = ([(t, url_de(t, urls, categoria), paginas_navegador(cfg, t)) for t in navegador]
                    + [(t, u, int(pc.get("navegador", 3))) for t, u in cat_nav])
        normal = tiendas_navegador_normal(cfg)
        de_normal = None
        if normal:  # Miravia y AliExpress: su propio Chrome normal, a la vez que las pestañas del perfil
            pags = max(1, int(cfg["navegador_normal"].get("paginas", 1)))
            sueltos = [(t, u, min(p, pags)) for t, u, p in trabajos if t in normal]
            trabajos = [x for x in trabajos if x[0] not in normal]
            if sueltos:
                de_normal = ex.submit(leer_navegador_normal, cfg, sueltos)
        if trabajos:
            try:
                nav = leer_con_pestanas(cfg, trabajos)
            except ImportError:
                nav = [(t, u, [], "falta playwright (fase 2)") for t, u, _ in trabajos]
            except Exception as e:
                nav = [(t, u, [], f"navegador: {str(e)[:80]}") for t, u, _ in trabajos]
        if de_normal is not None:
            nav = nav + de_normal.result()
        return list(normales) + list(de_api) + nav + list(de_cat)


def paginas_navegador(cfg, tienda):
    """En navegador casi todas las tiendas cargan al bajar: por defecto 1 página (mucho más rápido)."""
    pag = (cfg or {}).get("paginas", {})
    if tienda in pag:
        return max(1, int(pag[tienda]))
    return max(1, int((cfg or {}).get("navegador", {}).get("paginas", 1)))


# ------------------------------------------- catálogo completo (no solo ofertas)
# Instrucciones de lectura, paso 3: además de "ofertas", leer las categorías normales. Cada tienda guarda
# sus categorías en vigilante_data/urls_catalogo.json (las saca "python vigilante.py catalogo" de la
# portada) y en cada ronda se lee UNA, por turnos, como mucho cada "catalogo_cada_min". Así se amplía la
# cobertura y el histórico propio (la prueba más fiable de una bajada) sin multiplicar las peticiones.

RE_PALABRA_CATEGORIA = re.compile(
    r"calzado|zapatill|zapato|bota|sandalia|ropa|hombre|mujer|niñ[oa]s?|moda|abrigo|chaqueta|vestido|camiseta|"
    r"pantal|electrodom|informátic|informatic|portátil|portatil|ordenador|monitor|componente|móvil|movil|"
    r"smartphone|tablet|televis|\btv\b|audio|sonido|auricular|gaming|consola|videojuego|hogar|cocina|mueble|"
    r"decoraci|iluminaci|lámpara|lampara|textil|baño|dormitorio|salón|salon|deporte|running|fitness|ciclismo|"
    r"bicicleta|pádel|padel|fútbol|futbol|montaña|juguete|bebé|bebe|belleza|perfum|maquillaje|cosmétic|"
    r"cosmetic|cuidado|salud|nutrici|mascota|perro|gato|alimentaci|bebida|bricolaje|herramienta|fotograf|"
    r"cámara|camara|smartwatch|reloj|wearable|climatizaci|aspirador|cafetera|accesorio|electrónica|"
    r"electronica|novedades|colecci", re.I)
RE_RUTA_NO_CATEGORIA = re.compile(
    r"login|cuenta|account|cart|carrito|cesta|checkout|ayuda|help|blog|contact|nuestras-tiendas|store-?locator|faq|"
    r"pol[ií]tica|legal|cookie|newsletter|registro|regist|wishlist|favorit|track|pedido|order|devoluc|search|"
    r"buscar|busqueda|\?q=|/p/|/dp/|/product/|/producto/|\.(?:jpg|png|pdf)$|\d{5,}|ofertas?|rebajas|outlet|"
    r"sale\b|promo|descuento|black-?friday|cyber|servicios?/|service/|recomendador|"
    # Accesorios y recambios (MediaMarkt, 17/11/2026: 12 de sus 40 categorías): lo que sale de ahí lo descarta
    # el filtro de accesorios, así que leerlas solo gasta turnos del catálogo.
    r"accesorios?-|recambios?|repuestos?|tendederos?", re.I)


def categorias_de_portada(html, base_url, maximo=12):
    """Enlaces del menú a categorías normales de la propia tienda (calzado, ropa, electrodomésticos...).
    Las de ofertas ya se leen aparte; fichas, cuenta, carrito y ayuda no cuentan."""
    host = urlparse(base_url).netloc.replace("www.", "")
    salida, rutas = [], set()
    for m in RE_A_COMPLETO.finditer(html):
        h = RE_ATR_HREF.search(m.group(1))
        if not h:
            continue
        url = urljoin(base_url, unescape(h.group(1))).split("#")[0]
        partes = urlparse(url)
        # El mismo host, no subdominios: Carrefour enlaza seguros.carrefour.es/hogar en el menú.
        if partes.scheme not in ("http", "https") or partes.netloc.replace("www.", "") != host:
            continue
        # La misma ruta puede venir escrita con tildes o codificada (MediaMarkt: "electrodomésticos-553"
        # y "electrodom%C3%A9sticos-553"): es una sola categoría.
        ruta = unquote(partes.path).rstrip("/")
        segmentos = [s for s in ruta.split("/") if s]
        texto = " ".join(unescape(re.sub(r"<[^>]+>", " ", m.group(2))).split())
        if not segmentos or len(segmentos) > 4 or len(texto) > 40 or ruta.lower() in rutas:
            continue
        if RE_RUTA_NO_CATEGORIA.search(url) or not RE_PALABRA_CATEGORIA.search(f"{texto} {ruta}"):
            continue
        rutas.add(ruta.lower())
        salida.append(url)
        if len(salida) >= maximo:
            break
    return salida


def _archivo_catalogo():
    return DATOS / "urls_catalogo.json"


def urls_catalogo():
    try:
        return json.loads(_archivo_catalogo().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def guardar_catalogo(cat):
    DATOS.mkdir(parents=True, exist_ok=True)
    _archivo_catalogo().write_text(json.dumps(cat, ensure_ascii=False, indent=1), encoding="utf-8")


def quitar_de_catalogo(tienda, url, motivo):
    """Una categoría que no se puede leer (robots.txt, 404, antibot) sale de la lista: no se reintenta."""
    cat = urls_catalogo()
    if url in cat.get(tienda, []):
        cat[tienda].remove(url)
        guardar_catalogo(cat)
        log(f"catálogo {tienda}: fuera {url[:90]} ({motivo[:60]})")


def orden_catalogo(lista, pesos, max_turnos=4):
    """Orden de lectura de las categorías de una tienda. Sin pesos, la lista tal cual (turno circular).
    Con pesos, la categoría con más demanda sale más veces por vuelta (hasta `max_turnos` por cada vez
    que sale la de menos demanda), repartida y sin dejar a ninguna sin leer."""
    if not pesos or len(lista) < 2:
        return list(lista)
    w = {u: peso_de(pesos, urlparse(u).path or u) for u in lista}
    # Respecto a la de más peso (18/11/2026): respecto a la de menos, con pesos por volumen casi todas llegaban al
    # tope de 4 y televisores, cafeteras y consolas salían igual.
    maximo = max(w.values())
    veces = {u: max(1, min(max_turnos, round(max_turnos * w[u] / maximo))) for u in lista}
    if len(set(veces.values())) == 1:
        return list(lista)
    total, credito, salida = sum(veces.values()), dict.fromkeys(lista, 0), []
    for _ in range(total):  # reparto suave: cada paso suma su parte a todas y sale la que más lleva
        for u in lista:
            credito[u] += veces[u]
        u = max(lista, key=lambda x: credito[x])
        credito[u] -= total
        salida.append(u)
    return salida


def catalogo_turno(con, cfg, tiendas):
    """[(tienda, url)] de las categorías que tocan en esta ronda: una por tienda, por turnos, y solo si
    hace más de 'catalogo_cada_min' (60 por defecto) que se leyó la anterior de esa tienda.
    Las categorías con más demanda en Chollometro tienen más turnos (`orden_catalogo`)."""
    cada_def = int((cfg or {}).get("catalogo_cada_min", 60))
    por_tienda = (cfg or {}).get("catalogo_por_tienda", {})  # {"pccomponentes.com": {"cada_min": 15, "por_ronda": 3}}
    cat = urls_catalogo()
    ahora = datetime.now()
    salida = []
    for t in tiendas:
        lista = cat.get(t) or []
        if not lista:
            continue
        lista = orden_catalogo(lista, pesos_volumen(con, cfg, t))  # 18/11: por volumen de calientes en esa tienda
        cada = int(por_tienda.get(t, {}).get("cada_min", cada_def))
        cuantas = min(len(set(lista)), max(1, int(por_tienda.get(t, {}).get("por_ronda", 1))))
        fila = con.execute("SELECT v FROM meta WHERE k=?", (f"cat_ultimo:{t}",)).fetchone()
        if fila and datetime.fromisoformat(fila[0]) > ahora - timedelta(minutes=cada):
            continue
        fila = con.execute("SELECT v FROM meta WHERE k=?", (f"cat_turno:{t}",)).fetchone()
        i = int(fila[0]) if fila else 0
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"cat_turno:{t}", str((i + cuantas) % len(lista))))
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                    (f"cat_ultimo:{t}", ahora.isoformat(timespec="minutes")))
        elegidas = []
        for k in range(len(lista)):  # con turnos repetidos, la misma categoría no sale dos veces en una ronda
            u = lista[(i + k) % len(lista)]
            if u not in elegidas:
                elegidas.append(u)
            if len(elegidas) == cuantas:
                break
        salida += [(t, u) for u in elegidas]
    con.commit()
    return salida


def ofertas_extra_turno(con, cfg, urls, tiendas):
    """[(tienda, url)] de las páginas de ofertas adicionales de cada tienda (campañas activas, outlet...),
    guardadas por `descubrir` en urls_ofertas.json con la clave "tienda+". Como mucho cada
    `ofertas_extra_cada_min` (15) por tienda, para no multiplicar las peticiones de la ronda rápida."""
    cada = int((cfg or {}).get("filtros_codigo", {}).get("ofertas_extra_cada_min", 15))
    ahora, salida = datetime.now(), []
    for t in tiendas:
        extras = urls.get(f"{t}+") or []
        if not extras:
            continue
        fila = con.execute("SELECT v FROM meta WHERE k=?", (f"extra_ultimo:{t}",)).fetchone()
        if fila and datetime.fromisoformat(fila[0]) > ahora - timedelta(minutes=cada):
            continue
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"extra_ultimo:{t}", ahora.isoformat(timespec="minutes")))
        salida += [(t, u) for u in extras]
    con.commit()
    return salida


def actualizar_vigilancia(con, dias=400):
    """Rellena `vigilancia` con los ASIN de Amazon que llegaron a 200° (de `cm_destino`): grupo de Chollometro
    (mainGroup), tipo de producto (familia del título), precio con que triunfó (el más bajo a 200°), temperatura
    máxima y número de hilos. Conserva cuándo se miró cada uno. -> cuántos hay."""
    desde = int(time.time()) - dias * 86400
    filas = con.execute("""SELECT d.clave, c.titulo, c.precio, c.temp, c.publicado, c.extra FROM cm_destino d
        JOIN cm c ON c.id = d.hilo WHERE d.clave LIKE 'amazon:%' AND c.temp >= 200 AND c.publicado >= ?
        ORDER BY c.publicado""", (desde,)).fetchall()
    datos = {}
    for clave, titulo, precio, temp, publicado, extra in filas:
        asin = clave.split(":", 1)[1]
        try:
            grupo = ((json.loads(extra) if extra else {}).get("mainGroup") or {}).get("threadGroupUrlName")
        except (ValueError, AttributeError):
            grupo = None
        d = datos.setdefault(asin, {"exito": None, "temp": 0, "hilos": 0})
        d["hilos"] += 1
        d["temp"] = max(d["temp"], temp or 0)
        if precio and precio > 0:
            d["exito"] = min(d["exito"] or precio, precio)
        d.update(ultimo=publicado, nombre=titulo, grupo=grupo or d.get("grupo") or "otros",
                 tipo=familia_de(titulo) or "otros")
    for asin, d in datos.items():
        if not d["exito"]:
            continue
        con.execute("""INSERT INTO vigilancia (asin, grupo, tipo, exito, temp, hilos, ultimo, nombre) VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(asin) DO UPDATE SET grupo=excluded.grupo, tipo=excluded.tipo, exito=excluded.exito,
            temp=excluded.temp, hilos=excluded.hilos, ultimo=excluded.ultimo, nombre=excluded.nombre""",
                    (asin, d["grupo"], d["tipo"], d["exito"], d["temp"], d["hilos"], d["ultimo"], d["nombre"][:200]))
    con.commit()
    return con.execute("SELECT COUNT(*) FROM vigilancia").fetchone()[0]


def vigilancia_turno(con, cfg, tiendas):
    """{url de la ficha: precio de éxito} de los ASIN vigilados que tocan en esta ronda (`vigilancia_amazon`:
    {"por_ronda": 20}). Por turnos entre grupos (grupo de Chollometro + tipo), de los grupos más grandes a los
    pequeños; dentro de cada grupo, primero los que hace más que no se miran y los más calientes. Cuando haya
    Keepa, esto se lee de su API en vez de abrir las fichas."""
    por_ronda = int(((cfg or {}).get("vigilancia_amazon") or {}).get("por_ronda", 20))
    if "amazon.es" not in tiendas or por_ronda <= 0:
        return {}
    fila = con.execute("SELECT v FROM meta WHERE k='vigilancia_actualizada'").fetchone()
    if not fila or datetime.fromisoformat(fila[0]) < datetime.now() - timedelta(hours=6):
        actualizar_vigilancia(con)
        con.execute("INSERT OR REPLACE INTO meta VALUES ('vigilancia_actualizada', ?)",
                    (datetime.now().isoformat(timespec="minutes"),))
    grupos = {}
    for asin, grupo, tipo, exito in con.execute("""SELECT asin, grupo, tipo, exito FROM vigilancia
            ORDER BY COALESCE(mirado, ''), temp DESC"""):
        grupos.setdefault((grupo, tipo), []).append((asin, exito))
    orden = sorted(grupos.values(), key=len, reverse=True)
    elegidos, i = {}, 0
    while len(elegidos) < por_ronda and any(orden):
        g = orden[i % len(orden)]
        if g:
            asin, exito = g.pop(0)
            elegidos[f"https://www.amazon.es/dp/{asin}"] = exito
        i += 1
    ahora = datetime.now().isoformat(timespec="minutes")
    con.executemany("UPDATE vigilancia SET mirado=? WHERE asin=?", [(ahora, asin_de(u)) for u in elegidos])
    con.commit()
    return elegidos


def busquedas_turno(con, cfg, tiendas):
    """[(tienda, url)] de las búsquedas por familia que tocan en esta ronda (Miravia y AliExpress, 17/11/2026:
    "por categorías que más grados dan en Chollometro", 4 por ronda). Cada tienda tiene en tiendas.json
    (`busquedas_familia`) sus consultas agrupadas por familia; salen por turnos y la familia con más hilos a
    200° en esa tienda sale más a menudo: cada consulta avanza 1/tasa cada vez que se lee y se eligen las
    `por_ronda` más atrasadas. Desde el 18/11 el peso es el volumen de hilos calientes (pesos_volumen).
    Como mucho cada `cada_min` por tienda."""
    b = (cfg or {}).get("busquedas_familia") or {}
    por_ronda, cada = int(b.get("por_ronda", 4)), int(b.get("cada_min", 15))
    ahora, salida = datetime.now(), []
    for tienda, d in (b.get("tiendas") or {}).items():
        consultas = [(fam, q) for fam, qs in (d.get("consultas") or {}).items() for q in qs]
        if tienda not in tiendas or not consultas or not d.get("url"):
            continue
        fila = con.execute("SELECT v FROM meta WHERE k=?", (f"busq_ultimo:{tienda}",)).fetchone()
        if fila and datetime.fromisoformat(fila[0]) > ahora - timedelta(minutes=cada):
            continue
        # 18/11/2026: por volumen de hilos calientes de la familia en esa tienda (la tasa salía ~1 en todas).
        pesos = pesos_volumen(con, cfg, tienda) or {}
        fila = con.execute("SELECT v FROM meta WHERE k=?", (f"busq_pase:{tienda}",)).fetchone()
        try:
            pase = json.loads(fila[0]) if fila else {}
        except ValueError:
            pase = {}
        base = min((pase[q] for _, q in consultas if q in pase), default=0.0)
        for _, q in consultas:  # una consulta nueva entra al nivel de la más atrasada, sin adelantar a todas
            pase.setdefault(q, base)
        orden = sorted(range(len(consultas)), key=lambda i: (pase[consultas[i][1]], i))
        for i in orden[:por_ronda]:
            fam, q = consultas[i]
            # El peso es de la familia: se reparte entre sus consultas (si no, una familia con 4 consultas salía el
            # doble que otra con 2 del mismo peso).
            n_fam = sum(1 for f2, _ in consultas if f2 == fam)
            pase[q] += n_fam / max(PESO_MIN, pesos.get(fam, 1.0))
            texto = q.strip().lower().replace(" ", d.get("espacio", "+"))
            salida.append((tienda, d["url"].format(q=quote(texto, safe="-+"))))
        pase = {q: round(pase[q], 3) for _, q in consultas if q in pase}  # sin consultas que ya no están
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"busq_pase:{tienda}", json.dumps(pase)))
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"busq_ultimo:{tienda}", ahora.isoformat(timespec="minutes")))
    con.commit()
    return salida


def catalogo(*nombres):
    """Saca de la portada de cada tienda sus categorías normales y las guarda en urls_catalogo.json.
    nombres: categorías del vigilante o tiendas (vacío: todas). Respeta robots.txt (no lee lo prohibido)."""
    cfg = cargar_config()
    cats = cfg["categorias"]
    elegidas, con_nav = [], set()
    for c, d in cats.items():
        for t in d.get("tiendas", []) + d.get("navegador", []):
            if not nombres or c in nombres or t in nombres:
                elegidas.append(t)
        con_nav.update(d.get("navegador", []))
    con = bd()
    con_nav |= tiendas_a_navegador(con, elegidas)
    elegidas = filtrar_por_demanda(con, cfg, list(dict.fromkeys(elegidas)))
    con.close()
    guardado = urls_catalogo()

    def apuntar(t, html, final, bajar=None):
        # catalogo_por_tienda: {"niveles": 2, "max": 40} entra también en cada categoría de la portada y
        # recoge sus subcategorías (MediaMarkt: la portada solo enlaza 12, y varias son índices).
        por = cfg.get("catalogo_por_tienda", {}).get(t, {})
        maximo = int(por.get("max", 12))
        if por.get("urls"):
            # Lista fija: Amazon tiene sus categorías como /b?node=599370031, y la portada no las da por categorías.
            guardado[t] = [u for u in por["urls"] if permitido(u)]
            print(f"{t:30} {len(guardado[t]):3} categorías (lista fija)")
            return
        lista = [u for u in categorias_de_portada(html, final, maximo=min(12, maximo)) if permitido(u)]
        if bajar and int(por.get("niveles", 1)) >= 2:
            vistas = {unquote(urlparse(u).path).rstrip("/").lower() for u in lista}
            for u in list(lista):
                if len(lista) >= maximo:
                    break
                res = _seguro(bajar, u)
                if isinstance(res, Exception):
                    continue
                for sub in categorias_de_portada(res[0], res[1], maximo=40):
                    clave = unquote(urlparse(sub).path).rstrip("/").lower()
                    if clave not in vistas and permitido(sub) and len(lista) < maximo:
                        vistas.add(clave)
                        lista.append(sub)
        if lista:
            guardado[t] = lista
        print(f"{t:30} {len(lista):3} categorías")

    normales = [t for t in elegidas if t not in con_nav]
    with ThreadPoolExecutor(max_workers=6) as ex:
        for t, res in zip(normales, ex.map(lambda t: _seguro(descargar, url_tienda(t)), normales)):
            if isinstance(res, Exception):
                print(f"{t:30} no se lee: {str(res)[:70]}")
            else:
                apuntar(t, res[0], res[1], bajar=descargar)
    navs = [t for t in elegidas if t in con_nav]
    if navs:
        try:
            with Navegador(cfg) as nav:
                for t in navs:
                    try:
                        html, final, _ = nav.leer(url_tienda(t))
                        apuntar(t, html, final)
                    except Exception as e:
                        print(f"{t:30} no se lee: {str(e)[:70]}")
        except ImportError:
            print("falta playwright: las tiendas de navegador se quedan sin catálogo")
    guardar_catalogo(guardado)
    print(f"Guardado: {sum(len(x) for x in guardado.values())} categorías de {len(guardado)} tiendas")


RE_ENLACE_OFERTA = re.compile(r"ofert|outlet|promo|black|rebaj|descuent|liquid|chollo|deals?\b|\bsale\b|saldo|"
                              r"ahorr|flash|cyber|especial|campa[ñn]|precios?-(?:bajos|minimos)|sin-iva|mitad", re.I)
MAPAS = "mapas"  # data/mapas/<tienda>.json: lo que dio cada página probada
# Lo que no se prueba en el mapa. A diferencia del catálogo antiguo, las páginas de ofertas (black-friday, rebajas,
# outlet) SÍ entran: son lo que se busca.
RE_RUTA_NO_MAPA = re.compile(r"/(?:mi-?cuenta|cuenta|account|login|registro|carrito|cart|checkout|cesta|ayuda|help|"
                             r"contacto|faq|tiendas|stores|tarjeta|legal|privacidad|cookies|condiciones|empleo|"
                             r"blog|prensa|newsletter|app|seguros|financiacion|devoluciones|envios?|recompra|"
                             r"tarifa-plana|servicios?|entradas|viajes)(?:/|$|\?)|\.(?:pdf|jpg|png)$|^mailto:|^tel:", re.I)


def _modo_tienda(cfg, tienda):
    if tienda in tiendas_navegador_normal(cfg):
        return "normal"
    if any(tienda in (c.get("navegador") or []) for c in cfg["categorias"].values()):
        return "perfil"
    return "descarga"


def enlaces_de_mapa(html, base_url, maximo=80):
    """Candidatas de una portada: primero las de ofertas (outlet, promo, black, rebajas...), luego categorías."""
    host = urlparse(base_url).netloc.replace("www.", "")
    ofertas, vistas = [], set()
    for m in RE_A_COMPLETO.finditer(html):
        h = RE_ATR_HREF.search(m.group(1))
        if not h:
            continue
        url = urljoin(base_url, unescape(h.group(1))).split("#")[0]
        p = urlparse(url)
        ruta = unquote(p.path).rstrip("/").lower()
        if p.scheme not in ("http", "https") or p.netloc.replace("www.", "") != host or not ruta or ruta in vistas:
            continue
        texto = " ".join(unescape(re.sub(r"<[^>]+>", " ", m.group(2))).split())[:60]
        if RE_RUTA_NO_MAPA.search(url) or len([s for s in ruta.split("/") if s]) > 4:
            continue
        if RE_ENLACE_OFERTA.search(f"{ruta} {texto}") and not re.search(r"-pr-|/p/\w*\d{5,}|/dp/", ruta):
            vistas.add(ruta)
            ofertas.append(url)
    categorias = [u for u in categorias_de_portada(html, base_url, maximo=40)
                  if unquote(urlparse(u).path).rstrip("/").lower() not in vistas]
    return (ofertas + categorias)[:maximo]


def mapa(*tiendas, probar=45, guardar=20, minimo=8):
    """`mapa <tienda> ...`: mapa de las páginas de ofertas y categorías de cada tienda, probándolas una a una
    (Zeta, 18/11/2026: "recorre cada tienda que dé pocos resultados, haz un mapa de sus categorías y ofertas").
    Lee la portada, la página de ofertas y el catálogo guardado; prueba hasta `probar` enlaces con el mismo modo que
    la ronda (descarga, Chrome con perfil o Chrome normal) y guarda como catálogo las que den `minimo` productos o
    más, ordenadas por productos con precio anterior. Si en descarga sale casi todo vacío, prueba con Chrome normal
    (autorizado por Zeta para cualquier tienda) y, si rinde más, deja la tienda en Chrome normal. Con un reto o un
    bloqueo para esa tienda: no se insiste."""
    cfg = cargar_config()
    urls = urls_ofertas()
    catalogo_actual = urls_catalogo()
    (DATOS / MAPAS).mkdir(parents=True, exist_ok=True)
    resumen = {}
    for tienda in tiendas:
        modo = _modo_tienda(cfg, tienda)
        resultado = _mapa_tienda(cfg, tienda, modo, urls, catalogo_actual, probar, minimo)
        if modo == "descarga" and sum(n for _, n, _ in resultado["pruebas"]) < minimo * 2:
            print(f"{tienda}: en descarga casi no salen productos; pruebo con Chrome normal")
            normal = _mapa_tienda(cfg, tienda, "normal", urls, catalogo_actual, probar, minimo)
            if sum(n for _, n, _ in normal["pruebas"]) > sum(n for _, n, _ in resultado["pruebas"]):
                resultado, modo = normal, "normal"
                nn = cfg.setdefault("navegador_normal", {}).setdefault("tiendas", [])
                if tienda not in nn:
                    nn.append(tienda)
                for c in cfg["categorias"].values():  # el Chrome normal se reparte desde la lista de navegador
                    if tienda in (c.get("tiendas") or []):
                        c["tiendas"].remove(tienda)
                        c.setdefault("navegador", []).append(tienda)
                CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        buenas = ordenar_por_demanda(tienda, [x for x in resultado["pruebas"] if x[1] >= minimo and en_seccion(tienda, x[0])], cfg)
        if len(buenas) >= 2:
            cat = urls_catalogo()
            cat[tienda] = [u for u, _, _ in buenas[:guardar]]
            guardar_catalogo(cat)
            por = cfg.setdefault("catalogo_por_tienda", {})
            if tienda not in por:
                por[tienda] = {"cada_min": 20, "por_ronda": 3,
                               "_nota": "18/11/2026: catálogo hecho con `mapa` (páginas probadas una a una)."}
                CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        (DATOS / MAPAS / f"{re.sub(r'[^a-z0-9.]+', '_', tienda)}.json").write_text(
            json.dumps({"tienda": tienda, "modo": modo, "fecha": datetime.now().isoformat(timespec="minutes"),
                        "pruebas": resultado["pruebas"], "error": resultado.get("error")}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        resumen[tienda] = (modo, len(resultado["pruebas"]), len(buenas),
                           sum(n for _, n, _ in buenas[:guardar]), sum(a for _, _, a in buenas[:guardar]),
                           resultado.get("error"))
        print(f"{tienda}: {modo}, {len(resultado['pruebas'])} páginas probadas, {len(buenas)} buenas "
              f"({resumen[tienda][3]} productos, {resumen[tienda][4]} con precio anterior)"
              + (f" | parado: {resultado['error']}" if resultado.get("error") else ""))
    return resumen


def _peso_pagina(pesos, url):
    """Peso de demanda de una página por las palabras de su dirección (/black-friday/electronica/tv/ -> televisores)."""
    ruta = re.sub(r"[-_/]+", " ", unquote(urlparse(url).path))
    return peso_de(pesos, ruta) if pesos else 1.0


def ordenar_por_demanda(tienda, pruebas, cfg=None):
    """Las páginas del mapa, de más a menos interesantes: productos con precio anterior por el peso de su familia
    en Chollometro para esa tienda (Zeta, 18/11/2026: "las categorías más frecuentes en Chollometro"). Una página
    sin familia reconocible pesa 1."""
    con = bd()
    try:
        pesos = pesos_volumen(con, cfg or cargar_config(), tienda)
    finally:
        con.close()
    return sorted(pruebas, key=lambda x: (-(x[2] + 0.2 * x[1]) * _peso_pagina(pesos, x[0]), -x[1]))


def reordenar_mapas(guardar=20, minimo=8):
    """`mapa reordenar`: vuelve a elegir el catálogo de cada tienda con su mapa guardado (data/mapas), ya ordenado
    por demanda, sin leer nada otra vez."""
    cfg = cargar_config()
    cat = urls_catalogo()
    for f in sorted((DATOS / MAPAS).glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        buenas = ordenar_por_demanda(d["tienda"], [x for x in d["pruebas"] if x[1] >= minimo and en_seccion(d["tienda"], x[0])], cfg)
        if len(buenas) >= 2:
            cat[d["tienda"]] = [u for u, _, _ in buenas[:guardar]]
            print(f"{d['tienda']:28} {len(buenas):3} buenas -> {len(cat[d['tienda']])} en el catálogo; primera: "
                  f"{cat[d['tienda']][0][:70]}")
    guardar_catalogo(cat)


def _mapa_tienda(cfg, tienda, modo, urls, catalogo_actual, probar, minimo):
    """Las pruebas de `mapa` para una tienda en un modo. -> {"pruebas": [(url, productos, con_antes)], "error"}"""
    pruebas, error = [], None
    con = bd()
    try:
        pesos = pesos_volumen(con, cfg, tienda)
    finally:
        con.close()
    bases = [u for u in [url_tienda(tienda), url_de(tienda, urls, None)] if u]
    if modo == "descarga":
        nav_ctx = None
    else:
        nav_ctx = NavegadorNormal(cfg) if modo == "normal" else Navegador(cfg)
    try:
        nav = nav_ctx.__enter__() if nav_ctx else None
        # Cola: portada y página de ofertas (nivel 0); lo que enlazan (nivel 1); y lo que enlazan sus páginas de
        # ofertas (nivel 2: El Corte Inglés enlaza los listados del Black Friday desde /black-friday/, no desde la
        # portada). Cada página se lee una vez: da sus productos y sus enlaces.
        cola = [(u, 0) for u in dict.fromkeys(bases)] + [(u, 1) for u in catalogo_actual.get(tienda) or []]
        vistas = set()
        while cola and len(pruebas) < probar:
            u, nivel = cola.pop(0)
            clave = unquote(urlparse(u).path).rstrip("/").lower() + "?" + urlparse(u).query
            if clave in vistas or not permitido(u) or not en_seccion(tienda, u):
                continue
            vistas.add(clave)
            try:
                html, final, datos = nav.leer(u) if nav else (*descargar(u), [])
            except Exception as e:
                if re.search(r"reto|antibot|captcha|403|429", str(e), re.I):
                    error = str(e)[:80]
                    break
                pruebas.append((u, 0, 0))
                continue
            productos = extraer(html, final, datos)
            pruebas.append((u, len(productos), sum(1 for p in productos if p.get("precio_antes"))))
            if nivel == 0 or (nivel == 1 and RE_ENLACE_OFERTA.search(unquote(u))):
                hijos = enlaces_de_mapa(html, final)
                # Primero los de ofertas, y dentro de cada grupo los de las familias que más se calientan en
                # Chollometro en esta tienda, para que entren aunque se llegue al tope de páginas.
                por_peso = lambda hs: sorted(hs, key=lambda h: -_peso_pagina(pesos, h))
                cola = [(h, nivel + 1) for h in por_peso([h for h in hijos if RE_ENLACE_OFERTA.search(unquote(h))])] \
                    + cola + [(h, nivel + 1) for h in por_peso([h for h in hijos if not RE_ENLACE_OFERTA.search(unquote(h))])]
            if not nav:
                time.sleep(1.5)
    finally:
        if nav_ctx:
            try:
                nav_ctx.__exit__(None, None, None)
            except Exception:
                pass
    return {"pruebas": pruebas, "error": error}


def _seguro(funcion, *args):
    try:
        return funcion(*args)
    except Exception as e:
        return e


# ------------------------------- tiendas que en descarga normal salen vacías
# Instrucciones de lectura, pasos 1a-1c: si el HTML no trae productos, mirar la página como una persona
# (Chrome, que también recoge las peticiones JSON públicas de la página). Una tienda de descarga que da
# 0 productos sin error dos rondas seguidas pasa sola al grupo del navegador durante 7 días.

def tiendas_a_navegador(con, tiendas, dias=7):
    limite = (datetime.now() - timedelta(days=dias)).isoformat(timespec="minutes")
    salida = set()
    for t in tiendas:
        fila = con.execute("SELECT v FROM meta WHERE k=?", (f"a_navegador:{t}",)).fetchone()
        if fila and fila[0] > limite:
            salida.add(t)
    return salida


def anotar_vacias(con, cfg, lecturas, de_descarga):
    if not (cfg or {}).get("filtros_codigo", {}).get("navegador_si_vacia", True):
        return
    for tienda, _, productos, error in lecturas:
        if tienda not in de_descarga or error:
            continue
        clave = f"vacia:{tienda}"
        if productos:
            con.execute("DELETE FROM meta WHERE k=?", (clave,))
            continue
        fila = con.execute("SELECT v FROM meta WHERE k=?", (clave,)).fetchone()
        n = int(fila[0]) + 1 if fila else 1
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (clave, str(n)))
        if n >= 2:
            con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                        (f"a_navegador:{tienda}", datetime.now().isoformat(timespec="minutes")))
            con.execute("DELETE FROM meta WHERE k=?", (clave,))
            log(f"{tienda}: sin productos en descarga normal, pasa a leerse con Chrome 7 días")
    con.commit()


def leer_con_pestanas(cfg, trabajos):
    """trabajos: [(tienda, url, max_paginas)] -> [(tienda, url, productos, error)]"""
    import asyncio
    turno = Navegador(cfg)
    turno._cerrojo()  # mismo perfil de Chrome: una categoría cada vez
    try:
        return asyncio.run(_pestanas(cfg, trabajos))
    finally:
        turno._lock.unlink(missing_ok=True)


async def _pestanas(cfg, trabajos):
    import asyncio
    from playwright.async_api import async_playwright
    n = (cfg or {}).get("navegador", {})
    sem = asyncio.Semaphore(max(1, int(n.get("pestanas", 2))))
    espera, red, scroll_max = n.get("espera_ms", 2500), n.get("espera_red_ms", 8000), n.get("scroll_max", 8)
    async with async_playwright() as pw:
        (DATOS / "perfil").mkdir(parents=True, exist_ok=True)
        ctx = await pw.chromium.launch_persistent_context(
            str(DATOS / "perfil"), headless=not n.get("visible", False), channel=n.get("canal") or None,
            locale="es-ES", timezone_id="Europe/Madrid", user_agent=UA,
            viewport={"width": 1366, "height": 900}, args=["--disable-blink-features=AutomationControlled"])
        if n.get("bloquear_recursos", True):
            async def filtro(route):
                if route.request.resource_type in RECURSOS_PESADOS:
                    await route.abort()
                else:
                    await route.continue_()
            await ctx.route("**/*", filtro)

        async def leer(url, completo=False):
            # Solo peticiones TERMINADAS: leer el cuerpo de una respuesta que sigue abierta
            # (streaming, long-polling) bloquea para siempre (pasaba con MediaMarkt).
            # completo: segundo intento con otra configuración (con imágenes, el doble de espera y scroll).
            if not await asyncio.to_thread(permitido, url):
                base, pendiente = robots_pendiente_de_navegador(url)
                if pendiente:
                    pr = await ctx.new_page()
                    try:
                        r = await pr.goto(base + "/robots.txt", timeout=30_000)
                        reglas = reglas_de_respuesta(r.status, await r.text()) if r else None
                    except Exception:
                        reglas = None
                    finally:
                        await pr.close()
                    if reglas is not None:
                        guardar_robots(base, reglas)
                if not await asyncio.to_thread(permitido, url):
                    raise BloqueadoPorRobots(f"robots.txt no permite leer {url}")
            terminadas = []
            page = await ctx.new_page()
            x = 2 if completo else 1
            if completo:
                async def todo(route):
                    await route.continue_()
                await page.route("**/*", todo)
            page.on("requestfinished", lambda q: terminadas.append(q) if q.resource_type in ("xhr", "fetch") else None)
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                if resp is not None and resp.status >= 400:
                    raise OSError(f"HTTP {resp.status} en navegador")
                try:
                    await page.wait_for_load_state("networkidle", timeout=red * x)
                except Exception:
                    pass
                alto = 0
                for _ in range(scroll_max * x):
                    await page.mouse.wheel(0, 3000)
                    await page.wait_for_timeout(espera * x // 3)
                    nuevo = await page.evaluate("document.body ? document.body.scrollHeight : 0")
                    if nuevo <= alto:
                        break
                    alto = nuevo
                html, final = await page.content(), page.url
                datos = []
                for q in terminadas:
                    if len(datos) >= 80:
                        break
                    try:
                        r = await q.response()
                        if r is not None and "json" in (r.headers.get("content-type") or ""):
                            datos.append(await r.json())
                    except Exception:
                        pass
                return html, final, datos
            finally:
                await page.close()

        async def tienda(t, url, max_pag):
            async with sem:
                productos, claves, visitadas, actual = [], set(), set(), url
                try:
                    for i in range(max_pag):
                        if not actual or actual in visitadas:
                            break
                        visitadas.add(actual)
                        try:
                            html, final, datos = await leer(actual)
                            leidos = extraer(html, final, datos)
                            if i == 0 and not leidos:
                                comprobar_muro(html)
                                html, final, datos = await leer(actual, True)  # paso 1d: otra configuración
                                leidos = extraer(html, final, datos)
                                if not leidos:
                                    comprobar_muro(html)
                        except Exception:
                            if i == 0:
                                raise
                            break
                        nuevos = [p for p in leidos if (p["nombre"].lower(), p["precio"]) not in claves]
                        if not nuevos:
                            break
                        claves.update((p["nombre"].lower(), p["precio"]) for p in nuevos)
                        productos += nuevos
                        actual = siguiente_pagina(html, final)
                    return t, url, productos, None
                except Exception as e:
                    return t, url, productos, str(e)[:120]

        try:
            return list(await asyncio.gather(*(tienda(*x) for x in trabajos)))
        finally:
            await ctx.close()


COLORES = {"negro", "blanco", "gris", "azul", "rojo", "verde", "rosa", "plata", "oro", "dorado", "beige",
           "morado", "lila", "amarillo", "naranja", "black", "white", "grey", "gray", "blue", "red", "green",
           "silver", "gold", "pink", "purple", "midnight", "graphite", "grafito", "titanio", "titanium", "color"}


def modelo_base(nombre):
    """Nombre normalizado sin colores: variantes de color cuentan como el mismo modelo."""
    return " ".join(sorted(t for t in tokens(nombre) if t not in COLORES))


def titulos_perfil(con, cfg, cada_min=60):
    """Títulos que ya has publicado en Chollometro (caché de 1 hora)."""
    perfil = cfg.get("entrega", {}).get("perfil_chollometro")
    if not perfil:
        return []
    fila = con.execute("SELECT v FROM meta WHERE k='perfil_fecha'").fetchone()
    if fila and datetime.fromisoformat(fila[0]) > datetime.now() - timedelta(minutes=cada_min):
        return [t for (t,) in con.execute("SELECT titulo FROM perfil")]
    try:
        html, final = descargar(f"https://www.chollometro.com/profile/{perfil}")
    except Exception as e:
        log(f"perfil {perfil}: {e}")
        return [t for (t,) in con.execute("SELECT titulo FROM perfil")]
    parser = Enlaces()
    parser.feed(html)
    titulos = {t for h, t in parser.enlaces if h and "/ofertas/" in h and len(t) > 8}
    for bloque in bloques_json(html):
        _titulos_json(bloque, titulos, [0])
    if titulos:
        con.execute("DELETE FROM perfil")
        con.executemany("INSERT OR IGNORE INTO perfil VALUES (?)", [(t,) for t in titulos])
    con.execute("INSERT OR REPLACE INTO meta VALUES ('perfil_fecha', ?)", (datetime.now().isoformat(),))
    con.commit()
    return [t for (t,) in con.execute("SELECT titulo FROM perfil")]


def _titulos_json(obj, salida, cont):
    cont[0] += 1
    if cont[0] > 100_000:
        return
    if isinstance(obj, dict):
        t = obj.get("title")
        if isinstance(t, str) and 8 < len(t) < 250 and ("threadId" in obj or "temperature" in obj or "merchant" in obj):
            salida.add(t)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                _titulos_json(v, salida, cont)
    elif isinstance(obj, list):
        for v in obj:
            _titulos_json(v, salida, cont)


def en_silencio_entregas(cfg):
    t = ahora_madrid().strftime("%H:%M")
    return any(a <= t < b for a, b in cfg.get("horario", {}).get("silencio_entregas", []))


def descartado_por_memoria(con, p, dias_dudosa=30, dias_repetido=7, mios=()):
    """True si el modelo se marcó 'dudosa' hace poco, o si ya se envió y no ha bajado de precio."""
    limite = (datetime.now() - timedelta(days=dias_dudosa)).isoformat(timespec="minutes")
    m = con.execute("SELECT veredicto FROM modelos WHERE base=? AND fecha>=?", (p["base"], limite)).fetchone()
    if m and m[0] == "dudosa":
        return True
    if m and m[0] == "caducada":
        # El producto era bueno pero la oferta se acabó: vuelve a valer si regresa a ese precio o menos.
        enviado = con.execute("SELECT MIN(precio) FROM lista WHERE base=?", (p["base"],)).fetchone()[0]
        if enviado is not None and p["precio"] > enviado:
            return True
        return any(parecido(p["nombre"], t) >= 0.7 for t in mios)
    limite = (datetime.now() - timedelta(days=dias_repetido)).isoformat(timespec="minutes")
    previo = con.execute("SELECT MIN(precio) FROM lista WHERE base=? AND fecha>=?", (p["base"], limite)).fetchone()[0]
    if previo is not None and p["precio"] >= previo * 0.98:
        return True
    # Ya lo has publicado tú (PorLa_Jeta), aunque sea en otro color o tienda.
    return any(parecido(p["nombre"], t) >= 0.7 for t in mios)


FRENO_PAUSA_H = 2


def freno_telegram(con, token, chat, pendientes):
    """Freno de emergencia: si de repente hay muchos candidatos (una tienda que cambia sus enlaces y
    todo parece nuevo, un fallo de filtros...), no inunda Telegram ni invita a gastar en 'verifica'.
    Límites en tiendas.json > filtros_codigo: max_avisos_telegram_hora (20) y _dia (60).
    Al pasarse: manda solo hasta el límite, pausa los envíos FRENO_PAUSA_H horas y avisa UNA vez.
    Lo que no se envía queda pendiente (no se pierde hasta el final del día).
    -> cuántos pendientes se pueden enviar ahora."""
    try:
        f = cargar_config().get("filtros_codigo", {})
    except Exception:
        f = {}
    max_hora, max_dia = f.get("max_avisos_telegram_hora", 20), f.get("max_avisos_telegram_dia", 60)
    ahora = datetime.now()
    fila =con.execute("SELECT v FROM meta WHERE k='freno_telegram'").fetchone()
    if fila and fila[0] > ahora.isoformat(timespec="minutes"):
        return 0  # en pausa: ya se avisó
    hora = (ahora - timedelta(hours=1)).isoformat(timespec="minutes")
    en_hora = con.execute("SELECT COUNT(*) FROM lista WHERE enviado=1 AND fecha>=?", (hora,)).fetchone()[0]
    en_dia = con.execute("SELECT COUNT(*) FROM lista WHERE enviado=1 AND fecha>=?",
                         (ahora.strftime("%Y-%m-%d"),)).fetchone()[0]
    cupo = max(0, min(max_hora - en_hora, max_dia - en_dia))
    if pendientes <= cupo:
        return pendientes
    fin_dia = en_dia + cupo >= max_dia
    hasta = (ahora.replace(hour=23, minute=59) if fin_dia else ahora + timedelta(hours=FRENO_PAUSA_H))
    con.execute("INSERT OR REPLACE INTO meta (k, v) VALUES ('freno_telegram', ?)", (hasta.isoformat(timespec="minutes"),))
    con.commit()
    motivo = f"{max_dia} avisos hoy" if fin_dia else f"más de {max_hora} avisos en una hora"
    log(f"FRENO telegram: {pendientes} pendientes, {motivo}; pausa hasta {hasta:%H:%M}")
    enviar_telegram(token, chat, f"⚠️ Freno del vigilante: {motivo} ({pendientes} candidatos esperando). "
                                 f"Envíos en pausa hasta las {hasta:%H:%M}. Si no es normal, puede ser una tienda "
                                 f"que ha cambiado sus enlaces: mira vigilante_data/log.txt antes de pedir 'verifica'.",
                    hilo="sistema")
    return cupo


def enviar_pendientes(con, entrega):
    """Manda lo pendiente (incluido lo guardado en el silencio). Antes, última comprobación
    en Chollometro: si alguien lo ha publicado mientras tanto, no se envía."""
    token, chat = token_telegram(), str(entrega.get("chat_id", ""))
    if not (token and chat):
        log("telegram: falta token o chat_id")
        return
    hoy = datetime.now().strftime("%Y-%m-%d")
    pendientes = con.execute("SELECT id, datos FROM lista WHERE enviado=0 AND fecha>=? ORDER BY id", (hoy,)).fetchall()
    cupo = freno_telegram(con, token, chat, len(pendientes))
    copia = None
    if pendientes[:cupo]:
        actualizar_chollometro(con)
        copia = cargar_chollometro(con)
    for n, datos in pendientes[:cupo]:
        d = json.loads(datos)
        p = d.pop("_p", None) or {}
        if p:
            ch = estado_chollometro(p, copia)
            if ch == "misma" or ya_publicado_igual_o_mas_barato(ch, p.get("precio") or 0):
                con.execute("UPDATE lista SET enviado=2 WHERE id=?", (n,))  # 2 = publicada por otro
                con.commit()
                continue
            p["ch"] = d["ch"] = ch
        if enviar_telegram(token, chat, mensaje_candidato(n, p or {}), (p or {}).get("imagen_url"),
                           boton_verificar(n), hilo=f"cat:{d.get('cat')}"):  # pestaña de su categoría
            con.execute("UPDATE lista SET enviado=1, datos=? WHERE id=?", (json.dumps(d, ensure_ascii=False), n))
        con.commit()


def boton_verificar(n):
    """Botón en línea "Verificar #N" debajo de cada candidato. Lo atiende el plugin de Hermes
    plugins/vigilante-botones (callback "vg:verifica:N"): Hermes recibe "verifica N" como si lo
    hubiera escrito Zeta, en respuesta a ese aviso."""
    return json.dumps({"inline_keyboard": [[{"text": f"Verificar #{n}", "callback_data": f"vg:verifica:{n}"}]]})


def mensaje_candidato(n, p):
    # Sin emojis (regla de Instinct). El "antes" de la tienda se etiqueta como suyo, no como descuento real.
    d = f", -{round(100 * (1 - p['precio'] / p['precio_antes']))} %" if p.get("precio_antes") else ""
    antes = f" (referencia de la tienda: {p['precio_antes']:g} €{d})" if p.get("precio_antes") else ""
    # Etiquetas honestas (instrucciones de lectura, paso 7): lo comprobado y lo que NO se ha comprobado.
    mp = p.get("_mercado_propio")
    idealo = (f"Mercado: {p['idealo']:g} € en {mp['tienda']} (visto por el vigilante)" if p.get("idealo") and mp
              else f"Mercado: {p['idealo']:g} € (otras tiendas)" if p.get("idealo")
              else f"Mercado: sin comparar con otras tiendas. Idealo, a mano: {enlace_idealo(p)}")
    if p.get("_hist"):
        h = p["_hist"]
        origen = (h.get("fuente_corta") or ("PcComponentes, según PCComTracker" if p.get("_pcct_otra") else "comparador"))
        partes = ([f"mínimo {h['minimo']:g} €"] if h.get("minimo") else []) \
            + ([f"habitual {h['habitual']:g} €"] if h.get("habitual") and h.get("habitual") != h.get("minimo") else [])
        hist = f"Histórico ({origen}): " + (" · ".join(partes) or "sin datos")
    elif p.get("_min_previo") and p.get("_seguido_dias"):
        hist = f"Histórico propio: mínimo {p['_min_previo']:g} € en {p['_seguido_dias']} días"
    else:
        hist = "Histórico: sin verificar"
    idealo = f"{idealo}\n{hist}"
    ch = {"no": "no aparece en la copia local, duplicado sin revisar",
          "?": "duplicado sin revisar"}.get(p.get("ch"), p.get("ch", "").replace(":", " a ") + " €")
    baja = " · bajada" if p.get("estado") == "bajada" else ""
    resenas = ""
    if p.get("valoracion") is not None or p.get("resenas") is not None:
        partes = []
        if p.get("valoracion") is not None:
            partes.append(f"nota {p['valoracion']:g}")
        if p.get("resenas") is not None:
            partes.append(f"{p['resenas']} reseñas/ventas")
        resenas = "\n" + " · ".join(partes)
    socio = " (precio socio)" if p.get("socio") else ""
    if p.get("error_precio") and p.get("_min_previo"):
        baja = f" · posible error de precio (antes {p['_min_previo']:g} €)"
    if p.get("gratis"):
        baja += " · GRATIS"
    if p.get("dto_real"):
        resenas += f"\nDescuento real: -{round(p['dto_real'] * 100)} % sobre {p['ref_real']}"
    h = p.get("_cm_hot")
    if h and h.get("precio"):
        resenas += f"\nYa fue chollo: {h['temp']}° a {h['precio']:g} € (hace {h['dias']} días)"
    if p.get("promo") and p.get("precio_suelto"):
        socio += (f" por unidad comprando {p.get('unidades', 2)} ({p['precio_suelto']:g} € la unidad suelta; "
                  f"{p['promo']})")
    if p.get("cupon") and p.get("precio_sin_cupon"):
        socio += f" con {p['cupon']} (sin cupón: {p['precio_sin_cupon']:g} €)"
    elif p.get("cupon_posible"):
        resenas += f"\nPosible cupón: {p['cupon_posible']}"
    if p.get("tallas_aviso"):
        resenas += "\nOjo, tallas sin comprobar: Chollometro pide 2 o más en ropa y 3 o más en calzado"
    if p.get("_detectado"):
        resenas += f"\nDetectado {p['_detectado']} · avisado {datetime.now():%d/%m %H:%M}"
    return (f"#{n} · {p['tienda']}{baja}\n{p['nombre']}\n{p['precio']:g} €{socio}{antes}{resenas}\n"
            f"{idealo}\nChollometro: {ch}\n{lineas_iguales(p)}\n"
            f"Sin verificar: envío, stock y tallas (se miran al pulsar Verificar)\n"
            f"{p.get('url') or ''}")


def token_telegram():
    """Token del bot: variable de entorno, o el .env de Hermes (no se guarda en ningún otro sitio)."""
    for var in ("VIGILANTE_TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN"):
        if os.environ.get(var):
            return os.environ[var].strip()
    for env in (BASE.parent / ".env", HERMES / ".env", Path.home() / ".hermes" / ".env"):
        try:
            for linea in env.read_text(encoding="utf-8").splitlines():
                m = re.match(r"\s*(?:export\s+)?(\w*TELEGRAM\w*TOKEN\w*)\s*=\s*[\"']?([^\"'\s#]+)", linea, re.I)
                if m:
                    return m.group(2)
        except OSError:
            continue
    return None


def _post(url, datos, timeout=20):
    from urllib.parse import urlencode
    req = Request(url, data=urlencode(datos).encode(), headers={"User-Agent": UA})
    with _abridor().open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


TEMAS = "telegram_temas.json"  # pestañas del chat: {"publicar": id, "sistema": id, "cat:tecnologia": id, ...}
NOMBRES_TEMA = {"publicar": "✅ Para publicar", "sistema": "⚠️ Sistema"}
NOMBRES_CATEGORIA = {"tecnologia": "Tecnología", "generalistas": "Generalistas", "electro_marcas": "Electro de marca",
                     "videojuegos": "Videojuegos", "supermercado": "Supermercado", "hogar": "Hogar",
                     "salud_belleza": "Salud y belleza", "moda": "Moda", "deporte": "Deporte",
                     "juguetes_bebe": "Juguetes y bebé", "radar": "Radar", "moviles": "Móviles",
                     "mascotas": "Mascotas", "viajes": "Viajes"}


def tema_id(clave):
    """message_thread_id de una pestaña ("publicar", "sistema", "cat:<categoría>"), o None (chat general)."""
    try:
        return json.loads((DATOS / TEMAS).read_text(encoding="utf-8")).get(clave)
    except (OSError, ValueError):
        return None


def crear_temas():
    """`temas`: crea en el chat del bot las pestañas que falten (18/11/2026, Zeta: "como en Instinct"):
    una por categoría con tiendas (candidatos y descartes), "✅ Para publicar" (los 6 mensajes) y "⚠️ Sistema"
    (precarga, sesiones, frenos). El bot tiene los temas activados en el chat privado (has_topics_enabled)."""
    cfg = cargar_config()
    token, chat = token_telegram(), str(cfg.get("entrega", {}).get("chat_id", ""))
    try:
        temas = json.loads((DATOS / TEMAS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        temas = {}
    quiero = dict(NOMBRES_TEMA)
    for cat, d in cfg["categorias"].items():
        if d.get("tiendas") or d.get("navegador") or cat == "videojuegos":
            quiero[f"cat:{cat}"] = "🔔 " + NOMBRES_CATEGORIA.get(cat, cat.replace("_", " ").capitalize())
    for clave, nombre in quiero.items():
        if temas.get(clave):
            continue
        try:
            r = _post(f"https://api.telegram.org/bot{token}/createForumTopic", {"chat_id": chat, "name": nombre})
        except HTTPError as e:
            r = {"description": e.read().decode("utf-8", "replace")[:200]}
        if r.get("ok"):
            temas[clave] = r["result"]["message_thread_id"]
            print(f"creada: {nombre}")
        else:
            print(f"{nombre}: {r.get('description')}")
    DATOS.mkdir(parents=True, exist_ok=True)
    (DATOS / TEMAS).write_text(json.dumps(temas, ensure_ascii=False, indent=1), encoding="utf-8")
    return temas


CASI_POR_RONDA = 3      # casi chollos por ronda y categoría en la pestaña Radar
CASI_DTO_MIN = 0.40     # descuento visible mínimo de un casi chollo...
CASI_DTO_FAMILIA = 0.25  # ...o este, si su familia se calienta mucho en esa tienda (peso 1,5 o más)
CASI_REPETIR_DIAS = 7


def modo_telegram_cfg(cfg):
    return (cfg or {}).get("entrega", {}).get("modo") == "telegram"


def avisar_casi_chollos(con, cfg, casi, maximo=CASI_POR_RONDA):
    """Pestaña 🔔 Radar (Zeta, 18/11/2026): productos con buena pinta que se rechazan SOLO por no tener contraste
    (ni mercado ni histórico). Mucho descuento visible, o bastante en una familia que se calienta en esa tienda.
    Los decide Zeta a mano: el "antes" de la tienda no está comprobado. Como mucho `maximo` por ronda, los de
    más descuento, y el mismo producto una vez por semana."""
    token, chat = token_telegram(), str(cfg.get("entrega", {}).get("chat_id", ""))
    if not (token and chat):
        return 0
    buenos = []
    for p in casi:
        d = (1 - p["precio"] / p["precio_antes"]) if p.get("precio_antes") else None
        if d is None or not (d >= CASI_DTO_MIN or (d >= CASI_DTO_FAMILIA and p.get("peso", 1) >= 1.5)):
            continue
        if not marca_y_modelo(p["nombre"], p.get("marca"))[0]:
            continue  # sin marca reconocible (bodies de bebé, un armario sin nombre): no vale la pena (18/11/2026)
        buenos.append((d, p))
    hace = (datetime.now() - timedelta(days=CASI_REPETIR_DIAS)).isoformat(timespec="minutes")
    enviados = 0
    for d, p in sorted(buenos, key=lambda x: -x[0]):
        if enviados >= maximo:
            break
        clave = f"casi:{clave_vista(p['tienda'], p)}"
        fila = con.execute("SELECT v FROM meta WHERE k=?", (clave,)).fetchone()
        if fila and fila[0] >= hace:
            continue
        texto = (f"🔎 Casi chollo · {p['tienda']}\n{p['nombre']}\n{p['precio']:g} € (antes {p['precio_antes']:g} €, "
                 f"-{d:.0%} según la tienda)\nRechazado solo por no tener contraste: sin precio de mercado ni "
                 f"histórico. El \"antes\" no está comprobado: míralo tú.\n"
                 f"Detectado {p.get('_detectado') or '?'} · descartado {datetime.now():%d/%m %H:%M}\n"
                 f"{lineas_iguales(p)}\n{enlace_limpio(p.get('url')) or ''}")
        if enviar_telegram(token, chat, texto, p.get("imagen_url"), hilo="cat:radar"):
            con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (clave, datetime.now().isoformat(timespec="minutes")))
            enviados += 1
    con.commit()
    return enviados


def enviar_telegram(token, chat_id, texto, foto=None, teclado=None, html=False, hilo=None):
    """html: el texto ya viene en HTML de Telegram (negrita real). hilo: message_thread_id del tema, o el nombre de
    una pestaña ("publicar", "sistema", "cat:<categoría>"; ver `temas`). Sin esa pestaña, al chat general."""
    base = f"https://api.telegram.org/bot{token}/"
    if hilo and not str(hilo).isdigit():
        hilo = tema_id(hilo)
    extra = {"reply_markup": teclado} if teclado else {}
    if html:
        extra["parse_mode"] = "HTML"
    if hilo:
        extra["message_thread_id"] = str(hilo)
    if foto and len(texto) <= 1024:
        try:
            if _post(base + "sendPhoto", {"chat_id": chat_id, "photo": foto, "caption": texto, **extra}).get("ok"):
                return True
        except Exception as e:  # foto que Telegram no acepta: va sin foto
            log(f"telegram foto: {str(e)[:80]}")
    if not texto.strip():
        return False  # solo era la foto y no se pudo mandar
    try:
        return bool(_post(base + "sendMessage", {"chat_id": chat_id, "text": texto[:4096],
                                                 "disable_web_page_preview": "true", **extra}).get("ok"))
    except Exception as e:
        log(f"telegram: {str(e)[:80]}")
        return False

# ---------------------------------------------------- dossier para "verifica N"

RE_AVAIL = re.compile(r'"availability"\s*:\s*"([^"]+)"', re.I)
RE_SELLER = re.compile(r'"seller"\s*:\s*\{[^{}]*?"name"\s*:\s*"([^"]{2,60})"', re.I)
RE_OG_IMG = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', re.I)
RE_OG_TITULO = re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I)
RE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
RE_H1 = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
# Muros antibot y páginas de comprobación: un 200 con esto NO verifica nada.
RE_ANTIBOT = re.compile(r"captcha-delivery\.com|datadome|cf-chl|challenge-platform|just a moment\.\.\.|"
                        r"attention required|px-captcha|_incapsula_|access denied|verify you are (?:a )?human|"
                        r"comprueba que eres humano|no eres un robot", re.I)


def validar_ficha(ficha, candidato):
    """Manual de Instinct (paso 3): validada solo si la ficha real enseña el producto esperado (título),
    con precio, sin muro antibot y, si hay EAN en los dos lados, el mismo EAN. -> (bool, motivo)"""
    if ficha.get("error"):
        return False, ficha["error"]
    if ficha.get("antibot"):
        return False, "muro antibot o página de comprobación"
    if not ficha.get("precio_ahora"):
        return False, "la ficha no muestra el precio"
    titulo = ficha.get("titulo") or ""
    if titulo and not mismo_post(candidato["n"], titulo) and parecido(titulo, candidato["n"]) < 0.5:
        return False, f"el título de la ficha no es el producto esperado ({titulo[:60]})"
    if not titulo:
        return False, "la ficha no muestra el título del producto"
    e1, e2 = str(candidato.get("e") or ""), str(ficha.get("ean") or "")
    if e1 and e2 and e1.lstrip("0") != e2.lstrip("0"):
        return False, f"EAN distinto ({e2} en la ficha, {e1} en el listado)"
    return True, "ficha real con título y precio"
def ficha_se_intenta(cfg, tienda):
    """La confirmación en ficha va por descarga normal: no se intenta en las tiendas que solo se leen con
    navegador ni en las de `ficha_no_leer` (PcComponentes: antibot; pedirle la ficha en cada candidato
    sería insistir contra su muro). En esas el aviso sale como antes y lo mira Hermes."""
    if any(tienda in c.get("navegador", []) for c in cfg.get("categorias", {}).values()):
        return False
    d = dominio_tienda(tienda)
    return not any(d == h or d.endswith("." + h) for h in cfg.get("filtros_codigo", {}).get("ficha_no_leer", []))


def confirmar_en_ficha(p, tolerancia=0.02):
    """Antes de avisar, el código abre la ficha del producto y comprueba que es ese producto y a ese
    precio. Caso real (16/11/2026): Hermes descartaba al verificar lo que el listado leía mal (mochila
    adidas a 36 € en el listado de Spartoo y a 73,51 € en su ficha, con datos de otra marca; robot
    Cecotec a 81 € en el listado y a 229 € en la ficha). -> (True | False | None, motivo).
    None = la ficha no se pudo leer (antibot, error, sin precio): no confirma ni desmiente."""
    _, sin_tallas = regla_tallas(p, None)
    if sin_tallas:
        p["tallas_aviso"] = sin_tallas  # ropa o calzado: se quita si la ficha confirma las tallas
    if (clave_producto(p.get("url")) or "").startswith("privalia:"):
        # El producto de Privalia no tiene página propia: la "ficha" sería la campaña entera (otro título, otro
        # precio) y lo descartaría. El precio ya viene de los datos de la propia campaña.
        return None, "Privalia: el producto no tiene ficha propia"
    try:
        ficha = leer_ficha(p["url"])
    except Exception as e:
        return None, f"ficha no leída: {str(e)[:80]}"
    if ficha.get("error") or ficha.get("antibot") or not ficha.get("precio_ahora"):
        return None, "la ficha no se pudo leer o no muestra precio"
    titulo = ficha.get("titulo") or ""
    if titulo and not mismo_post(p["nombre"], titulo) and parecido(titulo, p["nombre"]) < 0.5:
        return False, f"la ficha es de otro producto ({titulo[:60]})"
    if ficha["precio_ahora"] > p["precio"] * (1 + tolerancia):
        return False, f"la ficha marca {ficha['precio_ahora']} € y el listado {p['precio']} €"
    if ficha.get("stock") == "no":
        return False, "la ficha lo da por agotado"
    # Reglas de publicación de Chollometro (docs/REGLAS_CHOLLOMETRO.md): las rechazan sin discusión.
    if ficha.get("una_unidad"):
        return False, "solo queda una unidad (Chollometro no la acepta)"
    if ficha.get("no_envia_es"):
        return False, "no se envía a España (Chollometro no la acepta)"
    ok_tallas, motivo = regla_tallas(p, ficha)
    if ok_tallas is False:
        return False, motivo
    if ok_tallas:
        p.pop("tallas_aviso", None)
    return True, "producto y precio confirmados en la ficha"


RE_META_DESC = re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{20,})', re.I)
RE_AGOTADO = re.compile(r"agotado|sin stock|no disponible|out of stock|avísame cuando", re.I)
RE_ENVIO = re.compile(r"[^.<>\n]{0,60}(?:env[ií]o|entrega)[^.<>\n]{0,80}", re.I)
# Reglas de publicación de Chollometro (18/11/2026, docs/REGLAS_CHOLLOMETRO.md): no se aceptan ofertas de una sola
# unidad ni productos que no se envían a España.
RE_UNA_UNIDAD = re.compile(r"\b(?:s[oó]lo|[uú]nicamente)\s+queda\s+1\b|\b[uú]ltima\s+unidad\b|\bqueda\s+1\s+unidad\b|"
                           r"\b1\s+unidad\s+disponible\b|\bonly\s+1\s+left\b", re.I)
RE_NO_ENVIA_ES = re.compile(r"no\s+(?:se\s+)?(?:env[ií]a|realiza(?:mos)?\s+env[ií]os|hace(?:mos)?\s+env[ií]os|"
                            r"est[aá]\s+disponible\s+para\s+env[ií]o)\s+a\s+(?:espa[ñn]a|tu\s+pa[ií]s|tu\s+ubicaci[oó]n)|"
                            r"no\s+se\s+puede\s+enviar\s+a\s+(?:espa[ñn]a|tu\s+pa[ií]s)|does\s+not\s+ship\s+to\s+spain", re.I)


def leer_ficha(url, nav=None):
    """Datos de la ficha que se pueden sacar sin IA."""
    if nav is not None:
        html, final, datos = nav.leer(url)
    else:
        (html, final), datos = descargar(url), []
    return analizar_ficha(html, final, datos)


def analizar_ficha(html, final, datos=()):
    plano = re.sub(r"<script\b.*?</script>|<style\b.*?</style>|<[^>]+>", " ", html, flags=re.S | re.I)
    plano = " ".join(plano.split())
    f = {"url_final": final}
    ps = extraer(html, final, datos)
    if ps:
        principal = ps[0]
        f["precio_ahora"] = principal["precio"]
        if principal["precio_antes"]:
            f["precio_antes"] = principal["precio_antes"]
        if principal["ean"]:
            f["ean"] = principal["ean"]
    m = RE_AVAIL.search(html)
    if m:
        f["stock"] = "sí" if re.search(r"instock|limitedavailability|preorder", m.group(1), re.I) else "no"
    elif RE_AGOTADO.search(plano):
        f["stock"] = "posiblemente agotado"
    m = RE_SELLER.search(html)
    if m:
        f["vendedor"] = m.group(1)
    if RE_UNA_UNIDAD.search(plano):
        f["una_unidad"] = True
    if RE_NO_ENVIA_ES.search(plano):
        f["no_envia_es"] = True
    envios = [e.strip() for e in RE_ENVIO.findall(plano) if re.search(r"gratis|€|\d", e)]
    if envios:
        f["envio"] = envios[0][:140]
    fotos = fotos_de(html, final)
    if fotos:
        f["foto"], f["fotos"] = fotos[0], fotos[:2]
    ld = productos_ld(html)
    ref = referencia_de(ld)
    if ref:
        f["referencia"] = ref
    var = variantes_de(ld, html)
    if var:
        f["variantes"] = var
    for rx in (RE_H1, RE_OG_TITULO, RE_TITLE):
        m = rx.search(html)
        if m and _texto(m.group(1)).strip():
            f["titulo"] = unescape(_texto(m.group(1)))[:200]
            break
    if (not ps or len(plano) < 1500) and RE_ANTIBOT.search(html[:20000]):
        f["antibot"] = True
    m = RE_META_DESC.search(html)
    if m:
        f["resumen"] = m.group(1)[:300]
    cupones = cupones_en(plano)
    if cupones:
        f["cupones"] = cupones
    vig = vigencia_en(plano)
    if vig:
        f["vigencia"] = vig
    specs = especificaciones(html)
    if specs:
        f["specs"] = specs
    return f


def productos_ld(html):
    """Objetos schema.org Product / ProductGroup de la ficha (JSON-LD y JSON de la página)."""
    salida = []

    def rec(o, prof=0):
        if prof > 8 or len(salida) > 40:
            return
        if isinstance(o, dict):
            t = o.get("@type")
            t = " ".join(map(str, t)) if isinstance(t, list) else str(t or "")
            if "Product" in t:
                salida.append(o)
            for x in o.values():
                rec(x, prof + 1)
        elif isinstance(o, list):
            for x in o:
                rec(x, prof + 1)
    for b in bloques_json(html):
        rec(b)
    return salida


def _urls_imagen(v):
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return _urls_imagen(v.get("url") or v.get("contentUrl"))
    if isinstance(v, list):
        return [u for x in v for u in _urls_imagen(x)]
    return []


def fotos_de(html, final):
    """Hasta 2 fotos del producto: og:image y las imágenes del Product (paso 5 de las instrucciones)."""
    cand = [m.group(1) for m in RE_OG_IMG.finditer(html)]
    for o in productos_ld(html)[:3]:
        cand += _urls_imagen(o.get("image"))
    salida = []
    for u in cand:
        u = urljoin(final, unescape(u.strip()))
        if u.startswith("http") and u not in salida:
            salida.append(u)
    return salida[:2]


def referencia_de(ld):
    for o in ld:
        for k in ("sku", "mpn", "productID", "gtin13", "gtin"):
            v = o.get(k)
            if isinstance(v, (str, int)) and str(v).strip():
                return str(v).strip()[:40]
    return None


RE_SELECT_TALLA = re.compile(r"<select\b[^>]*(?:name|id|class)=\"[^\"]*(?:talla|size|variant|color|capacidad)"
                             r"[^\"]*\"[^>]*>(.*?)</select>", re.I | re.S)
RE_OPTION = re.compile(r"<option\b([^>]*)>(.*?)</option>", re.I | re.S)


def variantes_de(ld, html):
    """Tallas o variantes con su stock: de los datos (hasVariant / varias ofertas) o del desplegable de tallas.
    -> ["42: hay stock", "43: agotada", ...] (máx. 25)."""
    salida, vistas = [], set()

    def stock(of):
        of = of[0] if isinstance(of, list) and of else of
        disp = str((of or {}).get("availability", "")) if isinstance(of, dict) else ""
        if not disp:
            return None
        return "hay stock" if re.search(r"instock|limitedavailability|preorder", disp, re.I) else "agotada"

    def poner(nombre, est):
        nombre = " ".join(str(nombre).split())[:40]
        if nombre and nombre.lower() not in vistas:
            vistas.add(nombre.lower())
            salida.append(f"{nombre}: {est}" if est else nombre)

    for o in ld:
        for var in o.get("hasVariant") or []:
            if isinstance(var, dict):
                poner(var.get("size") or var.get("color") or var.get("name") or var.get("sku"), stock(var.get("offers")))
        ofertas = o.get("offers")
        if isinstance(ofertas, list) and len(ofertas) > 1:
            for of in ofertas:
                if isinstance(of, dict) and (of.get("name") or of.get("sku")):
                    poner(of.get("name") or of.get("sku"), stock(of))
    if not salida:
        for sel in RE_SELECT_TALLA.findall(html):
            for attrs, texto in RE_OPTION.findall(sel):
                texto = unescape(_texto(texto)).strip()
                if not texto or re.search(r"elige|selecciona|choose|select", texto, re.I) or 'value=""' in attrs:
                    continue
                agotada = re.search(r"\bdisabled\b", attrs, re.I) or RE_AGOTADO.search(texto)
                poner(re.sub(r"\s*[-(]?\s*(?:agotad[oa]|sin stock|no disponible)\)?", "", texto, flags=re.I),
                      "agotada" if agotada else "seleccionable")  # el desplegable no dice si hay stock
    return salida[:25]


RE_CALZADO = re.compile(r"zapatill|zapato|\bbotas?\b|botin|sandalia|chancla|deportivas|sneaker|mocas[ií]n|alpargata|"
                        r"n[aá]uticos?|tacones", re.I)
RE_TALLA = re.compile(r"^(?:talla\s+)?(?:\d{2}(?:[.,]5)?(?:\s*(?:eu|1/3|2/3))?|\d{1,2}(?:[.,]5)?\s*(?:uk|us)|"
                      r"x{0,3}s|m|x{0,4}l|\d?xl|\d{2}-\d{2}|\d{2,3}\s*cm|w\d{2}(?:\s*l\d{2})?|u|única|unica)$", re.I)


def regla_tallas(p, ficha):
    """Reglas de Chollometro (8 y "difícil acceso", 18/11/2026): ropa con 2 tallas o más disponibles y calzado con
    3 o más; con menos, no se acepta suelta. -> (True | False | None, motivo). None: no es ropa ni calzado, o la
    ficha no dice las tallas (el aviso lo indica para comprobarlo antes de publicar)."""
    fam = familia_de(p["nombre"])
    tipo = "calzado" if fam == "calzado" or RE_CALZADO.search(p["nombre"]) else "ropa" if fam == "ropa" else None
    if not tipo:
        return None, ""
    tallas = [x for x in (ficha or {}).get("variantes") or [] if RE_TALLA.match(x.split(":")[0].strip())]
    if not tallas:
        return None, f"{tipo}: tallas sin comprobar"
    if any(re.sub(r"^talla\s+", "", x.split(":")[0].strip().lower()) in ("u", "única", "unica") for x in tallas):
        return True, "talla única"
    disponibles = [x for x in tallas if not x.endswith(": agotada")]
    minimo = 3 if tipo == "calzado" else 2
    if len(disponibles) < minimo:
        return False, f"{tipo} con {len(disponibles)} tallas disponibles (Chollometro pide {minimo} o más)"
    return True, f"{len(disponibles)} tallas disponibles"


RE_CUPON = re.compile(r"(?:c[oó]digo|cup[oó]n|code)(?:\s+(?:promocional|descuento|de descuento))?\s*[:\-«\"']?\s*"
                      r"([A-Z0-9][A-Z0-9-]{3,19})\b", re.I)
RE_VIGENCIA = re.compile(r"((?:hasta|v[aá]lid[oa]s? hasta|termina|finaliza|vigente hasta|del \d{1,2}[/-]\d{1,2} al)"
                         r"[^.;\n]{0,12}?(?:el\s+)?\d{1,2}(?:[/-]\d{1,2}(?:[/-]\d{2,4})?|\s+de\s+[a-záéíóú]+)"
                         r"(?:\s+(?:a las\s+)?\d{1,2}:\d{2})?)", re.I)


def cupones_en(texto):
    """Códigos de descuento que aparecen en la ficha (los verifica la IA)."""
    vistos = []
    for m in RE_CUPON.finditer(texto):
        c = m.group(1)
        # un código real suele mezclar mayúsculas y números, o ser todo mayúsculas
        if (c.isupper() or any(ch.isdigit() for ch in c)) and not c.isdigit() and c not in vistos:
            vistos.append(c)
    return vistos[:3]


def vigencia_en(texto):
    m = RE_VIGENCIA.search(texto)
    return " ".join(m.group(1).split())[:80] if m else None


RE_FILA_TABLA = re.compile(r"<tr[^>]*>\s*<t[hd][^>]*>(.*?)</t[hd]>\s*<td[^>]*>(.*?)</td>", re.S | re.I)
RE_DT_DD = re.compile(r"<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", re.S | re.I)


def especificaciones(html, maximo=15):
    """Pares característica: valor de la ficha (JSON-LD additionalProperty o tablas), para que la
    descripción se base en datos reales."""
    pares = []
    for bloque in bloques_json(html):
        _props(bloque, pares, [0])
        if len(pares) >= maximo:
            break
    if len(pares) < 3:
        for k, val in RE_FILA_TABLA.findall(html) + RE_DT_DD.findall(html):
            k, val = _texto(k), _texto(val)
            if 1 < len(k) <= 40 and 0 < len(val) <= 80:
                pares.append(f"{k}: {val}")
    vistos, salida = set(), []
    for x in pares:
        if x.lower() not in vistos:
            vistos.add(x.lower())
            salida.append(x)
    return salida[:maximo]


def _props(obj, salida, cont):
    cont[0] += 1
    if cont[0] > 50_000:
        return
    if isinstance(obj, dict):
        ap = obj.get("additionalProperty")
        if isinstance(ap, list):
            for x in ap:
                if isinstance(x, dict) and x.get("name") and x.get("value") not in (None, ""):
                    salida.append(f"{x['name']}: {x['value']}"[:120])
        for val in obj.values():
            if isinstance(val, (dict, list)):
                _props(val, salida, cont)
    elif isinstance(obj, list):
        for val in obj:
            _props(val, salida, cont)


RE_MINIMO = re.compile(r"(m[ií]nimo hist[oó]rico|precio m[ií]nimo|m[ií]nimo|lowest price|historical low|historic low)"
                       r"[^0-9€]{0,40}(\d{1,5}(?:[.,]\d{1,2})?)\s?€", re.I)


def minimo_en_texto(html):
    """Busca 'mínimo histórico ... X €' en una página de comparador."""
    texto = _texto(re.sub(r"<script\b.*?</script>|<style\b.*?</style>", " ", html, flags=re.S | re.I))
    m = RE_MINIMO.search(texto)
    return a_numero(m.group(2)) if m else None


def historico_generico(plantilla, nombre, ean=None):
    """Busca el producto en un comparador y lee su mínimo histórico. Dos pasos: búsqueda y, si
    hace falta, la ficha del resultado que más se parece. Devuelve (precio, url) o (None, None)."""
    url = plantilla.format(q=quote_plus(ean or nombre))
    html, final = descargar(url)
    minimo = minimo_en_texto(html)
    if minimo:
        return minimo, final
    parser = Enlaces()
    parser.feed(html)
    host = urlparse(final).netloc
    mejor, puntos = None, 0.6
    for href, texto in parser.enlaces:
        if not href or not texto:
            continue
        absoluta = urljoin(final, href)
        if urlparse(absoluta).netloc != host:
            continue
        pt = parecido(nombre, texto)
        if pt >= puntos:
            mejor, puntos = absoluta, pt
    if not mejor:
        return None, None
    html, final = descargar(mejor)
    return minimo_en_texto(html), final


def itad_minimo(nombre, clave):
    """Mínimo histórico de un juego de PC en IsThereAnyDeal (API gratuita con clave)."""
    txt, _ = descargar(f"https://api.isthereanydeal.com/games/lookup/v1?key={clave}&title={quote_plus(nombre)}")
    juego = (json.loads(txt) or {}).get("game") or {}
    if not juego.get("id"):
        return None, None
    req = Request(f"https://api.isthereanydeal.com/games/historylow/v1?key={clave}&country=ES",
                  data=json.dumps([juego["id"]]).encode(), headers={"Content-Type": "application/json", "User-Agent": UA})
    with _abridor().open(req, timeout=20) as r:
        datos = json.loads(r.read().decode("utf-8"))
    low = (datos[0].get("low") if datos else None) or {}
    precio = (low.get("price") or {}).get("amount")
    return (float(precio) if precio is not None else None), f"https://isthereanydeal.com/game/{juego.get('slug', '')}/"


def verificador_de(cfg, item):
    """Qué verificador de histórico toca para este candidato (el mismo orden que la habilidad)."""
    t = item.get("t", "")
    reglas = cfg.get("historicos", {})
    for clave, nombre in reglas.get("por_tienda", {}).items():
        if clave in t:
            return nombre
    cat = item.get("cat")
    return reglas.get("por_categoria", {}).get(cat) or reglas.get("defecto", "precioreal")


def historico(cfg, item):
    """Mínimo histórico por código. Si no se puede, devuelve None y lo busca la IA."""
    v = verificador_de(cfg, item)
    try:
        if item.get("h"):  # ficha del comparador ya conocida (PcComponentes leída por PCComTracker)
            r = historico_comparador(item["h"])
            if not r:
                return None
            fuente = urlparse(r["url"]).netloc.replace("www.", "")
            if "pccomtracker" in fuente and "pccomponentes" not in (item.get("t") or "pccomponentes"):
                fuente = "PcComponentes, según PCComTracker"  # producto de otra tienda: el histórico es de PcComponentes
            salida = {"minimo": r["minimo"], "fuente": fuente,
                      "url": r["url"], "es_minimo": item.get("p") is not None and item["p"] <= r["minimo"] * 1.03}
            if r.get("habitual"):
                salida["habitual"] = r["habitual"]
            if r.get("minimo_fecha"):
                salida["minimo_fecha"] = r["minimo_fecha"]
            return salida
        if v == "isthereanydeal":
            clave = os.environ.get("ITAD_KEY") or cfg.get("historicos", {}).get("itad_key")
            if not clave:
                return None
            precio, url = itad_minimo(item["n"], clave)
        else:
            plantilla = cfg.get("historicos", {}).get("busquedas", {}).get(v)
            if not plantilla:
                return None
            precio, url = historico_generico(plantilla, item["n"], item.get("e"))
    except Exception as e:
        log(f"histórico {v} {item.get('n', '')[:40]}: {e}")
        return None
    if precio is None:
        return None
    return {"minimo": precio, "fuente": v, "url": url,
            "es_minimo": item.get("p") is not None and item["p"] <= precio * 1.03}


def specs_comparador(p):
    """Características del MISMO modelo en la ficha de PrecioReal, para cuando la tienda no las da o no
    se puede leer (PcComponentes). -> {"specs": [...], "url": ficha} o None. Nada de modelos parecidos:
    con mismo_modelo, un Book6 no recibe las características del Book6 Pro."""
    if not identificable(p):
        return None
    try:
        html, url = descargar(PRECIOREAL_BUSQUEDA.format(q=quote_plus(consulta_comparador(p))))
        nombre = nombre_con_marca(p)
        ficha = next((r["url"] for r in extraer(html, url) if r["url"] and mismo_modelo(nombre, r["nombre"])), None)
        if not ficha:
            return None
        html, final = descargar(ficha)
        specs = especificaciones(html)
        return {"specs": specs, "url": final} if specs else None
    except Exception as e:
        log(f"specs precioreal {p['nombre'][:50]}: {e}")
        return None


def dossier(ids):
    """Lo que necesita la IA para 'verifica N', preparado por código (menos tokens, menos errores)."""
    print(json.dumps(dossier_items(ids), ensure_ascii=False, separators=(",", ":")))


def dossier_items(ids, ficha_manual=None):
    """Los datos de cada candidato, con su ficha leída en vivo: lo usan `dossier` (para la IA) y `decidir`.
    ficha_manual: precio, stock y vendedor que ha leído Hermes con su navegador en una tienda que el script
    no puede abrir (PcComponentes: 403). Entonces no se pide la ficha y se usan esos datos."""
    cfg = cargar_config()
    con_nav = {t for c in cfg["categorias"].values() for t in c.get("navegador", [])}
    con = bd()
    con_nav |= {k.split(":", 1)[1] for (k,) in con.execute("SELECT k FROM meta WHERE k LIKE 'a_navegador:%'")}
    # "num" es el número de la lista (#N); "id" dentro de cada candidato es el precio de Idealo.
    salida, filas = [], []
    for n in ids:
        fila = con.execute("SELECT id, base, datos, fecha FROM lista WHERE id=?", (int(n),)).fetchone()
        salida.append({"num": int(n), "error": "no existe"} if not fila else None)
        if fila:
            filas.append((len(salida) - 1, *fila))

    def con_lector(nav):
        for pos, i, base, datos, fecha in filas:
            c = json.loads(datos)
            item = {"num": i, **c, "detectada": fecha}
            m = con.execute("SELECT veredicto, motivo, fecha FROM modelos WHERE base=?", (base,)).fetchone()
            if m:
                item["memoria"] = f"{m[0]} ({m[2][:10]}): {m[1]}"
            if c.get("u"):
                # La ficha de la tienda puede fallar (PcComponentes: antibot); lo demás se hace igual.
                if ficha_manual:
                    item["ficha"] = {"titulo": c["n"], "fuente": "leída por Hermes con su navegador",
                                     **{k: x for k, x in ficha_manual.items() if x is not None}}
                    item["validada"], item["validacion"] = True, "ficha leída por Hermes con su navegador"
                else:
                    try:
                        usar_nav = nav if c.get("t") in con_nav else None
                        item["ficha"] = leer_ficha(c["u"], usar_nav)
                    except Exception as e:
                        item["ficha"] = {"error": f"no se pudo leer la ficha de la tienda: {str(e)[:80]}"}
                    # Manual de Instinct: un 200 con página genérica, un shell de JavaScript o un muro antibot no
                    # verifica nada. Si no se ve el producto esperado con su precio: "sin validar" y se retiene.
                    item["validada"], item["validacion"] = validar_ficha(item["ficha"], c)
                item["comprobado"] = ahora_madrid().strftime("%d/%m %H:%M")  # stock y precio caducan en minutos
                item["buscar_chollometro"] = busquedas_chollometro(c)
                if cfg.get("filtros_codigo", {}).get("buscar_en_chollometro", True):
                    buscar_en_chollometro(con, {"nombre": c["n"], "tienda": c["t"]})
                try:
                    ahora = item["ficha"].get("precio_ahora")
                    c_ch = estado_chollometro({"nombre": c["n"], "tienda": c["t"], "precio": ahora or c["p"]})
                    item["ch_ahora"] = c_ch
                    item["antecedentes"] = antecedentes_chollometro(c["n"])
                    item["mejor_cm"] = mejor_chollometro(c["n"])
                    item["minimo_propio"] = minimo_con_fecha(con, clave_vista(c["t"], {"url": c.get("u"),
                                                                                       "nombre": c["n"]}))
                    if c_ch == "misma":
                        item["aviso_ch"] = "ya está publicado en Chollometro en esta tienda: descártalo"
                    if ahora and abs(ahora - c["p"]) > 0.01:
                        item["aviso"] = f"el precio ha cambiado: {c['p']:g} € -> {ahora:g} €"
                    h = historico(cfg, {**c, "p": ahora or c["p"]})
                    if not h:
                        hp = habitual_propio(con, clave_vista(c["t"], {"url": c.get("u"), "nombre": c["n"]}))
                        if hp:  # sin comparador: el precio habitual que ha visto el propio vigilante
                            h = {"minimo": hp["minimo"], "habitual": hp["habitual"], "fuente": hp["fuente"],
                                 "es_minimo": (ahora or c["p"]) <= hp["minimo"] * 1.03}
                        elif c.get("t") == "amazon.es" and (cc := camel_guardado(con, c.get("u"))):
                            h = {"minimo": cc["minimo"], "habitual": cc.get("media"), "fuente": "CamelCamelCamel",
                                 "minimo_fecha": cc.get("minimo_fecha"),
                                 "es_minimo": (ahora or c["p"]) <= cc["minimo"] * 1.03}
                        elif c.get("rt") in REF_TIENDA_HISTORICO and c.get("a"):
                            # Amazon: "Mediano" (90 días) o "Más bajo" (30 días) de su precio tachado.
                            minimo = c["a"] if c["rt"] == "mas_bajo" else None
                            h = {"minimo": minimo, "habitual": c["a"], "fuente": REF_TIENDA_HISTORICO[c["rt"]],
                                 "es_minimo": bool(minimo and (ahora or c["p"]) <= minimo * 1.03),
                                 "min_30d": c["rt"] == "mas_bajo"}
                    if h:
                        item["historico"] = h
                except Exception as e:
                    log(f"dossier #{i}: {e}")
                if len(item["ficha"].get("specs") or []) < 4:
                    extra = specs_comparador({"nombre": c["n"], "precio": c["p"], "tienda": c["t"],
                                              "marca": None, "ean": c.get("e")})
                    if extra:
                        item["ficha"]["specs"] = (item["ficha"].get("specs") or []) + extra["specs"]
                        item["ficha"]["specs_fuente"] = extra["url"]
            salida[pos] = item
    if not ficha_manual and any(json.loads(d).get("t") in con_nav for _, _, _, d, _ in filas):
        try:
            with Navegador(cfg) as nav:
                con_lector(nav)
        except ImportError:
            con_lector(None)
    else:
        con_lector(None)
    con.close()
    return salida


# ---------------------------------------------- decisión por código (Modo Clásico)
# Zeta (17/11/2026): el "verifica" no tiene que pasar entero por una IA. Los filtros son una tabla de
# porcentajes: los aplica el código, sin interpretaciones (el #189, un 25 % bajo su habitual, lo descartó
# la IA leyendo mal una frase). La IA solo redacta el título y la descripción de lo que pasa.
# Guía "Cómo leer y comprobar ofertas" del 18/11/2026 (Zeta: "solo aplica la comprobación y los filtros en modo
# clásico"). subiendo/destacados: bajada sobre el habitual acreditado; mercado: ventaja frente a otras tiendas.
# Moda: además, descuento anunciado por la tienda (40 %, 30 % en marcas con gancho; 50 % en DESTACADOS) y tallas.
# Cosmética: "solo grandes chollos" (el listón de DESTACADOS de consumibles para todo).
CLASICO = {
    "tec": {"subiendo": 0.20, "destacados": 0.25, "mercado": 0.20},
    "consumible": {"subiendo": 0.20, "destacados": 0.30, "mercado": 0.20},
    "cosmetica": {"subiendo": 0.30, "destacados": 0.30, "mercado": 0.30},
    "moda": {"subiendo": 0.15, "destacados": 0.25, "mercado": 0.15, "mercado_destacados": 0.25,
             "anunciado": 0.40, "anunciado_gancho": 0.30, "anunciado_destacados": 0.50},
    "digital": {"subiendo": 0.80, "destacados": 0.80, "mercado": 0.80},
}
TALLAS_SUBIENDO = {"calzado": 3, "ropa": 3}
TALLAS_DESTACADOS = {"calzado": 4, "ropa": 3}
MARCA_GANCHO_HILOS = 30  # hilos de 200° o más en la copia para que una marca de moda "tenga gancho"
SOBRE_MINIMO_SUBIENDO, SOBRE_MINIMO_DESTACADOS = 0.15, 0.08
RE_TIENDA_DIGITAL = re.compile(r"steampowered|epicgames|gog\.com|humblebundle|instant-gaming|eneba|gamesplanet|"
                               r"store\.playstation|nintendo\.com|ubisoft|xbox\.com", re.I)


def clase_clasico(cfg, tienda, nombre):
    """A qué fila de la tabla del Modo Clásico pertenece un candidato: tec, consumible, moda o digital."""
    if RE_TIENDA_DIGITAL.search(tienda or ""):
        return "digital"
    cat = next((c for c, d in (cfg or {}).get("categorias", {}).items()
                if tienda in d.get("tiendas", []) + d.get("navegador", []) + d.get("apis", [])), None)
    fam = familia_de(nombre)
    if fam in ("calzado", "ropa", "bolsos") or cat == "moda":
        return "moda"
    if fam == "perfumes" or (cat == "salud_belleza" and fam not in ("higiene", "alimentacion", "bebidas")):
        return "cosmetica"
    if fam in ("higiene", "alimentacion", "bebidas") or cat in ("supermercado", "salud_belleza"):
        return "consumible"
    return "tec"


def umbrales_clasico(cfg, tienda, nombre):
    """(clase, umbrales) del Modo Clásico para un producto; `filtros_codigo.clasico.<clase>` los ajusta."""
    clase = clase_clasico(cfg, tienda, nombre)
    return clase, {**CLASICO[clase], **(cfg or {}).get("filtros_codigo", {}).get("clasico", {}).get(clase, {})}


_GANCHO = {}


def marca_con_gancho(nombre):
    """¿La marca del producto tiene gancho en Chollometro (al menos MARCA_GANCHO_HILOS hilos de 200° en la copia)?
    Para la excepción de moda del 30 % anunciado."""
    rx = rx_marcas()
    marcas = set(rx.findall(sin_tildes((nombre or "").lower()))) if rx else set()
    if not marcas:
        return False
    if DB not in _GANCHO:
        _GANCHO[DB] = {}
    cache = _GANCHO[DB]
    for m in marcas:
        if m not in cache:
            try:
                con = bd()
                try:
                    cache[m] = con.execute("SELECT COUNT(*) FROM cm WHERE temp >= 200 AND lower(titulo) LIKE ?",
                                           (f"%{m}%",)).fetchone()[0]
                finally:
                    con.close()
            except Exception:
                cache[m] = 0
        if cache[m] >= MARCA_GANCHO_HILOS:
            return True
    return False


def anunciado_moda_ok(u, nombre, impreso, destacados=False):
    """Moda: el descuento anunciado por la tienda llega al de la guía (40 %, 30 % con gancho, 50 % en DESTACADOS)."""
    if impreso is None:
        return False
    if destacados:
        return impreso >= u["anunciado_destacados"] - 0.005
    minimo = u["anunciado_gancho"] if marca_con_gancho(nombre) else u["anunciado"]
    return impreso >= minimo - 0.005


def historico_clasico_ok(precio, habitual, minimo, u):
    """Con histórico: al menos `subiendo` bajo el habitual acreditado y, si hay mínimo histórico, como mucho un 15 %
    por encima de él."""
    if not habitual or precio > habitual * (1 - u["subiendo"] + 0.005):
        return False
    return not minimo or precio <= minimo * (1 + SOBRE_MINIMO_SUBIENDO)


def _pct(x):
    return f"{round(x * 100)} %"


def decidir_item(item, cfg=None):
    """Aplica los filtros del Modo Clásico a un candidato del dossier. Sin IA y sin red: solo cuentas.
    -> {"num", "veredicto": "pasa" | "descartado" | "sin_validar", "motivo", y si pasa "etiqueta", "prueba",
    "precio"}. "memoria" dice qué veredicto guardar para el modelo: dudosa (bloquea 30 días) o caducada."""
    def fin(veredicto, motivo, **mas):
        return {"num": item.get("num"), "veredicto": veredicto, "motivo": motivo, **mas}
    if item.get("error"):
        return fin("descartado", str(item["error"]))
    ficha = item.get("ficha") or {}
    p0, ahora = item.get("p"), ficha.get("precio_ahora")
    if ahora and item.get("ps") and p0:
        ahora = round(ahora * p0 / item["ps"], 2)  # multicompra: la ficha da la unidad suelta; se aplica la promoción
    elif ahora and item.get("psc") and p0:
        ahora = round(ahora * p0 / item["psc"], 2)  # cupón: la ficha da el precio sin cupón; se aplica el cupón
    if str(item.get("memoria", "")).startswith("dudosa"):
        return fin("descartado", f"ya descartado antes: {item['memoria']}")
    if not item.get("validada"):
        motivo = item.get("validacion") or "no se pudo leer la ficha"
        if ahora and ficha.get("titulo"):  # la ficha se leyó y no es el producto del aviso
            return fin("descartado", motivo, memoria="dudosa")
        return fin("sin_validar", motivo)
    precio = ahora or p0
    subio = bool(ahora and p0 and ahora > p0 * 1.02)
    mem = "caducada" if subio else "dudosa"
    cambio = f" (el aviso decía {p0:g} € y ahora está a {precio:g} €)" if subio else ""
    if ficha.get("stock") == "no":
        return fin("descartado", "la ficha lo da por agotado", memoria="caducada")
    ch = item.get("ch_ahora") or item.get("ch")
    if ch == "misma" or ya_publicado_igual_o_mas_barato(ch, precio):
        return fin("descartado", f"ya está en Chollometro ({ch}) y ahora cuesta {precio:g} €: misma tienda solo si "
                                 f"baja más de un 5 % y más de 1 €; otra tienda, si ahora es más barato", memoria="dudosa")
    clase, u = umbrales_clasico(cfg, item.get("t"), item.get("n", ""))
    fam = familia_de(item.get("n", ""))
    tallas = [v for v in ficha.get("variantes") or [] if not v.endswith("agotada")]
    pocas_tallas = False  # sin las de DESTACADOS no pasa de SUBIENDO
    if fam in ("calzado", "ropa") and ficha.get("variantes"):
        if len(tallas) < TALLAS_SUBIENDO[fam]:
            return fin("descartado", f"quedan {len(tallas)} tallas con stock y se exigen {TALLAS_SUBIENDO[fam]} "
                                     f"en {'calzado' if fam == 'calzado' else 'ropa'}", memoria="caducada")
        pocas_tallas = len(tallas) < TALLAS_DESTACADOS[fam]
    if item.get("gratis"):
        return fin("pasa", "gratis", etiqueta="SUBIENDO", prueba="gratis", precio=precio, clase=clase)
    impreso = (item["a"] - precio) / item["a"] if item.get("a") and item["a"] > precio else None
    if clase == "moda" and not anunciado_moda_ok(u, item.get("n", ""), impreso):
        return fin("descartado", f"moda: la tienda anuncia un {_pct(impreso or 0)} de descuento y se exige un "
                                 f"{_pct(u['anunciado_gancho'] if marca_con_gancho(item.get('n', '')) else u['anunciado'])}"
                                 f"{cambio}", memoria=mem)
    h = dict(item.get("historico") or {})
    if h.get("min_30d"):
        h["minimo"] = None  # un mínimo de 30 días no es el mínimo histórico (guía, 3)
    mercado, etiqueta, prueba, fallo = item.get("id"), None, None, None
    if h.get("habitual"):
        baja = (h["habitual"] - precio) / h["habitual"]
        fuente = h.get("fuente", "histórico")
        if baja < u["subiendo"] - 0.005:
            return fin("descartado", f"baja un {_pct(baja)} sobre su precio habitual ({h['habitual']:g} € en {fuente}) "
                                     f"y se exige un {_pct(u['subiendo'])}{cambio}", memoria=mem)
        sobre = (precio - h["minimo"]) / h["minimo"] if h.get("minimo") else None
        if sobre is not None and sobre > SOBRE_MINIMO_SUBIENDO:
            return fin("descartado", f"está un {_pct(sobre)} por encima de su mínimo ({h['minimo']:g} € en {fuente}) "
                                     f"y se admite hasta un {_pct(SOBRE_MINIMO_SUBIENDO)}{cambio}", memoria=mem)
        if sobre is not None and sobre <= 0.03 and mercado and precio < mercado:
            etiqueta = "EXCEPCIONAL"
        elif (baja >= u["destacados"] - 0.005 and (sobre is None or sobre <= SOBRE_MINIMO_DESTACADOS)
              and (not mercado or precio <= mercado) and not pocas_tallas
              and (clase != "moda" or anunciado_moda_ok(u, item.get("n", ""), impreso, destacados=True))):
            etiqueta = "DESTACADOS"  # y el mejor precio frente a otras tiendas
        else:
            etiqueta = "SUBIENDO"
        prueba = (f"histórico de {fuente}: habitual {h['habitual']:g} €"
                  + (f", mínimo {h['minimo']:g} €" if h.get("minimo") else "")
                  + f", ahora {precio:g} € ({_pct(baja)} menos)")
    elif mercado:
        ventaja = (mercado - precio) / mercado
        if ventaja >= u["mercado"] - 0.005:
            if h.get("minimo") and precio <= h["minimo"] * 1.03:
                etiqueta = "EXCEPCIONAL"
            elif clase == "moda" and not pocas_tallas and anunciado_moda_ok(u, item.get("n", ""), impreso, True) \
                    and ventaja >= u.get("mercado_destacados", 1) - 0.005:
                etiqueta = "DESTACADOS"
            elif clase != "moda" and ventaja >= u["destacados"] - 0.005:
                etiqueta = "DESTACADOS"
            else:
                etiqueta = "SUBIENDO"
            prueba = (f"otras tiendas: el siguiente precio es {mercado:g} € y aquí está a {precio:g} € "
                      f"({_pct(ventaja)} menos)"
                      + (f"; mínimo histórico {h['minimo']:g} € en {h.get('fuente', 'histórico')}" if h.get("minimo") else ""))
        else:
            fallo = (f"solo un {_pct(ventaja)} por debajo del mercado ({mercado:g} €) "
                     f"y se exige un {_pct(u['mercado'])}{cambio}")
    if not etiqueta and item.get("hot") and not subio:
        etiqueta, prueba = "SUBIENDO", f"ya fue chollo caliente en Chollometro: {item['hot']}"
    if not etiqueta and clase == "moda" and not mercado and item.get("a") and (item["a"] - precio) / item["a"] >= 0.70:
        etiqueta = "SUBIENDO"
        prueba = (f"moda sin comparador: {_pct((item['a'] - precio) / item['a'])} sobre el precio de tienda "
                  f"({item['a']:g} €), que no es una referencia independiente")
    if not etiqueta and not fallo and item.get("dr") and not subio:
        m = re.match(r"-(\d+) % sobre (.+)", str(item["dr"]))
        if m and int(m.group(1)) / 100 >= u["subiendo"] - 0.005:
            etiqueta, prueba = "SUBIENDO", f"{m.group(1)} % menos sobre {m.group(2)}, calculado en la ronda"
    if not etiqueta:
        return fin("descartado", fallo or f"sin prueba de ventaja: no hay histórico, ni otra tienda más cara, "
                                          f"ni antecedente en Chollometro{cambio}", memoria=mem)
    return fin("pasa", prueba, etiqueta=etiqueta, prueba=prueba, precio=precio, clase=clase)


def eur(x):
    """44.95 -> '44,95 €'; 60.0 -> '60 €'; 1100 -> '1.100 €'."""
    if x is None:
        return "?"
    entero = abs(x - round(x)) < 0.005
    txt = f"{x:,.0f}" if entero else f"{x:,.2f}"
    return txt.replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def limpiar_envio(txt):
    """'Envío: Desde 15,95 € Devolución: Gratuita 30 días Garantía...' -> 'Desde 15,95 €'."""
    if not txt:
        return None
    txt = re.sub(r"^\s*(?:env[ií]o|gastos de env[ií]o|portes)\s*:?\s*", "", str(txt), flags=re.I)
    txt = re.split(r"\s+(?:devoluci[oó]n|garant[ií]a|recogida|entrega estimada)\b", txt, flags=re.I)[0]
    return txt.strip(" .;,")[:90] or None


def _hora_entrega(detectada):
    """'🕒 Detectada 15:31 · Enviada 15:39 del 17/11/2026' (con la fecha de detección si fue otro día)."""
    ahora = ahora_madrid()
    try:
        det = datetime.fromisoformat(detectada)
        txt = det.strftime("%H:%M") if det.date() == ahora.date() else det.strftime("%d/%m %H:%M")
    except (TypeError, ValueError):
        txt = "?"
    return f"🕒 Detectada {txt} · Enviada {ahora:%H:%M} del {ahora:%d/%m/%Y}"


def bloques_de_codigo(item, d):
    """Los bloques de la entrega que no necesitan redacción (precio, INFO ADICIONAL y enlace) y los datos para
    que la IA, o la plantilla, escriban el título y la descripción. Reglas de formato de Zeta (17/11/2026):
    resultado, no proceso; referencias bien etiquetadas (PVP no es habitual); lo desconocido se dice."""
    ficha, h = item.get("ficha") or {}, item.get("historico") or {}
    precio = d["precio"]
    fuente = h.get("fuente", "histórico")
    tallas = [v for v in ficha.get("variantes") or [] if not v.endswith("agotada")]
    if familia_de(item.get("n", "")) not in ("ropa", "calzado"):
        tallas = []  # en un televisor, las "variantes" de la ficha eran su propio nombre
    nombres_tallas = [v.split(":")[0] for v in tallas[:15]]
    envio = limpiar_envio(ficha.get("envio"))
    pvp = item["a"] if item.get("a") and item["a"] > precio else None
    if item.get("rt") in REF_TIENDA_HISTORICO and h.get("habitual") == item.get("a"):
        pvp = None  # el tachado de Amazon ya va como su histórico ("Mediano" o "Más bajo"), no como PVP
    # 4 · Precio: oferta, referencia acreditada y portes, por separado. El total solo si está comprobado.
    precios = [eur(precio)]
    promo, uds = item.get("promo"), item.get("uds") or 2
    suelto = round(precio * item["ps"] / item["p"], 2) if promo and item.get("ps") and item.get("p") else None
    if suelto:
        precios = [f"{eur(precio)} por unidad comprando {uds} ({promo})",
                   f"Unidad suelta: {eur(suelto)}. Total por {uds}: {eur(round(precio * uds, 2))}."]
    elif item.get("cup") and item.get("psc"):
        precios = [f"{eur(precio)} con {item['cup']}", f"Sin cupón: {eur(item['psc'])}."]
    if h.get("habitual") and item.get("rt") == "mas_bajo" and h["habitual"] == item.get("a"):
        precios.append(f"Precio más bajo de los últimos 30 días: {eur(h['habitual'])} (según Amazon)")
    elif h.get("habitual"):
        precios.append(f"Precio habitual: {eur(h['habitual'])} (en {fuente})")
    if pvp:
        precios.append(f"PVP de la tienda: {eur(pvp)}")
    precios.append(f"Envío: {envio}" if envio else "Envío: portes no verificados.")
    # 5 · INFO ADICIONAL
    info = [f"🏷️ {d['etiqueta']}", ""]
    if h.get("habitual") and h["habitual"] > precio:
        info.append(f"💶 {eur(precio)} frente a su precio habitual de {eur(h['habitual'])} en {fuente}: "
                    f"-{round((h['habitual'] - precio) / h['habitual'] * 100)} %.")
    elif pvp:
        info.append(f"💶 {eur(precio)} frente a PVP de {eur(pvp)}: -{round((pvp - precio) / pvp * 100)} % sobre "
                    f"esa referencia, no sobre habitual probado.")
    else:
        info.append(f"💶 {eur(precio)}.")
    if suelto:
        info.append(f"💶 Multicompra {promo}: {eur(precio)} por unidad comprando {uds}; suelta, {eur(suelto)}.")
    info += ["", f"🚚 Envío: {envio}." if envio else "🚚 Portes no verificados: total según cesta.", "",
             _hora_entrega(item.get("detectada")), ""]
    if h.get("minimo"):
        fm = h.get("minimo_fecha") or ""
        cuando = f" ({fm[8:10]}/{fm[5:7]}/{fm[:4]})" if len(fm) >= 10 else ""
        info.append(f"📉 Mínimo registrado en {fuente}: {eur(h['minimo'])}{cuando}"
                    + (f"; habitual {eur(h['habitual'])}." if h.get("habitual") else "."))
    elif item.get("id"):
        info.append(f"📉 Comparación puntual: siguiente precio en otras tiendas {eur(item['id'])}. "
                    f"Habitual no acreditado.")
    elif item.get("hot"):
        info.append(f"📉 Ya fue chollo caliente en Chollometro: {item['hot']}.")
    else:
        info.append("📉 Habitual no acreditado.")
    mc = item.get("mejor_cm")
    if mc:
        comp = ("igual o mejor que ahora" if precio <= mc["precio"] * 1.005
                else f"ahora {round((precio - mc['precio']) / mc['precio'] * 100)} % más caro")
        info.append(f"📉 Mejor precio publicado en Chollometro: {eur(mc['precio'])} ({mc['fecha']}, {mc['temp']}°); "
                    f"{comp}.")
    mp = item.get("minimo_propio")
    if mp and mp[2][:10] != datetime.now().strftime("%Y-%m-%d"):
        f, desde = mp[1], mp[2]
        info.append(f"📉 Mínimo visto por el vigilante en esta tienda: {eur(mp[0])} ({f[8:10]}/{f[5:7]}/{f[:4]}); "
                    f"seguido desde el {desde[8:10]}/{desde[5:7]}.")
    info.append("")
    antes = item.get("antecedentes") or []
    for a in antes[:3]:
        tipo = "Antecedente" if a.get("exacto") else "Alternativa"
        info.append(f"🔎 {tipo}: {eur(a['precio']) if a.get('precio') else 'sin precio'} · {a['temp']}° · "
                    f"{a['fecha']}{' (caducado)' if a.get('caducado') else ''} · {a['url']}")
    if not antes:
        busqueda = next(iter((item.get("buscar_chollometro") or {}).values()), None)
        info.append("🔎 Chollometro: no localizado en la consulta." + (f" Búsqueda manual: {busqueda}" if busqueda else ""))
    avisos = []
    if suelto:
        avisos.append(f"Promoción {promo} leída en el listado: hay que comprar {uds} y confirmarla en la cesta")
    if item.get("aviso_ch"):
        avisos.append(item["aviso_ch"])
    if item.get("aviso"):
        avisos.append(item["aviso"])
    if nombres_tallas:
        avisos.append("Tallas con stock: " + ", ".join(nombres_tallas))
    if ficha.get("vendedor"):
        avisos.append(f"Vendido por {ficha['vendedor']}")
    if ficha.get("cupones"):
        avisos.append("Cupones vistos en la ficha, sin probar: " + ", ".join(ficha["cupones"]))
    if not ficha.get("stock"):
        avisos.append("Stock no verificado")
    if not (ficha.get("foto") or item.get("img")):
        avisos.append("Sin foto verificada del producto")
    if avisos:
        info.append("")
        info += [f"⚠️ {a}." for a in avisos]
    return {"precios": "\n".join(precios), "info": "\n".join(info), "enlace": item.get("u", ""),
            "foto": ficha.get("foto") or item.get("img"), "etiqueta": d["etiqueta"],
            "redaccion": {"num": item["num"], "etiqueta": d["etiqueta"], "nombre": item.get("n"),
                          "tallas": [v.split(":")[0] for v in tallas[:15]], "specs": (ficha.get("specs") or [])[:15],
                          "resumen": ficha.get("resumen"), "titulo_ficha": ficha.get("titulo"),
                          "cupon": item.get("cup")}}


def plantilla_redaccion(r):
    """Título y descripción sin IA (0 tokens): correctos y sosos. Para `redaccion: plantilla` o sin Hermes.
    Descripción en bloques: encabezado con emoji y **negrita** pegado a sus viñetas, una línea entre bloques."""
    titulo = (r.get("nombre") or "")[:120]
    if r.get("tallas"):
        titulo = f"{titulo}, tallas {', '.join(r['tallas'][:8])}"[:140]
    bloques = []
    specs = [x for x in (r.get("specs") or [])[:10] if x]
    if specs:
        bloques.append("📋 **Características**\n" + "\n".join(f"• {x}" for x in specs))
    elif r.get("resumen"):
        bloques.append("📋 **Características**\n• " + str(r["resumen"]).strip())
    if r.get("tallas"):
        bloques.append("📏 **Tallas disponibles**\n• " + ", ".join(r["tallas"][:15]))
    return {"titulo": titulo, "descripcion": "\n\n".join(bloques) or (r.get("nombre") or "")}


def a_html_telegram(texto):
    """'🧥 **Diseño**' -> '🧥 <b>Diseño</b>' con el resto escapado: negrita real en Telegram (parse_mode HTML)."""
    partes = re.split(r"\*\*(.+?)\*\*", escape(texto, quote=False))
    return "".join(f"<b>{p}</b>" if i % 2 else p for i, p in enumerate(partes))


def ficha_de_args(args):
    """`--precio 30,00 --stock si --vendedor PcComponentes` -> {"precio_ahora": 30.0, "stock": "sí", ...}.
    None si no hay --precio. Es lo que Hermes apunta tras abrir la ficha con su navegador."""
    valores, clave = {}, None
    for a in args:
        if a.startswith("--"):
            clave = a[2:]
            valores[clave] = ""
        elif clave:
            valores[clave] = (valores[clave] + " " + a).strip()
    precio = a_numero(valores.get("precio")) if valores.get("precio") else None
    if not precio:
        return None
    stock = valores.get("stock", "").lower()
    return {"precio_ahora": precio,
            "stock": "no" if stock in ("no", "agotado", "sin stock") else "sí" if stock else None,
            "vendedor": valores.get("vendedor") or None}


MURO_HORAS = 6  # tras un muro antibot en la ficha, sus candidatas las mira Zeta durante estas horas


def muro_activo(con, tienda, horas=MURO_HORAS):
    """True si Hermes encontró hace menos de `horas` un muro antibot en las fichas de esa tienda."""
    fila = con.execute("SELECT v FROM meta WHERE k=?", (f"muro:{dominio_tienda(tienda or '')}",)).fetchone()
    try:
        return bool(fila) and datetime.now() - datetime.fromisoformat(fila[0]) < timedelta(hours=horas)
    except ValueError:
        return False


def texto_a_mano(n, item):
    return (f"#{n} {item.get('t', '')}: la ficha tiene muro antibot y no se puede leer automáticamente. "
            f"Mírala tú: {item.get('u', '')}\n"
            f"Si está bien, escribe a Hermes: comprueba {n} precio 00,00 stock si")


def muro(n, avisar=True):
    """`muro N`: Hermes ha encontrado un muro antibot (Cloudflare...) en la ficha de #N. No se insiste ni se
    busca otra vía: se apunta la tienda y, durante MURO_HORAS, sus candidatas pasan a Zeta para que las mire
    en su navegador ("a_mano") en vez de gastar verificaciones de Hermes. Caso: #189 y #193 de PcComponentes, 17/11/2026."""
    con = bd()
    fila = con.execute("SELECT datos FROM lista WHERE id=?", (int(n),)).fetchone()
    item = {**json.loads(fila[0]), "num": int(n)} if fila else {"num": int(n)}
    if item.get("t"):
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                    (f"muro:{dominio_tienda(item['t'])}", datetime.now().isoformat(timespec="minutes")))
        con.commit()
    con.close()
    d = {"num": int(n), "veredicto": "a_mano", "motivo": f"muro antibot en {item.get('t', '?')}: lo mira Zeta"}
    if avisar and item.get("t"):
        cfg = cargar_config()
        token, chat = token_telegram(), str(cfg.get("entrega", {}).get("chat_id", ""))
        if token and chat:
            enviar_telegram(token, chat, texto_a_mano(n, item), hilo=f"cat:{item.get('cat')}")
    log(f"muro #{n}: {d['motivo']}")
    print(json.dumps(d, ensure_ascii=False, separators=(",", ":")))
    return d


def decidir(n, avisar=True, ficha_manual=None):
    """`decidir N`: lee la ficha en vivo, aplica los filtros por código y actúa. Imprime una línea JSON.
    - descartado: guarda el veredicto del modelo y manda "#N descartado: motivo" a Telegram.
    - pasa: deja data/decision_N.json con los bloques hechos; con `redaccion: plantilla` entrega ya.
    - sin_validar: no hace nada más (el botón se lo pasa a la IA, que puede abrir la ficha con su navegador).
    - a_mano: sin validar en una tienda con muro reciente (`muro N`): se le manda a Zeta el enlace, sin IA."""
    cfg = cargar_config()
    item = dossier_items([n], ficha_manual=ficha_manual)[0]
    d = decidir_item(item, cfg)
    if d["veredicto"] == "sin_validar" and not ficha_manual and not item.get("error"):
        con = bd()
        if muro_activo(con, item.get("t")):
            d = {**d, "veredicto": "a_mano", "motivo": f"muro antibot en {item.get('t')}: lo mira Zeta"}
        con.close()
    archivo = DATOS / f"decision_{int(n)}.json"
    if d["veredicto"] == "pasa":
        b = bloques_de_codigo(item, d)
        archivo.write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
        if cfg.get("entrega", {}).get("redaccion", "hermes") == "plantilla":
            (DATOS / f"entrega_{int(n)}.json").write_text(json.dumps(plantilla_redaccion(b["redaccion"]),
                                                                    ensure_ascii=False), encoding="utf-8")
            d["entregado"] = bool(entregar(n, callado=True))
    else:
        archivo.unlink(missing_ok=True)
        if d["veredicto"] == "descartado" and d.get("memoria"):
            recordar(n, d["memoria"], d["motivo"], callado=True)
        # "Sin validar" no se avisa: el botón se lo pasa a Hermes para que lea la ficha ("comprueba N").
        if avisar and not item.get("error") and d["veredicto"] in ("descartado", "a_mano"):
            token, chat = token_telegram(), str(cfg.get("entrega", {}).get("chat_id", ""))
            if token and chat:
                enviar_telegram(token, chat, f"#{n} descartado: {d['motivo']}" if d["veredicto"] == "descartado"
                                else texto_a_mano(n, item), hilo=f"cat:{item.get('cat')}")  # junto al candidato
    log(f"decidir #{n}: {d['veredicto']} {d.get('etiqueta', '')} {d['motivo'][:140]}")
    print(json.dumps(d, ensure_ascii=False, separators=(",", ":")))
    return d


def redaccion(n):
    """`redaccion N`: los datos ya verificados que necesita la IA para escribir título y descripción."""
    try:
        b = json.loads((DATOS / f"decision_{int(n)}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(json.dumps({"num": int(n), "error": "no hay decisión guardada: pulsa Verificar o usa verifica N"},
                         ensure_ascii=False))
        return
    print(json.dumps(b["redaccion"], ensure_ascii=False, separators=(",", ":")))


# Orden de envío de las reglas de formato de Zeta (17/11/2026): foto, título, enlace, precio, INFO, descripción.
BLOQUES = ("titulo", "enlace", "precios", "info", "descripcion")


def entregar(n, callado=False):
    """La IA deja vigilante_data/entrega_N.json con los bloques y el script los manda como 6 mensajes: foto,
    título, enlace, precio, INFO ADICIONAL y descripción (con negrita real). Así la IA no repite texto y el
    formato sale siempre igual. Si `decidir` dejó data/decision_N.json, la IA solo escribe "titulo" y
    "descripcion": precio, INFO, enlace y foto salen de la decisión por código. La etiqueta va en la INFO,
    no en el título. Con `entrega.hilos` {categoría: id del tema}, va al tema de su categoría."""
    decir = (lambda *a: None) if callado else print
    archivo = DATOS / f"entrega_{int(n)}.json"
    decision = DATOS / f"decision_{int(n)}.json"
    try:
        b = json.loads(archivo.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        decir(f"#{n}: no puedo leer {archivo.name}: {e}")
        return False
    hecha = None
    try:
        hecha = json.loads(decision.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if hecha:
        for k in ("precios", "info", "enlace"):  # lo calculado por código manda sobre lo que escriba la IA
            b[k] = hecha[k]
        b.setdefault("foto", hecha.get("foto"))
    # Título sin etiqueta, precio, tienda ni código interno: la etiqueta va en la INFO.
    b["titulo"] = re.sub(r"^\s*\[[^\]]*\]\s*", "", str(b.get("titulo", ""))).strip()[:140]
    faltan = [k for k in BLOQUES if not str(b.get(k, "")).strip()]
    if faltan:
        decir(f"#{n}: faltan bloques: {', '.join(faltan)}")
        return False
    con = bd()
    fila = con.execute("SELECT datos FROM lista WHERE id=?", (int(n),)).fetchone()
    con.close()
    d = json.loads(fila[0]) if fila else {}
    foto = b.get("foto") or d.get("img")
    entrega = cargar_config().get("entrega", {})
    token, chat = token_telegram(), str(entrega.get("chat_id", ""))
    if not (token and chat):
        decir("falta token o chat_id")
        return False
    # Pestaña "✅ Para publicar" (18/11/2026, comando `temas`); entrega.hilos (tema por categoría) si se rellena.
    hilo = (entrega.get("hilos") or {}).get(d.get("cat")) or "publicar"
    extra = {"hilo": hilo}
    ok, enviados = True, 0
    if foto:
        ok = enviar_telegram(token, chat, "", foto, **extra)
        enviados += 1
    for k in BLOQUES:
        texto = str(b[k]).strip()
        if k == "descripcion":
            ok = enviar_telegram(token, chat, a_html_telegram(texto), html=True, **extra) and ok
        else:
            ok = enviar_telegram(token, chat, texto, **extra) and ok
        enviados += 1
    decir(f"#{n} entregado en {enviados} mensajes" if ok else f"#{n}: algún mensaje no se pudo enviar")
    if ok:
        archivo.unlink(missing_ok=True)
        if hecha:
            decision.unlink(missing_ok=True)
            recordar(n, "buena", hecha["info"].split("Prueba: ", 1)[-1].split("\n")[0], callado=True)
    return ok


def pagina_caida(con, pagina, n):
    """Si una página de ofertas da de pronto muchos menos productos (menos del 30 % de su máximo, que era
    de 10 o más), devuelve ese máximo: suele ser una campaña que ha terminado. Avisa una vez al día."""
    fila = con.execute("SELECT v FROM meta WHERE k=?", (f"pmax:{pagina}",)).fetchone()
    maximo = int(fila[0]) if fila else 0
    if n > maximo:
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"pmax:{pagina}", str(n)))
        return None
    if maximo < 10 or n > maximo * 0.3:
        return None
    hoy = datetime.now().strftime("%Y-%m-%d")
    avisado = con.execute("SELECT v FROM meta WHERE k=?", (f"pcaida:{pagina}",)).fetchone()
    if avisado and avisado[0] == hoy:
        return None
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"pcaida:{pagina}", hoy))
    return maximo


_REFRESCO_HECHO = [False]


def refrescar_extras(con, cfg, categoria, grupo, tiendas):
    """Una vez cada `ofertas_refresco_dias` (7) por categoría y grupo, vuelve a mirar la portada de las
    tiendas que se están leyendo y renueva sus páginas de ofertas adicionales (campañas nuevas entran,
    las terminadas salen). No cambia la página principal. Como mucho una categoría por ronda.
    Devuelve True si ha actualizado urls_ofertas.json."""
    dias = (cfg or {}).get("filtros_codigo", {}).get("ofertas_refresco_dias", 7)
    if not dias or _REFRESCO_HECHO[0] or not tiendas:
        return False
    clave = f"extras_fecha:{categoria}:{grupo or 'todo'}"
    fila = con.execute("SELECT v FROM meta WHERE k=?", (clave,)).fetchone()
    if fila and datetime.fromisoformat(fila[0]) > datetime.now() - timedelta(days=dias):
        return False
    # Se apunta antes de empezar: si falla, no se reintenta en cada ronda.
    con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (clave, datetime.now().isoformat(timespec="minutes")))
    con.commit()
    _REFRESCO_HECHO[0] = True
    import contextlib
    import io
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            descubrir(categoria, solo_extras=True, grupo=grupo, solo_tiendas=tiendas)
        log(f"{categoria}: páginas de ofertas adicionales renovadas ({len(tiendas)} tiendas)")
        return True
    except Exception as e:
        log(f"{categoria}: no se pudieron renovar las páginas adicionales: {str(e)[:120]}")
        return False


def recordar(n, veredicto, motivo, callado=False):
    """Guarda el veredicto de un modelo para no investigarlo otra vez (30 días)."""
    con = bd()
    fila = con.execute("SELECT base FROM lista WHERE id=?", (int(n),)).fetchone()
    if not fila:
        if not callado:
            print(f"#{n} no existe")
        return
    con.execute("INSERT OR REPLACE INTO modelos VALUES (?,?,?,?)",
                (fila[0], veredicto.lower(), motivo[:300], datetime.now().isoformat(timespec="minutes")))
    con.commit()
    con.close()
    if not callado:
        print(f"ok: #{n} -> {veredicto}")


def reglas_de(cfg, categoria, tienda):
    """Reglas efectivas: global < categoría < tienda."""
    f = cfg.get("filtros_codigo", {})
    cat = cfg.get("categorias", {}).get(categoria, {})
    t = cfg.get("reglas_tienda", {}).get(tienda, {})
    r = {
        "descuento_min": t.get("descuento_min", cat.get("descuento_min", f.get("descuento_min_real", 0.20))),
        # Sin tope por defecto: null o ausente = sin límite de precio.
        "precio_max": t.get("precio_max", cat.get("precio_max", f.get("precio_max_eur"))) or float("inf"),
        "min_resenas": t.get("min_resenas", cat.get("min_resenas", 0)),
        "min_valoracion": t.get("min_valoracion", cat.get("min_valoracion", 0)),
        "marca": t.get("exigir_marca", cat.get("exigir_marca", True)),
        "exigir_descuento": t.get("exigir_descuento", cat.get("exigir_descuento", False)),
        # Precio original mínimo: GOG rebaja al 90 % juegos de 5 € (0,55 €): no son chollos publicables.
        "antes_min": t.get("antes_min_eur", cat.get("antes_min_eur", 0)),
    }
    return r


def ahorro_suficiente(p, mejor, cat_cfg, f):
    """Ahorro mínimo en euros frente a la referencia: el mercado si lo hay; si no, el 'antes' de la tienda.
    Caso real: cuchillo Arcos a 10,99 € con el mercado a 11,96 € (1 € de ahorro) llegaba a Telegram."""
    if p.get("gratis"):
        return True
    ref = mejor or p.get("precio_antes") or p.get("_min_previo")
    if not ref:
        return True
    return ref - p["precio"] >= cat_cfg.get("ahorro_min_eur", f.get("ahorro_min_eur", 0))


def ya_publicado_igual_o_mas_barato(ch, precio):
    """'otra:X' o 'misma:X' con X <= precio: alguien ya lo publicó igual o más barato en Chollometro.
    Misma tienda: regla de duplicados de Chollometro, solo vale si baja más de un 5 % y más de 1 €
    (caso real, 16/11/2026: DualSense en Amazon a 54,90 € con hilo de Amazon a 56,99 €)."""
    if not isinstance(ch, str) or ":" not in ch:
        return False
    try:
        x = float(ch.split(":", 1)[1])
    except ValueError:
        return False
    if ch.startswith("misma:"):
        return not (precio < x * 0.95 and x - precio > 1)
    return False  # guía del 18/11/2026: otra tienda no es duplicado (El Corte Inglés/Hipercor ya cuentan como misma)


def normalizar_nota(v):
    """4,6 sobre 5 -> 92 ; 92 (%) -> 92 ; 9,1 sobre 10 -> 91."""
    if v <= 5:
        return v * 20
    if v <= 10:
        return v * 10
    return v


def excluido(p, tienda, cfg):
    palabras = cfg.get("filtros_codigo", {}).get("excluir", [])
    texto = f"{tienda} {p['nombre']}".lower()
    return any(re.search(rf"(?<!\w){re.escape(x.lower())}(?!\w)", texto) for x in palabras)


# ------------------------------------------------------- sesiones (login)

def estadistica(con, tienda, **sumas):
    hoy = datetime.now().strftime("%Y-%m-%d")
    con.execute("INSERT OR IGNORE INTO stats (fecha, tienda) VALUES (?,?)", (hoy, tienda))
    for k, n in sumas.items():
        con.execute(f"UPDATE stats SET {k}={k}+?, rondas=rondas+? WHERE fecha=? AND tienda=?",
                    (n, 1 if k == "leidos" else 0, hoy, tienda))


def en_pausa_fuera(con, lista):
    """Quita las tiendas que están en pausa por fallar seguido (captcha, 403, caída)."""
    ahora = datetime.now().isoformat(timespec="minutes")
    fuera = []
    for t in lista:
        clave = t[4:] if t.startswith("rss:") else t
        fila = con.execute("SELECT v FROM meta WHERE k=?", (f"pausa:{clave}",)).fetchone()
        if not (fila and fila[0] > ahora):
            fuera.append(t)
    return fuera


RE_NO_SE_LEE = re.compile(r"robots\.txt|antibot|captcha", re.I)
RE_NAV_OCUPADO = re.compile(r"navegador ocupado", re.I)


def registrar_fallos(con, lecturas, fijas=None):
    """3 fallos seguidos -> pausa de 30 min; cada fallo más la duplica, hasta 24 h.
    Un acierto lo reinicia. Así no se pierde tiempo ni se insiste con tiendas que bloquean.
    fijas: {tienda: minutos} (Miravia y AliExpress con Chrome normal): un reto o captcha la para esos minutos
    en el acto, siempre los mismos aunque se repita; otro fallo, a los 3 seguidos, igual (decisión de Zeta,
    17/11/2026: sin pausas que se doblen)."""
    fijas = fijas or {}
    for tienda, _, productos, error in lecturas:
        clave = f"fallos:{tienda}"
        if not error:
            con.execute("DELETE FROM meta WHERE k IN (?,?,?)", (clave, f"pausa:{tienda}", f"no_se_lee:{tienda}"))
            continue
        if RE_NAV_OCUPADO.search(error):
            # No es culpa de la tienda: otra ronda (o una ventana de `login`) tenía el navegador. El 17/11
            # una hora así dejó a Amazon, Carrefour y El Corte Inglés en pausa y a Fnac fuera 24 h.
            continue
        if tienda in fijas:
            fila = con.execute("SELECT v FROM meta WHERE k=?", (clave,)).fetchone()
            n = int(fila[0]) + 1 if fila else 1
            con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (clave, str(n)))
            if re.search(r"reto|antibot|captcha", error, re.I) or n >= 3:
                hasta = (datetime.now() + timedelta(minutes=fijas[tienda])).isoformat(timespec="minutes")
                con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"pausa:{tienda}", hasta))
                log(f"{tienda}: {error[:60]}, en pausa {fijas[tienda]} min")
            continue
        if RE_NO_SE_LEE.search(error):
            # Instrucciones de lectura, paso 4: si una web bloquea (robots.txt, antibot, captcha) se anota
            # "no se lee" y se pasa a la siguiente sin reintentar en bucle: fuera 24 h de una vez.
            hasta = (datetime.now() + timedelta(hours=24)).isoformat(timespec="minutes")
            ya = con.execute("SELECT 1 FROM meta WHERE k=?", (f"no_se_lee:{tienda}",)).fetchone()
            con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"pausa:{tienda}", hasta))
            con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                        (f"no_se_lee:{tienda}", f"{datetime.now():%d/%m %H:%M} {error[:90]}"))
            if not ya:
                log(f"{tienda}: no se lee ({error[:70]}), fuera 24 h")
            continue
        fila = con.execute("SELECT v FROM meta WHERE k=?", (clave,)).fetchone()
        n = int(fila[0]) + 1 if fila else 1
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (clave, str(n)))
        if n >= 3:
            minutos = min(24 * 60, 30 * 2 ** (n - 3))
            hasta = (datetime.now() + timedelta(minutes=minutos)).isoformat(timespec="minutes")
            con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"pausa:{tienda}", hasta))
            log(f"{tienda}: {n} fallos seguidos ({error[:60]}), en pausa {minutos} min")
    con.commit()


def vigilar_sesiones(con, cfg, lecturas):
    """Tiendas con login: si dan 0 productos dos rondas seguidas, la sesión ha caducado.
    Aviso por Telegram una vez al día por tienda."""
    con_login = set(cfg.get("requiere_login", []))
    for tienda, _, productos, error in lecturas:
        if tienda not in con_login:
            continue
        clave = f"login_fallos:{tienda}"
        if productos:
            con.execute("DELETE FROM meta WHERE k=?", (clave,))
            continue
        fila = con.execute("SELECT v FROM meta WHERE k=?", (clave,)).fetchone()
        fallos = int(fila[0]) + 1 if fila else 1
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (clave, str(fallos)))
        hoy = datetime.now().strftime("%Y-%m-%d")
        avisado = con.execute("SELECT v FROM meta WHERE k=?", (f"login_aviso:{tienda}",)).fetchone()
        if fallos >= 2 and not (avisado and avisado[0] == hoy):
            con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"login_aviso:{tienda}", hoy))
            texto = (f"⚠️ {tienda}: parece que la sesión ha caducado (0 productos dos rondas seguidas).\n"
                     f"En el portátil: python vigilante.py login {tienda}")
            log(texto.replace("\n", " "))
            token, chat = token_telegram(), str(cfg.get("entrega", {}).get("chat_id", ""))
            if token and chat:
                enviar_telegram(token, chat, texto, hilo="sistema")
    con.commit()


def login(*tiendas):
    """Abre el navegador del vigilante (ventana visible, mismo perfil) con una pestaña por tienda para
    que inicies sesión tú. Cuando cierres la ventana, las sesiones quedan guardadas para las rondas.
    Ninguna contraseña pasa por el script ni por la IA."""
    cfg = cargar_config()
    urls = [cfg.get("logins", {}).get(t) or url_tienda(t) for t in tiendas]
    nav = Navegador({**cfg, "navegador": {**cfg.get("navegador", {}), "visible": True, "bloquear_recursos": False}})
    print(f"Abriendo {', '.join(urls)}\nInicia sesión en cada pestaña (y marca 'recordarme' si sale). "
          "Cuando termines, CIERRA la ventana.")
    with nav:
        for i, url in enumerate(urls):
            page = nav._ctx.pages[0] if i == 0 and nav._ctx.pages else nav._ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except Exception as e:  # una tienda lenta no impide abrir las demás
                print(f"{url}: {str(e)[:80]}")
        try:
            nav._ctx.wait_for_event("close", timeout=0)  # espera a que cierres la ventana
        except Exception:
            pass
        nav._ctx = None  # ya está cerrado
    con = bd()
    for tienda in tiendas:
        con.execute("DELETE FROM meta WHERE k IN (?,?)", (f"login_fallos:{tienda}", f"login_aviso:{tienda}"))
    con.commit()
    con.close()
    print(f"Sesiones guardadas: {', '.join(tiendas)}. Compruébalo con: python vigilante.py prueba <categoría>")


def informe(dias=7):
    """Qué aporta cada tienda: rondas, productos leídos, errores, candidatos y enviados."""
    con = bd()
    desde = (datetime.now() - timedelta(days=dias - 1)).strftime("%Y-%m-%d")
    filas = con.execute("""SELECT tienda, SUM(rondas), SUM(leidos), SUM(errores), SUM(candidatos), SUM(enviados)
                           FROM stats WHERE fecha>=? GROUP BY tienda
                           ORDER BY SUM(enviados) DESC, SUM(candidatos) DESC, SUM(leidos) DESC""", (desde,)).fetchall()
    con.close()
    print(f"Últimos {dias} días{'':8}rondas   leídos  errores  candidatos  enviados")
    for t, r, l, e, c, en in filas:
        aviso = "  <- nunca da nada" if r and r >= 10 and not l else ("  <- falla mucho" if r and e > r / 2 else "")
        print(f"{t:28}{r:6}{l:9}{e:9}{c:12}{en:10}{aviso}")
    con = bd()
    nose = con.execute("SELECT k, v FROM meta WHERE k LIKE 'no_se_lee:%' ORDER BY k").fetchall()
    a_nav = con.execute("SELECT k FROM meta WHERE k LIKE 'a_navegador:%'").fetchall()
    con.close()
    if nose:
        print("\nNo se leen (bloqueo, robots.txt o captcha; se reintenta cada 24 h):")
        for k, val in nose:
            print(f"  {k.split(':', 1)[1]:28}{val}")
    if a_nav:
        print("\nLeídas con Chrome porque el HTML sale vacío: " + ", ".join(k[0].split(":", 1)[1] for k in a_nav))
    cat = urls_catalogo()
    if cat:
        print(f"\nCatálogo: {sum(len(x) for x in cat.values())} categorías de {len(cat)} tiendas (una por ronda y tienda)")


def compacto(p):
    """Lo mínimo que necesita la IA, con claves cortas (ver prompt)."""
    c = {"n": p["nombre"], "t": p["tienda"], "u": p["url"], "p": p["precio"],
         "a": p["precio_antes"], "id": p["idealo"], "e": p["ean"],
         "img": p["imagen_url"], "ch": p["ch"], "val": p.get("valoracion"), "res": p.get("resenas"),
         "socio": 1 if p.get("socio") else None, "err": 1 if p.get("error_precio") else None,
         "gratis": 1 if p.get("gratis") else None, "h": p.get("historico_url"),
         "hot": (f"{p['_cm_hot']['temp']}° a {p['_cm_hot']['precio']:g} € hace {p['_cm_hot']['dias']} días"
                 if p.get("_cm_hot") and p["_cm_hot"].get("precio") else None),
         "dr": (f"-{round(p['dto_real'] * 100)} % sobre {p['ref_real']}" if p.get("dto_real") else None),
         "promo": p.get("promo"), "ps": p.get("precio_suelto"), "uds": p.get("unidades"),
         "rt": p.get("ref_tienda"), "cup": p.get("cupon"), "psc": p.get("precio_sin_cupon"),
         "mp": p.get("_mercado_propio"),
         "hab": (f"{p['_hist']['habitual']:g} € ({p['_hist']['fuente_corta']})"
                 if (p.get("_hist") or {}).get("fuente_corta") else None)}
    if p["estado"] == "bajada":
        c["baja"] = 1
    return {k: v for k, v in c.items() if v is not None}


GRUPOS = {"rapidas": "descarga", "navegador": "navegador"}


def carga_en_marcha():
    """Hay una precarga larga de Chollometro en marcha (su cerrojo se renueva en cada página). Si lleva más de
    15 min sin renovarse, la carga murió: no cuenta."""
    cerrojo = DATOS / "precarga.lock"
    try:
        return time.time() - cerrojo.stat().st_mtime < 900
    except FileNotFoundError:
        return False


def ronda_grupo(grupo):
    """Una sola tarea para todas las categorías, separadas por forma de lectura: 'rapidas' (descarga normal
    y fuentes de datos, cada pocos minutos) o 'navegador' (las tiendas que necesitan Chrome). Cada tienda
    sigue con las reglas de su categoría; solo cambia cuándo se lee."""
    if carga_en_marcha():  # Zeta (18/11): mientras se carga el historial de Chollometro, no hay rondas
        log(f"ronda {grupo}: saltada, hay una carga de Chollometro en marcha")
        print(SILENCIO)
        return
    if grupo == "rapidas" and en_horario(cargar_config()):
        # La copia de Chollometro se mantiene al día en cada ronda rápida (si no, tendría huecos).
        try:
            con = bd()
            actualizar_chollometro(con)
            con.close()
        except Exception as e:
            log(f"chollometro: {str(e)[:150]}")
    for categoria in cargar_config()["categorias"]:
        try:
            ronda(categoria, solo=GRUPOS[grupo])
        except Exception as e:  # una categoría rota no para a las demás
            log(f"{grupo} {categoria}: {str(e)[:150]}")


def ronda(categoria, solo=None):
    """solo: None (todo), 'descarga' (sin navegador) o 'navegador' (solo las tiendas de navegador)."""
    cfg = cargar_config()
    if not en_horario(cfg):
        print(SILENCIO)
        return
    f = cfg.get("filtros_codigo", {})
    min_bajo = f.get("min_bajo_mercado", 0.05)  # cuánto más barato que el mejor precio de mercado
    desc_min = f.get("descuento_min_real", 0.20)
    desc_sin_idealo = f.get("descuento_min_sin_idealo", 0.35)
    precio_max = f.get("precio_max_eur", 300)
    max_tanda = f.get("max_candidatos_ia_por_tanda", 10)
    max_dia = f.get("max_verificaciones_ia_por_dia", 120)
    max_idealo = f.get("max_consultas_idealo_por_tanda", 25)

    con = bd()
    hoy = datetime.now().strftime("%Y-%m-%d")
    fila = con.execute("SELECT usadas FROM cupo WHERE fecha=?", (hoy,)).fetchone()
    usadas = fila[0] if fila else 0
    if usadas >= max_dia:  # cupo agotado: ni siquiera descarga
        con.close()
        print(SILENCIO)
        return

    rx = regex_marcas()
    tiendas, navegador = tiendas_de(cfg, categoria)
    # Tiendas cuyo HTML sale vacío en descarga normal: se leen con Chrome (como las ve una persona).
    pasar = tiendas_a_navegador(con, tiendas)
    tiendas, navegador = [t for t in tiendas if t not in pasar], navegador + sorted(pasar)
    urls = urls_ofertas()
    cat_cfg = cfg["categorias"][categoria]
    fuentes = (cat_cfg.get("apis", []) + [f"rss:{f}" for f in cat_cfg.get("fuentes", [])]
               + [f"tg:{c}" for c in cat_cfg.get("canales_telegram", [])])
    if solo == "descarga":
        navegador = []
    elif solo == "navegador":
        tiendas, fuentes = [], []
    if not (tiendas or navegador or fuentes):
        con.close()
        print(SILENCIO)
        return
    rec, t0 = f.get("log_recorrido"), time.time()
    antes = set(tiendas) | set(navegador) | set(fuentes)
    tiendas, navegador, fuentes = (en_pausa_fuera(con, x) for x in (tiendas, navegador, fuentes))
    en_pausa = antes - set(tiendas) - set(navegador) - set(fuentes)
    con_pausa = set(tiendas) | set(navegador)
    tiendas, navegador = filtrar_por_demanda(con, cfg, tiendas), filtrar_por_demanda(con, cfg, navegador)
    sin_demanda = con_pausa - set(tiendas) - set(navegador)
    if refrescar_extras(con, cfg, categoria, solo, set(tiendas) | set(navegador)):
        urls = urls_ofertas()
    extras = ofertas_extra_turno(con, cfg, urls, tiendas + navegador) + busquedas_turno(con, cfg, navegador)
    turno = catalogo_turno(con, cfg, tiendas + navegador) + extras
    vigilados = vigilancia_turno(con, cfg, navegador)
    turno += [("amazon.es", u) for u in vigilados]
    todas = leer_todas(tiendas, navegador, urls, categoria, cfg, fuentes, turno)
    if rec:
        recorrido(f"--- {categoria} ({solo or 'todo'}): {len(tiendas)} por descarga, {len(navegador)} con navegador, "
                  f"{len(fuentes)} fuentes, {len(turno)} categorías de catálogo")
        if en_pausa:
            recorrido(f"    en pausa por fallos, no se leen: {', '.join(sorted(en_pausa))}")
        if sin_demanda:
            recorrido(f"    sin demanda en Chollometro, no se leen: {', '.join(sorted(sin_demanda))}")
    de_catalogo = set(turno)
    lecturas = [x for x in todas if (x[0], x[1]) not in de_catalogo]
    fijas = {t: int(cfg["navegador_normal"].get("pausa_min", 30)) for t in tiendas_navegador_normal(cfg)}
    # Un reto en una búsqueda de Miravia o AliExpress también para la tienda sus 30 min.
    retos = [x for x in todas if (x[0], x[1]) in de_catalogo and x[0] in fijas and x[3]
             and re.search(r"reto|antibot|captcha", x[3], re.I)]
    registrar_fallos(con, lecturas + list({x[0]: x for x in retos}.values()), fijas)
    anotar_vacias(con, cfg, lecturas, set(tiendas))
    for tienda, url_cat, productos, error in todas:
        if (tienda, url_cat) in de_catalogo:
            if error:  # una categoría que no se puede leer no aparta la tienda: sale de su lista de categorías
                if RE_NO_SE_LEE.search(error) or re.search(r"\b(?:404|410)\b", error):
                    quitar_de_catalogo(tienda, url_cat, error)
                else:
                    log(f"catálogo {tienda} {url_cat[:80]}: {error[:80]}")
                if rec:
                    recorrido(f"    {tienda} [catálogo o campaña] {url_cat[:90]} -> ERROR: {str(error)[:100]}")
            else:
                lecturas.append((tienda, url_cat, productos, None))

    # 1) Filtros gratis: novedad, marca, precio y descuento de la tienda.
    previos = []
    vigilar_sesiones(con, cfg, lecturas)
    # Cupones de Miravia y AliExpress (18/11/2026): si cambia el juego de cupones de una tienda, sus productos se
    # revisan aunque su precio no haya cambiado (es cuando salen los chollos).
    cupones, cupones_nuevos = {}, set()
    if f.get("cupones", True) and any(x[0] in CUPONES_CM and x[2] for x in lecturas):
        try:
            actualizar_cupones(con)
        except Exception as e:
            log(f"cupones: {str(e)[:100]}")
        for t in {x[0] for x in lecturas if x[0] in CUPONES_CM}:
            cupones[t] = cupones_activos(con, t)
            firma = firma_cupones(con, t)
            fila = con.execute("SELECT v FROM meta WHERE k=?", (f"cupones_firma:{t}",)).fetchone()
            if firma and (not fila or fila[0] != firma):
                cupones_nuevos.add(t)
            con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"cupones_firma:{t}", firma))
    n_vig_leidos = n_vig_baratos = 0
    for tienda, url_leida, productos, error in lecturas:
        estadistica(con, tienda, leidos=len(productos), errores=1 if error else 0)
        modo = "navegador" if tienda in navegador else "descarga"
        if error:
            log(f"{categoria} {tienda}: {error}")
            if rec:
                recorrido(f"    {tienda} [{modo}] {(url_leida or '')[:90]} -> ERROR: {str(error)[:100]}")
            continue
        n_cambios, n_previos = 0, len(previos)
        guardar_pcct(con, productos)
        # Se siembra por página leída: una URL nueva de la misma tienda no inunda de avisos.
        pagina = f"{tienda}|{url_leida}"
        exito = vigilados.get(url_leida)
        sembrada = exito or con.execute("SELECT 1 FROM sembradas WHERE tienda=?", (pagina,)).fetchone()
        for p in productos:
            p["url"] = url_canonica(p.get("url"))
            if p.get("precio_unidad"):
                # Multicompra: cuenta el precio por unidad comprando las que pide la promoción (mínimo, mercado...).
                p["precio_suelto"], p["precio"] = p["precio"], p["precio_unidad"]
            estado = novedad(con, tienda, p)  # el historial guarda el precio de la tienda, sin cupón
            if sembrada and not estado and vuelve_oferta_caducada(con, p):
                estado = "vuelve"
            if tienda in cupones and aplicar_cupon(con, tienda, p, cupones[tienda]) and not estado \
                    and tienda in cupones_nuevos:
                estado = "cupón"
            if exito:  # vigilado: cuenta si está igual o más barato que cuando triunfó (+2 %), haya bajado o no
                estado = "vigilado" if p["precio"] <= exito * 1.02 else None
            if not sembrada or not estado:
                continue  # primera lectura de la tienda: solo memoriza
            n_cambios += 1
            r = reglas_de(cfg, categoria, tienda)
            if p["precio"] > r["precio_max"] or excluido(p, tienda, cfg):
                continue
            if not p.get("url"):
                continue  # sin enlace a la ficha no se puede verificar (Hermes: "Footlocker sin url ni ficha")
            if RE_SIN_PRECIO_OFICIAL.search(p["nombre"]):
                continue  # la tienda no lo vende: el precio leído no es suyo
            if (categoria in f.get("sin_infantil_en", []) or familia_de(p["nombre"]) in ("calzado", "ropa")) \
                    and RE_INFANTIL.search(p["nombre"]):
                continue  # 17/11/2026: ropa y calzado infantil y junior, fuera en cualquier categoría
            if RE_REACONDICIONADO.search(f"{p['nombre']} {p.get('url') or ''}"):
                continue  # 18/11/2026 (Zeta): reacondicionados, segunda mano y usados, fuera
            if r["marca"] and not marca_ok(p, rx, tienda):
                continue
            d = (1 - p["precio"] / p["precio_antes"]) if p["precio_antes"] else None
            if p.get("gratis"):
                d = 1.0  # regalo: 100 % de descuento
            # Posible error de precio: cae un 40 % o más respecto a su mínimo visto, aunque la
            # tienda no muestre precio tachado. Son los chollos que más se calientan.
            # Hace falta un mínimo de seguimiento: un precio visto hace una hora no es referencia.
            err = (p.get("_min_previo") and p["precio"] <= p["_min_previo"] * (1 - ERROR_PRECIO)
                   and p.get("_seguido_dias", 0) >= f.get("error_precio_dias_min", 0)
                   # Chollometro no acepta errores de precio de tiendas pequeñas (18/11/2026)
                   and tienda_grande(con, tienda, f.get("error_precio_min_calientes", 100)))
            if err:
                p["error_precio"] = True
                d = max(d or 0, 1 - p["precio"] / p["_min_previo"])
            if d is not None and d < r["descuento_min"]:
                continue
            if r["exigir_descuento"] and d is None:
                continue  # juegos: sin % visible no se puede saber si llega al 80 %
            if r["antes_min"] and not p.get("gratis") and (p["precio_antes"] or 0) < r["antes_min"]:
                continue
            if r["min_resenas"] and p.get("resenas") is not None and p["resenas"] < r["min_resenas"]:
                continue  # poco popular / tienda con pocas ventas
            if r["min_valoracion"] and p.get("valoracion") is not None and \
                    normalizar_nota(p["valoracion"]) < r["min_valoracion"]:
                continue
            previos.append({**p, "tienda": tienda, "estado": estado, "d": d,
                            "_detectado": datetime.now().strftime("%d/%m %H:%M"),  # al procesar la lectura de la tienda
                            "socio": bool(cfg.get("reglas_tienda", {}).get(tienda, {}).get("socio"))})
        if not sembrada and productos:
            con.execute("INSERT OR IGNORE INTO sembradas VALUES (?)", (pagina,))
        caida = pagina_caida(con, pagina, len(productos))
        if caida:
            log(f"{tienda}: la página da {len(productos)} productos y daba {caida}; puede que la campaña haya "
                f"terminado ({(url_leida or '')[:90]}). Vuelve a pasar: descubrir {categoria}")
            if rec:
                recorrido(f"    OJO {tienda}: daba {caida} productos y ahora {len(productos)}; campaña terminada?")
        if exito:
            n_vig_leidos, n_vig_baratos = n_vig_leidos + bool(productos), n_vig_baratos + n_cambios
            continue  # una línea para todos los vigilados, no una por ficha
        if rec:
            con_antes = sum(1 for x in productos if x.get("precio_antes"))
            nota = "" if sembrada else " (primera lectura de esta página: solo memoriza)"
            recorrido(f"    {tienda} [{modo}] {(url_leida or '')[:90]} -> {len(productos)} productos, "
                      f"{con_antes} con precio anterior, {n_cambios} nuevos o más baratos, "
                      f"{len(previos) - n_previos} pasan los filtros de tienda{nota}")
    if rec and vigilados:
        recorrido(f"    amazon.es [vigilancia] {len(vigilados)} ASIN que ya triunfaron -> {n_vig_leidos} leídos, "
                  f"{n_vig_baratos} igual o más baratos que cuando triunfaron")
    limpiar(con)
    con.commit()

    # 2) Mismo producto en varias tiendas: quedarse con el más barato.
    unicos = {}
    for p in previos:
        k = p["ean"] or " ".join(sorted(tokens(p["nombre"])))
        if k not in unicos or p["precio"] < unicos[k]["precio"]:
            unicos[k] = p
    previos = sorted(unicos.values(), key=lambda x: -(x["d"] or 0))

    # 2b) Chollometro antes que los comparadores (Zeta, 18/11/2026): la copia es local y no gasta consultas. Lo que
    # ya está publicado (reglas de duplicados de Chollometro) o se calentó siempre más de un 10 % más barato no
    # ocupa sitio en el lote de Idealo, PrecioReal, PCComTracker ni CamelCamelCamel.
    copia = None
    if previos:
        actualizar_chollometro(con)
        copia = cargar_chollometro(con)
        quedan, fuera = [], Counter()
        for p in previos:
            p["ch"] = estado_chollometro(p, copia)  # también deja _cm_hot (demanda probada)
            if p["ch"] == "misma" or ya_publicado_igual_o_mas_barato(p["ch"], p["precio"]):
                fuera["ya publicado"] += 1
            elif not p.get("error_precio") and not p.get("gratis") and (techo := ya_salio_mas_barato(p)):
                fuera["ya salió más barato"] += 1
                log(f"ya salió más barato en Chollometro (200°+ a {techo:g} € como mucho), no se envía: "
                    f"{p['tienda']} {p['nombre'][:60]} a {p['precio']} €")
            else:
                quedan.append(p)
        if rec and fuera:
            recorrido(f"    Chollometro antes de comparar: {len(previos)} -> {len(quedan)} "
                      f"({', '.join(f'{k} {n}' for k, n in fuera.items())})")
        previos = quedan

    # 3) Idealo (con caché) solo para los mejores. Las consultas que faltan van 3 a la vez.
    cat_cfg = cfg["categorias"][categoria]
    sin_idealo = cat_cfg.get("sin_idealo", False)  # viajes y juegos digitales: Idealo no aplica
    # Antes de consultar a nadie: lo que ya sabe el vigilante (cuesta milisegundos). Mercado propio (mismo EAN
    # en otras tiendas) y precio habitual propio. Así entran también candidatos que no caben en las consultas
    # externas (`max_idealo`), hasta `comparar_propio_max` más por ronda.
    if f.get("comparar_propio", True):
        for p in previos:
            p["_mp"] = mercado_propio(con, p, p["tienda"])
            p["_hp"] = habitual_propio(con, clave_vista(p["tienda"], p))
    lote = previos[:max_idealo]
    lote += [p for p in previos[max_idealo:] if p.get("_mp") or p.get("_hp")][:f.get("comparar_propio_max", 60)]
    precios_idealo, pendientes = {}, []
    for i, p in enumerate(lote):
        if sin_idealo or i >= max_idealo:
            continue
        hay, precio = idealo_cache(con, p)
        if hay:
            precios_idealo[i] = precio
        else:
            pendientes.append(i)
    # Moda y deporte: PrecioReal da el precio del modelo, no el del color o lavado (17/11: unas Levi's 726
    # "clean getaway" a 44,95 € contra 63 € de una ficha que ya no existía). No es contraste real.
    usar_pr = not cat_cfg.get("sin_precioreal", False)
    if pendientes and usar_pr:
        # 18/11/2026 (Zeta): primero PrecioReal (rápido y permitido) para todos; Idealo solo para lo que no encuentre.
        with ThreadPoolExecutor(max_workers=3) as ex:
            for i, mejor in zip(pendientes, ex.map(lambda i: precioreal_red(lote[i]), pendientes)):
                if mejor:
                    con.execute("INSERT OR REPLACE INTO idealo VALUES (?,?,?)",
                                (clave_idealo(lote[i]), mejor, datetime.now().isoformat(timespec="minutes")))
                    precios_idealo[i] = mejor
        pendientes = [i for i in pendientes if i not in precios_idealo]
    if pendientes and (_IDEALO_BLOQUEADO[0] or idealo_en_pausa()) and f.get("idealo_navegador", True):
        # La descarga de Idealo está bloqueada: los mejores, con navegador visible. Lo que no encuentre
        # (o no le dé tiempo) sigue por PrecioReal.
        por_nav = pendientes[:f.get("idealo_navegador_max", IDEALO_NAV_MAX)]
        for i, mejor in zip(por_nav, idealo_navegador([lote[i] for i in por_nav], cfg)):
            if mejor:
                con.execute("INSERT OR REPLACE INTO idealo VALUES (?,?,?)",
                            (clave_idealo(lote[i]), mejor, datetime.now().isoformat(timespec="minutes")))
                precios_idealo[i] = mejor
        pendientes = [i for i in pendientes if i not in precios_idealo]
    with ThreadPoolExecutor(max_workers=3) as ex:
        # PrecioReal ya se miró arriba: aquí solo Idealo (sin repetir PrecioReal de reserva).
        for i, mejor in zip(pendientes, ex.map(lambda i: idealo_red(lote[i], False), pendientes)):
            if mejor is not False:
                con.execute("INSERT OR REPLACE INTO idealo VALUES (?,?,?)",
                            (clave_idealo(lote[i]), mejor, datetime.now().isoformat(timespec="minutes")))
            precios_idealo[i] = mejor or None
    finales, casi = [], []  # casi: buena pinta pero sin contraste (pestaña Radar, 18/11/2026)
    if lote and copia is None:  # copia local al día (ya cargada en el paso 2b si había productos)
        actualizar_chollometro(con)
        copia = cargar_chollometro(con)
    pesos_t = {}  # 18/11/2026: orden de candidatos por volumen de calientes de la familia en su tienda (pesos_volumen)
    # Sin precio de mercado: histórico de PCComTracker del mismo modelo para productos de otras tiendas (Amazon,
    # MediaMarkt...), si su ficha está en el índice. Como mucho PCCT_MAX_RONDA fichas por ronda.
    n_pcct = 0
    pcct_aqui = categoria not in PCCT_FUERA
    for i, p in enumerate(lote):
        if (sin_idealo or not pcct_aqui or precios_idealo.get(i) or p.get("historico_url")
                or n_pcct >= PCCT_MAX_RONDA or not codigos_modelo(p["nombre"])):
            continue
        n_pcct += 1  # cuenta cada consulta (índice o buscador), no solo las que encuentran ficha
        url = pcct_de(con, p)
        if url:
            p["historico_url"], p["_pcct_otra"] = url, True
    # Amazon sin comparador: su histórico en CamelCamelCamel (17/11/2026), las de más descuento primero.
    if f.get("camelcamelcamel", True) and not sin_idealo:
        # También los que solo tienen el histórico de PcComponentes (PCComTracker de otra tienda): para Amazon manda
        # el de Amazon (18/11/2026, #216: switch TP-Link a 16,11 € "-37 %" frente al habitual de PcComponentes).
        de_amazon = [p for i, p in enumerate(lote) if p["tienda"] == "amazon.es" and not precios_idealo.get(i)
                     and (not p.get("historico_url") or p.get("_pcct_otra")) and asin_de(p.get("url"))]
        de_amazon.sort(key=lambda p: -(p.get("d") or 0))
        for p, h in zip(de_amazon, camel_lote(con, cfg, de_amazon[:CAMEL_MAX_RONDA])):
            if h:
                p["_camel"] = h
    for i, p in enumerate(lote):
        mejor = precios_idealo.get(i)
        if mejor and configuracion_ambigua(p["nombre"]):
            # 18/11/2026 (#214): "ThinkPad P14s Gen 7 14 inch AMD" a 1.943 € contra 2.734 € de Idealo, que era otra
            # configuración. Sin RAM ni capacidad en el nombre, el precio del comparador no es del mismo producto.
            log(f"configuración sin concretar, no se compara con el mercado: {p['tienda']} {p['nombre'][:60]}")
            mejor = None
        clase_c, u_c = umbrales_clasico(cfg, p["tienda"], p["nombre"])  # Modo Clásico (guía del 18/11/2026)
        mp = p.get("_mp")
        if mp and not sin_idealo and mejor is None:
            # Sin comparador: el mismo EAN en otra tienda que lee el vigilante sirve de precio de mercado.
            # Solo cuando el comparador no da nada: con Idealo, Idealo manda (si dos tiendas bajan a la vez
            # en una promoción de marca, compararlas entre sí descartaría las dos; eso lo decide el habitual).
            mejor, p["_mercado_propio"] = mp["precio"], mp
        d = p["d"]
        if "ch" not in p:  # ya mirado en el paso 2b
            p["ch"] = estado_chollometro(p, copia)  # también deja _cm_hot (demanda probada)
        dias_hist = cat_cfg.get("historial_propio_dias", f.get("historial_propio_dias", 0))
        bajada_hist = cat_cfg.get("bajada_min_propia", f.get("bajada_min_propia", 0.10))
        if p.get("gratis"):
            ok = True  # gratis: no hay precio de mercado que comparar
        elif sin_idealo:
            # Juegos y viajes: no hay comparador; con histórico propio activado, solo si baja de su mínimo
            # (Hermes: "rebaja del -84 % recurrente, 10 veces en 12 meses").
            ok = not dias_hist or p.get("error_precio") or historial_propio_ok(p, dias_hist, bajada_hist)
        elif mejor is not None:
            # Más barato que el mercado, no "parecido": LG QNED70 a 348 € con el mercado a 339 € pasaba
            # porque la tienda enseñaba un PVPR de 449 €. El "antes" de la tienda ya no cuenta aquí.
            ok = p.get("error_precio") or p["precio"] <= mejor * (1 - u_c["mercado"] + 0.005)
        elif p.get("historico_url") and not (p.get("_pcct_otra") and p.get("_camel")) \
                and (ref := _historico_seguro(p["historico_url"])):
            # Sin precio de mercado pero con el historial de la propia tienda (PcComponentes por PCComTracker):
            # cerca de su mínimo, o claramente por debajo de lo que suele costar.
            p["_hist"] = ref
            if ref.get("sin_precio_tienda") and not p.get("_pcct_otra"):
                pa = ref.get("amazon_precio")
                if not (ref.get("amazon_url") and pa and abs(pa - p["precio"]) <= p["precio"] * 0.02):
                    log(f"PcComponentes no tiene precio oficial ahora, no se envía: {p['nombre'][:60]} a {p['precio']} €")
                    continue
                # El precio en pantalla es el de Amazon (Zeta, 18/11/2026): la oferta es de Amazon, comparada con el
                # habitual de PcComponentes. Los duplicados se miran otra vez, ahora como Amazon.
                p.update(tienda="amazon.es", url=ref["amazon_url"], precio=pa, _vendedor_pcct="amazon")
                ref = {**ref, "fuente_corta": "habitual en PcComponentes (PCComTracker)"}
                ref.pop("tienda_url", None)
                p["_hist"], p["ch"] = ref, estado_chollometro(p, copia)
                if p["ch"] == "misma" or ya_publicado_igual_o_mas_barato(p["ch"], p["precio"]):
                    continue
            if ref.get("tienda_url") and raiz(p["tienda"]) not in urlparse(p.get("url") or "").netloc:
                p["url"] = ref["tienda_url"]  # el aviso lleva a la tienda, no al comparador
            ok = historico_clasico_ok(p["precio"], ref.get("habitual"), ref.get("minimo"), u_c)
        elif (cc := p.get("_camel")):
            # Amazon por CamelCamelCamel: como PCComTracker, cerca de su mínimo o claramente bajo su media.
            p["_hist"] = {"habitual": cc.get("media"), "minimo": cc["minimo"], "minimo_fecha": cc.get("minimo_fecha"),
                          "fuente_corta": "CamelCamelCamel"}
            ok = historico_clasico_ok(p["precio"], cc.get("media"), cc["minimo"], u_c)
        elif (ok_ref := contraste_ref_tienda(p, f)) is not None:
            ok = ok_ref
        elif p.get("_hp") and p["_hp"]["habitual"] and not sin_idealo:
            # Historial del propio vigilante: el precio habitual de este producto en esta tienda (mediana de lo
            # que costó al menos 7 días antes de la bajada). Es lo que mira el soporte de Chollometro.
            hp = p["_hp"]
            p["_hist"] = {"habitual": hp["habitual"], "minimo": hp["minimo"], "fuente_corta": "historial del vigilante"}
            ok = p["precio"] <= hp["habitual"] * (1 - u_c["subiendo"] + 0.005)
        elif cat_cfg.get("exigir_precio_mercado", f.get("exigir_precio_mercado", True)) and not p.get("error_precio"):
            # Sin precio de mercado no se puede saber si el "antes" de la tienda es real (Sony WF-C710N:
            # "antes 199 €" con el mercado a 71,95 €). Regla de Zeta: sin contraste claro, no se envía.
            # El histórico propio sí es contraste: baja de lo más barato visto en semanas.
            ok = bool(dias_hist) and historial_propio_ok(p, dias_hist, bajada_hist)
            p["_sin_contraste"] = not ok
        else:
            # Categorías sin comparador (moda, hogar, belleza...): sin precio de mercado, solo con mucho
            # descuento visible ("descuento_min_sin_mercado" de la categoría; si no, el global).
            if dias_hist:
                ok = p.get("error_precio") or historial_propio_ok(p, dias_hist, bajada_hist)
            else:
                ok = d is not None and d >= cat_cfg.get("descuento_min_sin_mercado", desc_sin_idealo)
            p["_sin_contraste"] = not ok
        if not ok and p.get("_mercado_propio") and p.get("_hp") and p["_hp"]["habitual"]:
            # La otra tienda también ha bajado (promoción de marca, Black Friday): lo que cuenta es el habitual.
            hp = p["_hp"]
            p["_hist"] = {"habitual": hp["habitual"], "minimo": hp["minimo"], "fuente_corta": "historial del vigilante"}
            ok = p["precio"] <= hp["habitual"] * (1 - u_c["subiendo"] + 0.005)
        if not ok and mejor is None and not p.get("gratis") and demanda_probada_ok(p):
            # Sin comparador, pero la comunidad ya lo calentó a este precio o más caro: precio y demanda probados.
            ok = True
        elif not ok and mejor is None and not p.get("gratis") and (cp := chollo_previo(p)):
            # Lo mismo con un chollo de 200° o más de los últimos 6 meses (17/11/2026): igual o más barato ahora.
            p["_cm_hot"] = {"temp": round(cp["temp"]), "precio": cp["precio"], "titulo": cp["titulo"][:80],
                            "dias": int((datetime.now().timestamp() - cp["publicado"]) // 86400),
                            "caducado": bool(cp["caducado"])}
            p["_chollo_previo"] = True
            ok = True
        if ok and es_accesorio(p["nombre"]) and not demanda_probada_ok(p) and not p.get("error_precio"):
            log(f"accesorio o recambio sin demanda probada, no se envía: {p['tienda']} {p['nombre'][:60]}")
            ok = False
        if ok and not p.get("gratis") and precio_sospechoso(p, mejor, f.get("precio_sospechoso", PRECIO_SOSPECHOSO)):
            log(f"precio sospechoso, no se envía: {p['tienda']} {p['nombre'][:60]} a {p['precio']} €")
            ok = False
        ok = ok and ahorro_suficiente(p, mejor, cat_cfg, f)
        if ok and clase_c == "moda" and not p.get("gratis") and not p.get("error_precio") \
                and not anunciado_moda_ok(u_c, p["nombre"], d):
            ok = False  # moda: 40 % anunciado por la tienda (30 % en marcas con gancho)
        exento = p.get("gratis") or p.get("error_precio") or (sin_idealo and not dias_hist)
        dto_min = u_c["mercado"] if cat_cfg.get("descuento_real_min", f.get("descuento_real_min", 0)) else 0
        if ok and dto_min and not exento:
            dto, ref = descuento_real(p, mejor, dias_hist or 14)
            ok = dto is not None and dto >= dto_min
            if ok:
                p["dto_real"], p["ref_real"] = round(dto, 3), ref
        if ok:
            p["idealo"] = mejor
            # Ordena por lo bueno que es: descuento + ventaja frente al mercado + error + demanda en Chollometro.
            hot = (p.get("_cm_hot") or {}).get("temp", 0)
            # Y por la demanda de su familia: un móvil va antes que una impresora con el mismo descuento.
            if p["tienda"] not in pesos_t:
                pesos_t[p["tienda"]] = pesos_volumen(con, cfg, p["tienda"])
            p["peso"] = peso_de(pesos_t[p["tienda"]], p["nombre"])
            p["score"] = ((d or 0) + ((mejor - p["precio"]) / mejor if mejor else 0)
                          + (1 if p.get("error_precio") else 0) + min(hot, 3000) / 3000
                          + max(-0.35, min(1.0, (p["peso"] - 1) * 0.5)))
            finales.append(p)
        elif p.get("_sin_contraste") and not p.get("gratis") and not es_accesorio(p["nombre"]):
            if p["tienda"] not in pesos_t:
                pesos_t[p["tienda"]] = pesos_volumen(con, cfg, p["tienda"])
            p["peso"] = peso_de(pesos_t[p["tienda"]], p["nombre"])
            casi.append(p)
    con.commit()
    if modo_telegram_cfg(cfg) and casi:
        avisar_casi_chollos(con, cfg, casi, f.get("casi_por_ronda", CASI_POR_RONDA))

    # 4) Memoria (modelos ya juzgados o ya enviados) y Chollometro, todo por código.
    entrega = cfg.get("entrega", {})
    modo_telegram = entrega.get("modo") == "telegram"
    finales.sort(key=lambda x: -x["score"])
    for p in finales:  # Chollometro no admite enlaces de afiliado ni de seguimiento (18/11/2026)
        p["url"] = enlace_limpio(p.get("url"))
    hueco = max_tanda if modo_telegram else min(max_tanda, max_dia - usadas)
    mios = titulos_perfil(con, cfg) if finales else []
    candidatos = []
    for p in finales:
        p["base"] = modelo_base(p["nombre"])
        if not descartado_por_memoria(con, p, mios=mios):
            candidatos.append(p)
    # Chollometro: se pone al día la copia local (como mucho cada 4 min) y se vuelve a mirar.
    revisar = candidatos[:hueco + max(3, hueco // 2)]
    if revisar:
        # Buscador de Chollometro para las mejores (17/11/2026): trae hilos de meses atrás que la copia no tiene.
        if f.get("buscar_en_chollometro", True):
            for p in revisar[:CM_BUSCAR_MAX]:
                buscar_en_chollometro(con, p)
        actualizar_chollometro(con)
        copia = cargar_chollometro(con)
        for p in revisar:
            p["ch"] = estado_chollometro(p, copia)
    # Máximo por tienda y ronda: una tienda que "rota" su catálogo (Forumsport, todo al -50 %) no puede
    # llenar sola la tanda. Los mejores de cada tienda primero (la lista ya va ordenada por score).
    max_tienda = f.get("max_por_tienda_y_ronda", 2)
    por_tienda, enviar = Counter(), []
    recientes = avisos_recientes(con, f.get("repetido_horas", 24)) if revisar else []
    for p in revisar:
        if p["ch"] == "misma" or ya_publicado_igual_o_mas_barato(p["ch"], p["precio"]):
            continue  # Hermes los descartaba siempre: "Chollometro ya tiene la misma oferta"
        if p.get("estado") != "vuelve" and \
                repetido_reciente(p, recientes + [(x["tienda"], x["nombre"], x["precio"]) for x in enviar]):
            continue  # 'vuelve' (oferta caducada que regresa) se reenvía a propósito
        if por_tienda[p["tienda"]] < max_tienda and len(enviar) < hueco:
            if f.get("confirmar_ficha") and not p.get("gratis") and ficha_se_intenta(cfg, p["tienda"]):
                p["ficha_ok"], motivo = confirmar_en_ficha(p, f.get("ficha_tolerancia", 0.02))
                if p["ficha_ok"] is False:
                    log(f"ficha no confirma, no se envía: {p['tienda']} {p['nombre'][:60]}: {motivo}")
                    continue
                if p.get("error_precio") and p["ficha_ok"] is not True:
                    # Un "error de precio" se salta el contraste de mercado: sin ficha que lo confirme
                    # suele ser un fallo de lectura (HP EliteBook de PCBox, 16/11/2026).
                    log(f"error de precio sin confirmar en ficha, no se envía: {p['tienda']} {p['nombre'][:60]}")
                    continue
            por_tienda[p["tienda"]] += 1
            enviar.append(p)
    for p in finales:
        estadistica(con, p["tienda"], candidatos=1)
    for p in enviar:
        estadistica(con, p["tienda"], enviados=1)
    con.commit()
    if rec:
        for p in enviar:
            recorrido(f"    AVISO {p['tienda']}: {p['nombre'][:70]} a {p['precio']} € (Chollometro: {p.get('ch')})")
        recorrido(f"=== {categoria}: {len(lecturas)} lecturas, {sum(len(x[2]) for x in lecturas)} productos, "
                  f"{len(previos)} pasan filtros de tienda, {len(finales)} con contraste real, "
                  f"{len(enviar)} avisos, {time.time() - t0:.0f} s")

    if modo_telegram:
        # Modo mixto: el código te manda la lista (0 tokens); tú pides "verifica N" a Hermes.
        silencio = en_silencio_entregas(cfg)
        for p in enviar:
            con.execute("INSERT INTO lista (fecha, base, precio, datos, enviado) VALUES (?,?,?,?,0)",
                        (datetime.now().isoformat(timespec="minutes"), p["base"], p["precio"],
                         json.dumps({**compacto(p), "cat": categoria, "_p": p}, ensure_ascii=False, default=str)))
        con.commit()
        if not silencio:  # en 14:00-15:00 se guardan y salen en la primera ronda después
            enviar_pendientes(con, entrega)
        con.close()
        print(SILENCIO)  # Hermes no se despierta: coste 0
        return

    if not enviar:
        con.close()
        print(SILENCIO)
        return
    # Suma atómica: si dos categorías terminan a la vez no se pisan el contador.
    con.execute("INSERT INTO cupo VALUES (?,?) ON CONFLICT(fecha) DO UPDATE SET usadas=usadas+excluded.usadas",
                (hoy, len(enviar)))
    con.commit()
    con.close()

    lista = [compacto(p) for p in enviar]
    archivo = DATOS / f"candidatos_{categoria}.json"
    salida = json.dumps({"cat": categoria, "c": lista}, ensure_ascii=False, separators=(",", ":"))
    archivo.write_text(salida, encoding="utf-8")
    print(salida)

# ------------------------------------------------- herramientas de apoyo

class Enlaces(HTMLParser):
    def __init__(self):
        super().__init__()
        self.enlaces, self._href, self._texto = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._texto = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self._href:
            self._texto.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.enlaces.append((self._href, " ".join("".join(self._texto).split())))
            self._href = None


RE_OFERTA = re.compile(r"ofert|outlet|promo|chollo|rebaj|descuent|liquidac|black|deal|sale|precios?-?bajos", re.I)


def _descargar3(url):
    html, final = descargar(url)
    return html, final, []


def _con_lectores(cfg, tiendas, navegador, trabajo):
    """Ejecuta trabajo(tienda, lector) con descarga normal o con el navegador según la tienda."""
    for t in tiendas:
        trabajo(t, _descargar3)
    if not navegador:
        return
    try:
        with Navegador(cfg) as nav:
            for t in navegador:
                trabajo(t, nav.leer)
    except ImportError:
        print("\nTiendas de navegador sin probar: falta Playwright. Instálalo con:\n"
              "  & $py -m pip install playwright\n  & $py -m playwright install chromium")


MIN_LISTADO = 3
# Páginas de campaña: caducan en semanas. 'descubrir' solo las elige si no hay nada permanente.
RE_TEMPORAL = re.compile(r"black.?friday|cyber.?monday|black.?week|semanas?.?black|bf\d\d|navidad|christmas|"
                         r"reyes|san.?valentin|prime.?day|dia.?sin.?iva|sin.?iva|el.?iva|singles?.?day|11.?11|"
                         r"vuelta.?al.?cole|halloween|adelantate|"
                         r"rebajas.?de.?(?:enero|verano)|galaxy.?day|(?<!\d)20[2-3]\d(?!\d)", re.I)


# Páginas de un solo público (niños, bebé, teen): tienen muchas rebajas y ganaban a la página general.
# Caso real (16/11/2026): Spartoo leía "Ofertas ... ninos.php", Fútbol Emotion "/ninos/oferta" y Women'secret
# "/nuestros-favoritos/teen"; en moda y deporte lo infantil se descarta, así que no salía nada.
RE_SEGMENTO = re.compile(r"(?<![a-z])(?:ni[nñ][oa]s?|kids?|teens?|beb[eé]s?|baby|infantil(?:es)?|junior)(?![a-z])", re.I)


def descubrir(categoria, solo_extras=False, grupo=None, solo_tiendas=None):
    """Busca la página de ofertas de cada tienda y, además, hasta `ofertas_extra_max` (2) páginas adicionales
    con rebajas (campañas activas, outlet), que se guardan con la clave "tienda+".
    solo_extras: no cambia la página principal ya guardada. grupo: 'descarga' o 'navegador'.
    solo_tiendas: limita a esas tiendas (las que se están leyendo)."""
    cfg = cargar_config()
    tiendas, navegador = tiendas_de(cfg, categoria)
    if grupo == "descarga":
        navegador = []
    elif grupo == "navegador":
        tiendas = []
    if solo_tiendas is not None:
        tiendas, navegador = [t for t in tiendas if t in solo_tiendas], [t for t in navegador if t in solo_tiendas]
    urls = urls_ofertas()
    max_extras = int(cfg.get("filtros_codigo", {}).get("ofertas_extra_max", 2))

    def trabajo(tienda, lector):
        try:
            html, final, _ = lector(url_tienda(tienda))
        except Exception as e:
            print(f"\n{tienda}: ERROR {str(e)[:80]}")
            return
        parser = Enlaces()
        parser.feed(html)
        host = urlparse(final).netloc.replace("www.", "")
        opciones = {}
        for href, texto in parser.enlaces:
            if not href or not (RE_OFERTA.search(href) or RE_OFERTA.search(texto)):
                continue
            absoluta = urljoin(final, href).split("#")[0]
            if urlparse(absoluta).netloc.endswith(host) and "/blog" not in absoluta and "help." not in absoluta:
                opciones.setdefault(absoluta, texto[:50])
        orden = sorted(opciones, key=lambda u: (bool(RE_SEGMENTO.search(urlparse(u).path)), bool(RE_TEMPORAL.search(u)),
                                                not RE_OFERTA.search(urlparse(u).path), len(u)))[:6]
        guardada = urls.get(tienda)
        if guardada and guardada not in orden:
            orden.insert(0, guardada)  # la que ya tenías también compite
            opciones.setdefault(guardada, "guardada")
        # Prueba cada candidata y se queda con la que más productos da (una sola vez, sin IA).
        print(f"\n{tienda}{' (navegador)' if lector is not _descargar3 else ''}:")
        mejor, mejor_n, probadas = None, (False, False, 0), []
        for u in orden:
            try:
                ps = extraer(*lector(u))
                n, n_antes = len(ps), sum(1 for x in ps if x["precio_antes"])
            except Exception:
                n = n_antes = -1
            temporal = bool(RE_TEMPORAL.search(urlparse(u).path + "?" + urlparse(u).query))
            print(f"   {n:4} prod {max(n_antes, 0):4} rebajados  {u}   [{opciones[u]}]"
                  + ("  (campaña)" if temporal else ""))
            # Primero, que sea permanente (las páginas de campaña caducan); después, los puntos:
            # un listado de ofertas tiene rebajas; una ficha, no.
            # Antes que nada, que sea la página general y no la de un solo público (niños, teen...).
            puntos = (not RE_SEGMENTO.search(urlparse(u).path), not temporal, n_antes * 3 + n)
            # Menos de MIN_LISTADO productos es una ficha suelta, no un listado de ofertas.
            # A igualdad de lo demás, la página general gana a una más estrecha que apenas la supera
            # (Zalando, 17/11/2026: /rebajas/nike/ con 115 productos ganaba a /rebajas/ con 114). Las
            # candidatas van de la dirección más corta a la más larga: la siguiente tiene que superar
            # a la elegida en más de un 10 %.
            supera = puntos[:2] > mejor_n[:2] or (puntos[:2] == mejor_n[:2] and puntos[2] > mejor_n[2] * 1.1)
            if n >= MIN_LISTADO and supera:
                mejor, mejor_n = u, puntos
            if n >= MIN_LISTADO and n_antes >= 1 and not RE_SEGMENTO.search(urlparse(u).path):
                probadas.append((n_antes * 3 + n, u))
        if solo_extras:
            mejor = guardada or url_tienda(tienda)  # la principal no se toca en el refresco automático
        if mejor:
            if not solo_extras:
                urls[tienda] = mejor
            print(f"   -> elegida: {mejor}")
            # Además de la principal, las otras páginas con rebajas (campañas activas, outlet...).
            extras = [u for _, u in sorted(probadas, reverse=True) if u != mejor][:max_extras]
            if extras:
                urls[f"{tienda}+"] = extras
                for u in extras:
                    print(f"   -> además: {u}")
            else:
                urls.pop(f"{tienda}+", None)
        else:
            print("   -> ninguna página da productos: usa 'volcar' para ver por qué")

    _con_lectores(cfg, tiendas, navegador, trabajo)
    DATOS.mkdir(exist_ok=True)
    URLS.write_text(json.dumps(urls, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nGuardado en {URLS}. Cambia las que no sean la página de ofertas.")
    print('Para una URL distinta solo en una categoría usa la clave "categoria:tienda".')


RE_CLAVE_PRECIO = re.compile(r"pric|preci|pvp|amount|discount|descuent", re.I)
# Solo para `volcar` (pistas para mirar a mano). Antes se llamaba RE_ANTIBOT y, al definirse después, pisaba al
# estricto en todo el programa: "captcha" o "cloudflare" en cualquier script de la página contaban como muro
# (MediaMarkt, 17/11/2026: 13 productos y "reto" por la palabra Captcha de su código).
RE_ANTIBOT_PISTAS = re.compile(r"captcha|cf-chl|cloudflare|datadome|akamai|access denied|perimeterx|incapsula", re.I)


def claves_precio(obj, salida, cont):
    cont[0] += 1
    if cont[0] > 200_000 or len(salida) >= 25:
        return
    if isinstance(obj, dict):
        for k, val in obj.items():
            if (RE_CLAVE_PRECIO.search(k) and len(k) <= 40 and k not in salida
                    and isinstance(val, (str, int, float)) and not isinstance(val, bool)
                    and a_numero(val) is not None and len(str(val)) <= 15):
                salida[k] = str(val)
            if isinstance(val, (dict, list)):
                claves_precio(val, salida, cont)
    elif isinstance(obj, list):
        for val in obj[:50]:
            claves_precio(val, salida, cont)


def volcar(seleccion):
    """Diagnóstico de tiendas que dan 0 productos. Guarda el HTML y resume qué hay dentro.
    Las tiendas de la lista 'navegador' de cualquier categoría se leen con el navegador."""
    cfg = cargar_config()
    con_nav = {t for c in cfg["categorias"].values() for t in c.get("navegador", [])}
    urls = urls_ofertas()

    def trabajo(tienda, lector):
        url = url_de(tienda, urls)
        try:
            html, final, datos = lector(url)
        except Exception as e:
            print(f"{tienda}: ERROR {str(e)[:120]}")
            return
        carpeta = DATOS / "html"
        carpeta.mkdir(parents=True, exist_ok=True)
        archivo = carpeta / (re.sub(r"\W", "_", tienda) + ".html")
        archivo.write_text(html, encoding="utf-8")
        bloques = list(bloques_json(html)) + list(datos)
        claves, cont = {}, [0]
        for b in bloques:
            claves_precio(b, claves, cont)
        visibles = re.sub(r"<script\b.*?</script>|<style\b.*?</style>", "", html, flags=re.S | re.I)
        euros = len(re.findall(r"\d[\d.,]*\s?€", visibles))
        modo = "navegador" if lector is not _descargar3 else "descarga"
        print(f"{tienda}  [{modo}]  ({final})")
        print(f"  tamaño: {len(html) // 1024} KB | bloques JSON: {len(bloques)} "
              f"(de ellos {len(datos)} respuestas de la web) | productos leídos: {len(extraer(html, final, datos))}")
        print(f"  precios con € en el HTML visible: {euros} | itemprop=price: {html.count('itemprop=' + chr(34) + 'price')}")
        antibot = RE_ANTIBOT_PISTAS.search(html)
        if antibot:
            print(f"  posible antibot: '{antibot.group(0)}'")
        if claves:
            print("  claves de precio: " + ", ".join(f"{k}={v}" for k, v in claves.items()))
        print(f"  HTML guardado en {archivo}")

    _con_lectores(cfg, [t for t in seleccion if t not in con_nav],
                  [t for t in seleccion if t in con_nav], trabajo)


MODOS_NAVEGADOR = (  # (nombre, oculto, canal, bloquear imágenes y fuentes)
    ("oculto antiguo", True, None, True),       # Chromium de Playwright sin ventana (chrome-headless-shell)
    ("oculto nuevo", True, "chrome", True),     # Chrome instalado sin ventana (--headless=new)
    ("visible", False, "chrome", True),         # lo que usan hoy las rondas
    ("visible+imágenes", False, "chrome", False),  # por si el scroll necesita las imágenes para cargar más
)


def modos(seleccion, nav_cls=None):
    """Lee la misma página de cada tienda con cada modo del navegador y compara productos, tiempo y retos.
    Una sola página por modo. Si sale un reto, no se resuelve: se paran los modos que quedan de esa tienda."""
    nav_cls = nav_cls or Navegador
    cfg, urls = cargar_config(), urls_ofertas()
    filas = []
    for tienda in seleccion:
        url = tienda if tienda.startswith("http") else url_de(tienda, urls)
        print(f"{tienda}  ({url})")
        for nombre, oculto, canal, bloquear in MODOS_NAVEGADOR:
            n = {**cfg.get("navegador", {}), "visible": not oculto, "canal": canal, "bloquear_recursos": bloquear}
            inicio = time.monotonic()
            try:
                with nav_cls({**cfg, "navegador": n}) as nav:
                    html, final, datos = nav.leer(url)
            except Exception as e:
                print(f"  {nombre:<17} ERROR {str(e)[:100]}")
                filas.append((tienda, nombre, None, None, "error"))
                continue
            seg = round(time.monotonic() - inicio)
            productos = len(extraer(html, final, datos))
            # Reto de verdad: página sin productos (o casi) y con un muro conocido; no la palabra "captcha" suelta.
            reto = RE_ANTIBOT.search(html[:30000]) if productos < 3 else None
            nota = f"reto: '{reto.group(0)}'" if reto else ""
            print(f"  {nombre:<17} {productos:>4} productos  {seg:>3} s  {len(html) // 1024:>5} KB  {nota}")
            filas.append((tienda, nombre, productos, seg, nota))
            if reto:
                print("  reto: no se resuelve; no pruebo más modos en esta tienda (pausa fija de 30 min)")
                break
    with open(DATOS / "modos.txt", "a", encoding="utf-8") as f:
        for fila in filas:
            f.write(datetime.now().strftime("%d/%m %H:%M") + " | " + " | ".join("" if x is None else str(x) for x in fila) + "\n")
    return filas


def prueba(categoria):
    cfg = cargar_config()
    tiendas, navegador = tiendas_de(cfg, categoria)
    urls = urls_ofertas()
    rx = regex_marcas()

    def trabajo(tienda, lector):
        mostrar(*leer_tienda(tienda, urls, categoria, None, paginas_de(cfg, tienda)), " ")

    def mostrar(t, url, productos, error, marca_nav):
        if error:
            print(f"{t:21}{marca_nav} ERROR  {error}")
            return
        con_antes = sum(1 for p in productos if p["precio_antes"])
        de_marca = sum(1 for p in productos if marca_ok(p, rx, t))
        con_ean = sum(1 for p in productos if p["ean"])
        print(f"{t:21}{marca_nav} {len(productos):4} prod | {con_antes:3} con precio anterior | "
              f"{de_marca:3} de marca | {con_ean:3} con EAN")
        for p in productos[:2]:
            print(f"{'':24}- {p['nombre'][:55]} | {p['precio']} € (antes {p['precio_antes']})")

    _con_lectores(cfg, tiendas, [], trabajo)
    if navegador:
        # Igual que la ronda real: varias pestañas a la vez ("pestanas" en tiendas.json).
        try:
            for lectura in leer_con_pestanas(cfg, [(t, url_de(t, urls, categoria), paginas_navegador(cfg, t))
                                                   for t in navegador]):
                mostrar(*lectura, "*")
        except ImportError:
            print("\nTiendas de navegador sin probar: falta Playwright.")
    cat_cfg = cfg["categorias"][categoria]
    for api in (cat_cfg.get("apis", []) + [f"rss:{f}" for f in cat_cfg.get("fuentes", [])]
                + [f"tg:{c}" for c in cat_cfg.get("canales_telegram", [])]):
        t, url, productos, error = leer_api(api)
        if error:
            print(f"{t:21}+ ERROR  {error}")
            continue
        con_antes = sum(1 for p in productos if p["precio_antes"])
        con_res = sum(1 for p in productos if p.get("resenas") is not None)
        print(f"{t:21}+ {len(productos):4} prod | {con_antes:3} con precio anterior | {con_res:3} con reseñas")
        for p in productos[:2]:
            print(f"{'':24}- {p['nombre'][:55]} | {p['precio']} € (antes {p['precio_antes']})")
    if navegador:
        print("(* = leída con navegador)")
    if cat_cfg.get("apis") or cat_cfg.get("fuentes") or cat_cfg.get("canales_telegram"):
        print("(+ = leída por su fuente de datos)")


_CABECERA_TAREA = ("import sys\nfrom pathlib import Path\n"
                   "sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))\n")


def wrappers():
    """tareas/vig_<x>.py: lo que lanza el Programador de tareas con pythonw.exe (sin ventana)."""
    carpeta = BASE / "tareas"
    carpeta.mkdir(exist_ok=True)
    for categoria in cargar_config()["categorias"]:
        (carpeta / f"vig_{categoria}.py").write_text(
            _CABECERA_TAREA + f"from vigilante import ronda\nronda({categoria!r})\n", encoding="utf-8")
        print(f"tareas/vig_{categoria}.py")
    for grupo in GRUPOS:  # las dos tareas unificadas: vig_rapidas.py y vig_navegador.py
        (carpeta / f"vig_{grupo}.py").write_text(
            _CABECERA_TAREA + f"from vigilante import ronda_grupo\nronda_grupo({grupo!r})\n", encoding="utf-8")
        print(f"tareas/vig_{grupo}.py")


def _utf8():
    # La consola de Windows (cp1252) rompe con nombres raros: forzar UTF-8.
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_utf8()

if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 2 and args[0] in ("descubrir", "prueba"):
        {"descubrir": descubrir, "prueba": prueba}[args[0]](args[1])
    elif len(args) >= 2 and args[0] == "volcar":
        volcar(args[1:])
    elif len(args) >= 2 and args[0] == "modos":
        modos(args[1:])
    elif len(args) >= 2 and args[0] == "dossier":
        dossier([a for x in args[1:] for a in re.findall(r"\d+", x)])
    elif len(args) >= 2 and args[0] == "login":
        login(*args[1:])
    elif len(args) >= 2 and args[0] == "historial":
        historial(" ".join(args[1:]))
    elif args[:1] == ["cupones"]:
        listar_cupones()
    elif len(args) >= 3 and args[0] == "cupon":
        comando_cupon(args[1:])
    elif args[:1] == ["informe"]:
        informe(int(args[1]) if len(args) > 1 else 7)
    elif len(args) == 2 and args[0] == "entregar":
        entregar(args[1])
    elif len(args) >= 2 and args[0] == "decidir":
        decidir(args[1], avisar="--callado" not in args, ficha_manual=ficha_de_args(args[2:]))
    elif len(args) >= 2 and args[0] == "muro":
        muro(args[1], avisar="--callado" not in args)
    elif len(args) == 2 and args[0] == "redaccion":
        redaccion(args[1])
    elif len(args) >= 3 and args[0] == "recordar":
        recordar(args[1], args[2], " ".join(args[3:]))
    elif args[:1] == ["chollometro"]:
        hora = next((x for x in args if re.fullmatch(r"\d{1,2}:\d{2}", x)), None)
        args = [x for x in args if x != hora]
        precargar_chollometro(*(int(a) for a in args[1:3]), hasta_hora=hora, desde=args[3] if len(args) > 3 else None,
                              pausa=float(args[4]) if len(args) > 4 else 1.5,
                              listado=args[5] if len(args) > 5 else "destacados",
                              inicio=int(args[6]) if len(args) > 6 else 1)
    elif args[:1] == ["seguir"]:
        seguir_precarga(args[1] if len(args) > 1 else None)
    elif args[:1] == ["sesion_cm"]:
        sesion_cm()
    elif args == ["temas"]:
        crear_temas()
    elif args == ["mapa", "reordenar"]:
        reordenar_mapas()
    elif args[:1] == ["mapa"]:
        mapa(*args[1:])
    elif args[:1] == ["destinos"]:
        destinos_cm(int(args[1]) if len(args) > 1 else 1000)
    elif args[:1] == ["catalogo"]:
        catalogo(*args[1:])
    elif args == ["wrappers"]:
        wrappers()
    elif len(args) == 1 and args[0] in GRUPOS:
        ronda_grupo(args[0])
    elif len(args) == 1:
        ronda(args[0])
    else:
        print(__doc__)
