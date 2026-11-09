# Provenance, prior art and claims

## What this document does not certify

This document is a disclosure, not a completed originality, license or privacy audit.
CMI is not claimed to be the first agent memory system, the first retrieval system, or a
clean-room implementation. An MIT license states the distribution terms; it does not by itself
prove originality, clear every upstream obligation, or authorize disclosure of private data.
No exhaustive comparison of source trees or historical commits has been completed here.

## Prior-art context and inspiration

The design draws on established ideas: hierarchical/virtual memory, content-addressed stores,
working-set locality, reversible context reduction, full-text retrieval and source-linked
knowledge. These ideas are not presented as inventions of this repository.

Relevant comparison points include **MemGPT: Towards LLMs as Operating Systems** (arXiv
2310.08560), which describes virtual context management inspired by memory hierarchies, and
**Hermes Agent's persistent memory and session search**, the native host ecosystem. They are
prior-art context and architectural inspiration/comparison, not a claim that their code was
copied, a formal dependency inventory, or proof that CMI's implementation is independently
original. Consult the paper and official Hermes memory documentation for their actual designs:

- MemGPT paper: `https://arxiv.org/abs/2310.08560`
- Hermes persistent memory: `https://hermes-agent.nousresearch.com/docs/user-guide/features/memory`

CMI's intended contribution is the specific combination of deterministic request-only context
compilation, recoverable stored originals and source-grounded project memory shared across
models in the native host. That is an engineering goal to validate, not a world-first assertion.
Automatic host binding, useful natural contribution and cross-model native recall must be
demonstrated together before the central innovation is called complete. Manual commands and
offline context-trimming replay do not establish that result.

## Implementation and integration boundary

`cmi/` contains the context engine, portable adapters, local recall/index storage and project
memory integration. `compat.py` uses host interfaces when available and standalone fallbacks
otherwise; native compaction relies on Hermes's compressor. Review those integrations against
the intended host version. The standalone package declares no runtime dependencies; this is
not a claim that the optional native integrations have no upstream code or service obligations.
`cmi_review/` is separately enabled functionality with auxiliary model calls and a distinct
cost/privacy boundary. It is not part of the zero-LLM memory compile-path claim.

Before public distribution, inventory any copied/adapted code and dependencies, identify source
versions and licenses, retain required notices, and document AI-assisted or other contribution
provenance as verified. Do not infer authorship or a clean-room process from a file header or
package metadata. Unverified provenance must remain marked unverified.

## Evidence and privacy

README figures are historical pilot measurements of particular sessions and configurations,
not a fresh rerun here or a guarantee of quality, billing or quota savings. Test fixtures and
reproducible distribution checks establish bounded behavior, not universal correctness.
See [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) for the unfinished full native acceptance gate.

The existing repository is private. Historical commits can retain personal/work names,
absolute paths and session data even after current files are edited. A working-tree cleanup
or deleting a file in a later commit does not sanitize prior objects. Review the entire intended
release surface, including history, fixtures, benchmark inputs, artifacts and metadata. A
separate sanitized release snapshot may be appropriate, subject to owner approval and audit.
Do not flip the existing repository public or publish its artifacts before that decision.
This change does not rewrite Git history and does not authorize any release.
