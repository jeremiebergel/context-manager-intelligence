# Changelog

Unreleased work is listed first. Dates are YYYY-MM-DD.

## 1.12.3 (2026-11-13)

- Project memory: `cmi_project_get` on a revised topic also lists its earlier revisions (id, revision,
  model, date, first 200 characters; at most 10: the last ones, or those around an older revision being
  opened plus the current one), so a model can read what a decision was before it
  changed and open any of them in full. A topic with one revision is unchanged. Checked live: a decision
  saved in Claude, reversed in a new Claude session, then a new GPT session quoted the original word for
  word and named the model that saved it (it opened revision 1 from the list).
- `bench/project_live_reversal.py`: live gate for that scenario (decide in Claude, reverse, read in GPT).
  `bench/project_live.py` resolves provider credentials like the Hermes CLI and binds the project
  explicitly (it could no longer reach Anthropic, and project memory stayed off during the run).

## 1.12.2 (2026-11-12)

- Safety, third barrier: a write that went through with CMI's short form as its payload is flagged in
  its own result at the next request, on every host (Hermes with or without the plugin hook, the proxy
  for Claude Code, OpenAI clients and Codex). The flag names the file and stores its last known content
  (last full write or last read in the conversation) under a recall key, so the model restores the file
  at its next step. A write repaired later by a full write is not flagged. Checked end to end on the real
  Hermes tools: the stub lands, the flag points to the recalled content, writing it back restores the
  file byte for byte. A scan of 5,376 past write/patch calls found 34 stub payloads since 2026-09-27
  (the 1.12.0 write stub, copied by the model); none is left in a file today.
- The `pre_tool_call` guard also blocks the 1.12.1 `cmi_omitted` note copied into a payload (at its head,
  or whole anywhere), and any write call that carries the `cmi_omitted` key (only an imitation of a
  shortened past call has it; with an empty `content` it would have written an empty file). The note
  wording lives in one place (`lifecycle.py`) for the renderer and the guard; its bytes are unchanged.

## 1.12.1 (2026-11-12)

- Safety: a model can no longer put a CMI stub in a file by copying CMI's short form of a past write.
  Seen live on 1.12.0: the model copied the `[CMI: content omitted ...]` stub, with an invented recall
  key, into a NEW write, and the file got the stub instead of its content. Two barriers:
  - a shortened past write no longer holds a stub in its payload: the payload keys (`content`,
    `old_string`/`new_string`, `edits`, `input`...) are removed and one `cmi_omitted` note (sizes, path,
    recall keys) is added. A call that imitates it reaches the tool without its payload, and the tool
    refuses it ("missing content"); checked against the real Hermes `write_file` and `patch` handlers,
    with nothing written;
  - a `pre_tool_call` hook (`cmi/write_guard.py`) blocks a write whose payload starts with any CMI
    stub (result digests, duplicates, batches, narration) or embeds a 1.12.0 write stub, and says how to
    get the original back. Hermes runs it when the plugin is enabled (`plugins.enabled`, which
    `hermes plugins install cmi` offers); checked through the real Hermes plugin loader and hook
    dispatch. `plugin.yaml` declares `provides_hooks: [pre_tool_call]`.
  Sessions that already use CMI re-write their cache once, at the first request on 1.12.1.
- PDF page count: an object stream is read up to its own `/Length` (its compressed bytes can contain
  the text "endstream", which cut it short and dropped its pages), and its `/First` is read from its own
  dictionary, never from the previous object. 19 of 19 real PDFs still match their page count.

## 1.12.0 (2026-11-11)

- Start-up phase 100 -> 30 requests in Hermes, 150 -> 30 through the proxy (Claude Code, Codex). With the
  forced-refresh defect below fixed, waiting longer only left mid-length sessions untouched.
  - Hermes, replay of 305 real sessions (Claude Opus/Sonnet 5.5, GPT-6.1, GPT-6; desktop, kanban, subagents):
    tokens sent -31.2% -> -35.3%, modelled cost -27.1% -> -28.6%, the same in both halves of the set; on GPT-6.1
    sessions -30.5% -> -42.8% tokens, on kanban runs -23.4% -> -38.9%, on subagents -0.4% -> -9.8%. Confirmed
    on 81 other sessions: -34.2% -> -35.0% cost, -39.3% -> -40.9% tokens.
  - Claude Code, replay of 35 real sessions at the 1-hour cache price: modelled cost -12.6% -> -14.8%, tokens
    -13.4% -> -17.2% (size-weighted); sessions of 107-130 requests go from untouched to -11%..-32%; none costs
    more than without CMI.
  - Tune with `CMI_WARMUP_REQUESTS` / `CMI_PROXY_WARMUP_REQUESTS`.
- Fix: a forced refresh ("the turn needs this result in full") no longer replaces a result that was already
  sent in full. A later pass (duplicate collapse) could shorten such a result, and the forced refresh sent that
  stub outside the payback rule: on a real Claude Code session, 195K tokens of cache were re-written to remove
  59, as soon as the start-up phase ended. Replay of 7 real sessions on their own clock: none worse, one from
  -10.7% to -14.1% cost, one from -19.1% to -19.9%. The opt-in `CMI_SUPERSEDED_REFRESH` keeps replacing an
  obsolete read sent in full (its full text is wrong once the file was edited).
- `bench/claude_code_replay.py` replays each session on its own clock and reads the cache state Claude Code
  recorded. A pause longer than the cache lifetime, or a request recorded as re-writing its prompt, loses the
  cache for both streams. Without the clock the bench never saw the re-compile CMI gets for free after a pause
  and under-stated the result (largest session: -17.6% cost without the clock, -25.7% with it). New output:
  `cold_idle`, `cold_other`, `by_request_rank`, `replayed_share_of_billed_input`. `CMI_REPLAY_CLOCK=0` gives
  the old behaviour.
- Token estimate: a base64 PDF (`document` block) is priced per page (about 2K tokens) instead of by its base64
  length. A 4 MB PDF weighed about 1M tokens. Pages packed in compressed object streams (PDF 1.5+) are
  counted, each page once even when an incremental update rewrites it; 19 of 19 real PDFs match their
  true page count.
- Shell-command parsing is cached per command text (it was about 40% of the compile time on a 208-request
  session).
- Proxy: the session key is computed in one pass (the system prompt and a first message that may carry a large
  attachment were serialized twice per request), and the request sizes in the log come from the bytes already
  at hand instead of serializing both bodies again.
- `install-service`: the backups of editor/agent settings it replaces and the log directory are owner-only
  (0700 directories, 0600 files); settings files may hold keys.
- Store (Windows): when several writers store the same content at once, the same-content check retries while
  Windows reports the file busy, instead of giving up on shortening that result.
- `bench/public_demo.py` runs with the shipped settings (it used to switch the start-up phase off) and has two
  2-hour pauses, longer than the provider cache lifetime: 28% not sent, 7/7 stubs recalled exactly. Its old
  3% came only from the forced-refresh defect fixed above.

## 1.11.5 (2026-11-10)

- Proxy (Claude Code, Codex): waits 150 requests (was 100) before rewriting the cache and uses a stricter payback
  horizon (0.2). Replay of 14 real Claude Code sessions at the 1-hour cache price: 4 sessions cost more
  (+3% to +12%) before, none meaningfully after (worst +0.3%); sessions under 150 requests are left untouched.
  Hermes keeps its own settings. Tune with `CMI_PROXY_WARMUP_REQUESTS` / `CMI_PROXY_TIER_LINDY`.
- Shortened results keep one flagged line per distinct error template, with a repeat count, so repeated errors
  no longer hide a different failure.
- `bench/claude_code_replay.py` prices writes at 2x by default (`CMI_WRITE_RATIO`).
- Proxy: opt-in per-job tool allowlist. A job's launcher sets the header `x-cmi-tools-allow: read_file,terminal`
  and the proxy drops the other tool schemas (about 15k tokens of a 19k-token fixed prefix on short runs). No header
  means no change; it fails open (an allowlist matching nothing is ignored, tools already used or named by
  `tool_choice` are kept) and the header is never forwarded upstream. Not measured live yet.

## 1.11.4 (2026-11-10)

- Every user-facing text is in English by default (Hermes plugin catalog review): the `/cmi` command and
  savings report, the review plugin (`/cmi-review`, French alias `/relecture` kept) and the desktop
  console. French is opt-in: `CMI_LANG=fr` for commands, the console's stored `lang` setting for the UI.
- Cost model: a 1-hour cache write is priced at 2x the input price (was 1.25x, the 5-minute rate) in
  tier decisions and in the cost shadow. Claude Code requests a 1-hour cache.

## 1.11.3 (2026-11-10)

- Proxy: when the upstream stream breaks mid-response, the client connection is dropped at once, so the
  client retries immediately. Before, the proxy raised an uncaught error and the client could wait on a
  half-open socket (seen live: Claude Code retrying one request 43 times, about 15 minutes apart).
- Measured live on Claude Code (Sonnet 5.5, 103-file audit, one run per arm, same task): with CMI 11.5%
  less sent, 0 interruptions, but billed cost $5.16 vs $4.50 without (+15%: 642K vs 435K cache-write
  tokens, 111 vs 107 turns). Under investigation; stated in the README.

## 1.11.2 (2026-11-09)

- `cmi-proxy install-service` / `uninstall-service` / `status` now also run on Linux (systemd user unit,
  verified on Debian 12: install, restart after a crash, uninstall; refuses cleanly and changes nothing
  without `systemctl --user`) and Windows (Scheduled Task at logon, Startup-folder script if Windows
  refuses it; install/uninstall round trip runs in Windows CI under an administrator account, the
  no-admin case is not verified yet). VS Code settings are found at each OS's own path.
- Community: GitHub Discussions, contributing guide, PR template, code of conduct.
- CI: actions/checkout v7, actions/setup-python v7.

## 1.11.1 (2026-11-09)

- Files the user names in the current message stay whole within a budget (`CMI_HOT_FILE_TOKENS`,
  default 32000; newest first, the newest always; fixed for the whole turn so the prompt cache holds).
  A message listing 125 file paths used to keep every read raw: 0% saved on a 125-request live session,
  28.7% on its offline replay with the cap.
- Live: 6 Hermes sessions on Claude Sonnet 5.5, about 195 requests each: 49% fewer tokens sent,
  modelled cost −27%. Replay of the 26 real sessions with this version: tokens −28%, modelled cost −24%
  (the 1.11.0 table rounded −24.6% to −25%), 0 of 26 costlier.
- Publication gate: private names are read from a file outside the repository
  (`~/.config/cmi/private-terms.txt` or `$CMI_PRIVATE_TERMS`), required by the export script.

## 1.11.0 — first public release (2026-11-09)

Measured on 26 real coding sessions, replayed with the provider's cache expiry: 28% fewer tokens on
average (up to 42%), modelled API cost −25% on average (up to −36%), −25% on sessions of 100+ requests,
no session costing more (0 of 26). Waits of 10, 20 and 30 requests still left three short sessions costlier; 100 removes them. Start-up phase: no cache rewrite before 100 requests.
Fixed since the 2026-11-03 candidate: Codex tool outputs sent as a list of text blocks were never
compressed (0%); error lines (FATAL…) are kept in digests; two runs with the same first message no
longer share one engine; screenshots inside tool results are priced at their billed size (a real
session had +15% cost because they were counted as base64); the replay bench now covers every
compaction segment; cache tier horizon 0.5 → 0.35; start-up phase of 100 requests before any cache rewrite (short sessions no longer lose money); project auto-binding
accepts Hermes Desktop sources `local`/`gui` and survives the `gui`→`local` rename (memory was being
switched off mid-conversation); project memory: plural matching, optional publish `tags`, search
`cues` (strict per cue, fused), exact user quote indexed, and a labelled second search stage (`match: approximate`, tool calls only, never the automatic context) when the strict AND finds nothing; `cmi-proxy install-service` /
`uninstall-service` / `status` (macOS, reversible VS Code and Codex wiring); `cmi_review` skips
test-only diffs and logs whether flagged files were touched.
Tried and removed: shell-output cleaning (ANSI/progress/repeats), 0.03% of real Bash output.
Known limits: project memory recall on paraphrases is weak (strict word match: 0/21 and 0/12 on our
benches; 4.2% on LoCoMo). Replay numbers measure tokens and modelled cost, not answer quality.

## 1.11.0 candidate (2026-11-03)

Supported scope: context engine and proxy. Project memory, Console and reviewer are opt-in;
project memory is experimental (gates open, see RELEASE_CHECKLIST.md).

- Cost on short sessions (replay of 64 real sessions, cache-aware shadow): tiers priced as
  cache reads over a Lindy horizon (0.5 × requests so far), free re-tier after a provider cache
  expiry (TTL read from Hermes config / `cache_control`), superseded reads refreshed at the next
  tier instead of mid-cache (`CMI_SUPERSEDED_REFRESH=1` restores it). 11–30 requests +8.6% →
  +0.6%, 31–80 −8.0% → −20.7%, 81+ −30.0% → −33.8%. Cost shadow models cache expiry.
- Forks (Hermes background review) adopt the parent's renderings, so their first request reuses
  the parent's cached prefix (forks were ~22% of all cache writes). Not covered by the replay.
- Reproducible offline benchmark `bench/public_demo.py` (synthetic session, run in CI).
- Audit 2026-11-03 (`docs/audit-2026-11-03.md`): ruff in CI, advisor (`--advisor`) blocks pass
  through the proxy byte-for-byte, public vulnerability-reporting channel.

- **CMI Console** (Hermes Desktop plugin): accounts and real quota windows, cooldowns and
  errors, sessions and API-price cost per project, subscription savings, CMI savings.
  Allowlist serialisation + leak guard on every response; capture mode for screen sharing.
- Console: one-click switch between two named Anthropic accounts on the open conversation
  (reorder, clear that account's local backoff, interrupt the retry loop, re-select the same
  model); names survive OAuth token refresh; unnamed logins stay visible.
- Project memory (opt-in): sourced decisions/observations shared across model sessions,
  automatic Desktop project binding, topic catalogue, deterministic decision-candidate nudge.
- Proxy hardening: Host allowlist (DNS rebinding), browser Origin refused, token for recall,
  token mandatory off-loopback.
- Publication hygiene: private names and machine paths removed; `tests/test_public_hygiene.py`.

## 1.10.0

- Step aging for long autonomous turns; tier payback horizon tuned on 53 replayed sessions.
- `cmi_review` 0.1: optional second-model review of code-changing turns.

## 1.6 – 1.9

- Cross-session tool-output index (`cmi_search`, SQLite FTS5) and backfill.
- Installable package with `cmi-proxy`; Anthropic Messages, OpenAI Chat Completions and OpenAI
  Responses wire formats; host tool vocabularies (Claude Code, Codex, Cline).
- Recoverable images and write/edit payloads; credential guard for the store and index.
- Rerun/shell-write supersession, provider-priced cache tiers, cache-aware cost shadow, savings
  ledger and `/cmi` report.

## 1.1 – 1.5

- Recall of named files, quality floor, deterministic output, content-addressed recall store
  and `cmi_recall` tool.
