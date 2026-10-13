"""Repeatable source recipes and offline extraction from saved captures.

This module never fetches URLs as part of recipe loading or capture extraction.
Network-facing helpers return prepared commands by default and require an
explicit ``execute=True`` to run them.
"""
from __future__ import annotations

import hashlib
import ast
import importlib.util
import inspect
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Any, Callable, Iterable
from urllib.parse import unquote, urljoin, urlsplit

from .parsing import DOM, emails, host, mapping, norm, parse_records, select, web_url

ROOT = Path(__file__).resolve().parent
RECIPES_DIR = ROOT / "recipes"
MAX_BATCH = 1000


def list_recipes() -> list[dict[str, str]]:
    """Return small recipe catalog entries without reading capture data."""
    out = []
    for path in sorted(RECIPES_DIR.glob("*.json")):
        recipe = _load_json(path)
        out.append({"id": recipe["id"], "name": recipe.get("name", recipe["id"]), "path": str(path)})
    return out


def load_recipe(path_or_sector: str | Path) -> dict[str, Any]:
    """Load a recipe by file path, id, sector name, or declared alias."""
    value = str(path_or_sector)
    requested = Path(value)
    if requested.is_file():
        path = requested
    else:
        path = RECIPES_DIR / (value + ".json")
        if not path.is_file():
            key = value.casefold().strip()
            path = next((p for p in sorted(RECIPES_DIR.glob("*.json"))
                         if (lambda r: key == str(r.get("sector", "")).casefold()
                             or key in {str(a).casefold() for a in r.get("aliases", [])})(_load_json(p))), None)
        if path is None:
            raise ValueError(f"Receta o sector desconocido: {value}")
    recipe = _load_json(path)
    if not isinstance(recipe.get("id"), str) or not isinstance(recipe.get("sources"), list):
        raise ValueError(f"Receta mal formada: {path}")
    shared_filters = recipe.get("candidate_filters", {})
    for source in recipe["sources"]:
        source["filters"] = {**shared_filters, **source.get("filters", {})}
        source["_recipe_id"] = recipe["id"]
    return recipe


def get_recipe(recipe_id: str) -> dict[str, Any]:
    return load_recipe(recipe_id)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"No se pudo leer {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Se esperaba objeto JSON: {path}")
    return value


def _capture_bytes(item: dict[str, Any]) -> bytes:
    path = Path(item.get("path", ""))
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"captura no disponible: {path}: {exc}") from exc
    actual = hashlib.sha256(raw).hexdigest()
    expected = item.get("sha256")
    if expected and actual.casefold() != str(expected).casefold():
        raise ValueError(f"SHA-256 no coincide para {path}: esperado {expected}, obtenido {actual}")
    return raw


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _figaro_cards(body: str, source: dict[str, Any], url: str) -> list[dict[str, Any]]:
    dom = DOM(body)
    rows = []
    for idx, card in enumerate(select(dom.root, source.get("row_selector", "div.card")), 1):
        ps = select(card, source.get("address_text_selector", "p"))
        hs = select(card, source.get("name_selector", "h6"))
        name = " ".join(hs[0].text().split()) if hs else ""
        raw_text = " ".join(ps[0].text().split()) if ps else ""
        email = next(iter(emails(raw_text)), "")
        zip_match = re.search(r"(?<!\d)(\d{5})(?!\d)", raw_text)
        postal = zip_match.group(1) if zip_match else ""
        city = ""
        if zip_match:
            city = re.split(r"\s+-\s+|\s+(?:T\.|/)", raw_text[zip_match.end():], maxsplit=1)[0].strip(" ,;.-")
        phone_match = re.search(r"(?i)\bT\.?\s*/?\s*([0-9][0-9+() .-]*)", raw_text.split(email, 1)[0] if email else raw_text)
        phone = re.sub(r"\D", "", phone_match.group(1)) if phone_match else ""
        before = raw_text.split(phone_match.group(0), 1)[0] if phone_match else raw_text
        address = before.split(email, 1)[0].strip(" /,;.-") if email else before.strip(" /,;.-")
        website = ""
        for a in select(card, "a"):
            href = a.attrs.get("href", "").strip()
            if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
                website = web_url(href if "://" in href else "https://" + href, url) or ""
        rows.append({"name": name, "subsector": "peluquería", "address": address, "city": city,
                     "postal_code": postal, "email": email, "phone": phone, "website": website,
                     "_filter_text": name + " " + raw_text,
                     "contact_url": url, "email_source": url if email else "", "relationship": "",
                     "_locator": f"{url}#card={idx}", "_selection_status": "candidato_pendiente_de_revision"})
    return rows


def _skeyndor_markers(body: str, source: dict[str, Any], url: str) -> list[dict[str, Any]]:
    pattern = source.get("payload_regex", r"setMarkers\('((?:\\.|[^'\\])*)'\s*\)")
    matches = list(re.finditer(pattern, body, re.S))
    if len(matches) != 1:
        raise ValueError(f"Se esperaba un único payload setMarkers; encontrados: {len(matches)}")
    literal = "'" + matches[0].group(1) + "'"
    # JS string escape sequences used by the saved payload are compatible with
    # JSON after decoding; avoid Python literal evaluation on source text.
    payload = ast.literal_eval(literal)
    rows = json.loads(payload)
    if not isinstance(rows, list):
        raise ValueError("setMarkers no contiene una lista JSON")
    out = []
    for idx, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            continue
        fields = source.get("fields", {})
        record = {target: row.get(origin, "") for target, origin in fields.items()}
        record.setdefault("contact_url", url)
        record["email_source"] = url if record.get("email") else ""
        record["_filter_text"] = " ".join(str(record.get(field, "")) for field in ("name", "address", "city", "category", "state"))
        record["_locator"] = f"{url}#marker={idx}"
        record["_selection_status"] = "candidato_pendiente_de_revision"
        out.append(record)
    return out


def _localbeauty_cards(body: str, source: dict[str, Any], url: str) -> list[dict[str, Any]]:
    dom = DOM(body)
    rows = []
    selector = source.get("row_selector", "div.item_item")
    for idx, card in enumerate(select(dom.root, selector), 1):
        h = select(card, source.get("name_selector", "h3"))
        name = " ".join(h[0].text().split()) if h else ""
        href = ""
        for a in select(card, "a"):
            href = a.attrs.get("href", "")
            if "/es-es/i/" in href:
                break
        contact_prefix = source.get("contact_block_prefix", "item_contact_")
        contact_nodes = [n for n in card.walk() if n.tag == "div" and n.attrs.get("id", "").startswith(contact_prefix)]
        contact_text = " ".join(contact_nodes[0].text().split()) if contact_nodes else ""
        email = next(iter(emails(contact_text)), "")
        phone_match = re.search(r"(?:\+?\d[\d ()-]{7,}\d)", contact_text)
        addr_match = re.search(r"[^|\n]*\b\d{5}\b[^|\n]*", contact_text)
        detail_url = web_url(href, url) if href else ""
        address = addr_match.group(0).strip() if addr_match else ""
        postal_match = re.search(r"\b(\d{5})\b", address)
        rows.append({"name": name, "subsector": "peluquería y belleza", "email": email,
                     "phone": phone_match.group(0).strip() if phone_match else "",
                     "address": address, "postal_code": postal_match.group(1) if postal_match else "",
                     "contact_url": detail_url or url, "related_urls": [detail_url] if detail_url else [],
                     "_filter_text": " ".join([url, *card.text().split()]),
                     "email_source": url if email else "",
                     "website": "", "_locator": f"{url}#card={idx}",
                     "_selection_status": "candidato_pendiente_de_revision"})
    return rows


def _localbeauty_discovery(body: str, source: dict[str, Any], url: str) -> list[dict[str, Any]]:
    dom = DOM(body)
    prefixes = tuple(source.get("listing_prefixes", ["/es-es/z/peluquerias/", "/es-es/z/centros-de-estetica/"]))
    found = []
    for anchor in select(dom.root, "a"):
        href = anchor.attrs.get("href", "").strip()
        if not href:
            continue
        target = web_url(href, url)
        path = urlsplit(target).path if target else ""
        if target and host(target) == host(url) and any(path.startswith(prefix) for prefix in prefixes) and target not in found:
            found.append(target)
    return [{"_discovery_url": target, "_locator": f"{url}#listing={index}",
             "_selection_status": "discovery_url"} for index, target in enumerate(found, 1)]


def _localbeauty_detail(body: str, source: dict[str, Any], url: str) -> list[dict[str, Any]]:
    dom = DOM(body)
    headings = select(dom.root, source.get("name_selector", "h1"))
    if not headings:
        raise ValueError("No se encontró h1 de ficha")
    name = " ".join(headings[0].text().split())
    addr_nodes = select(dom.root, source.get("address_selector", ".item-address"))
    block_nodes = select(dom.root, source.get("contact_selector", ".item-contact-buttons"))
    if not block_nodes or not addr_nodes:
        raise ValueError("Falta el bloque propio de contacto o dirección")
    contact = block_nodes[0]
    emails_found, phones, websites = [], [], []
    for a in contact.walk():
        if a.tag != "a":
            continue
        href = a.attrs.get("href", "")
        if href.casefold().startswith("mailto:"):
            emails_found.extend(emails(unquote(href[7:].split("?", 1)[0])))
        elif href.casefold().startswith("tel:"):
            phones.append(unquote(href[4:]))
        elif href.startswith(("http://", "https://")) and "localbeauty.es" not in href:
            websites.append(href)
    email = next(iter(dict.fromkeys(emails_found)), "")
    own_categories = []
    for anchor in select(dom.root, "a"):
        href = anchor.attrs.get("href", "")
        if "/es-es/z/" in href:
            label = " ".join(anchor.text().split()) or href.split("/es-es/z/", 1)[1].split("/", 1)[0]
            if label not in own_categories:
                own_categories.append(label)
        if len(own_categories) >= 6:
            break
    address = " ".join(addr_nodes[0].text().split())
    postal_match = re.search(r"\b(\d{5})\b", address)
    filter_text = " ".join([name, address, *own_categories])
    return [{"name": name, "subsector": "peluquería y belleza", "address": address,
             "postal_code": postal_match.group(1) if postal_match else "",
             "email": email, "phone": phones[0] if phones else "", "website": websites[0] if websites else "",
             "category": " | ".join(own_categories), "_filter_text": filter_text,
             "contact_url": url, "email_source": url if email else "", "_locator": url + "#ficha=1",
             "_selection_status": "candidato_pendiente_de_revision"}]


_CUSTOM = {"figaro_cards": _figaro_cards, "skeyndor_markers": _skeyndor_markers,
           "localbeauty_cards": _localbeauty_cards, "localbeauty_detail": _localbeauty_detail,
           "localbeauty_discovery": _localbeauty_discovery}


def _parse_capture(raw: bytes, capture: dict[str, Any], source: dict[str, Any]) -> list[dict[str, Any]]:
    url = web_url(capture.get("url") or capture.get("requested_url") or source.get("url", ""))
    if not url:
        raise ValueError("la captura no declara una URL HTTP(S)")
    body = _text(raw)
    parser = source.get("parser", "generic")
    if parser in _CUSTOM:
        rows = _CUSTOM[parser](body, source.get("parser_config", {}), url)
    elif parser == "generic":
        kind = source.get("kind", "html")
        parsed, _ = parse_records(body, {**source.get("config", {}), "kind": kind}, url)
        rows = []
        for locator, rec in parsed:
            rec = dict(rec); rec["_locator"] = locator; rec["_selection_status"] = "candidato_pendiente_de_revision"
            rows.append(rec)
    elif parser == "sitemap":
        dom = DOM(body)
        rows = [{"name": "", "contact_url": n.text().strip(), "_locator": url + f"#url={i}",
                 "_selection_status": "discovery_url"} for i, n in enumerate(select(dom.root, "loc"), 1) if n.text().strip()]
    else:
        raise ValueError(f"parser no soportado: {parser}")
    defaults = source.get("defaults", {})
    result = []
    for row in rows:
        if row.get("_discovery_url") or row.get("_selection_status") == "discovery_url":
            target = row.get("_discovery_url") or row.get("contact_url", "")
            requested_url = web_url(capture.get("requested_url") or url) or url
            result.append({"record_type": "discovery", "source_id": source.get("id", ""),
                           "source_group": source.get("source_group", ""), "source_url": requested_url, "final_url": url,
                           "discovered_url": web_url(target, url),
                           "source_capture": str(capture.get("path", "")),
                           "source_sha256": hashlib.sha256(raw).hexdigest(),
                           "locator": row.get("_locator", url), "scope_status": "discovery_url",
                           "scope": source.get("scope", {}), "publication_verified": False,
                           "publication_basis": ""})
            continue
        business = mapping(row, {"defaults": defaults})
        filter_row = {**row, "_discovery_context": capture.get("discovery_context", {})}
        filter_status, reason = _filter_candidate(filter_row, source)
        if filter_status in {"excluded", "pending_review"}:
            result.append({**business, "record_type": filter_status, "reason": reason,
                           "source_id": source.get("id", ""), "source_group": source.get("source_group", ""),
                           "source_url": url, "source_capture": str(capture.get("path", "")),
                           "source_sha256": hashlib.sha256(raw).hexdigest(), "locator": row.get("_locator", url),
                           "scope_status": filter_status, "scope": source.get("scope", {}),
                           "publication_verified": bool(source.get("publication_verified", False) and business.get("email")),
                           "publication_basis": source.get("publication_basis", "") if business.get("email") else ""})
            continue
        result.append({**business, "source_id": source.get("id", ""), "source_group": source.get("source_group", ""),
                       "record_type": "business_candidate",
                       "discovered_urls": row.get("related_urls", []),
                       "discovery_context": capture.get("discovery_context", {}),
                       "source_url": web_url(capture.get("requested_url") or url) or url, "final_url": url,
                       "source_capture": str(capture.get("path", "")),
                       "source_sha256": hashlib.sha256(raw).hexdigest(), "locator": row.get("_locator", url),
                       "scope_status": "scope_reviewed_candidate" if source.get("scope", {}).get("verified") else "candidato_pendiente_de_revision",
                       "scope": source.get("scope", {}),
                       "publication_verified": bool(source.get("publication_verified", False)),
                       "publication_basis": source.get("publication_basis", "") if business.get("email") else ""})
    return result


def _filter_candidate(row: dict[str, Any], source: dict[str, Any]) -> tuple[str, str]:
    """Apply only reviewed deterministic terms; never infer with a model."""
    if not row.get("name", "").strip():
        return "pending_review", "nombre no disponible en la captura"
    rules = source.get("filters", {})
    haystack = norm(" ".join(str(row.get(key, "")) for key in
                               ("name", "subsector", "category", "address", "_filter_text")))
    for group, terms in rules.get("exclude_any", {}).items():
        for term in terms:
            needle = norm(term)
            if needle and needle in haystack:
                return "excluded", f"{group}: {term}"
    for group, terms in rules.get("pending_any", {}).items():
        for term in terms:
            needle = norm(term)
            if needle and needle in haystack:
                return "pending_review", f"{group}: {term}"
    required = [norm(term) for term in rules.get("require_any", []) if norm(term)]
    if required and not any(term in haystack for term in required):
        return "pending_review", "actividad objetivo no explícita en nombre o categoría"
    discovery_context = row.get("_discovery_context") or {}
    if source.get("id") == "localbeauty-fichas" and not row.get("category") and not discovery_context.get("verified"):
        return "pending_review", "ficha sin categoría propia ni listado sectorial de origen"
    if not source.get("scope", {}).get("verified"):
        return "pending_review", "el alcance de la fuente no está verificado"
    return "business_candidate", ""


def extract_capture_batch(recipe_id: str, captures: Iterable[dict[str, Any]], *, batch_size: int = 50,
                          limit: int | None = None, offset: int = 0,
                          source_ids: set[str] | None = None) -> dict[str, Any]:
    """Replay saved captures in bounded batches; each bad snapshot is reported."""
    if type(batch_size) is not int or not 1 <= batch_size <= MAX_BATCH:
        raise ValueError(f"batch_size debe estar entre 1 y {MAX_BATCH}")
    if limit is not None and (type(limit) is not int or not 1 <= limit <= MAX_BATCH):
        raise ValueError(f"limit debe estar entre 1 y {MAX_BATCH}")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset debe ser un entero >= 0")
    recipe = load_recipe(recipe_id)
    source_map = {s["id"]: s for s in recipe["sources"]}
    selected = [s for s in recipe["sources"] if source_ids is None or s["id"] in source_ids]
    if source_ids and set(source_ids) - set(source_map):
        raise ValueError("source_ids contiene una fuente desconocida")
    entries = list(captures)
    total = len(entries)
    entries = entries[offset:offset + limit if limit is not None else None]
    records, discoveries, excluded, pending, errors, processed = [], [], [], [], [], 0
    batch_counts = []
    for start in range(0, len(entries), batch_size):
        chunk_count = 0
        for capture in entries[start:start + batch_size]:
            if limit is not None and processed >= limit:
                break
            processed += 1
            try:
                raw = _capture_bytes(capture)
                matches = [s for s in selected if _source_matches_capture(s, capture)]
                if not matches:
                    continue
                for source in matches:
                    parsed = _parse_capture(raw, capture, source)
                    business = [row for row in parsed if row.get("record_type") != "discovery"]
                    discoveries.extend(row for row in parsed if row.get("record_type") == "discovery")
                    records.extend(row for row in business if row.get("record_type") == "business_candidate")
                    excluded.extend(row for row in business if row.get("record_type") == "excluded")
                    pending.extend(row for row in business if row.get("record_type") == "pending_review")
                    chunk_count += len(parsed)
            except Exception as exc:
                errors.append({"capture": str(capture.get("path", "")), "url": capture.get("url", capture.get("requested_url", "")),
                               "error": str(exc), "type": type(exc).__name__})
        batch_counts.append(chunk_count)
        if limit is not None and processed >= limit:
            break
    next_offset = offset + processed if offset + processed < total else None
    return {"recipe_id": recipe_id, "captures_seen": processed, "captures_total": total,
            "offset": offset, "next_offset": next_offset, "records": records, "discoveries": discoveries,
            "excluded": excluded, "pending": pending, "errors": errors,
            "counts": {"business_candidates": len(records), "discoveries": len(discoveries),
                       "excluded": len(excluded), "pending_review": len(pending), "errors": len(errors)},
            "batch_records": batch_counts, "network_requests": 0,
            "provenance_fields": ["source_url", "source_capture", "source_sha256", "locator"]}


def import_maps_csv(path: str | Path, *, defaults: dict[str, Any] | None = None,
                    max_rows: int = MAX_BATCH) -> dict[str, Any]:
    """Use the engine's existing maps_csv field mapping without registering CRM data."""
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_BATCH:
        raise ValueError(f"max_rows debe estar entre 1 y {MAX_BATCH}")
    path = Path(path)
    raw = path.read_bytes()
    text = _text(raw)
    rows, _ = parse_records(text, {"kind": "maps_csv", "defaults": defaults or {}}, "file:" + path.name)
    if len(rows) > max_rows:
        raise ValueError(f"El CSV de Maps contiene {len(rows)} filas; máximo configurado: {max_rows}")
    records = []
    for locator, row in rows:
        records.append({**row, "source_id": "google-maps", "source_group": "google-maps",
                        "source_url": "", "source_capture": str(path), "source_sha256": hashlib.sha256(raw).hexdigest(),
                        "locator": locator, "scope_status": "candidato_pendiente_de_revision",
                        "publication_verified": False, "publication_basis": ""})
    return {"records": records, "count": len(records), "network_requests": 0,
            "source_sha256": hashlib.sha256(raw).hexdigest()}


def run_source(store: Any, source: dict[str, Any], budget: int, *, network: bool = False,
               captures: Iterable[dict[str, Any]] | None = None,
               work_fn: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Validate and run one route or native engine source under a request budget.

    Offline mode can import a native local file or replay supplied route
    captures. URL routes are only queued. Network mode is explicit and delegates
    to the existing resumable worker; captured-page routes use capture jobs and
    parse the resulting snapshots afterwards.
    """
    validation = validate_source(source)
    if not validation["valid"]:
        raise ValueError("Fuente no válida: " + "; ".join(validation["errors"]))
    if type(budget) is not int or not 1 <= budget <= MAX_BATCH:
        raise ValueError(f"budget debe estar entre 1 y {MAX_BATCH}")
    spec = dict(source)
    parser = spec.get("parser")
    supplied = list(captures if captures is not None else spec.get("captures", []))
    if parser and parser != "generic":
        existing = _capture_results_for_source(store, spec) if hasattr(store, "capture_results") else []
        discovery_entries = _resolve_discovery_entries(store, spec, limit=budget)
        discovery_context = {_capture_url_key(row): row.get("sector_context", {}) for row in discovery_entries}
        result_captures = _attach_discovery_context(_unique_captures([*supplied, *existing]), discovery_context)
        parsed = {"records": [], "discoveries": [], "excluded": [], "pending": [], "errors": []}
        processed = set(_read_route_state(store, spec["id"])["processed_captures"])
        capture_batch, processed_now = _new_capture_batch(store, spec["id"], result_captures, budget, processed=processed)
        if capture_batch:
            parsed = _extract_source_batch(spec, capture_batch, budget)
            processed.update(processed_now)
        urls = spec.get("start_urls") or ([spec["url"]] if spec.get("url") and not spec.get("url_prefix") else [])
        if not urls:
            urls = [row["discovered_url"] for row in discovery_entries if web_url(row.get("discovered_url", ""))]
        if not urls:
            if result_captures:
                outbox = _persist_source_batch(store, spec, parsed, processed_now)
                _write_discovery_state(store, spec, _discovery_entries(spec, parsed), processed_captures=processed_now)
                return _route_result(spec, parsed, requests=0, jobs=[], status="replayed",
                                     processed_captures=len(processed_now), state_file=str(_route_state_path(store, spec["id"]) or ""),
                                     **outbox)
            return {"source_id": spec["id"], "records": [], "discoveries": [], "count": 0, "errors": [],
                    "jobs": [], "requests": 0, "network_requests": 0, "status": "needs_discovery",
                    "discovery_source_id": spec.get("discovery_source_id", "")}
        if any(not web_url(url) for url in urls):
            raise ValueError("start_urls/discoveries debe contener solo URLs HTTP(S)")
        relevant_saved = {_capture_url_key(c) for c in result_captures}
        missing_urls = [web_url(url) for url in urls if _capture_url_key({"url": url}) not in relevant_saved]
        jobs_before = _pending_jobs(store, spec["id"])
        request_origins = {f"{urlsplit(job['url']).scheme}://{urlsplit(job['url']).netloc}"
                           for job in jobs_before if web_url(job.get("url", ""))}
        selected_urls = []
        queued_pages = len(jobs_before)
        for url in missing_urls:
            parsed_url = urlsplit(url)
            origin = f"{parsed_url.scheme}://{parsed_url.netloc}"
            candidate_origins = request_origins | {origin}
            if queued_pages + len(selected_urls) + 1 + len(candidate_origins) > budget:
                continue
            selected_urls.append(url)
            request_origins = candidate_origins
        enqueue = getattr(store, "enqueue", None)
        queued = sum(bool(enqueue("capture", url, source_id=spec["id"])) for url in selected_urls) if callable(enqueue) else 0
        jobs_before = _pending_jobs(store, spec["id"])
        if not network:
            if result_captures:
                outbox = _persist_source_batch(store, spec, parsed, processed_now)
                _write_discovery_state(store, spec, _discovery_entries(spec, parsed), processed_captures=processed_now)
                return _route_result(spec, parsed, requests=0, jobs=jobs_before,
                                     status="replayed_and_queued" if queued else "replayed", queued=queued,
                                     processed_captures=len(processed_now), state_file=str(_route_state_path(store, spec["id"]) or ""),
                                     **outbox)
            return {"source_id": spec["id"], "records": [], "discoveries": [],
                    "discovered_urls": [row.get("discovered_url", "") for row in discovery_entries[:budget]],
                    "count": 0, "discovery_count": len(discovery_entries), "excluded": [], "pending": [],
                    "errors": [], "jobs": jobs_before, "queued": queued, "requests": 0,
                    "network_requests": 0, "status": "queued", "discovery_source_id": spec.get("discovery_source_id", ""),
                    "discovery_inventory_count": len(discovery_entries)}
        if not queued and not jobs_before:
            outbox = _persist_source_batch(store, spec, parsed, processed_now)
            if result_captures:
                _write_discovery_state(store, spec, _discovery_entries(spec, parsed), processed_captures=processed_now)
            return _route_result(spec, parsed,
                                 requests=0, jobs=[], status="no_pending_jobs", processed_captures=len(processed_now),
                                 state_file=str(_route_state_path(store, spec["id"]) or ""), **outbox)
        # ``budget`` caps useful page captures. HTTP also requests robots.txt
        # once for every fresh origin, so reserve that explicit overhead.
        request_urls = [job["url"] for job in jobs_before] + selected_urls
        origins = {f"{urlsplit(url).scheme}://{urlsplit(url).netloc}" for url in request_urls if web_url(url)}
        worker_budget = budget
        page_budget = max(0, budget - len(origins))
        work_result = _run_worker(store, worker_budget, "capture", work_fn)
        completed = _capture_results_for_source(store, spec)
        combined = _attach_discovery_context(_unique_captures([*supplied, *completed]), discovery_context)
        extra_batch, extra_processed = _new_capture_batch(store, spec["id"], combined,
                                                          max(0, budget - len(processed_now)), processed=processed)
        if extra_batch:
            extra_parsed = _extract_source_batch(spec, extra_batch, budget)
            for key in ("records", "discoveries", "excluded", "pending", "errors"):
                parsed[key].extend(extra_parsed.get(key, []))
            processed.update(extra_processed)
        entries = _discovery_entries(spec, parsed)
        outbox = _persist_source_batch(store, spec, parsed, [*processed_now, *extra_processed])
        _write_discovery_state(store, spec, entries, processed_captures=[*processed_now, *extra_processed])
        return _route_result(spec, parsed, requests=work_result.get("requests", 0),
                             jobs=_pending_jobs(store, spec["id"]), status="captured", worker=work_result,
                             captures=len(completed), queued=queued, page_budget=page_budget,
                             request_budget=worker_budget, reserved_robots_requests=len(origins),
                             processed_captures=len(processed_now) + len(extra_processed),
                             state_file=str(_route_state_path(store, spec["id"]) or ""), **outbox)
    # A standard engine source accepts the existing native source schema.
    if spec.get("kind") not in {"csv", "maps_csv", "json", "jsonl", "html", "jsonld"}:
        raise ValueError("source debe declarar kind nativo o parser de ruta compatible")
    existing = store.db.execute("SELECT spec FROM sources WHERE id=?", (spec["id"],)).fetchone()
    if existing is None:
        store.add_source(spec)
    elif json.loads(existing["spec"]) != spec:
        raise ValueError("El id ya está registrado con otra configuración de fuente")
    if spec.get("path"):
        if network:
            raise ValueError("La fuente local no requiere red; usa network=False")
        counts = store.ingest_file(spec["id"], spec["path"])
        store.write_progress()
        return {"source_id": spec["id"], "records": [], "count": sum(counts.values()), "counts": counts,
                "errors": [], "jobs": [], "requests": 0, "network_requests": 0, "status": "imported"}
    if not spec.get("url"):
        raise ValueError("La fuente necesita path local o url HTTP(S)")
    queued = int(store.queue_source(spec["id"]))
    if not network:
        return {"source_id": spec["id"], "records": [], "count": 0, "errors": [], "jobs": [],
                "queued": queued, "requests": 0, "network_requests": 0, "status": "queued",
                "resumable_jobs": _pending_jobs(store, spec["id"])}
    work_result = _run_worker(store, budget, "directory", work_fn)
    return {"source_id": spec["id"], "records": [], "count": 0, "errors": [], "jobs": _pending_jobs(store, spec["id"]),
            "queued": queued, "requests": work_result.get("requests", 0),
            "network_requests": work_result.get("requests", 0), "worker": work_result}


def validate_source(source: dict[str, Any]) -> dict[str, Any]:
    """Validate a recipe source without network access or store mutation."""
    errors: list[str] = []
    if not isinstance(source, dict):
        return {"valid": False, "errors": ["source debe ser objeto JSON"], "network_requests": 0}
    source_id = source.get("id", "")
    if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,70}", source_id):
        errors.append("id debe usar minúsculas, números, guiones o guion bajo (máximo 71 caracteres)")
    parser = source.get("parser")
    route_parser = parser in _CUSTOM or parser == "sitemap"
    if parser and not route_parser and parser != "generic":
        errors.append(f"parser desconocido: {parser}")
    if route_parser:
        if not (source.get("url") or source.get("url_prefix") or source.get("start_urls")):
            errors.append("la ruta requiere url, url_prefix o start_urls")
        for key in ("url", "url_prefix"):
            if source.get(key) and not web_url(source[key]):
                errors.append(f"{key} debe ser HTTP(S) sin credenciales")
        config = source.get("parser_config", {})
        if not isinstance(config, dict):
            errors.append("parser_config debe ser objeto")
        start_urls = source.get("start_urls", [])
        if not isinstance(start_urls, list) or any(not web_url(url) for url in start_urls):
            errors.append("start_urls debe ser una lista de URLs HTTP(S)")
    else:
        if source.get("kind") not in {"csv", "maps_csv", "json", "jsonl", "html", "jsonld"}:
            errors.append("kind nativo debe ser csv, maps_csv, json, jsonl, html o jsonld")
        if not source.get("path") and not web_url(source.get("url", "")):
            errors.append("la fuente nativa requiere path local o url HTTP(S)")
        if source.get("kind") == "html" and (not source.get("row_selector") or not source.get("selectors")):
            errors.append("HTML requiere row_selector y selectors")
    scope = source.get("scope", {})
    if not isinstance(scope, dict):
        errors.append("scope debe ser objeto")
    elif type(scope.get("verified", False)) is not bool:
        errors.append("scope.verified debe ser booleano")
    elif scope.get("verified") and (not scope.get("basis") or scope.get("country") != "ES" or not scope.get("sector")):
        errors.append("scope verificado requiere country, sector y basis")
    elif scope.get("verified") and route_parser:
        filters = source.get("filters", {})
        if not isinstance(filters, dict) or not filters.get("exclude_any"):
            errors.append("una ruta de alcance verificado requiere filtros deterministas exclude_any")
    if type(source.get("publication_verified", False)) is not bool:
        errors.append("publication_verified debe ser booleano")
    if source.get("publication_verified") and not source.get("publication_basis"):
        errors.append("publication_verified requiere publication_basis")
    return {"valid": not errors, "errors": errors, "source_id": source_id,
            "adapter": parser or source.get("kind", ""), "network_required": bool(source.get("url") or source.get("url_prefix") or source.get("start_urls")),
            "network_requests": 0}


def _run_worker(store: Any, budget: int, kind: str, work_fn: Callable[..., Any] | None) -> dict[str, Any]:
    if store.config.get("fixture_mode"):
        raise ValueError("Una ejecución ficticia no permite red real")
    if work_fn is None:
        from .network import work as work_fn
    kwargs = {"budget": budget, "kind": kind}
    try:
        parameters = inspect.signature(work_fn).parameters.values()
        if any(parameter.name == "continue_after_target" or parameter.kind == inspect.Parameter.VAR_KEYWORD
               for parameter in parameters):
            kwargs["continue_after_target"] = True
    except (TypeError, ValueError):
        # Keep injected callables without an inspectable signature compatible.
        pass
    return work_fn(store, **kwargs)


def _pending_jobs(store: Any, source_id: str) -> list[dict[str, Any]]:
    try:
        rows = store.db.execute("SELECT id,kind,url,state,attempts FROM jobs WHERE source_id=? AND state IN ('queued','retry','running') ORDER BY rowid", (source_id,))
        return [dict(row) for row in rows]
    except (AttributeError, TypeError):
        return []


def _capture_url_key(capture: dict[str, Any]) -> str:
    return (web_url(capture.get("requested_url") or capture.get("url") or capture.get("discovered_url") or "") or "").rstrip("/").casefold()


def _capture_identity(capture: dict[str, Any]) -> str:
    url = _capture_url_key(capture)
    digest = str(capture.get("sha256", "")).casefold()
    if not digest and capture.get("path"):
        try:
            digest = hashlib.sha256(Path(capture["path"]).read_bytes()).hexdigest()
        except OSError:
            digest = ""
    return f"{url}|{digest or capture.get('path', '')}"


def _new_capture_batch(store: Any, source_id: str, captures: list[dict[str, Any]], limit: int,
                       *, processed: set[str] | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    if limit <= 0:
        return [], []
    done = processed if processed is not None else set(_read_route_state(store, source_id)["processed_captures"])
    selected, identities = [], []
    for capture in captures:
        identity = _capture_identity(capture)
        if identity in done:
            continue
        selected.append(capture)
        identities.append(identity)
        done.add(identity)
        if len(selected) >= limit:
            break
    return selected, identities


def _unique_captures(captures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique = {}
    for capture in captures:
        key = (str(capture.get("path", "")), str(capture.get("sha256", "")))
        if key == ("", ""):
            key = (_capture_url_key(capture), "")
        unique.setdefault(key, capture)
    return list(unique.values())


def _source_matches_capture(source: dict[str, Any], capture: dict[str, Any]) -> bool:
    requested = web_url(capture.get("requested_url") or capture.get("url") or "")
    final = web_url(capture.get("url") or capture.get("requested_url") or "")
    if not requested or not final:
        return False
    origin = web_url(source.get("url_prefix", "") or source.get("url", ""))
    if origin and host(final) != host(origin):
        return False
    prefix = web_url(source.get("url_prefix", ""))
    if prefix:
        if not requested.startswith(prefix) or not final.startswith(prefix) or host(final) != host(requested):
            return False
        path_prefixes = source.get("verified_path_prefixes", [])
        if path_prefixes:
            from urllib.parse import urlsplit
            path = urlsplit(requested).path
            return any(path.startswith(item) for item in path_prefixes)
        return True
    exact_urls = [web_url(source.get("url", "")), *(web_url(x) for x in source.get("start_urls", []))]
    return requested in {item for item in exact_urls if item} and host(final) == host(requested)


def _capture_results_for_source(store: Any, source: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        rows = store.capture_results()
    except AttributeError:
        return []
    return [row for row in rows if _source_matches_capture(source, row)]


def _attach_discovery_context(captures: list[dict[str, Any]], contexts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach verified parent-listing lineage to matching detail captures."""
    output = []
    for capture in captures:
        context = contexts.get(_capture_url_key(capture))
        output.append({**capture, "discovery_context": context} if context else capture)
    return output


def _route_state_path(store: Any, source_id: str) -> Path | None:
    run = getattr(store, "run", None)
    if not run:
        return None
    safe_id = re.sub(r"[^a-z0-9_-]", "_", source_id.casefold())
    return Path(run) / "rutas" / f"discovery-{safe_id}.json"


def _read_route_state(store: Any, source_id: str) -> dict[str, Any]:
    path = _route_state_path(store, source_id)
    if not path or not path.is_file():
        return {"urls": [], "processed_captures": []}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"urls": [], "processed_captures": []}
    if not isinstance(state, dict) or state.get("source_id") != source_id:
        return {"urls": [], "processed_captures": []}
    urls = state.get("urls", [])
    processed = state.get("processed_captures", [])
    return {"urls": [row for row in urls if isinstance(row, dict)] if isinstance(urls, list) else [],
            "processed_captures": [str(item) for item in processed] if isinstance(processed, list) else []}


def _read_discovery_state(store: Any, source_id: str) -> list[dict[str, Any]]:
    state = _read_route_state(store, source_id)
    return [row for row in state["urls"] if isinstance(row, dict) and web_url(row.get("discovered_url", ""))]


def _write_discovery_state(store: Any, source: dict[str, Any], entries: list[dict[str, Any]],
                           *, processed_captures: Iterable[str] = ()) -> None:
    path = _route_state_path(store, source["id"])
    if not path:
        return
    old_state = _read_route_state(store, source["id"])
    old = old_state["urls"]
    merged = {web_url(row.get("discovered_url", "")): row for row in old}
    for row in entries:
        url = web_url(row.get("discovered_url", ""))
        if url:
            merged[url] = row
    path.parent.mkdir(parents=True, exist_ok=True)
    processed = list(dict.fromkeys([*old_state["processed_captures"], *processed_captures]))
    payload = {"schema": 1, "source_id": source["id"], "recipe_id": source.get("_recipe_id", ""),
               "urls": list(merged.values()), "processed_captures": processed}
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def _outbox_directory(run_or_store: Any) -> Path | None:
    run = getattr(run_or_store, "run", None)
    if run is None and isinstance(run_or_store, (str, Path)):
        run = run_or_store
    if not isinstance(run, (str, Path)) or not run:
        return None
    return Path(run) / "rutas" / "outbox"


def _persist_source_batch(store: Any, source: dict[str, Any], parsed: dict[str, Any],
                          capture_identities: list[str]) -> dict[str, str]:
    """Durably write route output before its captures become processed."""
    if not capture_identities:
        return {}
    directory = _outbox_directory(store)
    if directory is None:
        return {}
    stable_rows = lambda key: sorted(parsed.get(key, []), key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True))
    payload = {"schema": 1, "source_id": source["id"], "recipe_id": source.get("_recipe_id", ""),
               "source_spec": {key: source[key] for key in ("id", "scope", "publication_verified", "publication_basis", "source_group", "role", "defaults") if key in source},
               "capture_identities": sorted(set(capture_identities)),
               "records": stable_rows("records"), "discoveries": stable_rows("discoveries"),
               "excluded": stable_rows("excluded"), "pending": stable_rows("pending"),
               "errors": stable_rows("errors")}
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    batch_id = hashlib.sha256(content.encode("utf-8")).hexdigest()
    payload["batch_id"] = batch_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{batch_id}.json"
    if not path.exists():
        temp = path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    return {"batch_id": batch_id, "outbox_path": str(path)}


def _validate_source_batch(payload: Any, batch_id: str) -> None:
    if not isinstance(payload, dict) or payload.get('batch_id') != batch_id:
        raise ValueError(f'Lote no válido: {batch_id}')
    content = json.dumps({key: value for key, value in payload.items() if key != 'batch_id'}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if hashlib.sha256(content.encode('utf-8')).hexdigest() != batch_id:
        raise ValueError(f'Huella del lote no coincide: {batch_id}')


def pending_source_batches(run_or_store: Any) -> list[dict[str, Any]]:
    """Return unacknowledged extraction batches for campaign recovery."""
    directory = _outbox_directory(run_or_store)
    if not directory or not directory.is_dir():
        return []
    batches = []
    for path in sorted(directory.glob("*.json")):
        if path.with_suffix(".ack").exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Outbox ilegible {path}: {exc}") from exc
        _validate_source_batch(payload, path.stem)
        payload["outbox_path"] = str(path)
        batches.append(payload)
    return batches


def load_source_batch(run_or_store: Any, batch_id: str) -> dict[str, Any]:
    """Load one durable extraction batch by its content-derived id."""
    if not re.fullmatch(r"[a-f0-9]{64}", str(batch_id)):
        raise ValueError("batch_id no válido")
    directory = _outbox_directory(run_or_store)
    if not directory:
        raise ValueError("No hay directorio de ejecución para el outbox")
    path = directory / f"{batch_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"No se pudo cargar el lote {batch_id}: {exc}") from exc
    _validate_source_batch(payload, batch_id)
    payload["outbox_path"] = str(path)
    return payload


def acknowledge_source_batch(run_or_store: Any, batch_id: str) -> dict[str, str]:
    """Mark an outbox batch imported; this does not mark leads as commercial CRM state."""
    payload = load_source_batch(run_or_store, batch_id)
    directory = _outbox_directory(run_or_store)
    assert directory is not None
    marker = directory / f"{batch_id}.ack"
    if not marker.exists():
        temp = marker.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"batch_id": batch_id, "acknowledged": True}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(marker)
    return {"batch_id": payload["batch_id"], "ack_path": str(marker)}


def _discovery_entries(source: dict[str, Any], parsed: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    scope = source.get("scope", {})
    for row in parsed.get("discoveries", []):
        url = web_url(row.get("discovered_url", ""))
        if not url:
            continue
        verified = bool(scope.get("verified"))
        basis = scope.get("basis", "") if verified else ""
        if source.get("parser") == "localbeauty_discovery":
            prefixes = source.get("parser_config", {}).get("listing_prefixes", [])
            path = urlsplit(url).path
            verified = host(url) == host(source.get("url", "")) and any(path.startswith(prefix) for prefix in prefixes)
            basis = "URL bajo la categoría sectorial peluquería/estética descubierta en la portada LocalBeauty." if verified else ""
        entries.append({"discovered_url": url, "source_id": source.get("id", ""),
                        "source_url": row.get("source_url", ""), "source_capture": row.get("source_capture", ""),
                        "source_sha256": row.get("source_sha256", ""), "locator": row.get("locator", ""),
                        "sector_context": {"verified": verified, "country": "ES" if verified else "",
                                           "sector": scope.get("sector", "") if verified else "", "basis": basis}})
    for row in parsed.get("records", []):
        if row.get("scope_status") != "scope_reviewed_candidate":
            continue
        for url in row.get("discovered_urls", []):
            url = web_url(url)
            if url:
                entries.append({"discovered_url": url, "source_id": source.get("id", ""),
                                "source_url": row.get("source_url", ""), "source_capture": row.get("source_capture", ""),
                                "source_sha256": row.get("source_sha256", ""), "locator": row.get("locator", ""),
                                "sector_context": {"verified": bool(scope.get("verified")),
                                                   "country": scope.get("country", ""), "sector": scope.get("sector", ""),
                                                   "basis": scope.get("basis", "")}})
    # Preserve the complete discovery inventory; MAX_BATCH limits processing,
    # never the accumulated lineage.
    return list({row["discovered_url"]: row for row in entries}.values())


def _resolve_discovery_entries(store: Any, source: dict[str, Any], *, limit: int = MAX_BATCH) -> list[dict[str, Any]]:
    parent_id = source.get("discovery_source_id")
    if not parent_id:
        return []
    recipe_id = source.get("_recipe_id", "")
    if not recipe_id:
        return []
    try:
        recipe = load_recipe(recipe_id)
        parent = next((row for row in recipe["sources"] if row["id"] == parent_id), None)
    except (ValueError, StopIteration):
        parent = None
    if not parent:
        return []
    captures = _capture_results_for_source(store, parent)
    if not captures:
        return _read_discovery_state(store, parent_id)
    processed = set(_read_route_state(store, parent_id)["processed_captures"])
    context_entries = _resolve_discovery_entries(store, parent, limit=limit)
    context = {_capture_url_key(row): row.get("sector_context", {}) for row in context_entries}
    captures = _attach_discovery_context(_unique_captures(captures), context)
    remaining = limit
    while remaining > 0:
        batch, identities = _new_capture_batch(store, parent_id, captures, min(MAX_BATCH, remaining), processed=processed)
        if not batch:
            break
        parsed = _extract_source_batch(parent, batch, MAX_BATCH)
        entries = _discovery_entries(parent, parsed)
        processed.update(identities)
        _persist_source_batch(store, parent, parsed, identities)
        _write_discovery_state(store, parent, entries, processed_captures=identities)
        remaining -= len(batch)
    return _read_discovery_state(store, parent_id)


def _route_result(source: dict[str, Any], parsed: dict[str, Any], *, requests: int,
                  jobs: list[dict[str, Any]], status: str, worker: dict[str, Any] | None = None,
                  **extra: Any) -> dict[str, Any]:
    rows = list(parsed.get("records", []))
    discoveries = list(parsed.get("discoveries", []))
    discovered_urls = list(dict.fromkeys(
        [row.get("discovered_url", "") for row in discoveries]
        + [url for row in rows for url in row.get("discovered_urls", [])]
    ))
    discovered_urls = [url for url in discovered_urls if url]
    return {"source_id": source["id"], "records": rows, "discoveries": discoveries,
            "discovered_urls": discovered_urls, "count": len(rows), "discovery_count": len(discoveries), "errors": parsed.get("errors", []),
            "excluded": parsed.get("excluded", []), "excluded_count": len(parsed.get("excluded", [])),
            "pending": parsed.get("pending", []), "pending_count": len(parsed.get("pending", [])),
            "jobs": jobs, "requests": requests, "network_requests": requests, "status": status,
            "worker": worker, **extra}


def _extract_source_batch(source: dict[str, Any], captures: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    # Parse inline through the same one-capture helper, keeping network and
    # store effects separate.
    records, errors = [], []
    for capture in captures[:limit]:
        try:
            raw = _capture_bytes(capture)
            url = capture.get("url") or capture.get("requested_url") or source.get("url", "")
            if not _source_matches_capture(source, capture):
                continue
            records.extend(_parse_capture(raw, capture, source))
        except Exception as exc:
            errors.append({"capture": str(capture.get("path", "")), "url": capture.get("url", ""), "error": str(exc), "type": type(exc).__name__})
    discoveries = [row for row in records if row.get("record_type") == "discovery"]
    excluded = [row for row in records if row.get("record_type") == "excluded"]
    pending = [row for row in records if row.get("record_type") == "pending_review"]
    business = [row for row in records if row.get("record_type") == "business_candidate"]
    return {"records": business, "discoveries": discoveries, "excluded": excluded, "pending": pending, "errors": errors}


def discover_urls(capture: dict[str, Any], *, selector: str = "", href_contains: str = "",
                  max_urls: int = 500, offset: int = 0) -> dict[str, Any]:
    """Extract sitemap URLs or matching links from one saved capture only."""
    if type(max_urls) is not int or not 1 <= max_urls <= MAX_BATCH:
        raise ValueError(f"max_urls debe estar entre 1 y {MAX_BATCH}")
    if type(offset) is not int or offset < 0:
        raise ValueError("offset debe ser un entero >= 0")
    raw = _capture_bytes(capture)
    body = _text(raw)
    if selector:
        nodes = select(DOM(body).root, selector)
        urls = [web_url(n.attrs.get("href", ""), capture.get("url", "")) for n in nodes]
    else:
        nodes = select(DOM(body).root, "loc")
        urls = [web_url(n.text().strip(), capture.get("url", "")) for n in nodes]
    urls = list(dict.fromkeys(u for u in urls if u and (not href_contains or href_contains in u)))
    page = urls[offset:offset + max_urls]
    next_offset = offset + len(page) if offset + len(page) < len(urls) else None
    return {"urls": page, "found": len(urls), "returned": len(page), "offset": offset,
            "next_offset": next_offset, "truncated": next_offset is not None, "source_url": capture.get("url", capture.get("requested_url", "")),
            "source_capture": str(capture.get("path", "")), "source_sha256": hashlib.sha256(raw).hexdigest(),
            "network_requests": 0}


def paginate_urls(template: str, pages: Iterable[int], *, max_pages: int = 100) -> list[str]:
    """Expand a reviewed URL template offline; ``{page}`` is one-based."""
    if "{page}" not in template or not template.startswith(("http://", "https://")):
        raise ValueError("template requiere URL HTTP(S) y marcador {page}")
    nums = list(pages)
    if len(nums) > max_pages or any(type(n) is not int or n < 1 for n in nums):
        raise ValueError(f"páginas debe contener enteros 1..{max_pages} y no superar ese límite")
    return list(dict.fromkeys(template.format(page=n) for n in nums))


def _run(argv: list[str], timeout: int = 8) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, check=False)


def _check(command: str, args: list[str], runner: Callable[..., Any]) -> dict[str, Any]:
    path = shutil.which(command)
    if not path:
        return {"available": False, "path": None, "detail": f"{command} no encontrado en PATH"}
    try:
        result = runner([path, *args], timeout=8)
        ok = result.returncode == 0
        return {"available": ok, "path": path, "detail": "disponible" if ok else (result.stderr or result.stdout or "comando falló").strip()[:300]}
    except subprocess.TimeoutExpired:
        return {"available": None, "path": path, "detail": "timeout; estado desconocido"}
    except Exception as exc:
        return {"available": None, "path": path, "detail": str(exc)[:300]}


def runtime_status(*, runner: Callable[..., Any] = _run, python_exe: str | Path = sys.executable,
                   skill_dir: str | Path | None = None) -> dict[str, Any]:
    """Check exact local runtime components; does not install, pull, or crawl."""
    python_exe = str(python_exe)
    py = {"available": Path(python_exe).is_file(), "path": python_exe}
    try:
        probe = runner([python_exe, "-c", "import importlib.util,json; print(json.dumps({'scrapling':importlib.util.find_spec('scrapling') is not None}))"], timeout=20)
        scrapling_ok: bool | None = bool(json.loads(probe.stdout).get("scrapling")) if probe.returncode == 0 else None
    except subprocess.TimeoutExpired:
        scrapling_ok = None
    except Exception:
        scrapling_ok = None
    scrapling_cli = _check("scrapling", ["--help"], runner)
    docker_version = _check("docker", ["--version"], runner)
    docker_daemon = _check("docker", ["info", "--format", "{{.ServerVersion}}"], runner) if docker_version["available"] else {"available": False, "path": docker_version["path"], "detail": "Docker CLI no disponible"}
    docker_image = _check("docker", ["image", "inspect", "gosom/google-maps-scraper:latest", "--format", "{{.Id}}"], runner) if docker_daemon["available"] else {"available": False, "path": docker_version["path"], "detail": "daemon no disponible"}
    skill = Path(skill_dir) if skill_dir else None
    maps_helper = (skill / "scripts" / "run-local.sh") if skill else None
    wsl_path = shutil.which("wsl") or shutil.which("wsl.exe")
    return {"python": py, "scrapling_module": {"available": scrapling_ok, "python": python_exe,
            "binary": scrapling_cli, "detail": "módulo importable" if scrapling_ok else "módulo ausente en este Python"},
            "maps": {"wsl_cli": {"available": bool(wsl_path), "path": wsl_path},
                     "docker_cli": docker_version, "docker_daemon": docker_daemon,
                     "image": docker_image, "skill_helper": {"present": bool(maps_helper and maps_helper.is_file()),
                     "available": None, "execution_verified": False,
                     "path": str(maps_helper) if maps_helper else None}},
            "network_requests": 0}


def maps_email_command(query_file: str | Path, output_dir: str | Path, *, skill_dir: str | Path,
                       depth: int = 1, format: str = "csv") -> dict[str, Any]:
    """Build (never run) a conservative Maps helper command with email enabled."""
    if type(depth) is not int or not 1 <= depth <= 10:
        raise ValueError("depth debe estar entre 1 y 10")
    if format not in {"csv", "json"}:
        raise ValueError("format debe ser csv o json")
    helper = Path(skill_dir) / "scripts" / "run-local.sh"
    if not helper.is_file():
        return {"available": False, "reason": f"No existe el helper Maps: {helper}", "argv": [], "network_requests": 0}
    if not (shutil.which("wsl") or shutil.which("wsl.exe")):
        return {"available": False, "reason": "WSL CLI no disponible", "argv": [], "network_requests": 0}
    argv = ["wsl", "bash", _to_wsl_path(helper), "--queries", _to_wsl_path(query_file), "--output-dir", _to_wsl_path(output_dir),
            "--depth", str(depth), "--format", format, "--email"]
    return {"available": None, "prepared": True, "execution_verified": False,
            "reason": "helper presente; ejecución local aún no verificada", "argv": argv,
            "command": " ".join(shlex.quote(part) for part in argv),
            "email_extraction": True, "helper_email_flag": "--email", "docker_email_flag": "-email",
            "network_requests": 0, "execution": "not_started"}


def _to_wsl_path(path: str | Path) -> str:
    raw = str(path)
    if raw.startswith("\\\\?\\"):
        raw = raw[4:]
    windows = PureWindowsPath(raw)
    if windows.is_absolute() and windows.drive:
        drive = windows.drive[0].lower()
        tail = "/".join(part for part in windows.parts[1:] if part not in {"\\", "/"})
        return f"/mnt/{drive}/{tail}"
    if raw.startswith("/"):
        return raw
    resolved = Path(raw).resolve()
    return _to_wsl_path(str(resolved))


def scrapling_fetch_command(url: str, output_path: str | Path, *, python_exe: str | Path = sys.executable,
                            mode: str = "http") -> dict[str, Any]:
    """Prepare a structured Scrapling fetch command; caller explicitly runs it."""
    target = web_url(url)
    if not target:
        raise ValueError("URL debe ser HTTP(S)")
    if mode not in {"http", "dynamic", "stealth"}:
        raise ValueError("mode debe ser http, dynamic o stealth")
    pycode = (
        "import json,sys; from scrapling.fetchers import Fetcher,DynamicFetcher,StealthyFetcher; "
        f"u={target!r}; m={mode!r}; f=Fetcher if m=='http' else (DynamicFetcher if m=='dynamic' else StealthyFetcher); "
        "p=f.get(u) if m=='http' else f.fetch(u); "
        "print(json.dumps({'url':u,'status':getattr(p,'status',None),'html':str(p), 'title':p.css('title::text').get() if p else ''},ensure_ascii=False))"
    )
    argv = [str(python_exe), "-c", pycode]
    return {"available": Path(str(python_exe)).is_file(), "argv": argv, "output_path": str(output_path),
            "mode": mode, "structured_result": ["url", "status", "title", "html"], "execution": "not_started",
            "network_requests": 0}


def scrapling_cli_command(url: str, output_path: str | Path, *, binary: str | Path = "scrapling",
                         mode: str = "get") -> dict[str, Any]:
    """Prepare an official Scrapling CLI command with the required safety flag."""
    target = web_url(url)
    if not target:
        raise ValueError("URL debe ser HTTP(S)")
    if mode not in {"get", "fetch", "stealthy-fetch"}:
        raise ValueError("mode debe ser get, fetch o stealthy-fetch")
    output = Path(output_path)
    argv = [str(binary), "extract", mode, target, str(output), "--ai-targeted"]
    return {"available": bool(shutil.which(str(binary)) or Path(str(binary)).is_file()), "argv": argv,
            "output_path": str(output), "mode": mode, "structured_result": ["url", "status", "title", "html"],
            "execution": "not_started", "network_requests": 0}


def run_prepared(command: dict[str, Any], *, runner: Callable[..., Any] = _run,
                 timeout: int = 60) -> dict[str, Any]:
    """Explicit execution hook for a prepared Scrapling command."""
    if command.get("execution") != "not_started" or not command.get("argv"):
        raise ValueError("Se requiere un comando preparado válido")
    result = runner(command["argv"], timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "el comando falló").strip()[:1000])
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("Scrapling devolvió una salida no JSON") from exc
    out = Path(command["output_path"])
    out.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    out.write_bytes(raw)
    return {"result": payload, "path": str(out), "sha256": hashlib.sha256(raw).hexdigest(), "network_requests": 1}


def run_scrapling_cli(command: dict[str, Any], *, runner: Callable[..., Any] = _run,
                      timeout: int = 60) -> dict[str, Any]:
    """Explicitly execute a prepared CLI command and normalize its saved page."""
    if command.get("execution") != "not_started" or not command.get("argv") or "--ai-targeted" not in command["argv"]:
        raise ValueError("Se requiere un comando Scrapling CLI preparado con --ai-targeted")
    result = runner(command["argv"], timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Scrapling CLI falló").strip()[:1000])
    out = Path(command["output_path"])
    try:
        raw = out.read_bytes()
    except OSError as exc:
        raise ValueError(f"Scrapling CLI no dejó el archivo esperado: {out}") from exc
    body = _text(raw)
    titles = [" ".join(node.text().split()) for node in select(DOM(body).root, "title")]
    payload = {"url": command["argv"][3], "status": "completed", "title": titles[0] if titles else "", "html": body}
    return {"result": payload, "path": str(out), "sha256": hashlib.sha256(raw).hexdigest(),
            "network_requests": 1}
