"""CMI review — pure core (no Hermes imports).

After a turn that changed code, a SECOND model reads exactly what changed (the diffs the agent's
own tools produced) next to the user's request, and lists real bugs. The findings are injected
into the next turn so the working model verifies and fixes them. Every review is logged to a
ledger (writer model, reviewer model, task category, findings) — the data a future router needs.

Why a second model whatever the first one is: on real multi-turn coding benchmarks the gap
between top models is close to a coin flip per task, so the "strong" model is wrong often too.
Fresh eyes on the diff + the spec catch what the author's context hides (in one real
Android TV change: a broken nav stack, an unfiltered spoiler and an unwired button — all visible in the diff).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import PurePosixPath
from typing import Any, Iterable, Optional

CODE_EXT = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".kt", ".kts", ".java", ".swift", ".go",
    ".rs", ".rb", ".php", ".c", ".cc", ".cpp", ".h", ".hpp", ".cs", ".vue", ".svelte", ".sql",
    ".sh", ".bash", ".zsh", ".css", ".scss", ".html", ".gradle", ".dart", ".lua", ".scala",
}
# Never review the agent's own scratch work.
SKIP_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/")
SKIP_PARTS = ("/.hermes/cache/", "/node_modules/", "/.git/", "/build/", "/dist/")

MAX_FILE_CHARS = 12_000
MAX_TOTAL_CHARS = 60_000
MAX_REQUEST_CHARS = 4_000
MAX_CLAIM_CHARS = 3_000

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "min_changed_lines": 8,
    "max_per_day": 40,
    # Skip categories whose reviews rarely find anything real (test-only diffs), and ask for a
    # bigger diff in categories where the ledger shows a low yield (2026-11-04: 104 reviews).
    "skip_categories": ["tests"],
    "category_min_lines": {"web-ui": 15, "shell": 12},
    "wait_seconds": 30,       # how long the next turn waits for a review still running
    "ttl_seconds": 6 * 3600,  # an undelivered review older than this is dropped
    "timeout": 180,
    # writer model glob -> [provider, model]. First match wins; never the writer itself.
    "reviewers": [
        ["claude-opus*", ["anthropic", "claude-sonnet-5"]],
        ["*", ["anthropic", "claude-opus-5-5"]],
    ],
    "fallback_reviewer": ["anthropic", "claude-sonnet-5"],
}

SEVERITIES = ("high", "medium", "low")


@dataclass
class Change:
    path: str
    tool: str
    text: str          # unified diff, or the full content for a write_file
    lines: int         # changed lines (+/-) or written lines


@dataclass
class Turn:
    request: str
    claim: str
    changes: list[Change] = field(default_factory=list)

    @property
    def changed_lines(self) -> int:
        return sum(c.lines for c in self.changes)

    @property
    def paths(self) -> list[str]:
        seen: list[str] = []
        for c in self.changes:
            if c.path not in seen:
                seen.append(c.path)
        return seen


def merge_config(user: Optional[dict]) -> dict:
    cfg = dict(DEFAULTS)
    if isinstance(user, dict):
        for k, v in user.items():
            if k in cfg and v is not None:
                cfg[k] = v
    return cfg


def is_code_path(path: str) -> bool:
    if not path:
        return False
    p = path.replace("\\", "/")
    if p.startswith(SKIP_PREFIXES) or any(s in p for s in SKIP_PARTS):
        return False
    return PurePosixPath(p).suffix.lower() in CODE_EXT


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") in (None, "text", "input_text")
        )
    return ""


def _json(raw: Any) -> Any:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _ok(result: Any) -> tuple[bool, dict]:
    data = _json(result)
    if not isinstance(data, dict):
        return False, {}
    if data.get("success") is False or data.get("error"):
        return False, data
    return True, data


def _diff_lines(diff: str) -> int:
    n = 0
    for line in diff.splitlines():
        if (line.startswith("+") and not line.startswith("+++")) or (line.startswith("-") and not line.startswith("---")):
            n += 1
    return n


def _is_real_user(msg: dict) -> bool:
    if msg.get("role") != "user":
        return False
    text = _text(msg.get("content"))
    return bool(text.strip())


def extract_turn(messages: list[dict]) -> Optional[Turn]:
    """Changes made by successful write_file / patch calls since the last user message."""
    if not messages:
        return None
    start = None
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], dict) and _is_real_user(messages[i]):
            start = i
            break
    if start is None:
        return None
    request = _text(messages[start].get("content"))
    results: dict[str, Any] = {}
    for m in messages[start + 1:]:
        if isinstance(m, dict) and m.get("role") == "tool" and m.get("tool_call_id"):
            results[m["tool_call_id"]] = m.get("content")
    turn = Turn(request=request[:MAX_REQUEST_CHARS], claim="")
    for m in messages[start + 1:]:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        claim = _text(m.get("content"))
        if claim.strip():
            turn.claim = claim[-MAX_CLAIM_CHARS:]
        for call in m.get("tool_calls") or []:
            fn = (call or {}).get("function") or {}
            name = fn.get("name") or ""
            if name not in ("write_file", "patch"):
                continue
            args = _json(fn.get("arguments")) or {}
            ok, data = _ok(results.get(call.get("id")))
            if not ok:
                continue
            path = str(args.get("path") or data.get("resolved_path") or "")
            if name == "patch":
                diff = str(data.get("diff") or "")
                if not diff:
                    continue
                # A multi-file V4A patch has no single path: take the first header.
                if not path:
                    hdr = re.search(r"^\+\+\+ b/(.+)$", diff, re.M)
                    path = hdr.group(1) if hdr else ""
                if not is_code_path(path):
                    continue
                turn.changes.append(Change(path, name, diff[:MAX_FILE_CHARS], _diff_lines(diff)))
            else:
                content = str(args.get("content") or "")
                if not content or not is_code_path(path):
                    continue
                turn.changes.append(Change(path, name, content[:MAX_FILE_CHARS], content.count("\n") + 1))
    return turn


def drop_secret_changes(turn: Turn, contains_secret) -> list[str]:
    """Never send a change holding a credential to another provider. Returns dropped paths."""
    dropped = [c.path for c in turn.changes if contains_secret(c.text)]
    if dropped:
        turn.changes = [c for c in turn.changes if c.path not in dropped]
    return dropped


def min_lines_for(category: str, cfg: dict) -> int:
    base = int(cfg.get("min_changed_lines", 8))
    per = cfg.get("category_min_lines") or {}
    try:
        return max(base, int(per.get(category, base)))
    except (TypeError, ValueError):
        return base


def should_review(turn: Optional[Turn], cfg: dict) -> bool:
    if not (turn and turn.changes):
        return False
    category = categorize(turn.paths)
    if category in (cfg.get("skip_categories") or []):
        return False
    return turn.changed_lines >= min_lines_for(category, cfg)


def finding_files(review: dict) -> list[str]:
    """Files named by the actionable findings of a delivered review."""
    return sorted({str(f.get("file")) for f in actionable(review.get("findings") or []) if f.get("file")})


def followup(files: Iterable[str], turn: Optional[Turn]) -> dict:
    """Did the turn after a delivered review touch the files it flagged? (proxy for "acted on")."""
    touched = [c.path for c in turn.changes] if turn else []
    hit = [f for f in files if any(t == f or t.endswith("/" + f.lstrip("./")) or f.endswith("/" + t) for t in touched)]
    return {"flagged": len(list(files)), "addressed_files": len(hit), "addressed": bool(hit)}


def family(model: str) -> str:
    m = (model or "").lower().split("/")[-1]
    for fam in ("opus", "sonnet", "haiku", "glm", "qwen", "gpt", "gemini", "grok", "deepseek", "kimi", "mistral", "llama"):
        if fam in m:
            return fam
    return m


def pick_reviewer(writer_model: str, cfg: dict) -> tuple[str, str]:
    w = (writer_model or "").lower().split("/")[-1]
    for rule in cfg.get("reviewers") or []:
        try:
            pattern, (provider, model) = rule[0], rule[1]
        except (TypeError, ValueError, IndexError):
            continue
        if fnmatch(w, str(pattern).lower()) and str(model).lower() != w:
            return str(provider), str(model)
    provider, model = cfg.get("fallback_reviewer") or DEFAULTS["fallback_reviewer"]
    return str(provider), str(model)


_CATEGORY_RULES = [
    ("android-ui", lambda p: p.endswith((".kt", ".kts")) and any(s in p.lower() for s in ("screen", "ui/", "compose", "view", "nav"))),
    ("android", lambda p: p.endswith((".kt", ".kts", ".java", ".gradle"))),
    ("ios", lambda p: p.endswith(".swift")),
    ("web-ui", lambda p: p.endswith((".tsx", ".jsx", ".vue", ".svelte", ".css", ".scss", ".html"))),
    ("tests", lambda p: any(s in p.lower() for s in (".spec.", ".test.", "/tests/", "/test_", "_test."))),
    ("backend", lambda p: p.endswith((".ts", ".js", ".mjs", ".cjs", ".go", ".rs", ".rb", ".php", ".cs", ".java", ".scala"))),
    ("python", lambda p: p.endswith(".py")),
    ("sql", lambda p: p.endswith(".sql")),
    ("shell", lambda p: p.endswith((".sh", ".bash", ".zsh"))),
]


def categorize(paths: Iterable[str]) -> str:
    """Coarse task category from the touched files (the ledger's routing key)."""
    votes: dict[str, int] = {}
    for p in paths:
        for name, rule in _CATEGORY_RULES:
            if rule(p):
                votes[name] = votes.get(name, 0) + 1
                break
        else:
            votes["other"] = votes.get("other", 0) + 1
    if not votes:
        return "other"
    return max(votes.items(), key=lambda kv: (kv[1], kv[0] != "tests"))[0]


def project_of(paths: Iterable[str]) -> str:
    for p in paths:
        m = re.search(r"/Projects/([^/]+)/", p)
        if m:
            return m.group(1)
    return ""


SYSTEM_PROMPT = """You are a senior code reviewer. Another AI coding agent just made the changes below to satisfy the user's request.
Your job: find REAL defects the change introduces or leaves — bugs, broken state/navigation, wrong API use, crashes, data loss, security holes, race conditions, and any place where the code does NOT do what the user asked (missed requirement, violated rule in the request).
Rules:
- The request tells you the intent, not that the code is correct: report real defects even when the user dictated the code or asked not to change it (the user still wants to know).
- Only report problems you can point to in the shown code. No style nits, no naming, no "consider adding tests", no speculation about code you cannot see.
- If something depends on code you cannot see, report it only if the visible code is clearly wrong; otherwise skip it.
- Fewer, certain findings beat many guesses. An empty list is a good answer when the change is sound.
Answer with ONE JSON object and nothing else:
{"verdict": "ok" | "issues", "findings": [{"severity": "high" | "medium" | "low", "file": "<path>", "where": "<function / line hint>", "problem": "<what is wrong, one or two sentences>", "fix": "<concrete fix>"}]}
high = breaks behaviour or the user's explicit requirement; medium = real bug in an edge case; low = minor but real."""


def build_messages(turn: Turn) -> list[dict]:
    parts = [f"## User request\n{turn.request.strip()}"]
    if turn.claim.strip():
        parts.append(f"## What the agent says it did\n{turn.claim.strip()}")
    budget = MAX_TOTAL_CHARS
    blocks = []
    for c in turn.changes:
        kind = "diff" if c.tool == "patch" else "full file written"
        block = f"### {c.path} ({kind})\n```\n{c.text}\n```"
        if len(block) > budget:
            blocks.append(f"### {c.path} ({kind}) — omitted, review budget exhausted")
            continue
        budget -= len(block)
        blocks.append(block)
    parts.append("## Changes\n" + "\n\n".join(blocks))
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "\n\n".join(parts)}]


def parse_review(text: str) -> Optional[dict]:
    """Tolerant JSON extraction. None when the reviewer answer is unusable."""
    if not text:
        return None
    raw = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.S)
    if fence:
        raw = fence.group(1)
    else:
        a, b = raw.find("{"), raw.rfind("}")
        if a < 0 or b <= a:
            return None
        raw = raw[a:b + 1]
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    findings = []
    for f in data.get("findings") or []:
        if not isinstance(f, dict) or not str(f.get("problem", "")).strip():
            continue
        sev = str(f.get("severity", "low")).lower()
        findings.append({
            "severity": sev if sev in SEVERITIES else "low",
            "file": str(f.get("file", ""))[:300],
            "where": str(f.get("where", ""))[:200],
            "problem": str(f.get("problem", ""))[:800],
            "fix": str(f.get("fix", ""))[:800],
        })
    order = {s: i for i, s in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: order[f["severity"]])
    return {"verdict": "issues" if findings else "ok", "findings": findings}


def counts(findings: list[dict]) -> dict[str, int]:
    return {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}


def actionable(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["severity"] in ("high", "medium")]


def format_injection(review: dict) -> str:
    """Context injected at the start of the next turn (only when something actionable was found)."""
    items = actionable(review.get("findings") or [])
    if not items:
        return ""
    lines = [
        f"[CMI review] The code you changed in the previous turn ({len(review.get('paths') or [])} file(s), "
        f"written by {review.get('writer') or 'the agent'}) was re-read by a second model ({review.get('reviewer')}). "
        "It reported the problems below. They are NOT verified: check each one in the real code before acting, "
        "fix the ones that are real, and discard false positives. Handle the user's new message too. "
        "Tell the user in one or two lines what the review found and what you fixed or rejected.",
    ]
    for i, f in enumerate(items, 1):
        where = f" ({f['where']})" if f.get("where") else ""
        lines.append(f"{i}. [{f['severity']}] {f['file']}{where}: {f['problem']}")
        if f.get("fix"):
            lines.append(f"   fix: {f['fix']}")
    return "\n".join(lines)



def fr() -> bool:
    """English by default; French with CMI_LANG=fr (opt-in)."""
    return os.environ.get("CMI_LANG", "").lower().startswith("fr")

def ledger_summary(rows: list[dict], days: int = 30, now: Optional[float] = None) -> str:
    """Per (writer, category) totals: how often each model's work needed a fix."""
    import time as _t
    now = now or _t.time()
    rows = [r for r in rows if r.get("kind") == "review" and now - float(r.get("t", 0)) <= days * 86400]
    if not rows:
        return f"CMI review : aucune relecture sur {days} j." if fr() else f"CMI review: no reviews over {days} d."
    agg: dict[tuple[str, str], list[int]] = {}
    for r in rows:
        key = (str(r.get("writer", "?")), str(r.get("category", "other")))
        a = agg.setdefault(key, [0, 0, 0])
        a[0] += 1
        c = r.get("counts") or {}
        a[1] += 1 if (c.get("high", 0) + c.get("medium", 0)) else 0
        a[2] += int(c.get("high", 0))
    out = ([f"CMI review — {len(rows)} relecture(s) sur {days} j", "", "| auteur | catégorie | relectures | avec problème | high |"]
           if fr() else [f"CMI review — {len(rows)} review(s) over {days} d", "", "| writer | category | reviews | with issues | high |"])
    out.append("|---|---|---|---|---|")
    for (w, cat), (n, bad, high) in sorted(agg.items(), key=lambda kv: -kv[1][0]):
        out.append(f"| {w} | {cat} | {n} | {bad} ({round(100 * bad / n)} %) | {high} |")
    last = rows[-1]
    c = last.get("counts") or {}
    out += ["", (f"Dernière : {last.get('project') or '?'} · {last.get('category')} · {last.get('writer')} relu par {last.get('reviewer')} → "
                 if fr() else f"Last: {last.get('project') or '?'} · {last.get('category')} · {last.get('writer')} reviewed by {last.get('reviewer')} → ")
            + f"{c.get('high', 0)} high, {c.get('medium', 0)} medium, {c.get('low', 0)} low"]
    return "\n".join(out)
