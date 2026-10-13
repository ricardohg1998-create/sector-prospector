"""Frozen, offline source profiles for repeatable local extraction.

This module never downloads pages or writes output files. Callers supply saved
listing/detail files; manifests describe the exact configuration and inputs.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .adapters import extract_records
from .parsing import DOM, select
from .parsing import web_url as normalize_url


_CONFIG_DIR = Path(__file__).resolve().parent / "adapters"
_PROFILE_FILES = {
    "asetra": _CONFIG_DIR / "profile_asetra.json",
    "mitallerdeconfianza": _CONFIG_DIR / "profile_mitaller.json",
    "femp": _CONFIG_DIR / "profile_femp.json",
}
_MAX_BATCH = 1000


def _canonical_json(value: Any) -> str:
    """Stable JSON representation; all serialized bytes use LF line endings."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_profile(profile_id: str) -> dict[str, Any]:
    try:
        config_path = _PROFILE_FILES[profile_id]
    except KeyError as exc:
        raise ValueError(f"Perfil desconocido: {profile_id}; disponibles: {', '.join(_PROFILE_FILES)}") from exc
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"No se pudo leer el perfil {config_path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("id") != profile_id or not isinstance(data.get("config"), dict):
        raise ValueError(f"Configuración de perfil no válida: {config_path}")
    return {**data, "config_path": config_path}


def _profile_snapshot(profile: dict[str, Any], override: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if override is not None and not isinstance(override, Mapping):
        raise ValueError("config_override debe ser un objeto/mapping")
    config = copy.deepcopy(profile["config"])
    config["_profile_name_selector"] = profile.get("name_selector", "")
    if override:
        values=copy.deepcopy(dict(override))
        if isinstance(values.get('defaults'),dict):
            values['defaults']={**config.get('defaults',{}),**values['defaults']}
        config.update(values)
    # Copy via canonical JSON so returned config is JSON-compatible and detached.
    return json.loads(_canonical_json(config))


def _profile_sha256(profile: dict[str, Any]) -> str:
    snapshot = {key: value for key, value in profile.items() if key != "config_path"}
    return _sha256((_canonical_json(snapshot) + "\n").encode("utf-8"))


def list_profiles() -> list[dict[str, Any]]:
    """List the available profiles and their extraction/discovery capabilities."""
    return [profile_info(profile_id) for profile_id in _PROFILE_FILES]


def profile_info(profile_id: str) -> dict[str, Any]:
    """Return stable, public metadata useful to CLI help and documentation."""
    profile = _load_profile(profile_id)
    discovery = profile.get("discovery")
    return {
        "id": profile["id"],
        "name": profile["name"],
        "adapter": profile["adapter"],
        "config_path": str(profile["config_path"]),
        "discovery_supported": bool(discovery),
        "max_limit": discovery.get("max_limit") if discovery else None,
    }


def get_profile(profile_id: str) -> dict[str, Any]:
    """Return profile metadata, frozen extraction config, and discovery rules."""
    profile = _load_profile(profile_id)
    return {
        **profile_info(profile_id),
        "config": _profile_snapshot(profile),
        "discovery": copy.deepcopy(profile.get("discovery")),
        "name_selector": profile.get("name_selector", ""),
    }


def _read_local_text(path: str | Path) -> str:
    file_path = Path(path)
    try:
        return file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return file_path.read_text(encoding="cp1252")


def _page_inputs(listing_pages: Any) -> list[tuple[Path, str]]:
    """Accept a {local_path: page_url} mapping or an iterable of (path, url)."""
    items = listing_pages.items() if isinstance(listing_pages, Mapping) else listing_pages
    result = []
    try:
        for item in items:
            if isinstance(item, Mapping):
                path, page_url = item.get("path"), item.get("url")
            else:
                path, page_url = item
            if not path or not page_url:
                raise ValueError("Cada página local requiere 'path' y su URL base original")
            normalized = normalize_url(str(page_url))
            if not normalized:
                raise ValueError(f"URL base no válida para página guardada: {page_url}")
            result.append((Path(path), normalized))
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValueError):
            raise
        raise ValueError("listing_pages debe contener rutas locales y URLs base") from exc
    if not result:
        raise ValueError("Debe indicarse al menos una página HTML local")
    return result


def _url_is_allowed(url: str, base_url: str, discovery: dict[str, Any], scope: str | None) -> bool:
    normalized = normalize_url(url, base_url)
    if not normalized:
        return False
    parts = urlsplit(normalized)
    host = (parts.hostname or "").lower().removeprefix("www.")
    allowed_hosts = {str(item).lower().removeprefix("www.") for item in discovery.get("allowed_hosts", [])}
    base_host = (urlsplit(base_url).hostname or "").lower().removeprefix("www.")
    if not allowed_hosts or host not in allowed_hosts or host != base_host:
        return False
    path = parts.path or "/"
    prefixes = [scope] if scope is not None else discovery.get("allowed_path_prefixes", ["/"])
    if not any(path.startswith(str(prefix)) for prefix in prefixes):
        return False
    return not any(path.startswith(str(prefix)) for prefix in discovery.get("excluded_path_prefixes", []))


def discover_fichas(
    profile_id: str,
    listing_pages: Any,
    *,
    scope: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    """Discover detail/page URLs from saved HTML; return URLs and a hash manifest.

    listing_pages is a mapping of local path to original page URL, or an iterable
    of {"path", "url"} objects / (path, url) pairs. Returned next_urls identify
    additional pages that can be saved by an external, explicitly authorized
    workflow and passed back here. No network access is performed.
    """
    profile = _load_profile(profile_id)
    discovery = profile.get("discovery")
    if not discovery:
        raise ValueError(f"El perfil {profile_id} no tiene descubrimiento HTML configurado")
    max_limit = int(discovery.get("max_limit", _MAX_BATCH))
    if limit is None:
        limit = max_limit
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > max_limit:
        raise ValueError(f"limit debe ser un entero entre 1 y {max_limit}")
    if type(offset) is not int or offset < 0:
        raise ValueError('offset debe ser entero no negativo sobre los mismos listados congelados')
    pages = _page_inputs(listing_pages)
    found: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    skipped = 0
    next_urls: list[str] = []
    page_manifest = []
    link_selector = discovery.get("link_selector")
    next_selector = discovery.get("next_selector")
    if not link_selector:
        raise ValueError(f"El perfil {profile_id} no define link_selector")
    for path, base_url in pages:
        content = _read_local_text(path)
        page_manifest.append({"path": str(path.resolve()), "url": base_url, "sha256": _sha256(path.read_bytes())})
        dom = DOM(content)
        for anchor in select(dom.root, link_selector):
            href = anchor.attrs.get("href", "")
            url = normalize_url(href, base_url)
            if not url or url in seen_urls or not _url_is_allowed(url, base_url, discovery, scope):
                continue
            seen_urls.add(url)
            if skipped < offset:
                skipped += 1
                continue
            if len(found) >= limit:
                break
            found.append({"url": url, "name_hint": " ".join(anchor.text().split()), "listing_path": str(path.resolve())})
        if next_selector:
            for anchor in select(dom.root, next_selector):
                href = anchor.attrs.get("href", "")
                url = normalize_url(href, base_url)
                if (url and url != base_url and _url_is_allowed(url, base_url, discovery, None)
                        and url not in next_urls):
                    next_urls.append(url)
        if len(found) >= limit:
            break
    manifest_body = {"profile_id": profile_id, "profile_sha256": _profile_sha256(profile),
                     "pages": page_manifest, "limit": limit,
                     "scope": scope, "offset":offset,"next_offset":offset+len(found),"fichas": found, "next_urls": next_urls}
    manifest = {**manifest_body, "manifest_sha256": _sha256((_canonical_json(manifest_body) + "\n").encode("utf-8"))}
    return {"fichas": found, "next_urls": next_urls, "next_offset":offset+len(found),"manifest": manifest}


def extract_file(
    profile_id: str,
    input_path: str | Path,
    source_url: str,
    *,
    config_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract one saved file and return records, frozen config, and hashes."""
    profile = _load_profile(profile_id)
    config = _profile_snapshot(profile, config_override)
    normalized = normalize_url(source_url)
    if not normalized or not source_url.lower().startswith(('http://','https://')):
        raise ValueError('source_url requiere URL HTTP(S) de la fuente')
    discovery=profile.get('discovery')
    if discovery and not _url_is_allowed(normalized,normalized,discovery,None):
        raise ValueError('URL de ficha fuera del dominio/ámbito del perfil')
    input_file = Path(input_path)
    try:
        input_bytes = input_file.read_bytes()
    except OSError as exc:
        raise ValueError(f"No se pudo leer el original local {input_file}: {exc}") from exc
    records = extract_records(input_file, profile["adapter"], config, source_url)
    name_selector = config.get("_profile_name_selector")
    if name_selector:
        dom = DOM(_read_local_text(input_file))
        names = [" ".join(node.text().split()) for node in select(dom.root, name_selector)]
        name = next((value for value in names if value), "")
        for record in records:
            if not record.get("name"):
                record["name"] = name
    if not records:
        raise ValueError(f"La ficha local no produjo registros: {input_file}")
    if any(not str(record.get("name", "")).strip() for record in records):
        raise ValueError(f"Hay registros sin nombre en {input_file}; lote cancelado")
    config_sha = _sha256((_canonical_json(config) + "\n").encode("utf-8"))
    output_bytes = (_canonical_json(records) + "\n").encode("utf-8")
    manifest = {
        "profile_id": profile_id,
        "profile_sha256": _profile_sha256(profile),
        "adapter": profile["adapter"],
        "config_path": str(profile["config_path"].resolve()),
        "config_sha256": config_sha,
        "input_path": str(input_file.resolve()),
        "input_sha256": _sha256(input_bytes),
        "source_url": source_url,
        "rows": len(records),
        "rows_with_email": sum(bool(record.get("email")) for record in records),
        "output_sha256": _sha256(output_bytes),
        "network_requests": 0,
    }
    return {"records": records, "manifest": manifest, "config_snapshot": config}


def extract_batch(
    profile_id: str,
    items: Iterable[Mapping[str, Any]],
    *,
    config_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract a bounded batch atomically in memory; any bad fiche aborts all."""
    batch = list(items)
    if not batch or len(batch) > _MAX_BATCH:
        raise ValueError(f"El lote debe contener entre 1 y {_MAX_BATCH} fichas")
    outputs = []
    records = []
    for index, item in enumerate(batch, start=1):
        if not isinstance(item, Mapping) or not item.get("path") or not item.get("url"):
            raise ValueError(f"La ficha {index} requiere path local y url fuente")
        try:
            result = extract_file(profile_id, item["path"], str(item["url"]), config_override=config_override)
        except Exception as exc:
            raise ValueError(f"Lote cancelado en ficha {index} ({item.get('path')}): {exc}") from exc
        records.extend(result["records"])
        outputs.append(result["manifest"])
    profile = _load_profile(profile_id)
    config = _profile_snapshot(profile, config_override)
    output_bytes = (_canonical_json(records) + "\n").encode("utf-8")
    manifest_body = {
        "profile_id": profile_id,
        "profile_sha256": _profile_sha256(profile),
        "adapter": profile["adapter"],
        "config_path": str(profile["config_path"].resolve()),
        "config_sha256": _sha256((_canonical_json(config) + "\n").encode("utf-8")),
        "items": outputs,
        "rows": len(records),
        "rows_with_email": sum(bool(record.get("email")) for record in records),
        "output_sha256": _sha256(output_bytes),
        "network_requests": 0,
    }
    manifest = {**manifest_body, "manifest_sha256": _sha256((_canonical_json(manifest_body) + "\n").encode("utf-8"))}
    return {"records": records, "manifest": manifest, "config_snapshot": config}
