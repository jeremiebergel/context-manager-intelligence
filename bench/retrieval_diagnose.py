"""Real MiniLM raw rankings. Calibration only; holdout is not inspected here."""
import hashlib
import json
import sys
from pathlib import Path
import time
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmi.project_retrieval import LocalSemanticRetriever
from project_retrieval import FACTS, QUERIES, populate
from cmi.project_memory import ProjectMemory


def main():
    fixture = Path(__file__).with_name('retrieval_holdout.json')
    data = json.loads(fixture.read_text())
    adapter = LocalSemanticRetriever(sys.argv[1], threshold=0)
    with tempfile.TemporaryDirectory(dir=Path.home()/'.hermes/cache/scratch') as tmp:
        memory=ProjectMemory(Path(tmp)/'memory.db')
        populate(memory)
        with memory._connect() as db:
            original_rows=[dict(r) for r in db.execute('''SELECT e.* FROM events e
                WHERE e.project='evaluation' AND e.revision=(SELECT MAX(n.revision)
                FROM events n WHERE n.project=e.project AND n.topic=e.topic)
                ORDER BY e.ts DESC LIMIT 4096''')]
    observations = {}
    for corpus, facts, queries in [('original', FACTS, [q for q in QUERIES if q[2] in ('paraphrase','multilingual','absence')]),
                                   ('calibration', data['facts'], [q[:3] for q in data['queries'] if q[3]=='calibration'])]:
        rows = [dict(id=t, topic=t, text=s, project=corpus, revision=1) for t,s in facts]
        rows += [dict(id=f'ui.{i}',topic=f'ui.{i}',text='The interface shows the current view and is in the sidebar.',project=corpus,revision=1) for i in range(20)]
        if corpus=='original':
            rows=original_rows
        vectors={headers:adapter._encode([r['topic']+'\n'+r['text'] if headers else r['text'] for r in rows]) for headers in (True,False)}
        records = []
        for query, expected, category in queries:
            for headers in (True, False):
                start = time.perf_counter()
                scores = vectors[headers] @ adapter._encode([query])[0]
                order = sorted(range(len(rows)), key=lambda i: -float(scores[i]))
                records.append(dict(query=query,expected=expected,category=category,headers=headers,
                    candidate_count=len(rows),
                    top=[dict(topic=rows[i]['topic'],score=float(scores[i])) for i in order[:3]],
                    expected_rank=next((n+1 for n,i in enumerate(order) if rows[i]['topic']==expected),None),
                    latency_ms=(time.perf_counter()-start)*1000))
        observations[corpus]=records
    output = dict(corpus_sha256=hashlib.sha256(fixture.read_bytes()).hexdigest(),model=adapter.model_name,
                  model_sha256=adapter.model_sha256,tokenizer_sha256=adapter.tokenizer_sha256,
                  protocol=dict(pooling='attention-mask mean',normalization='L2',max_tokens=128,prefix='none'),observations=observations)
    Path(sys.argv[2]).write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    for r in observations['calibration']:
        if r['headers']:
            print(r['expected'],r['query'],r['top'])

if __name__=='__main__':
    main()
