# Roadmap

CMI's promise: **with the same token budget, agent sessions last as long as possible, with no
loss of quality or memory.** Cheaper bills are a bonus, never the headline.

## Principles (every contribution is held to these)

1. **Zero loss.** Anything shortened is stored verbatim first and recallable by key.
2. **Deterministic, zero-LLM.** Same history in, same bytes out. No model call, no embeddings.
3. **Fail-open.** If CMI fails, the original request goes out unchanged.
4. **Never costlier.** Prompt-cache aware: CMI must not raise the bill on cached providers.
5. **Numbers before claims.** A change that alters output ships with a benchmark delta
   (tokens saved *and* answer quality) on the public suite.

## Done

- **v1.2** Verbatim store + `cmi_recall`: zero loss by construction.
- **v1.3–1.4** Supersession (a re-read or a write makes old reads stale), cache-aware tiers.
- **v1.5** Proactive lexical recall (old results come back when the user names them), savings ledger.
- **v1.6** Rerun supersession, provider-priced cache tiers, cost shadow + alert.
- **v1.7** Cross-session search of past tool outputs (`cmi_search`, SQLite FTS5).
- **v1.8** Installable package (`pip install`, `cmi-proxy`), runs without Hermes, CI on Linux/macOS.
- **v1.9 (in progress)** Credential guard (never on disk). Proxy speaks OpenAI Responses (Codex CLI).
  Anthropic parallel tool results judged per tool. One tool vocabulary for Hermes, Claude Code
  (`Read`/`Edit`/`Bash`, `file_path`), Codex (argv shell, `apply_patch`) and Cline. Old write/edit
  payloads shortened for every host. Evicted screenshots kept as image files (zero loss);
  user-pasted images never touched. `bench/claude_code_replay.py`: 8 real Claude Code sessions,
  0 invariant violations, 100% exact recall.
- **cmi_review 0.1 (2026-10-11)** Separate, optional Hermes plugin (the only part of the project
  that calls an LLM). Every turn that changes code is re-read by a second model, never the writer
  (Opus → Sonnet, anything else → Opus by default). Findings are injected into the next turn as
  "not verified, check before fixing", and every review lands in `cmi_reviews.jsonl` (writer,
  reviewer, task category, findings): the data a per-task model router needs. `/cmi-review` shows it.

## Next

### v1.9 — Proven outside Hermes
- [ ] Live runs: Claude Code and Codex CLI through `cmi-proxy` (needle recall with a real model,
      no API rejection, cache hit rate). The offline replay of real Claude Code sessions passes.
- [ ] `cmi-mcp`: an MCP server exposing `cmi_search` / `cmi_recall`, so Claude Code, Cursor and
      Cline get real recall tools instead of a file path in the stub.
- [ ] Proxy-side savings report (`GET /cmi/stats`, `cmi stats`).

### v1.10 — Long autonomous turns (Claude Code, Codex)
CMI ages content by *user* turns. Agentic CLIs often run 50–100 tool calls on one prompt: in the
single-prompt sessions replayed, 29–42% of the last request is tool output older than 10 agent
steps, and CMI leaves it all raw.
- [x] Step aging (2026-10-12): every 8 agent steps of one user turn count as one more turn of age;
      mid-turn re-tiers only at epoch boundaries, same payback rule. 5 largest kanban sessions
      (single prompt, 164-340 requests): quota saved 0% -> 42-55%, cached cost -24 to -35%. 4 largest
      desktop sessions: 25-50% -> 36-60%, never worse. Live needle gate (40-step turn, 3 needles,
      Sonnet 5 and Opus 5.5): 3/3 each via cmi_recall.
- [ ] Superseded plans: an older `ExitPlanMode` plan once a newer one exists (up to 100K tokens in
      one replayed session).
- [x] Quality gate: `bench/live_step_recall.py` (needles inside one long autonomous turn, real model).
- [ ] Measure with `count_tokens` whether old `thinking` blocks are billed (~30% of Claude Code
      history); the API allows omitting them from earlier turns.

### v2.0 — Public launch
- [ ] Public, reproducible benchmark on open agent trajectories (no private sessions), one command.
- [ ] Baselines: no CMI, naive truncation, host compaction, and other open-source compactors.
- [ ] Quality next to tokens: needle recall and task answers, not just bytes saved.
- [ ] PyPI `cmi-context`, Hermes plugin catalog entry, CONTRIBUTING, issue templates.

### v2.x — Built by the community
- [ ] **Pass API.** Split the compile pipeline into passes behind one interface, with a registry.
- [ ] **Digesters.** Tool-aware digests contributed per tool: test runners (pytest, jest), build
      logs (npm, cargo, gradle), stack traces, `git diff`, HTML, JSON APIs. Each digester ships with
      fixtures and its benchmark delta. This is the main contribution unit.
- [ ] **Host adapters** as small packages: Hermes (native), proxy (any API), MCP, and SDK hooks for
      people building their own agents.
- [ ] Leaderboard: the public benchmark run on every merged pass/digester.

### Later, if there is demand
- Shared team memory: one searchable index of tool outputs across a team's agents.
- Hosted proxy with a savings dashboard.

## How to contribute (until CONTRIBUTING.md lands)

Open an issue with the session shape where CMI does badly (redact it), or a digester proposal with
2–3 real fixtures. Every PR runs `pytest` standalone and `bench/proxy_e2e.py`.
