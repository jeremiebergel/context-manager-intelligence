# CMI Project Memory

Per-project, local, sourced memory shared by sessions, models and providers. **Experimental, but on by
default inside Hermes** (since 1.11.0): a session binds to a project only when its workspace folder
belongs to exactly one Hermes Desktop project, so nothing happens outside your projects. Turn it off
with `context.cmi.auto_project_binding: false`. The proxy (VS Code, Codex, terminal agents) does not
use project memory.

## What is verified (2026-11)

- One model publishes a fact with the user's exact words as proof, another model on another provider
  reads it back, a correction creates revision 2 and the old revision stops being returned.
- A single conversation that switches model and LLM keeps its memory (live test, three models).
- Per-project isolation: 0 leaks over 78 cross-project queries. Secrets are refused. Files are 0600.
- Paired live test (2026-11-09, real agent, Claude Sonnet 5.5, a fresh store per session, no history
  search, 3 repetitions per arm, small project with 9 facts): on a question whose answer exists only in
  past conversations, the session with memory found it every time (3 of 3 facts, 3 runs, about 5 seconds,
  about 4,900 tokens sent) and the session without memory answered "I don't know" every time (0 of 3,
  about 2,600 tokens). On a question answered by a status file in the project, both found everything (4 of
  4) at similar cost (about 11,500 tokens with memory, 14,000 without, ranges overlap): memory adds
  nothing there. One model, one small project, 3 runs: a tendency, not a statistic.
- A new session on a small real project (9 facts) answered "where is the project at?" from memory alone
  in 15 seconds for about $0.14, without rereading earlier sessions.

## What is not (yet)

- No automatic capture: facts exist only when a model decides to publish one.
- Search is word-based plus model-supplied synonyms (`tags`, `cues`) and a labelled approximate stage.
  A question that shares no word with the stored fact can still miss (0 of 21 and 0 of 12 on our
  paraphrase checks).
- The token saving of starting a session from memory is not measured. The cost is: about 1,800 tokens
  for the tool descriptions (cached) and about 270 bytes of context per request.

## Promise
One explicit project identity owns sourced, versioned knowledge shared by sessions and models. Each agent receives a task-specific working set, not the whole common transcript. Updates are visible at the next request boundary, not inside an already-running model response.

## Implemented
- `project_memory.py`: SQLite WAL/FTS5; project filter before retrieval limits; append-only revisions and optimistic compare-and-swap, preserving superseded records.
- Evidence stored exactly in the same transaction as the claim. SHA-256 integrity checked on expanded evidence retrieval. No dependency on the old 30-day recall-store retention.
- Database created 0600 before SQLite opens it; new dedicated parent directory 0700; live WAL/SHM inherit 0600. Symlink database/journal artifacts rejected. This is same-user storage, not an authenticated multi-tenant service.
- `project_bridge.py`: host-resolved user/tool originals captured before compression at request boundaries; publication requires an exact quote in that original. Quote/source checks establish provenance, NOT truth of the model's interpretation. All claims remain `reported`.
- Native context-engine tools: `cmi_project_publish`, compact `cmi_project_search`, and `cmi_project_get` with paginated exact evidence. Conflict responses expose the current record/revision for explicit arbitration.
- Automatic lexical retrieval before each model request, appended as a separate sourced-data message at the request tail; the existing history prefix is not rewritten. Host input is not mutated.
- Explicit binding via `CMIEngine.on_session_start(..., project_id=...)`, `bind_project(...)` or the host-set `CMI_PROJECT_ID` environment variable. No inference from CWD or model text. A compression-child session preserves its parent binding; reset clears the source registry but not project knowledge.
- Memory input tokens counted by the shadow accounting. A 4000-byte working-set ceiling, reduced near a request budget, is NOT a provider billing guarantee.
- `project_session.py` remains the earlier standalone prototype adapter; the engine integration uses `ProjectBridge` instead.

## Real gates passed
Evidence: `bench/results/project-memory-20261014.json`.

Eight successful, independently created Hermes sessions/processes used two real models (`gpt-6.1-sol` and `gpt-6-luna`) through the user's `openai-codex` provider:
1. Sol publishes a random synthetic decision (revision 1).
2. A new Sol session retrieves it without the writer conversation or marker in its prompt.
3. A different project returns `NO_PROJECT_DECISION`.
4. Sol corrects the decision (revision 2); a fresh Sol session retrieves only the correction.
5. Luna retrieves Sol's current decision.
6. Luna publishes its own correction (revision 3); a fresh Sol session retrieves it.
7. SQLite readback confirms three revisions, authors/models and three intact source hashes.

A probe with `gpt-5.4` was rejected by account entitlement (HTTP 400). It remains recorded as FAILED, not replaced with synthetic output. Luna was then tested through the same subscription provider; no paid fallback or Anthropic call was made by the gate.

Tests: baseline 90 passed / 1 skipped; reviewed corrected suite 177 passed / 1 skipped (initial pilot was 138 passed / 1 skipped). Includes two actual competing processes, source rejection, pagination, evidence tampering, privacy permissions, project isolation, reset, compression-child binding and injected-cost accounting. The skipped test is not counted as passed.

After independent review, six lifecycle/input/concurrency defects were reproduced and fixed with 39 regression tests. A second independent review approved the corrected source with no blocking findings. The corrected live gate (`bench/results/project-memory-fixed-20261014.json`) uses five fresh processes: Sol publishes, Luna recalls without the writer transcript, the unrelated project returns no decision, Luna corrects, and Sol retrieves the correction. Exact source hashes were checked by database readback.

## Still required before general release
1. Desktop Project / kanban project host binding and propagation to child agents. A global environment ID must not bind unrelated concurrent Desktop projects.
2. Autonomous identification/publication of useful decisions and discoveries without benchmark-specific instructions. Capturing originals is automatic; classifying them into durable knowledge currently relies on the agent invoking the publication tool.
3. Validity/freshness policies, verified/pending status transitions and explicit conflict journal. Revision supersession alone is not temporal truth or proof that an observation remains current.
4. Semantic retrieval: aliases, entity links, dependencies and task-goal-aware selection. This pilot uses lexical overlap only.
5. Real long-session quality, cache and token measurements; larger irrelevant corpora; concurrent agents performing different real tasks. Small synthetic probes do not establish production recall accuracy.
6. Cross-provider Codex/Claude gate when Claude is available, without a paid fallback.
7. Retention/deletion, bounded storage and reliable project attribution before migrating historical data.

## Rollout
Keep compression/store/recall working. The reviewed opt-in plugin was deployed with backup `/Users/USER/.hermes/backups/cmi-project-pilot-20261014T083829`; 18 copied files were verified byte-identical. `bench/project_native.py` then exercised the installed plugin without swapping engines: Sol published, database readback verified revision 1, and a fresh Luna CLI process retrieved the marker without the writer conversation (`bench/results/project-memory-native-20261014.json`). Do not set a global project binding for Desktop. Old sessions do not acquire new tools merely because source files are changed on disk.

## Reproduce
Use `CMI_STANDALONE=1 uv --system-certs run --with pytest python -m pytest -q --basetemp "$HOME/.hermes/cache/scratch/cmi-project-full-pytest"` for unit tests. Follow the runner instructions in `bench/project_live.py` for real model phases with a fresh scratch directory. Do not run the writer phase twice in the same project: expected revision 0 deliberately rejects overwriting existing data.
