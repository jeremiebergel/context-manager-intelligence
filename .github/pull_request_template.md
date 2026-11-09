## What

<!-- One change per PR. Link the issue or discussion: Fixes #123 -->

## Why

## How

## Verified with

<!-- Commands you ran and what they returned. Numbers in README/docs must come from a script in bench/. -->

- [ ] `CMI_STANDALONE=1 python -m pytest -q` passes
- [ ] `ruff check .` passes
- [ ] A test covers the new rule, including the case where it must *not* fire
- [ ] No credentials, private transcripts, machine paths or private project names added
- [ ] Core stays zero-LLM and deterministic (same input -> same bytes)
- [ ] I read the code I am changing (AI-assisted is fine, see CONTRIBUTING.md)
