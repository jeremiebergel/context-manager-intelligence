"""Deterministic detector of durable-decision candidates (no LLM, no network).

A candidate is a short user approval that directly follows an assistant proposal which itself
contains an explicit CHOICE between alternatives or a standing RULE. A bare go-ahead on an action
("go", "je lance l'inspect ?") is not a decision worth remembering and is rejected: that is the
noise this module exists to avoid. It only proposes; publishing stays an explicit, sourced act.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_APPROVAL = re.compile(
    r"^\s*(go|ok|oui|yes|yep|d'accord|parfait|valid[eé]e?|on part (là-dessus|sur)|on prend|"
    r"vas-y|enchaine|c'est bon|nickel|top)\b", re.I)
_MAX_APPROVAL_CHARS = 400

# The proposal states what is kept AND what is dropped / chosen over something, or a rule.
_CHOICE = re.compile(
    r"(on (garde|prend|écarte|ne prend pas|abandonne|retient)|je (garde|recommande|tranche)|"
    r"ma (décision|recommandation)|décision\s*:|règle\s*:|on ne (prend|fait|garde) pas|"
    r"\b(écarter|reporter|abandonner|supprimer|retirer)\b.{0,40}\||"
    r"\|\s*(écarter|ne pas prendre|on ne prend pas)\s*\||"
    r"(plutôt que|au lieu de|à la place de)\b)", re.I | re.S)
# An action proposal ending in a question about launching something is not a decision.
_ACTION_QUESTION = re.compile(
    r"(je (le )?(lance|commence|fais|regarde|vérifie|corrige|synchronise|modifie|l'attaque)"
    r"[^?\n]{0,120}\?\s*$)", re.I)
_TAIL_CHARS = 1200


def _text(message: Dict[str, Any]) -> str:
    c = message.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def find_candidates(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return [{index, approval, proposal}] for approvals that confirm a choice or rule."""
    out: List[Dict[str, Any]] = []
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        text = _text(m).strip()
        if not text or len(text) > _MAX_APPROVAL_CHARS or not _APPROVAL.match(text):
            continue
        prev: Optional[str] = None
        for j in range(i - 1, -1, -1):
            p = messages[j]
            if not isinstance(p, dict):
                continue
            if p.get("role") == "user":
                break  # never pair an approval with an older exchange
            if p.get("role") == "assistant" and _text(p).strip():
                if len(_text(p)) > 200:
                    prev = _text(p)
                break  # the nearest assistant text is the proposal; too short = skip
        if not prev:
            continue
        tail = prev[-_TAIL_CHARS:]
        if not _CHOICE.search(tail):
            continue
        if _ACTION_QUESTION.search(tail.strip()[-220:]) and not re.search(r"(décision|règle)\s*:", tail, re.I):
            continue
        out.append({"index": i, "approval": text, "proposal": tail})
    return out
