# Contributing to CMI

Thanks for helping. CMI changes what an agent sends to a model, so correctness and privacy come
before features.

## Before you write code

| Change | What to do |
|---|---|
| Typo, docs, small bug fix with a test | Open the PR directly |
| New rule, new client, new platform, or more than a couple of files | Start a thread in [Discussions → Ideas](https://github.com/jeremiebergel/context-manager-intelligence/discussions/categories/ideas) or comment on the issue first |

A large PR nobody asked for will probably be closed, however good it is: a five-minute discussion
saves you hours. Issues labelled [`good first issue`](https://github.com/jeremiebergel/context-manager-intelligence/labels/good%20first%20issue)
are scoped and ready to take: comment "I'll take it" so nobody duplicates the work.

Not a coder? Running CMI on your own sessions and posting the numbers in
[Discussions → Show and tell](https://github.com/jeremiebergel/context-manager-intelligence/discussions/categories/show-and-tell)
(good or bad) is one of the most useful contributions: it is how we find the sessions where CMI loses.

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
CMI_STANDALONE=1 python -m pytest -q        # no Hermes needed
python console-ui/build.py --check          # console bundle matches its source
node tests/console_switch.test.mjs          # console UI + desktop shell harness (Node ≥ 18)
```

Inside a Hermes checkout, `CMI_HERMES_SOURCE=/path/to/hermes-agent` also runs the contract test
that checks every gateway RPC the console calls really exists.

## Rules

- **Python ≥ 3.10, standard library only** for `cmi/` (the proxy must install anywhere).
- **Never lose data silently.** Anything CMI shortens must stay recoverable (`cmi_recall`) or be
  kept raw. Add a test for every new shortening rule, including the case where it must *not*
  fire.
- **No secrets, no private data** in code, tests, fixtures or benchmark results: use
  `example.com`, `/home/dev/…`, synthetic projects. `tests/test_public_hygiene.py` enforces the
  basics; anonymise recorded results before committing them.
- **Console:** edit `console-ui/src/*.js`, then run `python console-ui/build.py`; never edit
  `cmi_console/desktop/plugin.js` by hand. Every value sent to the UI goes through the allowlist
  shapers and the leak guard (`cmi_console/core.py`); every write needs an explicit `intent` and
  a user confirmation. Update `cmi_console/SECURITY.md` when you add an action.
- **Claims need evidence.** Benchmarks and README numbers must come from a script in `bench/`
  with its recorded output, and state their limits.

## Pull requests

- **One change per PR**, with tests. Over about 800 changed lines, split it.
- Target `main`. CI runs on Linux, macOS (Python 3.10, 3.12, 3.13) and Windows; it must be green.
- Fill in the template: what, why, how, and what you ran to verify it.
- **AI-assisted code is welcome; unread code is not.** PRs that look like raw model output (bulk
  rewrites nobody asked for, changes across many files, code the author clearly has not read) are
  closed. You are responsible for every line you submit.
- **Privacy gate.** `tests/test_public_hygiene.py` rejects home paths and personal e-mails. Never
  paste real session transcripts, ids or project names into tests or bench results.
- Security issues: please do not open a public issue — see [SECURITY.md](SECURITY.md).
- By contributing you agree your work is released under the MIT licence.

## Maintenance and support

CMI is maintained by one person, on a best-effort basis.

- **Security reports** (private, via GitHub): acknowledged within 7 days; fixes for the proxy and
  the recall store take priority over everything else.
- **Bugs with a reproduction**: triaged within about two weeks. Issues without one may be closed.
- **Supported scope**: the context engine and the proxy, on the latest release. Project memory,
  the Console and the reviewer are experimental/opt-in and supported on a lower priority.
- **Hermes compatibility**: tracked against the latest stable Hermes Agent
  (`python -m cmi.hermes_contract`). Older Hermes versions are not patched.
- **New features**: start with an issue. Model-calling features go in separate plugins, never in
  the core.
