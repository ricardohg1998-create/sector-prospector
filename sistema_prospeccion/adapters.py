"""Reusable, offline adapters for source formats not covered by parse_records.

Adapters are deterministic: they read only the supplied file and configuration,
and never fetch URLs. Configurations may be Python mappings or JSON file paths.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

from .parsing import DOM, select


def dependency_status(adapter: str) -> dict[str, Any]:
    """Report whether an adapter's optional runtime dependencies are present."""
    if adapter == "html_labels":
        return {"adapter": adapter, "available": True, "missing": [], "requires": ["stdlib", "sistema_prospeccion.parsing"]}
    if adapter == "pdf_columns":
        available = importlib.util.find_spec("pdfplumber") is not None
        return {"adapter": adapter, "available": available, "missing": [] if available else ["pdfplumber"], "requires": ["pdfplumber"]}
    return {"adapter": adapter, "available": False, "missing": [], "requires": [], "error": f"Adaptador desconocido: {adapter}"}


def _config(config: dict | str | Path) -> dict:
    if isinstance(config, dict):
        return config
    path = Path(config)
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"No se pudo leer la configuración JSON {path}: {exc}") from exc
    if not isinstance(result, dict):
        raise ValueError(f"La configuración debe ser un objeto JSON: {path}")
    return result


def _text_path(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Some archived pages declare legacy encodings despite UTF-8-ish content.
        return Path(path).read_text(encoding="cp1252")


def _clean(value: str, rules: dict) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    for prefix in rules.get("strip_prefixes", []):
        if value.casefold().startswith(str(prefix).casefold()):
            value = value[len(prefix):].strip(" \t:.-")
            break
    return value


def _normalize_location(record: dict) -> None:
    city, postal = record.get("city", "").strip(), record.get("postal_code", "").strip()
    if re.fullmatch(r"\d{5}", city) and postal and not re.fullmatch(r"\d{5}", postal):
        record["city"], record["postal_code"] = postal, city


def _html_labels(path: str | Path, cfg: dict, source_url: str) -> list[dict]:
    container_selector = cfg.get("container_selector", ".entry-content")
    label_selector = cfg.get("label_selector", "div")
    labels = cfg.get("labels")
    if not isinstance(labels, dict) or not labels:
        raise ValueError("html_labels requiere un objeto 'labels' con prefijos y campos destino")
    text = _text_path(path)
    dom = DOM(text)
    containers = select(dom.root, container_selector)
    if not containers:
        raise ValueError(f"No se encontró el contenedor HTML configurado: {container_selector}")
    if len(containers) != 1:
        raise ValueError("html_labels requiere una ficha y un único contenedor; acota el selector")
    # A ficha debe tener un único contenedor. This keeps page chrome/header out.
    container = containers[0]
    out: dict[str, str] = {}
    clean_rules = cfg.get("clean", {})
    for node in select(container, label_selector):
        raw = node.text()
        for prefix, field in labels.items():
            if raw.casefold().startswith(str(prefix).casefold()):
                value = _clean(raw[len(prefix):].lstrip(' :\t'), clean_rules)
                if value:
                    out[field] = value
                break
    if not out.get("phone") and out.get("mobile"):
        out["phone"] = out["mobile"]
    out.setdefault("email", "")
    out.setdefault("phone", "")
    out.setdefault("mobile", "")
    out.setdefault("website", "")
    out.setdefault("subsector", cfg.get("defaults", {}).get("subsector", ""))
    out.setdefault("relationship", cfg.get("defaults", {}).get("relationship", ""))
    for field in ('subsector','relationship','city','province','region','country'):
        if not out.get(field) and cfg.get('defaults',{}).get(field):
            out[field]=cfg['defaults'][field]
    if not out['relationship'] and any(term.casefold() in out.get('name','').casefold() for term in cfg.get('chain_terms',[])):
        out['relationship']='cadena'
    out["contact_url"] = source_url or cfg.get("source_url", "")
    if out.get("email"):
        out["email_source"] = out["contact_url"]
    _normalize_location(out)
    if not any(out.get(field) for field in ("name", "email", "phone", "address")):
        raise ValueError(f"No se encontraron campos de ficha en {path}; revisa selector y labels")
    return [out]


def _pdf_columns(path: str | Path, cfg: dict, source_url: str) -> list[dict]:
    status = dependency_status("pdf_columns")
    if not status["available"]:
        raise RuntimeError("El adaptador pdf_columns requiere pdfplumber; no está instalado")
    columns = cfg.get("columns")
    if not isinstance(columns, list) or len(columns) < 2:
        raise ValueError("pdf_columns requiere 'columns': lista de cortes x ordenados")
    fields = cfg.get("fields")
    if not isinstance(fields, list) or len(fields) not in {len(columns) - 1, len(columns)}:
        raise ValueError("'fields' debe mapear los intervalos entre cortes de columns (y opcionalmente el último tramo)")
    if len(columns) < 2 or any(float(columns[i]) >= float(columns[i + 1]) for i in range(len(columns) - 1)):
        raise ValueError("Los cortes de 'columns' deben ser números ascendentes")
    try:
        import pdfplumber
    except ImportError as exc:  # Defensive: dependency_status ran first.
        raise RuntimeError("El adaptador pdf_columns requiere pdfplumber; no está instalado") from exc

    page_numbers = cfg.get("pages", [1])  # zero-based, matching Python/pdfplumber
    if not isinstance(page_numbers, list) or not page_numbers:
        raise ValueError("'pages' debe ser una lista de índices de página desde cero")
    tolerance = float(cfg.get("row_y_tolerance", 1.0))
    row_x_max = float(cfg.get("row_number_x_max", columns[0]))
    min_y = float(cfg.get("min_row_y", 0))
    digit_pattern = re.compile(cfg.get("row_number_pattern", r"^\d"))
    cif_len = int(cfg.get("cif_length", 9))
    max_rows = cfg.get("max_rows")
    source_url = source_url or cfg.get("source_url", "")
    out: list[dict] = []

    try:
        pdf = pdfplumber.open(str(path))
    except Exception as exc:
        raise ValueError(f"No se pudo abrir el PDF {path}: {exc}") from exc
    with pdf:
        for page_index in page_numbers:
            page_index = int(page_index)
            if page_index < 0 or page_index >= len(pdf.pages):
                raise ValueError(f"Página PDF fuera de rango: {page_index} (total {len(pdf.pages)})")
            page = pdf.pages[page_index]
            # Cluster character tops to retain the original row reading order;
            # extracting words would split/merge long company and email cells.
            row_words = page.extract_words(x_tolerance=float(cfg.get("word_x_tolerance", 1)),
                                           y_tolerance=float(cfg.get("word_y_tolerance", 1)))
            tops = sorted({round(float(word["top"]), 1) for word in row_words
                           if float(word["x0"]) < row_x_max and float(word["top"]) > min_y
                           and digit_pattern.search(word.get("text", ""))})
            for row_number, y in enumerate(tops, 1):
                # Keep pdfplumber's character order. In this source it preserves
                # column reading order even where long text visually overlaps.
                row_chars = [ch for ch in page.chars if abs(float(ch["top"]) - y) < tolerance]
                starts = []
                for x in columns:
                    x = float(x)
                    starts.append(next((i for i, ch in enumerate(row_chars) if abs(float(ch["x0"]) - x) < float(cfg.get("column_x_tolerance", .12))), None))
                required = [int(i) for i in cfg.get("required_column_indices", range(min(6, len(starts))))]
                if any(starts[i] is None for i in required):
                    if cfg.get("skip_incomplete_rows", True):
                        continue
                    raise ValueError(f"Fila {row_number} incompleta en página {page_index + 1}")
                identity = "".join(ch.get("text", "") for ch in row_chars[:starts[0]]).strip()
                chunks = [""] * len(starts)
                for i, start in enumerate(starts):
                    if start is None:
                        continue
                    end = next((starts[j] for j in range(i + 1, len(starts)) if starts[j] is not None), len(row_chars))
                    chunks[i] = "".join(ch.get("text", "") for ch in row_chars[start:end]).strip()
                identity_match = re.match(r"^(.*?)(\S{%d})$" % cif_len, identity)
                if identity_match:
                    source_entity_key, source_cif = identity_match.groups()
                    source_entity_key = source_entity_key.strip()
                else:
                    source_entity_key, source_cif = identity, ""
                record = {field: chunks[i] for i, field in enumerate(fields)}
                record.update({"entity_key": source_entity_key, "source_cif": source_cif,
                               "source_brand": record.get("source_brand", ""),
                               "pdf_row": row_number})
                if "relationship" not in record:
                    chains = cfg.get("chain_terms", [])
                    record["relationship"] = "cadena" if any(term.casefold() in record.get("name", "").casefold() for term in chains) else ""
                record.setdefault("subsector", cfg.get("defaults", {}).get("subsector", ""))
                record["contact_url"] = f"{source_url}#page={page_index + 1}" if source_url else f"#page={page_index + 1}"
                if record.get("email"):
                    record["email_source"] = f"{record['contact_url']}&row={row_number}"
                _normalize_location(record)
                out.append(record)
                if max_rows is not None and len(out) >= int(max_rows):
                    return out
    return out


def extract_records(input_path: str | Path, adapter: str, config: dict | str | Path, source_url: str = "") -> list[dict]:
    """Extract normalized records from a local HTML or PDF file, without network access."""
    cfg = _config(config)
    if adapter == "html_labels":
        return _html_labels(input_path, cfg, source_url)
    if adapter == "pdf_columns":
        return _pdf_columns(input_path, cfg, source_url)
    raise ValueError(f"Adaptador desconocido: {adapter}")
