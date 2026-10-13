"""Puente por lotes al MCP existente. No crea claves ni hace llamadas a modelos."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .engine import Store, atomic_json, digest, now
from .parsing import norm, host
from .campaigns import load_state, save_state, read_json, locked

SOURCE_SYSTEM = 'ricardo-sector-prospector'
DEFAULT_PROJECT_ID = os.environ.get('PROSPECTOR_CRM_PROJECT_ID', '')
MAX_BYTES = 180_000


def business_key(item):
    # A public recipient/domain is not an identity. Keep the name and location.
    anchor = norm(item.get('address')) or norm(item.get('phone')) or host(item.get('website'))
    identity = [norm(item['name']), norm(item.get('city')), norm(item.get('province')), anchor, 'ES']
    return 'es-' + digest(json.dumps(identity, ensure_ascii=False))


def candidate(item):
    source = item.get('email_source', '')
    if not source.startswith(('https://', 'http://')):
        raise ValueError('El email requiere URL HTTP(S) de procedencia para el CRM')
    return {'key': item['company_id'], 'businessKey': business_key(item),
            'email': item['email'].strip().lower(), 'website': item.get('website', ''),
            'name': item['name'][:240], 'company': item['name'][:240],
            'location': ', '.join(v for v in [item.get('city'), item.get('province')] if v)[:240],
            'sourceUrl': source[:1500], 'observedOn': item.get('observed_at', '')[:10]}


def chunked(entries, limit):
    chunk = []
    for entry in entries:
        proposed = [*chunk, entry]
        if chunk and (len(proposed) > limit or len(json.dumps(proposed, ensure_ascii=False).encode()) > MAX_BYTES):
            yield chunk
            chunk = []
        if len(json.dumps(entry, ensure_ascii=False).encode()) > MAX_BYTES:
            raise ValueError('Entrada demasiado grande para el CRM')
        chunk.append(entry)
    if chunk:
        yield chunk


def _request(state, operation, tool, args, keys):
    revision = state.get('members_generation', 0) if operation == 'members' else 0
    rid = 'sector_' + digest(json.dumps([state['id'], operation, revision, args], sort_keys=True, ensure_ascii=False))[:48]
    if tool not in {'crm_bulk_check', 'crm_bulk_members'}:
        args = {**args, 'requestId': rid}
    request = {'request_id': rid, 'operation': operation, 'tool': tool, 'arguments': args, 'keys': keys}
    old = state['requests'].get(rid)
    if old and old != request:
        raise ValueError('La petición estable cambió')
    state['requests'][rid] = request
    return request


@locked
def crm_requests(run, operation='auto', project_id=None):
    state = load_state(run)
    if operation not in {'auto', 'check', 'reserve', 'export', 'members', 'renew', 'release'}:
        raise ValueError('Operación CRM no admitida')
    if project_id:
        if state.get('project_id') and state['project_id'] != project_id:
            raise ValueError('La tirada ya pertenece a otro proyecto')
        state['project_id'] = project_id
    with Store(run) as store:
        selection = store.selected_items()
        pool = selection['main'] + selection['reserve']
    unchecked = [candidate(i) for i in pool if i['company_id'] not in state['checks'] and i['company_id'] not in state['members']]
    if operation == 'auto':
        active = sum(m['status'] in {'available', 'reserved', 'exported'} for m in state['members'].values())
        checked_new = sum(i['company_id'] not in state['members'] and state['checks'].get(i['company_id'], {}).get('eligible') is True for i in pool)
        operation = 'check' if unchecked and active + checked_new < state['target'] else 'reserve'
    requests = []
    if operation == 'check':
        for entries in chunked(unchecked[:max(200, state['target'] * 2)], 200):
            matches = [{k: e[k] for k in ('key', 'businessKey', 'email', 'website')} for e in entries]
            requests.append(_request(state, 'check', 'crm_bulk_check', {'sourceSystem': SOURCE_SYSTEM, 'entries': matches}, [e['key'] for e in entries]))
    elif not state.get('crm_batch_id'):
        if not state.get('project_id'):
            if not DEFAULT_PROJECT_ID:
                raise ValueError('Specify --project-id or PROSPECTOR_CRM_PROJECT_ID for your own CRM project')
            state['project_id'] = DEFAULT_PROJECT_ID
        args = {'input': {'projectId': state['project_id'], 'name': f"{state['sector']} — {state['id'][:8]}",
                          'focus': 'Contactos sectoriales; exportación no equivale a envío'}}
        requests.append(_request(state, 'create', 'crm_batches_create', args, []))
    elif operation == 'members':
        state['members_generation'] = state.get('members_generation', 0) + 1
        args = {'batchId': state['crm_batch_id'], 'limit': 200}
        if state.get('members_cursor'):
            args['cursor'] = state['members_cursor']
        requests.append(_request(state, 'members', 'crm_bulk_members', args, []))
    elif operation == 'reserve':
        active = sum(m['status'] in {'available', 'reserved', 'exported'} for m in state['members'].values())
        remaining = max(0, state['target'] - active)
        eligible = [candidate(i) for i in pool if i['company_id'] not in state['members'] and state['checks'].get(i['company_id'], {}).get('eligible') is True][:remaining]
        if eligible:
            for entries in chunked(eligible, 100):
                args = {'input': {'batchId': state['crm_batch_id'], 'sourceSystem': SOURCE_SYSTEM,
                                  'runId': state['id'], 'sector': state['sector'], 'entries': entries}}
                requests.append(_request(state, 'import', 'crm_bulk_import', args, [e['key'] for e in entries]))
        else:
            for ids in chunked([cid for cid, m in state['members'].items() if m['status'] == 'available'], 100):
                entries = [{'memberId': state['members'][cid]['memberId'], 'expectedVersion': state['members'][cid]['version']} for cid in ids]
                args = {'input': {'batchId': state['crm_batch_id'], 'action': 'reserve', 'leaseMinutes': 1440, 'entries': entries}}
                requests.append(_request(state, 'reserve', 'crm_bulk_transition', args, ids))
    else:
        ids = [cid for cid, m in state['members'].items() if m['status'] == 'reserved']
        for group in chunked(ids, 100):
            entries = []
            for cid in group:
                member = state['members'][cid]
                if operation in {'export', 'renew'} and datetime.fromisoformat(member['reservedUntil'].replace('Z', '+00:00')) <= datetime.now(timezone.utc):
                    raise ValueError('Reserva local caducada; consultar members y volver a reservar')
                entries.append({'memberId': member['memberId'], 'expectedVersion': member['version'], 'reservationToken': member.get('reservationToken', '')})
            args = {'input': {'batchId': state['crm_batch_id'], 'action': operation, 'entries': entries}}
            if operation == 'renew':
                args['input']['leaseMinutes'] = 1440
            requests.append(_request(state, operation, 'crm_bulk_transition', args, group))
    # Pending requests are immutable and re-used after a timeout, not regenerated.
    requests = [r for r in requests if r['request_id'] not in state['receipts']]
    save_state(run, state)
    output = Path(run) / 'tiradas' / state['id'] / 'crm-peticiones.json'
    atomic_json(output, {'schema_version': 1, 'batch': state['id'], 'fixture_mode': state['fixture_mode'], 'requests': requests})
    request_files = []
    for request in requests:
        path = output.parent / 'peticiones' / (request['request_id'] + '.json')
        atomic_json(path, request)
        request_files.append({'file': str(path.resolve()), 'tool': request['tool'], 'request_id': request['request_id']})
    return {'file': str(output.resolve()), 'requests': len(requests), 'operation': operation,
            'batch': state['id'], 'fixture_mode': state['fixture_mode'], 'request_files': request_files,
            'meaning': 'Ejecutar mediante el MCP existente; no contiene credenciales ni confirma efectos'}


def unwrap(result):
    if isinstance(result, dict) and result.get('isError'):
        raise ValueError('CRM rechazó la petición; conservar recibos y resolver el tramo')
    if isinstance(result, dict) and 'content' in result:
        for block in result['content']:
            if block.get('type') == 'text':
                return json.loads(block['text'])
    return result


@locked
def crm_apply(run, file):
    state = load_state(run)
    response = read_json(file)
    if response.get('batch') != state['id'] or response.get('fixture_mode', False) != state['fixture_mode']:
        raise ValueError('Respuesta de otra tanda o modo ficticio')
    responses = response.get('responses')
    if not isinstance(responses, list) or not responses:
        raise ValueError('Faltan respuestas CRM')
    # Validate the entire receipt file before replacing any local state.
    seen = set()
    for envelope in responses:
        rid = envelope.get('request_id')
        if rid in seen or rid not in state['requests']:
            raise ValueError('Recibo duplicado o petición desconocida')
        seen.add(rid)
        result = unwrap(envelope.get('result'))
        request = state['requests'][rid]
        if not isinstance(result, dict):
            raise ValueError('Respuesta CRM no es objeto')
        receipt_hash = digest(json.dumps(result, sort_keys=True, ensure_ascii=False))
        if rid in state['receipts']:
            if state['receipts'][rid] != receipt_hash:
                raise ValueError('El recibo reintentado cambió')
            continue
        op = request['operation']
        if op == 'create':
            batch_id = result.get('id') or result.get('batch', {}).get('id')
            if not isinstance(batch_id, str) or not batch_id:
                raise ValueError('El CRM no devolvió ID de tanda')
            state['crm_batch_id'] = batch_id
        else:
            if op not in {'check', 'members'} and result.get('batchId') != state['crm_batch_id']:
                raise ValueError('El CRM respondió con otra tanda')
            if op not in {'check', 'import', 'members'} and result.get('action') != op:
                raise ValueError('El CRM respondió con otra transición')
            rows = result.get('records')
            if not isinstance(rows, list):
                raise ValueError('El CRM no devolvió records')
            if op == 'members':
                for row in rows:
                    if row.get('batchId') != state['crm_batch_id']:
                        raise ValueError('El CRM respondió con miembros de otra tanda')
                    key = row.get('entryKey')
                    if key in state['members']:
                        state['members'][key].update(row)
                        if row.get('effectiveStatus') == 'expired':
                            state['members'][key]['status'] = 'available'
                state['members_cursor'] = result.get('nextCursor')
            else:
                field = 'key' if op in {'check', 'import'} else 'memberId'
                indexed = {row.get(field): row for row in rows}
                expected = request['keys'] if field == 'key' else [state['members'][cid]['memberId'] for cid in request['keys']]
                if len(indexed) != len(rows) or set(indexed) != set(expected):
                    raise ValueError('Respuesta CRM incompleta o con entradas ajenas')
                for cid, identifier in zip(request['keys'], expected):
                    row = indexed[identifier]
                    if op == 'check':
                        if type(row.get('eligible')) is not bool or not isinstance(row.get('reasons'), list):
                            raise ValueError('Coincidencia CRM inválida')
                        state['checks'][cid] = {**row, 'checked_at': result.get('checkedAt', now())}
                    else:
                        required = {'import': {'available', 'excluded'}, 'reserve': {'reserved'},
                                    'renew': {'reserved'}, 'release': {'available'}, 'export': {'exported'}}[op]
                        if row.get('status') not in required or type(row.get('version')) is not int or not row.get('memberId'):
                            raise ValueError('Estado/versión CRM incoherentes')
                        if row['status'] == 'reserved' and not all(row.get(k) for k in ('reservationToken', 'reservedUntil')):
                            raise ValueError('Reserva sin token o vencimiento')
                        email = next((e['email'] for e in request['arguments'].get('input', {}).get('entries', []) if e.get('key') == cid), None)
                        state['members'][cid] = {**state['members'].get(cid, {}), **({'email': email} if email else {}), **row}
        state['receipts'][rid] = receipt_hash
    save_state(run, state)
    receipt_path = Path(run) / 'tiradas' / state['id'] / 'recibos' / (digest(Path(file).read_bytes()) + '.json')
    if not receipt_path.exists():
        atomic_json(receipt_path, response)
    return {'applied': len(responses), 'batch': state['id'], 'sent': False}
