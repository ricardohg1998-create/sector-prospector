"""Freeze profile batches with actual byte hashes and immutable inputs."""
import json
from pathlib import Path
from .engine import atomic_json, digest
from .profiles import extract_batch, get_profile


def canonical_bytes(value):
    return (json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode('utf-8')


def freeze_batch(profile, items, output, override=None):
    result=extract_batch(profile,items,config_override=override)
    output=Path(output).resolve()
    frozen=output.parent/'originales-perfil'
    config=canonical_bytes(result['config_snapshot'])
    config_path=frozen/(digest(config)+'.config.json')
    profile_raw=Path(get_profile(profile)['config_path']).read_bytes()
    profile_path=frozen/(digest(profile_raw)+'.profile.json')
    files={output:canonical_bytes(result['records']),config_path:config,profile_path:profile_raw}
    inputs=[]
    for entry in result['manifest']['items']:
        path=Path(entry['input_path']); raw=path.read_bytes()
        dest=frozen/(digest(raw)+path.suffix)
        files[dest]=raw
        inputs.append({'path':str(dest),'url':entry['source_url'],'sha256':digest(raw)})
    manifest={'format_version':1,'profile_id':profile,'extraction':result['manifest'],
              'config_snapshot':str(config_path),'profile_snapshot':str(profile_path),
              'profile_snapshot_sha256':digest(profile_raw),'inputs':inputs,'output':str(output),
              'output_sha256':digest(files[output]),'rows':len(result['records']), 'network_requests':0}
    manifest_path=output.with_suffix(output.suffix+'.profile-manifest.json')
    files[manifest_path]=(json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf-8')
    for path,raw in files.items():
        if path.exists() and path.read_bytes()!=raw:
            raise ValueError('Salida o snapshot distinto ya existente; conserva versión y usa otra ruta: '+str(path))
    for path,raw in files.items():
        if not path.exists():
            path.parent.mkdir(parents=True,exist_ok=True)
            tmp=path.with_name(path.name+'.tmp');tmp.write_bytes(raw);tmp.replace(path)
    return {'output':str(output),'manifest':str(manifest_path),'rows':len(result['records']),
            'files':len(items),'network_requests':0}


def verify_batch(manifest_path):
    """Replay using frozen config, not the potentially updated installed profile."""
    from .adapters import extract_records
    from .parsing import DOM,select
    meta=json.loads(Path(manifest_path).read_text('utf-8-sig'))
    cfgpath=Path(meta['config_snapshot']);cfg=json.loads(cfgpath.read_text('utf-8-sig'))
    checks={'config':digest(cfgpath.read_bytes())==meta['extraction']['config_sha256'],
            'profile':digest(Path(meta['profile_snapshot']).read_bytes())==meta['profile_snapshot_sha256'],
            'output':digest(Path(meta['output']).read_bytes())==meta['output_sha256'], 'inputs':True}
    records=[]
    for entry in meta['inputs']:
        path=Path(entry['path']);checks['inputs'] &= digest(path.read_bytes())==entry['sha256']
        rows=extract_records(path,meta['extraction']['adapter'],cfg,entry['url'])
        if cfg.get('_profile_name_selector'):
            try:text=path.read_text('utf-8')
            except UnicodeDecodeError:text=path.read_text('cp1252')
            dom=DOM(text)
            names=[' '.join(n.text().split()) for n in select(dom.root,cfg['_profile_name_selector'])]
            for row in rows:
                if not row.get('name'):row['name']=next((n for n in names if n),'')
        records.extend(rows)
    checks['replay']=records==json.loads(Path(meta['output']).read_text('utf-8-sig'))
    return {'valid':all(checks.values()),'checks':checks,'rows':len(records),'network_requests':0}
