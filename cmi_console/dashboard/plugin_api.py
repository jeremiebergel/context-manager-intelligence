"""CMI Console backend — routes mounted at /api/plugins/cmi_console/.

Security boundary (cmi_console/SECURITY.md):
- read-only except these POST routes: /accounts/name, /accounts/reconnect, /accounts/price (labels and
  prices in the plugin's own file); /accounts/first, which reorders the credential pool through Hermes' own
  move_entry; /accounts/add/begin + /accounts/add/finish, which run Hermes' own `hermes auth add anthropic`
  as a child process (the plugin never sees a token: it relays the login link and the one-time code, and
  undoes a login that turns out to be an account already in the pool). auth.json is never hand-edited;
  config and state.db are never written;
- every body passes ``core.guard_payload`` (known stored credentials + credential shapes);
  a hit returns 500 ``leak_guard`` and never echoes the offending value;
- no client-supplied path, filename or SQL ever reaches the readers;
- ``Cache-Control: no-store`` so the snapshot is not kept by any HTTP cache.
"""
from __future__ import annotations

import importlib.util
import logging
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter, Query
from pydantic import BaseModel
from fastapi.responses import Response

log = logging.getLogger(__name__)
router = APIRouter()

_PKG = "_cmi_console_pkg"
_MIN_REFRESH_S = 10.0
_CACHE_TTL_S = 20.0
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _load_pkg():
    """Import the package that sits one folder up under a private name (no sys.path changes)."""
    if _PKG in sys.modules:
        return sys.modules[_PKG]
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(_PKG, root / "_pkg.py", submodule_search_locations=[str(root)])
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_PKG] = mod
    spec.loader.exec_module(mod)
    return mod


def _mod(name: str):
    _load_pkg()
    return importlib.import_module(f"{_PKG}.{name}")


_lock = threading.Lock()
_cache: dict = {"at": 0.0, "body": None}


def _json(body: str, status: int = 200) -> Response:
    return Response(content=body, status_code=status, media_type="application/json", headers=_HEADERS)


@router.get("/health")
async def health():
    core = _mod("core")
    return _json(core.guard_payload({"ok": True, "schema": core.SCHEMA_VERSION, "read_only": True}))


@router.get("/snapshot")
def snapshot(refresh: bool = Query(False)):
    core, adapter = _mod("core"), _mod("hermes_adapter")
    now = time.time()
    with _lock:
        age = now - _cache["at"]
        if _cache["body"] is not None and (age < _MIN_REFRESH_S or (not refresh and age < _CACHE_TTL_S)):
            return _json(_cache["body"])
        try:
            payload, secrets = adapter.build_snapshot(now)
            body = core.guard_payload(payload, secrets)
        except core.LeakError:
            log.error("cmi_console: snapshot blocked by the leak guard")
            return _json('{"error":"leak_guard"}', 500)
        except Exception as exc:  # never leak a traceback to the UI
            log.exception("cmi_console: snapshot failed")
            return _json(core.guard_payload({"error": "snapshot_failed", "kind": type(exc).__name__}), 500)
        _cache.update(at=now, body=body)
        return _json(body)


class _FirstIn(BaseModel):
    tag: str
    intent: str = ""  # must equal "make-first": rejects blind/forged posts
    retry: bool = False  # explicit manual retry clears only the chosen account's local backoff


@router.post("/accounts/first")
def accounts_first(body: _FirstIn):
    prefs, adapter = _mod("account_prefs"), _mod("hermes_adapter")
    if body.intent != "make-first":
        return _json('{"error":"bad_intent"}', 400)
    try:
        with _lock:
            res = adapter.make_first(body.tag, retry=body.retry)
            _cache.update(at=0.0, body=None)  # next snapshot must show the new order
    except prefs.PrefsError as exc:
        return _json('{"error":"invalid","detail":"%s"}' % str(exc).replace('"', ""), 400)
    except Exception:
        log.exception("cmi_console: make_first failed")
        return _json('{"error":"failed"}', 500)
    return _json(_mod("core").guard_payload(res))


class _NameIn(BaseModel):
    key: str
    tag: str
    intent: str = ""  # must equal "name-account"


@router.post("/accounts/name")
def accounts_name(body: _NameIn):
    prefs = _mod("account_prefs")
    if body.intent != "name-account":
        return _json('{"error":"bad_intent"}', 400)
    try:
        with _lock:
            prefs.set_tags({body.key: body.tag})
            _cache.update(at=0.0, body=None)
    except prefs.PrefsError as exc:
        return _json('{"error":"invalid","detail":"%s"}' % str(exc).replace('"', ""), 400)
    return _json('{"ok":true}')


class _ReconIn(BaseModel):
    tag: str
    keys: list[str] = []
    intent: str = ""  # must equal "reconnect"


@router.post("/accounts/reconnect")
def accounts_reconnect(body: _ReconIn):
    prefs = _mod("account_prefs")
    if body.intent != "reconnect":
        return _json('{"error":"bad_intent"}', 400)
    try:
        with _lock:
            prefs.start_reconnect(body.tag, body.keys[:50])
    except prefs.PrefsError:
        return _json('{"error":"invalid"}', 400)
    return _json('{"ok":true,"command":"hermes auth add anthropic --type oauth"}')


class _AddBeginIn(BaseModel):
    tag: str
    name: str = ""
    intent: str = ""  # must equal "add-account"


@router.post("/accounts/add/begin")
def accounts_add_begin(body: _AddBeginIn):
    prefs, adapter, login = _mod("account_prefs"), _mod("hermes_adapter"), _mod("login")
    if body.intent != "add-account":
        return _json('{"error":"bad_intent"}', 400)
    try:
        res = adapter.add_account_begin(body.tag, body.name)  # own lock: must not block /snapshot while Hermes works
    except (prefs.PrefsError, login.LoginError):
        return _json('{"error":"invalid"}', 400)
    except Exception:
        log.exception("cmi_console: add_account_begin failed")
        return _json('{"error":"failed"}', 500)
    return _json(_mod("core").guard_payload(res))


class _AddFinishIn(BaseModel):
    id: str
    code: str
    intent: str = ""  # must equal "add-account-code"


@router.post("/accounts/add/finish")
def accounts_add_finish(body: _AddFinishIn):
    prefs, adapter, login = _mod("account_prefs"), _mod("hermes_adapter"), _mod("login")
    if body.intent != "add-account-code":
        return _json('{"error":"bad_intent"}', 400)
    try:
        res = adapter.add_account_finish(body.id, body.code)
        with _lock:
            _cache.update(at=0.0, body=None)
    except (prefs.PrefsError, login.LoginError):
        return _json('{"error":"invalid"}', 400)
    except Exception:
        log.exception("cmi_console: add_account_finish failed")
        return _json('{"error":"failed"}', 500)
    return _json(_mod("core").guard_payload(res))


class _ProvBeginIn(BaseModel):
    provider: str
    label: str = ""
    intent: str = ""  # must equal "add-provider"


class _ProvStatusIn(BaseModel):
    id: str
    intent: str = ""  # must equal "add-provider-status"


class _ProvKeyIn(BaseModel):
    provider: str
    key: str
    label: str = ""
    intent: str = ""  # must equal "add-provider-key"


def _prov_call(intent: str, want: str, fn):
    prefs, login = _mod("account_prefs"), _mod("login")
    if intent != want:
        return _json('{"error":"bad_intent"}', 400)
    try:
        res = fn(_mod("hermes_adapter"))
        with _lock:
            _cache.update(at=0.0, body=None)
    except (prefs.PrefsError, login.LoginError):
        return _json('{"error":"invalid"}', 400)
    except Exception:
        log.exception("cmi_console: %s failed", want)
        return _json('{"error":"failed"}', 500)
    return _json(_mod("core").guard_payload(res))


class _OpenIn(BaseModel):
    id: str
    intent: str = ""  # must equal "open-login"


@router.post("/login/open")
def login_open(body: _OpenIn):
    return _prov_call(body.intent, "open-login", lambda a: a.open_login(body.id))


@router.post("/providers/add/begin")
def providers_add_begin(body: _ProvBeginIn):
    return _prov_call(body.intent, "add-provider", lambda a: a.add_provider_begin(body.provider, body.label))


@router.post("/providers/add/status")
def providers_add_status(body: _ProvStatusIn):
    return _prov_call(body.intent, "add-provider-status", lambda a: a.add_provider_status(body.id))


@router.post("/providers/add/key")
def providers_add_key(body: _ProvKeyIn):
    return _prov_call(body.intent, "add-provider-key", lambda a: a.add_provider_key(body.provider, body.key, body.label))


class _PriceIn(BaseModel):
    tag: str
    eur: float
    intent: str = ""  # must equal "set-price"


@router.post("/accounts/price")
def accounts_price(body: _PriceIn):
    prefs = _mod("account_prefs")
    if body.intent != "set-price":
        return _json('{"error":"bad_intent"}', 400)
    try:
        with _lock:
            prefs.set_price(body.tag, body.eur)
            _cache.update(at=0.0, body=None)
    except prefs.PrefsError:
        return _json('{"error":"invalid"}', 400)
    return _json('{"ok":true}')
