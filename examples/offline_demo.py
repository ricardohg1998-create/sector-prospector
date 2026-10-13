"""Run the real core against synthetic businesses; no network or CRM calls."""
import argparse
import json
from pathlib import Path
import uuid

from sistema_prospeccion.engine import Store, new_run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--xlsx', action='store_true', help='Requires openpyxl')
    args = parser.parse_args()
    output = Path('work') / ('demo-' + uuid.uuid4().hex[:8])
    run = new_run('Example sector', output, fixture=True, target=3, mode='volume')
    with Store(run) as store:
        store.add_source({'id': 'synthetic', 'kind': 'json', 'path': 'unused.json',
                          'scope': {'country': 'ES', 'sector': 'Example sector', 'verified': True,
                                    'basis': 'Synthetic fixture, not a real source verification'},
                          'publication_verified': True, 'publication_basis': 'Synthetic fixture only'})
        rows = [
            ('Demo Alpha', 'Sevilla', 'shared@example.invalid'),
            ('Demo Beta', 'Madrid', 'shared@example.invalid'),
            ('Demo Gamma', 'Cádiz', 'gamma@example.invalid'),
            ('Demo Delta', 'Sevilla', ''),
        ]
        for index, (name, city, email) in enumerate(rows):
            record = {'name': name, 'city': city, 'address': f'Fictional street {index}',
                      'email': email, 'country': 'ES', 'contact_url': f'https://directory.example.invalid/{index}'}
            store.ingest('synthetic', f'row-{index}', record)
            if index == 0:
                assert store.ingest('synthetic', f'row-{index}', record) == 'unchanged'
        assert store.summary()['unique_companies'] == 4
        result = store.export(excel=args.xlsx)
    print(json.dumps({'meaning': 'Synthetic demonstration; no real businesses, network calls or outreach',
                      **result}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
