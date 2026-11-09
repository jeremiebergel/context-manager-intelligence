"""WorkingSet — decaying relevance index over files (NOT a file store).

Pure-Python deterministic scoring. No vector DB, no retrieval, no persistence
beyond the engine's in-memory state. Scores live in [0, 10].
"""
from __future__ import annotations
import re
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Tuple

# ---- Tunable coefficients -----------------------------------------------
WS_DECAY: float = 0.85
WS_MAX: float = 10.0
WS_PRIORITY_THRESHOLD: float = 3.0

BUMP_READ: float = 3.0
BUMP_WRITE: float = 6.0
BUMP_SEARCH_MATCH: float = 1.0
BUMP_TERMINAL_MENTION: float = 1.0
BUMP_USER_MENTION: float = 5.0
BUMP_IMPORT_EDGE: float = 2.0

FLOOR_USER_MENTION: float = 1.5
FLOOR_USER_MENTION_TURNS: int = 20
FLOOR_MODIFIED: float = 2.0
FLOOR_MODIFIED_TURNS: int = 15
FLOOR_DIRTY: float = 2.0

WS_MAX_FILES: int = 50

# Absolute (/…, ~/…) or relative paths; extensions up to 10 chars (.tsx, .yaml, .example).
_PATH_RE = re.compile(r"(?:~?/)?(?:[\w.\-]+/)*[\w.\-]+\.[A-Za-z0-9]{1,10}\b")
_IMPORT_RE = re.compile(
    r"""(?:from\s+['"]([^'"]+)['"]|import\s+['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"])"""
)


@lru_cache(maxsize=8192)
def _path_tokens(text: str) -> Tuple[str, ...]:
    # History messages are immutable, so the same texts are scanned on every request: memoize.
    return tuple(_PATH_RE.findall(text))


def extract_paths(text: str, *, known: Optional[Iterable[str]] = None) -> List[str]:
    if not text or not isinstance(text, str):
        return []
    tokens = _path_tokens(text)
    if known is None:
        return list(tokens)
    return match_known(tokens, known)



class KnownIndex(dict):
    """basename -> known paths. Build once per request, reuse for every lookup."""

    def __init__(self, known: Iterable[str] = ()) -> None:
        super().__init__()
        for k in known:
            if k:
                self.setdefault(k.rstrip("/").rsplit("/", 1)[-1], []).append(k)


def match_known(tokens: Iterable[str], known: Iterable[str]) -> List[str]:
    """Map path mentions to known files. Users and tools rarely spell a path the same way
    (`/abs/src/app.ts`, `src/app.ts`, `app.ts`), so a mention matches a known file when one is a
    path-suffix of the other. Returns known paths (deduplicated, order of first match).
    Indexed by basename: both sides of a suffix match share it, so each token only compares
    against the few known files with the same name (hot path, called per tool result)."""
    by_base = known if isinstance(known, KnownIndex) else KnownIndex(known)
    out: List[str] = []
    seen = set()
    for t in tokens:
        t = t.lstrip("~").strip()
        if not t:
            continue
        for k in by_base.get(t.rsplit("/", 1)[-1], ()):
            if k in seen:
                continue
            if k == t or k.endswith("/" + t.lstrip("/")) or t.endswith("/" + k.lstrip("/")):
                seen.add(k)
                out.append(k)
    return out

class WorkingSet:
    def __init__(self) -> None:
        self._scores: Dict[str, float] = {}
        self._floors: Dict[str, Tuple[float, int]] = {}
        self._dirty: set = set()
        self._import_edges: set = set()
        self._recent_reads: Dict[str, str] = {}
        self._read_order: List[str] = []
        self._turn: int = 0

    def reset(self) -> None:
        self.__init__()

    def on_turn_complete(self) -> None:
        self._turn += 1
        for p in list(self._scores):
            self._scores[p] = self._scores[p] * WS_DECAY
            if self._scores[p] < 0.05:
                del self._scores[p]
        for p in list(self._floors):
            floor, left = self._floors[p]
            left -= 1
            if left < 0:
                del self._floors[p]
            else:
                self._floors[p] = (floor, left)

    def user_mentioned_paths(self) -> set:
        return {
            p for p, (floor, left) in self._floors.items()
            if left > 0 and floor >= FLOOR_USER_MENTION
        }

    def bump(self, path: str, amount: float) -> None:
        if not path:
            return
        s = self._scores.get(path, 0.0) + amount
        self._scores[path] = min(WS_MAX, s)

    def note_read(self, path: str, content: str = "") -> None:
        self.bump(path, BUMP_READ)
        if content:
            self._recent_reads[path] = content[:20000]
            if path in self._read_order:
                self._read_order.remove(path)
            self._read_order.append(path)
            del self._read_order[:-5]
            self._propagate_imports(path)

    def note_write(self, path: str) -> None:
        self.bump(path, BUMP_WRITE)
        self._floors[path] = (FLOOR_MODIFIED, FLOOR_MODIFIED_TURNS)

    def note_search_match(self, path: str) -> None:
        self.bump(path, BUMP_SEARCH_MATCH)

    def note_terminal_mention(self, path: str) -> None:
        self.bump(path, BUMP_TERMINAL_MENTION)

    def note_user_mention(self, paths: Iterable[str]) -> None:
        for p in paths:
            self.bump(p, BUMP_USER_MENTION)
            self._floors[p] = (FLOOR_USER_MENTION, FLOOR_USER_MENTION_TURNS)

    def note_dirty(self, paths: Iterable[str]) -> None:
        self._dirty = set(paths)

    def _propagate_imports(self, importer: str) -> None:
        content = self._recent_reads.get(importer, "")
        if not content:
            return
        for m in _IMPORT_RE.finditer(content):
            target = next((g for g in m.groups() if g), None)
            if not target:
                continue
            key = (importer, target)
            if key in self._import_edges:
                continue
            self._import_edges.add(key)
            self.bump(target, BUMP_IMPORT_EDGE)

    def score(self, path: str) -> float:
        base = self._scores.get(path, 0.0)
        floor, _ = self._floors.get(path, (0.0, 0))
        if path in self._dirty:
            floor = max(floor, FLOOR_DIRTY)
        return max(base, floor)

    def is_prioritary(self, path: str) -> bool:
        return self.score(path) >= WS_PRIORITY_THRESHOLD

    def prioritary_files(self) -> List[str]:
        return [p for p in self._scores if self.is_prioritary(p)]

    def known_files(self) -> List[str]:
        return list(self._scores.keys())

    def top(self, n: int = 10) -> List[Tuple[str, float]]:
        ranked = sorted(
            ((p, self.score(p)) for p in self._scores),
            key=lambda kv: kv[1],
            reverse=True,
        )
        return ranked[:n]

    def render_index(self, max_lines: int = 30) -> str:
        lines = ["[WORKING SET — active files index]"]
        for path, score in self.top(max_lines - 1):
            tag = "*" if self.is_prioritary(path) else " "
            lines.append(f"{tag} {path} (relevance {score:.1f})")
        return "\n".join(lines)

    def state_size(self) -> int:
        return len(self._scores) + len(self._floors) + len(self._import_edges)
