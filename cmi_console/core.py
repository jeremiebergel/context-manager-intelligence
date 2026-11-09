"""CMI Console core: host-neutral data shaping and the security guard.

Stdlib only, no Hermes import: every host adapter (Hermes today, VS Code / standalone later)
hands plain dicts to these functions and serves what they return.

Security contract (see cmi_console/SECURITY.md):
- Allowlist serialisation: only the fields named here ever leave the backend.
- Free text (labels, titles, error reasons) is truncated, stripped of control characters,
  email-masked and secret-redacted.
- ``guard_payload`` is the last gate before the wire: it refuses the whole response if any
  known credential value, or anything shaped like one, appears anywhere in it.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

SCHEMA_VERSION = 1
ACCOUNT_TAGS = ("perso", "pro", "pro2")  # user-chosen names, one Anthropic account each (account_prefs.TAGS mirrors it)

# Credential shapes (kept in sync with cmi/secrets.py; duplicated so this package installs alone).
_SECRET_RES = [re.compile(p) for p in (
    r"sk-ant-[A-Za-z0-9_\-]{20,}",
    r"(?<![A-Za-z0-9])sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{32,}",
    r"\bgh[pousr]_[A-Za-z0-9]{36,}",
    r"github_pat_[A-Za-z0-9_]{50,}",
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    r"\bAIza[0-9A-Za-z_\-]{35}\b",
    r"\bxox[abprs]-[A-Za-z0-9\-]{10,}",
    r"\b[rs]k_live_[A-Za-z0-9]{20,}",
    r"\b\d{8,10}:AA[A-Za-z0-9_\-]{33}\b",
    r"\bhf_[A-Za-z0-9]{34,}\b",
    r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----",
    r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{16,}",
    r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}",
)]
_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]*@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
# Known secrets shorter than this are not substring-checked (too many false positives).
_MIN_KNOWN_SECRET = 12


class LeakError(RuntimeError):
    """A response was about to carry credential material. Never include the match in the message."""


def contains_secret(text: str) -> bool:
    return bool(text) and any(rx.search(text) for rx in _SECRET_RES)


def mask_email(text: str) -> str:
    return _EMAIL_RE.sub(lambda m: f"{m.group(1)}•••@{m.group(2)}", text)


def safe_text(value: Any, max_len: int = 120) -> Optional[str]:
    """Free text fit for the UI: control/bidi chars stripped, emails masked, secrets redacted."""
    if value is None:
        return None
    text = _CTRL_RE.sub("", str(value)).strip()
    for rx in _SECRET_RES:
        text = rx.sub("[redacted]", text)
    text = mask_email(text)
    return text[: max_len - 1] + "…" if len(text) > max_len else text


def opaque_id(*parts: Any) -> str:
    """Stable, non-reversible key for joining rows in the UI (never a raw token or account id)."""
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def _num(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN -> None


def _int(value: Any) -> int:
    n = _num(value)
    return int(n) if n is not None else 0


def looks_opaque(value: str) -> bool:
    """A stored value worth guarding: long, no whitespace or path separators, letters AND digits.
    Keeps env-var names ('ANTHROPIC_TOKEN') and paths from tripping the guard on legitimate rows."""
    return (len(value) >= 16 and not any(c.isspace() for c in value) and "/" not in value[:1]
            and any(c.isdigit() for c in value) and any(c.isalpha() for c in value))


def guard_payload(payload: Dict[str, Any], known_secrets: Iterable[str] = ()) -> str:
    """Serialise *payload*; raise LeakError if it carries a known secret or a credential shape."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    for secret in known_secrets:
        if secret and len(secret) >= _MIN_KNOWN_SECRET and looks_opaque(secret) and (
                secret in body or json.dumps(secret, ensure_ascii=False)[1:-1] in body):
            raise LeakError("response blocked: contains a stored credential")
    if contains_secret(body):
        raise LeakError("response blocked: contains a credential-shaped string")
    return body


# --- Accounts ------------------------------------------------------------------------------

def shape_window(w: Dict[str, Any]) -> Dict[str, Any]:
    used = _num(w.get("used_percent"))
    return {
        "label": safe_text(w.get("label"), 60) or "window",
        "used_percent": None if used is None else max(0.0, min(100.0, round(used, 1))),
        "resets_at": safe_text(w.get("resets_at"), 40),
        "scope": "model" if w.get("scope") == "model" else "account",
    }


def shape_account(raw: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    """One credential-pool entry -> wire row. *raw* comes from the host adapter and may hold
    anything (tokens included); only the names below are copied."""
    now = time.time() if now is None else now
    cooldowns = []
    for model, until in sorted((raw.get("model_cooldowns") or {}).items()):
        until_f = _num(until)
        if until_f is not None and until_f > now:
            cooldowns.append({"model": safe_text(model, 80), "seconds_left": int(until_f - now)})
    reset = _num(raw.get("last_error_reset_at"))
    state = raw.get("state")
    return {
        "key": opaque_id(raw.get("provider"), raw.get("key_basis") or raw.get("identity") or raw.get("id")),
        "provider": safe_text(raw.get("provider"), 40),
        "label": safe_text(raw.get("label"), 60) or "unnamed",
        "auth_type": safe_text(raw.get("auth_type"), 30) or "unknown",
        "source": safe_text(raw.get("source"), 40),
        "priority": _int(raw.get("priority")),
        "status": safe_text(raw.get("last_status"), 30) or "ok",
        "state": state if state in ("ready", "limited", "unknown", "unavailable") else "unknown",
        "request_count": _int(raw.get("request_count")),
        "last_error_code": _int(raw.get("last_error_code")) or None,
        "last_error_reason": safe_text(raw.get("last_error_reason"), 160),
        "last_error_reset_in_s": int(reset - now) if reset and reset > now else None,
        "model_cooldowns": cooldowns,
        "windows": [shape_window(w) for w in (raw.get("windows") or [])][:8],
        "resets_at": safe_text(raw.get("resets_at"), 40),
        "tag": raw.get("tag") if raw.get("tag") in ACCOUNT_TAGS else None,
        "email": None,  # Hermes stores none; shown only if a provider exposes it legitimately.
    }


def shape_provider(raw: Dict[str, Any]) -> Dict[str, Any]:
    models = [safe_text(m, 80) for m in (raw.get("models") or []) if m][:60]
    return {
        "slug": safe_text(raw.get("slug"), 40),
        "name": safe_text(raw.get("name") or raw.get("slug"), 60),
        "is_current": bool(raw.get("is_current")),
        "authenticated": bool(raw.get("authenticated")),
        "strategy": safe_text(raw.get("strategy"), 30),
        "total_models": _int(raw.get("total_models")) or len(models),
        "models": models,
        "usage_supported": bool(raw.get("usage_supported")),
        "windows": [shape_window(w) for w in (raw.get("windows") or [])][:8],
        "limit_resets_at": safe_text(raw.get("limit_resets_at"), 40),
    }


# --- Sessions & projects --------------------------------------------------------------------

def project_name(root: Optional[str], home: str = "") -> str:
    if not root:
        return ""
    root = root.rstrip("/")
    return root.rsplit("/", 1)[-1] or root


def split_container_repos(rows: List[Dict[str, Any]], min_children: int = 3) -> None:
    """A git repo used as a container of many project folders (e.g. ~/work/<project>) would
    lump them all together: when its sessions run in >= *min_children* distinct first-level
    subfolders, group each session by that subfolder instead (in place)."""
    def child(r):
        root, cwd = r.get("project_root") or "", r.get("cwd") or ""
        if root and cwd.startswith(root.rstrip("/") + "/"):
            first = cwd[len(root.rstrip("/")) + 1:].split("/", 1)[0]
            return root.rstrip("/") + "/" + first if first and not first.startswith((".", "_")) else None
        return None

    kids: Dict[str, set] = {}
    for r in rows:
        c = child(r)
        if c:
            kids.setdefault(r["project_root"], set()).add(c)
    for r in rows:
        c = child(r)
        if c and len(kids.get(r["project_root"], ())) >= min_children:
            r["project_root"] = c


def shape_sessions(rows: List[Dict[str, Any]], now: Optional[float] = None, home: str = "",
                   recent_limit: int = 30) -> Dict[str, Any]:
    """Aggregate session rows (already filtered to the window by the adapter) per project."""
    now = time.time() if now is None else now
    projects: Dict[str, Dict[str, Any]] = {}
    providers: Dict[str, Dict[str, Any]] = {}
    c30: Dict[str, float] = {}
    recent = []
    for r in rows:
        root = r.get("project_root") or ""
        tokens = sum(_int(r.get(k)) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
        cost = _num(r.get("cost_usd")) or 0.0
        prov = safe_text(r.get("provider"), 40) or "unknown"
        last = _num(r.get("last_activity")) or _num(r.get("started_at")) or 0.0
        key = opaque_id("project", root) if root else "none"
        p = projects.setdefault(key, {"key": key, "name": safe_text(project_name(root), 60) or "",
                                      "path": safe_text(_home_relative(root, home), 160) or "",
                                      "sessions": 0, "tokens": 0, "cost_usd": 0.0, "last_active": 0.0,
                                      "providers": {}})
        p["sessions"] += 1
        p["tokens"] += tokens
        p["cost_usd"] += cost
        p["last_active"] = max(p["last_active"], last)
        pp = p["providers"].setdefault(prov, {"sessions": 0, "cost_usd": 0.0, "last": 0.0})
        pp["sessions"] += 1
        pp["cost_usd"] += cost
        pp["last"] = max(pp["last"], last)
        if last >= now - 30 * 86400:
            c30[prov] = c30.get(prov, 0.0) + cost
        pv = providers.setdefault(prov, {"provider": prov, "sessions": 0, "tokens": 0, "cost_usd": 0.0})
        pv["sessions"] += 1
        pv["tokens"] += tokens
        pv["cost_usd"] += cost
        recent.append({
            "key": opaque_id("session", r.get("id")),
            "title": safe_text(r.get("title"), 90) or "",
            "project": p["name"],
            "provider": prov,
            "model": safe_text(r.get("model"), 60),
            "source": safe_text(r.get("source"), 20),
            "last_active": last,
            "tokens": tokens,
            "cost_usd": round(cost, 4),
        })
    out_projects = sorted(projects.values(), key=lambda p: -p["last_active"])
    for p in out_projects:
        p["cost_usd"] = round(p["cost_usd"], 4)
        p["providers"] = [{"provider": k, "sessions": v["sessions"], "cost_usd": round(v["cost_usd"], 4),
                           "active_7d": now - v["last"] < 7 * 86400}
                          for k, v in sorted(p["providers"].items(), key=lambda kv: -kv[1]["sessions"])]
    recent.sort(key=lambda s: -s["last_active"])
    return {
        "projects": out_projects[:60],
        "cost_30d": {k: round(v, 4) for k, v in c30.items()},
        "providers": sorted(({**v, "cost_usd": round(v["cost_usd"], 4)} for v in providers.values()),
                            key=lambda v: -v["sessions"]),
        "recent": recent[:recent_limit],
        "total": len(rows),
        # Hermes records the provider per session, not which pooled credential served it.
        "account_attribution": "unavailable",
    }


def _home_relative(path: str, home: str) -> str:
    if home and path and (path == home or path.startswith(home.rstrip("/") + "/")):
        return "~" + path[len(home.rstrip("/")):]
    return path


# --- CMI savings ledger ---------------------------------------------------------------------

def shape_savings(rows: Iterable[Dict[str, Any]], now: Optional[float] = None,
                  price: Optional[Callable[[str, str], Optional[float]]] = None,
                  days: int = 30) -> Dict[str, Any]:
    """Summarise ``cmi_stats.jsonl`` rows: windows (today/7d/30d), per-day series, per-model."""
    now = time.time() if now is None else now
    start = now - days * 86400
    day0 = now - (now % 86400) - time.timezone
    windows = {"today": day0, "7d": now - 7 * 86400, "30d": start}
    agg = {k: {"requests": 0, "raw": 0, "out": 0, "cost_raw": 0.0, "cost_cmi": 0.0, "priced": True} for k in windows}
    series: Dict[int, Dict[str, int]] = {}
    models: Dict[str, Dict[str, Any]] = {}
    prices: Dict[tuple, Optional[float]] = {}
    recalls = lexical = 0
    for r in rows:
        t = _num(r.get("t")) or 0.0
        if t < start:
            continue
        raw, out, n = _int(r.get("raw")), _int(r.get("out")), _int(r.get("n"))
        m, p = str(r.get("m") or ""), str(r.get("p") or "")
        if (m, p) not in prices:
            prices[(m, p)] = price(m, p) if price else None
        ppm = prices[(m, p)]
        for k, since in windows.items():
            if t >= since:
                a = agg[k]
                a["requests"] += n
                a["raw"] += raw
                a["out"] += out
                if ppm is None:
                    a["priced"] = False
                else:
                    a["cost_raw"] += _int(r.get("cr")) * ppm / 1e6
                    a["cost_cmi"] += _int(r.get("cc")) * ppm / 1e6
        d = int((t - start) // 86400)
        s = series.setdefault(d, {"raw": 0, "out": 0})
        s["raw"] += raw
        s["out"] += out
        mm = models.setdefault(m or "?", {"model": safe_text(m or "?", 60), "requests": 0, "saved": 0, "raw": 0})
        mm["requests"] += n
        mm["raw"] += raw
        mm["saved"] += raw - out
        if t >= now - 7 * 86400:
            recalls += _int(r.get("rc"))
            lexical += _int(r.get("lx"))
    out_windows = {}
    for k, a in agg.items():
        has_cost = a["cost_raw"] > 0
        out_windows[k] = {
            "requests": a["requests"], "tokens_raw": a["raw"], "tokens_saved": a["raw"] - a["out"],
            "saved_pct": round((a["raw"] - a["out"]) / a["raw"] * 100, 1) if a["raw"] else None,
            "cost_raw_usd": round(a["cost_raw"], 2) if has_cost else None,
            "cost_cmi_usd": round(a["cost_cmi"], 2) if has_cost else None,
            "cost_partial": has_cost and not a["priced"],
        }
    return {
        "windows": out_windows,
        "daily": [{"day": i, "tokens_raw": series.get(i, {}).get("raw", 0),
                   "tokens_saved": series.get(i, {}).get("raw", 0) - series.get(i, {}).get("out", 0)}
                  for i in range(days)],
        "models": sorted(models.values(), key=lambda v: -v["saved"])[:12],
        "recalls_7d": recalls,
        "anticipated_7d": lexical,
    }


def envelope(host: Dict[str, Any], sections: Dict[str, Any], errors: Dict[str, str],
             now: Optional[float] = None) -> Dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "generated_at": time.time() if now is None else now,
        "host": host,
        "sections": sections,
        # Per-section failure reasons (short codes, no stack traces) so the UI can show
        # "unavailable" for one panel without hiding the others.
        "errors": {k: safe_text(v, 120) for k, v in errors.items()},
    }
