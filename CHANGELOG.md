# Changelog

Unreleased work is listed first. Dates are YYYY-MM-DD.

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
