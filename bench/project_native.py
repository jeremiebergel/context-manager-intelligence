"""Native installed-plugin smoke test; no engine swapping or transcript sharing.

Run with Python. Credentials stay inside Hermes; only synthetic markers are recorded.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import uuid

root = Path(__file__).resolve().parents[1]
project = "cmi-native-smoke-" + uuid.uuid4().hex[:12]
marker = "native-" + uuid.uuid4().hex[:16]
env = dict(os.environ, CMI_PROJECT_ID=project)
base = ["hermes", "chat", "--provider", "openai-codex", "--ignore-rules", "-t", "context_engine",
        "--max-turns", "4", "--run-budget", "80", "-Q", "--in", str(root)]
writer_prompt = (f"Synthetic integration test. Explicit decision: native pilot marker is {marker}. "
                 "Publish it with cmi_project_publish, topic native.pilot.marker, kind decision, "
                 "expected_revision 0, source user:latest, and quote the marker exactly. "
                 "Confirm only after success. Use no unrelated tools.")
reader_prompt = "What is the native pilot marker in this project? Return its exact value and revision using project memory."
reports = {}
rows = []
for phase, model, prompt in (("writer", "gpt-6.1-sol", writer_prompt),
                             ("reader", "gpt-6-luna", reader_prompt)):
    result = subprocess.run(base + ["-m", model, "-q", prompt], env=env, cwd=root,
                            capture_output=True, text=True, timeout=120)
    reports[phase] = dict(model=model, returncode=result.returncode,
                          stdout=result.stdout, stderr=result.stderr, prompt=prompt)
    if result.returncode:
        raise RuntimeError(f"Native {phase} failed: {result.stderr or result.stdout}")
    if phase == "writer":
        path = Path.home() / ".hermes/cmi_project/memory.db"
        with sqlite3.connect(path) as db:
            rows = db.execute("SELECT id,text,revision,session,model FROM events WHERE project=? AND topic=?",
                              (project, "native.pilot.marker")).fetchall()
        if len(rows) != 1 or marker not in rows[0][1] or rows[0][2] != 1:
            raise RuntimeError("Native publication was not verified by database readback")
if marker not in reports["reader"]["stdout"] or marker in reader_prompt:
    raise RuntimeError("Native reader did not recover the marker without transcript transfer")
report = dict(project=project, synthetic=True, marker=marker,
              gate="native installed CMI plugin, distinct CLI processes and models", passed=True,
              reports=reports, event=dict(zip(("id", "text", "revision", "session", "model"), rows[0])))
output = root / "bench/results/project-memory-native-20261014.json"
output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps({"passed": True, "project": project, "writer": reports["writer"]["model"],
                  "reader": reports["reader"]["model"], "reader_output": reports["reader"]["stdout"],
                  "evidence": str(output)}, ensure_ascii=False))
