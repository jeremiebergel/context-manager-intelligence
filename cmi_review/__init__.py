"""CMI review — systematic second-model review of every non-trivial code change.

Hermes adapter (general plugin, hooks):
- ``post_llm_call``: when the finished turn changed code (successful write_file/patch on source
  files, at least ``min_changed_lines``), a background thread sends the diffs + the user's request
  to a reviewer model that is NOT the writer, and stores the findings for this session.
- ``pre_llm_call``: at the next turn of the same session, the high/medium findings are injected
  into the user message (one-shot) so the working model verifies and fixes them.
- Every review is appended to ``$HERMES_HOME/cmi_reviews.jsonl`` (writer, reviewer, category,
  counts) — the ledger that tells which model fails on which kind of task.
- ``/relecture`` shows the ledger; ``/relecture off`` / ``/relecture on`` toggle it.

Config (optional) in config.yaml under ``cmi_review:`` — see core.DEFAULTS.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

try:  # loaded by Hermes as a plugin package
    from . import core
except ImportError:  # pragma: no cover - direct import in tests
    import core  # type: ignore

__version__ = "0.1.0"
logger = logging.getLogger("cmi_review")

_INFLIGHT: dict[str, threading.Event] = {}
_LOCK = threading.Lock()
_AWAIT: dict[str, list] = {}  # session -> files flagged by the review just delivered


# ---------------------------------------------------------------- environment helpers
def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _state_dir() -> Path:
    d = _home() / "cmi_review"
    (d / "pending").mkdir(parents=True, exist_ok=True)
    return d


def _ledger_path() -> Path:
    return _home() / "cmi_reviews.jsonl"


def _config() -> dict:
    user = None
    try:
        from hermes_cli.config import load_config_readonly
        user = (load_config_readonly() or {}).get("cmi_review")
    except Exception:
        pass
    cfg = core.merge_config(user)
    if (_home() / "cmi_review" / "disabled").exists():
        cfg["enabled"] = False
    return cfg


def _contains_secret(text: str) -> bool:
    try:
        from cmi.secrets import contains_secret  # repo / pip install
        return contains_secret(text)
    except Exception:
        pass
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_cmi_review_secrets", _home() / "plugins" / "cmi" / "secrets.py")
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return bool(mod.contains_secret(text))
    except Exception:
        pass
    # Last resort: refuse anything that looks like a key assignment.
    import re
    return bool(re.search(r"(?i)(api_?key|secret|token|password)\s*[=:]\s*['\"]?[A-Za-z0-9/+_\-.]{16,}", text))


def _append_ledger(row: dict) -> None:
    try:
        line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(_ledger_path(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError as exc:
        logger.warning("cmi_review ledger write failed: %s", exc)


def _read_ledger() -> list[dict]:
    rows = []
    try:
        with open(_ledger_path(), encoding="utf-8") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return rows


def _reviews_today() -> int:
    cutoff = time.time() - 86400
    return sum(1 for r in _read_ledger() if r.get("kind") == "review" and float(r.get("t", 0)) >= cutoff)


def _pending_path(session_id: str) -> Path:
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "_-")[:120] or "unknown"
    return _state_dir() / "pending" / f"{safe}.json"


# ---------------------------------------------------------------- the review itself
def _call_reviewer(provider: str, model: str, messages: list[dict], timeout: float) -> tuple[str, dict]:
    from agent.auxiliary_client import call_llm
    route: dict[str, str] = {}
    resp = call_llm(provider=provider, model=model, messages=messages, max_tokens=4000,
                    temperature=0, timeout=timeout, route_info=route)
    try:
        text = resp.choices[0].message.content or ""
    except (AttributeError, IndexError):
        text = ""
    return text, route


def run_review(session_id: str, writer: str, platform: str, turn: core.Turn, cfg: dict,
               caller=None) -> dict:
    caller = caller or _call_reviewer
    provider, model = core.pick_reviewer(writer, cfg)
    started = time.time()
    row = {
        "kind": "review", "t": round(started), "session": session_id, "platform": platform,
        "writer": writer, "reviewer": model, "reviewer_provider": provider,
        "project": core.project_of(turn.paths), "category": core.categorize(turn.paths),
        "files": turn.paths[:20], "lines": turn.changed_lines,
    }
    review: Optional[dict] = None
    try:
        text, route = caller(provider, model, core.build_messages(turn), float(cfg.get("timeout", 180)))
        if route.get("model") and route["model"] not in ("default", model):
            row["reviewer"] = route["model"]  # an auxiliary fallback answered instead
            row["reviewer_provider"] = route.get("provider", provider)
        review = core.parse_review(text)
        if review is None:
            row["error"] = "unparseable reviewer answer"
    except Exception as exc:  # never break the user's session over a review
        row["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        logger.warning("cmi_review: review failed (%s/%s): %s", provider, model, exc)
    row["secs"] = round(time.time() - started, 1)
    if review is not None:
        row["verdict"] = review["verdict"]
        row["counts"] = core.counts(review["findings"])
        row["findings"] = [{k: f[k] for k in ("severity", "file", "problem")} for f in review["findings"]][:20]
        if core.actionable(review["findings"]):
            pending = dict(review, writer=writer, reviewer=row["reviewer"], paths=turn.paths, t=started)
            tmp = _pending_path(session_id).with_suffix(".tmp")
            tmp.write_text(json.dumps(pending, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, _pending_path(session_id))
    _append_ledger(row)
    return row


def _worker(session_id: str, writer: str, platform: str, turn: core.Turn, cfg: dict, done: threading.Event) -> None:
    try:
        run_review(session_id, writer, platform, turn, cfg)
    finally:
        done.set()
        with _LOCK:
            if _INFLIGHT.get(session_id) is done:
                _INFLIGHT.pop(session_id, None)


# ---------------------------------------------------------------- hooks
def on_post_llm_call(session_id: str = "", model: str = "", platform: str = "",
                     conversation_history: Optional[list] = None, **_: Any) -> None:
    try:
        if not session_id or (platform or "").lower() in ("cron",):
            return
        cfg = _config()
        if not cfg.get("enabled", True):
            return
        turn = core.extract_turn(conversation_history or [])
        with _LOCK:
            awaiting = _AWAIT.pop(session_id, None)
        if awaiting:  # the turn after a delivered review: did the model touch what was flagged?
            _append_ledger({"kind": "followup", "t": round(time.time()), "session": session_id,
                            **core.followup(awaiting, turn)})
        if turn is None:
            return
        dropped = core.drop_secret_changes(turn, _contains_secret)
        if dropped:
            logger.info("cmi_review: %d change(s) not sent (credential detected)", len(dropped))
        if not core.should_review(turn, cfg):
            return
        if _reviews_today() >= int(cfg.get("max_per_day", 40)):
            logger.info("cmi_review: daily cap reached, skipping")
            return
        done = threading.Event()
        with _LOCK:
            _INFLIGHT[session_id] = done
        threading.Thread(target=_worker, args=(session_id, model or "?", platform or "", turn, cfg, done),
                         name=f"cmi-review-{session_id[:12]}", daemon=True).start()
    except Exception as exc:
        logger.warning("cmi_review post_llm_call failed: %s", exc)


def on_pre_llm_call(session_id: str = "", **_: Any) -> Optional[dict]:
    try:
        if not session_id:
            return None
        cfg = _config()
        with _LOCK:
            ev = _INFLIGHT.get(session_id)
        if ev is not None:
            ev.wait(float(cfg.get("wait_seconds", 30)))
        path = _pending_path(session_id)
        if not path.exists():
            return None
        try:
            review = json.loads(path.read_text(encoding="utf-8"))
        finally:
            path.unlink(missing_ok=True)
        if time.time() - float(review.get("t", 0)) > float(cfg.get("ttl_seconds", 21600)):
            return None
        text = core.format_injection(review)
        if not text:
            return None
        _append_ledger({"kind": "delivered", "t": round(time.time()), "session": session_id,
                        "n": len(core.actionable(review.get("findings") or []))})
        with _LOCK:
            _AWAIT[session_id] = core.finding_files(review)
        return {"context": text}
    except Exception as exc:
        logger.warning("cmi_review pre_llm_call failed: %s", exc)
        return None


def _review_command(raw_args: str = "") -> str:
    arg = (raw_args or "").strip().lower()
    flag = _home() / "cmi_review" / "disabled"
    if arg in ("off", "stop", "disable"):
        _state_dir()
        flag.write_text("1")
        return "CMI review désactivée. `/relecture on` pour la réactiver."
    if arg in ("on", "start", "enable"):
        flag.unlink(missing_ok=True)
        return "CMI review activée : chaque changement de code est relu par un second modèle."
    state = "activée" if _config().get("enabled", True) else "DÉSACTIVÉE"
    return f"CMI review {state}.\n\n" + core.ledger_summary(_read_ledger())


def _drain_at_exit() -> None:
    """One-shot surfaces (``hermes chat -q``) exit right after the answer: finish the running
    review so it reaches the ledger and the next ``--resume`` turn (bounded wait)."""
    deadline = time.time() + 60
    with _LOCK:
        events = list(_INFLIGHT.values())
    for ev in events:
        ev.wait(max(0.0, deadline - time.time()))


import atexit  # noqa: E402
atexit.register(_drain_at_exit)


def register(ctx) -> None:
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    register_command = getattr(ctx, "register_command", None)
    if callable(register_command):
        register_command("relecture", _review_command,
                         description="CMI review : relectures automatiques par un second modèle (on/off, stats)")
