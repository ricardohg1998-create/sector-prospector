"""Pruebas locales con empresas ficticias y transporte en memoria. Sin red."""
import contextlib
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sistema_prospeccion.engine import Store, new_run, atomic_json, BRIEF_HEADINGS
from sistema_prospeccion.parsing import contact_page, parse_records, norm, emails, phone, select, DOM
from sistema_prospeccion.network import work, FetchError, BudgetReached, HTTP
from sistema_prospeccion.cli import main


class FakeHTTP:
    def __init__(self, pages, budget=100):
        self.pages, self.budget, self.used = pages, budget, 0

    def fetch(self, url, job_id=None):
        if self.used >= self.budget:
            raise BudgetReached("budget")
        self.used += 1
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return page, url


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = new_run("Talleres ficticios", self.root / "run", fixture=True, mode="qualified")
        self.store = Store(self.run)
        self.add_source()
        self.network_guard = patch("socket.create_connection", side_effect=AssertionError("Prohibida red en pruebas"))
        self.network_guard.start()

    def tearDown(self):
        self.network_guard.stop()
        self.store.db.close()
        self.temp.cleanup()

    def add_source(self, sid="fixture", **overrides):
        spec = {"id": sid, "kind": "csv", "path": str(self.root / "datos.csv"),
                "scope": {"country": "ES", "sector": "Talleres ficticios", "verified": True, "basis": "Fuente ficticia de test"},
                "publication_verified": True, "publication_basis": "Datos ficticios publicados en fixture"}
        spec.update(overrides)
        self.store.add_source(spec)

    def row(self, name="Taller FICTICIO Uno", city="Sevilla", email="info@uno.example", **extra):
        return {"name": name, "city": city, "address": "Calle FICTICIA 1", "email": email, "country": "ES", **extra}

    def ingest(self, locator="row1", **extra):
        return self.store.ingest("fixture", locator, self.row(**extra))

    def write_csv(self, rows):
        with (self.root / "datos.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def test_init_does_not_overwrite(self):
        with self.assertRaises(ValueError):
            new_run("Otro", self.run)

    def test_duplicate_same_source_idempotent(self):
        self.assertEqual(self.ingest(), "imported")
        self.assertEqual(self.ingest(), "unchanged")
        self.assertEqual(self.store.summary()["qualified_with_email"], 1)

    def test_cross_source_same_business_merges(self):
        self.add_source("second")
        self.ingest()
        self.store.ingest("second", "otherrow", self.row(email="otro@uno.example"))
        result = self.store.materialize()
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]["additional_emails"]), 1)
        self.assertEqual(self.store.summary()["raw_records"], 2)

    def test_same_domain_alone_does_not_merge(self):
        self.ingest(website="https://grupo.example/")
        self.ingest("row2", name="Taller FICTICIO Dos", city="Cádiz", website="https://grupo.example/", email="info@dos.example")
        self.assertEqual(len(self.store.materialize()), 2)

    def test_provider_ids_do_not_collide_between_sources(self):
        self.add_source("second")
        self.ingest(entity_key="123")
        self.store.ingest("second", "otherrow", self.row(name="Otro FICTICIO", city="Madrid", email="info@dos.example", entity_key="123"))
        self.assertEqual(self.store.summary()["unique_companies"], 2)

    def test_chain_locations_require_autonomy(self):
        self.ingest(website="https://grupo.example/")
        self.ingest("row2", city="Cádiz", website="https://grupo.example/", address="Otra 9")
        self.assertEqual(self.store.summary()["pending"], 2)
        for item in self.store.materialize():
            self.store.decide(item["company_id"], {"independence": True, "reason": "Autonomía confirmada en fixture"})
        self.assertEqual(self.store.summary()["qualified_with_email"], 2)

    def test_different_businesses_shared_email_remain_distinct(self):
        self.ingest()
        self.ingest("row2", name="Negocio FICTICIO diferente", city="Madrid")
        result = self.store.summary()
        self.assertEqual(result["unique_companies"], 2)
        self.assertEqual(result["shared_primary_emails"], 2)

    def test_missing_email_is_other_not_target(self):
        self.ingest(email="", phone="954000000")
        result = self.store.summary()
        self.assertEqual(result["others"], 1)
        self.assertEqual(result["qualified_with_email"], 0)

    def test_unverified_publication_not_target(self):
        self.add_source("unverified", publication_verified=False)
        self.store.ingest("unverified", "one", self.row())
        self.assertEqual(self.store.summary()["qualified_with_email"], 0)

    def test_foreign_country_overrides_scope(self):
        self.ingest(country="PT")
        self.assertEqual(self.store.summary()["pending"], 1)
        self.assertIn("ubicacion_ES_sin_confirmar", self.store.materialize()[0]["reason"])

    def test_restricted_contact_not_target_without_review(self):
        self.ingest(email="privacidad@empresa.es")
        self.assertEqual(self.store.summary()["qualified_with_email"], 0)

    def test_review_requires_actual_published_email(self):
        self.ingest()
        cid = self.store.materialize()[0]["company_id"]
        with self.assertRaisesRegex(ValueError, "sin evidencia"):
            self.store.decide(cid, {"email_approved": "inventado@uno.example", "reason": "Test"})
        self.assertEqual(self.store.summary()["qualified_with_email"], 1)

    def test_false_boolean_string_rejected(self):
        self.ingest()
        cid = self.store.materialize()[0]["company_id"]
        with self.assertRaises(ValueError):
            self.store.decide(cid, {"sector_match": "false", "reason": "test"})

    def test_source_redefinition_rejected(self):
        with self.assertRaises(ValueError):
            self.add_source(publication_verified=False)

    def test_same_name_city_without_strong_identity_is_pending(self):
        self.ingest(address="")
        self.ingest("row2", address="", email="otro@uno.example")
        self.assertEqual(self.store.summary()["qualified_with_email"], 0)
        self.assertEqual(self.store.summary()["pending"], 2)

    def test_production_never_counts_fictitious_emails(self):
        real_run = new_run("Talleres ficticios", self.root / "production-run")
        with Store(real_run) as real:
            real.add_source(self.store.source("fixture"))
            real.ingest("fixture", "one", self.row())
            self.assertEqual(real.summary()["qualified_with_email"], 0)

    def test_budget_exhausted_returns_claimed_job_to_queue(self):
        self.ingest(email="", website="https://uno.example/")
        self.store.queue_websites()
        class BudgetTransport:
            used = 0
            def fetch(inner, *args, **kwargs):
                inner.used = 1
                raise BudgetReached("Test")
        work(self.store, budget=1, transport=BudgetTransport())
        job = self.store.db.execute("SELECT state,attempts FROM jobs").fetchone()
        self.assertEqual(tuple(job), ("queued", 0))

    def test_retry_is_bounded(self):
        self.ingest(email="", website="https://uno.example/")
        self.store.queue_websites()
        transport = FakeHTTP({"https://uno.example/": FetchError("HTTP 503", retry=True)})
        with patch("sistema_prospeccion.network.time.sleep"):
            work(self.store, transport=transport)
        self.assertEqual(transport.used, 2)
        self.assertEqual(self.store.summary()["jobs"], {"failed": 1})

    def test_source_ids_unknown_and_invalid_scope(self):
        with self.assertRaises(ValueError):
            self.store.source("missing")
        with self.assertRaises(ValueError):
            self.add_source("bad", scope={"verified": True, "sector": "otro", "country": "ES", "basis": "test"})

    def test_reopen_database_preserves_queue_and_businesses(self):
        self.ingest(email="", website="https://uno.example/")
        self.store.queue_websites()
        with Store(self.run) as reopened:
            self.assertEqual(reopened.summary()["unique_companies"], 1)
            self.assertEqual(reopened.summary()["jobs"], {"queued": 1})

    def test_csv_import_preserves_original_hash(self):
        from sistema_prospeccion.engine import digest
        self.write_csv([self.row()])
        original = (self.root / "datos.csv").read_bytes()
        self.store.ingest_file("fixture")
        self.assertEqual((self.root / "datos.csv").read_bytes(), original)
        self.assertEqual((self.run / "originales" / (digest(original)+".csv")).read_bytes(), original)

    def test_file_reorder_preserves_company_ids_and_removal(self):
        rows = [self.row(), self.row(name="Taller FICTICIO Dos", city="Madrid", email="info@dos.example")]
        self.write_csv(rows)
        self.store.ingest_file("fixture")
        before = {i["name"]: i["company_id"] for i in self.store.materialize()}
        self.write_csv(list(reversed(rows)))
        self.store.ingest_file("fixture")
        after = {i["name"]: i["company_id"] for i in self.store.materialize()}
        self.assertEqual(before, after)
        self.write_csv([rows[1]])
        self.store.ingest_file("fixture")
        self.assertEqual(self.store.summary()["unique_companies"], 1)
        self.assertGreater(len(list((self.run / "originales").iterdir())), 1)

    def test_configure_backup_and_validation(self):
        self.store.configure({"request_budget": 19})
        self.assertEqual(self.store.config["request_budget"], 19)
        self.assertEqual(len(list(self.run.glob("config.before-*.json"))), 1)
        with self.assertRaises(ValueError):
            self.store.configure({"website_pages": -1})

    def test_csv_export_target_reserve_and_text_safety(self):
        self.store.configure({"target": 1})
        self.ingest(name="=FICTICIO()", phone="954000000")
        self.ingest("row2", name="Segundo FICTICIO", email="info@dos.example")
        result = self.store.export(excel=False)
        self.assertEqual(result["summary"]["main_export_rows"], 1)
        self.assertEqual(result["summary"]["reserve"], 1)
        with (self.run / "entregables/empresas.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter=";"))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["name"].startswith("'="))
        self.assertTrue(rows[0]["phone"].startswith("'+34"))
        self.assertEqual(result["summary"]["status"], "partial")

    def test_empty_export(self):
        self.store.export(excel=False)
        self.assertEqual((self.run / "entregables/empresas.csv").read_text(encoding="utf-8-sig").count("\n"), 1)

    def test_1005_fictional_records(self):
        with self.store.db:
            for index in range(1005):
                self.store.ingest("fixture", str(index), self.row(name=f"FICTICIO {index}", email=f"info@f{index}.example"), commit=False)
        summary = self.store.export(excel=False)["summary"]
        self.assertEqual(summary["main_export_rows"], 1000)
        self.assertEqual(summary["reserve"], 5)
        self.assertTrue(summary["fixture_mode"])

    def test_brief_template_cannot_be_reviewed(self):
        self.store.brief_template()
        with self.assertRaises(ValueError):
            self.store.review_brief("test")
        self.assertFalse(self.store.brief_status()["reviewed"])

    def test_brief_modified_or_base_changed_invalidates_review(self):
        self.ingest()
        brief = self.run / "entregables/brief-sector.md"
        brief.write_text("# FICTICIO\n\n" + "\n".join("## " + h + "\nContenido ficticio desarrollado para comprobar integridad.\n" for h in BRIEF_HEADINGS), encoding="utf-8")
        atomic_json(self.run / "investigacion/evidencias.json", [{"url": "https://fuente.example/", "title": "FICTICIO", "checked_at": "2026-10-09", "claim": "Dato ficticio", "scope": "Test", "inspected": True}])
        self.store.review_brief("test")
        self.assertTrue(self.store.brief_status()["reviewed"])
        brief.write_text(brief.read_text(encoding="utf-8") + "Cambio", encoding="utf-8")
        self.assertFalse(self.store.brief_status()["reviewed"])
        self.store.review_brief("test")
        cid = self.store.materialize()[0]["company_id"]
        self.store.decide(cid, {"reason": "Nueva revisión", "independence": True})
        self.assertFalse(self.store.brief_status()["reviewed"])

    def test_job_queue_resume_and_no_duplicates(self):
        self.add_source("web", kind="html", path="", url="https://directorio.example/", row_selector=".biz", selectors={"name": "h2", "city": ".city", "email": ".email"}, next_selector="a.next")
        self.store.queue_source("web")
        self.assertFalse(self.store.queue_source("web"))
        transport = FakeHTTP({"https://directorio.example/": '<div class="biz"><h2>Taller FICTICIO</h2><span class="city">Sevilla</span><span class="email">info@uno.example</span></div><a class="next" href="/p2">2</a>',
                              "https://directorio.example/p2": '<div class="biz"><h2>Dos FICTICIO</h2><span class="city">Cádiz</span><span class="email">info@dos.example</span></div>'}, budget=1)
        work(self.store, budget=1, transport=transport)
        self.assertEqual(self.store.summary()["qualified_with_email"], 1)
        work(self.store, budget=5, transport=FakeHTTP(transport.pages))
        self.assertEqual(self.store.summary()["qualified_with_email"], 2)
        self.assertEqual(self.store.summary()["jobs"], {"done": 2})

    def test_contact_extraction_preserves_identity(self):
        self.ingest(email="", website="https://uno.example/")
        self.store.queue_websites()
        transport = FakeHTTP({"https://uno.example/": '<body><a href="/contacto">Contacto</a></body>',
                              "https://uno.example/contacto": '<body><a href="mailto:info@uno.example">Email</a></body>'})
        work(self.store, transport=transport)
        item = self.store.materialize()[0]
        self.assertEqual(item["name"], "Taller FICTICIO Uno")
        self.assertEqual(item["email"], "info@uno.example")
        self.assertEqual(item["email_source"], "https://uno.example/contacto")

    def test_external_agency_email_on_footer_does_not_count(self):
        self.ingest(email="", website="https://uno.example/")
        self.store.queue_websites()
        work(self.store, transport=FakeHTTP({"https://uno.example/": '<body>Web realizada por agencia@otro.example</body>'}))
        item = self.store.materialize()[0]
        self.assertFalse(item["email"])
        self.assertEqual(item["emails_pending_attribution"], ["agencia@otro.example"])

    def test_permanent_http_failure_is_visible(self):
        self.ingest(email="", website="https://uno.example/")
        self.store.queue_websites()
        work(self.store, transport=FakeHTTP({"https://uno.example/": FetchError("HTTP 403")}))
        self.assertEqual(self.store.summary()["jobs"], {"failed": 1})
        self.assertEqual(self.store.summary()["qualified_with_email"], 0)

    def test_work_cli_refuses_network_without_flag_and_fixture_network(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["work", "--run", str(self.run)]), 2)
            self.assertEqual(main(["work", "--run", str(self.run), "--network"]), 2)

    def test_http_robots_and_originals_with_mocked_urlopen(self):
        from email.message import Message
        class Response:
            status = 200
            headers = Message()
            def __init__(inner, text, url):
                inner.body, inner.url = text.encode(), url
            def __enter__(inner): return inner
            def __exit__(inner, *args): pass
            def read(inner, n): return inner.body[:n]
            def geturl(inner): return inner.url
        replies = [Response("User-agent: *\nDisallow: /privado\n", "https://uno.example/robots.txt"), Response("<body>FICTICIO</body>", "https://uno.example/")]
        transport = HTTP(self.store, budget=5)
        with patch("sistema_prospeccion.network.urlopen", side_effect=replies) as opener, patch("sistema_prospeccion.network.time.sleep"):
            text, _ = transport.fetch("https://uno.example/")
            self.assertIn("FICTICIO", text)
            with self.assertRaisesRegex(FetchError, "excluye"):
                transport.fetch("https://uno.example/privado")
            self.assertEqual(opener.call_count, 2)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM responses").fetchone()[0], 2)
        self.assertEqual(transport.used, 2)

    def test_http_404_robots_allows_page_without_real_network(self):
        from urllib.error import HTTPError
        from email.message import Message
        class Response:
            status, headers = 200, Message()
            def __enter__(inner): return inner
            def __exit__(inner, *args): pass
            def read(inner, n): return b"FICTICIO"
            def geturl(inner): return "https://uno.example/"
        transport = HTTP(self.store, budget=2)
        with patch("sistema_prospeccion.network.urlopen", side_effect=[HTTPError("https://uno.example/robots.txt", 404, "missing", None, None), Response()]), patch("sistema_prospeccion.network.time.sleep"):
            self.assertEqual(transport.fetch("https://uno.example/")[0], "FICTICIO")

    def test_actual_http_budget_counts_robots(self):
        from email.message import Message
        class Response:
            status, headers = 200, Message()
            def __enter__(inner): return inner
            def __exit__(inner, *args): pass
            def read(inner, n): return b"User-agent: *\nAllow: /"
            def geturl(inner): return "https://uno.example/robots.txt"
        transport = HTTP(self.store, budget=1)
        with patch("sistema_prospeccion.network.urlopen", return_value=Response()) as opener:
            with self.assertRaises(BudgetReached):
                transport.fetch("https://uno.example/")
            self.assertEqual(opener.call_count, 1)


class ParsingTests(unittest.TestCase):
    def test_normalization_and_multiple_emails(self):
        self.assertEqual(norm("CÁDIZ, S.L."), "cadiz s l")
        self.assertEqual(phone("0034 954 000 000"), "+34954000000")
        self.assertEqual(emails("A@uno.example; b@dos.example"), ["a@uno.example", "b@dos.example"])

    def test_semicolon_csv_and_mapping(self):
        rows, _ = parse_records('nombre;correo;telefono\nFICTICIO;info@uno.example;954000000\n', {"kind": "csv"}, "file:///ficticio.csv")
        self.assertEqual(rows[0][1]["name"], "FICTICIO")
        self.assertIn("#row=2", rows[0][0])

    def test_json_nested_mapping(self):
        rows, _ = parse_records('{"data":[{"n":"FICTICIO","contact":{"mail":"info@uno.example"}}]}', {"kind": "json", "records_path": "data", "fields": {"name": "n", "email": "contact.mail"}}, "https://fuente.example/")
        self.assertEqual(rows[0][1]["email"], "info@uno.example")

    def test_selectors_attribute_descendant(self):
        root = DOM('<div class="biz"><a class="site" href="/uno">FICTICIO</a></div>').root
        self.assertEqual(len(select(root, '.biz a.site[href]')), 1)

    def test_visible_emails_not_scripts_and_same_host_links(self):
        text = '<body><script>const email="tracker@otro.example";</script><p>info@uno.example</p><a href="mailto:dos@uno.example?subject=Hola">Email</a><a href="/contacto">Contacto</a><a href="https://otro.example/contact">Otro</a></body>'
        found, links = contact_page(text, "https://uno.example/")
        self.assertEqual(found, ["dos@uno.example", "info@uno.example"])
        self.assertEqual(links, ["https://uno.example/contacto"])

    def test_jsonld(self):
        text = '<script type="application/ld+json">{"@type":"LocalBusiness","name":"FICTICIO","email":"info@uno.example","address":{"addressLocality":"Sevilla","addressCountry":"ES"}}</script>'
        rows, _ = parse_records(text, {"kind": "jsonld"}, "https://uno.example/")
        self.assertEqual(rows[0][1]["city"], "Sevilla")


if __name__ == "__main__":
    unittest.main()
