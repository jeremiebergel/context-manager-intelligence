---
name: Feature or plugin idea
about: Propose a change to the core or a plugin of the CMI ecosystem
labels: enhancement
---

**Problem:** what fails or costs too much today?

**Proposal:**

**Core or plugin?** The core stays deterministic and zero-LLM. Anything that calls a model
(routing, review, summarization) belongs in a separate opt-in plugin.

**How would we measure it?** (benchmark in `bench/`, test, before/after numbers)
