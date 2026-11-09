"""Native model/provider transition gate using actual CLI resume and separate sessions.

Synthetic markers are test inputs. Same-session continuity and fresh-session shared
memory are separate assertions; a blocked provider is recorded, never faked.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import uuid

root = Path(__file__).resolve().parents[1]
project = "cmi-transition-" + uuid.uuid4().hex[:12]
marker = "transition-" + uuid.uuid4().hex[:16]
corrected = "correction-" + uuid.uuid4().hex[:16]
env = dict(os.environ, CMI_PROJECT_ID=project)
base = ["hermes", "chat", "--ignore-rules", "-t", "context_engine", "--max-turns", "5",
        "--run-budget", "75", "-Q", "--in", str(root)]
reports = {}

def run(name, model, provider, prompt, resume=None):
    args = base + ["-m", model, "--provider", provider, "-q", prompt]
    if resume:
        args += ["--resume", resume]
    result = subprocess.run(args, env=env, cwd=root, capture_output=True, text=True, timeout=100)
    record = dict(model=model, provider=provider, prompt=prompt, resume=resume,
                  exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
    reports[name] = record
    print(json.dumps({"phase":name,"model":model,"provider":provider,
                      "exit_code":result.returncode,"output":result.stdout[:1500]},ensure_ascii=False),flush=True)
    return record

def current():
    path = Path.home() / ".hermes/cmi_project/memory.db"
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return [dict(x) for x in db.execute("SELECT * FROM events WHERE project=? AND topic=? ORDER BY revision",
                                           (project,"transition.marker"))]

writer = run("sol-writer", "gpt-6.1-sol", "openai-codex",
             f"Synthetic integration test decision: transition marker is {marker}. "
             "Publish it using cmi_project_publish, topic transition.marker, kind decision, expected_revision 0, "
             "source user:latest, and exact quote of the marker. Confirm only after success.")
rows = current()
assert len(rows)==1 and marker in rows[0]["text"], "Initial publication not verified"
sid = rows[0]["session"]
question = "What is the current transition marker and its revision? Retrieve the project record and cite its id; do not guess."
luna = run("same-session-luna", "gpt-6-luna", "openai-codex", question, sid)
assert marker in luna["stdout"], "Same-session model switch recall failed"
run("same-session-luna-correction", "gpt-6-luna", "openai-codex",
    f"Synthetic correction: transition marker is now {corrected}. Publish the correction under transition.marker, "
    "expected_revision 1, kind decision, source user:latest, exact quote; confirm after success.", sid)
rows = current()
assert len(rows)==2 and corrected in rows[-1]["text"] and rows[-1]["model"]=="gpt-6-luna"
assert rows[-1]["session"]==sid, "CLI resume did not preserve session identity"
sol = run("same-session-return-sol", "gpt-6.1-sol", "openai-codex", question, sid)
assert corrected in sol["stdout"] and marker not in sol["stdout"], "Return-to-original model recall failed"
fresh = run("fresh-same-model-sol", "gpt-6.1-sol", "openai-codex", question)
assert corrected in fresh["stdout"], "Fresh same-model session shared-memory recall failed"
claude = run("same-session-claude", "claude-sonnet-5", "anthropic", question, sid)
cross_same = corrected in claude["stdout"]
if cross_same:
    fresh_claude = run("fresh-claude", "claude-sonnet-5", "anthropic", question)
    cross_fresh = corrected in fresh_claude["stdout"]
else:
    cross_fresh = False
report = dict(project=project, synthetic=True, preserved_session_id=sid,
              gates={"same_session_model_switch":True,"same_session_corrected_return":True,
                     "fresh_same_model_recall":True,"cross_provider_same_session":cross_same,
                     "cross_provider_fresh_session":cross_fresh}, reports=reports, events=current(),
              limits=["Explicit per-process project binding; automatic Desktop binding separately gated",
                      "Synthetic marker scenario, not natural-publication reliability benchmark",
                      "No alternate provider fallback is attempted by this harness"])
output=root/"bench/results/project-memory-transitions-20261014.json"
output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({"gates":report["gates"],"evidence":str(output)},ensure_ascii=False))
