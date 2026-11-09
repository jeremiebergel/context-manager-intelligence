# Release status

## 1.11.0 (2026-11-09)

- [x] Code audit (`docs/audit-2026-11-03.md`), ruff and tests in CI.
- [x] Offline demo (`bench/public_demo.py`) and replay benches (`bench/cost_replay.py`, `bench/claude_code_replay.py`).
- [x] Clean single-commit export with a public-hygiene test (`scripts/export_public.sh`).
- [x] Reporting channel: GitHub private vulnerability reporting.
- [x] Hermes plugin catalog entry for `cmi`: pull request opened (NousResearch/hermes-agent #151887), `hermes plugins validate` ok at the pinned SHA; awaiting maintainer review.
- [x] Linux check (Docker, Debian, Python 3.12): install from GitHub, `status`, demo, 480 tests.
- [x] Windows: ported 2026-11-09 (O_BINARY on every data file, portable owner/mode checks, msvcrt locking for the project policy, retry on concurrent replace, LF checkout). Full suite runs in CI on windows-latest.
- [ ] Cursor, Windsurf, Copilot.

## Before project memory loses the "experimental" label

- [ ] Automatic capture of candidate facts, excluded from recall until confirmed.
- [ ] Recall on paraphrases measured on a public, offline benchmark (LoCoMo / LongMemEval subset).
- [ ] Measured token saving when a new session starts from memory, on several real projects.
- [ ] Staleness check when a cited file changes.
- [ ] Handoff between agents with an owner and a single claim.
