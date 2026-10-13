"""CLI del sistema. `python -m sistema_prospeccion ...`."""
import argparse
import json
import sys
from pathlib import Path

from .engine import Store, new_run, json_text, atomic_json, digest, WORKSPACE, RUNTIME, __version__
from .network import work
from .parsing import emails, parse_records, web_url
from .reporting import compact_status, balanced


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prospección sectorial: datos locales, sin llamadas a modelos ni envíos.")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser('routes-status', help='Comprobar runtimes de Maps/Scrapling sin iniciar captura')
    for route_name in ('maps-prepare', 'scrapling-prepare', 'maps-import'):
        route = sub.add_parser(route_name)
        route.add_argument('--run', required=True)
        if route_name == 'maps-prepare':
            route.add_argument('--queries')
            route.add_argument('--depth', type=int, default=1)
        elif route_name == 'scrapling-prepare':
            route.add_argument('--url', required=True)
            route.add_argument('--output', required=True)
            route.add_argument('--mode', choices=['get','fetch','stealthy-fetch'], default='get')
        else:
            route.add_argument('--file', required=True)
            route.add_argument('--scope-basis', required=True)
            route.add_argument('--publication-basis', required=True)
    for route_name in ('route-execute', 'maps-status'):
        route = sub.add_parser(route_name)
        route.add_argument('--plan', required=True)
        if route_name == 'route-execute':
            route.add_argument('--network', action='store_true')
    recipe = sub.add_parser('recipe-register', help='Guardar receta revisada para reutilizarla por sector')
    recipe.add_argument('--file', required=True)
    recipe.add_argument('--registry-root')
    prepare = sub.add_parser('campaign-prepare', help='Recuperar catálogo técnico y abrir/reanudar una tirada')
    prepare.add_argument('--sector', required=True)
    prepare.add_argument('--run')
    prepare.add_argument('--fixture', action='store_true')
    prepare.add_argument('--registry-root')
    prepare.add_argument('--target', type=int, default=1000)
    for command in ('campaign-run', 'campaign-status', 'campaign-export', 'campaign-crm-request', 'campaign-crm-apply', 'campaign-brief-save'):
        campaign = sub.add_parser(command)
        campaign.add_argument('--run', required=True)
        if command == 'campaign-run':
            campaign.add_argument('--network', action='store_true')
            campaign.add_argument('--budget', type=int, default=1000)
        if command == 'campaign-export':
            campaign.add_argument('--csv-only', action='store_true')
        if command == 'campaign-crm-request':
            campaign.add_argument('--operation', choices=['auto','check','reserve','export','members','renew','release'], default='auto')
            campaign.add_argument('--project-id')
        if command == 'campaign-crm-apply':
            campaign.add_argument('--file', required=True)
        if command == 'campaign-brief-save':
            campaign.add_argument('--registry-root')
    doctor = sub.add_parser("doctor", help="Comprobar rutas y dependencias sin red ni inicializar búsqueda")
    doctor.add_argument("--adapter", choices=["html_labels", "pdf_columns"])
    extract = sub.add_parser("adapter-extract", help="Congelar extracción local reproducible, sin red")
    extract.add_argument("--adapter", required=True, choices=["html_labels", "pdf_columns"])
    extract.add_argument("--input", required=True)
    extract.add_argument("--config", required=True)
    extract.add_argument("--output", required=True)
    extract.add_argument("--source-url", default="")
    verify = sub.add_parser('adapter-verify',help='Comprobar huellas y reproducción sin modificar archivos')
    verify.add_argument('--manifest',required=True)
    profiles=sub.add_parser('profiles',help='Perfiles reutilizables disponibles, sin red')
    discover=sub.add_parser('profile-discover',help='Obtener URLs de fichas y paginación de listados guardados')
    discover.add_argument('--profile',required=True)
    discover.add_argument('--file',required=True,help='JSON con lista de path y url de listados')
    discover.add_argument('--limit',type=int,default=50)
    discover.add_argument('--offset',type=int,default=0,help='Continuar un listado congelado sin perder fichas por el límite del lote')
    discover.add_argument('--scope')
    discover.add_argument('--output',required=True)
    batch=sub.add_parser('profile-extract',help='Extraer lote local y congelar originales/configuración/manifiesto')
    batch.add_argument('--profile',required=True)
    batch.add_argument('--file',required=True,help='JSON con lista de path y url de fichas/PDF')
    batch.add_argument('--config',help='JSON de parámetros que sustituyen los del perfil')
    batch.add_argument('--output',required=True)
    batch_verify=sub.add_parser('profile-verify',help='Comprobar huellas y reproducción del lote congelado')
    batch_verify.add_argument('--manifest',required=True)
    init = sub.add_parser("init", help="Crear ejecución vacía sin buscar empresas")
    init.add_argument("--sector", required=True)
    init.add_argument("--run")
    init.add_argument("--target", type=int, default=1000)
    init.add_argument("--fixture", action="store_true", help="Solo datos ficticios; marca todas las salidas")
    listing = sub.add_parser("list-runs")
    listing.add_argument("--sector")
    for name in ("status", "configure", "source-add", "source-check", "source-retire", "source-info", "sources", "sample", "website-candidates", "review-candidates", "contact-evidence", "review-file", "contact-import", "queue-captures", "capture-results", "import-file", "queue-source", "queue-websites", "work", "export", "brief-template", "brief-review", "decide", "merge", "retry-failed", "refresh-jobs", "audit"):
        p = sub.add_parser(name)
        p.add_argument("--run", required=True)
        if name == "source-add":
            p.add_argument("--spec", required=True)
        if name == "configure":
            p.add_argument("--settings", required=True)
        if name in {"import-file", "queue-source", "source-check", "source-retire", "source-info"}:
            p.add_argument("--source", required=True)
        if name in {"import-file", "source-check"}:
            p.add_argument("--file")
        if name == "source-retire":
            p.add_argument("--reason", required=True)
            p.add_argument("--replacement")
        if name in {"review-file", "contact-import", "queue-captures"}:
            p.add_argument("--file", required=True)
        if name=='capture-results':
            p.add_argument('--output',required=True,help='Lista de archivos/URLs para profile-extract; no incluye tareas pendientes/fallidas')
            p.add_argument('--host',help='Filtrar capturas de un dominio de fuente')
        if name == 'queue-websites':
            p.add_argument('--include-with-email', action='store_true', help='Enriquecer también negocios con email; conserva alternativas y procedencia')
            p.add_argument('--limit', type=int)
        if name in {"sample", "website-candidates", "review-candidates", "sources"}:
            p.add_argument("--limit", type=int, default=10 if name != "website-candidates" else 50)
        if name == "sample":
            p.add_argument("--population", choices=["main", "pending", "others", "all"], default="main")
        if name in {"status", "export"}:
            p.add_argument("--full", action="store_true", help="Incluir métricas técnicas y fuentes completas")
        if name == "work":
            p.add_argument("--network", action="store_true", help="Ejecutar peticiones reales; no usar durante construcción sin piloto autorizado")
            p.add_argument("--budget", type=int)
            p.add_argument("--kind", choices=["directory", "contact", "capture"])
            p.add_argument("--continue-after-target", action="store_true", help="Actualizar tareas aunque ya se haya alcanzado el objetivo")
        if name == "export":
            p.add_argument("--csv-only", action="store_true")
        if name == "brief-review":
            p.add_argument("--reviewer", required=True)
        if name == "decide":
            p.add_argument("--company", required=True)
            p.add_argument("--decision", required=True, help="Ruta a JSON con motivo y campos revisados")
        if name == 'contact-evidence':
            p.add_argument('--company',required=True)
        if name == "merge":
            p.add_argument("--keep", required=True)
            p.add_argument("--other", required=True)
            p.add_argument("--reason", required=True)
        if name == "retry-failed":
            p.add_argument("--reason", required=True)
        if name == "refresh-jobs":
            p.add_argument("--older-than-days", type=int, default=30)
            p.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in {'routes-status','maps-prepare','scrapling-prepare','maps-import','route-execute','maps-status'}:
            from . import route_tools
            if args.command == 'routes-status':
                result = route_tools.runtime_status()
            elif args.command == 'maps-prepare':
                result = route_tools.maps_prepare(args.run, args.queries, args.depth)
            elif args.command == 'scrapling-prepare':
                result = route_tools.scrapling_prepare(args.run, args.url, args.output, args.mode)
            elif args.command == 'maps-import':
                result = route_tools.maps_import(args.run, args.file, args.scope_basis, args.publication_basis)
            elif args.command == 'route-execute':
                result = route_tools.execute_route(args.plan, args.network)
            else:
                result = route_tools.maps_status(args.plan)
        elif args.command == 'recipe-register':
            from .campaigns import register_recipe
            result = register_recipe(args.file, args.registry_root)
        elif args.command.startswith('campaign-'):
            from .campaigns import campaign_prepare, campaign_run, campaign_status, campaign_export, campaign_brief_save
            from .crm_bridge import crm_requests, crm_apply
            if args.command == 'campaign-prepare':
                result = campaign_prepare(args.sector, args.run, args.fixture, args.registry_root, args.target)
            elif args.command == 'campaign-run':
                result = campaign_run(args.run, args.network, args.budget)
            elif args.command == 'campaign-status':
                result = campaign_status(args.run)
            elif args.command == 'campaign-export':
                result = campaign_export(args.run, not args.csv_only)
            elif args.command == 'campaign-crm-request':
                result = crm_requests(args.run, args.operation, args.project_id)
            elif args.command == 'campaign-crm-apply':
                result = crm_apply(args.run, args.file)
            elif args.command == 'campaign-brief-save':
                result = campaign_brief_save(args.run, args.registry_root)
        elif args.command=='profiles':
            from .profiles import list_profiles
            result={'profiles':list_profiles(),'network_requests':0}
        elif args.command=='profile-discover':
            from .profiles import discover_fichas
            data=discover_fichas(args.profile,json.loads(Path(args.file).read_text('utf-8-sig')),scope=args.scope,limit=args.limit,offset=args.offset)
            path=Path(args.output)
            if path.exists() and json.loads(path.read_text('utf-8-sig'))!=data:
                raise ValueError('Descubrimiento previo distinto: usa otra salida')
            if not path.exists():atomic_json(path,data)
            result={'output':str(path.resolve()),'fichas':len(data['fichas']),'next_urls':data['next_urls'],'next_offset':data['next_offset'],'network_requests':0}
        elif args.command=='profile-extract':
            from .profile_io import freeze_batch
            result=freeze_batch(args.profile,json.loads(Path(args.file).read_text('utf-8-sig')),args.output,
                                json.loads(Path(args.config).read_text('utf-8-sig')) if args.config else None)
        elif args.command=='profile-verify':
            from .profile_io import verify_batch
            result=verify_batch(args.manifest)
            print(json_text(result))
            return 0 if result['valid'] else 2
        elif args.command == "doctor":
            import importlib.util
            result = {"python": sys.executable, "engine_version": __version__, "guide_exists": (WORKSPACE / "SISTEMA-PROSPECCION-SECTORIAL.md").exists(),
                      "excel_exporter_exists": (WORKSPACE / 'sistema_prospeccion/excel_export.py').exists(),
                      "openpyxl_available": importlib.util.find_spec('openpyxl') is not None,
                      "dependencies": 'Python standard-library core; optional PDF adapter: pdfplumber; optional XLSX export: openpyxl.',
                      "network_requests": 0}
            if args.adapter:
                from .adapters import dependency_status
                result['adapter'] = dependency_status(args.adapter)
                if not result['adapter']['available']:
                    raise RuntimeError("Dependencias del adaptador no disponibles: " + json_text(result['adapter']))
        elif args.command == 'adapter-verify':
            from .adapters import extract_records
            path=Path(args.manifest)
            meta=json.loads(path.read_text(encoding='utf-8-sig'))
            output=Path(str(path).removesuffix('.manifest.json'))
            original,config=Path(meta['input_snapshot']),Path(meta['config_snapshot'])
            checks={'input':digest(original.read_bytes())==meta['input_sha256'],
                    'config':digest(config.read_bytes())==meta['config_sha256'],
                    'output':digest(output.read_bytes())==meta['output_sha256']}
            checks['replay']=extract_records(original,meta['adapter'],config,meta['source_url'])==json.loads(output.read_text(encoding='utf-8-sig'))
            result={'valid':all(checks.values()),'checks':checks,'network_requests':0,'files_modified':False}
            print(json_text(result))
            return 0 if result['valid'] else 2
        elif args.command == "adapter-extract":
            from .adapters import dependency_status, extract_records
            status = dependency_status(args.adapter)
            if not status['available']:
                raise RuntimeError("Faltan dependencias: " + json_text(status))
            if args.source_url and not web_url(args.source_url):
                raise ValueError("source-url requiere HTTP(S)")
            input_path, config_path, output_path = Path(args.input), Path(args.config), Path(args.output)
            records = extract_records(input_path, args.adapter, config_path, args.source_url)
            if not records or any(not r.get('name') for r in records):
                raise ValueError("Extracción vacía o con filas sin nombre; revisar configuración antes de importar")
            encoded = (json_text(records)+'\n').encode('utf-8')
            if output_path.exists() and output_path.read_bytes() != encoded:
                raise ValueError("Salida distinta ya existente: usa otra ruta para conservar la versión anterior")
            manifest_path = output_path.with_suffix(output_path.suffix+'.manifest.json')
            raw_path = output_path.parent/'originales-adaptador'/(digest(input_path.read_bytes())+input_path.suffix)
            saved_config = output_path.parent/'originales-adaptador'/(digest(config_path.read_bytes())+'.config.json')
            manifest = {'adapter': args.adapter, 'input': str(input_path.resolve()), 'input_sha256': digest(input_path.read_bytes()),
                        'config': str(config_path.resolve()), 'config_sha256': digest(config_path.read_bytes()),
                        'input_snapshot':str(raw_path.resolve()), 'config_snapshot':str(saved_config.resolve()),
                        'source_url': args.source_url, 'output_sha256': digest(encoded), 'rows': len(records),
                        'rows_with_email': sum(bool(emails(r.get('email',''))) for r in records),
                        'network_requests': 0, 'meaning': 'Extracción de un original local; no acredita revisión actual, sector ni publicación empresarial.'}
            if manifest_path.exists() and json.loads(manifest_path.read_text('utf-8')) != manifest:
                raise ValueError("Manifiesto previo distinto; usa otra ruta de salida")
            if not output_path.exists():
                atomic_json(output_path, records)
            raw_path.parent.mkdir(parents=True,exist_ok=True)
            for path,body in ((raw_path,input_path.read_bytes()),(saved_config,config_path.read_bytes())):
                if path.exists() and path.read_bytes()!=body:
                    raise ValueError('Snapshot existente alterado; no se sobrescribe: '+str(path))
                if not path.exists():
                    path.write_bytes(body)
            atomic_json(manifest_path, manifest)
            result = {**manifest, 'output':str(output_path.resolve()),'manifest':str(manifest_path.resolve())}
        elif args.command == "init":
            result = {"run": str(new_run(args.sector, args.run, args.target, args.fixture)), "network_requests": 0}
        elif args.command == "list-runs":
            root = WORKSPACE / "work/prospeccion"
            result = []
            for path in sorted(root.glob("*/*/config.json")) if root.exists() else []:
                config = json.loads(path.read_text(encoding="utf-8"))
                if not args.sector or config["sector"].casefold() == args.sector.casefold():
                    result.append({"run": str(path.parent), "sector": config["sector"], "fixture": config["fixture_mode"], "created_at": config["created_at"]})
        else:
            with Store(args.run) as store:
                if args.command == "status":
                    result = store.write_progress()
                    if not args.full:
                        result = compact_status(result)
                elif args.command == "configure":
                    result = store.configure(json.loads(Path(args.settings).read_text(encoding="utf-8-sig")))
                elif args.command == "source-add":
                    result = {"source": store.add_source(json.loads(Path(args.spec).read_text(encoding="utf-8-sig")))}
                elif args.command == "source-info":
                    result = store.source(args.source)
                elif args.command == "source-retire":
                    result = store.retire_source(args.source,args.reason,args.replacement)
                    store.write_progress()
                elif args.command == "source-check":
                    spec=store.active_source(args.source)
                    path=Path(args.file or spec.get('path',''))
                    body=path.read_text(encoding=spec.get('encoding','utf-8-sig'))
                    records,_=parse_records(body,spec,spec.get('url') or path.resolve().as_uri())
                    named=sum(bool(r.get('name')) for _,r in records)
                    if not records or named != len(records):
                        raise ValueError("Conector no reproducible: filas vacías o sin nombre. Corrige adaptador/configuración antes de importar")
                    result={'source_id':args.source,'rows':len(records),'rows_with_name':named,
                            'rows_with_email':sum(bool(emails(r.get('email',''))) for _,r in records),
                            'original_sha256':digest(path.read_bytes()),'network_requests':0}
                elif args.command in {"sample","website-candidates","review-candidates","sources"}:
                    if not 1<=args.limit<=250:
                        raise ValueError("limit debe estar entre 1 y 250")
                    items=store.materialize()
                    if args.command=='sources':
                        report=store.summary(items)['source_yield']
                        result={'sources':report[:args.limit],'total_sources':len(report)}
                    else:
                        specs={r['id']:json.loads(r['spec']) for r in store.db.execute('SELECT id,spec FROM sources')}
                        if args.command=='website-candidates':
                            chosen=[i for i in items if not i['website'] and i['website_status']=='no_investigado' and not i['decision'].get('exclude')]
                            fields=('company_id','name','city','province','address','phone','website_status')
                        elif args.command=='review-candidates':
                            chosen=[i for i in items if not i['problems'] and i['email'] and not i['decision'].get('commercial_reviewed_at')]
                            fields=('company_id','name','city','province','website','contact_url','email','website_status','commercial_signal','priority')
                        else:
                            chosen=[i for i in items if args.population=='all' or (args.population=='main' and not i['problems'] and i['email']) or (args.population=='pending' and i['problems']) or (args.population=='others' and not i['problems'] and not i['email'])]
                            if args.population=='main':
                                chosen=balanced(chosen,specs)[:store.config['target']]
                            fields=('company_id','name','city','province','email','email_source','website','website_status','commercial_signal','priority','priority_reason','reason')
                        result={'total_matching':len(chosen),'rows':[{k:i[k] for k in fields} for i in balanced(chosen,specs)[:args.limit]]}
                        if args.command=='website-candidates':
                            from .contacts import website_hints
                            for row,item in zip(result['rows'],balanced(chosen,specs)[:args.limit]): row['website_hints']=website_hints(item)
                elif args.command=='contact-evidence':
                    result=store.contact_evidence(args.company)
                elif args.command == "review-file":
                    result=store.decide_many(json.loads(Path(args.file).read_text(encoding='utf-8-sig')))
                    store.write_progress()
                elif args.command == 'contact-import':
                    from .contacts import import_contact_files
                    result=import_contact_files(store,json.loads(Path(args.file).read_text(encoding='utf-8-sig')))
                elif args.command=='queue-captures':
                    result=store.queue_captures(json.loads(Path(args.file).read_text('utf-8-sig')))
                elif args.command=='capture-results':
                    rows=store.capture_results()
                    if args.host:
                        from .parsing import host
                        rows=[row for row in rows if host(row['url'])==host(args.host)]
                    atomic_json(args.output,rows)
                    result={'output':str(Path(args.output).resolve()),'files':len(rows),'network_requests':0,
                            'jobs':dict(store.db.execute("SELECT state,count(*) FROM jobs WHERE kind='capture' GROUP BY state"))}
                elif args.command == "import-file":
                    result = store.ingest_file(args.source, args.file)
                    store.write_progress()
                elif args.command == "queue-source":
                    result = {"queued": store.queue_source(args.source)}
                elif args.command == "queue-websites":
                    result = {"queued": store.queue_websites(args.include_with_email, args.limit)}
                elif args.command == "work":
                    if not args.network:
                        raise ValueError("work requiere --network; no se ha realizado ninguna petición")
                    if store.config["fixture_mode"]:
                        raise ValueError("Una ejecución ficticia no permite red real")
                    if args.budget is not None and args.budget < 1:
                        raise ValueError("El presupuesto debe ser positivo")
                    result = work(store, args.budget, args.kind, continue_after_target=args.continue_after_target)
                elif args.command == "export":
                    result = store.export(not args.csv_only)
                    if not args.full:
                        result['summary']=compact_status(result['summary'])
                elif args.command == "brief-template":
                    result = {"brief": store.brief_template()}
                elif args.command == "brief-review":
                    result = store.review_brief(args.reviewer)
                elif args.command == "decide":
                    store.decide(args.company, json.loads(Path(args.decision).read_text(encoding="utf-8-sig")))
                    result = store.write_progress()
                elif args.command == "merge":
                    store.merge(args.keep, args.other, args.reason)
                    result = store.write_progress()
                elif args.command == "retry-failed":
                    changed = store.db.execute("UPDATE jobs SET state='queued',attempts=0,updated_at=datetime('now') WHERE state='failed' AND (source_id IS NULL OR source_id NOT IN (SELECT source_id FROM retired_sources))").rowcount
                    store.db.commit()
                    store.event("retry_failed", {"count": changed, "reason": args.reason})
                    result = {"requeued": changed}
                elif args.command == "refresh-jobs":
                    from datetime import datetime, timedelta, timezone
                    if args.older_than_days < 0:
                        raise ValueError("older-than-days no puede ser negativo")
                    cutoff = (datetime.now(timezone.utc)-timedelta(days=args.older_than_days)).isoformat(timespec="seconds")
                    changed = store.db.execute("UPDATE jobs SET state='queued',attempts=0 WHERE state='done' AND updated_at<? AND (source_id IS NULL OR source_id NOT IN (SELECT source_id FROM retired_sources))", (cutoff,)).rowcount
                    store.db.commit()
                    store.event("refresh_jobs", {"count": changed, "reason": args.reason})
                    result = {"requeued": changed}
                elif args.command == "audit":
                    result = {"sqlite_integrity": store.db.execute("PRAGMA integrity_check").fetchone()[0],
                              "foreign_key_errors": [list(r) for r in store.db.execute("PRAGMA foreign_key_check")],
                              "events": [dict(r) for r in store.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT 10")],
                              "failed_jobs": [dict(r) for r in store.db.execute("SELECT id,kind,state,error FROM jobs WHERE state='failed' LIMIT 20")]}
        print(json_text(result))
        return 0
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        print(json_text({"error": str(exc), "command": args.command}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
