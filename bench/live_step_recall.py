"""Live quality gate for v1.10 step aging: needles inside ONE long autonomous turn.

One user prompt, 40 read_file steps (like a kanban worker). Three needles sit in the middle of the
results of steps 1, 12 and 20, which step aging digests; their values look like every other line,
so no excerpt can keep them. The real Hermes agent loop (real provider, real tools, the REPO cmi
engine swapped in) is then asked for the three values without the files.

    hermes --run-module live_step_recall [model] [provider]
"""
import json
import os
import random
import sys
import uuid
from pathlib import Path

sys.path.insert(0, os.environ.get("CMI_SRC", str(Path(__file__).resolve().parents[1])))
from cmi.engine import CMIEngine  # noqa: E402
from run_agent import AIAgent  # noqa: E402

rng = random.Random(11)


def body(mod):
    return [f"worker-{i:03d} port={rng.randint(20000, 60000)} build={rng.choice('abcdefgh')}{rng.randint(100, 999)}-"
            f"{rng.choice('KLMN')}{rng.randint(10, 99)}-{rng.choice('xyz')}{rng.randint(1, 9)}" for i in range(160)]


NEEDLES = {0: (97, "51877", "qx4-Lm20-zz8"), 11: (33, "40213", "pr7-Tk55-ww2"), 19: (120, "28866", "ab9-Jn31-yy6")}
hist = [{"role": "user", "content": "Audite les 40 modules de /tmp/cmi_step/ (lis-les tous), puis on fera le rapport."}]
for s in range(40):
    lines = body(s)
    if s in NEEDLES:
        w, port, build = NEEDLES[s]
        lines[w] = f"worker-{w:03d} port={port} build={build}"
    cid = f"call_{s}"
    hist.append({"role": "assistant", "content": "", "tool_calls": [{"id": cid, "type": "function", "function": {
        "name": "read_file", "arguments": json.dumps({"path": f"/tmp/cmi_step/mod{s:02d}.env"})}}]})
    hist.append({"role": "tool", "tool_call_id": cid, "content": "\n".join(lines)})
hist.append({"role": "assistant", "content": "Les 40 modules sont lus."})

model = sys.argv[1] if len(sys.argv) > 1 else "claude-sonnet-5"
provider = sys.argv[2] if len(sys.argv) > 2 else "anthropic"
a = AIAgent(model=model, provider=provider, quiet_mode=True, platform="cli", skip_memory=True,
            skip_context_files=True, session_id=f"cmi_step_{uuid.uuid4().hex[:6]}", max_iterations=10)
old = a.context_compressor
eng = CMIEngine()
eng.on_session_start(a.session_id)
eng.update_model(model=model, context_length=getattr(old, "context_length", 200000) or 200000, provider=provider)
a.context_compressor = eng

compiled = eng.select_context(list(hist)) or hist
for s in NEEDLES:
    i = 2 + 2 * s
    stub = compiled[i]["content"]
    print(f"mod{s:02d}: digested={stub != hist[i]['content']} needle_visible={NEEDLES[s][2] in stub}")
saved = 1 - sum(len(json.dumps(m)) for m in compiled) / sum(len(json.dumps(m)) for m in hist)
print(f"request shrunk by {100 * saved:.0f}%")

q = ("Sans relire les fichiers (ils ont été supprimés du disque), donne exactement le port et le build de : "
     "worker-097 dans mod00, worker-033 dans mod11, worker-120 dans mod19.")
res = a.run_conversation(q, conversation_history=hist)
calls = [tc["function"]["name"] for m in res["messages"] if m.get("role") == "assistant" for tc in (m.get("tool_calls") or [])]
ans = res.get("final_response") or ""
ok = [port in ans and build in ans for _, port, build in NEEDLES.values()]
print("tools called:", calls)
print("answer:", ans[:400].replace("\n", " "))
print(f"CORRECT {sum(ok)}/3", ok)
