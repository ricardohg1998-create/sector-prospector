"""Contratos del modo de selección por volumen. Fixtures locales; sin red ni CRM."""
import csv
from pathlib import Path
import tempfile
import unittest

from sistema_prospeccion.engine import EXPORT_FIELDS, Store, new_run


class VolumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = new_run("Servicios de prueba", self.root / "run", fixture=True, target=1000)
        self.store = Store(self.run)
        self.store.add_source({
            "id": "fixture", "kind": "csv", "path": str(self.root / "unused.csv"),
            "scope": {"verified": True, "sector": "Servicios de prueba", "country": "ES", "basis": "fixture local"},
            "publication_verified": True, "publication_basis": "publicación ficticia del fixture",
        })

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def add(self, locator, *, name=None, email=None, relationship="", **fields):
        index = int(locator.removeprefix("row")) if locator.startswith("row") else len(self.store.materialize()) + 1
        return self.store.ingest("fixture", locator, {
            "name": name or f"Empresa de prueba {index}", "city": fields.pop("city", f"Ciudad {index}"),
            "address": fields.pop("address", f"Calle Prueba {index}"), "country": "ES",
            "email": email or f"contacto{index}@example.test", "relationship": relationship, **fields,
        })

    def test_volume_accepts_unknown_autonomy_but_keeps_explicit_exclusion(self):
        self.add("row1", relationship="cadena")
        item = self.store.materialize()[0]
        self.assertEqual(item["problems"], [])
        self.store.decide(item["company_id"], {"exclude": True, "reason": "Exclusión expresa de prueba"})
        self.assertEqual(self.store.selected_items()["pending"][0]["company_id"], item["company_id"])
        self.assertEqual(self.store.summary()["main_export_rows"], 0)

    def test_1005_unique_primary_emails_yield_1000_main_and_5_reserve(self):
        with self.store.db:
            for index in range(1005):
                self.add(f"row{index}", name=f"Empresa de prueba {index}", email=f"contacto{index}@example.test")
        selected = self.store.selected_items()
        self.assertEqual((len(selected["main"]), len(selected["reserve"])), (1000, 5))
        result = self.store.export(excel=False)
        self.assertEqual((result["summary"]["main_export_rows"], result["summary"]["reserve"]), (1000, 5))
        self.assertEqual(result["summary"]["brief"]["status"], "not_required")
        self.assertEqual(result["summary"]["status"], "complete")

    def test_repeated_primary_email_does_not_fill_target_and_stays_distinct(self):
        self.add("row1", name="Empresa Uno", email="contacto@shared.test")
        self.add("row2", name="Empresa Dos", email="contacto@shared.test")
        self.add("row3", name="Empresa Tres", email="unico@shared.test")
        selected = self.store.selected_items()
        self.assertEqual(len(selected["main"]), 2)
        self.assertEqual({item["email"] for item in selected["main"]}, {"contacto@shared.test", "unico@shared.test"})
        self.assertEqual(len(selected["shared"]), 1)
        representative = next(item for item in selected["main"] if item["email"] == "contacto@shared.test")
        self.assertNotEqual(representative["company_id"], selected["shared"][0]["company_id"])
        self.assertEqual(self.store.summary()["unique_companies"], 3)
        self.assertEqual(len(self.store.selected_items(include_ids=[representative["company_id"]])["main"]), 1)
        with self.assertRaisesRegex(ValueError, "representantes"):
            self.store.selected_items(include_ids=[selected["shared"][0]["company_id"]])

    def test_used_ids_and_emails_exclude_without_mutating_decisions_and_match_export(self):
        self.add("row1", name="Empresa Uno", email="uno@example.test")
        self.add("row2", name="Empresa Dos", email="dos@example.test")
        items = self.store.materialize()
        exclude_ids = [items[0]["company_id"]]
        exclude_emails = [items[1]["email"].upper()]
        selection = self.store.selected_items(exclude_ids=exclude_ids, exclude_emails=exclude_emails)
        self.assertEqual((len(selection["main"]), len(selection["excluded_used"])), (0, 2))
        self.assertFalse(any(item["decision"].get("exclude") for item in self.store.materialize()))
        summary = self.store.summary(exclude_ids=exclude_ids, exclude_emails=exclude_emails)
        exported = self.store.export(excel=False, exclude_ids=exclude_ids, exclude_emails=exclude_emails)
        self.assertEqual(summary["main_export_rows"], exported["summary"]["main_export_rows"])
        with (self.run / "entregables/empresas.csv").open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(list(csv.DictReader(handle, delimiter=";")), [])

    def test_volume_csv_contract_keeps_26_fields_and_shared_export(self):
        self.add("row1", name="Empresa Uno", email="contacto@shared.test")
        self.add("row2", name="Empresa Dos", email="contacto@shared.test")
        self.store.export(excel=False)
        with (self.run / "entregables/empresas.csv").open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter=";")
            self.assertEqual(tuple(reader.fieldnames), EXPORT_FIELDS)
            self.assertEqual(len(list(reader)), 1)
        with (self.run / "entregables/emails-compartidos.csv").open(encoding="utf-8-sig", newline="") as handle:
            self.assertEqual(len(list(csv.DictReader(handle, delimiter=";"))), 1)

    def test_include_ids_exports_only_requested_order_to_tiradas_directory(self):
        self.add("row1", name="Empresa Uno", email="uno@example.test")
        self.add("row2", name="Empresa Dos", email="dos@example.test")
        self.add("row3", name="Empresa Tres", email="tres@example.test")
        items = self.store.materialize()
        requested = [items[2]["company_id"], items[0]["company_id"]]
        output = self.run / "tiradas" / "tanda-01" / "entregables"
        result = self.store.export(excel=False, output_dir=output, include_ids=requested)
        with (output / "empresas.csv").open(encoding="utf-8-sig", newline="") as handle:
            exported = list(csv.DictReader(handle, delimiter=";"))
        with (output / "reserva.csv").open(encoding="utf-8-sig", newline="") as handle:
            reserve = list(csv.DictReader(handle, delimiter=";"))
        self.assertEqual([row["company_id"] for row in exported], requested)
        self.assertEqual(result["summary"]["main_export_rows"], 2)
        self.assertEqual(result["summary"]["main_export_rows"], self.store.summary(include_ids=requested)["main_export_rows"])
        self.assertEqual(len(reserve), 1)

    def test_empty_include_ids_means_export_no_main_and_unavailable_ids_fail(self):
        self.add("row1", name="Empresa Uno", email="uno@example.test")
        result = self.store.export(excel=False, include_ids=[])
        self.assertEqual(result["summary"]["main_export_rows"], 0)
        with self.assertRaisesRegex(ValueError, "include_ids"):
            self.store.export(excel=False, include_ids=["missing-id"])

    def test_legacy_config_without_mode_keeps_autonomy_and_brief_gate(self):
        self.store.db.close()
        config_path = self.run / "config.json"
        config = __import__("json").loads(config_path.read_text(encoding="utf-8"))
        config.pop("mode")
        config.pop("brief_required")
        config_path.write_text(__import__("json").dumps(config), encoding="utf-8")
        with Store(self.run) as legacy:
            legacy.ingest("fixture", "row1", {"name": "Cadena Uno", "city": "Sevilla", "address": "Calle 1",
                                                "country": "ES", "email": "uno@example.test", "relationship": "cadena"})
            self.assertEqual(legacy.materialize()[0]["problems"], ["autonomia_por_confirmar"])
            self.assertEqual(legacy.brief_status()["status"], "pending")

    def test_qualified_mode_alias_keeps_legacy_policy(self):
        self.store.configure({"mode": "qualified"})
        self.add("row1", relationship="cadena")
        self.assertEqual(self.store.materialize()[0]["problems"], ["autonomia_por_confirmar"])
        self.assertEqual(self.store.brief_status()["status"], "pending")


if __name__ == "__main__":
    unittest.main()
