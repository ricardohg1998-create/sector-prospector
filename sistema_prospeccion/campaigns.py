"""Catálogos técnicos reutilizables y tiradas; el estado comercial pertenece al CRM."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import uuid
from contextlib import contextmanager
from functools import wraps
import os

from .engine import Store, WORKSPACE, atomic_json, digest, new_run, now, slug

REGISTRY = WORKSPACE / 'work/catalogos'


@contextmanager
def campaign_lock(run):
    path = Path(run) / '.campaign.lock'
    with path.open('a+b') as handle:
        if not path.stat().st_size:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('El catálogo está ocupado por otro proceso; reintentar al terminar') from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def locked(function):
    @wraps(function)
    def execute(run, *args, **kwargs):
        with campaign_lock(run):
            return function(run, *args, **kwargs)
    return execute


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def register_recipe(file, registry=None):
    recipe = read_json(file)
    if not isinstance(recipe, dict) or not isinstance(recipe.get('sector'), str) or not recipe['sector'].strip():
        raise ValueError('La receta necesita sector')
    if not isinstance(recipe.get('sources'), list) or not recipe['sources']:
        raise ValueError('La receta necesita sources no vacías')
    from .source_routes import validate_source
    for source in recipe['sources']:
        checked = validate_source(source)
        if not checked['valid']:
            raise ValueError('Fuente inválida: ' + '; '.join(checked['errors']))
    destination = Path(registry or REGISTRY) / slug(recipe['sector']) / 'receta.json'
    if destination.exists() and read_json(destination) != recipe:
        shutil.copy2(destination, destination.with_name('receta.before-' + uuid.uuid4().hex[:8] + '.json'))
    atomic_json(destination, recipe)
    return {'recipe': str(destination.resolve()), 'sector': recipe['sector'], 'sources': len(recipe['sources'])}


def recipe_for(sector, registry=None):
    root = Path(registry or REGISTRY)
    path = root / slug(sector) / 'receta.json'
    if path.exists():
        return read_json(path), str(path.resolve())
    for candidate in root.glob('*/receta.json'):
        recipe = read_json(candidate)
        if slug(sector) in {slug(s) for s in [recipe['sector'], *recipe.get('aliases', [])]}:
            return recipe, str(candidate.resolve())
    try:
        from .source_routes import load_recipe
        recipe = load_recipe(sector)
        return recipe, None
    except (ValueError, KeyError, FileNotFoundError, ImportError):
        return None, None


def campaign_prepare(sector, run=None, fixture=False, registry=None, target=1000):
    recipe, recipe_path = recipe_for(sector, registry)
    canonical = recipe['sector'] if recipe else sector.strip()
    root = Path(registry or REGISTRY)
    # Fixtures never become the default live catalogue for a sector.
    if fixture and registry is None:
        root = Path(run).parent / 'registry-fixture' if run else WORKSPACE / 'work/fixtures-campaigns'
    entry = root / slug(canonical) / 'catalogo.json'
    saved = read_json(entry) if entry.exists() else {}
    destination = Path(run).resolve() if run else Path(saved['run']) if saved.get('run') else None
    if destination and (destination / 'config.json').exists():
        config = read_json(destination / 'config.json')
        if config['sector'] != canonical or config['fixture_mode'] != fixture:
            raise ValueError('Sector/modo ficticio distinto del catálogo solicitado')
        if config.get('mode') != 'volume':
            raise ValueError('Catálogo anterior: conservar original y adoptar una copia con mode=volume')
    else:
        destination = new_run(canonical, destination, target=target, fixture=fixture)
    destination = destination.resolve()
    state_path = destination / 'campaign.json'
    state = read_json(state_path) if state_path.exists() else {}
    if not state or state.get('closed'):
        batch_key = uuid.uuid4().hex
        project_id = state.get('project_id')
        state = {'schema_version': 1, 'id': batch_key, 'sector': canonical,
                 'run': str(destination), 'created_at': now(), 'target': target,
                 'fixture_mode': fixture, 'recipe': recipe, 'recipe_path': recipe_path,
                 'crm_batch_id': None, 'project_id': project_id, 'checks': {}, 'members': {},
                 'requests': {}, 'receipts': {}, 'closed': False, 'exported': False}
        research = root / slug(canonical) / 'investigacion'
        if (research / 'origen.json').exists():
            origin = read_json(research / 'origen.json')
            cached = research / 'brief-sector.md'
            evidence = research / 'evidencias.json'
            if not cached.exists() or digest(cached.read_bytes()) != origin['brief_sha256'] or not evidence.exists() or digest(evidence.read_bytes()) != origin.get('evidence_sha256'):
                raise ValueError('La investigación guardada cambió; revisar antes de reutilizar')
            state['research'] = {'status': 'reused', 'path': str(cached), 'saved_at': origin['saved_at']}
        else:
            state['research'] = {'status': 'pending', 'meaning': 'Preparar una investigación común al abrir el sector'}
        with Store(destination) as store:
            if state['research']['status'] == 'pending':
                store.configure({'brief_required': True})
        atomic_json(state_path, state)
    elif recipe:
        state.update(recipe=recipe, recipe_path=recipe_path)
        atomic_json(state_path, state)
    atomic_json(entry, {'sector': canonical, 'run': str(destination), 'fixture_mode': fixture,
                        'updated_at': now(), 'recipe_path': recipe_path})
    return campaign_status(destination)


def load_state(run):
    path = Path(run) / 'campaign.json'
    if not path.exists():
        raise ValueError('Ejecuta campaign-prepare antes de operar una tirada')
    state = read_json(path)
    if Path(state['run']).resolve() != Path(run).resolve():
        raise ValueError('La tirada pertenece a otro catálogo')
    return state


def save_state(run, state):
    atomic_json(Path(run) / 'campaign.json', state)
    atomic_json(Path(run) / 'tiradas' / state['id'] / 'estado.json', state)


def research_status(state):
    research = dict(state.get('research', {'status': 'pending'}))
    if research.get('status') in {'reviewed', 'reused'}:
        path = Path(research.get('path', ''))
        origin_path = path.parent / 'origen.json'
        evidence = path.parent / 'evidencias.json'
        origin = read_json(origin_path) if origin_path.is_file() else {}
        if not path.is_file() or digest(path.read_bytes()) != origin.get('brief_sha256') or not evidence.is_file() or digest(evidence.read_bytes()) != origin.get('evidence_sha256'):
            research.update(status='invalid', reason='La investigación común cambió o está incompleta')
    return research


def campaign_status(run):
    state = load_state(run)
    with Store(run) as store:
        selected = store.selected_items()
        stock = selected['main'] + selected['reserve']
        unknown = sum(i['company_id'] not in state['checks'] and i['company_id'] not in state['members'] for i in stock)
        allowed = sum(state['checks'].get(i['company_id'], {}).get('eligible') is True and state['members'].get(i['company_id'], {}).get('status') != 'excluded' for i in stock)
        counts = {}
        for member in state['members'].values():
            counts[member['status']] = counts.get(member['status'], 0) + 1
        jobs = dict(store.db.execute('SELECT state,count(*) FROM jobs GROUP BY state'))
        brief = store.brief_status()
    if state['closed']:
        next_action = 'tirada_entregada; campaign-prepare abre la siguiente'
    elif not state.get('recipe') and not stock:
        next_action = 'necesita_fuentes; especialista prepara y registra receta'
    elif counts.get('reserved'):
        next_action = 'campaign-crm-request --operation export'
    elif counts.get('available'):
        next_action = 'campaign-crm-request --operation reserve'
    elif counts.get('exported', 0) >= state['target']:
        next_action = 'campaign-export'
    elif allowed >= state['target']:
        next_action = 'campaign-crm-request --operation reserve'
    elif unknown:
        next_action = 'campaign-crm-request --operation check'
    elif allowed:
        next_action = 'campaign-crm-request --operation reserve'
    elif counts.get('exported'):
        next_action = 'campaign-run para completar o campaign-export parcial si fuentes agotadas'
    else:
        next_action = 'campaign-run o ampliar receta; sin contactos nuevos comprobados'
    return {'run': str(Path(run).resolve()), 'batch': state['id'], 'sector': state['sector'],
            'target': state['target'], 'fixture_mode': state['fixture_mode'],
            'stock_unique_recipients': len(stock), 'crm_unchecked': unknown,
            'crm_excluded': sum(c.get('eligible') is False for c in state['checks'].values()),
            'crm_eligible_checked': allowed, 'crm_members': counts,
            'jobs': jobs, 'recipe_available': bool(state.get('recipe')), 'brief': brief,
            'sector_research': research_status(state),
            'next_action': next_action}


def import_route_records(store, state, source, records):
    if not records:
        return
    # Normal results and crash recovery must generate the same frozen source.
    records = sorted(records, key=lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False))
    content_hash = digest(json.dumps(records, sort_keys=True, ensure_ascii=False))
    source_id = ('route-' + source['id'][:35] + '-' + content_hash[:16])[:71]
    path = Path(store.run) / 'fuentes' / (source_id + '.json')
    if not path.exists():
        atomic_json(path, records)
    scope = dict(source.get('scope', {}))
    if scope.get('verified') and scope.get('sector') != state['sector']:
        raise ValueError('La receta declara un sector diferente de la tirada')
    spec = {'id': source_id, 'kind': 'json', 'path': str(path.resolve()),
            'scope': scope, 'publication_verified': source.get('publication_verified', False),
            'publication_basis': source.get('publication_basis', ''),
            'source_group': source.get('source_group', source['id']), 'role': source.get('role', 'discovery'),
            'defaults': source.get('defaults', {})}
    store.add_source(spec)
    already = store.db.execute('SELECT 1 FROM observations WHERE source_id=? LIMIT 1', (source_id,)).fetchone()
    if not already:
        store.ingest_file(source_id)


@locked
def campaign_run(run, network=False, budget=1000):
    if type(budget) is not int or budget < 1:
        raise ValueError('budget debe ser entero positivo')
    state = load_state(run)
    if state['closed']:
        raise ValueError('La tirada ya está cerrada; prepara la siguiente')
    if network and state['fixture_mode']:
        raise ValueError('Una ejecución ficticia no permite red real')
    recipe = state.get('recipe')
    if not recipe:
        return {**campaign_status(run), 'stop_reason': 'necesita_receta', 'network_requests': 0}
    current = campaign_status(run)
    if current['crm_unchecked'] >= state['target'] or current['crm_eligible_checked'] >= state['target']:
        return {**current, 'stop_reason': 'stock_suficiente_consultar_CRM', 'network_requests': 0}
    from .source_routes import run_source, pending_source_batches, acknowledge_source_batch
    results = []
    remaining = budget
    with Store(run) as store:
        for batch in pending_source_batches(store):
            source = batch.get('source_spec') or next((s for s in recipe['sources'] if s['id'] == batch['source_id']), None)
            if source is None:
                raise ValueError('El lote pendiente no conserva su configuración original; no se descarta')
            import_route_records(store, state, source, batch['records'])
            acknowledge_source_batch(store, batch['batch_id'])
            results.append({'source_id': source['id'], 'status': 'recovered', 'count': len(batch['records']), 'requests': 0})
        for source in recipe['sources']:
            if remaining <= 0:
                break
            selected = store.selected_items()
            stock = selected['main'] + selected['reserve']
            unknown = sum(i['company_id'] not in state['checks'] and i['company_id'] not in state['members'] for i in stock)
            allowed = sum(state['checks'].get(i['company_id'], {}).get('eligible') is True and state['members'].get(i['company_id'], {}).get('status') != 'excluded' for i in stock)
            if unknown >= state['target'] or allowed >= state['target']:
                break
            result = run_source(store, source, min(1000, remaining), network=network)
            used = result.get('requests', result.get('network_requests', 0))
            if type(used) is not int or used < 0 or used > remaining:
                raise ValueError('La ruta devolvió un presupuesto incoherente')
            remaining -= used
            import_route_records(store, state, source, result.get('records'))
            if result.get('batch_id'):
                acknowledge_source_batch(store, result['batch_id'])
            # Keep payloads on disk; routine output contains counts, never all rows.
            compact = {k: v for k, v in result.items() if k not in {'records', 'discoveries', 'discovered_urls', 'excluded', 'pending', 'captures', 'jobs', 'resumable_jobs', 'errors'}}
            errors = result.get('errors', [])
            compact['error_count'] = len(errors)
            compact['error_sample'] = [{k: str(v)[:500] for k, v in error.items()} for error in errors[:3]]
            results.append(compact)
            selection = store.selected_items()
            available = selection['main'] + selection['reserve']
            unchecked = sum(i['company_id'] not in state['checks'] and i['company_id'] not in state['members'] for i in available)
            checked = sum(state['checks'].get(i['company_id'], {}).get('eligible') is True and state['members'].get(i['company_id'], {}).get('status') != 'excluded' for i in available)
            if unchecked >= state['target'] or checked >= state['target']:
                break
        store.write_progress()
    return {**campaign_status(run), 'sources_processed': len(results),
            'network_requests': budget - remaining, 'budget': budget,
            'stop_reason': 'budget' if not remaining else 'lote_terminado', 'results': results}


def campaign_brief_save(run, registry=None):
    state = load_state(run)
    with Store(run) as store:
        status = store.brief_status()
        if not status.get('reviewed'):
            raise ValueError('Solo se reutiliza investigación revisada con evidencia')
    root = Path(registry or REGISTRY) / slug(state['sector']) / 'investigacion'
    root.mkdir(parents=True, exist_ok=True)
    for name, source in [('brief-sector.md', Path(run) / 'entregables/brief-sector.md'),
                         ('evidencias.json', Path(run) / 'investigacion/evidencias.json')]:
        shutil.copy2(source, root / name)
    atomic_json(root / 'origen.json', {'source_run': str(Path(run).resolve()), 'saved_at': now(),
                                      'brief_sha256': digest((root / 'brief-sector.md').read_bytes()),
                                      'evidence_sha256': digest((root / 'evidencias.json').read_bytes())})
    state['research'] = {'status': 'reviewed', 'path': str(root / 'brief-sector.md'), 'saved_at': now()}
    save_state(run, state)
    return {'research': str(root), 'reviewed': True}


@locked
def campaign_export(run, excel=True):
    state = load_state(run)
    ids = [cid for cid, m in state['members'].items() if m['status'] == 'exported']
    if not ids:
        raise ValueError('Sin destinatarios confirmados como exportados por CRM; no se genera una tirada utilizable')
    if any(member['status'] == 'reserved' for member in state['members'].values()):
        raise ValueError('Quedan reservas pendientes: completar la exportación CRM o liberarlas antes de cerrar una entrega parcial')
    output = Path(run) / 'tiradas' / state['id'] / 'entregables'
    reused = bool(state.get('closed') and (output / 'tirada.json').exists())
    result = {}
    if not reused:
        with Store(run) as store:
            by_id = {i['company_id']: i for i in store.materialize()}
            if any(cid not in by_id or by_id[cid]['email'] != state['members'][cid].get('email') for cid in ids):
                raise ValueError('Los datos locales cambiaron respecto de la exportación CRM; conservar recibos y revisar')
            excluded = [cid for cid, check in state['checks'].items() if check.get('eligible') is False and cid not in ids]
            excluded += [cid for cid, member in state['members'].items() if member['status'] == 'excluded' and cid not in ids]
            result = store.export(excel=excel, include_ids=ids, exclude_ids=excluded, output_dir=output)
    research = research_status(state)
    research_path = research.get('path')
    research_ready = research.get('status') in {'reviewed', 'reused'}
    if research_ready:
        shutil.copy2(research_path, output / 'brief-sector.md')
        shutil.copy2(Path(research_path).parent / 'evidencias.json', output / 'evidencias-sector.json')
        atomic_json(output / 'investigacion-origen.json', research)
    state.update(exported=True, closed=True, delivered_at=now(), output=str(output.resolve()))
    save_state(run, state)
    contacts_status = 'complete' if len(ids) >= state['target'] else 'partial'
    delivery_status = 'complete' if contacts_status == 'complete' and research_ready else 'partial'
    atomic_json(output / 'tirada.json', {'batch': state['id'], 'crm_batch_id': state['crm_batch_id'],
                                       'rows': len(ids), 'target': state['target'],
                                       'status': contacts_status, 'delivery_status': delivery_status,
                                       'research_status': research.get('status', 'pending'),
                                       'fixture_mode': state['fixture_mode'], 'sent': False})
    return {'output': str(output.resolve()), 'rows': len(ids), 'target': state['target'],
            'status': contacts_status, 'delivery_status': delivery_status,
            'research_status': research.get('status', 'pending'),
            'excel': result.get('excel'), 'reused': reused, 'sent': False}
