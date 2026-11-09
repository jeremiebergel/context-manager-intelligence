"""Native same-session model-switch gate: Sol -> Luna -> Sol, exact DB readback.

The synthetic marker checks wire/session continuity, not cross-session memory by itself.
No paid provider fallback; use only existing openai-codex subscription.
"""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import uuid

root = Path(__file__).resolve().parents[1]
project = "cmi-switch-" + uuid.uuid4().hex[:12]
first, second = ["switch-" + uuid.uuid4().hex[:16] for _ in range(2)]
env = dict(os.environ, CMI_PROJECT_ID=project)
base = ["hermes", "chat", "--provider", "openai-codex", "--ignore-rules", "-t", "context_engine",
        "--max-turns", "9", "--run-budget", "100", "-Q", "--in", str(root)]
home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
reports = []
session = None
for phase, model, prompt in [
    ("create", "gpt-6.1-sol", f"Synthetic switch test. Decision: marker is {first}. Publish topic switch.marker, kind decision, source user:latest, quote marker exactly, expected_revision 0. Use one publication call, then finish immediately after success."),
    ("switch-update", "gpt-6-luna", f"Synthetic switch test. Correct our marker to {second}. Publish switch.marker, source user:latest, quote marker exactly, expected_revision 1, kind decision. Use one publication call, then finish immediately after success."),
    ("switch-back-read", "gpt-6.1-sol", "What is the current switch marker? Reply exact value and revision using current project state, not the obsolete value.")]:
    cmd = base + ["-m", model, "-q", prompt]
    if session:
        cmd += ["--resume", session]
    result = subprocess.run(cmd, env=env, cwd=root, capture_output=True, text=True, timeout=130)
    report = dict(phase=phase, requested_model=model, returncode=result.returncode,
                  stdout=result.stdout, stderr=result.stderr, prompt=prompt)
    reports.append(report)
    if result.returncode:
        raise RuntimeError(f"Switch phase failed: {phase}: {result.stderr or result.stdout}")
    with sqlite3.connect(home / "cmi_project/memory.db") as db:
        db.row_factory=sqlite3.Row
        events = [dict(r) for r in db.execute("SELECT * FROM events WHERE project=? AND topic=? ORDER BY revision", (project, "switch.marker"))]
    if not events:
        raise RuntimeError("First decision not published")
    if session is None:
        session = events[0]["session"]
    with sqlite3.connect(home / "state.db") as state:
        row = state.execute("SELECT model,model_config FROM sessions WHERE id=?", (session,)).fetchone()
    log = (home / "logs/agent.log").read_text(errors="replace").splitlines()
    calls = [line for line in log if session in line and "API call #" in line]
    if not calls or f"model={model} " not in calls[-1]:
        raise RuntimeError("Requested model switch not confirmed by actual API execution log")
    report.update(session=session, actual_model=model, last_api_log=calls[-1],
                  stored_session_model=row[0] if row else None)
if len(events) != 2 or events[0]["session"] != session or events[1]["session"] != session:
    raise RuntimeError("Continuity was not verified within the same session")
if [e["model"] for e in events] != ["gpt-6.1-sol", "gpt-6-luna"]:
    raise RuntimeError("Both models did not publish the expected revisions")
if second not in reports[-1]["stdout"] or first in reports[-1]["stdout"]:
    raise RuntimeError("Switched-back model did not return the current revision only")
output = root / "bench/results/project-memory-switch-20261014.json"
output.write_text(json.dumps(dict(passed=True, synthetic=True, project=project, session=session,
    gate="native same-session Sol -> Luna -> Sol", reports=reports, events=events,
    limits=["Short history; not compressed long-session wire test", "Same-session continuity not proof of isolated cross-session memory"]), ensure_ascii=False, indent=2))
print(json.dumps(dict(passed=True, session=session, actual_models=[r["actual_model"] for r in reports],
                     final=reports[-1]["stdout"], evidence=str(output)), ensure_ascii=False))
