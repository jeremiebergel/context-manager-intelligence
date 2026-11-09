"""Account names (perso / pro) chosen by the user: the only file CMI Console owns.

``$HERMES_HOME/cmi_console_accounts.json`` (0600, atomic) maps an opaque account key to a name.
No credential, email or path is stored. Names are display-only; the one action that changes
Hermes state is ``make_first`` (hermes_adapter), which uses Hermes' own ``move_entry``.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Dict, Optional

TAGS = ("perso", "pro", "pro2")  # mirrors core.ACCOUNT_TAGS
_KEY_RE = re.compile(r"^[0-9a-f]{12}$")
_lock = threading.Lock()


class PrefsError(ValueError):
    pass


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _path() -> Path:
    return _home() / "cmi_console_accounts.json"


def _clean(raw) -> Dict[str, str]:
    out = {}
    for k, v in (raw or {}).items():
        if isinstance(k, str) and _KEY_RE.match(k) and v in TAGS:
            out[k] = v
    return out


def load_tags() -> Dict[str, str]:
    """Read path (display): unreadable or corrupt means 'no names', never an exception."""
    try:
        return _clean(json.loads(_path().read_text("utf-8")).get("tags"))
    except (OSError, ValueError, AttributeError):
        return {}


def _load_strict() -> Dict[str, str]:
    """Write path: missing is empty, but corrupt/unreadable aborts (backed up) instead of wiping."""
    path = _path()
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise PrefsError("accounts file unreadable; not overwriting") from exc
    try:
        tags = json.loads(text)["tags"]
        if not isinstance(tags, dict):
            raise TypeError
        return _clean(tags)
    except (ValueError, KeyError, TypeError):
        try:
            bak = path.with_name(path.name + ".corrupt")
            if not bak.exists():
                bak.write_text(text, "utf-8")
                os.chmod(bak, 0o600)
        except OSError:
            pass
        raise PrefsError("accounts file invalid; backed up as .corrupt, not overwriting")


PENDING_TTL_S = 1800


def _read_pending() -> dict:
    try:
        pend = json.loads(_path().read_text("utf-8")).get("pending")
    except (OSError, ValueError, AttributeError):
        return {}
    import time as _t
    if (isinstance(pend, dict) and pend.get("tag") in TAGS and isinstance(pend.get("baseline"), list)
            and _t.time() - float(pend.get("at") or 0) < PENDING_TTL_S):
        return {"tag": pend["tag"], "baseline": [k for k in pend["baseline"] if isinstance(k, str) and _KEY_RE.match(k)]}
    return {}


def load_prices() -> Dict[str, float]:
    """Monthly subscription per name (EUR), typed by the user. Display only."""
    try:
        raw = json.loads(_path().read_text("utf-8")).get("prices") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    return {k: float(v) for k, v in raw.items() if k in TAGS and isinstance(v, (int, float)) and 0 <= v <= 10000}


def set_price(tag: str, eur) -> Dict[str, float]:
    if tag not in TAGS or isinstance(eur, bool) or not isinstance(eur, (int, float)) or not 0 <= eur <= 10000:
        raise PrefsError("invalid price")
    with _lock:
        cur = _load_strict()
        prices = load_prices()
        prices[tag] = round(float(eur), 2)
        _write(cur, _read_pending(), prices)
        return prices


NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,40}$")


def load_names() -> Dict[str, str]:
    """Free display name per slot (perso/pro/pro2) chosen by the user. Display only."""
    try:
        raw = json.loads(_path().read_text("utf-8")).get("names") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    return {k: v.strip() for k, v in raw.items() if k in TAGS and isinstance(v, str) and NAME_RE.match(v.strip() or "\n")}


def set_name(tag: str, name: str) -> Dict[str, str]:
    name = (name or "").strip()
    if tag not in TAGS or (name and not NAME_RE.match(name)):
        raise PrefsError("invalid name")
    with _lock:
        cur = _load_strict()
        names = load_names()
        if name:
            names[tag] = name
        else:
            names.pop(tag, None)
        _write(cur, _read_pending(), None, names)
        return names


def _write(tags: Dict[str, str], pending: dict, prices: Optional[Dict[str, float]] = None,
           names: Optional[Dict[str, str]] = None) -> None:
    if prices is None:
        prices = load_prices()
    if names is None:
        names = load_names()
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cmi_console_accounts.")
    try:
        doc = {"schema": 1, "tags": tags, "prices": prices, "names": names}
        if pending:
            import time as _t
            doc["pending"] = {"tag": pending["tag"], "baseline": pending["baseline"], "at": _t.time()}
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=1, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def start_reconnect(tag: str, baseline: list) -> None:
    """Remember 'the next new login is <tag>' (30 min). Display data only: opaque keys, no secret."""
    if tag not in TAGS or any(not _KEY_RE.match(str(k)) for k in baseline):
        raise PrefsError("invalid request")
    with _lock:
        _write(_load_strict(), {"tag": tag, "baseline": list(baseline)})


def pending() -> dict:
    return _read_pending()


def claim(keys: list, tag: str) -> Dict[str, str]:
    """Name *keys* as *tag* and clear the pending marker."""
    with _lock:
        cur = _load_strict()
        for k in keys:
            if _KEY_RE.match(k):
                cur[k] = tag
        _write(cur, {})
        return cur


def set_tags(updates: Dict[str, str]) -> Dict[str, str]:
    for k, v in updates.items():
        if not _KEY_RE.match(str(k)) or v not in TAGS:
            raise PrefsError("invalid key or name")
    with _lock:
        cur = _load_strict()
        cur.update(updates)
        _write(cur, _read_pending())
        return cur
