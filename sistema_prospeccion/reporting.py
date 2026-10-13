"""Compact operational reports and transparent selection. No model calls."""
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
import json

from .parsing import norm

PRIORITY_ORDER = {"alta": 0, "media": 1, "sin_evaluar": 2, "baja": 3}


def source_role(source_id, spec):
    # Backward compatibility with pilot sources predating explicit roles.
    return spec.get("role") or ("enrichment" if source_id.startswith("web-") else "correction" if source_id.startswith("revision-") else "discovery")


def balanced(items, sources=None):
    """Rotate territories and discovery sources within each evidence priority."""
    sources = sources or {}
    output = []
    for priority in sorted(PRIORITY_ORDER, key=PRIORITY_ORDER.get):
        groups = defaultdict(lambda: defaultdict(deque))
        for item in items:
            if item.get("priority", "sin_evaluar") != priority:
                continue
            discovery = [sid for sid in item.get("_source_ids", []) if source_role(sid, sources.get(sid, {})) == "discovery"]
            sid = discovery[0] if discovery else "sin_fuente_descubrimiento"
            group = sources.get(sid, {}).get("source_group") or sid
            groups[norm(item.get("province")) or "desconocida"][group].append(item)
        territories = deque(sorted(groups))
        source_turns = {province: deque(sorted(groups[province])) for province in territories}
        while territories:
            province = territories.popleft()
            group = source_turns[province].popleft()
            output.append(groups[province][group].popleft())
            if groups[province][group]:
                source_turns[province].append(group)
            if source_turns[province]:
                territories.append(province)
    return output


def metrics(store, items, qualified):
    sources = {r['id']: json.loads(r['spec']) for r in store.db.execute('SELECT id,spec FROM sources ORDER BY created_at,rowid')}
    retired = {r[0] for r in store.db.execute('SELECT source_id FROM retired_sources')}
    qualified_ids = {i['company_id'] for i in qualified}
    item_by_id = {i['company_id']: i for i in items}
    found = set(); source_yield = []; initial_rows = 0
    discovery_owner = {}
    for sid, spec in sources.items():
        observations = list(store.db.execute('SELECT company_id,record FROM observations WHERE source_id=? AND active=1', (sid,)))
        owners = {r['company_id'] for r in observations}
        eligible = owners & qualified_ids
        role = source_role(sid, spec)
        group_name = spec.get('source_group') or ('Webs de empresas' if role=='enrichment' else 'Revisiones de datos' if role=='correction' else sid)
        business_names=sorted({item_by_id[c]['name'] for c in owners if c in item_by_id})
        source_name = spec.get('source_name') or (group_name+': '+business_names[0] if len(business_names)==1 else group_name)
        if role == 'discovery':
            initial_rows += len(observations)
            for cid in eligible:
                discovery_owner.setdefault(cid, spec.get('source_group') or sid)
        stale = None
        if spec.get('published_at'):
            published = datetime.fromisoformat(spec['published_at']).replace(tzinfo=timezone.utc) if len(spec['published_at']) == 10 else datetime.fromisoformat(spec['published_at'])
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            stale = (datetime.now(timezone.utc)-published).days > store.config['fresh_days']
        source_yield.append({
            'source_id': sid, 'source_name':source_name, 'source_group': group_name, 'role': role,
            'retired': sid in retired, 'rows': len(observations), 'companies': len(owners),
            'qualified_companies': len(eligible),
            'new_qualified_companies': len(eligible-found) if role == 'discovery' else 0,
            'overlap_companies': len(eligible & found) if role == 'discovery' else 0,
            'emails_published': sum(bool(json.loads(r['record']).get('emails')) and bool(json.loads(r['record']).get('publication_verified')) for r in observations),
            'pending_companies': sum(bool(item_by_id[c]['problems']) for c in owners if c in item_by_id),
            'provinces': sorted({item_by_id[c]['province'] for c in owners if c in item_by_id and item_by_id[c]['province']}),
            'stale': stale, 'published_at': spec.get('published_at') or None,
            'selection_notes': spec.get('selection_notes', '')})
        if role == 'discovery':
            found.update(eligible)
    provinces = Counter(i['province'] or 'desconocida' for i in qualified)
    groups = Counter(discovery_owner.values())
    def dominant(counts):
        return counts.most_common(1)[0] if counts else ('', 0)
    province, nprovince = dominant(provinces); source, nsource = dominant(groups)
    notes = []
    if qualified and nprovince/len(qualified) > .5:
        notes.append('Más de la mitad de los contactos admisibles están en una provincia; contrastar cobertura antes de ampliar.')
    if qualified and nsource/len(qualified) > .75:
        notes.append('Más de tres cuartas partes proceden de un grupo de fuentes; comparar otra vía de descubrimiento.')
    if any(s['role']=='discovery' and s['rows'] and s['published_at'] is None for s in source_yield):
        notes.append('Hay fuentes sin fecha de actualización publicada conocida; la consulta reciente no prueba vigencia de sus contactos.')
    ordered = balanced(qualified, sources)
    main = ordered[:store.config['target']]
    main_email_counts = Counter(i['email'] for i in main)
    return {
        'initial_source_rows': initial_rows, 'source_yield': source_yield,
        'discovery_source_groups':len({s['source_group'] for s in source_yield if s['role']=='discovery' and s['rows']}),
        'primary_unique_recipients': len({i['email'] for i in main}),
        'qualified_unique_recipients': len({i['email'] for i in qualified}),
        'shared_primary_emails_main': sum(main_email_counts[i['email']]>1 for i in main),
        'pending_reasons': dict(Counter(reason for i in items for reason in i['problems'])),
        'website_status_counts': dict(Counter(i['website_status'] for i in main)),
        'priority_counts': dict(Counter(i['priority'] for i in main)),
        'commercial_review_counts': {'reviewed':sum(bool(i['decision'].get('commercial_reviewed_at')) for i in main),
                                     'not_reviewed':sum(not i['decision'].get('commercial_reviewed_at') for i in main),
                                     'population':'main_export_rows'},
        'coverage': {'dominant_province': province, 'dominant_province_share': nprovince/len(qualified) if qualified else 0,
                     'dominant_source_group': source, 'dominant_source_group_share': nsource/len(qualified) if qualified else 0,
                     'notes': notes, 'population': 'qualified_with_email'},
        'selection_method': 'Prioridad con evidencia; alternancia de provincia y grupo de fuentes dentro de cada prioridad. No es representatividad estadística.'}


def compact_status(summary):
    keys = ('engine_version','sector','fixture_mode','status','target','initial_source_rows','unique_companies',
            'qualified_with_email','main_export_rows','primary_unique_recipients','others','reserve','pending','pending_reasons',
            'jobs','website_status_counts','priority_counts','commercial_review_counts','coverage','brief')
    return {k:summary[k] for k in keys if k in summary}
