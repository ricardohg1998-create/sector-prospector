"""HTTP, caché de evidencia y cola reanudable; sin navegador o IA por registro."""
from __future__ import annotations

import json
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser
from datetime import datetime, timedelta, timezone

from .engine import now
from .parsing import contact_page, parse_records, host, web_url

USER_AGENT = "RicardoSectorResearch/1.0"


class FetchError(Exception):
    def __init__(self, message, retry=False):
        super().__init__(message)
        self.retry = retry


class BudgetReached(FetchError):
    pass


class HTTP:
    def __init__(self, store, budget):
        self.store, self.budget, self.used = store, budget, 0
        self.robots = {}
        self.last_domain_request = {}

    def _request(self, url, job_id=None, extension="html"):
        if self.used >= self.budget:
            raise BudgetReached("Presupuesto de peticiones de esta invocación alcanzado")
        parts = urlsplit(url)
        if not web_url(url):
            raise FetchError("URL no HTTP(S) o con credenciales")
        domain = parts.netloc
        wait = self.store.config["source_delay_seconds"] - (time.monotonic()-self.last_domain_request.get(domain, 0))
        if wait > 0:
            time.sleep(wait)
        self.used += 1
        self.last_domain_request[domain] = time.monotonic()
        try:
            request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json,text/csv,text/plain;q=0.8"})
            with urlopen(request, timeout=self.store.config["timeout_seconds"]) as response:
                maximum = self.store.config["max_response_bytes"]
                body = response.read(maximum + 1)
                if len(body) > maximum:
                    raise FetchError("Respuesta supera max_response_bytes; usa descarga/importación explícita")
                final = response.geturl()
                if not web_url(final):
                    raise FetchError("Redirección a destino no HTTP(S)")
                self.store.snapshot(body, url, final, job_id=job_id, status=response.status, extension=extension)
                charset = response.headers.get_content_charset() or "utf-8"
                return body.decode(charset, errors="replace"), final
        except HTTPError as exc:
            retry = exc.code in {408, 425, 429, 500, 502, 503, 504}
            raise FetchError(f"HTTP {exc.code}", retry=retry) from None
        except (URLError, socket.timeout, TimeoutError, OSError) as exc:
            raise FetchError(type(exc).__name__, retry=True) from None

    def fetch(self, url, job_id=None):
        parts = urlsplit(url)
        origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        if origin not in self.robots:
            parser = RobotFileParser()
            try:
                text, _ = self._request(origin + "/robots.txt", extension="txt")
                parser.parse(text.splitlines())
            except BudgetReached:
                raise
            except FetchError as exc:
                if str(exc) == "HTTP 404":
                    parser.parse([])
                else:
                    # A failed policy check is an unavailable source, not a negative crawl result.
                    raise FetchError("robots.txt no disponible: " + str(exc), retry=exc.retry) from None
            self.robots[origin] = parser
        parser = self.robots[origin]
        if not parser.can_fetch(USER_AGENT, url):
            raise FetchError("robots.txt excluye esta ruta")
        return self._request(url, job_id=job_id)


def claim(store, kind=None):
    expired = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    store.db.execute("BEGIN IMMEDIATE")
    store.db.execute("UPDATE jobs SET state='queued',error='lease_expired' WHERE state='running' AND updated_at<?", (expired,))
    query = "SELECT * FROM jobs WHERE state IN ('queued','retry')"
    args = []
    if kind:
        query += " AND kind=?"
        args.append(kind)
    query += " ORDER BY rowid LIMIT 1"
    row = store.db.execute(query, args).fetchone()
    if row:
        store.db.execute("UPDATE jobs SET state='running',attempts=attempts+1,updated_at=? WHERE id=?", (now(), row["id"]))
    store.db.commit()
    return dict(row) if row else None


def work(store, budget=None, kind=None, transport=None, continue_after_target=False):
    budget = budget or store.config["request_budget"]
    transport = transport or HTTP(store, budget)
    completed = failed = 0
    interrupted = False
    checkpoint_every = 25 if store.config.get('mode') == 'volume' else 1
    checkpoint_count = 0
    try:
        while transport.used < budget:
            if not continue_after_target and checkpoint_count % checkpoint_every == 0:
                target_field = 'main_export_rows' if store.config.get('mode') == 'volume' else 'qualified_with_email'
                if store.summary()[target_field] >= store.config["target"]:
                    break
            job = claim(store, kind)
            if not job:
                break
            if job["kind"] == "contact" and not store.db.execute("SELECT 1 FROM observations WHERE company_id=? AND active=1", (job["company_id"],)).fetchone():
                store.db.execute("UPDATE jobs SET state='done',error='entity_no_longer_active',updated_at=? WHERE id=?", (now(), job["id"]))
                store.db.commit()
                continue
            try:
                text, final = transport.fetch(job["url"], job_id=job["id"])
                if job['kind']=='capture':
                    # HTTP already archives raw response bytes under this job.
                    store.event('page_captured',{'job_id':job['id'],'url':final})
                elif job["kind"] == "directory":
                    source = store.source(job["source_id"])
                    records, next_urls = parse_records(text, source, final)
                    if source['kind']=='html' and (not records or not any(r.get('name') for _,r in records)):
                        raise ValueError('Conector HTML vacío o sin nombres: revisar configuración; datos previos conservados')
                    # Successful refresh of a page retires rows that disappeared from that page.
                    prefix = final + "#"
                    store.db.execute("UPDATE observations SET active=0 WHERE source_id=? AND substr(locator,1,?)=?", (job["source_id"], len(prefix), prefix))
                    store.db.commit()
                    for locator, record in records:
                        store.ingest(job["source_id"], locator, record)
                    page_count = store.db.execute("SELECT COUNT(*) FROM jobs WHERE source_id=? AND kind='directory'", (job["source_id"],)).fetchone()[0]
                    for link in next_urls:
                        if host(link) == host(source["url"]) and page_count < source.get("max_pages", 100):
                            page_count += store.enqueue("directory", link, source_id=job["source_id"])
                    store.event("source_page", {"source": job["source_id"], "records": len(records), "job_id": job["id"]})
                elif job["kind"] == "contact":
                    payload = json.loads(job["payload"])
                    root = payload["root"]
                    if host(final) != host(root):
                        raise FetchError("Cambio de dominio: confirmar web oficial antes de extraer")
                    found, links = contact_page(text, final)
                    store.ingest_contact_page(job['company_id'], text, final, save_snapshot=False)
                    count = store.db.execute("SELECT COUNT(*) FROM jobs WHERE company_id=? AND kind='contact'", (job["company_id"],)).fetchone()[0]
                    # Stop expanding this business once an admissible email is found.
                    entity = next(i for i in store.materialize() if i["company_id"] == job["company_id"])
                    if not entity["email"] or payload.get('enrich'):
                        for link in links:
                            if count < store.config["website_pages"]:
                                count += store.enqueue("contact", link, company_id=job["company_id"], payload={"root": root,"enrich":bool(payload.get('enrich'))})
                store.db.execute("UPDATE jobs SET state='done',updated_at=?,error='' WHERE id=?", (now(), job["id"]))
                store.db.commit()
                completed += 1
            except BudgetReached:
                store.db.execute("UPDATE jobs SET state='queued',attempts=attempts-1,updated_at=? WHERE id=?", (now(), job["id"]))
                store.db.commit()
                break
            except (FetchError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
                retry = isinstance(exc, FetchError) and exc.retry and job["attempts"]+1 < store.config["max_attempts"]
                state = "retry" if retry else "failed"
                store.db.execute("UPDATE jobs SET state=?,updated_at=?,error=? WHERE id=?", (state, now(), str(exc)[:240], job["id"]))
                store.db.commit()
                failed += not retry
                if retry:
                    time.sleep(min(2**job["attempts"], 4))
            checkpoint_count += 1
            if checkpoint_count % checkpoint_every == 0:
                store.write_progress()
    except KeyboardInterrupt:
        interrupted = True
        if 'job' in locals() and job:
            store.db.execute("UPDATE jobs SET state='queued',updated_at=? WHERE id=? AND state='running'", (now(), job["id"]))
            store.db.commit()
    result = {"completed": completed, "failed": failed, "requests": transport.used, "interrupted": interrupted, "budget": budget}
    store.event("work_finished", result)
    store.write_progress()
    return result
