# CMI reference

Details behind the [README](../README.md): how degradation works, invariants, recorded results, every
install path, the Console and the reviewer.

## What it does

On every request, CMI compiles the history:

| Pass | What happens | Guard |
|---|---|---|
| Tool results | Old results for files/commands you have stopped working on become a **digest** (the full path/command, plus head, tail and error lines) | Kept raw if recent, if they concern a file you are working on, or if the digest would not remove at least half. A result that contains an error scores higher (kept longer) but is not exempt: once old, it is digested and its error lines are kept in the digest (`[flagged]`, about 150 characters each, capped in total); the full original stays recoverable |
| **Recall** | If you name a file again ("go back to `src/auth/session.ts`", "fix session.ts"), its old result comes back **in full, on the same request** (during the first 100 requests of a session it stays recoverable with `cmi_recall` instead, to protect the cache) | Matches absolute, relative or bare-name spellings |
| Screenshots | Stale images are replaced by a text stub | Recent images are kept |
| Tool-call payloads | Old `write_file`/`patch` contents are shortened | Never if a later message refers to that call |
| Duplicates | A result identical to an earlier one becomes a reference | Only above a minimum size |
| Small-result batches | Runs of tiny results are folded into a single summary | Only when that is shorter |
| Narration | Old "let me look at…" filler is shortened | Never touches user messages |

## Recoverable degradation, within storage limits

```
[CMI digest] cmi:116c0a7a48 [read_file] read /srv/app/service.env (7,365 chars) [excerpt] …
```

When the model needs something that was shortened, it calls `cmi_recall("cmi:116c0a7a48")` and gets
the stored original, while it remains available, even if the source file has changed or been deleted since.
If an original cannot be saved (e.g. disk error), that content is simply not shortened.

Native Hermes uses `$HERMES_HOME/cmi_store/` (normally `~/.hermes/cmi_store/`).
Standalone mode uses `$CMI_HOME/cmi_store/` (normally `~/.cmi/cmi_store/`).
The content-addressed store prunes originals untouched for 30 days and applies a 512 MiB size cap;
old stub keys may therefore stop resolving. The search index is a separate `cmi_index.db`
under the same home. See [SECURITY.md](SECURITY.md) for other persisted state.
The recorded five-session pilot reported 1,771 keys in stubs, 1,771 exact recalls, 1.3 MB on disk.

Live check with the real Hermes loop (`bench/live_recall.py`): a value hidden in the middle of an
old, digested file read, then the question "what was that value?" with the file deleted. Claude
Sonnet 5 and GLM 5.3 both called `cmi_recall` and answered exactly.

## Tested invariants (not blanket guarantees)

Every `tool_call` keeps its `tool_result`, the output is byte-identical from one call to the next
(so the prompt cache stays stable), and a crash inside CMI sends the original request unchanged
(fail-open).

Compaction (summarizing the conversation when the window fills up) is still done by Hermes's
built-in compressor. CMI just makes it happen later and less often.

## Measured results (CMI 1.11.1)

26 real coding sessions (Claude Opus/Sonnet and GPT-6 models, several projects, 636M tokens), replayed
request by request with `bench/cost_replay.py`, which models the provider's cache (cache read 0.1x, write
1.25x, 5-minute expiry). Weighted by tokens:

| Measure | Average | Median | Best |
|---|---:|---:|---:|
| Tokens sent | −28% | −22% | −42% |
| Same token budget goes further | 1.4x | | 1.7x |
| Modelled API cost | −24% | −21% | −36% |
| 15 sessions of 100+ requests: tokens | −29% | −28% | −38% |
| 15 sessions of 100+ requests: modelled API cost | −25% | −23% | −34% |

Live (2026-11-09): 6 Hermes sessions on Claude Sonnet 5.5, about 195 requests each, a read-only audit of
191 project files (188M tokens): 49% fewer tokens sent (48% to 55%), modelled cost −27% (−25% to −32%).
A task message that lists every file path keeps those files whole (named files); since 1.11.1 they are
capped by `CMI_HOT_FILE_TOKENS` (default 32000, newest first, fixed for the turn).

No session costs more with CMI (17 cheaper, 9 untouched); all sessions of 100+ requests are cheaper. Until a session has
made 100 requests CMI does not rewrite a warm provider cache (no tier, no refresh of stale reads or named
files); when the cache has already expired, rewriting is free and CMI shortens at once. "Modelled cost" is CMI's accounting, not a bill, and providers do
not publish how tokens count against a subscription limit.

Through the proxy path, 9 further real Claude Code sessions (5,680 requests, 2026-11-09): 20% fewer
tokens, modelled cost −12.5% weighted by requests, every shortened result recalled byte-exact (1,992
recalls), 0 invariant violations. Eight sessions end at or below 0%; one 208-request session ends at
+0.7%, so "never more expensive" is measured on the 26 sessions above, not guaranteed.

Proxy path: `bench/claude_code_replay.py` rebuilds Claude Code sessions from `~/.claude/projects/*.jsonl`
and replays them through the proxy's compile path. Every request is checked: tool pairs, thinking
signatures, `cache_control` and user content unchanged, output byte-stable, every stub recallable.
Image tokens are counted at their billed size, not their base64 length.

Overhead: about 20 ms per request in steady state (up to ~450 ms on very long histories), no network, no model call.

Quality: a 10-task A/B benchmark (resume an edit, recall an old decision, re-use an old result…)
showed no regressions against running without CMI.

**If you pay per token with prompt caching** (e.g. Anthropic's API), fewer tokens does not always
mean a lower bill: shortening an old message invalidates the cached prefix after it. Cache tiers
(v1.4+) only rewrite the prefix when the saving pays the rewrite back within a few requests, so
long sessions end cheaper while short ones can end costlier (see the warning above; `/cmi` shows
both costs per session). On flat-rate plans and providers without prompt caching, token savings
translate into runway, but actual quotas, rate limits and billing differ.

## Install

Not on PyPI yet: install from GitHub, or from a local checkout with `python -m pip install .`.

### Any agent, through the proxy (Claude Code, Codex CLI, Cline, Aider, OpenCode, your own)

```bash
pip install "git+https://github.com/jeremiebergel/context-manager-intelligence"
cmi-proxy --port 8787
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude              # Anthropic Messages clients
OPENAI_BASE_URL=http://127.0.0.1:8787/v1 codex               # OpenAI API-key login only (see below)
```

Codex signed in with a ChatGPT account ignores `OPENAI_BASE_URL` (verified with Codex 0.164:
requests go straight to `chatgpt.com`, the proxy sees nothing). Use the provider setup under
*VS Code and other editors* below, which also works for the Codex CLI. On macOS, Homebrew's
Python refuses a global `pip install` (PEP 668): use a virtualenv, `pipx install` or
`uv tool install` instead.

| Wire format | Route | Clients |
|---|---|---|
| Anthropic Messages | `POST /v1/messages` | Claude Code, any Anthropic SDK client |
| OpenAI Chat Completions | `POST /v1/chat/completions` | Cline, Aider, OpenCode, most agents |
| OpenAI Responses | `POST /v1/responses` | Codex CLI, OpenAI Agents SDK |

The proxy compiles each request on the way out and streams responses back untouched. Only what
CMI changes is rewritten: thinking blocks and their signatures, encrypted reasoning items,
`cache_control` breakpoints, ids and unknown fields go upstream byte-for-byte. Parallel tool
results that share one Anthropic message are judged one by one. Responses requests that rely on
server-side history (`previous_response_id`) pass through unchanged. Your
API authentication is forwarded to the upstream provider. The proxy binds to `127.0.0.1` by
default, but `--host` can expose it elsewhere. `GET /cmi/<key>` is unauthenticated: keep the proxy
local and do not expose it to untrusted clients. Regex-based credential detection skips recognized
secret-bearing text for shortening/indexing; it can miss secrets, including those in images.
`CMI_ALLOW_SECRETS=1` disables this guard. This is not a guarantee that credentials never reach
disk or another process. See [SECURITY.md](SECURITY.md).

**VS Code and other editors:** there is no native VS Code extension; the official extensions
run their own agent binary, so they go through the proxy like the CLIs:

- *Claude Code extension* (verified live, subscription login included): in VS Code settings,
  `"claudeCode.environmentVariables": [{"name": "ANTHROPIC_BASE_URL", "value": "http://127.0.0.1:8787"}]`.
- *Codex / ChatGPT extension* with a ChatGPT login (verified live with Codex 0.164, CLI and the
  extension's bundled binary): that login talks to `chatgpt.com`, not `api.openai.com`. Start
  the proxy with `--openai-upstream https://chatgpt.com/backend-api/codex` (Anthropic requests
  still go to `api.anthropic.com`, so one proxy serves both extensions) and add to
  `~/.codex/config.toml`, before any other `[table]`:

  ```toml
  model_provider = "cmi"

  [model_providers.cmi]
  name = "cmi"
  base_url = "http://127.0.0.1:8787"
  wire_api = "responses"
  requires_openai_auth = true
  ```

  Codex already caps long command outputs itself, so the proxy saves less there than with
  Claude Code.
- Cline, Roo Code, Continue…: point their Anthropic or OpenAI-compatible base URL at
  `http://127.0.0.1:8787`.

A third-party harness has no `cmi_recall` tool, so each stub also names the original's file path
(readable with any file-read tool), and `GET /cmi/<key>` serves it.

**Background service:** `cmi-proxy install-service` starts the proxy at login (loopback only,
`--openai-upstream` set for ChatGPT logins) and writes the two editor settings above, backed up in
`~/.cmi/backups`; `cmi-proxy uninstall-service` removes both, `cmi-proxy status` checks them. No admin
rights are needed on macOS and Linux; on Windows that is the design, but CI runs it as an administrator
so the no-admin case is not verified yet:

| OS | Service | Logs |
|---|---|---|
| macOS | LaunchAgent `~/Library/LaunchAgents/com.cmi.proxy.plist` | `~/.cmi/logs` |
| Linux | systemd user unit `~/.config/systemd/user/cmi-proxy.service`, `Restart=on-failure` | `journalctl --user -u cmi-proxy` |
| Windows | Scheduled Task `cmi-proxy` at logon (`pythonw`, no console); if Windows refuses it, `cmi-proxy.cmd` in the Startup folder | none |

On Linux the install stops with an error and changes nothing when `systemctl --user` is unavailable
(containers without systemd, WSL1): run `cmi-proxy` yourself there. The user unit runs while you are
logged in; `loginctl enable-linger` keeps it running without a session. With the Windows Startup-folder
fallback, `uninstall-service` cannot stop the running proxy: it stops at sign-out.

Python ≥ 3.10, standard library only.

### Hermes Agent (native plugin)

```bash
hermes plugins install jeremiebergel/context-manager-intelligence/cmi
```

Then enable it in `~/.hermes/config.yaml`:

```yaml
context:
  engine: cmi
```

Restart Hermes (quit the desktop app, restart the gateway). The logs should show no
`Context engine 'cmi' not found — falling back to built-in compressor`. Inside Hermes the model
also gets the `cmi_recall` and `cmi_search` tools and the `/cmi` savings report.

Requires Hermes Agent ≥ 0.21 (last verified: 0.22.5). CMI uses some Hermes internals; a Hermes
upgrade can move them. Check your install with the Python that runs Hermes (the plugin is not
pip-installed, so point `PYTHONPATH` at the plugins folder):

```bash
cd ~/.hermes/hermes-agent
PYTHONPATH=~/.hermes/plugins ./venv/bin/python -m cmi.hermes_contract   # OK, or the exact symbols that moved
```

The core engine falls back on every one of them (fail-open: Hermes keeps working, CMI features
degrade); a missing symbol is worth an issue.

## CMI Console (Hermes Desktop control center)

A Hermes Desktop page that shows, in one place, what you need when you run agents on
subscriptions:

- **Accounts and real quota windows** — 5-hour session and weekly usage per account
  (Anthropic, OpenAI Codex), cooldowns, last error and time to reset. Providers without a
  quota API (Z.AI, Copilot) are listed as connected.
- **One-click account switch** — name your two Anthropic logins (*Personal* / *Work*); when one
  is blocked, **Switch to this account** puts the other first, clears only its local backoff, stops the
  running retry loop and re-selects the same model on the open conversation. It never moves a
  non-Anthropic conversation to Claude and never resets a provider quota.
- **Projects and cost** — sessions and API-price equivalent per project and provider, plus what
  a flat subscription saves you (indicative).
- **CMI savings** — tokens kept out of requests by the context engine.

Install (requires Hermes ≥ 0.22 with the desktop app):

```bash
hermes plugins install jeremiebergel/context-manager-intelligence/cmi_console
hermes plugins enable cmi_console
```

Then enable the desktop half in *Capabilities → Plugins* and restart Hermes. The console never
returns a credential: every response passes an allowlist and a leak guard, and every write is
an explicit, confirmed click. See [cmi_console/SECURITY.md](cmi_console/SECURITY.md).

Limits: exactly two named Anthropic accounts (`perso`/`pro`); account order is shared by all
conversations of the profile; after a fresh `hermes auth` login the account must be named again
(automatic when done through **Reconnect**). The console relies on some internal Hermes
functions and may need an update after a Hermes release.

## Optional reviewer (separate cost and data boundary)

The distribution also contains `cmi_review`, a separate Hermes plugin. Installing the package
is not the same as registering/enabling that plugin. When enabled in Hermes, it sends code
changes and the user request to a reviewer model and persists findings/metadata locally.
It makes additional LLM calls, may consume paid tokens or subscription quota, and subjects
that content to the reviewer provider's policies. The zero-LLM claim applies to the memory/
context compile path, **not** this optional reviewer or live model benchmarks. Review the
configuration before enabling it; see [SECURITY.md](SECURITY.md) and [PROVENANCE.md](PROVENANCE.md).

## Benchmark

Reproducible, offline (synthetic session, no private data; on the pinned commit it reports about
25% of characters not sent, every stub recallable exactly, tool pairs intact, byte-identical reruns):

```bash
python bench/public_demo.py          # add --json for machine-readable output
```

Synthetic numbers show the mechanism, not your savings. Measure your own sessions:

```bash
cd ~/.hermes/hermes-agent
./venv/bin/python <repo>/bench/runway.py              # your 5 largest request dumps
./venv/bin/python <repo>/bench/runway.py session.json # any OpenAI/Anthropic messages list
./venv/bin/python <repo>/bench/zero_loss.py           # every stub key -> exact original
./venv/bin/python <repo>/bench/live_recall.py         # real model uses cmi_recall (makes API calls)
```

## Tests

```bash
pip install -e ".[dev]"
CMI_STANDALONE=1 python -m pytest -q        # without Hermes, the way the proxy runs
python bench/proxy_e2e.py                   # proxy against a fake upstream, no network
~/.hermes/hermes-agent/venv/bin/python -m pytest -q   # inside Hermes
```

The tests are behaviour contracts: tool-call pairing, byte-stable output, the input is never
mutated, fail-open, no LLM call, recall of a named file, no marginal digests, incremental
learning, every degradation recallable verbatim, no degradation when originals cannot be saved.

## Layout

| File (`cmi/`) | Role |
|---|---|
| `engine.py` | `CMIEngine`: the compile pipeline (and the Hermes `ContextEngine` contract) |
| `proxy.py` | `cmi-proxy`: CMI in front of any Anthropic / OpenAI-compatible API |
| `wire.py` | Anthropic Messages / OpenAI Responses <-> the canonical history; writes back only what changed |
| `secrets.py` | Best-effort regex credential detection; not a comprehensive privacy filter |
| `compat.py` | Uses Hermes classes inside Hermes, portable stand-ins everywhere else |
| `lifecycle.py` | Scoring and rendering of tool results (raw / digest / one-liner) |
| `working_set.py` | Which files are "hot" (read, written, named by the user), with decay |
| `store.py` | Verbatim, content-addressed store behind `cmi_recall` |
| `index.py` | Full-text index of past tool outputs behind `cmi_search` (SQLite FTS5) |
| `cost_guard.py`, `stats.py` | Cache-aware cost shadow and the savings ledger |
| `capabilities.py` | Per-model limits |

Other components: `cmi_console/` (Hermes plugin: dashboard API + desktop page),
`console-ui/` (dependency-free UI source; `python console-ui/build.py` writes
`cmi_console/desktop/plugin.js`), `cmi_review/` (optional reviewer plugin), `bench/` (benchmarks
and recorded results).

See [ROADMAP.md](ROADMAP.md) for where this is going.

## License

MIT. Licensing is not an originality or privacy audit; see [PROVENANCE.md](PROVENANCE.md).
