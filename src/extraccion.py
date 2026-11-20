"""Extracción declarativa por tienda (revisión del 20/11/2026, propuesta 3): título y marca de cada tarjeta según unas reglas
en `config/extraccion.json`, para que una tienda nueva o un cambio de HTML no obligue a tocar el motor. El extractor
genérico sigue de reserva: estas reglas solo corrigen el nombre y la marca de los productos que ya ha encontrado.

Selectores sencillos, sin dependencias (solo la biblioteca estándar):
  etiqueta, .clase, etiqueta.clase, [atributo], [atributo=valor], [atributo*=trozo], [atributo^=inicio]
  separados por espacios = descendiente. Al final, "@atributo" saca un atributo en vez del texto.

Reglas por tienda:
  {"zalando.es": {"tarjeta": "article", "enlace": "a@href", "marca": "h3 span:0", "titulo": "h3 span:1"}}
`:N` toma el N-ésimo resultado (desde 0); sin él, el primero. `titulo` puede ser una lista de selectores: se unen con espacio.
"""
import json
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

REGLAS = Path(__file__).resolve().parent.parent / "config" / "extraccion.json"
VACIAS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class Nodo:
    __slots__ = ("tag", "attrs", "hijos", "padre", "textos")

    def __init__(self, tag, attrs=None, padre=None):
        self.tag, self.attrs, self.padre = tag, dict(attrs or {}), padre
        self.hijos, self.textos = [], []

    def texto(self):
        partes = list(self.textos)
        for h in self.hijos:
            partes.append(h.texto())
        return " ".join(" ".join(partes).split())

    def descendientes(self):
        for h in self.hijos:
            yield h
            yield from h.descendientes()


class _Arbol(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.raiz = Nodo("#raiz")
        self.actual = self.raiz
        self.ignorar = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "svg", "noscript"):
            self.ignorar += 1
            return
        if self.ignorar:
            return
        n = Nodo(tag, [(k, v or "") for k, v in attrs], self.actual)
        self.actual.hijos.append(n)
        if tag not in VACIAS:
            self.actual = n

    def handle_startendtag(self, tag, attrs):
        if not self.ignorar and tag not in ("script", "style", "svg", "noscript"):
            self.actual.hijos.append(Nodo(tag, [(k, v or "") for k, v in attrs], self.actual))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "svg", "noscript"):
            self.ignorar = max(0, self.ignorar - 1)
            return
        if self.ignorar or tag in VACIAS:
            return
        n = self.actual
        while n is not self.raiz and n.tag != tag:  # HTML mal cerrado: se sube hasta la etiqueta que se cierra
            n = n.padre
        if n is not self.raiz:
            self.actual = n.padre

    def handle_data(self, data):
        if not self.ignorar and data.strip():
            self.actual.textos.append(data)


def arbol(html):
    p = _Arbol()
    try:
        p.feed(html or "")
        p.close()
    except Exception:
        pass
    return p.raiz


def _partir_compuesto(c):
    """'a.tarjeta[data-x=1]' -> ('a', ['tarjeta'], [('data-x', '=', '1')])"""
    tag, clases, attrs, i = "", [], [], 0
    while i < len(c):
        ch = c[i]
        if ch == ".":
            j = i + 1
            while j < len(c) and c[j] not in ".[":
                j += 1
            clases.append(c[i + 1:j])
            i = j
        elif ch == "[":
            j = c.index("]", i)
            dentro = c[i + 1:j]
            for op in ("*=", "^=", "="):
                if op in dentro:
                    k, val = dentro.split(op, 1)
                    attrs.append((k.strip(), op, val.strip().strip("\"'")))
                    break
            else:
                attrs.append((dentro.strip(), "", ""))
            i = j + 1
        else:
            j = i
            while j < len(c) and c[j] not in ".[":
                j += 1
            tag = c[i:j].lower()
            i = j
    return tag, clases, attrs


def _encaja(n, compuesto):
    tag, clases, attrs = compuesto
    if tag and n.tag != tag:
        return False
    if clases:
        suyas = set((n.attrs.get("class") or "").split())
        if not all(c in suyas for c in clases):
            return False
    for k, op, val in attrs:
        if k not in n.attrs:
            return False
        v = n.attrs.get(k) or ""
        if (op == "=" and v != val) or (op == "*=" and val not in v) or (op == "^=" and not v.startswith(val)):
            return False
    return True


def seleccionar(nodo, selector):
    """Nodos que encajan con el selector (descendientes), en orden de documento."""
    actuales = [nodo]
    for parte in selector.split():
        comp = _partir_compuesto(parte)
        siguientes, vistos = [], set()
        for a in actuales:
            for d in a.descendientes():
                if id(d) not in vistos and _encaja(d, comp):
                    vistos.add(id(d))
                    siguientes.append(d)
        actuales = siguientes
    return actuales


def valor(nodo, selector):
    """Texto (o atributo con "@atributo") del resultado indicado del selector; None si no hay."""
    if not selector:
        return None
    selector, _, atributo = selector.partition("@")
    indice = 0
    if ":" in selector.split()[-1]:
        selector, _, n = selector.rpartition(":")
        indice = int(n) if n.isdigit() else 0
    encontrados = seleccionar(nodo, selector.strip()) if selector.strip() else [nodo]
    if len(encontrados) <= indice:
        return None
    e = encontrados[indice]
    t = (e.attrs.get(atributo.strip()) or "") if atributo else e.texto()
    t = " ".join(unescape(t).split())
    return t or None


def reglas_de(tienda, archivo=None):
    try:
        datos = json.loads((archivo or REGLAS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return datos.get(tienda)


def tarjetas(html, base_url, reglas):
    """[{url, titulo, marca}] de cada tarjeta según las reglas de la tienda."""
    raiz = arbol(html)
    salida = []
    for t in seleccionar(raiz, reglas["tarjeta"]):
        href = valor(t, reglas.get("enlace", "a@href"))
        if not href:
            continue
        sel_titulo = reglas.get("titulo")
        partes = [valor(t, s) for s in (sel_titulo if isinstance(sel_titulo, list) else [sel_titulo])]
        titulo = " ".join(x for x in partes if x)
        marca = valor(t, reglas.get("marca"))
        if titulo:
            salida.append({"url": urljoin(base_url, href), "titulo": titulo, "marca": marca})
    return salida


def nombre_con_marca(titulo, marca):
    if marca and marca.lower() not in titulo.lower():
        return f"{marca} {titulo}"
    return titulo
