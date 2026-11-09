# Automatic project binding

On by default inside Hermes. A session binds to a project when its workspace folder is inside exactly
one Hermes Desktop project; cron, Telegram and one-shot sessions never bind. This document describes how
the host identity is resolved and the failure modes.

## Turn it off

```yaml
context:
  cmi:
    auto_project_binding: false
```

A non-boolean value also turns it off (fail closed).

No global `CMI_PROJECT_ID` should be set. Existing explicit per-process binding and
`on_session_start(project_id=...)` take precedence; an explicit empty/null binding,
`CMI_PROJECT_ID=""`, or `bind_project(None)` disables automatic binding for that
engine. Invalid explicit project IDs fail closed rather than falling back.

Automatic mode advertises the same project tool schemas before and after session
start. Until host evidence resolves, their handlers return the existing disabled
error. Default/off mode preserves the existing schema contract. Tool descriptions
ask agents to publish sourced decisions/results without waiting for a manual
"save this" request; instruction compliance is not an autonomous-publication
acceptance test.

## Audited host facts and chosen architecture

Audited the actual checkout under `~/.hermes/hermes-agent` and the readonly default
profile databases, not a hypothetical core API:

- `agent/agent_init.py:2124-2146` requests `get_tool_schemas()` **before**
  `on_session_start(session_id, hermes_home, platform, model, context_length,
  conversation_id)`. It does not pass a project ID or parent session ID.
- `tui_gateway/session_workdir.py::_ensure_session_db_row` persists a Desktop
  session's explicit workspace as `sessions.cwd`. The live Desktop session
  `SESSION_ID` has source `desktop`, workspace
  `/Users/USER/Desktop/Projects`, and no parent. A readonly call to the new
  resolver against that live metadata returned the expected project id; no configuration was
  enabled and no binding was installed in the running session.
- `state.db.sessions` has `id`, `source`, `cwd`, and `parent_session_id`, but **no
  project_id**. `run_agent.py::_ensure_db_session` creates a session row lazily;
  `agent/turn_context.py` ensures it after building the system prompt and before
  preflight compression / the first LLM call.
  `tools/delegate_tool.py` passes a child session DB, `parent_session_id=parent_sid`,
  and `platform="subagent"`. Thus a cold child's schema hook and initial session
  hook cannot assume its row exists. Retry identity resolution at request selection
  and project-tool handling, before capture/retrieval/publication.
- `projects.db.projects` carries `id`, `slug`, `primary_path`, and `archived`;
  `project_folders` carries `project_id`, `path`, and `is_primary` (also label/time
  metadata). The resolver never reads `project_meta.active_id`.
- `hermes_cli/kanban_db.py::_board_path` has explicit `HERMES_KANBAN_DB` precedence
  over board/default resolution. Named boards use `kanban/boards/<slug>/kanban.db`;
  metadata is `board.json`, not a `boards` table. Board project defaults are stamped
  into a task's own `project_id` on creation; later board changes are not task scope.
- `hermes_cli/kanban_db_dispatch.py:2876-2910` exports `HERMES_KANBAN_TASK`,
  `HERMES_KANBAN_DB`, `HERMES_KANBAN_RUN_ID`, and `HERMES_KANBAN_CLAIM_LOCK` for a
  scoped worker. Task and run rows carry status/claim fields. These are trusted
  process metadata from the dispatcher, not model-selected tool arguments.

The smallest portable implementation is a plugin-only resolver. No host patch is
needed for this bounded slice; no host tables are migrated. Every host connection
uses SQLite URI `mode=ro`, short-lived connections, and a 100 ms busy timeout.
Fixtures match the relevant actual table columns, including `task_runs`; tests do
not invent a `boards` table.

### Identity rules

1. Explicit binding/opt-out wins over automatic mode.
2. Start from the hook's exact session ID in the supplied Hermes home. Follow
   `parent_session_id`, never a child's potentially different workspace. Reject
   missing ancestors, cycles, or lineage exceeding 64 rows.
3. For a Desktop root, use **stored host workspace**, not process cwd or prompt
   text. Canonicalize absolute registered folder paths and the stored workspace;
   choose the longest unique registered nonarchived project match. An equal-depth
   match belonging to two projects, including symlink aliases, is ambiguous and
   disables binding. Secondary registered folders are valid evidence.
4. For a kanban root, require the exact absolute dispatcher DB pin and canonical
   positive decimal run ID. Require the exact task and joined current `task_runs`
   row both to be `running`, with matching run/claim grant. If `claim_expires` is
   present it must be an unexpired integer. Require the task's project to exist and
   not be archived. No board switch, default board, board project fallback, task
   title, or worker cwd can establish scope.
5. Unknown standalone/CLI roots, missing files/tables/rows, locked/corrupt databases,
   absent worker pins, unregistered projects, and unsupported schemas stay off.
   Initial lazy-row absence is retried. Once a binding is established, metadata is
   revalidated before every request-selection and project-tool call. Revocation or
   changed scope scrubs captured sources and latches the engine off rather than
   silently switching projects.
6. A same-engine compression boundary preserves its trusted original session
   anchor and home (including repeated rotations), unless explicitly overridden.
   The original host identity is still revalidated; an archived project or revoked
   worker claim cannot be rescued by compression.

Binding is identity only. Existing source capture, project isolation, exact quote
matching, optimistic revisions/conflict rejection, and `reported` claim status
remain intact. There is no transcript import, semantic guessing, truth promotion,
mandatory extractor LLM, or additional model invocation.

## Limitations and rollout/publication blockers

- **Parent policy propagation is now plugin-owned:** `<profile-home>/cmi_policy/`
  holds a versioned `sessions.db` registry (directory `0700`, database and lock
  `0600`, same-user ownership; symlinks/hardlinks and unsafe modes are rejected).
  Policy is keyed only by actual `state.db.sessions.id` rows. It stores explicit
  binding, opt-out, or automatic scope plus the exact host lineage and scoped
  worker authority evidence (claim fingerprint, not the raw claim). Independent
  children require parent policy, not child cwd or global active project. Nearest
  explicit own policy wins; inherited scope cannot override a revoked or different
  worker grant. Missing/corrupt/unsupported/locked policy stays off for children
  and already-bound sessions. A new root may bootstrap from trusted host identity;
  this is not authentication and cannot distinguish deliberate same-user deletion
  of all policy from first installation. Do not delete the registry to reset it.
  Concurrent writers/readers are serialized by a private bounded file lock and
  SQLite transactions. Defaults and compression reconstruction use atomic
  insert-if-absent, never replacement; existing explicit mode/evidence survives
  reconstruction. Failed automatic authority is conditionally revoked only while
  its row remains `auto`, not by overwriting concurrent explicit user intent.
  Successful rebind constructs locally then commits its final policy, with no
  intermediate durable `off`. Existing malformed or partially missing registries
  (including a missing lock beside an intact database) are never silently repaired.
  Compression retains original host authority/home but checks durable policy on
  every current/intermediate continuation and host ancestor up to that anchor.
  Observed revocation scrubs sources and latches automatic authority off; deliberate
  owner opt-out additionally revokes the original compression owner.
  Cold rows are retried without inventing policy IDs. Parents created before this
  registry exists must run the updated plugin before independent child automatic
  inheritance can succeed. A missing/cyclic/overlong host lineage fails closed.
  Native host integration and independent review are still required before rollout.
- Home/path layout is intentionally strict. If a profile stores its session/project
  DB elsewhere, or persistence is disabled, metadata will not resolve. There is no
  guessed/global fallback. Worker resumption without the original scoped run/claim
  pins and legacy tasks missing `project_id` stay off, even when `board.json` is linked.
- Host environment and local DBs are trusted same-user inputs, not a sandbox against
  malicious local plugins. Grant checks precede a call; different SQLite files do
  not provide an atomic cross-database revocation transaction.
- Static early schemas are necessary on this core lifecycle: project tools are
  visible but disabled when automatic mode lacks evidence. Agent behavior around
  these disabled tools needs the native end-to-end gate.
- Publication remains agent-invoked. Improved tool instructions reduce manual-save
  friction but do **not** establish reliable autonomous sourced updates. Selection
  remains bounded lexical/topic retrieval, not measured precise semantic/current-task
  selection. Neither is sufficient for the user's central innovation.
- Before public release, verify automatic shared knowledge in fresh sessions,
  autonomous sourced decisions/results without manual save prompts, precise
  task-specific retrieval, live cross-model updates and conflict handling, and
  measured bounded token/latency overhead. No partial/alpha memory demo substitutes
  for these gates.

### Next rollout steps (parent-owned)

1. Review production diff, failure paths, and the parent-override limitation; close
   that privacy/identity gap before unrestricted independent-child deployment.
2. Run the native host gate with separate Desktop sessions, delegated child (cold
   row creation), repeated compression, and a project-linked scoped kanban worker,
   plus unrelated/ambiguous negative controls. Use different models and verify
   source receipts/current revisions. Do not change Desktop paths or active project.
3. Only after review, deploy the reviewed plugin and enable the boolean in the
   intended profile; read back installed/config state and rerun cold initialization.
4. Complete the central-innovation gates above. Commit/publish only when those
   end-to-end requirements are verified. This slice was not staged or committed.

## Local TDD evidence

- Cold lazy Desktop binding test first: **1 failed** (missing constructor mode),
  then **1 passed** after the initial resolver/schema integration.
- Lineage/scoped-worker additions before implementation: **4 failed, 26 passed**;
  then **30 passed** after parent traversal and exact run/claim/task validation.
- Revoked-grant/run-status/natural-publication additions: **3 failed, 31 passed**;
  then **34 passed** after automatic revalidation and publication instructions.
- Expired-grant regression before expiry validation: **1 failed, 41 passed**;
  then **42 passed**. Supplemental configuration/secondary-folder/repeated-rotation
  checks are regression coverage, not presented as additional production RED cycles.

Full-suite command (using the required isolated scratch test directory):

```sh
CMI_STANDALONE=1 uv --system-certs run --with pytest python -m pytest -q --basetemp /Users/USER/.hermes/cache/scratch/cmi-autobind-tests
```

Final execution: **219 passed, 1 skipped in 6.44s**. `git diff --check` passed.
The pre-existing untracked `uv.lock` retains its October 13 mtime; no lockfile,
commit, staging, deployment, or public publication was performed.

### Binding-policy revocation corrections

`tests/test_project_policy_revocation.py` reproduces the five independent defects
before their fixes: transient committed `off`, explicit-to-auto restart promotion,
stale reconstruction overwriting opt-out, partial-registry repair, and compressed
current/intermediate policy revocations being ignored. Additional RED cycles cover
stale reconstruction replacing a concurrent explicit override and a host
intermediate not previously seen by the engine. The opt-out interleavings also run
in separate threads, pausing after the policy read while an independent engine
commits opt-out; both first bootstrap and existing-policy reconstruction are tested.

Final correction suite: **320 passed, 8 skipped in 9.63s**; existing assertions
remain unchanged. `git diff --check` passed. No model calls, configuration changes,
staging, commits, deployment, or lockfile edits were made.
