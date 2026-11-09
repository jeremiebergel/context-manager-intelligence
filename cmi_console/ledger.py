"""Account ledger: which pooled account served each API request (opaque key only).

Hermes does not record the account per request. After every request the hook diffs the per-entry
``request_count`` that Hermes persists in auth.json (read-only): exactly one entry rose -> that
account; none or several (parallel sessions) -> unattributed, never guessed.
File: $HERMES_HOME/cmi_console_ledger.jsonl (0600): time, opaque session hash, provider, model,
token counts, opaque account key. No credential, email, path or prompt text.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

_lock = threading.Lock()
_last: Dict[str, Dict[str, int]] = {}  # provider -> {account key: request_count}
MAX_BYTES = 8 * 1024 * 1024


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _opaque(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def _counts(provider: str) -> Dict[str, int]:
    """account key -> request_count, keyed exactly like the console's account keys."""
    from agent.account_usage_cache import _identity_id_for
    from agent.credential_pool import PooledCredential
    from hermes_cli.auth import read_credential_pool

    out: Dict[str, int] = {}
    for e in (read_credential_pool() or {}).get(provider) or []:
        try:
            ident = _identity_id_for(provider, PooledCredential.from_dict(provider, e))
        except Exception:
            ident = f"entry:{e.get('id', '')}"
        k = _opaque(provider, f"entry:{e['id']}" if e.get("id") else ident)  # same basis as the console key
        out[k] = out.get(k, 0) + int(e.get("request_count") or 0)
    return out


def attribute(provider: str) -> Optional[str]:
    now = _counts(provider)
    with _lock:
        before = _last.get(provider)
        _last[provider] = now
    if before is None:
        return None
    rose = [k for k, v in now.items() if v > before.get(k, 0)]
    return rose[0] if len(rose) == 1 else None


def on_post_api_request(**kw: Any) -> None:
    try:
        provider = str(kw.get("provider") or "")
        if provider not in ("anthropic", "openai-codex"):
            return
        usage = kw.get("usage") or {}
        row = {
            "t": round(time.time(), 1),
            "s": _opaque("session", kw.get("session_id")),
            "p": provider,
            "m": str(kw.get("model") or "")[:80],
            "in": int(usage.get("input_tokens") or 0),
            "out": int(usage.get("output_tokens") or 0),
            "cr": int(usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens") or 0),
            "k": attribute(provider),
        }
        path = _home() / "cmi_console_ledger.jsonl"
        with _lock:
            if path.exists() and path.stat().st_size > MAX_BYTES:
                os.replace(path, path.with_suffix(".jsonl.1"))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception:  # an observer must never break a request
        return


def summarize(now: float, days: int = 30) -> Dict[str, Any]:
    path = _home() / "cmi_console_ledger.jsonl"
    per: Dict[str, Dict[str, int]] = {}
    total = unattributed = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict) or now - float(r.get("t") or 0) > days * 86400:
                    continue
                total += 1
                k = r.get("k")
                if not k:
                    unattributed += 1
                    continue
                a = per.setdefault(str(k)[:12], {"requests": 0, "tokens": 0})
                a["requests"] += 1
                a["tokens"] += int(r.get("in") or 0) + int(r.get("out") or 0) + int(r.get("cr") or 0)
    except OSError:
        pass
    return {"installed": path.exists(), "days": days, "requests": total, "unattributed": unattributed, "accounts": per}
