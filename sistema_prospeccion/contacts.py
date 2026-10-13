"""Contact evidence shared by queued HTTP and already downloaded official pages."""
from .parsing import contact_page, host, web_url

FREEMAIL = {'gmail.com', 'hotmail.com', 'hotmail.es', 'outlook.com', 'outlook.es',
            'yahoo.com', 'yahoo.es', 'icloud.com', 'live.com', 'msn.com', 'proton.me', 'protonmail.com'}


def website_hints(item):
    """Domains are only leads for identity checking; never assign official websites."""
    domains = sorted({e.rsplit('@',1)[-1] for e in item.get('all_published_emails',[])
                      if e.rsplit('@',1)[-1] not in FREEMAIL})
    return [{'url':'https://'+domain+'/', 'basis':'Dominio de email publicado; confirmar identidad',
             'confirmed':False} for domain in domains[:3]]


def prepare_contact(item, text, url):
    if not item or item['decision'].get('exclude'):
        raise ValueError('Empresa activa inexistente o excluida')
    root = web_url(item.get('website', ''))
    url = web_url(url)
    if not root or not url or host(url) != host(root):
        raise ValueError('Confirmar primero la web del negocio; la página debe pertenecer a su dominio')
    found, _ = contact_page(text, url)
    domain = host(root)
    review = [address for address in found if address.rsplit('@', 1)[-1] not in FREEMAIL
              and not (address.endswith('@'+domain) or domain.endswith('.'+address.rsplit('@', 1)[-1]))]
    return {'email':' | '.join(found), 'contact_url':url, 'email_source':url,
            'needs_attribution_review':review}, root


def import_contact_files(store, entries):
    """Preflight the whole batch before changing any contact or snapshot."""
    from pathlib import Path
    if not isinstance(entries, list) or not entries:
        raise ValueError('Se requiere lista no vacía de company_id, path y url')
    items = {i['company_id']:i for i in store.materialize()}
    prepared = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {'company_id','path','url','encoding'} or not all(isinstance(entry.get(k),str) and entry[k].strip() for k in ('company_id','path','url')):
            raise ValueError('Formato: company_id, path, url y encoding opcional')
        if not entry['url'].lower().startswith(('http://','https://')) or not isinstance(entry.get('encoding','utf-8-sig'),str):
            raise ValueError('url requiere HTTP(S) y encoding debe ser texto')
        key = (entry['company_id'], entry['url'])
        if key in seen:
            raise ValueError('Página duplicada en el lote')
        seen.add(key)
        raw = Path(entry['path']).read_bytes()
        text = raw.decode(entry.get('encoding', 'utf-8-sig'))
        prepare_contact(items.get(entry['company_id']), text, entry['url'])
        sid = 'web-'+entry['company_id']
        if store.db.execute('SELECT 1 FROM sources WHERE id=?',(sid,)).fetchone(): store.active_source(sid)
        prepared.append((entry['company_id'],text,entry['url'],raw))
    results = [store.ingest_contact_page(cid, text, url, raw) for cid,text,url,raw in prepared]
    store.write_progress()
    return {'pages':len(results),'emails_found':sum(r['emails_found'] for r in results),
            'pending_attribution':sum(r['pending_attribution'] for r in results),'network_requests':0}
