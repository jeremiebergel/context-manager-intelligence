"""Frozen source-identity benchmark; no answers/generated proxy inference."""
from __future__ import annotations
import argparse, hashlib, importlib.metadata, json, platform, resource
import subprocess, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmi.project_memory import ProjectMemory
from cmi.project_retrieval import LocalSemanticRetriever, LocalQueryDocumentRetriever
from project_retrieval import legacy_search, summarize
from retrieval_frozen import prior_search

FROZEN_SHA = '81337fbc36f99f250eb3eb9f2b09e62fbd4939858a42a57cae5219c4d3609dc7'


def resource_probe(kind, directory):
    cls = LocalSemanticRetriever if kind == 'minilm' else LocalQueryDocumentRetriever
    start = time.perf_counter()
    adapter = cls(directory)
    load = (time.perf_counter()-start)*1000
    rows = [dict(project='p',id='r',revision=1,topic='uploads.recovery',text='Uploads interrupted by a network disconnect resume from the last acknowledged chunk rather than starting again.')]
    start = time.perf_counter()
    adapter.rank('How can a severed connection continue a file transfer?',rows,limit=3)
    return dict(load_ms=load,first_single_record_inference_ms=(time.perf_counter()-start)*1000,
                max_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                model_bytes=(Path(directory)/'model.onnx').stat().st_size,
                tokenizer_bytes=(Path(directory)/'tokenizer.json').stat().st_size,
                rss_units='bytes on macOS',providers=adapter._session.get_providers(),
                model=adapter.model_name,revision=adapter.model_revision,model_sha256=adapter.model_sha256,
                tokenizer_sha256=adapter.tokenizer_sha256)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--minilm-dir');ap.add_argument('--e5-dir');ap.add_argument('--output')
    ap.add_argument('--resource-model',choices=['minilm','e5']);ap.add_argument('--directory')
    args=ap.parse_args()
    if args.resource_model:
        print(json.dumps(resource_probe(args.resource_model,args.directory)));return
    fixture=Path(__file__).with_name('retrieval_holdout.json')
    assert hashlib.sha256(fixture.read_bytes()).hexdigest()==FROZEN_SHA
    data=json.loads(fixture.read_text())
    resources={kind:json.loads(subprocess.check_output([sys.executable,__file__,'--resource-model',kind,'--directory',directory],text=True))
               for kind,directory in [('minilm',args.minilm_dir),('e5',args.e5_dir)]}
    with tempfile.TemporaryDirectory(prefix='cmi-heldout-',dir=Path.home()/'.hermes/cache/scratch') as temp:
        db=Path(temp)/'memory.db';memory=ProjectMemory(db)
        receipts={}
        for topic,text in data['facts']:
            memory.publish('harbour',topic,'OBSOLETE: '+text,source='fixture:obsolete:'+topic)
            receipts[topic]=memory.publish('harbour',topic,text,source='fixture:current:'+topic,expected_revision=1)
            memory.publish('foreign',topic,text,source='fixture:foreign:'+topic)
        for i in range(20):
            memory.publish('harbour',f'ui.{i}','The interface shows the current view and is in the sidebar.',source=f'fixture:noise:{i}')
        connection=memory._connect()
        try:
            corpus_counts=dict(target_topics=len(data['facts']),
                all_events=connection.execute('SELECT COUNT(*) FROM events').fetchone()[0],
                foreign_rows=connection.execute("SELECT COUNT(*) FROM events WHERE project='foreign'").fetchone()[0],
                current_project_rows=connection.execute('''SELECT COUNT(*) FROM events e WHERE e.project='harbour'
                    AND e.revision=(SELECT MAX(n.revision) FROM events n WHERE n.project=e.project AND n.topic=e.topic)''').fetchone()[0],
                obsolete_rows=connection.execute('''SELECT COUNT(*) FROM events e WHERE e.revision<
                    (SELECT MAX(n.revision) FROM events n WHERE n.project=e.project AND n.topic=e.topic)''').fetchone()[0])
        finally:
            connection.close()
        assert corpus_counts==dict(target_topics=12,all_events=56,foreign_rows=12,current_project_rows=32,obsolete_rows=12)
        minilm=LocalSemanticRetriever(args.minilm_dir)
        calibrated_minilm=LocalSemanticRetriever(args.minilm_dir,threshold=.7)
        calibrated_minilm.top_only=True
        calibrated_minilm.minimum_margin=.01
        e5=LocalQueryDocumentRetriever(args.e5_dir)
        current=ProjectMemory(db,semantic=minilm)
        calibrated=ProjectMemory(db,semantic=calibrated_minilm)
        candidate=ProjectMemory(db,semantic=e5)
        methods={'baseline_or':lambda q:legacy_search(memory,'harbour',q),
                 'current_lexical':lambda q:prior_search(memory,'harbour',q,limit=3),
                 'current_minilm':lambda q:prior_search(current,'harbour',q,limit=3),
                 'fixed_minilm':lambda q:current.search('harbour',q,limit=3),
                 'calibrated_minilm':lambda q:calibrated.search('harbour',q,limit=3),
                 'candidate_e5':lambda q:candidate.search('harbour',q,limit=3)}
        results={}
        for name,search in methods.items():
            observations=[]
            for query,topic,cat,split in data['queries']:
                if split!='holdout':continue
                expected={receipts[topic]['id']} if topic else set()
                start=time.perf_counter();hits=search(query);elapsed=(time.perf_counter()-start)*1000
                assert all(r['project']=='harbour' and (r['topic'].startswith('ui.') or r['revision']==2) for r in hits)
                # Read source identity, not similar text, not generated answer quality.
                observations.append(dict(query=query,category=cat,expected_ids=sorted(expected),
                    expected_source=receipts[topic]['source'] if topic else None,
                    returned=[{k:r[k] for k in ('id','topic','source','text','revision','project')} |
                              ({'retrieval':r['retrieval']} if 'retrieval' in r else {}) for r in hits],
                    relevant_returned=sum(r['id'] in expected for r in hits),
                    irrelevant_text_bytes=sum(len(r['text'].encode()) for r in hits if r['id'] not in expected),latency_ms=elapsed))
            results[name]=dict(overall=summarize(observations),
                categories={c:summarize([r for r in observations if r['category']==c]) for c in sorted({r['category'] for r in observations})},
                observations=observations)
        # Header ablation is reporting only, after parameters have been frozen.
        raw={}
        rows=[dict(project='harbour',id=t,revision=2,topic=t,text=s) for t,s in data['facts']]
        rows += [dict(project='harbour',id=f'ui.{i}',revision=1,topic=f'ui.{i}',text='The interface shows the current view and is in the sidebar.') for i in range(20)]
        for name,a in [('minilm',minilm),('e5',e5)]:
            records=[]
            for query,topic,cat,split in data['queries']:
                if split!='holdout':continue
                for headers in (True,False):
                    docs=[a.document_prefix+(r['topic']+'\n' if headers else '')+r['text'] for r in rows]
                    scores=a._encode(docs) @ a._encode([a.query_prefix+query])[0]
                    order=sorted(range(len(rows)),key=lambda i:-float(scores[i]))
                    records.append(dict(query=query,category=cat,expected=topic,headers=headers,
                        expected_rank=next((n+1 for n,i in enumerate(order) if rows[i]['id']==topic),None),
                        top=[dict(topic=rows[i]['topic'],score=float(scores[i])) for i in order[:3]]))
            raw[name]=records
    report=dict(format=1,scope='independent fictional operations corpus; NOT real sessions, broad semantic validation, or answerability proof',
                corpus_sha256=FROZEN_SHA,corpus=corpus_counts,
                split=dict(calibration=12,holdout=36),limit=3,
                candidate_policy=dict(threshold=.85,minimum_margin=.01,top_only=True,selection='calibration only; frozen before holdout; no tuning after holdout'),
                platform=dict(system=platform.system(),machine=platform.machine(),python=platform.python_version()),
                runtime_versions={p:importlib.metadata.version(p) for p in ('numpy','onnxruntime','tokenizers')},
                cold_resources=resources,results=results,raw_rankings=raw)
    Path(args.output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v['overall'] for k,v in results.items()},indent=2))

if __name__=='__main__':main()
