"""Real main-model catalogue evaluation; no fixture answers enter model prompts.

Copy temporarily into the Hermes checkout; CMI_SRC points to a frozen code tree.
Run <run_dir> [model] [provider] [comma-separated case ids or all]. Each case is a
fresh real Hermes agent. Read-only native tool results come from the prepared driver.
"""
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, os.environ["CMI_SRC"])
from bench.project_catalog import load, tool_step, score
from cmi.engine import CMIEngine
from run_agent import AIAgent

run_dir = Path(sys.argv[1])
model = sys.argv[2] if len(sys.argv) > 2 else "gpt-6.1-sol"
provider = sys.argv[3] if len(sys.argv) > 3 else "openai-codex"
selection = sys.argv[4] if len(sys.argv) > 4 else "all"
questions = load(run_dir / "questions.json")["cases"]
if selection != "all":
    allowed = set(selection.split(","))
    questions = [q for q in questions if q["case_id"] in allowed]
    if len(questions) != len(allowed):
        raise ValueError("Unknown or duplicate selected cases")
schemas = load(run_dir / "schemas.json")

class DriverEngine(CMIEngine):
    def __init__(self, case):
        super().__init__(auto_project_binding=False)
        self.case = case
    def get_tool_schemas(self):
        return schemas
    def handle_tool_call(self, name, args, **kwargs):
        try:
            return json.dumps(tool_step(run_dir, dict(case_id=self.case, name=name,
                             arguments=args or {}))["result"], ensure_ascii=False)
        except (ValueError, TypeError) as error:
            return json.dumps({"error":str(error)})
    def select_context(self, request_messages, **kwargs):
        # This isolates active-model navigation, not passive semantic injection.
        return None

answers, details = [], []
for index, case in enumerate(questions):
    sid = "catalog-live-" + uuid.uuid4().hex[:12]
    agent = AIAgent(model=model, provider=provider, platform="tool", quiet_mode=True,
                    enabled_toolsets=["context_engine"], skip_memory=True,
                    skip_context_files=True, skip_background_review=True,
                    session_id=sid, max_iterations=10, run_budget_seconds=90,
                    save_trajectories=False, reasoning_config={"enabled":True,"effort":"low"})
    engine = DriverEngine(case["case_id"])
    engine.on_session_start(sid, hermes_home=str(run_dir / "runtime-home"), project_id=None)
    engine.update_model(model=model, provider=provider, context_length=100000)
    engine._compactor = getattr(agent.context_compressor, "_compactor", agent.context_compressor)
    agent.context_compressor = engine
    agent.tools = [{"type":"function","function":s} for s in schemas]
    agent.valid_tool_names = {s["name"] for s in schemas}
    agent._context_engine_tool_names = set(agent.valid_tool_names)
    agent.fallback_model = None
    prompt = (case["question"] + "\n\nUse only this project's stored sources. Discover topics if search is empty or "
              "uncertain, and inspect complete exact evidence for any cited record. Topic similarity is not "
              "proof that a requested detail exists. If sources do not support the requested detail, abstain. "
              'Return only JSON: {"answer":"...","cited_ids":["actual opened IDs"],"abstained":true_or_false}.')
    started = time.monotonic()
    result = agent.run_conversation(prompt)
    final = result.get("final_response") or ""
    try:
        fragment = final[final.index("{"):final.rindex("}")+1]
        parsed = json.loads(fragment)
        if not isinstance(parsed.get("answer"),str) or not isinstance(parsed.get("cited_ids"),list) or type(parsed.get("abstained")) is not bool:
            raise ValueError("Malformed answer schema")
        submission = dict(case_id=case["case_id"], answer=parsed["answer"],
                          cited_ids=parsed["cited_ids"], abstained=parsed["abstained"])
    except (ValueError, TypeError, json.JSONDecodeError):
        submission = dict(case_id=case["case_id"], answer=final, cited_ids=[], abstained=False,
                          parse_error=True)
    usage = {key:getattr(agent,key,None) for key in (
        "session_input_tokens","session_output_tokens","session_cache_read_tokens",
        "session_cache_write_tokens","session_api_calls")}
    detail = dict(case_id=case["case_id"], model=model, provider=provider, session=sid,
                  prompt=prompt, final=final, error=result.get("error"), usage=usage,
                  elapsed_seconds=time.monotonic()-started, messages=result.get("messages",[]))
    answers.append(submission); details.append(detail)
    (run_dir / "actual-answers.json").write_text(json.dumps(answers,ensure_ascii=False,indent=2))
    with (run_dir / "actual-runs.jsonl").open("a") as f:
        f.write(json.dumps(detail,ensure_ascii=False,default=str)+"\n")
    print(json.dumps({"progress":index+1,"selected_cases":len(questions),"case":case["case_id"],
                      "error":detail["error"],"parsed":not submission.get("parse_error",False),
                      "elapsed_seconds":round(detail["elapsed_seconds"],2)},ensure_ascii=False),flush=True)
    if detail["error"]:
        break
summary = score(run_dir, answers)
(run_dir / "actual-score.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps({"model":model,"provider":provider,"completed":len(answers),
                  "evidence":str(run_dir),"score":summary},ensure_ascii=False),flush=True)
