"""Persist actual model-enabled pytest output and evaluated file fingerprints."""
import hashlib,json,os,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
command=[sys.executable,'-m','pytest','-q','--basetemp',str(Path.home()/'.hermes/cache/scratch/cmi-retrieval-verified-suite')]
result=subprocess.run(command,cwd=root,capture_output=True,text=True)
paths=['cmi/project_memory.py','cmi/project_retrieval.py','bench/retrieval_holdout.json']
report=dict(command=command,exit_code=result.returncode,stdout=result.stdout,stderr=result.stderr,
            environment={key:os.environ.get(key) for key in ['CMI_STANDALONE','CMI_TEST_RETRIEVAL_MODEL','CMI_TEST_RETRIEVAL_E5']},
            sha256={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in paths})
(root/'bench/results/retrieval-verification-20261014.json').write_text(json.dumps(report,indent=2)+'\n')
print(result.stdout);print(result.stderr);sys.exit(result.returncode)
