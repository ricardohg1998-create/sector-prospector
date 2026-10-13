"""Regression checks for review findings. Fictitious data and no real network."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sistema_prospeccion.engine import Store, new_run, atomic_json, BRIEF_HEADINGS
from sistema_prospeccion.cli import main
from sistema_prospeccion.network import work
from sistema_prospeccion.reporting import balanced
from sistema_prospeccion.parsing import parse_records


class ImprovementsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.run=new_run('Sector ficticio',self.root/'run',fixture=True)
        self.store=Store(self.run)
        self.guard=patch('socket.create_connection',side_effect=AssertionError('No real network'))
        self.guard.start()
        self.source('uno')

    def tearDown(self):
        self.guard.stop(); self.store.db.close(); self.tmp.cleanup()

    def source(self,sid,**extra):
        spec={'id':sid,'kind':'json','path':str(self.root/(sid+'.json')),
              'scope':{'verified':True,'country':'ES','sector':'Sector ficticio','basis':'Ficticio'},
              'publication_verified':True,'publication_basis':'Ficticio',**extra}
        self.store.add_source(spec)
        return spec

    def ingest(self,sid='uno',name='Negocio FICTICIO',province='Sevilla',**extra):
        row={'name':name,'city':province,'province':province,'address':'Calle FICTICIA 1','email':'hola@ficticio.example',**extra}
        self.store.ingest(sid,sid+':'+name,row)
        return next(i['company_id'] for i in self.store.materialize() if i['name']==name)

    def signal(self,**extra):
        return {'commercial_signal':'Formulario de contacto identificado en fuente ficticia',
                'signal_source':'https://ficticio.example/contacto','signal_observed_at':'2026-10-10',
                'priority':'alta','priority_reason':'Recepción de solicitudes revisable','reason':'Observación ficticia',**extra}

    def test_default_unknown_web_is_not_absence(self):
        cid=self.ingest()
        self.assertEqual(self.store.materialize()[0]['website_status'],'no_investigado')
        self.store.decide(cid,{'website_status':'busqueda_sin_confirmar','reason':'Búsqueda ficticia sin coincidencia segura'})
        self.assertEqual(self.store.materialize()[0]['website_status'],'busqueda_sin_confirmar')
        with self.assertRaises(ValueError):
            self.store.decide(cid,{'website_status':'web_identificada','reason':'No hay URL'})

    def test_priority_and_signal_require_evidence(self):
        cid=self.ingest()
        for value in ({'priority':'alta','priority_reason':'Intuición'}, {'commercial_signal':'Supuesto problema'}, {'commercial_signal':'x','signal_source':'javascript:bad','signal_observed_at':'2026-10-10'}):
            with self.assertRaises(ValueError): self.store.decide(cid,{'reason':'Ficticio',**value})
        self.store.decide(cid,self.signal())
        item=self.store.materialize()[0]
        self.assertEqual(item['priority'],'alta')
        self.assertEqual(self.store.summary()['priority_counts'],{'alta':1})

    def test_batch_invalid_entry_does_not_partially_update(self):
        first=self.ingest(); second=self.ingest(name='Segundo FICTICIO')
        with self.assertRaises(ValueError):
            self.store.decide_many([{'company_id':first,'decision':self.signal()}, {'company_id':second,'decision':{'reason':'x','priority':'alta'}}])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0],0)
        self.store.decide_many([{'company_id':first,'decision':self.signal()}, {'company_id':second,'decision':{'reason':'Búsqueda sin resolver','website_status':'busqueda_sin_confirmar'}}])
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM decisions').fetchone()[0],2)

    def test_source_retirement_preserves_evidence_and_ids(self):
        cid=self.ingest()
        sha=self.store.snapshot(b'FICTICIO','https://ficticio.example/')
        self.source('dos')
        self.ingest(sid='dos')
        self.store.enqueue('directory','https://ficticio.example/',source_id='uno')
        self.store.db.execute("UPDATE jobs SET state='failed'");self.store.db.commit()
        result=self.store.retire_source('uno','Adaptador corregido','dos')
        self.assertEqual(result['observations_retired'],1)
        self.assertEqual(self.store.materialize()[0]['company_id'],cid)
        self.assertTrue((self.run/'originales'/(sha+'.txt')).exists())
        self.assertEqual(self.store.db.execute('SELECT state FROM jobs').fetchone()[0],'cancelled')
        with self.assertRaises(ValueError): self.ingest(sid='uno',name='No reactivar FICTICIO')
        with self.assertRaises(ValueError): self.store.queue_source('uno')
        with contextlib.redirect_stdout(io.StringIO()): self.assertEqual(main(['retry-failed','--run',str(self.run),'--reason','Ficticio']),0)
        self.assertEqual(self.store.db.execute('SELECT state FROM jobs').fetchone()[0],'cancelled')

    def test_reviewed_city_correction_preserves_identity_on_new_source(self):
        cid=self.ingest(city='28913')
        self.store.decide(cid,{'city':'Leganés','reason':'CP/municipio invertidos en fuente ficticia'})
        self.source('corregida')
        self.ingest(sid='corregida',city='Leganés')
        self.assertEqual(len(self.store.materialize()),1)
        self.assertEqual(self.store.materialize()[0]['company_id'],cid)

    def test_partial_correction_observation_preserves_identity_on_new_source(self):
        cid=self.ingest(city='28913')
        self.source('revision-fixture',role='correction')
        self.store.ingest('revision-fixture','correccion',{'city':'Leganés'},company_id=cid)
        self.source('corregida')
        self.ingest(sid='corregida',city='Leganés')
        self.assertEqual(len(self.store.materialize()),1)
        self.assertEqual(self.store.materialize()[0]['company_id'],cid)

    def test_provenance_survives_json_mapping_and_reimport(self):
        spec=self.store.source('uno')
        row={'name':'Negocio FICTICIO','city':'Sevilla','address':'Calle FICTICIA 1','email':'hola@ficticio.example',
             'email_source':'https://ficticio.example/lista.pdf#page=2&row=1','source_cif':'FICTICIO123','source_brand':'Red FICTICIA'}
        atomic_json(spec['path'],[row]); self.store.ingest_file('uno')
        cid=self.store.materialize()[0]['company_id']; self.store.ingest_file('uno')
        self.assertEqual(self.store.materialize()[0]['company_id'],cid)
        self.assertEqual(self.store.materialize()[0]['email_source'],row['email_source'])
        stored=json.loads(self.store.db.execute('SELECT record FROM observations').fetchone()[0])
        self.assertEqual(stored['source_cif'],'FICTICIO123')

    def test_source_metrics_separate_enrichment_duplicates_and_recipients(self):
        first=self.ingest(); self.ingest(name='Segundo FICTICIO',province='Madrid')
        self.source('dos',source_group='Asociación ficticia')
        self.ingest(sid='dos')
        self.source('web-fixture',role='enrichment')
        self.store.ingest('web-fixture','web',{'email':'otro@ficticio.example'},company_id=first)
        summary=self.store.summary()
        self.assertEqual(summary['initial_source_rows'],3)
        self.assertEqual(summary['raw_records'],4)
        self.assertEqual(summary['primary_unique_recipients'],1)
        self.assertEqual(summary['shared_primary_emails_main'],2)
        source=next(s for s in summary['source_yield'] if s['source_id']=='dos')
        self.assertEqual(source['overlap_companies'],1)
        self.assertEqual(source['new_qualified_companies'],0)

    def test_balancing_rotates_provinces_before_multiple_source_groups(self):
        rows=[{'company_id':str(n),'province':'Alicante','_source_ids':[f'fuente{n}']} for n in range(8)]
        rows.append({'company_id':'m','province':'Madrid','_source_ids':['madrid']})
        selected=balanced(rows)
        self.assertEqual([i['province'] for i in selected[:2]],['Alicante','Madrid'])
        rows[-1]['priority']='alta'
        self.assertEqual(balanced(rows)[0]['company_id'],'m')

    def test_broken_html_check_and_refresh_preserve_prior_rows(self):
        spec=self.source('html',kind='html',url='https://ficticio.example/',row_selector='.entry',selectors={'name':'h1'})
        self.ingest(sid='html')
        path=self.root/'broken.html'; path.write_text('<div class="entry">Sin encabezado</div>',encoding='utf-8')
        with self.assertRaises(ValueError): self.store.ingest_file('html',path)
        self.store.enqueue('directory',spec['url'],source_id='html')
        class Fake:
            used=0
            def fetch(this,url,job_id=None):
                this.used+=1; return path.read_text('utf-8'),url
        work(self.store,budget=1,transport=Fake())
        self.assertEqual(self.store.summary()['unique_companies'],1)
        self.assertEqual(self.store.summary()['jobs'],{'failed':1})

    def test_compact_cli_and_review_file(self):
        cid=self.ingest()
        self.ingest(name='Segundo FICTICIO')
        self.store.configure({'target':1})
        sample_output=io.StringIO()
        with contextlib.redirect_stdout(sample_output): self.assertEqual(main(['sample','--run',str(self.run),'--population','main','--limit','10']),0)
        self.assertEqual(json.loads(sample_output.getvalue())['total_matching'],1)
        output=io.StringIO()
        with contextlib.redirect_stdout(output): code=main(['status','--run',str(self.run)])
        result=json.loads(output.getvalue())
        self.assertEqual(code,0); self.assertNotIn('source_yield',result)
        reviews=self.root/'reviews.json'; atomic_json(reviews,[{'company_id':cid,'decision':self.signal()}])
        with contextlib.redirect_stdout(io.StringIO()): code=main(['review-file','--run',str(self.run),'--file',str(reviews)])
        self.assertEqual(code,0); self.assertEqual(self.store.materialize()[0]['priority'],'alta')

    def test_adapter_cli_freezes_and_refuses_changed_output(self):
        path=self.root/'card.html'
        path.write_text('<div class="entry"><div>Nombre: FICTICIO</div><div>Email: hola@ficticio.example</div></div>',encoding='utf-8')
        cfg=self.root/'adapter.json'; atomic_json(cfg,{'container_selector':'.entry','labels':{'Nombre':'name','Email':'email'}})
        # Use the maintained ASETRA config, replacing only its container for a fixture.
        cfgdata=json.loads((Path(__file__).parents[1]/'adapters/asetra_labels.json').read_text('utf-8'))
        cfgdata['container_selector']='.entry'
        atomic_json(cfg,cfgdata)
        path.write_text('<div class="entry"><div>Nombre comercial FICTICIO</div><div>Email hola@ficticio.example</div></div>',encoding='utf-8')
        out=self.root/'frozen.json'
        args=['adapter-extract','--adapter','html_labels','--input',str(path),'--config',str(cfg),'--output',str(out),'--source-url','https://ficticio.example/']
        with contextlib.redirect_stdout(io.StringIO()): code=main(args)
        self.assertEqual(code,0)
        self.assertTrue(out.with_suffix('.json.manifest.json').exists())
        path.write_text('<div class="entry"><div>Nombre comercial Otro FICTICIO</div></div>',encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()): code=main(args)
        self.assertEqual(code,2)

if __name__=='__main__': unittest.main()
