"""Select conservative gates on frozen CALIBRATION ONLY raw inference."""
import hashlib,json
from pathlib import Path

p=Path(__file__).parent
fixture=p/'retrieval_holdout.json'
assert hashlib.sha256(fixture.read_bytes()).hexdigest()=='81337fbc36f99f250eb3eb9f2b09e62fbd4939858a42a57cae5219c4d3609dc7'
minilm=[r for r in json.loads((p/'results/retrieval-diagnosis-full-20261014.json').read_text())['observations']['calibration'] if r['headers']]
e5=json.loads((p/'results/retrieval-e5-calibration-raw-20261014.json').read_text())
report={}
for name,records in [('minilm',minilm),('e5',e5)]:
    choices=[]
    for threshold in [n/100 for n in range(30,96,5)]:
        for margin in [.01,.02,.04,.06,.1,.15,.2,.3]:
            hits=[r for r in records if r['top'][0]['score']>=threshold and r['top'][0]['score']-r['top'][1]['score']>=margin]
            correct=sum(r['top'][0]['topic']==r['expected'] for r in hits)
            errors=len(hits)-correct
            choices.append(dict(threshold=threshold,margin=margin,correct=correct,errors=errors,returned=len(hits)))
    valid=[c for c in choices if c['errors']==0]
    best=max(valid,key=lambda c:(c['correct'],c['threshold'],-c['margin']))
    report[name]=dict(selection=best,grid=choices,positive_queries=sum(r['expected'] is not None for r in records),negative_queries=sum(r['expected'] is None for r in records))
report['method']='Require zero source-identity errors on calibration; maximize correct retrievals; tie higher cosine then smaller margin. Small sample, not statistically validated. No holdout inspected.'
(p/'results/retrieval-calibration-selection-20261014.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({name:report[name]['selection'] for name in ['minilm','e5']},indent=2))
