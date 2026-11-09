"""Real-upstream proxy check: the Hermes agent (its own credentials, never read here) talks to
the real Anthropic API THROUGH cmi-proxy, with Hermes' own CMI disabled so only the proxy
compiles. Same needle task as live_recall.py; the model has no cmi_recall tool here, only the
file path printed in the stub, like any third-party harness behind the proxy."""
import json, sys, tempfile, threading, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cmi import proxy

from http.server import ThreadingHTTPServer
from run_agent import AIAgent

home = tempfile.mkdtemp(prefix="cmi_px_real_")
sessions = proxy.Sessions(home)
srv = ThreadingHTTPServer(("127.0.0.1", 0), proxy.make_handler(
    sessions, proxy.Upstream("https://api.anthropic.com"), proxy.Upstream("https://api.openai.com"), False))
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"

# The needle looks like every other line (same shape, same keys), so no excerpt heuristic can
# single it out: the model must actually get the original back.
import random
rng = random.Random(7)
CODENAME = "-".join(["qx4", "Lm20", "zz8"])
lines = [f"worker-{i:03d} port={rng.randint(20000, 60000)} build={rng.choice('abcdefgh')}{rng.randint(100,999)}-{rng.choice('KLMN')}{rng.randint(10,99)}-{rng.choice('xyz')}{rng.randint(1,9)}" for i in range(160)]
lines[97] = f"worker-097 port=51877 build={CODENAME}"
content = "\n".join(lines)
hist = [{"role": "user", "content": "Lis la config du service."},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_cfg", "type": "function",
         "function": {"name": "read_file", "arguments": json.dumps({"path": "/tmp/cmi_demo/service.env"})}}]},
        {"role": "tool", "tool_call_id": "call_cfg", "content": content},
        {"role": "assistant", "content": "Config lue."}]
for t in range(8):
    hist += [{"role": "user", "content": f"Autre sujet {t} : explique brièvement ce qu'est un mutex."},
             {"role": "assistant", "content": "Un mutex garantit l'accès exclusif à une ressource partagée."}]

# Route Hermes' HTTP traffic to the proxy at the transport level: the client keeps its real base
# URL and credentials (OAuth identity is decided on the api.anthropic.com host), only the socket
# goes to the proxy, which forwards to the real API.
import httpx
SENT = []
_orig_compile = proxy.compile_request
def _spy(sessions_, route, body):
    out, stats = _orig_compile(sessions_, route, body)
    SENT.append((body, out, stats))
    return out, stats
proxy.compile_request = _spy
_orig_send = httpx.HTTPTransport.handle_request
def _via_proxy(self, request):
    if request.url.host == "api.anthropic.com":
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=srv.server_address[1])
        request.headers["host"] = f"127.0.0.1:{srv.server_address[1]}"
    return _orig_send(self, request)
httpx.HTTPTransport.handle_request = _via_proxy

a = AIAgent(model="claude-sonnet-5", provider="anthropic", quiet_mode=True, platform="cli",
            skip_memory=True, skip_context_files=True, session_id=f"cmi_px_{uuid.uuid4().hex[:6]}", max_iterations=6)
a.context_compressor.select_context = lambda *x, **k: None      # only the proxy compiles
a.tools = [t for t in a.tools if t["function"]["name"] != "cmi_recall"]
a.valid_tool_names.discard("cmi_recall")
res = a.run_conversation("Sans relancer la commande d'origine (le fichier a été supprimé), donne le port et le build exacts de worker-097 dans la config du service lue au début.",
                         conversation_history=hist)
calls = [(tc["function"]["name"], tc["function"]["arguments"][:160]) for m in res["messages"][len(hist):] if m.get("role") == "assistant" for tc in (m.get("tool_calls") or [])]
for m in res["messages"][len(hist):]:
    if m.get("role") == "tool":
        print("  tool result:", str(m.get("content"))[:160].replace("\n", " "))
# What the model actually saw for the old read (last request the proxy compiled)
eng = next(iter(sessions._lru.values()))[0]
ans = res.get("final_response") or ""
stored = list(Path(home, "cmi_store").glob("*.txt"))
print("proxy sessions:", len(sessions._lru), "| originals stored:", len(stored))
print("tools called:", calls)
print("answer:", ans[:200].replace("\n", " "))
print("CORRECT:", CODENAME in ans and "51877" in ans)

for body, out, stats in SENT:
    blob = json.dumps(out.get("messages"), ensure_ascii=False)
    print("request:", stats, "| needle visible:", CODENAME in blob, "| stub present:", "[CMI digest]" in blob)

for body, out, stats in SENT:
    for m in out["messages"]:
        c = json.dumps(m, ensure_ascii=False)
        if CODENAME in c:
            print("needle in:", m["role"], c[:700])
