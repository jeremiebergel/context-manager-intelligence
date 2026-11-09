<h1 align="center">CMI — Context Manager Intelligence</h1>

<p align="center"><b>Make your AI coding plan last longer.</b><br>
Free, open source, and it runs on your own computer.</p>

<p align="center">
<img alt="license MIT" src="https://img.shields.io/badge/license-MIT-blue">
<img alt="python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue">
<img alt="LLM calls: 0" src="https://img.shields.io/badge/LLM%20calls-0-brightgreen">
<img alt="about 20 ms per request" src="https://img.shields.io/badge/overhead-~20%20ms%2Freq-brightgreen">
<a href="https://github.com/jeremiebergel/context-manager-intelligence/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/jeremiebergel/context-manager-intelligence/actions/workflows/ci.yml/badge.svg"></a>
<a href="https://github.com/jeremiebergel/context-manager-intelligence/discussions"><img alt="Discussions" src="https://img.shields.io/github/discussions/jeremiebergel/context-manager-intelligence?label=discussions"></a>
</p>

<p align="center"><a href="https://github.com/jeremiebergel/context-manager-intelligence/releases/download/v1.11.2/cmi-demo-16x9.mp4"><img alt="CMI in 60 seconds" src="docs/demo-video.gif" width="100%"></a><br><sub>60-second demo, plays automatically · <a href="https://github.com/jeremiebergel/context-manager-intelligence/releases/download/v1.11.2/cmi-demo-16x9.mp4">MP4 version</a></sub></p>

## In plain words

When you work with an AI assistant like Claude Code or Codex, every new message sends the **whole
conversation** back to the AI: every file it opened an hour ago, every test it already ran, every old
screenshot. Most of that is no longer useful, but it still counts against your plan, again and again.
That is why long sessions hit the limit so fast.

CMI sits between you and the AI and **folds the old pages**. Recent work stays open. Old things become a
short note, like a sticky note on a folder. If the AI needs one of them again, it unfolds it and gets the
original back, exactly as it was. Nothing is thrown away.

## What you get

- **About 28% less sent to the AI on average**, up to 42% on a single session. On long sessions measured
  live, almost half (49%) less.
- **The same plan goes about 1.4× further.** You hit your usage limit later.
- **If you pay per use, a lower bill:** about 24% less on average, and none of the 26 sessions we measured
  cost more than without CMI.
- **It remembers your project** *(experimental, in Hermes Agent for now)*: decisions taken in one
  conversation can be found again in a later one, even after switching from Claude to ChatGPT. If a
  decision changes, the new version replaces the old one.

<sub>Measured on 26 real work sessions of the author, with Claude and GPT models. Your sessions will differ:
you can measure your own (see below).</sub>

## Is it safe?

- It runs **only on your computer**. Nothing is sent anywhere except to the AI you already use.
- It uses **no extra AI** to decide what to fold: plain rules, the same result every time.
- Your passwords and API keys pass through untouched; CMI never stores them.
- Free and open source (MIT licence): anyone can read the code.

## Does it work with my tools?

Claude Code (terminal and VS Code), Codex and Hermes Agent: yes, tested. Other assistants that let you
change their server address (Cline, Aider, OpenCode…) should work too. On macOS and Linux it installs in
two commands; Windows is in preview.

```bash
uv tool install "git+https://github.com/jeremiebergel/context-manager-intelligence"
cmi-proxy install-service
```

Then keep working as usual. `cmi-proxy uninstall-service` removes everything and puts your settings back.

---

# Technical details

Everything below is for developers: how it works, the full measurements and their method, installation
for every client, and troubleshooting.

<p align="center"><b>A coding agent re-sends the whole conversation on every request. CMI sends only what matters now.</b><br>
Local, deterministic, zero-LLM, about 20 ms per request. Nothing is lost: every shortened result comes back byte-exact.</p>

<table align="center">
<tr><td align="center"><b>28%</b><br>fewer tokens sent<br><sub>on average, up to 42%</sub></td>
<td align="center"><b>1.4×</b><br>further on the same<br>subscription budget</td>
<td align="center"><b>−24%</b><br>API cost on average<br><sub>up to −36%</sub></td>
<td align="center"><b>−25%</b><br>API cost on sessions<br>of 100+ requests</td>
<td align="center"><b>49%</b><br>fewer tokens sent<br><sub>live, 195-request sessions</sub></td></tr>
</table>
<p align="center"><sub>26 real coding sessions with Claude and GPT models, replayed request by request through CMI 1.11.1, weighted by tokens; plus 6 live Hermes sessions on Claude Sonnet 5.5. Method below.</sub></p>

<p align="center"><img alt="cmi-proxy status and the offline demo" src="docs/demo.gif" width="760"></p>

## Subscription: more work before you hit the limit

On Claude Pro/Max and ChatGPT/Codex plans you do not pay per token, you run into a usage limit. CMI
cuts what each request carries, so the same limit covers more work and the context window fills up later:

- **28% fewer tokens sent on average, up to 42% on a single session** — the same token budget goes about **1.4× further** (up to 1.7×).
- On sessions of 100+ requests: **29% fewer tokens on average**, 38% at best.
- **Live, on long sessions: 49% fewer tokens sent** (6 Hermes sessions of about 195 requests each on Claude Sonnet 5.5, 188M tokens: 48% to 55% per session).

## API: a lower bill

With prompt caching, shortening old content only pays if the cache rewrite is repaid by later requests.
CMI prices every rewrite against the provider's cache rules and acts only when it pays back, and it
waits 100 requests before rewriting a warm cache.

- **−24% modelled cost on average, up to −36%** on the best session.
- **−25% on average on sessions of 100+ requests**, every one of them cheaper; **−27%** on the 6 live long sessions.
- **No session costs more**: 17 of 26 are cheaper, the other 9 (all short) are left untouched at 0%. A short session is only shortened when the provider cache has already expired, because rewriting it is then free.

<p align="center"><img alt="Tokens sent with and without CMI" src="docs/tokens-with-without.svg" width="760"></p>

<p align="center"><img alt="API cost with CMI vs without, per real session" src="docs/cost-per-session.svg" width="760"></p>

<p align="center"><img alt="The longer the session, the more CMI saves" src="docs/cost-vs-length.svg" width="760"></p>

## Install in two minutes (macOS, Linux; Windows in preview)

```bash
uv tool install "git+https://github.com/jeremiebergel/context-manager-intelligence"   # or pipx / a venv
cmi-proxy install-service    # starts CMI in the background and wires VS Code (Claude Code + Codex)
cmi-proxy status             # is it running, and are VS Code / Codex pointed at it?
```

Open VS Code and work as usual: Claude Code and Codex now go through CMI. Every change is backed up in
`~/.cmi/backups`; `cmi-proxy uninstall-service` restores everything byte for byte. The service listens
on `127.0.0.1` only, uses about 13 MB and 0% CPU when idle.

**Any other agent** (Cline, Aider, OpenCode, Claude Code or Codex in a terminal, your own scripts): start
`cmi-proxy --port 8787` and point the client's base URL at `http://127.0.0.1:8787`
(`ANTHROPIC_BASE_URL` for Anthropic clients; a provider entry for Codex). See
[docs/REFERENCE.md](docs/REFERENCE.md#install). **Hermes Agent** also has a native plugin with the same
engine: `hermes plugins install jeremiebergel/context-manager-intelligence/cmi`.

## What it does for you

- **Keeps what matters.** Recent work, files you name, and error lines (`FATAL`, `Traceback`) stay in full.
- **Shortens what is old.** Stale reads and long outputs become a short summary with a recall key.
- **Loses nothing.** The original is stored first and returns byte-exact through `cmi_recall`.
- **Protects your cache.** Same input, same bytes: your provider cache stays warm.
- **Stays private.** Zero LLM calls, zero network, zero telemetry; local only (`127.0.0.1`).

## Measured results

| Measure (26 real sessions, 636M tokens) | Average | Best |
|---|---:|---:|
| Tokens sent | **−28%** (median −22%) | **−42%** |
| Same subscription budget goes | **1.4×** further | 1.7× |
| Modelled API cost, all sessions | **−24%** (median −21%) | **−36%** |
| Sessions of 100+ requests (15): tokens | **−29%** | −38% |
| Sessions of 100+ requests (15): modelled API cost | **−25%** | −34% |
| Sessions costing more than without CMI | **0 of 26** | |
| Live: 6 Hermes sessions of ~195 requests (Sonnet 5.5): tokens | **−49%** | −55% |
| Live: same sessions, modelled API cost | **−27%** | −32% |
| Integrity | every shortened result recalled byte-exact, 0 invariant violations | |
| Overhead | about 20 ms per request | |

**Method.** Real coding sessions with Claude Opus/Sonnet and GPT-6 models across several projects,
replayed request by request through CMI 1.11.1, with the provider's 5-minute cache expiry taken into
account (`bench/cost_replay.py`; the proxy path is replayed by `bench/claude_code_replay.py`).
"Modelled cost" is CMI's own accounting of the provider's cache pricing, not a bill, and providers do
not publish how a token counts against a plan: we report tokens sent, not a number of messages. These
numbers measure tokens and modelled cost, not answer quality. Replay your own sessions to get your own figures.

**Live sessions (2026-11-09).** A real Hermes agent on Claude Sonnet 5.5, one task: read 191 project
files one by one, then answer questions about them (about 195 requests per session, 6 sessions, each in
a fresh home). Tokens sent are counted by CMI on the actual requests. Every agent finished the task;
when it needed a file it had received shortened, it got it back in full with `cmi_recall`. A read-only
audit is a favourable case (many large, old file reads); these sessions had no run without CMI, so they
show what CMI removes, not a difference in answer quality.

Tested: Claude Code (CLI and VS Code extension), Codex CLI and Hermes on macOS; install, `cmi-proxy status`,
the offline demo and the full test suite on Linux (Debian, Python 3.12). `install-service` uses a LaunchAgent on
macOS, a systemd user unit on Linux (install, restart after a crash and uninstall verified on Debian 12) and a
logon Scheduled Task on Windows (install round trip run in Windows CI as an administrator; the no-admin case is not verified yet); see
[docs/REFERENCE.md](docs/REFERENCE.md#install). Windows is covered by the test suite in CI on every push
(project memory included; file privacy relies on the NTFS permissions of your user profile instead of POSIX
modes). Not tested: Cursor, Windsurf, Copilot, the extensions' own UI. Status: **1.11.2**; the context engine and proxy are the supported scope.

## Project memory across sessions and models *(experimental, on by default in Hermes)*

In Hermes, sessions opened inside one of your projects share a local memory: one session writes a
decision with your exact words as proof, another session on another model or provider reads it back, and a
correction replaces the old revision. Tested live across two providers and across a model switch in one
conversation; in a paired test it found decisions that exist only in past conversations (3 of 3 runs)
where the same agent without memory found none (0 of 3), and it added nothing when a status file already
held the answer. It is word-based search (plus synonyms the model supplies), so a question that shares no
word with the stored fact can still miss, and nothing is captured unless a model publishes it. Turn it off
with `context.cmi.auto_project_binding: false`. See [PROJECT_MEMORY.md](PROJECT_MEMORY.md).

## Check it on your own sessions

```bash
git clone https://github.com/jeremiebergel/context-manager-intelligence && cd context-manager-intelligence
python bench/claude_code_replay.py            # replays your Claude Code sessions from ~/.claude/projects
```

It prints, per session, tokens not sent, modelled cost change and integrity checks. Nothing is sent
anywhere and no model is called.

## Troubleshooting

- **Claude Code or Codex stopped answering:** the service is down while the setting is still in place. Run
  `cmi-proxy status`; if it says not running, `cmi-proxy install-service` restarts it, or
  `cmi-proxy uninstall-service` removes the setting.
- **`status` says "not pointed":** reload VS Code (`Developer: Reload Window`) and open a new conversation;
  a conversation started before the install keeps its old settings.
- **No savings on a short session:** expected. CMI does not rewrite a warm cache before 100 requests.
- **Something odd?** Open an issue with the output of `cmi-proxy status`.

## Safety

Local only (`127.0.0.1`, foreign `Host` headers refused); your credentials pass through and are never
read or stored; secrets are detected and never written to the recall store; files are private (0600).
Shortened contents are kept in plain files under `~/.cmi/cmi_store` (or your Hermes home) so they can be
restored; any program that can read your home folder can read them. Details in [SECURITY.md](SECURITY.md).

## Community

CMI is young and built in the open. The most useful things you can do right now:

- **Try it and post your numbers** in [Show and tell](https://github.com/jeremiebergel/context-manager-intelligence/discussions/categories/show-and-tell),
  including sessions where it did nothing or cost more: those are the ones we learn from.
- **Ask anything** in [Q&A](https://github.com/jeremiebergel/context-manager-intelligence/discussions/categories/q-a);
  propose features in [Ideas](https://github.com/jeremiebergel/context-manager-intelligence/discussions/categories/ideas).
- **Pick a [good first issue](https://github.com/jeremiebergel/context-manager-intelligence/labels/good%20first%20issue)**:
  Windows testers, new clients (Cursor, Aider…), Linux distributions, docs.
- Read [CONTRIBUTING.md](CONTRIBUTING.md) before a large PR, and the [code of conduct](CODE_OF_CONDUCT.md).

If CMI saves you tokens, a ⭐ helps other people find it.

## More

[Reference](docs/REFERENCE.md) · [Roadmap](ROADMAP.md) · [Changelog](CHANGELOG.md) ·
[Contributing](CONTRIBUTING.md) · MIT licence
