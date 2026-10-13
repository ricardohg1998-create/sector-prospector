import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sistema_prospeccion.adapters import dependency_status, extract_records


ROOT = Path(__file__).resolve().parents[2]


class HtmlLabelAdapterTests(unittest.TestCase):
    def extract(self, body):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ficha.html"
            path.write_text(body, encoding="utf-8")
            return extract_records(path, "html_labels", ROOT / "sistema_prospeccion" / "adapters" / "asetra_labels.json",
                                   "https://asetramadrid.com/ficha/")

    def test_uses_ficha_container_and_swaps_inverted_city_postal_code(self):
        rows = self.extract("""<header><div>Email cabecera@example.org</div></header>
            <main class='entry-content'><h1>Ficha</h1><div>Nombre comercial Taller Uno</div>
            <div>Población 28913</div><div>Código postal LEGANÉS</div><div>Email contacto@example.es</div>
            <div>Móvil 600123456</div></main>""")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "Taller Uno")
        self.assertEqual(row["city"], "LEGANÉS")
        self.assertEqual(row["postal_code"], "28913")
        self.assertEqual(row["email"], "contacto@example.es")
        self.assertEqual(row["phone"], "600123456")
        self.assertEqual(row["email_source"], "https://asetramadrid.com/ficha/")

    def test_long_name_and_missing_email_are_preserved_as_empty(self):
        name = "TALLER DE REPARACIONES Y NEUMÁTICOS DEL CORREDOR DEL HENARES SL"
        rows = self.extract(f"<div class='entry-content'><div>Nombre comercial {name}</div><div>Email</div></div>")
        self.assertEqual(rows[0]["name"], name)
        self.assertEqual(rows[0]["email"], "")
        self.assertNotIn("email_source", rows[0])

    def test_bad_configuration_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "x.html"
            path.write_text("<div></div>", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "labels"):
                extract_records(path, "html_labels", {"container_selector": ".x"})



class PdfAdapterTests(unittest.TestCase):
    def test_dependency_status_is_explicit(self):
        result = dependency_status("pdf_columns")
        self.assertIn("available", result)
        self.assertEqual(result["adapter"], "pdf_columns")
        if not result["available"]:
            self.assertIn("pdfplumber", result["missing"])



if __name__ == "__main__":
    unittest.main()
