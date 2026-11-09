"""Live check: does a real model use cmi_recall when the answer is only in a digested result?

Builds a conversation where an old read_file result holds a specific value (a port number and
an arbitrary codename placed in the middle, so the digest's excerpt misses them), lets CMI
digest it, then asks for the value. Runs the real Hermes agent loop (real provider, real
tools) and reports whether the model called cmi_recall and answered correctly."""
import json, sys, uuid
from run_agent import AIAgent

# The needle looks like every other line, so no excerpt heuristic can single it out: the model
# must get the original back.
import random
rng = random.Random(7)
CODENAME = "-".join(["qx4", "Lm20", "zz8"])
lines = [f"worker-{i:03d} port={rng.randint(20000, 60000)} build={rng.choice('abcdefgh')}{rng.randint(100,999)}-{rng.choice('KLMN')}{rng.randint(10,99)}-{rng.choice('xyz')}{rng.randint(1,9)}" for i in range(160)]
lines[97] = f"worker-097 port=51877 build={CODENAME}"
content = "\n".join(lines)

path = "/tmp/cmi_demo/service.env"
hist = [{"role": "user", "content": "Lis la config du service."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_cfg", "type": "function",
         "function": {"name": "read_file", "arguments": json.dumps({"path": path})}}]},
        {"role": "tool", "tool_call_id": "call_cfg", "content": content},
        {"role": "assistant", "content": "Config lue."}]
for t in range(8):
    hist += [{"role": "user", "content": f"Autre sujet {t} : explique brièvement ce qu'est un mutex."},
             {"role": "assistant", "content": "Un mutex garantit l'accès exclusif à une ressource partagée."}]

a = AIAgent(model=sys.argv[1] if len(sys.argv) > 1 else "claude-sonnet-5", provider=(sys.argv[2] if len(sys.argv) > 2 else "anthropic"),
            quiet_mode=True, platform="cli", skip_memory=True, skip_context_files=True,
            session_id=f"cmi_live_{uuid.uuid4().hex[:6]}", max_iterations=6)
cc = a.context_compressor
cc.on_turn_complete(hist)
compiled = cc.select_context(hist) or hist
stub = compiled[2]["content"]
print("digested:", stub != content, "| needle visible in stub:", CODENAME in stub)
print("stub:", stub[:220].replace("\n", " "))

res = a.run_conversation("Sans relire le fichier sur disque (il a été supprimé depuis), donne le port et le build exacts de worker-097 dans la config du service qu'on a lue au début.",
                         conversation_history=hist)
calls = [tc["function"]["name"] for m in res["messages"] if m.get("role") == "assistant" for tc in (m.get("tool_calls") or [])]
ans = res.get("final_response") or ""
print("tools called:", calls)
print("answer:", ans[:300].replace("\n", " "))
print("CORRECT:", CODENAME in ans and "51877" in ans)
