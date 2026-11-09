"""Natural-work publication gate: no user instruction to save or call memory tools.

Run as a temporary Hermes runtime module with CMI_SRC pointing to a frozen source tree.
The scenario uses the user's actual release requirement, not a made-up product decision.
"""
import json
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0, os.environ["CMI_SRC"])
from cmi.engine import CMIEngine
from run_agent import AIAgent

phase, folder = sys.argv[1:3]
folder = Path(folder)
folder.mkdir(parents=True, exist_ok=True)
model = "gpt-6.1-sol" if phase == "writer" else "gpt-6-luna"
agent = AIAgent(model=model, provider="openai-codex", max_iterations=6,
                quiet_mode=True, enabled_toolsets=[], skip_memory=True,
                skip_context_files=True, skip_background_review=True)
engine = CMIEngine()
engine.update_model(model=model, provider="openai-codex", context_length=100000)
engine.on_session_start("natural-" + phase + "-" + uuid.uuid4().hex[:10],
                        hermes_home=str(folder), project_id="cmi-real-release-policy")
old = agent.context_compressor
engine._compactor = getattr(old, "_compactor", old)
agent.context_compressor = engine
agent.tools = [{"type": "function", "function": s}
               for s in engine.get_tool_schemas() if s["name"].startswith("cmi_project_")]
agent.valid_tool_names = {t["function"]["name"] for t in agent.tools}
agent._context_engine_tool_names = set(agent.valid_tool_names)
agent.fallback_model = None
if phase == "writer":
    prompt = ("Pour CMI, la première publication GitHub doit inclure le fonctionnement complet du cœur : "
              "partage intermodèle par projet, publication naturelle de décisions sourcées, rappel ciblé "
              "dans les nouvelles sessions et corrections visibles. Une démo de compression ou "
              "d'enregistrement manuel ne suffit pas. Propose un ordre d'implémentation en cinq points maximum.")
else:
    prompt = ("Pour notre première mise en ligne, est-ce qu'une alpha limitée avec une démo manuelle suffit ? "
              "Quelle règle a-t-on retenue ? Donne la règle et l'identifiant de sa source si elle existe dans le projet ; "
              "sinon indique que tu ne retrouves pas la décision, sans l'inventer.")
result = agent.run_conversation(prompt)
report = dict(phase=phase, model=model, session=engine._session_id, prompt=prompt,
              final=result.get("final_response"), error=result.get("error"),
              messages=result.get("messages", []), telemetry=dict(engine.telemetry))
(folder / (phase + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
print(json.dumps({"phase":phase, "model":model, "final":report["final"], "error":report["error"],
                  "tools":[c["function"]["name"] for m in report["messages"] for c in m.get("tool_calls",[])]},
                 ensure_ascii=False))
engine.on_session_end(engine._session_id, result.get("messages") or [])
