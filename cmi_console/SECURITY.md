# CMI Console — security model

The console shows account, quota and session data. It sits next to `auth.json`, which holds
live OAuth tokens and API keys in clear text. The design goal: **a compromised or buggy UI
cannot obtain a credential, and the console only changes what the user explicitly clicks and confirms.**

## Guarantees (each one is enforced by `tests/test_cmi_console.py`)

| # | Guarantee | How |
|---|---|---|
| 1 | **Explicit, confirmed actions only** | Quota and session data are read-only. Writes happen only after a user click + confirmation, each POST carrying a fixed `intent`: `/accounts/first` (`make-first`) reorders the chosen Anthropic account first through Hermes' own `move_entry` (same code as `hermes auth priority`) and, for a manual retry, clears **only that account's** local backoff via `reset_status` — no quota is ever "reset" at the provider; `/accounts/name`, `/accounts/price`, `/accounts/reconnect` write the plugin's own `cmi_console_accounts.json` (0600, atomic; opaque key → `perso`/`pro`, price, pending reconnect; no credential or email) — reconnect returns the `hermes auth` command for the user to run, it never logs in. `/accounts/add/begin` + `/accounts/add/finish` (`add-account`, `add-account-code`) run Hermes' own `hermes auth add anthropic` as a child process: the plugin relays the sign-in link and the one-time code the user pastes, never sees a token, keeps one login at a time (10-minute limit), and removes again — through Hermes' own pool — a login whose quota windows match an account already connected, so the same account is never added twice. `/providers/add/begin`, `/providers/add/status`, `/providers/add/key` (`add-provider`, `add-provider-status`, `add-provider-key`) do the same for the other providers through `hermes auth add <provider>`: the provider must be one Hermes lists, the optional account name is validated (≤ 40 printable chars, never a flag), OAuth providers are opened by Hermes itself and polled until it has saved the login (the plugin never sees a token), and an **API key** pasted by the user is the one secret that transits the plugin: it is passed once to the child (visible in the process list for a few seconds — Hermes has no non-interactive stdin path), never stored, logged or returned, and the leak guard still covers every response. Never a hand edit of `auth.json`. The desktop shell's **Switch to this account** then re-selects the *same* model on the focused Anthropic session (`config.set … --provider anthropic --session`), stopping a running turn first (`session.interrupt`); it never moves a non-Anthropic session to Claude and refuses a session owned by another profile/connection. `state.db` is opened `mode=ro`.
| 2 | **Allowlist serialisation** | `core.shape_account` / `shape_provider` / `shape_sessions` copy named fields only. A new field in `auth.json` never leaks by default. |
| 3 | **Leak guard on every response** | `core.guard_payload` serialises the final body and refuses it (HTTP 500 `leak_guard`, value never echoed or logged) if it contains any value stored under a `*token*`/`*key*`/`*secret*`/`*password*` field in the pool, any credential-ish env var, or a known credential shape (`sk-ant-`, `sk-`, `ghp_`, `AIza`, JWT, `Bearer …`, private keys, …). |
| 4 | **Sanitised free text** | Labels, titles, error reasons: control and bidi characters stripped, secrets redacted, emails masked (`j•••@domain`), length bounded. |
| 5 | **Opaque ids** | Accounts, sessions and projects are keyed by a truncated SHA-256; the raw pool id, token fingerprint and session id never leave the backend. |
| 6 | **Bounded client input** | GET takes only `refresh` (boolean). POST bodies are pydantic-typed: `tag` ∈ {`perso`,`pro`,`pro2`}, the one-time sign-in code (8–600 chars of `[A-Za-z0-9_#.~-]`), opaque 12-hex keys, a price within 0–10 000 €, a fixed `intent`. No path, file name or SQL fragment comes from the UI. |
| 7 | **Rate limiting** | Snapshots are cached 20 s; a forced refresh is honoured at most every 10 s. Provider quota calls are Hermes's own read-only usage refresh, throttled by Hermes (≥120 s per account). |
| 8 | **No HTTP caching** | `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`. |
| 9 | **UI has no sinks** | No `dangerouslySetInnerHTML`, `innerHTML`, `eval`, `Function`, `fetch`, `WebSocket`, `localStorage`, `window.open`. All data is rendered as React text. The only I/O is the shell-provided `api()` (Hermes: `ctx.rest`, authenticated by the dashboard session token). |
| 10 | **Opt-in twice** | The Python backend loads only when the plugin is in `plugins.enabled`; the desktop half is `defaultEnabled: false`. |
| 11 | **Capture mode** | One toggle blurs account labels, project names and session titles for screen sharing. |
| 12 | **Auditable build** | `console-ui/build.py` is ~50 lines of stdlib Python, no npm dependency; `build.py --check` fails CI when the shipped bundle drifts from source. |

## Out of scope / known limits

- **Local trust boundary.** Any process running as your user can already read `auth.json`.
  The console does not widen that: it adds no new listener (it rides the Hermes dashboard
  server and its session token) and returns no secret.
- **Email** is not stored by Hermes; the console shows "not stored" rather than calling a
  provider identity endpoint.
- **Account switching** is deliberately absent from this version. When added it will be a
  separate, explicit, confirmed action with an audit log entry — never automatic rotation
  across accounts, which can breach provider terms.

## Reporting

Report suspected leaks privately (see the repository `SECURITY.md`), never in a public issue.
