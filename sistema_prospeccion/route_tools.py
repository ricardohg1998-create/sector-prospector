"""Entradas ejecutables para herramientas opcionales, sin scripts ad hoc por sector."""
import csv
import json
from pathlib import Path
import subprocess
import shutil

from .campaigns import load_state
from .engine import atomic_json, digest, Store
from .parsing import parse_records, web_url
from . import source_routes as routes

MAPS_SKILL = Path.home() / '.agents/skills/google-maps-scraper'
AREAS = ('A Coruña|Albacete|Alicante|Almería|Araba|Asturias|Ávila|Badajoz|Barcelona|Bizkaia|Burgos|Cáceres|Cádiz|Cantabria|Castellón|Ciudad Real|Córdoba|Cuenca|Gipuzkoa|Girona|Granada|Guadalajara|Huelva|Huesca|Illes Balears|Jaén|La Rioja|Las Palmas|León|Lleida|Lugo|Madrid|Málaga|Melilla|Murcia|Navarra|Ourense|Palencia|Pontevedra|Salamanca|Santa Cruz de Tenerife|Segovia|Sevilla|Soria|Tarragona|Teruel|Toledo|Valencia|Valladolid|Zamora|Zaragoza|Ceuta').split('|')


def runtime_status():
    import inspect
    parameters = inspect.signature(routes.runtime_status).parameters
    key = 'skill_dir' if 'skill_dir' in parameters else 'maps_skill_dir'
    return routes.runtime_status(**{key: MAPS_SKILL})


def maps_prepare(run, queries=None, depth=1):
    state = load_state(run)
    root = Path(run) / 'tiradas' / state['id'] / 'maps'
    root.mkdir(parents=True, exist_ok=True)
    file = Path(queries) if queries else root / 'queries.txt'
    if not queries:
        file.write_text('\n'.join(f"{state['sector']} en {area}, España" for area in AREAS) + '\n', encoding='utf-8')
    if not file.is_file() or not file.read_text('utf-8').strip():
        raise ValueError('Se necesita archivo de consultas no vacío')
    plan = routes.maps_email_command(file.resolve(), root.resolve(), skill_dir=MAPS_SKILL, depth=depth)
    # The installed Bash helper has CRLF and WSL Docker is unavailable here.
    # Use the same scraper flags through the verified native Docker CLI.
    docker = shutil.which('docker') or shutil.which('docker.exe')
    if docker:
        if type(depth) is not int or not 1 <= depth <= 10:
            raise ValueError('depth debe estar entre 1 y 10')
        container = 'gmaps-sector-' + state['id'][:16]
        plan.update(available=True, transport='docker-native', container=container,
                    argv=[docker, 'run', '--pull=never', '--detach', '--name', container,
                          '--volume', str(file.resolve()) + ':/queries.txt:ro',
                          '--volume', str(root.resolve()) + ':/output',
                          'gosom/google-maps-scraper', '-input', '/queries.txt',
                          '-results', '/output/results.csv', '-email', '-depth', str(depth),
                          '-c', '2', '-lang', 'es', '-resume', '-exit-on-inactivity', '3m'])
    plan.update(kind='maps', run=str(Path(run).resolve()), fixture_mode=state['fixture_mode'],
                result_file=str(root / 'results.csv'), queries_sha256=digest(file.read_bytes()))
    path = root / 'plan.json'
    atomic_json(path, plan)
    return {'plan': str(path.resolve()), 'available': plan['available'], 'queries': len(file.read_text('utf-8').splitlines()), 'email_extraction': True, 'started': False}


def scrapling_prepare(run, url, output, mode='get'):
    state = load_state(run)
    plan = routes.scrapling_cli_command(url, output, mode=mode)
    plan.update(kind='scrapling', run=str(Path(run).resolve()), fixture_mode=state['fixture_mode'])
    path = Path(run) / 'tiradas' / state['id'] / 'scrapling-plan.json'
    atomic_json(path, plan)
    return {'plan': str(path.resolve()), 'available': plan['available'], 'started': False}


def execute_route(file, network=False):
    plan = json.loads(Path(file).read_text('utf-8'))
    if not network or plan.get('fixture_mode'):
        raise ValueError('Ejecutar una ruta requiere --network y campaña real')
    if not plan.get('available'):
        raise ValueError('Runtime no disponible; consultar routes-status')
    if plan.get('kind') == 'scrapling':
        return routes.run_scrapling_cli(plan)
    if plan.get('kind') != 'maps':
        raise ValueError('Ruta no admitida')
    if plan.get('transport') == 'docker-native':
        check = subprocess.run([plan['argv'][0], 'inspect', '--format', '{{.State.Status}}', plan['container']], capture_output=True, text=True, timeout=20)
        if not check.returncode:
            if check.stdout.strip() == 'running':
                return {'started': True, 'already_running': True, 'result_file': plan['result_file']}
            started = subprocess.run([plan['argv'][0], 'start', plan['container']], capture_output=True, text=True, timeout=30)
            if started.returncode:
                raise RuntimeError('No se pudo reanudar el contenedor existente')
            return {'started': True, 'resumed': True, 'result_file': plan['result_file']}
    result = subprocess.run(plan['argv'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
    if result.returncode:
        raise RuntimeError('Maps no inició: ' + (result.stderr or result.stdout)[-500:])
    return {'started': True, 'result_file': plan['result_file'], 'sent': False,
            'meaning': 'Contenedor en segundo plano; consultar maps-status y conservar resultados parciales'}


def maps_status(file):
    plan = json.loads(Path(file).read_text('utf-8'))
    command = ['docker', 'inspect', '--format', '{{json .State}}', plan.get('container', 'gmaps-scraper-agent')]
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
    state = json.loads(result.stdout) if not result.returncode else {'Status': 'not_found'}
    path = Path(plan['result_file'])
    count = 0
    if path.exists():
        with path.open(encoding='utf-8-sig', newline='') as handle:
            count = sum(bool(row.get('title') or row.get('name')) for row in csv.DictReader(handle))
    return {'state': state.get('Status'), 'exit_code': state.get('ExitCode'), 'rows': count,
            'output': str(path), 'complete': state.get('Status') == 'exited' and state.get('ExitCode') == 0}


def maps_import(run, file, scope_basis, publication_basis):
    if not scope_basis.strip() or not publication_basis.strip():
        raise ValueError('La muestra de la fuente requiere bases de alcance y publicación revisadas')
    state = load_state(run)
    raw = Path(file).read_bytes()
    parsed, _ = parse_records(raw.decode('utf-8-sig'), {'kind': 'maps_csv', 'fields': {'contact_url': 'link'}}, Path(file).resolve().as_uri())
    rows = [row for _, row in parsed]
    for row in rows:
        row['email_source'] = web_url(row.get('website')) or web_url(row.get('contact_url'))
        if row.get('email') and not row['email_source']:
            # Keep the discovered business, but not an unattributable recipient.
            row['email'] = ''
    source_id = 'maps-' + digest(raw)[:20]
    path = Path(run) / 'fuentes' / (source_id + '.json')
    atomic_json(path, rows)
    with Store(run) as store:
        store.snapshot(raw, Path(file).resolve().as_uri(), extension='csv')
        store.add_source({'id': source_id, 'kind': 'json', 'path': str(path.resolve()),
                          'source_group': 'google-maps', 'scope': {'sector': state['sector'], 'country': 'ES', 'verified': True, 'basis': scope_basis},
                          'publication_verified': True, 'publication_basis': publication_basis})
        result = store.ingest_file(source_id)
    return {'source': source_id, 'rows': len(rows), 'result': result, 'network_requests': 0}
