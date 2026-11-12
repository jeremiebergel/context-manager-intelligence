"""Recall store: every original the engine takes out of a request, kept verbatim.

Degrading context is only safe if it is reversible. Each original (tool result, old
write_file/patch payload, narration) is written once, content-addressed, and its key
(``cmi:<hex>``) goes into the stub the model sees; ``cmi_recall`` (or reading the file) returns
the exact bytes. Content addressing makes writes idempotent and keys stable across sessions,
compactions, restarts and model switches. No LLM, no network, no dependency: plain files.

Integrity: a key is the shortest sha256 prefix (12 hex minimum) whose file holds exactly this
content, so a prefix collision can never return someone else's bytes.
Privacy: originals can contain whatever tools printed, so the directory is 0700 and files 0600,
and content holding a credential (see secrets.py) is refused: it is never written to disk.
Growth: originals untouched for RETENTION_DAYS are pruned, and the store is capped at MAX_BYTES.
"""
from __future__ import annotations

from . import _fs

import base64
import binascii
import hashlib
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

from .secrets import contains_secret, guard_enabled

KEY_MIN = 12
KEY_RE = re.compile(r"cmi:([0-9a-f]{%d,64})" % KEY_MIN)
_KEY_FULL = re.compile(r"[0-9a-f]{%d,64}" % KEY_MIN)
RETENTION_DAYS = 30
_IMG_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/gif": "gif", "image/webp": "webp"}
_SUFFIXES = {".txt"} | {"." + e for e in _IMG_EXT.values()}
MAX_BYTES = 512 * 1024 * 1024
# Re-check the size cap after this many bytes written by one process, not only at session start.
PRUNE_EVERY_BYTES = 32 * 1024 * 1024
# A live original's mtime is refreshed at most this often (retention clock).
TOUCH_EVERY_S = 86400


class RecallStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root: Optional[Path] = None
        self._written: dict = {}   # sha256 -> (key, last touch), for this process
        self._refused: set = set()  # sha256 of content holding a credential
        self._since_prune = 0
        if root:
            self.set_root(root)

    def set_root(self, root: Path) -> None:
        self.root = Path(root)
        self._written = {}

    def _ensure_dir(self) -> None:
        if self.root.is_symlink():
            # Originals may hold secrets: never follow a planted link out of HERMES_HOME.
            raise OSError("cmi_store is a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not _fs.mode_is(self.root.stat(), 0o700):
            _fs.chmod_private(self.root, 0o700)
            self._tighten_files()

    def _tighten_files(self) -> None:
        """Stores created by older versions left files 0644."""
        for p in self._files():
            try:
                if not _fs.IS_WINDOWS and not p.is_symlink() and (p.stat().st_mode & 0o077):
                    os.chmod(p, 0o600)
            except OSError:
                pass

    def _files(self):
        return (p for p in self.root.glob("*") if p.suffix in _SUFFIXES)

    def put_image(self, data_b64: str, media_type: str) -> Optional[str]:
        """Store a base64 image as a real image file (so any file-viewing tool can show it again)
        and return its key; None when it cannot be stored. Callers then keep the image."""
        ext = _IMG_EXT.get((media_type or "").lower())
        if self.root is None or not ext or not isinstance(data_b64, str) or not data_b64:
            return None
        try:
            data = base64.b64decode(data_b64, validate=True)
        except (ValueError, binascii.Error):
            return None
        digest = hashlib.sha256(data).hexdigest()
        hit = self._written.get(digest)
        if hit and (self.root / f"{hit[0]}.{ext}").exists():
            return hit[0]
        try:
            self._ensure_dir()
            for n in range(KEY_MIN, 65, 4):
                key = digest[:n]
                path = self.root / f"{key}.{ext}"
                if not path.exists():
                    self._write(path, data)
                    self._since_prune += len(data)
                    break
                if self._same(path, data):
                    os.utime(path)
                    break
            else:
                return None
        except OSError:
            return None
        self._written[digest] = (key, time.time())
        return key

    def image_path(self, key: str) -> Optional[Path]:
        m = KEY_RE.search(key or "")
        key = m.group(1) if m else (key or "").strip()
        if self.root is None or not _KEY_FULL.fullmatch(key):
            return None
        for ext in sorted(set(_IMG_EXT.values())):
            p = self.root / f"{key}.{ext}"
            if p.is_file() and not p.is_symlink():
                return p
        return None

    def put(self, content: str) -> Optional[str]:
        """Store ``content`` and return its key; None when it cannot be stored (no root, disk
        error, or it holds a credential). Callers must then keep the original in the request."""
        if self.root is None or not content:
            return None
        data = content.encode("utf-8", "surrogatepass")
        digest = hashlib.sha256(data).hexdigest()
        hit = self._written.get(digest)
        if hit:
            key, touched = hit
            path = self.root / f"{key}.txt"
            try:
                # Another process may have pruned it; the stub must never point to nothing.
                if time.time() - touched < TOUCH_EVERY_S and path.exists():
                    return key
                if self._same(path, data):
                    os.utime(path)
                    self._written[digest] = (key, time.time())
                    return key
            except OSError:
                pass
        if digest in self._refused:
            return None
        if guard_enabled() and contains_secret(content):
            self._refused.add(digest)
            return None
        try:
            self._ensure_dir()
            for n in range(KEY_MIN, 65, 4):
                key = digest[:n]
                path = self.root / f"{key}.txt"
                if not path.exists():
                    self._write(path, data)
                    self._since_prune += len(data)
                    break
                if self._same_settled(path, data):
                    os.utime(path)  # keep live originals out of the retention window
                    break
            else:
                return None
        except OSError:
            return None
        self._written[digest] = (key, time.time())
        if self._since_prune >= PRUNE_EVERY_BYTES:
            self._since_prune = 0
            self.prune(keep=path)
        return key

    @staticmethod
    def _same(path: Path, data: bytes) -> bool:
        return (not path.is_symlink() and path.stat().st_size == len(data)
                and path.read_bytes() == data)

    @classmethod
    def _same_settled(cls, path: Path, data: bytes) -> bool:
        """``_same``, retried while Windows reports the file busy: another writer is replacing it
        with the same bytes right now (content-addressed). Elsewhere it is ``_same``."""
        for attempt in range(10):
            try:
                return cls._same(path, data)
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.01 * (attempt + 1))
        return False

    def _write(self, path: Path, data: bytes) -> None:
        # Unique per writer: sessions in one process (threads) and several processes may store
        # the same content at once; a shared tmp name interleaved their bytes.
        tmp = path.with_name(f".{path.stem}.{os.getpid()}.{threading.get_ident()}.{os.urandom(4).hex()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _fs.O_NOFOLLOW | _fs.O_BINARY, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        for attempt in range(10):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                # Windows refuses to replace a file another writer is replacing right now. If the same
                # bytes are already there the work is done; otherwise back off and retry.
                try:
                    if self._same(path, data):
                        os.unlink(tmp)
                        return
                except OSError:
                    pass
                if attempt == 9:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
                time.sleep(0.01 * (attempt + 1))

    def get(self, key: str) -> Optional[str]:
        m = KEY_RE.search(key or "")
        key = m.group(1) if m else (key or "").strip()
        if self.root is None or not _KEY_FULL.fullmatch(key):
            return None
        path = self.root / f"{key}.txt"
        if _fs.IS_WINDOWS and path.is_symlink():  # no O_NOFOLLOW there: refuse a planted link explicitly
            return None
        try:
            fd = os.open(path, os.O_RDONLY | _fs.O_NOFOLLOW | _fs.O_BINARY)
        except OSError:
            return None
        try:
            with os.fdopen(fd, "rb") as f:
                return f.read().decode("utf-8", "surrogatepass")
        except (OSError, UnicodeDecodeError):
            return None

    def path_of(self, key: str) -> Optional[Path]:
        return self.root / f"{key}.txt" if self.root else None

    def prune(self, days: int = RETENTION_DAYS, max_bytes: int = MAX_BYTES, keep: Optional[Path] = None) -> int:
        """Drop originals untouched for ``days``, then the oldest ones beyond ``max_bytes``."""
        if self.root is None or not self.root.is_dir() or self.root.is_symlink():
            return 0
        if not _fs.mode_is(self.root.stat(), 0o700):
            _fs.chmod_private(self.root, 0o700)
            self._tighten_files()
        cutoff = time.time() - days * 86400
        files = []
        for p in self._files():
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((st.st_mtime, st.st_size, p))
        files.sort()
        total = sum(s for _, s, _ in files)
        removed = 0
        for mtime, size, p in files:
            if mtime >= cutoff and total <= max_bytes:
                break
            if p == keep:
                continue
            try:
                p.unlink()
                removed += 1
                total -= size
            except OSError:
                pass
        if removed:
            self._after_removal()
        return removed

    def purge_secrets(self) -> int:
        """Delete stored originals that hold a credential (stores written before the guard)."""
        if self.root is None or not self.root.is_dir() or self.root.is_symlink():
            return 0
        removed = 0
        for p in self.root.glob("*.txt"):
            try:
                if not p.is_symlink() and contains_secret(p.read_text("utf-8", "replace")):
                    p.unlink()
                    removed += 1
            except OSError:
                pass
        if removed:
            self._after_removal(compact=True)
        try:
            from .index import OutputIndex
            idx = self.root.parent / "cmi_index.db"
            if idx.exists():
                removed += OutputIndex(idx).purge_secrets()
        except Exception:
            pass
        return removed

    def _after_removal(self, compact: bool = False) -> None:
        self._written = {d: v for d, v in self._written.items() if (self.root / f"{v[0]}.txt").exists()}
        # Keep the cross-session index in step: a search hit must always be recallable.
        try:
            from .index import OutputIndex
            idx = self.root.parent / "cmi_index.db"
            if idx.exists():
                live = {p.stem for p in self.root.glob("*.txt")}
                ix = OutputIndex(idx)
                con = ix._db()
                if con is not None:
                    stale = [k for (k,) in con.execute("SELECT key FROM docs") if k not in live]
                    ix.forget(stale)
                    if compact:
                        ix.compact()
        except Exception:
            pass
