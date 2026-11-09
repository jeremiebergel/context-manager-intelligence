"""Hermes host adapter for the CMI Console. Read-only, except make_first and the add-account flow below.

Everything Hermes-specific lives here; ``core`` stays host-neutral. Sources, all read-only:
- ``auth.json`` credential pool via ``hermes_cli.auth.read_credential_pool`` (pure file read);
- the model-picker payload ``hermes_cli.inventory.build_model_options_payload`` (models, quota
  windows, per-account state). Opening it is what the model picker already does: it may start
  Hermes's own throttled (>=120 s), read-only usage refresh against the provider's usage API;
- ``state.db`` sessions, opened with SQLite ``mode=ro``;
- ``cmi_stats.jsonl`` (CMI savings ledger).

Nothing here writes to auth.json, config.yaml, state.db or any credential.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:  # loaded as a package (tests) or as a sibling file (plugin_api.py)
    from . import core  # type: ignore
except ImportError:  # pragma: no cover
    import core  # type: ignore

SESSION_WINDOW_DAYS = 365
_SECRET_FIELD_HINTS = ("token", "key", "secret", "password")


def _home() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _collect_secrets(obj: Any, out: Set[str], parent: str = "") -> None:
    """Every string stored under a credential-ish field name, for the leak guard."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            _collect_secrets(v, out, str(k).lower())
    elif isinstance(obj, list):
        for v in obj:
            _collect_secrets(v, out, parent)
    elif isinstance(obj, str) and any(h in parent for h in _SECRET_FIELD_HINTS) and len(obj) >= 12:
        out.add(obj)


def _ledger():
    try:
        from . import ledger  # type: ignore
    except ImportError:  # pragma: no cover
        import ledger  # type: ignore
    return ledger


def _prefs():
    try:
        from . import account_prefs  # type: ignore
    except ImportError:  # pragma: no cover
        import account_prefs  # type: ignore
    return account_prefs


def _claim_new_login(accounts: List[Dict[str, Any]]) -> None:
    """After the user pressed 'Reconnect <tag>' and logged in, name the new unnamed entries."""
    try:
        prefs = _prefs()
        pend = prefs.pending()
        if not pend:
            return
        base = set(pend["baseline"])
        new = [a for a in accounts if a["provider"] == "anthropic" and not a.get("tag") and a["key"] not in base
               and not str(a.get("source") or "").startswith("env:")]
        if new:
            prefs.claim([a["key"] for a in new], pend["tag"])
            for a in new:
                a["tag"] = pend["tag"]
    except Exception:  # display helper: never break the snapshot
        pass


def _inherit_names(accounts: List[Dict[str, Any]]) -> None:
    """A re-login adds a NEW pool entry for the same account (new token, no name). Give it the name
    of the one named account whose live quota windows are identical (display only, never saved).
    Ambiguous matches (several names share the numbers) are left unnamed for the user to pick."""
    def sig(a):
        ws = [(w.get("label"), w.get("used_percent")) for w in a.get("windows") or []]
        return tuple(ws) if ws and all(u is not None for _l, u in ws) else None

    for a in accounts:
        if a.get("tag") or a["state"] == "unavailable" or sig(a) is None:
            continue
        names = {b["tag"] for b in accounts if b.get("tag") and b["provider"] == a["provider"] and sig(b) == sig(a)}
        if len(names) == 1:
            a["tag"], a["tag_guess"] = next(iter(names)), True


def _infer_last_name(accounts: List[Dict[str, Any]]) -> None:
    """One unnamed Anthropic login left and exactly one name without any login: it is that one.
    Display only, never saved; with two or more candidates we do not guess."""
    anth = [a for a in accounts if a["provider"] == "anthropic" and a["state"] != "unavailable"
            and not str(a.get("source") or "").startswith("env:")]
    unnamed = [a for a in anth if not a.get("tag")]
    missing = [t for t in _prefs().TAGS if not any(a.get("tag") == t for a in anth)]
    if len(unnamed) == 1 and len(missing) == 1:
        unnamed[0]["tag"], unnamed[0]["tag_guess"] = missing[0], True


def make_first(tag: str, provider: str = "anthropic", *, retry: bool = False) -> Dict[str, Any]:
    """Put every entry named *tag* at the front of *provider*'s pool, via Hermes' own move_entry.

    Affects new sessions and the next rotation (a live session keeps its account until a quota
    error). Never edits auth.json by hand; never touches tokens."""
    prefs = _prefs()
    if tag not in prefs.TAGS or provider != "anthropic":
        raise prefs.PrefsError("invalid request")
    from agent.account_usage_cache import _identity_id_for
    from agent.credential_pool import load_pool

    tags = prefs.load_tags()
    pool = load_pool(provider)
    def named(e):
        ident = _identity_id_for(provider, e)
        basis = entry_key_basis(provider, {"id": getattr(e, "id", None)}, ident)
        return tags.get(core.opaque_id(provider, basis)) or tags.get(core.opaque_id(provider, ident))

    mine = [e for e in pool.entries() if named(e) == tag]
    if not mine:
        raise prefs.PrefsError("no account has this name")
    for i, e in enumerate(sorted(mine, key=lambda e: e.priority)):
        pool.move_entry(e.id, i)
        if retry:
            # Explicit user retry only: don't clear a sibling's backoff or the provider's quota.
            pool.reset_status(e.id)
    return {"moved": len(mine), "tag": tag}


USAGE_WAIT_S = 12.0
_add_state: Dict[str, Any] = {}
_add_lock = threading.RLock()  # one add-account flow at a time; guards _add_state and the whole finish sequence


def _anthropic_entry_ids() -> List[str]:
    from hermes_cli.auth import read_credential_pool

    rows = (read_credential_pool() or {}).get("anthropic") or []
    return [str(e["id"]) for e in rows if isinstance(e, dict) and e.get("id")]


def _entry_key(entry_id: str) -> str:
    return core.opaque_id("anthropic", entry_key_basis("anthropic", {"id": entry_id}, ""))


def _reset_signature(account: Dict[str, Any]) -> Set[Tuple[str, str]]:
    """(window, reset instant) pairs. Two logins of ONE Claude account report the same reset
    instants; two accounts never do, unless both are idle (no reset yet), which proves nothing."""
    return {(str(w.get("label")), str(w.get("resets_at"))) for w in account.get("windows") or []
            if w.get("resets_at") and w.get("scope") != "model"}


def add_account_begin(tag: str, name: str = "") -> Dict[str, Any]:
    """Start Hermes' own Claude login for the name *tag*; returns the link to open."""
    prefs = _prefs()
    name = (name or "").strip()
    if tag not in prefs.TAGS or (name and not prefs.NAME_RE.match(name)):
        raise prefs.PrefsError("invalid request")
    from . import login  # type: ignore

    with _add_lock:
        ids = _anthropic_ids_safe()
        started = login.start()
        _add_state.clear()
        _add_state.update(id=started["id"], tag=tag, ids=ids, name=name)
        return {"id": started["id"], "url": started["url"], "tag": tag}


def _anthropic_ids_safe() -> List[str]:
    try:
        return _anthropic_entry_ids()
    except Exception as exc:
        raise _prefs().PrefsError("credential pool unreadable") from exc


def add_account_finish(sid: str, code: str) -> Dict[str, Any]:
    """Give Hermes the pasted code. Then: a login for an account the pool already holds is
    removed again and reported as a duplicate; otherwise the new login is named *tag*."""
    prefs = _prefs()
    from . import login  # type: ignore

    with _add_lock:
        st = dict(_add_state)
        if not st or st.get("id") != sid:
            raise prefs.PrefsError("no login in progress")
        res = login.submit(sid, code)
        if not res.get("ok"):
            return {"status": "failed", "reason": res.get("reason") or "failed", "detail": res.get("detail") or ""}
        _add_state.clear()
        tag, before = st["tag"], set(st["ids"])
        new_ids = [i for i in _anthropic_entry_ids() if i not in before]
        if not new_ids:
            return {"status": "failed", "reason": "not_saved"}
        new_keys = {_entry_key(i) for i in new_ids}
        # Hermes reads the new login's quota in the background; wait briefly for it (never forever).
        deadline = time.time() + USAGE_WAIT_S
        while True:
            accounts, _s = read_providers_and_accounts(time.time())
            by_key = {a["key"]: a for a in accounts["accounts"] if a["provider"] == "anthropic"}
            fresh = [by_key[k] for k in new_keys if k in by_key]
            sig_new = set().union(*(_reset_signature(a) for a in fresh)) if fresh else set()
            if sig_new or time.time() >= deadline:
                break
            time.sleep(1.5)
        old = [a for k, a in by_key.items() if k not in new_keys]
        twin = next((a for a in old if sig_new & _reset_signature(a)), None)
        if twin is not None:
            _remove_anthropic_entries(new_ids)
            return {"status": "duplicate", "of": twin.get("tag")}
        prefs.claim(list(new_keys), tag)
        if st.get("name"):
            prefs.set_name(tag, st["name"])
        return {"status": "added", "tag": tag, "checked": bool(sig_new)}


_prov_state: Dict[str, Any] = {}


def _pool_ids(provider: str) -> List[str]:
    from hermes_cli.auth import read_credential_pool

    rows = (read_credential_pool() or {}).get(provider) or []
    return [str(e["id"]) for e in rows if isinstance(e, dict) and e.get("id")]


def provider_flows() -> Dict[str, str]:
    """slug -> sign-in flow ("link" | "key") for every provider Hermes knows, except Claude (own flow)."""
    from . import login  # type: ignore

    slugs = [str(r.get("slug")) for r in (_providers_payload().get("providers") or []) if r.get("slug")]
    return {s: login.flow_for(s) for s in slugs if s != "anthropic" and login.SLUG_RE.match(s)}


def _providers_payload() -> Dict[str, Any]:
    from hermes_cli.inventory import build_model_options_payload, load_picker_context

    return build_model_options_payload(load_picker_context())


def open_login(sid: str) -> Dict[str, Any]:
    from . import login  # type: ignore

    return {"opened": login.open_in_browser(sid)}


def add_provider_begin(provider: str, label: str = "") -> Dict[str, Any]:
    """Start the OAuth login of a non-Claude provider; returns the link (and code to type, if any)."""
    prefs = _prefs()
    from . import login  # type: ignore

    with _add_lock:
        if provider not in provider_flows() or login.flow_for(provider) != "link":
            raise prefs.PrefsError("invalid request")
        ids = _pool_ids(provider)
        started = login.start_link(provider, label)
        _prov_state.clear()
        _prov_state.update(id=started["id"], provider=provider, ids=ids)
        return {"id": started["id"], "url": started["url"], "user_code": started.get("user_code"), "provider": provider}


def add_provider_status(sid: str) -> Dict[str, Any]:
    prefs = _prefs()
    from . import login  # type: ignore

    with _add_lock:
        if _prov_state.get("id") != sid:
            raise prefs.PrefsError("no login in progress")
        res = login.status(sid)
        if res["state"] == "waiting":
            return {"status": "waiting"}
        st = dict(_prov_state)
        _prov_state.clear()
        if res["state"] != "done":
            return {"status": res["state"]}
        added = [i for i in _pool_ids(st["provider"]) if i not in set(st["ids"])]
        return {"status": "added" if added else "failed", "provider": st["provider"]}


def add_provider_key(provider: str, key: str, label: str = "") -> Dict[str, Any]:
    """Add an API-key credential for *provider* through Hermes' own ``auth add``."""
    prefs = _prefs()
    from . import login  # type: ignore

    with _add_lock:
        if provider not in provider_flows() or login.flow_for(provider) != "key":
            raise prefs.PrefsError("invalid request")
        before = set(_pool_ids(provider))
        res = login.add_key(provider, key, label)
        if not res.get("ok"):
            return {"status": "failed", "reason": res.get("reason") or "failed"}
        added = [i for i in _pool_ids(provider) if i not in before]
        return {"status": "added" if added else "failed", "provider": provider}


def _remove_anthropic_entries(entry_ids: List[str]) -> None:
    """Undo a login Hermes just added (same call as `hermes auth remove`)."""
    from agent.credential_pool import load_pool

    pool = load_pool("anthropic")
    for eid in entry_ids:
        index, matched, _err = pool.resolve_target(eid)
        if matched is not None and index is not None and matched.id == eid:
            pool.remove_index(index)


def entry_key_basis(provider: str, entry: Dict[str, Any], identity: str) -> str:
    """What an account's display key (and its perso/pro name) hangs on.

    The pool entry id survives OAuth token refreshes; the token fingerprint (``identity``) does
    not, so keying names on it made them vanish every time Hermes rotated the access token.
    Entries without an id fall back to the fingerprint."""
    eid = entry.get("id")
    return f"entry:{eid}" if eid else identity


def _identity(provider: str, entry: Dict[str, Any]) -> str:
    """Same identity Hermes uses for per-account usage slots (token fingerprint / Codex principal)."""
    try:
        from agent.account_usage_cache import _identity_id_for
        from agent.credential_pool import PooledCredential

        return _identity_id_for(provider, PooledCredential.from_dict(provider, entry))
    except Exception:
        return f"entry:{entry.get('id', '')}"


def read_providers_and_accounts(now: float) -> Tuple[Dict[str, Any], Set[str]]:
    from hermes_cli.auth import read_credential_pool
    from hermes_cli.inventory import build_model_options_payload, load_picker_context

    secrets: Set[str] = set()
    pool = read_credential_pool() or {}
    _collect_secrets(pool, secrets)

    ctx = load_picker_context()
    payload = build_model_options_payload(ctx)
    try:
        from agent.account_usage_cache import has_account_usage
    except Exception:  # pragma: no cover
        has_account_usage = lambda _slug: False  # noqa: E731
    try:
        from agent.credential_pool import get_pool_strategy
    except Exception:  # pragma: no cover
        get_pool_strategy = lambda _slug: None  # noqa: E731

    providers: List[Dict[str, Any]] = []
    usage_by_identity: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in payload.get("providers") or []:
        slug = str(row.get("slug") or "")
        usage = row.get("usage") or {}
        for acc in usage.get("accounts") or []:
            usage_by_identity[(slug, str(acc.get("id") or ""))] = acc
        limit = row.get("limit") or {}
        providers.append(core.shape_provider({
            "slug": slug, "name": row.get("name"), "is_current": row.get("is_current"),
            "authenticated": row.get("authenticated"), "total_models": row.get("total_models"),
            "models": row.get("models"), "usage_supported": has_account_usage(slug),
            "strategy": get_pool_strategy(slug) if pool.get(slug) else None,
            "windows": usage.get("windows") or [], "limit_resets_at": limit.get("resets_at"),
        }))

    single_windows = {p["slug"]: p["windows"] for p in providers}
    tags = _prefs().load_tags()
    accounts: List[Dict[str, Any]] = []
    for slug, entries in pool.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            identity = _identity(slug, entry)
            usage = usage_by_identity.get((slug, identity))
            basis = entry_key_basis(slug, entry, identity)
            # Names saved before keys moved to the entry id are still keyed by token fingerprint.
            tag = tags.get(core.opaque_id(slug, basis)) or tags.get(core.opaque_id(slug, identity))
            raw = {**entry, "provider": slug, "identity": identity, "key_basis": basis, "tag": tag}
            if usage:
                raw.update(windows=usage.get("windows"), state=usage.get("state"), resets_at=usage.get("resets_at"))
            elif len(entries) == 1 and single_windows.get(slug):
                raw.update(windows=single_windows[slug], state="ready")
            elif entry.get("last_status") == "dead":
                raw["state"] = "unavailable"
            accounts.append(core.shape_account(raw, now))
    _claim_new_login(accounts)
    _inherit_names(accounts)
    _infer_last_name(accounts)
    accounts.sort(key=lambda a: (a["provider"] or "", a["priority"]))
    active = {
        "provider": core.safe_text(payload.get("provider") or ctx.current_provider, 40),
        "model": core.safe_text(payload.get("model") or ctx.current_model, 80),
    }
    return {"active": active, "providers": providers, "accounts": accounts, "prices": _prefs().load_prices(), "names": _prefs().load_names(), "ledger": _ledger().summarize(now)}, secrets


def read_sessions(now: float) -> Dict[str, Any]:
    db = _home() / "state.db"
    if not db.exists():
        raise FileNotFoundError("state.db not found")
    since = now - SESSION_WINDOW_DAYS * 86400
    uri = f"file:{db.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=2.0)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(
            """SELECT id, source, title, model, billing_provider AS provider,
                      COALESCE(NULLIF(git_repo_root, ''), NULLIF(cwd, '')) AS project_root,
                      NULLIF(cwd, '') AS cwd,
                      started_at, last_activity_at AS last_activity,
                      input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                      COALESCE(actual_cost_usd, estimated_cost_usd) AS cost_usd
               FROM sessions
               WHERE COALESCE(hidden, 0) = 0 AND COALESCE(last_activity_at, started_at) >= ?
               ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT 5000""",
            (since,))
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        con.close()
    core.split_container_repos(rows)
    out = core.shape_sessions(rows, now, home=str(Path.home()))
    out["window_days"] = SESSION_WINDOW_DAYS
    return out


def _price(model: str, provider: str) -> Optional[float]:
    try:
        from agent.usage_pricing import get_pricing_entry

        e = get_pricing_entry(model, provider=provider or None)
        if e and e.input_cost_per_million is not None:
            return float(e.input_cost_per_million)
    except Exception:
        pass
    return None


def read_savings(now: float) -> Dict[str, Any]:
    path = _home() / "cmi_stats.jsonl"
    if not path.exists():
        return {"installed": False}

    def rows():
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue

    out = core.shape_savings(rows(), now, price=_price)
    out["installed"] = True
    return out


def host_info() -> Dict[str, Any]:
    version = profile = None
    try:
        # Same source as `hermes --version` (the install stamp's baseVersion can lag behind).
        from hermes_cli.version_info import get_version_info

        version = get_version_info().derived_version
    except Exception:
        try:
            import hermes_cli

            version = getattr(hermes_cli, "__version__", None)
        except Exception:
            pass
    try:
        from hermes_cli.profiles import get_active_profile_name

        profile = get_active_profile_name()
    except Exception:
        pass
    return {"name": "hermes", "version": core.safe_text(version, 30), "profile": core.safe_text(profile, 40)}


def build_snapshot(now: Optional[float] = None) -> Tuple[Dict[str, Any], Set[str]]:
    """Whole console payload plus the secret set the caller must guard against."""
    now = time.time() if now is None else now
    sections: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    secrets: Set[str] = set()
    try:
        sections["accounts"], secrets = read_providers_and_accounts(now)
    except Exception as exc:
        errors["accounts"] = type(exc).__name__
    for name, fn in (("sessions", read_sessions), ("savings", read_savings)):
        try:
            sections[name] = fn(now)
        except Exception as exc:
            errors[name] = type(exc).__name__
    # Env-provided credentials (e.g. ANTHROPIC_TOKEN) are guarded too, even if not in the pool.
    for k, v in os.environ.items():
        if any(h in k.lower() for h in _SECRET_FIELD_HINTS) and isinstance(v, str) and len(v) >= 12:
            secrets.add(v)
    return core.envelope(host_info(), sections, errors, now), secrets
