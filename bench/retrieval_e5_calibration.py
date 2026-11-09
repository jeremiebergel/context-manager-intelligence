"""Real pinned E5 rankings, calibration split only; no acceptance filtering."""
import argparse,hashlib,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from cmi.project_retrieval import LocalQueryDocumentRetriever

p=Path(__file__).parent
ap=argparse.ArgumentParser();ap.add_argument('model_dir');args=ap.parse_args()
fixture=p/'retrieval_holdout.json'
assert hashlib.sha256(fixture.read_bytes()).hexdigest()=='81337fbc36f99f250eb3eb9f2b09e62fbd4939858a42a57cae5219c4d3609dc7'
data=json.loads(fixture.read_text())
a=LocalQueryDocumentRetriever(args.model_dir)
rows=[{'topic':t,'text':s} for t,s in data['facts']]
rows += [{'topic':f'ui.{i}','text':'The interface shows the current view and is in the sidebar.'} for i in range(20)]
v=a._encode([a.document_prefix+r['topic']+'\n'+r['text'] for r in rows])
results=[]
for query,topic,category,split in data['queries']:
    if split!='calibration':continue
    scores=v @ a._encode([a.query_prefix+query])[0]
    order=sorted(range(len(rows)),key=lambda i:-float(scores[i]))
    results.append(dict(query=query,expected=topic,top=[dict(topic=rows[i]['topic'],score=float(scores[i])) for i in order[:3]]))
(p/'results/retrieval-e5-calibration-raw-20261014.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(results,ensure_ascii=False,indent=2))
