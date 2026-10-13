import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sistema_prospeccion.campaigns import campaign_prepare
from sistema_prospeccion.route_tools import maps_prepare, maps_import, execute_route
from sistema_prospeccion.engine import Store


class ToolRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / 'run'
        campaign_prepare('Negocios ficticios', self.run, True, self.root / 'registry')

    def tearDown(self):
        self.temp.cleanup()

    def test_native_maps_plan_has_email_resume_and_network_is_explicit(self):
        with patch('sistema_prospeccion.route_tools.shutil.which', return_value='docker.exe'):
            result = maps_prepare(self.run)
        plan = json.loads(Path(result['plan']).read_text('utf-8'))
        self.assertIn('-email', plan['argv'])
        self.assertIn('-resume', plan['argv'])
        self.assertEqual(52, result['queries'])
        self.assertTrue(plan['container'].startswith('gmaps-sector-'))
        with self.assertRaises(ValueError):
            execute_route(result['plan'], network=True)

    def test_maps_import_accepts_over_1000_rows_and_preserves_recipient_source(self):
        file = self.root / 'maps.csv'
        with file.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=['title','address','emails','website','link'])
            writer.writeheader()
            for index in range(1005):
                writer.writerow({'title': f'Negocio {index}', 'address': f'Calle {index}, Madrid',
                                 'emails': f'contacto{index}@example.org', 'website': f'https://example.org/{index}',
                                 'link': f'https://maps.google.com/?cid={index}'})
        result = maps_import(self.run, file, 'Categoría y territorio de la muestra ficticia', 'CSV ficticio, correos de web declarada')
        self.assertEqual(1005, result['rows'])
        with Store(self.run) as store:
            selection = store.selected_items()
            self.assertEqual(1000, len(selection['main']))
            self.assertEqual(5, len(selection['reserve']))
            self.assertTrue(all(i['email_source'].startswith('https://example.org/') for i in selection['main']))


if __name__ == '__main__':
    unittest.main()
