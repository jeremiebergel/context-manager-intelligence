"""Credential detection. CMI never writes a credential to disk: content that contains one is
refused by the store, so it is neither shortened (it stays raw in the request, zero loss) nor
indexed. High precision first: a false positive only costs a missed shortening.

Each pattern is gated by a plain substring check, so ordinary text costs a few fast ``in`` scans.
Set CMI_ALLOW_SECRETS=1 to turn the guard off.
"""
from __future__ import annotations

import os
import re

_ENV_NAME = r"[A-Z0-9_]*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|ACCESS_KEY)[A-Z0-9_]*"
_OPAQUE = r"(?=[A-Za-z0-9/+_\-.]*[0-9])(?=[A-Za-z0-9/+_\-.]*[A-Za-z])[A-Za-z0-9/+_\-.]{16,}"

# (substrings, any of which must be present; pattern).
_RULES = [
    (("sk-ant-",), r"sk-ant-[A-Za-z0-9_\-]{20,}"),                                   # Anthropic
    (("sk-",), r"(?<![A-Za-z0-9])sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}"),  # OpenAI
    (("ghp_", "gho_", "ghu_", "ghs_", "ghr_"), r"\bgh[pousr]_[A-Za-z0-9]{36,}"),    # GitHub
    (("github_pat_",), r"github_pat_[A-Za-z0-9_]{50,}"),
    (("AKIA", "ASIA"), r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),                            # AWS key id
    (("AIza",), r"\bAIza[0-9A-Za-z_\-]{35}\b"),                                      # Google
    (("xox",), r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),                                  # Slack
    (("k_live_",), r"\b[rs]k_live_[A-Za-z0-9]{20,}"),                                # Stripe live
    ((":AA",), r"\b\d{8,10}:AA[A-Za-z0-9_\-]{33}\b"),                                # Telegram bot
    (("hf_",), r"\bhf_[A-Za-z0-9]{34,}\b"),                                          # Hugging Face
    (("PRIVATE KEY-----",), r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),             # PEM keys
    (("eyJ",), r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{16,}"),  # JWT
]
_COMPILED = [(subs, re.compile(p)) for subs, p in _RULES]
# .env / config line assigning a secret-named variable a long opaque value (name case-insensitive).
_ENV_HINTS = ("key", "secret", "token", "passw")
_ENV_RE = re.compile(r"^[ \t]*(?:export[ \t]+)?(?i:" + _ENV_NAME + r")[ \t]*[=:][ \t]*[\"']?" + _OPAQUE, re.M)


def guard_enabled() -> bool:
    return os.environ.get("CMI_ALLOW_SECRETS") != "1"


def contains_secret(text: str) -> bool:
    if not text:
        return False
    for subs, rx in _COMPILED:
        if any(s in text for s in subs) and rx.search(text):
            return True
    low = text.lower()
    return any(h in low for h in _ENV_HINTS) and _ENV_RE.search(text) is not None
