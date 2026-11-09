"""Real Hermes project-memory gate; run each phase in a separate process.

Copy this runner into the Hermes checkout as _cmi_project_live_probe.py, then:
CMI_SRC=<repo> hermes --run-module _cmi_project_live_probe <phase> <root> [model] [provider]
Remove the temporary runner afterward. No live plugin replacement or paid fallback.
Synthetic random decisions are test inputs, never claimed production knowledge.
"""
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, os.environ.get("CMI_SRC", str(Path(__file__).resolve().parents[1])))
from cmi.engine import CMIEngine
from run_agent import AIAgent

phase, root_arg = sys.argv[1:3]
root = Path(root_arg)
root.mkdir(parents=True, exist_ok=True)
model = sys.argv[3] if len(sys.argv) > 3 else "gpt-6.1-sol"
provider = sys.argv[4] if len(sys.argv) > 4 else "openai-codex"
project = "cmi-live-pilot" if phase != "isolation" else "unrelated-project"
sid = "cmi_project_" + phase + "_" + uuid.uuid4().hex[:10]
agent = AIAgent(model=model, provider=provider, platform="tool", quiet_mode=True,
                enabled_toolsets=["context_engine"], skip_memory=True, skip_context_files=True,
                skip_background_review=True, session_id=sid, max_iterations=5,
                run_budget_seconds=100, save_trajectories=False,
                reasoning_config={"enabled": True, "effort": "low"})
old = agent.context_compressor
engine = CMIEngine()
engine.on_session_start(sid, hermes_home=str(root), project_id=project)
engine.update_model(model=model, provider=provider,
                    context_length=getattr(old, "context_length", 200000) or 200000)
agent.context_compressor = engine
# Keep the real Hermes tool dispatcher; expose only the scoped project tools for this probe.
schemas = [s for s in engine.get_tool_schemas() if s["name"].startswith("cmi_project_")]
agent.tools = [{"type": "function", "function": s} for s in schemas]
agent.valid_tool_names = {s["name"] for s in schemas}
agent._context_engine_tool_names = set(agent.valid_tool_names)
agent.fallback_model = None

if phase in ("writer", "update", "cross-writer"):
    marker = "pilot-" + uuid.uuid4().hex[:16]
    (root / (phase + "_expected.txt")).write_text(marker)
    revision = {"writer": 0, "update": 1, "cross-writer": 2}[phase]
    prompt = (f"This is a synthetic integration test. Explicit project decision: the pilot marker is {marker}. "
              f"Record this decision for other sessions using cmi_project_publish, topic pilot.marker, "
              f"expected_revision {revision}, kind decision, source user:latest, and quote the marker exactly. "
              "Do not use any unrelated tool. Confirm only after the publication succeeds.")
else:
    prompt = ("What is the current pilot marker for this project? Consult the project memory if needed. "
              "Answer with the exact marker and revision. If no decision exists in this project, "
              "say NO_PROJECT_DECISION. Do not invent a marker or consult any unrelated source.")

result = agent.run_conversation(prompt)
messages = result.get("messages", [])
calls = [tc.get("function", {}).get("name") for m in messages if m.get("role") == "assistant"
         for tc in m.get("tool_calls") or []]
tool_results = [m.get("content") for m in messages if m.get("role") == "tool"]
report = dict(phase=phase, session=sid, model=agent.model, provider=agent.provider,
              final=result.get("final_response", ""), tools=calls, tool_results=tool_results,
              telemetry=engine.telemetry, prompt=prompt,
              last_prompt_tokens=engine.last_prompt_tokens,
              last_completion_tokens=engine.last_completion_tokens,
              error=result.get("error"))
(root / (phase + "_result.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps({k: report[k] for k in ("phase", "session", "model", "provider", "final", "tools", "error")}, ensure_ascii=False))
agent.close()
