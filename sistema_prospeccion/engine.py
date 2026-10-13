"""Persistencia, procedencia, identidad y exportación. Solo biblioteca estándar."""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from collections import Counter, defaultdict

from . import __version__
from .parsing import FIELDS, norm, phone, emails, email_kind, web_url, host, parse_records

WORKSPACE = Path(__file__).resolve().parent.parent
GUIDE = WORKSPACE / "SISTEMA-PROSPECCION-SECTORIAL.md"
RUNTIME = Path(os.environ.get('PROSPECTOR_RUNTIME', str(WORKSPACE / '.runtime')))
BRIEF_HEADINGS = ["Destinatario y contexto", "Situaciones y necesidades", "Soluciones existentes y encaje", "Composición de la base", "Evidencia y cuestiones abiertas"]
EXPORT_FIELDS = ("company_id", "name", "sector", "subsector", "city", "province", "region", "address", "postal_code", "email", "additional_emails", "phone", "website", "contact_url", "email_source", "observed_at", "identity_status", "email_status", "relationship", "reason")
COMMERCIAL_FIELDS = ("website_status", "commercial_signal", "signal_source", "signal_observed_at", "priority", "priority_reason")
EXPORT_FIELDS += COMMERCIAL_FIELDS
WEBSITE_STATES = {"no_investigado", "web_identificada", "busqueda_sin_confirmar", "web_no_disponible"}
PRIORITIES = {"sin_evaluar", "alta", "media", "baja"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value):
    if not isinstance(value, bytes):
        value = str(value).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def json_text(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)


def atomic_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    # The exact UTF-8/LF bytes are also used by manifests and idempotence checks.
    temporary.write_bytes((json_text(obj) + "\n").encode("utf-8"))
    temporary.replace(path)


def validate_config(config):
    for key in ("target", "timeout_seconds", "max_response_bytes", "max_attempts", "website_pages", "fresh_days", "request_budget"):
        if type(config.get(key)) is not int or config[key] < 1:
            raise ValueError(f"{key} debe ser entero positivo")
    if not isinstance(config.get("source_delay_seconds"), (int, float)) or config["source_delay_seconds"] < 0:
        raise ValueError("source_delay_seconds debe ser número no negativo")
    for key in ("include_terms", "exclude_terms"):
        if not isinstance(config.get(key), list) or any(not isinstance(v, str) or not v.strip() for v in config[key]):
            raise ValueError(f"{key} debe ser lista de términos no vacíos")
    if not isinstance(config.get("sector"), str) or not config["sector"].strip():
        raise ValueError("sector vacío")
    if type(config.get("fixture_mode")) is not bool:
        raise ValueError("fixture_mode debe ser booleano")
    if config.get("mode", "legacy") not in {"legacy", "qualified", "volume"}:
        raise ValueError("mode debe ser qualified, legacy o volume")
    if "brief_required" in config and type(config["brief_required"]) is not bool:
        raise ValueError("brief_required debe ser booleano")


def slug(text):
    return norm(text).replace(" ", "-")[:70] or "sector"


def new_run(sector, destination=None, target=1000, fixture=False, mode="volume"):
    if not sector.strip() or target < 1:
        raise ValueError("Sector y objetivo positivo son necesarios")
    destination = Path(destination) if destination else WORKSPACE / "work/prospeccion" / slug(sector) / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("La carpeta ya contiene archivos; usa esa ejecución o elige una nueva")
    destination.mkdir(parents=True, exist_ok=True)
    for folder in ("originales", "entregables", "investigacion"):
        (destination / folder).mkdir(exist_ok=True)
    config = {"schema_version": 1, "engine_version": __version__, "sector": sector.strip(), "target": target,
              "country": "ES", "audience": "pymes y negocios de barrio", "created_at": now(),
              "fixture_mode": fixture, "mode": mode, "brief_required": mode != "volume", "include_terms": [], "exclude_terms": [],
              "source_delay_seconds": 1.0, "timeout_seconds": 20, "max_response_bytes": 5_000_000,
              "max_attempts": 2, "website_pages": 4, "fresh_days": 30, "request_budget": 250,
              "guide_sha256": digest(GUIDE.read_bytes()) if GUIDE.exists() else None,
              "scope_notes": "", "network_enabled": False}
    atomic_json(destination / "config.json", config)
    with Store(destination) as store:
        store.event("init", {"fixture_mode": fixture, "sector": sector})
    return destination.resolve()


class Store:
    def __init__(self, run):
        self.run = Path(run).resolve()
        self.config = json.loads((self.run / "config.json").read_text(encoding="utf-8"))
        validate_config(self.config)
        self.db = sqlite3.connect(self.run / "state.sqlite", timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,spec TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS retired_sources(source_id TEXT PRIMARY KEY REFERENCES sources(id),reason TEXT NOT NULL,retired_at TEXT NOT NULL,replacement TEXT);
        CREATE TABLE IF NOT EXISTS companies(id TEXT PRIMARY KEY,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY,company_id TEXT NOT NULL REFERENCES companies(id),
            source_id TEXT NOT NULL REFERENCES sources(id),locator TEXT NOT NULL,record TEXT NOT NULL,
            observed_at TEXT NOT NULL,fingerprint TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,UNIQUE(source_id,locator));
        CREATE TABLE IF NOT EXISTS decisions(company_id TEXT PRIMARY KEY REFERENCES companies(id),data TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,source_id TEXT,company_id TEXT,url TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued',attempts INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '',payload TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS responses(id INTEGER PRIMARY KEY,job_id TEXT,url TEXT NOT NULL,final_url TEXT NOT NULL,
            sha256 TEXT NOT NULL,path TEXT NOT NULL,fetched_at TEXT NOT NULL,status INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at TEXT NOT NULL,kind TEXT NOT NULL,data TEXT NOT NULL);
        """)
        self.db.commit()
        self._identity_index = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.db.close()

    def event(self, kind, data):
        self.db.execute("INSERT INTO events(at,kind,data) VALUES(?,?,?)", (now(), kind, json_text(data)))
        self.db.commit()

    def configure(self, settings):
        allowed = {"include_terms", "exclude_terms", "scope_notes", "source_delay_seconds", "timeout_seconds", "max_response_bytes", "max_attempts", "website_pages", "fresh_days", "request_budget", "target", "mode", "brief_required"}
        if set(settings) - allowed:
            raise ValueError("No se puede cambiar sector, país o modo ficticio en una ejecución existente")
        settings = dict(settings)
        if "mode" in settings and "brief_required" not in settings:
            settings["brief_required"] = settings["mode"] != "volume"
        proposed = {**self.config, **settings}
        validate_config(proposed)
        path = self.run / "config.json"
        shutil.copy2(path, self.run / ("config.before-" + uuid.uuid4().hex[:8] + ".json"))
        atomic_json(path, proposed)
        self.config = proposed
        self.event("configured", settings)
        return proposed

    def add_source(self, spec):
        spec = dict(spec)
        source_id = spec.get("id", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,70}", source_id):
            raise ValueError("id de fuente: minúsculas, números, guiones; máximo 71 caracteres")
        if spec.get("kind") not in {"csv", "maps_csv", "json", "jsonl", "html", "jsonld"}:
            raise ValueError("kind debe ser csv, maps_csv, json, jsonl, html o jsonld")
        if not spec.get("path") and not web_url(spec.get("url")):
            raise ValueError("La fuente requiere path local o url HTTP(S)")
        if spec.get("path"):
            spec["path"] = str(Path(spec["path"]).resolve())
        for field in ("fields", "defaults", "selectors"):
            if field in spec and not isinstance(spec[field], dict):
                raise ValueError(field + " debe ser objeto JSON")
        if any(k not in FIELDS or not isinstance(v, str) for k, v in spec.get("fields", {}).items()):
            raise ValueError("fields requiere nombres admitidos y rutas de texto")
        scope = spec.get("scope", {})
        if not isinstance(scope, dict) or type(scope.get("verified", False)) is not bool or type(spec.get("publication_verified", False)) is not bool:
            raise ValueError("scope.verified y publication_verified deben ser booleanos")
        if type(spec.get("max_pages", 100)) is not int or spec.get("max_pages", 100) < 1:
            raise ValueError("max_pages debe ser entero positivo")
        if scope.get("verified") and (not scope.get("basis") or scope.get("sector") != self.config["sector"] or scope.get("country") != "ES"):
            raise ValueError("scope verificado requiere sector exacto, country ES y basis de la revisión")
        if spec.get("publication_verified") and not spec.get("publication_basis"):
            raise ValueError("publication_verified requiere publication_basis")
        if spec.get("kind") == "html" and (not spec.get("selectors") or not spec.get("row_selector")):
            raise ValueError("Un directorio HTML necesita row_selector y selectors")
        if spec.get("role", "discovery") not in {"discovery", "enrichment", "correction"}:
            raise ValueError("role debe ser discovery, enrichment o correction")
        for key in ("source_name", "source_group", "selection_notes", "published_at"):
            if key in spec and not isinstance(spec[key], str):
                raise ValueError(key + " debe ser texto")
        if spec.get("published_at"):
            try:
                datetime.fromisoformat(spec["published_at"])
            except ValueError:
                raise ValueError("published_at requiere fecha ISO conocida, no inferida de la URL") from None
        existing = self.db.execute("SELECT spec FROM sources WHERE id=?", (source_id,)).fetchone()
        if existing and json.loads(existing["spec"]) != spec:
            raise ValueError("Ese id tiene otra configuración; usa un id nuevo para conservar trazabilidad")
        self.db.execute("INSERT OR IGNORE INTO sources VALUES(?,?,?)", (source_id, json_text(spec), now()))
        self.db.commit()
        self.event("source_registered", {"source_id": source_id})
        return source_id

    def source(self, source_id):
        row = self.db.execute("SELECT spec FROM sources WHERE id=?", (source_id,)).fetchone()
        if not row:
            raise ValueError("Fuente no registrada")
        return json.loads(row["spec"])

    def active_source(self, source_id):
        spec = self.source(source_id)
        if self.db.execute("SELECT 1 FROM retired_sources WHERE source_id=?", (source_id,)).fetchone():
            raise ValueError("Fuente retirada; registra la versión corregida con otro ID")
        return spec

    def retire_source(self, source_id, reason, replacement=None):
        self.source(source_id)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Retirar una fuente requiere motivo")
        if replacement:
            if replacement == source_id:
                raise ValueError("La sustitución requiere otro ID")
            self.active_source(replacement)
        with self.db:
            self.db.execute("INSERT INTO retired_sources VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET reason=excluded.reason,retired_at=excluded.retired_at,replacement=excluded.replacement", (source_id, reason, now(), replacement))
            count = self.db.execute("UPDATE observations SET active=0 WHERE source_id=? AND active=1", (source_id,)).rowcount
            self.db.execute("UPDATE jobs SET state='cancelled',error='source_retired',updated_at=? WHERE source_id=? AND state IN ('queued','retry','running','failed')", (now(), source_id))
        self._identity_index = None
        self.event("source_retired", {"source_id": source_id, "reason": reason, "replacement": replacement, "observations_retired": count})
        return {"source_id": source_id, "observations_retired": count, "originals_preserved": True}

    def snapshot(self, body, url, final_url=None, job_id=None, status=200, extension="txt"):
        sha = digest(body)
        filename = sha + "." + re.sub(r"[^a-z0-9]", "", extension.lower())
        path = self.run / "originales" / filename
        if not path.exists():
            path.write_bytes(body)
        self.db.execute("INSERT INTO responses(job_id,url,final_url,sha256,path,fetched_at,status) VALUES(?,?,?,?,?,?,?)",
                        (job_id, url, final_url or url, sha, str(path.relative_to(self.run)), now(), status))
        self.db.commit()
        return sha

    def ingest_file(self, source_id, path=None):
        spec = self.active_source(source_id)
        source_path = Path(path or spec.get("path", ""))
        body = source_path.read_bytes()
        base = source_path.resolve().as_uri()
        self.snapshot(body, base, extension=source_path.suffix.lstrip(".") or "txt")
        text = body.decode(spec.get("encoding", "utf-8-sig"))
        records, next_urls = parse_records(text, spec, base)
        if spec['kind'] == 'html' and (not records or not any(r.get('name') for _,r in records)):
            raise ValueError('Conector HTML vacío o sin nombres; comprobar source-check antes de sustituir datos')
        if next_urls:
            self.event("local_pagination_not_followed", {"source_id": source_id, "urls": next_urls})
        # Cache identities before refreshing rows: a reordered CSV must not reassign businesses.
        self._index()
        counts = Counter()
        with self.db:
            self.db.execute("UPDATE observations SET active=0 WHERE source_id=?", (source_id,))
            for locator, record in records:
                counts[self.ingest(source_id, locator, record, commit=False)] += 1
        self._identity_index = None
        self.event("file_imported", {"source_id": source_id, "records": len(records), "result": dict(counts)})
        return dict(counts)

    def _identity_keys(self, record):
        name, city, address = norm(record.get("name")), norm(record.get("city")), norm(record.get("address"))
        keys = set()
        if name and city and address:
            keys.add(("address", name, city, address))
        if name and city and record.get("phone"):
            keys.add(("phone", name, city, phone(record["phone"]), address))
        if name and city and host(record.get("website")):
            keys.add(("site", name, city, host(record["website"]), address))
        if record.get("entity_key"):
            keys.add(("explicit", record["entity_key"]))
        return keys

    def _index(self):
        if self._identity_index is None:
            self._identity_index = defaultdict(set)
            decisions={r['company_id']:json.loads(r['data']) for r in self.db.execute('SELECT company_id,data FROM decisions')}
            combined=defaultdict(dict)
            for row in self.db.execute("SELECT company_id,record FROM observations WHERE active=1 ORDER BY observed_at,id"):
                record=json.loads(row['record'])
                corrected={**record,**{k:v for k,v in decisions.get(row['company_id'],{}).items() if k in {'name','city','address','website'}}}
                combined[row['company_id']].update({k:v for k,v in record.items() if v})
                # Keep original evidence keys and reviewed corrections for refreshes.
                for key in self._identity_keys(record) | self._identity_keys(corrected):
                    self._identity_index[key].add(row["company_id"])
            for company_id,record in combined.items():
                record.update({k:v for k,v in decisions.get(company_id,{}).items() if k in {'name','city','address','website'}})
                for key in self._identity_keys(record):
                    self._identity_index[key].add(company_id)
        return self._identity_index

    def ingest(self, source_id, locator, record, company_id=None, commit=True):
        targeted_observation = company_id is not None
        spec = self.active_source(source_id)
        record = {**{k: "" for k in FIELDS}, **record}
        for field in ("website", "contact_url"):
            record[field] = web_url(record.get(field, ""))
        record["phone"] = phone(record.get("phone"))
        if not record.get("subsector") and record.get("category"):
            record["subsector"] = record["category"]
        if record.get("entity_key"):
            record["entity_key"] = source_id + ":" + str(record["entity_key"])
        record["emails"] = emails(record.get("email", ""))
        record["scope"] = spec.get("scope", {})
        record["publication_verified"] = bool(spec.get("publication_verified"))
        record["publication_basis"] = spec.get("publication_basis", "")
        record["email_source"] = record.get("email_source") or locator
        fingerprint = digest(json_text(record))
        old = self.db.execute("SELECT * FROM observations WHERE source_id=? AND locator=?", (source_id, locator)).fetchone()
        if old and old["fingerprint"] == fingerprint:
            self.db.execute("UPDATE observations SET active=1 WHERE id=?", (old["id"],))
            if commit:
                self.db.commit()
            return "unchanged"
        if old and not company_id and self._identity_keys(record) & self._identity_keys(json.loads(old["record"])):
            company_id = old["company_id"]
        if not company_id:
            keys = self._identity_keys(record)
            candidates = set()
            for key in keys:
                candidates.update(self._index().get(key, set()))
            if len(candidates) == 1:
                company_id = candidates.pop()
            elif len(candidates) > 1:
                record["ambiguous_identity"] = True
        company_id = company_id or uuid.uuid4().hex[:20]
        self.db.execute("INSERT OR IGNORE INTO companies VALUES(?,?)", (company_id, now()))
        observation_id = digest(source_id + "\n" + locator)[:24]
        self.db.execute("INSERT INTO observations(id,company_id,source_id,locator,record,observed_at,fingerprint) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET company_id=excluded.company_id,record=excluded.record,observed_at=excluded.observed_at,fingerprint=excluded.fingerprint,active=1",
                        (observation_id, company_id, source_id, locator, json_text(record), now(), fingerprint))
        for key in self._identity_keys(record):
            self._index()[key].add(company_id)
        if targeted_observation:
            # Partial enrichment/correction changes the combined identity keys.
            self._identity_index = None
        if commit:
            self.db.commit()
        return "updated" if old else "imported"

    def _validate_decision(self, company_id, decision):
        allowed = {"sector_match", "country", "independence", "exclude", "reason", "name", "city", "province", "region", "address", "email_approved", "website", "identity_confirmed", "commercial_reviewed_at", "commercial_review_reason", *COMMERCIAL_FIELDS}
        if not isinstance(decision, dict):
            raise ValueError("Cada decisión debe ser un objeto")
        if set(decision) - allowed or not decision.get("reason"):
            raise ValueError("Decisión requiere reason y solo campos documentados")
        for field in ("sector_match", "independence", "exclude", "identity_confirmed"):
            if field in decision and type(decision[field]) is not bool:
                raise ValueError(field + " debe ser booleano")
        if not self.db.execute("SELECT 1 FROM companies WHERE id=?", (company_id,)).fetchone():
            raise ValueError("Empresa inexistente")
        if decision.get("email_approved"):
            published = set()
            for row in self.db.execute("SELECT record FROM observations WHERE company_id=? AND active=1", (company_id,)):
                record = json.loads(row[0])
                if record.get("publication_verified"):
                    published.update(record.get("emails", []))
            if decision["email_approved"] not in published:
                raise ValueError("Email aprobado sin evidencia publicada en las observaciones")
        prior = self.db.execute("SELECT data FROM decisions WHERE company_id=?", (company_id,)).fetchone()
        data = json.loads(prior[0]) if prior else {}
        data.update(decision)
        if "website" in data and data["website"] and not web_url(data["website"]):
            raise ValueError("website requiere URL HTTP(S) válida")
        for field in COMMERCIAL_FIELDS:
            if field in data and not isinstance(data[field], str):
                raise ValueError(field + " debe ser texto")
        if data.get("website_status", "no_investigado") not in WEBSITE_STATES:
            raise ValueError("website_status no reconocido")
        if data.get("website_status") in {"web_identificada", "web_no_disponible"}:
            published_web = any(json.loads(r[0]).get("website") for r in self.db.execute("SELECT record FROM observations WHERE company_id=? AND active=1", (company_id,)))
            if not data.get("website") and ("website" in data or not published_web):
                raise ValueError("Ese estado requiere una web identificada")
        if data.get("priority", "sin_evaluar") not in PRIORITIES:
            raise ValueError("priority debe ser sin_evaluar, alta, media o baja")
        if data.get("signal_source") and not web_url(data["signal_source"]):
            raise ValueError("signal_source requiere URL HTTP(S)")
        if data.get("signal_observed_at"):
            try:
                datetime.fromisoformat(data["signal_observed_at"])
            except ValueError:
                raise ValueError("signal_observed_at requiere fecha ISO") from None
        if data.get("commercial_signal") and not all(data.get(k) for k in ("signal_source", "signal_observed_at")):
            raise ValueError("Una señal observada requiere fuente y fecha")
        if data.get("priority", "sin_evaluar") != "sin_evaluar" and not all(data.get(k) for k in ("priority_reason", "commercial_signal", "signal_source", "signal_observed_at")):
            raise ValueError("Priorizar requiere señal, fuente, fecha y motivo; no es probabilidad de conversión")
        if data.get('commercial_reviewed_at') or data.get('commercial_review_reason'):
            if not all(isinstance(data.get(k),str) and data[k].strip() for k in ('commercial_reviewed_at','commercial_review_reason')):
                raise ValueError('La revisión comercial requiere fecha ISO y motivo, aunque no haya señal')
            try:
                datetime.fromisoformat(data['commercial_reviewed_at'])
            except ValueError:
                raise ValueError('commercial_reviewed_at requiere fecha ISO') from None
        return data

    def decide_many(self, entries):
        if not isinstance(entries, list) or not entries:
            raise ValueError("Se requiere una lista no vacía de decisiones con company_id y decision")
        prepared = []
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"company_id", "decision"} or not isinstance(entry['company_id'],str) or not entry['company_id'] or entry["company_id"] in seen:
                raise ValueError("Formato: company_id único y decision por registro")
            seen.add(entry["company_id"])
            prepared.append((entry["company_id"], entry["decision"], self._validate_decision(entry["company_id"], entry["decision"])))
        with self.db:
            for company_id, decision, data in prepared:
                self.db.execute("INSERT INTO decisions VALUES(?,?,?) ON CONFLICT(company_id) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at", (company_id, json_text(data), now()))
                self.db.execute("INSERT INTO events(at,kind,data) VALUES(?,?,?)", (now(), "decision", json_text({"company_id": company_id, "decision": decision})))
        self._identity_index = None
        return {"reviewed": len(prepared)}

    def decide(self, company_id, decision):
        self.decide_many([{"company_id": company_id, "decision": decision}])

    def merge(self, keep, other, reason):
        if keep == other or not reason:
            raise ValueError("Fusión requiere dos IDs distintos y motivo")
        if self.db.execute("SELECT COUNT(*) FROM companies WHERE id IN (?,?)", (keep, other)).fetchone()[0] != 2:
            raise ValueError("IDs inexistentes")
        # Overrides of the discarded entity must be resolved explicitly.
        if self.db.execute("SELECT 1 FROM decisions WHERE company_id=?", (other,)).fetchone():
            raise ValueError("La entidad secundaria tiene decisiones: resuélvelas antes de fusionar")
        with self.db:
            self.db.execute("UPDATE observations SET company_id=? WHERE company_id=?", (keep, other))
            self.db.execute("UPDATE jobs SET company_id=? WHERE company_id=?", (keep, other))
            self.db.execute("DELETE FROM companies WHERE id=?", (other,))
        self.event("merge", {"keep": keep, "other": other, "reason": reason})
        self._identity_index = None

    def enqueue(self, kind, url, source_id=None, company_id=None, payload=None):
        url = web_url(url)
        if not url:
            return False
        key = digest(json_text([kind, source_id, company_id, url]))[:24]
        cur = self.db.execute("INSERT OR IGNORE INTO jobs(id,kind,source_id,company_id,url,updated_at,payload) VALUES(?,?,?,?,?,?,?)",
                              (key, kind, source_id, company_id, url, now(), json_text(payload or {})))
        self.db.commit()
        return bool(cur.rowcount)

    def queue_source(self, source_id):
        spec = self.active_source(source_id)
        if not spec.get("url"):
            raise ValueError("Fuente local: usa import-file")
        return self.enqueue("directory", spec["url"], source_id=source_id)

    def queue_captures(self, entries):
        if isinstance(entries,dict):entries=entries.get('fichas')
        if not isinstance(entries,list) or not entries or len(entries)>1000:
            raise ValueError('Lista de 1–1.000 URLs o resultado de profile-discover requerido')
        urls=[]
        for entry in entries:
            url=entry.get('url','') if isinstance(entry,dict) else entry
            if not isinstance(url,str) or not url.lower().startswith(('http://','https://')) or not web_url(url):
                raise ValueError('Cada captura requiere URL HTTP(S) explícita')
            urls.append(web_url(url))
        return {'queued':sum(self.enqueue('capture',url) for url in dict.fromkeys(urls)),
                'urls':len(set(urls)),'network_requests':0}

    def capture_results(self):
        rows=self.db.execute("SELECT r.* FROM responses r JOIN jobs j ON j.id=r.job_id WHERE j.kind='capture' AND j.state='done' AND r.id=(SELECT MAX(r2.id) FROM responses r2 WHERE r2.job_id=j.id) ORDER BY j.rowid")
        return [{'path':str((self.run/r['path']).resolve()),'url':r['final_url'],'requested_url':r['url'],'sha256':r['sha256']} for r in rows]

    def queue_websites(self, include_with_email=False, limit=None):
        count = 0
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError('limit debe ser un entero positivo')
        from .reporting import balanced
        for item in balanced(self.materialize()):
            if item["website"] and (include_with_email or not item["email"]) and not item["decision"].get("exclude"):
                count += self.enqueue("contact", item["website"], company_id=item["company_id"], payload={"root": item["website"],"enrich":include_with_email})
                if limit is not None and count >= limit:
                    break
        return count

    def ingest_contact_page(self, company_id, text, url, raw=None, save_snapshot=True):
        """Reuse an already inspected official page; preserve all email evidence."""
        from .contacts import prepare_contact
        item = next((i for i in self.materialize() if i['company_id'] == company_id), None)
        record, root = prepare_contact(item, text, url)
        sid = 'web-' + company_id
        if not self.db.execute('SELECT 1 FROM sources WHERE id=?', (sid,)).fetchone():
            self.add_source({'id':sid, 'kind':'html', 'url':root, 'row_selector':'body',
                             'selectors':{'name':'title'}, 'role':'enrichment',
                             'source_group':'Webs de empresas', 'publication_verified':True,
                             'publication_basis':'Página de la web identificada del negocio; correos externos quedan pendientes de atribución.'})
        self.active_source(sid)
        body = raw if raw is not None else text.encode('utf-8')
        sha = self.snapshot(body, url, extension='html') if save_snapshot else None
        result = self.ingest(sid, url, record, company_id=company_id)
        self.event('contact_page', {'company_id':company_id,'url':url,'emails':len(emails(record['email'])),
                                   'snapshot_sha256':sha,'result':result})
        return {'company_id':company_id,'emails_found':len(emails(record['email'])),
                'pending_attribution':len(record['needs_attribution_review']),'snapshot_sha256':sha,'result':result}

    def contact_evidence(self, company_id):
        item = next((i for i in self.materialize() if i['company_id']==company_id),None)
        if item is None:
            raise ValueError('Empresa activa inexistente')
        evidence = []
        for row in self.db.execute('SELECT source_id,locator,record,observed_at FROM observations WHERE company_id=? AND active=1',(company_id,)):
            record = json.loads(row['record'])
            if not record.get('publication_verified'): continue
            for address in record.get('emails',[]):
                evidence.append({'email':address,'source':record.get('email_source') or row['locator'],
                                 'observed_at':row['observed_at'],'source_id':row['source_id'],
                                 'selected_primary':address==item['email'],
                                 'pending_attribution':address in item['emails_pending_attribution']})
        return {'company_id':company_id,'emails':evidence}

    def materialize(self):
        by_company = defaultdict(list)
        for row in self.db.execute("SELECT * FROM observations WHERE active=1 ORDER BY observed_at,id"):
            by_company[row["company_id"]].append(row)
        decisions = {r["company_id"]: json.loads(r["data"]) for r in self.db.execute("SELECT * FROM decisions")}
        items = []
        for company_id, observations in by_company.items():
            item = {field: "" for field in EXPORT_FIELDS}
            item.update(company_id=company_id, sector=self.config["sector"], decision=decisions.get(company_id, {}))
            item["_source_ids"] = sorted({row["source_id"] for row in observations})
            decision = item["decision"]
            found_emails = {}
            attribution_pending = set()
            scope_ok = False
            country_ok = False
            ambiguous = False
            for row in observations:
                record = json.loads(row["record"])
                for field in FIELDS:
                    if field in item and record.get(field):
                        item[field] = record[field]
                item["observed_at"] = row["observed_at"]
                scope = record.get("scope", {})
                scope_ok |= bool(scope.get("verified") and scope.get("sector") == self.config["sector"])
                country = norm(record.get("country"))
                if country:
                    country_ok = country in {"es", "espana", "spain"}
                elif not country_ok:
                    country_ok = bool(scope.get("verified") and scope.get("country") == "ES")
                ambiguous |= bool(record.get("ambiguous_identity"))
                for address in record.get("emails", []):
                    if record.get("publication_verified"):
                        found_emails[address] = (record.get("email_source") or row["locator"], row["observed_at"])
                        if address in record.get("needs_attribution_review", []):
                            attribution_pending.add(address)
            for field in ("name", "city", "province", "region", "address", "website"):
                if field in decision:
                    item[field] = decision[field]
            item["website_status"] = "web_identificada" if item["website"] else "no_investigado"
            item["priority"] = "sin_evaluar"
            for field in COMMERCIAL_FIELDS:
                if field in decision:
                    item[field] = decision[field]
            if "sector_match" in decision:
                scope_ok = decision["sector_match"] is True
            if "country" in decision:
                country_ok = decision["country"] == "ES"
            identity = bool(item["name"] and (item["city"] or item["address"] or item["phone"] or item["website"]))
            if "identity_confirmed" in decision:
                identity = decision["identity_confirmed"] is True
                ambiguous = not identity
            suitable = [e for e in found_emails if e not in attribution_pending and (email_kind(e) == "publicado_formato_valido_buzon_desconocido" or (self.config["fixture_mode"] and email_kind(e) == "ficticio"))]
            if decision.get("email_approved"):
                if decision["email_approved"] not in found_emails:
                    raise ValueError("Email aprobado sin evidencia publicada en las observaciones")
                suitable = [decision["email_approved"]] + [e for e in suitable if e != decision["email_approved"]]
            suitable = list(dict.fromkeys(suitable))
            item["email"] = next(iter(suitable), "")
            item["additional_emails"] = suitable[1:]
            item["all_published_emails"] = sorted(found_emails)
            item["emails_pending_attribution"] = sorted(attribution_pending)
            if item["email"]:
                item["email_source"] = found_emails[item["email"]][0]
                item["email_observed_at"] = found_emails[item["email"]][1]
                item["identity_observed_at"] = item["observed_at"]
                item["observed_at"] = item["email_observed_at"]
                item["email_status"] = email_kind(item["email"])
            else:
                item["email_status"] = "sin_email_publicado_admisible"
            problems = []
            if not identity or ambiguous:
                problems.append("identidad_por_resolver")
            if not scope_ok:
                problems.append("sector_sin_confirmar")
            if not country_ok:
                problems.append("ubicacion_ES_sin_confirmar")
            haystack = norm(" ".join(item.get(k, "") for k in ("name", "subsector")))
            if any(norm(term) in haystack for term in self.config.get("exclude_terms", [])):
                problems.append("exclusion_configurada")
            if self.config.get("include_terms") and not any(norm(term) in haystack for term in self.config["include_terms"]):
                problems.append("inclusion_por_revisar")
            relationship = norm(item["relationship"])
            volume_mode = self.config.get("mode", "legacy") == "volume"
            if not volume_mode and relationship in {"centralizada", "sede", "cadena"} and decision.get("independence") is not True:
                problems.append("autonomia_por_confirmar")
            if decision.get("independence") is False or decision.get("exclude"):
                problems.append("exclusion_revisada")
            item["identity_status"] = "por_resolver" if "identidad_por_resolver" in problems else "identidad_suficiente"
            item["problems"] = problems
            item["reason"] = "; ".join(problems) or (("emails_contextuales_o_atribucion_por_revisar" if found_emails else "sin_email_publicado_admisible") if not item["email"] else "criterios_cumplidos")
            item["review_reason"] = decision.get("reason", "")
            items.append(item)
        # Same brand + central site/contact, different addresses: keep separate but require autonomy review.
        grouped = defaultdict(list)
        for item in items:
            for contact in [host(item["website"]), item["phone"], *item["all_published_emails"]]:
                if contact and norm(item["name"]):
                    grouped[(norm(item["name"]), contact)].append(item)
        for group in grouped.values():
            if len({i["company_id"] for i in group}) < 2:
                continue
            for item in group:
                if not volume_mode and item["decision"].get("independence") is not True and "autonomia_por_confirmar" not in item["problems"]:
                    item["problems"].append("autonomia_por_confirmar")
                    item["reason"] = "; ".join(item["problems"])
                    item["relationship"] = "posibles_sedes_contacto_compartido"
        name_city = defaultdict(list)
        for item in items:
            if item["name"] and item["city"]:
                name_city[(norm(item["name"]), norm(item["city"]))].append(item)
        for group in name_city.values():
            if len(group) > 1:
                for item in group:
                    if not volume_mode and item["decision"].get("independence") is not True and "autonomia_por_confirmar" not in item["problems"]:
                        item["problems"].append("identidades_similares_por_resolver")
                        item["reason"] = "; ".join(item["problems"])
        email_owners = defaultdict(set)
        for item in items:
            for address in [item["email"], *item["additional_emails"]]:
                if address:
                    email_owners[address].add(item["company_id"])
        for item in items:
            item["shared_email"] = bool(item["email"] and len(email_owners[item["email"]]) > 1)
            if item["shared_email"] and not item["relationship"]:
                item["relationship"] = "email_compartido_revisar_destinatarios"
        return sorted(items, key=lambda i: (len(i["problems"]), norm(i["province"]), norm(i["city"]), norm(i["name"]), i["company_id"]))

    def summary(self, items=None, exclude_ids=None, exclude_emails=None, include_ids=None):
        from .reporting import metrics
        items = self.materialize() if items is None else items
        qualified = [i for i in items if not i["problems"] and i["email"]]
        selection = self.selected_items(items=items, exclude_ids=exclude_ids, exclude_emails=exclude_emails, include_ids=include_ids)
        brief_required = self.config.get("brief_required", True)
        volume_mode = self.config.get("mode", "legacy") == "volume"
        summary_population = selection["main"] if volume_mode else qualified
        source_stats = []
        for source in self.db.execute("SELECT id FROM sources ORDER BY id"):
            rows = list(self.db.execute("SELECT company_id,record FROM observations WHERE source_id=? AND active=1", (source["id"],)))
            source_stats.append({"source_id": source["id"], "records": len(rows), "companies": len({r["company_id"] for r in rows}),
                                 "rows_with_email": sum(bool(json.loads(r["record"]).get("emails")) for r in rows)})
        jobs = {r["state"]: r["n"] for r in self.db.execute("SELECT state,COUNT(*) n FROM jobs GROUP BY state")}
        brief = self.brief_status()
        return {**metrics(self, items, qualified), "engine_version": __version__, "sector": self.config["sector"], "fixture_mode": self.config["fixture_mode"],
                "generated_at": now(), "target": self.config["target"], "raw_records": self.db.execute("SELECT COUNT(*) FROM observations WHERE active=1").fetchone()[0],
                "unique_companies": len(items), "qualified_with_email": len(qualified),
                "main_export_rows": len(selection["main"]), "reserve": len(selection["reserve"]),
                "others": len(selection["others"]), "pending": len(selection["pending"]),
                "shared_primary_emails": len(selection["shared"]) if volume_mode else sum(i["shared_email"] for i in items),
                **({"excluded_used": len(selection["excluded_used"])} if volume_mode else {}), "jobs": jobs, "sources": source_stats,
                "province_counts": dict(Counter(i["province"] or "desconocida" for i in summary_population)),
                "subsector_counts": dict(Counter(i["subsector"] or "desconocido" for i in summary_population)),
                "missing_fields": {k: sum(not i[k] for i in summary_population) for k in ("city", "province", "phone", "website")},
                "brief": brief, "status": "complete" if len(selection["main"]) >= self.config["target"] and (brief["reviewed"] or not brief_required) else "partial",
                "limits": ["Email publicado no demuestra entregabilidad ni interés.", "Identidad suficiente según reglas no equivale a verificación registral.",
                           "Cobertura de las fuentes, no censo ni muestra representativa.", "No incluye email comercial ni contenido o diseño web."]}

    def selected_items(self, exclude_ids=None, exclude_emails=None, items=None, include_ids=None):
        """Clasifica la selección actual; exclusiones explícitas solo afectan esta lectura/exportación."""
        from .reporting import balanced
        items = self.materialize() if items is None else items
        sources = {r['id']: json.loads(r['spec']) for r in self.db.execute('SELECT id,spec FROM sources')}
        used_ids = {str(v) for v in (exclude_ids or ())}
        used_emails = {str(v).strip().casefold() for v in (exclude_emails or ()) if str(v).strip()}
        has_include_ids = include_ids is not None
        requested_ids = [str(v) for v in (include_ids or ())]
        if len(requested_ids) != len(set(requested_ids)):
            raise ValueError("include_ids no puede contener IDs repetidos")
        result = {key: [] for key in ("main", "reserve", "others", "pending", "shared", "excluded_used")}
        with_email = []
        for item in items:
            if item["problems"]:
                result["pending"].append(item)
            elif not item["email"]:
                result["others"].append(item)
            else:
                with_email.append(item)
        eligible = []
        for item in with_email:
            if item["company_id"] in used_ids or item["email"].strip().casefold() in used_emails:
                result["excluded_used"].append(item)
            else:
                eligible.append(item)
        # In volume, keep one balanced identity per primary recipient; other identities
        # sharing that recipient remain separate in the shared bucket.
        if self.config.get("mode", "legacy") == "volume":
            owners = defaultdict(list)
            for item in eligible:
                owners[item["email"].strip().casefold()].append(item)
            representatives = []
            for group in owners.values():
                if len(group) == 1:
                    representatives.extend(group)
                    continue
                representative = balanced(group, sources)[0]
                representatives.append(representative)
                result["shared"].extend(item for item in group if item["company_id"] != representative["company_id"])
            eligible = representatives
        ordered = balanced(eligible, sources)
        target = self.config["target"]
        if has_include_ids:
            by_id = {item["company_id"]: item for item in ordered}
            missing = [company_id for company_id in requested_ids if company_id not in by_id]
            if missing:
                raise ValueError("include_ids requiere empresas elegibles representantes y no excluidas; IDs no disponibles: " + ", ".join(missing[:5]))
            result["main"] = [by_id[company_id] for company_id in requested_ids]
            remaining = [item for item in ordered if item["company_id"] not in set(requested_ids)]
            result["reserve"] = remaining
        else:
            result["main"] = ordered[:target]
            result["reserve"] = ordered[target:]
        return result

    def write_progress(self, summary=None):
        summary = self.summary() if summary is None else summary
        atomic_json(self.run / "resumen.json", summary)
        return summary

    def brief_status(self):
        if not self.config.get("brief_required", "mode" not in self.config):
            return {"required": False, "reviewed": False, "status": "not_required", "reason": "no_requerida"}
        path = self.run / "investigacion/brief-review.json"
        if not path.exists():
            return {"required": True, "reviewed": False, "status": "pending", "reason": "investigacion_pendiente"}
        meta = json.loads(path.read_text(encoding="utf-8"))
        brief = self.run / "entregables/brief-sector.md"
        evidence = self.run / "investigacion/evidencias.json"
        valid = brief.exists() and evidence.exists() and digest(brief.read_bytes()) == meta.get("brief_sha256") and digest(evidence.read_bytes()) == meta.get("evidence_sha256")
        current = self.db.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE kind IN ('file_imported','source_page','decision','merge','configured','contact_page','source_retired')").fetchone()[0]
        # Sector research is shared across volume lots. Changing the catalogue
        # updates its composition report, not the underlying market evidence.
        if self.config.get('mode', 'legacy') != 'volume' and current != meta.get("data_revision", 0):
            valid = False
        return {"required": True, "reviewed": bool(valid), "status": "reviewed" if valid else "invalid", "reviewed_at": meta.get("reviewed_at"), "reviewer": meta.get("reviewer"), "reason": "revision_registrada" if valid else "archivos_o_base_modificados_revisar"}

    def brief_template(self):
        path = self.run / "entregables/brief-sector.md"
        if path.exists():
            raise ValueError("El brief ya existe; no se sobrescribe investigación")
        lines = [f"# Investigación: {self.config['sector']}", "", "Estado: pendiente de investigación. Esta plantilla no es una investigación terminada.", ""]
        for title in BRIEF_HEADINGS:
            lines.extend(["## " + title, "", "Pendiente.", ""])
        path.write_text("\n".join(lines), encoding="utf-8")
        atomic_json(self.run / "investigacion/evidencias.json", [])
        return str(path)

    def review_brief(self, reviewer):
        if not reviewer.strip():
            raise ValueError("Se requiere responsable de revisión")
        path = self.run / "entregables/brief-sector.md"
        evidence_path = self.run / "investigacion/evidencias.json"
        text = path.read_text(encoding="utf-8")
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        if not isinstance(evidence, list) or not evidence:
            raise ValueError("La investigación necesita evidencia inspeccionada")
        for source in evidence:
            if not all(source.get(k) for k in ("url", "title", "checked_at", "claim", "scope")) or source.get("inspected") is not True:
                raise ValueError("Cada evidencia requiere url,title,checked_at,claim,scope e inspected=true")
        for title in BRIEF_HEADINGS:
            match = re.search(r"^## " + re.escape(title) + r"\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
            if not match or len(match[1].strip()) < 20 or re.fullmatch(r"\s*(?:Pendiente\.?|TODO)\s*", match[1], re.I):
                raise ValueError("Bloque incompleto: " + title)
        meta = {"reviewer": reviewer, "reviewed_at": now(), "brief_sha256": digest(path.read_bytes()), "evidence_sha256": digest(evidence_path.read_bytes()),
                "data_revision": self.db.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE kind IN ('file_imported','source_page','decision','merge','configured','contact_page','source_retired')").fetchone()[0],
                "meaning": "Revisión declarada por responsable; el motor no confirma por sí mismo veracidad o calidad comercial."}
        atomic_json(self.run / "investigacion/brief-review.json", meta)
        self.event("brief_reviewed", meta)
        return meta

    def export(self, excel=True, exclude_ids=None, exclude_emails=None, output_dir=None, include_ids=None):
        items = self.materialize()
        selection = self.selected_items(exclude_ids=exclude_ids, exclude_emails=exclude_emails, items=items, include_ids=include_ids)
        summary = self.summary(items, exclude_ids=exclude_ids, exclude_emails=exclude_emails, include_ids=include_ids)
        # Keep summary and actual export aligned when a caller supplies prior-use exclusions.
        summary = {**summary, "main_export_rows": len(selection["main"]), "reserve": len(selection["reserve"]), "others": len(selection["others"]),
                   "pending": len(selection["pending"])}
        if self.config.get("mode", "legacy") == "volume":
            summary.update(shared_primary_emails=len(selection["shared"]), excluded_used=len(selection["excluded_used"]))
        main, reserve = selection["main"], selection["reserve"]
        others, pending, shared = selection["others"], selection["pending"], selection["shared"]
        output = Path(output_dir).resolve() if output_dir is not None else self.run / "entregables"
        output.mkdir(parents=True, exist_ok=True)
        exports = [("empresas.csv", main), ("otros-contactos.csv", others), ("pendientes.csv", pending), ("reserva.csv", reserve)]
        if self.config.get("mode", "legacy") == "volume":
            exports.append(("emails-compartidos.csv", shared))
        for filename, rows in exports:
            with (output / filename).open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=EXPORT_FIELDS, delimiter=";", extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    values = {k: " | ".join(row[k]) if isinstance(row[k], list) else row[k] for k in EXPORT_FIELDS}
                    # Spreadsheet formula injection: +34 phone is intentionally text too.
                    values = {k: "'"+str(v) if str(v).startswith(("=", "+", "-", "@", "\t", "\r")) else v for k, v in values.items()}
                    writer.writerow(values)
        data = {"summary": summary, "companies": main, "others": others, "pending": pending}
        if self.config.get("mode", "legacy") == "volume":
            data.update(reserve=reserve, shared=shared, excluded_used=selection["excluded_used"])
        atomic_json(output / "datos-exportacion.json", data)
        atomic_json(output / "resumen.json", summary)
        self.write_progress(summary if output_dir is None else None)
        excel_path = output / "empresas.xlsx"
        if excel:
            result = subprocess.run([sys.executable, '-m', 'sistema_prospeccion.excel_export', '--input', str(output / 'datos-exportacion.json'), '--output', str(excel_path)], capture_output=True, text=True, encoding='utf-8', errors='replace')
            if result.returncode:
                raise RuntimeError("CSV y JSON conservados; exportación Excel falló: " + result.stderr[-600:])
        event_data = {"main_rows": len(main), "excel": excel, "status": summary["status"]}
        if self.config.get("mode", "legacy") == "volume":
            event_data.update(reserve_rows=len(reserve), shared_rows=len(shared), excluded_used=len(selection["excluded_used"]))
        self.event("export", event_data)
        return {"output": str(output), "excel": str(excel_path) if excel else None, "summary": summary}
