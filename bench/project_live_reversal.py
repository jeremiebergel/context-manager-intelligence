"""Live gate: decide in Claude, reverse it in Claude, then read it with GPT (2026-11-12).

Each phase runs in its own process and its own fresh Hermes agent, sharing only the project store
under <root>. Prompts are worded like a user, without topic names or revision numbers, so the
models have to find and update the decision themselves.

Copy into the Hermes checkout as _cmi_reversal_probe.py, then:
CMI_SRC=<repo> hermes --run-module _cmi_reversal_probe <phase> <root> <model> <provider>
Phases: decide, reverse (Claude), read, history (GPT). Remove the copy afterward. No paid fallback.
"""
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, os.environ.get("CMI_SRC", str(Path(__file__).resolve().parents[1])))
from cmi.engine import CMIEngine  # noqa: E402
from run_agent import AIAgent  # noqa: E402

PROMPTS = {
    "decide": ("Project decision from me: we deploy the API on Fly.io. Save this decision in the project "
               "memory so future sessions, on any model, know it. Confirm when it is saved."),
    "reverse": ("Change of plan: we no longer deploy on Fly.io. We deploy the API on Render, because it is "
                "cheaper for us. Update the project memory so other sessions get the new decision. "
                "Confirm when it is saved."),
    "read": "Where do we deploy the API? Check the project memory. Answer in one sentence.",
    "history": ("Did we decide something else about where to deploy the API before the current choice? "
                "What was it? Check the project memory. If you cannot find an earlier decision, say so."),
}

# Second scenario, a non-technical one: CMI_REVERSAL_SCENARIO=currency.
if os.environ.get("CMI_REVERSAL_SCENARIO") == "currency":
    PROMPTS = {
        "decide": ("Project decision from me: our prices are in US dollars. Save this decision in the project "
                   "memory so future sessions, on any model, know it. Confirm when it is saved."),
        "reverse": ("Change of plan: our prices are no longer in dollars, they are in euros now. Update the "
                    "project memory so other sessions get the new decision. Confirm when it is saved."),
        "read": "What currency are our prices in? Check the project memory. Answer in one sentence.",
        "history": ("Did we use a different currency for our prices before the current one? Quote the earlier "
                    "decision word for word and say which model saved it. Check the project memory. If you "
                    "cannot find an earlier decision, say so."),
    }

phase, root_arg, model, provider = sys.argv[1:5]
root = Path(root_arg)
root.mkdir(parents=True, exist_ok=True)
project = "cmi-reversal-gate"
sid = "cmi_reversal_" + phase + "_" + uuid.uuid4().hex[:10]
# Resolve credentials the way the Hermes CLI does (Anthropic needs it; Codex resolves itself).
from hermes_cli.runtime_provider import resolve_runtime_provider  # noqa: E402
runtime = resolve_runtime_provider(requested=provider, target_model=model)
agent = AIAgent(model=model, provider=runtime.get("provider", provider), api_key=runtime.get("api_key"),
                base_url=runtime.get("base_url"), api_mode=runtime.get("api_mode"),
                platform="tool", quiet_mode=True,
                enabled_toolsets=["context_engine"], skip_memory=True, skip_context_files=True,
                skip_background_review=True, session_id=sid, max_iterations=8,
                run_budget_seconds=180, save_trajectories=False,
                reasoning_config={"enabled": True, "effort": "low"})
old = agent.context_compressor
engine = CMIEngine(auto_project_binding=False)  # explicit binding: no Hermes host session row here
engine.on_session_start(sid, hermes_home=str(root), project_id=project)
engine.update_model(model=model, provider=provider,
                    context_length=getattr(old, "context_length", 200000) or 200000)
agent.context_compressor = engine
schemas = [s for s in engine.get_tool_schemas() if s["name"].startswith("cmi_project_")]
agent.tools = [{"type": "function", "function": s} for s in schemas]
agent.valid_tool_names = {s["name"] for s in schemas}
agent._context_engine_tool_names = set(agent.valid_tool_names)
agent.fallback_model = None

prompt = PROMPTS[phase]
try:
    result = agent.run_conversation(prompt)
except Exception as exc:  # still leave a result file the next phase and the reader can see
    result = {"messages": [], "final_response": "", "error": f"{type(exc).__name__}: {exc}"}
messages = result.get("messages", [])
calls = [dict(name=tc.get("function", {}).get("name"), args=tc.get("function", {}).get("arguments"))
         for m in messages if m.get("role") == "assistant" for tc in m.get("tool_calls") or []]
tool_results = [m.get("content") for m in messages if m.get("role") == "tool"]
report = dict(phase=phase, session=sid, model=agent.model, provider=agent.provider, prompt=prompt,
              final=result.get("final_response", ""), tools=calls, tool_results=tool_results,
              error=result.get("error"))
(root / (phase + "_result.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps({k: report[k] for k in ("phase", "model", "provider", "final", "error")}, ensure_ascii=False))
print("tools:", [c["name"] for c in calls])
agent.close()
