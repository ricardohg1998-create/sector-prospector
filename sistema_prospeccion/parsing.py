"""Normalización y extracción determinista, sin inferir emails ni necesidades."""
from __future__ import annotations

import csv
import html
import io
import json
import re
import unicodedata
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote

EMAIL = re.compile(r"(?<![\w.+-])([\w.!#$%&'*+/=?^`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63})(?![\w.-])", re.I)
RESTRICTED = {"privacy", "privacidad", "dpo", "rgpd", "noreply", "no-reply", "empleo", "jobs", "careers", "support", "soporte"}
FIELDS = ("name", "subsector", "city", "province", "region", "address", "postal_code", "email", "phone", "website", "contact_url", "country", "relationship", "category", "entity_key", "email_source", "source_cif", "source_brand")
ALIASES = {
    "name": ("name", "nombre", "title", "nombre comercial", "empresa"),
    "email": ("email", "emails", "e-mail", "correo", "correo electronico"),
    "phone": ("phone", "telefono", "telephone"),
    "website": ("website", "web", "sitio web", "url"),
    "address": ("address", "direccion"), "city": ("city", "localidad", "municipio", "ciudad"),
    "province": ("province", "provincia"), "region": ("region", "comunidad"),
    "postal_code": ("postal_code", "codigo postal", "postalcode", "cp"),
    "category": ("category", "categoria"), "country": ("country", "pais"),
}


def norm(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if digits.startswith("0034") and len(digits) == 13:
        digits = digits[4:]
    if digits.startswith("34") and len(digits) == 11:
        digits = digits[2:]
    return "+34" + digits if len(digits) == 9 else str(value or "").strip()


def emails(value):
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    found = EMAIL.findall(unquote(html.unescape(str(value or ""))))
    return sorted({e.lower().strip(".") for e in found if not e.lower().endswith((".png", ".jpg", ".svg", ".webp"))})


def email_kind(address):
    local, _, domain = address.lower().partition("@")
    if domain in {"example.com", "example.org", "example.net"} or domain.endswith((".example", ".invalid", ".test")):
        return "ficticio"
    if local in RESTRICTED or local.startswith(("noreply", "no-reply")):
        return "contextual"
    return "publicado_formato_valido_buzon_desconocido"


def web_url(value, base=""):
    value = str(value or "").strip()
    if not value:
        return ""
    if base:
        value = urljoin(base, value)
    elif not re.match(r"^[a-z][a-z0-9+.-]*:", value, re.I):
        value = "https://" + value
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        return ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


def host(value):
    return (urlsplit(web_url(value)).hostname or "").removeprefix("www.")


def get_path(record, path):
    value = record
    if not path:
        return value
    for key in path.split("."):
        try:
            value = value[int(key)] if isinstance(value, list) else value[key]
        except (KeyError, TypeError, ValueError, IndexError):
            return ""
    return value


def mapping(record, spec):
    mapped = {}
    lookup = {norm(k): v for k, v in record.items()}
    for field in FIELDS:
        if field in spec.get("fields", {}):
            value = get_path(record, spec["fields"][field])
        else:
            value = next((lookup[norm(k)] for k in ALIASES.get(field, (field,)) if norm(k) in lookup), "")
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)
        mapped[field] = str(value or "").strip()
    mapped.update({k: str(v) for k, v in spec.get("defaults", {}).items() if k in FIELDS and not mapped.get(k)})
    return mapped


class Node:
    def __init__(self, tag="root", attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []
        self.parts = []

    def text(self):
        return " ".join(p.text() if isinstance(p, Node) else p for p in self.parts).strip()

    def walk(self):
        for child in self.children:
            yield child
            yield from child.walk()


class DOM(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.current = self.root
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        self.current.parts.append(node)
        if tag not in self.VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                break
            node = node.parent

    def handle_data(self, data):
        self.current.parts.append(data)


def matches(node, token):
    """CSS subset: tag, .class, #id, [attr], [attr=value], descendant, comma."""
    attrs = re.findall(r"\[([\w-]+)(?:=['\"]?([^\]'\"]+)['\"]?)?\]", token)
    token = re.sub(r"\[[^]]*\]", "", token)
    tag = re.match(r"^[\w*-]+", token)
    if tag and tag[0] != "*" and node.tag != tag[0]:
        return False
    ids = re.findall(r"#([\w-]+)", token)
    classes = re.findall(r"\.([\w-]+)", token)
    return (all(node.attrs.get("id") == i for i in ids)
            and all(c in (node.attrs.get("class") or "").split() for c in classes)
            and all(k in node.attrs and (not v or node.attrs.get(k) == v) for k, v in attrs))


def select(root, selector):
    if not selector:
        return [root]
    if any(x in selector for x in (">", "+", "~", ":")):
        raise ValueError("Selector no soportado: usa tag, clase, id, atributo o descendiente")
    result = []
    for alternative in selector.split(","):
        tokens = alternative.strip().split()
        if not tokens:
            continue
        for node in root.walk():
            if not matches(node, tokens[-1]):
                continue
            ancestor = node.parent
            ok = True
            for token in reversed(tokens[:-1]):
                while ancestor and not matches(ancestor, token):
                    ancestor = ancestor.parent
                if not ancestor:
                    ok = False
                    break
                ancestor = ancestor.parent
            if ok and node not in result:
                result.append(node)
    return result


def value_at(root, extractor):
    if isinstance(extractor, str):
        extractor = {"selector": extractor}
    nodes = select(root, extractor.get("selector", ""))
    values = [n.attrs.get(extractor["attr"], "") if extractor.get("attr") else n.text() for n in nodes]
    return " | ".join(v for v in values if v) if extractor.get("all") else next(iter(values), "")


def html_records(text, spec, base):
    dom = DOM(text)
    out = []
    for index, node in enumerate(select(dom.root, spec.get("row_selector", ""))):
        record = {field: value_at(node, extractor) for field, extractor in spec.get("selectors", {}).items() if field in FIELDS}
        record = mapping(record, {"defaults": spec.get("defaults", {})})
        for field in ("website", "contact_url"):
            if record.get(field):
                record[field] = web_url(record[field], base)
        out.append((f"{base}#record={index + 1}", record))
    next_urls = []
    if spec.get("next_selector"):
        next_urls = [web_url(n.attrs.get("href", ""), base) for n in select(dom.root, spec["next_selector"])]
    return out, [u for u in next_urls if u]


def jsonld_records(text, base):
    out = []
    dom = DOM(text)

    def visit(obj):
        if isinstance(obj, list):
            for item in obj:
                visit(item)
        elif isinstance(obj, dict):
            if obj.get("name") and (obj.get("address") or obj.get("telephone") or obj.get("email")):
                address = obj.get("address", {})
                address = address if isinstance(address, dict) else {"streetAddress": address}
                out.append({"name": obj.get("name", ""), "email": obj.get("email", ""),
                    "phone": obj.get("telephone", ""), "website": obj.get("url", ""),
                    "address": address.get("streetAddress", ""), "city": address.get("addressLocality", ""),
                    "province": address.get("addressRegion", ""), "country": address.get("addressCountry", ""),
                    "postal_code": address.get("postalCode", "")})
            for key in ("@graph", "itemListElement", "item", "mainEntity"):
                if key in obj:
                    visit(obj[key])

    for node in dom.root.walk():
        if node.tag == "script" and node.attrs.get("type") == "application/ld+json":
            try:
                visit(json.loads(node.text()))
            except (ValueError, TypeError):
                continue
    return [(f"{base}#jsonld={i+1}", mapping(row, {})) for i, row in enumerate(out)], []


def parse_records(body, spec, base):
    kind = spec["kind"]
    if kind in {"csv", "maps_csv"}:
        try:
            dialect = csv.Sniffer().sniff(body[:8192], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(body.lstrip("\ufeff")), dialect=dialect)
        return [(f"{base}#row={i+2}", mapping(row, spec)) for i, row in enumerate(reader)], []
    if kind in {"json", "jsonl"}:
        rows = [json.loads(line) for line in body.splitlines() if line.strip()] if kind == "jsonl" else get_path(json.loads(body), spec.get("records_path", ""))
        if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
            raise ValueError("La ruta JSON debe contener una lista de objetos")
        return [(f"{base}#record={i+1}", mapping(row, spec)) for i, row in enumerate(rows)], []
    if kind == "html":
        if not spec.get("row_selector") or not spec.get("selectors"):
            raise ValueError("HTML requiere row_selector y selectors; no se adivinan fichas")
        return html_records(body, spec, base)
    if kind == "jsonld":
        return jsonld_records(body, base)
    raise ValueError("Tipo de fuente no soportado")


def contact_page(text, base):
    dom = DOM(text)
    found = set()
    links = []
    visible = [p for p in dom.root.parts if isinstance(p,str)]
    for node in dom.root.walk():
        if node.tag in {"script", "style", "noscript"}:
            continue
        if node.tag == "a":
            href = node.attrs.get("href") or ""
            if href.lower().startswith("mailto:"):
                found.update(emails(href.split("?", 1)[0]))
            if any(w in norm(href + " " + node.text()) for w in ("contact", "aviso legal", "equipo", "about")):
                link = web_url(href, base)
                if link and host(link) == host(base):
                    links.append(link)
        # Only direct text to avoid reintroducing script contents through ancestor.text().
        ancestor = node
        hidden = False
        while ancestor:
            if ancestor.tag in {"script", "style", "noscript"}:
                hidden = True
                break
            ancestor = ancestor.parent
        if not hidden:
            visible.extend(p for p in node.parts if isinstance(p, str))
    found.update(emails(" ".join(visible)))
    return sorted(found), list(dict.fromkeys(links))
