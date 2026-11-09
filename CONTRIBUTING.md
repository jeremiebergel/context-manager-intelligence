# Contributing to CMI

Thanks for helping. CMI changes what an agent sends to a model, so correctness and privacy come
before features.

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

Small, focused PRs with tests. Describe what you verified and how. Security issues: please do
not open a public issue — see [SECURITY.md](SECURITY.md).

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
