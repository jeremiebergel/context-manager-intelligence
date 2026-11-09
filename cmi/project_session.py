"""Portable opt-in adapter; not yet wired into the live Hermes engine.

The host must supply an explicit project identity, not infer it from LLM text.
A new snapshot is queried before each request. No LLM or embeddings are called.
"""
from __future__ import annotations

from pathlib import Path

from .project_memory import ProjectMemory


class ProjectSession:
    def __init__(self, path: Path, *, project: str, session: str, model: str):
        if not isinstance(project, str) or not project.strip():
            raise ValueError("Explicit project identity is required")
        self.project = project
        self.session = session
        self.model = model
        self.memory = ProjectMemory(path)

    def publish(self, topic, text, *, source, kind="observation", expected_revision=0):
        return self.memory.publish(self.project, topic, text, source=source,
                                   session=self.session, model=self.model, kind=kind,
                                   expected_revision=expected_revision)

    def prepare(self, messages, *, max_bytes=4000):
        """Append sourced data to the current user message, leaving the prefix unchanged.

        Stateless: inputs are the host's original history, never an earlier prepare output.
        Non-text user messages pass through unchanged in this first prototype.
        """
        for i in range(len(messages) - 1, -1, -1):
            m = messages[i]
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            text = m.get("content")
            if not isinstance(text, str):
                return messages
            context = self.memory.context(self.project, text, max_bytes=max_bytes)
            if not context:
                return messages
            result = list(messages)
            result[i] = dict(m, content=text + "\n\n" + context)
            return result
        return messages
