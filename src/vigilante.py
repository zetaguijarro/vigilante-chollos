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
  python vigilante.py entregar <n>                manda la oferta verificada en 5 mensajes (formato móvil)
  python vigilante.py recordar <n> <buena|dudosa|caducada> <motivo>   guarda el veredicto del modelo
                                                (caducada: la oferta se acabó; avisa si vuelve a ese precio)
  python vigilante.py wrappers                crea vig_<categoria>.py para cada categoría
  python vigilante.py <categoria>             ronda normal (la lanza el cron)

Solo usa la librería estándar de Python: no hay que instalar nada.
"""
import gzip
import json
import os
import re
import sqlite3
import sys
import time
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse
from http.cookiejar import CookieJar
import threading
import ssl
from urllib.request import HTTPCookieProcessor, HTTPSHandler, Request, build_opener

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
    if LOG.exists() and LOG.stat().st_size > 1_000_000:
        LOG.replace(LOG.with_suffix(".old.txt"))
    with LOG.open("a", encoding="utf-8") as f:
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


def _descargar(url, timeout):
    req = Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "es-ES,es;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    })
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
    return {t for t in re.findall(r"[a-z0-9]+", s.lower()) if t not in STOP and (len(t) > 1 or t.isdigit())}


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


def extraer(html, base_url, extra_json=()):
    """Productos de una página. extra_json: respuestas JSON capturadas por el navegador."""
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
RE_TEXTO_GENERICO =re.compile(r"^(?:ver|comprar|añadir|acceder|más|mas|detalles|ficha)\b", re.I)


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
        texto = " ".join(unescape(re.sub(r"<[^>]+>", " ", m.group(2))).split())
        f = fichas.setdefault(url, [m.start(), []])
        if texto:
            f[1].append(texto)
        # Tarjeta entera dentro del enlace (marca + nombre + precio): el nombre va en su título <h2>/<h3>.
        f[1] += [" ".join(unescape(re.sub(r"<[^>]+>", " ", x)).split())
                 for x in re.findall(r"<h[2-4]\b[^>]*>(.*?)</h[2-4]>", m.group(2), re.S | re.I)]
        f[1] += [unescape(t) for t in RE_ATR_TITULO.findall(m.group(0))]
    orden = sorted(fichas.items(), key=lambda kv: kv[1][0])
    salida = []
    for n, (url, (ini, nombres)) in enumerate(orden):
        fin = min(ini + tope, orden[n + 1][1][0] if n + 1 < len(orden) else len(plano))
        visible = unescape(re.sub(r"<[^>]+>", " ", RE_TACHADO.sub(" ", plano[ini:fin])))
        visible = RE_PRECIO_PARTIDO.sub(r"\1\2\3", visible)  # Back Market: '684 ,23 €' -> '684,23 €'
        precio = None
        for e in RE_EUROS.finditer(visible):
            antes_txt, despues_txt = visible[max(0, e.start() - 20):e.start()], visible[e.end():e.end() + 16]
            if RE_NO_PRECIO.search(antes_txt) or RE_NO_PRECIO_DESPUES.match(despues_txt):
                continue
            precio = a_numero(e.group(1).replace(" ", ""))
            break
        buenos = [t for t in nombres if 8 <= len(t) <= 250 and re.search(r"[a-zA-Z]{3}", t)
                  and not RE_TEXTO_GENERICO.match(t) and not RE_EUROS.search(t)]
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
                for e in RE_EUROS.findall(unescape(re.sub(r"<[^>]+>", " ", contenido))):
                    v = a_numero(e.replace(" ", ""))
                    if v and p["precio"] * 1.01 < v < p["precio"] * 4:
                        p["precio_antes"] = round(v, 2)
                        break
                if p["precio_antes"]:
                    break
            if p["precio_antes"]:
                break


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
    """)
    columnas = {c[1] for c in con.execute("PRAGMA table_info(lista)")}
    if "enviado" not in columnas:  # bases de versiones anteriores
        con.execute("ALTER TABLE lista ADD COLUMN enviado INTEGER DEFAULT 1")
    if "desde" not in {c[1] for c in con.execute("PRAGMA table_info(vistos)")}:
        # Desde cuándo se sigue el precio de cada producto (histórico propio). Las filas viejas
        # empiezan a contar ahora: no se sabe cuándo se vieron por primera vez.
        con.execute("ALTER TABLE vistos ADD COLUMN desde TEXT")
        con.execute("UPDATE vistos SET desde=?", (datetime.now().isoformat(timespec="minutes"),))
        con.commit()
    return con


ERROR_PRECIO = 0.40


def novedad(con, tienda, p):
    """'nuevo' | 'bajada' (por debajo de su mínimo visto) | None. Guarda el precio."""
    clave = f"{tienda}|{p['url'] or p['nombre'].lower()}"
    ahora = datetime.now().isoformat(timespec="minutes")
    # INSERT OR IGNORE: seguro aunque otra categoría lea la misma tienda a la vez.
    cur = con.execute("INSERT OR IGNORE INTO vistos (clave, precio, minimo, visto, desde) VALUES (?,?,?,?,?)",
                      (clave, p["precio"], p["precio"], ahora, ahora))
    if cur.rowcount == 1:
        p["_seguido_dias"] = 0
        return "nuevo"
    previo, minimo, desde = con.execute("SELECT precio, minimo, desde FROM vistos WHERE clave=?",
                                        (clave,)).fetchone()
    p["_precio_previo"] = previo
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


def limpiar(con):
    limite = (datetime.now() - timedelta(days=OLVIDAR_DIAS)).isoformat(timespec="minutes")
    con.execute("DELETE FROM vistos WHERE visto < ?", (limite,))
    con.execute("DELETE FROM idealo WHERE fecha < ?", (limite,))
    hace_60 = (datetime.now() - timedelta(days=60)).isoformat(timespec="minutes")
    hace_90 = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    con.execute("DELETE FROM lista WHERE fecha < ?", (hace_60,))
    con.execute("DELETE FROM stats WHERE fecha < ?", (hace_90,))

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


def idealo_red(p):
    """Consulta Idealo por red (sin tocar la base de datos: se puede lanzar en paralelo).
    Devuelve el precio, None si no lo encuentra, o False si la consulta falló."""
    consulta = clave_idealo(p)
    if not identificable(p):
        return None  # genérico: cualquier coincidencia sería otro producto
    if _IDEALO_BLOQUEADO[0]:
        return precioreal_red(p)  # ya dio 403 en esta ronda: no insistir producto por producto
    q = quote_plus(p["ean"] or p["nombre"])
    rutas = sorted(IDEALO_BUSQUEDAS, key=lambda r: r != _IDEALO_BUENA[0])  # primero la que ya funcionó
    resultados, fallo = [], None
    for ruta in rutas:
        try:
            html, url = descargar(ruta.format(q=q))
            time.sleep(1.5)
        except Exception as e:
            fallo = e
            if "403" in str(e) or isinstance(e, BloqueadoPorRobots):
                _IDEALO_BLOQUEADO[0] = True
                log(f"idealo no se puede leer ({str(e)[:40]}): el resto de la ronda va directo a PrecioReal")
                break
            continue
        resultados = extraer(html, url)
        if resultados:
            _IDEALO_BUENA[0] = ruta
            break
    if not resultados and fallo is not None:
        if not _IDEALO_BLOQUEADO[0]:
            log(f"idealo {consulta}: {fallo}")
        return precioreal_red(p)  # Idealo bloquea los programas (Akamai, 403): PrecioReal sí lo permite
    candidatos = resultados[:3] if p["ean"] else [r for r in resultados
                                                  if parecido(p["nombre"], r["nombre"]) >= 0.6]
    precios = [r["precio"] for r in candidatos]
    return min(precios) if precios else None


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


def lista_codigos(nombre):
    """Códigos de modelo del nombre, en orden, sin especificaciones (IP54, 256GB, 120Hz, USB3...)."""
    # Al menos 2 cifras: 'BOOK6' o 'PIXEL9' son la familia, no el modelo exacto.
    return [c for c in RE_CODIGO_MODELO.findall(nombre.upper()) if len(c) >= 5 and len(re.findall(r"\d", c)) >= 2
            and not re.fullmatch(r"\d+(?:GB|TB|MB|W|MAH|HZ|MM|CM|L|KG|RPM|H|K|G|P)?", c)
            and not re.fullmatch(r"(?:IP|IPX|USB|HDMI|DDR|PCIE|WIFI|BT|LTE)\d*\w?", c)
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
    nombre = nombre_con_marca(p)
    # Menos de la mitad del precio de la tienda: casi siempre un accesorio con el mismo nombre de
    # modelo (PETKIT Purobot Max Pro 2 a 7,19 € = bolsas), no el producto.
    precios = [r["precio"] for r in extraer(html, url)
               if mismo_modelo(nombre, r["nombre"]) and r["precio"] >= p["precio"] * 0.5]
    return min(precios) if precios else None


# ------------------------------------------------- Chollometro (copia local)
# Su robots.txt PROHÍBE /search (antes se usaba: corregido el 7/11/2026). Sí permite /nuevos (todo lo
# que se publica, ~30 por página) y la portada /?page=N (las destacadas: lo que se ha calentado).
# Se guarda una copia local: sirve para saber si ya está publicado y, sobre todo, qué se calienta.

CM_NUEVOS = "https://www.chollometro.com/nuevos"
CM_PORTADA = "https://www.chollometro.com/"
CM_REFRESCO_MIN = 4      # como mucho una actualización cada 4 minutos
CM_FRESCA_MIN = 30       # sin actualizar en 30 min, "no publicado" pasa a "sin comprobar"
CM_RECIENTE_DIAS = 30    # un post de hace más de 30 días y caducado ya no es duplicado
CM_HOT = 300             # temperatura a partir de la que un post "se calentó"
CM_GUARDAR_DIAS = 400


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
            "caducado": 1 if o.get("isExpired") else 0}
    return list(hilos.values())


def guardar_hilos(con, hilos):
    ahora = datetime.now().isoformat(timespec="minutes")
    con.executemany("""INSERT INTO cm (id, titulo, precio, tienda, host, temp, publicado, caducado, visto)
                       VALUES (:id, :titulo, :precio, :tienda, :host, :temp, :publicado, :caducado, :visto)
                       ON CONFLICT(id) DO UPDATE SET precio=excluded.precio, temp=excluded.temp,
                       caducado=excluded.caducado, visto=excluded.visto""",
                    [{**h, "visto": ahora} for h in hilos])
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
    except Exception as e:
        log(f"chollometro (copia local): {str(e)[:120]}")
        return chollometro_fresca(con)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('cm_ok', ?)", (ahora.isoformat(timespec="minutes"),))
    con.execute("DELETE FROM cm WHERE publicado < ?", (int((ahora - timedelta(days=CM_GUARDAR_DIAS)).timestamp()),))
    con.commit()
    return True


def chollometro_fresca(con):
    fila = con.execute("SELECT v FROM meta WHERE k='cm_ok'").fetchone()
    return bool(fila and datetime.fromisoformat(fila[0]) > datetime.now() - timedelta(minutes=CM_FRESCA_MIN))


def cargar_chollometro(con):
    """Posts guardados (para comparar en memoria) y si la copia está al día."""
    filas = con.execute("SELECT id, titulo, precio, tienda, host, temp, publicado, caducado FROM cm").fetchall()
    claves = ("id", "titulo", "precio", "tienda", "host", "temp", "publicado", "caducado")
    return {"hilos": [dict(zip(claves, f)) for f in filas], "fresca": chollometro_fresca(con)}


def mismo_post(nombre, titulo):
    """¿El post de Chollometro es este producto? Los títulos de Chollometro son cortos ('Nike air force 1
    07 camel') y los de las tiendas largos: se compara sobre el más corto, con el número de modelo igual."""
    ta, tb = tokens(nombre) - COLORES, tokens(titulo) - COLORES
    comunes = ta & tb
    if codigos_modelo(nombre) & codigos_modelo(titulo):
        return True
    if len(comunes) < 2:
        return False
    na, nb = primer_numero(nombre), primer_numero(titulo)
    if na and nb and na != nb and nb not in ta:
        return False
    corto = min(len(ta), len(tb))
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
    for h in copia["hilos"]:
        if not mismo_post(p["nombre"], h["titulo"]):
            continue
        if h["temp"] >= CM_HOT and (hot is None or h["temp"] > hot["temp"]) \
                and mismo_producto_hot(p["nombre"], h["titulo"]):
            hot = h
        if not h["precio"] or (h["publicado"] < limite and h["caducado"]):
            continue
        es_misma = len(tienda) >= 4 and (tienda in h["host"].lower() or tienda in h["tienda"].lower().replace(" ", ""))
        if es_misma:
            if abs(h["precio"] - p["precio"]) <= p["precio"] * 0.02:
                misma.append(None)
            else:
                misma.append(h["precio"])
        else:
            otros.append(h["precio"])
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


def descuento_real(p, mejor):
    """Descuento sobre referencias independientes, no sobre el "antes" de la tienda (regla de Instinct:
    BUENA = al menos un 25 % sobre comparables exactos actuales o sobre un precio inicial verificado).
    -> (descuento, de qué referencia) o (None, None)."""
    refs = []
    if mejor:
        refs.append((mejor, "mercado (mismo modelo en otras tiendas)"))
    h = p.get("_hist") or {}
    if h.get("habitual"):
        refs.append((h["habitual"], "precio habitual en la tienda (PCComTracker)"))
    if p.get("estado") == "bajada" and p.get("_min_previo") and p.get("_seguido_dias", 0) >= 1:
        refs.append((p["_min_previo"], f"mínimo visto por el vigilante en {p['_seguido_dias']} días"))
    if demanda_probada_ok(p) and p.get("precio_antes"):
        refs.append((p["precio_antes"], "precio inicial de la tienda (ya fue chollo caliente a ese precio o más)"))
    mejores = [((r - p["precio"]) / r, etiqueta) for r, etiqueta in refs if r and r > 0]
    return max(mejores) if mejores else (None, None)


def enlace_busqueda_chollometro(nombre):
    """Enlace de búsqueda para que Zeta la haga A MANO (su robots.txt prohíbe /search a los programas:
    el vigilante y Hermes nunca la abren)."""
    return "https://www.chollometro.com/search?q=" + quote_plus(consulta_corta(nombre, 6))


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


def mismo_producto_hot(nombre, titulo):
    """Para "ya fue chollo" no basta con parecerse (mismo_post): ese hilo deja pasar un producto sin
    comparador. Casos reales (15-16/11/2026): monitor Philips a 41 € emparejado con un hilo de 599 €,
    AirPods Max a 130 € con otro de 378,99 €, zapatillas Puma de niña con otro modelo.
    Hace falta el mismo código de modelo o el mismo modelo con la regla estricta del comparador."""
    return bool(codigos_modelo(nombre) & codigos_modelo(titulo)) or mismo_modelo(nombre, titulo)


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
# Tallas de niño en moda y deporte (Spartoo: "Zapatos Niña ... V Inf", "AC+ PS"). Júnior/GS se queda:
# son tallas 35-40 que también compran adultos (Zeta publicó unas Gazelle Bold júnior).
RE_INFANTIL = re.compile(r"(?<!\w)(?:niñ[oa]s?|nin[oa]s?|beb[eé]s?|infantil|inf|ps|td|toddler|baby|kids?|"
                         r"preescolar|pre-?school)(?!\w)", re.I)
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


def precargar_chollometro(dias_nuevos=3, paginas_portada=60, pausa=1.5):
    """Primera carga: /nuevos de los últimos días (duplicados) y la portada hacia atrás (lo que se
    calentó, ~1 día por página). Educada: una página cada `pausa` segundos."""
    con = bd()
    limite = (datetime.now() - timedelta(days=dias_nuevos)).timestamp()
    total = 0
    for base, paginas, hasta in ((CM_NUEVOS, 40 * dias_nuevos + 10, limite), (CM_PORTADA, paginas_portada, 0)):
        n = 0
        for n in range(1, paginas + 1):
            try:
                html, _ = descargar(_pagina(base, n))
            except Exception as e:
                log(f"precarga chollometro {base} p{n}: {e}")
                break
            hilos = hilos_chollometro(html)
            if not hilos:
                break
            guardar_hilos(con, hilos)
            total += len(hilos)
            if min(h["publicado"] for h in hilos) < hasta:
                break
            time.sleep(pausa)
        print(f"{base}: hasta la página {n}, {total} posts guardados")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('cm_ok', ?)", (datetime.now().isoformat(timespec="minutes"),))
    con.commit()
    n_hot = con.execute("SELECT COUNT(*) FROM cm WHERE temp>=?", (CM_HOT,)).fetchone()[0]
    print(f"copia local: {con.execute('SELECT COUNT(*) FROM cm').fetchone()[0]} posts, {n_hot} con {CM_HOT}° o más")
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
    salida = {"minimo": minimo, "habitual": a_numero(m.group(1)) if m else None, "url": final}
    # Enlace directo a la tienda (dentro del /goto/ del comparador, sin visitarlo).
    for h in RE_GOTO.findall(html):
        real = enlace_real(urljoin(final, unescape(h)))
        if real.startswith("http") and "pccomtracker" not in urlparse(real).netloc:
            salida["tienda_url"] = real
            break
    return salida


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
        if trabajos:
            try:
                nav = leer_con_pestanas(cfg, trabajos)
            except ImportError:
                nav = [(t, u, [], "falta playwright (fase 2)") for t, u, _ in trabajos]
            except Exception as e:
                nav = [(t, u, [], f"navegador: {str(e)[:80]}") for t, u, _ in trabajos]
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
    r"sale\b|promo|descuento|black-?friday|cyber", re.I)


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
        if partes.scheme not in ("http", "https") or not partes.netloc.replace("www.", "").endswith(host):
            continue
        ruta = partes.path.rstrip("/")
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


def catalogo_turno(con, cfg, tiendas):
    """[(tienda, url)] de las categorías que tocan en esta ronda: una por tienda, por turnos, y solo si
    hace más de 'catalogo_cada_min' (60 por defecto) que se leyó la anterior de esa tienda."""
    cada_def = int((cfg or {}).get("catalogo_cada_min", 60))
    por_tienda = (cfg or {}).get("catalogo_por_tienda", {})  # {"pccomponentes.com": {"cada_min": 15, "por_ronda": 3}}
    cat = urls_catalogo()
    ahora = datetime.now()
    salida = []
    for t in tiendas:
        lista = cat.get(t) or []
        if not lista:
            continue
        cada = int(por_tienda.get(t, {}).get("cada_min", cada_def))
        cuantas = min(len(lista), max(1, int(por_tienda.get(t, {}).get("por_ronda", 1))))
        fila = con.execute("SELECT v FROM meta WHERE k=?", (f"cat_ultimo:{t}",)).fetchone()
        if fila and datetime.fromisoformat(fila[0]) > ahora - timedelta(minutes=cada):
            continue
        fila = con.execute("SELECT v FROM meta WHERE k=?", (f"cat_turno:{t}",)).fetchone()
        i = int(fila[0]) if fila else 0
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (f"cat_turno:{t}", str((i + cuantas) % len(lista))))
        con.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",
                    (f"cat_ultimo:{t}", ahora.isoformat(timespec="minutes")))
        salida += [(t, lista[(i + k) % len(lista)]) for k in range(cuantas)]
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

    def apuntar(t, html, final):
        lista = [u for u in categorias_de_portada(html, final) if permitido(u)]
        if lista:
            guardado[t] = lista
        print(f"{t:30} {len(lista):3} categorías")

    normales = [t for t in elegidas if t not in con_nav]
    with ThreadPoolExecutor(max_workers=6) as ex:
        for t, res in zip(normales, ex.map(lambda t: _seguro(descargar, url_tienda(t)), normales)):
            if isinstance(res, Exception):
                print(f"{t:30} no se lee: {str(res)[:70]}")
            else:
                apuntar(t, *res)
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
                                 f"que ha cambiado sus enlaces: mira vigilante_data/log.txt antes de pedir 'verifica'.")
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
                           boton_verificar(n)):
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
    idealo = (f"Mercado: {p['idealo']:g} € (otras tiendas)" if p.get("idealo")
              else "Mercado: sin comparar con otras tiendas")
    if p.get("_hist"):
        h = p["_hist"]
        hist = (f"Histórico (comparador): mínimo {h['minimo']:g} €"
                + (f" · habitual {h['habitual']:g} €" if h.get("habitual") else ""))
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
    return (f"#{n} · {p['tienda']}{baja}\n{p['nombre']}\n{p['precio']:g} €{socio}{antes}{resenas}\n"
            f"{idealo}\nChollometro: {ch}\nBúsqueda manual: {enlace_busqueda_chollometro(p['nombre'])}\n"
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


def enviar_telegram(token, chat_id, texto, foto=None, teclado=None):
    base = f"https://api.telegram.org/bot{token}/"
    extra = {"reply_markup": teclado} if teclado else {}
    if foto and len(texto) <= 1024:
        try:
            if _post(base + "sendPhoto", {"chat_id": chat_id, "photo": foto, "caption": texto, **extra}).get("ok"):
                return True
        except Exception as e:  # foto que Telegram no acepta: va sin foto
            log(f"telegram foto: {str(e)[:80]}")
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
    return True, "producto y precio confirmados en la ficha"


RE_META_DESC = re.compile(r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']{20,})', re.I)
RE_AGOTADO = re.compile(r"agotado|sin stock|no disponible|out of stock|avísame cuando", re.I)
RE_ENVIO = re.compile(r"[^.<>\n]{0,60}(?:env[ií]o|entrega)[^.<>\n]{0,80}", re.I)


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
            salida = {"minimo": r["minimo"], "fuente": urlparse(r["url"]).netloc.replace("www.", ""),
                      "url": r["url"], "es_minimo": item.get("p") is not None and item["p"] <= r["minimo"] * 1.03}
            if r.get("habitual"):
                salida["habitual"] = r["habitual"]
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
    cfg = cargar_config()
    con_nav = {t for c in cfg["categorias"].values() for t in c.get("navegador", [])}
    con = bd()
    con_nav |= {k.split(":", 1)[1] for (k,) in con.execute("SELECT k FROM meta WHERE k LIKE 'a_navegador:%'")}
    # "num" es el número de la lista (#N); "id" dentro de cada candidato es el precio de Idealo.
    salida, filas = [], []
    for n in ids:
        fila = con.execute("SELECT id, base, datos FROM lista WHERE id=?", (int(n),)).fetchone()
        salida.append({"num": int(n), "error": "no existe"} if not fila else None)
        if fila:
            filas.append((len(salida) - 1, *fila))

    def con_lector(nav):
        for pos, i, base, datos in filas:
            c = json.loads(datos)
            item = {"num": i, **c}
            m = con.execute("SELECT veredicto, motivo, fecha FROM modelos WHERE base=?", (base,)).fetchone()
            if m:
                item["memoria"] = f"{m[0]} ({m[2][:10]}): {m[1]}"
            if c.get("u"):
                # La ficha de la tienda puede fallar (PcComponentes: antibot); lo demás se hace igual.
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
                try:
                    ahora = item["ficha"].get("precio_ahora")
                    c_ch = estado_chollometro({"nombre": c["n"], "tienda": c["t"], "precio": ahora or c["p"]})
                    item["ch_ahora"] = c_ch
                    if c_ch == "misma":
                        item["aviso_ch"] = "ya está publicado en Chollometro en esta tienda: descártalo"
                    if ahora and abs(ahora - c["p"]) > 0.01:
                        item["aviso"] = f"el precio ha cambiado: {c['p']:g} € -> {ahora:g} €"
                    h = historico(cfg, {**c, "p": ahora or c["p"]})
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
    if any(json.loads(d).get("t") in con_nav for _, _, _, d in filas):
        try:
            with Navegador(cfg) as nav:
                con_lector(nav)
        except ImportError:
            con_lector(None)
    else:
        con_lector(None)
    con.close()
    print(json.dumps(salida, ensure_ascii=False, separators=(",", ":")))


BLOQUES = ("titulo", "precios", "info", "enlace", "descripcion")


def entregar(n):
    """Formato móvil: la IA deja vigilante_data/entrega_N.json con los 5 bloques y el script
    los manda como 5 mensajes (el primero con la foto). Así la IA no repite texto y el formato
    sale siempre igual."""
    archivo = DATOS / f"entrega_{int(n)}.json"
    try:
        b = json.loads(archivo.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        print(f"#{n}: no puedo leer {archivo.name}: {e}")
        return False
    faltan = [k for k in BLOQUES if not str(b.get(k, "")).strip()]
    if faltan:
        print(f"#{n}: faltan bloques: {', '.join(faltan)}")
        return False
    con = bd()
    fila = con.execute("SELECT datos FROM lista WHERE id=?", (int(n),)).fetchone()
    con.close()
    d = json.loads(fila[0]) if fila else {}
    foto = b.get("foto") or d.get("img")
    token, chat = token_telegram(), str(cargar_config().get("entrega", {}).get("chat_id", ""))
    if not (token and chat):
        print("falta token o chat_id")
        return False
    ok = enviar_telegram(token, chat, str(b["titulo"]).strip(), foto)
    for k in BLOQUES[1:]:
        ok = enviar_telegram(token, chat, str(b[k]).strip()) and ok
    print(f"#{n} entregado en 5 mensajes" if ok else f"#{n}: algún mensaje no se pudo enviar")
    if ok:
        archivo.unlink(missing_ok=True)
    return ok


def recordar(n, veredicto, motivo):
    """Guarda el veredicto de un modelo para no investigarlo otra vez (30 días)."""
    con = bd()
    fila = con.execute("SELECT base FROM lista WHERE id=?", (int(n),)).fetchone()
    if not fila:
        print(f"#{n} no existe")
        return
    con.execute("INSERT OR REPLACE INTO modelos VALUES (?,?,?,?)",
                (fila[0], veredicto.lower(), motivo[:300], datetime.now().isoformat(timespec="minutes")))
    con.commit()
    con.close()
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
    return x <= precio * 1.02


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


def registrar_fallos(con, lecturas):
    """3 fallos seguidos -> pausa de 30 min; cada fallo más la duplica, hasta 24 h.
    Un acierto lo reinicia. Así no se pierde tiempo ni se insiste con tiendas que bloquean."""
    for tienda, _, productos, error in lecturas:
        clave = f"fallos:{tienda}"
        if not error:
            con.execute("DELETE FROM meta WHERE k IN (?,?,?)", (clave, f"pausa:{tienda}", f"no_se_lee:{tienda}"))
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
                enviar_telegram(token, chat, texto)
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
         "dr": (f"-{round(p['dto_real'] * 100)} % sobre {p['ref_real']}" if p.get("dto_real") else None)}
    if p["estado"] == "bajada":
        c["baja"] = 1
    return {k: v for k, v in c.items() if v is not None}


GRUPOS = {"rapidas": "descarga", "navegador": "navegador"}


def ronda_grupo(grupo):
    """Una sola tarea para todas las categorías, separadas por forma de lectura: 'rapidas' (descarga normal
    y fuentes de datos, cada pocos minutos) o 'navegador' (las tiendas que necesitan Chrome). Cada tienda
    sigue con las reglas de su categoría; solo cambia cuándo se lee."""
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
    turno = catalogo_turno(con, cfg, tiendas + navegador)
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
    registrar_fallos(con, lecturas)
    anotar_vacias(con, cfg, lecturas, set(tiendas))
    for tienda, url_cat, productos, error in todas:
        if (tienda, url_cat) in de_catalogo:
            if error:  # una categoría que no se puede leer no aparta la tienda: sale de su lista de categorías
                if RE_NO_SE_LEE.search(error) or re.search(r"\b(?:404|410)\b", error):
                    quitar_de_catalogo(tienda, url_cat, error)
                else:
                    log(f"catálogo {tienda} {url_cat[:80]}: {error[:80]}")
            else:
                lecturas.append((tienda, url_cat, productos, None))

    # 1) Filtros gratis: novedad, marca, precio y descuento de la tienda.
    previos = []
    vigilar_sesiones(con, cfg, lecturas)
    for tienda, url_leida, productos, error in lecturas:
        estadistica(con, tienda, leidos=len(productos), errores=1 if error else 0)
        modo = "navegador" if tienda in navegador else "descarga"
        if error:
            log(f"{categoria} {tienda}: {error}")
            if rec:
                recorrido(f"    {tienda} [{modo}] {(url_leida or '')[:90]} -> ERROR: {str(error)[:100]}")
            continue
        n_cambios, n_previos = 0, len(previos)
        # Se siembra por página leída: una URL nueva de la misma tienda no inunda de avisos.
        pagina = f"{tienda}|{url_leida}"
        sembrada = con.execute("SELECT 1 FROM sembradas WHERE tienda=?", (pagina,)).fetchone()
        for p in productos:
            estado = novedad(con, tienda, p)
            if sembrada and not estado and vuelve_oferta_caducada(con, p):
                estado = "vuelve"
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
            if categoria in f.get("sin_infantil_en", []) and RE_INFANTIL.search(p["nombre"]):
                continue
            if r["marca"] and not marca_ok(p, rx, tienda):
                continue
            d = (1 - p["precio"] / p["precio_antes"]) if p["precio_antes"] else None
            if p.get("gratis"):
                d = 1.0  # regalo: 100 % de descuento
            # Posible error de precio: cae un 40 % o más respecto a su mínimo visto, aunque la
            # tienda no muestre precio tachado. Son los chollos que más se calientan.
            # Hace falta un mínimo de seguimiento: un precio visto hace una hora no es referencia.
            err = (p.get("_min_previo") and p["precio"] <= p["_min_previo"] * (1 - ERROR_PRECIO)
                   and p.get("_seguido_dias", 0) >= f.get("error_precio_dias_min", 0))
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
                            "socio": bool(cfg.get("reglas_tienda", {}).get(tienda, {}).get("socio"))})
        if not sembrada and productos:
            con.execute("INSERT OR IGNORE INTO sembradas VALUES (?)", (pagina,))
        if rec:
            con_antes = sum(1 for x in productos if x.get("precio_antes"))
            nota = "" if sembrada else " (primera lectura de esta página: solo memoriza)"
            recorrido(f"    {tienda} [{modo}] {(url_leida or '')[:90]} -> {len(productos)} productos, "
                      f"{con_antes} con precio anterior, {n_cambios} nuevos o más baratos, "
                      f"{len(previos) - n_previos} pasan los filtros de tienda{nota}")
    limpiar(con)
    con.commit()

    # 2) Mismo producto en varias tiendas: quedarse con el más barato.
    unicos = {}
    for p in previos:
        k = p["ean"] or " ".join(sorted(tokens(p["nombre"])))
        if k not in unicos or p["precio"] < unicos[k]["precio"]:
            unicos[k] = p
    previos = sorted(unicos.values(), key=lambda x: -(x["d"] or 0))

    # 3) Idealo (con caché) solo para los mejores. Las consultas que faltan van 3 a la vez.
    cat_cfg = cfg["categorias"][categoria]
    sin_idealo = cat_cfg.get("sin_idealo", False)  # viajes y juegos digitales: Idealo no aplica
    lote = previos[:max_idealo]
    precios_idealo, pendientes = {}, []
    for i, p in enumerate(lote):
        if sin_idealo:
            continue
        hay, precio = idealo_cache(con, p)
        if hay:
            precios_idealo[i] = precio
        else:
            pendientes.append(i)
    with ThreadPoolExecutor(max_workers=3) as ex:
        for i, mejor in zip(pendientes, ex.map(lambda i: idealo_red(lote[i]), pendientes)):
            if mejor is not False:
                con.execute("INSERT OR REPLACE INTO idealo VALUES (?,?,?)",
                            (clave_idealo(lote[i]), mejor, datetime.now().isoformat(timespec="minutes")))
            precios_idealo[i] = mejor or None
    finales = []
    copia = None
    if lote:  # copia local al día (como mucho una lectura cada 4 min, compartida por todas las categorías)
        actualizar_chollometro(con)
        copia = cargar_chollometro(con)
    for i, p in enumerate(lote):
        mejor = precios_idealo.get(i)
        d = p["d"]
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
            ok = p.get("error_precio") or p["precio"] <= mejor * (1 - cat_cfg.get("min_bajo_mercado", min_bajo))
        elif p.get("historico_url") and (ref := _historico_seguro(p["historico_url"])):
            # Sin precio de mercado pero con el historial de la propia tienda (PcComponentes por PCComTracker):
            # cerca de su mínimo, o claramente por debajo de lo que suele costar.
            p["_hist"] = ref
            if ref.get("tienda_url") and raiz(p["tienda"]) not in urlparse(p.get("url") or "").netloc:
                p["url"] = ref["tienda_url"]  # el aviso lleva a la tienda, no al comparador
            ok = p["precio"] <= ref["minimo"] * 1.03 or bool(ref.get("habitual") and p["precio"] <= ref["habitual"] * 0.9)
        elif cat_cfg.get("exigir_precio_mercado", f.get("exigir_precio_mercado", True)) and not p.get("error_precio"):
            # Sin precio de mercado no se puede saber si el "antes" de la tienda es real (Sony WF-C710N:
            # "antes 199 €" con el mercado a 71,95 €). Regla de Zeta: sin contraste claro, no se envía.
            # El histórico propio sí es contraste: baja de lo más barato visto en semanas.
            ok = bool(dias_hist) and historial_propio_ok(p, dias_hist, bajada_hist)
        else:
            # Categorías sin comparador (moda, hogar, belleza...): sin precio de mercado, solo con mucho
            # descuento visible ("descuento_min_sin_mercado" de la categoría; si no, el global).
            if dias_hist:
                ok = p.get("error_precio") or historial_propio_ok(p, dias_hist, bajada_hist)
            else:
                ok = d is not None and d >= cat_cfg.get("descuento_min_sin_mercado", desc_sin_idealo)
        if not ok and mejor is None and not p.get("gratis") and demanda_probada_ok(p):
            # Sin comparador, pero la comunidad ya lo calentó a este precio o más caro: precio y demanda probados.
            ok = True
        if ok and not p.get("gratis") and precio_sospechoso(p, mejor, f.get("precio_sospechoso", PRECIO_SOSPECHOSO)):
            log(f"precio sospechoso, no se envía: {p['tienda']} {p['nombre'][:60]} a {p['precio']} €")
            ok = False
        ok = ok and ahorro_suficiente(p, mejor, cat_cfg, f)
        exento = p.get("gratis") or p.get("error_precio") or (sin_idealo and not dias_hist)
        dto_min = cat_cfg.get("descuento_real_min", f.get("descuento_real_min", 0))
        if ok and dto_min and not exento:
            dto, ref = descuento_real(p, mejor)
            ok = dto is not None and dto >= dto_min
            if ok:
                p["dto_real"], p["ref_real"] = round(dto, 3), ref
        if ok:
            p["idealo"] = mejor
            # Ordena por lo bueno que es: descuento + ventaja frente al mercado + error + demanda en Chollometro.
            hot = (p.get("_cm_hot") or {}).get("temp", 0)
            p["score"] = ((d or 0) + ((mejor - p["precio"]) / mejor if mejor else 0)
                          + (1 if p.get("error_precio") else 0) + min(hot, 3000) / 3000)
            finales.append(p)
    con.commit()

    # 4) Memoria (modelos ya juzgados o ya enviados) y Chollometro, todo por código.
    entrega = cfg.get("entrega", {})
    modo_telegram = entrega.get("modo") == "telegram"
    finales.sort(key=lambda x: -x["score"])
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


def descubrir(categoria):
    cfg = cargar_config()
    tiendas, navegador = tiendas_de(cfg, categoria)
    urls = urls_ofertas()

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
        mejor, mejor_n = None, (False, False, 0)
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
            if n >= MIN_LISTADO and puntos > mejor_n:
                mejor, mejor_n = u, puntos
        if mejor:
            urls[tienda] = mejor
            print(f"   -> elegida: {mejor}")
        else:
            print("   -> ninguna página da productos: usa 'volcar' para ver por qué")

    _con_lectores(cfg, tiendas, navegador, trabajo)
    DATOS.mkdir(exist_ok=True)
    URLS.write_text(json.dumps(urls, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nGuardado en {URLS}. Cambia las que no sean la página de ofertas.")
    print('Para una URL distinta solo en una categoría usa la clave "categoria:tienda".')


RE_CLAVE_PRECIO = re.compile(r"pric|preci|pvp|amount|discount|descuent", re.I)
RE_ANTIBOT = re.compile(r"captcha|cf-chl|cloudflare|datadome|akamai|access denied|perimeterx|incapsula", re.I)


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
        antibot = RE_ANTIBOT.search(html)
        if antibot:
            print(f"  posible antibot: '{antibot.group(0)}'")
        if claves:
            print("  claves de precio: " + ", ".join(f"{k}={v}" for k, v in claves.items()))
        print(f"  HTML guardado en {archivo}")

    _con_lectores(cfg, [t for t in seleccion if t not in con_nav],
                  [t for t in seleccion if t in con_nav], trabajo)


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
    elif len(args) >= 2 and args[0] == "dossier":
        dossier([a for x in args[1:] for a in re.findall(r"\d+", x)])
    elif len(args) >= 2 and args[0] == "login":
        login(*args[1:])
    elif args[:1] == ["informe"]:
        informe(int(args[1]) if len(args) > 1 else 7)
    elif len(args) == 2 and args[0] == "entregar":
        entregar(args[1])
    elif len(args) >= 3 and args[0] == "recordar":
        recordar(args[1], args[2], " ".join(args[3:]))
    elif args[:1] == ["chollometro"]:
        precargar_chollometro(*(int(a) for a in args[1:3]))
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
