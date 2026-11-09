# Security and privacy boundaries

## Status and reporting

CMI is open-source software provided as is, not an audited security product.
No penetration test, multi-tenant certification or blanket confidentiality guarantee is
claimed. Do not deploy it with untrusted tenants or publish private session datasets.
Report suspected vulnerabilities privately through GitHub's **private vulnerability
reporting** ("Security" tab → "Report a vulnerability") on the public repository. Do not put
credentials, recall keys or private transcripts in public issues.

## Threat model

CMI assumes a trusted operator, trusted local host and trusted agent integration. It is
intended for a single operating-system user. Permissions and logical project scopes reduce
accidental disclosure; they are **not a sandbox against another process running as the same
user**, an administrator, malware, or a compromised host. A project ID, source hash or recall
key is an identifier, not an authorization credential. Cross-session recall and search under
one home can expose earlier work to agents using that home. Separate trusted users/workloads
with separate OS accounts and homes; do not treat a shared home as tenant isolation.

### Proxy boundary

The proxy defaults to `127.0.0.1`. Every request is gated before anything else:

- the `Host` header must be `localhost`, `127.0.0.1` or `::1` (or a name given with `--allow-host`),
  which blocks DNS-rebinding from a web page;
- any request carrying an `Origin` header (browsers always send it cross-origin) is refused;
- `GET /cmi/<key>` and `/cmi-health` require the `x-cmi-token` header when `--token` /
  `CMI_PROXY_TOKEN` is set;
- a non-loopback `--host` is refused unless a token is set;
- a malformed `content-length` is a 400.

This is a local guard, **not** a hardened network gateway: without a token, any local process able
to connect can still read a stored original whose key it knows (keys are content-addressed and
high-entropy, but are identifiers, not credentials). Forwarded `POST` requests are not
token-gated, so any local process can use the proxy as a relay to the configured upstreams
with its own credentials; the proxy adds none. There is no TLS: do not expose the port through public
interfaces, tunnels or reverse proxies to untrusted clients without an independently reviewed
access-control and TLS boundary.

The proxy forwards provider credentials and requests upstream. Choose and trust the upstream
endpoint, its transport, and the host/client logging configuration. CMI is not a credential
vault and does not remove sensitive material from the raw conversation or upstream requests.

### Persistence, retention and deletion

- Native Hermes stores live under the effective `HERMES_HOME` (normally `~/.hermes`);
  standalone stores use `CMI_HOME` (normally `~/.cmi`). The native host resolves its own home.
- `cmi_store/` holds text originals and supported image files; `cmi_index.db` is a separate
  SQLite search index. The recall store uses owner-only directory/file permissions on supported
  hosts, not encryption at rest. Paths and identifiers can themselves reveal private work.
- Recall pruning removes originals untouched for 30 days and applies a 512 MiB store cap.
  Deletion, pruning, disk failures and host integration can make old keys unresolvable.
  Tests of exact recall at one point do not promise permanent recovery.
- Project memory has additional database/evidence state and its own lifecycle. Do not assume
  recall-store pruning deletes project memory, host transcripts, telemetry or reviewer state.
  See [PROJECT_MEMORY.md](PROJECT_MEMORY.md) for its contract.
- Optional `cmi_review` persists pending findings under `HERMES_HOME/cmi_review/` and a ledger
  in `HERMES_HOME/cmi_reviews.jsonl`; these can contain paths, requests or findings about code.
- Backups, snapshots, SQLite journal/WAL files, external client logs, upstream provider retention
  and original host session history are outside recall-store deletion. There is no secure-erasure
  guarantee. Review and remove all applicable copies when handling a deletion request.

### Best-effort secret guard

Regex detection refuses recognized credential-bearing text for recall storage/indexing.
It can miss unfamiliar formats, split/encoded values, private business information and secrets
in screenshots or other images; false positives are also possible. `CMI_ALLOW_SECRETS=1`
disables the guard. Do not use that setting with sensitive data. The guard is not comprehensive
redaction and does not sanitize raw requests, all host stores, or every metadata field.
Review tool outputs before allowing an agent to persist them; never rely on the claim that
credentials cannot reach disk. Rotate exposed credentials rather than relying on deletion.

### Untrusted content and poisoning

Tool output, retrieved web pages, source files and user-submitted evidence may contain prompt
injection, false assertions or malicious instructions. Remembering a quote and its source
proves provenance, **not truth or authority**. A model interpretation remains a reported
claim; retrieval must not promote it to a trusted system instruction. Conflicts, corrections,
project-boundary mistakes and source poisoning require validation. Logical project scoping is
not an adversarial authorization boundary. See [PROJECT_BINDING.md](PROJECT_BINDING.md).

### Optional reviewer: extra model calls and disclosure

The memory/context compile path does not require an auxiliary LLM. The separately enabled
`cmi_review` plugin does: it sends changes and the user request to a configured reviewer model,
may consume paid API tokens or subscription quota, and saves findings locally. Its best-effort
secret filtering is not a confidentiality guarantee. Check provider policies, configuration,
limits and whether the code/request may leave the host before registering/enabling it.
Live model benchmarks also make provider calls; distribution/unit tests do not require them.

## Release conditions

The public repository is published from a fresh single-commit export (no development history);
`tests/test_public_hygiene.py` guards it against private names and machine paths. Project memory
remains experimental: its native acceptance gates are open. See
[RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) and [PROVENANCE.md](PROVENANCE.md).
